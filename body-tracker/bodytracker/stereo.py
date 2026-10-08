"""Two-camera geometry: find where the cameras are, then fuse both views.

No checkerboard needed. While you walk around, both cameras see the same joints
(knees, ankles, shoulders...). Those matching points are enough to work out how
camera B sits relative to camera A (the "essential matrix"). After that, every
joint seen by both cameras is triangulated into true 3D, and a joint hidden from
one camera (a crossed leg, say) is filled in from the camera that can still see it.

Coordinates here are OpenCV camera coordinates of camera A: x right, y down,
z away from the camera, the same axes MediaPipe uses for its world landmarks.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import skeleton as S

# Left/right landmark pairs (MediaPipe numbering) used for swap checks.
PAIRS_LEGS = [(S.L_HIP, S.R_HIP), (S.L_KNEE, S.R_KNEE), (S.L_ANKLE, S.R_ANKLE),
              (S.L_HEEL, S.R_HEEL), (S.L_TOE, S.R_TOE)]
PAIRS_UPPER = [(1, 4), (2, 5), (3, 6), (7, 8), (9, 10), (11, 12), (13, 14), (15, 16),
               (17, 18), (19, 20), (21, 22)]

# For filling in a joint from one camera: which already-placed joint it hangs off.
PARENT = {
    S.L_KNEE: S.L_HIP, S.R_KNEE: S.R_HIP,
    S.L_ANKLE: S.L_KNEE, S.R_ANKLE: S.R_KNEE,
    S.L_HEEL: S.L_ANKLE, S.R_HEEL: S.R_ANKLE,
    S.L_TOE: S.L_ANKLE, S.R_TOE: S.R_ANKLE,
}
# Order matters: parents before children.
FILL_ORDER = [S.L_KNEE, S.R_KNEE, S.L_ANKLE, S.R_ANKLE, S.L_HEEL, S.R_HEEL, S.L_TOE, S.R_TOE]

BONES = [(S.L_HIP, S.L_KNEE), (S.R_HIP, S.R_KNEE), (S.L_KNEE, S.L_ANKLE), (S.R_KNEE, S.R_ANKLE),
         (S.L_SHOULDER, S.R_SHOULDER), (S.L_HIP, S.R_HIP), (S.L_SHOULDER, S.L_HIP),
         (S.R_SHOULDER, S.R_HIP)]


def intrinsics(width: int, height: int, hfov_deg: float) -> np.ndarray:
    """Approximate camera matrix from the image size and horizontal field of view."""
    f = (width / 2.0) / math.tan(math.radians(hfov_deg) / 2.0)
    return np.array([[f, 0, width / 2.0], [0, f, height / 2.0], [0, 0, 1.0]])


def normalize_points(image_xy: np.ndarray, width: int, height: int, K: np.ndarray) -> np.ndarray:
    """MediaPipe image coords (0..1) -> normalised camera coords (x/z, y/z)."""
    px = image_xy * np.array([width, height])
    return (px - K[:2, 2]) / np.array([K[0, 0], K[1, 1]])


def swap_lr(arr: np.ndarray, pairs) -> np.ndarray:
    out = arr.copy()
    for a, b in pairs:
        out[[a, b]] = arr[[b, a]]
    return out


def triangulate(n1: np.ndarray, n2: np.ndarray, R: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Linear (DLT) triangulation of N points from normalised coords. Returns (N, 3) in camera A."""
    P1 = np.hstack([np.eye(3), np.zeros((3, 1))])
    P2 = np.hstack([R, t.reshape(3, 1)])
    n = len(n1)
    A = np.empty((n, 4, 4))
    A[:, 0] = n1[:, [0]] * P1[2] - P1[0]
    A[:, 1] = n1[:, [1]] * P1[2] - P1[1]
    A[:, 2] = n2[:, [0]] * P2[2] - P2[0]
    A[:, 3] = n2[:, [1]] * P2[2] - P2[1]
    _, _, vt = np.linalg.svd(A)
    X = vt[:, -1, :]
    w = X[:, 3:4]
    w = np.where(np.abs(w) < 1e-12, 1e-12, w)
    return X[:, :3] / w


