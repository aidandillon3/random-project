"""MediaPipe Pose Landmarker wrapper.

Runs in VIDEO mode, which tracks the body from the previous frame instead of
searching the whole image every time - much faster after the first detection.
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_URL = (
    "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
    "pose_landmarker_{name}/float16/latest/pose_landmarker_{name}.task"
)


def model_path(name: str) -> Path:
    if name not in ("lite", "full", "heavy"):
        raise ValueError(f"model must be lite, full or heavy, not {name!r}")
    path = MODELS_DIR / f"pose_landmarker_{name}.task"
    if not path.exists():
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        url = MODEL_URL.format(name=name)
        print(f"Downloading pose model ({name}) from {url} ...")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(path)
    return path


@dataclass
class PoseResult:
    world: np.ndarray  # (33, 3) meters, MediaPipe axes, origin between hips
    image: np.ndarray  # (33, 2) normalised image coords, for drawing
    visibility: np.ndarray  # (33,) 0..1


class PoseEstimator:
    def __init__(self, model: str = "full", min_detection_confidence: float = 0.5,
                 min_tracking_confidence: float = 0.5):
        from mediapipe.tasks.python import BaseOptions
        from mediapipe.tasks.python import vision

        options = vision.PoseLandmarkerOptions(
            base_options=BaseOptions(model_asset_path=str(model_path(model))),
            running_mode=vision.RunningMode.VIDEO,
            num_poses=1,
            min_pose_detection_confidence=min_detection_confidence,
            min_pose_presence_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
            output_segmentation_masks=False,
        )
        self._landmarker = vision.PoseLandmarker.create_from_options(options)
        self._last_ts = -1

    def process(self, frame_bgr: np.ndarray, stamp_s: float) -> PoseResult | None:
        import mediapipe as mp
        import cv2

        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        ts = int(stamp_s * 1000)
        if ts <= self._last_ts:  # timestamps must strictly increase
            ts = self._last_ts + 1
        self._last_ts = ts
        res = self._landmarker.detect_for_video(image, ts)
        if not res.pose_world_landmarks:
            return None
        wl = res.pose_world_landmarks[0]
        il = res.pose_landmarks[0]
        world = np.array([[lm.x, lm.y, lm.z] for lm in wl], dtype=float)
        img = np.array([[lm.x, lm.y] for lm in il], dtype=float)
        vis = np.array([(lm.visibility if lm.visibility is not None else 1.0) for lm in il], dtype=float)
        return PoseResult(world=world, image=img, visibility=vis)

    def close(self) -> None:
        self._landmarker.close()
