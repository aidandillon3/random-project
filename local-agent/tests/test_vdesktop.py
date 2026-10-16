"""The virtual computer: setting it up in WSL, talking to it, and the screen tools pointed at it."""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())

from localagent import prompts, vdesktop  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.tools import REGISTRY, ToolContext, ToolError, enabled_tools  # noqa: E402

# A stand-in for guest.py: the same protocol, canned answers.
FAKE_GUEST = textwrap.dedent('''
    import base64, json, sys, time
    if "--fail" in sys.argv:
        print(json.dumps({"event": "error", "message": "Xvfb not installed"}), flush=True)
        sys.exit(1)
    print("guest: starting", file=sys.stderr, flush=True)
    print(json.dumps({"event": "ready", "width": 1280, "height": 800}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        op, rid = req.get("op"), req.get("id")
        if op == "quit":
            break
        if op == "peek":
            out = {"ok": True, "jpeg": base64.b64encode(b"JPEG").decode(), "width": 1280, "height": 800,
                   "cursor": [1, 2], "window": "Firefox"}
        elif op == "input":
            out = {"ok": True, "cursor": [req.get("x"), req.get("y")]}
        else:
            name = req.get("name")
            if name == "die":
                sys.exit(3)
            if name == "slow":
                time.sleep(1.5)
            if name == "hang":
                time.sleep(60)
            if name == "fail":
                out = {"ok": False, "error": "No window title contains 'x'.", "cursor": [5, 5]}
            else:
                out = {"ok": True, "text": json.dumps({"name": name, "args": req.get("args"), "opts": req.get("opts")}),
                       "images": [base64.b64encode(b"PNG" + name.encode()).decode()], "cursor": [10, 20]}
        print(json.dumps({"id": rid, **out}), flush=True)
''')


OK_STATUS = "Default Distribution: Ubuntu\nDefault Version: 2"
# what wsl --import said on a PC where the Virtual Machine Platform wasn't on yet
NO_VM_IMPORT = ("The operation could not be started because a required feature is not installed.\r\n"
                "Error code: Wsl/Service/RegisterDistro/CreateVm/HCS/HCS_E_SERVICE_NOT_AVAILABLE\r\n"
                "If this error was unexpected, please consider searching for existing issues or filing a new "
                "issue at https://github.com/microsoft/WSL/issues.")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.object(vdesktop, "HOME", self.tmp / "vdesktop")
        p.start()
        self.addCleanup(p.stop)


class FakeDesktop(vdesktop.Desktop):
    def __init__(self, script: Path, extra=()):
        super().__init__()
        self.script, self.extra = script, list(extra)

    def command(self, size, work):
        return [sys.executable, "-u", str(self.script), *self.extra], dict(os.environ)


