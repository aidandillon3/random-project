"""Words shown in the app: mode descriptions, setup steps and help."""

MODES = {
    "mono": {
        "title": "One camera",
        "tag": "Easiest",
        "best_for": "Standing, walking and hanging out. Getting started.",
        "pros": ["Any webcam works", "Set up in 5 minutes", "Lowest CPU use"],
        "cons": ["Can lose a leg when one crosses in front of the other",
                 "Guesses depth from one view, so forward/back is less exact"],
        "needs": "1 webcam (60 fps ideal)",
    },
    "stereo": {
        "title": "Two cameras",
        "tag": "Most accurate",
        "best_for": "Dancing, crossed legs, sitting, fast footwork.",
        "pros": ["Real 3D from two angles", "A leg hidden from one camera is filled in from the other",
                 "Much better forward/back accuracy"],
        "cons": ["Needs two webcams and a one-time camera pairing walk",
                 "Uses about twice the CPU"],
        "needs": "2 webcams, placed at roughly a right angle to each other",
    },
    "kinect": {
        "title": "Kinect v2",
        "tag": "Depth sensor",
        "best_for": "If you already own an Xbox One Kinect with its PC adapter.",
        "pros": ["Measures depth directly", "Handles crossed legs better than one webcam",
                 "Barely uses the CPU"],
        "cons": ["30 fps only, slightly more delay", "Needs the Kinect SDK and adapter",
                 "Struggles when you face away or lie down"],
        "needs": "Kinect v2 + Kinect for Windows adapter + Kinect for Windows SDK 2.0",
    },
}


def recommend(cameras: str, kinect: bool, activity: str) -> tuple[str, str]:
    """(mode, reason) for the "help me choose" questions."""
    two = cameras == "2"
    if activity == "active":
        if two:
            return "stereo", "Two cameras see around crossed legs and fast moves, which one camera can't."
        if kinect:
            return "kinect", "The Kinect's depth sensor copes with crossed legs better than one webcam."
        return "mono", "One camera works, but crossed legs will drop out sometimes. A second webcam fixes that."
    if activity == "sitting":
        if two:
            return "stereo", "Sitting hides your legs from a single view; two angles keep them tracked."
        return "mono", "Put the camera off to one side, about 45 degrees, so it sees your legs while you sit."
    if two:
        return "stereo", "You have two cameras, so you may as well get real 3D. One camera is fine too."
    return "mono", "For standing and walking, one camera is accurate enough and the easiest to set up."


# Each step: (key, title, html body). Keys with a live check are ticked automatically.
COMMON_START = [
    ("python", "Install the basics", """
<p>You've done this if you're reading it. If you ever reinstall: install
<b>Python 3.12</b> from python.org (tick <i>Add python.exe to PATH</i>), then double-click
<b>setup.bat</b>.</p>"""),
    ("vmt", "Install Virtual Motion Tracker (VMT)", """
<p>VMT is a free SteamVR driver that turns this app's output into real SteamVR trackers.</p>
<ol>
<li>Download it from
<a href="https://gpsnmeajp.github.io/VirtualMotionTrackerDocument/download/">the VMT site</a>
and unzip it.</li>
<li>Start SteamVR, then run <b>vmt_manager.exe</b>. Allow it through the firewall.</li>
<li>Click <b>Install</b>, then <b>OK</b>, then <b>Restart SteamVR</b>. Allow SteamVR through the firewall too.</li>
<li>Open VMT Manager again, go to the <b>Room Setup</b> tab, click <b>Set Room Matrix</b>, then <b>OK</b>.
It should show <b>0: OK</b>.</li>
</ol>
<p>Using VRChat without SteamVR (Virtual Desktop's VDXR mode)? Skip this and pick
<b>VRChat OSC</b> on the Output page instead.</p>"""),
]

