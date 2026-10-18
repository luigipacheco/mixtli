# SPDX-License-Identifier: GPL-3.0-or-later
"""
Point operations - pure numpy, no bpy.

The pyntcloud-shaped half of Mixtli. Everything here works on plain (N,3) /
(N,4) arrays, so Python owns the whole pipeline: scatter, filter, grid,
export. Nothing here needs scipy, pandas or numba; Blender ships numpy.

Kept free of bpy imports on purpose - it can be unit-tested outside Blender.
"""

import numpy as np

GREY = np.array([0.8, 0.8, 0.8, 1.0], dtype=np.float32)


# ---------------------------------------------------------------- colour

def srgb_to_linear(a):
    """Decode sRGB-encoded floats (0-1) to linear scene-referred."""
    a = np.asarray(a, dtype=np.float32)
    return np.where(a <= 0.04045, a / 12.92,
                    np.power((a + 0.055) / 1.055, 2.4)).astype(np.float32)


def linear_to_srgb(a):
    a = np.clip(np.asarray(a, dtype=np.float32), 0.0, 1.0)
    return np.where(a <= 0.0031308, a * 12.92,
                    1.055 * np.power(a, 1.0 / 2.4) - 0.055).astype(np.float32)


def sample_image(arr, uv, bilinear=True):
    """Sample an (h,w,4) array at uv (N,2) with tiling repeat. Returns (N,4).

    Blender image buffers start bottom-left, which matches UV space, so no
    flip is needed.
    """
    h, w = arr.shape[0], arr.shape[1]
    u = np.mod(uv[:, 0], 1.0)
    v = np.mod(uv[:, 1], 1.0)
    if not bilinear:
        x = np.clip((u * w).astype(np.int32), 0, w - 1)
        y = np.clip((v * h).astype(np.int32), 0, h - 1)
        return arr[y, x]
    x = u * w - 0.5
    y = v * h - 0.5
    x0 = np.floor(x).astype(np.int32)
    y0 = np.floor(y).astype(np.int32)
    fx = (x - x0).astype(np.float32)[:, None]
    fy = (y - y0).astype(np.float32)[:, None]
    x0m, x1m = np.mod(x0, w), np.mod(x0 + 1, w)
    y0m, y1m = np.mod(y0, h), np.mod(y0 + 1, h)
    p00 = arr[y0m, x0m]; p10 = arr[y0m, x1m]
    p01 = arr[y1m, x0m]; p11 = arr[y1m, x1m]
    return ((p00 * (1 - fx) + p10 * fx) * (1 - fy) +
            (p01 * (1 - fx) + p11 * fx) * fy)


# ---------------------------------------------------------------- scatter

def scatter_on_triangles(tri_pos, tri_col, count, seed):
    """Area-weighted barycentric sampling.

    Colour is interpolated per point the same way Distribute Points on Faces
    does it in geometry nodes, so the two paths agree visually.
    """
    rng = np.random.default_rng(seed)
    e1 = tri_pos[:, 1] - tri_pos[:, 0]
    e2 = tri_pos[:, 2] - tri_pos[:, 0]
    area = 0.5 * np.linalg.norm(np.cross(e1, e2), axis=1)
    if float(area.sum()) <= 0.0:
        return (np.zeros((0, 3), np.float32), np.zeros((0, 4), np.float32))

    cdf = np.cumsum(area.astype(np.float64))
    cdf /= cdf[-1]
    pick = np.searchsorted(cdf, rng.random(count))
    np.clip(pick, 0, len(area) - 1, out=pick)

    u = rng.random(count)
    v = rng.random(count)
    flip = (u + v) > 1.0            # fold the unit square onto the triangle
    u[flip] = 1.0 - u[flip]
    v[flip] = 1.0 - v[flip]
    w = (1.0 - u - v)[:, None]
    u = u[:, None]
    v = v[:, None]

    P = tri_pos[pick, 0] * w + tri_pos[pick, 1] * u + tri_pos[pick, 2] * v
    C = tri_col[pick, 0] * w + tri_col[pick, 1] * u + tri_col[pick, 2] * v
    return P.astype(np.float32), C.astype(np.float32)


# ---------------------------------------------------------------- grid

def cell_index(P, voxel, world_anchored=True):
    """Integer grid cell per point, plus the grid origin.

    world_anchored keys the lattice to the object origin, so the same voxel
    size always lands on the same planes no matter how many points were
    scattered or which seed was used - and separate objects share one grid.
    Set it False to key the lattice to the cloud's own bounding box instead.
    """
    origin = np.zeros(3, dtype=np.float64) if world_anchored else P.min(0)
    return np.floor((P - origin) / voxel).astype(np.int64), origin


def filter_sparse_cells(P, C, voxel, min_count, world_anchored=True, extras=None):
    """Drop points sitting in thinly-populated cells.

    The cheap, grid-based cousin of a statistical outlier filter - no
    KD-tree, which is what would otherwise drag scipy in. Note the threshold
    is only meaningful relative to average occupancy (points / cells).
    """
    if min_count <= 1 or len(P) == 0:
        return P, C, 0
    idx, _ = cell_index(P, voxel, world_anchored)
    _, inv, counts = np.unique(idx, axis=0, return_inverse=True, return_counts=True)
    keep = counts[inv.ravel()] >= min_count
    if extras:
        for k, (kind, v) in list(extras.items()):
            extras[k] = (kind, v[keep])
    return P[keep], C[keep], int((~keep).sum())


