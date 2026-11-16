"""Which laser are we talking to, and is it one this software is built for?

xTool Studio 1.7.30 treats every P2S as one device: deviceCode "ZY013", driven by the
same plugin (xcs-ext-p2s). Inside that it knows two hardware sub-types, read from the
serial number (machineInfo.machineSubType): "001" and "004"; "004" additionally offers
the conveyor-feeder mode. The job format, camera, measuring and rotary code are shared.
Other xTool lasers use other codes (e.g. ZY015 = P3, MP2 = P2) and different plugins.

Reference machine everything here was verified on: P2S, ZY013, sub-type 004, 55 W CO2,
firmware 40.22.011.
"""
from __future__ import annotations

SUPPORTED_CODE = "ZY013"
REFERENCE = {"deviceCode": "ZY013", "machineType": "P2S", "machineSubType": "004",
             "laserPower": 55, "firmware": "40.22.011"}
KNOWN_CODES = {"ZY013": "P2S", "ZY015": "P3", "MP2": "P2", "MLR": "P1", "MF1": "F1",
               "GS002": "F1 Ultra", "MD2": "S1", "MLM": "M1", "ZH009": "M1 Ultra",
               "MD1": "D1", "MDP": "D1 Pro", "MDP2": "D1 Pro 2.0"}
SUB_TYPES = {"001": "P2S (sub-type 001)", "004": "P2S (sub-type 004, conveyor-feeder capable)"}


def firmware_of(info: dict) -> str:
    return ((info.get("firmware") or {}).get("package_version") or "")


def describe(info: dict) -> dict:
    """Summary plus a list of (level, message) findings for a machineInfo reply.

    level: "ok", "warn" (works, but differs from the machine it was verified on),
    "stop" (don't send jobs: not a P2S / no binary job support).
    """
    code = info.get("deviceCode") or ""
    sub = str(info.get("machineSubType") or "")
    power = info.get("laserPower")
    fw = firmware_of(info)
    name = KNOWN_CODES.get(code, code or "unknown")
    out = []
    if code == SUPPORTED_CODE:
        out.append(("ok", f"xTool {info.get('machineType') or 'P2S'} (device code {code}), same device type as the verified machine"))
    else:
        out.append(("stop", f"This is a {name} (device code {code or '?'}), not a P2S. Its jobs use a different "
                            "xTool plugin, so this software must not send jobs to it yet."))
    if sub and code == SUPPORTED_CODE:
        lvl = "ok" if sub in SUB_TYPES else "warn"
        out.append((lvl, f"Hardware sub-type {sub}" + (f": {SUB_TYPES[sub]}" if sub in SUB_TYPES else
                                                       " (not one Studio 1.7.30 knows for the P2S)")))
    fmts = info.get("gcodeFormat") or []
    if "bin" in fmts:
        out.append(("ok", "Takes binary jobs (BGC), the format verified to fire the laser"))
    else:
        out.append(("stop" if code == SUPPORTED_CODE else "warn",
                    f"Job formats {fmts or '?'}: no binary format; text jobs ran without firing on the P2S"))
    if power is not None:
        if power == REFERENCE["laserPower"]:
            out.append(("ok", f"{power} W laser, same as the verified machine"))
        else:
            out.append(("warn", f"{power} W laser (verified machine: {REFERENCE['laserPower']} W). Jobs work the "
                                "same way, but power % gives a different result: test settings on scrap first"))
    if fw:
        same = fw.startswith(REFERENCE["firmware"])
        out.append(("ok" if same else "warn", f"Firmware {fw}" + ("" if same else
                    f" (verified on {REFERENCE['firmware']}; newer firmware has kept the same protocol so far)")))
    label = f"{name} {power} W" if power else name
    return {"model": name, "label": label, "code": code, "subType": sub, "laserPower": power,
            "firmware": fw, "supported": code == SUPPORTED_CODE and "bin" in fmts,
            "findings": [{"level": l, "message": m} for l, m in out]}
