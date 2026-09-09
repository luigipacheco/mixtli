# SPDX-License-Identifier: GPL-3.0-or-later
"""
Flattening many materials down to one colour attribute.

Two routes to the same result:

  SAMPLE  Resolve each material slot to an image texture (or a flat colour)
          and read the pixels straight through the UVs. Exact, noise-free,
          no render, seconds instead of minutes.
  CYCLES  A real bake-to-colour-attribute, for shader networks too involved
          to resolve to a single texture.

Colour management is the part that quietly goes wrong otherwise:
Image.pixels hands back the raw buffer, still sRGB-encoded for byte images,
while a FLOAT_COLOR attribute is read as linear. We decode on the way in.
"""

import bpy
import numpy as np

from .pointops import GREY, srgb_to_linear, sample_image

COLOR_SOCKETS = ("Base Color", "Color", "Emission Color", "Tint")
SKIP_NODES = {"ShaderNodeBsdfTransparent", "ShaderNodeLightPath",
              "ShaderNodeHoldout", "ShaderNodeBsdfTranslucent"}


# ---------------------------------------------------------------- shaders

def output_node(nt):
    for n in nt.nodes:
        if n.bl_idname == "ShaderNodeOutputMaterial" and n.is_active_output:
            return n
    for n in nt.nodes:
        if n.bl_idname == "ShaderNodeOutputMaterial":
            return n
    return None


def resolve_color(node, depth=0, seen=None):
    """Walk a shader network back from the surface to find what drives colour.

    Returns ('IMAGE', image) | ('CONST', rgba) | (None, None).
    """
    if node is None or depth > 12:
        return (None, None)
    seen = seen or set()
    if node.name in seen:
        return (None, None)
    seen.add(node.name)

    if node.bl_idname in SKIP_NODES:
        return (None, None)

    if node.bl_idname == "ShaderNodeTexImage":
        return ("IMAGE", node.image) if node.image else (None, None)

    if node.bl_idname == "ShaderNodeRGB":
        return ("CONST", np.array(node.outputs[0].default_value, dtype=np.float32))

    # Mix / Add Shader: follow the shader branches, ignoring transparency tricks
    if node.bl_idname in ("ShaderNodeMixShader", "ShaderNodeAddShader"):
        for inp in node.inputs:
            if inp.type != 'SHADER' or not inp.links:
                continue
            kind, val = resolve_color(inp.links[0].from_node, depth + 1, seen)
            if kind:
                return (kind, val)
        return (None, None)

    for name in COLOR_SOCKETS:
        inp = node.inputs.get(name)
        if inp is None:
            continue
        if inp.links:
            return resolve_color(inp.links[0].from_node, depth + 1, seen)
        try:
            return ("CONST", np.array(inp.default_value, dtype=np.float32))
        except TypeError:
            pass

    # Generic passthrough (Mix Color, Gamma, Hue/Sat wrappers...)
    for inp in node.inputs:
        if inp.type in ('RGBA', 'SHADER') and inp.links:
            kind, val = resolve_color(inp.links[0].from_node, depth + 1, seen)
            if kind:
                return (kind, val)

    return (None, None)


def material_color_source(mat):
    """('IMAGE', img) | ('CONST', rgba) | (None, None) for one material slot."""
    if mat is None:
        return ("CONST", GREY.copy())
    if not mat.use_nodes or mat.node_tree is None:
        return ("CONST", np.array(mat.diffuse_color, dtype=np.float32))
    out = output_node(mat.node_tree)
    if out is None or not out.inputs["Surface"].links:
        return ("CONST", np.array(mat.diffuse_color, dtype=np.float32))
    kind, val = resolve_color(out.inputs["Surface"].links[0].from_node)
    if kind is None:
        return (None, None)
    return (kind, val)


def image_array(img):
    """(h, w, 4) float32 in LINEAR space, origin bottom-left."""
    w, h = img.size
    if w == 0 or h == 0 or not img.has_data:
        return None
    ch = img.channels
    buf = np.empty(w * h * ch, dtype=np.float32)
    img.pixels.foreach_get(buf)
    buf = buf.reshape(h, w, ch)
    if ch == 3:
        buf = np.dstack([buf, np.ones((h, w, 1), dtype=np.float32)])
    elif ch < 3:
        buf = np.repeat(buf[:, :, :1], 4, axis=2)
        buf[:, :, 3] = 1.0
    if img.colorspace_settings.name in ("sRGB", "Filmic sRGB"):
        buf[:, :, :3] = srgb_to_linear(buf[:, :, :3])
    return buf


# ---------------------------------------------------------------- mesh data

