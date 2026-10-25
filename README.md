# ossia score Live Link for Blender

Streams the open Blender scene to a **glTF Live Link** process in ossia score
(Visuals/3D). The whole scene is sent once, from Blender's own meshes or as
glTF; after that, everything that is a value streams on its own, without
exporting again: object transforms, bone poses, shape key weights,
visibility, light and camera parameters, and material factors. Posing a rig,
scrubbing the timeline or dragging a roughness slider updates score at frame
rate.

## Install

Blender 4.2 or later. Build the extension package and install it:

    blender --command extension build
    # Edit > Preferences > Get Extensions > Install from Disk… > score_livelink-1.0.0.zip

Or from a terminal: `blender --command extension install-file -r user_default -e score_livelink-1.0.0.zip`.

For development, symlink this folder into
`~/.config/blender/<version>/extensions/user_default/score_livelink`.

## Try it

`examples/` has a ready-made pair:

1. Open `examples/livelink-demo.score` in score and press Play. A window
   opens; it stays black until Blender connects.
2. Open `examples/livelink-demo.blend` in Blender, press **Start Live Link**
   in the sidebar's **score** tab, then Play in Blender's timeline.

The score window shows Blender's scene through Blender's camera, and every
kind of streamed value is animated: the gold monkey turns and the ring orbits
(transforms), the purple tentacle sways (bones), the mint blob squashes
(shape keys), the ring blinks (visibility), the lamp changes color (light),
the camera zooms (camera) and the ball shifts from red to orange (material).
Edit anything and score follows.

The score file is score-docs' scene-graph-filter example with its asset,
camera and light replaced by the live link, and skinning added to its render
pipeline (`add_skinning.py`: that pipeline does not skin, so rigged meshes
would not follow their bones). `make_demo.py`, `make_demo_score.js` and
`add_skinning.py` regenerate the files.

`examples/livelink-openpbr.score` takes the same .blend through an OpenPBR
render chain instead: score-csf-testers' `classic_pbr_openpbr` shader, the
OpenPBR LUTs and tone mapping, from score-corpus' `sponza-openpbr` example
(`make_openpbr_score.js`, `openpbr_shader.py` and `add_skinning.py`
regenerate it). It needs score-addon-academy, and the shader's
`#include "openpbr.h"` must resolve: score looks for shader includes in the
packages folder and its first-level subfolders, so a link
`packages/openpbr` to academy's `3rdparty/OpenPBR-BSDF` does it. Its first
start spends about 45 seconds compiling the shader.

## Use

1. In score, add a **glTF Live Link** process and connect its Scene output
   like an Asset Loader's. It listens on 127.0.0.1:9877 by default.
2. In Blender, open the 3D View sidebar (N), tab **score**, and press
   **Start Live Link**. It connects (and reconnects) on its own; score gets the
   whole scene on every connection.

To stream from another machine, set the process's Bind address to `0.0.0.0`
and the add-on's Host to score's address. Anyone who can reach the port can
replace the scene.

## What is sent when

| Change in Blender                                          | Sent              |
|------------------------------------------------------------|-------------------|
| Moving objects; animation playback                         | Transforms        |
| Posing an armature (Pose Mode, actions, constraints)       | Bone poses        |
| Shape key values (sliders, drivers, keyframes)             | Weights           |
| Hiding / showing objects in the viewport                   | Visibility        |
| Light color, power, exposure, temperature, spot cone, distance | Light values  |
| Camera focal length, sensor, clipping, orthographic scale  | Camera values     |
| Base Color, Alpha, Metallic, Roughness, Emission of a Principled BSDF, when not wired to other nodes | Material values |
| The World's color or environment image, and its strength   | Ambient light     |
| The scene's active camera                                  | Active camera     |
| Adding objects                                             | Those objects, as a patch |
| Deleting objects                                           | Their names, removed |
| Modifiers, or material slots, of an object in Object Mode  | That object, removed and patched back |
| Mesh edits in Edit Mode, on native meshes                  | That mesh, natively |
| Other mesh edits (Sculpt, paint modes; glTF objects)       | Full glTF export  |
| Material node changes beyond those values; Alpha crossing 1 | Full glTF export |
| Reparenting; changes to a child, or to objects shown by collection instances; new bones; light type changes | Full glTF export |
| Selection, viewport and UI changes                         | Nothing           |

