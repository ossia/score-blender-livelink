# Puts score-csf-testers' current classic_pbr_openpbr shader in the OpenPBR
# render pipelines of a .score:
#
#   python3 openpbr_shader.py livelink-openpbr.score <score-csf-testers>
#
# A pipeline is taken for an OpenPBR one when its fragment stage reads the
# OpenPBR LUTs; its stages are replaced with presets/rasterizers/
# classic_pbr_openpbr.frag and .vert.

import json
import os
import sys


def main(path, testers):
    base = os.path.join(testers, "presets", "rasterizers", "classic_pbr_openpbr")
    with open(base + ".frag") as f:
        frag = f.read()
    with open(base + ".vert") as f:
        vert = f.read()
    with open(path) as f:
        doc = json.load(f)
    replaced = 0

    def walk(o):
        nonlocal replaced
        if isinstance(o, dict):
            if "Fragment" in o and "Vertex" in o and "openpbr_lut_metal" in o["Fragment"]:
                o["Fragment"], o["Vertex"] = frag, vert
                replaced += 1
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(doc)
    if replaced == 0:
        raise SystemExit("no OpenPBR render pipeline in " + path)
    with open(path, "w") as f:
        json.dump(doc, f)
    print("updated %d OpenPBR render pipeline(s) in %s" % (replaced, path))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
