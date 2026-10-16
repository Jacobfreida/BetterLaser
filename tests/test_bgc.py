"""BGC encoder must round-trip through a port of Studio's own decoder."""
import os, sys
HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from p2s import bgc, gcode, xcs

samples = [open(os.path.join(HERE, "fixtures", "studio_square_circle.gcode")).read(),
           gcode.generate(xcs.load(os.path.join(HERE, "fixtures", "square_circle.xcs")))]
for src in samples:
    plain = bgc.normalize(src)
    back = bgc.decode(bgc.encode(src))
    assert back == plain, [(a, b) for a, b in zip(plain.splitlines(), back.splitlines()) if a != b][:3]
assert bgc.decode(bgc.encode('G0Z15.500\nG198 P84 "M9033 V75"\nG1X-1.25Y0.05\n')) == 'G0Z15.500\nG198 P84 "M9033 V75"\nG1X-1.25Y0.05\n'
# Studio's own binary job (fetched from the laser) must re-encode byte for byte
studio = os.path.join(HERE, "fixtures", "studio_dogs.bgc")
if os.path.exists(studio):
    raw = open(studio, "rb").read()
    assert bgc.encode(bgc.decode(raw)) == raw, "re-encoding Studio's BGC changed it"
print(f"BGC round-trip exact ({len(samples)} files, e.g. {len(samples[0])} B text -> {len(bgc.encode(samples[0]))} B bgc)")
