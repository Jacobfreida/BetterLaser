"""p2s-tools desktop app: a local web server + browser UI (no installs needed).

    python p2s.py gui            (or double-click p2s-app.bat on Windows)

Only listens on 127.0.0.1, so nothing else on the network can reach it.
"""
from __future__ import annotations

import base64
import json
import math
import logging
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import compat, bgc, design, fonts, gcode, xcs
from .device import P2S, DeviceError

log = logging.getLogger("p2s.app")
HERE = os.path.dirname(os.path.abspath(__file__))
UI_FILE = os.path.join(HERE, "ui", "index.html")
USB_HOST = "172.25.3.1" if sys.platform != "darwin" else "172.25.4.1"
IR_OFFSET_X_DEFAULT = 68.0      # plugin Wit[RULE_TWO]: IR sensor sits 68 mm right of the laser
HEAD_FEED = 9600                # plugin w7
# Moving the head by laserMeasureOffset alone left the laser (not the sensor) over the spot;
# on the user's P2S a further ~60 mm overshot a little, ~50 mm lands on target.
IR_EXTRA_SHIFT = 50.0
BED_MAX_X, BED_MAX_Y = 600.0, 305.0


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
            self.compat = compat.describe(self.info)
            return {**self.info, "compat": self.compat}

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
               "firmware": (self.info.get("firmware") or {}).get("package_version"),
               "compat": getattr(self, "compat", None)}
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

    def _sensor_head_x(self, x: float, y: float, shift):
        """Head X that puts the height sensor over bed point x, and the extra shift used."""
        off = self.ir_offset_x()
        extra = IR_EXTRA_SHIFT if shift is None else float(shift)
        hx = x - off - extra
        if hx < -off or hx > 600 or y < 0 or y > 308:
            raise DeviceError(f"Can't measure there: the height sensor sits to the right of the laser, "
                              f"so it can't reach the first {extra:.0f} mm of the bed. Pick a point at least "
                              f"{extra:.0f} mm from the left edge.")
        return hx, extra

    @staticmethod
    def _restore(d):
        """Measuring LED off, lights back on, head home (Studio's clean-up after measuring)."""
        for args in ((("/v1/peripheral/param", "PUT"), {"params": {"type": "ir_led"}, "data": {"action": "off", "index": 1}}),
                     (("/v1/peripheral/param", "PUT"), {"params": {"type": "fill_light"}, "data": {"action": "set_bri", "idx": 2, "value": 255}}),
                     (("/v1/peripheral/param", "PUT"), {"params": {"type": "fill_light"}, "data": {"action": "set_bri", "idx": 3, "value": 255}}),
                     (("/v1/laser-head/control", "POST"), {"data": {"action": "go_to", "waitTime": 0, "f": HEAD_FEED, "x": 0, "y": 0}})):
            try:
                d.request(*args[0], **args[1])
            except Exception:
                pass

    def measure_preview(self, x: float, y: float, shift: float | None = None) -> dict:
        """Move the sensor over (x, y), switch on the head's red measuring laser and photograph
        it with the head camera (stream "1", Studio's captureLocalImage), so the user can see
        exactly where the measurement will be taken. The head stays there with the laser on
        until measure() or measure_cancel()."""
        with self.lock:
            d = self.need()
            hx, extra = self._sensor_head_x(x, y, shift)
            try:
                d.request("/v1/laser-head/control", "POST", timeout=35,
                          data={"action": "go_to", "waitTime": 30000, "f": HEAD_FEED, "x": hx, "y": y})
                d.request("/v1/peripheral/param", "PUT", params={"type": "ir_led"}, data={"action": "on", "index": 1})
                time.sleep(0.4)
                jpg = d.camera_snapshot("1")
            except Exception:
                self._restore(d)
                raise
            return {"jpeg": base64.b64encode(jpg).decode(), "head_x": hx, "head_y": y, "shift": extra}

    # -- moving the head and the red pointer ------------------------------------
    # Studio: setLaserHead / getLaserPosition (/v1/laser-head/control go_to / get_coord) and
    # turnOnCoaxialIr / turnOffCoaxialIr (PUT /v1/peripheral/param type coaxial_Ir): a red
    # dot that shines down the beam path, so it marks exactly where the laser would fire.
    head_pos = (0.0, 0.0)

    def _need_idle(self, d):
        mode = ((d.status() or {}).get("curMode") or {}).get("mode")
        if mode not in (None, "P_IDLE", "P_SLEEP", "P_WORK_DONE"):
            raise DeviceError(f"The laser is busy ({mode}); the head can only be moved when it's idle.")

    @staticmethod
    def _pointer_fix():
        """Optional correction (settings pointer_dx / pointer_dy, mm) if the red dot lands away
        from where it was sent. Not needed if go_to coordinates match the bed (unconfirmed)."""
        st = load_settings()
        return float(st.get("pointer_dx") or 0), float(st.get("pointer_dy") or 0)

    def _go(self, d, x, y, wait=True):
        x = min(BED_MAX_X, max(0.0, float(x))); y = min(BED_MAX_Y, max(0.0, float(y)))
        fx, fy = self._pointer_fix()
        d.request("/v1/laser-head/control", "POST", timeout=35,
                  data={"action": "go_to", "waitTime": 30000 if wait else 0, "f": HEAD_FEED, "x": x - fx, "y": y - fy})
        self.head_pos = (x, y)
        return x, y

    def pointer(self, on: bool) -> dict:
        with self.lock:
            self.need().request("/v1/peripheral/param", "PUT", params={"type": "coaxial_Ir"},
                                data={"action": "on" if on else "off"})
        return {"ok": True, "pointer": on}

    def where(self) -> dict:
        with self.lock:
            d = self.need()
            try:
                r = d.request("/v1/laser-head/control", "POST", data={"action": "get_coord", "waitTime": 0}, timeout=10) or {}
                if r.get("x") is not None:
                    fx, fy = self._pointer_fix()
                    self.head_pos = (float(r["x"]) + fx, float(r["y"]) + fy)
            except Exception:
                pass
        return {"x": self.head_pos[0], "y": self.head_pos[1]}

    def move(self, x=None, y=None, dx=0.0, dy=0.0, pointer=None) -> dict:
        """Move the head to (x, y), or by (dx, dy) from where it is. Kept on the bed."""
        if x is None or y is None:
            cur = self.where()
            x, y = cur["x"] + float(dx), cur["y"] + float(dy)
        with self.lock:
            d = self.need()
            self._need_idle(d)
            if pointer is not None:
                d.request("/v1/peripheral/param", "PUT", params={"type": "coaxial_Ir"},
                          data={"action": "on" if pointer else "off"})
            x, y = self._go(d, x, y)
        return {"x": x, "y": y}

    def trace(self, points) -> dict:
        """Run the red pointer through a list of points (e.g. a job's outline), then stay at the last."""
        with self.lock:
            d = self.need()
            self._need_idle(d)
            d.request("/v1/peripheral/param", "PUT", params={"type": "coaxial_Ir"}, data={"action": "on"})
            for px, py in points:
                self._go(d, px, py)
        return {"x": self.head_pos[0], "y": self.head_pos[1]}

    def measure_cancel(self) -> dict:
        with self.lock:
            self._restore(self.need())
        return {"ok": True}

    def measure(self, x: float, y: float, shift: float | None = None) -> dict:
        """Measure the material height at bed point (x, y) with the head's IR sensor.

        The sensor sits right of the laser. Subtracting the machine's laserMeasureOffset
        once (as we read Studio's code) left the LASER over the clicked point on a real
        P2S, with the sensor about 60 mm to the right, so by default the head is moved a
        further IR_EXTRA_SHIFT mm. `shift` (settings: ir_shift_x) overrides that amount, in mm.
        """
        with self.lock:
            d = self.need()
            hx, extra = self._sensor_head_x(x, y, shift)
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
                self._restore(d)
            dist = r.get("distance")
            if dist is None:
                raise DeviceError(f"The machine didn't return a distance: {r}")
            dist = float(dist)
            focal = 25 - dist            # honeycomb mode: k7(...).value = 25 - z
            ok = -50 <= focal <= 25
            return {"distance": dist, "focal": round(focal, 2), "ok": ok, "head_x": hx, "head_y": y,
                    "shift": extra, "sensor": {"x": r.get("coord_x"), "y": r.get("coord_y")},
                    "message": "" if ok else "Nothing measured: is the material under the height sensor?"}


