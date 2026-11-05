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


def filter_sparse_cells(P, C, voxel, min_count, world_anchored=True):
    """Drop points sitting in thinly-populated cells.

    The cheap, grid-based cousin of a statistical outlier filter - no
    KD-tree, which is what would otherwise drag scipy in. Note the threshold
    is only meaningful relative to average occupancy (points / cells).
    """
    if min_count <= 1 or len(P) == 0:
        return P, C, 0
    idx, _ = cell_index(P, voxel, world_anchored)
    _, inv, counts = np.unique(idx, axis=0, return_inverse=True, return_counts=True)
    keep = counts[inv] >= min_count
    return P[keep], C[keep], int((~keep).sum())


def voxel_downsample(P, C, voxel, mode='CENTROID', world_anchored=True):
    """One point per occupied cell.

    'CENTROID' averages the points in the cell, which follows the surface.
    'CENTER' snaps to the cell centre for a true regular lattice. Colour is
    the cell mean either way.
    """
    if len(P) == 0:
        return P, C, np.zeros(0, np.int64)
    idx, origin = cell_index(P, voxel, world_anchored)
    key, inv, counts = np.unique(idx, axis=0, return_inverse=True, return_counts=True)
    n = len(key)
    denom = counts[:, None]
    col = np.stack([np.bincount(inv, weights=C[:, k], minlength=n)
                    for k in range(4)], 1) / denom
    if mode == 'CENTER':
        pos = origin + (key + 0.5) * voxel
    else:
        pos = np.stack([np.bincount(inv, weights=P[:, k], minlength=n)
                        for k in range(3)], 1) / denom
    return pos.astype(np.float32), col.astype(np.float32), counts


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


# ---------------------------------------------------------------- solid fill

MAX_LATTICE_CELLS = 100_000_000


def lattice_bounds(bbmin, bbmax, voxel, world_anchored=True, pad=1):
    """The cells covering a box: (origin, lo, shape).

    Keyed like cell_index(): to the object origin, or to the box's own
    minimum corner. Cell (i, j, k) of the lattice has its centre at
    origin + (lo + (i, j, k) + 0.5) * voxel. `pad` rings of empty cells
    round the box keep a fill from ever touching the lattice edge.
    """
    bbmin = np.asarray(bbmin, dtype=np.float64)
    bbmax = np.asarray(bbmax, dtype=np.float64)
    origin = np.zeros(3) if world_anchored else bbmin.copy()
    lo = np.floor((bbmin - origin) / voxel).astype(np.int64) - pad
    hi = np.floor((bbmax - origin) / voxel).astype(np.int64) + pad
    return origin, lo, hi - lo + 1


def lattice_centres(flat, origin, lo, shape, voxel):
    """Centres of the cells given by flat index, (K, 3) float32."""
    ijk = np.stack(np.unravel_index(flat, shape), 1)
    return (origin + (lo + ijk + 0.5) * voxel).astype(np.float32)


def voxel_cells(P, C, voxel, origin):
    """Occupied cells of a lattice keyed to `origin`:
    (idx (M,3) int, centroid (M,3), mean colour (M,4), counts (M,))."""
    idx = np.floor((P - origin) / voxel).astype(np.int64)
    key, inv, counts = np.unique(idx, axis=0, return_inverse=True, return_counts=True)
    n = len(key)
    denom = counts[:, None]
    pos = np.stack([np.bincount(inv, weights=P[:, k], minlength=n)
                    for k in range(3)], 1) / denom
    col = np.stack([np.bincount(inv, weights=C[:, k], minlength=n)
                    for k in range(4)], 1) / denom
    return key, pos.astype(np.float32), col.astype(np.float32), counts


