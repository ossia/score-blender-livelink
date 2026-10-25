# ossia score glTF Live Link protocol

Version 1. This document specifies how a 3D application (a *sender*: the
Blender add-on in this repository, or any other DCC exporter) streams a scene
to the **glTF Live Link** process of ossia score (the *receiver*). The C++
reference is score's
[`src/plugins/score-plugin-threedim/Threedim/LiveLink/Protocol.hpp`](https://github.com/ossia/score/blob/master/src/plugins/score-plugin-threedim/Threedim/LiveLink/Protocol.hpp); where the two
disagree, `Protocol.hpp` and its tests
([`tests/unit/GltfLiveLinkTest.cpp`](https://github.com/ossia/score/blob/master/tests/unit/GltfLiveLinkTest.cpp)) are
authoritative.

The protocol is layered so that a sender can start small:

1. **glTF only.** Send a binary glTF (`.glb`) of the whole scene whenever it
   changes. That alone is a complete, conforming sender.
2. **Values.** Stream transforms, bone poses, morph weights, visibility,
   light, camera and material parameters as small records instead of
   re-exporting.
3. **Structure.** Add, remove and replace single objects with glTF patches
   and removals instead of re-exporting the scene.
4. **Native.** Send meshes and images as raw buffers and files with a JSON
   scene description, skipping glTF encoding altogether.

Each layer is optional and negotiated (see *Negotiation*).

## Transport

A single TCP connection from the sender to the receiver. The receiver
listens on `127.0.0.1:9877` by default (port and bind address are settings
of the process); a sender should make both configurable. The receiver keeps
the **newest** connection and closes any older one: a sender that reconnects
replaces its own stale session. Senders reconnect on their own after a
disconnection and send the whole scene again on every connection.

There is no authentication. Anyone who can reach the port can replace the
scene; the receiver binds to the loopback interface unless told otherwise.

## Framing

All integers and floats are little-endian. The stream is a sequence of
frames:

| Field  | Type            | Meaning                                  |
|--------|-----------------|------------------------------------------|
| length | u32             | byte count of what follows (type + body) |
| type   | u8              | frame type, below                        |
| body   | u8[length - 1]  |                                          |

A length of 0 or above 512 MiB is a protocol error: the receiver drops the
connection, since the stream cannot be resynchronised. A frame whose body is
malformed is ignored as a whole (every frame is all-or-nothing). Frames of an
unknown type are skipped.

Common body types:

- `str`: u16 byte count, then that many UTF-8 bytes (no terminator).
- `f32`, `u8`, `u16`, `u32`, `u64`: as named.
- `seq`: u64, see *Ordering*.

## Frame types

| Type | Name            | Body                                              | Layer     |
|------|-----------------|---------------------------------------------------|-----------|
| 1    | `hello`         | UTF-8 JSON                                        | all       |
| 2    | `scene`         | seq, then a complete `.glb`                       | glTF      |
| 3    | `transforms`    | seq, u32 count, count *transform* records         | values    |
| 4    | `joints`        | seq, u32 count, count *armature pose* records     | values    |
| 5    | `weights`       | seq, u32 count, count *weights* records           | values    |
| 6    | `visibility`    | seq, u32 count, count *visibility* records        | values    |
| 7    | `lights`        | seq, u32 count, count *light* records             | values    |
| 8    | `cameras`       | seq, u32 count, count *camera* records            | values    |
| 9    | `materials`     | seq, u32 count, count *material* records          | values    |
| 10   | `remove`        | seq, u32 count, count `str` node names            | structure |
| 11   | `patch`         | seq, then a `.glb` of a few objects               | structure |
| 12   | `environment`   | seq, u32 count, count *environment* records       | values    |
| 13   | `active_camera` | seq, u32 count, count `str` camera node names     | values    |
| 14   | `mesh`          | see *Native frames*                               | native    |
| 15   | `image`         | seq, `str` key, `str` MIME type, file bytes       | native    |
| 16   | `native_scene`  | seq, u8 reset, UTF-8 JSON                         | native    |

The `.glb` of `scene` and `patch` frames must start with the `glTF` magic.

## Negotiation

The sender opens every connection with a `hello`:

```json
{"protocol": 1, "app": "blender", "version": "5.2.2", "file": "/path/scene.blend",
 "features": ["scene", "transforms", "patch", "remove", "mesh", "image", "native_scene"]}
```

`features` lists the frame types the sender may send. The receiver answers
with its own `hello`:

```json
{"protocol": 1, "app": "ossia score",
 "accepts": ["scene", "patch", "remove", "transforms", "joints", "weights", "visibility",
             "lights", "cameras", "materials", "environment", "active_camera",
             "mesh", "image", "native_scene"]}
```

Rules:

- The sender sends only frame types listed in `accepts`.
- A sender that receives no answer within one second takes the receiver for
  one that does not negotiate, and sends it `scene` frames only.
- A receiver handles frames sent without a hello the same way.
- Fields of either hello that a peer does not know are ignored.

`scene` is the one frame type every receiver accepts: a sender can always fall
back to sending the whole scene as glTF.

## Ordering and supersession

`seq` is a single counter shared by every frame type, strictly increasing on
the sender for the whole connection. The receiver uses it to decide what a
frame supersedes:

- A **scene** (a `scene` frame, or a `native_scene` with reset set) replaces
  everything. Value records with a seq lower than or equal to the scene's are
  dropped: the scene already contains those values. Pending patches,
  removals and native scenes older than it are discarded too.
- **Structural frames** (`patch`, `remove`, `native_scene`, `mesh`, `image`)
  are applied in seq order, all of them, even when the receiver is slower
  than the sender. A frame waits for every earlier structural frame, so a
  removal cannot overtake the patch that added what it removes, even though
  patches are parsed in the background.
- **Value records** are coalesced: the receiver keeps only the newest record
  per key (per node, bone or material) and per kind. A record overrides the
  corresponding value of the current scene until a newer record or a newer
  scene replaces it.
- `mesh` and `image` frames are resources, never superseded by a scene: a
  native scene with reset typically follows the resources it needs.

A sender should send the structural frames of one change in the order the
receiver must apply them, with increasing seqs: a removal before the patch
that adds an object back, meshes and images before the native scene that
names them.

## Conventions

Everything is expressed as glTF expresses it:

- Right-handed, **Y up**, metres. Quaternions are `x, y, z, w`.
- Node transforms are **local** to the parent node, as glTF node TRS.
- UV origin at the **top left** (glTF's convention, V downwards).
- Nodes are addressed **by name** (the glTF node name). Names need not be
  unique: a record applies to every node of that name.
- Bones are addressed by armature node name and bone node name.
- Materials are addressed by material name (glTF material name), and apply to
  every material of that name.

Colors are linear. Light intensity is in the units the receiver's renderer
uses: KHR_lights_punctual's candela and lux render very bright in score, so
senders should offer a choice (the Blender add-on defaults to Blender's
watts divided by 4π for point and spot lights, its strength for suns).

## Value records

All records start with the key (`str`) they apply to.

| Record          | Layout after the key                                                           |
|-----------------|--------------------------------------------------------------------------------|
| transform       | f32 translation[3], f32 rotation[4] (x, y, z, w), f32 scale[3]                  |
| armature pose   | u32 bone count, then per bone a transform record named after the bone          |
| weights         | u16 n, f32 weights[n]: morph target weights of the node's mesh                 |
| visibility      | u8 visible (0 or 1)                                                             |
| light           | u8 type (0 directional, 1 point, 2 spot), f32 color[3], f32 intensity, f32 range (0: infinite), f32 inner cone, f32 outer cone (radians) |
| camera          | u8 projection (0 perspective, 1 orthographic), f32 yfov (radians), f32 aspect (0: unset), f32 znear, f32 zfar, f32 xmag, f32 ymag |
| material        | u32 fields, f32 base color[4], f32 metallic, f32 roughness, f32 emissive[3], f32 emissive strength |
| environment     | f32 ambient[3], f32 strength: light received from every direction              |
| active camera   | (none: the key is the camera node's name)                                      |

`fields` in a material record says which values are meant (the others keep
the scene's): 1 base color RGB, 2 alpha, 4 metallic, 8 roughness, 16
emissive (factor and strength together). Emissive factors stay within 0..1,
any excess in the strength (KHR_materials_emissive_strength).

The keys of `environment` and `active_camera` records do not matter: there is
one of each per scene, the newest wins. Neither is part of glTF, so the
receiver keeps them across scenes; a sender sends them after every scene.
An empty active camera name means the receiver's default (the first camera of
its tree): a sender should name one whenever the scene has cameras, so that
both sides agree.

Visibility is not taken from glTF: a scene shows every node, and a sender
sends visibility records for the hidden ones after it.

## Structural frames

`remove` deletes the named nodes and their subtrees from the current scene,
and the materials nothing uses any more.

`patch` adds the root nodes of its `.glb` to the root of the current scene.
Node ids in it are renumbered, so a patch can come from an independent
export. A patch brings its own copy of every material it uses, existing ones
included; material records apply to every copy.

Replacing an object is a removal followed by a patch of it, with increasing
seqs.

## Native frames

Native frames carry what an export would, without encoding glTF.

### `mesh` (14)

```
u64 seq
str key
u16 primitive count
per primitive:
  u32 vertex count v
  u32 index count i          (0: not indexed, a triangle list of v vertices)
  u8  attributes             (1 normal, 2 texcoord0, 4 texcoord1, 8 color0, 16 skin)
  u16 morph target count t
  f32 position[3v]
  f32 normal[3v]             if attributes & 1
  f32 texcoord0[2v]          if attributes & 2
  f32 texcoord1[2v]          if attributes & 4
  f32 color0[4v]             if attributes & 8
  u16 joints[4v]             if attributes & 16
  f32 weights[4v]            if attributes & 16
  u32 indices[i]
  per morph target:
    u8  which                (1 position deltas, 2 normal deltas)
    f32 position deltas[3v]  if which & 1
    f32 normal deltas[3v]    if which & 2
```

One primitive per material slot. Every index must be lower than v; the frame
must be consumed exactly. Joints index the `joints` list of the skin of the
node using the mesh; weights sum to 1 per vertex. Positions are in the node's
space, or, for a skinned mesh, in the space its inverse bind matrices expect
(glTF's skinning model). Morph target deltas are relative to the positions
sent; the node's `weights` say how much of each applies.

The receiver keeps meshes by key. A mesh sent again under the same key
replaces the previous one. Several nodes may name one mesh: it is uploaded
once.

### `image` (15)

The image file as is (PNG or JPEG; a receiver may decode more), kept by key
like meshes. Images are color-managed by the slot using them: base color and
emissive textures are sRGB, the others linear.

### `native_scene` (16)

```
u64 seq
u8  reset      (1: replaces the scene, as a scene frame; 0: added, as a patch)
UTF-8 JSON
```

```json
{
  "nodes": [
    {"name": "Rig", "parent": -1, "t": [0, 0, 0], "r": [0, 0, 0, 1], "s": [1, 1, 1], "visible": true},
    {"name": "Bone", "parent": 0, "t": [0, 1, 0], "r": [0, 0, 0, 1], "s": [1, 1, 1]},
    {"name": "Body", "parent": 0, "t": [0, 0, 0], "r": [0, 0, 0, 1], "s": [1, 1, 1],
     "mesh": "obj:Body", "materials": ["Skin"], "weights": [0.0, 0.25],
     "skin": {"armature": 0, "joints": ["Bone"], "inverse_bind": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, -1, 0, 1]}},
    {"name": "Lamp", "parent": -1, "t": [2, 3, 1], "r": [0, 0, 0, 1], "s": [1, 1, 1],
     "light": [1, 1.0, 0.9, 0.8, 40.0, 0.0, 0.0, 0.785]},
    {"name": "Camera", "parent": -1, "t": [0, 1, 5], "r": [0, 0, 0, 1], "s": [1, 1, 1],
     "camera": [0, 0.69, 1.778, 0.1, 100.0, 0.0, 0.0]}
  ],
  "materials": {
    "Skin": {"name": "Skin", "base_color": [1, 1, 1, 1], "metallic": 0, "roughness": 0.5,
             "emissive": [0, 0, 0], "emissive_strength": 1, "alpha": "opaque",
             "double_sided": false,
             "textures": {"base_color": {"image": "img:skin_diff", "uv": 0}}}
  }
}
```

Nodes:

| Field       | Meaning                                                                   |
|-------------|---------------------------------------------------------------------------|
| `name`      | node name, as records address it                                         |
| `parent`    | index of the parent in `nodes` (earlier in the list), or -1 |
| `t` `r` `s` | local translation, rotation (x, y, z, w), scale                           |
| `visible`   | default true                                                             |
| `mesh`      | key of a mesh frame                                                       |
| `materials` | per primitive of the mesh, a key of `materials` ("" for none)             |
| `weights`   | initial morph target weights                                              |
| `skin`      | `armature`: index of the node the joints are under; `joints`: joint node names, under it, in the order mesh joints index; `inverse_bind`: 16 floats per joint, column-major |
| `light`     | `[type, r, g, b, intensity, range, inner, outer]`, as a light record      |
| `camera`    | `[projection, yfov, aspect, znear, zfar, xmag, ymag]`, as a camera record |

Nodes are listed parents first. Joint transforms are those of the joint
nodes, so armature pose records drive native skins as they drive glTF ones.

Materials are keyed by the names nodes use; `name` is the material's own name
(several keys may share one, for instance when texture coordinates differ),
which material records address. Fields, all optional, follow glTF's
metallic-roughness model and its extensions:

| Field | glTF equivalent |
|-------|-----------------|
| `base_color` [4], `metallic`, `roughness` | pbrMetallicRoughness factors |
| `emissive` [3], `emissive_strength` | emissiveFactor, KHR_materials_emissive_strength |
| `alpha` (`opaque`, `mask`, `blend`), `alpha_cutoff`, `double_sided` | alphaMode, alphaCutoff, doubleSided |
| `unlit` | KHR_materials_unlit |
| `ior` | KHR_materials_ior |
| `transmission` | KHR_materials_transmission |
| `specular`, `specular_color` [3] | KHR_materials_specular |
| `clearcoat`, `clearcoat_roughness` | KHR_materials_clearcoat |
| `sheen_color` [3], `sheen_roughness` | KHR_materials_sheen |
| `anisotropy`, `anisotropy_rotation` | KHR_materials_anisotropy |
| `iridescence`, `iridescence_ior`, `iridescence_thickness` [min, max] | KHR_materials_iridescence |

`textures` maps a slot to a texture info. Slots, with the channels glTF
reads from them: `base_color`, `metallic_roughness` (G roughness, B
metallic), `normal`, `occlusion` (R), `emissive`, `transmission` (R),
`specular` (A), `specular_color`, `clearcoat` (R), `clearcoat_roughness` (G),
`clearcoat_normal`, `sheen_color`, `sheen_roughness` (A).

| Texture info field | Meaning |
|--------------------|---------|
| `image` | key of an image frame |
| `uv` | texture coordinate set (0 or 1) |
| `wrap` | `repeat` (default), `clamp`, `mirror` |
| `filter` | `linear` (default), `nearest` |
| `scale` | normal map scale (normal and clearcoat_normal slots) |
| `strength` | occlusion strength |
| `transform` | `{"offset": [2], "rotation", "scale": [2]}`, as KHR_texture_transform |

Unknown fields are ignored, and a material, mesh or image that a node names
but that was not received is left out (the node keeps its other content).

With `reset` set, the scene replaces the current one and the receiver drops
the meshes and images it does not use. Without, its root nodes are added as a
patch's are: replacing an object is a removal followed by a native scene of
it.

## Receiver behaviour

- Records for nodes, bones or materials that do not exist are kept: they
  apply once a patch or scene brings that name.
- The scene stays published after the sender disconnects.
- Parsing (glTF) and tangent generation (native meshes with normals and UVs,
  MikkTSpace) happen off the render thread.

## Writing a sender

The minimal sender connects, sends a hello, and sends the whole scene as a
`scene` frame every time it changes, with an increasing seq. From there, in
order of benefit:

1. Send `transforms` records for moved objects instead of a new scene
   (compute them as your glTF exporter computes node TRS, or values and
   export will disagree).
2. Send visibility, light, camera and material records for the parameters
   your users tweak most.
3. Send `remove` and `patch` frames for added, deleted and edited objects.
4. Implement the native frames for the bulk of your geometry, keeping
   `patch` for what you do not translate.

The Blender add-on in this repository is a complete example; its `link.py`
holds its encoders, which do not depend on Blender.
