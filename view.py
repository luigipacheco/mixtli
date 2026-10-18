# SPDX-License-Identifier: GPL-3.0-or-later
"""
Colour a cloud by any attribute it carries, from the sidebar, in one click.

Text-like values (integer codes, booleans, LiDAR classes, mapped GIS tags)
get a palette and a legend you can recolour or switch off per value.
Numbers get a ramp between two editable bounds. Colour attributes show as
they are; vectors - normals - as a normal map. Switched-off values and
numbers out of range can be dimmed or hidden outright.

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
HIDE_ATTR = "mx_view_hide"
VIEW_MOD = "Mixtli View"
VIEW_GROUP = "Mixtli_AttributeView"
SCALAR_TYPES = {'FLOAT': np.float32, 'INT': np.int32, 'INT8': np.int32,
                'BOOLEAN': bool}
COLOUR_TYPES = {'FLOAT_COLOR', 'BYTE_COLOR'}
VECTOR_TYPES = {'FLOAT_VECTOR'}
SKIP_ATTRS = {"position", "radius", "id", VIEW_ATTR, HIDE_ATTR}
MAX_LEGEND = 256
CATEGORY_LIMIT = 64          # an INT with no legend and at most this many values

# sRGB, chosen to stay apart from each other on a dark viewport
PALETTE = ((0.31, 0.47, 0.65), (0.95, 0.56, 0.17), (0.88, 0.34, 0.35),
           (0.46, 0.72, 0.70), (0.35, 0.63, 0.31), (0.93, 0.79, 0.28),
           (0.69, 0.48, 0.63), (1.00, 0.62, 0.65), (0.61, 0.46, 0.37),
           (0.73, 0.69, 0.67), (0.55, 0.82, 0.96), (0.80, 0.92, 0.42))
NONE_COLOUR = (0.30, 0.30, 0.30)
RAMPS = {   # sRGB stops
    'VIRIDIS': ((0.27, 0.00, 0.33), (0.23, 0.32, 0.55), (0.13, 0.57, 0.55),
                (0.37, 0.79, 0.38), (0.99, 0.91, 0.14)),
    'TERRAIN': ((0.17, 0.33, 0.60), (0.35, 0.62, 0.38), (0.84, 0.80, 0.52),
                (0.62, 0.46, 0.32), (0.97, 0.96, 0.95)),
    'HEAT': ((0.05, 0.03, 0.15), (0.45, 0.10, 0.45), (0.85, 0.25, 0.30),
             (0.98, 0.60, 0.20), (0.99, 0.98, 0.70)),
    'DIVERGING': ((0.23, 0.30, 0.75), (0.55, 0.69, 0.99), (0.87, 0.87, 0.87),
                  (0.96, 0.60, 0.48), (0.71, 0.02, 0.15)),
    'GREY': ((0.05, 0.05, 0.05), (0.95, 0.95, 0.95)),
}
RAMP = RAMPS['VIRIDIS']

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
    """Point-domain attributes worth colouring by, most useful first: GIS,
    then other scalars, then colours and vectors."""
    d = point_data(obj)
    if d is None:
        return []
    names = [(a.name, a.data_type) for a in d.attributes
             if a.domain == 'POINT'
             and a.data_type in SCALAR_TYPES.keys() | COLOUR_TYPES | VECTOR_TYPES
             and not a.name.startswith(".") and a.name not in SKIP_ATTRS]
    return [n for n, _ in sorted(names, key=lambda x: (
        x[1] not in SCALAR_TYPES, not x[0].startswith("gis_"), x[0]))]


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


def ramp(t, name='VIRIDIS'):
    stops = np.asarray(RAMPS.get(name, RAMP), dtype=np.float32)
    t = np.clip(t, 0.0, 1.0) * (len(stops) - 1)
    i = np.minimum(t.astype(np.int64), len(stops) - 2)
    f = (t - i)[:, None]
    return stops[i] * (1.0 - f) + stops[i + 1] * f


def read_vectors(d, name):
    """(N, 3|4) floats for a colour or vector attribute, else None."""
    a = d.attributes.get(name)
    if a is None or a.domain != 'POINT':
        return None
    if a.data_type in COLOUR_TYPES:
        buf = np.empty(len(a.data) * 4, dtype=np.float32)
        if len(buf):
            a.data.foreach_get("color", buf)
        return buf.reshape(-1, 4)
    if a.data_type in VECTOR_TYPES:
        buf = np.empty(len(a.data) * 3, dtype=np.float32)
        if len(buf):
            a.data.foreach_get("vector", buf)
        return buf.reshape(-1, 3)
    return None


def write_hidden(d, hide):
    a = d.attributes.get(HIDE_ATTR)
    if hide is None:
        if a is not None:
            d.attributes.remove(a)
        return
    if a is not None and (a.data_type != 'BOOLEAN' or a.domain != 'POINT'):
        d.attributes.remove(a)
        a = None
    if a is None:
        a = d.attributes.new(HIDE_ATTR, 'BOOLEAN', 'POINT')
    a.data.foreach_set("value", np.ascontiguousarray(hide, dtype=bool))


def recolor(obj):
    """Rebuild mx_view_color from the object's current view settings."""
    v = obj.mixtli_view
    d = point_data(obj)
    if d is None:
        return False
    if v.kind in ('COLOUR', 'VECTOR'):
        vec = read_vectors(d, v.attribute)
        if vec is None:
            return False
        out = np.ones((len(vec), 4), dtype=np.float32)
        if v.kind == 'COLOUR':
            out[:, :3] = vec[:, :3]
        else:
            ln = np.linalg.norm(vec, axis=1, keepdims=True)
            unit = vec / np.where(ln > 0, ln, 1.0)
            out[:, :3] = lin(np.clip(unit * 0.5 + 0.5, 0.0, 1.0))
        write_colour(d, out)
        write_hidden(d, None)
        return True
    vals = read_values(d, v.attribute)
    if vals is None:
        return False
    n = len(vals)
    out = np.ones((n, 4), dtype=np.float32)
    off = np.zeros(n, dtype=bool)
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
            shown = np.array([it.show for it in v.items], dtype=bool)[order][pos]
            off = ~(hit & shown)
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
        out[:, :3] = lin(ramp(t, v.ramp))
        off = (t < 0.0) | (t > 1.0)
        if v.skip_zero:
            out[f == 0.0, :3] = lin(NONE_COLOUR)
            off |= f == 0.0
    write_colour(d, out)
    write_hidden(d, off if v.hide_off else None)
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
    gi = ng.nodes.new("NodeGroupInput");   gi.location = (-520, 0)
    hide = ng.nodes.new("GeometryNodeInputNamedAttribute"); hide.location = (-520, -160)
    hide.data_type = 'BOOLEAN'
    hide.inputs["Name"].default_value = HIDE_ATTR
    dele = ng.nodes.new("GeometryNodeDeleteGeometry"); dele.location = (-300, 0)
    dele.domain = 'POINT'
    setm = ng.nodes.new("GeometryNodeSetMaterial"); setm.location = (-60, 0)
    setm.inputs["Material"].default_value = mat
    go = ng.nodes.new("NodeGroupOutput");  go.location = (180, 0)
    ng.links.new(gi.outputs["Geometry"], dele.inputs["Geometry"])
    ng.links.new(hide.outputs["Attribute"], dele.inputs["Selection"])
    ng.links.new(dele.outputs["Geometry"], setm.inputs["Geometry"])
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
    obj.mixtli_view.error = ""
    d = point_data(obj)
    a = d.attributes.get(name) if d is not None else None
    if a is not None and a.domain == 'POINT' and a.data_type in COLOUR_TYPES | VECTOR_TYPES:
        v = obj.mixtli_view
        _suspend = True
        try:
            v.attribute = name
            v.kind = 'COLOUR' if a.data_type in COLOUR_TYPES else 'VECTOR'
            v.items.clear()
            v.distinct = 0
        finally:
            _suspend = False
        recolor(obj)
        show_modifier(obj)
        return None
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
            v.ramp = 'TERRAIN' if name in ("elevation", "height_above_ground") \
                or "height" in name else v.ramp
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


