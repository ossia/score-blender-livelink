# Adds linear-blend skinning to the PBR Render Pipelines of a .score (score-docs'
# classic PBR one, score-csf-testers' classic_pbr_openpbr), so that
# skinned meshes (rigged Blender characters, the demo's tentacle) follow their
# bones instead of being drawn unskinned:
#
#   python3 add_skinning.py livelink-demo.score
#
# The pipeline of score-docs' 3D examples reads no joints: its PerDraw keeps
# the renderer's transform_slot / skeleton_offset words as padding. This
# names them, declares JOINTS_0 / WEIGHTS_0 (optional, so unskinned meshes
# still bind) and the joint_matrices buffer, and skins position, normal and
# tangent in the vertex stage, as tests/gfx/corpus/rr-scene-skin.vs does.

import json
import re
import sys

SKIN = """
    // Linear-blend skinning: the draw's skeleton_offset is its first entry in
    // joint_matrices, or ~0u for an unskinned mesh.
    mat4 skin = mat4(1.0);
    float wsum = weights_0.x + weights_0.y + weights_0.z + weights_0.w;
    if(pd.skeleton_offset != 0xFFFFFFFFu && wsum > 1e-5)
    {
        uint b = pd.skeleton_offset;
        vec4 w = weights_0 / wsum;
        skin = w.x * joint_matrices.mats[b + joints_0.x]
             + w.y * joint_matrices.mats[b + joints_0.y]
             + w.z * joint_matrices.mats[b + joints_0.z]
             + w.w * joint_matrices.mats[b + joints_0.w];
    }
    vec3 s_position = (skin * vec4(position, 1.0)).xyz;
    vec3 s_normal = mat3(skin) * normal;
    vec3 s_tangent = mat3(skin) * tangent.xyz;
"""


def patch_header(fragment):
    start = fragment.index("/*") + 2
    end = fragment.index("*/")
    header = json.loads(fragment[start:end])

    for t in header.get("TYPES", []):
        if t["NAME"] == "PerDraw":
            names = {"_pad0": "transform_slot", "_pad1": "skeleton_offset"}
            for field in t["LAYOUT"]:
                field["NAME"] = names.get(field["NAME"], field["NAME"])

    # Optional, with a zero default: an unskinned mesh has neither, and
    # without a DEFAULT score refuses to build the pipeline for it (weights
    # have no neutral value of their own). Zero weights skip the skinning.
    inputs = header["VERTEX_INPUTS"]
    for name, ty in (("joints_0", "uvec4"), ("weights_0", "vec4")):
        entry = next((i for i in inputs if i["NAME"] == name), None)
        if entry is None:
            entry = {"TYPE": ty, "NAME": name}
            inputs.append(entry)
        entry["REQUIRED"] = False
        entry["DEFAULT"] = [0, 0, 0, 0]

    if not any(i["NAME"] == "joint_matrices" for i in header["INPUTS"]):
        header["INPUTS"].append(
            {
                "NAME": "joint_matrices",
                "TYPE": "storage",
                "ACCESS": "read_only",
                "VISIBILITY": "vertex",
                "LAYOUT": [{"NAME": "mats", "TYPE": "mat4[]"}],
            }
        )
    return fragment[:start] + json.dumps(header, indent=2) + fragment[end:]


def patch_vertex(vertex):
    if "joint_matrices" in vertex:
        return vertex
    # Wherever the shader reads its draw's PerDraw, however it aligns it.
    anchor = re.search(r"^[ \t]*PerDraw pd\s*=\s*per_draws\.data\[drawId\];[^\n]*\n", vertex, re.M)
    if anchor is None:
        raise SystemExit("no PerDraw read in the vertex stage: cannot add skinning")
    vertex = vertex[: anchor.end()] + SKIN + vertex[anchor.end():]
    vertex = vertex.replace("instLinear * position + inst_translation", "instLinear * s_position + inst_translation")
    vertex = vertex.replace("normalize(normalMatrix * normal)", "normalize(normalMatrix * s_normal)")
    vertex = vertex.replace("normalize(normalMatrix * tangent.xyz)", "normalize(normalMatrix * s_tangent)")
    return vertex


def main(path):
    with open(path) as f:
        doc = json.load(f)
    patched = 0

    def walk(o):
        nonlocal patched
        if isinstance(o, dict):
            if "Vertex" in o and "Fragment" in o and "per_draws" in o["Fragment"]:
                o["Fragment"] = patch_header(o["Fragment"])
                o["Vertex"] = patch_vertex(o["Vertex"])
                patched += 1
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc)
    with open(path, "w") as f:
        json.dump(doc, f)
    print("skinned %d render pipeline(s) in %s" % (patched, path))


if __name__ == "__main__":
    main(sys.argv[1])
