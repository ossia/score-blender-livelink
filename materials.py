# Blender materials as the native scene's JSON describes them (see
# Threedim/LiveLink/Protocol.hpp, native_scene): glTF's metallic-roughness
# model and its extensions, as Blender's glTF exporter would export them.
#
# The Principled BSDF is translated as the exporter does, extensions
# included; through the nodes between it and its image textures the way the
# exporter looks through them (factors from Multiply nodes, channels from
# Separate Color, Mapping nodes as texture transforms). Other shaders, which
# the exporter exports plain white, are approximated: a Diffuse BSDF as a
# rough dielectric of its color, a Glossy as a metal, a Glass as
# transmission, Mix and Add Shader as a blend of what they combine.
#
# Translation never fails: what has no equivalent (a procedural texture, a
# volume) is left out, as the exporter leaves it out.

import math
import os
import struct
import zlib

import bpy
import numpy as np

from . import values as V

# --- images ---------------------------------------------------------------------

_MIME = {"PNG": "image/png", "JPEG": "image/jpeg"}
_encoded = {}  # (image name, signature) -> PNG bytes


def image_key(img):
    return "img:" + img.name


def image_signature(img):
    """Changes when the bytes to send do."""
    path = bpy.path.abspath(img.filepath, library=img.library) if img.filepath else ""
    try:
        mtime = os.path.getmtime(path) if path else 0.0
    except OSError:
        mtime = 0.0
    return (img.filepath, img.packed_file.size if img.packed_file else 0, mtime, tuple(img.size),
            img.source, img.generated_type if img.source == "GENERATED" else "", img.is_dirty)


def _png(pixels, width, height):
    """An 8-bit RGBA PNG of rows given bottom up, as Blender stores them."""
    rows = np.flipud(pixels.reshape(height, width, 4))
    raw = np.zeros((height, width * 4 + 1), np.uint8)
    raw[:, 1:] = rows.reshape(height, width * 4)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))


def image_payload(img):
    """(MIME type, bytes) for an image: an unmodified PNG or JPEG file as it
    is on disk or packed, anything else (generated, painted, EXR, a movie's
    frame) encoded once as PNG. None when it has no pixels."""
    if img.source == "FILE" and img.file_format in _MIME and not img.is_dirty:
        try:
            if img.packed_file is not None:
                return _MIME[img.file_format], bytes(img.packed_file.data)
            with open(bpy.path.abspath(img.filepath, library=img.library), "rb") as f:
                return _MIME[img.file_format], f.read()
        except OSError:
            pass  # missing on disk: maybe loaded, maybe not
    key = (img.name, image_signature(img))
    if key not in _encoded:
        w, h = img.size
        if w == 0 or h == 0:
            return None
        px = np.empty(w * h * img.channels, np.float32)
        img.pixels.foreach_get(px)
        px = px.reshape(-1, img.channels)
        rgba = np.ones((w * h, 4), np.float32)
        rgba[:, : min(3, img.channels)] = px[:, : min(3, img.channels)]
        if img.channels == 1:
            rgba[:, 1] = rgba[:, 2] = rgba[:, 0]
        if img.channels == 4:
            rgba[:, 3] = px[:, 3]
        if img.is_float and img.colorspace_settings.name not in {"Non-Color", "Raw"}:
            # Linear floats; the slot reading this is an sRGB one (colors).
            c = np.clip(rgba[:, :3], 0, 1)
            rgba[:, :3] = np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055)
        data = (np.clip(rgba, 0, 1) * 255 + 0.5).astype(np.uint8)
        _encoded.clear() if len(_encoded) > 256 else None
        _encoded[key] = _png(data, w, h)
    return "image/png", _encoded[key]


# --- following links ----------------------------------------------------------------

_PASS_THROUGH = {"REROUTE", "HUE_SAT", "GAMMA", "BRIGHTCONTRAST", "CURVE_RGB", "INVERT", "RGBTOBW", "CLAMP", "MAP_RANGE", "VALTORGB"}
_WRAP = {"REPEAT": "repeat", "EXTEND": "clamp", "CLIP": "clamp", "MIRROR": "mirror"}


class _Found:
    """What feeds an input: an image (and its texture info), a constant
    factor applied to it, and the channel taken from it."""

    def __init__(self):
        self.image_node = None
        self.factor = None
        self.channel = None
        self.ramp = None  # a Color Ramp met on the way, for procedural inputs


