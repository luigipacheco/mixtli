# SPDX-License-Identifier: GPL-3.0-or-later
"""
A site in a few clicks: place -> Google 3D Tiles -> point cloud -> GIS data
-> coloured by attribute.

  Locate        a place name (OpenStreetMap Nominatim) or "lat, lon" pasted
                from Google Maps.
  Import Tiles  Google Photorealistic 3D Tiles for a circle around it,
                straight from the Map Tiles API - no other add-on needed.
                Lands in Blosm's 3D Tiles frame and writes scene["lat"] /
                scene["lon"] the way Blosm does, so Blosm imports, GIS data
                and these tiles all agree.
  Build Site    the lot, in order, each step reusing the existing operators:
                tiles, Regular Grid point cloud, OpenStreetMap, Map to Points.
"""

import math
import os
import tempfile
import time
import urllib.error

import bpy
import numpy as np
from bpy.props import (BoolProperty, EnumProperty, FloatProperty, PointerProperty,
                       StringProperty)
from mathutils import Matrix

from . import geo, tiles

COLLECTION = "Google 3D Tiles"
SAME_ORIGIN_KM = 20.0

# max geometric error per detail level; Google's leaves sit below ~1 m
DETAIL = {'LOW': 24.0, 'MEDIUM': 12.0, 'HIGH': 6.0, 'MAX': 0.0}


def prefs(context):
    a = context.preferences.addons.get(__package__)
    return a.preferences if a else None


def api_key(context):
    p = prefs(context)
    return (p.google_api_key.strip() if p else "") or os.environ.get("GOOGLE_MAPS_API_KEY", "")


# ---------------------------------------------------------------- settings

class MIXTLI_AP_prefs(bpy.types.AddonPreferences):
    bl_idname = __package__

    google_api_key: StringProperty(
        name="Google Maps API Key", subtype='PASSWORD',
        description="A Google Cloud key with the Map Tiles API enabled. "
                    "Stored in your Blender preferences, never in the .blend")

    def draw(self, context):
        col = self.layout.column()
        col.prop(self, "google_api_key")
        col.label(text="console.cloud.google.com > APIs > enable 'Map Tiles API' > "
                       "Credentials > Create API key", icon='INFO')


class MIXTLI_PG_site(bpy.types.PropertyGroup):
    query: StringProperty(
        name="Place", description="A place name or address, or 'lat, lon' as Google "
                                  "Maps copies it (right-click a spot on the map)")
    lat: FloatProperty(name="Latitude", min=-90.0, max=90.0, precision=7)
    lon: FloatProperty(name="Longitude", min=-180.0, max=180.0, precision=7)
    located: StringProperty(options={'HIDDEN'})
    radius: FloatProperty(name="Radius", default=150.0, min=10.0, soft_max=1500.0,
                          subtype='DISTANCE', description="Import tiles within this "
                                                          "distance of the place")
    detail: EnumProperty(
        name="Detail",
        items=[('LOW', "Low", "Blocks and massing - fast, big areas"),
               ('MEDIUM', "Medium", "Buildings read clearly"),
               ('HIGH', "High", "Facades and trees"),
               ('MAX', "Max", "The finest tiles there are - small areas only")],
        default='HIGH')
    spacing: FloatProperty(
        name="Point Spacing", default=0.5, min=0.02, soft_max=5.0, subtype='DISTANCE',
        description="Voxel size of the point cloud made from the tiles")
    make_cloud: BoolProperty(name="Point Cloud", default=True,
                             description="Turn the tiles into a gridded point cloud")
    get_gis: BoolProperty(name="GIS Data", default=True,
                          description="Download OpenStreetMap and map it onto the points")
    hide_mesh: BoolProperty(name="Hide Tiles Mesh", default=True,
                            description="Hide the textured mesh once the cloud is made")
    max_tiles: bpy.props.IntProperty(name="Max Tiles", default=600, min=1,
                                     description="Refuse a request bigger than this")
    show_options: BoolProperty(name="Options", default=False)
    status: StringProperty(options={'HIDDEN'})


# ---------------------------------------------------------------- helpers