class ProtocolTest(Base):
    def setUp(self):
        super().setUp()
        self.script = self.tmp / "fake_guest.py"
        self.script.write_text(FAKE_GUEST, encoding="utf-8")
        self.d = FakeDesktop(self.script)
        self.addCleanup(self.d.stop)
        self.events = []
        self.d.listen(self.events.append)
        p = mock.patch.object(vdesktop, "_desktop", self.d)
        p.start()
        self.addCleanup(p.stop)
        self.s = Settings(workspace=str(self.tmp / "ws"), computer="virtual")
        self.ctx = ToolContext(settings=self.s, workspace=self.tmp / "ws")
        self.ctx.screen["model_name"] = "qwen3.8-27b"

    def tool(self, name):
        return {t.name: t for t in vdesktop.wrap_tools(list(REGISTRY.values()))}[name]

    def test_a_tool_call_starts_it_and_comes_back_as_a_tool_result(self):
        r = self.tool("click").func(self.ctx, {"x": 500, "y": 300})
        sent = json.loads(r.text)
        self.assertEqual((sent["name"], sent["args"]), ("click", {"x": 500, "y": 300}))
        opts = sent["opts"]
        self.assertEqual(opts["model_name"], "qwen3.8-27b")
        self.assertTrue(opts["capture_after"])
        self.assertEqual(opts["screenshot_max_side"], self.s.screenshot_max_side)
        self.assertEqual(r.images, [b"PNGclick"])
        self.assertTrue(self.d.running)
        self.assertEqual(self.d.size, (1280, 800))
        kinds = [(e["type"], e.get("state") or e.get("name")) for e in self.events]
        self.assertEqual(kinds, [("state", "starting"), ("state", "running"), ("action", "click"), ("result", "click")])
        self.assertEqual(self.events[2]["summary"], "left click x1 at (500, 300)")
        self.assertEqual(self.events[3]["cursor"], [10, 20])
        self.assertEqual(len(self.d.history), 2)

    def test_capture_after_and_friendly_names_go_along(self):
        self.ctx.capture_after = False
        r = self.tool("screenshot").func(self.ctx, {})
        self.assertFalse(json.loads(r.text)["opts"]["capture_after"])
        self.assertEqual([e["summary"] for e in self.events if e["type"] == "action"], ["Looked at the screen"])

    def test_a_tool_error_becomes_a_tool_error(self):
        with self.assertRaises(ToolError) as cm:
            self.d.call("fail", {}, self.ctx)
        self.assertIn("No window title", str(cm.exception))
        result = [e for e in self.events if e["type"] == "result"][-1]
        self.assertFalse(result["ok"])

    def test_peek_and_user_input(self):
        self.assertIsNone(self.d.peek())            # not running: nothing, no start
        self.d.start(self.s)
        frame = self.d.peek(480)
        self.assertEqual((frame["jpeg"], frame["width"], frame["window"]), (b"JPEG", 1280, "Firefox"))
        self.assertEqual(self.d.user_input({"kind": "click", "x": 3, "y": 4})["cursor"], [3, 4])

    def test_peeks_are_answered_while_a_tool_is_running(self):
        self.d.start(self.s)
        done = []
        t = threading.Thread(target=lambda: done.append(self.d.call("slow", {}, self.ctx)))
        t.start()
        time.sleep(0.2)
        # the fake guest answers in order, so this only checks requests don't block each other host-side
        self.assertTrue(self.d.request({"op": "peek"}, timeout=5)["ok"])
        t.join(5)
        self.assertEqual(len(done), 1)

    def test_it_dying_mid_call_is_an_error_not_a_hang(self):
        self.d.start(self.s)
        t0 = time.time()
        with self.assertRaises(ToolError) as cm:
            self.d.call("die", {}, self.ctx)
        self.assertLess(time.time() - t0, 5)
        self.assertIn("stopped", str(cm.exception))
        self.assertFalse(self.d.running)
        self.assertIn("error", [e.get("state") for e in self.events])
        # and the next call starts it again, telling the model its windows are gone
        r = self.d.call("screenshot", {}, self.ctx)
        self.assertTrue(r.text.startswith("(The virtual computer was restarted"))
        r = self.d.call("screenshot", {}, self.ctx)
        self.assertFalse(r.text.startswith("(The virtual computer was restarted"))

    def test_a_deliberate_stop_is_not_reported_as_a_problem(self):
        self.d.start(self.s)
        self.d.stop()
        self.assertEqual([e["state"] for e in self.events if e["type"] == "state"], ["starting", "running", "stopped"])

    def test_a_start_error_is_reported(self):
        d = FakeDesktop(self.script, ["--fail"])
        self.addCleanup(d.stop)
        with self.assertRaises(vdesktop.DesktopError) as cm:
            d.start(self.s)
        self.assertIn("Xvfb not installed", str(cm.exception))
        self.assertEqual(d.state, "error")
        with self.assertRaises(ToolError):
            d.call("click", {"x": 1, "y": 1}, self.ctx)

    def test_stop_while_waiting_ends_the_call(self):
        self.d.start(self.s)
        self.addCleanup(self.d._kill)        # it's still busy; no polite quit
        threading.Timer(0.3, self.ctx.stop.set).start()
        t0 = time.time()
        with self.assertRaises(ToolError) as cm:
            self.d.call("hang", {}, self.ctx)
        self.assertIn("Stopped", str(cm.exception))
        self.assertLess(time.time() - t0, 10)

    def test_a_new_screen_size_restarts_it(self):
        self.d.start(self.s)
        first = self.d._proc
        self.d.start(self.s)
        self.assertIs(self.d._proc, first)          # same settings: the same one
        self.s.desktop_size = "1920x1080"
        self.d.start(self.s)
        self.assertIsNot(self.d._proc, first)

    def test_idle_stop(self):
        self.d.start(self.s)
        self.d.idle_check()
        self.assertTrue(self.d.running)
        self.d.busy = 1                             # in the middle of a tool call: never
        self.d.last_used = time.time() - vdesktop.IDLE_STOP - 1
        self.d.idle_check()
        self.assertTrue(self.d.running)
        self.d.busy = 0
        self.d.idle_check()
        self.assertFalse(self.d.running)
        self.assertIn("unused", self.events[-1]["text"])


