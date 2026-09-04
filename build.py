# SPDX-License-Identifier: GPL-3.0-or-later
"""
Turning a flattened mesh into something you can look at.

Two owners, never mixed mid-pipeline:

  Geometry nodes  own the DISTRIBUTE and VERTS paths. Live, non-destructive,
                  scrub the density and watch it update.
  Python          owns the GRID path. Global knowledge of every point at
                  once, which is what voxel gridding needs and what nodes
                  cannot do - a node has no way to know which other points
                  share its cell.

The reason not to mix them: a geometry-nodes point cloud can't be read back
into Python cleanly. evaluated_get().data hands you the mesh component,
which is empty, because the points live in a component the API barely
exposes. So each path runs end to end in one place.
"""

import bpy
import numpy as np

from . import pointops
from .colour import bake_onto, surface_area, triangle_arrays


# ---------------------------------------------------------------- sizing

def sizing(obj, cfg):
    """(density, radius) suited to this object's real-world scale.

    A fixed points-per-square-metre is useless across scales: the same
    number that looks right on a teapot produces 300 million points on a
    city block. Everything is derived from measured surface area instead.
    """
    if cfg.point_source == 'GRID':
        area = surface_area(obj)
        if cfg.voxel_size > 0.0:
            spacing = cfg.voxel_size
        elif area > 0:
            spacing = (area / max(cfg.py_points, 1)) ** 0.5
        else:
            spacing = max(obj.dimensions) / 100.0
        density = 0.0
    elif cfg.point_source == 'VERTS':
        nv = max(len(obj.data.vertices), 1)
        area = surface_area(obj)
        spacing = (area / nv) ** 0.5 if area > 0 else max(obj.dimensions) / 100.0
        density = 0.0
    else:
        if cfg.density_mode == 'ABSOLUTE':
            density = cfg.density
        else:
            area = surface_area(obj)
            density = (cfg.target_points / area) if area > 0 else cfg.density
        density = max(density, 1e-9)
        spacing = (1.0 / density) ** 0.5

    if cfg.radius_mode == 'MANUAL':
        radius = max(cfg.point_radius, 1e-6)
    else:
        radius = max(spacing * cfg.radius_factor, 1e-6)
    return density, radius


# ---------------------------------------------------------------- materials

def point_material(cfg):
    name = "Mixtli_%s" % cfg.attr_name
    mat = bpy.data.materials.get(name)
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    attr = nt.nodes.new("ShaderNodeVertexColor")
    attr.layer_name = cfg.attr_name
    attr.location = (-320, 0)
    emis = nt.nodes.new("ShaderNodeEmission")
    emis.inputs["Strength"].default_value = cfg.emission_strength
    emis.location = (-120, 0)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (80, 0)
    nt.links.new(attr.outputs["Color"], emis.inputs["Color"])
    nt.links.new(emis.outputs["Emission"], out.inputs["Surface"])
    return mat


# ---------------------------------------------------------------- nodes

def point_cloud_group(cfg, mat, density, radius, key):
    """One node group per SOURCE object.

    Sharing a single group across objects is a trap: processing object 2
    would delete the group object 1 is using, and per-modifier input
    overrides don't survive the operator's undo push, so a shared group
    cannot carry two different scales. An existing group is rebuilt in
    place rather than removed, so clouds from earlier runs keep working.
    """
    name = ("Mixtli_%s" % key)[:60]
    ng = bpy.data.node_groups.get(name)
    if ng is None:
        ng = bpy.data.node_groups.new(name, 'GeometryNodeTree')
    else:
        ng.interface.clear()
        ng.nodes.clear()

    ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    s_rad = ng.interface.new_socket("Radius", in_out='INPUT', socket_type='NodeSocketFloat')
    s_rad.default_value = radius
    s_rad.min_value = 0.0
    s_den = None
    if cfg.point_source == 'DISTRIBUTE':
        s_den = ng.interface.new_socket("Density", in_out='INPUT',
                                        socket_type='NodeSocketFloat')
        s_den.default_value = density
        s_den.min_value = 0.0

    n = ng.nodes
    gi = n.new("NodeGroupInput");  gi.location = (-600, 0)
    go = n.new("NodeGroupOutput"); go.location = (400, 0)

    if cfg.point_source == 'DISTRIBUTE':
        src = n.new("GeometryNodeDistributePointsOnFaces")
        src.location = (-360, 0)
        src.inputs["Seed"].default_value = cfg.seed
        ng.links.new(gi.outputs["Geometry"], src.inputs["Mesh"])
        ng.links.new(gi.outputs["Density"], src.inputs["Density"])
    else:
        src = n.new("GeometryNodeMeshToPoints")
        src.location = (-360, 0)
        src.mode = 'VERTICES'
        ng.links.new(gi.outputs["Geometry"], src.inputs["Mesh"])

    rad = n.new("GeometryNodeSetPointRadius"); rad.location = (-120, 0)
    ng.links.new(src.outputs["Points"], rad.inputs["Points"])
    ng.links.new(gi.outputs["Radius"], rad.inputs["Radius"])

    setm = n.new("GeometryNodeSetMaterial"); setm.location = (140, 0)
    setm.inputs["Material"].default_value = mat
    ng.links.new(rad.outputs["Points"], setm.inputs["Geometry"])
    ng.links.new(setm.outputs["Geometry"], go.inputs[0])
    return ng, s_rad, s_den


