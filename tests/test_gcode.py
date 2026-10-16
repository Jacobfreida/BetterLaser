"""Our G-code must match a real xTool Studio 1.7.30 export of the same project."""
import math, os, re, sys
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from p2s import gcode, xcs

job = xcs.load(os.path.join(HERE, "fixtures", "square_circle.xcs"))
gcode.check_bounds(job)
ours = gcode.generate(job).splitlines()
studio = open(os.path.join(HERE, "fixtures", "studio_square_circle.gcode")).read().splitlines()

skip = ("# date", "# version")
mv = re.compile(r"^G[01]X([-\d.]+)Y([-\d.]+)(S\d+F\d+)?$")
a = [l for l in studio if not l.startswith(skip)]
b = [l for l in ours if not l.startswith(skip)]
assert [l for l in a if not mv.match(l)] == [l for l in b if not mv.match(l)], "setup/header lines differ"
ma = [mv.match(l) for l in a if mv.match(l)]
mb = [mv.match(l) for l in b if mv.match(l)]
assert len(ma) == len(mb), (len(ma), len(mb))
def seg_dist(p, a, b):
    ax, ay = a; bx, by = b; px, py = p
    dx, dy = bx - ax, by - ay
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L))
    return math.hypot(px - ax - t * dx, py - ay - t * dy)

pa = [(float(m[1]), float(m[2])) for m in ma]
pb = [(float(m[1]), float(m[2])) for m in mb]
assert [m[3] for m in ma if m[3]] == [m[3] for m in mb if m[3]], "power/speed lines differ"
worst = max(min(seg_dist(p, pa[i], pa[i + 1]) for i in range(len(pa) - 1)) for p in pb)
start = max(math.dist(x, y) for x, y in [(pa[0], pb[0])] + [(pa[i], pb[i]) for i, m in enumerate(ma) if m[3]])
assert worst < 0.01, worst
assert start < 0.05, start
print(f"G-code matches Studio export: {len(mb)} moves; every point within {worst:.4f} mm of Studio's path; "
      f"start points within {start:.3f} mm")
