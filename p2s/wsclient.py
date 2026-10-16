"""Minimal WebSocket client (RFC 6455) using only the standard library.

Just enough for the P2S: binary frames, TLS with the device's self-signed
certificate, ping/pong and close. Thread-safe sends.
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
import threading
from urllib.parse import urlparse

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketClosed(Exception):
    pass


class WebSocket:
    def __init__(self, url: str, timeout: float = 5.0, verify_tls: bool = False):
        u = urlparse(url)
        secure = u.scheme == "wss"
        port = u.port or (443 if secure else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        if secure:
            ctx = ssl.create_default_context()
            if not verify_tls:
                # The P2S uses a self-signed cert; xTool Studio skips verification too.
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            raw = ctx.wrap_socket(raw, server_hostname=u.hostname)
        self.sock = raw
        self._send_lock = threading.Lock()
        self._buf = b""
        self.closed = False
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {u.hostname}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0]
        if b" 101 " not in status + b" ":
            raise ConnectionError(f"WebSocket upgrade failed: {status.decode(errors='replace')}")
        self.sock.settimeout(None)

    # -- low level -------------------------------------------------------
    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketClosed("connection closed during handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WebSocketClosed("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        # XOR-mask quickly via int arithmetic on 8-byte blocks
        masked = _mask(payload, mask)
        with self._send_lock:
            if self.closed:
                raise WebSocketClosed("socket closed")
            self.sock.sendall(bytes(header) + masked)

    # -- public ----------------------------------------------------------
    def send_binary(self, data: bytes) -> None:
        self._send_frame(OP_BIN, data)

    def recv(self) -> bytes:
        """Return the next complete data message (bytes). Handles control frames."""
        message = bytearray()
        while True:
            b1, b2 = self._read_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if b2 & 0x80 else None
            payload = self._read_exact(n)
            if mask:
                payload = _mask(payload, mask)
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.close()
                raise WebSocketClosed("closed by device")
            message += payload
            if fin:
                return bytes(message)

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._send_frame(OP_CLOSE, struct.pack(">H", 1000))
        except Exception:
            pass
        self.closed = True
        try:
            self.sock.close()
        except Exception:
            pass


def _mask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return b""
    n = len(data)
    key = int.from_bytes((mask * (n // 4 + 1))[:n], "big")
    return (int.from_bytes(data, "big") ^ key).to_bytes(n, "big")
