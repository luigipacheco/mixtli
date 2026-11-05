# SPDX-License-Identifier: GPL-3.0-or-later
"""
Turning a flattened mesh into something you can look at.

Two owners, never mixed mid-pipeline:

  Geometry nodes  own the DISTRIBUTE and VERTS paths. Live, non-destructive,
                  scrub the density and watch it update.
  Python          owns the GRID and VOLUME paths. Global knowledge of every
                  point at once, which is what voxel gridding needs and
                  what nodes cannot do - a node has no way to know which
                  other points share its cell, or whether a cell with no
                  point in it at all is inside the mesh.

The reason not to mix them: a geometry-nodes point cloud can't be read back
into Python cleanly. evaluated_get().data hands you the mesh component,
which is empty, because the points live in a component the API barely
exposes. So each path runs end to end in one place.
"""

import bpy
import numpy as np

from . import pointops
from .colour import (bake_onto, input_kind, read_point_arrays, surface_area,
                     triangle_arrays)


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

def material_usable(mat):
    """A material someone can look at: nodes on, and an output with a surface."""
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return False
    return any(n.type == 'OUTPUT_MATERIAL' and n.inputs["Surface"].is_linked
               for n in mat.node_tree.nodes)


def point_material(cfg):
    """The material that shows `cfg.attr_name`, one per attribute name.

    Built once. An existing, working one is handed back untouched, so edits
    made to it in the Shader Editor survive another run and every change of
    view attribute. `cfg.reset_material` forces a rebuild from the settings.

    `cfg.shader`: PRINCIPLED (default) feeds the colour into a Principled
    BSDF, so the points are lit like everything else in the scene; EMISSION
    is the old flat, unlit look, at `cfg.emission_strength`.
    """
    name = "Mixtli_%s" % cfg.attr_name
    mat = bpy.data.materials.get(name)
    if mat is not None and material_usable(mat) and not getattr(cfg, "reset_material", False):
        return mat
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    attr = nt.nodes.new("ShaderNodeVertexColor")
    attr.layer_name = cfg.attr_name
    attr.location = (-320, 0)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (120, 0)
    if getattr(cfg, "shader", 'PRINCIPLED') == 'EMISSION':
        shader = nt.nodes.new("ShaderNodeEmission")
        shader.inputs["Strength"].default_value = getattr(cfg, "emission_strength", 1.0)
        nt.links.new(attr.outputs["Color"], shader.inputs["Color"])
        nt.links.new(shader.outputs["Emission"], out.inputs["Surface"])
    else:
        shader = nt.nodes.new("ShaderNodeBsdfPrincipled")
        nt.links.new(attr.outputs["Color"], shader.inputs["Base Color"])
        nt.links.new(shader.outputs["BSDF"], out.inputs["Surface"])
    shader.location = (-120, 0)
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
    ng, saved = _fresh_group(("Mixtli_%s" % key)[:60])

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
    _restore_inputs(ng, saved)
    return ng, s_rad, s_den


def display_group(mat):
    """Shared display group: scale the per-point radius and set the material.

    Deliberately reads the stored `radius` attribute and multiplies it, rather
    than exposing an absolute radius. That keeps ONE group correct for every
    object regardless of scale - an absolute value in a shared group would be
    wrong for all but one of them. A mesh coming in is taken by its vertices,
    so the group also works dropped on an imported face-less cloud.
    """
    ng, saved = _fresh_group("Mixtli_Display")

    ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    s_scale = ng.interface.new_socket("Radius Scale", in_out='INPUT',
                                      socket_type='NodeSocketFloat')
    s_scale.default_value = 1.0
    s_scale.min_value = 0.0

    n = ng.nodes
    gi = n.new("NodeGroupInput");  gi.location = (-1000, 0)
    go = n.new("NodeGroupOutput"); go.location = (300, 0)
    points = _as_points(ng, gi.outputs["Geometry"], -780, 0)
    rin = n.new("GeometryNodeInputRadius"); rin.location = (-560, -400)
    mul = n.new("ShaderNodeMath"); mul.location = (-360, -340)
    mul.operation = 'MULTIPLY'
    rad = n.new("GeometryNodeSetPointRadius"); rad.location = (-150, 0)
    setm = n.new("GeometryNodeSetMaterial"); setm.location = (70, 0)
    setm.inputs["Material"].default_value = mat

    ng.links.new(rin.outputs[0], mul.inputs[0])
    ng.links.new(gi.outputs["Radius Scale"], mul.inputs[1])
    ng.links.new(points, rad.inputs["Points"])
    ng.links.new(mul.outputs[0], rad.inputs["Radius"])
    ng.links.new(rad.outputs["Points"], setm.inputs["Geometry"])
    ng.links.new(setm.outputs["Geometry"], go.inputs[0])
    _restore_inputs(ng, saved)
    return ng, s_scale