MODE_STEPS = {
    "mono": [
        ("camera", "Place the camera", """
<ul>
<li>2 to 3 m away, at about hip-to-chest height.</li>
<li>It must see your <b>whole body, feet included</b>, everywhere you'll stand.</li>
<li>Light the room well. A bright window behind you makes things worse.</li>
<li>Fix it firmly in place. If it moves, just calibrate again.</li>
</ul>
<p>Pick your camera on the <b>Cameras</b> page. This step ticks itself once the camera is running.</p>"""),
        ("body", "Check that it sees you", """
<p>Stand in your play space. On the <b>Cameras</b> page you should see a stick figure on you.
Green lines are joints it sees clearly; red ones are guesses.</p>"""),
    ],
    "stereo": [
        ("camera", "Place both cameras", """
<ul>
<li>Put them at <b>roughly a right angle</b> to each other as seen from where you stand
(for example one in front of you, one off to your left), both 2 to 3 m away.</li>
<li><b>Both</b> must see your whole body, feet included.</li>
<li>Somewhere between 60 and 120 degrees apart works. Too close together and they can't
see around your legs; directly opposite and they can't be paired.</li>
<li>Plug them into different USB ports (ideally one on the back of the PC each) so they
can both run at full speed.</li>
<li>Fix them firmly. If either moves, pair them again.</li>
</ul>
<p>Pick camera A and camera B on the <b>Cameras</b> page. If you know a camera's field of
view, enter it there (70 degrees suits most webcams).</p>"""),
        ("body", "Check both see you", """
<p>On the <b>Cameras</b> page both previews should show a stick figure on you.</p>"""),
        ("pair", "Pair the cameras (one time)", """
<p>The app works out where the cameras are by watching you move. No printouts needed.</p>
<ol>
<li>Click <b>Pair cameras</b> on the Cameras page.</li>
<li>Walk slowly around your whole play space for 10 to 20 seconds. Turn around, lift your knees,
step side to side. Stay visible to both cameras.</li>
<li>It finishes by itself and tells you how well it went. "Great" or "good" is what you want.</li>
</ol>"""),
    ],
    "kinect": [
        ("sdk", "Install the Kinect software", """
<ol>
<li>Install the <b>Kinect for Windows SDK 2.0</b> from Microsoft.</li>
<li>Plug the Kinect into its <b>Kinect for Windows adapter</b> and a <b>USB 3</b> port.</li>
<li>Open <b>Kinect Configuration Verifier</b> (installed with the SDK) and check everything is green.</li>
</ol>
<p>This mode is for the <b>Kinect v2</b> (Xbox One Kinect). The original Xbox 360 Kinect isn't supported.</p>"""),
        ("camera", "Place the Kinect", """
<ul>
<li>About 2 to 3.5 m away, at roughly waist to chest height, facing you.</li>
<li>It must see your whole body, feet included.</li>
<li>Keep it away from direct sunlight; it disturbs the depth sensor.</li>
</ul>
<p>This step ticks itself once the Kinect is running.</p>"""),
        ("body", "Check that it sees you", """
<p>Stand in front of it. The body view on the <b>Calibrate</b> page should move with you.
If your left and right are swapped, tick <b>Swap left and right</b> on the Cameras page.</p>"""),
    ],
}

COMMON_END = [
    ("steamvr", "Start SteamVR", """
<p>Start Virtual Desktop and SteamVR. The app finds your headset by itself; this ticks when it does.
Without SteamVR the trackers still work in VRChat OSC mode, placed from the camera alone.</p>"""),
    ("calibrated", "Calibrate your body", """
<ol>
<li>Go to the <b>Calibrate</b> page and enter your height.</li>
<li>Click <b>Calibrate</b>. You have a few seconds to get ready.</li>
<li>Headset on, stand in your play space, face the camera (camera A in two-camera mode),
stand up straight and <b>look straight ahead</b>, not down.</li>
<li>Hold still until it says <b>Calibrated</b>. It's saved automatically.</li>
</ol>"""),
    ("roles", "Tell SteamVR which tracker is which (first time)", """
<p>In SteamVR: <b>Settings → Controllers → Manage Trackers</b>, then set:</p>
<table cellpadding="3">
<tr><td>VMT_0</td><td>Waist</td></tr>
<tr><td>VMT_1</td><td>Left Foot</td></tr>
<tr><td>VMT_2</td><td>Right Foot</td></tr>
<tr><td>VMT_3</td><td>Left Knee</td></tr>
<tr><td>VMT_4</td><td>Right Knee</td></tr>
</table>
<p>In VRChat OSC mode skip this; VRChat sorts them out when you calibrate in game.</p>"""),
    ("sending", "Calibrate in your game", """
<p>This ticks when trackers are being sent. Then calibrate full-body tracking in your game
(in VRChat, the usual T-pose). If something feels off, use the <b>Adjust</b> page.</p>"""),
]


