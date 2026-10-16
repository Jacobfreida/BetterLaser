"""Turn text into vector outlines using installed TrueType fonts (pure Python).

Supports .ttf and .ttc (TrueType outlines, which covers nearly all Windows fonts).
CFF-based .otf fonts are listed as unsupported.
"""
from __future__ import annotations

import glob
import os
import struct
import sys
from functools import lru_cache

Point = tuple[float, float]


class FontError(Exception):
    pass


class TTFont:
    def __init__(self, path: str, index: int = 0):
        self.path = path
        data = open(path, "rb").read()
        self.data = data
        base = 0
        if data[:4] == b"ttcf":
            n = struct.unpack(">I", data[8:12])[0]
            if index >= n:
                raise FontError("font index out of range")
            base = struct.unpack(">I", data[12 + 4 * index:16 + 4 * index])[0]
        num_tables = struct.unpack(">H", data[base + 4:base + 6])[0]
        self.tables = {}
        for i in range(num_tables):
            rec = data[base + 12 + 16 * i: base + 28 + 16 * i]
            tag, _, off, length = struct.unpack(">4sIII", rec)
            self.tables[tag.decode("latin1")] = (off, length)
        if "glyf" not in self.tables:
            raise FontError("not a TrueType-outline font (CFF/OTF not supported)")
        h = self._t("head")
        self.units_per_em = struct.unpack(">H", h[18:20])[0]
        self.loca_long = struct.unpack(">h", h[50:52])[0] == 1
        self.num_glyphs = struct.unpack(">H", self._t("maxp")[4:6])[0]
        hhea = self._t("hhea")
        self.ascender, self.descender, self.line_gap = struct.unpack(">hhh", hhea[4:10])
        self.num_hmetrics = struct.unpack(">H", hhea[34:36])[0]
        self._hmtx = self._t("hmtx")
        self._loca = self._t("loca")
        self._glyf_off = self.tables["glyf"][0]
        self.cmap = self._read_cmap()
        self.kern = self._read_kern()
        self.family, self.style = self._read_names()

    def _t(self, tag: str) -> bytes:
        if tag not in self.tables:
            raise FontError(f"missing table {tag}")
        off, length = self.tables[tag]
        return self.data[off:off + length]

    # -- metadata ---------------------------------------------------------------
    def _read_names(self):
        try:
            t = self._t("name")
        except FontError:
            return os.path.basename(self.path), ""
        count, str_off = struct.unpack(">HH", t[2:6])
        found = {}
        for i in range(count):
            pid, eid, lid, nid, length, off = struct.unpack(">HHHHHH", t[6 + 12 * i: 18 + 12 * i])
            if nid not in (1, 2, 16, 17):
                continue
            raw = t[str_off + off: str_off + off + length]
            if pid == 3 or pid == 0:
                s = raw.decode("utf-16-be", "replace")
                pri = 0 if (pid == 3 and lid == 0x409) else 1
            else:
                s = raw.decode("latin1", "replace")
                pri = 2
            if nid not in found or pri < found[nid][0]:
                found[nid] = (pri, s)
        fam = (found.get(16) or found.get(1) or (0, os.path.basename(self.path)))[1]
        sty = (found.get(17) or found.get(2) or (0, "Regular"))[1]
        return fam, sty

    def _read_cmap(self) -> dict[int, int]:
        t = self._t("cmap")
        n = struct.unpack(">H", t[2:4])[0]
        subs = []
        for i in range(n):
            pid, eid, off = struct.unpack(">HHI", t[4 + 8 * i: 12 + 8 * i])
            fmt = struct.unpack(">H", t[off:off + 2])[0]
            subs.append((pid, eid, off, fmt))
        # prefer full-unicode format 12, then BMP format 4
        for want in (12, 4):
            for pid, eid, off, fmt in subs:
                if fmt == want and (pid == 0 or (pid == 3 and eid in (1, 10))):
                    return self._cmap12(t, off) if fmt == 12 else self._cmap4(t, off)
        for pid, eid, off, fmt in subs:   # symbol fonts
            if fmt == 4:
                return self._cmap4(t, off)
        raise FontError("no usable character map")

    @staticmethod
    def _cmap4(t, off):
        segx2 = struct.unpack(">H", t[off + 6:off + 8])[0]
        n = segx2 // 2
        ends = struct.unpack(f">{n}H", t[off + 14: off + 14 + segx2])
        starts = struct.unpack(f">{n}H", t[off + 16 + segx2: off + 16 + 2 * segx2])
        deltas = struct.unpack(f">{n}h", t[off + 16 + 2 * segx2: off + 16 + 3 * segx2])
        ro_pos = off + 16 + 3 * segx2
        ranges = struct.unpack(f">{n}H", t[ro_pos: ro_pos + segx2])
        m = {}
        for i in range(n):
            for c in range(starts[i], ends[i] + 1):
                if c == 0xFFFF:
                    break
                if ranges[i] == 0:
                    g = (c + deltas[i]) & 0xFFFF
                else:
                    p = ro_pos + 2 * i + ranges[i] + 2 * (c - starts[i])
                    g = struct.unpack(">H", t[p:p + 2])[0]
                    if g:
                        g = (g + deltas[i]) & 0xFFFF
                if g:
                    m[c] = g
        return m

    @staticmethod
    def _cmap12(t, off):
        n = struct.unpack(">I", t[off + 12: off + 16])[0]
        m = {}
        for i in range(n):
            s, e, g = struct.unpack(">III", t[off + 16 + 12 * i: off + 28 + 12 * i])
            if e - s > 0x30000:
                continue
            for c in range(s, e + 1):
                m[c] = g + (c - s)
        return m

    def _read_kern(self) -> dict[tuple[int, int], int]:
        if "kern" not in self.tables:
            return {}
        t = self._t("kern")
        try:
            ntab = struct.unpack(">H", t[2:4])[0]
            pos, out = 4, {}
            for _ in range(ntab):
                _, length, cov = struct.unpack(">HHH", t[pos:pos + 6])
                if cov >> 8 == 0:     # format 0
                    npairs = struct.unpack(">H", t[pos + 6:pos + 8])[0]
                    for i in range(npairs):
                        l, r, v = struct.unpack(">HHh", t[pos + 14 + 6 * i: pos + 20 + 6 * i])
                        out[(l, r)] = v
                pos += length
            return out
        except struct.error:
            return {}

    # -- glyphs -----------------------------------------------------------------
    def advance(self, gid: int) -> int:
        i = min(gid, self.num_hmetrics - 1)
        return struct.unpack(">H", self._hmtx[4 * i: 4 * i + 2])[0]

    def _glyph_range(self, gid):
        if self.loca_long:
            a, b = struct.unpack(">II", self._loca[4 * gid: 4 * gid + 8])
        else:
            a, b = (2 * x for x in struct.unpack(">HH", self._loca[2 * gid: 2 * gid + 4]))
        return a, b

    def contours(self, gid: int, depth: int = 0) -> list[list[tuple[float, float, bool]]]:
        """Raw contours: lists of (x, y, on_curve) in font units."""
        if gid >= self.num_glyphs or depth > 8:
            return []
        a, b = self._glyph_range(gid)
        if b <= a:
            return []
        d = self.data
        p = self._glyf_off + a
        ncont = struct.unpack(">h", d[p:p + 2])[0]
        if ncont >= 0:
            return self._simple(p, ncont)
        return self._composite(p, depth)

    def _simple(self, p, ncont):
        d = self.data
        ends = struct.unpack(f">{ncont}H", d[p + 10: p + 10 + 2 * ncont])
        npts = ends[-1] + 1 if ncont else 0
        q = p + 10 + 2 * ncont
        ilen = struct.unpack(">H", d[q:q + 2])[0]
        q += 2 + ilen
        flags = []
        while len(flags) < npts:
            f = d[q]; q += 1
            flags.append(f)
            if f & 8:
                r = d[q]; q += 1
                flags.extend([f] * r)
        flags = flags[:npts]
        xs, ys = [], []
        for coords, short, same in ((xs, 2, 16), (ys, 4, 32)):
            v = 0
            for f in flags:
                if f & short:
                    dv = d[q]; q += 1
                    v += dv if f & same else -dv
                elif not f & same:
                    v += struct.unpack(">h", d[q:q + 2])[0]; q += 2
                coords.append(v)
        out, start = [], 0
        for e in ends:
            out.append([(xs[i], ys[i], bool(flags[i] & 1)) for i in range(start, e + 1)])
            start = e + 1
        return out

    def _composite(self, p, depth):
        d = self.data
        q = p + 10
        out = []
        while True:
            flags, gid = struct.unpack(">HH", d[q:q + 4]); q += 4
            if flags & 1:
                dx, dy = struct.unpack(">hh", d[q:q + 4]); q += 4
            else:
                dx, dy = struct.unpack(">bb", d[q:q + 2]); q += 2
            a, b, c, e = 1.0, 0.0, 0.0, 1.0
            if flags & 8:
                a = e = struct.unpack(">h", d[q:q + 2])[0] / 16384; q += 2
            elif flags & 0x40:
                a, e = (v / 16384 for v in struct.unpack(">hh", d[q:q + 4])); q += 4
            elif flags & 0x80:
                a, b, c, e = (v / 16384 for v in struct.unpack(">hhhh", d[q:q + 8])); q += 8
            if not flags & 2:
                dx = dy = 0    # point-matching: rare, ignored
            for cont in self.contours(gid, depth + 1):
                out.append([(a * x + c * y + dx, b * x + e * y + dy, on) for x, y, on in cont])
            if not flags & 0x20:
                break
        return out


