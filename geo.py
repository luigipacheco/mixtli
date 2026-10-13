# SPDX-License-Identifier: GPL-3.0-or-later
"""
Geographic maths: from latitude/longitude to Blender metres, and the spatial
queries that put GIS features onto points.

No bpy in here, so all of it can be tested outside Blender.

Two frames, because Blosm uses two:

  TransverseMercator  OSM buildings, roads and terrain. A spherical TM
                      centred on scene["lat"], scene["lon"]; a copy of
                      blosm/util/transverse_mercator.py, vectorised.
  TilesFrame          Google (and other) 3D Tiles. WGS84 ECEF, shifted by
                      the scene centre and rotated so +Z points up and +Y
                      north; blosm/threed_tiles/blender.py, finalize().

They agree at the centre and drift apart with distance - the sphere is not
the ellipsoid - by roughly 0.2% at 45 degrees latitude and 0.7% at the
equator, so 2-7 m per kilometre. A cloud must be mapped with the frame its
source was imported in, or features slide off it towards the edges.
"""

import math

import numpy as np

WGS84_A = 6378137.0
WGS84_B = 6356752.3142451793
WGS84_E2 = 1.0 - (WGS84_B / WGS84_A) ** 2


# ---------------------------------------------------------------- frames

class TransverseMercator:
    """Blosm's projection for OSM and terrain imports. z is height as given."""
    name = 'TM'
    radius = 6378137.0

    def __init__(self, lat, lon, k=1.0):
        self.lat, self.lon, self.k = float(lat), float(lon), float(k)
        self.lat_rad = math.radians(self.lat)

    def to_local(self, lat, lon, h=0.0):
        lat = np.radians(np.asarray(lat, dtype=np.float64))
        dlon = np.radians(np.asarray(lon, dtype=np.float64) - self.lon)
        b = np.sin(dlon) * np.cos(lat)
        kr = self.k * self.radius
        x = 0.5 * kr * np.log((1.0 + b) / (1.0 - b))
        y = kr * (np.arctan(np.tan(lat) / np.cos(dlon)) - self.lat_rad)
        z = np.broadcast_to(np.asarray(h, dtype=np.float64), x.shape)
        return np.stack([x, y, z], -1)

    def to_geo(self, xyz):
        xyz = np.asarray(xyz, dtype=np.float64)
        kr = self.k * self.radius
        x, y = xyz[..., 0] / kr, xyz[..., 1] / kr
        d = y + self.lat_rad
        lon = self.lon + np.degrees(np.arctan(np.sinh(x) / np.cos(d)))
        lat = np.degrees(np.arcsin(np.sin(d) / np.cosh(x)))
        return lat, lon, xyz[..., 2].copy()


def ecef(lat, lon, h=0.0):
    """Geodetic WGS84 to earth-centred XYZ, the way Blosm computes it."""
    lat = np.radians(np.asarray(lat, dtype=np.float64))
    lon = np.radians(np.asarray(lon, dtype=np.float64))
    h = np.asarray(h, dtype=np.float64)
    cl = np.cos(lat)
    n = np.stack(np.broadcast_arrays(cl * np.cos(lon), cl * np.sin(lon), np.sin(lat)), -1)
    r2 = np.array([WGS84_A ** 2, WGS84_A ** 2, WGS84_B ** 2])
    k = n * r2
    gamma = np.sqrt((n * k).sum(-1, keepdims=True))
    return k / gamma + n * h[..., None]


def ecef_to_geodetic(p):
    """Earth-centred XYZ back to (lat, lon, h). Iterated to sub-millimetre."""
    p = np.asarray(p, dtype=np.float64)
    x, y, z = p[..., 0], p[..., 1], p[..., 2]
    lon = np.arctan2(y, x)
    r = np.hypot(x, y)
    lat = np.arctan2(z, r * (1.0 - WGS84_E2))
    for _ in range(6):
        s = np.sin(lat)
        n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * s * s)
        h = r / np.cos(lat) - n
        lat = np.arctan2(z, r * (1.0 - WGS84_E2 * n / (n + h)))
    s = np.sin(lat)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * s * s)
    h = r / np.cos(lat) - n
    return np.degrees(lat), np.degrees(lon), h


def _rot_x(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)


def _rot_z(a):
    c, s = math.cos(a), math.sin(a)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


