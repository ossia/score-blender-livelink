# The native channel: Blender's own evaluated meshes and its images' files,
# and a JSON scene that refers to them (formats in
# score's src/plugins/score-plugin-threedim/Threedim/LiveLink/Protocol.hpp), instead
# of a glTF export.
#
# It covers what it represents as the glTF exporter would, object family by
# object family (a parentless object and everything under it): meshes,
# curves and text, empties, lights and cameras, with materials as
# materials.py translates them. A family holding anything else goes through
# the glTF exporter instead, as a patch next to the native objects. glTF
# stays the fallback for the whole scene too, with receivers that do not
# take native frames.

import os

import bpy
import numpy as np

from . import values as V


class Unsupported(Exception):
    """Something the native channel does not represent: use glTF."""


from .materials import image_key, image_payload, image_signature, material_desc  # noqa: F401


# --- objects ------------------------------------------------------------------------

_GEOMETRY = {"MESH", "CURVE", "FONT", "SURFACE", "META"}


def instancers(depsgraph):
    """Names of the objects whose evaluation instances others (geometry
    nodes, particles, vertex or face instancing)."""
    return {i.parent.original.name for i in depsgraph.object_instances if i.is_instance and i.parent is not None}


def armature_of(o):
    """The armature deforming o, if any (its first Armature modifier's)."""
    for m in getattr(o, "modifiers", ()):
        if m.type == "ARMATURE" and m.object is not None:
            return m.object
    return None


def check_object(o, names, apply_modifiers, instancing, material_cache):
    """Raises Unsupported when o cannot be sent natively among `names`."""
    if o.parent is not None and o.parent_type not in ("OBJECT", "BONE"):
        raise Unsupported(f"{o.name}: parented to a {o.parent_type.lower()}")
    if o.parent_type == "BONE" and (o.parent.type != "ARMATURE" or o.parent_bone not in o.parent.pose.bones):
        raise Unsupported(f"{o.name}: parented to a missing bone")
    if o.type not in _GEOMETRY:
        return  # empties, lights, cameras, armatures; lattices, probes and the like as plain nodes, as the exporter writes them
    if V.has_armature_modifier(o):
        arm = armature_of(o)
        if arm is None or arm.name not in names or o.type != "MESH":
            raise Unsupported(f"{o.name}: skinned to an armature not sent with it")
    uv_names = tuple(l.name for l in o.data.uv_layers) if o.type == "MESH" else ()
    for slot in o.material_slots:
        if slot.material is None:
            continue
        key = (slot.material.name, uv_names)
        if key not in material_cache:
            material_cache[key] = material_desc(slot.material, uv_names)


def families(objects):
    """The parentless objects of `objects`, each with everything under it."""
    names = {o.name for o in objects}
    for o in objects:
        if o.parent is None or o.parent.name not in names:
            yield [o] + [c for c in o.children_recursive if c.name in names]


def split(context, objects, settings):
    """(native objects, glTF objects, materials) for `objects`, by family."""
    depsgraph = context.evaluated_depsgraph_get()
    instancing = instancers(depsgraph)
    materials = {}
    names = {o.name for o in objects}
    native, gltf = [], []
    for family in families(objects):
        try:
            cache = {}
            for o in family:
                check_object(o, names, settings.apply_modifiers, instancing, cache)
            materials.update(cache)
            native.extend(family)
        except Unsupported as e:
            print("score live link: glTF for", family[0].name, "-", e)
            gltf.extend(family)
    return native, gltf, materials


# --- meshes ---------------------------------------------------------------------------

_GEOMETRY = {"MESH", "CURVE", "FONT", "SURFACE", "META"}

# Blender's Z-up to glTF's Y-up, as the exporter converts: (x, y, z) -> (x, z, -y).
_C = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], np.float32)


def mesh_key(o, apply_modifiers):
    """Objects sharing unmodified mesh data share one mesh; skinned meshes,
    sent in world space, are their object's."""
    if o.type == "MESH" and not V.has_armature_modifier(o) and (not apply_modifiers or len(o.modifiers) == 0):
        return "data:" + o.data.name
    return "obj:" + o.name


def _corner_normals(me):
    n = np.empty(len(me.loops) * 3, np.float32)
    me.corner_normals.foreach_get("vector", n)
    return n.reshape(-1, 3)


