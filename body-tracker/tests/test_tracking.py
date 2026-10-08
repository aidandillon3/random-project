import math
import socket
import struct

import numpy as np
import pytest

from bodytracker import calibration as calib
from bodytracker import skeleton as S
from bodytracker.config import Config
from bodytracker.engine import Engine
from bodytracker.filters import OneEuroPoints
from bodytracker.mathutil import (
    matrix_from_quat, openvr_to_unity_quat, quat_from_matrix, unity_quat_to_euler_zxy, yaw_matrix,
)
from bodytracker.osc import encode_message
from bodytracker.outputs import VmtOutput, VrchatOutput

from .synthetic import EYE_HEIGHT, camera_rotation, standing_room, to_mediapipe


# ----------------------------------------------------------------------------- math

def _axis_quat(axis, deg):
    h = math.radians(deg) / 2
    a = np.asarray(axis, dtype=float) * math.sin(h)
    return np.array([a[0], a[1], a[2], math.cos(h)])


def _qmul(a, b):
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return np.array([
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
        aw * bw - ax * bx - ay * by - az * bz,
    ])


@pytest.mark.parametrize("ex,ey,ez", [(10, 20, 30), (-40, 170, 5), (0, -90, 0), (60, 0, -120)])
def test_unity_euler_roundtrip(ex, ey, ez):
    # Unity: Quaternion.Euler(x, y, z) = qY * qX * qZ
    q = _qmul(_qmul(_axis_quat([0, 1, 0], ey), _axis_quat([1, 0, 0], ex)), _axis_quat([0, 0, 1], ez))
    e = unity_quat_to_euler_zxy(q)
    q2 = _qmul(_qmul(_axis_quat([0, 1, 0], e[1]), _axis_quat([1, 0, 0], e[0])), _axis_quat([0, 0, 1], e[2]))
    assert abs(abs(np.dot(q, q2)) - 1) < 1e-9


def test_quat_matrix_roundtrip():
    rng = np.random.default_rng(1)
    for _ in range(50):
        q = rng.normal(size=4)
        q /= np.linalg.norm(q)
        q2 = quat_from_matrix(matrix_from_quat(q))
        assert abs(abs(np.dot(q, q2)) - 1) < 1e-9


def test_openvr_to_unity_quat_is_mirror_of_rotation():
    # A yaw of +30 degrees in OpenVR (CCW from above) is -30 in Unity's left-handed frame.
    q = quat_from_matrix(yaw_matrix(math.radians(30)))
    e = unity_quat_to_euler_zxy(openvr_to_unity_quat(q))
    assert e[1] == pytest.approx(-30, abs=1e-6)


# --------------------------------------------------------------------------- filter

def test_one_euro_removes_jitter_when_still():
    rng = np.random.default_rng(0)
    f = OneEuroPoints(1, min_cutoff=1.0, beta=0.5)
    out = []
    for i in range(300):
        out.append(f.update(i / 60, np.array([[1.0, 2.0, 3.0]]) + rng.normal(0, 0.01, (1, 3)))[0].copy())
    out = np.array(out[60:])
    assert np.std(out, axis=0).max() < 0.004  # raw jitter is 0.01
    assert np.allclose(out.mean(axis=0), [1, 2, 3], atol=0.003)


def test_one_euro_prediction_cancels_lag_on_steady_motion():
    f = OneEuroPoints(1, min_cutoff=1.0, beta=1.0)
    v = 1.0  # m/s
    for i in range(120):
        t = i / 60
        f.update(t, np.array([[v * t, 0, 0]]))
    t = 119 / 60
    lag = v * t - f.x[0, 0]
    predicted = f.predict(lag / v)[0, 0]  # look ahead by the lag
    assert lag > 0.005
    assert abs(predicted - v * t) < 0.2 * lag


def test_one_euro_holds_masked_points():
    f = OneEuroPoints(2)
    f.update(0.0, np.zeros((2, 3)))
    f.update(1 / 60, np.ones((2, 3)), mask=np.array([True, False]))
    assert np.all(f.x[1] == 0)
    assert np.all(f.x[0] > 0)


# ---------------------------------------------------------------------- calibration

@pytest.mark.parametrize("tilt,cam_yaw,model_scale,facing,mirror", [
    (0, 0, 1.0, 0, False),
    (25, 0, 0.9, 0, False),  # camera on a desk pointing up at you... or down from a shelf
    (-15, 40, 1.1, 0.6, False),
    (10, -70, 0.85, -1.2, True),  # mirrored output gets detected and undone
])
def test_calibration_recovers_upright_scaled_body(tilt, cam_yaw, model_scale, facing, mirror):
    room = standing_room(facing_yaw=facing)
    mp = to_mediapipe(room, camera_rotation(tilt, cam_yaw), model_scale, mirror)
    cam = S.mediapipe_to_openvr_camera(mp)
    hmd_forward = yaw_matrix(facing) @ np.array([0, 0, -1.0])
    c = calib.solve([cam] * 5, user_height_m=1.80, hmd_forward=hmd_forward, hmd_eye_height_m=EYE_HEIGHT)
    assert c.mirror == mirror
    p = c.apply(cam)
    hip = 0.5 * (room[S.L_HIP] + room[S.R_HIP])
    # recovered body equals the real body up to translation
    assert np.allclose(p, room - hip, atol=0.03)


