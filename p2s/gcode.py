"""Generate P2S G-code, matching xTool Studio 1.7.30 output.

Sources:
- Header, per-element setup and footer: xTool's P2S plugin (xcs-ext-p2s 1.5.66,
  templates P9.* and functions Vct/Rct/zct/Fct).
- Move-line format: a real Studio export of the Square and Circle project
  (algorithmVersion 1.7.5). For that project, our output matches Studio's line
  for line on the square, and to within 0.01 mm on the circle.

Units: power = percent x 10 (0-1000), speed = mm/s x 60 (mm/min), mm coords.
"""
from __future__ import annotations

import json
import math
import time

from .xcs import Job, Element

MOTION_VERIFIED = True

ACCEL = 3200
TRAVEL_FEED = 9600
SEGMENT = 0.1           # buildParams.delta: curve segment length, mm
BED_W, BED_H = 600, 305

PROCESSING_CODE = {"VECTOR_ENGRAVING": "S1", "VECTOR_CUTTING": "S3",
                   "FILL_VECTOR_ENGRAVING": "S4", "BITMAP_ENGRAVING": "S2"}
MODE_TOOL = {"LASER_PLANE": "T0", "HONEYCOMB_PANEL": "T0", "FREE_PLANE": "T0"}


def accessories(job: Job) -> str:
    """Accessory block as Studio wrote it for this machine (purifier, duct fan...).

    Fan/purifier gears come from the project; the other lines are copied
    verbatim from the user's export and depend on which accessories are fitted.
    """
    fan = job.fan_gear if job.fan_gear is not None else 3
    pur = job.purifier_gear if job.purifier_gear is not None else 4
    return (f'G198 P84 "M9033 V75"\n'
            f'M9064 B{fan}\nG198 P70 "M9064 B{fan}"\nG198 P78 "M9064 B{fan}"\n'
            f'M9039 C{pur}\nG198 P69 "M9039 C{pur}"\n'
            f'G198 P76 "M9039 V50"\n'
            f'G198 P76 "M9043 H4 I4 J4 K194 L4 M4"\n')


def _c(v: float) -> str:
    """Coordinate format seen in Studio output: 3 decimals, trailing zeros trimmed."""
    s = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))


def file_header(job: Job) -> str:
    return (f"# date={time.strftime('%Y_%m_%d_%H_%M_%S')}\n"
            "# version=p2s-tools\n"
            "# algorithmVersion=1.7.5\n"
            f'# gc={{"size":{{"w":{BED_W},"h":{BED_H}}}}}\n'
            '# gc={"offset":{"x":0,"y":0}}\n'
            '# gc={"start":{"x":0,"y":0.0000}}\n'
            '# gc={"keys":["x","y"],"rm":1,"is3DMode":false}\n'
            '# timeConfig={"vectorAccel":420,"bitmapAccel":3200,"thresholdAngle":174}\n'
            "G90\nG0 F3000\n")


def head(job: Job, summary: Element) -> str:
    return ("\n# P2s HEAD\n"
            f'# WorkingInfo={{"mode":"{job.mode}"}}\n'
            f'# WorkingInfo={{"material":{json.dumps(job.material)}}}\n'
            f'# WorkingInfo={{"focalLen":{json.dumps(job.focal_len)}}}\n'
            f'# WorkingInfo={{"speed":{_num(summary.speed)}}}\n'
            f'# WorkingInfo={{"power":{_num(summary.power)}}}\n'
            + accessories(job) +
            f"M32X{ACCEL}\nG0 F{TRAVEL_FEED}\nG0 X0 Y0\n"
            "M4 S0\nG1 F1000\nM19 S1\nM18 S0\n\n")