def _on_pick(self, context):
    """Picking from the panel's attribute field colours by it straight away."""
    if _suspend:
        return
    obj = self.id_data
    if not isinstance(obj, bpy.types.Object):
        return
    if not self.attribute:
        hide_modifier(obj)
        return
    self.error = show(obj, self.attribute) or ""


class MIXTLI_PG_view(bpy.types.PropertyGroup):
    attribute: StringProperty(name="Colour By", update=_on_pick,
                              description="Point attribute to colour the cloud by")
    error: StringProperty()
    kind: EnumProperty(items=[('CATEGORY', "Category", ""), ('NUMBER', "Number", ""),
                              ('COLOUR', "Colour", ""), ('VECTOR', "Vector", "")])
    ramp: EnumProperty(
        name="Ramp", update=_on_change, default='VIRIDIS',
        items=[('VIRIDIS', "Viridis", "Perceptually even, colour-blind safe"),
               ('TERRAIN', "Terrain", "Water, vegetation, ground, rock, snow - for heights"),
               ('HEAT', "Heat", "Dark to bright - intensity, density"),
               ('DIVERGING', "Diverging", "Blue - neutral - red, for values about a midpoint"),
               ('GREY', "Grey", "Black to white")])
    hide_off: BoolProperty(
        name="Hide Off / Out of Range", default=False, update=_on_change,
        description="Remove switched-off values and numbers outside Low-High "
                    "instead of dimming them")
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
        if self.properties.is_property_set("attribute"):     # a quick-pick button
            return self.execute(context)
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


