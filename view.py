# SPDX-License-Identifier: GPL-3.0-or-later
"""
Colour a cloud by any attribute it carries, from the sidebar, in one click -
and fix the values the data got wrong.

Text-like values (integer codes, booleans, LiDAR classes, mapped GIS tags)
get a palette and a legend you can recolour or switch off per value.
Numbers get a ramp between two editable bounds.

The colour is written to `mx_view_color` and shown through a "Mixtli View"
modifier placed last on the stack, whose only job is to swap the material.
Remove it - "Source Colour" - and the cloud looks exactly as before; nothing
else on the object is touched.

Editing works like vertex groups. In Edit Mode, select points, pick a value
in the legend, Assign. A GIS correction - that "commercial" building is a
museum - can take the whole feature the selection touches, and is kept on
the object so Map to Points puts it back after a remap.

What you change stays. Legend colours and toggles are remembered per
attribute, and the view material and node group are built once: edits made
to them in the Shader or Node Editor survive a change of attribute.
"""

import json
import types

import bpy
import numpy as np
from bpy.props import (BoolProperty, CollectionProperty, EnumProperty,
                       FloatProperty, FloatVectorProperty, IntProperty,
                       StringProperty)

from .build import point_material
from .pointops import srgb_to_linear

VIEW_ATTR = "mx_view_color"
VIEW_MOD = "Mixtli View"
VIEW_GROUP = "Mixtli_AttributeView"
FEATURE_ATTR = "gis_feature"
SCALAR_TYPES = {'FLOAT': np.float32, 'INT': np.int32, 'INT8': np.int32,
                'BOOLEAN': bool}
SKIP_ATTRS = {"position", "radius", "id", VIEW_ATTR}
MAX_LEGEND = 256
CATEGORY_LIMIT = 64          # an INT with no legend and at most this many values

# sRGB, chosen to stay apart from each other on a dark viewport
PALETTE = ((0.31, 0.47, 0.65), (0.95, 0.56, 0.17), (0.88, 0.34, 0.35),
           (0.46, 0.72, 0.70), (0.35, 0.63, 0.31), (0.93, 0.79, 0.28),
           (0.69, 0.48, 0.63), (1.00, 0.62, 0.65), (0.61, 0.46, 0.37),
           (0.73, 0.69, 0.67), (0.55, 0.82, 0.96), (0.80, 0.92, 0.42))
NONE_COLOUR = (0.30, 0.30, 0.30)
RAMP = ((0.27, 0.00, 0.33), (0.23, 0.32, 0.55), (0.13, 0.57, 0.55),
        (0.37, 0.79, 0.38), (0.99, 0.91, 0.14))          # viridis stops

# ASPRS LAS 1.4 standard classes, for an INT attribute named like *class*
ASPRS = {0: "never classified", 1: "unclassified", 2: "ground",
         3: "low vegetation", 4: "medium vegetation", 5: "high vegetation",
         6: "building", 7: "low noise", 9: "water", 10: "rail",
         11: "road surface", 13: "wire guard", 14: "wire conductor",
         15: "transmission tower", 17: "bridge deck", 18: "high noise"}

_suspend = False
_items_cache = []


def palette_colour(i):
    if i < len(PALETTE):
        return PALETTE[i]
    h = (i * 0.618033988749895) % 1.0               # golden-ratio hue walk
    import colorsys
    return colorsys.hsv_to_rgb(h, 0.55, 0.9)


# ---------------------------------------------------------------- data access

def point_data(obj):
    if obj is None:
        return None
    if obj.type in {'POINTCLOUD', 'MESH'}:
        return obj.data
    return None


def point_count(d):
    return len(d.points) if isinstance(d, bpy.types.PointCloud) else len(d.vertices)


class object_mode:
    """Step out of Edit Mode for a block, and back in afterwards.

    A mesh being edited lives in BMesh, where its attributes read as empty
    and cannot be written. Point clouds edit in place and need no detour.
    """

    def __init__(self, obj):
        self.obj = obj
        self.back = False

    def __enter__(self):
        if self.obj.type == 'MESH' and self.obj.mode == 'EDIT':
            bpy.ops.object.mode_set(mode='OBJECT')
            self.back = True
        return self

    def __exit__(self, *exc):
        if self.back:
            bpy.ops.object.mode_set(mode='EDIT')
        return False


def gis_meta(obj):
    """The object's GIS record: legends, how it was mapped, kept corrections."""
    try:
        m = json.loads(obj.get("mixtli_gis", "{}"))
    except (TypeError, ValueError):
        m = {}
    return m if isinstance(m, dict) else {}


def set_gis_meta(obj, meta):
    obj["mixtli_gis"] = json.dumps(meta)


def legends(obj):
    return gis_meta(obj).get("legends", {})


