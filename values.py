# The values the live link streams, computed as Blender's glTF exporter
# (io_scene_gltf2, Y-up) would write them into the .glb, so that a streamed
# value lands where the next full export would put it.
#
# Where the exporter's own helpers are pure functions they are called
# directly; otherwise its logic is transcribed, with the file it comes from.

import math

import bpy
from mathutils import Matrix, Quaternion

# --- transforms -----------------------------------------------------------------

# Rotation the exporter appends to cameras and lights when converting to
# Y-up (io_scene_gltf2/blender/exp/tree.py): Blender's cameras and lights look
# down their local -Z, glTF's down -Z of a Y-up frame.
_CAMERA_LIGHT_CORRECTION = Quaternion((2**0.5 / 2, -(2**0.5) / 2, 0.0, 0.0)).to_matrix().to_4x4()

# What the exporter right-multiplies bone matrices with for Y-up
# (VExportTree.axis_basis_change, io_scene_gltf2/blender/exp/tree.py).
_BONE_AXIS_BASIS = Matrix(((1.0, 0.0, 0.0, 0.0), (0.0, 0.0, 1.0, 0.0), (0.0, -1.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0)))


def _gltf_world(obj):
    m = obj.matrix_world.copy()
    if obj.type in {"CAMERA", "LIGHT"}:
        m = m @ _CAMERA_LIGHT_CORRECTION
    return m


def has_armature_modifier(obj):
    return any(m.type == "ARMATURE" for m in getattr(obj, "modifiers", ()))


def streamable(obj):
    """Whether the exporter's node TRS for obj is what local_trs computes."""
    if obj.instance_type == "COLLECTION" and obj.instance_collection is not None:
        return False
    if has_armature_modifier(obj):
        return False
    parent = obj.parent
    if parent is None:
        return True
    if obj.parent_type != "OBJECT":
        return False
    return parent.type != "ARMATURE" and not has_armature_modifier(parent)


def _decompose(m):
    loc, rot, sca = m.decompose()
    rot.normalize()
    # Same swizzles as io_scene_gltf2/blender/exp/nodes.py and joints.py.
    return (
        (loc[0], loc[2], -loc[1]),
        (rot[1], rot[3], -rot[2], rot[0]),
        (sca[0], sca[2], sca[1]),
    )


def local_trs(obj):
    """glTF node TRS of obj: (translation, rotation xyzw, scale)."""
    m = _gltf_world(obj)
    if obj.parent is not None:
        m = _gltf_world(obj.parent).inverted_safe() @ m
    return _decompose(m)


def bone_trs(arm):
    """{bone name: TRS} of an armature's pose, as glTF joint nodes.

    The exporter (with export_rest_position_armature off, as the live link
    exports) places a bone at armature world @ pose matrix @ axis basis, and
    expresses it relative to its parent bone, or to the armature node for a
    root bone (io_scene_gltf2/blender/exp/tree.py, joints.py).
    """
    arm_world = arm.matrix_world
    world = {pb.name: arm_world @ pb.matrix @ _BONE_AXIS_BASIS for pb in arm.pose.bones}
    out = {}
    for pb in arm.pose.bones:
        parent = world[pb.parent.name] if pb.parent is not None else arm_world
        out[pb.name] = _decompose(parent.inverted_safe() @ world[pb.name])
    return out


def bone_structure(arm):
    """What a pose stream cannot change: the bones, their parents and rests."""
    return tuple(
        (b.name, b.parent.name if b.parent else "", tuple(round(x, 5) for row in b.matrix_local for x in row))
        for b in arm.data.bones
    )


# --- shape keys -------------------------------------------------------------------

try:
    from io_scene_gltf2.blender.com.data_path import get_sk_exported as _get_sk_exported
except ImportError:  # transcribed from io_scene_gltf2/blender/com/data_path.py

    def _get_sk_exported(key_blocks):
        return [
            k
            for i, k in enumerate(key_blocks)
            if i != 0 and not k.mute and k.relative_key != k
        ]


