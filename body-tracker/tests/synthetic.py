"""A fake person, so the math can be tested without a camera."""

from __future__ import annotations

import numpy as np

from bodytracker import skeleton as S
from bodytracker.mathutil import yaw_matrix

# A 1.75 m person standing in OpenVR room axes, facing -Z, feet on the floor.
# The person's right is +X.
_STAND = {
    S.NOSE: (0.0, 1.62, -0.10),
    S.L_EYE: (-0.03, 1.65, -0.08), S.R_EYE: (0.03, 1.65, -0.08),
    S.L_EAR: (-0.075, 1.62, 0.0), S.R_EAR: (0.075, 1.62, 0.0),
    S.L_SHOULDER: (-0.18, 1.45, 0.0), S.R_SHOULDER: (0.18, 1.45, 0.0),
    S.L_HIP: (-0.10, 0.95, 0.0), S.R_HIP: (0.10, 0.95, 0.0),
    S.L_KNEE: (-0.10, 0.50, -0.02), S.R_KNEE: (0.10, 0.50, -0.02),
    S.L_ANKLE: (-0.10, 0.09, 0.0), S.R_ANKLE: (0.10, 0.09, 0.0),
    S.L_HEEL: (-0.10, 0.04, 0.05), S.R_HEEL: (0.10, 0.04, 0.05),
    S.L_TOE: (-0.10, 0.02, -0.15), S.R_TOE: (0.10, 0.02, -0.15),
}

EYE_HEIGHT = 1.65


def standing_room(facing_yaw: float = 0.0, position=(0.0, 0.0, 0.0)) -> np.ndarray:
    """(33, 3) landmarks in room space. Unused landmarks sit at the hip centre."""
    p = np.zeros((S.NUM_LANDMARKS, 3))
    p[:] = (0.0, 0.95, 0.0)
    for i, v in _STAND.items():
        p[i] = v
    return p @ yaw_matrix(facing_yaw).T + np.asarray(position)


def camera_rotation(tilt_deg: float, yaw_deg: float) -> np.ndarray:
    """Room -> camera rotation for a camera pitched down by `tilt_deg` and turned by `yaw_deg`."""
    t = np.radians(tilt_deg)
    pitch = np.array([[1, 0, 0], [0, np.cos(t), -np.sin(t)], [0, np.sin(t), np.cos(t)]])
    return pitch @ yaw_matrix(np.radians(yaw_deg))


def to_mediapipe(room: np.ndarray, cam_rot: np.ndarray, model_scale: float = 1.0,
                 mirror: bool = False) -> np.ndarray:
    """What MediaPipe would report: hip-centred, camera axes (x right, y down, z away), its own scale."""
    hip = 0.5 * (room[S.L_HIP] + room[S.R_HIP])
    cam = (room - hip) @ cam_rot.T * model_scale
    if mirror:
        cam[:, 0] *= -1
    return S.mediapipe_to_openvr_camera(cam)  # the flip is its own inverse
