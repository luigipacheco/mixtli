# SPDX-License-Identifier: GPL-3.0-or-later
"""
GIS data on a point cloud: georeference, fetch, map, check.

  Georeference  Blosm leaves scene["lat"], scene["lon"] behind on every
                import; that is the anchor. Or type one in, and write it
                back so a later Blosm import lands in the same place.
  Get Data      OpenStreetMap, a GeoJSON file, or an OGC API Features
                server, all kept as GeoJSON in a text block.
  Map           polygon tags and line distances onto point attributes.
  Check         slide the building footprints over the cloud's roofs and
                report the shift that fits best - in both of Blosm's
                frames, because using the wrong one is the usual mistake.
"""

import json
import math
import os
import time
import urllib.error

import bpy
import numpy as np
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       FloatVectorProperty, IntProperty, PointerProperty,
                       StringProperty)

from . import geo, gis_map, gis_sources, view

TEXT_NAME = "Mixtli GIS.geojson"
REPORT_NAME = "Mixtli GIS Report"
OVERLAY_NAME = "Mixtli GIS Features"
BIG_AREA_KM2 = 9.0


# ---------------------------------------------------------------- georeference

def georef(context):
    """(lat, lon, where it came from) or None."""
    s = context.scene.mixtli_gis
    scene = context.scene
    if s.georef == 'SCENE':
        if "lat" in scene and "lon" in scene:
            return float(scene["lat"]), float(scene["lon"]), "Blosm scene lat/lon"
        return None
    return s.lat, s.lon, "manual"


def detect_frame(obj):
    names = [obj.name] + [c.name for c in obj.users_collection]
    return 'TILES' if any("3d tiles" in n.lower() for n in names) else 'TM'


def frame_kind(obj, s):
    return s.frame if s.frame != 'AUTO' else detect_frame(obj)


FRAME_LABEL = {'TM': "Transverse Mercator (Blosm OSM / terrain)",
               'TILES': "ECEF tangent frame (Blosm 3D Tiles)"}


# ---------------------------------------------------------------- points

def has_points(obj):
    return obj is not None and obj.type in {'POINTCLOUD', 'MESH'} and \
        (len(obj.data.points) if obj.type == 'POINTCLOUD' else len(obj.data.vertices)) > 0


def world_positions(obj):
    d = obj.data
    if obj.type == 'POINTCLOUD':
        n = len(d.points)
        P = np.empty(n * 3, dtype=np.float32)
        d.attributes["position"].data.foreach_get("vector", P)
    else:
        n = len(d.vertices)
        P = np.empty(n * 3, dtype=np.float32)
        d.vertices.foreach_get("co", P)
    M = np.array(obj.matrix_world, dtype=np.float64)
    return P.reshape(n, 3).astype(np.float64) @ M[:3, :3].T + M[:3, 3]


def ground_height(z):
    """For the 3D Tiles frame, features must be projected at the ground's
    ellipsoidal height or they shift by h x 0.2 degrees (Blosm's tilt) -
    4-5 m on a city 2 km up. The low end of the cloud is the ground."""
    return float(np.percentile(z, 5)) if len(z) else 0.0


def write_attribute(d, name, kind, values):
    a = d.attributes.get(name)
    if a is not None and (a.data_type != kind or a.domain != 'POINT'):
        d.attributes.remove(a)
        a = None
    if a is None:
        a = d.attributes.new(name, kind, 'POINT')
    a.data.foreach_set("value", np.ascontiguousarray(values).ravel())


def bbox_latlon(objs, frame, pad):
    """(south, west, north, east) around the objects, padded in metres."""
    corners = []
    for o in objs:
        M = np.array(o.matrix_world, dtype=np.float64)
        bb = np.array([list(c) for c in o.bound_box], dtype=np.float64)
        corners.append(bb @ M[:3, :3].T + M[:3, 3])
    c = np.concatenate(corners)
    lo, hi = c[:, :2].min(0) - pad, c[:, :2].max(0) + pad
    z = float(c[:, 2].min())
    box = np.array([[lo[0], lo[1], z], [hi[0], lo[1], z], [hi[0], hi[1], z], [lo[0], hi[1], z]])
    lat, lon, _ = frame.to_geo(box)
    area = float((hi[0] - lo[0]) * (hi[1] - lo[1])) / 1e6
    return (float(lat.min()), float(lon.min()), float(lat.max()), float(lon.max())), area


