# SPDX-License-Identifier: GPL-3.0-or-later
"""
Reading point-cloud files with everything they carry, not just xyz and colour.

  LAS 1.0-1.4   LiDAR. Point formats 0-10: intensity, returns,
                classification, scan angle, source ID, GPS time, RGB, NIR.
  PLY           scans and photogrammetry. ASCII or binary; every vertex
                property comes through - normals, confidence, scalar fields.
  XYZ / TXT /   one point per line. A header line names the columns;
  CSV / PTS     without one, the column count decides (xyz, xyzi, xyzrgb,
                xyzirgb).

Every reader returns the same Cloud. Positions stay float64 here, because
LiDAR comes in projected coordinates hundreds of kilometres from anywhere
and float32 would round them to the metre; the caller recentres before
Blender sees them. LAZ is compressed with a codec numpy cannot read - it is
refused with what to do instead. No bpy here.
"""

import os
import re

import numpy as np

from .pointops import srgb_to_linear

EXTENSIONS = (".las", ".ply", ".xyz", ".txt", ".csv", ".pts")


class Cloud:
    def __init__(self, position, color=None, attrs=None, info=None):
        self.position = np.asarray(position, dtype=np.float64)
        self.color = color                  # (N, 4) linear float32, or None
        self.attrs = attrs or {}            # name -> (blender type, array)
        self.info = info or []

    def __len__(self):
        return len(self.position)