def _flatten(cont, scale, ox, oy, tol) -> list[Point]:
    """Quadratic B-spline contour -> closed polyline (y flipped: mm, y down)."""
    n = len(cont)
    if n == 0:
        return []
    pts = [(x, y, on) for x, y, on in cont]
    # start at an on-curve point (or the midpoint of the first two off-points)
    k = next((i for i, p in enumerate(pts) if p[2]), None)
    if k is None:
        x0 = (pts[0][0] + pts[1 % n][0]) / 2; y0 = (pts[0][1] + pts[1 % n][1]) / 2
        pts = [(x0, y0, True)] + pts[1:] + [pts[0]] if n > 1 else pts
        k = 0
    pts = pts[k:] + pts[:k]
    out = []
    tx = lambda x, y: (ox + x * scale, oy - y * scale)
    cur = pts[0]
    out.append(tx(cur[0], cur[1]))
    i, m = 1, len(pts)
    while i <= m:
        p = pts[i % m]
        if p[2]:
            out.append(tx(p[0], p[1])); cur = p; i += 1; continue
        nxt = pts[(i + 1) % m]
        end = nxt if nxt[2] else ((p[0] + nxt[0]) / 2, (p[1] + nxt[1]) / 2, True)
        # subdivide quadratic cur -> p -> end
        dist = (abs(cur[0] - 2 * p[0] + end[0]) + abs(cur[1] - 2 * p[1] + end[1])) * scale
        steps = max(2, min(64, int((dist / max(tol, 1e-6)) ** 0.5) + 1))
        for s in range(1, steps + 1):
            t = s / steps
            x = (1 - t) ** 2 * cur[0] + 2 * (1 - t) * t * p[0] + t * t * end[0]
            y = (1 - t) ** 2 * cur[1] + 2 * (1 - t) * t * p[1] + t * t * end[1]
            out.append(tx(x, y))
        cur = end
        i += 1 if not nxt[2] else 2
    if out and out[0] != out[-1]:
        out.append(out[0])
    return out


