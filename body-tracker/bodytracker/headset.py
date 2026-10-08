"""Read the headset pose from SteamVR (works with Virtual Desktop streaming to SteamVR).

Your legs are placed relative to your real headset, which SteamVR knows with
almost no delay. That means walking around moves your legs with headset-level
latency; the camera only has to supply where your legs are *relative to your head*.
"""

from __future__ import annotations

import time

import numpy as np

RETRY_S = 3.0


class Headset:
    """Connects to SteamVR lazily and keeps retrying, so start order doesn't matter.

    Call `pose()` from one thread only (the output thread).
    """

    def __init__(self) -> None:
        self.vr = None
        self._openvr = None
        self._poses = None
        self._next_try = 0.0
        self.error: str | None = None
        try:
            import openvr  # pip install openvr

            self._openvr = openvr
        except ImportError:
            self.error = "the 'openvr' package is not installed (pip install openvr)"

    @property
    def connected(self) -> bool:
        return self.vr is not None

    def _connect(self) -> None:
        now = time.monotonic()
        if self._openvr is None or now < self._next_try:
            return
        self._next_try = now + RETRY_S
        try:
            self.vr = self._openvr.init(self._openvr.VRApplication_Background)
            self._poses = (self._openvr.TrackedDevicePose_t * 1)()  # index 0 is always the headset
            self.error = None
        except Exception as e:  # SteamVR not running yet
            self.vr = None
            self.error = f"SteamVR not reachable ({e})"

    def pose(self):
        """(position (3,), rotation 3x3) in SteamVR standing space (floor at y=0), or None."""
        if self.vr is None:
            self._connect()
            if self.vr is None:
                return None
        ovr = self._openvr
        try:
            self.vr.getDeviceToAbsoluteTrackingPose(ovr.TrackingUniverseStanding, 0.0, self._poses)
        except Exception as e:  # SteamVR closed
            self.error = f"lost SteamVR ({e})"
            self.close()
            return None
        p = self._poses[0]
        if not p.bPoseIsValid:
            return None
        m = p.mDeviceToAbsoluteTracking
        rot = np.array([[m[r][c] for c in range(3)] for r in range(3)])
        pos = np.array([m[0][3], m[1][3], m[2][3]])
        return pos, rot

    def close(self) -> None:
        if self.vr is not None:
            try:
                self._openvr.shutdown()
            except Exception:
                pass
            self.vr = None