def exports_morph_targets(obj, apply_modifiers):
    """Whether the exporter writes obj's shape keys as morph targets.

    With modifiers applied, it exports the evaluated mesh, which has none
    (io_scene_gltf2/blender/exp/nodes.py, primitive_extract.py)."""
    if obj.type != "MESH" or obj.data.shape_keys is None:
        return False
    if apply_modifiers and len(obj.modifiers) > 0:
        return False
    return len(_get_sk_exported(obj.data.shape_keys.key_blocks)) > 0


def morph_weights(obj):
    return tuple(k.value for k in _get_sk_exported(obj.data.shape_keys.key_blocks))


def shape_key_structure(obj):
    keys = obj.data.shape_keys
    if keys is None:
        return ()
    return tuple((k.name, k.mute, k.relative_key.name) for k in keys.key_blocks)


# --- lights -------------------------------------------------------------------------

LIGHT_TYPES = {"SUN": 0, "POINT": 1, "SPOT": 2}


class _NullLog:
    def warning(self, *args):
        pass

    info = debug = error = warning


try:
    from io_scene_gltf2.blender.exp import light_spots as _gltf_spots
    from io_scene_gltf2.blender.exp import lights as _gltf_lights

    _gather_color = getattr(_gltf_lights, "__gather_color")
    _gather_intensity = getattr(_gltf_lights, "__gather_intensity")
    _gather_inner = getattr(_gltf_spots, "__gather_inner_cone_angle")
    _gather_outer = getattr(_gltf_spots, "__gather_outer_cone_angle")
except (ImportError, AttributeError):
    _gather_color = None


def light_values(obj, lighting_mode):
    """(type, color, intensity, range, inner, outer) as KHR_lights_punctual."""
    lamp = obj.data
    if _gather_color is not None:
        es = {"gltf_lighting_mode": lighting_mode, "current_paths": {}, "log": _NullLog()}
        color = tuple(_gather_color(lamp, es))
        intensity = _gather_intensity(lamp, obj.matrix_world, es)
        if lamp.type == "SPOT":
            inner, outer = _gather_inner(lamp, es), _gather_outer(lamp, es)
        else:
            inner, outer = 0.0, math.pi / 4
    else:
        # io_scene_gltf2/blender/exp/lights.py without the Cycles node path.
        color = tuple(lamp.color)
        if lamp.use_temperature:
            color = tuple(c * t for c, t in zip(color, lamp.temperature_color))
        strength = lamp.energy
        if not lamp.normalize:
            strength *= lamp.area(matrix_world=obj.matrix_world)
        if lighting_mode == "RAW":
            intensity = strength
        else:
            intensity = strength if lamp.type == "SUN" else strength / (4 * math.pi)
            if lighting_mode == "SPEC":
                intensity *= 683
        intensity *= 2**lamp.exposure
        if lamp.type == "SPOT":
            outer = lamp.spot_size * 0.5
            inner = outer - outer * lamp.spot_blend
        else:
            inner, outer = 0.0, math.pi / 4
    rng = lamp.cutoff_distance if getattr(lamp, "use_custom_distance", False) else 0.0
    return (LIGHT_TYPES[lamp.type], color, intensity, rng, inner, outer)


# --- cameras --------------------------------------------------------------------------

try:
    from io_scene_gltf2.blender.com.conversion import yvof_blender_to_gltf as _yfov
except ImportError:  # io_scene_gltf2/blender/com/conversion.py

    def _yfov(angle, width, height, sensor_fit):
        aspect = width / height
        if width >= height:
            return angle if sensor_fit == "VERTICAL" else 2.0 * math.atan(math.tan(angle * 0.5) / aspect)
        return 2.0 * math.atan(math.tan(angle * 0.5) / aspect) if sensor_fit == "HORIZONTAL" else angle


def camera_values(obj, scene):
    """(projection, yfov, aspect, znear, zfar, xmag, ymag), as
    io_scene_gltf2/blender/exp/cameras.py writes them."""
    cam = obj.data
    render = scene.render
    width = render.resolution_x * render.pixel_aspect_x
    height = render.resolution_y * render.pixel_aspect_y
    if cam.type == "ORTHO":
        square = max(width, height)
        return (
            1,
            0.0,
            0.0,
            cam.clip_start,
            cam.clip_end,
            cam.ortho_scale * (width / square) / 2,
            cam.ortho_scale * (height / square) / 2,
        )
    return (0, _yfov(cam.angle, width, height, cam.sensor_fit), width / height, cam.clip_start, cam.clip_end, 0.0, 0.0)