@lru_cache(maxsize=32)
def load_font(path: str, index: int = 0) -> TTFont:
    return TTFont(path, index)


def text_paths(text: str, font_path: str, size_mm: float, *, index: int = 0,
               align: str = "left", line_spacing: float = 1.2, letter_spacing_mm: float = 0.0,
               tol: float = 0.02) -> tuple[list[list[Point]], tuple[float, float, float, float]]:
    """Return (closed polylines in mm with y down, bbox (x0, y0, x1, y1)).

    size_mm is the font's em height. Origin: first baseline at y = ascender.
    """
    f = load_font(font_path, index)
    scale = size_mm / f.units_per_em
    asc = f.ascender * scale
    line_h = (f.ascender - f.descender + f.line_gap) * scale * line_spacing / 1.2 if line_spacing else size_mm
    lines = text.split("\n")
    widths, glyph_lines = [], []
    for line in lines:
        gl, x, prev = [], 0.0, None
        for ch in line:
            gid = f.cmap.get(ord(ch), 0)
            if prev is not None:
                x += f.kern.get((prev, gid), 0) * scale
            gl.append((gid, x))
            x += f.advance(gid) * scale + letter_spacing_mm
            prev = gid
        widths.append(max(0.0, x - letter_spacing_mm if line else 0.0))
        glyph_lines.append(gl)
    maxw = max(widths) if widths else 0.0
    paths = []
    for li, (gl, w) in enumerate(zip(glyph_lines, widths)):
        shift = {"center": (maxw - w) / 2, "right": maxw - w}.get(align, 0.0)
        base = asc + li * line_h
        for gid, x in gl:
            for cont in f.contours(gid):
                poly = _flatten(cont, scale, shift + x, base, tol)
                if len(poly) >= 3:
                    paths.append(poly)
    if paths:
        xs = [p[0] for c in paths for p in c]; ys = [p[1] for c in paths for p in c]
        bbox = (min(xs), min(ys), max(xs), max(ys))
    else:
        bbox = (0.0, 0.0, 0.0, 0.0)
    return paths, bbox


