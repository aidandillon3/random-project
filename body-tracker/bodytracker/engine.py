"""Core tracking pipeline, independent of camera, window and network.

    camera frame -> pose landmarks -> calibrate -> One Euro filter
        ... later, at send time ...
    predict to "now" -> adjust -> anchor to headset -> tracker poses

Filtering happens once per camera frame; prediction and anchoring happen per
*send*, so the output can run faster than the camera (e.g. 120 Hz from a 60 fps
webcam) and every packet is extrapolated to the moment it leaves.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass

import numpy as np

from . import calibration as calib
from .config import Config
from .filters import OneEuroPoints
from .mathutil import basis_from_up_forward, heading_yaw, normalize, quat_from_matrix, yaw_matrix
from .skeleton import (
    L_ANKLE, L_HIP, L_KNEE, LEG_LEFT, LEG_RIGHT, NUM_LANDMARKS, R_ANKLE, R_HIP, R_KNEE, USED,
    TrackerPose, body_forward, compute_trackers, face_forward, head_point, mediapipe_to_openvr_camera,
)

_LEG_POINTS = set(LEG_LEFT + LEG_RIGHT)
_USED_POS = {idx: i for i, idx in enumerate(USED)}
_LEG_L = [_USED_POS[i] for i in LEG_LEFT]
_LEG_R = [_USED_POS[i] for i in LEG_RIGHT]

# A leg-label swap is accepted only if it explains the new frame this much better.
SWAP_RATIO = 0.5
SWAP_MIN_DIST_M = 0.25


def guard_leg_swap(prev: np.ndarray | None, new: np.ndarray, mask: np.ndarray):
    """Undo MediaPipe swapping your left and right legs (common when legs cross).

    `prev`/`new` are (len(USED), 3) in the same space. If the new frame's legs line up
    far better with the previous frame when swapped, swap them back. Returns
    (points, mask, swapped).
    """
    if prev is None:
        return new, mask, False
    same = np.linalg.norm(new[_LEG_L] - prev[_LEG_L], axis=1).sum() + \
        np.linalg.norm(new[_LEG_R] - prev[_LEG_R], axis=1).sum()
    crossed = np.linalg.norm(new[_LEG_L] - prev[_LEG_R], axis=1).sum() + \
        np.linalg.norm(new[_LEG_R] - prev[_LEG_L], axis=1).sum()
    if same > SWAP_MIN_DIST_M and crossed < SWAP_RATIO * same:
        out = new.copy()
        out[_LEG_L], out[_LEG_R] = new[_LEG_R], new[_LEG_L]
        m = mask.copy()
        m[_LEG_L], m[_LEG_R] = mask[_LEG_R], mask[_LEG_L]
        return out, m, True
    return new, mask, False


def solve_knee(hip: np.ndarray, knee: np.ndarray, ankle: np.ndarray, thigh: float, shin: float):
    """Two-bone fix: keep hip and ankle, move the knee so both bones have their real length.

    The camera's depth guess often stretches or shrinks a leg; this keeps the leg
    the length it was at calibration. The knee stays on the side it was measured on.
    Returns (knee, ankle); the ankle only moves if the leg was over-stretched.
    """
    v = ankle - hip
    d = float(np.linalg.norm(v))
    if d < 1e-6 or thigh <= 0 or shin <= 0:
        return knee, ankle
    direction = v / d
    d_max = (thigh + shin) * 0.999
    d_min = abs(thigh - shin) + 1e-3
    if d > d_max:
        ankle = hip + direction * d_max
        d = d_max
    d = max(d, d_min)
    a = (thigh * thigh - shin * shin + d * d) / (2 * d)
    h = math.sqrt(max(thigh * thigh - a * a, 0.0))
    bend = knee - hip
    bend = bend - np.dot(bend, direction) * direction
    if np.linalg.norm(bend) < 1e-6:
        return hip + direction * a, ankle
    return hip + direction * a + normalize(bend) * h, ankle


@dataclass
class Output:
    trackers: list[TrackerPose]
    head_pos: np.ndarray
    head_rot: np.ndarray  # quaternion
    age_s: float  # how old the newest body data is (grows while you are out of view)


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.filter = OneEuroPoints(len(USED), cfg.smoothing, cfg.responsiveness)
        self._lock = threading.Lock()
        self._stamp: float | None = None
        self._calib_samples: list[np.ndarray] | None = None
        self.last_cam_points: np.ndarray | None = None
        self.swaps = 0  # how many leg swaps were undone (shown in the app)

    # ------------------------------------------------------------------ input
    def update(self, world_mp: np.ndarray | None, visibility: np.ndarray | None, stamp: float) -> None:
        """Feed one camera frame's result (None if no body was found)."""
        if world_mp is None:
            return
        cam = mediapipe_to_openvr_camera(world_mp)
        self.last_cam_points = cam
        if self._calib_samples is not None:
            self._calib_samples.append(cam)

        cal = self.cfg.calibration
        pts = cal.apply(cam) if cal.valid else cam
        used = pts[USED]
        mask = np.ones(len(USED), dtype=bool)
        if visibility is not None:
            for i, idx in enumerate(USED):
                if idx in _LEG_POINTS and visibility[idx] < self.cfg.min_visibility:
                    mask[i] = False
        with self._lock:
            if self.cfg.leg_swap_guard:
                used, mask, swapped = guard_leg_swap(self.filter.x, used, mask)
                self.swaps += int(swapped)
            self.filter.min_cutoff = self.cfg.smoothing
            self.filter.beta = self.cfg.responsiveness
            self.filter.update(stamp, used, mask)
            self._stamp = stamp

    # ------------------------------------------------------------ calibration
    def begin_calibration(self) -> None:
        self._calib_samples = []

    def finish_calibration(self, hmd_pose=None) -> calib.Calibration:
        samples, self._calib_samples = self._calib_samples or [], None
        hmd_forward = hmd_height = None
        if hmd_pose is not None:
            pos, rot = hmd_pose
            hmd_forward = -rot[:, 2]
            hmd_height = float(pos[1])
        result = calib.solve(samples, self.cfg.user_height_m, hmd_forward, hmd_height)
        with self._lock:
            self.cfg.calibration = result
            self.filter.reset()  # old filter state is in the old space
        return result

    @property
    def calibrating(self) -> bool:
        return self._calib_samples is not None

    # ----------------------------------------------------------------- output
    def compute(self, now: float, hmd_pose=None) -> Output | None:
        """Tracker poses for time `now`. `hmd_pose` = (pos, rot3x3) in room space, or None."""
        cfg = self.cfg
        with self._lock:
            if self.filter.x is None or self._stamp is None:
                return None
            age = now - self._stamp
            horizon = age + cfg.prediction_ms / 1000.0
            horizon = max(0.0, min(horizon, cfg.max_prediction_ms / 1000.0))
            used = self.filter.predict(horizon)
        p = np.zeros((NUM_LANDMARKS, 3))
        p[USED] = used

        bones = cfg.calibration.bone_lengths
        if cfg.lock_bone_lengths and cfg.calibration.valid and bones:
            for hip, knee, ankle, side in ((L_HIP, L_KNEE, L_ANKLE, "left"), (R_HIP, R_KNEE, R_ANKLE, "right")):
                k, a = solve_knee(p[hip], p[knee], p[ankle], bones.get(f"{side}_thigh", 0.0),
                                  bones.get(f"{side}_shin", 0.0))
                shift = a - p[ankle]
                p[knee] = k
                p[ankle] = a
                p[LEG_LEFT[2:] if side == "left" else LEG_RIGHT[2:]] += shift  # heel/toe follow the ankle

        head = head_point(p)
        # live adjustments, pivoting around the head so the head stays put
        yaw_adj = yaw_matrix(math.radians(cfg.yaw_offset_deg))
        p = head + cfg.scale_adjust * ((p - head) @ yaw_adj.T)

        # offsets are relative to the way your hips face
        hip_yaw = heading_yaw(body_forward(p))
        offset = yaw_matrix(hip_yaw) @ np.array([cfg.offset_x_m, cfg.offset_y_m, -cfg.offset_z_m])

        if hmd_pose is not None:
            hmd_pos, hmd_rot = hmd_pose
            p = p - head + np.asarray(hmd_pos) + offset
            head_pos = np.asarray(hmd_pos, dtype=float)
            head_rot = quat_from_matrix(hmd_rot)
        else:
            # no headset: stand the body on the floor. VRChat re-aligns it to your head anyway,
            # so the head we report must not include the user's offsets or they'd cancel out.
            floor = np.array([0.0, cfg.calibration.hip_height_m, 0.0])
            p = p + floor + offset
            head_pos = head + floor
            ff = face_forward(p)
            head_rot = quat_from_matrix(basis_from_up_forward(np.array([0.0, 1.0, 0.0]), ff))

        trackers = compute_trackers(p)
        for t in trackers:
            if t.name == "hip":
                t.position = t.position + np.array([0.0, cfg.hip_offset_y_m, 0.0])
        enabled = set(cfg.enabled_trackers)
        trackers = [t for t in trackers if t.name in enabled]
        return Output(trackers, head_pos, head_rot, age)