def camera_structure(obj):
    # A panoramic camera is not exported as a glTF camera at all.
    return obj.data.type


# --- materials ----------------------------------------------------------------------

# Field bits of a materials record (Threedim/LiveLink/Protocol.hpp).
MAT_BASE_RGB, MAT_ALPHA, MAT_METALLIC, MAT_ROUGHNESS, MAT_EMISSIVE = 1, 2, 4, 8, 16

_STREAMED_INPUTS = ("Base Color", "Alpha", "Metallic", "Roughness", "Emission Color", "Emission Strength")

# Node properties that only lay the node out in the editor.
_UI_PROPERTIES = {
    "location",
    "width",
    "width_hidden",
    "height",
    "dimensions",
    "select",
    "show_options",
    "show_preview",
    "show_texture",
    "hide",
    "label",
    "color",
    "use_custom_color",
    "name",
    "rna_type",
    "warning_propagation",
    "location_absolute",
}


def _surface_principled(mat):
    """The Principled BSDF wired straight into the active output, if any."""
    if not mat.use_nodes or mat.node_tree is None:
        return None
    for node in mat.node_tree.nodes:
        if node.type == "OUTPUT_MATERIAL" and node.is_active_output:
            surface = node.inputs.get("Surface")
            if surface is None or not surface.is_linked:
                return None
            src = surface.links[0].from_node
            return src if src.type == "BSDF_PRINCIPLED" else None
    return None


def _value(v):
    if hasattr(v, "__len__") and not isinstance(v, str):
        return tuple(round(x, 6) if isinstance(x, float) else x for x in v)
    if isinstance(v, float):
        return round(v, 6)
    if isinstance(v, bpy.types.ID):
        return v.name
    return v


def _tree_fingerprint(tree, skip, seen):
    if tree is None or tree.name in seen:
        return ()
    seen.add(tree.name)
    out = []
    for node in tree.nodes:
        props = []
        for p in node.bl_rna.properties:
            pid = p.identifier
            if pid in _UI_PROPERTIES or pid.startswith("bl_") or p.type == "COLLECTION":
                continue
            try:
                props.append((pid, _value(getattr(node, pid))))
            except (AttributeError, TypeError):
                pass
        inputs = []
        for s in node.inputs:
            if (node.name, s.identifier) in skip and not s.is_linked:
                inputs.append((s.identifier, "streamed"))
            else:
                inputs.append((s.identifier, s.is_linked, _value(getattr(s, "default_value", None))))
        # Some nodes keep their value on an output: Value, RGB.
        outputs = tuple(_value(getattr(s, "default_value", None)) for s in node.outputs)
        out.append((node.bl_idname, node.name, tuple(props), tuple(inputs), outputs))
        if node.type == "GROUP":
            out.append(_tree_fingerprint(node.node_tree, set(), seen))
    links = tuple(
        (l.from_node.name, l.from_socket.identifier, l.to_node.name, l.to_socket.identifier, l.is_muted) for l in tree.links
    )
    return (tuple(out), links)


def material_structure(mat):
    """Everything about mat that a materials record cannot carry.

    Equal fingerprints mean the next export would differ from the last one
    only in the streamed factors."""
    p = _surface_principled(mat)
    skip = set()
    alpha_below_one = False
    if p is not None:
        for name in _STREAMED_INPUTS:
            s = p.inputs.get(name)
            if s is not None:
                skip.add((p.name, s.identifier))
        a = p.inputs.get("Alpha")
        # Alpha below 1 switches the exported alphaMode: that is structure.
        alpha_below_one = a is not None and not a.is_linked and a.default_value < 1.0
    material_props = tuple(
        _value(getattr(mat, k, None))
        for k in ("blend_method", "surface_render_method", "use_backface_culling", "alpha_threshold", "use_nodes")
    )
    return (material_props, alpha_below_one, _tree_fingerprint(mat.node_tree, skip, set()))


