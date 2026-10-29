"""Mouse buttons, keys and mouse moves the way games read them (Windows SendInput).

pyautogui taps a button or key with no time between down and up and sends keys as virtual-key
codes, and it moves the mouse by setting the pointer's position. Many games miss all three: they
want a press that lasts a few frames, keys as hardware scan codes, and mouse movement as relative
motion (that is what turns the camera when the game hides the pointer). Ordinary apps take this
input just as well, so on Windows it is used everywhere. Elsewhere pyautogui does the same jobs.
"""

from __future__ import annotations

import ctypes
import sys

WINDOWS = sys.platform == "win32"

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
MOVE, ABSOLUTE, VIRTUALDESK = 0x0001, 0x8000, 0x4000
BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
KEYUP, SCANCODE, EXTENDED = 0x0002, 0x0008, 0x0001

# Keys whose scan code needs the "extended" flag (else e.g. the arrows arrive as the number pad's)
# page up/down, end, home, the arrows, insert, delete; the Windows keys, apps, number pad /, num lock,
# right ctrl and alt, print screen
EXTENDED_VK = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,
               0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5, 0x2C}

# The names press_keys takes, for when pyautogui's own table isn't there
VK = {"backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "pause": 0x13,
      "capslock": 0x14, "esc": 0x1B, "space": 0x20, " ": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
      "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C, "insert": 0x2D,
      "delete": 0x2E, "win": 0x5B, "winleft": 0x5B, "winright": 0x5C, "apps": 0x5D, "numlock": 0x90,
      "shiftleft": 0xA0, "shiftright": 0xA1, "ctrlleft": 0xA2, "ctrlright": 0xA3, "altleft": 0xA4,
      "altright": 0xA5, ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
      "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE}
VK.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
VK.update({f"num{i}": 0x60 + i for i in range(10)})

if WINDOWS:
    from ctypes import wintypes

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]


def vk_code(key: str) -> int | None:
    try:   # pyautogui's table knows every name it accepts
        from pyautogui import _pyautogui_win

        code = _pyautogui_win.keyboardMapping.get(key)
        if code is not None and code >= 0:
            return int(code) & 0xFF
    except Exception:  # noqa: BLE001 - its private table moved: ours below
        pass
    return VK.get(key)


def _send(*items) -> None:
    arr = (INPUT * len(items))(*items)
    sent = ctypes.windll.user32.SendInput(len(items), arr, ctypes.sizeof(INPUT))
    if sent != len(items):
        # Windows blocks input sent to a program running as administrator from one that isn't
        raise OSError("Windows didn't take the input (is the target app running as administrator? Then run "
                      "this app as administrator too).")


def key(name: str, up: bool) -> None:
    vk = vk_code(name)
    if vk is None:
        raise ValueError(f"unknown key {name!r}")
    scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    flags = (KEYUP if up else 0) | (SCANCODE if scan else 0) | (EXTENDED if vk in EXTENDED_VK else 0)
    _send(INPUT(type=INPUT_KEYBOARD, u=_U(ki=KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags))))


def button(name: str, up: bool) -> None:
    down_flag, up_flag = BUTTONS.get(name or "left", BUTTONS["left"])
    _send(INPUT(type=INPUT_MOUSE, u=_U(mi=MOUSEINPUT(dwFlags=up_flag if up else down_flag))))


def move_by(dx: int, dy: int) -> None:
    _send(INPUT(type=INPUT_MOUSE, u=_U(mi=MOUSEINPUT(dx=int(dx), dy=int(dy), dwFlags=MOVE))))


def move_to(x: int, y: int, seconds: float = 0.0, sleep=None) -> None:
    """Move the pointer like a real mouse does, in a few steps: games that draw their own pointer
    (Terraria) follow mouse input, not the pointer position that SetCursorPos sets."""
    import time

    user32 = ctypes.windll.user32
    left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)       # the desktop over all screens
    width, height = max(2, user32.GetSystemMetrics(78)), max(2, user32.GetSystemMetrics(79))
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    steps = max(2, min(30, int(seconds / 0.01)))
    for i in range(1, steps + 1):
        px = pt.x + (x - pt.x) * i / steps
        py = pt.y + (y - pt.y) * i / steps
        nx = round((px - left) * 65535 / (width - 1))
        ny = round((py - top) * 65535 / (height - 1))
        _send(INPUT(type=INPUT_MOUSE, u=_U(mi=MOUSEINPUT(dx=nx, dy=ny, dwFlags=MOVE | ABSOLUTE | VIRTUALDESK))))
        (sleep or time.sleep)(max(0.005, seconds / steps))
    user32.GetCursorPos(ctypes.byref(pt))
    if abs(pt.x - x) > 1 or abs(pt.y - y) > 1:       # rounding on a very wide desktop: land exactly
        user32.SetCursorPos(int(x), int(y))


def window_at(x: int, y: int):
    """(the top-level window under a screen point, the one in front now, its title)."""
    user32 = ctypes.windll.user32
    user32.WindowFromPoint.restype = wintypes.HWND
    user32.WindowFromPoint.argtypes = [wintypes.POINT]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    user32.GetForegroundWindow.restype = wintypes.HWND
    hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
    root = user32.GetAncestor(hwnd, 2) if hwnd else None      # GA_ROOT
    buf = ctypes.create_unicode_buffer(256)
    if root:
        user32.GetWindowTextW(root, buf, 256)
    return root, user32.GetForegroundWindow(), buf.value


def bring_to_front(hwnd) -> None:
    user32 = ctypes.windll.user32
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    key("alt", False)      # a program may only take the front after a key press: tap Alt first
    key("alt", True)
    user32.SetForegroundWindow(hwnd)