def voxel_downsample(P, C, voxel, mode='CENTROID', world_anchored=True, extras=None):
    """One point per occupied cell.

    'CENTROID' averages the points in the cell, which follows the surface.
    'CENTER' snaps to the cell centre for a true regular lattice. Colour is
    the cell mean either way. `extras`, a dict of name -> (blender type,
    array), is reduced in place: see reduce_cells.
    """
    if len(P) == 0:
        return P, C, np.zeros(0, np.int64)
    idx, origin = cell_index(P, voxel, world_anchored)
    key, inv, counts = np.unique(idx, axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()                    # numpy 2 can hand it back as (N, 1)
    n = len(key)
    if extras:
        for k, (kind, v) in list(extras.items()):
            extras[k] = (kind, reduce_cells(kind, v, inv, n, counts))
    denom = counts[:, None]
    col = np.stack([np.bincount(inv, weights=C[:, k], minlength=n)
                    for k in range(4)], 1) / denom
    if mode == 'CENTER':
        pos = origin + (key + 0.5) * voxel
    else:
        pos = np.stack([np.bincount(inv, weights=P[:, k], minlength=n)
                        for k in range(3)], 1) / denom
    return pos.astype(np.float32), col.astype(np.float32), counts


def reduce_cells(kind, v, inv, n, counts):
    """One value per cell. Numbers and vectors average; integer codes and
    booleans take the commonest value in the cell, because the mean of
    'ground' and 'building' is not a class."""
    if kind in ('FLOAT', 'FLOAT_VECTOR', 'FLOAT_COLOR'):
        v2 = v.reshape(len(v), -1).astype(np.float64)
        out = np.stack([np.bincount(inv, weights=v2[:, k], minlength=n)
                        for k in range(v2.shape[1])], 1) / counts[:, None]
        return out.reshape((n,) + v.shape[1:]).astype(np.float32)
    pair, pc = np.unique(np.stack([inv, v.astype(np.int64)], 1), axis=0,
                         return_counts=True)
    order = np.lexsort((-pc, pair[:, 0]))        # by cell, commonest first
    pair = pair[order]
    first = np.r_[True, pair[1:, 0] != pair[:-1, 0]]
    out = np.zeros(n, dtype=np.int64)
    out[pair[first, 0]] = pair[first, 1]
    return out.astype(bool) if kind == 'BOOLEAN' else out.astype(np.int32)


# ---------------------------------------------------------------- terrain

def height_above_ground(P, cell=5.0, window=60.0):
    """Approximate height of each point over the local ground.

    Ground is the lowest point per cell, then the lowest of those within
    `window` - wide enough to see past most buildings to the street - so a
    roof never mistakes itself for ground. Slopes steeper than the window
    can resolve read as a few metres of height; good for massing and
    canopy, not for survey.
    """
    P = np.asarray(P, dtype=np.float64)
    if len(P) == 0:
        return np.zeros(0, np.float32)
    lo = P[:, :2].min(0)
    ij = np.floor((P[:, :2] - lo) / cell).astype(np.int64)
    w, h = ij.max(0) + 1
    flat = ij[:, 1] * w + ij[:, 0]
    g = np.full(w * h, np.inf)
    np.minimum.at(g, flat, P[:, 2])
    g = g.reshape(h, w)
    r = max(int(round(window / cell / 2.0)), 1)
    for axis in (0, 1):                      # separable min filter
        pad = [(0, 0), (0, 0)]
        pad[axis] = (r, r)
        gp = np.pad(g, pad, constant_values=np.inf)
        n = g.shape[axis]
        g = np.min([np.take(gp, np.arange(k, k + n), axis=axis)
                    for k in range(2 * r + 1)], axis=0)
    ground = g.ravel()[flat]
    return np.maximum(P[:, 2] - ground, 0.0).astype(np.float32)


# ---------------------------------------------------------------- export

def write_ply(path, P, C):
    """Binary little-endian PLY with 8-bit colour.

    Colours are re-encoded to sRGB on the way out, because that is what
    every PLY viewer assumes.
    """
    n = len(P)
    rgb = (linear_to_srgb(C[:, :3]) * 255.0 + 0.5).astype(np.uint8)
    header = ("ply\n"
              "format binary_little_endian 1.0\n"
              "comment written by Mixtli\n"
              "element vertex %d\n"
              "property float x\nproperty float y\nproperty float z\n"
              "property uchar red\nproperty uchar green\nproperty uchar blue\n"
              "end_header\n" % n).encode("ascii")
    dt = np.dtype([('x', '<f4'), ('y', '<f4'), ('z', '<f4'),
                   ('red', 'u1'), ('green', 'u1'), ('blue', 'u1')])
    arr = np.empty(n, dtype=dt)
    arr['x'], arr['y'], arr['z'] = P[:, 0], P[:, 1], P[:, 2]
    arr['red'], arr['green'], arr['blue'] = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    with open(path, 'wb') as f:
        f.write(header)
        f.write(arr.tobytes())
    return n