def material_values(mat):
    """(fields, base color[4], metallic, roughness, emissive[3], strength) for
    the inputs of mat's Principled BSDF that are not wired to anything: there
    the exporter's factors are the inputs' values
    (io_scene_gltf2/blender/exp/material/pbr_metallic_roughness.py,
    materials.py, extensions/emission.py)."""
    p = _surface_principled(mat)
    fields = 0
    base = [1.0, 1.0, 1.0, 1.0]
    metallic = roughness = 1.0
    emissive = (0.0, 0.0, 0.0)
    strength = 1.0
    if p is None:
        return (fields, tuple(base), metallic, roughness, emissive, strength)

    def unlinked(name):
        s = p.inputs.get(name)
        return s if s is not None and not s.is_linked else None

    if s := unlinked("Base Color"):
        base[0:3] = list(s.default_value)[0:3]
        fields |= MAT_BASE_RGB
    if s := unlinked("Alpha"):
        base[3] = s.default_value
        fields |= MAT_ALPHA
    if s := unlinked("Metallic"):
        metallic = s.default_value
        fields |= MAT_METALLIC
    if s := unlinked("Roughness"):
        roughness = s.default_value
        fields |= MAT_ROUGHNESS
    c, k = unlinked("Emission Color"), unlinked("Emission Strength")
    if c is not None and k is not None:
        f = [x * k.default_value for x in list(c.default_value)[0:3]]
        # Factors above 1 go to KHR_materials_emissive_strength.
        if max(f) > 1.0:
            strength = max(f)
            f = [x / strength for x in f]
        emissive = tuple(f)
        fields |= MAT_EMISSIVE
    return (fields, tuple(base), metallic, roughness, emissive, strength)


# --- visibility -----------------------------------------------------------------------


def visible(obj, view_layer):
    try:
        return obj.visible_get(view_layer=view_layer)
    except (TypeError, RuntimeError):
        return obj.visible_get()


# --- world --------------------------------------------------------------------------
#
# glTF has no world. What is streamed instead is the light it sends from every
# direction, as the scene's ambient light: the Background's color (for an
# environment image, its average over the sphere) times its strength. A
# uniform world of radiance L lights a diffuse surface of albedo a as a * L,
# which is what an ambient term of L gives.

_image_means = {}


def _image_mean(image):
    """Average linear color of an equirectangular image over the sphere."""
    import numpy as np

    w, h = image.size
    key = (image.name, image.filepath, w, h)
    if key in _image_means:
        return _image_means[key]
    mean = (1.0, 1.0, 1.0)
    if w > 0 and h > 0:
        px = np.empty(w * h * image.channels, dtype=np.float32)
        image.pixels.foreach_get(px)
        px = px.reshape(h, w, image.channels)[:, :, :3]
        if not image.is_float and image.colorspace_settings.name == "sRGB":
            px = px**2.2
        # Rows nearer the poles cover less of the sphere.
        lat = (np.arange(h) + 0.5) / h * np.pi - np.pi / 2
        weights = np.cos(lat)
        mean = tuple(float(c) for c in (px.mean(axis=1) * weights[:, None]).sum(axis=0) / weights.sum())
    _image_means[key] = mean
    return mean


def world_values(world):
    """(ambient rgb, strength) of a World."""
    if world is None:
        return ((0.0, 0.0, 0.0), 0.0)
    fallback = (tuple(world.color), 1.0)
    if not world.use_nodes or world.node_tree is None:
        return fallback
    out = next((n for n in world.node_tree.nodes if n.type == "OUTPUT_WORLD" and n.is_active_output), None)
    if out is None or not out.inputs["Surface"].is_linked:
        return ((0.0, 0.0, 0.0), 0.0)
    bg = out.inputs["Surface"].links[0].from_node
    if bg.type != "BACKGROUND":
        return fallback  # a shader mix (e.g. a light path trick): no single value

    color_in, strength_in = bg.inputs["Color"], bg.inputs["Strength"]
    strength = strength_in.default_value if not strength_in.is_linked else 1.0
    if not color_in.is_linked:
        return (tuple(color_in.default_value)[:3], strength)
    src = color_in.links[0].from_node
    if src.type == "RGB":
        return (tuple(src.outputs[0].default_value)[:3], strength)
    if src.type in {"TEX_ENVIRONMENT", "TEX_IMAGE"} and src.image is not None:
        return (_image_mean(src.image), strength)
    return (fallback[0], strength)
