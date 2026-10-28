"""Strata: a fast engine made for Qwen3.8-Flash-Next (github.com/Niko1221/Strata, MIT).

llama.cpp runs Flash Next with its experts in RAM at roughly 20-27 tokens/s. Strata keeps the most
used experts on the GPU, works the rest out on the CPU at the same time, and adds an MTP draft layer,
for about 50-90 tokens/s on 12-16 GB cards. It runs only Flash Next (and its fine-tunes, Swift 1.5
among them), so the 27B mode stays on llama.cpp.

This app downloads Strata into its own folder, runs Strata's setup once (which fetches its engine,
Python packages and the draft layer, and reuses the model files already downloaded here), then
starts Strata's server, which speaks the same OpenAI API the chat uses.

Qwen3.6-35B-A3B runs on a fork instead (Q36: xyehya/Strata, branch q36-r2, ykarout's speed work for that model),
installed in a folder of its own so Flash Next keeps the version it was checked with. The fork has no Windows engine,
so this repo's GitHub Actions builds one (.github/workflows/strata-q36-windows.yml) into a pre-release, which is put
in its engine folder before its setup runs: setup then keeps it as a ready-made engine.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import zipfile
from pathlib import Path

import requests

from . import odp
from .config import APP_DIR

WINDOWS = sys.platform == "win32"
STRATA_DIR = APP_DIR / "strata"
# Pinned to the version this app was checked against: v0.1.45 (2026-10-16; was 7695859, v0.1.43, until
# 2026-10-20). Its setup fetches the engine of the same version and the draft layer itself. An install of another
# version is updated in place on the next start (download keeps .venv, the engine and the configs) and set up again.
COMMIT = "477ac13cbe8e582c20484871ce459d20d82df09d"
VERSION = "0.1.45"
ZIP_URL = f"https://github.com/Niko1221/Strata/archive/{COMMIT}.zip"
STAMP = "localagent-setup.json"
INSTALLED = "localagent-commit.txt"     # the commit this app downloaded (installs before 2026-10-20 have none)
SOURCE_FILES = "localagent-files.txt"   # Strata's own files, so an update can tell them from setup's


class Install:
    """A Strata folder: which repo and commit it's downloaded from, and the engine build it takes, if any."""

    def __init__(self, key: str, root: Path | None, repo: str, commit: str, version: str, engine: dict | None = None):
        self.key, self._root, self.repo, self.commit, self.version, self.engine = key, root, repo, commit, version, engine

    @property
    def root(self) -> Path:
        return self._root or STRATA_DIR       # (the main one: STRATA_DIR as it is now)

    @property
    def zip_url(self) -> str:
        return f"https://github.com/{self.repo}/archive/{self.commit}.zip"


MAIN = Install("", None, "Niko1221/Strata", COMMIT, f"v{VERSION}")
# The fork's q36-r2 branch at the commit ykarout's numbers were measured on (2026-10-23). Its engine: the newest build
# of the workflow's pre-releases, of build Q36_BUILD or newer (the workflow's BUILD), for this commit.
Q36_BUILD = 1
Q36 = Install("q36", APP_DIR / "strata-q36", "xyehya/Strata", "e073a5200be56f911aa038ada8b4cb8f80285647",
              "q36-r2 e073a52",
              engine={"tag_prefix": "strata-q36-", "asset": "localagent-strata-q36-win-cuda-13.4-x64.zip",
                      "build": Q36_BUILD, "workflow": "Strata Qwen3.6 engine for Windows"})
INSTALLS = {MAIN.key: MAIN, Q36.key: Q36}
ENGINE_EXE = "strata.exe" if WINDOWS else "strata"


def install_of(preset) -> Install:
    """The Strata folder that runs this mode."""
    return INSTALLS.get((getattr(preset, "strata", None) or {}).get("install", ""), MAIN)


def supports(preset) -> bool:
    return bool(preset and getattr(preset, "strata", None))


def active(settings, preset) -> bool:
    """Strata runs this model when it's one Strata can run and Strata is picked (or, on "auto",
    recommended for it)."""
    if not supports(preset):
        return False
    return settings.engine == "strata" or (settings.engine == "auto" and preset.launch.get("engine") == "strata")


def installed(root: Path | None = None) -> bool:
    root = root or STRATA_DIR
    return (root / "setup.py").is_file() and (root / "serve" / "server.py").is_file()


def outdated(root: Path | None = None, commit: str = COMMIT) -> bool:
    """Installed, but not the version this app is pinned to."""
    root = root or STRATA_DIR
    try:
        have = (root / INSTALLED).read_text(encoding="utf-8").strip()
    except OSError:
        have = ""
    return installed(root) and have != commit