def _linked(socket):
    if socket is None or not socket.is_linked:
        return None
    link = socket.links[0]
    return None if link.is_muted else link


def _first_linked_input(node, types=None):
    for s in node.inputs:
        if s.is_linked and (types is None or s.type in types):
            return s
    return None


def _follow(socket, depth=0):
    """Walks up from an input to an Image Texture, through the nodes the
    exporter looks through."""
    found = _Found()
    link = _linked(socket)
    while link is not None and depth < 16:
        depth += 1
        node, out = link.from_node, link.from_socket
        if node.type == "TEX_IMAGE":
            if node.image is not None:
                found.image_node = node
                if out.name == "Alpha" and found.channel is None:
                    found.channel = "A"
            return found
        if node.type == "MIX" and node.data_type == "RGBA":
            a, b = node.inputs[6], node.inputs[7]
            if node.blend_type == "MULTIPLY" and a.is_linked != b.is_linked:
                const = b if a.is_linked else a
                found.factor = tuple(const.default_value)
            nxt = a if a.is_linked else b if b.is_linked else None
            link = _linked(nxt)
            continue
        if node.type == "MIX_RGB":  # Blender < 3.4 files
            a, b = node.inputs[1], node.inputs[2]
            if node.blend_type == "MULTIPLY" and a.is_linked != b.is_linked:
                const = b if a.is_linked else a
                found.factor = tuple(const.default_value)
            link = _linked(a if a.is_linked else b)
            continue
        if node.type == "MATH":
            a, b = node.inputs[0], node.inputs[1]
            if node.operation == "MULTIPLY" and a.is_linked != b.is_linked:
                found.factor = (b if a.is_linked else a).default_value
            link = _linked(a if a.is_linked else b)
            continue
        if node.type in ("SEPARATE_COLOR", "SEPRGB", "SEPARATE_RGB"):
            found.channel = {"Red": "R", "Green": "G", "Blue": "B", "R": "R", "G": "G", "B": "B"}.get(out.name, "R")
            link = _linked(node.inputs[0])
            continue
        if node.type == "VALTORGB":
            found.ramp = node.color_ramp
            link = _linked(node.inputs[0])
            continue
        if node.type in _PASS_THROUGH or node.type == "NORMAL_MAP":
            link = _linked(_first_linked_input(node, {"RGBA", "VALUE", "VECTOR"}))
            continue
        return found  # procedural, or anything else: no image
    return found


def _uv_index(name, uv_names):
    if not name:
        return 0
    return uv_names.index(name) if name in uv_names[:2] else 0


def _texture_info(node, uv_names):
    """Texture info of an Image Texture node: image key, texture coordinates
    (a UV map, through a Mapping node as a KHR_texture_transform)."""
    info = {"image": image_key(node.image), "uv": 0, "wrap": _WRAP.get(node.extension, "repeat")}
    if node.interpolation == "Closest":
        info["filter"] = "nearest"
    link = _linked(node.inputs.get("Vector"))
    while link is not None:
        src = link.from_node
        if src.type == "MAPPING" and src.vector_type in ("POINT", "TEXTURE"):
            loc = src.inputs["Location"].default_value
            rot = src.inputs["Rotation"].default_value
            scale = src.inputs["Scale"].default_value
            if src.vector_type == "TEXTURE":  # the inverse of a POINT mapping
                scale = [1 / s if s else 1.0 for s in scale]
                loc = [-loc[0] * scale[0], -loc[1] * scale[1]]
                rot = [0, 0, -rot[2]]
            # io_scene_gltf2/blender/com/conversion.py: texture_transform_blender_to_gltf
            r = rot[2]
            info["transform"] = {
                "offset": [loc[0] - scale[1] * math.sin(r), 1 - loc[1] - scale[1] * math.cos(r)],
                "rotation": r,
                "scale": [scale[0], scale[1]],
            }
            link = _linked(src.inputs["Vector"])
            continue
        if src.type == "UVMAP":
            info["uv"] = _uv_index(src.uv_map, uv_names)
        break  # texture coordinates of another kind: the first UV map
    return info


# --- shaders -------------------------------------------------------------------------


def _rgb(socket):
    return list(socket.default_value)[:3]


def _new():
    return {
        "base_color": [1.0, 1.0, 1.0, 1.0], "metallic": 1.0, "roughness": 1.0,
        "emissive": [0.0, 0.0, 0.0], "emissive_strength": 1.0, "textures": {},
    }


