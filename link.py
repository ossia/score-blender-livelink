# Wire format and sender thread for the score glTF live link.
#
# Mirrors score's src/plugins/score-plugin-threedim/Threedim/LiveLink/Protocol.hpp:
# every frame is  u32 length | u8 type | body  (little-endian), with
#   1 hello       UTF-8 JSON
#   2 scene       u64 seq | .glb bytes
#   11 patch      u64 seq | .glb bytes of a few objects, added to the scene
#   3..10, 12, 13 u64 seq | u32 count | count records of that type
#   14 mesh, 15 image, 16 native_scene: see native.py
# and a `str` written as u16 byte count + UTF-8 bytes.
#
# Negotiation: the hello lists what this sender can send; score answers with
# a hello listing what it accepts ("accepts"). A receiver that does not
# answer within HELLO_TIMEOUT is taken for one that does not negotiate, and
# gets glTF scene frames only.
#
# Nothing here imports bpy, so the module can be tested outside Blender.

import json
import socket
import struct
import threading

FRAME_HELLO = 1
FRAME_SCENE = 2
FRAME_TRANSFORMS = 3
FRAME_JOINTS = 4
FRAME_WEIGHTS = 5
FRAME_VISIBILITY = 6
FRAME_LIGHTS = 7
FRAME_CAMERAS = 8
FRAME_MATERIALS = 9
FRAME_REMOVE = 10
FRAME_PATCH = 11
FRAME_ENVIRONMENT = 12
FRAME_ACTIVE_CAMERA = 13
FRAME_MESH = 14
FRAME_IMAGE = 15
FRAME_NATIVE_SCENE = 16

# Frame type names, as hellos list them.
FRAME_NAMES = {
    FRAME_SCENE: "scene", FRAME_TRANSFORMS: "transforms", FRAME_JOINTS: "joints",
    FRAME_WEIGHTS: "weights", FRAME_VISIBILITY: "visibility", FRAME_LIGHTS: "lights",
    FRAME_CAMERAS: "cameras", FRAME_MATERIALS: "materials", FRAME_REMOVE: "remove",
    FRAME_PATCH: "patch", FRAME_ENVIRONMENT: "environment",
    FRAME_ACTIVE_CAMERA: "active_camera", FRAME_MESH: "mesh", FRAME_IMAGE: "image",
    FRAME_NATIVE_SCENE: "native_scene",
}
# What a receiver that does not answer the hello gets: whole glTF scenes.
GLTF_ONLY = frozenset({FRAME_NAMES[FRAME_SCENE]})
HELLO_TIMEOUT = 1.0

PROTOCOL_VERSION = 1


def _str(s):
    raw = s.encode("utf-8")[:0xFFFF]
    return struct.pack("<H", len(raw)) + raw


def _trs(name, t, r, s):
    return _str(name) + struct.pack("<10f", *t, *r, *s)


# Record encoders, by frame type. Each takes the record as queued: a tuple
# whose first element is its key.
def _transform(rec):
    name, t, r, s = rec
    return _trs(name, t, r, s)


def _weights(rec):
    name, w = rec
    return _str(name) + struct.pack("<H", len(w)) + struct.pack("<%df" % len(w), *w)


def _visibility(rec):
    name, v = rec
    return _str(name) + struct.pack("<B", 1 if v else 0)


def _light(rec):
    name, (ty, color, intensity, rng, inner, outer) = rec
    return _str(name) + struct.pack("<B7f", ty, *color, intensity, rng, inner, outer)


def _camera(rec):
    name, (proj, yfov, aspect, znear, zfar, xmag, ymag) = rec
    return _str(name) + struct.pack("<B6f", proj, yfov, aspect, znear, zfar, xmag, ymag)


def _material(rec):
    name, (fields, base, metallic, roughness, emissive, strength) = rec
    return _str(name) + struct.pack("<I10f", fields, *base, metallic, roughness, *emissive, strength)


def _removal(rec):
    (name,) = rec
    return _str(name)


def _environment(rec):
    name, (ambient, strength) = rec
    return _str(name) + struct.pack("<4f", *ambient, strength)


def _active_camera(rec):
    _, name = rec
    return _str(name)


_ENCODERS = {
    FRAME_REMOVE: _removal,
    FRAME_ACTIVE_CAMERA: _active_camera,
    FRAME_ENVIRONMENT: _environment,
    FRAME_TRANSFORMS: _transform,
    FRAME_WEIGHTS: _weights,
    FRAME_VISIBILITY: _visibility,
    FRAME_LIGHTS: _light,
    FRAME_CAMERAS: _camera,
    FRAME_MATERIALS: _material,
}


