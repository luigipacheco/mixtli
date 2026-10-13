# SPDX-License-Identifier: GPL-3.0-or-later
"""
Where GIS features come from, and the one shape they all end up in.

  OpenStreetMap      the Overpass API, for any bounding box on earth
  GeoJSON file       QGIS exports, city open-data portals, anything
  OGC API Features   a standards-based server; it already speaks GeoJSON

Every source is turned into GeoJSON (RFC 7946: WGS84, [lon, lat]) and kept
as text, so a download is made once, can be saved next to the .blend, and
re-mapped as often as needed. No bpy here.
"""

import json
import re
import urllib.parse
import urllib.request

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "Mixtli Blender add-on (https://github.com/luigipacheco/mixtli)"

# Closed ways under these keys are lines, not areas, unless area=yes.
LINEAR_KEYS = {"highway", "barrier", "waterway", "railway", "power", "route"}

DEFAULT_OSM_KEYS = ("building,building:levels,height,landuse,natural,leisure,"
                    "amenity,highway,surface,waterway")


def split_keys(text):
    return [k.strip() for k in (text or "").split(",") if k.strip()]


# ---------------------------------------------------------------- http

def _get(url, data=None, accept="application/json", timeout=120):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": USER_AGENT, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


# ---------------------------------------------------------------- OpenStreetMap

def overpass_query(bbox, keys, timeout=90):
    """bbox is (south, west, north, east). Ways and multipolygon relations
    carrying any of `keys`, with their geometry inline."""
    s, w, n, e = bbox
    box = "(%.7f,%.7f,%.7f,%.7f)" % (s, w, n, e)
    parts = []
    for k in keys:
        k = k.replace('"', '')
        parts.append('way["%s"]%s;' % (k, box))
        parts.append('relation["%s"]["type"="multipolygon"]%s;' % (k, box))
    return "[out:json][timeout:%d];(%s);out geom;" % (timeout, "".join(parts))


def fetch_osm(bbox, keys, url=OVERPASS_URL):
    q = overpass_query(bbox, keys)
    data = urllib.parse.urlencode({"data": q}).encode("utf-8")
    return osm_to_geojson(_get(url, data=data))


def _coords(geometry):
    return [(g["lon"], g["lat"]) for g in geometry or () if g]


def _is_area(tags, coords):
    if len(coords) < 4 or coords[0] != coords[-1]:
        return False
    if tags.get("area") == "no":
        return False
    if tags.get("area") == "yes":
        return True
    return not any(k in tags for k in LINEAR_KEYS)


def join_rings(pieces):
    """Chain way pieces end to end into closed rings. Open leftovers - the
    relation crosses the edge of the download - are dropped."""
    pieces = [list(p) for p in pieces if len(p) >= 2]
    rings = []
    while pieces:
        ring = pieces.pop()
        grown = True
        while ring[0] != ring[-1] and grown:
            grown = False
            for i, p in enumerate(pieces):
                if p[0] == ring[-1]:
                    ring += p[1:]
                elif p[-1] == ring[-1]:
                    ring += p[-2::-1]
                elif p[-1] == ring[0]:
                    ring = p[:-1] + ring
                elif p[0] == ring[0]:
                    ring = p[:0:-1] + ring
                else:
                    continue
                pieces.pop(i)
                grown = True
                break
        if ring[0] == ring[-1] and len(ring) >= 4:
            rings.append(ring)
    return rings


def _point_in_ring(pt, ring):
    x, y = pt
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def _multipolygon(members):
    outers = join_rings(_coords(m.get("geometry")) for m in members
                        if m.get("type") == "way" and m.get("role") != "inner")
    inners = join_rings(_coords(m.get("geometry")) for m in members
                        if m.get("type") == "way" and m.get("role") == "inner")
    polys = [[o] for o in outers]
    for ring in inners:
        for poly in polys:
            if _point_in_ring(ring[0], poly[0]):
                poly.append(ring)
                break
    return polys