# ---------------------------------------------------------------- loaded data

def loaded_geojson():
    t = bpy.data.texts.get(TEXT_NAME)
    if t is None:
        return None
    try:
        return json.loads(t.as_string())
    except ValueError:
        return None


def store_geojson(gj):
    t = bpy.data.texts.get(TEXT_NAME) or bpy.data.texts.new(TEXT_NAME)
    t.clear()
    t.write(json.dumps(gj))
    return t


def features_for(obj, context, gj, offset=None):
    """Parse, project into this object's frame, return (features, frame, info)."""
    s = context.scene.mixtli_gis
    ref = georef(context)
    if ref is None:
        raise RuntimeError("No georeference: import with Blosm first, or set it manually")
    kind = frame_kind(obj, s)
    frame = geo.make_frame(kind, ref[0], ref[1])
    raw, skipped = gis_sources.parse_geojson(gj)
    xyz = world_positions(obj)
    h = ground_height(xyz[:, 2]) if kind == 'TILES' else 0.0
    off = tuple(s.offset) if offset is None else offset
    feats = gis_map.project_features(raw, frame, h, off)
    return feats, raw, xyz, {"frame": kind, "lat": ref[0], "lon": ref[1],
                             "georef": ref[2], "height": h, "offset": list(off),
                             "skipped": skipped}


def write_report(lines):
    t = bpy.data.texts.get(REPORT_NAME) or bpy.data.texts.new(REPORT_NAME)
    t.clear()
    t.write("\n".join(lines) + "\n")
    print("[mixtli] " + "\n[mixtli] ".join(lines))


# ---------------------------------------------------------------- settings

class MIXTLI_PG_gis(bpy.types.PropertyGroup):
    georef: EnumProperty(
        name="Georeference",
        items=[('SCENE', "Blosm", "Use scene['lat'] / scene['lon'], which Blosm "
                                  "writes on its first import"),
               ('MANUAL', "Manual", "Type the latitude and longitude of the "
                                    "scene origin")],
        default='SCENE')
    lat: FloatProperty(name="Latitude", default=0.0, min=-90.0, max=90.0, precision=7)
    lon: FloatProperty(name="Longitude", default=0.0, min=-180.0, max=180.0, precision=7)
    frame: EnumProperty(
        name="Frame",
        items=[('AUTO', "Auto", "3D Tiles frame when the cloud sits in a Blosm "
                                "'3D Tiles' collection, otherwise Transverse Mercator"),
               ('TM', "OSM / Terrain", "Blosm's Transverse Mercator, used for "
                                       "OpenStreetMap, terrain and GPX imports"),
               ('TILES', "3D Tiles", "Blosm's earth-centred frame, used for Google "
                                     "and other 3D Tiles imports")],
        default='AUTO')
    source: EnumProperty(
        name="Source",
        items=[('OSM', "OpenStreetMap", "Download from the Overpass API for the "
                                        "area under the selected clouds"),
               ('FILE', "GeoJSON File", "A WGS84 GeoJSON file, e.g. from QGIS or "
                                        "an open-data portal"),
               ('OGC', "OGC API Features", "An OGC API - Features collection "
                                           "'items' URL")],
        default='OSM')
    filepath: StringProperty(name="File", subtype='FILE_PATH')
    ogc_url: StringProperty(
        name="Items URL",
        description="e.g. https://server/collections/buildings/items")
    overpass_url: StringProperty(name="Overpass", default=gis_sources.OVERPASS_URL)
    fields: StringProperty(
        name="Fields", default=gis_sources.DEFAULT_OSM_KEYS,
        description="Comma-separated tags / properties to map. Blank maps every "
                    "property (up to 32). For OpenStreetMap these are also what "
                    "gets downloaded")
    save_path: StringProperty(
        name="Save Copy", subtype='FILE_PATH',
        description="Optional: also write downloads to this .geojson file")
    padding: FloatProperty(name="Padding", default=20.0, min=0.0, subtype='DISTANCE',
                           description="Download this far beyond the clouds' bounds")
    line_max: FloatProperty(name="Line Distance", default=15.0, min=0.01,
                            subtype='DISTANCE',
                            description="Distances to roads and other lines are "
                                        "measured up to this; beyond it, the max")
    offset: FloatVectorProperty(name="Offset", size=2, subtype='TRANSLATION',
                                unit='LENGTH',
                                description="Shift applied to features before mapping")
    search: FloatProperty(name="Search", default=10.0, min=1.0, subtype='DISTANCE',
                          description="Largest shift the alignment check tries")
    cell: FloatProperty(name="Cell", default=0.5, min=0.05, subtype='DISTANCE',
                        description="Raster resolution for the alignment check")
    suggested: FloatVectorProperty(size=2, options={'HIDDEN'})
    suggested_frame: StringProperty(options={'HIDDEN'})
    has_suggestion: BoolProperty(options={'HIDDEN'})
    loaded: StringProperty(options={'HIDDEN'})
    show_options: BoolProperty(name="Options", default=False)


