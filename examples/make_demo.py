# Builds livelink-demo.blend, a scene to try the score Live Link with:
#
#   blender -b --factory-startup --python make_demo.py -- livelink-demo.blend
#
# Everything the link carries is in it and animated, so that pressing Play in
# Blender streams each kind of value to score frame by frame: object
# transforms (a parented hierarchy), bones (a skinned tentacle), shape keys
# (a squashing blob), visibility (a blinking ring), light color, camera zoom
# and a material's base color. Textured PBR materials, a sun and a point light
# come with the scene itself.

import math
import sys

import bpy

out = sys.argv[sys.argv.index("--") + 1] if "--" in sys.argv else "livelink-demo.blend"

bpy.ops.wm.read_factory_settings(use_empty=True)
scene = bpy.context.scene
scene.frame_start, scene.frame_end = 1, 120
scene.render.fps = 30
# Constant-speed loops; set here rather than per F-curve, which Blender 4.4+
# keeps inside layered actions.
bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"


def material(name, color=(0.8, 0.8, 0.8), metallic=0.0, roughness=0.5, emission=None, image=None):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    p = nt.nodes["Principled BSDF"]
    p.inputs["Base Color"].default_value = (*color, 1.0)
    p.inputs["Metallic"].default_value = metallic
    p.inputs["Roughness"].default_value = roughness
    if emission:
        p.inputs["Emission Color"].default_value = (*emission[0], 1.0)
        p.inputs["Emission Strength"].default_value = emission[1]
    if image:
        tex = nt.nodes.new("ShaderNodeTexImage")
        tex.image = image
        nt.links.new(tex.outputs["Color"], p.inputs["Base Color"])
    return m


def active():
    return bpy.context.view_layer.objects.active


# Floor with an image texture. A generated image is stored as its recipe in
# the .blend, and exported as a PNG.
grid = bpy.data.images.new("Grid", 1024, 1024)
grid.generated_type = "COLOR_GRID"
bpy.ops.mesh.primitive_plane_add(size=12)
floor = active()
floor.name = "Floor"
floor.data.materials.append(material("FloorMat", roughness=0.7, image=grid))

# Gold monkey, turning once per loop.
bpy.ops.mesh.primitive_monkey_add(location=(0, 0, 1.2))
monkey = active()
monkey.name = "Monkey"
monkey.modifiers.new("Subdiv", "SUBSURF").levels = 2
bpy.ops.object.shade_smooth()
monkey.data.materials.append(material("Gold", (1.0, 0.75, 0.3), metallic=1.0, roughness=0.25))
monkey.rotation_euler = (0, 0, 0)
monkey.keyframe_insert("rotation_euler", frame=1)
monkey.rotation_euler = (0, 0, 2 * math.pi)
monkey.keyframe_insert("rotation_euler", frame=121)

# A bouncing red ball.
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.5, location=(2.5, 0, 0.5))
ball = active()
ball.name = "Ball"
bpy.ops.object.shade_smooth()
ball.data.materials.append(material("Red", (0.9, 0.08, 0.05), roughness=0.3))
for f, z in ((1, 0.5), (30, 2.5), (60, 0.5), (90, 2.5), (120, 0.5)):
    ball.location.z = z
    ball.keyframe_insert("location", index=2, frame=f)

# A glowing ring orbiting the monkey: the ring is a child of a turning empty,
# so it also exercises parent-relative transforms.
orbit = bpy.data.objects.new("Orbit", None)
scene.collection.objects.link(orbit)
orbit.location = (0, 0, 1.2)
orbit.keyframe_insert("rotation_euler", index=2, frame=1)
orbit.rotation_euler.z = -2 * math.pi
orbit.keyframe_insert("rotation_euler", index=2, frame=121)
bpy.ops.mesh.primitive_torus_add(location=(0, -2.5, 1.2), major_radius=0.45, minor_radius=0.12)
ring = active()
ring.name = "Ring"
ring.rotation_euler = (math.pi / 2, 0, 0)
ring.data.materials.append(material("Glow", (0.1, 0.4, 1.0), emission=((0.2, 0.6, 1.0), 1.5)))
ring.parent = orbit
ring.matrix_parent_inverse = orbit.matrix_world.inverted()

# Visibility: the ring blinks off for a quarter of each loop.
for f, hidden in ((1, False), (60, True), (90, False)):
    ring.hide_viewport = hidden
    ring.keyframe_insert("hide_viewport", frame=f)

