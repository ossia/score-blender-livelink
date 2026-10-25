# ossia score Live Link: streams the open Blender scene to a "glTF Live Link"
# process in ossia score.
#
# The whole scene goes out as a binary glTF, exported with Blender's own glTF
# exporter, when its geometry, its hierarchy or the structure of a material
# change. Everything else goes out as values that override the last export:
# object transforms, bone poses, shape key weights, visibility, light and
# camera parameters, and the factors of a Principled BSDF's unwired inputs.
# Dragging an object, posing a rig, playing an animation or tweaking a
# roughness costs a few bytes instead of a re-export.
#
# Each streamed value is computed as the exporter computes it (values.py), so
# it lands where the next full export would put it. The add-on keeps what it
# last sent; on every change it diffs the scene against that and sends the
# difference, or exports again when the difference is not a value: a mesh
# edit, a new bone, a material node added, an alpha crossing 1.
#
# Adding, deleting or changing the geometry of a few objects does not export
# the scene again either: deleted objects are removed by name, new or changed
# ones are exported alone and added as a patch (a small .glb), when nothing
# else in the scene depends on them (see _plan_patch).

bl_info = {
    "name": "ossia score Live Link",
    "author": "ossia team",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "View3D > Sidebar > score",
    "description": "Stream the scene live to ossia score as glTF",
    "category": "Import-Export",
}

import os
import time

import bpy
from bpy.app.handlers import persistent

from . import fast_export
from . import link as _link
from . import native as N
from . import values as V

# Data-blocks whose change can only be shown by exporting again.
_FULL_EXPORT_TYPES = tuple(
    getattr(bpy.types, t)
    for t in ("Curve", "Image", "Texture", "PointCloud", "GreasePencil", "Volume", "MetaBall", "GeometryNodeTree")
    if hasattr(bpy.types, t)
)

# Interaction modes in which an object's data is being edited in place.
_EDITING_MODES = {"EDIT", "SCULPT", "VERTEX_PAINT", "WEIGHT_PAINT", "TEXTURE_PAINT", "SCULPT_CURVES", "EDIT_GPENCIL"}


def _round(v):
    if isinstance(v, (tuple, list)):
        return tuple(_round(x) for x in v)
    if isinstance(v, float):
        return round(v, 6)
    return v


def _structure_signature(scene):
    """Per object, what beyond data-block edits changes the exported nodes."""
    return {
        o.name: (
            o.type,
            o.parent.name if o.parent else "",
            o.parent_type,
            o.data.name if o.data else "",
            tuple(s.material.name if s.material else "" for s in o.material_slots),
        )
        for o in scene.objects
    }


class _Session:
    """State of a running link. One per Blender process."""

    def __init__(self, host, port):
        self.link = _link.Link(
            host,
            port,
            _link.hello_info("blender", bpy.app.version_string, bpy.data.filepath),
        )
        self.seq = 0
        self.connection_id = 0
        self.full_dirty = True
        self.check = False  # something changed: diff the values
        self.structure_dirty = False
        # Objects whose geometry changed in a way no value describes.
        self.geometry_dirty = set()
        # What to diff: the objects and materials the depsgraph reported, or
        # everything (a frame change, a scene setting) when scan_all is set.
        self.dirty_objects = set()
        self.dirty_materials = set()
        self.scan_all = True
        self.signature = None
        # Per kind, {key: value} as score has it, and per kind the structure
        # fingerprints of the last export.
        self.sent = {}
        self.exported = {}
        self.last_full = 0.0
        self.export_cost = 0.0
        self.last_patch = 0.0
        self.patch_cost = 0.0
        # Negotiated on each connection (link.accepts): the frame types score
        # takes, whether meshes go native, and the images it has.
        self.accepts = _link.GLTF_ONLY
        self.native = False
        self.images_sent = {}
        self.exporting = False
        self.last_status = None
        self.link.start()

    def next_seq(self):
        self.seq += 1
        return self.seq


_session = None


def _settings(context=None):
    return (context or bpy.context).scene.score_livelink


# --- change detection -------------------------------------------------------
#
# The depsgraph flags are coarse: a shape key slider, a bone rotation and a
# light's energy all flag their object's geometry. So the handler only rules
# out what cannot be a value change, and the tick finds the values by diffing.