def reprojection_error(X: np.ndarray, n1: np.ndarray, n2: np.ndarray, R: np.ndarray,
                       t: np.ndarray) -> np.ndarray:
    """Per-point worst reprojection error (normalised units) across both cameras."""
    def proj(P):
        z = np.where(np.abs(P[:, 2:3]) < 1e-9, 1e-9, P[:, 2:3])
        return P[:, :2] / z
    e1 = np.linalg.norm(proj(X) - n1, axis=1)
    e2 = np.linalg.norm(proj(X @ R.T + t) - n2, axis=1)
    behind = (X[:, 2] <= 0) | ((X @ R.T + t)[:, 2] <= 0)
    return np.where(behind, 1.0, np.maximum(e1, e2))


@dataclass
class StereoCalibration:
    R: list = field(default_factory=lambda: np.eye(3).tolist())  # camera A -> camera B rotation
    t: list = field(default_factory=lambda: [1.0, 0.0, 0.0])  # unit length; real size comes from metric_scale
    metric_scale: float = 1.0  # meters per triangulated unit
    hfov_a: float = 70.0
    hfov_b: float = 70.0
    inlier_ratio: float = 0.0
    error_px: float = 0.0
    valid: bool = False

    def Rm(self) -> np.ndarray:
        return np.asarray(self.R, dtype=float)

    def tv(self) -> np.ndarray:
        return np.asarray(self.t, dtype=float)


class StereoCalibrationError(Exception):
    pass


def _skew(v):
    return np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])


def _sampson(params, n1, n2):
    import cv2

    R, _ = cv2.Rodrigues(params[:3])
    th, ph = params[3], params[4]
    t = np.array([math.sin(th) * math.cos(ph), math.sin(th) * math.sin(ph), math.cos(th)])
    E = _skew(t) @ R
    x1 = np.hstack([n1, np.ones((len(n1), 1))])
    x2 = np.hstack([n2, np.ones((len(n2), 1))])
    Ex1 = x1 @ E.T
    Etx2 = x2 @ E
    num = np.sum(x2 * Ex1, axis=1)
    den = Ex1[:, 0] ** 2 + Ex1[:, 1] ** 2 + Etx2[:, 0] ** 2 + Etx2[:, 1] ** 2
    return num / np.sqrt(np.maximum(den, 1e-12))


def refine_pose(n1, n2, R, t, iterations: int = 30):
    """Levenberg-Marquardt polish of (R, t) on all inliers, minimising the Sampson error.

    RANSAC picks a good answer from small random subsets; this uses every point,
    which matters a lot with noisy body landmarks.
    """
    import cv2

    t = t / np.linalg.norm(t)
    rvec, _ = cv2.Rodrigues(R)
    p = np.array([*rvec.reshape(3), math.acos(np.clip(t[2], -1, 1)), math.atan2(t[1], t[0])])
    # robust weights: points far off get less say
    lam = 1e-3
    r = _sampson(p, n1, n2)
    cost = float(np.sum(r ** 2))
    for _ in range(iterations):
        J = np.empty((len(r), 5))
        for k in range(5):
            dp = np.zeros(5)
            dp[k] = 1e-6
            J[:, k] = (_sampson(p + dp, n1, n2) - r) / 1e-6
        H = J.T @ J
        g = J.T @ r
        improved = False
        for _ in range(10):
            step = np.linalg.solve(H + lam * np.diag(np.diag(H) + 1e-12), -g)
            r_new = _sampson(p + step, n1, n2)
            c_new = float(np.sum(r_new ** 2))
            if c_new < cost:
                p, r, cost = p + step, r_new, c_new
                lam = max(lam / 10, 1e-9)
                improved = True
                break
            lam *= 10
        if not improved or np.linalg.norm(step) < 1e-10:
            break
    R_out, _ = cv2.Rodrigues(p[:3])
    th, ph = p[3], p[4]
    t_out = np.array([math.sin(th) * math.cos(ph), math.sin(th) * math.sin(ph), math.cos(th)])
    return R_out, t_out


@dataclass
class ViewSample:
    """One camera's pose result: image coords, world landmarks, visibility, image size."""
    image: np.ndarray  # (33, 2) 0..1
    world: np.ndarray  # (33, 3) MediaPipe world, metric, hip-centred, camera axes
    visibility: np.ndarray  # (33,)
    width: int
    height: int


