"""Command-line tool for the xTool P2S.

Only `send` puts a job on the machine, and it never starts it: you press the
P2S's start button yourself, with the lid closed, standing at the machine.

  python -m p2s.cli gui                         # open the Workbench app in your browser
  python -m p2s.cli usb status                  # 'usb' = USB-C cable (172.25.3.1)
  python -m p2s.cli 192.168.1.50 info
  python -m p2s.cli 192.168.1.50 status
  python -m p2s.cli 192.168.1.50 watch          # print live events (open the lid, press buttons)
  python -m p2s.cli 192.168.1.50 snapshot out.jpg [--stream 1]
  python -m p2s.cli 192.168.1.50 probe          # try every read-only route, save results to probe.json
  python -m p2s.cli usb check                   # is this laser compatible? read-only; saves check.json
  python -m p2s.cli convert project.xcs out.gcode  # offline: project file -> P2S G-code
  python -m p2s.cli 192.168.1.50 send project.xcs  # load a job (or a .gcode file); start it on the machine
  python -m p2s.cli usb fetch tmp.bgcode job.bgc  # read-only: download a file from the machine
  python -m p2s.cli decode job.bgc job.txt          # offline: binary job -> readable G-code
  python -m p2s.cli usb calib                       # read-only: save camera/IR calibration files
  python -m p2s.cli 192.168.1.50 pause
  python -m p2s.cli 192.168.1.50 stop
"""
from __future__ import annotations

import argparse
import json
import logging
import time

from .device import P2S, DeviceError

# Read-only routes worth recording from a real machine (all GET).
PROBE_ROUTES = [
    ("/v1/device/machineInfo", {}, {}),
    ("/v1/device/runningStatus", {}, {}),
    ("/v1/device/workingInfo", {}, {}),
    ("/v1/processing/progress", {}, {}),
    ("/v1/peripheral/param", {"type": "gap"}, {}),
    ("/v1/peripheral/param", {"type": "machine_lock"}, {}),
    ("/v1/peripheral/param", {"type": "ext_purifier"}, {}),
    ("/v1/laser-head/focus/parameter", {}, {}),
    ("/v1/config/get", {}, {"alias": "config", "type": "user", "kv": [
        "flameAlarm", "beepEnable", "gapCheck", "machineLockCheck", "workingMode",
        "airAssistDelay", "airassistCut", "airassistGrave", "taskId", "globalOffsetZ",
        "laserOffsetRed", "laserOffsetBlue", "zMoveSpeed"]}),
]


def convert(argv) -> None:
    from . import gcode, xcs
    src, dst = argv[0], argv[1] if len(argv) > 1 else argv[0].rsplit(".", 1)[0] + ".gcode"
    job = xcs.load(src)
    gcode.check_bounds(job)
    open(dst, "w", newline="\n").write(gcode.generate(job))
    for e in job.elements:
        print(f"  {e.shape:7} {e.processing_type:17} power {e.power:g}%  speed {e.speed:g} mm/s  passes {e.passes}")
    print(f"Wrote {dst}")
    if not gcode.MOTION_VERIFIED:
        print("NOTE: move-line format is provisional - for comparison only, do not run on the machine.")