def _explained_by_values(obj, batch):
    """Whether a geometry update of a mesh object, in Object Mode, is only
    its shape keys or its armature moving."""
    keys = getattr(obj.data, "shape_keys", None)
    if keys is not None and ("Key", keys.name) in batch:
        return True
    for m in getattr(obj, "modifiers", ()):
        if m.type == "ARMATURE" and m.object is not None and ("Object", m.object.name) in batch:
            return True
    return False


def _users_of(scene, data):
    """The scene's objects whose data, or whose mesh's shape keys, is data."""
    if isinstance(data, bpy.types.Key):
        return [o.name for o in scene.objects if o.type == "MESH" and o.data.shape_keys == data]
    return [o.name for o in scene.objects if o.data == data]


def _materials_of_tree(tree):
    """Names of the materials a shader node tree update may concern, or None
    for all of them: a node group can be used anywhere."""
    if not tree.is_embedded_data:
        return None
    # A material's own tree (the material is usually in the batch too), or a
    # light's or world's, which no material uses.
    return [m.name for m in bpy.data.materials if m.node_tree == tree]


@persistent
def _on_depsgraph_update(scene, depsgraph):
    s = _session
    if s is None or s.exporting:
        return
    # ID names are unique per type only.
    batch = {(type(u.id).__name__ if not isinstance(u.id, bpy.types.Object) else "Object", u.id.name) for u in depsgraph.updates}
    for u in depsgraph.updates:
        idb = u.id
        if isinstance(idb, bpy.types.Object):
            obj = idb.original
            s.check = True
            s.dirty_objects.add(obj.name)
            if u.is_updated_geometry:
                if obj.mode == "EDIT" and s.native:
                    s.geometry_dirty.add(obj.name)  # a native patch, if it can be one
                elif obj.mode in _EDITING_MODES:
                    s.full_dirty = True
                elif obj.type in {"LIGHT", "CAMERA", "ARMATURE", "EMPTY"}:
                    pass  # values, diffed in the tick
                elif not _explained_by_values(obj, batch):
                    s.geometry_dirty.add(obj.name)
            if u.is_updated_transform and not V.streamable(obj):
                s.full_dirty = True
        elif isinstance(idb, bpy.types.Mesh):
            keys = idb.original.shape_keys
            if u.is_updated_geometry and (keys is None or ("Key", keys.name) not in batch):
                s.geometry_dirty.update(_users_of(scene, idb.original))
        elif isinstance(idb, bpy.types.Material):
            s.dirty_materials.add(idb.original.name)
            s.check = True
        elif isinstance(idb, bpy.types.ShaderNodeTree):
            names = _materials_of_tree(idb.original)
            if names is None:
                s.dirty_materials.update(m.name for m in bpy.data.materials)
            else:
                s.dirty_materials.update(names)
            s.check = True
        elif isinstance(idb, bpy.types.World):
            s.check = True
        elif isinstance(idb, (bpy.types.Light, bpy.types.Camera, bpy.types.Armature, bpy.types.Key)):
            # Their objects are usually in the batch as well.
            s.dirty_objects.update(_users_of(scene, idb.original))
            s.check = True
        elif isinstance(idb, (bpy.types.Collection, bpy.types.Scene)):
            # Objects added, removed, renamed, reparented or hidden show up
            # here, but so does every selection change: compare later. A
            # scene setting (the render resolution of a camera's aspect, a
            # view layer's hidden objects) can concern any object.
            s.structure_dirty = True
            s.scan_all = True
            s.check = True
        elif isinstance(idb, _FULL_EXPORT_TYPES):
            s.full_dirty = True


@persistent
def _on_frame_change(scene, *args):
    s = _session
    if s is None or s.exporting:
        return
    s.check = True
    s.scan_all = True
    s.dirty_materials.update(_animated_materials())
    if scene.score_livelink.export_on_frame_change:
        s.full_dirty = True


@persistent
def _on_load_post(*args):
    s = _session
    if s is not None:
        s.full_dirty = True
        s.signature = None


# --- the values score has ------------------------------------------------------


def _animated(idb):
    ad = idb.animation_data if idb is not None else None
    return ad is not None and (ad.action is not None or len(ad.drivers) > 0)


