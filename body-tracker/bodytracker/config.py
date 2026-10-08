"""Settings, saved to config.json so tweaks and calibration survive restarts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .calibration import Calibration

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "config.json"


@dataclass
class Config:
    # --- camera ---
    camera_index: int = 0
    camera_width: int = 640  # the pose model works at 256px, more pixels only add latency
    camera_height: int = 480
    camera_fps: int = 60
    camera_backend: str = "auto"  # auto | dshow | msmf | v4l2 | any

    # --- pose model ---
    model: str = "full"  # lite (fastest) | full (balanced) | heavy (most accurate, slow)
    min_detection_confidence: float = 0.5
    min_tracking_confidence: float = 0.5
    min_visibility: float = 0.5  # below this a leg point is treated as hidden and held

    # --- you ---
    user_height_m: float = 1.75

    # --- smoothing / latency (adjustable live in the window) ---
    smoothing: float = 1.5  # One Euro min_cutoff in Hz: lower = smoother, higher = snappier
    responsiveness: float = 0.8  # One Euro beta: higher = less lag when moving fast
    prediction_ms: float = 20.0  # extra look-ahead on top of measured processing delay
    max_prediction_ms: float = 100.0

    # --- placement (adjustable live in the window) ---
    offset_x_m: float = 0.0  # move all trackers left/right
    offset_y_m: float = 0.0  # move all trackers up/down
    offset_z_m: float = 0.0  # move all trackers forward/back
    yaw_offset_deg: float = 0.0  # spin the whole body around your head
    scale_adjust: float = 1.0  # multiply the calibrated size
    hip_offset_y_m: float = 0.0  # hip tracker only, up/down (e.g. belt height)

    # --- output ---
    output: str = "steamvr"  # steamvr (Virtual Motion Tracker driver) | vrchat (OSC) | both | none
    output_rate_hz: float = 120.0  # send rate; poses between camera frames are extrapolated
    use_headset: bool = True  # read headset pose from SteamVR to place the body
    vmt_host: str = "127.0.0.1"
    vmt_port: int = 39570
    vmt_first_index: int = 0  # VMT tracker slots used: first_index .. first_index+4
    vrchat_host: str = "127.0.0.1"
    vrchat_port: int = 9000
    vrchat_send_head: bool = True

    enabled_trackers: list = field(
        default_factory=lambda: ["hip", "left_foot", "right_foot", "left_knee", "right_knee"]
    )

    # --- calibration ---
    calibration_delay_s: float = 5.0  # time to get into position after pressing C

    # --- window ---
    show_preview: bool = True

    calibration: Calibration = field(default_factory=Calibration)

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> "Config":
        cfg = cls()
        if path.exists():
            data = json.loads(path.read_text())
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k == "calibration" and isinstance(v, dict):
                    cfg.calibration = Calibration(**v)
                elif k in known:
                    setattr(cfg, k, v)
        return cfg

    def save(self, path: Path = DEFAULT_PATH) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))