def locate(s):
    """(lat, lon) for s.query, also shown in s.lat / s.lon. Returned in full
    precision: the properties are float32, a fifth of a metre out at 100
    degrees. Raises ValueError with a reason."""
    q = s.query.strip()
    if not q:
        if s.lat or s.lon:                      # typed under Options
            s.located = s.located or "%.6f, %.6f" % (s.lat, s.lon)
            return s.lat, s.lon
        raise ValueError("Type a place or 'lat, lon'")
    ll = tiles.parse_latlon(q)
    if ll:
        s.lat, s.lon = ll
        s.located = "%.6f, %.6f" % ll
        return ll
    if not getattr(bpy.app, "online_access", True):
        raise ValueError("Online access is off: Preferences > System > Network")
    hit = tiles.geocode(q)
    if not hit:
        raise ValueError("Nothing found for '%s'" % q)
    s.lat, s.lon = hit[0], hit[1]
    s.located = hit[2][:120]
    return hit[0], hit[1]


def scene_origin(context, lat, lon, report):
    """The georeference everything shares. Kept when the site is near it, so
    earlier imports still line up; moved to the site otherwise."""
    sc = context.scene
    if "lat" in sc and "lon" in sc:
        olat, olon = float(sc["lat"]), float(sc["lon"])
        d = np.linalg.norm(geo.ecef(lat, lon) - geo.ecef(olat, olon)) / 1000.0
        if d <= SAME_ORIGIN_KM:
            return olat, olon
        report.append("scene origin moved: %.1f km from the previous one - "
                      "older imports will not line up" % d)
    sc["lat"], sc["lon"] = lat, lon
    return lat, lon


def target_collection(context):
    col = bpy.data.collections.get(COLLECTION)
    if col is None:
        col = bpy.data.collections.new(COLLECTION)
    if col.name not in context.scene.collection.children:
        try:
            context.scene.collection.children.link(col)
        except RuntimeError:
            pass
    return col


def _layer_collection(lc, col):
    if lc.collection == col:
        return lc
    for c in lc.children:
        r = _layer_collection(c, col)
        if r:
            return r
    return None


def import_tile(context, path, roots, frame, tile_t):
    """Import one patched glb and place its roots. Returns new objects."""
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=path)
    new = [o for o in bpy.data.objects if o not in before]
    for o in new:
        if o.parent is None:
            m = roots.get(o.name.split(".")[0])
            if m is not None:
                o.matrix_world = Matrix(tiles.placement(frame, tile_t, m).tolist())
    return new


def merge(context, objs, name):
    """Flatten the import hierarchy and join every tile into one mesh."""
    context.view_layer.update()
    meshes = [o for o in objs if o.type == 'MESH']
    for o in meshes:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
    for o in objs:
        if o.type != 'MESH':
            bpy.data.objects.remove(o, do_unlink=True)
    if not meshes:
        return None
    if len(meshes) > 1:
        with context.temp_override(active_object=meshes[0], object=meshes[0],
                                   selected_objects=meshes,
                                   selected_editable_objects=meshes):
            bpy.ops.object.join()
    out = meshes[0]
    out.name = name
    out.data.name = name
    return out


# ---------------------------------------------------------------- operators

class MIXTLI_OT_site_locate(bpy.types.Operator):
    """Find the place: a name or address, or 'lat, lon'"""
    bl_idname = "scene.mixtli_site_locate"
    bl_label = "Find"

    def execute(self, context):
        s = context.scene.mixtli_site
        s.located = ""
        try:
            locate(s)
        except (ValueError, OSError, urllib.error.URLError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, "%s (%.6f, %.6f)" % (s.located, s.lat, s.lon))
        return {'FINISHED'}


class MIXTLI_OT_tiles_import(bpy.types.Operator):
    """Import Google Photorealistic 3D Tiles around the place, as one textured mesh"""
    bl_idname = "scene.mixtli_tiles_import"
    bl_label = "Import 3D Tiles"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        obj = run_import(self, context)
        return {'FINISHED'} if obj else {'CANCELLED'}