def _animated_materials():
    """Names of the materials a frame change may change: those animated or
    driven, and all of them when a node group is, since any could use it."""
    if any(_animated(g) for g in bpy.data.node_groups):
        return [m.name for m in bpy.data.materials]
    return [m.name for m in bpy.data.materials if _animated(m) or _animated(m.node_tree)]


def _material_fingerprints(materials):
    return {m.name: V.material_structure(m) for m in materials}


def _used_materials(names=None):
    """The materials to diff: those named (when they still exist), or all."""
    if names is None:
        return [m for m in bpy.data.materials if m.users]
    mats = bpy.data.materials
    return [m for m in (mats.get(n) for n in names) if m is not None and m.users]


def _morph_objects(objects, settings):
    return [o for o in objects if V.exports_morph_targets(o, settings.apply_modifiers)]


def _current(context, settings, objects=None, materials=None):
    """The streamed values of objects and materials (default: the whole
    scene), by kind and key, as the next export would write them."""
    scene = context.scene
    view_layer = context.view_layer
    if objects is None:
        objects = scene.objects
    if materials is None:
        materials = _used_materials()
    cur = {k: {} for k in ("xf", "vis", "bones", "weights", "lights", "cams", "mats", "env", "active")}
    for o in objects:
        if V.streamable(o):
            cur["xf"][o.name] = V.local_trs(o)
        cur["vis"][o.name] = V.visible(o, view_layer)
        if o.type == "ARMATURE":
            for bone, trs in V.bone_trs(o).items():
                cur["bones"][(o.name, bone)] = trs
        elif o.type == "LIGHT" and o.data.type in V.LIGHT_TYPES:
            cur["lights"][o.name] = V.light_values(o, settings.light_units)
        elif o.type == "CAMERA" and o.data.type in {"PERSP", "ORTHO"}:
            cur["cams"][o.name] = V.camera_values(o, scene)
    for o in _morph_objects(objects, settings):
        cur["weights"][o.name] = V.morph_weights(o)
    for m in materials:
        cur["mats"][m.name] = V.material_values(m)
    # One world at a time: whatever its name, it is "world" to score.
    cur["env"] = {"world": V.world_values(scene.world)}
    # A scene can hold several cameras; glTF does not say which one renders.
    # Without an active one, the first by name: named explicitly, so that
    # score does not pick another (its default is the first in its tree).
    camera = scene.camera
    if camera is None:
        camera = min((o for o in scene.objects if o.type == "CAMERA"), key=lambda o: o.name, default=None)
    cur["active"] = {"camera": camera.name if camera is not None else ""}
    return cur


def _structure(objects, settings):
    """Fingerprints of what values cannot express, per kind."""
    return {
        "bones": {o.name: V.bone_structure(o) for o in objects if o.type == "ARMATURE"},
        "morph": {o.name: V.shape_key_structure(o) for o in _morph_objects(objects, settings)},
        "lights": {o.name: o.data.type for o in objects if o.type == "LIGHT"},
        "cams": {o.name: V.camera_structure(o) for o in objects if o.type == "CAMERA"},
    }


def _same_structure(now, exported, keys):
    """Whether the fingerprints in now match those exported, for keys (an
    object or material name gaining or losing an entry counts as a change)."""
    for kind, fps in now.items():
        old = exported.get(kind, {})
        for key in keys:
            if fps.get(key) != old.get(key):
                return False
    return True


# --- sending ----------------------------------------------------------------


def _exporter_kwargs(path, settings):
    wanted = dict(
        filepath=path,
        export_format="GLB",
        use_selection=False,
        use_visible=settings.visible_only,
        export_apply=settings.apply_modifiers,
        export_yup=True,
        export_animations=False,
        export_current_frame=True,
        export_rest_position_armature=False,
        export_cameras=True,
        export_lights=True,
        export_extras=False,
        will_save_settings=False,
        check_existing=False,
        export_import_convert_lighting_mode=settings.light_units,
    )
    # Options come and go between exporter versions; drop what this one lacks.
    props = bpy.ops.export_scene.gltf.get_rna_type().properties.keys()
    return {k: v for k, v in wanted.items() if k in props}


class _NotExportable(Exception):
    pass


