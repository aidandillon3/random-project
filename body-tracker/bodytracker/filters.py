"""One Euro filter, vectorised over many 3D points.

The One Euro filter (Casiez et al. 2012) is a low-pass filter whose cutoff rises
with speed: when you stand still it smooths hard (no jitter), when you move fast
it barely smooths (no lag). Two knobs:

  min_cutoff  lower = smoother when still, higher = snappier when still (Hz)
  beta        higher = less lag during fast motion

It also keeps a smoothed velocity, which we reuse for latency-hiding prediction.
"""

from __future__ import annotations

import math

import numpy as np


def _alpha(cutoff: np.ndarray | float, dt: float) -> np.ndarray | float:
    tau = 1.0 / (2.0 * math.pi * np.asarray(cutoff))
    return 1.0 / (1.0 + tau / dt)


class OneEuroPoints:
    """Filters an (N, 3) array of points. Points can be skipped per update (occlusion)."""

    def __init__(self, n: int, min_cutoff: float = 1.5, beta: float = 0.5, d_cutoff: float = 1.0):
        self.n = n
        self.min_cutoff = min_cutoff
        self.beta = beta
        self.d_cutoff = d_cutoff
        self.x: np.ndarray | None = None  # filtered positions (N, 3)
        self.dx = np.zeros((n, 3))  # filtered velocity, units/second
        self.raw_prev: np.ndarray | None = None
        self.held = np.zeros(n, dtype=bool)  # points that were masked out last update
        self.t: float | None = None

    def reset(self) -> None:
        self.x = None
        self.raw_prev = None
        self.held[:] = False
        self.dx[:] = 0.0
        self.t = None

    def update(self, t: float, raw: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
        """Feed raw (N, 3) points sampled at time `t` (seconds).

        `mask` (N,) bool: False means "this point is unreliable this frame"; it keeps
        its previous value and its velocity decays toward zero so prediction does not
        fling an occluded foot across the room.
        """
        raw = np.asarray(raw, dtype=float)
        if mask is None:
            mask = np.ones(self.n, dtype=bool)
        if self.x is None or self.t is None or self.raw_prev is None:
            self.x = raw.copy()
            self.raw_prev = raw.copy()
            self.dx[:] = 0.0
            self.t = t
            return self.x
        dt = t - self.t
        if dt <= 1e-6:
            return self.x
        dt = min(dt, 0.25)  # after a long stall, don't treat the jump as velocity
        self.t = t

        m = mask[:, None]
        # velocity from consecutive raw samples, smoothed with a fixed cutoff
        dx_raw = (raw - self.raw_prev) / dt
        # a point coming back from occlusion jumps; that jump is not velocity
        dx_raw = np.where(self.held[:, None], self.dx, dx_raw)
        a_d = _alpha(self.d_cutoff, dt)
        dx_new = self.dx + a_d * (dx_raw - self.dx)
        # speed-adaptive cutoff per point
        speed = np.linalg.norm(dx_new, axis=1)
        cutoff = self.min_cutoff + self.beta * speed
        a = _alpha(cutoff, dt)[:, None]
        x_new = self.x + a * (raw - self.x)

        self.dx = np.where(m, dx_new, self.dx * 0.5)
        self.x = np.where(m, x_new, self.x)
        self.raw_prev = np.where(m, raw, self.raw_prev)
        self.held = ~mask
        return self.x

    def predict(self, horizon_s: float) -> np.ndarray:
        """Extrapolate the filtered points `horizon_s` seconds past the last sample."""
        assert self.x is not None
        return self.x + self.dx * horizon_s