def ensure_color_attribute(me, name):
    ca = me.color_attributes.get(name)
    if ca is not None and (ca.domain != 'CORNER' or ca.data_type != 'FLOAT_COLOR'):
        me.color_attributes.remove(ca)
        ca = None
    if ca is None:
        ca = me.color_attributes.new(name, 'FLOAT_COLOR', 'CORNER')
    idx = me.color_attributes.find(name)
    if idx >= 0:
        me.color_attributes.active_color_index = idx
        me.color_attributes.render_color_index = idx
    return me.color_attributes[name]


def corner_material_index(me):
    """Per-face-corner material slot index.

    Loops are stored in polygon order, so repeating the per-face value
    expands it onto the corner domain for free.
    """
    npoly = len(me.polygons)
    pm = np.empty(npoly, dtype=np.int32)
    lt = np.empty(npoly, dtype=np.int32)
    me.polygons.foreach_get("material_index", pm)
    me.polygons.foreach_get("loop_total", lt)
    return np.repeat(pm, lt)


POINT_COLOR_TYPES = {'FLOAT_COLOR', 'BYTE_COLOR'}


def input_kind(obj):
    """'MESH' if there are faces to sample, 'POINTS' for a PointCloud object
    or a face-less mesh (how most .ply point clouds import), else None."""
    if obj.type == 'POINTCLOUD':
        return 'POINTS'
    if obj.type == 'MESH':
        return 'MESH' if len(obj.data.polygons) else 'POINTS'
    return None


def find_color_attribute(data, preferred=None):
    """Point-domain colour attribute, preferring `preferred`, then the active
    colour, then whatever colour attribute exists.

    Imported clouds name it all sorts of things - Col, Color, COLOR_0 - so
    look rather than assume.
    """
    attrs = data.attributes
    if preferred and preferred in attrs:
        a = attrs[preferred]
        if a.domain == 'POINT' and a.data_type in POINT_COLOR_TYPES:
            return a
    ca = getattr(data, "color_attributes", None)
    if ca is not None and ca.active_color is not None:
        a = ca.active_color
        if a.domain == 'POINT' and a.data_type in POINT_COLOR_TYPES:
            return a
    for a in attrs:
        if a.domain == 'POINT' and a.data_type in POINT_COLOR_TYPES:
            return a
    return None


def read_point_arrays(obj, preferred_attr):
    """(P (N,3), C (N,4), incoming_radius|None, colour attribute name|None).

    Reads an already-existing cloud rather than making one: no bake, no
    scatter. `color` comes back linear for both FLOAT_COLOR and BYTE_COLOR,
    Blender handles the byte decode.
    """
    d = obj.data
    if obj.type == 'POINTCLOUD':
        n = len(d.points)
        P = np.empty(n * 3, dtype=np.float32)
        if n:
            d.attributes["position"].data.foreach_get("vector", P)
    else:
        n = len(d.vertices)
        P = np.empty(n * 3, dtype=np.float32)
        if n:
            d.vertices.foreach_get("co", P)
    P = P.reshape(n, 3)

    C = np.tile(GREY, (n, 1))
    ca = find_color_attribute(d, preferred_attr)
    if ca is not None and len(ca.data) == n and n:
        buf = np.empty(n * 4, dtype=np.float32)
        ca.data.foreach_get("color", buf)
        C = buf.reshape(n, 4)

    incoming_radius = None
    ra = d.attributes.get("radius")
    if ra is not None and ra.domain == 'POINT' and ra.data_type == 'FLOAT' \
            and len(ra.data) == n and n:
        buf = np.empty(n, dtype=np.float32)
        ra.data.foreach_get("value", buf)
        buf = buf[buf > 0.0]
        if len(buf):
            incoming_radius = float(np.median(buf))

    return P, C, incoming_radius, (ca.name if ca is not None else None)


def surface_area(obj):
    """World-space surface area, in square metres."""
    me = obj.data
    n = len(me.polygons)
    if n == 0:
        return 0.0
    a = np.empty(n, dtype=np.float32)
    me.polygons.foreach_get("area", a)
    sx, sy, sz = obj.matrix_world.to_scale()
    return float(a.sum()) * float((abs(sx) + abs(sy) + abs(sz)) / 3.0) ** 2