def _export_glb(context, objects=None):
    """The scene as a .glb, or only `objects` (with the exporter's
    selection-only mode; the selection is put back afterwards)."""
    settings = _settings(context)
    path = os.path.join(bpy.app.tempdir or "/tmp", "score_livelink.glb")
    kwargs = _exporter_kwargs(path, settings)
    if objects is not None:
        kwargs["use_selection"] = True
        view_layer = context.view_layer
        selected = [o for o in view_layer.objects if o.select_get()]
        active = view_layer.objects.active
        for o in selected:
            o.select_set(False)
        # Hidden or unselectable objects cannot be selected: they are shown
        # and made selectable for the export (view layer state, put back
        # right after; their visibility streams to score as any other's).
        hidden = [o for o in objects if o.hide_get(view_layer=view_layer)]
        locked = [o for o in objects if o.hide_select]
        disabled = [o for o in objects if o.hide_viewport]
        try:
            for o in disabled:
                o.hide_viewport = False
            for o in hidden:
                o.hide_set(False, view_layer=view_layer)
            for o in locked:
                o.hide_select = False
            for o in objects:
                o.select_set(True)
            if not all(o.select_get() for o in objects):
                raise _NotExportable("an object cannot be selected")  # in an excluded collection
            return _export_glb_with(context, path, kwargs)
        finally:
            for o in objects:
                o.select_set(False)
            for o in locked:
                o.hide_select = True
            for o in hidden:
                o.hide_set(True, view_layer=view_layer)
            for o in disabled:
                o.hide_viewport = True
            for o in selected:
                o.select_set(True)
            view_layer.objects.active = active
    return _export_glb_with(context, path, kwargs)


def _export_glb_with(context, path, kwargs):
    override = {}
    if context.window is None and context.window_manager.windows:
        # Timers run without a window, and the exporter sets window.scene.
        override["window"] = context.window_manager.windows[0]
    override["scene"] = context.scene

    # An object in Edit Mode has no exportable evaluated mesh, so the exporter
    # leaves the mode itself (io_scene_gltf2/blender/exp/export.py); return to
    # it afterwards, so that the user can keep editing while it streams.
    active = context.view_layer.objects.active
    mode = active.mode if active is not None else "OBJECT"
    with context.temp_override(**override), fast_export.patched():
        try:
            bpy.ops.export_scene.gltf(**kwargs)
        finally:
            if mode != "OBJECT" and active.mode != mode:
                bpy.ops.object.mode_set(mode=mode)

    with open(path, "rb") as f:
        return f.read()


def _send_full(context):
    s = _session
    settings = _settings(context)
    start = time.monotonic()
    s.exporting = True
    try:
        _push_full(context, settings)
        # Leaving and re-entering Edit Mode tags the edited meshes. Evaluate
        # now, while updates are ignored, or that tag would read as an edit
        # and export again.
        context.view_layer.update()
    finally:
        s.exporting = False
    s.export_cost = time.monotonic() - start
    s.last_full = time.monotonic()
    s.full_dirty = False
    s.structure_dirty = False
    s.geometry_dirty.clear()
    s.dirty_objects.clear()
    s.dirty_materials.clear()
    s.signature = _structure_signature(context.scene)

    # The export carries every current value, except visibility: it holds
    # hidden objects too (unless Visible Objects Only), all of them shown.
    s.sent = {k: {key: _round(v) for key, v in d.items()} for k, d in _current(context, settings).items()}
    s.sent["vis"] = {key: True for key in s.sent["vis"]}
    # Not part of the glTF at all.
    s.sent["env"] = {}
    s.sent["active"] = {}
    s.exported = _structure(context.scene.objects, settings)
    s.exported["mats"] = _material_fingerprints(_used_materials())
    # Score has every value but the visibility of hidden objects.
    s.scan_all = True
    s.check = True


def _exported_objects(context, settings):
    """The objects an export holds: all, or the visible ones only."""
    if not settings.visible_only:
        return list(context.scene.objects)
    return [o for o in context.scene.objects if V.visible(o, context.view_layer)]


