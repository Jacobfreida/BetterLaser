"""Machine identification: the verified P2S passes, other P2S builds pass with notes, other lasers are refused."""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from p2s import compat

ref = {"deviceCode": "ZY013", "machineType": "P2S", "machineSubType": "004", "laserPower": 55,
       "gcodeFormat": ["txt", "bin"], "firmware": {"package_version": "40.22.011.2020.01.ht6"}}
d = compat.describe(ref)
assert d["supported"] and all(f["level"] == "ok" for f in d["findings"]), d
d = compat.describe({**ref, "machineSubType": "001", "laserPower": 90})
assert d["supported"] and [f["level"] for f in d["findings"]].count("warn") == 1, d
d = compat.describe({**ref, "deviceCode": "ZY015", "machineType": "P3"})
assert not d["supported"] and d["findings"][0]["level"] == "stop" and d["model"] == "P3", d
d = compat.describe({**ref, "gcodeFormat": ["txt"]})
assert not d["supported"], d
print("compat checks passed: P2S variants accepted (with notes), other lasers and text-only firmware refused")
