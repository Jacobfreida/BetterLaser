"""Geometry checks for the app's design model: fill, rotation, text sizing, image power."""
import base64, glob, math, os, sys
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from p2s import design, gcode, bgc, fonts
from p2s.raster import fill_rows

def close(a, b, tol=1e-6): return abs(a - b) <= tol

# 1. fill: a 10 x 10 square at 10 lines/mm covers exactly 10 mm per line, 100 lines
sq = [[(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]]
rows = fill_rows(sq, 0.1)
assert len(rows) == 100 and all(close(b - a, 10) for _, segs in rows for a, b in segs)

# 2. nonzero rule: two overlapping squares fill as their union (no hole in the overlap)
two = sq + [[(5, 0), (15, 0), (15, 10), (5, 10), (5, 0)]]
assert all(len(segs) == 1 and close(segs[0][1] - segs[0][0], 15) for _, segs in fill_rows(two, 0.5))
# ...and a reversed inner contour (a letter "O") does leave a hole
ring = sq + [[(3, 3), (3, 7), (7, 7), (7, 3), (3, 3)]]
mid = [segs for y, segs in fill_rows(ring, 0.5) if 3 < y < 7]
assert mid and all(len(s) == 2 for s in mid)

# 3. rotation about the centre: a 40 x 10 rect rotated 90 degrees spans 10 x 40
o = {"type": "rect", "x": 100, "y": 100, "w": 40, "h": 10, "rot": 90}
pts = [p for path in design.place(*design.local_geometry(o), o) for p in path]
xs, ys = [p[0] for p in pts], [p[1] for p in pts]
assert close(max(xs) - min(xs), 10, 1e-6) and close(max(ys) - min(ys), 40, 1e-6)
assert close((max(xs) + min(xs)) / 2, 120) and close((max(ys) + min(ys)) / 2, 105)

# 4. text fills its box exactly (outline bbox == object box)
font = next(iter(glob.glob("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf") + [f["id"] for f in fonts.list_fonts()]))
if "|" not in font:
    font += "|0"
o = {"type": "text", "x": 20, "y": 30, "w": 70, "h": 15, "rot": 0, "text": {"content": "Hello", "font": font, "size": 20}}
pts = [p for path in design.place(*design.local_geometry(o), o) for p in path]
assert close(min(p[0] for p in pts), 20, 1e-6) and close(max(p[0] for p in pts), 90, 1e-6)
assert close(min(p[1] for p in pts), 30, 1e-6) and close(max(p[1] for p in pts), 45, 1e-6)

# 5. image power: black -> max, white -> nothing, grey -> between min and max
px = bytes([0, 128, 255, 0])
d = {"focal": 15.5, "objects": [{"id": "i", "type": "image", "x": 10, "y": 10, "w": 4, "h": 1,
     "image": {"cols": 4, "rows": 1, "data": base64.b64encode(px).decode()},
     "op": {"mode": "image", "power": 50, "min_power": 10, "speed": 100, "density": 10, "image_mode": "grayscale"}}]}
job = design.build_job(d)
(_, runs), = job.elements[0].rows
assert [s for _, _, s in runs] == [500, round(100 + 400 * 127 / 255), 500], runs
assert close(runs[0][0], 10) and close(runs[-1][1], 14)

# 6. a full mixed job encodes to BGC and back, cuts come last, one focus move
d = {"focal": 12.0, "objects": [
    {"id": "c", "type": "ellipse", "x": 50, "y": 50, "w": 30, "h": 30, "op": {"mode": "cut", "power": 80, "speed": 15}},
    {"id": "f", "type": "rect", "x": 10, "y": 10, "w": 20, "h": 5, "op": {"mode": "fill", "power": 20, "speed": 200, "density": 50}}]}
g = gcode.generate(design.build_job(d))
assert bgc.decode(bgc.encode(g)) == bgc.normalize(g)
lines = g.splitlines()
assert lines.index("M2519S4T0") < lines.index("M2519S3T0")
assert [l for l in lines if l.startswith("G0Z")] == ["G0Z12"]
assert lines[-15:].count("# END") == 1

# 7. text wrapped round a circle sits outside it, centred over the top; inside text along the bottom
circ = {"id": "c", "type": "ellipse", "x": 100, "y": 100, "w": 100, "h": 100, "op": {"mode": "off"}}
top = design.wrap_text_paths({"content": "HELLO", "font": font, "size": 10, "wrap": {"target": "c", "side": "outside", "gap": 2}}, circ)
pts = [p for q in top for p in q]
r = [math.dist(p, (150, 150)) for p in pts]
assert min(r) >= 50 + 2 - 0.3 and max(r) <= 50 + 2 + 10.5, (min(r), max(r))
assert abs((min(p[0] for p in pts) + max(p[0] for p in pts)) / 2 - 150) < 1.5 and max(p[1] for p in pts) < 110
bot = design.wrap_text_paths({"content": "HELLO", "font": font, "size": 10, "wrap": {"target": "c", "side": "inside", "gap": 2}}, circ)
pts = [p for q in bot for p in q]
assert max(math.dist(p, (150, 150)) for p in pts) <= 50 - 2 + 0.3 and min(p[1] for p in pts) > 180
d = {"focal": 10, "objects": [circ, {"id": "t", "type": "text", "x": 0, "y": 0, "w": 1, "h": 1,
     "text": {"content": "HELLO", "font": font, "size": 10, "wrap": {"target": "c"}}, "op": {"mode": "score", "power": 10, "speed": 100}}]}
assert len(design.build_job(d).elements[0].paths) == len(top)

# 8. a line is an open path: scored end to end, never closed
line = {"id": "l", "type": "path", "shape": {"kind": "line"}, "x": 10, "y": 10, "w": 50, "h": 0.01, "nat_w": 50, "nat_h": 0.01,
        "paths": [[[0, 0], [50, 0.01]]], "op": {"mode": "score", "power": 10, "speed": 100}}
g = gcode.generate(design.build_job({"focal": 10, "objects": [line]})).splitlines()
assert [l for l in g if l.startswith(("G0X", "G1X"))] == ["G0X10Y10", "G0X10Y10", "G1X60Y10.01S100F6000"], g
print("design checks passed: fill, nonzero holes/overlaps, rotation, text box, image power, job order, "
      "text wrapped round shapes, lines")