def osm_to_geojson(osm):
    """Overpass `out geom` JSON to a GeoJSON FeatureCollection. Tags become
    properties, plus '@id' (way/123) so a feature can be traced back."""
    feats = []
    for el in osm.get("elements", ()):
        tags = dict(el.get("tags") or {})
        if not tags:
            continue
        props = dict(tags)
        props["@id"] = "%s/%s" % (el.get("type"), el.get("id"))
        if el.get("type") == "way":
            c = _coords(el.get("geometry"))
            if len(c) < 2:
                continue
            if _is_area(tags, c):
                geom = {"type": "Polygon", "coordinates": [c]}
            else:
                geom = {"type": "LineString", "coordinates": c}
        elif el.get("type") == "relation":
            polys = _multipolygon(el.get("members") or ())
            if not polys:
                continue
            geom = {"type": "MultiPolygon", "coordinates": polys}
        else:
            continue
        feats.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": feats,
            "mixtli_source": "OpenStreetMap contributors (ODbL), via Overpass"}


# ---------------------------------------------------------------- OGC API Features

def fetch_ogc(items_url, bbox, max_pages=20, limit=1000):
    """GET {collection}/items?bbox=... and follow rel=next links."""
    s, w, n, e = bbox
    sep = "&" if "?" in items_url else "?"
    url = items_url + sep + urllib.parse.urlencode({
        "bbox": "%.7f,%.7f,%.7f,%.7f" % (w, s, e, n), "limit": limit, "f": "json"})
    feats = []
    for _ in range(max_pages):
        page = _get(url, accept="application/geo+json, application/json")
        feats.extend(page.get("features") or ())
        url = next((l.get("href") for l in page.get("links") or ()
                    if l.get("rel") == "next" and l.get("href")), None)
        if not url:
            break
    return {"type": "FeatureCollection", "features": feats,
            "mixtli_source": items_url}


# ---------------------------------------------------------------- GeoJSON

_CRS84 = re.compile(r"(CRS84|EPSG:+4326|EPSG/0/4326)$", re.I)


def parse_geojson(gj):
    """GeoJSON to ([(kind, parts, props)], skipped). kind is 'polygon' with
    parts a list of rings, or 'line' with parts a list of paths. Coordinates
    stay [lon, lat]. Points are skipped - they have no extent to fall in."""
    crs = (((gj.get("crs") or {}).get("properties") or {}).get("name") or "")
    if crs and not _CRS84.search(crs):
        raise ValueError("GeoJSON is in %s; reproject it to WGS84 (EPSG:4326), "
                         "e.g. Export in QGIS with that CRS" % crs)
    if gj.get("type") == "FeatureCollection":
        feats = gj.get("features") or ()
    elif gj.get("type") == "Feature":
        feats = [gj]
    else:
        feats = [{"type": "Feature", "geometry": gj, "properties": {}}]

    out, skipped = [], 0

    def add(geom, props):
        nonlocal skipped
        t = (geom or {}).get("type")
        c = (geom or {}).get("coordinates")
        if t == "Polygon":
            out.append(('polygon', [[p[:2] for p in r] for r in c if len(r) >= 3], props))
        elif t == "MultiPolygon":
            out.append(('polygon', [[p[:2] for p in r] for poly in c for r in poly
                                    if len(r) >= 3], props))
        elif t == "LineString":
            out.append(('line', [[p[:2] for p in c]], props))
        elif t == "MultiLineString":
            out.append(('line', [[p[:2] for p in l] for l in c], props))
        elif t == "GeometryCollection":
            for g in geom.get("geometries") or ():
                add(g, props)
        else:
            skipped += 1

    for f in feats:
        add(f.get("geometry"), dict(f.get("properties") or {}))
    out = [f for f in out if f[1]]

    for _, parts, _ in out[:50]:
        for lon, lat in parts[0][:3]:
            if abs(lon) > 180 or abs(lat) > 90:
                raise ValueError("coordinates look projected, not longitude/latitude; "
                                 "reproject the file to WGS84 (EPSG:4326)")
    return out, skipped


# ---------------------------------------------------------------- property values

_NUMBER = re.compile(r"^\s*(-?\d+(?:\.\d+)?)")


def as_number(v):
    """OSM numbers are text: '12', '12 m', '3;4'. Take the leading number."""
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = _NUMBER.match(v)
        if m:
            return float(m.group(1))
    return None


def classify(values, numeric_share=0.9):
    """'number' when nearly every value reads as one, else 'category'."""
    vals = [v for v in values if v not in (None, "")]
    if not vals:
        return None
    nums = sum(1 for v in vals if as_number(v) is not None)
    return 'number' if nums >= numeric_share * len(vals) else 'category'


def attr_name(prefix, key):
    s = re.sub(r"[^0-9A-Za-z_]+", "_", key).strip("_").lower() or "field"
    return (prefix + s)[:60]