MACHINE = Machine()


def job_start_and_outline(design_json: dict):
    """Where the laser first fires in a job, and the corners of the area it covers (bed mm).

    The start is the first travel move of the G-code we would send. On the rotary the head
    stays on the rotary line (Y) and the object turns, so the outline is just the X span.
    """
    job = design.build_job(design_json)
    gcode.check_bounds(job)
    text = gcode.generate(job)
    rot = gcode.rotary_of(job)
    start = None
    for line in text.splitlines():
        m = re.match(r"G0X(-?[\d.]+)(?:Y(-?[\d.]+))?$", line)
        if m:
            start = (float(m[1]), float(m[2]) if m[2] is not None and not rot else (rot.axis_y if rot else 0.0))
            break
    pts = [p for e in job.elements for path in e.paths for p in path]
    pts += [(x, yy) for e in job.elements for yy, runs in e.rows for a, b, _ in runs for x in (a, b)]
    if not pts or start is None:
        raise ValueError("Nothing to frame")
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    if rot:
        y = rot.axis_y
        corners = [(min(xs), y), (max(xs), y), (min(xs), y)]
    else:
        corners = [(min(xs), min(ys)), (max(xs), min(ys)), (max(xs), max(ys)), (min(xs), max(ys)), (min(xs), min(ys))]
    return {"start": start, "outline": corners, "rotary": bool(rot)}