# ---------------------------------------------------------------- operators

def _point_objects(context):
    return [o for o in context.selected_objects if has_points(o)]


class MIXTLI_OT_gis_fetch(bpy.types.Operator):
    """Load GIS features for the area under the selected point clouds"""
    bl_idname = "object.mixtli_gis_fetch"
    bl_label = "Get GIS Data"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.mixtli_gis
        t0 = time.time()
        try:
            if s.source == 'FILE':
                path = bpy.path.abspath(s.filepath)
                with open(path, "r", encoding="utf-8") as f:
                    gj = json.load(f)
                label = os.path.basename(path)
            else:
                objs = _point_objects(context)
                if not objs:
                    self.report({'ERROR'}, "Select the point cloud(s) to fetch data for")
                    return {'CANCELLED'}
                if not getattr(bpy.app, "online_access", True):
                    self.report({'ERROR'}, "Online access is off: Preferences > System "
                                           "> Network > Allow Online Access")
                    return {'CANCELLED'}
                ref = georef(context)
                if ref is None:
                    self.report({'ERROR'}, "No georeference: import with Blosm first, "
                                           "or set Georeference to Manual")
                    return {'CANCELLED'}
                frame = geo.make_frame(frame_kind(objs[0], s), ref[0], ref[1])
                bbox, km2 = bbox_latlon(objs, frame, s.padding)
                if km2 > BIG_AREA_KM2:
                    self.report({'WARNING'}, "%.1f km2 is a big request; Overpass may "
                                             "time out" % km2)
                if s.source == 'OSM':
                    gj = gis_sources.fetch_osm(bbox, gis_sources.split_keys(s.fields),
                                               s.overpass_url or gis_sources.OVERPASS_URL)
                    label = "OpenStreetMap"
                else:
                    if not s.ogc_url:
                        self.report({'ERROR'}, "Set the OGC API items URL")
                        return {'CANCELLED'}
                    gj = gis_sources.fetch_ogc(s.ogc_url, bbox)
                    label = "OGC API"
                if s.save_path:
                    with open(bpy.path.abspath(s.save_path), "w", encoding="utf-8") as f:
                        json.dump(gj, f)
            gis_sources.parse_geojson(gj)           # fail now on a bad file, not later
        except (OSError, ValueError, urllib.error.URLError) as e:
            self.report({'ERROR'}, "Could not load GIS data: %s" % e)
            return {'CANCELLED'}
        store_geojson(gj)
        n = len(gj.get("features") or ())
        s.loaded = "%s - %d features" % (label, n)
        s.has_suggestion = False
        self.report({'INFO'}, "%d features from %s in %.1fs" % (n, label, time.time() - t0))
        return {'FINISHED'}