def _skin_weights(o, me, joint_of):
    """(joints, weights), 4 per vertex: the vertex groups of deforming bones,
    the 4 heaviest, normalised, as the exporter writes JOINTS_0/WEIGHTS_0."""
    groups = {g.index: joint_of[g.name] for g in o.vertex_groups if g.name in joint_of}
    nv = len(me.vertices)
    joints = np.zeros((nv, 4), np.uint16)
    weights = np.zeros((nv, 4), np.float32)
    for v in me.vertices:
        ws = sorted(((g.weight, groups[g.group]) for g in v.groups if g.group in groups and g.weight > 0.0), reverse=True)[:4]
        for k, (w, j) in enumerate(ws):
            joints[v.index, k] = j
            weights[v.index, k] = w
    total = weights.sum(axis=1, keepdims=True)
    np.divide(weights, total, out=weights, where=total > 0)
    return joints, weights


def _morph_deltas(me, keys):
    """Per shape key: position deltas per vertex and normal deltas per corner,
    from the mesh (the basis) to the key, in Blender's axes."""
    pos = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", pos)
    pos = pos.reshape(-1, 3)
    nrm = _corner_normals(me)
    out = []
    for kb in keys:
        co = np.empty(len(kb.data) * 3, np.float32)
        kb.data.foreach_get("co", co)
        dn = None
        try:
            dn = np.asarray(kb.normals_split_get(), np.float32).reshape(-1, 3) - nrm
        except (AttributeError, ValueError):
            pass
        out.append((co.reshape(-1, 3) - pos, dn))
    return out


def mesh_primitives(me, slot_count, matrix=None, skin=None, morphs=()):
    """One primitive per material slot used: per-corner vertex streams in glTF
    axes. `matrix` (4x4) moves positions and normals first (a skinned mesh's
    world matrix); `skin`, (joints, weights) per vertex; `morphs`, per target
    (position deltas per vertex, normal deltas per corner)."""
    me.calc_loop_triangles()
    nt = len(me.loop_triangles)
    if nt == 0:
        return [], []
    pos = np.empty(len(me.vertices) * 3, np.float32)
    me.vertices.foreach_get("co", pos)
    pos = pos.reshape(-1, 3)
    nrm = _corner_normals(me)
    # Positions (and position deltas) by the linear part `lin` (and the
    # translation), normals (and their deltas) by its inverse transpose.
    lin, normal_lin, shift = _C, _C, np.zeros(3, np.float32)
    if matrix is not None:
        m = np.array(matrix, np.float32)
        lin = _C @ m[:3, :3]
        normal_lin = _C @ np.linalg.inv(m[:3, :3]).T
        shift = _C @ m[:3, 3]
    pos = pos @ lin.T + shift
    nrm = nrm @ normal_lin.T
    if matrix is not None:
        lens = np.linalg.norm(nrm, axis=1, keepdims=True)
        np.divide(nrm, lens, out=nrm, where=lens > 0)
    vid = np.empty(len(me.loops), np.int32)
    me.loops.foreach_get("vertex_index", vid)
    uvs = []
    for layer in list(me.uv_layers)[:2]:
        uv = np.empty(len(me.loops) * 2, np.float32)
        layer.data.foreach_get("uv", uv)
        uv = uv.reshape(-1, 2)
        uv[:, 1] = 1.0 - uv[:, 1]  # glTF's V runs downwards
        uvs.append(uv)
    tri = np.empty(nt * 3, np.int32)
    me.loop_triangles.foreach_get("loops", tri)
    tri = tri.reshape(-1, 3)
    mat = np.empty(nt, np.int32)
    me.loop_triangles.foreach_get("material_index", mat)
    np.clip(mat, 0, max(slot_count, 1) - 1, out=mat)
    morphs = [(d @ lin.T, None if n is None else n @ normal_lin.T) for d, n in morphs]

    nv = len(pos)
    prims, slots = [], []
    for slot in np.unique(mat):
        corners = tri[mat == slot].reshape(-1)
        rep, indices = _merge_corners(corners, vid, nv, [nrm] + uvs + [n for _, n in morphs if n is not None])
        verts = vid[rep]
        p = {
            "vertex_count": len(rep),
            "positions": np.ascontiguousarray(pos[verts]),
            "normals": np.ascontiguousarray(nrm[rep]),
            "indices": indices,
        }
        if uvs:
            p["texcoords"] = np.ascontiguousarray(uvs[0][rep])
        if len(uvs) > 1:
            p["texcoords1"] = np.ascontiguousarray(uvs[1][rep])
        if skin is not None:
            p["joints"] = np.ascontiguousarray(skin[0][verts])
            p["weights"] = np.ascontiguousarray(skin[1][verts])
        if morphs:
            p["targets"] = [
                {"positions": np.ascontiguousarray(d[verts]),
                 "normals": None if n is None else np.ascontiguousarray(n[rep])}
                for d, n in morphs
            ]
        prims.append(p)
        slots.append(int(slot))
    return prims, slots


