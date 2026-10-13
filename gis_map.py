# SPDX-License-Identifier: GPL-3.0-or-later
"""
Putting GIS features onto points: which polygon is each point in, and how
far is it from the nearest line.

Polygons are applied largest first, so where they nest - a building on a
residential landuse - each key takes its value from the smallest polygon
that carries it. The building gets building=yes AND landuse=residential.

Text values become integer codes with a legend; numbers stay numbers. Both
are ordinary point attributes, so MX 21 (class ID) and MX 27 (scalar range)
read them like any other. No bpy here.
"""

import numpy as np

from . import geo
from .gis_sources import as_number, attr_name, classify

MAX_CATEGORIES = 1024
MAX_FIELDS = 32


def project_features(features, frame, height=0.0, offset=(0.0, 0.0)):
    """[(kind, parts in lon/lat, props)] -> same with parts as (M, 2) arrays
    of Blender XY metres. One vectorised projection for every vertex."""
    flat = [np.asarray(p, dtype=np.float64) for _, parts, _ in features for p in parts]
    if not flat:
        return []
    ll = np.concatenate(flat)
    xy = frame.to_local(ll[:, 1], ll[:, 0], height)[:, :2] + np.asarray(offset, np.float64)
    out, at, k = [], 0, 0
    for kind, parts, props in features:
        arrs = []
        for _ in parts:
            n = len(flat[k])
            arrs.append(xy[at:at + n])
            at += n
            k += 1
        out.append((kind, arrs, props))
    return out


def pick_fields(features, keys=None):
    """{key: 'number' | 'category'} for the keys worth an attribute."""
    if keys:
        wanted = list(dict.fromkeys(keys))
    else:
        seen = {}
        for _, _, props in features:
            for k in props:
                if not k.startswith("@"):
                    seen[k] = seen.get(k, 0) + 1
        wanted = sorted(seen, key=lambda k: -seen[k])[:MAX_FIELDS]
    fields = {}
    for k in wanted:
        kind = classify(props.get(k) for _, _, props in features)
        if kind:
            fields[k] = kind
    return fields


def build_legend(features, key):
    """Code 1 for the commonest value, 0 means 'no value'."""
    counts = {}
    for _, _, props in features:
        v = props.get(key)
        if v not in (None, ""):
            v = str(v)
            counts[v] = counts.get(v, 0) + 1
    order = sorted(counts, key=lambda v: (-counts[v], v))
    if len(order) > MAX_CATEGORIES:
        return None
    return {v: i + 1 for i, v in enumerate(order)}


def map_features(xyz, features, fields, line_max=25.0, report=None):
    """xyz: (N, 3) world positions. features: projected, from
    project_features. Returns (attributes, legends) where attributes maps a
    name to (blender type, array) and legends maps a name to its metadata."""
    report = report if report is not None else []
    n = len(xyz)
    px, py = xyz[:, 0].astype(np.float64), xyz[:, 1].astype(np.float64)
    polys = [f for f in features if f[0] == 'polygon']
    lines = [f for f in features if f[0] == 'line']

    attrs, legends = {}, {}
    if n == 0:
        return attrs, legends
    span = np.maximum(np.ptp(np.stack([px, py], 1), 0), 1e-6)
    cell = max(float(np.sqrt(span[0] * span[1] / max(n, 1)) * 16.0), 0.5)
    grid = geo.PointGrid(np.stack([px, py], 1), cell)

    # ------------------------------------------------ polygons
    poly_fields, cats = {}, {}
    for k, kind in fields.items():
        if not any(k in p for _, _, p in polys):
            continue
        if kind == 'category':
            leg = build_legend(polys, k)
            if leg is None:
                report.append("  skipped '%s': more than %d distinct values"
                              % (k, MAX_CATEGORIES))
                continue
            cats[k] = leg
        poly_fields[k] = kind

    inside = np.zeros(n, dtype=bool)
    feature = np.full(n, -1, dtype=np.int32)
    values = {k: (np.zeros(n, np.int32) if kind == 'category' else np.zeros(n, np.float32))
              for k, kind in poly_fields.items()}
    order = sorted(range(len(polys)), key=lambda i: -geo.polygon_area(polys[i][1]))
    hits = 0
    for i in order:
        _, rings, props = polys[i]
        b = np.concatenate(rings)
        cand = grid.query(*b.min(0), *b.max(0))
        if len(cand) == 0:
            continue
        idx = cand[geo.points_in_rings(px[cand], py[cand], rings)]
        if len(idx) == 0:
            continue
        hits += 1
        inside[idx] = True
        feature[idx] = i
        for k, kind in poly_fields.items():
            v = props.get(k)
            if v in (None, ""):
                continue
            if kind == 'category':
                values[k][idx] = cats[k][str(v)]
            else:
                num = as_number(v)
                if num is not None:
                    values[k][idx] = num
    report.append("  %d of %d polygons touch the cloud; %.1f%% of points inside one"
                  % (hits, len(polys), 100.0 * inside.mean()))

    if polys:
        attrs["gis_inside"] = ('BOOLEAN', inside)
        attrs["gis_feature"] = ('INT', feature)
        legends["gis_feature"] = {"kind": "id", "ids": [p.get("@id", "") for _, _, p in polys]}
    for k, kind in poly_fields.items():
        name = attr_name("gis_", k)
        if kind == 'category':
            attrs[name] = ('INT', values[k])
            legends[name] = {"kind": "category", "key": k,
                             "labels": {str(c): v for v, c in cats[k].items()}}
        else:
            attrs[name] = ('FLOAT', values[k])
            legends[name] = {"kind": "number", "key": k}

    # ------------------------------------------------ lines
    if lines:
        best = np.full(n, np.inf)
        nearest = np.full(n, -1, dtype=np.int32)
        for i, (_, parts, _) in enumerate(lines):
            segs = geo.line_segments(parts)
            if len(segs) == 0:
                continue
            lo = segs.reshape(-1, 2).min(0) - line_max
            hi = segs.reshape(-1, 2).max(0) + line_max
            cand = grid.query(lo[0], lo[1], hi[0], hi[1])
            if len(cand) == 0:
                continue
            d = geo.segment_distance(px[cand], py[cand], segs)
            closer = d < best[cand]
            best[cand[closer]] = d[closer]
            nearest[cand[closer]] = i
        near = best <= line_max
        nearest[~near] = -1
        attrs["gis_line_dist"] = ('FLOAT', np.where(near, best, line_max).astype(np.float32))
        legends["gis_line_dist"] = {"kind": "number", "key": "distance to nearest line (m)",
                                    "max": float(line_max)}
        report.append("  %d lines; %.1f%% of points within %.4g m of one"
                      % (len(lines), 100.0 * near.mean(), line_max))
        for k, kind in fields.items():
            if kind != 'category' or not any(k in p for _, _, p in lines):
                continue
            leg = build_legend(lines, k)
            if leg is None:
                continue
            code = np.zeros(len(lines) + 1, dtype=np.int32)    # last slot: none
            for i, (_, _, p) in enumerate(lines):
                v = p.get(k)
                if v not in (None, ""):
                    code[i] = leg[str(v)]
            name = attr_name("gis_line_", k)
            attrs[name] = ('INT', code[nearest])                 # -1 -> last slot
            legends[name] = {"kind": "category", "key": k,
                             "labels": {str(c): v for v, c in leg.items()}}
    return attrs, legends