def apply_display_group(obj, mat):
    ng, s_scale = display_group(mat)
    mod = obj.modifiers.new("Mixtli Display", 'NODES')
    mod.node_group = ng
    set_modifier_input(mod, s_scale.identifier, 1.0)
    return mod


def set_modifier_input(mod, identifier, value):
    """Blender 5.x keeps each geometry-nodes modifier input as a small group
    under mod.properties.inputs - {value, type, attribute_name} - so the
    value goes into its "value" slot; assigning to the slot itself replaces
    the group and the modifier quietly ignores it. 4.x used mod[identifier]."""
    props = getattr(mod, "properties", None)
    if props is not None:
        try:
            props.inputs[identifier]["value"] = value      # Blender 5.x
            return True
        except Exception:
            pass
    try:
        mod[identifier] = value                            # Blender 4.x
        return True
    except Exception:
        return False


def get_modifier_input(mod, identifier):
    """The stored value of a modifier input, or None when there is none."""
    props = getattr(mod, "properties", None)
    if props is not None:
        try:
            return props.inputs[identifier]["value"]
        except Exception:
            pass
    try:
        return mod[identifier]
    except Exception:
        return None


def _input_items(ng):
    return [i for i in ng.interface.items_tree
            if i.item_type == 'SOCKET' and i.in_out == 'INPUT' and hasattr(i, "default_value")]


def _modifier_inputs(ng):
    """[(modifier, {socket name: value})] for every modifier using `ng`."""
    saved = []
    for obj in bpy.data.objects:
        for mod in obj.modifiers:
            if mod.type != 'NODES' or mod.node_group != ng:
                continue
            vals = {}
            for item in _input_items(ng):
                v = get_modifier_input(mod, item.identifier)
                if v is not None:
                    vals[item.name] = v
            saved.append((mod, vals))
    return saved


def _restore_inputs(ng, saved):
    """Put each modifier's values back by socket name; a socket it did not
    have yet takes the interface default."""
    for mod, vals in saved:
        for item in _input_items(ng):
            set_modifier_input(mod, item.identifier, vals.get(item.name, item.default_value))


def _fresh_group(name):
    """An empty group to build into, plus what its modifiers held.

    Clearing an interface and adding its sockets again gives them new
    identifiers, and Blender fills every existing modifier's slot for a
    socket the moment the socket is added - with its default of 0, before
    the builder has set the real one. Left alone, each run would zero the
    radius and density of every earlier cloud sharing the group, so the
    values are taken before and restored with _restore_inputs() after.
    """
    ng = bpy.data.node_groups.get(name)
    if ng is None:
        return bpy.data.node_groups.new(name, 'GeometryNodeTree'), []
    saved = _modifier_inputs(ng)
    ng.interface.clear()
    ng.nodes.clear()
    return ng, saved


def set_default_inputs(mod):
    """Write the interface defaults into a new modifier explicitly."""
    for item in _input_items(mod.node_group):
        set_modifier_input(mod, item.identifier, item.default_value)


