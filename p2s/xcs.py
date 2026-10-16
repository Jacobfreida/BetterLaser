"""Read xTool Studio .xcs project files (plain JSON) into simple job elements."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

SUPPORTED_TYPES = {"RECT", "CIRCLE"}
VECTOR_MODES = {"VECTOR_ENGRAVING", "VECTOR_CUTTING"}


@dataclass
class Element:
    id: str
    shape: str
    processing_type: str        # VECTOR_ENGRAVING / VECTOR_CUTTING / FILL_VECTOR_ENGRAVING / BITMAP_ENGRAVING
    power: float                # percent, 0-100 (max power for grayscale images)
    speed: float                # mm/s
    passes: int
    air: float                  # air-assist percent
    paths: list[list[tuple[float, float]]] = field(default_factory=list)  # mm, machine coords
    # raster (fill / bitmap) only: [(y, [(x_start, x_end, S 0-1000), ...]), ...]
    rows: list = field(default_factory=list)
    density: float = 100.0      # lines per cm
    power_range: tuple = (0.0, 0.0)   # percent, grayscale min/max
    bitmap_mode: str = "grayscale"
    scan: str = "zMode"         # zMode = back and forth, oneWay = left to right only

    @property
    def is_raster(self) -> bool:
        return self.processing_type in ("FILL_VECTOR_ENGRAVING", "BITMAP_ENGRAVING")


@dataclass
class Job:
    mode: str
    material: int | None
    thickness: float | None
    focal_len: float | None
    fan_gear: int | None
    purifier_gear: int | None
    elements: list[Element]


def _params(entry: dict, ptype: str) -> dict:
    d = entry["data"][ptype]
    p = d["parameter"]
    return p.get(d.get("materialType")) or next(iter(p.values()))


def load(path: str, seg: float = 0.1) -> Job:
    doc = json.load(open(path, encoding="utf-8"))
    canvas_id = doc["canvasId"]
    canvas = next(c for c in doc["canvas"] if c["id"] == canvas_id)
    dev = dict(doc["device"]["data"]["value"])[canvas_id]
    mode = dev["mode"]
    job_cfg = dev["data"].get(mode, {})
    # Per-device settings are the ones Studio actually sends.
    dev_displays = dict(dev["displays"]["value"])

    elements = []
    for disp in sorted(canvas["displays"], key=lambda d: d.get("zOrder", 0)):
        if not disp.get("visible", True):
            continue
        entry = dev_displays.get(disp["id"]) or disp
        if entry.get("processIgnore"):
            continue
        ptype = entry.get("processingType") or disp.get("processingType")
        if ptype not in VECTOR_MODES:
            raise NotImplementedError(f"{disp['type']} uses {ptype}; only vector score/cut is supported so far")
        if disp["type"] not in SUPPORTED_TYPES:
            raise NotImplementedError(f"shape type {disp['type']} not supported yet")
        if abs(disp.get("angle", 0)) > 1e-9:
            raise NotImplementedError("rotated shapes not supported yet")
        p = _params(entry, ptype)
        elements.append(Element(
            id=disp["id"], shape=disp["type"], processing_type=ptype,
            power=float(p["power"]), speed=float(p["speed"]), passes=int(p.get("repeat", 1)),
            air=float(p.get("airPump", 0)),
            paths=[_outline(disp, seg)]))
    return Job(mode=mode, material=job_cfg.get("material"), thickness=job_cfg.get("thickness"),
               focal_len=job_cfg.get("focalLength"), fan_gear=job_cfg.get("fanGear"),
               purifier_gear=job_cfg.get("purifierGear"), elements=elements)


def _outline(d: dict, seg: float) -> list[tuple[float, float]]:
    x, y, w, h = d["x"], d["y"], abs(d["width"]), abs(d["height"])
    if d["type"] == "RECT":
        if d.get("radius"):
            raise NotImplementedError("rounded rectangles not supported yet")
        return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]
    # CIRCLE / ellipse inscribed in the bounding box, ~0.1 mm segments (Studio's delta)
    rx, ry = w / 2, h / 2
    cx, cy = x + rx, y + ry
    perim = math.pi * (3 * (rx + ry) - math.sqrt((3 * rx + ry) * (rx + 3 * ry)))  # Ramanujan
    n = max(16, math.ceil(perim / seg))
    pts = [(cx + rx * math.cos(2 * math.pi * i / n), cy + ry * math.sin(2 * math.pi * i / n)) for i in range(n)]
    return pts + [pts[0]]