class TilesFrame:
    """Blosm's frame for 3D Tiles: local = Q @ (ECEF - C).

    Blosm builds Q from the *geocentric* latitude of C, not the geodetic one,
    so local +Z is tilted from the true vertical by up to 0.19 degrees. That
    is copied here on purpose - matching Blosm matters more than matching the
    textbook. Blosm also computes a height offset but never applies it, so z
    stays ellipsoidal height relative to the centre.
    """
    name = 'TILES'

    def __init__(self, lat, lon):
        self.lat, self.lon = float(lat), float(lon)
        self.center = ecef(self.lat, self.lon, 0.0)
        c = self.center
        lat_gc = math.atan(c[2] / math.sqrt(c[0] * c[0] + c[1] * c[1]))
        self.q = _rot_x(lat_gc - math.pi / 2.0) @ _rot_z(math.radians(-90.0 - self.lon))

    def to_local(self, lat, lon, h=0.0):
        return (ecef(lat, lon, h) - self.center) @ self.q.T

    def to_geo(self, xyz):
        p = np.asarray(xyz, dtype=np.float64) @ self.q + self.center
        return ecef_to_geodetic(p)


def make_frame(kind, lat, lon):
    return TilesFrame(lat, lon) if kind == 'TILES' else TransverseMercator(lat, lon)


# ---------------------------------------------------------------- spatial index

class PointGrid:
    """Bucket 2D points into square cells so a bounding box can be answered
    without scanning every point. Cells are stored row by row, so one column
    of cells inside a box is one contiguous slice."""

    def __init__(self, xy, cell):
        xy = np.asarray(xy, dtype=np.float64)
        self.n = len(xy)
        self.cell = float(cell)
        if self.n == 0:
            self.origin = np.zeros(2)
            self.nx = self.ny = 1
            self.order = np.zeros(0, np.int64)
            self.keys = np.zeros(0, np.int64)
            return
        self.origin = xy.min(0)
        ij = np.floor((xy - self.origin) / self.cell).astype(np.int64)
        self.nx, self.ny = (ij.max(0) + 1).tolist()
        key = ij[:, 0] * self.ny + ij[:, 1]
        self.order = np.argsort(key, kind='stable')
        self.keys = key[self.order]

    def query(self, xmin, ymin, xmax, ymax):
        if self.n == 0:
            return self.order
        ox, oy = self.origin
        i0 = max(int(math.floor((xmin - ox) / self.cell)), 0)
        i1 = min(int(math.floor((xmax - ox) / self.cell)), self.nx - 1)
        j0 = max(int(math.floor((ymin - oy) / self.cell)), 0)
        j1 = min(int(math.floor((ymax - oy) / self.cell)), self.ny - 1)
        if i0 > i1 or j0 > j1:
            return self.order[:0]
        lo = np.searchsorted(self.keys, np.arange(i0, i1 + 1) * self.ny + j0, 'left')
        hi = np.searchsorted(self.keys, np.arange(i0, i1 + 1) * self.ny + j1, 'right')
        parts = [self.order[a:b] for a, b in zip(lo, hi) if b > a]
        return np.concatenate(parts) if parts else self.order[:0]


# ---------------------------------------------------------------- polygons

def ring_area(ring):
    x, y = ring[:, 0], ring[:, 1]
    return 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))


def polygon_area(rings):
    """Rings of one or more polygons, holes included: outer minus holes."""
    if not rings:
        return 0.0
    a = [abs(ring_area(r)) for r in rings]
    return max(a[0] - sum(a[1:]), 0.0) if len(rings) > 1 else a[0]


def edges_of(rings):
    """(x1, y1, x2, y2) for every edge of every ring, closing each ring."""
    segs = []
    for r in rings:
        if len(r) < 3:
            continue
        if np.allclose(r[0], r[-1]):
            r = r[:-1]
        segs.append(np.concatenate([r, np.roll(r, -1, 0)], 1))
    if not segs:
        return np.zeros((0, 4))
    return np.concatenate(segs)


