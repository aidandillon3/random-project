"""Kinect v2 (Xbox One Kinect / Kinect for Windows v2) body tracking.

The Kinect measures depth directly, so legs keep real 3D positions even when the
color image is ambiguous. Joints come from Microsoft's own skeleton tracker
through the Kinect for Windows SDK 2.0.

We use the `pykinect2` package only for its COM interface definitions. Its
published version predates 64-bit Python 3 fixes, so we load it with three small
patches applied in memory (nothing on disk is changed).
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import time
import types

import numpy as np

from . import skeleton as S
from .mathutil import normalize

# Kinect v2 joint numbering (JointType_*)
K_SPINE_BASE, K_SPINE_MID, K_NECK, K_HEAD = 0, 1, 2, 3
K_SHOULDER_L, K_SHOULDER_R = 4, 8
K_HIP_L, K_KNEE_L, K_ANKLE_L, K_FOOT_L = 12, 13, 14, 15
K_HIP_R, K_KNEE_R, K_ANKLE_R, K_FOOT_R = 16, 17, 18, 19
K_SPINE_SHOULDER = 20
K_COUNT = 25

TRACKED, INFERRED = 2, 1

# Kinect camera space: x to the sensor's left, y up, z out of the sensor.
# MediaPipe-style camera axes: x right (as seen by the camera), y down, z away.
_K_TO_CAM = np.array([-1.0, -1.0, 1.0])


def patch_pykinect_source(src: str) -> str:
    """Make pykinect2's generated COM bindings import on 64-bit Python 3 and new comtypes."""
    lines = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("# -*- coding"):
            continue  # 'mbcs' cookie; we already decoded the text
        if stripped.startswith("assert sizeof(tagSTATSTG)") or stripped.startswith("assert alignment(tagSTATSTG)"):
            continue  # struct is 80 bytes on 64-bit, the check was written for 32-bit
        if "_check_version" in stripped:
            continue  # pins an ancient comtypes version
        lines.append(line)
    return "\n".join(lines) + "\n"


def load_pykinect():
    """Import pykinect2.PyKinectV2 with the fixes above. Raises ImportError with a clear message."""
    if "pykinect2.PyKinectV2" in sys.modules:
        return sys.modules["pykinect2.PyKinectV2"]
    spec = importlib.util.find_spec("pykinect2")
    if spec is None or not spec.submodule_search_locations:
        raise ImportError("Kinect support needs 'pykinect2' and 'comtypes' (run setup.bat again).")
    import os

    path = os.path.join(list(spec.submodule_search_locations)[0], "PyKinectV2.py")
    with open(path, "rb") as f:
        raw = f.read()
    src = patch_pykinect_source(raw.decode("utf-8", errors="replace"))
    pkg = sys.modules.get("pykinect2") or types.ModuleType("pykinect2")
    pkg.__path__ = list(spec.submodule_search_locations)
    sys.modules["pykinect2"] = pkg
    mod = types.ModuleType("pykinect2.PyKinectV2")
    mod.__file__ = path
    sys.modules["pykinect2.PyKinectV2"] = mod
    try:
        exec(compile(src, path, "exec"), mod.__dict__)
    except Exception:
        del sys.modules["pykinect2.PyKinectV2"]
        raise
    return mod


def kinect_to_landmarks(joints: np.ndarray, states: np.ndarray, swap_sides: bool = False):
    """Kinect joints (25, 3) + tracking states -> our 33-landmark layout.

    Returns (points (33, 3) in MediaPipe camera axes, hip-centred; visibility (33,)).
    Points Kinect doesn't have (eyes, ears, heels...) are built from nearby joints.
    """
    j = np.asarray(joints, dtype=float) * _K_TO_CAM
    conf = np.where(states == TRACKED, 1.0, np.where(states == INFERRED, 0.4, 0.0))
    if swap_sides:
        for a, b in ((K_SHOULDER_L, K_SHOULDER_R), (K_HIP_L, K_HIP_R), (K_KNEE_L, K_KNEE_R),
                     (K_ANKLE_L, K_ANKLE_R), (K_FOOT_L, K_FOOT_R)):
            j[[a, b]] = j[[b, a]]
            conf[[a, b]] = conf[[b, a]]

    p = np.zeros((S.NUM_LANDMARKS, 3))
    v = np.zeros(S.NUM_LANDMARKS)

    def put(idx, k):
        p[idx] = j[k]
        v[idx] = conf[k]

    put(S.L_SHOULDER, K_SHOULDER_L)
    put(S.R_SHOULDER, K_SHOULDER_R)
    put(S.L_HIP, K_HIP_L)
    put(S.R_HIP, K_HIP_R)
    put(S.L_KNEE, K_KNEE_L)
    put(S.R_KNEE, K_KNEE_R)
    put(S.L_ANKLE, K_ANKLE_L)
    put(S.R_ANKLE, K_ANKLE_R)
    put(S.L_TOE, K_FOOT_L)
    put(S.R_TOE, K_FOOT_R)

    # Body frame in these (y down) axes: up = -y direction of the spine.
    up = normalize(j[K_SPINE_SHOULDER] - j[K_SPINE_BASE])
    right = normalize(j[K_HIP_R] - j[K_HIP_L])
    forward = normalize(np.cross(up, right))  # chest direction (same rule as skeleton.body_forward)

    head = j[K_HEAD]
    hv = conf[K_HEAD]
    eyes = head + forward * 0.07
    for idx, side in ((S.L_EYE, -1), (S.R_EYE, 1)):
        p[idx] = eyes + right * 0.03 * side
        v[idx] = hv
    for idx, side in ((S.L_EAR, -1), (S.R_EAR, 1)):
        p[idx] = head + right * 0.075 * side
        v[idx] = hv
    p[S.NOSE] = eyes + forward * 0.03 - up * 0.03
    v[S.NOSE] = hv
    # heel: a few cm below the ankle, a little behind it
    for heel, ankle, foot in ((S.L_HEEL, K_ANKLE_L, K_FOOT_L), (S.R_HEEL, K_ANKLE_R, K_FOOT_R)):
        back = j[ankle] - j[foot]
        back = back - np.dot(back, up) * up
        p[heel] = j[ankle] - up * 0.05 + normalize(back) * 0.04
        v[heel] = min(conf[ankle], conf[foot])

    unused = v == 0
    hip_mid = 0.5 * (p[S.L_HIP] + p[S.R_HIP])
    p[unused & (np.linalg.norm(p, axis=1) == 0)] = hip_mid
    return p - hip_mid, v