class MIXTLI_OT_gis_map(bpy.types.Operator):
    """Write the loaded GIS features onto the selected clouds as point attributes"""
    bl_idname = "object.mixtli_gis_map"
    bl_label = "Map to Points"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bpy.data.texts.get(TEXT_NAME) is not None and bool(_point_objects(context))

    def execute(self, context):
        s = context.scene.mixtli_gis
        gj = loaded_geojson()
        if gj is None:
            self.report({'ERROR'}, "No GIS data loaded - Get GIS Data first")
            return {'CANCELLED'}
        lines, done = [], 0
        for obj in _point_objects(context):
            t0 = time.time()
            try:
                feats, raw, xyz, info = features_for(obj, context, gj)
            except (RuntimeError, ValueError) as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}
            keys = gis_sources.split_keys(s.fields)
            fields = gis_map.pick_fields(raw, keys)
            if keys and not fields:
                lines.append("  none of the listed fields exist; mapping all properties")
                fields = gis_map.pick_fields(raw)
            lines.append("%s - %s, %s" % (obj.name, FRAME_LABEL[info["frame"]], info["georef"]))
            rep = []
            attrs, legs = gis_map.map_features(xyz, feats, fields, s.line_max, rep)
            lines += rep
            d = obj.data
            for name in [a.name for a in d.attributes if a.name.startswith("gis_")]:
                if name not in attrs:
                    d.attributes.remove(d.attributes[name])
            for name, (kind, values) in attrs.items():
                write_attribute(d, name, kind, values)
            d.update_tag()
            obj["mixtli_gis"] = json.dumps({
                "legends": legs,
                "meta": dict(info, source=s.loaded, fields=fields,
                             mapped=time.strftime("%Y-%m-%d %H:%M"))})
            lines.append("  wrote %s in %.2fs" % (", ".join(attrs) or "nothing",
                                                  time.time() - t0))
            first = next((n for n, m in legs.items() if m.get("kind") == "category"), None)
            if first and not obj.mixtli_view.attribute:
                view.show(obj, first)
            elif obj.mixtli_view.attribute in attrs:
                view.show(obj, obj.mixtli_view.attribute)
            done += 1
        write_report(lines)
        self.report({'INFO'}, "Mapped GIS attributes onto %d object(s) - see '%s'"
                    % (done, REPORT_NAME))
        return {'FINISHED'}


class MIXTLI_OT_gis_validate(bpy.types.Operator):
    """Measure how well building footprints sit on the cloud's roofs, in both
    of Blosm's frames, and suggest an offset"""
    bl_idname = "object.mixtli_gis_validate"
    bl_label = "Check Alignment"

    @classmethod
    def poll(cls, context):
        return bpy.data.texts.get(TEXT_NAME) is not None and has_points(context.active_object)

    def execute(self, context):
        s = context.scene.mixtli_gis
        obj = context.active_object
        gj = loaded_geojson()
        ref = georef(context)
        if gj is None or ref is None:
            self.report({'ERROR'}, "Needs loaded GIS data and a georeference")
            return {'CANCELLED'}
        raw, _ = gis_sources.parse_geojson(gj)
        polys = [f for f in raw if f[0] == 'polygon']
        blds = [f for f in polys if "building" in f[2]] or polys
        xyz = world_positions(obj)
        lat, lon, _ = geo.make_frame(frame_kind(obj, s), ref[0], ref[1]).to_geo(xyz.mean(0))
        lines = ["Alignment check - %s" % obj.name,
                 "  georeference: %.7f, %.7f (%s)" % (ref[0], ref[1], ref[2]),
                 "  cloud centre: %.7f, %.7f" % (float(lat), float(lon)),
                 "  look: https://www.openstreetmap.org/#map=19/%.6f/%.6f"
                 % (float(lat), float(lon)),
                 "  %d points, %d footprints (%s)"
                 % (len(xyz), len(blds), "buildings" if blds is not polys else "all polygons"),
                 "  current offset: %.2f, %.2f m" % tuple(s.offset)]
        if not blds:
            lines.append("  no polygons to test against - draw the features and "
                         "check by eye instead")
            write_report(lines)
            self.report({'WARNING'}, "Nothing to measure alignment against")
            return {'CANCELLED'}

        current = frame_kind(obj, s)
        results = {}
        for kind in ('TM', 'TILES'):
            frame = geo.make_frame(kind, ref[0], ref[1])
            h = ground_height(xyz[:, 2]) if kind == 'TILES' else 0.0
            fp = [rings for _, rings, _ in gis_map.project_features(blds, frame, h, tuple(s.offset))]
            r = geo.estimate_offset(xyz[:, :2], xyz[:, 2], fp, s.cell, s.search)
            results[kind] = r
            if r is None:
                lines.append("  %s: no overlap between footprints and cloud" % kind)
                continue
            lines.append("  %s%s: best shift %+.2f, %+.2f m, score %.2f (%.2f unshifted)%s"
                         % (FRAME_LABEL[kind], " [in use]" if kind == current else "",
                            r["dx"], r["dy"], r["score"], r["score_at_zero"],
                            " - at the search limit" if r["at_search_edge"] else ""))
        ok = {k: r for k, r in results.items() if r is not None}
        if not ok:
            write_report(lines)
            self.report({'WARNING'}, "Footprints do not overlap the cloud - check the "
                                     "georeference")
            return {'CANCELLED'}
        # A frame that fits as it stands beats one that needs shifting: with a
        # shift both can score well, but only the right frame is right
        # everywhere - the wrong one's error grows away from the centre.
        def needs_shift(k):
            return math.hypot(ok[k]["dx"], ok[k]["dy"]) > ok[k]["cell"] * 1.5
        best_kind = max(ok, key=lambda k: (not needs_shift(k) and not ok[k]["at_search_edge"],
                                           ok[k]["score"]))
        r = ok[best_kind]
        shift = math.hypot(r["dx"], r["dy"])
        lines.append("")
        if r["at_search_edge"] or r["score"] < 0.3:
            verdict = ("No convincing fit within %.0f m. Check the georeference, raise "
                       "Search, or draw the features to look." % r["search"])
        elif shift <= r["cell"] * 1.5:
            verdict = "Aligned to within %.2f m in the %s frame." % (r["cell"] * 1.5, best_kind)
        else:
            verdict = ("Features fit best shifted by %+.2f, %+.2f m (%s frame)."
                       % (r["dx"], r["dy"], best_kind))
        if best_kind != current:
            verdict += " That is not the frame in use - set Frame to %s." % (
                "3D Tiles" if best_kind == 'TILES' else "OSM / Terrain")
        lines.append(verdict)
        lines.append("Scores are the mean normalised height under the footprints: "
                     "roofs score high, ground near 0.")
        s.suggested = (s.offset[0] + r["dx"], s.offset[1] + r["dy"])
        s.suggested_frame = best_kind
        s.has_suggestion = shift > r["cell"] * 1.5 or best_kind != current
        write_report(lines)
        self.report({'INFO'}, verdict)
        return {'FINISHED'}


