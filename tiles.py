# SPDX-License-Identifier: GPL-3.0-or-later
"""
Google Photorealistic 3D Tiles (or any 3D Tiles 1.0 tileset) for a site:
which tiles to fetch, and where each one goes.

  Select   walk the tileset from root.json, keeping tiles whose bounding
           volume reaches the site cylinder, refining until a tile's
           geometric error is at or below the detail asked for.
  Place    a tile's glb is earth-centred: metres from the centre of the
           earth, which float32 - all Blender stores - holds only to half a
           metre. So the glb's root transforms are lifted out, composed in
           float64 with the tile transform and the site frame, and handed
           back as one small matrix. The glb is rewritten with identity
           roots so the importer never sees a big number.

The site frame is geo.TilesFrame, the same one Blosm uses for 3D Tiles, so
GIS data and Blosm imports line up with what lands here. No bpy here.
"""

import json
import math
import struct
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np

from . import geo

GOOGLE_ROOT = "https://tile.googleapis.com/v1/3dtiles/root.json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Mixtli Blender add-on (https://github.com/luigipacheco/mixtli)"

# glTF is +Y up; 3D Tiles content is +Z up. The same swap Blender's glTF
# importer applies, so after import Blender coordinates are tileset ones.
Y_UP_TO_Z_UP = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]],
                        dtype=np.float64)


# ---------------------------------------------------------------- http

def _open(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def child_url(base, uri):
    """Resolve `uri` against `base`, carrying the base's query parameters
    (Google's key and session) unless the child sets its own."""
    url = urllib.parse.urljoin(base, uri)
    if not url.startswith(("http:", "https:")):
        return url
    b = urllib.parse.urlsplit(base)
    u = urllib.parse.urlsplit(url)
    q = dict(urllib.parse.parse_qsl(b.query))
    q.update(urllib.parse.parse_qsl(u.query))
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))


def root_url(key):
    return GOOGLE_ROOT + "?" + urllib.parse.urlencode({"key": key})


def strip_secrets(url):
    """The URL without key / session, for reports and cache names."""
    u = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k not in ("key", "session")]
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))


# ---------------------------------------------------------------- location

def parse_latlon(text):
    """'19.4326, -99.1332' (as Google Maps copies it) -> (lat, lon) or None."""
    parts = [p for p in text.replace(";", ",").replace(" ", ",").split(",") if p]
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    return lat, lon


def geocode(text):
    """Place name -> (lat, lon, display name), via OpenStreetMap Nominatim."""
    url = NOMINATIM_URL + "?" + urllib.parse.urlencode({"q": text, "format": "json",
                                                          "limit": 1})
    hits = json.loads(_open(url, 30).decode("utf-8"))
    if not hits:
        return None
    h = hits[0]
    return float(h["lat"]), float(h["lon"]), h.get("display_name", text)


# ---------------------------------------------------------------- bounding volumes

def _matrix(m):
    """3D Tiles / glTF column-major 16 numbers -> row-major 4x4."""
    return np.asarray(m, dtype=np.float64).reshape(4, 4).T


class Site:
    """A vertical cylinder: everything within `radius` metres, horizontally,
    of the up-line through lat/lon, from below sea level to the highest peak."""
    LOW, HIGH = -500.0, 9000.0

    def __init__(self, lat, lon, radius):
        self.lat, self.lon, self.radius = float(lat), float(lon), float(radius)
        self.base = geo.ecef(self.lat, self.lon, 0.0)
        self.up = geo.ecef(self.lat, self.lon, 1.0) - self.base

    def probes(self, c):
        """Points on the up-line near the height of `c`, a radius apart."""
        h = float(np.clip((c - self.base) @ self.up, self.LOW, self.HIGH))
        hs = np.clip(h + self.radius * np.arange(-2, 3), self.LOW, self.HIGH)
        return self.base + hs[:, None] * self.up


def reaches(volume, transform, site):
    """Does a tile's bounding volume come within the site cylinder? Unknown
    volumes count as reaching, so nothing is silently dropped."""
    r = site.radius
    if "box" in volume:
        b = np.asarray(volume["box"], dtype=np.float64)
        c = transform[:3, :3] @ b[:3] + transform[:3, 3]
        axes = (transform[:3, :3] @ b[3:].reshape(3, 3).T).T
        for p in site.probes(c):
            d = p - c
            q = c.copy()
            for a in axes:
                ll = float(a @ a)
                if ll > 0:
                    q += a * np.clip((d @ a) / ll, -1.0, 1.0)
            if float(np.linalg.norm(p - q)) <= r:
                return True
        return False
    if "sphere" in volume:
        s = np.asarray(volume["sphere"], dtype=np.float64)
        c = transform[:3, :3] @ s[:3] + transform[:3, 3]
        scale = max(np.linalg.norm(transform[:3, :3], axis=0).max(), 1e-12)
        d = np.linalg.norm(site.probes(c) - c, axis=1).min()
        return float(d) <= s[3] * scale + r
    if "region" in volume:                  # radians, always geodetic
        w, s, e, n, lo, hi = volume["region"][:6]
        lat, lon = math.radians(site.lat), math.radians(site.lon)
        pad = r / geo.WGS84_A
        padlon = pad / max(math.cos(lat), 1e-6)
        return (s - pad <= lat <= n + pad) and (w - padlon <= lon <= e + padlon)
    return True


# ---------------------------------------------------------------- selection

class Selection:
    def __init__(self):
        self.tiles = []          # (glb url, 4x4 tile transform, geometric error)
        self.requests = 0
        self.skipped = 0         # content in formats other than glb


