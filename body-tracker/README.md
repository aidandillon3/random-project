# Body Tracker: hip, knee and foot trackers from a camera

Point a camera (or two, or a Kinect) at yourself, calibrate once, and you get five
full-body trackers in SteamVR: **hip, left/right knee and left/right foot**. It's made to sit
on top of a Quest 3 with Virtual Desktop, which already covers your head, chest and arms.

Everything happens in the app window: choosing a mode, step-by-step setup guides with live
checks, camera previews, calibration and adjustment sliders.

## Install (once)

1. Install **Python 3.12** from [python.org](https://www.python.org/downloads/). Tick
   *"Add python.exe to PATH"* during install.
2. Double-click **`setup.bat`** in this folder and wait for "Setup done".
3. Double-click **`run.bat`**. The app opens on the **Start** page.

The app's **Setup guide** page then walks you through the rest for the mode you pick:
installing the free Virtual Motion Tracker driver, placing cameras, calibrating and
assigning tracker roles in SteamVR. Steps tick themselves off as the app detects them.

## The three modes

| | One camera | Two cameras | Kinect v2 |
|---|---|---|---|
| Best for | Standing, walking, getting started | Dancing, crossed legs, sitting | If you own an Xbox One Kinect + PC adapter |
| Crossed legs | Can drop the hidden leg | **Hidden leg filled in from the other camera** | Better than one webcam |
| Depth (forward/back) | Guessed | **Measured** | **Measured** |
| Setup | 5 minutes | Plus a 20-second walk to pair the cameras | Kinect SDK 2.0 + adapter |
| Frame rate | Up to 60 fps | Up to 60 fps | 30 fps |

Not sure? The Start page has a three-question "help me choose".

### Why two cameras fix crossed legs

One camera can't see a leg hidden behind the other, so the pose model has to guess, and it
often loses the leg or swaps left and right. With two cameras about a right angle apart,
the hidden leg is almost always visible to the other camera. The app triangulates every
joint both cameras see into true 3D, and fills in a joint hidden from one camera using the
camera that still sees it. Camera positions are worked out by watching you walk around for
a few seconds, so no printed checkerboard is needed.

### Fixes that help every mode

- **Left/right mix-up guard.** If the pose model suddenly swaps your legs' labels, the app
  notices that the swapped version matches your previous pose and swaps them back.
- **Real leg length.** Thigh and shin lengths are measured at calibration. Each frame, the
  knee is placed so both bones keep that length, which stops legs stretching when depth is
  misjudged.
- **Hidden joints are held** in place instead of jumping around.

## Why it's low latency

1. **No stale frames.** Each camera is read on its own thread that keeps only the newest
   frame, and it asks for MJPG at 60 fps.
2. **Your headset moves the body.** The legs are placed under your real headset position
   from SteamVR, so walking and crouching move at headset speed. The camera only has to say
   how your legs are bent.
3. **Smoothing plus prediction.** A One Euro filter smooths hard when you're still and
   barely at all when you move. Each pose is pushed forward to the moment it is sent, at
   120 Hz.

## Settings

Everything you change in the app is saved automatically to `config.json` in this folder.
Delete that file to start fresh.

## Kinect notes

- Only the **Kinect v2** (Xbox One Kinect / Kinect for Windows v2) is supported. It needs
  the Kinect for Windows adapter, a USB 3 port and the
  [Kinect for Windows SDK 2.0](https://www.microsoft.com/en-us/download/details.aspx?id=44561).
- The app talks to the SDK through the `pykinect2` package. That package is old, so the
  app applies its 64-bit Python 3 fixes in memory when loading it.

## For tinkerers

- `bodytracker/engine.py` is the core pipeline, `stereo.py` the two-camera maths,
  `kinect.py` the Kinect reader, `sources.py` the three modes and `gui.py` the window.
- Run the tests, no camera needed: `.venv\Scripts\python -m pip install pytest`, then
  `.venv\Scripts\python -m pytest`.
- Console mode without a window: `run.bat --no-gui`. Try a video instead of a camera with
  `run.bat --no-gui --video clip.mp4 --output none --calibrate`.
