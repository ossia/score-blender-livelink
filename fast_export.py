# A faster glTF export of modified meshes, for the live link's own exports.
#
# For an object with modifiers, Blender's glTF exporter takes the mesh with
# to_mesh(preserve_all_data_layers=True, depsgraph=...)
# (io_scene_gltf2/blender/exp/nodes.py, __gather_mesh). Asking for every data
# layer makes Blender evaluate the modifier stack again from scratch, even
# though the depsgraph holds the evaluated mesh already: on heavy modifiers
# (a Decimate of a 2.5 M face mesh) that is tens of seconds per object, on
# every export. The mesh the depsgraph holds is the same, except that it may
# lack a layer the viewport did not need: it is used when it has every
# attribute and UV map of the original mesh, and the exporter's own call is
# made otherwise. Objects with an Armature modifier keep the exporter's path,
# which turns that modifier off and so needs a new evaluation anyway.
#
# The exporter is patched for the duration of an export only (`patched`), so
# Blender's own File > Export is untouched; if its code is not what this
# expects (another Blender version), nothing is patched.

import contextlib
import inspect
import textwrap

_CALL = "blender_data_owner.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)"
_HELPER = "_score_livelink_to_mesh"
_patched_fn = None
_unavailable = None  # why patching is not possible, once known


def _layers(mesh):
    names = {a.name for a in mesh.attributes if not a.name.startswith(".")}
    return names | {"uv:" + u.name for u in mesh.uv_layers}


def to_mesh(owner, depsgraph):
    """The evaluated mesh as the exporter wants it, without re-evaluating
    when the depsgraph's has everything."""
    original = owner.original
    if not any(m.type == "ARMATURE" for m in original.modifiers):
        mesh = owner.to_mesh()
        if _layers(original.data) <= _layers(mesh):
            return mesh
        owner.to_mesh_clear()
    return owner.to_mesh(preserve_all_data_layers=True, depsgraph=depsgraph)


def _build():
    global _patched_fn, _unavailable
    if _patched_fn is not None or _unavailable is not None:
        return
    try:
        from io_scene_gltf2.blender.exp import nodes
        original = nodes.__dict__["__gather_mesh"]
        src = textwrap.dedent(inspect.getsource(original))
    except (ImportError, KeyError, OSError, TypeError) as e:
        _unavailable = "exporter not found: %s" % e
        return
    if src.count(_CALL) != 1:
        _unavailable = "exporter's __gather_mesh differs from the expected one"
        return
    scope = {}
    exec(compile(src.replace(_CALL, _HELPER + "(blender_data_owner, depsgraph)"), nodes.__file__, "exec"), nodes.__dict__, scope)
    _patched_fn = scope["__gather_mesh"]


@contextlib.contextmanager
def patched():
    """Exports made in this block take evaluated meshes as they are."""
    _build()
    if _patched_fn is None:
        yield False
        return
    from io_scene_gltf2.blender.exp import nodes
    original = nodes.__dict__["__gather_mesh"]
    nodes.__dict__["__gather_mesh"] = _patched_fn
    nodes.__dict__[_HELPER] = to_mesh
    try:
        yield True
    finally:
        nodes.__dict__["__gather_mesh"] = original
        nodes.__dict__.pop(_HELPER, None)


def unavailable_reason():
    _build()
    return _unavailable