def main() -> None:
    import sys
    if len(sys.argv) > 1 and sys.argv[1] in ("gui", "app"):
        from .app import run
        port = int(sys.argv[2]) if len(sys.argv) > 2 else 8765
        return run(port)
    if len(sys.argv) > 1 and sys.argv[1] == "convert":
        return convert(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "decode":
        from . import bgc
        src = sys.argv[2]
        dst = sys.argv[3] if len(sys.argv) > 3 else src + ".txt"
        text = bgc.decode(open(src, "rb").read())
        open(dst, "w", newline="\n").write(text)
        print(f"Wrote {dst} ({text.count(chr(10))} lines)")
        return
    ap = argparse.ArgumentParser(prog="p2s")
    ap.add_argument("host", help="'usb' for a USB-C connection, or the P2S's Wi-Fi IP address")
    ap.add_argument("command", choices=["info", "status", "watch", "snapshot", "probe", "check", "send", "pause", "stop", "fetch", "calib"])
    ap.add_argument("out", nargs="?", default="snapshot.jpg")
    ap.add_argument("dest", nargs="?", help="fetch: local file to save to")
    ap.add_argument("--filetype", type=int, default=1, help="fetch: 1 = job files, 5 = logs/images")
    ap.add_argument("--stream", default="0", help="camera: 0 = overview, 1 = close-up")
    ap.add_argument("--format", choices=["auto", "bin", "txt"], default="auto",
                    help="send: job format; auto = whatever xTool Studio would use (bin on the P2S)")
    ap.add_argument("-y", "--yes", action="store_true", help="send: skip the confirmation prompt")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--port", type=int, default=28900, help=argparse.SUPPRESS)
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO, format="%(message)s")

    if a.host.lower() == "usb":
        # USB-C: the P2S appears as a private network adapter that reaches only the laser.
        # Same addresses xTool Studio uses (USB_IP_CONFIGS, V2 firmware).
        import platform
        a.host = "172.25.4.1" if platform.system() == "Darwin" else "172.25.3.1"
    job_text = None
    if a.command == "send":
        job_text = load_job(a.out)
        if not job_text:
            return

    print(f"Connecting to {a.host}:{a.port} ... (close xTool Studio first)")
    try:
        p = P2S(a.host, port=a.port).connect()
    except OSError as e:
        print(f"Could not connect: {e}")
        print("Check: USB-C cable plugged in and laser powered on, xTool Studio fully closed "
              "(also from the system tray), and the laser shows in Studio when Studio is open.")
        raise SystemExit(1)
    with p:
        print("Connected, handshake OK.")
        if a.command == "info":
            show(p.machine_info())
        elif a.command == "status":
            show(p.status())
            try:
                show({"lid": p.lid()})
            except DeviceError as e:
                print("lid:", e)
        elif a.command == "snapshot":
            img = p.camera_snapshot(a.stream)
            open(a.out, "wb").write(img)
            print(f"Saved {len(img)} bytes to {a.out}")
        elif a.command == "watch":
            p.on_event(lambda ev: show(ev))
            print("Watching events. Open/close the lid or press a button. Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                pass
        elif a.command == "send":
            print("Machine status:")
            show(p.status())
            if not a.yes and input("Load this job onto the P2S? Type yes: ").strip().lower() != "yes":
                print("Cancelled.")
                return
            t = time.time()
            task, fmt = p.send_job(job_text, fmt=a.format, progress=lambda done, total: print(
                f"\r  uploading {done * 100 // total}%", end="", flush=True))
            print(f"\nJob loaded in {time.time() - t:.1f}s ({'binary tmp.bgcode' if fmt == 'bin' else 'text tmp.gcode'}, task {task}).")
            st = p.status().get("curMode", {})
            print(f"Machine now: {st.get('mode')} {st.get('subMode')}  task {st.get('taskId')}")
            print("Close the lid and press the START button on the P2S to run it.")
            print(f"To cancel: python -m p2s.cli {a.host} stop")
        elif a.command == "fetch":
            name = a.out if a.out != "snapshot.jpg" else "tmp.bgcode"
            dest = a.dest or name.replace("/", "_")
            try:
                data = p.download(name, a.filetype)
            except DeviceError as e:
                print(f"Could not download {name} (filetype {a.filetype}): {e}")
                raise SystemExit(1)
            open(dest, "wb").write(data)
            print(f"Saved {len(data)} bytes to {dest}")
        elif a.command == "calib":
            import zipfile
            out = "p2s_calibration.zip"
            with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
                for name in ("global_calib.json", "local_calib.json", "global_ir.txt", "local_ir.txt"):
                    try:
                        data = p.request("/v1/file/content", "GET", params={"type": name}, timeout=20)
                        body = data if isinstance(data, str) else json.dumps(data)
                        z.writestr(name, body)
                        print(f"OK   {name} ({len(body)} chars)")
                    except DeviceError as e:
                        print(f"FAIL {name}: {e}")
                cfg = p.request("/v1/config/get", data={"alias": "config", "type": "user",
                                                        "kv": ["laserMeasureOffset", "laserOffsetRed", "globalOffsetZ"]})
                z.writestr("config.json", json.dumps(cfg, indent=2))
                print("OK   config", cfg)
                for stream in ("0", "1"):
                    try:
                        z.writestr(f"camera{stream}.jpg", p.camera_snapshot(stream))
                        print(f"OK   camera {stream} snapshot")
                    except Exception as e:
                        print(f"FAIL camera {stream}: {e}")
            print(f"Saved {out}. Send me this file.")
        elif a.command in ("pause", "stop"):
            show(p.set_state(a.command))
            print(f"{a.command} sent.")
        elif a.command == "check":
            run_check(p, a.out if a.out != "snapshot.jpg" else "check.json")
        elif a.command == "probe":
            results = []
            for url, params, data in PROBE_ROUTES:
                try:
                    r = {"ok": True, "data": p.request(url, params=params, data=data)}
                except DeviceError as e:
                    r = {"ok": False, "error": str(e), "response": e.response}
                results.append({"url": url, "params": params, "result": r})
                print(("OK   " if r["ok"] else "FAIL ") + url, params or "")
            out = a.out if a.out != "snapshot.jpg" else "probe.json"
            json.dump(results, open(out, "w"), indent=2)
            print(f"Saved results to {out}. Send me this file.")


def run_check(p, out_path: str) -> None:
    """Read-only compatibility check: identify the laser and try every feature's read path.

    Nothing moves and the laser never fires. Saves a report (serial number shortened)."""
    from . import compat
    report = {"steps": []}

    def step(name, fn):
        try:
            v = fn()
            report["steps"].append({"step": name, "ok": True, "result": v})
            print(f"  OK    {name}")
            return v
        except Exception as e:                              # noqa: BLE001 - report everything
            report["steps"].append({"step": name, "ok": False, "error": str(e)})
            print(f"  FAIL  {name}: {e}")
            return None

    info = p.machine_info() or {}
    d = compat.describe(info)
    sn = str(info.get("sn") or "")
    report["machine"] = {k: info.get(k) for k in ("deviceCode", "machineType", "machineSubType", "laserPower",
                                                  "laserType", "gcodeFormat", "hardware", "deviceName")}
    report["machine"]["sn"] = sn[:10] + "…" if sn else ""
    report["machine"]["firmware"] = (info.get("firmware") or {}).get("version")
    report["summary"] = d
    print(f"\nLaser: {d['label']}  (code {d['code']}, sub-type {d['subType'] or '?'}, firmware {d['firmware'] or '?'})")
    for f in d["findings"]:
        print({"ok": "  OK    ", "warn": "  NOTE  ", "stop": "  STOP  "}[f["level"]] + f["message"])
    print("\nFeature checks (read-only):")
    step("status", lambda: (p.status() or {}).get("curMode"))
    step("lid sensor", lambda: p.lid())
    step("key switch", lambda: p.request("/v1/peripheral/param", params={"type": "machine_lock"}))
    step("height sensor offset (laserMeasureOffset)", lambda: p.request(
        "/v1/config/get", data={"alias": "config", "type": "user", "kv": ["laserMeasureOffset"]}))
    step("head position (get_coord)", lambda: p.request(
        "/v1/laser-head/control", "POST", data={"action": "get_coord", "waitTime": 0}, timeout=10))
    cal = step("camera calibration (global_calib.json)", lambda: len(str(p.request(
        "/v1/file/content", params={"type": "global_calib.json"}, timeout=20) or "")))
    for stream, label in (("0", "lid camera"), ("1", "head camera")):
        jpg = step(f"{label} photo", lambda s=stream: len(p.camera_snapshot(s)))
    json.dump(report, open(out_path, "w"), indent=2, default=str)
    ok = d["supported"] and all(s["ok"] for s in report["steps"])
    print(f"\n{'Looks compatible.' if ok else 'See the notes above.'} Saved {out_path}; send it over to compare with the verified P2S.")


def load_job(path: str) -> str | None:
    from . import gcode, xcs
    if path.lower().endswith(".xcs"):
        job = xcs.load(path)
        gcode.check_bounds(job)
        print(f"Job from {path} ({job.mode}, focal length {job.focal_len} mm):")
        for e in job.elements:
            print(f"  {e.shape:7} {e.processing_type:17} power {e.power:g}%  speed {e.speed:g} mm/s  passes {e.passes}")
        return gcode.generate(job)
    text = open(path, encoding="utf-8").read()
    if "# P2s HEAD" not in text:
        print("That file doesn't look like P2S G-code (no '# P2s HEAD'). Not sending it.")
        return None
    print(f"G-code file {path}: {text.count(chr(10))} lines")
    return text


def show(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
