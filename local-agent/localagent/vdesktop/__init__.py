"""The virtual computer: a Linux desktop the agent uses instead of the user's own screen.

On Windows it is a WSL distribution of its own (LocalAgentDesktop) that can't see the Windows
drives or start Windows programs; only the work folder is shared, as ~/Work. It runs as WSL 1 by
default: Linux programs on Windows' own Linux layer, with no virtual machine, so it needs neither
the hypervisor nor the Host Compute Service (settings "desktop_wsl_version": 2 for WSL 2). Its screen is
drawn in memory (Xvfb), so nothing appears on the Windows desktop. On Linux it is a hidden X
display on this PC. The screen tools run inside it (guest.py) and the app shows a live view of
it next to the chat.
"""

from __future__ import annotations

import base64
import collections
import dataclasses
import hashlib
import importlib.util
import io
import itertools
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

from ..config import DATA_DIR
from ..tools.base import ToolError, ToolResult

WINDOWS = sys.platform == "win32"
HERE = Path(__file__).resolve().parent
DISTRO = "LocalAgentDesktop"
HOME = DATA_DIR / "vdesktop"
GUEST_DIR = "/opt/localagent/app"       # where the app's part goes inside the distribution
SIZES = ["1024x768", "1280x800", "1366x768", "1600x900", "1920x1080"]
IDLE_STOP = 30 * 60                     # stopped after this long unused, to give the memory back

# The Linux it runs: Ubuntu 24.04, from the list `wsl --install` itself uses. The pinned copy is
# for when that list can't be read.
IMAGE_KEY = "Ubuntu-24.04"
IMAGE_URL = "https://releases.ubuntu.com/24.04.5/ubuntu-24.04.5-wsl-amd64.wsl"
IMAGE_SHA256 = "bb415d824822c4b878125729af451a5d18fb13d1cf5cbed9a7393ad64ac6039e"
DISTRO_INFO = "https://raw.githubusercontent.com/microsoft/WSL/master/distributions/DistributionInfo.json"

# Written into the image before WSL first starts it: no systemd, no Windows drives, no Windows
# programs, no GPU. setup.sh writes the final one (the same, plus the default user).
FIRST_WSL_CONF = """[boot]
systemd=false
[automount]
enabled=false
mountFsTab=false
[interop]
enabled=false
appendWindowsPath=false
[gpu]
enabled=false
"""
# wsl-distribution.conf would start Ubuntu's first-run questions and add a Start menu entry.
DROP_FROM_IMAGE = {"etc/wsl-distribution.conf"}

NO_WSL = ("WSL isn't installed. Press \"Install WSL\" (Windows asks for permission). When its window says "
          "it's done, restart the PC, then press Set up again.")
# WSL is there but can't start a virtual machine: the Virtual Machine Platform feature is off (or
# waiting for a restart), or virtualization is off in the firmware. The error codes are the same in
# every language; the sentences are WSL's English ones.
NO_VM = ("WSL can't start virtual machines on this PC yet: Windows' \"Virtual Machine Platform\" is off, or "
         "waiting for a restart.\n"
         "1. If WSL was installed or updated just now, restart the PC, then press Set up again.\n"
         "2. Otherwise press \"Turn on virtual machines\" in the setup window (Windows asks for permission). It "
         "switches on the Virtual Machine Platform and the Windows hypervisor. Restart the PC when it has finished, "
         "then press Set up again.\n"
         "3. If it still fails, open Task Manager > Performance > CPU. If it says \"Virtualization: Disabled\", "
         "switch on SVM (AMD) or VT-x (Intel) in the PC's BIOS settings.")
NO_VM_SIGNS = re.compile(r"HCS_E_SERVICE_NOT_AVAILABLE|HCS_E_HYPERV_NOT_INSTALLED|0x80370114|0x80370102|"
                         r"required feature is not installed|Virtual Machine Platform|"
                         r"virtualization is not enabled", re.I)
# WSL 1 needs Windows' own "Windows Subsystem for Linux" component (wsl --install doesn't always turn
# it on).
NO_WSL1 = ("Windows' \"Windows Subsystem for Linux\" component, which runs Linux without a virtual machine, "
           "isn't switched on yet, or is waiting for a restart.\n"
           "1. If you switched it on just now, restart the PC, then press Set up again.\n"
           "2. Otherwise press \"Turn on WSL 1\" in the setup window (Windows asks for permission), restart the "
           "PC when it has finished, then press Set up again.")
