# Body Tracker: webcam hip, knee and foot trackers

Point one webcam at yourself, calibrate once, and you get five full-body trackers in
SteamVR: **hip, left/right knee and left/right foot**. It's made to sit on top of a
Quest 3 with Virtual Desktop, which already covers your head, chest and arms.

## Why it should feel faster than other webcam trackers

Most webcam trackers lag for three reasons. This one handles each of them:

1. **No stale frames.** Webcams and OpenCV queue frames up, and queued frames are old.
   A background thread reads the camera nonstop and keeps only the newest frame.
   It also asks for MJPG at 60 fps, because many webcams only reach 30 fps without it.
2. **Your headset moves the body, the camera only bends it.** The legs are placed
   relative to your real headset position, which SteamVR knows with almost no delay.
   When you walk or crouch, the whole body moves at headset speed. The camera only
   has to report where your legs are *relative to your head*, and that changes much
   more slowly.
3. **Smart smoothing plus prediction.** A One Euro filter smooths hard when you stand
   still (no jitter) and barely at all when you move fast (no lag). It also tracks how
   fast each joint is moving. Each pose is pushed forward in time to the moment it is
   sent, at 120 Hz, so you get smooth motion between camera frames and the processing
   delay is hidden.

Pose detection uses Google's MediaPipe Pose (the `full` model by default). On a test
CPU it took about 18 ms per frame (`lite`: about 14 ms).

## What you need

- A Windows PC that runs SteamVR, plus Virtual Desktop streaming your Quest 3 to SteamVR.
- A webcam. 60 fps is best (a PS3 Eye, a Logitech C922 or a phone used as a webcam all
  work). A 30 fps camera works too, it just adds a little delay.