def _native_frames(context, objects, reset):
    """Encoded native frames (images score lacks, meshes, the native scene)
    for the families of `objects` that can be native, and the objects left
    for glTF."""
    s = _session
    settings = _settings(context)
    native, gltf, materials = N.split(context, objects, settings)
    frames = []
    if native or reset:
        scene, meshes, images = N.build(context, native, materials, settings)
        if reset:
            s.images_sent = {}  # score keeps only what the reset uses
        for key, img in images.items():
            sig = N.image_signature(img)
            if s.images_sent.get(key) != sig:
                payload = N.image_payload(img)
                if payload is not None:
                    frames.append(_link.encode_image(s.next_seq(), key, *payload))
                    s.images_sent[key] = sig
        for key, prims in meshes.items():
            if prims:
                frames.append(_link.encode_mesh(s.next_seq(), key, prims))
        frames.append(_link.encode_native_scene(s.next_seq(), reset, scene))
    return frames, gltf


def _push_full(context, settings):
    """Sends the whole scene: natively where it can, as glTF otherwise."""
    s = _session
    if s.native:
        try:
            frames, rest = _native_frames(context, _exported_objects(context, settings), reset=True)
            glb = _export_glb(context, rest) if rest else None
            s.link.push_reset(frames)
            if glb is not None:
                s.link.push_patch(s.next_seq(), glb)
            return
        except (N.Unsupported, _NotExportable) as e:
            print("score live link: exporting the whole scene as glTF,", e)
        except Exception:  # a bug in the native path: glTF still works
            import traceback
            traceback.print_exc()
            print("score live link: the native channel failed, exporting the whole scene as glTF")
    s.link.push_scene(s.next_seq(), _export_glb(context))


def _instanced_objects(scene):
    """Names of the objects that collection instances show copies of."""
    out = set()
    for o in scene.objects:
        if o.instance_type == "COLLECTION" and o.instance_collection is not None:
            out.update(x.name for x in o.instance_collection.all_objects)
    return out


def _plan_patch(context, old, new, geometry_dirty):
    """How to show a change of the scene's objects without exporting all of
    it: (names to remove, names to export and add), or None when it cannot
    be done that way.

    New objects are exported alone and added; deleted ones are removed; one
    whose geometry or material slots changed is removed and added back. An
    object can be exported alone when its whole family is: its parent and
    children are exported with it (an exported root is added under the
    scene's root, where its transform is the exporter's), its armature too,
    and no collection instance shows a copy of it."""
    if old is None:
        return None
    # Outside Object Mode only native meshes can be sent alone: the exporter
    # leaves the mode to export (see _send_patch).
    if context.mode != "OBJECT" and not _session.native:
        return None
    added = new.keys() - old.keys()
    removed = old.keys() - new.keys()
    kept = new.keys() & old.keys()
    redo = {n for n in kept if new[n] != old[n]} | (geometry_dirty & kept)
    export = added | redo
    if not export:
        return (sorted(removed), [])

    objects = context.scene.objects
    children = {}
    for o in objects:
        if o.parent is not None:
            children.setdefault(o.parent.name, []).append(o.name)
    instanced = _instanced_objects(context.scene)
    for name in export:
        o = objects.get(name)
        if o is None or name in instanced:
            return None
        if o.parent is not None and (o.parent.name not in export or o.parent_type != "OBJECT"):
            return None
        if any(c not in export for c in children.get(name, ())):
            return None
        for m in getattr(o, "modifiers", ()):
            if m.type == "ARMATURE" and m.object is not None and m.object.name not in export:
                return None
    return (sorted(removed | redo), sorted(export))


