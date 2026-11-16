"""Photo engraving must reproduce a real Studio job (a dithered dog, fetched from the laser).

We read the burned pixels back out of Studio's file, run them through our own design ->
G-code pipeline with the same settings, and compare every line of the block.
"""
import base64, os, re, sys
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from p2s import bgc, design, gcode

lines = bgc.decode(open(os.path.join(HERE, "fixtures", "studio_dogs.bgc"), "rb").read()).splitlines()
a = lines.index("G1F18000S0")
b = lines.index("# P2s BITMAP TAIL")
block = lines[a:b]

# --- pull the rows (y, [(x0, x1, S)]) out of Studio's moves
rows, y, x, s, cur = [], None, None, 0, None
for l in block:
    m = re.fullmatch(r"G0X([\d.]+)Y([\d.]+)", l)
    if m:
        x, y = float(m[1]), float(m[2]); cur = []; rows.append((y, cur)); continue
    m = re.fullmatch(r"G1Y([\d.]+)", l)
    if m:
        y = float(m[1]); cur = []; rows.append((y, cur)); continue
    m = re.fullmatch(r"G1X([\d.]+)(?:S(\d+))?", l)
    if m:
        nx = float(m[1]); s = int(m[2]) if m[2] else s
        if s:
            cur.append((min(x, nx), max(x, nx), s))
        x = nx

pitch = 10 / 120                      # density "12.00" lines/mm
left = min(r[0][0] for _, r in rows if r)
right = max(r[-1][1] for _, r in rows if r)
cols = round((right - left) / pitch)
n = len(rows)
px = bytearray([255]) * (cols * n)
for i, (_, segs) in enumerate(rows):
    for x0, x1, _ in segs:
        for c in range(round((x0 - left) / pitch), round((x1 - left) / pitch)):
            px[i * cols + c] = 0

d = {"focal": 23.4, "objects": [{"id": "dog", "type": "image", "x": left, "y": rows[0][0] - pitch / 2,
     "w": cols * pitch, "h": n * pitch, "rot": 0,
     "image": {"cols": cols, "rows": n, "data": base64.b64encode(bytes(px)).decode()},
     "op": {"mode": "image", "power": 8, "speed": 300, "density": 120, "air": 25,
            "image_mode": "dither", "dither": "Jarvis", "scan": "zMode"}}]}
ours = gcode.generate(design.build_job(d)).splitlines()
oa = ours.index("G1F18000S0")
ob = ours.index("# P2s BITMAP TAIL")
mine = ours[oa:ob]

# the block setup lines must match exactly
head_studio = lines[lines.index("# P2s BITMAP HEAD"):a]
head_ours = [l for l in ours[ours.index("# P2s BITMAP HEAD"):oa] if l.strip()]
assert head_studio == head_ours, (head_studio, head_ours)

assert len(mine) == len(block), (len(mine), len(block))
worst = 0.0
for s_line, o_line in zip(block, mine):
    ks = re.sub(r"[\d.]+", "#", s_line); ko = re.sub(r"[\d.]+", "#", o_line)
    assert ks == ko, (s_line, o_line)
    for p, q in zip(re.findall(r"[\d.]+", s_line), re.findall(r"[\d.]+", o_line)):
        worst = max(worst, abs(float(p) - float(q)))
assert worst <= 0.0011, worst
print(f"Photo block matches Studio: {len(block)} lines, {n} rows x {cols} px, "
      f"identical commands and powers, coordinates within {worst:.4f} mm")
