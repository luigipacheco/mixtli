# SPDX-License-Identifier: GPL-3.0-or-later
"""Operator and sidebar panel."""

import time

import bpy
from bpy.props import (BoolProperty, EnumProperty, FloatProperty,
                       IntProperty, StringProperty)

from .build import process
from .colour import input_kind, surface_area

BIG_POINT_WARNING = 25_000_000


def fmt_count(n):
    n = int(n)
    if n >= 1_000_000:
        return "%.1fM" % (n / 1_000_000.0)
    if n >= 1_000:
        return "%.0fk" % (n / 1_000.0)
    return str(n)


def fmt_area(a):
    if a >= 1_000_000:
        return "%.2f km2" % (a / 1_000_000.0)
    return "%.4g m2" % a


class MIXTLI_OT_pointcloud(bpy.types.Operator):
    """Flatten all materials to a colour attribute and build a point cloud"""
    bl_idname = "object.mixtli_pointcloud"
    bl_label = "Mesh to Colour Point Cloud"
    bl_options = {'REGISTER', 'UNDO'}

    # --- main
    point_source: EnumProperty(
        name="Points From",
        items=[('DISTRIBUTE', "Scatter on Faces",
                "Scatter points across the surface at a chosen density, "
                "live in geometry nodes"),
               ('VERTS', "Mesh Vertices",
                "One point per existing vertex"),
               ('GRID', "Regular Grid (Python)",
                "Scatter, filter and voxel-grid in numpy, then output a real "
                "PointCloud object. Non-destructive; add geometry nodes after")],
        default='DISTRIBUTE')
    density_mode: EnumProperty(
        name="Density",
        items=[('TARGET', "Target Count", "Aim for a total number of points"),
               ('ABSOLUTE', "Points / m2", "Fixed density per square metre")],
        default='TARGET')
    target_points: IntProperty(
        name="Target Points", default=1_500_000, min=100, soft_max=20_000_000)
    density: FloatProperty(
        name="Points / m2", default=200.0, min=0.0000001, soft_max=10000.0)

    # --- python grid path
    py_points: IntProperty(
        name="Scatter Points", default=2_000_000, min=100, soft_max=20_000_000,
        description="Points scattered before filtering and gridding. Aim for "
                    "25-50 per cell or the grid comes out with holes")
    voxel_size: FloatProperty(
        name="Voxel Size", default=0.0, min=0.0, soft_max=100.0, subtype='DISTANCE',
        description="Grid cell size. 0 keeps the raw scattered points")
    grid_mode: EnumProperty(
        name="Cell Position",
        items=[('CENTROID', "Centroid",
                "Average of the points in the cell - follows the surface"),
               ('CENTER', "Cell Centre",
                "Snap to the cell centre - a perfectly regular lattice")],
        default='CENTROID')
    world_anchored: BoolProperty(
        name="Anchor Grid to Origin", default=True,
        description="Key the lattice to the object origin so the same voxel "
                    "size always lands on the same planes, and separate "
                    "objects share one grid. Off keys it to the cloud's "
                    "bounding box, which shifts with seed and point count")
    min_cell_points: IntProperty(
        name="Min Points / Cell", default=0, min=0, soft_max=50,
        description="Drop cells holding fewer points than this. Cheap outlier "
                    "removal; only meaningful below the average occupancy")
    export_ply: BoolProperty(name="Export PLY", default=False)
    ply_path: StringProperty(name="PLY Path", default="//pointcloud.ply",
                             subtype='FILE_PATH')
    display_group: BoolProperty(
        name="Add Display Node Group", default=True,
        description="Add a shared geometry-nodes modifier with a Radius Scale "
                    "slider, so point size stays adjustable after the fact")

    # --- shared
    radius_mode: EnumProperty(
        name="Radius",
        items=[('AUTO', "Auto", "Size the points from their spacing"),
               ('MANUAL', "Manual", "Set the point radius directly")],
        default='AUTO')
    radius_factor: FloatProperty(
        name="Radius Factor", default=0.6, min=0.01, soft_max=3.0,
        description="Radius as a fraction of point spacing. 0.5 makes "
                    "neighbours touch along the axes, 0.87 seals the gaps")
    point_radius: FloatProperty(
        name="Point Radius", default=0.02, min=0.000001, soft_max=10.0,
        subtype='DISTANCE')
    duplicate: BoolProperty(
        name="Keep Original (work on a copy)", default=True)

    # --- advanced
    show_advanced: BoolProperty(name="Advanced", default=False)
    method: EnumProperty(
        name="Colour From",
        items=[('AUTO', "Auto", "Sample textures directly; bake with Cycles if needed"),
               ('SAMPLE', "Sample Textures", "Read texture pixels through the UVs"),
               ('CYCLES', "Cycles Bake", "Always bake with Cycles")],
        default='AUTO')
    attr_name: StringProperty(name="Attribute", default="Col")
    bilinear: BoolProperty(name="Bilinear Filtering", default=True)
    seed: IntProperty(name="Seed", default=0)
    clear_slots: BoolProperty(
        name="Replace Material Slots", default=True,
        description="Drop the flattened slots and leave only the point material")
    emission_strength: FloatProperty(name="Emission Strength", default=1.0, min=0.0)
    cycles_samples: IntProperty(name="Cycles Samples", default=16, min=1, max=4096)
    cycles_bake_type: EnumProperty(
        name="Bake Pass",
        items=[('AUTO', "Auto", "EMIT for emission-driven materials, else DIFFUSE"),
               ('DIFFUSE', "Diffuse", ""),
               ('EMIT', "Emit", ""),
               ('COMBINED', "Combined", "")],
        default='AUTO')
    suffix: StringProperty(name="Suffix", default="_pointcloud")

    # --- cached stats, filled in invoke()
    stat_area: FloatProperty(options={'HIDDEN', 'SKIP_SAVE'})
    stat_objects: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    stat_verts: IntProperty(options={'HIDDEN', 'SKIP_SAVE'})
    stat_is_points: BoolProperty(options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        return any(input_kind(o) for o in context.selected_objects)

    def estimate(self):
        """(points, spacing, radius) for the current settings."""
        area = self.stat_area
        if self.point_source == 'GRID':
            pts = self.py_points
            if self.voxel_size > 0.0:
                spacing = self.voxel_size
                # occupied cells on a surface scale with area / voxel^2,
                # capped by the number of points actually scattered
                pts = min(pts, area / (self.voxel_size ** 2)) if area > 0 else pts
            else:
                spacing = (area / max(pts, 1)) ** 0.5 if area > 0 else 0.0
            radius = self.point_radius if self.radius_mode == 'MANUAL' \
                else spacing * self.radius_factor
            return pts, spacing, radius
        if self.point_source == 'VERTS':
            pts = self.stat_verts
        elif self.density_mode == 'TARGET':
            pts = self.target_points * max(self.stat_objects, 1)
        else:
            pts = self.density * area
        eff = (pts / area) if area > 0 else 0.0
        spacing = (1.0 / eff) ** 0.5 if eff > 0 else 0.0
        radius = self.point_radius if self.radius_mode == 'MANUAL' \
            else spacing * self.radius_factor
        return pts, spacing, radius

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False

        if self.stat_is_points:
            # input is already a cloud: nothing to bake or scatter
            layout.prop(self, "voxel_size")
            if self.voxel_size > 0.0:
                layout.prop(self, "grid_mode")
                layout.prop(self, "world_anchored")
                layout.prop(self, "min_cell_points")
            layout.prop(self, "radius_mode")
            layout.prop(self, "point_radius" if self.radius_mode == 'MANUAL'
                        else "radius_factor")
            layout.prop(self, "display_group")

            box = layout.box()
            col = box.column(align=True)
            col.scale_y = 0.85
            col.label(text="%d cloud(s)  -  %s points in"
                           % (self.stat_objects, fmt_count(self.stat_verts)),
                      icon='OUTLINER_OB_POINTCLOUD')
            if self.voxel_size > 0.0:
                col.label(text="Gridding at %.4g m - cell count reported on run"
                               % self.voxel_size)
            else:
                col.label(text="Voxel Size 0 - keeping every point as-is")

            layout.separator()
            layout.prop(self, "show_advanced", toggle=True,
                        icon='TRIA_DOWN' if self.show_advanced else 'TRIA_RIGHT')
            if self.show_advanced:
                box = layout.box()
                box.use_property_split = True
                box.prop(self, "attr_name")
                box.prop(self, "emission_strength")
                box.prop(self, "suffix")
                box.separator()
                box.prop(self, "export_ply")
                if self.export_ply:
                    box.prop(self, "ply_path")
            return

        layout.prop(self, "point_source")
        if self.point_source == 'DISTRIBUTE':
            layout.prop(self, "density_mode")
            layout.prop(self, "target_points" if self.density_mode == 'TARGET'
                        else "density")
        elif self.point_source == 'GRID':
            layout.prop(self, "py_points")
            layout.prop(self, "voxel_size")
            if self.voxel_size > 0.0:
                layout.prop(self, "grid_mode")
                layout.prop(self, "world_anchored")
                layout.prop(self, "min_cell_points")
        layout.prop(self, "radius_mode")
        layout.prop(self, "point_radius" if self.radius_mode == 'MANUAL'
                    else "radius_factor")
        if self.point_source != 'GRID':
            layout.prop(self, "duplicate")

        pts, spacing, radius = self.estimate()
        box = layout.box()
        col = box.column(align=True)
        col.scale_y = 0.85
        col.label(text="%d object(s)  -  %s surface"
                       % (self.stat_objects, fmt_area(self.stat_area)),
                  icon='OUTLINER_OB_MESH')
        if self.point_source == 'GRID' and self.voxel_size > 0.0:
            per_cell = self.py_points / max(pts, 1.0)
            col.label(text="%s scattered  ->  ~%s cells  -  r %.3g m"
                           % (fmt_count(self.py_points), fmt_count(pts), radius))
            col.label(text="~%.1f points per cell  (keep Min Points / Cell below this)"
                           % per_cell)
            if per_cell < 10:
                col.label(text="Low coverage - raise Scatter Points", icon='ERROR')
        else:
            col.label(text="~%s points  -  %.3g m apart  -  r %.3g m"
                           % (fmt_count(pts), spacing, radius))
        if pts > BIG_POINT_WARNING:
            col.label(text="Heavy - viewport may crawl", icon='ERROR')

        layout.separator()
        layout.prop(self, "show_advanced", toggle=True,
                    icon='TRIA_DOWN' if self.show_advanced else 'TRIA_RIGHT')
        if self.show_advanced:
            box = layout.box()
            box.use_property_split = True
            box.prop(self, "method")
            box.prop(self, "attr_name")
            box.prop(self, "clear_slots")
            box.prop(self, "emission_strength")
            if self.point_source != 'VERTS':
                box.prop(self, "seed")
            box.prop(self, "bilinear")
            box.prop(self, "suffix")
            if self.point_source == 'GRID':
                box.separator()
                box.prop(self, "export_ply")
                if self.export_ply:
                    box.prop(self, "ply_path")
            if self.method in ('AUTO', 'CYCLES'):
                box.separator()
                box.prop(self, "cycles_bake_type")
                box.prop(self, "cycles_samples")

    def invoke(self, context, event):
        targets = [o for o in context.selected_objects if input_kind(o)]
        pts = [o for o in targets if input_kind(o) == 'POINTS']
        self.stat_is_points = bool(pts) and len(pts) == len(targets)
        self.stat_objects = len(targets)
        if self.stat_is_points:
            self.stat_verts = sum(
                len(o.data.points) if o.type == 'POINTCLOUD' else len(o.data.vertices)
                for o in targets)
            self.stat_area = 0.0
        else:
            mesh = [o for o in targets if input_kind(o) == 'MESH']
            self.stat_verts = sum(len(o.data.vertices) for o in mesh)
            self.stat_area = sum(surface_area(o) for o in mesh)
        return context.window_manager.invoke_props_dialog(self, width=380)

    def execute(self, context):
        t0 = time.time()
        targets = [o for o in context.selected_objects if input_kind(o)]
        if not targets:
            self.report({'ERROR'}, "Select at least one mesh object")
            return {'CANCELLED'}

        report, made = [], []
        for o in targets:
            r = process(o, self, report)
            if r:
                made.append(r)

        for o in context.view_layer.objects:
            o.select_set(False)
        for o in made:
            o.select_set(True)
        if made:
            context.view_layer.objects.active = made[-1]

        print("[mixtli] " + "\n[mixtli] ".join(report))
        if not made:
            self.report({'ERROR'}, "Could not produce colour data - see System Console")
            return {'CANCELLED'}
        self.report({'INFO'}, "%d point cloud(s) in %.2fs"
                    % (len(made), time.time() - t0))
        return {'FINISHED'}


class MIXTLI_PT_panel(bpy.types.Panel):
    bl_label = "Mixtli"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Mixtli"

    def draw(self, context):
        layout = self.layout
        sel = [o for o in context.selected_objects if o.type == 'MESH']
        col = layout.column()
        col.scale_y = 1.4
        col.operator("object.mixtli_pointcloud", icon='OUTLINER_OB_POINTCLOUD')
        if not sel:
            layout.label(text="Select a mesh object", icon='INFO')
        else:
            layout.label(text="%d mesh object(s) selected" % len(sel))


CLASSES = (MIXTLI_OT_pointcloud, MIXTLI_PT_panel)


def register():
    for c in CLASSES:
        bpy.utils.register_class(c)


def unregister():
    for c in reversed(CLASSES):
        bpy.utils.unregister_class(c)
