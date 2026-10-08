"""Turn pose landmarks into hip / knee / foot tracker poses.

Landmarks come from MediaPipe Pose (33 points, "world" landmarks in meters,
origin between the hips). "Left"/"right" are the person's own left/right.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .mathutil import basis_from_up_forward, basis_from_up_right, normalize, quat_from_matrix

NOSE = 0
L_EYE, R_EYE = 2, 5
L_EAR, R_EAR = 7, 8
L_SHOULDER, R_SHOULDER = 11, 12
L_HIP, R_HIP = 23, 24
L_KNEE, R_KNEE = 25, 26
L_ANKLE, R_ANKLE = 27, 28
L_HEEL, R_HEEL = 29, 30
L_TOE, R_TOE = 31, 32  # MediaPipe calls these "foot index"

NUM_LANDMARKS = 33

# Points we actually filter and use. Everything else is ignored.
USED = [
    NOSE, L_EYE, R_EYE, L_EAR, R_EAR, L_SHOULDER, R_SHOULDER,
    L_HIP, R_HIP, L_KNEE, R_KNEE, L_ANKLE, R_ANKLE, L_HEEL, R_HEEL, L_TOE, R_TOE,
]

# Which landmarks each tracker depends on (for occlusion handling).
LEG_LEFT = [L_KNEE, L_ANKLE, L_HEEL, L_TOE]
LEG_RIGHT = [R_KNEE, R_ANKLE, R_HEEL, R_TOE]

TRACKER_NAMES = ["hip", "left_foot", "right_foot", "left_knee", "right_knee"]


def mediapipe_to_openvr_camera(points: np.ndarray) -> np.ndarray:
    """MediaPipe world axes (x right, y down, z away from camera) -> y up, z toward camera.

    This is a 180 degree turn about X (a proper rotation, no mirroring). The result
    uses OpenVR conventions; the camera looks down -Z.
    """
    out = np.array(points, dtype=float, copy=True)
    out[..., 1] *= -1.0
    out[..., 2] *= -1.0
    return out


def mid(p: np.ndarray, a: int, b: int) -> np.ndarray:
    return 0.5 * (p[a] + p[b])


def body_up(p: np.ndarray) -> np.ndarray:
    return normalize(mid(p, L_SHOULDER, R_SHOULDER) - mid(p, L_HIP, R_HIP))


def body_right(p: np.ndarray) -> np.ndarray:
    return normalize(p[R_HIP] - p[L_HIP])


def body_forward(p: np.ndarray) -> np.ndarray:
    """Chest-facing direction from the hip line (depends on left/right labels)."""
    return normalize(np.cross(body_up(p), body_right(p)))


def face_forward(p: np.ndarray) -> np.ndarray:
    """Nose-pointing direction (independent of left/right labels)."""
    return normalize(p[NOSE] - mid(p, L_EAR, R_EAR))


def head_point(p: np.ndarray) -> np.ndarray:
    """Point between the eyes: closest landmark to where a headset reports its pose."""
    return mid(p, L_EYE, R_EYE)


@dataclass
class TrackerPose:
    name: str
    position: np.ndarray  # (3,) OpenVR space
    rotation: np.ndarray  # quaternion (x, y, z, w)


def compute_trackers(p: np.ndarray) -> list[TrackerPose]:
    """Tracker poses from calibrated (y-up, metric) landmarks.

    Orientation convention per tracker (local -Z is "forward"):
      hip   +Y along the spine, +X toward the person's right
      knee  +Y up the shin (ankle -> knee), +X toward the person's right
      foot  -Z along the foot (heel -> toe), +Y roughly up the shin
    SteamVR/VRChat calibrate tracker orientation against your avatar, so only
    consistency matters, not the exact convention.
    """
    right = body_right(p)
    hip_rot = basis_from_up_right(body_up(p), right)
    hip_pos = mid(p, L_HIP, R_HIP)

    out = [TrackerPose("hip", hip_pos, quat_from_matrix(hip_rot))]

    feet = []
    knees = []
    for side, (knee, ankle, heel, toe) in (
        ("left", LEG_LEFT),
        ("right", LEG_RIGHT),
    ):
        shin_up = p[knee] - p[ankle]
        foot_fwd = p[toe] - p[heel]
        foot_rot = basis_from_up_forward(shin_up, foot_fwd)
        feet.append(TrackerPose(f"{side}_foot", p[ankle].copy(), quat_from_matrix(foot_rot)))
        knee_rot = basis_from_up_right(shin_up, right)
        knees.append(TrackerPose(f"{side}_knee", p[knee].copy(), quat_from_matrix(knee_rot)))
    return out + feet + knees