def set_modifier_input(mod, identifier, value):
    """Blender 5.x moved geometry-nodes modifier inputs off IDProperties onto
    mod.properties.inputs; 4.x and earlier used mod[identifier]."""
    try:
        mod.properties.inputs[identifier] = value      # Blender 5.x
        return True
    except Exception:
        pass
    try:
        mod[identifier] = value                        # Blender 4.x
        return True
    except Exception:
        return False


def apply_group(obj, ng, s_rad, s_den, density, radius):
    for m in list(obj.modifiers):
        if m.type == 'NODES':
            obj.modifiers.remove(m)
    mod = obj.modifiers.new("Mixtli", 'NODES')
    mod.node_group = ng
    set_modifier_input(mod, s_rad.identifier, radius)
    if s_den is not None:
        set_modifier_input(mod, s_den.identifier, density)
    return mod


# ---------------------------------------------------------------- pointcloud

def build_pointcloud_object(name, P, C, radius, mat, matrix, collections, attr_name):
    """A real PointCloud datablock, not a mesh with a modifier. Geometry
    nodes can still be stacked on top for post-processing."""
    pc = bpy.data.pointclouds.new(name)
    pc.resize(len(P))
    pc.attributes["position"].data.foreach_set(
        "vector", np.ascontiguousarray(P).ravel())
    ra = pc.attributes.get("radius") or pc.attributes.new("radius", 'FLOAT', 'POINT')
    ra.data.foreach_set("value", np.full(len(P), radius, dtype=np.float32))
    ca = pc.attributes.get(attr_name) or pc.attributes.new(attr_name, 'FLOAT_COLOR', 'POINT')
    ca.data.foreach_set("color", np.ascontiguousarray(C).ravel())
    pc.materials.append(mat)
    obj = bpy.data.objects.new(name, pc)
    obj.matrix_world = matrix
    for c in collections:
        c.objects.link(obj)
    return obj


# ---------------------------------------------------------------- pipelines

def process_grid(src, cfg, report):
    """Python owns everything here: bake onto a scratch copy, scatter,
    filter, grid, emit a PointCloud. The source object is never touched."""
    tmp = src.copy()
    tmp.data = src.data.copy()
    bpy.context.scene.collection.objects.link(tmp)   # bake ops need it linked
    try:
        report.append("%s -> point cloud" % src.name)
        if not bake_onto(tmp, cfg, report):
            report.append("  FAILED to produce colour data")
            return None

        tri_pos, tri_col = triangle_arrays(tmp.data, cfg.attr_name)
        if tri_pos is None:
            report.append("  no triangles")
            return None

        P, C = pointops.scatter_on_triangles(tri_pos, tri_col, cfg.py_points, cfg.seed)
        report.append("  scattered %d points on %d triangles" % (len(P), len(tri_pos)))

        anchored = cfg.world_anchored
        if cfg.voxel_size > 0.0 and cfg.min_cell_points > 1:
            P, C, dropped = pointops.filter_sparse_cells(
                P, C, cfg.voxel_size, cfg.min_cell_points, anchored)
            report.append("  dropped %d points in cells with < %d points"
                          % (dropped, cfg.min_cell_points))

        if cfg.voxel_size > 0.0:
            before = len(P)
            P, C, counts = pointops.voxel_downsample(
                P, C, cfg.voxel_size, cfg.grid_mode, anchored)
            report.append("  voxel %.4g m (%s%s): %d -> %d points, %.1f per cell"
                          % (cfg.voxel_size, cfg.grid_mode.lower(),
                             ", anchored" if anchored else "", before, len(P),
                             float(counts.mean()) if len(counts) else 0.0))

        if len(P) == 0:
            report.append("  nothing left after filtering")
            return None
    finally:
        md = tmp.data
        bpy.data.objects.remove(tmp, do_unlink=True)
        if md.users == 0:
            bpy.data.meshes.remove(md)

    _, radius = sizing(src, cfg)
    report.append("  radius %.4g m" % radius)
    mat = point_material(cfg)
    obj = build_pointcloud_object(src.name + cfg.suffix, P, C, radius, mat,
                                  src.matrix_world.copy(),
                                  src.users_collection, cfg.attr_name)

    if cfg.export_ply and cfg.ply_path:
        path = bpy.path.abspath(cfg.ply_path)
        try:
            n = pointops.write_ply(path, P, C)
            report.append("  wrote %d points to %s" % (n, path))
        except Exception as e:
            report.append("  PLY export failed: %s" % e)
    return obj


def process(obj, cfg, report):
    if cfg.point_source == 'GRID':
        return process_grid(obj, cfg, report)

    key = obj.name                     # name the node group after the SOURCE
    if cfg.duplicate:
        new = obj.copy()
        new.data = obj.data.copy()
        new.name = obj.name + cfg.suffix
        for c in obj.users_collection:
            c.objects.link(new)
        obj = new
    report.append("%s" % obj.name)

    if not bake_onto(obj, cfg, report):
        report.append("  FAILED to produce colour data")
        return None

    density, radius = sizing(obj, cfg)
    report.append("  density %.4g pts/m2, radius %.4g m" % (density, radius))
    mat = point_material(cfg)
    ng, s_rad, s_den = point_cloud_group(cfg, mat, density, radius, key)
    apply_group(obj, ng, s_rad, s_den, density, radius)

    if cfg.clear_slots:
        obj.data.materials.clear()
        obj.data.materials.append(mat)
    return obj