def _as_points(ng, geo_out, x, y):
    """Nodes turning whatever comes in into points: a point cloud passes
    through as it is; a mesh contributes its vertices, with their `radius`
    attribute when they carry one (an imported or a 4.x cloud) and Mesh to
    Points' own default otherwise. Returns the points socket."""
    n = ng.nodes
    sep = n.new("GeometryNodeSeparateComponents")
    sep.location = (x, y)
    ng.links.new(geo_out, sep.inputs["Geometry"])
    named = n.new("GeometryNodeInputNamedAttribute")
    named.location = (x, y - 240)
    named.data_type = 'FLOAT'
    named.inputs["Name"].default_value = "radius"
    pick = n.new("GeometryNodeSwitch")
    pick.input_type = 'FLOAT'
    pick.location = (x + 190, y - 220)
    pick.inputs["False"].default_value = 0.05
    ng.links.new(named.outputs["Exists"], pick.inputs["Switch"])
    ng.links.new(named.outputs["Attribute"], pick.inputs["True"])
    m2p = n.new("GeometryNodeMeshToPoints")
    m2p.location = (x + 190, y - 60)
    m2p.mode = 'VERTICES'
    ng.links.new(sep.outputs["Mesh"], m2p.inputs["Mesh"])
    ng.links.new(pick.outputs[0], m2p.inputs["Radius"])
    join = n.new("GeometryNodeJoinGeometry")
    join.location = (x + 400, y)
    ng.links.new(sep.outputs["Point Cloud"], join.inputs[0])
    ng.links.new(m2p.outputs["Points"], join.inputs[0])
    return join.outputs[0]


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

ATTR_FIELD = {'FLOAT': "value", 'INT': "value", 'BOOLEAN': "value",
              'FLOAT_VECTOR': "vector", 'FLOAT_COLOR': "color"}


def _convert_to_pointcloud(obj):
    """Blender 4.x: the convert operator turns a face-less mesh into a
    PointCloud and keeps every attribute and material on the way."""
    me = obj.data
    try:
        for o in bpy.context.view_layer.objects:
            o.select_set(False)
        obj.select_set(True)
        bpy.context.view_layer.objects.active = obj
        bpy.ops.object.convert(target='POINTCLOUD')
    except Exception as e:
        print("[mixtli] kept as a face-less mesh cloud, convert failed: %s" % e)
        return
    if obj.type == 'POINTCLOUD' and me.users == 0:
        bpy.data.meshes.remove(me)


def build_pointcloud_object(name, P, C, radius, mat, matrix, collections, attr_name,
                            extra=None):
    """A real PointCloud datablock, not a mesh with a modifier. Geometry
    nodes can still be stacked on top for post-processing. `extra` adds
    further point attributes: {name: (values, data_type)}.

    Blender 4.x has no way to size a PointCloud from Python, so there the
    points are built as a face-less mesh and converted afterwards."""
    n = len(P)
    pc = bpy.data.pointclouds.new(name)
    if hasattr(pc, "resize"):
        pc.resize(n)
        data = pc
        data.attributes["position"].data.foreach_set(
            "vector", np.ascontiguousarray(P).ravel())
    else:
        bpy.data.pointclouds.remove(pc)
        data = bpy.data.meshes.new(name)
        data.vertices.add(n)
        data.vertices.foreach_set("co", np.ascontiguousarray(P).ravel())
    ra = data.attributes.get("radius") or data.attributes.new("radius", 'FLOAT', 'POINT')
    ra.data.foreach_set("value", np.full(n, radius, dtype=np.float32))
    ca = data.attributes.get(attr_name) or data.attributes.new(attr_name, 'FLOAT_COLOR', 'POINT')
    ca.data.foreach_set("color", np.ascontiguousarray(C).ravel())
    for aname, (values, dtype) in (extra or {}).items():
        a = data.attributes.get(aname) or data.attributes.new(aname, dtype, 'POINT')
        a.data.foreach_set(ATTR_FIELD[dtype], np.ascontiguousarray(values).ravel())
    data.materials.append(mat)
    obj = bpy.data.objects.new(name, data)
    obj.matrix_world = matrix
    for c in collections:
        c.objects.link(obj)
    if obj.type == 'MESH':
        data.update()
        _convert_to_pointcloud(obj)
    return obj


# ---------------------------------------------------------------- volume

def volume_material():
    """The fog material for Mixtli Volume: a Principled Volume. Built once
    and reused as it is, like the point materials, so edits to it stick."""
    name = "Mixtli_Volume"
    mat = bpy.data.materials.get(name)
    if mat is not None and mat.use_nodes and mat.node_tree is not None and any(
            n.type == 'OUTPUT_MATERIAL' and n.inputs["Volume"].is_linked
            for n in mat.node_tree.nodes):
        return mat
    if mat is None:
        mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    nt.nodes.clear()
    vol = nt.nodes.new("ShaderNodeVolumePrincipled")
    vol.location = (-220, 0)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    out.location = (120, 0)
    nt.links.new(vol.outputs["Volume"], out.inputs["Volume"])
    return mat


