"""The app's design model -> a machine Job.

A design is plain JSON (also the app's project file format):

{
  "focal": 15.5,                     # Z for the job (25 - measured distance), mm
  "fan_gear": 3, "purifier_gear": 4,
  "rotary": {"type": "roller" | "chuck", "diameter": 80, "axis_y": 125,   # optional, RA2
             "align": "center" | "edge"},   # centre of the design or Studio's edge on the line
  "objects": [
    {"id": "...", "type": "rect" | "ellipse" | "text" | "image" | "path",
     "x": 10, "y": 10, "w": 50, "h": 20, "rot": 0,          # mm, rot = degrees clockwise
     "text": {"content": "Hi", "font": "<path>|<index>", "size": 20, "align": "left",
              "line_spacing": 1.2, "letter_spacing": 0},
     "image": {"cols": 300, "rows": 120, "data": "<base64, one byte per pixel>"},
     "paths": [[[x, y], ...], ...], "nat_w": 10, "nat_h": 10,   # type "path", local coords
     "op": {"mode": "score" | "cut" | "fill" | "image" | "off",
            "power": 30, "speed": 100, "passes": 1, "air": 25,
            "density": 100, "scan": "zMode",
            "image_mode": "grayscale" | "dither", "min_power": 0}}
  ]
}

Image pixels arrive already processed by the app (resized to the engraving
grid, adjusted, and dithered when chosen): 0 = black = full power, 255 = white.
"""
from __future__ import annotations

import base64
import math

from . import fonts
from .raster import fill_rows, pixel_runs
from .xcs import Element, Job

PROC = {"score": "VECTOR_ENGRAVING", "cut": "VECTOR_CUTTING",
        "fill": "FILL_VECTOR_ENGRAVING", "image": "BITMAP_ENGRAVING"}
ORDER = {"image": 0, "fill": 1, "score": 2, "cut": 3}     # cuts always last


class DesignError(ValueError):
    pass


def _ellipse(w, h, seg=0.1):
    rx, ry = w / 2, h / 2
    perim = math.pi * (3 * (rx + ry) - math.sqrt(max(0.0, (3 * rx + ry) * (rx + 3 * ry))))
    n = max(16, math.ceil(perim / seg))
    pts = [(rx + rx * math.cos(2 * math.pi * i / n), ry + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return pts + [pts[0]]


def text_geometry(t: dict):
    """Local outlines for a text object + natural size."""
    font = t.get("font") or ""
    path, _, idx = font.rpartition("|")
    if not path:
        path, idx = font, "0"
    paths, (x0, y0, x1, y1) = fonts.text_paths(
        t.get("content", ""), path, float(t.get("size", 10)), index=int(idx or 0),
        align=t.get("align", "left"), line_spacing=float(t.get("line_spacing", 1.2)),
        letter_spacing_mm=float(t.get("letter_spacing", 0)))
    paths = [[(x - x0, y - y0) for x, y in p] for p in paths]
    return paths, max(x1 - x0, 1e-6), max(y1 - y0, 1e-6)


def _font_of(t: dict):
    font = t.get("font") or ""
    path, _, idx = font.rpartition("|")
    if not path:
        path, idx = font, "0"
    return path, int(idx or 0)


def _resample(path, step=0.25):
    """Closed polyline -> evenly spaced points, plus the total length."""
    pts = list(path)
    if math.dist(pts[0], pts[-1]) > 1e-9:
        pts.append(pts[0])
    out, carry = [pts[0]], 0.0
    for a, b in zip(pts, pts[1:]):
        seg = math.dist(a, b)
        t = step - carry
        while t <= seg + 1e-12:
            out.append((a[0] + (b[0] - a[0]) * t / seg, a[1] + (b[1] - a[1]) * t / seg))
            t += step
        carry = seg - (t - step)
    length = sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))
    if math.dist(out[-1], out[0]) < step * 0.5:
        out.pop()
    return out, length


def wrap_outline(target: dict):
    """The outline that text wraps around: the target's longest closed loop, in bed mm."""
    if target.get("type") not in ("rect", "ellipse", "path"):
        raise DesignError("Text can only wrap around a rectangle, ellipse or shape.")
    loops = place(*local_geometry(target), target)
    loops = [p for p in loops if len(p) > 2]
    if not loops:
        raise DesignError("That shape has no outline to wrap around.")
    return max(loops, key=lambda p: sum(math.dist(a, b) for a, b in zip(p, p[1:])))


