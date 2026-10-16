"""Talk to an xTool P2S (V2 firmware) over its TLS WebSocket API on port 28900.

Everything here mirrors what xTool Studio 1.7.30 does (atomm-sharedworker.js
and the editor's FileUploader/FileDownloader). Nothing bypasses the machine's
own safety checks: the lid interlock, the safety key, flame detection and
cooling faults are all enforced by the firmware.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import queue
import threading
import time
from typing import Any, Callable

from . import framing as fr
from .wsclient import WebSocket, WebSocketClosed

log = logging.getLogger("p2s")

PORT = 28900
PING_TRANSACTION_ID = 65510          # fixed id Studio uses for heartbeats
USER_KEY = "bWFrZWJsb2NrLXh0b29s"     # base64("makeblock-xtool"), Studio's default
PACKET_SIZE = 1024 * 1024            # Studio's requested upload packet size
DOWNLOAD_WINDOW = 5 * 1024 * 1024

# File types seen in Studio
FILETYPE_GCODE = 1
FILETYPE_FIRMWARE = 2   # never used by this tool
FILETYPE_CUSTOM = 5     # camera snapshots, logs


class DeviceError(Exception):
    def __init__(self, msg: str, response: Any = None):
        super().__init__(msg)
        self.response = response


class P2S:
    def __init__(self, host: str, user_id: str = "mk-guest", timeout: float = 10.0, port: int = PORT):
        self.host = host
        self.port = port
        self.user_id = user_id
        self.timeout = timeout
        self._tx = itertools.count(1)
        self._pending: dict[int, queue.Queue] = {}
        self._lock = threading.Lock()
        self._file_q: dict[int, queue.Queue] = {}     # per file-transfer channel
        self._event_handlers: list[Callable[[dict], None]] = []
        self._alive = False
        self.instruction: WebSocket | None = None
        self.file: WebSocket | None = None

    # ------------------------------------------------------------------ setup
    def connect(self) -> "P2S":
        sid = int(time.time() * 1000)
        base = f"wss://{self.host}:{self.port}/websocket?id={sid}&function="
        self.instruction = WebSocket(base + "instruction", timeout=self.timeout)
        self.file = WebSocket(base + "file_stream", timeout=self.timeout)
        self._alive = True
        threading.Thread(target=self._reader, args=(self.instruction, "instruction"), daemon=True).start()
        threading.Thread(target=self._reader, args=(self.file, "file"), daemon=True).start()
        # Handshake - the device closes the session if this fails.
        # Exactly what Studio 1.7.30 sends (sendFirstMessageCode).
        self.request("/v1/user/parity", "GET", data={
            "userID": self.user_id, "userKey": USER_KEY})
        threading.Thread(target=self._heartbeat, daemon=True).start()
        return self

    def close(self) -> None:
        self._alive = False
        for ws in (self.instruction, self.file):
            if ws:
                ws.close()

    def __enter__(self):
        return self if self._alive else self.connect()

    def __exit__(self, *exc):
        self.close()

    def on_event(self, fn: Callable[[dict], None]) -> None:
        """Register a callback for unsolicited push events (status, lid, buttons...)."""
        self._event_handlers.append(fn)

    # --------------------------------------------------------------- plumbing
    def _heartbeat(self) -> None:
        last_file = 0.0
        while self._alive:
            time.sleep(3)
            try:
                self._send_json({"type": "request", "method": "GET", "url": "/v1/user/ping",
                                 "params": {}, "data": {}, "timestamp": _now(),
                                 "transactionId": PING_TRANSACTION_ID})
                if time.time() - last_file >= 5:
                    self.file.send_binary(fr.encode(b"\x02", fr.FILE_TRANSFER, True))
                    last_file = time.time()
            except Exception as e:  # connection dropped
                log.warning("heartbeat failed: %s", e)
                self._alive = False

    def _send_json(self, obj: dict) -> None:
        self.instruction.send_binary(fr.encode(json.dumps(obj, separators=(",", ":")), fr.JSON))

    def _reader(self, ws: WebSocket, name: str) -> None:
        reader = fr.FrameReader()
        while self._alive:
            try:
                msg = ws.recv()
            except (WebSocketClosed, OSError) as e:
                if self._alive:
                    log.warning("%s channel closed: %s", name, e)
                self._alive = False
                for q in list(self._pending.values()) + list(self._file_q.values()):
                    q.put(DeviceError("connection closed"))
                return
            for ptype, payload in reader.feed(msg):
                try:
                    self._dispatch(ptype, payload)
                except Exception:
                    log.exception("error handling %s frame", name)

    def _dispatch(self, ptype: int, payload: bytes) -> None:
        if ptype == fr.JSON:
            obj = json.loads(payload.decode("utf-8"))
            if obj.get("transactionId") == PING_TRANSACTION_ID:
                return
            if obj.get("url") == "/v1/filetransfer/finish":          # upload result
                d = obj.get("data") or {}
                q = self._file_q.get(d.get("channel"))
                if q:
                    q.put(("finish", d))
                return
            tid = obj.get("transactionId")
            if tid is None and isinstance(obj.get("data"), dict):
                tid = obj["data"].get("transactionId")
            if obj.get("type") == "response" and tid in self._pending:
                self._pending[tid].put(obj)
                return
            for fn in self._event_handlers:
                fn(obj)
        elif ptype == fr.FILE_TRANSFER:
            if payload == b"\x82":   # file-channel pong
                return
            pkt = fr.parse_file_packet(payload)
            if pkt and pkt[1] in self._file_q:
                self._file_q[pkt[1]].put(pkt)

    # ----------------------------------------------------------------- API
    def request(self, url: str, method: str = "GET", params: dict | None = None,
                data: dict | None = None, timeout: float | None = None) -> Any:
        with self._lock:
            tid = next(self._tx)
            if tid > 65500:
                self._tx = itertools.count(1)
                tid = next(self._tx)
        q: queue.Queue = queue.Queue()
        self._pending[tid] = q
        try:
            self._send_json({"type": "request", "method": method, "url": url,
                             "params": params or {}, "data": data or {},
                             "timestamp": _now(), "transactionId": tid})
            try:
                resp = q.get(timeout=timeout or self.timeout)
            except queue.Empty:
                raise DeviceError(f"timeout waiting for {method} {url}") from None
        finally:
            self._pending.pop(tid, None)
        if isinstance(resp, Exception):
            raise resp
        if resp.get("code", 0) != 0:
            raise DeviceError(f"{method} {url} -> code {resp.get('code')}: {resp.get('msg')}", resp)
        return resp.get("data")

    # convenience wrappers (P2S routes from ha-xtool + Studio bundle)
    def machine_info(self):
        return self.request("/v1/device/machineInfo")

    def status(self):
        return self.request("/v1/device/runningStatus")

    def lid(self):
        return self.request("/v1/peripheral/param", params={"type": "gap"})

    def progress(self):
        return self.request("/v1/processing/progress")

    def set_state(self, action: str):
        if action not in ("start", "pause", "stop"):
            raise ValueError(action)
        return self.request("/v1/processing/state", "PUT", params={"action": action})

    # ------------------------------------------------------------ file transfer
    def _new_channel(self) -> int:
        with self._lock:
            for ch in range(1, 256):
                if ch not in self._file_q:
                    self._file_q[ch] = queue.Queue()
                    return ch
        raise DeviceError("no free file-transfer channel")

    def upload(self, data: bytes, filename: str, filetype: int = FILETYPE_GCODE,
               progress: Callable[[int, int], None] | None = None,
               timeout: float = 60.0) -> None:
        """Device-driven sliding window: device sends FILE_REQUEST(offset, window),
        we answer with FILE_DATA packets, device finishes with /v1/filetransfer/finish."""
        if not data:
            raise ValueError("empty file")
        ch = self._new_channel()
        q = self._file_q[ch]
        try:
            r = self.request("/v1/filetransfer/upload", "PUT", data={
                "filetype": filetype, "filename": filename, "filesize": len(data),
                "digesttype": 1, "digestdata": hashlib.md5(data).hexdigest(),
                "channel": ch, "packetsize": PACKET_SIZE})
            psize = min(PACKET_SIZE, int((r or {}).get("packetsize") or PACKET_SIZE))
            while True:
                try:
                    item = q.get(timeout=timeout)
                except queue.Empty:
                    raise DeviceError("upload timed out waiting for device") from None
                if isinstance(item, Exception):
                    raise item
                if item[0] == "finish":
                    info = item[1]
                    if info.get("code", 0) != 0:
                        raise DeviceError(f"upload rejected: {info}", info)
                    return
                if item[0] != "request":
                    continue
                _, _, offset, window = item
                end = min(offset + window, len(data))
                pos = offset
                while pos < end:
                    n = min(psize, end - pos)
                    pkt = fr.file_data_packet(ch, pos, data[pos:pos + n])
                    # Studio sends file packets with the payload CRC disabled.
                    self.file.send_binary(fr.encode(pkt, fr.FILE_TRANSFER, with_crc=False))
                    pos += n
                    if progress:
                        progress(pos, len(data))
        except BaseException:
            try:
                self.request("/v1/filetransfer/finish", "PUT",
                             data={"code": -9, "message": "File transfer aborted", "channel": ch})
            except Exception:
                pass
            raise
        finally:
            self._file_q.pop(ch, None)

    def download(self, filename: str, filetype: int = FILETYPE_CUSTOM, timeout: float = 30.0) -> bytes:
        ch = self._new_channel()
        q = self._file_q[ch]
        try:
            r = self.request("/v1/filetransfer/download", "PUT", data={
                "filetype": filetype, "filename": filename, "digesttype": 1,
                "channel": ch, "packetsize": PACKET_SIZE})
            size = int(r["filesize"])
            buf = bytearray(size)
            got = 0
            while got < size:
                window = min(DOWNLOAD_WINDOW, size - got)
                start = got
                self.file.send_binary(fr.encode(fr.file_request_packet(ch, start, window),
                                                fr.FILE_TRANSFER, with_crc=False))
                recv = 0
                while recv < window:
                    item = q.get(timeout=timeout)
                    if isinstance(item, Exception):
                        raise item
                    if item[0] != "data":
                        continue
                    _, _, off, chunk = item
                    buf[off:off + len(chunk)] = chunk
                    recv += len(chunk)
                    got += len(chunk)
            if r.get("digestdata") and hashlib.md5(buf).hexdigest() != r["digestdata"]:
                raise DeviceError("download MD5 mismatch")
            self.request("/v1/filetransfer/finish", "PUT",
                         data={"code": 0, "message": "file transfer finish", "channel": ch})
            return bytes(buf)
        finally:
            self._file_q.pop(ch, None)

    def camera_snapshot(self, stream: str = "0") -> bytes:
        r = self.request("/v1/camera/image", "GET", data={"stream": stream}, timeout=15)
        return self.download(r["filename"], FILETYPE_CUSTOM)

    # ------------------------------------------------------------------ jobs
    def send_job(self, gcode: str | bytes, progress=None, fmt: str = "auto") -> tuple[str, str]:
        """Upload a job and load it. The operator then starts it with the machine's button.

        Mirrors the P2S plugin (xcs-ext-p2s 1.5.66, uploadGCode): when the machine
        lists "bin" in machineInfo.gcodeFormat, Studio compiles the job to BGC and
        uploads `tmp.bgcode` with fileType "bin"; otherwise `tmp.gcode` / "txt".
        Then PUT /v1/processing/upload/config {fileType, autoStart:0, taskId:<uuid>}.
        Returns (task_id, format used).
        """
        import uuid
        from . import bgc
        text = gcode.decode() if isinstance(gcode, bytes) else gcode
        if fmt == "auto":
            formats = (self.machine_info() or {}).get("gcodeFormat") or ["txt"]
            fmt = "bin" if "bin" in formats else "txt"
        if fmt == "bin":
            data, name = bgc.encode(text), "tmp.bgcode"
        else:
            data, name = text.encode(), "tmp.gcode"
        task_id = str(uuid.uuid4())
        self.upload(data, name, FILETYPE_GCODE, progress=progress)
        self.request("/v1/processing/upload/config", "PUT",
                     data={"fileType": fmt, "autoStart": 0, "taskId": task_id}, timeout=60)
        return task_id, fmt


def _now() -> int:
    return int(time.time() * 1000)