def _resolution_by_size(node):
    """Points to Volume / Volume to Mesh sized by voxel size, not amount.
    4.x keeps the mode as a node property; 5.x made it a menu socket."""
    if hasattr(node, "resolution_mode"):
        node.resolution_mode = 'VOXEL_SIZE'
        return
    sock = node.inputs.get("Resolution Mode")
    if sock is not None:
        try:
            sock.default_value = 'Size'
        except Exception:
            pass


def volume_group(vol_mat, point_mat=None):
    """Shared group: the points as a fog volume, or as the mesh around one.

    Everything is relative to the stored per-point radius, as in
    display_group(), so one group serves clouds of any scale: the voxel
    size is the mean radius times Voxel Scale, and each point's sphere is
    its own radius times Radius Scale. With the Mixtli defaults (radius
    0.6 of the spacing) a Radius Scale of 1.5 overlaps neighbours into a
    solid. A mesh coming in is taken by its vertices, so the group also
    works dropped on a Scatter on Faces cloud or a plain mesh.
    """
    ng, saved = _fresh_group("Mixtli_Volume")

    ng.interface.new_socket("Geometry", in_out='INPUT', socket_type='NodeSocketGeometry')
    ng.interface.new_socket("Geometry", in_out='OUTPUT', socket_type='NodeSocketGeometry')
    s_vox = ng.interface.new_socket("Voxel Scale", in_out='INPUT', socket_type='NodeSocketFloat')
    s_vox.default_value = 1.0
    s_vox.min_value = 0.05
    s_vox.description = "Voxel size as a multiple of the mean point radius"
    s_rad = ng.interface.new_socket("Radius Scale", in_out='INPUT', socket_type='NodeSocketFloat')
    s_rad.default_value = 1.5
    s_rad.min_value = 0.0
    s_rad.description = "Each point's sphere as a multiple of its radius; 1.5 fuses neighbours"
    s_den = ng.interface.new_socket("Density", in_out='INPUT', socket_type='NodeSocketFloat')
    s_den.default_value = 1.0
    s_den.min_value = 0.0
    s_mesh = ng.interface.new_socket("Mesh Output", in_out='INPUT', socket_type='NodeSocketBool')
    s_mesh.default_value = False
    s_mesh.description = "Volume to Mesh: the surface wrapping the volume instead of the fog"

    n = ng.nodes
    gi = n.new("NodeGroupInput")
    gi.location = (-960, 0)
    go = n.new("NodeGroupOutput")
    go.location = (900, 0)

    points = _as_points(ng, gi.outputs["Geometry"], -760, 0)

    rin = n.new("GeometryNodeInputRadius")
    rin.location = (-340, -300)
    stat = n.new("GeometryNodeAttributeStatistic")
    stat.location = (-140, -220)
    stat.data_type = 'FLOAT'
    stat.domain = 'POINT'
    ng.links.new(points, stat.inputs["Geometry"])
    ng.links.new(rin.outputs[0], stat.inputs["Attribute"])
    vox = n.new("ShaderNodeMath")
    vox.operation = 'MULTIPLY'
    vox.location = (80, -220)
    ng.links.new(stat.outputs["Mean"], vox.inputs[0])
    ng.links.new(gi.outputs["Voxel Scale"], vox.inputs[1])
    rad = n.new("ShaderNodeMath")
    rad.operation = 'MULTIPLY'
    rad.location = (80, -400)
    ng.links.new(rin.outputs[0], rad.inputs[0])
    ng.links.new(gi.outputs["Radius Scale"], rad.inputs[1])

    p2v = n.new("GeometryNodePointsToVolume")
    p2v.location = (300, 0)
    _resolution_by_size(p2v)
    ng.links.new(points, p2v.inputs["Points"])
    ng.links.new(gi.outputs["Density"], p2v.inputs["Density"])
    ng.links.new(vox.outputs[0], p2v.inputs["Voxel Size"])
    ng.links.new(rad.outputs[0], p2v.inputs["Radius"])

    setv = n.new("GeometryNodeSetMaterial")
    setv.location = (520, 100)
    setv.inputs["Material"].default_value = vol_mat
    ng.links.new(p2v.outputs["Volume"], setv.inputs["Geometry"])

    v2m = n.new("GeometryNodeVolumeToMesh")
    v2m.location = (520, -140)
    ng.links.new(p2v.outputs["Volume"], v2m.inputs["Volume"])
    mesh_out = v2m.outputs["Mesh"]
    if point_mat is not None:
        setm = n.new("GeometryNodeSetMaterial")
        setm.location = (700, -140)
        setm.inputs["Material"].default_value = point_mat
        ng.links.new(v2m.outputs["Mesh"], setm.inputs["Geometry"])
        mesh_out = setm.outputs["Geometry"]

    sw = n.new("GeometryNodeSwitch")
    sw.input_type = 'GEOMETRY'
    sw.location = (720, 60)
    ng.links.new(gi.outputs["Mesh Output"], sw.inputs["Switch"])
    ng.links.new(setv.outputs["Geometry"], sw.inputs["False"])
    ng.links.new(mesh_out, sw.inputs["True"])
    ng.links.new(sw.outputs[0], go.inputs[0])
    _restore_inputs(ng, saved)
    return ng