def ray_crossings(tri_pos, origin, lo, shape, voxel, axis, chunk=1_000_000):
    """Where the triangles cross the lattice's rays along `axis`.

    One ray runs through the centres of each row of cells parallel to
    `axis`. Returns (row, t): the flat index of the row over the other two
    axes (lower axis major) and the coordinate along `axis` where a
    triangle crosses it. The point-in-triangle test is even-odd over the
    three projected edges with a half-open rule, so an edge shared by two
    triangles is counted for exactly one of them and a vertex on the ray
    never double counts - the same trick as points_in_rings() in geo.py.
    Work is proportional to the rows each triangle's projection covers,
    done in chunks so a huge triangle over a fine lattice stays in memory.
    """
    u_ax, v_ax = [a for a in range(3) if a != axis]
    nu, nv = int(shape[u_ax]), int(shape[v_ax])
    ou = origin[u_ax] + (lo[u_ax] + 0.5) * voxel
    ov = origin[v_ax] + (lo[v_ax] + 0.5) * voxel
    T = np.asarray(tri_pos, dtype=np.float64)
    pu, pv, pa = T[:, :, u_ax], T[:, :, v_ax], T[:, :, axis]
    n = np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0])
    nlen = np.linalg.norm(n, axis=1)
    ok = np.abs(n[:, axis]) > 1e-9 * np.maximum(nlen, 1e-30)   # not edge-on to the ray

    iu0 = np.clip(np.ceil((pu.min(1) - ou) / voxel), 0, nu).astype(np.int64)
    iu1 = np.clip(np.floor((pu.max(1) - ou) / voxel), -1, nu - 1).astype(np.int64)
    iv0 = np.clip(np.ceil((pv.min(1) - ov) / voxel), 0, nv).astype(np.int64)
    iv1 = np.clip(np.floor((pv.max(1) - ov) / voxel), -1, nv - 1).astype(np.int64)
    cv = np.maximum(iv1 - iv0 + 1, 0)
    cnt = np.where(ok, np.maximum(iu1 - iu0 + 1, 0) * cv, 0)
    cs = np.concatenate([[0], np.cumsum(cnt)])
    rows_out, t_out = [], []

    def run(tri, k):
        iu = iu0[tri] + k // cv[tri]
        iv = iv0[tri] + k % cv[tri]
        U = ou + iu * voxel
        V = ov + iv * voxel
        inside = np.zeros(len(tri), dtype=bool)
        for a, b in ((0, 1), (1, 2), (2, 0)):
            u1, v1, u2, v2 = pu[tri, a], pv[tri, a], pu[tri, b], pv[tri, b]
            # one orientation per edge whichever triangle it belongs to, so
            # both triangles sharing it compute the same crossing point and
            # a row point exactly on the edge is claimed by exactly one
            swap = (v1 > v2) | ((v1 == v2) & (u1 > u2))
            u1, u2 = np.where(swap, u2, u1), np.where(swap, u1, u2)
            v1, v2 = np.where(swap, v2, v1), np.where(swap, v1, v2)
            crosses = (v1 > V) != (v2 > V)
            with np.errstate(divide='ignore', invalid='ignore'):
                ui = u1 + (V - v1) * (u2 - u1) / (v2 - v1)
            inside ^= crosses & (U < ui)
        if not inside.any():
            return
        tri, iu, iv, U, V = tri[inside], iu[inside], iv[inside], U[inside], V[inside]
        t = pa[tri, 0] - (n[tri, u_ax] * (U - pu[tri, 0]) +
                          n[tri, v_ax] * (V - pv[tri, 0])) / n[tri, axis]
        rows_out.append(iu * nv + iv)
        t_out.append(t)

    nt, s = len(T), 0
    while s < nt:
        e = int(np.searchsorted(cs, cs[s] + chunk, 'right')) - 1
        if e <= s:                            # one triangle wider than a chunk
            e = s + 1
            c = int(cnt[s])
            for k0 in range(0, c, chunk):
                k = np.arange(k0, min(k0 + chunk, c))
                run(np.full(len(k), s, dtype=np.int64), k)
        else:
            total = int(cs[e] - cs[s])
            if total:
                tri = np.repeat(np.arange(s, e), cnt[s:e])
                k = np.arange(total) - np.repeat(cs[s:e] - cs[s], cnt[s:e])
                run(tri, k)
        s = e
    if not rows_out:
        return np.zeros(0, np.int64), np.zeros(0, np.float64)
    return np.concatenate(rows_out), np.concatenate(t_out)


def _row_intervals(row, t):
    """Pair up each row's crossings in order: (row, t_start, t_end).
    An odd leftover - an open surface - is dropped."""
    if len(row) == 0:
        return row, t, t
    order = np.lexsort((t, row))
    row, t = row[order], t[order]
    starts = np.concatenate([[0], np.flatnonzero(np.diff(row)) + 1])
    counts = np.diff(np.concatenate([starts, [len(row)]]))
    pos = np.arange(len(row)) - np.repeat(starts, counts)
    is_start = (pos % 2 == 0) & (pos + 1 < np.repeat(counts, counts))
    i = np.flatnonzero(is_start)
    return row[i], t[i], t[i + 1]


