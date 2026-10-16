"""p2s-tools desktop app: a local web server + browser UI (no installs needed).

    python p2s.py gui            (or double-click p2s-app.bat on Windows)

Only listens on 127.0.0.1, so nothing else on the network can reach it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import bgc, design, fonts, gcode, xcs
from .device import P2S, DeviceError

log = logging.getLogger("p2s.app")
HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "ui", "index.html")
USB_HOST = "172.25.3.1" if sys.platform != "darwin" else "172.25.4.1"
IR_OFFSET_X_DEFAULT = 68.0      # plugin Wit[RULE_TWO]: IR sensor sits 68 mm right of the laser
HEAD_FEED = 9600                # plugin w7


def settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "p2s-tools")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def load_settings() -> dict:
    try:
        return json.load(open(settings_path(), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(s: dict) -> None:
    json.dump(s, open(settings_path(), "w", encoding="utf-8"), indent=2)


class Machine:
    """One shared connection to the laser, used by every request."""

    def __init__(self):
        self.dev: P2S | None = None
        self.host = None
        self.info = {}
        self.lock = threading.RLock()
        self.events = []
        self.last_error = ""

    @property
    def connected(self) -> bool:
        return bool(self.dev and self.dev._alive)

    def connect(self, host: str):
        with self.lock:
            self.disconnect()
            host = USB_HOST if host.lower() == "usb" else host
            port = 28900
            if host.count(":") == 1:
                host, port = host.split(":")[0], int(host.split(":")[1])
            dev = P2S(host, timeout=8, port=port).connect()
            dev.on_event(self._event)
            self.dev, self.host = dev, host
            self.info = dev.machine_info() or {}
            return self.info

    def disconnect(self):
        if self.dev:
            try:
                self.dev.close()
            except Exception:
                pass
        self.dev = None

    def _event(self, ev):
        self.events.append({"t": time.time(), **ev})
        del self.events[:-50]

    def need(self) -> P2S:
        if not self.connected:
            raise DeviceError("Not connected to the laser. Click Connect first (and close xTool Studio).")
        return self.dev

    def status(self) -> dict:
        if not self.connected:
            return {"connected": False, "error": self.last_error}
        d = self.dev
        out = {"connected": True, "host": self.host, "name": self.info.get("deviceName"),
               "firmware": (self.info.get("firmware") or {}).get("package_version")}
        try:
            st = d.status() or {}
            cm = st.get("curMode") or {}
            out.update(mode=cm.get("mode"), subMode=cm.get("subMode"), taskId=cm.get("taskId"),
                       alarms=(st.get("curAlarmInfo") or {}).get("alarm"))
            out["lidClosed"] = (d.request("/v1/peripheral/param", params={"type": "gap"}) or {}).get("state") == "off"
            out["keyIn"] = (d.request("/v1/peripheral/param", params={"type": "machine_lock"}) or {}).get("state") == "on"
            if cm.get("mode") not in (None, "P_IDLE", "P_SLEEP"):
                out["progress"] = d.progress()
        except Exception as e:
            out["error"] = str(e)
        out["events"] = self.events[-5:]
        return out

    # -- height measurement (Studio's accurateMeasure, RULE_TWO) -------------------
    def ir_offset_x(self) -> float:
        try:
            cfg = self.dev.request("/v1/config/get", data={"alias": "config", "type": "user",
                                                           "kv": ["laserMeasureOffset"]}) or {}
            s = cfg.get("laserMeasureOffset") or ""
            if "X" in s and "Y" in s:
                return float(s[s.index("X") + 1:s.index("Y")])
        except Exception:
            pass
        return IR_OFFSET_X_DEFAULT

    def measure(self, x: float, y: float) -> dict:
        with self.lock:
            d = self.need()
            off = self.ir_offset_x()
            hx = x - off                 # put the IR sensor (not the laser) over the clicked point
            if hx < 0 or hx > 620 or y < 0 or y > 308:
                raise DeviceError(f"Can't measure there: the sensor is {off:.0f} mm right of the laser head, "
                                  f"so pick a point at least {off:.0f} mm from the left edge.")
            try:
                d.request("/v1/laser-head/control", "POST", timeout=35,
                          data={"action": "go_to", "waitTime": 30000, "f": HEAD_FEED, "x": hx, "y": y})
                d.request("/v1/peripheral/param", "PUT", params={"type": "fill_light"},
                          data={"action": "set_bri", "idx": 0, "value": 0})
                d.request("/v1/peripheral/param", "PUT", params={"type": "ir_led"}, data={"action": "on", "index": 1})
                time.sleep(0.1)
                r = d.request("/v1/peripheral/param", "PUT", params={"type": "ir_measure_distance"},
                              data={"action": "get_distance", "type": "single"}, timeout=20) or {}
            finally:
                for args in ((("/v1/peripheral/param", "PUT"), {"params": {"type": "ir_led"}, "data": {"action": "off", "index": 1}}),
                             (("/v1/peripheral/param", "PUT"), {"params": {"type": "fill_light"}, "data": {"action": "set_bri", "idx": 2, "value": 255}}),
                             (("/v1/peripheral/param", "PUT"), {"params": {"type": "fill_light"}, "data": {"action": "set_bri", "idx": 3, "value": 255}}),
                             (("/v1/laser-head/control", "POST"), {"data": {"action": "go_to", "waitTime": 0, "f": HEAD_FEED, "x": 0, "y": 0}})):
                    try:
                        d.request(*args[0], **args[1])
                    except Exception:
                        pass
            dist = r.get("distance")
            if dist is None:
                raise DeviceError(f"The machine didn't return a distance: {r}")
            dist = float(dist)
            focal = 25 - dist            # honeycomb mode: k7(...).value = 25 - z
            ok = -50 <= focal <= 25
            return {"distance": dist, "focal": round(focal, 2), "ok": ok,
                    "message": "" if ok else "Out of range: is there material under the sensor?"}


MACHINE = Machine()


def job_text(design_json: dict) -> tuple[str, xcs.Job]:
    job = design.build_job(design_json)
    gcode.check_bounds(job)
    return gcode.generate(job), job


def estimate_seconds(job) -> float:
    t = 0.0
    for e in job.elements:
        v = max(e.speed, 1)
        n = max(1, e.passes)
        for p in e.paths:
            t += n * sum(((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5 for a, b in zip(p, p[1:])) / v
        for _, runs in e.rows:
            if runs:
                t += n * ((runs[-1][1] - runs[0][0]) + 2 * gcode.overscan(e.speed)) / v + 0.05
    return t


def xcs_to_design(doc: dict) -> dict:
    """Bring a Studio project's rectangles and circles into the app."""
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".xcs", delete=False, encoding="utf-8") as f:
        json.dump(doc, f)
    try:
        canvas_id = doc["canvasId"]
        canvas = next(c for c in doc["canvas"] if c["id"] == canvas_id)
        dev = dict(doc["device"]["data"]["value"])[canvas_id]
        dev_displays = dict(dev["displays"]["value"])
        cfg = dev["data"].get(dev["mode"], {})
        objs, skipped = [], []
        for d in canvas["displays"]:
            entry = dev_displays.get(d["id"]) or d
            t = {"RECT": "rect", "CIRCLE": "ellipse"}.get(d["type"])
            ptype = entry.get("processingType") or d.get("processingType")
            if not t:
                skipped.append(d["type"]); continue
            p = xcs._params(entry, ptype)
            mode = {"VECTOR_ENGRAVING": "score", "VECTOR_CUTTING": "cut", "FILL_VECTOR_ENGRAVING": "fill"}.get(ptype, "score")
            objs.append({"id": d["id"], "type": t, "x": d["x"], "y": d["y"], "w": abs(d["width"]),
                         "h": abs(d["height"]), "rot": d.get("angle", 0) or 0,
                         "op": {"mode": mode, "power": p.get("power", 10), "speed": p.get("speed", 100),
                                "passes": p.get("repeat", 1), "air": p.get("airPump", 25),
                                "density": p.get("density", 100)}})
        return {"focal": cfg.get("focalLength"), "fan_gear": cfg.get("fanGear", 3),
                "purifier_gear": cfg.get("purifierGear", 4), "objects": objs, "skipped": skipped}
    finally:
        os.unlink(f.name)


