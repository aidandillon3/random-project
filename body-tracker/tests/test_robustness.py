"""Crossed legs, swapped labels and stretched legs."""

import numpy as np
import pytest

from bodytracker import skeleton as S
from bodytracker import stereo as ST
from bodytracker.config import Config
from bodytracker.engine import Engine, guard_leg_swap, solve_knee

from .synthetic import EYE_HEIGHT, camera_rotation, standing_room, to_mediapipe
from .test_stereo import CAM_A, CAM_B, _calibrate


def _engine(**kw):
    eng = Engine(Config(prediction_ms=0, **kw))
    cam_rot = camera_rotation(10, 20)
    room = standing_room()
    eng.begin_calibration()
    for i in range(5):
        eng.update(to_mediapipe(room, cam_rot), None, i / 60)
    eng.finish_calibration((np.array([0.0, EYE_HEIGHT, 0.0]), np.eye(3)))
    return eng, cam_rot


def test_leg_swap_from_the_pose_model_is_undone():
    eng, cam_rot = _engine()
    room = standing_room()
    room[[S.R_KNEE, S.R_ANKLE, S.R_HEEL, S.R_TOE]] += [0.25, 0, 0]  # legs apart
    for i in range(20):
        eng.update(to_mediapipe(room, cam_rot), None, 1 + i / 60)
    swapped = room.copy()
    for a, b in ST.PAIRS_LEGS[1:]:  # knees, ankles, heels, toes swap labels
        swapped[[a, b]] = room[[b, a]]
    eng.update(to_mediapipe(swapped, cam_rot), None, 1 + 20 / 60)
    assert eng.swaps == 1
    out = eng.compute(1 + 20 / 60, (S.head_point(room), np.eye(3)))
    by = {t.name: t.position for t in out.trackers}
    assert np.allclose(by["left_foot"], room[S.L_ANKLE], atol=0.03)


def test_swap_guard_leaves_real_motion_alone():
    prev = np.zeros((len(S.USED), 3))
    new = prev + [0.05, 0, 0]  # whole body moved a little
    out, _, swapped = guard_leg_swap(prev, new, np.ones(len(S.USED), bool))
    assert not swapped and np.array_equal(out, new)


def test_solve_knee_restores_bone_lengths():
    hip = np.array([0.0, 1.0, 0.0])
    ankle = np.array([0.0, 0.05, -0.1])
    knee = np.array([0.0, 0.6, -0.4])  # stretched thigh
    k, a = solve_knee(hip, knee, ankle, 0.5, 0.5)
    assert np.linalg.norm(k - hip) == pytest.approx(0.5, abs=1e-9)
    assert np.linalg.norm(a - k) == pytest.approx(0.5, abs=1e-9)
    assert np.allclose(a, ankle)
    assert k[2] < -0.05  # knee still bends forward


def test_solve_knee_pulls_in_overstretched_leg():
    hip = np.array([0.0, 1.0, 0.0])
    ankle = np.array([0.0, -0.2, 0.0])
    k, a = solve_knee(hip, np.array([0.0, 0.5, -0.01]), ankle, 0.45, 0.45)
    assert np.linalg.norm(a - hip) < 0.9


def test_two_cameras_keep_crossed_leg_through_whole_pipeline():
    cal = _calibrate(np.random.default_rng(21))
    eng = Engine(Config(prediction_ms=0))
    room = standing_room()
    a, b = CAM_A.view(room), CAM_B.view(room)
    pts, vis = ST.fuse(a, b, cal)
    eng.begin_calibration()
    for i in range(5):
        eng.update(pts, vis, i / 30)
    hmd = (S.head_point(room), np.eye(3))
    eng.finish_calibration(hmd)

    # cross the left foot over in front of the right; camera A loses the left leg
    crossed = standing_room()
    leg = [S.L_KNEE, S.L_ANKLE, S.L_HEEL, S.L_TOE]
    swing = camera_rotation(-15, 0)[:, [0, 1, 2]]  # tip the leg forward...
    c, s = np.cos(np.radians(20)), np.sin(np.radians(20))
    across = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])  # ...and across the other leg
    hip = crossed[S.L_HIP]
    crossed[leg] = hip + (crossed[leg] - hip) @ (across @ swing).T
    assert crossed[S.L_ANKLE][0] > crossed[S.R_ANKLE][0]  # really crossed over
    for i in range(30):
        a, b = CAM_A.view(crossed), CAM_B.view(crossed)
        hidden = [S.L_KNEE, S.L_ANKLE, S.L_HEEL, S.L_TOE]
        a.visibility[hidden] = 0.1
        a.image[hidden] += 0.15  # the single camera's guess is way off
        pts, vis = ST.fuse(a, b, cal)
        eng.update(pts, vis, 1 + i / 30)
    out = eng.compute(1 + 29 / 30, (S.head_point(crossed), np.eye(3)))
    by = {t.name: t.position for t in out.trackers}
    assert np.allclose(by["left_foot"], crossed[S.L_ANKLE], atol=0.05)
    assert np.allclose(by["left_knee"], crossed[S.L_KNEE], atol=0.06)


def test_config_roundtrip_keeps_stereo_and_bones(tmp_path):
    cfg = Config(mode="stereo")
    cfg.stereo.valid = True
    cfg.stereo.metric_scale = 2.5
    cfg.calibration.bone_lengths = {"left_thigh": 0.44}
    path = tmp_path / "c.json"
    cfg.save(path)
    back = Config.load(path)
    assert back.mode == "stereo" and back.stereo.valid and back.stereo.metric_scale == 2.5
    assert back.calibration.bone_lengths["left_thigh"] == 0.44
