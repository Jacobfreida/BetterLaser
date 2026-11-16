"""Scanline helpers for fill engraving (vector shapes) and photo engraving (pixels)."""
from __future__ import annotations

import math

Point = tuple[float, float]


def fill_rows(paths: list[list[Point]], spacing: float, nonzero: bool = True) -> list[tuple[float, list[tuple[float, float]]]]:
    """Horizontal scanlines through closed polygons -> [(y, [(x0, x1), ...]), ...].

    Uses the nonzero winding rule (how fonts and most design apps fill shapes),
    so overlapping contours don't punch holes.
    """
    edges = []
    for p in paths:
        for (x0, y0), (x1, y1) in zip(p, p[1:] + p[:1]):
            if y0 != y1:
                edges.append((x0, y0, x1, y1))
    if not edges:
        return []
    ymin = min(min(e[1], e[3]) for e in edges)
    ymax = max(max(e[1], e[3]) for e in edges)
    rows = []
    # sample at pixel centres so the fill sits exactly inside the outline
    n = int(math.floor((ymax - ymin) / spacing))
    for i in range(n + 1):
        y = ymin + (i + 0.5) * spacing
        if y > ymax:
            break
        xs = []
        for x0, y0, x1, y1 in edges:
            if (y0 <= y < y1) or (y1 <= y < y0):
                x = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
                xs.append((x, 1 if y1 > y0 else -1))
        if not xs:
            continue
        xs.sort()
        segs, wind, start = [], 0, None
        for x, d in xs:
            prev = wind
            wind = wind + d if nonzero else (0 if wind else 1)
            inside_before = prev != 0
            inside_after = wind != 0
            if not inside_before and inside_after:
                start = x
            elif inside_before and not inside_after and start is not None:
                if x - start > 1e-6:
                    segs.append((start, x))
                start = None
        if segs:
            rows.append((y, segs))
    return rows


def pixel_runs(values: list[int], x0: float, px: float, power_of) -> list[tuple[float, float, int]]:
    """One row of pixels -> [(x_start, x_end, S), ...] merging equal neighbours, S>0 only."""
    runs, cur, start = [], None, 0
    for i, v in enumerate(values + [None]):
        s = power_of(v) if v is not None else None
        if s != cur:
            if cur:
                runs.append((x0 + start * px, x0 + i * px, cur))
            cur, start = s, i
    return runs