def set_labels(obj, name, labels, added=None):
    """Remember {code: text} for attribute `name` on the object, so a later
    view - and a reopened file - shows the names rather than the numbers.
    `added` lists the values typed in by hand, which a remap keeps."""
    meta = gis_meta(obj)
    leg = meta.setdefault("legends", {}).setdefault(name, {})
    leg.setdefault("kind", "category")
    leg["labels"] = {str(k): v for k, v in labels.items()}
    if added is not None:
        leg["added"] = list(added)
    set_gis_meta(obj, meta)


def added_labels(obj, name):
    return [t for t in legends(obj).get(name, {}).get("added", []) if isinstance(t, str)]


def edits(obj):
    return [e for e in gis_meta(obj).get("edits", []) if isinstance(e, dict)]


def record_edit(obj, edit):
    """Keep a correction to a gis_* attribute, for Map to Points to re-apply."""
    meta = gis_meta(obj)
    meta.setdefault("edits", []).append(edit)
    set_gis_meta(obj, meta)


def viewable(obj):
    """Point-domain scalar attributes, most useful first: GIS, then the rest."""
    d = point_data(obj)
    if d is None:
        return []
    names = [a.name for a in d.attributes
             if a.domain == 'POINT' and a.data_type in SCALAR_TYPES
             and not a.name.startswith(".") and a.name not in SKIP_ATTRS]
    return sorted(names, key=lambda n: (not n.startswith("gis_"), n))


def read_values(d, name):
    a = d.attributes.get(name)
    if a is None or a.data_type not in SCALAR_TYPES or a.domain != 'POINT':
        return None
    buf = np.empty(len(a.data), dtype=SCALAR_TYPES[a.data_type])
    if len(buf):
        a.data.foreach_get("value", buf)
    return buf


def write_values(d, name, values):
    a = d.attributes.get(name)
    if a is None or a.data_type not in SCALAR_TYPES or a.domain != 'POINT':
        return False
    a.data.foreach_set("value", np.ascontiguousarray(
        values, dtype=SCALAR_TYPES[a.data_type]).ravel())
    d.update_tag()
    return True


def write_colour(d, rgba):
    a = d.attributes.get(VIEW_ATTR)
    if a is not None and (a.data_type != 'FLOAT_COLOR' or a.domain != 'POINT'):
        d.attributes.remove(a)
        a = None
    if a is None:
        a = d.attributes.new(VIEW_ATTR, 'FLOAT_COLOR', 'POINT')
    a.data.foreach_set("color", np.ascontiguousarray(rgba, dtype=np.float32).ravel())
    d.update_tag()


# ---------------------------------------------------------------- selection

def selected_mask(obj):
    """Which points are selected in Edit Mode; None when the object is not
    being edited, so there is no selection to speak of."""
    if obj.mode != 'EDIT':
        return None
    d = point_data(obj)
    n = point_count(d)
    if obj.type == 'MESH':
        obj.update_from_editmode()
        m = np.zeros(n, dtype=bool)
        d.vertices.foreach_get("select", m)
        return m
    a = d.attributes.get(".selection")
    if a is None:                           # no attribute: nothing deselected
        return np.ones(n, dtype=bool)
    if a.data_type == 'BOOLEAN':
        m = np.zeros(n, dtype=bool)
        a.data.foreach_get("value", m)
        return m
    f = np.zeros(n, dtype=np.float32)
    a.data.foreach_get("value", f)
    return f > 0.5


def set_selected(obj, mask):
    """Make `mask` the Edit Mode selection. A mesh must be in Object Mode
    for this (see object_mode); it picks the flags up on re-entering."""
    d = point_data(obj)
    mask = np.ascontiguousarray(mask, dtype=bool)
    if obj.type == 'MESH':
        d.vertices.foreach_set("select", mask)
        d.update_tag()
        return
    a = d.attributes.get(".selection")
    if a is None:
        a = d.attributes.new(".selection", 'BOOLEAN', 'POINT')
    if a.data_type == 'BOOLEAN':
        a.data.foreach_set("value", mask)
    else:
        a.data.foreach_set("value", mask.astype(np.float32))
    d.update_tag()


def extend_to_features(obj, d, sel, vals):
    """Grow `sel` to every point of each GIS feature it touches.

    A feature counts only if it carries the attribute being edited (`vals`
    is not 0 on it) or the selection covers at least half of it. A box
    dragged round a building catches a few points of the block it stands
    in; those stay single points rather than turning the whole block.

    Returns (mask, feature ids, loose): the ids are what a correction is
    recorded by, so Map to Points can put it back; `loose` marks the points
    in the mask that no id covers - outside every polygon, in a feature the
    data gave no id, or in one not taken - and are recorded one by one.
    """
    feat = read_values(d, FEATURE_ATTR)
    if feat is None:
        return sel, None, None
    ids = legends(obj).get(FEATURE_ATTR, {}).get("ids", [])
    touched = np.unique(feat[sel])
    taken = []
    for i in touched[touched >= 0].tolist():
        pts = feat == i
        if np.any(vals[pts] != 0) or sel[pts].mean() >= 0.5:
            taken.append(i)
    mask = sel | np.isin(feat, taken)
    keyed = [i for i in taken if i < len(ids) and ids[i]]
    loose = mask & ~np.isin(feat, keyed)
    return mask, [ids[i] for i in keyed], loose