# The ball's color drifts from red to orange and back: material values.
red = bpy.data.materials["Red"].node_tree.nodes["Principled BSDF"].inputs["Base Color"]
for f, c in ((1, (0.9, 0.08, 0.05, 1)), (60, (1.0, 0.45, 0.0, 1)), (120, (0.9, 0.08, 0.05, 1))):
    red.default_value = c
    red.keyframe_insert("default_value", frame=f)

# Shape keys: a blob squashing flat and back. No modifiers on it: applied
# modifiers would drop its shape keys from the export.
bpy.ops.mesh.primitive_uv_sphere_add(radius=0.6, location=(-1.2, -3.2, 0.6), segments=32, ring_count=16)
blob = active()
blob.name = "Blob"
bpy.ops.object.shade_smooth()
blob.data.materials.append(material("Mint", (0.2, 0.85, 0.6), roughness=0.4))
blob.shape_key_add(name="Basis")
squash = blob.shape_key_add(name="Squash")
for v in squash.data:
    v.co.z = 0.6 + (v.co.z - 0.6) * 0.3
    v.co.x *= 1.4
    v.co.y *= 1.4
for f, w in ((1, 0.0), (30, 1.0), (60, 0.0), (90, 1.0), (120, 0.0)):
    squash.value = w
    squash.keyframe_insert("value", frame=f)

# Bones: a tentacle skinned to a chain of three bones that sway.
bpy.ops.mesh.primitive_cylinder_add(radius=0.25, depth=3.0, location=(1.4, -3.4, 1.5), vertices=16)
tentacle = active()
tentacle.name = "Tentacle"
bpy.ops.object.mode_set(mode="EDIT")
bpy.ops.mesh.subdivide(number_cuts=12)
bpy.ops.object.mode_set(mode="OBJECT")
bpy.ops.object.shade_smooth()
tentacle.data.materials.append(material("Purple", (0.5, 0.2, 0.9), roughness=0.35))
bpy.ops.object.armature_add(location=(1.4, -3.4, 0.0))
rig = active()
rig.name = "Rig"
bpy.ops.object.mode_set(mode="EDIT")
bones = rig.data.edit_bones
b = bones[0]
b.name, b.head, b.tail = "Base", (0, 0, 0), (0, 0, 1)
for name, z0, z1 in (("Middle", 1, 2), ("Tip", 2, 3)):
    nb = bones.new(name)
    nb.head, nb.tail, nb.parent, nb.use_connect = (0, 0, z0), (0, 0, z1), b, True
    b = nb
bpy.ops.object.mode_set(mode="OBJECT")
tentacle.select_set(True)
rig.select_set(True)
bpy.context.view_layer.objects.active = rig
bpy.ops.object.parent_set(type="ARMATURE_AUTO")
rig.select_set(False)
for pb, phase in zip(rig.pose.bones, (0.0, 0.6, 1.2)):
    pb.rotation_mode = "XYZ"
    for f in range(1, 122, 15):
        pb.rotation_euler = (0.45 * math.sin(2 * math.pi * f / 120 + phase), 0, 0.3 * math.cos(2 * math.pi * f / 120 + phase))
        pb.keyframe_insert("rotation_euler", frame=f)

# Lights. Their energies are the physical units the glTF exporter converts.
sun = bpy.data.objects.new("Sun", bpy.data.lights.new("SunData", "SUN"))
sun.data.energy = 3.0
sun.rotation_euler = (math.radians(50), 0, math.radians(30))
scene.collection.objects.link(sun)
lamp = bpy.data.objects.new("Lamp", bpy.data.lights.new("LampData", "POINT"))
lamp.data.energy = 300.0
lamp.data.color = (1.0, 0.6, 0.3)
lamp.location = (-3, -2, 3)
scene.collection.objects.link(lamp)
# Its color cycles: light values.
for f, c in ((1, (1.0, 0.6, 0.3)), (40, (0.3, 0.6, 1.0)), (80, (0.6, 1.0, 0.3)), (120, (1.0, 0.6, 0.3))):
    lamp.data.color = c
    lamp.data.keyframe_insert("color", frame=f)

# Camera, aimed with a constraint: moving it in Blender moves score's view.
cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("CameraData"))
cam.location = (7, -7, 5)
scene.collection.objects.link(cam)
track = cam.constraints.new("TRACK_TO")
track.target = monkey
track.track_axis = "TRACK_NEGATIVE_Z"
track.up_axis = "UP_Y"
scene.camera = cam
# A slow zoom in and out: camera values.
for f, lens in ((1, 35.0), (60, 60.0), (120, 35.0)):
    cam.data.lens = lens
    cam.data.keyframe_insert("lens", frame=f)

scene.frame_set(1)
bpy.ops.wm.save_as_mainfile(filepath=out)
print("saved", out)