def triangle_arrays(me, attr_name):
    """(tri_pos (T,3,3), tri_col (T,3,4)) - triangle corners and their colour."""
    me.calc_loop_triangles()
    nt = len(me.loop_triangles)
    if nt == 0:
        return None, None
    tl = np.empty(nt * 3, dtype=np.int32)
    tv = np.empty(nt * 3, dtype=np.int32)
    me.loop_triangles.foreach_get("loops", tl)
    me.loop_triangles.foreach_get("vertices", tv)

    nv = len(me.vertices)
    V = np.empty(nv * 3, dtype=np.float32)
    me.vertices.foreach_get("co", V)
    tri_pos = V.reshape(nv, 3)[tv].reshape(nt, 3, 3)

    ca = me.color_attributes.get(attr_name)
    if ca is None:
        tri_col = np.tile(GREY, (nt, 3, 1))
    else:
        nl = len(me.loops)
        C = np.empty(nl * 4, dtype=np.float32)
        ca.data.foreach_get("color", C)
        tri_col = C.reshape(nl, 4)[tl].reshape(nt, 3, 4)
    return tri_pos, tri_col


# ---------------------------------------------------------------- flatten

def bake_sample(obj, cfg, report):
    """Flatten every material slot into one per-corner colour attribute by
    reading texture pixels through the UVs. True on success."""
    me = obj.data
    nloops = len(me.loops)
    if nloops == 0:
        return False

    slots = list(me.materials) if len(me.materials) else [None]
    sources = [material_color_source(m) for m in slots]
    if any(k is None for k, _ in sources):
        bad = [slots[i].name for i, (k, _) in enumerate(sources)
               if k is None and slots[i]]
        report.append("  unresolvable slots: %s" % ", ".join(bad[:5]))
        return False

    uv_layer = me.uv_layers.active
    if any(k == 'IMAGE' for k, _ in sources) and uv_layer is None:
        report.append("  no UV map; using average texture colour")

    uv = None
    if uv_layer is not None:
        uv = np.empty(nloops * 2, dtype=np.float32)
        uv_layer.data.foreach_get("uv", uv)
        uv = uv.reshape(nloops, 2)

    cmat = corner_material_index(me) if len(me.materials) \
        else np.zeros(nloops, np.int32)
    out = np.tile(GREY, (nloops, 1))

    for i, (kind, val) in enumerate(sources):
        mask = cmat == i
        if not mask.any():
            continue
        if kind == 'CONST':
            c = np.ones(4, dtype=np.float32)
            c[:min(4, len(val))] = val[:4]
            out[mask] = c
        else:
            arr = image_array(val)
            if arr is None:
                continue
            if uv is None:
                out[mask] = arr.reshape(-1, 4).mean(axis=0)
            else:
                out[mask] = sample_image(arr, uv[mask], cfg.bilinear)
            del arr

    out[:, 3] = np.clip(out[:, 3], 0.0, 1.0)
    ca = ensure_color_attribute(me, cfg.attr_name)
    ca.data.foreach_set("color", out.ravel())
    me.update()
    report.append("  sampled %d corners across %d slot(s)" % (nloops, len(slots)))
    return True


def pick_bake_type(obj, cfg):
    if cfg.cycles_bake_type != 'AUTO':
        return cfg.cycles_bake_type
    emissive = total = 0
    for m in obj.data.materials:
        if not m or not m.use_nodes:
            continue
        total += 1
        if any(n.bl_idname == "ShaderNodeEmission" for n in m.node_tree.nodes):
            emissive += 1
    # A DIFFUSE bake of emission-driven materials comes back black
    return 'EMIT' if total and emissive / total > 0.5 else 'DIFFUSE'


def bake_cycles(obj, cfg, report):
    scene = bpy.context.scene
    prev_engine = scene.render.engine
    prev_target = scene.render.bake.target
    ensure_color_attribute(obj.data, cfg.attr_name)

    btype = pick_bake_type(obj, cfg)
    scene.render.engine = 'CYCLES'
    scene.cycles.samples = cfg.cycles_samples
    scene.render.bake.target = 'VERTEX_COLORS'
    if btype == 'DIFFUSE':
        scene.render.bake.use_pass_direct = False
        scene.render.bake.use_pass_indirect = False
        scene.render.bake.use_pass_color = True

    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    try:
        bpy.ops.object.bake(type=btype)
        report.append("  cycles bake (%s, %d samples)" % (btype, cfg.cycles_samples))
        ok = True
    except Exception as e:
        report.append("  cycles bake failed: %s" % e)
        ok = False
    finally:
        scene.render.engine = prev_engine
        scene.render.bake.target = prev_target
    return ok


def bake_onto(obj, cfg, report):
    """Run whichever colour method is configured. True on success."""
    ok = False
    if cfg.method in ('AUTO', 'SAMPLE'):
        ok = bake_sample(obj, cfg, report)
    if not ok and cfg.method in ('AUTO', 'CYCLES'):
        ok = bake_cycles(obj, cfg, report)
    return ok