def _mark_spans(shape, axis, row, a0, a1):
    """Dense bool over the lattice: cells a0..a1 (inclusive) along `axis`
    of each row. A difference array summed along the axis, so the cost is
    one pass over the lattice however many spans there are."""
    u_ax, v_ax = [a for a in range(3) if a != axis]
    nv, na = int(shape[v_ax]), int(shape[axis])
    keep = a1 >= a0
    row, a0, a1 = row[keep], a0[keep], a1[keep]
    dshape = [int(s) for s in shape]
    dshape[axis] = na + 1
    diff = np.zeros(dshape, dtype=np.int8)
    idx = [None, None, None]
    idx[u_ax] = row // nv
    idx[v_ax] = row % nv
    idx[axis] = a0
    np.add.at(diff, tuple(idx), 1)
    idx[axis] = a1 + 1
    np.add.at(diff, tuple(idx), -1)
    filled = np.cumsum(diff, axis=axis, dtype=np.int8)
    sl = [slice(None)] * 3
    sl[axis] = slice(0, na)
    return filled[tuple(sl)] > 0


def fill_parity(tri_pos, origin, lo, shape, voxel, axis):
    """Cells whose centre lies between an odd and the next even crossing
    along `axis`: inside, for a closed surface."""
    row, t = ray_crossings(tri_pos, origin, lo, shape, voxel, axis)
    row, t0, t1 = _row_intervals(row, t)
    oa = origin[axis] + (lo[axis] + 0.5) * voxel
    na = int(shape[axis])
    a0 = np.clip(np.ceil((t0 - oa) / voxel), 0, na).astype(np.int64)
    a1 = np.clip(np.floor((t1 - oa) / voxel), -1, na - 1).astype(np.int64)
    return _mark_spans(shape, axis, row, a0, a1)


def fill_below(tri_pos, origin, lo, shape, voxel, base, axis=2):
    """Cells between `base` and the topmost crossing along `axis`: the
    solid under a terrain or an open-bottomed scan."""
    row, t = ray_crossings(tri_pos, origin, lo, shape, voxel, axis)
    if len(row) == 0:
        return np.zeros([int(s) for s in shape], dtype=bool)
    order = np.lexsort((t, row))
    row, t = row[order], t[order]
    last = np.concatenate([row[1:] != row[:-1], [True]])
    row, tmax = row[last], t[last]
    oa = origin[axis] + (lo[axis] + 0.5) * voxel
    na = int(shape[axis])
    a0 = np.full(len(row), int(np.clip(np.ceil((base - oa) / voxel), 0, na)), dtype=np.int64)
    a1 = np.clip(np.floor((tmax - oa) / voxel), -1, na - 1).astype(np.int64)
    return _mark_spans(shape, axis, row, a0, a1)


def solid_fill(tri_pos, origin, lo, shape, voxel, rule='CLOSED', base=None):
    """Which cells of the lattice are inside the surface, as a dense bool.

    CLOSED  ray parity along each of the three axes; a cell is inside
            when at least two agree. One axis alone is exact for a
            watertight mesh, and the vote survives the small holes and
            missing faces that scanned and tiled meshes always have.
    BELOW   everything between the topmost surface and `base` - the
            lowest point of the mesh when None - for terrain and scans
            that are open underneath.
    """
    if rule == 'BELOW':
        if base is None:
            base = float(np.asarray(tri_pos)[:, :, 2].min())
        return fill_below(tri_pos, origin, lo, shape, voxel, base)
    votes = np.zeros([int(s) for s in shape], dtype=np.int8)
    for axis in range(3):
        votes += fill_parity(tri_pos, origin, lo, shape, voxel, axis)
    return votes >= 2


def propagate_from_shell(interior, shell_flat, shell_pos, shape, voxel, origin, lo,
                         max_depth=0.0):
    """Flood inward from the shell cells, one layer of cells at a time.

    Each newly reached cell takes the nearest surface sample known to the
    neighbours that reached it, so every interior cell ends up with a
    nearest shell cell and its distance to it - a vector distance transform
    by propagation, exact to within a cell or so. `max_depth` > 0 stops
    the flood that far below the surface, leaving a thick shell.

    Returns (cell (K,) flat index, nearest (K,) index into the shell
    arrays, depth (K,) distance) for the reached cells, shell excluded.
    """
    shape = tuple(int(s) for s in shape)
    nearest = np.full(interior.size, -1, dtype=np.int32)
    nearest[shell_flat] = np.arange(len(shell_flat), dtype=np.int32)
    shell_pos = np.asarray(shell_pos, dtype=np.float64)
    frontier = np.asarray(shell_flat, dtype=np.int64)
    out_cell, out_near, out_depth = [], [], []
    while len(frontier):
        ijk = np.array(np.unravel_index(frontier, shape))
        cand, src = [], []
        for ax in range(3):
            for d in (-1, 1):
                nb = ijk.copy()
                nb[ax] += d
                valid = (nb[ax] >= 0) & (nb[ax] < shape[ax])
                flat = np.ravel_multi_index(tuple(nb[:, valid]), shape)
                keep = interior[flat] & (nearest[flat] < 0)
                cand.append(flat[keep])
                src.append(nearest[frontier[valid]][keep])
        cand = np.concatenate(cand)
        src = np.concatenate(src)
        if not len(cand):
            break
        cpos = origin + (lo + np.array(np.unravel_index(cand, shape)).T + 0.5) * voxel
        dist = np.linalg.norm(cpos - shell_pos[src], axis=1)
        order = np.lexsort((dist, cand))
        cand, src, dist = cand[order], src[order], dist[order]
        first = np.concatenate([[True], cand[1:] != cand[:-1]])
        cand, src, dist = cand[first], src[first], dist[first]
        if max_depth > 0.0:
            near = dist <= max_depth
            cand, src, dist = cand[near], src[near], dist[near]
            if not len(cand):
                break
        nearest[cand] = src
        out_cell.append(cand)
        out_near.append(src)
        out_depth.append(dist.astype(np.float32))
        frontier = cand
    if not out_cell:
        return (np.zeros(0, np.int64), np.zeros(0, np.int32), np.zeros(0, np.float32))
    return np.concatenate(out_cell), np.concatenate(out_near), np.concatenate(out_depth)