def steps_for(mode: str):
    return COMMON_START + MODE_STEPS.get(mode, MODE_STEPS["mono"]) + COMMON_END


HELP_HTML = """
<h2>Troubleshooting</h2>
<h3>A leg drops out when I cross my legs</h3>
<p>One camera can't see a leg that's hidden behind the other. <b>Two-camera mode</b> fixes this:
the second camera sees the hidden leg. In one-camera mode, putting the camera higher and a bit
off to the side helps, and the app already holds a hidden leg in place and undoes left/right
mix-ups.</p>
<h3>Feet float or sink into the floor</h3>
<p>Calibrate again with your headset on and SteamVR running. Then use <b>Size %</b> or
<b>Up/Down</b> on the Adjust page.</p>
<h3>Legs lag behind</h3>
<p>Raise <b>Responsiveness</b>, then <b>Prediction</b>. Check the camera runs near 60 fps in the
status bar; a dark room makes webcams drop to 30 fps or less, so add light or turn off auto exposure.</p>
<h3>Legs jitter when I stand still</h3>
<p>Raise <b>Smoothing</b>. Better light also reduces jitter a lot.</p>
<h3>Trackers don't show up in SteamVR</h3>
<ul>
<li>Check VMT Manager's Room Setup shows <b>0: OK</b>.</li>
<li>The app must say <b>Calibrated</b>, and the Output page must say it is sending.</li>
</ul>
<h3>"Could not get a picture from camera"</h3>
<p>Another program (Discord, OBS, a browser tab) may be using it. Close it, or pick another camera.</p>
<h3>Phone as a camera (Iriun, DroidCam)</h3>
<ul>
<li>Open the app on the phone <b>and</b> on the PC before starting tracking, with both on the same
Wi-Fi (or plugged in by USB). Keep both apps up to date so their versions match.</li>
<li>If the preview shows the app's own "waiting" or logo picture, the phone isn't connected yet.</li>
<li>In the Iriun PC app, pick 640x480 or 1280x720. Bigger pictures only add delay.</li>
<li>Phone cameras are virtual cameras that can't use the fast webcam settings. The app notices and
switches to the camera's own mode by itself; the message at the bottom of the window then says "fell back".</li>
<li>Still black? Reinstall the Iriun PC app (that reinstalls its camera driver) and restart the PC.</li>
</ul>
<h3>Two cameras: pairing fails or is "rough"</h3>
<ul>
<li>Both cameras must see your whole body during the walk.</li>
<li>Move the cameras further apart (about a right angle as seen from you).</li>
<li>Walk around more of the space, turn around, and lift your knees.</li>
<li>Set the field of view if your cameras are unusually wide or narrow.</li>
</ul>
<h3>Kinect: "Kinect not found"</h3>
<p>Run <b>Kinect Configuration Verifier</b>. The Kinect needs its adapter, a USB 3 port, and the
Kinect for Windows SDK 2.0.</p>
<h2>How it keeps latency low</h2>
<ul>
<li>It always uses the newest camera frame and never a queued one.</li>
<li>Your legs are placed under your real headset position, so walking and crouching move at
headset speed.</li>
<li>Smoothing is heavy when you're still and light when you move, and every pose is predicted
forward to the moment it's sent, 120 times a second.</li>
</ul>
"""
