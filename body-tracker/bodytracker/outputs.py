"""Where tracker poses go.

SteamVR: through the free "Virtual Motion Tracker" (VMT) driver, which turns OSC
messages into real SteamVR trackers. Works in any SteamVR game, VRChat included,
and they show up in SteamVR's "Manage Trackers" like hardware trackers.

VRChat OSC: VRChat's built-in OSC trackers. No driver needed, also works when
VRChat runs without SteamVR (e.g. Virtual Desktop's VDXR runtime).
"""

from __future__ import annotations

from .mathutil import openvr_to_unity_pos, openvr_to_unity_quat, unity_quat_to_euler_zxy
from .osc import OscSender
from .skeleton import TrackerPose

VMT_TRACKER = 1  # VMT "enable" value for a generic tracker
VMT_DISABLE = 0

# VRChat OSC slots 1..8. Order here is just a stable mapping; VRChat assigns
# body parts by where the trackers are when you calibrate in game.
VRCHAT_SLOTS = {"hip": 1, "left_foot": 2, "right_foot": 3, "left_knee": 4, "right_knee": 5}
TRACKER_ORDER = ["hip", "left_foot", "right_foot", "left_knee", "right_knee"]


class VmtOutput:
    """Sends poses in SteamVR's standing space via /VMT/Room/Driver."""

    def __init__(self, host: str, port: int, first_index: int = 0):
        self.osc = OscSender(host, port)
        self.first_index = first_index

    def _index(self, name: str) -> int:
        return self.first_index + TRACKER_ORDER.index(name)

    def send(self, trackers: list[TrackerPose], head_pos=None, head_rot=None) -> None:
        for t in trackers:
            p, q = t.position, t.rotation
            self.osc.send(
                "/VMT/Room/Driver",
                self._index(t.name), VMT_TRACKER, 0.0,
                float(p[0]), float(p[1]), float(p[2]),
                float(q[0]), float(q[1]), float(q[2]), float(q[3]),
            )

    def disable(self) -> None:
        for name in TRACKER_ORDER:
            self.osc.send(
                "/VMT/Room/Driver", self._index(name), VMT_DISABLE, 0.0,
                0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0,
            )

    def close(self) -> None:
        self.osc.close()


class VrchatOutput:
    """VRChat OSC trackers. Unity space: left-handed, +Z forward, euler degrees (ZXY)."""

    def __init__(self, host: str, port: int, send_head: bool = True):
        self.osc = OscSender(host, port)
        self.send_head = send_head

    def send(self, trackers: list[TrackerPose], head_pos=None, head_rot=None) -> None:
        for t in trackers:
            slot = VRCHAT_SLOTS[t.name]
            p = openvr_to_unity_pos(t.position)
            e = unity_quat_to_euler_zxy(openvr_to_unity_quat(t.rotation))
            base = f"/tracking/trackers/{slot}"
            self.osc.send(base + "/position", float(p[0]), float(p[1]), float(p[2]))
            self.osc.send(base + "/rotation", float(e[0]), float(e[1]), float(e[2]))
        # VRChat moves our whole tracking space every frame so this "head" lines up
        # with your real head, and slowly turns it so its yaw lines up too.
        if self.send_head and head_pos is not None:
            p = openvr_to_unity_pos(head_pos)
            self.osc.send("/tracking/trackers/head/position", float(p[0]), float(p[1]), float(p[2]))
            if head_rot is not None:
                e = unity_quat_to_euler_zxy(openvr_to_unity_quat(head_rot))
                # yaw only: VRChat ignores pitch/roll here, sending them just adds noise
                self.osc.send("/tracking/trackers/head/rotation", 0.0, float(e[1]), 0.0)

    def disable(self) -> None:
        pass

    def close(self) -> None:
        self.osc.close()


def make_outputs(cfg) -> list:
    outs = []
    if cfg.output in ("steamvr", "both"):
        outs.append(VmtOutput(cfg.vmt_host, cfg.vmt_port, cfg.vmt_first_index))
    if cfg.output in ("vrchat", "both"):
        outs.append(VrchatOutput(cfg.vrchat_host, cfg.vrchat_port, cfg.vrchat_send_head))
    return outs

