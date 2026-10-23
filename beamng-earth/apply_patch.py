"""Applies this repo's high-speed changes to a checkout of TecnaGamer/BeamNG.Earth (MIT) at v1.5.5-beta
(written for v1.5.4-beta; applies unchanged).

Usage: python apply_patch.py <BeamNG.Earth checkout>

1. World server (server/earthd.py): above FAR_LEAD_MS the far-field detail (which tiles are fine, and the order
   they are built in) is centred FAR_LEAD_S of driving ahead of the car instead of on it, so the ground ahead is
   already sharp when the car gets there (Connection.view_focus; a free camera away from the car still wins).
   The near block, roads, trees and collision still follow the car.
2. Mod (follow.lua): the 512 block's fast mode (prepare the next block 0.75 s earlier, reach 420 m) starts at
   80 m/s (179 mph) instead of 150 m/s. In an offline model of follow.decide this keeps 60-70 m more sharp ground
   ahead of the car between 80 and 150 m/s, with about one extra block move every 2 km at 200 mph.

Every edit must match its anchor exactly once, or the script stops without writing anything.
"""
import os
import sys

OLD_VIEW_FOCUS = '''    @property
    def view_focus(self):
        """Where the far field is centred: the camera when it is away from the car (camera_away), else the car."""
        return self.view_at or self.focus
'''

NEW_VIEW_FOCUS = '''    # High speed (this repo's patch): the far field's detail centred ahead of a fast car.
    FAR_LEAD_MS = 35.0      # m/s (78 mph) and faster
    FAR_LEAD_S = 2.0        # s of driving ahead
    FAR_LEAD_MAX_M = 250.0

    @property
    def view_focus(self):
        """Where the far field is centred: the camera when it is away from the car (camera_away), else the car, or
        FAR_LEAD_S ahead of it at FAR_LEAD_MS or more."""
        if self.view_at:
            return self.view_at
        vx, vy = self.vel
        s = math.hypot(vx, vy)
        if not (s >= self.FAR_LEAD_MS and math.isfinite(s)):
            return self.focus
        d = min(self.FAR_LEAD_MAX_M, s * self.FAR_LEAD_S)
        return (self.focus[0] + vx / s * d, self.focus[1] + vy / s * d)
'''

EDITS = {
    os.path.join("server", "earthd.py"): [
        (OLD_VIEW_FOCUS, NEW_VIEW_FOCUS),
    ],
    os.path.join("mod", "beamng_earth", "lua", "ge", "extensions", "beamngEarth", "follow.lua"): [
        ("M.FAST_V_512 = 150\n", "M.FAST_V_512 = 80                            -- (was 150; this repo's high-speed patch)\n"),
    ],
}


def main(root):
    out = {}
    for rel, edits in EDITS.items():
        path = os.path.join(root, rel)
        with open(path, encoding="utf-8", newline="") as f:
            text = f.read()
        crlf = "\r\n" in text
        text = text.replace("\r\n", "\n")
        for old, new in edits:
            n = text.count(old)
            if n != 1:
                sys.exit(f"{rel}: expected 1 match for {old.strip()[:60]!r}, found {n}")
            text = text.replace(old, new)
        out[path] = text.replace("\n", "\r\n") if crlf else text
    for path, text in out.items():
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        print("patched", path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