class _Translator:
    def __init__(self, uv_names):
        self.uv_names = list(uv_names)
        self.images = {}

    def tex(self, desc, slot, socket, extra=None):
        """Puts the image feeding socket in slot. Returns what was found."""
        f = _follow(socket)
        if f.image_node is not None:
            info = _texture_info(f.image_node, self.uv_names)
            if extra:
                info.update(extra)
            desc["textures"][slot] = info
            self.images[image_key(f.image_node.image)] = f.image_node.image
        return f

    def color(self, desc, key, slot, socket, alpha=None):
        """A color input: its value, or white times a Multiply factor and the
        image feeding it, or, procedural, its Color Ramp's average."""
        if not socket.is_linked:
            desc[key][0:3] = _rgb(socket)
            return
        f = self.tex(desc, slot, socket)
        if f.image_node is not None:
            desc[key][0:3] = list(f.factor[:3]) if isinstance(f.factor, tuple) else [1.0, 1.0, 1.0]
        elif f.ramp is not None:
            els = f.ramp.elements
            desc[key][0:3] = [sum(e.color[c] for e in els) / len(els) for c in range(3)]
        else:
            desc[key][0:3] = [1.0, 1.0, 1.0]  # as the exporter: no factor, no texture

    def scalar(self, socket, default=1.0):
        """A scalar input's value, or the factor applied to the image feeding
        it, or the glTF default."""
        if not socket.is_linked:
            return socket.default_value
        f = _follow(socket)
        return f.factor if isinstance(f.factor, float) else default

    # Principled BSDF ------------------------------------------------------------

    def principled(self, p, mat):
        d = _new()
        get = p.inputs.get
        self.color(d, "base_color", "base_color", get("Base Color"))
        alpha = get("Alpha")
        d["base_color"][3] = alpha.default_value if not alpha.is_linked else 1.0
        d["metallic"] = self.scalar(get("Metallic"))
        d["roughness"] = self.scalar(get("Roughness"))
        # One image holds both (G roughness, B metallic). From two images, the
        # roughness one, which shows more.
        for socket in (get("Roughness"), get("Metallic")):
            if socket.is_linked and "metallic_roughness" not in d["textures"]:
                self.tex(d, "metallic_roughness", socket)
        nm = _linked(get("Normal"))
        if nm is not None and nm.from_node.type == "NORMAL_MAP":
            n = nm.from_node
            f = self.tex(d, "normal", n.inputs["Color"], {"scale": n.inputs["Strength"].default_value})
            if f.image_node is not None and n.uv_map:
                d["textures"]["normal"]["uv"] = _uv_index(n.uv_map, self.uv_names)

        # Emission, normalised to <= 1 with the rest in the strength, as the
        # exporter writes KHR_materials_emissive_strength.
        self.emission(d, get("Emission Color"), get("Emission Strength"))

        # Extensions: what io_scene_gltf2/blender/exp/material/extensions writes.
        tw = get("Transmission Weight")
        if tw is not None and (tw.is_linked or tw.default_value != 0.0):
            d["transmission"] = self.scalar(tw)
            if tw.is_linked:
                self.tex(d, "transmission", tw)
        ior = get("IOR")
        if ior is not None and not ior.is_linked:
            d["ior"] = ior.default_value
        spec, tint = get("Specular IOR Level"), get("Specular Tint")
        if spec is not None and not spec.is_linked:
            fac = spec.default_value * 2.0
            d["specular"] = min(fac, 1.0)
            if tint is not None and not tint.is_linked:
                d["specular_color"] = [c * max(fac, 1.0) for c in _rgb(tint)]
        cw = get("Coat Weight")
        if cw is not None and (cw.is_linked or cw.default_value != 0.0):
            d["clearcoat"] = self.scalar(cw)
            if cw.is_linked:
                self.tex(d, "clearcoat", cw)
            cr = get("Coat Roughness")
            r = self.scalar(cr, 0.0)
            # glTF's default is 0, Blender's 0.03: the exporter writes it only
            # when it is not Blender's.
            d["clearcoat_roughness"] = 0.0 if abs(r - 0.03) <= 1e-5 else r
            cn = _linked(get("Coat Normal"))
            if cn is not None and cn.from_node.type == "NORMAL_MAP":
                self.tex(d, "clearcoat_normal", cn.from_node.inputs["Color"],
                         {"scale": cn.from_node.inputs["Strength"].default_value})
        sw = get("Sheen Weight")
        if sw is not None and (sw.is_linked or sw.default_value != 0.0):
            st = get("Sheen Tint")
            d["sheen_color"] = _rgb(st) if not st.is_linked else [1.0, 1.0, 1.0]
            if st.is_linked:
                self.tex(d, "sheen_color", st)
            d["sheen_roughness"] = self.scalar(get("Sheen Roughness"), 0.0)
        an = get("Anisotropic")
        if an is not None and not an.is_linked and an.default_value != 0.0:
            d["anisotropy"] = an.default_value
            rot = get("Anisotropic Rotation")
            d["anisotropy_rotation"] = (rot.default_value if rot is not None and not rot.is_linked else 0.0) * 2 * math.pi
        tf = get("Thin Film Thickness")
        if tf is not None and not tf.is_linked and tf.default_value > 0.0:
            d["iridescence"] = 1.0
            d["iridescence_thickness"] = [tf.default_value, tf.default_value]
            ti = get("Thin Film IOR")
            if ti is not None and not ti.is_linked:
                d["iridescence_ior"] = ti.default_value

        blended = alpha.is_linked or alpha.default_value < 1.0
        if alpha.is_linked:
            fa = _follow(alpha)
            if fa.image_node is not None and "base_color" not in d["textures"]:
                self.tex(d, "base_color", alpha)
        d["alpha"] = "blend" if blended else "opaque"
        return d

    def emission(self, d, color, strength):
        """Emission color times strength, normalised to <= 1 with the rest in
        the strength, as the exporter writes KHR_materials_emissive_strength."""
        self.color(d, "emissive", "emissive", color)
        k = strength.default_value if strength is not None and not strength.is_linked else 1.0
        f = [c * k for c in d["emissive"]]
        m = max(f)
        d["emissive"], d["emissive_strength"] = ([c / m for c in f], m) if m > 1.0 else (f, 1.0)

    # Other shaders, approximated -------------------------------------------------

    def shader(self, node, mat, depth=0):
        if node is None or depth > 8:
            return _new()
        t = node.type
        if t == "BSDF_PRINCIPLED":
            return self.principled(node, mat)
        get = node.inputs.get
        d = _new()
        if t in ("BSDF_DIFFUSE", "BSDF_TRANSLUCENT", "SUBSURFACE_SCATTERING", "BSDF_TOON", "BSDF_SHEEN", "BSDF_HAIR", "BSDF_HAIR_PRINCIPLED", "BSDF_RAY_PORTAL"):
            if get("Color") is not None:
                self.color(d, "base_color", "base_color", get("Color"))
            d["metallic"], d["roughness"] = 0.0, 1.0
        elif t in ("BSDF_GLOSSY", "BSDF_ANISOTROPIC", "BSDF_METALLIC"):
            if get("Color") is not None:
                self.color(d, "base_color", "base_color", get("Color"))
            elif get("Base Color") is not None:
                self.color(d, "base_color", "base_color", get("Base Color"))
            d["metallic"] = 1.0
            d["roughness"] = self.scalar(get("Roughness"), 0.5) if get("Roughness") is not None else 0.5
        elif t in ("BSDF_GLASS", "BSDF_REFRACTION"):
            self.color(d, "base_color", "base_color", get("Color"))
            d["metallic"] = 0.0
            d["roughness"] = self.scalar(get("Roughness"), 0.0)
            d["transmission"] = 1.0
            if get("IOR") is not None and not get("IOR").is_linked:
                d["ior"] = get("IOR").default_value
        elif t == "EMISSION":
            self.emission(d, get("Color"), get("Strength"))
            d["base_color"][0:3] = [0.0, 0.0, 0.0]
            d["metallic"] = 0.0
        elif t == "BSDF_TRANSPARENT":
            d["base_color"] = [1.0, 1.0, 1.0, 0.0]
            d["alpha"] = "blend"
            d["metallic"] = 0.0
        elif t == "EEVEE_SPECULAR":
            self.color(d, "base_color", "base_color", get("Base Color"))
            d["metallic"] = 0.0
            d["roughness"] = self.scalar(get("Roughness"), 0.5)
            d["specular_color"] = _rgb(get("Specular")) if not get("Specular").is_linked else [1.0, 1.0, 1.0]
            self.emission(d, get("Emissive Color"), None)
            tr = get("Transparency")
            if tr is not None and not tr.is_linked and tr.default_value > 0.0:
                d["base_color"][3] = 1.0 - tr.default_value
                d["alpha"] = "blend"
        elif t in ("MIX_SHADER", "ADD_SHADER"):
            a_link, b_link = _linked(node.inputs[-2]), _linked(node.inputs[-1])
            a = self.shader(a_link.from_node, mat, depth + 1) if a_link else None
            b = self.shader(b_link.from_node, mat, depth + 1) if b_link else None
            if t == "ADD_SHADER":
                return _add(a, b)
            fac = node.inputs[0]
            src = _linked(fac)
            if src is not None and src.from_node.type == "LIGHT_PATH":
                # The camera sees the second shader where the ray type is true.
                w = 1.0 if src.from_socket.name in ("Is Camera Ray",) else 0.0
            elif src is not None:
                w = 0.5  # a mask, a Fresnel: half of each
            else:
                w = fac.default_value
            return _mix(a, b, w)
        elif t == "GROUP" and node.node_tree is not None:
            out = next((n for n in node.node_tree.nodes if n.type == "GROUP_OUTPUT" and n.is_active_output), None)
            sock = out.inputs[0] if out is not None and out.inputs else None
            link = _linked(sock)
            if link is not None and link.from_socket.type == "SHADER":
                return self.shader(link.from_node, mat, depth + 1)
        # Holdout, volumes, anything else: the glTF default.
        return d