def encode_frame(frame_type, body):
    return struct.pack("<IB", len(body) + 1, frame_type) + body


def encode_hello(info):
    return encode_frame(FRAME_HELLO, json.dumps(info).encode("utf-8"))


def encode_scene(seq, glb, frame_type=FRAME_SCENE):
    return encode_frame(frame_type, struct.pack("<Q", seq) + glb)


def encode_mesh(seq, key, primitives):
    """primitives: dicts of "positions", and optionally "normals",
    "texcoords", "texcoords1", "colors", "weights" (float32 arrays), "joints"
    (uint16, 4 per vertex), "indices" (uint32) and "targets" (a list of dicts
    of "positions" and "normals" deltas), in glTF axes and UV convention."""
    parts = [struct.pack("<Q", seq), _str(key), struct.pack("<H", len(primitives))]
    for p in primitives:
        attrs = 0
        for bit, name in ((1, "normals"), (2, "texcoords"), (4, "texcoords1"), (8, "colors"), (16, "joints")):
            if p.get(name) is not None:
                attrs |= bit
        indices = p.get("indices")
        targets = p.get("targets") or ()
        parts.append(struct.pack("<IIBH", p["vertex_count"], 0 if indices is None else len(indices), attrs, len(targets)))
        for name in ("positions", "normals", "texcoords", "texcoords1", "colors", "joints", "weights", "indices"):
            if p.get(name) is not None:
                parts.append(p[name].tobytes())
        for t in targets:
            pos, nrm = t.get("positions"), t.get("normals")
            parts.append(struct.pack("<B", (1 if pos is not None else 0) | (2 if nrm is not None else 0)))
            for arr in (pos, nrm):
                if arr is not None:
                    parts.append(arr.tobytes())
    return encode_frame(FRAME_MESH, b"".join(parts))


def encode_image(seq, key, mime, data):
    return encode_frame(FRAME_IMAGE, struct.pack("<Q", seq) + _str(key) + _str(mime) + data)


def encode_native_scene(seq, reset, scene):
    body = json.dumps(scene, separators=(",", ":")).encode("utf-8")
    return encode_frame(FRAME_NATIVE_SCENE, struct.pack("<QB", seq, 1 if reset else 0) + body)


def encode_records(frame_type, seq, records):
    """records: the queued tuples of one frame type."""
    if frame_type == FRAME_JOINTS:
        # Queued per bone as ((armature, bone), t, r, s); sent per armature.
        by_armature = {}
        for (armature, bone), t, r, s in records:
            by_armature.setdefault(armature, []).append(_trs(bone, t, r, s))
        parts = [_str(a) + struct.pack("<I", len(b)) + b"".join(b) for a, b in by_armature.items()]
        count = len(by_armature)
    else:
        enc = _ENCODERS[frame_type]
        parts = [enc(r) for r in records]
        count = len(parts)
    return encode_frame(frame_type, struct.pack("<QI", seq, count) + b"".join(parts))


def encode_transforms(seq, records):
    return encode_records(FRAME_TRANSFORMS, seq, records)