class StereoCalibrator:
    """Collects matching joints while you walk around, then solves the camera pair."""

    MIN_FRAMES = 150
    MIN_POINTS = 3000

    def __init__(self, hfov_a: float, hfov_b: float, min_visibility: float = 0.8):
        self.hfov_a = hfov_a
        self.hfov_b = hfov_b
        self.min_vis = min_visibility
        self.n1: list[np.ndarray] = []
        self.n2: list[np.ndarray] = []
        self.frames: list[tuple[ViewSample, ViewSample]] = []

    def add(self, a: ViewSample, b: ViewSample) -> None:
        Ka = intrinsics(a.width, a.height, self.hfov_a)
        Kb = intrinsics(b.width, b.height, self.hfov_b)
        ok = (a.visibility > self.min_vis) & (b.visibility > self.min_vis)
        ok[:11] = False  # face points are tiny and jittery; the body is plenty
        if ok.sum() < 6:
            return
        self.n1.append(normalize_points(a.image[ok], a.width, a.height, Ka))
        self.n2.append(normalize_points(b.image[ok], b.width, b.height, Kb))
        self.frames.append((a, b))

    @property
    def progress(self) -> float:
        pts = sum(len(x) for x in self.n1)
        return min(1.0, min(len(self.frames) / self.MIN_FRAMES, pts / self.MIN_POINTS))

    def solve(self) -> StereoCalibration:
        import cv2

        if self.progress < 1.0:
            raise StereoCalibrationError(
                "Not enough data yet. Keep walking around where both cameras can see all of you.")
        n1 = np.concatenate(self.n1)
        n2 = np.concatenate(self.n2)
        # threshold is in normalised units: 0.012 is about 5 px on a 640 px wide 70 degree camera
        E, mask = cv2.findEssentialMat(n1, n2, focal=1.0, pp=(0.0, 0.0), method=cv2.RANSAC,
                                       prob=0.999, threshold=0.012)
        if E is None or E.shape != (3, 3):
            raise StereoCalibrationError(
                "Could not work out the camera positions. Try moving the cameras further apart.")
        _, R, t, mask2 = cv2.recoverPose(E, n1, n2, focal=1.0, pp=(0.0, 0.0), mask=mask.copy())
        t = t.reshape(3)
        inliers = mask2.reshape(-1).astype(bool)
        R, t = refine_pose(n1[inliers], n2[inliers], R, t)
        ratio = float(inliers.mean())
        if ratio < 0.4:
            raise StereoCalibrationError(
                "The two views didn't agree well. Make sure both cameras see your whole body, then try again.")
        X = triangulate(n1[inliers], n2[inliers], R, t)
        err = float(np.median(reprojection_error(X, n1[inliers], n2[inliers], R, t)))

        # real-world size: compare bone lengths from MediaPipe (meters) with triangulated ones
        ratios = []
        for a, b in self.frames:
            Ka = intrinsics(a.width, a.height, self.hfov_a)
            Kb = intrinsics(b.width, b.height, self.hfov_b)
            Xf = triangulate(normalize_points(a.image, a.width, a.height, Ka),
                             normalize_points(b.image, b.width, b.height, Kb), R, t)
            for i, j in BONES:
                if min(a.visibility[i], a.visibility[j], b.visibility[i], b.visibility[j]) < self.min_vis:
                    continue
                tri = np.linalg.norm(Xf[i] - Xf[j])
                mp_len = 0.5 * (np.linalg.norm(a.world[i] - a.world[j]) + np.linalg.norm(b.world[i] - b.world[j]))
                if tri > 1e-6:
                    ratios.append(mp_len / tri)
        if not ratios:
            raise StereoCalibrationError("Could not measure your size from both cameras.")
        fx = intrinsics(self.frames[0][0].width, self.frames[0][0].height, self.hfov_a)[0, 0]
        return StereoCalibration(
            R=R.tolist(), t=t.tolist(), metric_scale=float(np.median(ratios)),
            hfov_a=self.hfov_a, hfov_b=self.hfov_b, inlier_ratio=ratio, error_px=err * fx, valid=True,
        )


