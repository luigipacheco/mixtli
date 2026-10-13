# SPDX-License-Identifier: GPL-3.0-or-later
"""
Colour a cloud by any attribute it carries, from the sidebar, in one click.

Text-like values (integer codes, booleans, LiDAR classes, mapped GIS tags)
get a palette and a legend you can recolour or switch off per value.
Numbers get a ramp between two editable bounds.

The colour is written to `mx_view_color` and shown through a "Mixtli View"
modifier placed last on the stack, whose only job is to swap the material.
Remove it - "Source Colour" - and the cloud looks exactly as before; nothing
else on the object is touched.
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


def legends(obj):
    try:
        return json.loads(obj.get("mixtli_gis", "{}")).get("legends", {})
    except (TypeError, ValueError):
        return {}


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


def write_colour(d, rgba):
    a = d.attributes.get(VIEW_ATTR)
    if a is not None and (a.data_type != 'FLOAT_COLOR' or a.domain != 'POINT'):
        d.attributes.remove(a)
        a = None
    if a is None:
        a = d.attributes.new(VIEW_ATTR, 'FLOAT_COLOR', 'POINT')
    a.data.foreach_set("color", np.ascontiguousarray(rgba, dtype=np.float32).ravel())
    d.update_tag()


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


# ---------------------------------------------------------------- display modifier

def view_group():
    mat = point_material(types.SimpleNamespace(attr_name=VIEW_ATTR, emission_strength=1.0))
    ng = bpy.data.node_groups.get(VIEW_GROUP)
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
    """The view modifier, re-added last so its material wins over any
    Set Material earlier in the stack (Mixtli Display, MX 10, ...)."""
    old = obj.modifiers.get(VIEW_MOD)
    if old is not None:
        obj.modifiers.remove(old)
    mod = obj.modifiers.new(VIEW_MOD, 'NODES')
    mod.node_group = view_group()
    return mod


def hide_modifier(obj):
    old = obj.modifiers.get(VIEW_MOD)
    if old is not None:
        obj.modifiers.remove(old)


# ---------------------------------------------------------------- legend setup

def show(obj, name):
    """Point the view at `name`, build its legend and colour the cloud."""
    global _suspend
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
        v.attribute = name
        v.kind = kind
        v.items.clear()
        if kind == 'CATEGORY':
            codes, counts = np.unique(vals.astype(np.int64), return_counts=True)
            labels = meta.get("labels", {})
            if a_type == 'BOOLEAN':
                labels = {"0": "false", "1": "true"}
            elif not labels and "class" in name.lower():
                labels = {str(k): s for k, s in ASPRS.items()}
            if len(codes) <= MAX_LEGEND:
                for i, (c, k) in enumerate(sorted(zip(codes.tolist(), counts.tolist()),
                                                  key=lambda x: (x[0] == 0, -x[1]))):
                    it = v.items.add()
                    it.code = c
                    it.count = k
                    it.label = labels.get(str(c), "none" if c == 0 and labels else str(c))
                    it.color = NONE_COLOUR if (c == 0 and labels and a_type != 'BOOLEAN') \
                        else palette_colour(i)
                    it.show = True
            v.distinct = len(codes)
        else:
            f = vals.astype(np.float64)
            skip = name.startswith("gis_") and name != "gis_line_dist"
            sample = f[f != 0.0] if skip and np.any(f != 0.0) else f
            v.skip_zero = skip
            v.lo = float(np.percentile(sample, 2)) if len(sample) else 0.0
            v.hi = float(np.percentile(sample, 98)) if len(sample) else 1.0
            if v.hi <= v.lo:
                v.hi = v.lo + 1.0
            v.distinct = 0
    finally:
        _suspend = False
    recolor(obj)
    show_modifier(obj)
    return None


# ---------------------------------------------------------------- properties

class MIXTLI_PG_legend_item(bpy.types.PropertyGroup):
    code: IntProperty()
    label: StringProperty()
    count: IntProperty()
    color: FloatVectorProperty(subtype='COLOR', size=3, min=0.0, max=1.0,
                               default=(0.5, 0.5, 0.5), update=_on_change)
    show: BoolProperty(default=True, update=_on_change,
                       description="Colour this value; off dims it")


class MIXTLI_PG_view(bpy.types.PropertyGroup):
    attribute: StringProperty()
    kind: EnumProperty(items=[('CATEGORY', "Category", ""), ('NUMBER', "Number", "")])
    items: CollectionProperty(type=MIXTLI_PG_legend_item)
    distinct: IntProperty()
    lo: FloatProperty(name="Low", update=_on_change)
    hi: FloatProperty(name="High", update=_on_change)
    skip_zero: BoolProperty(
        name="Zero Means No Data", default=False, update=_on_change,
        description="Grey out points whose value is exactly 0 - for mapped GIS "
                    "numbers, 0 is what a point outside every feature gets")
    dim: FloatProperty(name="Dimmed", default=0.08, min=0.0, max=1.0, update=_on_change,
                       description="Grey level for values switched off")


# ---------------------------------------------------------------- operators

def _attribute_items(self, context):
    global _items_cache
    obj = context.active_object
    leg = legends(obj) if obj else {}
    _items_cache = [(n, n, leg.get(n, {}).get("key", "")) for n in viewable(obj)] \
        or [("NONE", "(no scalar attributes)", "")]
    return _items_cache


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
        obj = context.active_object
        hide_modifier(obj)
        obj.mixtli_view.attribute = ""
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
            return
        if not v.items:
            layout.label(text="%d distinct values - colours hashed" % v.distinct)
            return
        row = layout.row(align=True)
        row.operator("object.mixtli_view_toggle_all", text="All").state = True
        row.operator("object.mixtli_view_toggle_all", text="None").state = False
        row.prop(v, "dim", text="Dim")
        col = layout.column(align=True)
        for it in v.items[:48]:
            r = col.row(align=True)
            r.prop(it, "show", text="")
            sub = r.row(align=True)
            sub.ui_units_x = 1.6
            sub.prop(it, "color", text="")
            r.label(text="%s  (%s)" % (it.label, _fmt(it.count)))
        if len(v.items) > 48:
            col.label(text="... %d more" % (len(v.items) - 48))


def _fmt(n):
    return "%.1fM" % (n / 1e6) if n >= 1e6 else ("%.0fk" % (n / 1e3) if n >= 1e4 else str(n))


CLASSES = (MIXTLI_PG_legend_item, MIXTLI_PG_view, MIXTLI_OT_view_attribute,
           MIXTLI_OT_view_source, MIXTLI_OT_view_toggle_all, MIXTLI_PT_view)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Object.mixtli_view = bpy.props.PointerProperty(type=MIXTLI_PG_view)


def unregister():
    if hasattr(bpy.types.Object, "mixtli_view"):
        del bpy.types.Object.mixtli_view
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
