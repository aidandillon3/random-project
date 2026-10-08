import numpy as np

from bodytracker import calibration as calib
from bodytracker import kinect as KN
from bodytracker import skeleton as S

from .synthetic import standing_room


def kinect_joints_from_room(room, sensor_pos=(0.0, 0.8, 2.5)):
    """Fake Kinect output for a person in the room, sensor looking down -Z (at the person)."""
    j = np.zeros((KN.K_COUNT, 3))
    m = {
        KN.K_HIP_L: S.L_HIP, KN.K_HIP_R: S.R_HIP, KN.K_KNEE_L: S.L_KNEE, KN.K_KNEE_R: S.R_KNEE,
        KN.K_ANKLE_L: S.L_ANKLE, KN.K_ANKLE_R: S.R_ANKLE, KN.K_FOOT_L: S.L_TOE, KN.K_FOOT_R: S.R_TOE,
        KN.K_SHOULDER_L: S.L_SHOULDER, KN.K_SHOULDER_R: S.R_SHOULDER,
    }
    for k, i in m.items():
        j[k] = room[i]
    j[KN.K_HEAD] = 0.5 * (room[S.L_EAR] + room[S.R_EAR])
    j[KN.K_SPINE_BASE] = 0.5 * (room[S.L_HIP] + room[S.R_HIP])
    j[KN.K_SPINE_SHOULDER] = 0.5 * (room[S.L_SHOULDER] + room[S.R_SHOULDER])
    # room (x right, y up, z toward the sensor) -> Kinect space (x sensor-left, y up, z out of sensor)
    rel = j - np.asarray(sensor_pos)
    return rel * np.array([-1.0, 1.0, -1.0])  # a 180 degree turn about Y


def test_kinect_mapping_feeds_calibration_correctly():
    # person faces +Z (toward the sensor at z=2.5)
    room = standing_room(facing_yaw=np.pi)
    joints = kinect_joints_from_room(room)
    states = np.full(KN.K_COUNT, KN.TRACKED)
    pts, vis = KN.kinect_to_landmarks(joints, states)
    assert vis[S.L_KNEE] == 1.0
    cam = S.mediapipe_to_openvr_camera(pts)
    # person faces the sensor, so their chest points toward the camera (+Z in y-up camera axes)
    assert S.body_forward(cam)[2] > 0.9
    # nose sits in front of the ears, so the mirror check passes
    c = calib.solve([cam] * 3, 1.75)
    assert not c.mirror
    p = c.apply(cam)
    heel_y = 0.5 * (p[S.L_HEEL][1] + p[S.R_HEEL][1])
    assert p[S.L_TOE][1] < p[S.L_KNEE][1] < p[S.L_HIP][1]
    assert heel_y < p[S.L_ANKLE][1]


def test_kinect_inferred_joints_get_low_visibility():
    room = standing_room()
    joints = kinect_joints_from_room(room)
    states = np.full(KN.K_COUNT, KN.TRACKED)
    states[KN.K_KNEE_L] = KN.INFERRED
    states[KN.K_ANKLE_R] = 0
    _, vis = KN.kinect_to_landmarks(joints, states)
    assert vis[S.L_KNEE] < 0.5 and vis[S.R_ANKLE] == 0.0


def test_pykinect_patch_removes_32bit_checks():
    src = "# -*- coding: mbcs -*-\nx = 1\nassert sizeof(tagSTATSTG) == 72, sizeof(tagSTATSTG)\n" \
          "assert alignment(tagSTATSTG) == 8, alignment(tagSTATSTG)\n" \
          "from comtypes import _check_version; _check_version('')\ny = 2\n"
    out = KN.patch_pykinect_source(src)
    assert "tagSTATSTG" not in out and "_check_version" not in out and "coding" not in out
    assert "x = 1" in out and "y = 2" in out
