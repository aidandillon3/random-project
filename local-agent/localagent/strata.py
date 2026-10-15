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
# Pinned to the version this app was checked against (2026-10-11); its setup fetches Strata's
# ready-made engine and the draft layer itself.
COMMIT = "7695859aa13afb248bdb4d36f9b2e95707db079d"
ZIP_URL = f"https://github.com/Niko1221/Strata/archive/{COMMIT}.zip"
STAMP = "localagent-setup.json"


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
    progress("Strata downloaded.", 1.0)
    return root


def data_dir(model: Path) -> Path:
    """Strata's draft layer and engine data: next to the models folder, on the same big drive."""
    return model.parent.parent / "Strata-data"


def wanted(preset, model: Path, settings) -> dict:
    """What setup is run with; when it changes, setup runs again (the port is given at each start)."""
    return {"family": preset.strata["family"], "model": preset.strata["model"], "gguf_dir": str(model.parent),
            "context": int(settings.ctx_size), "vision": bool(settings.use_vision)}


def needs_setup(preset, model: Path, settings, root: Path | None = None) -> bool:
    root = root or STRATA_DIR
    if not (config_path(preset, root).is_file() and python(root).is_file()):
        return True
    try:
        return json.loads((root / STAMP).read_text(encoding="utf-8")) != wanted(preset, model, settings)
    except (OSError, ValueError):
        return True


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
    (root / STAMP).write_text(json.dumps(wanted(preset, model, settings)), encoding="utf-8")


# Engine options setup doesn't write (and drops again on each setup run), put back into the config's "args":
# keep up to 10 prompt checkpoints, and park conversations of 8K tokens or more (a helper's task in between
# doesn't throw the main chat out) in 4 GiB of RAM. Not 8 GiB: IQ2_XS keeps about 48 GB of the 64 GB in RAM.
ENGINE_ARGS = {"--prompt-cache": "10", "--conversation-cache-mib": "4096", "--conversation-cache-min-tokens": "8192"}


def tune_config(preset, root: Path | None = None) -> bool:
    """Add ENGINE_ARGS to strata-<model>.json where it doesn't have them (it keeps values set by hand). True
    when the file changed."""
    path = config_path(preset, root or STRATA_DIR)
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    args = cfg.get("args")
    if not isinstance(args, list):
        return False
    add = [x for flag, value in ENGINE_ARGS.items() if flag not in args for x in (flag, value)]
    if not add:
        return False
    cfg["args"] = args + add
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