def apply_volume_group(obj, point_mat=None):
    ng = volume_group(volume_material(), point_mat)
    mod = obj.modifiers.new("Mixtli Volume", 'NODES')
    mod.node_group = ng
    set_default_inputs(mod)
    return mod


# ---------------------------------------------------------------- pipelines

def points_radius(P, cfg, incoming_radius):
    """Radius for an incoming cloud, which has no surface area to measure."""
    if cfg.radius_mode == 'MANUAL':
        return max(cfg.point_radius, 1e-6)
    if cfg.voxel_size > 0.0:
        return max(cfg.voxel_size * cfg.radius_factor, 1e-6)
    if incoming_radius:
        return incoming_radius            # whatever the import already carried
    n = max(len(P), 1)
    dims = np.maximum(P.max(0) - P.min(0), 1e-9)
    vol = float(dims[0] * dims[1] * dims[2])
    spacing = (vol / n) ** (1.0 / 3.0) if vol > 0 else float(dims.max()) / 100.0
    return max(spacing * cfg.radius_factor, 1e-6)


def process_points(src, cfg, report):
    """Input is already a cloud - a PointCloud object, or a .ply imported as a
    face-less mesh. Nothing to bake or scatter, so read it, optionally grid
    it, and hand back a PointCloud with a material and a radius."""
    what = "point cloud" if src.type == 'POINTCLOUD' else "face-less mesh"
    report.append("%s (%s)" % (src.name, what))

    P, C, incoming_radius, cname = read_point_arrays(src, cfg.attr_name)
    if len(P) == 0:
        report.append("  no points")
        return None
    report.append("  %d points, colour from %s"
                  % (len(P), ("'%s'" % cname) if cname else "none - using grey"))

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

    radius = points_radius(P, cfg, incoming_radius)
    report.append("  radius %.4g m" % radius)
    mat = point_material(cfg)
    obj = build_pointcloud_object(src.name + cfg.suffix, P, C, radius, mat,
                                  src.matrix_world.copy(),
                                  src.users_collection, cfg.attr_name)
    if cfg.display_group:
        apply_display_group(obj, mat)
    if getattr(cfg, "volume_group", False):
        apply_volume_group(obj, mat)

    if cfg.export_ply and cfg.ply_path:
        path = bpy.path.abspath(cfg.ply_path)
        try:
            n = pointops.write_ply(path, P, C)
            report.append("  wrote %d points to %s" % (n, path))
        except Exception as e:
            report.append("  PLY export failed: %s" % e)
    return obj


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
    if cfg.display_group:
        apply_display_group(obj, mat)
    if getattr(cfg, "volume_group", False):
        apply_volume_group(obj, mat)

    if cfg.export_ply and cfg.ply_path:
        path = bpy.path.abspath(cfg.ply_path)
        try:
            n = pointops.write_ply(path, P, C)
            report.append("  wrote %d points to %s" % (n, path))
        except Exception as e:
            report.append("  PLY export failed: %s" % e)
    return obj


