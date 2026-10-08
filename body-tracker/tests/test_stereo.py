import math

import numpy as np
import pytest

from bodytracker import skeleton as S
from bodytracker import stereo as ST

from .synthetic import standing_room

W, H, FOV = 640, 480, 70.0


def look_at(cam_pos, target=(0.0, 1.0, 0.0)):
    """Room (y up) -> OpenCV camera rotation (rows: right, down, forward)."""
    f = np.asarray(target, float) - np.asarray(cam_pos, float)
    f /= np.linalg.norm(f)
    r = np.cross(f, [0, 1, 0])
    r /= np.linalg.norm(r)
    d = np.cross(f, r)
    return np.vstack([r, d, f])


class Cam:
    def __init__(self, pos, fov=FOV):
        self.C = np.asarray(pos, float)
        self.R = look_at(pos)
        self.K = ST.intrinsics(W, H, fov)

    def view(self, room, noise_px=0.0, rng=None):
        Xc = (room - self.C) @ self.R.T
        px = Xc[:, :2] / Xc[:, 2:3] * [self.K[0, 0], self.K[1, 1]] + self.K[:2, 2]
        if noise_px and rng is not None:
            px = px + rng.normal(0, noise_px, px.shape)
        hip = 0.5 * (room[S.L_HIP] + room[S.R_HIP])
        world = (room - hip) @ self.R.T
        return ST.ViewSample(image=px / [W, H], world=world, visibility=np.ones(33), width=W, height=H)


CAM_A = Cam((0.0, 1.2, 3.0))
CAM_B = Cam((2.8, 1.0, 0.8))


def random_pose(rng):
    room = standing_room(facing_yaw=rng.uniform(-1.5, 1.5),
                         position=(rng.uniform(-0.8, 0.8), 0.0, rng.uniform(-0.8, 0.8)))
    # lift a knee / kick a bit so the body isn't flat
    side = rng.choice([S.L_KNEE, S.R_KNEE])
    room[side] += [0, rng.uniform(0, 0.3), rng.uniform(-0.3, 0)]
    return room


def _calibrate(rng):
    cal = ST.StereoCalibrator(FOV, FOV)
    while cal.progress < 1.0:
        room = random_pose(rng)
        cal.add(CAM_A.view(room, 1.5, rng), CAM_B.view(room, 1.5, rng))
    return cal.solve()


def test_stereo_calibration_recovers_camera_pair():
    rng = np.random.default_rng(3)
    c = _calibrate(rng)
    R_true = CAM_B.R @ CAM_A.R.T
    t_true = CAM_B.R @ (CAM_A.C - CAM_B.C)
    ang = math.degrees(math.acos(np.clip((np.trace(R_true.T @ c.Rm()) - 1) / 2, -1, 1)))
    assert ang < 2.0
    assert np.dot(c.tv() / np.linalg.norm(c.tv()), t_true / np.linalg.norm(t_true)) > 0.99
    # metric scale turns the unit baseline into meters
    assert c.metric_scale == pytest.approx(np.linalg.norm(t_true), rel=0.05)
    assert c.error_px < 3.0


@pytest.fixture(scope="module")
def calib():
    return _calibrate(np.random.default_rng(5))


def _truth_in_a(room):
    hip = 0.5 * (room[S.L_HIP] + room[S.R_HIP])
    return (room - hip) @ CAM_A.R.T


def test_fuse_matches_truth(calib):
    room = random_pose(np.random.default_rng(9))
    pts, vis = ST.fuse(CAM_A.view(room), CAM_B.view(room), calib)
    legs = [S.L_HIP, S.R_HIP, S.L_KNEE, S.R_KNEE, S.L_ANKLE, S.R_ANKLE, S.L_TOE, S.R_TOE]
    assert np.abs(pts[legs] - _truth_in_a(room)[legs]).max() < 0.04
    assert vis[legs].min() > 0.9


def test_fuse_fills_leg_hidden_from_one_camera(calib):
    room = random_pose(np.random.default_rng(11))
    a = CAM_A.view(room)
    # camera A loses the left leg completely: wrong image positions, low visibility,
    # and a bad 3D guess (the crossed-leg failure)
    hidden = [S.L_KNEE, S.L_ANKLE, S.L_HEEL, S.L_TOE]
    a.visibility[hidden] = 0.1
    a.image[hidden] += 0.2
    a.world[hidden] += [0.3, 0.0, 0.0]
    pts, vis = ST.fuse(a, CAM_B.view(room), calib)
    assert np.abs(pts[hidden] - _truth_in_a(room)[hidden]).max() < 0.05


def test_fuse_undoes_left_right_swap_in_one_camera(calib):
    room = random_pose(np.random.default_rng(13))
    b = CAM_B.view(room)
    b.image = ST.swap_lr(b.image, ST.PAIRS_LEGS)
    b.world = ST.swap_lr(b.world, ST.PAIRS_LEGS)
    pts, _ = ST.fuse(CAM_A.view(room), b, calib)
    legs = [S.L_KNEE, S.R_KNEE, S.L_ANKLE, S.R_ANKLE]
    assert np.abs(pts[legs] - _truth_in_a(room)[legs]).max() < 0.04


def test_calibrator_refuses_too_little_data():
    with pytest.raises(ST.StereoCalibrationError):
        ST.StereoCalibrator(FOV, FOV).solve()
