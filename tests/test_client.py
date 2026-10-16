import os, sys, hashlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from p2s import framing as fr
from p2s.device import P2S
from fake_p2s import FakeP2S

# CRC table must match the one in xTool Studio (Kl[0..3] = 0, 49345, 49537, 320)
assert fr._T[:4] == [0, 49345, 49537, 320], fr._T[:4]
# Round trip + resync after garbage
f = fr.encode('{"a":1}', fr.JSON)
r = fr.FrameReader()
assert r.feed(b"\x00\xBA" + f[:5]) == []
assert r.feed(f[5:] + f) == [(4, b'{"a":1}')] * 2
bad = bytearray(f); bad[12] ^= 1
assert fr.FrameReader().feed(bytes(bad)) == []

dev = FakeP2S(window=300_000)
data = os.urandom(1_234_567)
seen = []
with P2S("127.0.0.1", port=dev.port) as p:
    assert dev.parity_ok
    assert p.status() == {"curMode": {"mode": "P_IDLE"}}
    p.upload(data, "tmp.gcode", progress=lambda a, b: seen.append(a))
    assert dev.uploads["tmp.gcode"] == data
    assert seen[-1] == len(data)
    jpg = p.camera_snapshot()
    assert jpg == dev.files["snap.jpg"]
    from p2s import bgc
    task, fmt = p.send_job("G0 X0 Y0\n# comment\nG1X1.5Y2S100F12000\n")
    assert fmt == "bin" and bgc.decode(dev.uploads["tmp.bgcode"]) == "G0 X0 Y0\n# comment\nG1X1.5Y2S100F12000\n"
    assert dev.log[-1]["data"] == {"fileType": "bin", "autoStart": 0, "taskId": task}
    task, fmt = p.send_job("G0 X0 Y0\n", fmt="txt")
    assert fmt == "txt" and dev.uploads["tmp.gcode"] == b"G0 X0 Y0\n"
    urls = [x["url"] for x in dev.log]
print("ALL OK", urls)