- **Python 3.12** from [python.org](https://www.python.org/downloads/). Tick
  *"Add python.exe to PATH"* during install.
- **Virtual Motion Tracker (VMT)**, a free SteamVR driver that turns this program's
  output into real SteamVR trackers: [download VMT](https://gpsnmeajp.github.io/VirtualMotionTrackerDocument/download/).

## One-time setup

1. **Install VMT.**
   1. Start SteamVR.
   2. Run VMT Manager and allow it through the firewall.
   3. Click **Install**, then **OK**, then **Restart SteamVR**. Allow SteamVR through
      the firewall too.
   4. Open VMT Manager again, go to the **Room Setup** tab, click **Set Room Matrix**,
      then **OK**. Wait until it shows **"0: OK"**.
2. **Install the tracker.** Double-click `setup.bat` in this folder and wait for
   "Setup done". The first run downloads the pose model (about 10 MB).
3. **Place the camera.**
   - Put it about 2–3 m away, roughly at hip-to-chest height.
   - It must see your **whole body, feet included**, in the space where you play.
   - Light your body well, and avoid having a bright window behind you. A plain
     background helps.
   - Fix the camera firmly in place. If it moves later, just calibrate again.

## Every time you play

1. Start Virtual Desktop and SteamVR, then double-click `run.bat`.
   A window opens with the camera view and a stick figure drawn on you.
   Green lines mean a joint is seen clearly; red lines mean it is hidden.
2. **Calibrate.** You only need to do this the first time and after moving the camera.
   1. Press **C** in the window. You then have 5 seconds to get into position.
   2. Put the headset on and stand where you'll play.
   3. Face the camera and stand up straight with your feet a little apart.
   4. Look straight ahead, not down at your feet.
   5. Hold still until the window says "Calibrated".
   6. Press **S** to save. Next time it starts already calibrated.
3. **First time only: tell SteamVR what each tracker is.** Go to SteamVR **Settings →
   Controllers → Manage Trackers** and set these roles:

   | Tracker | Role |
   |---|---|
   | VMT_0 | Waist |
   | VMT_1 | Left Foot |
   | VMT_2 | Right Foot |
   | VMT_3 | Left Knee |
   | VMT_4 | Right Knee |

4. Calibrate full-body tracking in your game (in VRChat, the usual T-pose calibration).

Keys in the window: **C** calibrate, **S** save, **P** pause or resume the trackers,
**R** reset the position sliders, **Q** quit.

## Adjusting it while you play

Drag the sliders in the window. Changes apply instantly, and **S** saves them.

| Slider | What it does |
|---|---|
| Smoothing | Higher means steadier when you stand still. Too high and small movements feel mushy. |
| Responsiveness | Higher means less lag on fast kicks and steps, but a bit more shake while moving. |
| Prediction ms | How far ahead to guess. Raise it if feet feel behind you; lower it if they overshoot when you stop. Try 10–40. |
| Size % | Scales your legs around your head. Use it if feet float or sink (100 = as calibrated). |
| Turn deg | Spins the legs around you if they point a bit off from where you face (180 = no change). |
| Left/Right, Up/Down, Fwd/Back cm | Shifts all trackers relative to the way your hips face (50 = no change). |
| Hip height cm | Moves only the hip tracker up or down (30 = no change). |

A good way to tune: stand still and raise Smoothing until the jitter stops. Then kick
quickly and raise Responsiveness until the kick no longer lags.

## Settings file

After you press **S**, everything is saved in `config.json` next to this README. You
can edit it with Notepad while the tracker is closed. Useful entries:

- `camera_index`: which camera to use (0, 1, 2…). Run `run.bat --list-cameras` to see them.
- `model`: `lite` (fastest), `full` (default) or `heavy` (most accurate, slow).
- `user_height_m`: your height. It's only used when the headset isn't connected during calibration.
- `enabled_trackers`: drop `"left_knee"` and `"right_knee"` if you only want hip and feet.
- `output`: `steamvr` (default), `vrchat` (VRChat OSC, see below), `both` or `none`.
- `camera_fps`, `camera_width`, `camera_height`: 640×480 at 60 is the sweet spot. More
  pixels don't help, because the model shrinks the image to 256 px anyway.

## Using VRChat OSC instead of SteamVR trackers

If you run VRChat through Virtual Desktop's VDXR mode (without SteamVR), VMT can't
reach VRChat. Use VRChat's built-in OSC trackers instead:

1. Set `"output": "vrchat"` in `config.json`.
2. In VRChat, open the Action Menu → Options → OSC → Enabled.
3. Calibrate full-body tracking in game as usual.

Without SteamVR the program can't read your headset. Instead it sends a head estimate
from the camera, and VRChat uses it to line the trackers up with your real head. Expect
the SteamVR route to feel a bit tighter.

## Troubleshooting

- **"Could not open camera"**: another app (Discord, OBS, a browser) may be using the
  camera, or the index is wrong. Try `run.bat --list-cameras`.
- **Low camera fps in the window**: try `"camera_backend": "msmf"` in `config.json`,
  turn off the camera's auto exposure, or add light. Dark rooms make webcams slow their
  shutter, which drops the frame rate.
- **"headset: waiting for SteamVR"**: start SteamVR. The tracker connects by itself
  once SteamVR is running.
- **Trackers don't appear in SteamVR**: make sure VMT's Room Setup shows "0: OK", and
  that the window says CALIBRATED.
- **Legs face the wrong way, or left and right are swapped**: calibrate again while
  facing the camera and looking straight ahead.
- **Feet jitter when hidden** (for example behind furniture): hidden joints are frozen
  until they're seen again. Moving the camera so it sees your feet clearly fixes it.

## For tinkerers

- Code lives in `bodytracker/`. `engine.py` is the core pipeline, `calibration.py`
  holds the calibration math and `filters.py` the One Euro filter.
- Run the tests (no camera needed): `.venv\Scripts\python -m pip install pytest`, then
  `.venv\Scripts\python -m pytest`.
- Try it on a video file instead of a camera:
  `run.bat --video clip.mp4 --output none --calibrate`.