def wrap_text_paths(t: dict, target: dict):
    """Text set along a closed outline (a circle badge, an arc over a shape...).

    t["wrap"]: {"side": "outside" | "inside", "gap": mm from the outline, "shift": degrees}
      outside: reads clockwise over the top, letters standing on the outline
      inside:  reads anticlockwise along the bottom, letters hanging inside it
    shift slides the text round the outline (360 = once round). Returns bed-mm loops.
    """
    w = t.get("wrap") or {}
    side = w.get("side", "outside")
    gap = float(w.get("gap", 1.0))
    loop = wrap_outline(target)
    pts, L = _resample(loop)
    n = len(pts)
    if n < 8 or L <= 0:
        raise DesignError("That shape is too small to wrap text around.")
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))
    if (area > 0) != (side == "outside"):          # area > 0: clockwise on screen (y down)
        pts = pts[::-1]
    step = L / n
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    cx = (min(xs) + max(xs)) / 2
    aim = (cx, min(ys)) if side == "outside" else (cx, max(ys))
    k0 = min(range(n), key=lambda i: math.dist(pts[i], aim))
    s0 = k0 * step + float(w.get("shift", 0)) / 360.0 * L

    def at(s):
        f = (s / step) % n
        i = int(f); u = f - i
        a, b = pts[i], pts[(i + 1) % n]
        return (a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u)

    path, idx = _font_of(t)
    glyphs, width, _, _ = fonts.glyph_run(t.get("content", ""), path, float(t.get("size", 10)), index=idx,
                                         letter_spacing_mm=float(t.get("letter_spacing", 0)))
    if width > L:
        raise DesignError(f"The text is {width:.0f} mm long but the outline is only {L:.0f} mm round; "
                          "make the text smaller or the shape bigger.")
    out = []
    for gx, adv, conts in glyphs:
        if not conts:
            continue
        mid = s0 - width / 2 + gx + adv / 2
        h = max(adv / 2, 0.5)
        a, b = at(mid - h), at(mid + h)
        dl = math.dist(a, b) or 1.0
        dx, dy = (b[0] - a[0]) / dl, (b[1] - a[1]) / dl      # along the outline
        nx, ny = -dy, dx                                     # glyph "down"
        p = at(mid)
        bx, by = p[0] - nx * gap, p[1] - ny * gap             # baseline point, gap away from the outline
        for c in conts:
            out.append([(bx + dx * (u - adv / 2) + nx * v, by + dy * (u - adv / 2) + ny * v) for u, v in c])
    return out


def local_geometry(o: dict):
    """(paths in local coords, natural width, natural height) for vector objects."""
    t = o["type"]
    w, h = float(o["w"]), float(o["h"])
    if t == "rect":
        return [[(0, 0), (w, 0), (w, h), (0, h), (0, 0)]], w, h
    if t == "ellipse":
        return [_ellipse(w, h)], w, h
    if t == "text":
        return text_geometry(o.get("text", {}))
    if t == "path":
        paths = [[(float(x), float(y)) for x, y in p] for p in o.get("paths", [])]
        return paths, float(o.get("nat_w") or w), float(o.get("nat_h") or h)
    raise DesignError(f"object type {t} has no outline")


def place(paths, nat_w, nat_h, o):
    """Scale local geometry to the object box, rotate about its centre, move into place."""
    x, y, w, h = float(o["x"]), float(o["y"]), float(o["w"]), float(o["h"])
    sx, sy = w / nat_w, h / nat_h
    a = math.radians(float(o.get("rot", 0) or 0))
    ca, sa = math.cos(a), math.sin(a)
    cx, cy = x + w / 2, y + h / 2
    out = []
    for p in paths:
        q = []
        for px, py in p:
            lx, ly = px * sx - w / 2, py * sy - h / 2
            q.append((cx + lx * ca - ly * sa, cy + lx * sa + ly * ca))
        out.append(q)
    return out