def process_volume(src, cfg, report):
    """Python owns this path too: bake onto a scratch copy, scatter the
    shell, test every lattice cell against the triangles, flood colour and
    depth inward, emit a PointCloud. The source object is never touched.

    Voxel Size is in the object's local units, like the GRID path: the
    lattice is keyed to the object, and the object's transform is put on
    the cloud afterwards."""
    tmp = src.copy()
    tmp.data = src.data.copy()
    bpy.context.scene.collection.objects.link(tmp)   # bake ops need it linked
    try:
        report.append("%s -> solid point cloud" % src.name)
        if not bake_onto(tmp, cfg, report):
            report.append("  FAILED to produce colour data")
            return None

        tri_pos, tri_col = triangle_arrays(tmp.data, cfg.attr_name)
        if tri_pos is None:
            report.append("  no triangles")
            return None
        flat = tri_pos.reshape(-1, 3)
        dims = flat.max(0) - flat.min(0)
        voxel = cfg.voxel_size if cfg.voxel_size > 0.0 else float(dims.max()) / 50.0
        if voxel <= 0.0:
            report.append("  flat object, nothing to fill")
            return None

        P, C = pointops.scatter_on_triangles(tri_pos, tri_col, cfg.py_points, cfg.seed)
        report.append("  scattered %d points on %d triangles for the shell"
                      % (len(P), len(tri_pos)))
        flat_colour = tuple(cfg.flat_colour) if cfg.interior_colour == 'FLAT' else None
        try:
            P, C, depth, info = pointops.solid_cloud(
                tri_pos, P, C, voxel, cfg.world_anchored, cfg.fill_rule,
                cfg.fill_depth, cfg.include_surface, cfg.grid_mode == 'CENTROID',
                flat_colour)
        except ValueError as e:
            report.append("  %s" % e)
            return None
        report.append("  voxel %.4g, %s rule: lattice %dx%dx%d, %d cells inside, "
                      "%d on the surface, %d interior filled%s"
                      % (voxel, cfg.fill_rule.lower(), info["shape"][0],
                         info["shape"][1], info["shape"][2], info["inside"],
                         info["shell"], info["filled"],
                         (", %d left out by Fill Depth" % info["skipped"])
                         if info["skipped"] else ""))
        if info["filled"] == 0 and cfg.fill_rule == 'CLOSED':
            report.append("  no interior cells - is the mesh closed? An open sheet "
                          "wants the Below Surface rule")
        if len(P) == 0:
            report.append("  nothing to output")
            return None
    finally:
        md = tmp.data
        bpy.data.objects.remove(tmp, do_unlink=True)
        if md.users == 0:
            bpy.data.meshes.remove(md)

    if cfg.radius_mode == 'MANUAL':
        radius = max(cfg.point_radius, 1e-6)
    else:
        radius = max(voxel * cfg.radius_factor, 1e-6)
    report.append("  radius %.4g m, %d points" % (radius, len(P)))
    mat = point_material(cfg)
    obj = build_pointcloud_object(src.name + cfg.suffix, P, C, radius, mat,
                                  src.matrix_world.copy(), src.users_collection,
                                  cfg.attr_name, extra={"depth": (depth, 'FLOAT')})
    if cfg.display_group:
        apply_display_group(obj, mat)
    if getattr(cfg, "volume_group", False):
        apply_volume_group(obj, mat)

    if cfg.export_ply and cfg.ply_path:
        path = bpy.path.abspath(cfg.ply_path)
        try:
            n = pointops.write_ply(path, P, C)
            report.append("  wrote %d points to %s" % (n, path))
        except Exception as e:
            report.append("  PLY export failed: %s" % e)
    return obj


def process(obj, cfg, report):
    if input_kind(obj) == 'POINTS':
        return process_points(obj, cfg, report)   # already a cloud
    if cfg.point_source == 'GRID':
        return process_grid(obj, cfg, report)
    if cfg.point_source == 'VOLUME':
        return process_volume(obj, cfg, report)

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