# ---------------------------------------------------------------- colouring

def lin(rgb):
    return srgb_to_linear(np.asarray(rgb, dtype=np.float32))


def ramp(t):
    stops = np.asarray(RAMP, dtype=np.float32)
    t = np.clip(t, 0.0, 1.0) * (len(stops) - 1)
    i = np.minimum(t.astype(np.int64), len(stops) - 2)
    f = (t - i)[:, None]
    return stops[i] * (1.0 - f) + stops[i + 1] * f


def recolor(obj):
    """Rebuild mx_view_color from the object's current view settings."""
    with object_mode(obj):
        return _recolor(obj)


def _recolor(obj):
    v = obj.mixtli_view
    d = point_data(obj)
    vals = read_values(d, v.attribute) if d is not None else None
    if vals is None:
        return False
    n = len(vals)
    out = np.ones((n, 4), dtype=np.float32)
    dim = lin([v.dim] * 3)
    if v.kind == 'CATEGORY':
        codes = vals.astype(np.int64)
        if len(v.items):
            table_codes = np.array([it.code for it in v.items], dtype=np.int64)
            table = np.array([lin(it.color) if it.show else dim for it in v.items],
                             dtype=np.float32)
            order = np.argsort(table_codes)
            pos = np.searchsorted(table_codes[order], codes)
            pos = np.clip(pos, 0, len(order) - 1)
            hit = table_codes[order][pos] == codes
            out[:, :3] = np.where(hit[:, None], table[order][pos], dim)
        else:                                        # too many values for a legend
            h = (codes * 2654435761) & 0xFFFFFF
            hsv = np.stack([(h & 0xFF) / 255.0, 0.45 + ((h >> 8) & 0x3F) / 255.0,
                            0.65 + ((h >> 14) & 0x3F) / 255.0], 1)
            out[:, :3] = lin(_hsv_to_rgb(hsv))
            out[codes == 0, :3] = lin(NONE_COLOUR)
    else:
        f = vals.astype(np.float64)
        span = v.hi - v.lo
        t = (f - v.lo) / span if span != 0 else np.zeros(n)
        out[:, :3] = lin(ramp(t))
        if v.skip_zero:
            out[f == 0.0, :3] = lin(NONE_COLOUR)
    write_colour(d, out)
    return True


def _hsv_to_rgb(hsv):
    h, s, val = hsv[:, 0] * 6.0, hsv[:, 1], hsv[:, 2]
    i = np.floor(h).astype(np.int64) % 6
    f = h - np.floor(h)
    p, q, t = val * (1 - s), val * (1 - s * f), val * (1 - s * (1 - f))
    sel = [(val, t, p), (q, val, p), (p, val, t), (p, q, val), (t, p, val), (val, p, q)]
    rgb = np.empty((len(h), 3))
    for k, (r, g, b) in enumerate(sel):
        m = i == k
        rgb[m] = np.stack([r[m], g[m], b[m]], 1)
    return rgb


def _on_change(self, context):
    if _suspend:
        return
    obj = self.id_data
    if isinstance(obj, bpy.types.Object) and obj.mixtli_view.attribute:
        recolor(obj)


def _on_label(self, context):
    """A value renamed in the legend: keep the name on the object."""
    self.name = self.label
    if _suspend:
        return
    obj = self.id_data
    if isinstance(obj, bpy.types.Object) and obj.mixtli_view.attribute:
        v = obj.mixtli_view
        set_labels(obj, v.attribute, {it.code: it.label for it in v.items})


# ---------------------------------------------------------------- display modifier

def group_usable(ng):
    """A tree with an output that something feeds - one worth keeping."""
    return ng is not None and any(
        n.type == 'GROUP_OUTPUT' and len(n.inputs) and n.inputs[0].is_linked
        for n in ng.nodes)