class Handler(BaseHTTPRequestHandler):
    server_version = "p2s-tools"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode())

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        try:
            if self.path in ("/", "/index.html"):
                return self._send(200, open(UI_FILE, "rb").read(), "text/html; charset=utf-8")
            if self.path == "/api/status":
                return self._json(MACHINE.status())
            if self.path == "/api/fonts":
                return self._json(fonts.list_fonts())
            if self.path == "/api/settings":
                return self._json(load_settings())
            self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._error(e)

    def do_POST(self):
        # Only accept requests from our own page (blocks other websites in the same browser).
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and not origin.endswith("//" + host):
            return self._json({"error": "forbidden origin"}, 403)
        try:
            b = self._body()
            p = self.path
            if p == "/api/connect":
                info = MACHINE.connect(b.get("host") or "usb")
                return self._json({"ok": True, "info": info})
            if p == "/api/disconnect":
                MACHINE.disconnect(); return self._json({"ok": True})
            if p == "/api/text":
                paths, w, h = design.text_geometry(b)
                return self._json({"paths": [[[round(x, 3), round(y, 3)] for x, y in q] for q in paths],
                                   "nat_w": w, "nat_h": h})
            if p == "/api/settings":
                s = load_settings(); s.update(b); save_settings(s); return self._json(s)
            if p == "/api/import_xcs":
                return self._json(xcs_to_design(b))
            if p == "/api/gcode":
                text, job = job_text(b)
                return self._json({"gcode": text, "seconds": estimate_seconds(job),
                                   "raster_verified": gcode.RASTER_VERIFIED,
                                   "has_raster": any(e.is_raster for e in job.elements)})
            if p == "/api/send":
                text, job = job_text(b["design"])
                if any(e.is_raster for e in job.elements) and not gcode.RASTER_VERIFIED and not b.get("allow_unverified"):
                    return self._json({"error": "Fill/photo engraving isn't verified against Studio yet."}, 400)
                with MACHINE.lock:
                    d = MACHINE.need()
                    st = (d.status() or {}).get("curMode", {})
                    if st.get("mode") not in ("P_IDLE", "P_WORK_DONE", "P_SLEEP", None):
                        raise DeviceError(f"The laser is busy ({st.get('mode')}). Stop or finish the current job first.")
                    task, fmt = d.send_job(text)
                return self._json({"ok": True, "task": task, "format": fmt, "seconds": estimate_seconds(job)})
            if p in ("/api/pause", "/api/stop"):
                with MACHINE.lock:
                    return self._json(MACHINE.need().set_state(p.rsplit("/", 1)[1]) or {"ok": True})
            if p == "/api/camera":
                with MACHINE.lock:
                    jpg = MACHINE.need().camera_snapshot(str(b.get("stream", "0")))
                return self._json({"jpeg": base64.b64encode(jpg).decode()})
            if p == "/api/calib_file":
                with MACHINE.lock:
                    data = MACHINE.need().request("/v1/file/content", params={"type": b.get("name", "global_calib.json")}, timeout=20)
                return self._json({"data": data})
            if p == "/api/measure":
                return self._json(MACHINE.measure(float(b["x"]), float(b["y"])))
            self._send(404, b"not found", "text/plain")
        except Exception as e:
            self._error(e)

    def _error(self, e):
        if not isinstance(e, (DeviceError, design.DesignError, ValueError, KeyError, NotImplementedError)):
            traceback.print_exc()
        MACHINE.last_error = str(e)
        if MACHINE.dev and not MACHINE.dev._alive:
            MACHINE.disconnect()
        self._json({"error": str(e) or e.__class__.__name__}, 400)


def run(port: int = 8765, open_browser: bool = True) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"p2s-tools is running at {url}")
    print("Keep this window open while you use the app. Close it (or press Ctrl+C) to quit.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        MACHINE.disconnect()


if __name__ == "__main__":
    run()