def element_head(e: Element, mode: str) -> str:
    """Per-element setup, from the plugin's Vct(): head template + blockConfig + M117/M2519/M15."""
    air = round(max(10, e.air) * 2.55) if e.air else 0
    codes = (f"M117{'S3' if e.processing_type == 'VECTOR_CUTTING' else 'S1'}\n"
             f"M2519{PROCESSING_CODE[e.processing_type]}{MODE_TOOL.get(mode, 'T0')}\n"
             f"M15 S{air}\n")
    if not e.is_raster:
        return ("\n# P2s VECTOR HEAD\nG4 P0.1\nG90\n# motion_start\n"
                f'\n# blockConfig={{"powerFactor": {_num(round(e.power * 10 / 1000, 3))}, "isVector": true}} \n'
                + codes)
    lo, hi = e.power_range if e.processing_type == "BITMAP_ENGRAVING" else (0, e.power)
    cfg = {"powerFactor": round(e.power * 10 / 1000, 3), "density": f"{e.density / 10:.2f}",
           "power": [_int_or(lo * 10), _int_or(hi * 10)],
           "bitmapMode": e.bitmap_mode or "grayscale", "isVector": False}
    return ("# P2s BITMAP HEAD\nG4 P0.1\n\n\n\n# motion_start\n"
            f"# blockConfig={json.dumps(cfg, separators=(',', ':'))}\n" + codes)


def _int_or(v: float):
    return int(v) if float(v).is_integer() else round(v, 3)


VECTOR_TAIL = "# P2s VECTOR TAIL\n# motion_end\n"
BITMAP_TAIL = "# P2s BITMAP TAIL\n# motion_end\n"
END = ("# END\n# P2s TAIL\nG90\nG0 S0\nM3 S0\nG0 Z0\nG0 X0 Y0\n"
       "G0 U0\nG0 F10000\nG1 F10000\nG4 P0.1\nM109 S0\nM6 P1\n\n")
TAIL = VECTOR_TAIL + END     # kept for compatibility

# Raster (photo/fill) move format checked against a dithered photo job that
# Studio sent to the laser (fetched back with `p2s usb fetch tmp.bgcode`).
RASTER_VERIFIED = True


def _start_nearest(path, pos):
    """Closed paths start at the vertex nearest the head, like Studio's planner."""
    closed = len(path) > 2 and math.dist(path[0], path[-1]) < 1e-6
    if not closed:
        return path if math.dist(path[0], pos) <= math.dist(path[-1], pos) else path[::-1]
    ring = path[:-1]
    i = min(range(len(ring)), key=lambda k: math.dist(ring[k], pos))
    ring = ring[i:] + ring[:i]
    return ring + [ring[0]]


def overscan(speed_mm_s: float, accel: float = ACCEL) -> float:
    """Run-up distance so the head is at full speed when it reaches the image (plugin: ize)."""
    d = speed_mm_s ** 2 / accel / 2
    return 0.0 if d < 0.001 else d


def element_overscan(e: Element) -> float:
    """Plugin ize(): v^2/2a, but never running past the bed edges on either side."""
    xs = [x for _, runs in e.rows for a, b, _ in runs for x in (a, b)]
    if not xs:
        return 0.0
    d = min(overscan(e.speed), min(xs), BED_W - max(xs))
    return 0.0 if d < 0.001 else d