def select_tiles(root, site, max_error, max_tiles=600,
                 fetch=None, workers=8, progress=None):
    """Walk the tileset from `root` (a URL) and return a Selection of the
    glb tiles covering the Site at the asked-for detail.

    Refinement is REPLACE, as Google serves it: a tile is used when it is
    good enough or has nothing finer, otherwise its children are. External
    tilesets (content that is itself a .json) are fetched and walked as if
    they were inline - level by level, in parallel.
    """
    fetch = fetch or (lambda u: json.loads(_open(u).decode("utf-8")))
    sel = Selection()
    frontier = [(root, np.eye(4))]          # tileset json urls to expand
    with ThreadPoolExecutor(workers) as pool:
        while frontier:
            docs = list(pool.map(lambda f: fetch(f[0]), frontier))
            sel.requests += len(frontier)
            nxt = []
            for (url, parent_t), doc in zip(frontier, docs):
                stack = [(doc["root"], parent_t)]
                while stack:
                    tile, t = stack.pop()
                    if "transform" in tile:
                        t = t @ _matrix(tile["transform"])
                    if not reaches(tile.get("boundingVolume", {}), t, site):
                        continue
                    kids = tile.get("children") or []
                    err = float(tile.get("geometricError", 0.0))
                    content = tile.get("content") or {}
                    uri = content.get("uri") or content.get("url")
                    if uri and urllib.parse.urlsplit(uri).path.endswith(".json"):
                        nxt.append((child_url(url, uri), t))
                        continue
                    if kids and (err > max_error or not uri):
                        stack.extend((k, t) for k in kids)
                        continue
                    if uri:
                        path = urllib.parse.urlsplit(uri).path.lower()
                        if path.endswith((".glb", ".gltf")):
                            sel.tiles.append((child_url(url, uri), t, err))
                        else:
                            sel.skipped += 1
            if len(sel.tiles) > max_tiles:
                raise RuntimeError(
                    "%d+ tiles for this area at this detail (limit %d). Use a "
                    "smaller radius or a coarser detail" % (len(sel.tiles), max_tiles))
            frontier = nxt
            if progress:
                progress(sel)
    return sel


def download(urls, fetch=None, workers=8, progress=None):
    fetch = fetch or _open
    out = [None] * len(urls)
    with ThreadPoolExecutor(workers) as pool:
        for i, data in enumerate(pool.map(fetch, urls)):
            out[i] = data
            if progress:
                progress(i + 1, len(urls))
    return out


# ---------------------------------------------------------------- glb

def read_glb(data):
    """(json dict, binary chunk bytes or None)."""
    magic, version, length = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise ValueError("not a glb file")
    pos, js, binary = 12, None, None
    while pos < length:
        clen, ctype = struct.unpack_from("<II", data, pos)
        chunk = data[pos + 8:pos + 8 + clen]
        if ctype == 0x4E4F534A:
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == 0x004E4942:
            binary = bytes(chunk)
        pos += 8 + clen
    return js, binary


def write_glb(js, binary):
    j = json.dumps(js, separators=(",", ":")).encode("utf-8")
    j += b" " * (-len(j) % 4)
    parts = [struct.pack("<II", len(j), 0x4E4F534A), j]
    if binary is not None:
        b = binary + b"\0" * (-len(binary) % 4)
        parts += [struct.pack("<II", len(b), 0x004E4942), b]
    body = b"".join(parts)
    return struct.pack("<III", 0x46546C67, 2, 12 + len(body)) + body


def _quat(q):
    x, y, z, w = q
    return np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                     [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                     [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])


def node_matrix(node):
    if "matrix" in node:
        return _matrix(node["matrix"])
    m = np.eye(4)
    m[:3, :3] = _quat(node.get("rotation", (0, 0, 0, 1))) * \
        np.asarray(node.get("scale", (1, 1, 1)), dtype=np.float64)
    m[:3, 3] = node.get("translation", (0, 0, 0))
    return m


def lift_roots(data):
    """Take the root-node transforms out of a glb.

    Returns (new glb bytes, {root node index: 4x4 in glTF space}, copyright).
    CESIUM_RTC's centre, used by older tilesets, is folded into the roots.
    """
    js, binary = read_glb(data)
    scenes = js.get("scenes") or [{"nodes": list(range(len(js.get("nodes", []))))}]
    roots = scenes[js.get("scene", 0)].get("nodes", [])
    rtc = np.eye(4)
    c = (js.get("extensions") or {}).get("CESIUM_RTC", {}).get("center")
    if c:
        rtc[:3, 3] = c
        js["extensions"].pop("CESIUM_RTC")
        used = js.get("extensionsUsed") or []
        if "CESIUM_RTC" in used:
            used.remove("CESIUM_RTC")
    lifted = {}
    for i in roots:
        node = js["nodes"][i]
        lifted[i] = rtc @ node_matrix(node)
        for k in ("matrix", "translation", "rotation", "scale"):
            node.pop(k, None)
    copyright = (js.get("asset") or {}).get("copyright", "")
    return write_glb(js, binary), lifted, copyright


def placement(frame, tile_t, gltf_root):
    """Blender matrix_world for an imported root object.

    The importer converts the root's mesh to Z-up (Y) and, with the root's
    transform lifted out, leaves it at identity. Its true place in the site
    frame L is L . T . Y . R . Y^-1 - all float64, ending small.
    """
    local = np.eye(4)
    local[:3, :3] = frame.q
    local[:3, 3] = -frame.q @ frame.center
    return local @ tile_t @ Y_UP_TO_Z_UP @ gltf_root @ np.linalg.inv(Y_UP_TO_Z_UP)