def job_text(design_json: dict) -> tuple[str, xcs.Job]:
    job = design.build_job(design_json)
    gcode.check_bounds(job)
    return gcode.generate(job), job


def raster_verified(job) -> bool:
    """Fill/photo moves are checked against Studio on the flat bed; on the rotary not yet."""
    if job.rotary:
        return gcode.RASTER_VERIFIED and gcode.ROTARY_RASTER_VERIFIED
    return gcode.RASTER_VERIFIED


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
        rotary = dev["mode"] == "LASER_CYLINDER"
        if rotary and (cfg.get("rotaryAttachmentModel") or "ra2") != "ra2":
            raise ValueError("Only RA2 rotary projects can be imported.")
        objs, skipped = [], []
        for d in canvas["displays"]:
            entry = dev_displays.get(d["id"]) or d
            t = {"RECT": "rect", "CIRCLE": "ellipse"}.get(d["type"])
            ptype = entry.get("processingType") or d.get("processingType")
            if not t:
                skipped.append(d["type"]); continue
            p = xcs._params(entry, ptype)
            mode = {"VECTOR_ENGRAVING": "score", "VECTOR_CUTTING": "cut", "FILL_VECTOR_ENGRAVING": "fill"}.get(ptype, "score")
            w, h, ang = abs(d["width"]), abs(d["height"]), d.get("angle", 0) or 0
            # Studio's x/y is the shape's own corner after rotating about it; find the centre
            a = math.radians(ang)
            cx = d["x"] + w / 2 * math.cos(a) - h / 2 * math.sin(a)
            cy = d["y"] + w / 2 * math.sin(a) + h / 2 * math.cos(a)
            if rotary:
                # Studio's rotary canvas is this app's bed view turned 90 degrees:
                # X = canvas y + 50, and y = 250 - canvas x (U is measured from the axis line)
                cx, cy, ang = cy + 50, 250 - cx, ang - 90
            objs.append({"id": d["id"], "type": t, "x": cx - w / 2, "y": cy - h / 2, "w": w,
                         "h": h, "rot": ang,
                         "op": {"mode": mode, "power": p.get("power", 10), "speed": p.get("speed", 100),
                                "passes": p.get("repeat", 1), "air": p.get("airPump", 25),
                                "density": p.get("density", 100)}})
        out = {"focal": cfg.get("focalLength"), "fan_gear": cfg.get("fanGear", 3),
               "purifier_gear": cfg.get("purifierGear", 4), "objects": objs, "skipped": skipped}
        if rotary:
            dia = cfg.get("diameter") or ((cfg.get("perimeter") or 0) / math.pi) or None
            out["rotary"] = {"type": "chuck" if cfg.get("rotaryAttachmentType") == "catch" else "roller",
                             "diameter": dia, "axis_y": 125}
        return out
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
            if p == "/api/text" and (b.get("wrap") or {}).get("target"):
                # text wrapped round a shape: bed-mm outlines, returned in a box of their own
                paths = design.wrap_text_paths(b, b["target_obj"])
                xs = [x for q in paths for x, _ in q] or [0]; ys = [y for q in paths for _, y in q] or [0]
                x0, y0 = min(xs), min(ys)
                return self._json({"paths": [[[round(x - x0, 3), round(y - y0, 3)] for x, y in q] for q in paths],
                                   "x": x0, "y": y0, "nat_w": max(max(xs) - x0, 1e-6), "nat_h": max(max(ys) - y0, 1e-6)})
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
                                   "raster_verified": raster_verified(job),
                                   "has_raster": any(e.is_raster for e in job.elements)})
            if p == "/api/send":
                text, job = job_text(b["design"])
                c = getattr(MACHINE, "compat", None)
                if c and not c["supported"]:
                    return self._json({"error": "Not sending: " + next((f["message"] for f in c["findings"]
                                                                        if f["level"] == "stop"), "this laser isn't supported")}, 400)
                if any(e.is_raster for e in job.elements) and not raster_verified(job) and not b.get("allow_unverified"):
                    return self._json({"error": "Fill/photo engraving on the rotary isn't verified against Studio yet."}, 400)
                try:
                    MACHINE.pointer(False)           # red alignment pointer off before the job
                except Exception:
                    pass
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
            if p == "/api/head":
                act = b.get("action")
                if act == "where":
                    return self._json(MACHINE.where())
                if act == "pointer":
                    return self._json(MACHINE.pointer(bool(b.get("on"))))
                if act == "home":
                    MACHINE.pointer(False)
                    return self._json(MACHINE.move(0, 0))
                if act == "move":
                    return self._json(MACHINE.move(b.get("x"), b.get("y"), b.get("dx", 0), b.get("dy", 0), b.get("pointer")))
                if act in ("start", "frame"):
                    info = job_start_and_outline(b["design"])
                    if act == "start":
                        return self._json({**MACHINE.move(*info["start"], pointer=True), **info})
                    pos = MACHINE.trace(info["outline"] + [info["start"]])
                    return self._json({**pos, **info})
                return self._json({"error": f"unknown head action {act}"}, 400)
            if p in ("/api/measure_preview", "/api/measure_cancel"):
                if p.endswith("cancel"):
                    return self._json(MACHINE.measure_cancel())
                shift = b.get("shift", load_settings().get("ir_shift_x"))
                return self._json(MACHINE.measure_preview(float(b["x"]), float(b["y"]),
                                                          None if shift in (None, "") else float(shift)))
            if p == "/api/measure":
                shift = b.get("shift", load_settings().get("ir_shift_x"))
                return self._json(MACHINE.measure(float(b["x"]), float(b["y"]),
                                                  None if shift in (None, "") else float(shift)))
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