class ToolsTest(unittest.TestCase):
    def test_virtual_mode_points_the_screen_tools_at_it(self):
        ws = Path(tempfile.mkdtemp())
        virtual = {t.name: t for t in enabled_tools(Settings(computer="virtual"), 0, ws)}
        real = {t.name: t for t in enabled_tools(Settings(computer="desktop"), 0, ws)}
        control = {n for n, t in real.items() if t.group == "control"}
        self.assertTrue({"ui_elements", "click_element"} <= control)
        self.assertEqual({n for n, t in virtual.items() if t.group == "control"},
                         control - {"ui_elements", "click_element"})
        for n in control - vdesktop.WINDOWS_ONLY:
            self.assertIsNot(virtual[n].func, real[n].func, n)
            self.assertEqual((virtual[n].risk, virtual[n].required), (real[n].risk, real[n].required))
        self.assertIs(virtual["read_file"], real["read_file"])
        self.assertEqual(virtual["screenshot"].params, {})
        self.assertIn("virtual computer", virtual["open"].description)
        self.assertIn("localc", virtual["open"].description)
        self.assertIsNotNone(virtual["open"].risk_for)       # opening a .sh still asks like a command
        none = enabled_tools(Settings(computer="virtual", enable_control=False), 0, ws)
        self.assertFalse(any(t.group == "control" for t in none))

    def test_the_system_prompt_says_where_the_screen_is(self):
        ws = Path(tempfile.mkdtemp())

        def prompt(**kw):
            s = Settings(**kw)
            return prompts.build_system_prompt(s, ws, enabled_tools(s, 0, ws), "", "powershell", False)
        virtual, real = prompt(computer="virtual"), prompt(computer="desktop")
        self.assertIn("own virtual computer", virtual)
        self.assertIn("~/Work folder is the work folder", virtual)
        self.assertNotIn("ui_elements", virtual)
        self.assertNotIn("virtual computer", real)
        self.assertIn("ui_elements and click_element over clicking", real)
        self.assertIn("is its own", prompt(computer="virtual", desktop_share_work=False))
        self.assertNotIn("virtual computer", prompt(computer="virtual", enable_control=False))
        self.assertEqual(prompt(computer="virtual"), virtual)     # the same every time: cached