class KinectSource:
    """Polls the Kinect v2 body reader on a background thread."""

    def __init__(self, swap_sides: bool = False):
        import ctypes

        K = load_pykinect()
        self._K = K
        self._ctypes = ctypes
        self.swap_sides = swap_sides
        self.sensor = ctypes.POINTER(K.IKinectSensor)()
        hr = ctypes.windll.kinect20.GetDefaultKinectSensor(ctypes.byref(self.sensor))
        if hr != 0:
            raise RuntimeError("Kinect not found. Is the Kinect for Windows SDK 2.0 installed "
                               "and the Kinect plugged in?")
        self.sensor.Open()
        source = self.sensor.BodyFrameSource
        self.body_count = source.BodyCount
        self.reader = source.OpenReader()
        self._tracking_id = None
        self._lock = threading.Condition()
        self._latest = None
        self._seq = 0
        self._stamp = 0.0
        self._running = True
        self.measured_fps = 0.0
        self._thread = threading.Thread(target=self._loop, name="kinect", daemon=True)
        self._thread.start()

    def describe(self) -> str:
        return "Kinect v2 body tracking @ 30 fps"

    def _pick_body(self, bodies):
        K = self._K
        tracked = []
        for i in range(self.body_count):
            b = bodies[i]
            if b and b.IsTracked:
                tracked.append(b)
        if not tracked:
            return None
        for b in tracked:  # stick with the same person
            if b.TrackingId == self._tracking_id:
                return b
        # otherwise the person closest to the sensor's centre line
        def score(b):
            joints = (K._Joint * K.JointType_Count)()
            b.GetJoints(K.JointType_Count, self._ctypes.cast(joints, self._ctypes.POINTER(K._Joint)))
            pos = joints[K_SPINE_BASE].Position
            return abs(pos.x) + 0.2 * pos.z
        best = min(tracked, key=score)
        self._tracking_id = best.TrackingId
        return best

    def _loop(self) -> None:
        K = self._K
        ct = self._ctypes
        from _ctypes import COMError

        last = time.perf_counter()
        while self._running:
            try:
                frame = self.reader.AcquireLatestFrame()
            except COMError:
                time.sleep(0.002)  # no new frame yet
                continue
            now = time.perf_counter()
            try:
                bodies = (ct.POINTER(K.IBody) * self.body_count)()
                frame.GetAndRefreshBodyData(self.body_count, ct.cast(bodies, ct.POINTER(ct.POINTER(K.IBody))))
                body = self._pick_body(bodies)
                result = None
                if body is not None:
                    joints = (K._Joint * K.JointType_Count)()
                    body.GetJoints(K.JointType_Count, ct.cast(joints, ct.POINTER(K._Joint)))
                    xyz = np.array([[joints[i].Position.x, joints[i].Position.y, joints[i].Position.z]
                                    for i in range(K_COUNT)])
                    states = np.array([joints[i].TrackingState for i in range(K_COUNT)])
                    result = kinect_to_landmarks(xyz, states, self.swap_sides)
            except COMError:
                result = None
            finally:
                frame = None  # release the COM frame promptly or the reader stalls
            dt = now - last
            last = now
            if dt > 0:
                self.measured_fps += 0.1 * (1.0 / dt - self.measured_fps)
            with self._lock:
                self._latest = result
                self._seq += 1
                self._stamp = now
                self._lock.notify_all()

    def read(self, after_seq: int, timeout: float = 1.0):
        """(seq, stamp, (points, visibility) or None) for a frame newer than after_seq, or None."""
        with self._lock:
            if not self._lock.wait_for(lambda: self._seq > after_seq or not self._running, timeout):
                return None
            if not self._running:
                return None
            return self._seq, self._stamp, self._latest

    def close(self) -> None:
        self._running = False
        self._thread.join(timeout=1.0)
        try:
            self.sensor.Close()
        except Exception:
            pass