def _add(a, b):
    if a is None or b is None:
        return a or b or _new()
    # An emission added to a BSDF, typically: the BSDF, with the emission.
    base, glow = (b, a) if max(a["emissive"]) * a["emissive_strength"] > max(b["emissive"]) * b["emissive_strength"] else (a, b)
    out = dict(base, textures=dict(base["textures"]))
    out["emissive"] = [x + y for x, y in zip(base["emissive"], glow["emissive"])]
    out["emissive_strength"] = max(base["emissive_strength"], glow["emissive_strength"])
    if "emissive" in glow["textures"]:
        out["textures"]["emissive"] = glow["textures"]["emissive"]
    return out


def _mix(a, b, w):
    if a is None or b is None:
        return a or b or _new()
    if w <= 0.0:
        return a
    if w >= 1.0:
        return b
    lerp = lambda x, y: x + (y - x) * w
    out = {}
    for k in set(a) | set(b):
        x, y = a.get(k), b.get(k)
        if k == "textures":
            # The dominant shader's, completed by the other's.
            first, second = (a, b) if w < 0.5 else (b, a)
            out[k] = dict(second["textures"], **first["textures"])
        elif isinstance(x, (int, float)) and isinstance(y, (int, float)):
            out[k] = lerp(x, y)
        elif isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
            out[k] = [lerp(p, q) for p, q in zip(x, y)]
        else:
            out[k] = x if (w < 0.5 and x is not None) or y is None else y
    if a.get("alpha") == "blend" or b.get("alpha") == "blend":
        out["alpha"] = "blend"
    return out