def _send_patch(context, removed, export, signature):
    s = _session
    settings = _settings(context)
    scene = context.scene
    start = time.monotonic()
    objects = [scene.objects[n] for n in export]
    glb = None
    frames = []
    remove_seq = s.next_seq()  # the removal goes before what replaces it
    if objects:
        s.exporting = True
        try:
            rest = objects
            if s.native:
                frames, rest = _native_frames(context, objects, reset=False)
            if rest and context.mode != "OBJECT":
                raise _NotExportable("glTF objects outside Object Mode")
            if rest:
                glb = _export_glb(context, rest)
            context.view_layer.update()
        except (_NotExportable, N.Unsupported) as e:
            print("score live link: exporting the whole scene,", e)
            s.full_dirty = True
            return
        except Exception:  # a bug in the native path: a full export instead
            import traceback
            traceback.print_exc()
            s.full_dirty = True
            return
        finally:
            s.exporting = False

    s.link.push_remove(remove_seq, removed)
    s.link.push_frames(frames)
    if glb is not None:
        s.link.push_patch(s.next_seq(), glb)
    s.patch_cost = time.monotonic() - start
    s.last_patch = time.monotonic()

    # What score has now: nothing of the removed, the current values of the
    # exported (shown, as in any export), and their materials' structure.
    # Keys are object names, or (armature, bone) for bones.
    gone = set(removed)
    for kind, d in list(s.sent.items()) + list(s.exported.items()):
        if kind not in ("mats", "env", "active"):
            for key in [k for k in d if (k if isinstance(k, str) else k[0]) in gone]:
                del d[key]
    if objects:
        mats = {slot.material for o in objects for slot in o.material_slots if slot.material is not None}
        new_mats = [m for m in mats if m.name not in s.exported["mats"]]
        cur = _current(context, settings, objects, new_mats)
        cur["vis"] = {key: True for key in cur["vis"]}
        del cur["env"], cur["active"]
        for kind, d in cur.items():
            s.sent.setdefault(kind, {}).update({key: _round(v) for key, v in d.items()})
        for kind, fps in _structure(objects, settings).items():
            s.exported.setdefault(kind, {}).update(fps)
        s.exported["mats"].update(_material_fingerprints(new_mats))
        # The hidden ones were exported shown.
        s.dirty_objects.update(export)
        s.check = True

    s.signature = signature
    s.structure_dirty = False
    s.geometry_dirty.clear()


_FRAMES = {
    "xf": _link.FRAME_TRANSFORMS,
    "bones": _link.FRAME_JOINTS,
    "weights": _link.FRAME_WEIGHTS,
    "vis": _link.FRAME_VISIBILITY,
    "lights": _link.FRAME_LIGHTS,
    "cams": _link.FRAME_CAMERAS,
    "mats": _link.FRAME_MATERIALS,
    "env": _link.FRAME_ENVIRONMENT,
    "active": _link.FRAME_ACTIVE_CAMERA,
}


def _send_values(context):
    """Sends what changed since the last export or value update. Returns False
    when a change needs a full export instead."""
    s = _session
    settings = _settings(context)
    scene = context.scene
    s.check = False

    if s.scan_all:
        objects = list(scene.objects)
    else:
        objects = [o for o in (scene.objects.get(n) for n in s.dirty_objects) if o is not None]
    materials = _used_materials(s.dirty_materials)
    s.scan_all = False
    s.dirty_objects.clear()
    s.dirty_materials.clear()

    names = [o.name for o in objects]
    if not _same_structure(_structure(objects, settings), s.exported, names):
        return False
    fingerprints = _material_fingerprints(materials)
    if any(fp != s.exported["mats"].get(name) for name, fp in fingerprints.items()):
        return False

    cur = _current(context, settings, objects, materials)
    if settings.visible_only and any(s.sent["vis"].get(k) != v for k, v in cur["vis"].items()):
        return False  # hidden objects are not in the export at all
    # An object a collection instance shows: its copies are in the scene too,
    # and only a new export moves them.
    moved = [k for k, v in cur["xf"].items() if s.sent.get("xf", {}).get(k) != _round(v)]
    if moved and set(moved) & _instanced_objects(scene):
        return False

    seq = None
    for kind, frame in _FRAMES.items():
        if _link.FRAME_NAMES[frame] not in s.accepts:
            continue
        sent = s.sent.setdefault(kind, {})
        records = []
        for key, value in cur[kind].items():
            r = _round(value)
            if sent.get(key) == r:
                continue
            sent[key] = r
            if kind == "xf" or kind == "bones":
                records.append((key, *value))
            else:
                records.append((key, value))
        if records:
            if seq is None:
                seq = s.next_seq()
            s.link.push_records(frame, seq, records)
    return True


def _status_text(s):
    if s is None:
        return "Stopped"
    if s.link.connected:
        return "Connected to %s:%d (%s)" % (s.link.host, s.link.port, "native" if s.native else "glTF")
    if s.link.last_error:
        return "Waiting for score (%s)" % s.link.last_error
    return "Waiting for score"