def _image_rows(o: dict, op: dict) -> list:
    img = o.get("image") or {}
    cols, rows = int(img["cols"]), int(img["rows"])
    px = base64.b64decode(img["data"])
    if len(px) != cols * rows:
        raise DesignError("image data size doesn't match cols x rows")
    if float(o.get("rot", 0) or 0):
        raise DesignError("rotate images in the app before engraving (rotation is baked into the pixels)")
    x, y, w, h = float(o["x"]), float(o["y"]), float(o["w"]), float(o["h"])
    pw, ph = w / cols, h / rows
    hi = float(op.get("power", 0)) * 10
    lo = float(op.get("min_power", 0)) * 10
    dither = op.get("image_mode") == "dither"

    def power_of(v):
        if dither:
            return int(round(hi)) if v < 128 else 0
        if v >= 255:
            return 0
        return int(round(lo + (hi - lo) * (255 - v) / 255))

    out = []
    for r in range(rows):
        line = list(px[r * cols:(r + 1) * cols])
        runs = pixel_runs(line, x, pw, power_of)
        if runs:
            out.append((y + (r + 0.5) * ph, runs))
    return out


def build_job(design: dict) -> Job:
    objs = [o for o in design.get("objects", []) if (o.get("op") or {}).get("mode", "off") != "off"]
    if not objs:
        raise DesignError("Nothing to engrave: every object is set to Off.")
    # engrave first, cut last; among cuts the smallest go first, so holes inside a part are
    # cut before its outline frees it (Python's sort is stable: drawing order breaks ties)
    objs.sort(key=lambda o: (ORDER.get(o["op"]["mode"], 9),
                             abs(float(o.get("w", 0)) * float(o.get("h", 0))) if o["op"]["mode"] == "cut" else 0))
    by_id = {str(o.get("id")): o for o in design.get("objects", [])}
    elements = []
    for o in objs:
        op = o["op"]
        mode = op["mode"]
        if mode not in PROC:
            raise DesignError(f"unknown mode {mode}")
        e = Element(id=str(o.get("id", "")), shape=o["type"], processing_type=PROC[mode],
                    power=float(op.get("power", 0)), speed=float(op.get("speed", 0)),
                    passes=int(op.get("passes", 1)), air=float(op.get("air", 25)),
                    density=float(op.get("density", 100)), scan=op.get("scan", "zMode"))
        if mode == "image":
            if o["type"] != "image":
                raise DesignError("Image mode only applies to pictures")
            e.rows = _image_rows(o, op)
            e.power_range = (float(op.get("min_power", 0)), float(op.get("power", 0)))
            e.bitmap_mode = "grayscale" if op.get("image_mode") != "dither" else (op.get("dither") or "Jarvis")
        else:
            if o["type"] == "image":
                raise DesignError("Pictures can only use Image mode")
            wrap = (o.get("text") or {}).get("wrap") if o["type"] == "text" else None
            if wrap:
                target = by_id.get(wrap.get("target"))
                if target is None:
                    raise DesignError("Wrapped text's shape is missing; unwrap the text or undo the delete.")
                paths = wrap_text_paths(o["text"], target)
            else:
                paths = place(*local_geometry(o), o)
            if mode == "fill" and (o.get("shape") or {}).get("kind") == "line":
                raise DesignError("A line can't be filled; use Score or Cut.")
            if mode == "fill":
                spacing = 10.0 / max(1.0, e.density)
                e.rows = [(yy, [(a, b, int(round(e.power * 10))) for a, b in segs])
                          for yy, segs in fill_rows(paths, spacing)]
                e.power_range = (0.0, e.power)
            else:
                e.paths = paths
        if e.paths or e.rows:
            elements.append(e)
    if not elements:
        raise DesignError("The design produced no laser moves (empty text or blank image?)")
    rot = design.get("rotary") or None
    if rot:
        rot = {"model": "ra2", "type": rot.get("type", "roller"),
               "diameter": float(rot["diameter"]) if rot.get("diameter") else None,
               "axis_y": float(rot.get("axis_y", 125)), "align": rot.get("align", "center")}
        if rot["type"] == "chuck" and not rot["diameter"]:
            raise DesignError("Chuck mode needs the object's diameter.")
    return Job(mode="LASER_CYLINDER" if rot else "HONEYCOMB_PANEL", material=0 if rot else None,
               thickness=None,
               focal_len=float(design["focal"]) if design.get("focal") is not None else None,
               fan_gear=design.get("fan_gear", 3), purifier_gear=design.get("purifier_gear", 4),
               elements=elements, rotary=rot)
