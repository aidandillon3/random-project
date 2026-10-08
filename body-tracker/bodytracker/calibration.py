"""Work out how the camera sits relative to you and the room.

Calibration only needs you to stand up straight, facing the camera, for about a
second. From that pose we measure:

  * tilt   which way is "up" in the camera's view (camera can be tilted/angled)
  * yaw    which way you were facing, so it can be matched to the headset's facing
  * scale  how tall the pose model thinks you are vs. how tall you actually are
  * mirror whether left/right came out mirrored (auto-detected and fixed)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .mathutil import heading_yaw, normalize, rotation_between, yaw_matrix
from .skeleton import (
    L_ANKLE, L_EAR, L_HEEL, L_HIP, L_KNEE, L_SHOULDER, R_ANKLE, R_EAR, R_HEEL, R_HIP, R_KNEE, R_SHOULDER,
    body_forward, face_forward, head_point, mid,
)

# Eye height is about 93.3% of standing height for adults.
EYE_HEIGHT_RATIO = 0.933
# Heel landmark sits a few cm above the floor.
HEEL_ABOVE_FLOOR_M = 0.03


@dataclass
class Calibration:
    rotation: list = field(default_factory=lambda: np.eye(3).tolist())  # camera -> room
    scale: float = 1.0
    hip_height_m: float = 0.95  # standing hip height, used to put the floor at y=0 without a headset
    bone_lengths: dict = field(default_factory=dict)  # left_thigh, left_shin, ... in meters
    mirror: bool = False
    valid: bool = False

    def matrix(self) -> np.ndarray:
        return np.asarray(self.rotation, dtype=float)

    def apply(self, cam_points: np.ndarray) -> np.ndarray:
        """Camera-space landmarks (y-up OpenVR axes) -> calibrated room-aligned, metric."""
        p = np.asarray(cam_points, dtype=float)
        if self.mirror:
            p = p * np.array([-1.0, 1.0, 1.0])
        return self.scale * (p @ self.matrix().T)


class CalibrationError(Exception):
    pass


def solve(
    samples: list[np.ndarray],
    user_height_m: float,
    hmd_forward: np.ndarray | None = None,
    hmd_eye_height_m: float | None = None,
) -> Calibration:
    """Solve calibration from a few frames of standing-still landmarks.

    samples          list of (33, 3) landmark arrays in y-up camera axes
    user_height_m    your height, used when no headset height is available
    hmd_forward      headset forward vector in room space at the same moment, if known
    hmd_eye_height_m headset height above the floor at the same moment, if known
    """
    if len(samples) < 3:
        raise CalibrationError("Not enough pose frames - make sure your whole body is visible.")
    p = np.median(np.stack(samples), axis=0)

    # 1. mirror check: the hip-line forward and the nose forward must agree.
    mirror = False
    if np.dot(body_forward(p), face_forward(p)) < 0:
        mirror = True
        p = p * np.array([-1.0, 1.0, 1.0])

    # 2. tilt: standing straight, ears and shoulders sit right above the ankles
    #    (the "plumb line" posture). Eyes are further forward, so they're not used.
    feet = mid(p, L_ANKLE, R_ANKLE)
    upper = 0.5 * (mid(p, L_EAR, R_EAR) + mid(p, L_SHOULDER, R_SHOULDER))
    up_cam = normalize(upper - feet)
    if np.linalg.norm(up_cam) < 0.5:
        raise CalibrationError("Could not see you clearly - stand up straight, whole body in view.")
    tilt = rotation_between(up_cam, np.array([0.0, 1.0, 0.0]))

    # 3. yaw: line up your facing with the headset's facing (or with -Z if no headset).
    fwd = tilt @ body_forward(p)
    fwd_h = np.array([fwd[0], 0.0, fwd[2]])
    if np.linalg.norm(fwd_h) < 0.2:
        raise CalibrationError("Could not tell which way you are facing - face the camera.")
    current_yaw = heading_yaw(fwd_h)
    target_yaw = 0.0
    if hmd_forward is not None:
        hf = np.asarray(hmd_forward, dtype=float)
        if math.hypot(hf[0], hf[2]) > 0.2:
            target_yaw = heading_yaw(hf)
    yaw = yaw_matrix(target_yaw - current_yaw)
    rot = yaw @ tilt

    # 4. scale: measured eye height above the heels vs. real eye height.
    q = p @ rot.T
    heel_y = 0.5 * (q[L_HEEL][1] + q[R_HEEL][1])
    eyes_above_heels = head_point(q)[1] - heel_y
    if eyes_above_heels < 0.3:
        raise CalibrationError("Pose looks too short - is your whole body, feet included, in view?")
    if hmd_eye_height_m is not None and hmd_eye_height_m > 0.8:
        target = hmd_eye_height_m
    else:
        target = user_height_m * EYE_HEIGHT_RATIO
    scale = (target - HEEL_ABOVE_FLOOR_M) / eyes_above_heels
    hip_height = scale * (-heel_y) + HEEL_ABOVE_FLOOR_M  # landmarks are centred on the hips

    m = q * scale
    bones = {
        "left_thigh": float(np.linalg.norm(m[L_HIP] - m[L_KNEE])),
        "right_thigh": float(np.linalg.norm(m[R_HIP] - m[R_KNEE])),
        "left_shin": float(np.linalg.norm(m[L_KNEE] - m[L_ANKLE])),
        "right_shin": float(np.linalg.norm(m[R_KNEE] - m[R_ANKLE])),
    }
    # both legs are the same length in real life; averaging halves the noise
    for part in ("thigh", "shin"):
        avg = 0.5 * (bones[f"left_{part}"] + bones[f"right_{part}"])
        bones[f"left_{part}"] = bones[f"right_{part}"] = avg

    return Calibration(rotation=rot.tolist(), scale=float(scale), hip_height_m=float(hip_height),
                       bone_lengths=bones, mirror=mirror, valid=True)