def _raster_moves(e: Element, pos, out, z_cb):
    """Raster block in Studio's format (checked against a P2S photo job fetched from the laser):

        G1F<feed>S0                 once per block
        G0X<x - overscan>Y<y>       first row only, then the focus move
        G1X<first burn>             laser-off run-up
        G1X<x>S<power>/G1X<x>S0     burns and gaps
        G1X<last burn + overscan>S0 run-out
        G1Y<next y>                 next row (back the other way in zMode)
        G1X<run-up start>           ...
    """
    over = element_overscan(e)
    out.append(f"G1F{_num(e.speed * 60)}S0\n")
    first = True
    flip = False
    for _ in range(max(1, e.passes)):
        for y, runs in e.rows:
            if not runs:
                continue
            reverse = flip and e.scan == "zMode"
            seq = [(b, a, s) for a, b, s in reversed(runs)] if reverse else list(runs)
            sign = -1 if reverse else 1
            x_first, x_last = seq[0][0], seq[-1][1]
            x_in, x_out = x_first - sign * over, x_last + sign * over
            if first:
                out.append(f"G0X{_c(x_in)}Y{_c(y)}\n")
                z_cb()
                first = False
            elif e.scan == "zMode":
                out.append(f"G1Y{_c(y)}\n")
                # only back up for the run-up if the last run-out didn't already go past it
                if (pos[0] - x_in) * sign > 1e-9:
                    out.append(f"G1X{_c(x_in)}\n")
                else:
                    x_in = pos[0]
            else:                               # one-way: travel back to the left with the laser off
                out.append(f"G0X{_c(x_in)}Y{_c(y)}\n")
            if abs(x_in - x_first) > 1e-9:
                out.append(f"G1X{_c(x_first)}\n")
            cur = x_first
            for a, b, s in seq:
                if abs(a - cur) > 1e-9:
                    out.append(f"G1X{_c(a)}S0\n")
                out.append(f"G1X{_c(b)}S{s}\n")
                cur = b
            out.append(f"G1X{_c(x_out)}S0\n")
            pos = (x_out, y)
            flip = not flip
    return pos


def generate(job: Job) -> str:
    if not job.elements:
        raise ValueError("nothing to process")
    out = [file_header(job), head(job, job.elements[-1])]
    pos = (0.0, 0.0)
    z_set = False

    def set_z(p):
        nonlocal z_set
        if not z_set and job.focal_len is not None:
            out.append(f"G0Z{job.focal_len:.3f}\nG0X{_c(p[0])}Y{_c(p[1])}\n")
            z_set = True

    def raster_z():
        # Studio repeats the focus move at the start of every photo/fill block
        nonlocal z_set
        if job.focal_len is not None:
            out.append(f"G0Z{_c(job.focal_len)}\n")
            z_set = True

    for i, e in enumerate(job.elements):
        out.append(element_head(e, job.mode))
        if e.is_raster:
            pos = _raster_moves(e, pos, out, raster_z)
        else:
            s, f = _num(e.power * 10), _num(e.speed * 60)
            for _ in range(max(1, e.passes)):
                for raw in e.paths:
                    path = _start_nearest(raw, pos)
                    x0, y0 = path[0]
                    out.append(f"G0X{_c(x0)}Y{_c(y0)}\n")
                    set_z(path[0])
                    first = True
                    for x, y in path[1:]:
                        out.append(f"G1X{_c(x)}Y{_c(y)}" + (f"S{s}F{f}" if first else "") + "\n")
                        first = False
                    pos = path[-1]
        nxt = job.elements[i + 1] if i + 1 < len(job.elements) else None
        # Studio runs same-kind elements back to back and closes the block when the kind changes
        if nxt is None or nxt.is_raster != e.is_raster:
            out.append(BITMAP_TAIL if e.is_raster else VECTOR_TAIL)
    out.append(END)
    return "".join(out)


def check_bounds(job: Job, width: float = BED_W, height: float = BED_H) -> None:
    for e in job.elements:
        if not 0 < e.power <= 100:
            raise ValueError(f"{e.shape}: power {e.power}% out of range")
        if not 0 < e.speed <= 1000:
            raise ValueError(f"{e.shape}: speed {e.speed} mm/s out of range")
        pts = [p for path in e.paths for p in path]
        pts += [(x, y) for y, runs in e.rows for a, b, _ in runs for x in (a, b)]
        for x, y in pts:
            if not (-1e-6 <= x <= width + 1e-6 and -1e-6 <= y <= height + 1e-6):
                raise ValueError(f"{e.shape} {e.id} goes outside the {width}x{height} mm bed at ({x:.1f}, {y:.1f})")
