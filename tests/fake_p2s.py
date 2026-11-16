"""A tiny fake P2S for offline testing of the client (not a real device!).

Speaks the WebSocket + BABE framing, answers parity/ping/status, and runs the
device side of the sliding-window upload and download protocols.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from p2s import framing as fr  # noqa: E402

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ServerWS:
    def __init__(self, sock):
        self.sock, self.buf, self.lock = sock, b"", threading.Lock()

    def handshake(self) -> str:
        while b"\r\n\r\n" not in self.buf:
            self.buf += self.sock.recv(4096)
        head, self.buf = self.buf.split(b"\r\n\r\n", 1)
        lines = head.decode().split("\r\n")
        path = lines[0].split(" ")[1]
        key = next(l.split(":", 1)[1].strip() for l in lines if l.lower().startswith("sec-websocket-key"))
        acc = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Accept: {acc}\r\n\r\n").encode())
        return path

    def _exact(self, n):
        while len(self.buf) < n:
            c = self.sock.recv(65536)
            if not c:
                raise EOFError
            self.buf += c
        o, self.buf = self.buf[:n], self.buf[n:]
        return o

    def recv(self):
        b1, b2 = self._exact(2)
        n = b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", self._exact(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", self._exact(8))[0]
        assert b2 & 0x80, "client frames must be masked"
        m = self._exact(4)
        p = bytes(b ^ m[i % 4] for i, b in enumerate(self._exact(n))) if n < 4096 else _unmask(self._exact(n), m)
        if b1 & 0x0F == 8:
            raise EOFError
        return p

    def send(self, data: bytes):
        n = len(data)
        h = bytes([0x82]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536
                             else bytes([127]) + struct.pack(">Q", n))
        with self.lock:
            self.sock.sendall(h + data)


def _unmask(d, m):
    k = int.from_bytes((m * (len(d) // 4 + 1))[:len(d)], "big")
    return (int.from_bytes(d, "big") ^ k).to_bytes(len(d), "big")


class FakeP2S:
    def __init__(self, window=300_000, files=None, calib=None):
        self.calib = calib or {}
        self.window = window
        self.uploads: dict[str, bytes] = {}
        self.files = files or {"snap.jpg": b"\xff\xd8fakejpeg" * 5000}
        self.log: list[dict] = []
        self.parity_ok = False
        self.instr = self.file = None
        self.pending_upload = {}
        self.ready = threading.Event()
        d = tempfile.mkdtemp()
        self.cert, self.key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=p2s",
                        "-keyout", self.key, "-out", self.cert, "-days", "1"], check=True, capture_output=True)
        self.srv = socket.socket()
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.srv.listen()
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(self.cert, self.key)
        while True:
            c, _ = self.srv.accept()
            threading.Thread(target=self._serve, args=(ctx.wrap_socket(c, server_side=True),), daemon=True).start()

    def _serve(self, s):
        ws = ServerWS(s)
        path = ws.handshake()
        chan = "instruction" if "function=instruction" in path else "file" if "file_stream" in path else "media"
        if chan == "instruction":
            self.instr = ws
        elif chan == "file":
            self.file = ws
        reader = fr.FrameReader()
        try:
            while True:
                for ptype, payload in reader.feed(ws.recv()):
                    if ptype == fr.JSON:
                        self._json(ws, json.loads(payload))
                    elif ptype == fr.FILE_TRANSFER:
                        self._file(payload)
        except (EOFError, OSError):
            pass

    def _reply(self, ws, req, data=None, code=0):
        ws.send(fr.encode(json.dumps({"type": "response", "code": code, "msg": "ok",
                                      "transactionId": req["transactionId"], "data": data or {}}), fr.JSON))

    def _json(self, ws, req):
        url = req["url"]
        if url != "/v1/user/ping":
            self.log.append(req)
        if url == "/v1/user/parity":
            self.parity_ok = req["data"].get("userKey") == "bWFrZWJsb2NrLXh0b29s"
            return self._reply(ws, req, {}, 0 if self.parity_ok else 1)
        if not self.parity_ok:
            return
        if url == "/v1/user/ping":
            return self._reply(ws, req)
        if url == "/v1/device/machineInfo":
            return self._reply(ws, req, {"deviceCode": "ZY013", "gcodeFormat": ["txt", "bin"]})
        if url == "/v1/device/runningStatus":
            return self._reply(ws, req, {"curMode": {"mode": "P_IDLE"}})
        if url == "/v1/filetransfer/upload":
            d = req["data"]
            self.pending_upload[d["channel"]] = dict(d, buf=bytearray(d["filesize"]), got=0)
            self._reply(ws, req, {"packetsize": 65536})            # device shrinks packet size
            self._request_window(d["channel"], 0)
            return
        if url == "/v1/filetransfer/download":
            d = req["data"]
            body = self.files[d["filename"]]
            self.pending_upload[("dl", d["channel"])] = body
            return self._reply(ws, req, {"filesize": len(body), "digesttype": 1,
                                         "digestdata": hashlib.md5(body).hexdigest(), "packetsize": 65536})
        if url == "/v1/file/content":
            return self._reply(ws, req, self.calib.get(req["params"].get("type"), '{"image_src_w":2448,"image_src_h":2048}'))
        if url == "/v1/peripheral/param" and req["params"].get("type") == "ir_measure_distance":
            return self._reply(ws, req, {"coord_x": 0, "coord_y": 0, "distance": 9.5})
        if url == "/v1/peripheral/param" and req["params"].get("type") == "gap":
            return self._reply(ws, req, {"state": "off"})
        if url == "/v1/peripheral/param" and req["params"].get("type") == "machine_lock":
            return self._reply(ws, req, {"state": "on"})
        if url == "/v1/config/get":
            return self._reply(ws, req, {"laserMeasureOffset": "X64.5Y1.4Z136.7"})
        if url == "/v1/camera/image":
            return self._reply(ws, req, {"filename": "snap.jpg"})
        return self._reply(ws, req, {"result": "ok"})

    def _request_window(self, ch, offset):
        u = self.pending_upload[ch]
        w = min(self.window, u["filesize"] - offset)
        self.file.send(fr.encode(fr.file_request_packet(ch, offset, w), fr.FILE_TRANSFER, False))
        u["window_end"] = offset + w

    def _file(self, payload):
        if payload == b"\x02":
            return self.file.send(fr.encode(b"\x82", fr.FILE_TRANSFER))
        kind, ch, off, x = fr.parse_file_packet(payload)
        if kind == "data":
            u = self.pending_upload[ch]
            u["buf"][off:off + len(x)] = x
            u["got"] += len(x)
            if u["got"] >= u["filesize"]:
                ok = hashlib.md5(u["buf"]).hexdigest() == u["digestdata"]
                self.uploads[u["filename"]] = bytes(u["buf"])
                msg = {"url": "/v1/filetransfer/finish",
                       "data": {"code": 0 if ok else -1, "channel": ch, "message": "done"}}
                self.instr.send(fr.encode(json.dumps(msg), fr.JSON))
            elif off + len(x) >= u["window_end"]:
                self._request_window(ch, u["got"])
        elif kind == "request":
            body = self.pending_upload[("dl", ch)]
            end = min(off + x, len(body))
            for p in range(off, end, 65536):
                self.file.send(fr.encode(fr.file_data_packet(ch, p, body[p:min(p + 65536, end)]),
                                         fr.FILE_TRANSFER))