def _best_swap(na, nb, vis_ok, R, t):
    """Pick how to read camera B's left/right labels so both views agree best."""
    candidates = [[], PAIRS_LEGS + PAIRS_UPPER, PAIRS_LEGS]
    best = None
    for pairs in candidates:
        nb_c = swap_lr(nb, pairs) if pairs else nb
        ok = vis_ok(pairs)
        if ok.sum() < 3:
            continue
        X = triangulate(na[ok], nb_c[ok], R, t)
        e = float(np.mean(np.minimum(reprojection_error(X, na[ok], nb_c[ok], R, t), 0.1)))
        if best is None or e < best[0] - 1e-4:
            best = (e, pairs)
    return best[1] if best else []


def fuse(a: ViewSample, b: ViewSample, cal: StereoCalibration, min_visibility: float = 0.5,
         max_error: float = 0.03):
    """Combine two views into one set of 33 metric 3D points (camera A axes, hip-centred).

    Returns (points (33, 3), visibility (33,)).
    """
    R, t = cal.Rm(), cal.tv()
    Ka = intrinsics(a.width, a.height, cal.hfov_a)
    Kb = intrinsics(b.width, b.height, cal.hfov_b)
    na = normalize_points(a.image, a.width, a.height, Ka)
    nb = normalize_points(b.image, b.width, b.height, Kb)
    va, vb = a.visibility.copy(), b.visibility.copy()

    # MediaPipe sometimes swaps left and right in one view (most often when seen from behind or
    # with legs crossed). Read camera B's labels whichever way agrees with camera A.
    def vis_ok(pairs):
        vb_c = swap_lr(vb, pairs) if pairs else vb
        ok = (va > min_visibility) & (vb_c > min_visibility)
        ok[:11] = False
        return ok

    pairs = _best_swap(na, nb, vis_ok, R, t)
    world_b = b.world
    if pairs:
        nb = swap_lr(nb, pairs)
        vb = swap_lr(vb, pairs)
        world_b = swap_lr(b.world, pairs)

    X = triangulate(na, nb, R, t) * cal.metric_scale
    err = reprojection_error(X / cal.metric_scale, na, nb, R, t)
    both = (va > min_visibility) & (vb > min_visibility) & (err < max_error)

    # Each camera's own 3D guess, rotated into camera A's axes (directions only matter).
    world_a = a.world
    world_b_in_a = world_b @ R  # R^T applied to row vectors

    out = np.where(both[:, None], X, np.nan)
    vis = np.where(both, np.maximum(va, vb), 0.0)

    def single(i):
        """Best single-camera guess for joint i relative to its parent (or None)."""
        if va[i] >= vb[i]:
            return world_a, va[i]
        return world_b_in_a, vb[i]

    # hips: if one is missing, place it from the other using whichever camera sees it
    for h, other in ((S.L_HIP, S.R_HIP), (S.R_HIP, S.L_HIP)):
        if np.isnan(out[h, 0]) and not np.isnan(out[other, 0]):
            w, v = single(h)
            out[h] = out[other] + (w[h] - w[other])
            vis[h] = v * 0.9
    if np.isnan(out[S.L_HIP, 0]) or np.isnan(out[S.R_HIP, 0]):
        # no stereo body at all: fall back to the more confident single camera
        if va[S.L_HIP] + va[S.R_HIP] >= vb[S.L_HIP] + vb[S.R_HIP]:
            return world_a.copy(), va
        return world_b_in_a.copy(), vb

    for i in FILL_ORDER:
        if np.isnan(out[i, 0]):
            w, v = single(i)
            p = PARENT[i]
            out[i] = out[p] + (w[i] - w[p])
            vis[i] = v * 0.9
    # anything else (face, arms): copy from camera A's guess, positioned by the hips
    rest = np.isnan(out[:, 0])
    if rest.any():
        hip_mid = 0.5 * (out[S.L_HIP] + out[S.R_HIP])
        hip_a = 0.5 * (world_a[S.L_HIP] + world_a[S.R_HIP])
        out[rest] = hip_mid + (world_a[rest] - hip_a)
        vis[rest] = np.minimum(va[rest], 0.5)

    hip_mid = 0.5 * (out[S.L_HIP] + out[S.R_HIP])
    return out - hip_mid, vis
