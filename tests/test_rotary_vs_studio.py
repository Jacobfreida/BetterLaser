"""Rotary (RA2 roller) job must reproduce a real Studio job fetched from the laser.

Studio's file engraves the word HELLO (score, 49 %, 20 mm/s, focus 17) on the roller
rotary. We take its outlines back out of the U/X moves, convert them to this app's
bed-aligned coordinates (x = X, y = axis line + U), run them through our own
design -> G-code pipeline and compare the whole file line by line.
"""
import os, re, sys
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from p2s import bgc, design, gcode

studio = bgc.decode(open(os.path.join(HERE, "fixtures", "studio_rotary_roller.bgc"), "rb").read()).splitlines()
AXIS = 125.0

paths, cur, x = [], None, None
for l in studio:
    m = re.fullmatch(r"G0X(-?[\d.]+)", l)
    if m:
        x = float(m[1]); continue
    m = re.fullmatch(r"G0U(-?[\d.]+)", l)
    if m:
        p = (x, AXIS + float(m[1]))
        if cur and cur[0] == p and len(cur) == 1:
            continue                                  # the repeat after the focus move
        cur = [p]; paths.append(cur); continue
    m = re.fullmatch(r"G1U(-?[\d.]+)X(-?[\d.]+)(?:S\d+F\d+)?", l)
    if m:
        cur.append((float(m[2]), AXIS + float(m[1])))

xs = [p[0] for q in paths for p in q]; ys = [p[1] for q in paths for p in q]
x0, y0, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
d = {"focal": 17, "fan_gear": 3, "purifier_gear": 2,
     "rotary": {"type": "roller", "axis_y": AXIS, "align": "edge"},
     "objects": [{"id": "hello", "type": "path", "x": x0, "y": y0, "w": w, "h": h, "rot": 0,
                  "nat_w": w, "nat_h": h, "paths": [[[px - x0, py - y0] for px, py in q] for q in paths],
                  "op": {"mode": "score", "power": 49, "speed": 20, "air": 100}}]}
job = design.build_job(d)
gcode.check_bounds(job)
ours = bgc.normalize(gcode.generate(job)).splitlines()

skip = ("# date=", "# version=", "# algorithmVersion=")
a = [l.rstrip() for l in studio if not l.startswith(skip)]
b = [l.rstrip() for l in ours if not l.startswith(skip)]
assert len(a) == len(b), (len(a), len(b))
for s_line, o_line in zip(a, b):           # same commands in the same order, S/F/Z included
    ks = re.sub(r"-?[\d.]+", "#", s_line); ko = re.sub(r"-?[\d.]+", "#", o_line)
    assert ks == ko, (s_line, o_line)
    if not re.match(r"G[01][UX]", s_line):
        assert s_line == o_line, (s_line, o_line)

def outlines(lines):
    out, cur, x = [], None, None
    for l in lines:
        m = re.fullmatch(r"G0X(-?[\d.]+)", l)
        if m:
            x = float(m[1]); continue
        m = re.fullmatch(r"G0U(-?[\d.]+)", l)
        if m:
            if not (cur and len(cur) == 1 and cur[0] == (float(m[1]), x)):
                cur = [(float(m[1]), x)]; out.append(cur)
            continue
        m = re.match(r"G1U(-?[\d.]+)X(-?[\d.]+)", l)
        if m:
            cur.append((float(m[1]), float(m[2])))
    return out

# every outline is the same loop of points; closed loops may start at a different vertex
# when two candidates are about equally near (Studio's tie-break isn't known)
worst = start_off = 0.0
for p, q in zip(outlines(a), outlines(b)):
    start_off = max(start_off, ((p[0][0] - q[0][0]) ** 2 + (p[0][1] - q[0][1]) ** 2) ** 0.5)
    ring_p, ring_q = p[:-1], q[:-1]
    k = min(range(len(ring_q)), key=lambda i: abs(ring_q[i][0] - ring_p[0][0]) + abs(ring_q[i][1] - ring_p[0][1]))
    ring_q = ring_q[k:] + ring_q[:k]
    for (u1, x1), (u2, x2) in zip(ring_p, ring_q):
        worst = max(worst, abs(u1 - u2), abs(x1 - x2))
assert worst <= 0.0011, worst
assert start_off <= 0.2, start_off
print(f"Rotary job matches Studio: {len(a)} lines, {len(paths)} outlines; header, M535U/G0Y setup, "
      f"T1 tool and tail identical; points within {worst:.4f} mm, start points within {start_off:.3f} mm")

# chuck: same moves with U flipped, and M535U = 128 / circumference
d["rotary"] = {"type": "chuck", "diameter": 80, "axis_y": AXIS, "align": "edge"}
ch = gcode.generate(design.build_job(d)).splitlines()
assert f"M535U{128 / (3.141592653589793 * 80)!r}" in ch
u_roll = [float(m[1]) for l in ours for m in [re.match(r"G1U(-?[\d.]+)", l)] if m]
u_chuck = [float(m[1]) for l in ch for m in [re.match(r"G1U(-?[\d.]+)", l)] if m]
assert all(abs(p + q) < 1e-9 for p, q in zip(u_roll, u_chuck))
print("Chuck mode: U mirrored, M535U = 128 / circumference")

# centre alignment (the default): the middle of the design goes to the top of the object,
# so U runs the same distance each way, wherever the design sits on the bed
for dy in (0, -40):
    d["rotary"] = {"type": "roller", "axis_y": AXIS}
    d["objects"][0]["y"] = y0 + dy
    g = bgc.normalize(gcode.generate(design.build_job(d))).splitlines()
    us = [float(m[1]) for l in g for m in [re.match(r"G[01]U(-?[\d.]+)", l)] if m]
    assert abs(min(us) + max(us)) < 0.002 and abs(max(us) - min(us) - h) < 0.002, (min(us), max(us))
    assert len(g) == len(ours) and "G0Y125" in g
print(f"Centre alignment: design spans U {min(us):.3f} to {max(us):.3f} (centred on the top of the object)")