NO_WSL1_SIGNS = re.compile(r"WSL_E_WSL1_NOT_SUPPORTED|WSL1 is not supported|WSL 1 is not supported|"
                           r"Windows Subsystem for Linux\W{0,3} optional component", re.I)
VERSION_NOTE = {1: "WSL 1, no virtual machine", 2: "WSL 2, in a virtual machine"}
NOT_SET_UP = ("The virtual computer isn't set up yet. Tell the user to press Set up in the side panel "
              "(or Settings > What it may use), or to switch screen control to their own desktop.")
LOCAL_PACKAGES = "xvfb openbox tint2 xdotool wmctrl xclip x11-xserver-utils dbus-x11"


class DesktopError(Exception):
    """Something the user needs to read (not set up, didn't start...)."""


class WindowsFeatureMissing(DesktopError):
    """Windows lacks a component only the user can switch on (admin permission, then a restart)."""
    need = ""


class NoVirtualMachines(WindowsFeatureMissing):
    """WSL is installed but can't run a virtual machine (see NO_VM)."""
    need = "vm"


class NoWsl1(WindowsFeatureMissing):
    """WSL 1's Windows component is off (see NO_WSL1)."""
    need = "wsl1"


class Stopped(Exception):
    pass


def _said(out: str, keep: int = 300) -> str:
    """WSL's own words, short and on one line (its error code included), shown under the explanation:
    a screenshot then says what really happened."""
    lines = [line.strip() for line in out.splitlines()
             if line.strip() and not line.strip().startswith("If this error was unexpected")]
    return " ".join(lines)[-keep:]


def _failed(what: str, out: str, keep: int = 600) -> DesktopError:
    """A wsl.exe failure, in words the user can act on when it's a known one."""
    said = f"\n\nWSL said: {_said(out)}" if _said(out) else ""
    if NO_WSL1_SIGNS.search(out):
        return NoWsl1(NO_WSL1 + said)
    if NO_VM_SIGNS.search(out):
        return NoVirtualMachines(NO_VM + said)
    return DesktopError(f"{what}: {out[-keep:]}")


def kind() -> str:
    return "wsl" if WINDOWS else "local"


# ------------------------------------------------------------------ WSL
def wsl_exe() -> str | None:
    exe = shutil.which("wsl.exe") or shutil.which("wsl")
    if not exe and WINDOWS:
        p = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe"
        exe = str(p) if p.is_file() else None
    return exe


def _flags() -> int:
    return subprocess.CREATE_NO_WINDOW if WINDOWS else 0


def _env() -> dict:
    return dict(os.environ, WSL_UTF8="1")    # wsl.exe's own messages in UTF-8, not UTF-16


def text_of(raw: bytes) -> str:
    """wsl.exe writes UTF-16 unless WSL_UTF8 is honoured (older versions)."""
    if raw.count(b"\x00") > len(raw) // 4:
        return raw.decode("utf-16-le", "replace").replace("\ufeff", "")
    return raw.decode("utf-8", "replace")


def _wsl(args: list[str], *, data: bytes | None = None, timeout: float = 120) -> tuple[int, str]:
    exe = wsl_exe()
    if not exe:
        raise DesktopError(NO_WSL)
    try:
        r = subprocess.run([exe, *args], input=data, capture_output=True, timeout=timeout, env=_env(),
                           creationflags=_flags())
    except subprocess.TimeoutExpired:
        raise DesktopError(f"wsl {' '.join(args[:3])} didn't finish in {timeout:.0f} seconds.")
    except OSError as e:
        raise DesktopError(f"Couldn't run wsl.exe: {e}")
    return r.returncode, (text_of(r.stdout) + text_of(r.stderr)).strip()


