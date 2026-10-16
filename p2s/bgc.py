"""xTool "BGC" binary G-code, the form xTool Studio sends to the P2S.

Studio uploads `tmp.bgcode` (fileType "bin") whenever the machine lists "bin" in
`gcodeFormat`; plain text is only used for its "Import G-code" feature.

The format is a byte-level packing of G-code text, defined by Studio's decoder
(main process, `fs-decode-bgc-file`, functions Gb/Kb/qb/Jb/Yb):
  10            newline
  192           space
  194 / 205     '"' / '-'
  225..250      'A'..'Z'
  206           decimal point inside a number
  0x00..0x99    a run of packed decimal digits, two per byte (high nibble first).
                In the integer part a leading 0 nibble in the first byte is
                dropped; in the fraction one trailing '0' is dropped.
Comment lines: 0xC3 ('#') followed by the comment text as plain bytes, up to the newline
(seen in a job Studio sent to the laser; Studio's preview decoder doesn't handle them).
Blank lines are dropped.
"""
from __future__ import annotations

import re

_NUM = re.compile(r"\d+(?:\.\d*)?|\.\d+")


def _pack_digits(d: str, pad_left: bool) -> bytes:
    if len(d) % 2:
        d = ("0" + d) if pad_left else (d + "0")
    return bytes(int(d[i]) << 4 | int(d[i + 1]) for i in range(0, len(d), 2))


def _encode_number(tok: str) -> bytes:
    if "." in tok:
        ip, fp = tok.split(".", 1)
        out = _pack_digits(ip, True) if ip else b""
        out += b"\xce"
        if fp:
            # odd-length fractions get a pad 0, which the decoder strips again
            out += _pack_digits(fp, False)
        return out
    return _pack_digits(tok, True)


def strip_comments(text: str) -> str:
    """Code lines only (used when a comment-free form is wanted, e.g. comparisons)."""
    lines = []
    for line in text.splitlines():
        code = line.split("#", 1)[0].split(";", 1)[0].rstrip()
        if code:
            lines.append(code)
    return "\n".join(lines) + "\n"


def normalize(text: str) -> str:
    """What survives a BGC round trip: no blank lines, no trailing spaces."""
    return "".join(l.rstrip() + "\n" for l in text.splitlines() if l.strip())


def encode(text: str) -> bytes:
    """G-code text -> BGC, exactly as Studio writes it (comments kept, blank lines dropped).

    Studio's own files (fetched from the laser) store '#' as 0xC3 followed by the
    comment's plain text up to the newline.
    """
    out = bytearray()
    for line in text.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("#"):
            out.append(0xC3)
            out += line[1:].encode("utf-8")
            out.append(10)
            continue
        i = 0
        while i < len(line):
            ch = line[i]
            m = _NUM.match(line, i)
            if m:
                out += _encode_number(m.group(0))
                i = m.end()
                continue
            if "A" <= ch <= "Z":
                out.append(225 + ord(ch) - 65)
            elif ch == " ":
                out.append(192)
            elif ch == '"':
                out.append(194)
            elif ch == "-":
                out.append(205)
            else:
                raise ValueError(f"character {ch!r} can't be encoded in BGC: {line!r}")
            i += 1
        out.append(10)
    return bytes(out)


# --- decoder: line-for-line port of Studio's Yb() ---------------------------
def _digits(b: bytes) -> str:
    ip, fp, frac, first = "", "", False, True
    for o in b:
        if o == 206:
            frac = True
            continue
        hi, lo = o >> 4 & 15, o & 15
        if frac:
            fp += chr(48 + hi) + chr(48 + lo)
        else:
            if not (first and hi == 0):
                ip += chr(48 + hi)
            ip += chr(48 + lo)
            first = False
    if frac and fp.endswith("0"):
        fp = fp[:-1]
    return f"{ip}.{fp}" if frac else ip


def decode(data: bytes) -> str:
    out, i, n = [], 0, len(data)
    while i < n:
        e = data[i]
        if e == 0xC3:          # '#': the rest of the line is plain text (a comment)
            j = data.find(b"\n", i)
            j = n if j < 0 else j
            out.append("#" + data[i + 1:j].decode("utf-8", "replace"))
            i = j
            continue
        if e == 10:
            out.append("\n"); i += 1; continue
        if e == 192:
            out.append(" "); i += 1; continue
        if 225 <= e <= 250:
            out.append(chr(e - 225 + 65)); i += 1; continue
        if e in (194, 205):
            out.append('"' if e == 194 else "-"); i += 1; continue
        if e <= 153 or e == 206:
            j = i
            while j < n and not (data[j] == 10 or (data[j] > 153 and data[j] != 206)):
                j += 1
            seg = data[i:j]
            out.append("." if seg == b"\xce" else _digits(seg))
            i = j
            continue
        out.append(chr(e)); i += 1
    return "".join(out)