def run_import(op, context):
    s = context.scene.mixtli_site
    key = api_key(context)
    if not key:
        op.report({'ERROR'}, "Set your Google Maps API key: Preferences > Add-ons > "
                             "Mixtli (or env GOOGLE_MAPS_API_KEY)")
        return None
    if not getattr(bpy.app, "online_access", True):
        op.report({'ERROR'}, "Online access is off: Preferences > System > Network > "
                             "Allow Online Access")
        return None
    t0 = time.time()
    lines = []
    wm = context.window_manager
    try:
        lat, lon = locate(s)
        olat, olon = scene_origin(context, lat, lon, lines)
        frame = geo.TilesFrame(olat, olon)
        site = tiles.Site(lat, lon, s.radius)
        print("[mixtli] selecting tiles within %.0f m of %.6f, %.6f" % (s.radius, lat, lon))
        sel = tiles.select_tiles(tiles.root_url(key), site, DETAIL[s.detail], s.max_tiles)
        if not sel.tiles:
            op.report({'ERROR'}, "No tiles here - Google may not cover this place")
            return None
        print("[mixtli] downloading %d tiles" % len(sel.tiles))
        wm.progress_begin(0, len(sel.tiles))
        data = tiles.download([u for u, _, _ in sel.tiles],
                              progress=lambda i, n: wm.progress_update(i // 2))
    except (ValueError, RuntimeError, OSError, urllib.error.URLError, KeyError) as e:
        wm.progress_end()
        msg = str(e)
        if isinstance(e, urllib.error.HTTPError) and e.code in (400, 403):
            msg = "Google refused the key (%d) - is the Map Tiles API enabled for it?" % e.code
        op.report({'ERROR'}, "3D Tiles: %s" % msg)
        return None

    col = target_collection(context)
    lc = _layer_collection(context.view_layer.layer_collection, col)
    prev = context.view_layer.active_layer_collection
    if lc:
        context.view_layer.active_layer_collection = lc
    credits, objs = {}, []
    tmp = tempfile.mkdtemp(prefix="mixtli_tiles_")
    try:
        for i, ((url, t, _), blob) in enumerate(zip(sel.tiles, data)):
            try:
                patched, lifted, cr = tiles.lift_roots(blob)
            except (ValueError, KeyError) as e:
                lines.append("  tile %d skipped: %s" % (i, e))
                continue
            js, binary = tiles.read_glb(patched)
            roots = {}
            for n, m in lifted.items():
                tag = "mxt%d_%d" % (i, n)
                js["nodes"][n]["name"] = tag
                roots[tag] = m
            path = os.path.join(tmp, "tile_%04d.glb" % i)
            with open(path, "wb") as f:
                f.write(tiles.write_glb(js, binary))
            for c in cr.split(";"):
                c = c.strip()
                if c:
                    credits[c] = credits.get(c, 0) + 1
            objs += import_tile(context, path, roots, frame, t)
            wm.progress_update(len(sel.tiles) // 2 + i // 2)
    finally:
        wm.progress_end()
        if prev:
            context.view_layer.active_layer_collection = prev
        for f in os.listdir(tmp):
            os.remove(os.path.join(tmp, f))
        os.rmdir(tmp)

    name = "Tiles %s" % (s.query.strip() or s.located)[:40]
    obj = merge(context, objs, name)
    if obj is None:
        op.report({'ERROR'}, "The tiles held no geometry")
        return None
    credit = "; ".join(sorted(credits, key=lambda c: -credits[c]))
    obj["mixtli_tiles"] = credit
    obj["mixtli_frame"] = 'TILES'
    for o in context.view_layer.objects:
        o.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    context.scene.mixtli_gis.georef = 'SCENE'
    s.status = "%d tiles, %s" % (len(sel.tiles), credit[:60])
    lines = ["Google 3D Tiles - %s" % obj.name,
             "  site %.7f, %.7f, radius %.0f m, detail %s"
             % (lat, lon, s.radius, s.detail.lower()),
             "  scene origin %.7f, %.7f" % (olat, olon),
             "  %d tiles from %d tileset requests in %.1fs"
             % (len(sel.tiles), sel.requests, time.time() - t0),
             "  credit: Google; %s" % credit] + lines
    print("[mixtli] " + "\n[mixtli] ".join(lines))
    op.report({'INFO'}, "%d tiles in %.0fs - data: Google, %s"
              % (len(sel.tiles), time.time() - t0, credit[:80]))
    return obj


def grid_settings(obj, spacing):
    """Regular Grid parameters for a tiles mesh: ~25 scattered points per
    cell, capped where numpy starts to strain."""
    from .colour import surface_area
    cells = surface_area(obj) / (spacing * spacing)
    return dict(point_source='GRID', voxel_size=spacing, grid_mode='CENTROID',
                py_points=int(min(max(cells * 25, 200_000), 20_000_000)),
                radius_mode='AUTO', radius_factor=0.6)


class MIXTLI_OT_site_build(bpy.types.Operator):
    """Everything for the place in one go: Google 3D Tiles, a point cloud,
    OpenStreetMap data mapped onto it, coloured by attribute"""
    bl_idname = "scene.mixtli_site_build"
    bl_label = "Build Site"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        s = context.scene.mixtli_site
        t0 = time.time()
        mesh = run_import(self, context)
        if mesh is None:
            return {'CANCELLED'}
        done = ["tiles"]
        if s.make_cloud:
            cloud = make_cloud(self, context, mesh, s.spacing, s.hide_mesh)
            if cloud is None:
                return {'FINISHED'}
            done.append("point cloud")
            if s.get_gis:
                if gis_for(self, context, cloud):
                    done.append("GIS")
        self.report({'INFO'}, "Site built in %.0fs: %s" % (time.time() - t0, ", ".join(done)))
        return {'FINISHED'}


def make_cloud(op, context, mesh, spacing, hide_mesh=True):
    for o in context.view_layer.objects:
        o.select_set(False)
    mesh.select_set(True)
    context.view_layer.objects.active = mesh
    r = bpy.ops.object.mixtli_pointcloud('EXEC_DEFAULT', **grid_settings(mesh, spacing))
    cloud = context.view_layer.objects.active
    if 'FINISHED' not in r or cloud is mesh:
        op.report({'WARNING'}, "Tiles imported, but the point cloud failed - see console")
        return None
    cloud["mixtli_frame"] = 'TILES'
    if hide_mesh:
        mesh.hide_set(True)
        mesh.hide_render = True
    return cloud


def gis_for(op, context, cloud):
    g = context.scene.mixtli_gis
    g.source = 'OSM'
    for o in context.view_layer.objects:
        o.select_set(False)
    cloud.select_set(True)
    context.view_layer.objects.active = cloud
    try:
        r = bpy.ops.object.mixtli_gis_fetch('EXEC_DEFAULT')
        if 'FINISHED' in r:
            r = bpy.ops.object.mixtli_gis_map('EXEC_DEFAULT')
    except RuntimeError as e:
        op.report({'WARNING'}, "Cloud made, GIS step failed: %s" % e)
        return False
    return 'FINISHED' in r


# ---------------------------------------------------------------- panel

class MIXTLI_PT_site(bpy.types.Panel):
    bl_label = "Site from Google 3D Tiles"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Mixtli"
    bl_parent_id = "MIXTLI_PT_panel"
    bl_order = 0

    def draw(self, context):
        layout = self.layout
        s = context.scene.mixtli_site
        if not api_key(context):
            box = layout.box()
            box.label(text="Needs a Google Maps API key", icon='ERROR')
            p = prefs(context)
            if p:
                box.prop(p, "google_api_key", text="Key")
            box.label(text="Map Tiles API, from console.cloud.google.com")
        row = layout.row(align=True)
        row.prop(s, "query", text="", icon='VIEWZOOM')
        row.operator("scene.mixtli_site_locate", text="", icon='WORLD')
        if s.located:
            layout.label(text=s.located, icon='PINNED')
        col = layout.column(align=True)
        col.prop(s, "radius")
        col.row(align=True).prop(s, "detail", expand=True)
        row = layout.row(align=True)
        row.prop(s, "make_cloud", toggle=True)
        sub = row.row(align=True)
        sub.enabled = s.make_cloud
        sub.prop(s, "get_gis", toggle=True)
        if s.make_cloud:
            layout.prop(s, "spacing")
        col = layout.column()
        col.scale_y = 1.5
        col.operator("scene.mixtli_site_build", icon='WORLD_DATA')
        layout.operator("scene.mixtli_tiles_import", text="Import Tiles Only", icon='IMPORT')
        if s.status:
            layout.label(text=s.status, icon='CHECKMARK')
        layout.prop(s, "show_options", icon='TRIA_DOWN' if s.show_options else 'TRIA_RIGHT',
                    emboss=False)
        if s.show_options:
            col = layout.column(align=True)
            col.use_property_split = True
            col.prop(s, "lat")
            col.prop(s, "lon")
            col.prop(s, "max_tiles")
            col.prop(s, "hide_mesh")
        layout.label(text="Map data: Google; credits kept on the object")


CLASSES = (MIXTLI_AP_prefs, MIXTLI_PG_site, MIXTLI_OT_site_locate, MIXTLI_OT_tiles_import,
           MIXTLI_OT_site_build, MIXTLI_PT_site)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)
    bpy.types.Scene.mixtli_site = PointerProperty(type=MIXTLI_PG_site)


def unregister():
    if hasattr(bpy.types.Scene, "mixtli_site"):
        del bpy.types.Scene.mixtli_site
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