class SetupTest(Base):
    def test_text_of_reads_both_of_wsls_encodings(self):
        self.assertEqual(vdesktop.text_of("Ubuntu\r\nLocalAgentDesktop\r\n".encode("utf-16-le")).split(),
                         ["Ubuntu", "LocalAgentDesktop"])
        self.assertEqual(vdesktop.text_of("Grüße".encode()), "Grüße")

    def test_repack_drops_the_first_run_and_sets_wsl_conf(self):
        src = self.tmp / "img.wsl"
        cap = "\x01\x00\x00\x02\x00\x14\udc80\x00"
        with tarfile.open(src, "w:gz", format=tarfile.PAX_FORMAT, encoding="utf-8", errors="surrogateescape") as t:
            def add(name, data=b"", **kw):
                info = tarfile.TarInfo(name)
                info.size = len(data)
                for k, v in kw.items():
                    setattr(info, k, v)
                t.addfile(info, io.BytesIO(data) if data else None)
            add("./etc", type=tarfile.DIRTYPE, mode=0o755)
            add("./etc/wsl.conf", b"[boot]\nsystemd=true\n", mode=0o644)
            add("./etc/wsl-distribution.conf", b"[oobe]\ncommand=/usr/lib/wsl/wsl-setup\n")
            add("./usr/bin/ping", b"\x7fELF", mode=0o755, pax_headers={"SCHILY.xattr.security.capability": cap})
            add("./usr/bin/ping4", type=tarfile.SYMTYPE, linkname="ping")
            add("./usr/bin/ping6", type=tarfile.LNKTYPE, linkname="./usr/bin/ping")
            add("./home/x", b"secret", uid=1000, gid=1000, mode=0o600)
        dst = self.tmp / "rootfs.tar"
        vdesktop.repack(src, dst)
        with tarfile.open(dst, "r:", encoding="utf-8", errors="surrogateescape") as t:
            members = {m.name.removeprefix("./"): m for m in t}
            self.assertNotIn("etc/wsl-distribution.conf", members)
            conf = t.extractfile(members["etc/wsl.conf"]).read().decode()
            self.assertIn("systemd=false", conf)
            self.assertIn("[automount]\nenabled=false", conf)
            self.assertIn("[interop]\nenabled=false", conf)
            self.assertEqual(members["usr/bin/ping"].pax_headers["SCHILY.xattr.security.capability"], cap)
            self.assertEqual(members["usr/bin/ping4"].linkname, "ping")
            self.assertTrue(members["usr/bin/ping6"].islnk())
            self.assertEqual((members["home/x"].uid, members["home/x"].mode), (1000, 0o600))
            self.assertEqual(t.extractfile(members["home/x"]).read(), b"secret")
        self.assertFalse((self.tmp / "rootfs.tar.part").exists())

    def test_repack_adds_wsl_conf_when_the_image_has_none(self):
        src = self.tmp / "img.tar.gz"
        with tarfile.open(src, "w:gz") as t:
            info = tarfile.TarInfo("bin/true")
            t.addfile(info, io.BytesIO(b""))
        vdesktop.repack(src, self.tmp / "out.tar")
        with tarfile.open(self.tmp / "out.tar") as t:
            self.assertIn("interop", t.extractfile("etc/wsl.conf").read().decode())

    def test_repack_can_be_cancelled(self):
        src = self.tmp / "img.tar.gz"
        with tarfile.open(src, "w:gz") as t:
            t.addfile(tarfile.TarInfo("a"), io.BytesIO(b""))
        stop = threading.Event()
        stop.set()
        with self.assertRaises(vdesktop.Stopped):
            vdesktop.repack(src, self.tmp / "out.tar", stop)

    def test_guest_files(self):
        files = vdesktop.guest_files()
        tools = Path(vdesktop.__file__).parent.parent / "tools"
        self.assertEqual(files["vtools/screen.py"], (tools / "screen.py").read_bytes())
        with tarfile.open(fileobj=io.BytesIO(vdesktop.guest_tar())) as t:
            m = {x.name: x for x in t}
            self.assertEqual(set(m), set(files))
            self.assertEqual(m["start.sh"].mode, 0o755)
            self.assertNotIn(b"\r", t.extractfile("setup.sh").read())

    def test_image_source_prefers_the_list_wsl_install_uses(self):
        info = {"ModernDistributions": {"Ubuntu": [
            {"Name": "Ubuntu-22.04", "Amd64Url": {"Url": "https://x/22.wsl", "Sha256": "aa"}},
            {"Name": "Ubuntu-24.04", "Amd64Url": {"Url": "https://x/24.wsl", "Sha256": "0xBB"}}]}}
        ok = types.SimpleNamespace(get=lambda *a, **k: types.SimpleNamespace(raise_for_status=lambda: None,
                                                                             json=lambda: info))
        self.assertEqual(vdesktop.image_source(ok), ("https://x/24.wsl", "bb"))

        def broken(*a, **k):
            raise OSError("offline")
        self.assertEqual(vdesktop.image_source(types.SimpleNamespace(get=broken)),
                         (vdesktop.IMAGE_URL, vdesktop.IMAGE_SHA256))

    def _session(self, body: bytes, calls: list):
        class R:
            def __init__(self, headers):
                start = int(headers.get("Range", "bytes=0-")[6:-1] or 0)
                self.status_code = 206 if start else 200
                self.data = body[start:]
                self.headers = {"content-length": str(len(self.data))}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, n):
                for i in range(0, len(self.data), 4):
                    yield self.data[i:i + 4]

        def get(url, headers=None, **k):
            calls.append((url, dict(headers or {})))
            return R(headers or {})
        return types.SimpleNamespace(get=get)

    def test_download_resumes_and_checks_the_checksum(self):
        body = b"ubuntu image bytes" * 10
        sha = hashlib.sha256(body).hexdigest()
        dest = self.tmp / "img.wsl"
        dest.with_name("img.wsl.part").write_bytes(body[:50])
        calls = []
        with mock.patch.object(vdesktop, "image_source", lambda s: ("https://x/24.wsl", sha)):
            out = vdesktop.download_image(session=self._session(body, calls), dest=dest)
        self.assertEqual(out.read_bytes(), body)
        self.assertEqual(calls[0][1], {"Range": "bytes=50-"})
        # already there and whole: nothing downloaded
        calls.clear()
        with mock.patch.object(vdesktop, "image_source", lambda s: ("https://x/24.wsl", sha)):
            vdesktop.download_image(session=self._session(body, calls), dest=dest)
        self.assertEqual(calls, [])

    def test_a_damaged_download_is_deleted(self):
        dest = self.tmp / "img.wsl"
        with mock.patch.object(vdesktop, "image_source", lambda s: ("https://x/24.wsl", "00" * 32)):
            with self.assertRaises(vdesktop.DesktopError) as cm:
                vdesktop.download_image(session=self._session(b"abc" * 9, []), dest=dest)
        self.assertIn("damaged", str(cm.exception))
        self.assertFalse(dest.exists() or dest.with_name("img.wsl.part").exists())

    def test_install_in_wsl(self):
        calls, logged, progress = [], [], []
        HOME = vdesktop.HOME

        def fake_wsl(args, data=None, timeout=0):
            calls.append((args, data is not None))
            if args[0] == "--list":
                return 0, ""
            return 0, ""

        setup_out = b"".join(line.encode() + b"\n" for line in [
            "@@step 1 7 Updating the package lists", "Get:1 http://archive.ubuntu.com noble InRelease",
            "@@step 4 7 Installing Firefox", "@@warn Firefox couldn't be installed from Mozilla.", "@@done"])
        popen = mock.MagicMock()
        popen.return_value.stdout = io.BytesIO(setup_out)
        popen.return_value.poll.return_value = 0
        popen.return_value.returncode = 0
        img = HOME / "ubuntu-24.04.wsl"

        def fake_download(progress, stop, session):
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(b"img")
            return img

        def fake_repack(src, dst, stop):
            dst.write_bytes(b"tar")

        with mock.patch.multiple(vdesktop, kind=lambda: "wsl", wsl_status=lambda: (True, OK_STATUS), _wsl=fake_wsl,
                                 wsl_exe=lambda: "wsl.exe", download_image=fake_download, repack=fake_repack), \
                mock.patch.object(vdesktop.subprocess, "Popen", popen):
            warnings = vdesktop.install(progress=lambda m, f: progress.append((m, f)), log=logged.append)
        self.assertEqual(warnings, ["Firefox couldn't be installed from Mozilla."])
        args = [c[0] for c in calls]
        self.assertEqual(args[0], ["--list", "--quiet"])
        self.assertEqual(args[1], ["--import", vdesktop.DISTRO, str(HOME / "disk"), str(HOME / "rootfs.tar"),
                                   "--version", "2"])
        self.assertEqual(args[2][:6], ["-d", vdesktop.DISTRO, "-u", "root", "--cd", "/"])
        self.assertTrue(calls[2][1])                 # the app's files go in on stdin
        self.assertEqual(args[-1], ["--terminate", vdesktop.DISTRO])
        self.assertEqual(popen.call_args[0][0][-1], f"{vdesktop.GUEST_DIR}/setup.sh")
        self.assertIn(("Installing Firefox (step 4 of 7)...", 3 / 7), progress)
        self.assertIn("Get:1 http://archive.ubuntu.com noble InRelease", logged)
        self.assertFalse((HOME / "rootfs.tar").exists() or img.exists())
        self.assertTrue((HOME / "installed.json").is_file())

    def test_a_failed_setup_says_why_and_isnt_marked_done(self):
        popen = mock.MagicMock()
        popen.return_value.stdout = io.BytesIO(b"@@step 2 7 Installing the desktop\nE: Unable to locate package x\n")
        popen.return_value.poll.return_value = 100
        popen.return_value.returncode = 100
        with mock.patch.multiple(vdesktop, kind=lambda: "wsl", wsl_status=lambda: (True, OK_STATUS),
                                 wsl_exe=lambda: "wsl.exe",
                                 _wsl=lambda args, **k: (0, vdesktop.DISTRO if args[0] == "--list" else "")), \
                mock.patch.object(vdesktop.subprocess, "Popen", popen):
            with self.assertRaises(vdesktop.DesktopError) as cm:
                vdesktop.install()
        self.assertIn("Unable to locate package x", str(cm.exception))
        self.assertFalse((vdesktop.HOME / "installed.json").exists())

    def test_without_wsl_it_says_how_to_get_it(self):
        with mock.patch.multiple(vdesktop, kind=lambda: "wsl", wsl_status=lambda: (False, "")):
            with self.assertRaises(vdesktop.DesktopError) as cm:
                vdesktop.install()
        self.assertIn("Install WSL", str(cm.exception))
        self.assertNotIsInstance(cm.exception, vdesktop.NoVirtualMachines)

    def test_no_virtual_machines_is_explained_and_the_download_kept(self):
        HOME = vdesktop.HOME
        img = HOME / "ubuntu-24.04.wsl"

        def fake_download(progress, stop, session):
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(b"img")
            return img

        def fake_wsl(args, data=None, timeout=0):
            if args[0] == "--import":
                return 4294967295, NO_VM_IMPORT
            return 1, "Windows Subsystem for Linux has no installed distributions."

        with mock.patch.multiple(vdesktop, kind=lambda: "wsl", wsl_status=lambda: (True, OK_STATUS), _wsl=fake_wsl,
                                 download_image=fake_download, repack=lambda src, dst, stop: dst.write_bytes(b"t")):
            with self.assertRaises(vdesktop.NoVirtualMachines) as cm:
                vdesktop.install()
        self.assertEqual(str(cm.exception), vdesktop.NO_VM)
        self.assertIn("restart the PC", str(cm.exception))
        self.assertIn("Turn on virtual machines", str(cm.exception))
        self.assertTrue(img.is_file())                        # no second download
        self.assertFalse((HOME / "rootfs.tar").exists())
        self.assertFalse((HOME / "installed.json").exists())

    def test_no_virtual_machines_is_noticed_before_downloading(self):
        said = ('WSL2 is not supported with your current machine configuration.\nPlease enable the "Virtual '
                'Machine Platform" optional component and ensure virtualization is enabled in the BIOS.')
        download = mock.Mock()
        with mock.patch.multiple(vdesktop, kind=lambda: "wsl", wsl_status=lambda: (True, said),
                                 download_image=download):
            with self.assertRaises(vdesktop.NoVirtualMachines):
                vdesktop.install()
        download.assert_not_called()

    def test_status_says_what_windows_lacks(self):
        def status(ready, said, names=""):
            with mock.patch.multiple(vdesktop, kind=lambda: "wsl", wsl_status=lambda: (ready, said),
                                     distros=lambda: names.split()):
                return vdesktop.status()
        info = status(False, "")
        self.assertEqual((info["needs"], info["problem"], info["wsl"]), ("wsl", vdesktop.NO_WSL, False))
        info = status(False, "Error code: Wsl/Service/CreateInstance/CreateVm/HCS/HCS_E_HYPERV_NOT_INSTALLED")
        self.assertEqual((info["needs"], info["problem"]), ("vm", vdesktop.NO_VM))
        info = status(True, 'Please enable the "Virtual Machine Platform" optional component.', vdesktop.DISTRO)
        self.assertEqual((info["needs"], info["problem"], info["wsl"]), ("vm", vdesktop.NO_VM, True))
        info = status(True, OK_STATUS)
        self.assertEqual((info["needs"], info["problem"], info["installed"]), ("", "", False))

    def test_other_wsl_failures_keep_their_own_words(self):
        err = vdesktop._failed("WSL couldn't add the virtual computer", "The file already exists.")
        self.assertNotIsInstance(err, vdesktop.NoVirtualMachines)
        self.assertEqual(str(err), "WSL couldn't add the virtual computer: The file already exists.")
        self.assertIsInstance(vdesktop._failed("x", NO_VM_IMPORT), vdesktop.NoVirtualMachines)

    def test_starting_without_virtual_machines_says_what_to_do(self):
        class NoVm(vdesktop.Desktop):
            def command(self, size, work):
                raise vdesktop.DesktopError("Couldn't copy the app's files into the virtual computer: " + NO_VM_IMPORT)
        d = NoVm()
        with self.assertRaises(vdesktop.DesktopError) as cm:
            d.start(Settings(workspace=str(self.tmp)))
        self.assertEqual(str(cm.exception), vdesktop.NO_VM)
        self.assertEqual((d.state, d.state_text), ("error", vdesktop.NO_VM))

    def test_install_wsl_keeps_its_window_open_for_the_restart_note(self):
        with mock.patch.object(vdesktop.subprocess, "Popen") as popen:
            vdesktop.install_wsl()
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[:3], ["powershell", "-NoProfile", "-Command"])
        self.assertIn("-Verb RunAs", cmd[3])
        self.assertIn("wsl.exe --install --no-distribution", cmd[3])
        self.assertIn("restart the PC", cmd[3])
        self.assertTrue(cmd[3].endswith("& pause'"))
        inner = cmd[3].split("-ArgumentList '", 1)[1][:-1]
        self.assertNotIn("'", inner)                         # it's inside a single-quoted PowerShell string
        self.assertFalse(set(inner) & set('"()<>|^%'), inner)  # nothing cmd.exe would read as syntax

    def test_the_wsl_start_command(self):
        d = vdesktop.Desktop()
        with mock.patch.multiple(vdesktop, kind=lambda: "wsl", installed=lambda: True, push_guest=lambda: None,
                                 wsl_exe=lambda: "wsl.exe"):
            args, env = d.command("1280x800", r"C:\Users\Aidan\My Work")
            self.assertEqual(args[:9], ["wsl.exe", "-d", vdesktop.DISTRO, "-u", "root", "--cd", "/", "-e", "/bin/sh"])
            self.assertEqual(args[9:12], [f"{vdesktop.GUEST_DIR}/start.sh", "--size", "1280x800"])
            self.assertEqual(base64.b64decode(args[args.index("--work-b64") + 1]).decode(), r"C:\Users\Aidan\My Work")
            self.assertEqual(env["WSL_UTF8"], "1")
            args, _ = d.command("1024x768", "")
            self.assertNotIn("--work-b64", args)
        with mock.patch.multiple(vdesktop, kind=lambda: "wsl", installed=lambda: False):
            with self.assertRaises(vdesktop.DesktopError) as cm:
                d.command("1280x800", "")
        self.assertIn("isn't set up", str(cm.exception))

    def test_the_local_command_gets_its_own_home(self):
        d = vdesktop.Desktop()
        with mock.patch.multiple(vdesktop, kind=lambda: "local", local_missing=lambda: []):
            args, env = d.command("1280x800", "/tmp/ws")
        self.assertEqual(env["HOME"], str(vdesktop.HOME / "home"))
        self.assertEqual(args[-2:], ["--work", "/tmp/ws"])
        self.assertTrue((vdesktop.HOME / "app" / "vtools" / "screen.py").is_file())

    def test_scripts_are_valid_shell(self):
        here = Path(vdesktop.__file__).parent
        sh = shutil.which("sh")
        if not sh:
            self.skipTest("no sh")
        for name in ("setup.sh", "start.sh"):
            r = subprocess.run([sh, "-n", str(here / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_start_sh_runs_the_guest_as_the_agent_user_without_windows_paths(self):
        text = (Path(vdesktop.__file__).parent / "start.sh").read_text()
        self.assertIn("runuser -u agent -- env -i", text)
        self.assertIn("umount -l /mnt/wslg", text)
        conf = (Path(vdesktop.__file__).parent / "setup.sh").read_text()
        for line in ("[automount]\nenabled=false", "[interop]\nenabled=false", "appendWindowsPath=false"):
            self.assertIn(line, conf)


def _can_run_guest() -> bool:
    if not all(shutil.which(p) for p in ("Xvfb", "openbox", "wmctrl", "xdotool")):
        return False
    try:
        import importlib.util

        import tkinter  # noqa: F401 - pyautogui won't load on Linux without it

        return all(importlib.util.find_spec(m) for m in ("mss", "PIL", "pyautogui", "Xlib"))
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(sys.platform.startswith("linux") and _can_run_guest(), "needs Xvfb, openbox and pyautogui")
class RealGuestTest(Base):
    """The real guest.py on a hidden X display of this machine (what WSL runs, minus WSL)."""

    def test_screen_tools_in_the_hidden_desktop(self):
        d = vdesktop.Desktop()
        self.addCleanup(d.stop)
        ws = self.tmp / "ws"
        s = Settings(workspace=str(ws), computer="virtual", desktop_size="1024x768")
        ctx = ToolContext(settings=s, workspace=ws)
        ctx.screen["model_name"] = "some-model"      # pixel coordinates
        with mock.patch.object(vdesktop, "_desktop", d):
            tools = {t.name: t for t in vdesktop.wrap_tools(list(REGISTRY.values()))}
            r = tools["screenshot"].func(ctx, {})
            self.assertIn("1024x768 real pixels", r.text)
            self.assertTrue(r.images[0].startswith(b"\x89PNG"))
            self.assertEqual(tools["list_windows"].func(ctx, {}).text, "No windows are open.")
            tools["open"].func(ctx, {"target": "xterm"})
            deadline = time.time() + 10
            while "xterm" not in tools["list_windows"].func(ctx, {}).text and time.time() < deadline:
                time.sleep(0.3)
            self.assertIn("* xterm", tools["list_windows"].func(ctx, {}).text)
            tools["type_text"].func(ctx, {"text": "echo made in the virtual computer > ~/Work/out.txt", "enter": True})
            deadline = time.time() + 10
            while not (ws / "out.txt").exists() and time.time() < deadline:
                time.sleep(0.2)
            self.assertEqual((ws / "out.txt").read_text().strip(), "made in the virtual computer")
            r = tools["click"].func(ctx, {"x": 100, "y": 100})
            self.assertEqual(len(r.images), 1)
            tools["set_clipboard"].func(ctx, {"text": "héllo"})
            self.assertEqual(tools["get_clipboard"].func(ctx, {}).text, "héllo")
            with self.assertRaises(ToolError):
                tools["open"].func(ctx, {"target": "no-such-program-here"})
            frame = d.peek(320)
            self.assertEqual((frame["width"], frame["height"]), (1024, 768))
            self.assertTrue(frame["jpeg"].startswith(b"\xff\xd8"))
            self.assertEqual(d.user_input({"kind": "click", "x": 50, "y": 60})["cursor"], [50, 60])


if __name__ == "__main__":
    unittest.main()
