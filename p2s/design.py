"""The app's design model -> a machine Job.

A design is plain JSON (also the app's project file format):

{
  "focal": 15.5,                     # Z for the job (25 - measured distance), mm
  "fan_gear": 3, "purifier_gear": 4,
  "rotary": {"type": "roller" | "chuck", "diameter": 80, "axis_y": 125},   # optional, RA2
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
    objs.sort(key=lambda o: ORDER.get(o["op"]["mode"], 9))
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
            paths = place(*local_geometry(o), o)
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
               "axis_y": float(rot.get("axis_y", 125))}
        if rot["type"] == "chuck" and not rot["diameter"]:
            raise DesignError("Chuck mode needs the object's diameter.")
    return Job(mode="LASER_CYLINDER" if rot else "HONEYCOMB_PANEL", material=0 if rot else None,
               thickness=None,
               focal_len=float(design["focal"]) if design.get("focal") is not None else None,
               fan_gear=design.get("fan_gear", 3), purifier_gear=design.get("purifier_gear", 4),
               elements=elements, rotary=rot)