def _merge_corners(corners, vid, nv, corner_attrs):
    """(the corner each vertex of the primitive takes its attributes from,
    uint32 indices of the primitive's corners): a corner shares the vertex
    of the first corner of its mesh vertex when all its per-corner
    attributes (normal, UVs) are equal to that corner's, else it gets a
    vertex of its own. Exact, and linear but for one sort."""
    verts = vid[corners]
    first = np.full(nv, -1, np.int64)
    first[verts[::-1]] = corners[::-1]  # the first corner of each vertex wins
    base = first[verts]
    same = np.ones(len(corners), bool)
    for a in corner_attrs:
        same &= np.all(a[corners] == a[base], axis=1)
    used = np.unique(verts)
    vmap = np.full(nv, -1, np.int64)
    vmap[used] = np.arange(len(used))
    extra = ~same
    indices = vmap[verts]
    indices[extra] = len(used) + np.arange(int(extra.sum()))
    rep = np.concatenate([first[used], corners[extra]])
    return rep, indices.astype(np.uint32)


def _rest_mesh(context, o, depsgraph):
    """A skinned mesh as the exporter exports it: modifiers applied, but its
    Armature modifiers, which the skin replaces, off."""
    others = [m for m in o.modifiers if m.type != "ARMATURE" and m.show_viewport]
    if not others:
        return o.data, None
    arm_mods = [m for m in o.modifiers if m.type == "ARMATURE" and m.show_viewport]
    for m in arm_mods:
        m.show_viewport = False
    try:
        context.view_layer.update()
        e = o.evaluated_get(context.evaluated_depsgraph_get())
        return e.to_mesh(), e
    finally:
        for m in arm_mods:
            m.show_viewport = True


def object_mesh(context, o, depsgraph, apply_modifiers, skin=None):
    """(primitives, material slot of each) of o's mesh as exported. `skin`:
    {bone name: joint index} of its armature, for a skinned mesh."""
    slots = len(o.material_slots)
    if o.type == "MESH" and o.mode == "EDIT":
        o.update_from_editmode()  # the edit mesh into o.data
    morph = o.type == "MESH" and V.exports_morph_targets(o, apply_modifiers)
    if skin is not None or morph:
        # The basis (shape keys) or the rest pose (skins), from the mesh data.
        me, owner = (o.data, None) if (morph or not apply_modifiers) else _rest_mesh(context, o, depsgraph)
        try:
            keys = [k for k in V._get_sk_exported(o.data.shape_keys.key_blocks)] if morph else []
            return mesh_primitives(
                me, slots,
                matrix=o.matrix_world if skin is not None else None,
                skin=_skin_weights(o, me, skin) if skin is not None else None,
                morphs=_morph_deltas(me, keys) if keys else ())
        finally:
            if owner is not None:
                owner.to_mesh_clear()
                context.view_layer.update()
    if o.type == "MESH" and not apply_modifiers:
        return mesh_primitives(o.data, slots)
    e = o.evaluated_get(depsgraph)
    if o.type == "MESH" and o.mode != "EDIT" and e.is_evaluated:
        return mesh_primitives(e.data, slots)
    # Not evaluated (disabled in viewports, which the exporter exports all
    # the same), curves, text, and meshes in Edit Mode (whose evaluated mesh
    # wraps the edit mesh, without corner data of its own): a mesh made for
    # the purpose.
    if not e.is_evaluated:
        me = o.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)
        try:
            return mesh_primitives(me, slots)
        finally:
            o.to_mesh_clear()
    me = e.to_mesh()
    try:
        return mesh_primitives(me, slots)
    finally:
        e.to_mesh_clear()