def read(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".laz":
        raise ValueError("LAZ is compressed. Decompress it to .las first - "
                         "'laszip -i in.laz -o out.las', 'pdal translate in.laz "
                         "out.las', or open and save in CloudCompare")
    if ext == ".las":
        return read_las(path)
    if ext == ".ply":
        return read_ply(path)
    if ext in (".xyz", ".txt", ".csv", ".pts"):
        return read_text(path)
    raise ValueError("unsupported file type '%s'" % ext)


def colour_from(rgb):
    """Integer or float RGB of unknown range -> linear (N, 4)."""
    rgb = np.asarray(rgb, dtype=np.float32)
    top = float(rgb.max()) if rgb.size else 1.0
    scale = 65535.0 if top > 255.0 else (255.0 if top > 1.0 else 1.0)
    out = np.ones((len(rgb), 4), dtype=np.float32)
    out[:, :3] = srgb_to_linear(np.clip(rgb / scale, 0.0, 1.0))
    return out


# ---------------------------------------------------------------- LAS

# format -> [(name, numpy type, byte offset)] after X, Y, Z
_LAS_LEGACY = [("intensity", "<u2", 12), ("bits", "u1", 14), ("class", "u1", 15),
               ("scan_angle", "i1", 16), ("user_data", "u1", 17),
               ("point_source_id", "<u2", 18)]
_LAS_14 = [("intensity", "<u2", 12), ("bits", "u1", 14), ("flags", "u1", 15),
           ("class", "u1", 16), ("user_data", "u1", 17), ("scan_angle", "<i2", 18),
           ("point_source_id", "<u2", 20), ("gps_time", "<f8", 22)]
LAS_FORMATS = {
    0: _LAS_LEGACY,
    1: _LAS_LEGACY + [("gps_time", "<f8", 20)],
    2: _LAS_LEGACY + [("rgb", ("<u2", 3), 20)],
    3: _LAS_LEGACY + [("gps_time", "<f8", 20), ("rgb", ("<u2", 3), 28)],
    4: _LAS_LEGACY + [("gps_time", "<f8", 20)],
    5: _LAS_LEGACY + [("gps_time", "<f8", 20), ("rgb", ("<u2", 3), 28)],
    6: _LAS_14,
    7: _LAS_14 + [("rgb", ("<u2", 3), 30)],
    8: _LAS_14 + [("rgb", ("<u2", 3), 30), ("nir", "<u2", 36)],
    9: _LAS_14,
    10: _LAS_14 + [("rgb", ("<u2", 3), 30), ("nir", "<u2", 36)],
}
LAS_MIN_LENGTH = {0: 20, 1: 28, 2: 26, 3: 34, 4: 57, 5: 63,
                  6: 30, 7: 36, 8: 38, 9: 59, 10: 67}


def read_las(path):
    with open(path, "rb") as f:
        head = f.read(375)
    if head[:4] != b"LASF":
        raise ValueError("not a LAS file")
    major, minor = head[24], head[25]
    offset = int(np.frombuffer(head, "<u4", 1, 96)[0])
    fmt_byte = head[104]
    if fmt_byte & 0x80 or fmt_byte & 0x40:
        raise ValueError("this LAS is LAZ-compressed; decompress it first")
    fmt = fmt_byte & 0x3F
    length = int(np.frombuffer(head, "<u2", 1, 105)[0])
    count = int(np.frombuffer(head, "<u4", 1, 107)[0])
    if (major, minor) >= (1, 4) and len(head) >= 255:
        count = int(np.frombuffer(head, "<u8", 1, 247)[0]) or count
    scale = np.frombuffer(head, "<f8", 3, 131)
    shift = np.frombuffer(head, "<f8", 3, 155)
    if fmt not in LAS_FORMATS or length < LAS_MIN_LENGTH[fmt]:
        raise ValueError("unsupported LAS point format %d" % fmt)

    fields = [("xyz", ("<i4", 3), 0)] + LAS_FORMATS[fmt]
    dt = np.dtype({"names": [n for n, _, _ in fields],
                   "formats": [t for _, t, _ in fields],
                   "offsets": [o for _, _, o in fields], "itemsize": length})
    count = min(count, (os.path.getsize(path) - offset) // length)
    rec = np.memmap(path, dt, "r", offset, (count,))

    P = rec["xyz"].astype(np.float64) * scale + shift
    attrs = {"intensity": ('INT', rec["intensity"].astype(np.int32))}
    bits = rec["bits"]
    legacy = fmt < 6
    if legacy:
        attrs["return_number"] = ('INT', (bits & 0x07).astype(np.int32))
        attrs["number_of_returns"] = ('INT', ((bits >> 3) & 0x07).astype(np.int32))
        cls = rec["class"]
        attrs["classification"] = ('INT', (cls & 0x1F).astype(np.int32))
        withheld = (cls & 0x80) != 0
        attrs["scan_angle"] = ('FLOAT', rec["scan_angle"].astype(np.float32))
    else:
        attrs["return_number"] = ('INT', (bits & 0x0F).astype(np.int32))
        attrs["number_of_returns"] = ('INT', ((bits >> 4) & 0x0F).astype(np.int32))
        attrs["classification"] = ('INT', rec["class"].astype(np.int32))
        withheld = (rec["flags"] & 0x04) != 0
        attrs["scan_angle"] = ('FLOAT', (rec["scan_angle"] * 0.006).astype(np.float32))
    if withheld.any():
        attrs["withheld"] = ('BOOLEAN', withheld)
    attrs["user_data"] = ('INT', rec["user_data"].astype(np.int32))
    attrs["point_source_id"] = ('INT', rec["point_source_id"].astype(np.int32))
    info = ["LAS %d.%d, point format %d, %d points" % (major, minor, fmt, count)]
    if "gps_time" in rec.dtype.names:
        t = rec["gps_time"]
        t0 = float(t.min()) if len(t) else 0.0
        attrs["gps_time"] = ('FLOAT', (t - t0).astype(np.float32))
        info.append("gps_time is seconds after %.3f" % t0)
    if "nir" in rec.dtype.names:
        attrs["nir"] = ('INT', rec["nir"].astype(np.int32))
    color = None
    if "rgb" in rec.dtype.names and len(rec) and rec["rgb"].any():
        color = colour_from(rec["rgb"])
    return Cloud(P, color, attrs, info)


def write_las(path, P, cls=None, intensity=None, rgb=None, scale=0.001):
    """Minimal LAS 1.2 writer (format 0 or 2). For tests and round trips."""
    P = np.asarray(P, dtype=np.float64)
    n = len(P)
    fmt = 2 if rgb is not None else 0
    length = 26 if fmt == 2 else 20
    shift = np.floor(P.min(0)) if n else np.zeros(3)
    head = bytearray(227)
    head[0:4] = b"LASF"
    head[24], head[25] = 1, 2
    head[94:96] = np.uint16(227).tobytes()
    head[96:100] = np.uint32(227).tobytes()
    head[104] = fmt
    head[105:107] = np.uint16(length).tobytes()
    head[107:111] = np.uint32(n).tobytes()
    head[131:155] = np.full(3, scale, "<f8").tobytes()
    head[155:179] = shift.astype("<f8").tobytes()
    mx, mn = (P.max(0), P.min(0)) if n else (np.zeros(3), np.zeros(3))
    head[179:227] = np.array([mx[0], mn[0], mx[1], mn[1], mx[2], mn[2]], "<f8").tobytes()
    fields = [("xyz", ("<i4", 3), 0)] + LAS_FORMATS[fmt]
    dt = np.dtype({"names": [f for f, _, _ in fields], "formats": [t for _, t, _ in fields],
                   "offsets": [o for _, _, o in fields], "itemsize": length})
    rec = np.zeros(n, dt)
    rec["xyz"] = np.round((P - shift) / scale).astype(np.int32)
    rec["bits"] = 0x09                                  # return 1 of 1
    if cls is not None:
        rec["class"] = cls
    if intensity is not None:
        rec["intensity"] = intensity
    if rgb is not None:
        rec["rgb"] = rgb
    with open(path, "wb") as f:
        f.write(bytes(head))
        f.write(rec.tobytes())


# ---------------------------------------------------------------- PLY

_PLY_TYPES = {"char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
              "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
              "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
              "float": "f4", "float32": "f4", "double": "f8", "float64": "f8"}
_COLOUR = (("red", "green", "blue"), ("r", "g", "b"), ("diffuse_red", "diffuse_green",
                                                       "diffuse_blue"))
_NORMAL = ("nx", "ny", "nz")


def _ply_header(f):
    if f.readline().strip() != b"ply":
        raise ValueError("not a PLY file")
    fmt, elements = None, []
    while True:
        line = f.readline()
        if not line:
            raise ValueError("PLY header never ends")
        w = line.decode("ascii", "replace").split()
        if not w or w[0] in ("comment", "obj_info"):
            continue
        if w[0] == "format":
            fmt = w[1]
        elif w[0] == "element":
            elements.append([w[1], int(w[2]), []])
        elif w[0] == "property":
            elements[-1][2].append(("list",) + tuple(w[2:]) if w[1] == "list"
                                   else (w[2], w[1]))
        elif w[0] == "end_header":
            return fmt, elements


def read_ply(path):
    with open(path, "rb") as f:
        fmt, elements = _ply_header(f)
        names = [e[0] for e in elements]
        if "vertex" not in names:
            raise ValueError("PLY has no vertices")
        faces = next((e[1] for e in elements if e[0] == "face"), 0)
        if faces:
            raise ValueError("this PLY is a mesh (%d faces) - use File > Import > "
                             "Stanford PLY, then Mesh to Point Cloud" % faces)
        if names[0] != "vertex":
            raise ValueError("PLY vertices must come first")
        _, n, props = elements[0]
        if any(p[0] == "list" for p in props):
            raise ValueError("PLY vertex lists are not supported")
        if fmt == "ascii":
            data = np.loadtxt(f, dtype=np.float64, max_rows=n, ndmin=2)
            cols = {name: data[:, i] for i, (name, _) in enumerate(props)}
        else:
            end = "<" if fmt == "binary_little_endian" else ">"
            dt = np.dtype([(name, end + _PLY_TYPES[t]) for name, t in props])
            rec = np.frombuffer(f.read(dt.itemsize * n), dt, n)
            cols = {name: rec[name] for name, _ in props}
    types = dict(props)
    return _from_columns(cols, ["PLY %s, %d points" % (fmt, n)], types)


# ---------------------------------------------------------------- text

_NUM = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
_SPLIT = re.compile(r"[,;\s]+")


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        first = f.readline().strip()
        words = [w for w in _SPLIT.split(first) if w]
        skip, names = 0, None
        if words and not all(_NUM.match(w) for w in words):
            names = [w.lower().strip('"') for w in words]
            skip = 1
        elif len(words) == 1:                       # .pts starts with a count
            skip = 1
    delim = "," if "," in first else None
    data = np.loadtxt(path, dtype=np.float64, delimiter=delim, skiprows=skip,
                      ndmin=2, comments=("#", "//"))
    k = data.shape[1]
    if names is None or len(names) != k:
        names = {3: ["x", "y", "z"], 4: ["x", "y", "z", "intensity"],
                 6: ["x", "y", "z", "red", "green", "blue"],
                 7: ["x", "y", "z", "intensity", "red", "green", "blue"]}.get(
            k, ["x", "y", "z"] + ["field_%d" % i for i in range(4, k + 1)])
    if k < 3:
        raise ValueError("need at least x, y, z columns")
    cols = {nm: data[:, i] for i, nm in enumerate(names)}
    return _from_columns(cols, ["%s, %d points, columns: %s"
                                % (os.path.basename(path), len(data), ", ".join(names))])


# ---------------------------------------------------------------- columns

def attr_name(key):
    return (re.sub(r"[^0-9A-Za-z_]+", "_", key).strip("_").lower() or "field")[:60]


def _from_columns(cols, info, types=None):
    """Named columns -> Cloud: x/y/z, a colour triple, normals, and the rest
    as scalars - integers when every value is whole, else floats."""
    for a in ("x", "y", "z"):
        if a not in cols:
            raise ValueError("no '%s' column" % a)
    P = np.stack([cols.pop("x"), cols.pop("y"), cols.pop("z")], 1)
    color = None
    for trip in _COLOUR:
        if all(c in cols for c in trip):
            color = colour_from(np.stack([cols.pop(c) for c in trip], 1))
            break
    cols.pop("alpha", None)
    attrs = {}
    if all(c in cols for c in _NORMAL):
        attrs["normal"] = ('FLOAT_VECTOR',
                           np.stack([cols.pop(c) for c in _NORMAL], 1).astype(np.float32))
    for name, v in cols.items():
        t = (types or {}).get(name)
        whole = (t is not None and not t.startswith(("float", "double"))) or \
            (t is None and len(v) and np.all(np.mod(v, 1) == 0) and np.abs(v).max() < 2 ** 31)
        attrs[attr_name(name)] = ('INT', v.astype(np.int32)) if whole else \
            ('FLOAT', v.astype(np.float32))
    return Cloud(P, color, attrs, info)


# ---------------------------------------------------------------- sizing

def spacing(P):
    """Typical distance between neighbours, from the bounding box: the area
    of half its surface - the ground for LiDAR, roughly the skin for a
    scanned object - shared out between the points."""
    n = len(P)
    if n < 2:
        return 0.01
    d = np.maximum(P.max(0) - P.min(0), 1e-9)
    half_skin = d[0] * d[1] + d[1] * d[2] + d[0] * d[2]
    return float(np.sqrt(half_skin / n))