def view_group(rebuild=False):
    """The view node group: a Set Material and nothing else. Built once;
    after that it is the user's, and whatever they add to it stays."""
    ng = bpy.data.node_groups.get(VIEW_GROUP)
    if group_usable(ng) and not rebuild:
        return ng
    mat = point_material(types.SimpleNamespace(attr_name=VIEW_ATTR, reset_material=rebuild))
    if ng is None:
        ng = bpy.data.node_groups.new(VIEW_GROUP, 'GeometryNodeTree')
    else:
        ng.interface.clear()
        ng.nodes.clear()
    ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    gi = ng.nodes.new("NodeGroupInput");   gi.location = (-300, 0)
    setm = ng.nodes.new("GeometryNodeSetMaterial"); setm.location = (-60, 0)
    setm.inputs["Material"].default_value = mat
    go = ng.nodes.new("NodeGroupOutput");  go.location = (180, 0)
    ng.links.new(gi.outputs["Geometry"], setm.inputs["Geometry"])
    ng.links.new(setm.outputs["Geometry"], go.inputs[0])
    return ng


def show_modifier(obj):
    """The view modifier, last on the stack so its material wins over any
    Set Material before it (Mixtli Display, MX 10, ...). An existing one is
    kept as it is - its group, its settings - and only moved back to the
    end if something was added after it."""
    ng = view_group()
    mod = obj.modifiers.get(VIEW_MOD)
    if mod is not None and mod.type != 'NODES':
        obj.modifiers.remove(mod)
        mod = None
    if mod is None:
        mod = obj.modifiers.new(VIEW_MOD, 'NODES')
    if mod.node_group is None:
        mod.node_group = ng
    i, last = obj.modifiers.find(VIEW_MOD), len(obj.modifiers) - 1
    if i != last:
        try:
            obj.modifiers.move(i, last)
        except Exception:                   # no move(): re-add, keeping its group
            keep = mod.node_group
            obj.modifiers.remove(mod)
            mod = obj.modifiers.new(VIEW_MOD, 'NODES')
            mod.node_group = keep
    return mod


def hide_modifier(obj):
    old = obj.modifiers.get(VIEW_MOD)
    if old is not None:
        obj.modifiers.remove(old)


# ---------------------------------------------------------------- legend setup

def _stash(v):
    """Remember the legend as it stands, under its attribute's name.
    Only ever called with updates suspended."""
    if not v.attribute:
        return
    s = v.saved.get(v.attribute)
    if s is None:
        s = v.saved.add()
        s.name = v.attribute
    s.items.clear()
    for it in v.items:
        c = s.items.add()
        c.code, c.label, c.show = it.code, it.label, it.show
        c.color = it.color
    s.lo, s.hi, s.skip_zero = v.lo, v.hi, v.skip_zero
    s.has_range = v.kind == 'NUMBER'


def _labels_for(name, a_type, meta):
    """({code: text}, whether every one of them belongs in the legend even
    with no point carrying it). Names kept on the object do; the built-in
    fallbacks are only for the values that turn up."""
    labels = meta.get("labels") or {}
    if labels:
        return labels, True
    if a_type == 'BOOLEAN':
        return {"0": "false", "1": "true"}, False
    if "class" in name.lower():
        return {str(k): s for k, s in ASPRS.items()}, False
    return {}, False


def _is_code(k):
    return k.lstrip("-").isdigit()


def _build_items(v, vals, labels, list_all, a_type, saved):
    codes, counts = np.unique(vals.astype(np.int64), return_counts=True)
    v.distinct = len(codes)
    present = dict(zip(codes.tolist(), counts.tolist()))
    listed = set(present)
    if list_all:
        listed |= {int(k) for k in labels if _is_code(k)}
    if len(listed) > MAX_LEGEND:
        return
    prev = {}
    if saved is not None:
        for it in saved.items:
            prev[("code", it.code)] = it
            prev[("label", it.label)] = it
    order = sorted(listed, key=lambda c: (c == 0, -present.get(c, 0), c))
    for i, c in enumerate(order):
        it = v.items.add()
        it.code = c
        it.count = present.get(c, 0)
        it.label = labels.get(str(c), "none" if c == 0 and labels else str(c))
        old = prev.get(("label", it.label)) if it.label != str(c) else None
        if old is None:
            old = prev.get(("code", c))
        if old is not None:                 # as the user last had it
            it.color = old.color
            it.show = old.show
        else:
            it.color = NONE_COLOUR if (c == 0 and labels and a_type != 'BOOLEAN') \
                else palette_colour(i)
            it.show = True