def test_calibration_without_headset_uses_height_and_faces_minus_z():
    room = standing_room(facing_yaw=0.0)
    cam = S.mediapipe_to_openvr_camera(to_mediapipe(room, camera_rotation(20, 30), 1.0))
    c = calib.solve([cam] * 3, user_height_m=1.90)
    p = c.apply(cam)
    eye_height = S.head_point(p)[1] - 0.5 * (p[S.L_HEEL][1] + p[S.R_HEEL][1]) + calib.HEEL_ABOVE_FLOOR_M
    assert eye_height == pytest.approx(1.90 * calib.EYE_HEIGHT_RATIO, abs=1e-6)
    assert S.body_forward(p)[2] < -0.95


def test_calibration_rejects_too_few_frames():
    with pytest.raises(calib.CalibrationError):
        calib.solve([], 1.75)


# -------------------------------------------------------------------------- engine

def _calibrated_engine(**cfg_kw):
    cfg = Config(**cfg_kw)
    eng = Engine(cfg)
    cam_rot = camera_rotation(15, 25)
    room = standing_room()
    eng.begin_calibration()
    for i in range(5):
        eng.update(to_mediapipe(room, cam_rot, 0.9), None, i / 60)
    hmd = (np.array([0.0, EYE_HEIGHT, 0.0]), np.eye(3))
    eng.finish_calibration(hmd)
    eng.update(to_mediapipe(room, cam_rot, 0.9), None, 0.5)
    return eng, cam_rot


def test_engine_places_trackers_on_body_under_headset():
    eng, cam_rot = _calibrated_engine(prediction_ms=0)
    where = np.array([1.0, 0.0, -2.0])
    facing = math.radians(70)
    room = standing_room(facing_yaw=facing, position=where)
    for i in range(30):
        eng.update(to_mediapipe(room, cam_rot, 0.9), None, 1 + i / 60)
    hmd_rot = yaw_matrix(facing)
    hmd = (S.head_point(room), hmd_rot)
    out = eng.compute(1 + 29 / 60, hmd)
    by = {t.name: t for t in out.trackers}
    assert np.allclose(by["hip"].position, 0.5 * (room[S.L_HIP] + room[S.R_HIP]), atol=0.03)
    assert np.allclose(by["left_foot"].position, room[S.L_ANKLE], atol=0.03)
    assert np.allclose(by["right_knee"].position, room[S.R_KNEE], atol=0.03)
    # feet end up just above the floor
    assert 0.0 < by["left_foot"].position[1] < 0.15
    # foot tracker -Z points along the foot, which faces where the person faces
    foot_fwd = -matrix_from_quat(by["right_foot"].rotation)[:, 2]
    assert np.dot(foot_fwd, yaw_matrix(facing) @ [0, 0, -1]) > 0.95
    hip_fwd = -matrix_from_quat(by["hip"].rotation)[:, 2]
    assert np.dot(hip_fwd, yaw_matrix(facing) @ [0, 0, -1]) > 0.95


def test_engine_live_adjustments():
    eng, cam_rot = _calibrated_engine(prediction_ms=0)
    room = standing_room()
    for i in range(30):
        eng.update(to_mediapipe(room, cam_rot, 0.9), None, 1 + i / 60)
    hmd = (S.head_point(room), np.eye(3))
    base = {t.name: t.position for t in eng.compute(1.5, hmd).trackers}
    eng.cfg.offset_z_m = 0.1  # forward = -Z when facing -Z
    eng.cfg.hip_offset_y_m = -0.05
    moved = {t.name: t.position for t in eng.compute(1.5, hmd).trackers}
    assert moved["left_foot"] - base["left_foot"] == pytest.approx([0, 0, -0.1], abs=1e-6)
    assert moved["hip"] - base["hip"] == pytest.approx([0, -0.05, -0.1], abs=1e-6)


def test_engine_prediction_leads_motion():
    eng, cam_rot = _calibrated_engine(prediction_ms=30, smoothing=1.0, responsiveness=1.0)
    v = 1.5  # kick the right foot forward at 1.5 m/s, everything else still
    t = 0.0
    for i in range(40):
        t = 1 + i / 60
        room = standing_room()
        room[[S.R_ANKLE, S.R_HEEL, S.R_TOE]] += [0, 0, -v * i / 60]
        eng.update(to_mediapipe(room, cam_rot, 0.9), None, t)
    truth = room[S.R_ANKLE][2]
    hmd = (S.head_point(room), np.eye(3))
    eng.cfg.prediction_ms = 0
    lagging = {x.name: x for x in eng.compute(t, hmd).trackers}["right_foot"].position[2]
    eng.cfg.prediction_ms = 30
    predicted = {x.name: x for x in eng.compute(t, hmd).trackers}["right_foot"].position[2]
    assert abs(predicted - truth) < abs(lagging - truth)