class Link(threading.Thread):
    """Connects to score and sends whatever is queued, reconnecting forever.

    The queue holds at most one scene and, per frame type, one record per key
    (object, bone or material): when the network is slower than the scene
    changes, intermediate states are dropped rather than buffered. Patches
    and removals are structure, and all of them are sent, in order. A scene
    is always sent before the patches and the records queued with it.

    `connection_id` increases on every successful connection; the main thread
    compares it to know when score needs the whole scene again. `accepts`,
    set before it increases, holds what the receiver said it accepts.
    """

    def __init__(self, host, port, hello):
        super().__init__(name="score-livelink", daemon=True)
        self.host = host
        self.port = port
        self.hello = hello

        self._cv = threading.Condition()
        self._scene = None
        self._structural = []  # encoded patch and remove frames, in order
        self._reset = False
        self._records = {}  # frame type -> (seq, {key: record})
        self._stopping = False
        self._sock = None

        self.connected = False
        self.connection_id = 0
        self.accepts = GLTF_ONLY
        self.peer = {}
        self.last_error = ""
        self.bytes_sent = 0
        self.scenes_sent = 0
        self.patches_sent = 0

    # Main thread API -------------------------------------------------------

    def push_scene(self, seq, glb):
        with self._cv:
            self._scene = (seq, glb)
            # Whatever is still queued is older than this scene, which already
            # contains it.
            self._structural.clear()
            self._reset = False
            self._records.clear()
            self._cv.notify()

    def push_reset(self, frames):
        """Encoded frames that together replace the scene (a native scene
        with reset, and the meshes and images it needs before it)."""
        with self._cv:
            self._scene = None
            self._records.clear()
            self._structural = list(frames)
            self._reset = True  # counted as a scene once sent
            self._cv.notify()

    def push_frames(self, frames):
        """Encoded structural frames, sent in order after what is queued."""
        if not frames:
            return
        with self._cv:
            self._structural.extend(frames)
            self._cv.notify()

    def push_patch(self, seq, glb):
        with self._cv:
            self._structural.append(encode_scene(seq, glb, FRAME_PATCH))
            self._cv.notify()

    def push_remove(self, seq, names):
        if not names:
            return
        names = set(names)
        with self._cv:
            self._structural.append(encode_records(FRAME_REMOVE, seq, [(n,) for n in names]))
            # Values queued for the removed nodes are older than the removal,
            # and would land after it (records go out after structure) on
            # whatever is added back under the same name.
            for _, queued in self._records.values():
                for key in [k for k in queued if (k if isinstance(k, str) else k[0]) in names]:
                    del queued[key]
            self._cv.notify()

    def push_records(self, frame_type, seq, records):
        if not records:
            return
        with self._cv:
            _, queued = self._records.get(frame_type, (0, {}))
            for rec in records:
                queued[rec[0]] = rec
            self._records[frame_type] = (seq, queued)
            self._cv.notify()

    def push_transforms(self, seq, records):
        self.push_records(FRAME_TRANSFORMS, seq, records)

    def stop(self):
        with self._cv:
            self._stopping = True
            self._cv.notify()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # Network thread --------------------------------------------------------

    def run(self):
        while True:
            with self._cv:
                if self._stopping:
                    break

            if not self.connected and not self._connect():
                with self._cv:
                    self._cv.wait(1.0)
                continue

            with self._cv:
                while not self._stopping and self._scene is None and not self._structural and not self._records:
                    self._cv.wait(1.0)
                if self._stopping:
                    break
                scene, self._scene = self._scene, None
                structural, self._structural = self._structural, []
                reset, self._reset = self._reset, False
                records, self._records = self._records, {}

            try:
                if scene is not None:
                    self._send(encode_scene(*scene))
                    self.scenes_sent += 1
                for frame in structural:
                    self._send(frame)
                    self.patches_sent += 1
                if reset:
                    self.scenes_sent += 1
                for frame_type, (seq, queued) in sorted(records.items()):
                    self._send(encode_records(frame_type, seq, queued.values()))
            except OSError as e:
                # What was in flight is lost; the main thread resends the whole
                # scene once connection_id moves.
                self._disconnect(str(e))

        self._disconnect("")

    def _connect(self):
        try:
            sock = socket.create_connection((self.host, self.port), timeout=2.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(10.0)
            self._sock = sock
            self._send(encode_hello(self.hello))
            self.peer, self.accepts = self._read_hello(sock)
        except OSError as e:
            self._disconnect(str(e))
            return False
        self.last_error = ""
        self.connected = True
        self.connection_id += 1
        return True

    @staticmethod
    def _read_hello(sock):
        """The receiver's hello and what it accepts; glTF scenes only when
        it does not answer in time."""
        sock.settimeout(HELLO_TIMEOUT)
        try:
            data = b""
            while len(data) < 5 or len(data) < 4 + struct.unpack("<I", data[:4])[0]:
                chunk = sock.recv(65536)
                if not chunk:
                    raise OSError("connection closed during the hello")
                data += chunk
                if len(data) >= 4 and struct.unpack("<I", data[:4])[0] > 1 << 20:
                    return {}, GLTF_ONLY
            n = struct.unpack("<I", data[:4])[0]
            if data[4] != FRAME_HELLO:
                return {}, GLTF_ONLY
            peer = json.loads(data[5:4 + n].decode("utf-8"))
            return peer, frozenset(peer.get("accepts", ())) or GLTF_ONLY
        except (socket.timeout, ValueError):
            return {}, GLTF_ONLY
        finally:
            sock.settimeout(10.0)

    def _disconnect(self, error):
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self.connected = False
        if error:
            self.last_error = error

    def _send(self, data):
        self._sock.sendall(data)
        self.bytes_sent += len(data)


def hello_info(app, version, filepath):
    return {
        "protocol": PROTOCOL_VERSION,
        "app": app,
        "version": version,
        "file": filepath,
        "features": sorted(FRAME_NAMES.values()),
    }