# --- scenes ------------------------------------------------------------------------------


def _inverse_binds(arm):
    """Per bone, the exporter's inverse bind matrix, column-major:
    (Y-up basis change @ armature world @ bone rest)^-1
    (io_scene_gltf2/blender/exp/skins.py)."""
    from mathutils import Matrix

    basis = Matrix(((1, 0, 0, 0), (0, 0, 1, 0), (0, -1, 0, 0), (0, 0, 0, 1)))
    out = []
    for b in arm.data.bones:
        m = (basis @ (arm.matrix_world @ b.matrix_local)).inverted_safe()
        out.extend(m[r][c] for c in range(4) for r in range(4))
    return out


# Instances beyond this many under one object are not sent natively.
MAX_INSTANCES = 200000


def build(context, objects, materials, settings):
    """(scene JSON, meshes {key: primitives}, images {key: image}) for
    objects. Nodes come parents first and name their parent by index."""
    scene = context.scene
    depsgraph = context.evaluated_depsgraph_get()
    apply = settings.apply_modifiers
    meshes, slots_of, images = {}, {}, {}
    nodes = []
    index = {}  # scene object name -> node index
    # JSON key of each (material, UV maps) description: the material's name,
    # suffixed when its UV maps make it a different one on another mesh.
    json_keys, mats = {}, {}

    def material_key(mat, uv_names):
        key = (mat.name, uv_names)
        if key not in json_keys:
            if key not in materials:  # an instanced object's
                materials[key] = material_desc(mat, uv_names)
            name = mat.name if mat.name not in mats else f"{mat.name}#{len(json_keys)}"
            desc, imgs = materials[key]
            mats[name] = dict(desc, name=mat.name)
            images.update(imgs)
            json_keys[key] = name
        return json_keys[key]

    def slot_materials(o, key):
        uv_names = tuple(l.name for l in o.data.uv_layers) if o.type == "MESH" else ()
        return [
            (material_key(o.material_slots[i].material, uv_names)
             if i < len(o.material_slots) and o.material_slots[i].material else "")
            for i in slots_of[key]
        ]

    def add_node(name, parent, trs):
        t, r, s = trs
        nodes.append({"name": name, "parent": parent, "t": list(t), "r": list(r), "s": list(s), "visible": True})
        return nodes[-1]

    skins = []  # (node, armature): filled once every node has its index
    bone_nodes = {}  # (armature object, bone) -> node index

    def emit(o, name, parent, trs, skinned=True):
        """The node of an object, its content, and what it instances."""
        node = add_node(name, parent, trs)
        me_index = len(nodes) - 1
        if o.type in _GEOMETRY:
            arm = armature_of(o) if skinned and V.has_armature_modifier(o) else None
            joint_of = {b.name: i for i, b in enumerate(arm.data.bones)} if arm is not None else None
            key = mesh_key(o, apply)
            if key not in meshes:
                meshes[key], slots_of[key] = object_mesh(context, o, depsgraph, apply, joint_of)
            if meshes[key]:
                node["mesh"] = key
                node["materials"] = slot_materials(o, key)
                if o.type == "MESH" and V.exports_morph_targets(o, apply):
                    node["weights"] = list(V.morph_weights(o))
                if arm is not None:
                    skins.append((node, arm))
        elif o.type == "ARMATURE":
            # Bones as nodes under the armature's, as the exporter makes them,
            # with the transforms bone poses stream as.
            trs_of = V.bone_trs(o)
            bone_index = {}
            for pb in sorted(o.pose.bones, key=lambda b: len(b.parent_recursive)):  # parents first
                bone_index[pb.name] = len(nodes)
                bone_nodes[(o.name, pb.name)] = len(nodes)
                add_node(pb.name, bone_index[pb.parent.name] if pb.parent is not None else me_index, trs_of[pb.name])
        elif o.type == "LIGHT" and o.data.type in V.LIGHT_TYPES:
            ty, color, intensity, rng, inner, outer = V.light_values(o, settings.light_units)
            node["light"] = [ty, *color, intensity, rng, inner, outer]
        elif o.type == "CAMERA" and o.data.type in {"PERSP", "ORTHO"}:
            node["camera"] = list(V.camera_values(o, scene))
        if o.instance_type == "COLLECTION" and o.instance_collection is not None:
            emit_collection(o.instance_collection, me_index, name + "/", 0)
        return me_index

    def emit_collection(coll, parent, prefix, depth):
        """A collection instance's copies: its objects under the instancer's
        node, moved by the collection's offset, sharing the originals'
        meshes. Named after the instancer, so that values streamed for the
        originals do not land on them."""
        if depth > 8:
            return
        members = list(coll.all_objects)
        inside = {o.name for o in members}
        offset = coll.instance_offset
        copy = {}
        for o in sorted(members, key=_depth):
            if o.parent is not None and o.parent.name in inside:
                trs = V.local_trs(o)
                p = copy[o.parent.name]
            else:
                from mathutils import Matrix
                m = Matrix.Translation(-offset) @ V._gltf_world(o)
                trs = V._decompose(m)
                p = parent
            # A rig in an instance is drawn as it stands: no skin.
            copy[o.name] = emit(o, prefix + o.name, p, trs, skinned=False)

    for o in sorted(objects, key=_depth):  # parents first
        parent, trs = -1, V.local_trs(o)
        if o.parent is not None and o.parent_type == "BONE" and (o.parent.name, o.parent_bone) in bone_nodes:
            # Under the bone's node: relative to the bone as bone poses place
            # it (armature world @ pose matrix @ axis basis).
            pb = o.parent.pose.bones[o.parent_bone]
            bone_world = o.parent.matrix_world @ pb.matrix @ V._BONE_AXIS_BASIS
            parent = bone_nodes[(o.parent.name, o.parent_bone)]
            trs = V._decompose(bone_world.inverted_safe() @ V._gltf_world(o))
        elif o.parent is not None and o.parent.name in index:
            parent = index[o.parent.name]
        index[o.name] = emit(o, o.name, parent, trs)

    # Instances the depsgraph makes (particles, vertex and face instancing,
    # geometry nodes): one node each under their instancer's, sharing the
    # mesh of what they instance. Read while iterating: an instance's object
    # only lives until the next one.
    from mathutils import Matrix
    counts = {}
    for inst in depsgraph.object_instances:
        if not inst.is_instance or inst.parent is None:
            continue
        owner = inst.parent.original.name
        if owner not in index or inst.parent.original.instance_type == "COLLECTION":
            continue  # collection instances: emit_collection's
        ob = inst.instance_object
        if ob.type not in _GEOMETRY:
            continue
        n = counts.get(owner, 0)
        if n >= MAX_INSTANCES:
            continue
        counts[owner] = n + 1
        original = ob.original
        # A real object (particles, vertex instancing, Object Info): its mesh,
        # shared with it. Geometry the nodes made: a mesh of its own.
        if original is not None and original.name != owner and original.name in bpy.data.objects and original.data is not None:
            key = mesh_key(original, apply)
            source = original
        else:
            key = "gn:%s:%x" % (owner, ob.data.as_pointer())
            source = None
        if key not in meshes:
            if source is not None:
                meshes[key], slots_of[key] = object_mesh(context, source, depsgraph, apply)
            elif ob.type == "MESH":
                meshes[key], slots_of[key] = mesh_primitives(ob.data, len(ob.material_slots))
            else:  # a curve, text: as a mesh
                me = ob.to_mesh()
                try:
                    meshes[key], slots_of[key] = mesh_primitives(me, len(ob.material_slots))
                finally:
                    ob.to_mesh_clear()
        if not meshes[key]:
            continue
        world = inst.parent.matrix_world
        local = V._decompose(world.inverted_safe() @ inst.matrix_world)
        node = add_node("%s.%d" % (owner, n), index[owner], local)
        node["mesh"] = key
        node["materials"] = slot_materials(source if source is not None else ob, key)

    for node, arm in skins:
        node["skin"] = {
            "armature": index[arm.name],
            "joints": [b.name for b in arm.data.bones],
            "inverse_bind": _inverse_binds(arm),
        }
    return {"nodes": nodes, "materials": mats}, meshes, images


def _depth(o):
    d = 0
    while o.parent is not None:
        o, d = o.parent, d + 1
    return d