def python(root: Path | None = None) -> Path:
    root = root or STRATA_DIR
    return root / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def config_path(preset, root: Path | None = None) -> Path:
    """setup.py writes strata-<tag>.json: the family's tag ("swift-", or "" for the original) + the size."""
    root = root or install_of(preset).root
    st = preset.strata
    return root / f"strata-{(st.get('tag', '') + st['model']).lower()}.json"


class Stopped(Exception):
    pass


def _source_files(root: Path) -> list[str]:
    """The files the last download put in root (not what setup made there since)."""
    try:
        return [ln for ln in (root / SOURCE_FILES).read_text(encoding="utf-8").splitlines()
                if ln and ".." not in Path(ln).parts and not Path(ln).is_absolute()]
    except OSError:
        return []


def download(progress=lambda msg, frac: None, root: Path | None = None, url: str = ZIP_URL, session=None,
             stop=None, commit: str = COMMIT) -> Path:
    """Fetch Strata's source (about 55 MB) into root. Its setup fetches the rest. stop: an Event that ends
    it early (Stopped), leaving the folder as it was."""
    root = root or STRATA_DIR
    session = session or requests.Session()
    progress("Downloading Strata from GitHub...", 0.0)
    with session.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or 0)
        buf = io.BytesIO()
        for chunk in r.iter_content(1 << 20):
            if stop is not None and stop.is_set():
                raise Stopped()
            buf.write(chunk)
            progress(f"Downloading Strata ({buf.tell() / 1e6:.0f} MB)...", buf.tell() / total if total else 0.0)
    tmp = root.with_name(root.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(buf) as z:
        z.extractall(tmp)
    inner = next((d for d in tmp.iterdir() if d.is_dir()), None)    # Strata-<commit>/
    if not inner or not (inner / "setup.py").is_file():
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError("Downloaded Strata, but its setup.py wasn't in the zip.")
    root.mkdir(parents=True, exist_ok=True)
    # Like Strata's own update (a git pull): its files are replaced and the ones a newer version dropped go, while
    # what setup made or downloaded stays (.venv, the engine, third_party's llama.cpp, configs).
    files = sorted(f.relative_to(inner).as_posix() for f in inner.rglob("*") if f.is_file())
    for rel in set(_source_files(root)) - set(files):
        (root / rel).unlink(missing_ok=True)
    for rel in files:
        dest = root / rel
        if dest.is_dir():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(inner / rel), str(dest))
    (root / SOURCE_FILES).write_text("\n".join(files), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    if not WINDOWS:
        for sh in root.glob("*.sh"):
            sh.chmod(0o755)
    (root / INSTALLED).write_text(commit, encoding="utf-8")
    progress("Strata downloaded.", 1.0)
    return root


def download_install(ins: Install, progress=lambda msg, frac: None, session=None, stop=None) -> Path:
    return download(progress, ins.root, ins.zip_url, session, stop, ins.commit)


def engine_ready(ins: Install) -> bool:
    """The install needs no engine of ours (its setup fetches one), or ours is in its engine folder: a build of the
    install's commit, not older than it takes."""
    if not ins.engine:
        return True
    eng = ins.root / "engine"
    try:
        meta = json.loads((eng / "BUILD.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return ((eng / ENGINE_EXE).is_file() and isinstance(meta, dict) and meta.get("commit") == ins.commit
            and int(meta.get("build") or 0) >= ins.engine["build"])


def pick_engine_release(ins: Install, releases: list) -> tuple[dict | None, dict | None]:
    """The newest finished pre-release with the install's engine: (release, its zip)."""
    e = ins.engine
    want = f"{e['tag_prefix']}b"

    def build(tag: str) -> int:
        rest = tag[len(want):] if tag.startswith(want) else ""
        num = rest.split("-", 1)[0]
        return int(num) if num.isdigit() else 0

    ordered = sorted((r for r in releases if isinstance(r, dict) and not r.get("draft")
                      and build(str(r.get("tag_name", ""))) >= e["build"]
                      and f"-{ins.commit[:7]}-" in str(r.get("tag_name", ""))),
                     key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
    for rel in ordered:
        asset = next((a for a in rel.get("assets", []) if a.get("name") == e["asset"]
                      and a.get("state", "uploaded") == "uploaded"), None)
        if asset:
            return rel, asset
    return None, None


def download_engine(ins: Install, settings, progress=lambda msg, frac: None, session=None, stop=None,
                    api: str = odp.API) -> Path:
    """Put the install's engine (built in this repo's GitHub Actions) into its engine folder, replacing what is
    there. The repo is private, so it needs the GitHub token from Settings (or the gh CLI's login)."""
    session = session or requests.Session()
    tok = odp.github_token(settings)
    if not tok:
        raise RuntimeError("This mode's engine is built in your private GitHub repo, so downloading it needs a GitHub "
                           "token: put one in Settings > GitHub (a fine-grained token with read access to "
                           f"{odp.REPO}'s contents is enough).")
    auth = {"Authorization": f"Bearer {tok}", "X-GitHub-Api-Version": "2022-11-28"}
    progress("Asking GitHub for the Strata engine for Qwen3.6...", 0.0)
    r = session.get(f"{api}/repos/{odp.REPO}/releases?per_page=50", timeout=30,
                    headers={**auth, "Accept": "application/vnd.github+json"})
    data = r.json() if r.content else {}
    if r.status_code in (401, 403, 404) or not isinstance(data, list):
        msg = data.get("message", r.status_code) if isinstance(data, dict) else r.status_code
        raise RuntimeError(f"GitHub refused to list {odp.REPO}'s releases ({msg}). Check the token in Settings > "
                           "GitHub: it needs read access to that repo's contents.")
    rel, asset = pick_engine_release(ins, data)
    if not asset:
        raise RuntimeError(f"No build of this mode's engine in {odp.REPO}'s releases yet. It's made by the "
                           f"'{ins.engine['workflow']}' job under the repo's Actions tab.")
    progress(f"Downloading {asset['name']}...", 0.0)
    buf = io.BytesIO()
    with session.get(asset["url"], stream=True, timeout=60,
                     headers={**auth, "Accept": "application/octet-stream"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or asset.get("size") or 0)
        for chunk in r.iter_content(1 << 20):
            if stop is not None and stop.is_set():
                raise Stopped()
            buf.write(chunk)
            if total:
                progress(f"Downloading {asset['name']} ({buf.tell() / 1e6:.0f} MB)...", buf.tell() / total)
    eng = ins.root / "engine"
    tmp = ins.root / "engine.new"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(buf) as z:
        z.extractall(tmp)
    old = ins.root / "engine.old"
    shutil.rmtree(old, ignore_errors=True)
    if eng.exists():
        try:
            eng.rename(old)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"Couldn't replace {eng} ({e}). Stop the model and try again.")
    tmp.rename(eng)
    shutil.rmtree(old, ignore_errors=True)
    if not engine_ready(ins):
        raise RuntimeError(f"Downloaded {asset['name']}, but it isn't a build of this mode's engine "
                           f"(no {ENGINE_EXE}, or not for {ins.commit[:7]}).")
    progress(f"Strata engine {rel.get('tag_name', '')} is ready.", 1.0)
    return eng / ENGINE_EXE


def data_dir(model: Path) -> Path:
    """Strata's draft layer and engine data: next to the models folder, on the same big drive."""
    return model.parent.parent / "Strata-data"


def wanted(preset, model: Path, settings, gpus: list[int] | None = None) -> dict:
    """What setup is run with; when it changes, setup runs again (the port is given at each start). A new Strata
    version too: its setup fetches the engine that goes with it. gpus: on a PC with two or more NVIDIA cards, the
    ones picked under "Run the AI on" (nvidia-smi's numbers, the main one first). Strata's setup saves its card in
    the config and the engine runs there whatever the environment says, so a new pick sets it up again (and so
    does a second card put in since: nvidia-smi's numbers can move)."""
    w = {"family": preset.strata["family"], "model": preset.strata["model"], "gguf_dir": str(model.parent),
         "context": int(settings.ctx_size), "vision": bool(settings.use_vision), "strata": install_of(preset).commit}
    if gpus:
        w["gpus"] = list(gpus)
    return w


def _stamps(root: Path) -> dict:
    """What setup last ran with, per size ("swift:IQ3_S"), so switching sizes doesn't set the other one up again.
    Older versions kept one setup's choices only."""
    try:
        got = json.loads((root / STAMP).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(got, dict):
        return {}
    if "family" in got:
        return {f"{got['family']}:{got.get('model')}": got}
    return got


def set_up_before(root: Path | None = None) -> bool:
    """Setup finished for some size here, so its engine and draft layer are on the PC."""
    return bool(_stamps(root or STRATA_DIR))


def _files_gone(config: Path) -> bool:
    """The size's prepared files moved or were deleted since setup (Strata's server refuses to start: "the model's
    tokenizer is missing; run setup again"). Setup fetches them again."""
    try:
        tok = json.loads(config.read_text(encoding="utf-8-sig")).get("tokenizer")
    except (OSError, ValueError, AttributeError):
        return False
    return bool(tok) and not (Path(tok) / "vocab.json").is_file()


def needs_setup(preset, model: Path, settings, root: Path | None = None, gpus: list[int] | None = None) -> bool:
    root = root or install_of(preset).root
    if not (config_path(preset, root).is_file() and python(root).is_file()):
        return True
    if _files_gone(config_path(preset, root)):
        return True
    w = wanted(preset, model, settings, gpus)
    return _stamps(root).get(f"{w['family']}:{w['model']}") != w


def setup_args(preset, model: Path, settings, root: Path | None = None, gpus: list[int] | None = None) -> list[str]:
    """Strata's own installer, without questions. It uses the model files where they are (--gguf-dir). With
    gpus, the card(s) to use (--gpu N, or --gpus for a model shared across several); without, setup picks."""
    root = root or install_of(preset).root
    w = wanted(preset, model, settings, gpus)
    if not WINDOWS:
        launch = ["sh", str(root / "setup.sh")]
    elif python(root).is_file():
        # what START-HERE.bat runs once its .venv exists, without cmd.exe in between
        launch = [str(python(root)), str(root / "setup.py")]
    else:
        # The first time, the .bat installs Python and makes the .venv. "cmd /c" removes the first and the last
        # quote of a line that starts with one and has more than two, which breaks paths with spaces
        # (C:\Users\Aidan Dillon\...); a line starting with "call" keeps its quotes. /d: no AutoRun commands.
        launch = ["cmd", "/d", "/c", "call", str(root / "START-HERE.bat")]
    return launch + ["--yes", "--no-start", "--no-browser", "--family", w["family"], "--model", w["model"],
                     "--gguf-dir", w["gguf_dir"], "--data-dir", str(data_dir(model)),
                     "--context", str(w["context"]), "--vision", "gpu" if w["vision"] else "no",
                     "--port", str(settings.port)] + _gpu_args(gpus)


def _gpu_args(gpus: list[int] | None) -> list[str]:
    if not gpus:
        return []
    return ["--gpu", str(gpus[0])] if len(gpus) == 1 else ["--gpus", ",".join(str(i) for i in gpus)]


def mark_downloaded(preset, model: Path) -> list[Path]:
    """A fork's setup (strata["install"]) takes a --gguf-dir file only with its <file>.done finish mark, which its
    own download writes: without one it calls the app's finished download missing. Mark each model file that is
    all there. The mark has no SHA-256 in it, so setup still checks the file against the pinned one once (and writes
    that into the mark). The marks written."""
    if not preset.strata.get("install"):
        return []
    marked = []
    for name in preset.files:
        f = model.parent / name
        m = f.with_name(f.name + ".done")
        if not m.exists() and f.is_file() and f.stat().st_size == preset.sizes.get(name):
            m.write_text("downloaded by Local Agent", encoding="utf-8")
            marked.append(m)
    return marked


def mark_setup_done(preset, model: Path, settings, root: Path | None = None, gpus: list[int] | None = None) -> None:
    root = root or install_of(preset).root
    w = wanted(preset, model, settings, gpus)
    stamps = {**_stamps(root), f"{w['family']}:{w['model']}": w}
    (root / STAMP).write_text(json.dumps(stamps), encoding="utf-8")


# Engine options setup doesn't write (and drops again on each setup run), put back into the config's "args":
# keep up to 10 prompt checkpoints, and park conversations of 8K tokens or more (a helper's task in between
# doesn't throw the main chat out) in 4 GiB of RAM. Not 8 GiB: IQ2_XS keeps about 48 GB of the 64 GB in RAM.
# A mode's strata["engine_args"] changes them for its size (the 3-bit sizes park less).
# --adapt-async 1: the expert cache swaps on a helper thread instead of pausing the answer (Strata's README, #1904,
# v0.1.44+: +4-5% decode, +7-10% on code and long documents on an RTX 5070; answers can differ slightly from the
# default but stay the same from run to run). The engine turns it off by itself, saying why, where it can't run.
# --vram-reserve-mib 1728: VRAM the expert cache leaves free. With the desktop on the same card (Aidan's four
# monitors take about 3 GB) the cache otherwise fills it to 0 MiB free, Windows pages GPU memory to system RAM and
# decode falls (IQ3_XXS at 64K: about 50 tok/s, 85-117 with it). 1728 is what the engine's own warning asked for.
# Without the desktop on the card, the engine's own 700 is enough.
ENGINE_ARGS = {"--prompt-cache": "10", "--conversation-cache-mib": "4096", "--conversation-cache-min-tokens": "8192",
               "--adapt-async": "1", "--vram-reserve-mib": "1728"}
NO_MONITORS_RESERVE = "700"
# A fork's expert_vram (below) is set with the desktop on the card (about 3 GB); without it, this much more.
NO_MONITORS_EXPERT_VRAM = 1.5
# The options that follow the "monitors on the AI's GPU" switch: set again at each start, not kept as they were.
MONITOR_FLAGS = ("--vram-reserve-mib", "--expert-vram")
# Put into the config's "env" the same way. STRATA_PIN_BY_NEED: Windows lets a program lock only about half the RAM
# for the graphics card; this locks the experts the card needs most first (#1905, v0.1.44+: +4.4% prompt reading).
ENGINE_ENV = {"STRATA_PIN_BY_NEED": "1"}


def tune_config(preset, root: Path | None = None, model_dir: Path | None = None, monitors: bool = True) -> bool:
    """Add ENGINE_ARGS (with the mode's own values) to strata-<model>.json where it doesn't have them (it keeps
    values set by hand, except MONITOR_FLAGS'). True when the file changed. A fork's mode (strata["install"]) takes
    only its own options: the main path's are not options of its engine, which then refuses to start. An option
    without a value (None) is a switch; a file name value (the expert profile) is the file in the model's folder.
    monitors: the desktop is on the card the model uses, so MONITOR_FLAGS leave VRAM free for it (without, the
    cache gets that VRAM)."""
    path = config_path(preset, root or install_of(preset).root)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    args = cfg.get("args")
    if not isinstance(args, list):
        return False
    own = bool(preset.strata.get("install"))
    want = {**({} if own else ENGINE_ARGS), **preset.strata.get("engine_args", {})}
    if not monitors and "--vram-reserve-mib" in want:
        want["--vram-reserve-mib"] = NO_MONITORS_RESERVE
    # a fork's VRAM budget for the experts (strata["expert_vram"]: GiB at 64K context, GiB less per 32K more): its
    # engine fills the card to 0 free otherwise, with the same paging as above (Qwen3.6: 50-73 tok/s, 105-148)
    ev = preset.strata.get("expert_vram")
    if ev:
        try:
            ctx = int(args[args.index("--max-context") + 1])
        except (ValueError, IndexError):
            ctx = 65536
        gib = max(ev[0] - ev[1] * max(ctx - 65536, 0) / 32768, 1.0) + (0 if monitors else NO_MONITORS_EXPERT_VRAM)
        want["--expert-vram"] = f"{gib:.1f}"
    args = list(args)
    changed = False
    for flag in MONITOR_FLAGS:          # already there with the other choice's value
        if flag in want and flag in args and args.index(flag) + 1 < len(args) \
                and args[args.index(flag) + 1] != want[flag]:
            args[args.index(flag) + 1] = want[flag]
            changed = True
    add = []
    for flag, value in want.items():
        if flag in args:
            continue
        if value is None:
            add.append(flag)
            continue
        if model_dir is not None and flag == "--expert-profile":
            value = str(Path(model_dir) / value)
        add += [flag, value]
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    add_env = {k: v for k, v in {**({} if own else ENGINE_ENV), **preset.strata.get("env", {})}.items()
               if k not in env}
    if not add and not add_env and not changed:
        return False
    cfg["args"] = args + add
    if add_env:
        cfg["env"] = {**env, **add_env}
    tmp = path.with_name(path.name + ".tmp")          # whole or not at all, as Strata's setup writes it
    try:
        tmp.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False                                  # it runs without them, just with less caching
    return True


def serve_args(preset, settings, root: Path | None = None, monitors: bool = True) -> list[str]:
    """What Strata's run-<model> script runs, minus opening the browser. --fit-max-tokens: a request whose prompt
    plus max_tokens is longer than the context gets a shorter answer instead of an error. Also makes sure the
    config has ENGINE_ARGS (setup rewrites them away)."""
    root = root or install_of(preset).root
    model = getattr(settings, "model_path", "")
    tune_config(preset, root, Path(model).parent if model else None, monitors)
    return [str(python(root)), str(root / "serve" / "server.py"), "--engine", "strata",
            "--config", str(config_path(preset, root)), "--port", str(settings.port), "--fit-max-tokens"]