def show(obj, name):
    """Point the view at `name`, build its legend and colour the cloud."""
    global _suspend
    with object_mode(obj):
        d = point_data(obj)
        vals = read_values(d, name) if d is not None else None
        if vals is None:
            return "'%s' is not a point attribute on %s" % (name, obj.name)
        meta = legends(obj).get(name, {})
        a_type = d.attributes[name].data_type
        if meta.get("kind") in ("category", "id") or a_type == 'BOOLEAN':
            kind = 'CATEGORY'
        elif meta.get("kind") == "number" or a_type == 'FLOAT':
            kind = 'NUMBER'
        else:
            kind = 'CATEGORY' if len(np.unique(vals)) <= CATEGORY_LIMIT else 'NUMBER'

        v = obj.mixtli_view
        _suspend = True
        try:
            _stash(v)
            v.attribute = name
            v.kind = kind
            v.items.clear()
            saved = v.saved.get(name)
            if kind == 'CATEGORY':
                labels, list_all = _labels_for(name, a_type, meta)
                _build_items(v, vals, labels, list_all, a_type, saved)
            else:
                f = vals.astype(np.float64)
                skip = name.startswith("gis_") and name != "gis_line_dist"
                sample = f[f != 0.0] if skip and np.any(f != 0.0) else f
                v.skip_zero = skip
                v.lo = float(np.percentile(sample, 2)) if len(sample) else 0.0
                v.hi = float(np.percentile(sample, 98)) if len(sample) else 1.0
                if v.hi <= v.lo:
                    v.hi = v.lo + 1.0
                if saved is not None and saved.has_range:
                    v.lo, v.hi, v.skip_zero = saved.lo, saved.hi, saved.skip_zero
                v.distinct = 0
            v.active = min(v.active, max(len(v.items) - 1, 0))
        finally:
            _suspend = False
        _recolor(obj)
    show_modifier(obj)
    return None


def refresh(obj):
    """Counts after the values changed; a code the legend did not know gets
    a row. Call with the attributes readable (see object_mode)."""
    global _suspend
    v = obj.mixtli_view
    d = point_data(obj)
    vals = read_values(d, v.attribute) if d is not None else None
    if vals is None or v.kind != 'CATEGORY' or not len(v.items):
        return
    codes, counts = np.unique(vals.astype(np.int64), return_counts=True)
    present = dict(zip(codes.tolist(), counts.tolist()))
    _suspend = True
    try:
        known = {it.code for it in v.items}
        for it in v.items:
            it.count = present.get(it.code, 0)
        for c in codes.tolist():
            if c not in known and len(v.items) < MAX_LEGEND:
                it = v.items.add()
                it.code, it.count, it.label, it.show = c, present[c], str(c), True
                it.color = palette_colour(len(v.items) - 1)
        v.distinct = len(codes)
    finally:
        _suspend = False


# ---------------------------------------------------------------- properties

class MIXTLI_PG_legend_item(bpy.types.PropertyGroup):
    code: IntProperty()
    label: StringProperty(name="Value", update=_on_label,
                          description="Double-click to rename; the name is kept "
                                      "on the object")
    count: IntProperty()
    color: FloatVectorProperty(subtype='COLOR', size=3, min=0.0, max=1.0,
                               default=(0.5, 0.5, 0.5), update=_on_change)
    show: BoolProperty(default=True, update=_on_change,
                       description="Colour this value; off dims it")


class MIXTLI_PG_saved_legend(bpy.types.PropertyGroup):
    """A legend as the user left it, keyed (by `name`) on its attribute."""
    items: CollectionProperty(type=MIXTLI_PG_legend_item)
    lo: FloatProperty()
    hi: FloatProperty()
    skip_zero: BoolProperty()
    has_range: BoolProperty()


class MIXTLI_PG_view(bpy.types.PropertyGroup):
    attribute: StringProperty()
    kind: EnumProperty(items=[('CATEGORY', "Category", ""), ('NUMBER', "Number", "")])
    items: CollectionProperty(type=MIXTLI_PG_legend_item)
    active: IntProperty(name="Active Value", min=0,
                        description="The value Assign and Select work with")
    saved: CollectionProperty(type=MIXTLI_PG_saved_legend)
    distinct: IntProperty()
    lo: FloatProperty(name="Low", update=_on_change)
    hi: FloatProperty(name="High", update=_on_change)
    skip_zero: BoolProperty(
        name="Zero Means No Data", default=False, update=_on_change,
        description="Grey out points whose value is exactly 0 - for mapped GIS "
                    "numbers, 0 is what a point outside every feature gets")
    dim: FloatProperty(name="Dimmed", default=0.08, min=0.0, max=1.0, update=_on_change,
                       description="Grey level for values switched off")
    by_feature: BoolProperty(
        name="Whole Feature", default=True,
        description="Assign to every point of the GIS feature(s) the selection "
                    "touches, not only the selected points - one point on a "
                    "mis-tagged building fixes the whole building. A feature "
                    "is taken if it carries this attribute or the selection "
                    "covers at least half of it, so a box round a building "
                    "does not spill into the block it stands in")
    value: FloatProperty(name="Value", description="Number to assign to the selected points")


# ---------------------------------------------------------------- operators

def _attribute_items(self, context):
    global _items_cache
    obj = context.active_object
    leg = legends(obj) if obj else {}
    _items_cache = [(n, n, leg.get(n, {}).get("key", "")) for n in viewable(obj)] \
        or [("NONE", "(no scalar attributes)", "")]
    return _items_cache