class MIXTLI_OT_gis_apply_suggestion(bpy.types.Operator):
    """Use the offset and frame the alignment check found"""
    bl_idname = "object.mixtli_gis_apply_suggestion"
    bl_label = "Apply Suggested Fix"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.mixtli_gis
        s.offset = s.suggested
        if s.suggested_frame and s.suggested_frame != frame_kind(context.active_object, s):
            s.frame = s.suggested_frame
        s.has_suggestion = False
        self.report({'INFO'}, "Offset %.2f, %.2f m - run Map to Points again" % tuple(s.offset))
        return {'FINISHED'}


class MIXTLI_OT_gis_overlay(bpy.types.Operator):
    """Draw the loaded features as wire edges over the active cloud, to check by eye"""
    bl_idname = "object.mixtli_gis_overlay"
    bl_label = "Draw Features"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return bpy.data.texts.get(TEXT_NAME) is not None and has_points(context.active_object)

    def execute(self, context):
        obj = context.active_object
        try:
            feats, _, xyz, info = features_for(obj, context, loaded_geojson())
        except (RuntimeError, ValueError, TypeError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        z = float(xyz[:, 2].max()) + 0.5 if len(xyz) else 0.0
        verts, edges = [], []
        for kind, parts, _ in feats:
            for p in parts:
                closed = kind == 'polygon'
                if closed and len(p) > 1 and np.allclose(p[0], p[-1]):
                    p = p[:-1]
                base = len(verts)
                verts.extend((float(x), float(y), z) for x, y in p)
                m = len(p)
                edges.extend((base + i, base + i + 1) for i in range(m - 1))
                if closed and m > 2:
                    edges.append((base + m - 1, base))
        old = bpy.data.objects.get(OVERLAY_NAME)
        if old is not None:
            me_old = old.data
            bpy.data.objects.remove(old, do_unlink=True)
            if me_old.users == 0:
                bpy.data.meshes.remove(me_old)
        me = bpy.data.meshes.new(OVERLAY_NAME)
        me.from_pydata(verts, edges, [])
        ov = bpy.data.objects.new(OVERLAY_NAME, me)
        ov.show_in_front = True
        ov.display_type = 'WIRE'
        for c in obj.users_collection:
            c.objects.link(ov)
        self.report({'INFO'}, "%d features drawn at z %.1f (%s frame) - view from top"
                    % (len(feats), z, info["frame"]))
        return {'FINISHED'}


class MIXTLI_OT_gis_write_georef(bpy.types.Operator):
    """Write the manual latitude/longitude to scene['lat'] / scene['lon'], so
    Blosm imports made afterwards line up with this scene"""
    bl_idname = "scene.mixtli_gis_write_georef"
    bl_label = "Write for Blosm"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.mixtli_gis
        context.scene["lat"] = s.lat
        context.scene["lon"] = s.lon
        s.georef = 'SCENE'
        return {'FINISHED'}


# ---------------------------------------------------------------- panel

class MIXTLI_PT_gis(bpy.types.Panel):
    bl_label = "GIS Data"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Mixtli"
    bl_parent_id = "MIXTLI_PT_panel"
    bl_order = 1
    bl_options = {'DEFAULT_CLOSED'}

    def draw(self, context):
        layout = self.layout
        s = context.scene.mixtli_gis
        obj = context.active_object

        col = layout.column(align=True)
        col.row(align=True).prop(s, "georef", expand=True)
        ref = georef(context)
        if s.georef == 'MANUAL':
            col.prop(s, "lat")
            col.prop(s, "lon")
            col.operator("scene.mixtli_gis_write_georef", icon='EXPORT')
        elif ref is None:
            col.label(text="No Blosm import in this scene", icon='ERROR')
        else:
            col.label(text="%.6f, %.6f" % ref[:2], icon='WORLD')
        row = layout.row(align=True)
        row.prop(s, "frame")
        if s.frame == 'AUTO' and has_points(obj):
            layout.label(text="-> " + ("3D Tiles" if detect_frame(obj) == 'TILES'
                                       else "OSM / Terrain"))

        box = layout.box()
        box.prop(s, "source")
        if s.source == 'FILE':
            box.prop(s, "filepath")
        elif s.source == 'OGC':
            box.prop(s, "ogc_url")
        box.prop(s, "fields")
        box.operator("object.mixtli_gis_fetch", icon='IMPORT')
        if s.loaded:
            box.label(text=s.loaded, icon='CHECKMARK')

        col = layout.column()
        col.scale_y = 1.3
        col.operator("object.mixtli_gis_map", icon='GROUP_VERTEX')
        row = layout.row(align=True)
        row.operator("object.mixtli_gis_validate", icon='CON_SAMEVOL')
        row.operator("object.mixtli_gis_overlay", icon='MOD_WIREFRAME')
        if s.has_suggestion:
            layout.operator("object.mixtli_gis_apply_suggestion", icon='CHECKMARK')

        layout.prop(s, "show_options", icon='TRIA_DOWN' if s.show_options else 'TRIA_RIGHT',
                    emboss=False)
        if s.show_options:
            col = layout.column(align=True)
            col.use_property_split = True
            col.prop(s, "offset")
            col.prop(s, "line_max")
            col.prop(s, "padding")
            col.prop(s, "search")
            col.prop(s, "cell")
            if s.source == 'OSM':
                col.prop(s, "overpass_url")
            if s.source != 'FILE':
                col.prop(s, "save_path")
        if s.source == 'OSM':
            layout.label(text="Data (c) OpenStreetMap contributors, ODbL")


CLASSES = (MIXTLI_PG_gis, MIXTLI_OT_gis_fetch, MIXTLI_OT_gis_map, MIXTLI_OT_gis_validate,
           MIXTLI_OT_gis_apply_suggestion, MIXTLI_OT_gis_overlay,
           MIXTLI_OT_gis_write_georef, MIXTLI_PT_gis)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.mixtli_gis = PointerProperty(type=MIXTLI_PG_gis)


def unregister():
    if hasattr(bpy.types.Scene, "mixtli_gis"):
        del bpy.types.Scene.mixtli_gis
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