def test_engine_without_headset_sends_head_for_vrchat_alignment():
    cfg = Config(prediction_ms=0)
    eng = Engine(cfg)
    room = standing_room()
    cam_rot = camera_rotation(0, 0)
    eng.begin_calibration()
    for i in range(5):
        eng.update(to_mediapipe(room, cam_rot), None, i / 60)
    eng.finish_calibration(None)
    eng.update(to_mediapipe(room, cam_rot), None, 5 / 60)
    out = eng.compute(5 / 60, None)
    by = {t.name: t for t in out.trackers}
    # head -> foot vector is preserved, so VRChat's head alignment puts feet in place
    assert np.allclose(by["left_foot"].position - out.head_pos, room[S.L_ANKLE] - S.head_point(room), atol=0.05)


def test_enabled_trackers_filter():
    eng, cam_rot = _calibrated_engine(enabled_trackers=["hip", "left_foot", "right_foot"])
    out = eng.compute(1.0, None)
    assert [t.name for t in out.trackers] == ["hip", "left_foot", "right_foot"]


# --------------------------------------------------------------------------- output

def test_osc_encoding_matches_spec():
    msg = encode_message("/a", 1, 2.0, "hi")
    assert msg == (b"/a\0\0" + b",ifs\0\0\0\0" + struct.pack(">i", 1) + struct.pack(">f", 2.0) + b"hi\0\0")


def _recv_all(sock, n):
    out = []
    for _ in range(n):
        data, _ = sock.recvfrom(4096)
        out.append(data)
    return out


def _decode(msg: bytes):
    def read_str(b, i):
        end = b.index(b"\0", i)
        s = b[i:end].decode()
        return s, (end + 4) & ~3
    addr, i = read_str(msg, 0)
    tags, i = read_str(msg, i)
    args = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack(">i", msg[i:i + 4])[0]); i += 4
        elif t == "f":
            args.append(struct.unpack(">f", msg[i:i + 4])[0]); i += 4
        elif t == "s":
            s, i = read_str(msg, i); args.append(s)
    return addr, args


@pytest.fixture
def udp():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(2)
    yield s
    s.close()


def test_vmt_output_packets(udp):
    eng, _ = _calibrated_engine()
    out = eng.compute(1.0, (np.array([0, EYE_HEIGHT, 0.0]), np.eye(3)))
    vmt = VmtOutput("127.0.0.1", udp.getsockname()[1], first_index=3)
    vmt.send(out.trackers)
    msgs = [_decode(m) for m in _recv_all(udp, 5)]
    assert all(a == "/VMT/Room/Driver" for a, _ in msgs)
    assert [args[0] for _, args in msgs] == [3, 4, 5, 6, 7]
    assert all(args[1] == 1 and len(args) == 10 for _, args in msgs)
    vmt.close()


def test_vrchat_output_packets(udp):
    eng, _ = _calibrated_engine()
    out = eng.compute(1.0, None)
    vrc = VrchatOutput("127.0.0.1", udp.getsockname()[1])
    vrc.send(out.trackers, out.head_pos, out.head_rot)
    msgs = dict(_decode(m) for m in _recv_all(udp, 12))
    for slot in range(1, 6):
        assert len(msgs[f"/tracking/trackers/{slot}/position"]) == 3
        assert len(msgs[f"/tracking/trackers/{slot}/rotation"]) == 3
    # Unity flips Z: foot at room z=0 stays 0, hip y positive
    assert msgs["/tracking/trackers/head/position"][1] > 1.0
    vrc.close()


def test_engine_without_headset_stands_on_floor():
    eng, _ = _calibrated_engine()
    out = eng.compute(1.0, None)
    by = {t.name: t for t in out.trackers}
    assert 0.0 < by["left_foot"].position[1] < 0.15
    assert out.head_pos[1] == pytest.approx(EYE_HEIGHT, abs=0.03)


def test_occluded_point_returning_does_not_spike_velocity():
    f = OneEuroPoints(1)
    f.update(0.0, np.zeros((1, 3)))
    for i in range(1, 10):
        f.update(i / 60, np.ones((1, 3)), mask=np.array([False]))
    f.update(10 / 60, np.ones((1, 3)) * 0.5)
    assert np.abs(f.dx).max() < 1e-6


def test_offsets_move_trackers_relative_to_reported_head_without_headset():
    eng, _ = _calibrated_engine()
    base = eng.compute(1.0, None)
    eng.cfg.offset_y_m = 0.1
    moved = eng.compute(1.0, None)
    foot = lambda o: {t.name: t for t in o.trackers}["left_foot"].position - o.head_pos  # noqa: E731
    assert foot(moved) - foot(base) == pytest.approx([0, 0.1, 0], abs=1e-6)