def font_dirs() -> list[str]:
    if sys.platform.startswith("win"):
        win = os.environ.get("WINDIR", r"C:\Windows")
        dirs = [os.path.join(win, "Fonts")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
        return dirs
    if sys.platform == "darwin":
        return ["/System/Library/Fonts", "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
    return ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts"),
            os.path.expanduser("~/.local/share/fonts")]


def _quick_names(fh, base: int):
    """Family/style and outline type from just the table directory + name table."""
    fh.seek(base)
    hdr = fh.read(12)
    n = struct.unpack(">H", hdr[4:6])[0]
    recs = fh.read(16 * n)
    tables = {}
    for i in range(n):
        tag, _, off, length = struct.unpack(">4sIII", recs[16 * i:16 * i + 16])
        tables[tag] = (off, length)
    if b"glyf" not in tables or b"name" not in tables:
        return None
    off, length = tables[b"name"]
    fh.seek(off)
    t = fh.read(length)
    count, str_off = struct.unpack(">HH", t[2:6])
    found = {}
    for i in range(count):
        pid, eid, lid, nid, ln, o = struct.unpack(">HHHHHH", t[6 + 12 * i: 18 + 12 * i])
        if nid not in (1, 2, 16, 17):
            continue
        raw = t[str_off + o: str_off + o + ln]
        if pid in (0, 3):
            v, pri = raw.decode("utf-16-be", "replace"), 0 if (pid == 3 and lid == 0x409) else 1
        else:
            v, pri = raw.decode("latin1", "replace"), 2
        if nid not in found or pri < found[nid][0]:
            found[nid] = (pri, v)
    fam = (found.get(16) or found.get(1) or (0, ""))[1]
    sty = (found.get(17) or found.get(2) or (0, "Regular"))[1]
    return (fam, sty) if fam else None


@lru_cache(maxsize=1)
def list_fonts() -> list[dict]:
    """[{id, family, style, path, index}] for every TrueType-outline font installed."""
    out, seen = [], set()
    for d in font_dirs():
        for path in glob.glob(os.path.join(d, "**", "*"), recursive=True):
            if path.lower().rsplit(".", 1)[-1] not in ("ttf", "ttc", "otf"):
                continue
            try:
                with open(path, "rb") as fh:
                    head = fh.read(12)
                    bases = [0]
                    if head[:4] == b"ttcf":
                        n = min(struct.unpack(">I", head[8:12])[0], 8)
                        fh.seek(12)
                        bases = list(struct.unpack(f">{n}I", fh.read(4 * n)))
                    for i, base in enumerate(bases):
                        names = _quick_names(fh, base)
                        if not names or names in seen:
                            continue
                        seen.add(names)
                        out.append({"id": f"{path}|{i}", "family": names[0], "style": names[1],
                                    "path": path, "index": i})
            except (OSError, struct.error):
                continue
    out.sort(key=lambda r: (r["family"].lower(), r["style"] != "Regular", r["style"]))
    return out