The add-on keeps what score was last sent and diffs the scene against it on
every change; anything that differs but is not one of the values above
triggers a full export. Each streamed value is computed as Blender's glTF
exporter computes it (`values.py`, which calls the exporter's own light and
camera helpers where it can), so a streamed value and the next export agree.

A patch is a .glb of only the objects concerned (the exporter's
selection-only mode; the selection is put back afterwards), which score adds
to the scene it has: adding an object to a scene that takes a minute to
export costs tens of milliseconds. An object goes alone when its family
does: a patch holds whole hierarchies, an object and its armature, and
nothing that a collection instance also shows. Otherwise, and with **Partial
Updates** off, the whole scene is exported.

Full exports and patches are spaced by **Min. Export Interval** and by twice
their own duration, so a heavy scene stays editable. **Re-export on Frame Change** also
exports on every frame, for animated geometry or materials.

## Native geometry

When score supports it (it says so when the add-on connects, see below),
the scene skips the glTF exporter: the add-on reads Blender's already
evaluated meshes into vertex buffers, sends its images' PNG and JPEG files as
they are (and encodes the others, generated or painted, once), and describes
nodes, materials, lights, cameras, skins and instances in a small JSON
scene. That is what an export would carry, at a fraction of the cost, and it
is what makes Edit Mode stream: editing a mesh sends that mesh alone.

What goes native:

- meshes, curves and text, with their modifiers applied, merged into
  indexed vertices;
- shape keys, as morph targets, and armatures with their skinned meshes
  (bones as nodes, the exporter's inverse bind matrices), so poses and shape
  key values stream to them as to glTF ones;
- objects parented to bones;
- collection instances (copies of the collection's objects under the
  instancer, sharing their meshes) and the instances the depsgraph makes
  (particles, vertex and face instancing, geometry nodes), up to 200000 per
  object;
- materials: the Principled BSDF as the glTF exporter exports it, extensions
  (transmission, IOR, specular, clearcoat, sheen, anisotropy, thin film) and
  texture transforms (Mapping nodes) included, looking through the nodes the
  exporter looks through. Other shaders, which the exporter exports plain
  white, are approximated: Diffuse as a rough dielectric, Glossy as a metal,
  Glass as transmission, Emission as emission, Mix and Add Shader as a blend
  of what they combine.

What still goes through the glTF exporter, as a patch next to the native
objects: objects parented to vertices or lattices, and skinned meshes whose
armature is not sent with them. The console says which and why.
**Native Geometry** turns the channel off.

## Negotiation and fallback

On connecting, the add-on's hello lists what it can send and score answers
with what it accepts ([`PROTOCOL.md`](PROTOCOL.md#negotiation)). The add-on sends only
frame types score accepts; a receiver that does not answer within a second
gets plain glTF scenes and value records. Another DCC exporter can drive
the same process with nothing but glTF scene frames, and adopt the patch,
value and native frames one at a time.

## Light units

score's renderer expects light intensities on the scale of its own Light
process (around 1 to 100). The glTF standard's physical units (a 1000 W point
light is about 54000 cd) blow a score scene out to white, so **Light Units**
defaults to *Unitless*. *Raw* sends Blender's numbers as they are;
*Physical* is what a .glb exported for other tools would contain.

## Limitations

- Shape keys only reach score on meshes without modifiers while **Apply
  Modifiers** is on: the exporter applies them, and an applied mesh has no
  shape keys. Turn it off, or keep shape-keyed meshes modifier-free.
- Skinned meshes need a render pipeline that skins (see `add_skinning.py`).
- Objects parented to bones or to skinned meshes, and collection instances,
  fall back to full exports when they move.
- Area lights are dropped: glTF has no area lights. Neither are meshes with
  an emissive material lights in score: they glow, but light nothing.
- Without the native channel (Native Geometry off, or a receiver that does
  not take native frames), only
  Principled BSDF materials keep their look: the exporter exports Diffuse,
  Glossy and Mix Shader setups plain white. Natively they are approximated.
- The World is ambient light only: one color from every direction (an
  environment image's average). There is no image-based lighting, and a
  World mixing shaders sends its plain color.
- Procedural textures (Noise, Voronoi, ...) are dropped: bake them to images.
- glTF has no node for Blender-only data (particles, volumes, grease pencil);
  what the glTF exporter drops never reaches score.

The receiving process is documented at
https://ossia.io/score-docs/processes/gltf-live-link.html, and the wire
format in [`PROTOCOL.md`](PROTOCOL.md); any program that can write a .glb
can drive the process.