def _viewing(context):
    """The active cloud, if it is being coloured by an attribute."""
    obj = context.active_object
    if point_data(obj) is None:
        return None
    if not obj.mixtli_view.attribute or obj.modifiers.get(VIEW_MOD) is None:
        return None
    return obj


def _editing(context):
    """... and in Edit Mode, with a selection to assign to."""
    obj = _viewing(context)
    return obj if obj is not None and obj.mode == 'EDIT' else None


def _by_feature(obj, v):
    """Whole Feature applies to values that came from polygons; a line's tag
    (gis_line_*) has no polygon to grow into."""
    d = point_data(obj)
    return v.by_feature and d.attributes.get(FEATURE_ATTR) is not None \
        and not v.attribute.startswith("gis_line_")


class MIXTLI_OT_view_attribute(bpy.types.Operator):
    """Colour the active cloud by one of its attributes"""
    bl_idname = "object.mixtli_view_attribute"
    bl_label = "Colour by Attribute"
    bl_options = {'REGISTER', 'UNDO'}
    bl_property = "attribute"

    attribute: EnumProperty(name="Attribute", items=_attribute_items)

    @classmethod
    def poll(cls, context):
        return point_data(context.active_object) is not None

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if self.attribute == "NONE":
            return {'CANCELLED'}
        err = show(context.active_object, self.attribute)
        if err:
            self.report({'ERROR'}, err)
            return {'CANCELLED'}
        return {'FINISHED'}


class MIXTLI_OT_view_source(bpy.types.Operator):
    """Go back to the cloud's own colour"""
    bl_idname = "object.mixtli_view_source"
    bl_label = "Source Colour"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and o.modifiers.get(VIEW_MOD) is not None

    def execute(self, context):
        global _suspend
        obj = context.active_object
        hide_modifier(obj)
        _suspend = True
        try:
            _stash(obj.mixtli_view)
            obj.mixtli_view.attribute = ""
        finally:
            _suspend = False
        return {'FINISHED'}


class MIXTLI_OT_view_rebuild(bpy.types.Operator):
    """Rebuild the view material and node group from scratch, discarding any
    edits made to them in the Shader or Node Editor"""
    bl_idname = "object.mixtli_view_rebuild"
    bl_label = "Rebuild View Material"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return _viewing(context) is not None

    def execute(self, context):
        obj = context.active_object
        ng = view_group(rebuild=True)
        mod = obj.modifiers.get(VIEW_MOD)
        if mod is not None:
            mod.node_group = ng
        show_modifier(obj)
        return {'FINISHED'}


class MIXTLI_OT_view_toggle_all(bpy.types.Operator):
    """Switch every value in the legend on or off"""
    bl_idname = "object.mixtli_view_toggle_all"
    bl_label = "Toggle All"
    bl_options = {'REGISTER', 'UNDO'}

    state: BoolProperty()

    def execute(self, context):
        global _suspend
        obj = context.active_object
        _suspend = True
        try:
            for it in obj.mixtli_view.items:
                it.show = self.state
        finally:
            _suspend = False
        recolor(obj)
        return {'FINISHED'}


class MIXTLI_OT_view_assign(bpy.types.Operator):
    """Give the selected points the active legend value - or, for a number,
    the value typed in. Whole Feature grows the selection to every point of
    the GIS feature(s) it touches"""
    bl_idname = "object.mixtli_view_assign"
    bl_label = "Assign Value"
    bl_options = {'REGISTER', 'UNDO'}

    clear: BoolProperty(name="Remove", default=False,
                        description="Set the selected points to none (0) instead")

    @classmethod
    def poll(cls, context):
        return _editing(context) is not None

    def execute(self, context):
        obj = _editing(context)
        v = obj.mixtli_view
        d = point_data(obj)
        if v.kind == 'CATEGORY':
            if self.clear:
                code, label = 0, ""
            elif not (0 <= v.active < len(v.items)):
                self.report({'ERROR'}, "Pick a value in the legend first")
                return {'CANCELLED'}
            else:
                it = v.items[v.active]
                code, label = it.code, it.label if it.code else ""
            value, shown = code, (label or "none")
        else:
            value = 0.0 if self.clear else v.value
            shown = "%g" % value
        sel = selected_mask(obj)
        if sel is None or not sel.any():
            self.report({'WARNING'}, "No points selected")
            return {'CANCELLED'}

        with object_mode(obj):
            vals = read_values(d, v.attribute)
            if vals is None or len(vals) != len(sel):
                self.report({'ERROR'}, "Could not read '%s'" % v.attribute)
                return {'CANCELLED'}
            features, loose = None, None
            mask = sel
            if _by_feature(obj, v):
                mask, features, loose = extend_to_features(obj, d, sel, vals)
            vals[mask] = value
            write_values(d, v.attribute, vals)
            if v.attribute.startswith("gis_"):
                rec = {"attr": v.attribute}
                if v.kind == 'CATEGORY':
                    rec["label"] = label
                else:
                    rec["value"] = float(value)
                if features:
                    rec["features"] = features
                idx = np.flatnonzero(mask if loose is None else loose)
                if len(idx):
                    rec["points"] = idx.tolist()
                record_edit(obj, rec)
            refresh(obj)
            _recolor(obj)
        n = int(mask.sum())
        self.report({'INFO'}, "%s -> %d point%s%s" % (
            shown, n, "" if n == 1 else "s",
            " in %d feature%s" % (len(features), "" if len(features) == 1 else "s")
            if features else ""))
        return {'FINISHED'}


