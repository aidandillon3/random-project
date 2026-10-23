"""The outside of a generated body: side profile, plan view and cross-sections.

Shape(P) answers, for any position y along the car, how high the top and the
floor are, how wide the body is and whether the station is closed (hood, trunk),
has a cabin (windows and roof) or is open (pickup bed, roadster cockpit).
section(y) returns the half cross-section (left side, x >= 0) as K points from
the bottom centre around the side to the top centre. The mesh and the node
structure are both built from these sections, so the nodes sit on the skin.
"""
import math

K = 18
S_FLOOR_MID, S_FLOOR, S_WELL = 1, 2, 3
S_SILL, S_SIDE = 4, 6
S_SHOULDER, S_BELT = 9, 10
S_EDGE, S_TOP = 13, 17


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def lerp(a, b, t):
    return a + (b - a) * t


def smoothstep(t):
    t = clamp(t, 0.0, 1.0)
    return t * t * (3 - 2 * t)


class Shape:
    STEP = 0.01   # profile sampling (m)

    def __init__(self, P, travel_bump=None):
        self.P = P
        self.notes = []
        L, WB = P.length, P.wheelbase
        self.yF, self.yR = -WB / 2, WB / 2
        self.y0 = self.yF - P.frontOverhang
        self.y1 = self.y0 + L
        self.W = P.width
        self.H = P.height
        self.round = P.roundness
        self.axles = [
            {"id": "F", "y": self.yF, "r": P.rF, "tw": P.twF, "track": P.trackF,
             "arch": P.rF + 0.035 + 0.3 * P.travelF, "steer": True},
            {"id": "R", "y": self.yR, "r": P.rR, "tw": P.twR, "track": P.trackR,
             "arch": P.rR + 0.035 + 0.3 * P.travelR, "steer": False},
        ]
        self._keypoints()
        self._sample()

    # -- side profile ----------------------------------------------------------------
    def _keypoints(self):
        P = self.P
        H, y0, y1 = self.H, self.y0, self.y1
        self.z_cowl = min(P.beltline + 0.03, H - 0.12)
        self.y_cowl = y0 + P.hoodLength
        rake = math.radians(P.windshieldAngle)
        rise = max(H - self.z_cowl, 0.05)
        if P.openTop:
            # a roadster's windshield is a frame on the cowl; the cockpit starts right behind it
            self.y_wst = self.y_cowl + 0.32 * math.cos(rake)
            self.ws_top = self.z_cowl + 0.32 * math.sin(rake)
        else:
            self.y_wst = self.y_cowl + rise / math.tan(rake)
            self.ws_top = H
        if self.y_wst > y1 - 0.45:
            self.y_wst = y1 - 0.45
            self.notes.append("The windshield was made steeper so the cabin fits.")
        self.y_rt = min(self.y_wst + P.roofLength, y1 - 0.08)
        rear = math.radians(P.rearAngle)
        if P.bed:
            self.z_rail = clamp(P.tailHeight, P.clearance + 0.45, H - 0.25)
            self.y_cb = min(self.y_rt + (H - self.z_rail) / math.tan(rear), y1 - 0.6)
            self.y_open0, self.y_open1 = self.y_cb + 0.05, y1 - 0.045
            self.z_deck = self.z_rail
            self.y_rb = self.y_cb
        else:
            self.z_deck = clamp(P.tailHeight, P.clearance + 0.25, H - 0.03)
            self.y_rb = self.y_rt + max(H - self.z_deck, 0.0) / math.tan(rear)
            if self.y_rb > y1 - 0.03:
                self.y_rb = y1 - 0.03
            self.y_open0 = self.y_open1 = None
        if P.openTop:
            self.y_open0, self.y_open1 = self.y_cowl + 0.03, max(self.y_rt, self.y_cowl + 0.5)
            self.y_rt = self.y_open1
            self.y_rb = self.y_open1 + 0.25
        self.z_open_floor = (max(P.clearance + 0.3, self.z_rail - 0.55) if P.bed else P.clearance + 0.24)

    def _top_raw(self, y):
        P, H = self.P, self.H
        y0, y1 = self.y0, self.y1
        if y <= self.y_cowl:
            t = (y - y0) / max(self.y_cowl - y0, 1e-6)
            z = lerp(P.noseHeight, self.z_cowl, 1 - (1 - clamp(t, 0, 1)) ** 1.7)
        elif not P.openTop and y <= self.y_wst:
            z = lerp(self.z_cowl, H, (y - self.y_cowl) / max(self.y_wst - self.y_cowl, 1e-6))
        elif P.openTop and y <= self.y_rb:
            # cockpit sides and the fairing behind it: the door tops
            z = self.z_cowl if y <= self.y_open1 else lerp(self.z_cowl, self.z_deck, (y - self.y_open1) / 0.25)
        elif not P.openTop and y <= self.y_rt:
            t = (y - self.y_wst) / max(self.y_rt - self.y_wst, 1e-6)
            z = H + 0.012 * math.sin(math.pi * t)
        elif P.bed:
            z = lerp(H, self.z_rail, smoothstep((y - self.y_rt) / max(self.y_cb - self.y_rt, 1e-6))) \
                if y <= self.y_cb else self.z_rail
        elif y <= self.y_rb:
            z = lerp(H, self.z_deck, (y - self.y_rt) / max(self.y_rb - self.y_rt, 1e-6))
        else:
            t = (y - self.y_rb) / max(y1 - self.y_rb, 1e-6)
            z = lerp(self.z_deck, P.tailHeight * 0.985, t)
        # round off the leading and trailing edges
        s = y - y0
        if s < 0.09:
            z -= (0.03 + 0.04 * self.round) * (1 - s / 0.09) ** 2
        s = y1 - y
        if s < 0.07:
            z -= (0.02 + 0.03 * self.round) * (1 - s / 0.07) ** 2
        return z

    def _sample(self):
        n = int(round((self.y1 - self.y0) / self.STEP)) + 1
        self.n = n
        ys = [self.y0 + i * self.STEP for i in range(n)]
        raw = [self._top_raw(y) for y in ys]
        # soften the creases (cowl, roof edges, deck) with a small gaussian
        sig = 0.035 + 0.035 * self.round
        rad = int(3 * sig / self.STEP)
        wts = [math.exp(-0.5 * (k * self.STEP / sig) ** 2) for k in range(-rad, rad + 1)]
        top = []
        for i in range(n):
            acc = wsum = 0.0
            for k, w in enumerate(wts):
                j = clamp(i + k - rad, 0, n - 1)
                acc += raw[j] * w
                wsum += w
            top.append(acc / wsum)
        # keep the ends where they are (smoothing pulls them toward the middle)
        top[0], top[-1] = raw[0], raw[-1]
        self._top = top

    def _interp(self, arr, y):
        f = (y - self.y0) / self.STEP
        i = int(math.floor(f))
        if i < 0:
            return arr[0]
        if i >= self.n - 1:
            return arr[-1]
        return lerp(arr[i], arr[i + 1], f - i)

    # -- what is where -----------------------------------------------------------------
    def kind(self, y):
        """ "open" (bed or cockpit), "cabin" (windows and roof) or "closed" (hood, trunk, nose)."""
        return self.kind_raw(y)

    def top(self, y):
        return self._interp(self._top, y)

    def fender(self, y):
        """Height the body side must reach over the wheels (or -1 away from them)."""
        za = self.arch(y)
        return za + 0.09 + 0.04 * self.round if za > 0 else -1.0

    def belt(self, y):
        P = self.P
        if self.P.bed and self.y_open0 is not None and y >= self.y_open0:
            z = self.z_rail
        else:
            z = P.beltline + 0.05 * (y - self.y_cowl) / max(P.length, 1.0)
        zf = self.fender(y)
        if zf > 0:
            # fenders rise over big wheels (a soft max, so the line stays smooth)
            k = 0.06
            z = (z + zf + math.sqrt((z - zf) ** 2 + k * k)) / 2
        top = self.top(y)
        kind = self.kind_raw(y)
        if kind == "cabin":
            return min(z, top - 0.08)
        if kind == "open":
            return z
        return min(z, top + 0.12)

    def kind_raw(self, y):
        if self.y_open0 is not None and self.y_open0 <= y <= self.y_open1:
            return "open"
        if self.P.openTop:
            return "closed"
        end = self.y_cb if self.P.bed else self.y_rb
        if self.y_cowl < y < end and self.top(y) - (self.P.beltline + 0.05 * (y - self.y_cowl) / max(self.P.length, 1.0)) > 0.05:
            return "cabin"
        return "closed"

    def bottom(self, y):
        P = self.P
        z = P.clearance
        rF = self.axles[0]["r"]
        rR = self.axles[1]["r"]
        lift_f = 0.06 + 0.22 * P.clearance
        lift_r = 0.07 + 0.25 * P.clearance
        a = self.yF - 0.55 * rF
        if y < a:
            z += lift_f * smoothstep((a - y) / max(a - self.y0, 1e-6))
        b = self.yR + 0.55 * rR
        if y > b:
            z += lift_r * smoothstep((y - b) / max(self.y1 - b, 1e-6))
        return min(z, self.belt(y) - 0.18, self.top(y) - 0.25)

    def half_width(self, y):
        w = self.W / 2
        r_end = (0.18 + 0.45 * self.round) * min(1.0, self.P.length / 4.5)
        c = 0.035 + 0.15 * self.round
        s = y - self.y0
        if s < r_end:
            w -= c * (1 - s / r_end) ** 2
        s = self.y1 - y
        if s < r_end:
            w -= c * 0.9 * (1 - s / r_end) ** 2
        return w

    def x_in(self):
        """Half-width of the floor between the wheel wells (the frame rails)."""
        out = []
        for a in self.axles:
            out.append(a["track"] / 2 - a["tw"] / 2 - (0.13 if a["steer"] else 0.06))
        return clamp(min(out), 0.22, self.W / 2 - 0.14)

    def arch(self, y):
        """Height of the wheel arch opening at y (or -1 away from the wheels)."""
        best = -1.0
        for a in self.axles:
            d = y - a["y"]
            if abs(d) < a["arch"]:
                best = max(best, a["r"] + math.sqrt(a["arch"] ** 2 - d * d))
        return best

    def shoulder(self, y):
        zb, zbelt = self.bottom(y), self.belt(y)
        return max(zbelt - 0.045 - 0.04 * self.round, zb + 0.12)

    def low(self, y):
        """Bottom edge of the body side: the floor, or the top of the wheel arch."""
        zb = self.bottom(y)
        za = self.arch(y)
        zl = max(zb, za)
        lim = self.shoulder(y) - 0.035
        if zl > lim:
            zl = lim
            if "The tires are taller than the fenders: raise the window line or the body." not in self.notes:
                self.notes.append("The tires are taller than the fenders: raise the window line or the body.")
        return zl

    def open_floor(self, y):
        return max(self.z_open_floor, self.bottom(y) + 0.1)

    # -- cross-sections ------------------------------------------------------------------
    def section(self, y):
        """K (x, z) points of the left half, bottom centre first, top centre last."""
        kind = self.kind(y)
        w = self.half_width(y)
        zb = self.bottom(y)
        zl = self.low(y)
        zbelt = self.belt(y)
        zt = self.top(y)
        zsh = min(self.shoulder(y), zbelt - 0.02)
        rnd = self.round
        tuck = 0.025 + 0.035 * rnd
        rb = clamp(0.045 + 0.05 * rnd, 0.01, max((zsh - zl) * 0.3, 0.01))
        xi = min(self.x_in(), w - tuck - rb - 0.02)
        pts = [(0.0, zb), (xi * 0.5, zb), (xi, zb), (xi, zl)]
        xs0 = w - tuck - rb
        pts.append((xs0, zl))
        pts.append((xs0 + rb * 0.7071, zl + rb * 0.2929))
        z_s0 = zl + rb
        pts.append((w - tuck, z_s0))
        for u in (1 / 3, 2 / 3):
            z = lerp(z_s0, zsh, u)
            pts.append((w - tuck * (1 - u) ** 2, z))
        pts.append((w, zsh))
        xbelt = w - 0.012 - 0.012 * rnd
        pts.append((xbelt, zbelt))
        if kind == "open":
            rail = 0.06
            zof = self.open_floor(y)
            hump = max(zof, zl + 0.03) if zl > zof else zof
            xw = w - rail - 0.012
            pts += [(w - rail, zbelt), (xw, lerp(zbelt, hump, 0.5)), (xw, hump),
                    (min(xi + 0.03, xw - 0.02), hump), (xi, zof), (xi * 0.5, zof), (0.0, zof)]
        else:
            g = clamp((zt - zbelt) / 0.35, 0.0, 1.0)
            if kind != "cabin":
                g = min(g, 0.25)
            wr = w * self.P.roofWidth
            xe = lerp(w * 0.93, wr, g)
            rc = lerp(0.02 + 0.02 * rnd, 0.03 + 0.07 * rnd, g)
            ze = max(zt - rc, zbelt + 0.006)
            a0 = (xbelt - 0.02 * g, zbelt + 0.008)
            for u in (1 / 3, 2 / 3):
                x = lerp(a0[0], xe, u) + 0.012 * g * math.sin(math.pi * u)
                pts.append((x, lerp(a0[1], ze, u)))
            pts.append((xe, ze))
            for deg in (22.5, 45.0, 67.5):
                a = math.radians(deg)
                pts.append((xe * math.cos(a), ze + (zt - ze) * math.sin(a)))
            pts.append((0.0, zt))
        return pts

    # -- windows -------------------------------------------------------------------------
    def side_window(self, y, z):
        """Is (y, z) on a side window (between the pillars, above the belt)?"""
        P = self.P
        if P.openTop:
            return False
        rake = math.radians(P.windshieldAngle)
        zb = self.belt(y)
        if z < zb + 0.025:
            return False
        zt = self.top(y)
        if z > zt - (0.035 + 0.06 * self.round) - 0.015:
            return False
        front = self.y_cowl + (z - self.z_cowl) / math.tan(rake) + 0.075
        if y < front:
            return False
        if P.bed:
            back = self.y_rt - 0.06
        else:
            rear = math.radians(P.rearAngle)
            back = self.y_rt + (self.H - z) / math.tan(rear) - 0.09
            back = min(back, self.y1 - 0.12)
        if y > back:
            return False
        span = back - front
        if span > 1.35:
            yb = front + span * 0.47
            if abs(y - yb) < 0.045:
                return False
        return True

    def windshield(self, y):
        return (not self.P.openTop) and self.y_cowl + 0.04 < y < self.y_wst - 0.02

    def rear_window(self, y):
        if self.P.openTop or self.P.bed:
            return self.P.bed and self.y_rt + 0.05 < y < self.y_cb - 0.05 and False
        return self.y_rt + 0.03 < y < self.y_rb - 0.035