def points_in_rings(px, py, rings, chunk=2_000_000):
    """Even-odd point-in-polygon. Holes and multi-polygons work for free:
    every ring flips the parity, so pass all rings of the feature together."""
    e = edges_of(rings)
    inside = np.zeros(len(px), dtype=bool)
    if len(e) == 0 or len(px) == 0:
        return inside
    x1, y1, x2, y2 = e.T
    step = max(chunk // len(e), 1)
    for s in range(0, len(px), step):
        X = px[s:s + step, None]
        Y = py[s:s + step, None]
        crosses = (y1 > Y) != (y2 > Y)
        with np.errstate(divide='ignore', invalid='ignore'):
            xi = x1 + (Y - y1) * (x2 - x1) / (y2 - y1)
        inside[s:s + step] = (np.count_nonzero(crosses & (X < xi), 1) & 1).astype(bool)
    return inside


def segment_distance(px, py, segs, chunk=2_000_000):
    """Distance from each point to the nearest of `segs` (K, 4)."""
    out = np.full(len(px), np.inf)
    if len(segs) == 0 or len(px) == 0:
        return out
    x1, y1, x2, y2 = segs.T
    dx, dy = x2 - x1, y2 - y1
    ll = dx * dx + dy * dy
    ll = np.where(ll > 0, ll, 1.0)
    step = max(chunk // len(segs), 1)
    for s in range(0, len(px), step):
        X = px[s:s + step, None]
        Y = py[s:s + step, None]
        t = np.clip(((X - x1) * dx + (Y - y1) * dy) / ll, 0.0, 1.0)
        qx, qy = x1 + t * dx - X, y1 + t * dy - Y
        out[s:s + step] = np.sqrt((qx * qx + qy * qy).min(1))
    return out


def line_segments(parts):
    segs = [np.concatenate([p[:-1], p[1:]], 1) for p in parts if len(p) >= 2]
    return np.concatenate(segs) if segs else np.zeros((0, 4))


# ---------------------------------------------------------------- alignment

def _block_min(a, valid, block):
    """Lowest valid value in each block x block tile, broadcast back out."""
    h, w = a.shape
    H, W = -(-h // block) * block, -(-w // block) * block
    big = np.full((H, W), np.inf)
    big[:h, :w] = np.where(valid, a, np.inf)
    m = big.reshape(H // block, block, W // block, block).min((1, 3))
    m = np.repeat(np.repeat(m, block, 0), block, 1)[:h, :w]
    return np.where(np.isfinite(m), m, 0.0)


def estimate_offset(xy, z, footprints, cell=1.0, search=10.0, max_cells=16_000_000):
    """How far do building footprints have to move to sit on the roofs?

    Rasterise the cloud to its highest point per cell, take out the terrain
    with a coarse block minimum, then slide the footprint mask over it and
    score each shift by the mean normalised height under the mask. Both the
    score's numerator and its denominator are cross-correlations, so FFTs
    try every shift in one go.

    Returns a dict: best shift (dx, dy) in metres - move the features by it
    to match the cloud - with the score there and at zero, or None when
    there is nothing to measure.
    """
    xy = np.asarray(xy, dtype=np.float64)
    z = np.asarray(z, dtype=np.float64)
    if len(xy) == 0 or not footprints:
        return None
    lo = xy.min(0) - search
    hi = xy.max(0) + search
    span = hi - lo
    cell = max(cell, math.sqrt(span[0] * span[1] / max_cells))
    w = int(span[0] // cell) + 1
    h = int(span[1] // cell) + 1
    ij = np.floor((xy - lo) / cell).astype(np.int64)
    flat = ij[:, 1] * w + ij[:, 0]
    dsm = np.full(h * w, -np.inf)
    np.maximum.at(dsm, flat, z)
    dsm = dsm.reshape(h, w)
    valid = np.isfinite(dsm)
    if valid.sum() < 16:
        return None
    ndsm = dsm - _block_min(dsm, valid, max(int(round(30.0 / cell)), 2))
    ndsm = np.where(valid, ndsm, 0.0)
    v = ndsm[valid]
    zs = np.where(valid, (ndsm - v.mean()) / (v.std() + 1e-9), 0.0)

    gx = lo[0] + (np.arange(w) + 0.5) * cell
    gy = lo[1] + (np.arange(h) + 0.5) * cell
    cx, cy = np.meshgrid(gx, gy)
    cx, cy = cx.ravel(), cy.ravel()
    grid = PointGrid(np.stack([cx, cy], 1), cell * 8)
    mask = np.zeros(h * w, dtype=bool)
    for rings in footprints:
        b = np.concatenate(rings)
        idx = grid.query(*b.min(0), *b.max(0))
        if len(idx):
            mask[idx] |= points_in_rings(cx[idx], cy[idx], rings)
    mask = mask.reshape(h, w)
    if mask.sum() < 4:
        return None

    s = int(math.ceil(search / cell))
    H, W = h + 2 * s, w + 2 * s
    fm = np.fft.rfft2(mask.astype(np.float64), (H, W))
    num = np.fft.irfft2(np.conj(fm) * np.fft.rfft2(zs, (H, W)), (H, W))
    den = np.fft.irfft2(np.conj(fm) * np.fft.rfft2(valid.astype(np.float64), (H, W)), (H, W))
    shifts = np.arange(-s, s + 1)
    num = num[np.ix_(shifts % H, shifts % W)]
    den = den[np.ix_(shifts % H, shifts % W)]
    enough = den > max(0.25 * den[s, s], 4.0)
    score = np.where(enough, num / np.maximum(den, 1e-9), -np.inf)
    by, bx = np.unravel_index(np.argmax(score), score.shape)
    best, zero = float(score[by, bx]), float(score[s, s])
    return {
        "dx": float(shifts[bx] * cell), "dy": float(shifts[by] * cell),
        "score": best, "score_at_zero": zero,
        "cell": float(cell), "search": float(s * cell),
        "footprint_cells": int(mask.sum()),
        "at_search_edge": bool(abs(shifts[bx]) == s or abs(shifts[by]) == s),
    }