class MIXTLI_OT_view_select(bpy.types.Operator):
    """Select, or deselect, every point carrying the active legend value"""
    bl_idname = "object.mixtli_view_select"
    bl_label = "Select by Value"
    bl_options = {'REGISTER', 'UNDO'}

    deselect: BoolProperty(default=False)

    @classmethod
    def poll(cls, context):
        obj = _editing(context)
        return obj is not None and obj.mixtli_view.kind == 'CATEGORY' \
            and len(obj.mixtli_view.items) > 0

    def execute(self, context):
        obj = _editing(context)
        v = obj.mixtli_view
        d = point_data(obj)
        if not (0 <= v.active < len(v.items)):
            self.report({'ERROR'}, "Pick a value in the legend first")
            return {'CANCELLED'}
        code = v.items[v.active].code
        sel = selected_mask(obj)
        with object_mode(obj):
            vals = read_values(d, v.attribute)
            if vals is None or len(vals) != len(sel):
                self.report({'ERROR'}, "Could not read '%s'" % v.attribute)
                return {'CANCELLED'}
            m = vals.astype(np.int64) == code
            set_selected(obj, (sel & ~m) if self.deselect else (sel | m))
        return {'FINISHED'}


class MIXTLI_OT_view_add_value(bpy.types.Operator):
    """Add a value to the legend - a tag the data did not have, like
    'museum' - to assign to points the map got wrong"""
    bl_idname = "object.mixtli_view_add_value"
    bl_label = "New Value"
    bl_options = {'REGISTER', 'UNDO'}

    label: StringProperty(name="Value", default="")

    @classmethod
    def poll(cls, context):
        obj = _viewing(context)
        if obj is None or obj.mixtli_view.kind != 'CATEGORY':
            return False
        a = point_data(obj).attributes.get(obj.mixtli_view.attribute)
        return a is not None and a.data_type in ('INT', 'INT8')

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        global _suspend
        obj = context.active_object
        v = obj.mixtli_view
        label = self.label.strip()
        if not label:
            self.report({'ERROR'}, "Type a value")
            return {'CANCELLED'}
        for i, it in enumerate(v.items):
            if it.label == label:
                v.active = i
                self.report({'INFO'}, "'%s' is already in the legend" % label)
                return {'FINISHED'}
        meta_labels = legends(obj).get(v.attribute, {}).get("labels", {})
        used = [it.code for it in v.items] + [int(k) for k in meta_labels if _is_code(k)]
        code = max(used + [0]) + 1
        _suspend = True
        try:
            it = v.items.add()
            it.code, it.count, it.show = code, 0, True
            it.color = palette_colour(len(v.items) - 1)
            it.label = label
        finally:
            _suspend = False
        v.active = len(v.items) - 1
        set_labels(obj, v.attribute, {it.code: it.label for it in v.items},
                   added=added_labels(obj, v.attribute) + [label])
        return {'FINISHED'}


class MIXTLI_OT_view_remove_value(bpy.types.Operator):
    """Take the active value out of the legend. Only one no point carries"""
    bl_idname = "object.mixtli_view_remove_value"
    bl_label = "Remove Value"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = _viewing(context)
        if obj is None:
            return False
        v = obj.mixtli_view
        return v.kind == 'CATEGORY' and 0 <= v.active < len(v.items) \
            and v.items[v.active].count == 0

    def execute(self, context):
        obj = context.active_object
        v = obj.mixtli_view
        gone = v.items[v.active].label
        v.items.remove(v.active)
        v.active = min(v.active, max(len(v.items) - 1, 0))
        set_labels(obj, v.attribute, {it.code: it.label for it in v.items},
                   added=[t for t in added_labels(obj, v.attribute) if t != gone])
        return {'FINISHED'}


class MIXTLI_OT_view_clear_edits(bpy.types.Operator):
    """Forget the recorded corrections, so the next Map to Points starts
    from the map data alone. Values already on the points stay as they are"""
    bl_idname = "object.mixtli_view_clear_edits"
    bl_label = "Forget Corrections"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        o = context.active_object
        return o is not None and bool(edits(o))

    def execute(self, context):
        meta = gis_meta(context.active_object)
        meta.pop("edits", None)
        set_gis_meta(context.active_object, meta)
        return {'FINISHED'}