def _redraw_panels():
    wm = bpy.context.window_manager
    for window in wm.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()


def _tick():
    s = _session
    if s is None:
        return None
    context = bpy.context
    settings = _settings(context)

    status = (_status_text(s), s.link.scenes_sent)
    if status != s.last_status:
        s.last_status = status
        _redraw_panels()

    if not s.link.connected:
        return 0.25

    if s.link.connection_id != s.connection_id:
        # A new connection: score knows nothing yet, and may not be the same
        # score as before.
        s.connection_id = s.link.connection_id
        s.accepts = s.link.accepts
        s.images_sent = {}
        s.full_dirty = True
    native = settings.native_geometry and {"mesh", "image", "native_scene"} <= s.accepts
    if native != s.native:
        s.native = native
        s.full_dirty = True

    plan = signature = None
    if not s.full_dirty and (s.structure_dirty or s.geometry_dirty):
        signature = _structure_signature(context.scene)
        if signature == s.signature and not s.geometry_dirty:
            s.structure_dirty = False  # a selection change, say
        else:
            can_patch = settings.partial_updates and {"patch", "remove"} <= s.accepts
            plan = _plan_patch(context, s.signature, signature, s.geometry_dirty) if can_patch else None
            if plan is None:
                s.full_dirty = True

    now = time.monotonic()
    try:
        # Exports are spaced by twice their own duration at least, so that a
        # heavy scene being edited keeps Blender responsive.
        if s.full_dirty:
            if now - s.last_full >= max(settings.min_export_interval, 2.0 * s.export_cost):
                _send_full(context)
        elif plan is not None:
            # Same spacing; until then the change stays pending.
            if now - s.last_patch >= max(settings.min_export_interval, 2.0 * s.patch_cost):
                _send_patch(context, *plan, signature)
        elif s.check and not _send_values(context):
            s.full_dirty = True
    except Exception as e:  # keep the timer alive whatever the exporter does
        s.full_dirty = False
        s.link.last_error = "export failed: %s" % e
        print("score live link:", e)

    return 1.0 / max(1.0, settings.rate)


# --- start / stop -------------------------------------------------------------