def solid_cloud(tri_pos, P_surf, C_surf, voxel, world_anchored=True, rule='CLOSED',
                max_depth=0.0, include_surface=True, shell_centroid=False,
                interior_colour=None):
    """A solid point cloud from a surface and points scattered over it.

    The scattered points give the shell: one cell per occupied lattice cell
    with the mean colour, as voxel_downsample() would. The triangles give
    the inside, by solid_fill(). Interior cells sit on the lattice and take
    the colour of their nearest shell cell (or `interior_colour`), plus a
    depth: the distance to that shell cell, 0 on the shell itself.

    Returns (P (N,3), C (N,4), depth (N,), info): info counts the lattice
    cells, those inside by the fill rule, the shell, the interior (inside
    minus shell), how many were filled and how many Fill Depth left out.
    """
    tri_pos = np.asarray(tri_pos)
    flat_tri = tri_pos.reshape(-1, 3)
    origin, lo, shape = lattice_bounds(flat_tri.min(0), flat_tri.max(0), voxel, world_anchored)
    ncells = int(np.prod(shape))
    if ncells > MAX_LATTICE_CELLS:
        side = (ncells / MAX_LATTICE_CELLS) ** (1.0 / 3.0) * voxel
        raise ValueError("a %dx%dx%d lattice is %d cells; raise Voxel Size to %.3g or more"
                         % (shape[0], shape[1], shape[2], ncells, side * 1.05))
    shape_t = tuple(int(s) for s in shape)

    if len(P_surf):
        key, spos, scol, _ = voxel_cells(P_surf, C_surf, voxel, origin)
        rel = key - lo
        inb = np.all((rel >= 0) & (rel < shape), axis=1)
        rel, spos, scol = rel[inb], spos[inb], scol[inb]
        shell_flat = np.ravel_multi_index(tuple(rel.T), shape_t)
    else:
        shell_flat = np.zeros(0, np.int64)
        spos = np.zeros((0, 3), np.float32)
        scol = np.zeros((0, 4), np.float32)

    inside = solid_fill(tri_pos, origin, lo, shape, voxel, rule)
    n_parity = int(inside.sum())
    interior = inside.ravel()
    interior[shell_flat] = False
    n_inside = int(interior.sum())

    cells, nearest, depth = propagate_from_shell(
        interior, shell_flat, spos, shape, voxel, origin, lo, max_depth)
    P_in = lattice_centres(cells, origin, lo, shape, voxel)
    if interior_colour is not None:
        C_in = np.tile(np.asarray(interior_colour, dtype=np.float32), (len(cells), 1))
    elif len(scol):
        C_in = scol[nearest]
    else:
        C_in = np.tile(GREY, (len(cells), 1))

    if include_surface and len(shell_flat):
        P_sh = spos if shell_centroid else lattice_centres(shell_flat, origin, lo, shape, voxel)
        P = np.concatenate([P_sh, P_in])
        C = np.concatenate([scol, C_in])
        depth = np.concatenate([np.zeros(len(shell_flat), np.float32), depth])
    else:
        P, C = P_in, C_in

    info = {"cells": ncells, "shape": list(shape_t), "inside": n_parity,
            "shell": int(len(shell_flat)), "interior": n_inside,
            "filled": int(len(cells)), "skipped": n_inside - int(len(cells))}
    return (P.astype(np.float32), C.astype(np.float32), depth.astype(np.float32), info)
