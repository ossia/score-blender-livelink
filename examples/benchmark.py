# Measures what the live link costs Blender on a scene, without score:
#
#   blender -b --factory-startup --disable-autoexec scene.blend \
#       --python benchmark.py [-- --json results.json]
#
# Prints the scene's size, the full export (time and .glb size), the time the
# add-on spends turning one change into records (a material slider, an object
# move, a frame of playback), and adding and deleting an object. Records go to a stub link, so nothing
# is sent anywhere.

import json
import os
import sys
import time

import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import score_livelink as L  # noqa: E402


class _StubLink:
    connected = True
    connection_id = 1
    scenes_sent = 0
    host = "stub"
    port = 0
    last_error = ""

    def __init__(self):
        self.records = 0
        self.glb_size = 0
        self.scenes = 0
        self.structural = 0

    accepts = None  # set below, from link.FRAME_NAMES

    def push_scene(self, seq, glb):
        self.glb_size = len(glb)
        self.scenes += 1

    def push_reset(self, frames):
        self.glb_size = sum(len(f) for f in frames)
        self.scenes += 1

    def push_frames(self, frames):
        self.structural += len(frames)

    def push_patch(self, seq, glb):
        self.glb_size += len(glb)  # the glTF part of a native scene, or a patch
        self.structural += 1

    def push_remove(self, seq, names):
        self.structural += 1

    def push_records(self, frame_type, seq, records):
        self.records += len(records)


def _session():
    s = L._Session.__new__(L._Session)
    s.__dict__.update(
        link=_StubLink(),
        seq=0,
        connection_id=1,
        full_dirty=False,
        check=False,
        structure_dirty=False,
        dirty_objects=set(),
        geometry_dirty=set(),
        dirty_materials=set(),
        scan_all=True,
        signature=None,
        sent={},
        exported={},
        last_full=0.0,
        last_patch=0.0,
        patch_cost=0.0,
        # A stub that accepts everything, like a current score.
        accepts=frozenset(L._link.FRAME_NAMES.values()),
        native=bpy.context.scene.score_livelink.native_geometry,
        images_sent={},
        export_cost=0.0,
        exporting=False,
        last_status=None,
    )
    return s


def _stats(scene):
    depsgraph = bpy.context.evaluated_depsgraph_get()
    verts = tris = 0
    for o in scene.objects:
        if o.type == "MESH":
            me = o.evaluated_get(depsgraph).data
            verts += len(me.vertices)
            tris += sum(len(p.vertices) - 2 for p in me.polygons)
    return {
        "objects": len(scene.objects),
        "meshes": sum(o.type == "MESH" for o in scene.objects),
        "lights": sum(o.type == "LIGHT" for o in scene.objects),
        "materials": sum(1 for m in bpy.data.materials if m.users),
        "images": len(bpy.data.images),
        "vertices": verts,
        "triangles": tris,
    }


def _timed(fn, n):
    """Median of n runs of fn, in ms. fn returns the part it wants timed."""
    times = sorted(fn(i) for i in range(n))
    return times[len(times) // 2] * 1000.0


def main():
    args = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    out_json = args[args.index("--json") + 1] if "--json" in args else None

    L.register()
    context = bpy.context
    scene = context.scene
    s = L._session = _session()
    bpy.app.handlers.depsgraph_update_post.append(L._on_depsgraph_update)

    result = {"file": os.path.basename(bpy.data.filepath), **_stats(scene)}

    exports = []
    for _ in range(3):
        start = time.perf_counter()
        L._send_full(context)
        exports.append(time.perf_counter() - start)
    L._send_values(context)  # the visibility the export forces on
    result["export_ms"] = sorted(exports)[1] * 1000.0
    result["glb_mb"] = s.link.glb_size / 1e6

    def diff(i):
        sent = s.link.records
        start = time.perf_counter()
        if not L._send_values(context):
            raise RuntimeError("the change needed a full export")
        elapsed = time.perf_counter() - start
        if s.link.records == sent and not playing:
            raise RuntimeError("the change sent nothing")
        return elapsed

    playing = False

    # A material of an object in the scene, whose roughness is streamed.
    principled = None
    for m in {slot.material for o in scene.objects for slot in o.material_slots if slot.material}:
        if (p := L.V._surface_principled(m)) is not None and not p.inputs["Roughness"].is_linked:
            principled = p
            break
    if principled is not None:

        def material(i):
            principled.inputs["Roughness"].default_value = 0.3 + 0.01 * (i % 50)
            context.view_layer.update()
            return diff(i)

        result["material_edit_ms"] = _timed(material, 20)

    movable = next((o for o in scene.objects if o.type == "MESH" and L.V.streamable(o)), None)
    if movable is not None:
        x = movable.location.x

        def move(i):
            movable.location.x = x + 0.01 * (i + 1)
            context.view_layer.update()
            return diff(i)

        result["move_ms"] = _timed(move, 20)
        movable.location.x = x
        context.view_layer.update()
        L._send_values(context)

    playing = True  # a frame of a static scene sends nothing

    def frame(i):
        scene.frame_set(scene.frame_start + 1 + i)
        L._on_frame_change(scene)
        return diff(i)

    result["frame_ms"] = _timed(frame, 10)

    # Adding then deleting an object: one tick of the add-on each, which
    # should send a patch and a removal, not the scene.
    scene.score_livelink.min_export_interval = 0.0

    def structural(change):
        scenes, sent = s.link.scenes, s.link.structural
        change()
        context.view_layer.update()
        s.last_patch = 0.0
        start = time.perf_counter()
        L._tick()
        elapsed = (time.perf_counter() - start) * 1000.0
        if s.link.scenes != scenes or s.link.structural == sent:
            raise RuntimeError("the change was not sent as a patch or a removal")
        return elapsed

    def add():
        mesh = bpy.data.meshes.new("BenchCube")
        mesh.from_pydata(
            [(x, y, z) for x in (0, 1) for y in (0, 1) for z in (0, 1)], [],
            [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])
        scene.collection.objects.link(bpy.data.objects.new("BenchCube", mesh))

    result["add_object_ms"] = structural(add)
    result["delete_object_ms"] = structural(lambda: bpy.data.objects.remove(bpy.data.objects["BenchCube"]))

    print("BENCHMARK " + json.dumps(result))
    if out_json:
        with open(out_json, "w") as f:
            json.dump(result, f, indent=2)


main()