def distros() -> list[str]:
    code, out = _wsl(["--list", "--quiet"], timeout=30)
    return [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else []


def distro_version() -> int:
    """The WSL version the distribution runs as (0 when WSL doesn't say)."""
    code, out = _wsl(["--list", "--verbose"], timeout=30)
    for line in out.splitlines() if code == 0 else []:
        words = line.replace("*", " ").split()
        if len(words) >= 3 and words[0] == DISTRO and words[-1].isdigit():
            return int(words[-1])
    return 0


def wsl_status() -> tuple[bool, str]:
    """Whether WSL is installed and answers (wsl --status fails, or asks to install, when it isn't),
    and what it said."""
    if not wsl_exe():
        return False, ""
    try:
        code, out = _wsl(["--status"], timeout=30)
    except DesktopError:
        return False, ""
    return code == 0, out


def wsl_ready() -> bool:
    return wsl_status()[0]


# What "Install WSL" / "Turn on ..." run, by what's missing: WSL itself (without a Linux distribution)
# and WSL 1's Windows component; for WSL 2 also the Virtual Machine Platform (an existing WSL install can
# leave it off) and the hypervisor starting at boot (something can have set that to off). Each step is a
# no-op when it's already so.
WSL1_FIX = ["wsl.exe --install --no-distribution",
            "dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart"]
WSL_FIX = [*WSL1_FIX,
           "dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart",
           "bcdedit.exe /set hypervisorlaunchtype auto"]


def install_wsl(need: str = "wsl") -> None:
    """Run the fix for `need` in a console window as admin (Windows asks for permission). The window
    stays open at the end so what it says, and the note to restart, can be read."""
    note = "When it has finished, restart the PC, then press Set up again in Local Agent."
    fix = WSL_FIX if need == "vm" or wsl_version() == 2 else WSL1_FIX
    steps = " & ".join([*fix, "echo.", f"echo {note}", "pause"])
    subprocess.Popen(["powershell", "-NoProfile", "-Command",
                      f"Start-Process cmd.exe -Verb RunAs -ArgumentList '/c {steps}'"],
                     creationflags=_flags())


# ------------------------------------------------------------------ state
def _marker() -> Path:
    return HOME / "installed.json"


def _marker_info() -> dict:
    try:
        info = json.loads(_marker().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return info if isinstance(info, dict) else {}


def wsl_version() -> int:
    """The WSL version new setups use: 1 (no virtual machine) unless the settings ask for 2."""
    from ..config import Settings

    return 2 if Settings.load().desktop_wsl_version == 2 else 1


def local_missing() -> list[str]:
    """On Linux: what this PC lacks to run the virtual computer itself."""
    missing = [p for p in ("Xvfb", "openbox", "xdotool", "wmctrl") if not shutil.which(p)]
    # found, not imported: pyautogui connects to the screen when imported
    return missing + [m for m in ("mss", "PIL", "pyautogui", "Xlib") if importlib.util.find_spec(m) is None]


def installed() -> bool:
    if kind() == "local":
        return not local_missing()
    return _marker().is_file() and DISTRO in distros()


def status() -> dict:
    """For the settings and the side panel. Runs wsl.exe on Windows, so not on the GUI thread."""
    d = desktop()
    # needs: what Windows still lacks: "wsl" (not installed), "vm" (can't run virtual machines) or "wsl1"
    # wsl_version: what it was set up as (0: not set up, or set up before this was recorded)
    info = {"kind": kind(), "running": d.running, "installed": False, "problem": "", "wsl": True, "needs": "",
            "wsl_version": 0}
    if kind() == "local":
        missing = local_missing()
        info["installed"] = not missing
        if missing:
            info["problem"] = (f"Missing on this PC: {', '.join(missing)}. Install them with your package "
                               f"manager (e.g. sudo apt install {LOCAL_PACKAGES}).")
        return info
    ready, said = wsl_status()
    no_vm = bool(NO_VM_SIGNS.search(said)) and wsl_version() == 2      # WSL 1 needs no virtual machine
    if not ready:
        info.update(wsl=False, problem=NO_VM if no_vm else NO_WSL, needs="vm" if no_vm else "wsl")
        return info
    names = distros()
    if no_vm:
        info.update(problem=NO_VM, needs="vm")
    elif wsl_version() == 1 and DISTRO not in names and NO_WSL1_SIGNS.search(said):
        info.update(problem=NO_WSL1, needs="wsl1")     # (one already added keeps the version it was added as)
    info["installed"] = _marker().is_file() and DISTRO in names
    if info["installed"]:
        info["wsl_version"] = int(_marker_info().get("wsl_version") or 0)
    if DISTRO in names and not _marker().is_file() and not info["needs"]:
        info["problem"] = "Setup didn't finish last time. Press Set up to finish it."
    return info


# ------------------------------------------------------------------ the app's part inside it
def guest_files() -> dict[str, bytes]:
    tools = HERE.parent / "tools"
    return {
        "guest.py": (HERE / "guest.py").read_bytes(),
        "start.sh": (HERE / "start.sh").read_bytes(),
        "setup.sh": (HERE / "setup.sh").read_bytes(),
        "vtools/__init__.py": b"",
        "vtools/base.py": (tools / "base.py").read_bytes(),
        "vtools/screen.py": (tools / "screen.py").read_bytes(),
    }


def guest_tar() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, data in guest_files().items():
            if name.endswith(".sh"):
                data = data.replace(b"\r\n", b"\n")     # a git checkout on Windows may have added \r
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o755 if name.endswith(".sh") else 0o644, int(time.time())
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def push_guest() -> None:
    """Copy the app's part into the distribution (every start, so it always matches the app)."""
    new = GUEST_DIR + ".new"
    script = (f"rm -rf {new} && mkdir -p {new} && tar -xf - -C {new} && rm -rf {GUEST_DIR} && "
              f"mv {new} {GUEST_DIR}")
    code, out = _wsl(["-d", DISTRO, "-u", "root", "--cd", "/", "-e", "/bin/sh", "-c", script], data=guest_tar(),
                     timeout=120)
    if code != 0:
        raise _failed("Couldn't copy the app's files into the virtual computer", out, 500)


def local_app_dir() -> Path:
    """On Linux: the same files, run from the app's data folder."""
    app = HOME / "app"
    for name, data in guest_files().items():
        p = app / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.is_file() or p.read_bytes() != data:
            p.write_bytes(data)
    return app


# ------------------------------------------------------------------ setting it up (Windows)
def image_source(session=None) -> tuple[str, str]:
    """The URL and SHA-256 of the Ubuntu image, from the list wsl --install uses, else the pinned one."""
    try:
        import requests

        r = (session or requests).get(DISTRO_INFO, timeout=20)
        r.raise_for_status()
        for group in r.json().get("ModernDistributions", {}).values():
            for d in group:
                if d.get("Name") == IMAGE_KEY and d.get("Amd64Url", {}).get("Url"):
                    return d["Amd64Url"]["Url"], d["Amd64Url"]["Sha256"].lower().removeprefix("0x")
    except Exception:  # noqa: BLE001 - offline, moved, changed format: the pinned one
        pass
    return IMAGE_URL, IMAGE_SHA256


def download_image(progress=lambda msg, frac: None, stop: threading.Event | None = None, session=None,
                   dest: Path | None = None) -> Path:
    """Download the image (about 390 MB), resuming a .part file, and check its SHA-256."""
    import requests

    session = session or requests.Session()
    url, sha = image_source(session)
    dest = dest or HOME / "ubuntu-24.04.wsl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_file() and _sha256(dest, stop) == sha:
        return dest
    part = dest.with_name(dest.name + ".part")
    start = part.stat().st_size if part.is_file() else 0
    try:
        with session.get(url, headers={"Range": f"bytes={start}-"} if start else {}, stream=True, timeout=60) as r:
            if r.status_code == 416:          # already complete
                pass
            else:
                r.raise_for_status()
                if start and r.status_code != 206:
                    start = 0
                total = start + int(r.headers.get("content-length") or 0)
                got = start
                with open(part, "ab" if start else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        if stop is not None and stop.is_set():
                            raise Stopped()
                        f.write(chunk)
                        got += len(chunk)
                        progress(f"Downloading Ubuntu ({got / 1e6:.0f} of {total / 1e6:.0f} MB)...",
                                 got / total if total else None)
    except requests.RequestException as e:
        raise DesktopError(f"The download broke off ({e}). Press Set up again to resume it.")
    progress("Checking the download...", None)
    if _sha256(part, stop) != sha:
        part.unlink(missing_ok=True)
        raise DesktopError("The Ubuntu download was damaged on the way (its checksum didn't match) and has been "
                           "deleted. Press Set up again to download it again.")
    part.replace(dest)
    return dest


def _sha256(path: Path, stop=None) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            if stop is not None and stop.is_set():
                raise Stopped()
            h.update(chunk)
    return h.hexdigest()


def repack(src: Path, dst: Path, stop: threading.Event | None = None) -> None:
    """Copy the image's files into a plain tar for wsl --import, without wsl-distribution.conf and
    with this app's wsl.conf. File owners, modes, links and extended attributes are kept."""
    replace = {"etc/wsl.conf": FIRST_WSL_CONF.encode()}
    tmp = dst.with_name(dst.name + ".part")
    enc = {"encoding": "utf-8", "errors": "surrogateescape"}
    with tarfile.open(src, "r|*", **enc) as tin, tarfile.open(tmp, "w|", format=tarfile.PAX_FORMAT, **enc) as tout:
        for m in tin:
            if stop is not None and stop.is_set():
                raise Stopped()
            name = m.name.removeprefix("./").rstrip("/")
            if name in DROP_FROM_IMAGE:
                continue
            if name in replace and m.isreg():
                data = replace.pop(name)
                m.size = len(data)
                tout.addfile(m, io.BytesIO(data))
            else:
                tout.addfile(m, tin.extractfile(m) if m.isreg() else None)
        for name, data in replace.items():      # the image had none
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o644, int(time.time())
            tout.addfile(info, io.BytesIO(data))
    tmp.replace(dst)


def install(progress=lambda msg, frac: None, log=lambda line: None, stop: threading.Event | None = None,
            session=None) -> list[str]:
    """Set up (or repair) the virtual computer. Returns warnings (e.g. Firefox couldn't be installed).
    progress(message, fraction or None) and log(line) are called from this thread."""
    if kind() == "local":
        missing = local_missing()
        if missing:
            raise DesktopError(f"Missing on this PC: {', '.join(missing)}. Install them with your package manager "
                               f"(e.g. sudo apt install {LOCAL_PACKAGES}) and pip (mss, pillow, pyautogui).")
        return []
    stop = stop or threading.Event()
    want = wsl_version()
    ready, said = wsl_status()
    if want == 2 and NO_VM_SIGNS.search(said):      # before downloading anything
        raise NoVirtualMachines(NO_VM)
    if not ready:
        raise DesktopError(NO_WSL)
    names = distros()
    if want == 1 and DISTRO not in names and NO_WSL1_SIGNS.search(said):
        raise _failed("", said)                     # wsl --status says so when WSL 1's driver isn't there
    desktop().stop()
    HOME.mkdir(parents=True, exist_ok=True)
    version = 0
    if DISTRO in names:
        version = int(_marker_info().get("wsl_version") or 0) or distro_version()
    else:
        img = download_image(progress, stop, session)
        tar = HOME / "rootfs.tar"
        progress("Preparing the Linux image...", None)
        try:
            repack(img, tar, stop)
            version = _import(tar, want, progress)
        finally:
            tar.unlink(missing_ok=True)
            tar.with_name(tar.name + ".part").unlink(missing_ok=True)
        img.unlink(missing_ok=True)
    progress("Copying the app's part into it...", None)
    push_guest()
    warnings = _run_setup(progress, log, stop)
    _wsl(["--terminate", DISTRO], timeout=60)     # wsl.conf applies from the next start
    _marker().write_text(json.dumps({"done": time.strftime("%Y-%m-%d %H:%M"), "warnings": warnings,
                                     "wsl_version": version}), encoding="utf-8")
    progress("The virtual computer is ready.", 1.0)
    return warnings


DISK = {1: "disk-wsl1", 2: "disk"}


def _import(tar: Path, want: int, progress) -> int:
    """Add the distribution to WSL as `want`. A WSL 2 that can't start its virtual machine falls back
    to WSL 1. Raises with what to do (the download is kept for the next try)."""
    for version in ([2, 1] if want == 2 else [1]):
        progress("Adding it to WSL..." if version == want else
                 "This PC can't run WSL 2's virtual machine: adding it as WSL 1, which needs none...", None)
        code, out = _wsl(["--import", DISTRO, str(HOME / DISK[version]), str(tar), "--version", str(version)],
                         timeout=1800)
        if code == 0:
            return version
        if version == 2 and NO_VM_SIGNS.search(out) and not NO_WSL1_SIGNS.search(out):
            continue
        raise _failed("WSL couldn't add the virtual computer", out)
    raise AssertionError("unreachable")


def _run_setup(progress, log, stop: threading.Event) -> list[str]:
    proc = subprocess.Popen([wsl_exe(), "-d", DISTRO, "-u", "root", "--cd", "/", "-e", "/bin/sh",
                             f"{GUEST_DIR}/setup.sh"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=_env(),
                            creationflags=_flags())
    warnings, tail, done = [], collections.deque(maxlen=30), False

    def cancel():
        while proc.poll() is None:
            if stop.wait(0.5):
                proc.kill()
                return
    threading.Thread(target=cancel, daemon=True).start()
    try:
        for raw in proc.stdout:
            line = text_of(raw).rstrip()
            m = re.match(r"@@step (\d+) (\d+) (.*)", line)
            if m:
                n, total = int(m.group(1)), int(m.group(2))
                progress(f"{m.group(3)} (step {n} of {total})...", (n - 1) / total)
            elif line.startswith("@@warn "):
                warnings.append(line[7:])
            elif line == "@@done":
                done = True
            else:
                tail.append(line)
            log(line)
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
    if stop.is_set():
        raise Stopped()
    if proc.returncode != 0 or not done:
        said = "\n".join(list(tail)[-8:])
        err = _failed("", said)
        if isinstance(err, WindowsFeatureMissing):
            raise err
        raise DesktopError("Setting up the virtual computer failed:\n" + said)
    return warnings


def remove() -> None:
    """Delete the virtual computer and everything in it (the shared work folder is untouched)."""
    desktop().stop()
    if kind() == "wsl" and DISTRO in distros():
        code, out = _wsl(["--unregister", DISTRO], timeout=300)
        if code != 0:
            raise _failed("WSL couldn't remove it", out, 400)
    _marker().unlink(missing_ok=True)
    for folder in DISK.values():
        shutil.rmtree(HOME / folder, ignore_errors=True)


# ------------------------------------------------------------------ running it
class Desktop:
    """The running virtual computer: one guest process, requests and answers as JSON lines."""

    def __init__(self):
        self._lock = threading.RLock()        # start and stop
        self._wlock = threading.Lock()        # writing to the guest
        self._proc: subprocess.Popen | None = None
        self._ids = itertools.count(1)
        self._waiting: dict[int, list] = {}
        self._ready = threading.Event()
        self._start_error = ""
        self._want: tuple | None = None
        self._listeners: list = []
        self.size = (0, 0)
        self.history: collections.deque = collections.deque(maxlen=300)
        self.log_tail: collections.deque = collections.deque(maxlen=200)
        self.last_used = 0.0
        self.busy = 0
        self.generation = 0          # counts starts: a chat that saw another one has lost its windows
        self.state, self.state_text = "stopped", ""
        self._idle = threading.Thread(target=self._idle_watch, daemon=True)
        self._idle.start()

    # -------------------------------------------------------------- events
    def listen(self, fn) -> None:
        self._listeners.append(fn)

    def unlisten(self, fn) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _emit(self, ev: dict) -> None:
        ev.setdefault("time", time.time())
        if ev["type"] in ("action", "result"):
            self.history.append(ev)
        for fn in list(self._listeners):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - a broken listener must not break a tool call
                pass

    def _set_state(self, state: str, text: str = "") -> None:
        self.state, self.state_text = state, text
        self._emit({"type": "state", "state": state, "text": text})

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None and self._ready.is_set()

    # -------------------------------------------------------------- start / stop
    def _wanted(self, settings) -> tuple:
        size = settings.desktop_size if settings.desktop_size in SIZES else "1280x800"
        work = str(settings.workspace_path()) if settings.desktop_share_work else ""
        return size, work

    def start(self, settings, stop: threading.Event | None = None) -> None:
        """Start it (or restart it when the screen size or shared folder changed). Blocks until it's
        ready; raises DesktopError with what went wrong."""
        with self._lock:
            want = self._wanted(settings)
            if self.running and want == self._want:
                return
            if self.running:
                self._stop_locked()
            self._set_state("starting", "Starting the virtual computer...")
            try:
                self._launch(want)
                self._wait_ready(stop)
            except Exception as e:
                self._kill()
                msg = str(e) if isinstance(e, DesktopError) else f"{type(e).__name__}: {e}"
                known = _failed("", msg)
                if isinstance(known, WindowsFeatureMissing):
                    msg = str(known)
                self._set_state("error", msg)
                raise DesktopError(msg) from e
            self._want = want
            self.generation += 1
            self.last_used = time.time()
            self._set_state("running", f"Running ({self.size[0]}x{self.size[1]})")

    def _launch(self, want: tuple) -> None:
        self._ready.clear()
        self._start_error = ""
        self.log_tail.clear()
        HOME.mkdir(parents=True, exist_ok=True)
        args, env = self.command(*want)
        self._proc = proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, env=env, cwd=str(HOME), creationflags=_flags())
        threading.Thread(target=self._read, args=(proc,), daemon=True).start()
        threading.Thread(target=self._read_log, args=(proc,), daemon=True).start()

    def command(self, size: str, work: str) -> tuple[list[str], dict]:
        """What runs the guest: start.sh as root in the WSL distribution, or guest.py here on Linux."""
        if kind() == "wsl":
            if not installed():
                raise DesktopError(NOT_SET_UP)
            push_guest()
            args = [wsl_exe(), "-d", DISTRO, "-u", "root", "--cd", "/", "-e", "/bin/sh", f"{GUEST_DIR}/start.sh",
                    "--size", size]
            if work:   # base64: a Windows path through wsl.exe's quoting, unharmed
                args += ["--work-b64", base64.b64encode(work.encode("utf-8")).decode()]
            return args, _env()
        missing = local_missing()
        if missing:
            raise DesktopError(f"Missing on this PC: {', '.join(missing)}.")
        app = local_app_dir()
        home = HOME / "home"           # a home of its own: its own Firefox profile and settings, not the user's
        home.mkdir(parents=True, exist_ok=True)
        args = [sys.executable, "-u", str(app / "guest.py"), "--size", size, "--work", work or str(HOME / "work")]
        return args, dict(_env(), HOME=str(home))

    def _wait_ready(self, stop) -> None:
        end = time.time() + 120         # the first start of WSL after a reboot can be slow
        while not self._ready.wait(0.2):
            if self._start_error:
                raise DesktopError(f"The virtual computer didn't start: {self._start_error}")
            if self._proc is None or self._proc.poll() is not None:
                tail = "\n".join(list(self.log_tail)[-6:])
                raise DesktopError("The virtual computer stopped while starting." + (f"\n{tail}" if tail else ""))
            if stop is not None and stop.is_set():
                raise DesktopError("Stopped while the virtual computer was starting.")
            if time.time() > end:
                raise DesktopError("The virtual computer didn't start within 2 minutes.")

    def _read(self, proc: subprocess.Popen) -> None:
        for raw in proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                self.log_tail.append(text_of(raw).rstrip())
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("event") == "ready":
                self.size = (int(msg.get("width") or 0), int(msg.get("height") or 0))
                self._ready.set()
            elif msg.get("event") == "error":
                self._start_error = str(msg.get("message") or "unknown error")
            elif "id" in msg:
                w = self._waiting.get(msg["id"])
                if w:
                    w[1] = msg
                    w[0].set()
        # gone: wake everyone still waiting
        for w in list(self._waiting.values()):
            w[0].set()
        if proc is self._proc and self._ready.is_set() and self.state == "running":
            self._ready.clear()
            tail = "\n".join(list(self.log_tail)[-4:])
            self._set_state("error", "The virtual computer stopped unexpectedly." + (f"\n{tail}" if tail else ""))

    def _read_log(self, proc: subprocess.Popen) -> None:
        path = HOME / "desktop.log"
        try:
            f = open(path, "w", encoding="utf-8")
        except OSError:
            f = None
        try:
            for raw in proc.stderr:
                line = text_of(raw).rstrip()
                self.log_tail.append(line)
                if f:
                    f.write(line + "\n")
                    f.flush()
        finally:
            if f:
                f.close()

    def stop(self) -> None:
        with self._lock:
            if self._proc is None:
                return
            self._stop_locked()
            self._set_state("stopped", "")

    def _stop_locked(self) -> None:
        proc = self._proc
        self._ready.clear()          # from here on its going away isn't a surprise
        if proc is not None and proc.poll() is None:
            try:
                with self._wlock:
                    proc.stdin.write(b'{"op": "quit"}\n')
                    proc.stdin.flush()
                proc.wait(5)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        self._kill()
        if kind() == "wsl":
            try:
                _wsl(["--terminate", DISTRO], timeout=60)    # everything in it, and its memory
            except DesktopError:
                pass

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        self._ready.clear()
        if proc is not None and proc.poll() is None:
            proc.kill()
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                pass

    def _idle_watch(self) -> None:
        while True:
            time.sleep(30)
            self.idle_check()

    def idle_check(self) -> None:
        """Stop it when nothing has used it for a while (watching it doesn't count)."""
        if self.running and not self.busy and time.time() - self.last_used > IDLE_STOP:
            with self._lock:
                if self.running and not self.busy and time.time() - self.last_used > IDLE_STOP:
                    self._stop_locked()
                    self._set_state("stopped", f"Stopped after {IDLE_STOP // 60} minutes unused, to free "
                                               "memory. It starts again when needed.")

    # -------------------------------------------------------------- requests
    def request(self, msg: dict, timeout: float = 60, stop: threading.Event | None = None) -> dict:
        proc = self._proc
        if proc is None or proc.poll() is not None or not self._ready.is_set():
            raise DesktopError("The virtual computer isn't running.")
        rid = next(self._ids)
        waiter = [threading.Event(), None]
        self._waiting[rid] = waiter
        try:
            line = (json.dumps({**msg, "id": rid}) + "\n").encode("utf-8")
            try:
                with self._wlock:
                    proc.stdin.write(line)
                    proc.stdin.flush()
            except (OSError, ValueError):
                raise DesktopError("The virtual computer stopped.")
            end = time.time() + timeout
            stopped_at = None
            while not waiter[0].wait(0.1):
                if time.time() > end:
                    raise DesktopError(f"The virtual computer didn't answer within {timeout:.0f} seconds.")
                if stop is not None and stop.is_set():
                    stopped_at = stopped_at or time.time()
                    if time.time() - stopped_at > 3:
                        raise DesktopError("Stopped.")
            if waiter[1] is None:
                raise DesktopError("The virtual computer stopped.")
            return waiter[1]
        finally:
            self._waiting.pop(rid, None)

    def call(self, name: str, args: dict, ctx, summary: str = "") -> ToolResult:
        """Run a screen tool in the virtual computer, starting it first if needed."""
        s = ctx.settings
        self.busy += 1
        try:
            try:
                self.start(s, ctx.stop)
            except DesktopError as e:
                raise ToolError(str(e))
            self.last_used = time.time()
            seen, ctx.screen["desktop_generation"] = ctx.screen.get("desktop_generation"), self.generation
            restarted = seen is not None and seen != self.generation
            self._emit({"type": "action", "name": name, "summary": summary or name})
            opts = {"screenshot_max_side": s.screenshot_max_side, "coord_mode": s.coord_mode,
                    "grid_overlay": s.grid_overlay, "screenshot_after_action": s.screenshot_after_action,
                    "model_name": ctx.screen.get("model_name") or getattr(s, "model", ""),
                    "capture_after": getattr(ctx, "capture_after", True)}
            try:
                resp = self.request({"op": "tool", "name": name, "args": args, "opts": opts}, timeout=180,
                                    stop=ctx.stop)
            except DesktopError as e:
                self._emit({"type": "result", "name": name, "ok": False, "text": str(e)})
                raise ToolError(str(e))
            self.last_used = time.time()
            ok = bool(resp.get("ok"))
            text = str(resp.get("text") or "") if ok else str(resp.get("error") or "failed")
            self._emit({"type": "result", "name": name, "ok": ok, "text": text.splitlines()[0][:200] if text else "",
                        "cursor": resp.get("cursor")})
            if restarted:
                text = ("(The virtual computer was restarted since your last action here: windows that were "
                        "open before are closed.)\n" + text)
            if not ok:
                raise ToolError(text)
            images = [base64.b64decode(i) for i in resp.get("images") or []]
            return ToolResult(text, images=images)
        finally:
            self.busy -= 1

    def peek(self, width: int = 640, quality: int = 70) -> dict | None:
        """A small JPEG of the screen right now, for the live view (None when it isn't running)."""
        if not self.running:
            return None
        try:
            resp = self.request({"op": "peek", "width": width, "quality": quality}, timeout=10)
        except DesktopError:
            return None
        if not resp.get("ok"):
            return None
        resp["jpeg"] = base64.b64decode(resp.get("jpeg") or "")
        return resp

    def user_input(self, event: dict) -> dict:
        """The user's own click or keys from the live view (real screen pixels)."""
        self.last_used = time.time()
        return self.request({**event, "op": "input"}, timeout=30)


_desktop: Desktop | None = None
_desktop_lock = threading.Lock()


def desktop() -> Desktop:
    global _desktop
    with _desktop_lock:
        if _desktop is None:
            _desktop = Desktop()
        return _desktop


# ------------------------------------------------------------------ the tools, pointed at it
WINDOWS_ONLY = {"ui_elements", "click_element"}   # Windows accessibility: nothing to read in Linux apps
DESCRIPTIONS = {
    "screenshot": """
Take a screenshot of your virtual computer's screen. The red circle is the mouse pointer. Use the
coordinates of this image with click, drag, scroll and zoom.""",
    "open": """
Open a program, file, folder or web address on your virtual computer, the way double-clicking
would, e.g. "firefox", "https://example.com", "localc", "lowriter", "pcmanfm", "mousepad", "xterm",
"~/Work/report.docx". Programs open by name only, without arguments.""",
}


def wrap_tools(tools: list) -> list:
    """The screen tools, run in the virtual computer instead of on this PC's screen."""
    out = []
    for t in tools:
        if t.group != "control":
            out.append(t)
        elif t.name not in WINDOWS_ONLY:
            params = {} if t.name == "screenshot" else t.params     # one screen: no monitor to pick
            out.append(dataclasses.replace(t, func=_remote(t), description=DESCRIPTIONS.get(t.name, t.description)
                                           .strip(), params=params))
    return out


# for the activity list, the tools that don't describe themselves
FRIENDLY = {"screenshot": "Looked at the screen", "zoom": "Zoomed in to read", "list_windows": "Listed the windows",
            "get_clipboard": "Read the clipboard"}


def _remote(t):
    def run(ctx, a):
        return desktop().call(t.name, a, ctx, t.describe(a) if t.describe else FRIENDLY.get(t.name, t.name))
    return run


def active(settings) -> bool:
    return getattr(settings, "computer", "desktop") == "virtual"
