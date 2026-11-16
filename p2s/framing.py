"""xTool "BABE" frame envelope used on every P2S WebSocket channel.

Layout (10-byte header + payload), all integers big-endian:
  0-1  0xBA 0xBE magic
  2-4  payload length (3 bytes)
  5    protocol type (low 7 bits); bit 7 set = payload CRC not computed
  6-7  CRC-16/ARC of payload (0 when bit 7 set)
  8-9  CRC-16/ARC of header bytes 0-7

Source: xTool Studio 1.7.30 atomm-sharedworker.js (MessageEncoder.encodeFrame /
MessageParser.extractCompletePackets).
"""
from __future__ import annotations

MAGIC = b"\xBA\xBE"

# ProtocolCode enum from the app
F0F7, F3F4, F8F9, JSON, BUFFER = 1, 2, 3, 4, 5
NETWORK_CONFIG, FILE_TRANSFER, MEDIA_STREAM = 32, 33, 34

# File-transfer sub-packets (first payload byte)
FILE_REQUEST = 1      # opcode, channel, offset(5), window(3)
FILE_DATA = 129       # opcode, channel, offset(5), data...


def _table() -> list[int]:
    t = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        t.append(c)
    return t


_T = _table()


def crc16(data: bytes) -> int:
    """CRC-16/ARC (poly 0xA001 reflected, init 0) - same as npm `crc` crc16."""
    c = 0
    for b in data:
        c = (_T[(c ^ b) & 0xFF] ^ (c >> 8)) & 0xFFFF
    return c


def encode(payload: bytes | str, ptype: int, with_crc: bool = True) -> bytes:
    if isinstance(payload, str):
        payload = payload.encode()
    n = len(payload)
    if n >= 1 << 24:
        raise ValueError("payload too large for one frame")
    h = bytearray(MAGIC)
    h += n.to_bytes(3, "big")
    h.append((ptype & 0x7F) | (0 if with_crc else 0x80))
    h += (crc16(payload) if with_crc else 0).to_bytes(2, "big")
    h += crc16(bytes(h[:8])).to_bytes(2, "big")
    return bytes(h) + payload


class FrameReader:
    """Accumulates WebSocket messages and yields complete (ptype, payload) frames.

    Resyncs byte-by-byte on bad magic / CRC, exactly like the app does.
    """

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self.buf += data
        out: list[tuple[int, bytes]] = []
        b, i, n = self.buf, 0, len(self.buf)
        while i + 10 <= n:
            if b[i] != 0xBA or b[i + 1] != 0xBE:
                i += 1
                continue
            total = 10 + int.from_bytes(b[i + 2:i + 5], "big")
            if i + total > n:
                break
            if crc16(bytes(b[i:i + 8])) != int.from_bytes(b[i + 8:i + 10], "big"):
                i += 1
                continue
            payload = bytes(b[i + 10:i + total])
            if not (b[i + 5] & 0x80) and crc16(payload) != int.from_bytes(b[i + 6:i + 8], "big"):
                i += 1
                continue
            out.append((b[i + 5] & 0x7F, payload))
            i += total
        del self.buf[:i]
        return out


def file_data_packet(channel: int, offset: int, data: bytes) -> bytes:
    return bytes([FILE_DATA, channel]) + offset.to_bytes(5, "big") + data


def file_request_packet(channel: int, offset: int, window: int) -> bytes:
    return bytes([FILE_REQUEST, channel]) + offset.to_bytes(5, "big") + window.to_bytes(3, "big")


def parse_file_packet(p: bytes):
    """Returns ('request', ch, offset, window) / ('data', ch, offset, bytes) / None."""
    if len(p) >= 10 and p[0] == FILE_REQUEST:
        return ("request", p[1], int.from_bytes(p[2:7], "big"), int.from_bytes(p[7:10], "big"))
    if len(p) >= 7 and p[0] == FILE_DATA:
        return ("data", p[1], int.from_bytes(p[2:7], "big"), p[7:])
    return None
