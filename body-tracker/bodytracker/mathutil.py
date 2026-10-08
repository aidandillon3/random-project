"""Small vector / rotation helpers.

Conventions used everywhere in this package unless stated otherwise:
  * "OpenVR space": right-handed, meters, +Y up, +X right, -Z forward.
  * Quaternions are (x, y, z, w) numpy arrays.
  * Rotation matrices are 3x3 with the object's local axes as columns.
"""

from __future__ import annotations

import math

import numpy as np

EPS = 1e-9


def normalize(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    if n < EPS:
        return np.zeros_like(v, dtype=float)
    return np.asarray(v, dtype=float) / n


def basis_from_up_forward(up_hint: np.ndarray, forward_hint: np.ndarray) -> np.ndarray:
    """Rotation whose local +Y is `up_hint` and local -Z is (roughly) `forward_hint`.

    `up_hint` is kept exactly; `forward_hint` is orthogonalised against it.
    Falls back to an arbitrary perpendicular if the hints are parallel.
    """
    y = normalize(up_hint)
    f = forward_hint - np.dot(forward_hint, y) * y
    if np.linalg.norm(f) < 1e-6:
        f = np.cross(y, [1.0, 0.0, 0.0])
        if np.linalg.norm(f) < 1e-6:
            f = np.cross(y, [0.0, 0.0, 1.0])
    z = -normalize(f)
    x = np.cross(y, z)
    return np.column_stack([x, y, z])


def basis_from_up_right(up_hint: np.ndarray, right_hint: np.ndarray) -> np.ndarray:
    """Rotation whose local +Y is `up_hint` and local +X is (roughly) `right_hint`."""
    y = normalize(up_hint)
    r = right_hint - np.dot(right_hint, y) * y
    if np.linalg.norm(r) < 1e-6:
        r = np.cross([0.0, 0.0, 1.0], y)
    x = normalize(r)
    z = np.cross(x, y)
    return np.column_stack([x, y, z])


def quat_from_matrix(m: np.ndarray) -> np.ndarray:
    """3x3 rotation matrix -> unit quaternion (x, y, z, w)."""
    m = np.asarray(m, dtype=float)
    tr = m[0, 0] + m[1, 1] + m[2, 2]
    if tr > 0:
        s = math.sqrt(tr + 1.0) * 2
        w = 0.25 * s
        x = (m[2, 1] - m[1, 2]) / s
        y = (m[0, 2] - m[2, 0]) / s
        z = (m[1, 0] - m[0, 1]) / s
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = math.sqrt(1.0 + m[0, 0] - m[1, 1] - m[2, 2]) * 2
        w = (m[2, 1] - m[1, 2]) / s
        x = 0.25 * s
        y = (m[0, 1] + m[1, 0]) / s
        z = (m[0, 2] + m[2, 0]) / s
    elif m[1, 1] > m[2, 2]:
        s = math.sqrt(1.0 + m[1, 1] - m[0, 0] - m[2, 2]) * 2
        w = (m[0, 2] - m[2, 0]) / s
        x = (m[0, 1] + m[1, 0]) / s
        y = 0.25 * s
        z = (m[1, 2] + m[2, 1]) / s
    else:
        s = math.sqrt(1.0 + m[2, 2] - m[0, 0] - m[1, 1]) * 2
        w = (m[1, 0] - m[0, 1]) / s
        x = (m[0, 2] + m[2, 0]) / s
        y = (m[1, 2] + m[2, 1]) / s
        z = 0.25 * s
    q = np.array([x, y, z, w])
    q = q / np.linalg.norm(q)
    if q[3] < 0:  # canonical hemisphere keeps outputs continuous
        q = -q
    return q


def matrix_from_quat(q: np.ndarray) -> np.ndarray:
    x, y, z, w = normalize(np.asarray(q, dtype=float))
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ]
    )


def yaw_matrix(yaw_rad: float) -> np.ndarray:
    """Rotation about +Y (counter-clockwise seen from above)."""
    c, s = math.cos(yaw_rad), math.sin(yaw_rad)
    return np.array([[c, 0.0, s], [0.0, 1.0, 0.0], [-s, 0.0, c]])


def rotation_between(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Smallest rotation matrix taking direction `a` onto direction `b`."""
    a = normalize(a)
    b = normalize(b)
    v = np.cross(a, b)
    c = float(np.dot(a, b))
    if c < -1 + 1e-9:
        # 180 degrees: rotate about any axis perpendicular to a
        axis = normalize(np.cross(a, [1.0, 0.0, 0.0]))
        if np.linalg.norm(axis) < 1e-6:
            axis = normalize(np.cross(a, [0.0, 1.0, 0.0]))
        return 2 * np.outer(axis, axis) - np.eye(3)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1.0 / (1.0 + c))


def heading_yaw(forward: np.ndarray) -> float:
    """Yaw (about +Y) that rotates OpenVR forward (-Z) onto the horizontal part of `forward`."""
    return math.atan2(-forward[0], -forward[2])


# --- OpenVR (right-handed) <-> Unity (left-handed) -------------------------------

def openvr_to_unity_pos(p: np.ndarray) -> np.ndarray:
    return np.array([p[0], p[1], -p[2]])


def openvr_to_unity_quat(q: np.ndarray) -> np.ndarray:
    return np.array([-q[0], -q[1], q[2], q[3]])


def unity_quat_to_euler_zxy(q: np.ndarray) -> np.ndarray:
    """Unity quaternion -> Unity euler degrees (x, y, z), applied in Z, X, Y order.

    Equivalent to Unity's Quaternion.eulerAngles, which is what VRChat expects.
    """
    # Unity uses the same quaternion algebra; handedness only lives in the data.
    m = matrix_from_quat(q)
    # For R = Ry * Rx * Rz:  m[1,2] = -sin(ex)
    sx = -m[1, 2]
    sx = max(-1.0, min(1.0, sx))
    ex = math.asin(sx)
    if abs(sx) < 0.99999:
        ey = math.atan2(m[0, 2], m[2, 2])
        ez = math.atan2(m[1, 0], m[1, 1])
    else:  # gimbal lock
        ey = math.atan2(-m[2, 0], m[0, 0])
        ez = 0.0
    return np.degrees([ex, ey, ez])