class MIXTLI_OT_view_height(bpy.types.Operator):
    """Write each point's height as an attribute and colour by it"""
    bl_idname = "object.mixtli_view_height"
    bl_label = "Colour by Height"
    bl_options = {'REGISTER', 'UNDO'}

    mode: EnumProperty(
        name="Height",
        items=[('ELEVATION', "Elevation", "World Z of each point, as 'elevation'"),
               ('GROUND', "Above Ground", "Approximate height over the local ground, as "
                                          "'height_above_ground' - building and canopy "
                                          "heights")])

    @classmethod
    def poll(cls, context):
        return point_data(context.active_object) is not None

    def execute(self, context):
        from .gis import world_positions
        from .pointops import height_above_ground
        obj = context.active_object
        P = world_positions(obj)
        if self.mode == 'ELEVATION':
            name, vals = "elevation", P[:, 2].astype(np.float32)
        else:
            name, vals = "height_above_ground", height_above_ground(P)
        d = obj.data
        a = d.attributes.get(name)
        if a is not None and (a.data_type != 'FLOAT' or a.domain != 'POINT'):
            d.attributes.remove(a)
            a = None
        if a is None:
            a = d.attributes.new(name, 'FLOAT', 'POINT')
        a.data.foreach_set("value", np.ascontiguousarray(vals, dtype=np.float32))
        d.update_tag()
        err = show(obj, name)
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
            obj.mixtli_view.attribute = ""
        finally:
            _suspend = False
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
    bl_order = 4

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        d = point_data(obj)
        if d is None:
            layout.label(text="Select a point cloud", icon='INFO')
            return
        v = obj.mixtli_view
        active = bool(v.attribute) and obj.modifiers.get(VIEW_MOD) is not None
        row = layout.row(align=True)
        row.prop_search(v, "attribute", d, "attributes", text="", icon='VIEWZOOM')
        row.operator("object.mixtli_view_source", text="", icon='LOOP_BACK')
        row = layout.row(align=True)
        row.operator("object.mixtli_view_height", text="Elevation",
                     icon='SORT_ASC').mode = 'ELEVATION'
        row.operator("object.mixtli_view_height", text="Above Ground",
                     icon='MOD_LENGTH').mode = 'GROUND'
        if v.error:
            layout.label(text=v.error, icon='ERROR')
        if not active:
            names = viewable(obj)[:6]
            if names:
                col = layout.column(align=True)
                for n in names:
                    col.operator("object.mixtli_view_attribute", text=n,
                                 icon='COLOR').attribute = n
            return
        if v.kind in ('COLOUR', 'VECTOR'):
            layout.label(text="Showing %s %s" % ("colour" if v.kind == 'COLOUR'
                                                  else "direction as RGB", v.attribute))
            return
        if v.kind == 'NUMBER':
            layout.prop(v, "ramp")
            col = layout.column(align=True)
            col.prop(v, "lo")
            col.prop(v, "hi")
            layout.prop(v, "skip_zero")
            layout.prop(v, "hide_off")
            return
        if not v.items:
            layout.label(text="%d distinct values - colours hashed" % v.distinct)
            return
        row = layout.row(align=True)
        row.operator("object.mixtli_view_toggle_all", text="All").state = True
        row.operator("object.mixtli_view_toggle_all", text="None").state = False
        row.prop(v, "dim", text="Dim")
        layout.prop(v, "hide_off")
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
           MIXTLI_OT_view_height, MIXTLI_OT_view_source, MIXTLI_OT_view_toggle_all, MIXTLI_PT_view)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Object.mixtli_view = bpy.props.PointerProperty(type=MIXTLI_PG_view)


def unregister():
    if hasattr(bpy.types.Object, "mixtli_view"):
        del bpy.types.Object.mixtli_view
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