def material_desc(mat, uv_names=()):
    """(description, {image key: image}) of a material on a mesh whose UV
    maps are `uv_names`."""
    tr = _Translator(uv_names)
    d = _new()
    if mat.use_nodes and mat.node_tree is not None:
        out = next((n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL" and n.is_active_output), None)
        if out is None:
            out = next((n for n in mat.node_tree.nodes if n.type == "OUTPUT_MATERIAL"), None)
        link = _linked(out.inputs["Surface"]) if out is not None else None
        if link is not None:
            d = tr.shader(link.from_node, mat)
    else:
        d["base_color"] = list(mat.diffuse_color)
        d["metallic"], d["roughness"] = mat.metallic, mat.roughness
    d.setdefault("alpha", "opaque")
    d["double_sided"] = not mat.use_backface_culling
    if getattr(mat, "blend_method", "OPAQUE") == "CLIP":
        d["alpha"] = "mask"
        d["alpha_cutoff"] = getattr(mat, "alpha_threshold", 0.5)
    # Images that cannot be sent at all are left out, as missing ones are.
    for slot, info in list(d["textures"].items()):
        img = tr.images.get(info["image"])
        if img is None or image_payload(img) is None:
            del d["textures"][slot]
    used = {info["image"] for info in d["textures"].values()}
    return d, {k: v for k, v in tr.images.items() if k in used}
