"""The 1-bit 27B mode: IST-DASLab's disaggregated llama.cpp (ODP, "offloaded disaggregated prefill").

A 1-bit Qwen3.8-27B writes the answer from the GPU, while a 4-bit (NVFP4) copy of its layers, trained to
go with it, reads the prompt: it is streamed off the SSD one layer at a time, so it costs no VRAM. By the
paper's numbers that wins back much of what 1 bit loses (+32.5 points on MMLU-Pro), but at 3 bits it no
longer helps. Research code: tested by its makers on Linux with one GPU type.

The fork has no Windows builds, so this repo's GitHub Actions builds one (.github/workflows/
odp-llama-windows.yml, with local-agent/odp/odp-windows.patch) and puts it in a pre-release here. The
repo is private, so the download needs the GitHub token from Settings (or the gh CLI's login).
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import requests

from .config import APP_DIR

WINDOWS = sys.platform == "win32"
EXE = "llama-server.exe" if WINDOWS else "llama-server"
ODP_DIR = APP_DIR / "odp-llama"
REPO = "aidandillon3/random-project"
API = "https://api.github.com"
TAG_PREFIX = "odp-llama-"
ASSET = "localagent-odp-llama-win-cuda-13.4-x64.zip"
CUDA13_DRIVER = 580     # the build uses CUDA 13
# the oldest build that works: build 1 left out the DLLs it links (OpenSSL), so Windows wouldn't start it.
# Matches BUILD in .github/workflows/odp-llama-windows.yml
BUILD = 2


def supports(preset) -> bool:
    return bool(preset and getattr(preset, "odp", None))


def installed_build(root: Path) -> int:
    try:
        return int(json.loads((root / "BUILD.json").read_text(encoding="utf-8-sig")).get("build", 1))
    except (OSError, ValueError, TypeError, AttributeError):
        return 1


def find_server(root: Path | None = None) -> Path | None:
    """The engine, unless it's missing or an older build that doesn't work (then Start downloads it again)."""
    root = root or ODP_DIR
    hit = root / EXE
    return hit if hit.is_file() and installed_build(root) >= BUILD else None


def outdated(root: Path | None = None) -> bool:
    root = root or ODP_DIR
    return (root / EXE).is_file() and installed_build(root) < BUILD


def _tag_build(tag: str) -> int:
    m = re.match(rf"{TAG_PREFIX}b(\d+)-", tag)
    return int(m.group(1)) if m else 1


def blocks_path(preset, model: Path) -> Path:
    """The first of the prefill blocks, next to the decoder. A file, not the folder: the folder also holds the
    decoder, and the first part names the variant exactly (docs/ODP.md)."""
    return model.parent / preset.odp["blocks"]


def github_token(settings) -> str:
    tok = (getattr(settings, "github_token", "") or os.environ.get("GITHUB_TOKEN")
           or os.environ.get("GH_TOKEN") or "").strip()
    if tok:
        return tok
    gh = shutil.which("gh")
    if gh:
        try:
            out = subprocess.run([gh, "auth", "token"], capture_output=True, text=True, timeout=15,
                                 creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
            if out.returncode == 0:
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return ""


def pick_release(releases: list) -> tuple[dict | None, dict | None]:
    """The newest finished build that works: (release, its zip)."""
    ordered = sorted((r for r in releases if isinstance(r, dict) and not r.get("draft")
                      and str(r.get("tag_name", "")).startswith(TAG_PREFIX)
                      and _tag_build(str(r["tag_name"])) >= BUILD),
                     key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
    for rel in ordered:
        asset = next((a for a in rel.get("assets", []) if a.get("name") == ASSET
                      and a.get("state", "uploaded") == "uploaded"), None)
        if asset:
            return rel, asset
    return None, None


def download(settings, progress=lambda msg, frac: None, root: Path | None = None, session=None,
             api: str = API) -> Path:
    """Fetch the newest build from this repo's releases into root (replacing an older one) and return its
    llama-server."""
    root = root or ODP_DIR
    session = session or requests.Session()
    tok = github_token(settings)
    if not tok:
        raise RuntimeError("The 1-bit engine is built in your private GitHub repo, so downloading it needs a "
                           "GitHub token: put one in Settings > GitHub (a fine-grained token with read access "
                           f"to {REPO}'s contents is enough).")
    auth = {"Authorization": f"Bearer {tok}", "X-GitHub-Api-Version": "2022-11-28"}
    progress("Asking GitHub for the 1-bit engine...", 0.0)
    r = session.get(f"{api}/repos/{REPO}/releases?per_page=30", timeout=30,
                    headers={**auth, "Accept": "application/vnd.github+json"})
    data = r.json() if r.content else {}
    if r.status_code in (401, 403, 404) or not isinstance(data, list):
        msg = data.get("message", r.status_code) if isinstance(data, dict) else r.status_code
        raise RuntimeError(f"GitHub refused to list {REPO}'s releases ({msg}). Check the token in Settings > "
                           "GitHub: it needs read access to that repo's contents.")
    rel, asset = pick_release(data)
    if not asset:
        raise RuntimeError(f"No working build of the 1-bit engine (build {BUILD} or newer) in {REPO}'s releases "
                           "yet. It's made by the 'ODP llama.cpp for Windows' job under the repo's Actions tab.")
    # the asset's API url with Accept: octet-stream redirects to the file; requests drops the token there
    progress(f"Downloading {asset['name']}...", 0.0)
    buf = io.BytesIO()
    with session.get(asset["url"], stream=True, timeout=60,
                     headers={**auth, "Accept": "application/octet-stream"}) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or asset.get("size") or 0)
        for chunk in r.iter_content(1 << 20):
            buf.write(chunk)
            if total:
                progress(f"Downloading {asset['name']} ({buf.tell() / 1e6:.0f} MB)...", buf.tell() / total)
    progress("Unpacking...", 1.0)
    tmp = root.with_name(root.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with zipfile.ZipFile(buf) as z:
        z.extractall(tmp)
    if not (tmp / EXE).is_file() or installed_build(tmp) < BUILD:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"Downloaded {asset['name']}, but it isn't a working build of the 1-bit engine "
                           f"(no {EXE}, or older than build {BUILD}).")
    old = root.with_name(root.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    if root.exists():
        try:
            root.rename(old)
        except OSError as e:
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError(f"Couldn't replace {root} ({e}). Stop the model and try again.")
    tmp.rename(root)
    shutil.rmtree(old, ignore_errors=True)
    progress(f"1-bit engine {rel.get('tag_name', '')} is ready.", 1.0)
    return root / EXE