def start(host, port):
    global _session
    stop()
    _session = _Session(host, port)
    for handlers, fn in (
        (bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
        (bpy.app.handlers.frame_change_post, _on_frame_change),
        (bpy.app.handlers.load_post, _on_load_post),
    ):
        if fn not in handlers:
            handlers.append(fn)
    bpy.app.timers.register(_tick, first_interval=0.0, persistent=True)


def stop():
    global _session
    if bpy.app.timers.is_registered(_tick):
        bpy.app.timers.unregister(_tick)
    for handlers, fn in (
        (bpy.app.handlers.depsgraph_update_post, _on_depsgraph_update),
        (bpy.app.handlers.frame_change_post, _on_frame_change),
        (bpy.app.handlers.load_post, _on_load_post),
    ):
        if fn in handlers:
            handlers.remove(fn)
    if _session is not None:
        _session.link.stop()
        _session.link.join(timeout=3.0)
        _session = None


def is_running():
    return _session is not None


# --- UI -----------------------------------------------------------------------


def _export_option_changed(self, context):
    # The scene score has was exported with the old value.
    if _session is not None:
        _session.full_dirty = True
        _session.last_full = 0.0


class SCORE_PG_livelink(bpy.types.PropertyGroup):
    host: bpy.props.StringProperty(name="Host", default="127.0.0.1")
    port: bpy.props.IntProperty(name="Port", default=9877, min=1, max=65535)
    rate: bpy.props.FloatProperty(
        name="Rate",
        description="How many times per second changes are looked for and sent",
        default=60.0,
        min=1.0,
        max=240.0,
        subtype="FACTOR",
    )
    min_export_interval: bpy.props.FloatProperty(
        name="Min. Export Interval",
        description="Shortest delay between two full glTF exports, in seconds",
        default=0.1,
        min=0.0,
        max=10.0,
        subtype="TIME_ABSOLUTE",
        unit="TIME_ABSOLUTE",
    )
    apply_modifiers: bpy.props.BoolProperty(name="Apply Modifiers", default=True, update=_export_option_changed)
    visible_only: bpy.props.BoolProperty(
        name="Visible Objects Only",
        description="Leave hidden objects out of the exported scene",
        default=False,
        update=_export_option_changed,
    )
    light_units: bpy.props.EnumProperty(
        name="Light Units",
        description="How Blender's light power is converted for score",
        items=(
            ("SPEC", "Physical (glTF)", "Candela and lux, as the glTF standard wants: 1000 W becomes about 54000 cd"),
            ("COMPAT", "Unitless", "Non-physical units: point and spot watts divided by 4\u03c0, sun strength as is"),
            ("RAW", "Raw (Blender)", "Blender's own numbers, unconverted"),
        ),
        default="COMPAT",
        update=_export_option_changed,
    )
    native_geometry: bpy.props.BoolProperty(
        name="Native Geometry",
        description=(
            "Send meshes and images to score directly, without a glTF export, when "
            "score supports it. Objects the native channel cannot represent still "
            "go as glTF"
        ),
        default=True,
        update=_export_option_changed,
    )
    partial_updates: bpy.props.BoolProperty(
        name="Partial Updates",
        description=(
            "Send added, deleted and modified objects alone instead of exporting "
            "the whole scene again, when nothing else depends on them"
        ),
        default=True,
    )
    export_on_frame_change: bpy.props.BoolProperty(
        name="Re-export on Frame Change",
        description=(
            "Export the whole scene on every frame change, for animated geometry "
            "or materials. Transforms are streamed on frame changes regardless"
        ),
        default=False,
    )


class SCORE_OT_livelink_start(bpy.types.Operator):
    bl_idname = "score_livelink.start"
    bl_label = "Start Live Link"
    bl_description = "Connect to ossia score and stream the scene"

    def execute(self, context):
        settings = _settings(context)
        start(settings.host, settings.port)
        return {"FINISHED"}


class SCORE_OT_livelink_stop(bpy.types.Operator):
    bl_idname = "score_livelink.stop"
    bl_label = "Stop Live Link"
    bl_description = "Stop streaming to ossia score"

    def execute(self, context):
        stop()
        return {"FINISHED"}


class SCORE_OT_livelink_resend(bpy.types.Operator):
    bl_idname = "score_livelink.resend"
    bl_label = "Resend Scene"
    bl_description = "Export and send the whole scene again"

    @classmethod
    def poll(cls, context):
        return _session is not None

    def execute(self, context):
        _session.full_dirty = True
        _session.last_full = 0.0
        return {"FINISHED"}


class SCORE_PT_livelink(bpy.types.Panel):
    bl_label = "Live Link"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "score"

    def draw(self, context):
        layout = self.layout
        settings = _settings(context)
        running = _session is not None

        col = layout.column()
        col.enabled = not running
        col.prop(settings, "host")
        col.prop(settings, "port")

        if running:
            row = layout.row(align=True)
            row.operator("score_livelink.stop", icon="PAUSE")
            row.operator("score_livelink.resend", text="", icon="FILE_REFRESH")
        else:
            layout.operator("score_livelink.start", icon="PLAY")

        box = layout.box()
        box.label(text=_status_text(_session), icon="LINKED" if running and _session.link.connected else "UNLINKED")
        if running:
            box.label(text="Scenes sent: %d (last export %.0f ms)" % (_session.link.scenes_sent, _session.export_cost * 1000.0))

        layout.separator()
        col = layout.column()
        col.prop(settings, "rate")
        col.prop(settings, "min_export_interval")
        col.prop(settings, "apply_modifiers")
        col.prop(settings, "visible_only")
        col.prop(settings, "light_units")
        col.prop(settings, "native_geometry")
        col.prop(settings, "partial_updates")
        col.prop(settings, "export_on_frame_change")


_classes = (
    SCORE_PG_livelink,
    SCORE_OT_livelink_start,
    SCORE_OT_livelink_stop,
    SCORE_OT_livelink_resend,
    SCORE_PT_livelink,
)


def register():
    for cls in _classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.score_livelink = bpy.props.PointerProperty(type=SCORE_PG_livelink)


def unregister():
    stop()
    del bpy.types.Scene.score_livelink
    for cls in reversed(_classes):
        bpy.utils.unregister_class(cls)