# ---------------------------------------------------------------- panel

class MIXTLI_UL_legend(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "show", text="")
        sub = row.row(align=True)
        sub.ui_units_x = 1.4
        sub.prop(item, "color", text="")
        row.prop(item, "label", text="", emboss=False)
        sub = row.row(align=True)
        sub.alignment = 'RIGHT'
        sub.label(text=_fmt(item.count))


class MIXTLI_PT_view(bpy.types.Panel):
    bl_label = "View by Attribute"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Mixtli"
    bl_parent_id = "MIXTLI_PT_panel"
    bl_order = 2

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        if point_data(obj) is None:
            layout.label(text="Select a point cloud", icon='INFO')
            return
        v = obj.mixtli_view
        active = bool(v.attribute) and obj.modifiers.get(VIEW_MOD) is not None
        row = layout.row(align=True)
        row.operator("object.mixtli_view_attribute",
                     text=v.attribute if active else "Colour by Attribute...",
                     icon='VIEWZOOM')
        row.operator("object.mixtli_view_source", text="", icon='LOOP_BACK')
        if not active:
            return
        if v.kind == 'NUMBER':
            col = layout.column(align=True)
            col.prop(v, "lo")
            col.prop(v, "hi")
            layout.prop(v, "skip_zero")
            self.draw_edit(layout, obj, v)
            return
        if not v.items:
            layout.label(text="%d distinct values - colours hashed" % v.distinct)
            return
        row = layout.row(align=True)
        row.operator("object.mixtli_view_toggle_all", text="All").state = True
        row.operator("object.mixtli_view_toggle_all", text="None").state = False
        row.prop(v, "dim", text="Dim")
        row = layout.row()
        row.template_list("MIXTLI_UL_legend", "", v, "items", v, "active",
                          rows=min(max(len(v.items), 4), 10))
        col = row.column(align=True)
        col.operator("object.mixtli_view_add_value", text="", icon='ADD')
        col.operator("object.mixtli_view_remove_value", text="", icon='REMOVE')
        col.separator()
        col.operator("object.mixtli_view_rebuild", text="", icon='FILE_REFRESH')
        self.draw_edit(layout, obj, v)

    def draw_edit(self, layout, obj, v):
        """Assign / Remove / Select / Deselect, the vertex-group way."""
        col = layout.column(align=True)
        if obj.mode == 'EDIT':
            if v.kind == 'NUMBER':
                row = col.row(align=True)
                row.prop(v, "value", text="")
                row.operator("object.mixtli_view_assign", text="Assign").clear = False
            else:
                row = col.row(align=True)
                row.operator("object.mixtli_view_assign", text="Assign").clear = False
                row.operator("object.mixtli_view_assign", text="Remove").clear = True
                row = col.row(align=True)
                row.operator("object.mixtli_view_select", text="Select").deselect = False
                row.operator("object.mixtli_view_select", text="Deselect").deselect = True
            d = point_data(obj)
            if d.attributes.get(FEATURE_ATTR) is not None \
                    and not v.attribute.startswith("gis_line_"):
                col.prop(v, "by_feature")
        elif obj.type == 'POINTCLOUD' and bpy.app.version < (5, 0, 0):
            col.label(text="Editing point clouds needs Blender 5.0+", icon='INFO')
        else:
            col.label(text="Edit Mode: select points, then assign", icon='INFO')
        n = len(edits(obj))
        if n:
            row = col.row(align=True)
            row.label(text="%d correction%s kept for Map to Points"
                           % (n, "" if n == 1 else "s"), icon='FILE_TICK')
            row.operator("object.mixtli_view_clear_edits", text="", icon='X')


def _fmt(n):
    return "%.1fM" % (n / 1e6) if n >= 1e6 else ("%.0fk" % (n / 1e3) if n >= 1e4 else str(n))


CLASSES = (MIXTLI_PG_legend_item, MIXTLI_PG_saved_legend, MIXTLI_PG_view,
           MIXTLI_UL_legend, MIXTLI_OT_view_attribute, MIXTLI_OT_view_source,
           MIXTLI_OT_view_rebuild, MIXTLI_OT_view_toggle_all,
           MIXTLI_OT_view_assign, MIXTLI_OT_view_select,
           MIXTLI_OT_view_add_value, MIXTLI_OT_view_remove_value,
           MIXTLI_OT_view_clear_edits, MIXTLI_PT_view)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Object.mixtli_view = bpy.props.PointerProperty(type=MIXTLI_PG_view)


def unregister():
    if hasattr(bpy.types.Object, "mixtli_view"):
        del bpy.types.Object.mixtli_view
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
