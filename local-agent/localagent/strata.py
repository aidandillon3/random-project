"""Strata: a fast engine made for Qwen3.8-Flash-Next (github.com/Niko1221/Strata, MIT).

llama.cpp runs Flash Next with its experts in RAM at roughly 20-27 tokens/s. Strata keeps the most
used experts on the GPU, works the rest out on the CPU at the same time, and adds an MTP draft layer,
for about 50-90 tokens/s on 12-16 GB cards. It runs only Flash Next (and its fine-tunes, Swift 1.5
among them), so the 27B mode stays on llama.cpp.

This app downloads Strata into its own folder, runs Strata's setup once (which fetches its engine,
Python packages and the draft layer, and reuses the model files already downloaded here), then
starts Strata's server, which speaks the same OpenAI API the chat uses.
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


def outdated(root: Path | None = None) -> bool:
    """Installed, but not the version this app is pinned to."""
    root = root or STRATA_DIR
    try:
        have = (root / INSTALLED).read_text(encoding="utf-8").strip()
    except OSError:
        have = ""
    return installed(root) and have != COMMIT


def python(root: Path | None = None) -> Path:
    root = root or STRATA_DIR
    return root / ".venv" / ("Scripts/python.exe" if WINDOWS else "bin/python")


def config_path(preset, root: Path | None = None) -> Path:
    """setup.py writes strata-<tag>.json: the family's tag ("swift-", or "" for the original) + the size."""
    root = root or STRATA_DIR
    st = preset.strata
    return root / f"strata-{(st.get('tag', '') + st['model']).lower()}.json"


class Stopped(Exception):
    pass


def download(progress=lambda msg, frac: None, root: Path | None = None, url: str = ZIP_URL, session=None,
             stop=None) -> Path:
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
    for item in inner.iterdir():            # keeps .venv, the engine and configs of an earlier install
        dest = root / item.name
        if dest.is_dir():
            shutil.rmtree(dest)
        elif dest.exists():
            dest.unlink()
        shutil.move(str(item), str(dest))
    shutil.rmtree(tmp, ignore_errors=True)
    if not WINDOWS:
        for sh in root.glob("*.sh"):
            sh.chmod(0o755)
    (root / INSTALLED).write_text(COMMIT, encoding="utf-8")
    progress("Strata downloaded.", 1.0)
    return root


def data_dir(model: Path) -> Path:
    """Strata's draft layer and engine data: next to the models folder, on the same big drive."""
    return model.parent.parent / "Strata-data"


def wanted(preset, model: Path, settings) -> dict:
    """What setup is run with; when it changes, setup runs again (the port is given at each start). A new Strata
    version too: its setup fetches the engine that goes with it."""
    return {"family": preset.strata["family"], "model": preset.strata["model"], "gguf_dir": str(model.parent),
            "context": int(settings.ctx_size), "vision": bool(settings.use_vision), "strata": COMMIT}


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


def needs_setup(preset, model: Path, settings, root: Path | None = None) -> bool:
    root = root or STRATA_DIR
    if not (config_path(preset, root).is_file() and python(root).is_file()):
        return True
    if _files_gone(config_path(preset, root)):
        return True
    w = wanted(preset, model, settings)
    return _stamps(root).get(f"{w['family']}:{w['model']}") != w


def setup_args(preset, model: Path, settings, root: Path | None = None) -> list[str]:
    """Strata's own installer, without questions. It uses the model files where they are (--gguf-dir)."""
    root = root or STRATA_DIR
    w = wanted(preset, model, settings)
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
                     "--port", str(settings.port)]


def mark_setup_done(preset, model: Path, settings, root: Path | None = None) -> None:
    root = root or STRATA_DIR
    w = wanted(preset, model, settings)
    stamps = {**_stamps(root), f"{w['family']}:{w['model']}": w}
    (root / STAMP).write_text(json.dumps(stamps), encoding="utf-8")


# Engine options setup doesn't write (and drops again on each setup run), put back into the config's "args":
# keep up to 10 prompt checkpoints, and park conversations of 8K tokens or more (a helper's task in between
# doesn't throw the main chat out) in 4 GiB of RAM. Not 8 GiB: IQ2_XS keeps about 48 GB of the 64 GB in RAM.
# A mode's strata["engine_args"] changes them for its size (the 3-bit sizes park less).
# --adapt-async 1: the expert cache swaps on a helper thread instead of pausing the answer (Strata's README, #1904,
# v0.1.44+: +4-5% decode, +7-10% on code and long documents on an RTX 5070; answers can differ slightly from the
# default but stay the same from run to run). The engine turns it off by itself, saying why, where it can't run.
ENGINE_ARGS = {"--prompt-cache": "10", "--conversation-cache-mib": "4096", "--conversation-cache-min-tokens": "8192",
               "--adapt-async": "1"}
# Put into the config's "env" the same way. STRATA_PIN_BY_NEED: Windows lets a program lock only about half the RAM
# for the graphics card; this locks the experts the card needs most first (#1905, v0.1.44+: +4.4% prompt reading).
ENGINE_ENV = {"STRATA_PIN_BY_NEED": "1"}


def tune_config(preset, root: Path | None = None) -> bool:
    """Add ENGINE_ARGS (with the mode's own values) to strata-<model>.json where it doesn't have them (it keeps
    values set by hand). True when the file changed."""
    path = config_path(preset, root or STRATA_DIR)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    args = cfg.get("args")
    if not isinstance(args, list):
        return False
    want = {**ENGINE_ARGS, **preset.strata.get("engine_args", {})}
    add = [x for flag, value in want.items() if flag not in args for x in (flag, value)]
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    add_env = {k: v for k, v in ENGINE_ENV.items() if k not in env}
    if not add and not add_env:
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


def serve_args(preset, settings, root: Path | None = None) -> list[str]:
    """What Strata's run-<model> script runs, minus opening the browser. --fit-max-tokens: a request whose prompt
    plus max_tokens is longer than the context gets a shorter answer instead of an error. Also makes sure the
    config has ENGINE_ARGS (setup rewrites them away)."""
    root = root or STRATA_DIR
    tune_config(preset, root)
    return [str(python(root)), str(root / "serve" / "server.py"), "--engine", "strata",
            "--config", str(config_path(preset, root)), "--port", str(settings.port), "--fit-max-tokens"]
