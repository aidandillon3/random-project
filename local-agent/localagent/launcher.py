"""Finding, downloading and starting llama.cpp's llama-server with fast settings for this PC."""

from __future__ import annotations

import collections
import io
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import requests

from . import vision
from .config import APP_DIR
from .gguf import GGUFError, GiB, ModelInfo, Plan, driver_version, gguf_files, gpu_info, model_info, plan, \
    projection_dim, read_meta

WINDOWS = sys.platform == "win32"
EXE = "llama-server.exe" if WINDOWS else "llama-server"
LLAMA_DIR = APP_DIR / "llama.cpp"


# ---------------------------------------------------------------------- finding things
def find_server(settings) -> Path | None:
    if settings.server_exe and Path(settings.server_exe).is_file():
        return Path(settings.server_exe)
    if LLAMA_DIR.exists():
        # not a download being unpacked or replaced (b1234.tmp/.old); a CUDA build without its CUDA runtime only
        # as a last resort
        hits = [p for p in LLAMA_DIR.rglob(EXE)
                if not any(d.endswith((".tmp", ".old")) for d in p.relative_to(LLAMA_DIR).parts)]
        hits.sort(key=lambda p: (_complete_build(p), p.stat().st_mtime), reverse=True)
        if hits:
            return hits[0]
    found = shutil.which("llama-server")
    return Path(found) if found else None


def _complete_build(exe: Path) -> bool:
    """A CUDA build (ggml-cuda.dll) needs the CUDA runtime DLLs next to it, else it quietly runs on the CPU."""
    folder = exe.parent
    return not (folder / "ggml-cuda.dll").exists() or any(folder.glob("cudart64_*.dll"))


def default_model_dirs() -> list[Path]:
    home = Path.home()
    dirs = [APP_DIR / "models", home / ".lmstudio" / "models", home / ".cache" / "lm-studio" / "models",
            home / "models", home / "Downloads"]
    return [d for d in dirs if d.is_dir()]


def find_models(dirs: list[Path]) -> list[Path]:
    seen, out = set(), []
    for d in dirs:
        for p in gguf_files(d):      # leaves out Strata's data folder (its draft layer is not a chat model)
            low = p.name.lower()
            if "mmproj" in low or low in seen:
                continue
            m = re.search(r"-(\d{5})-of-\d{5}\.gguf$", low)
            if m and m.group(1) != "00001":
                continue
            seen.add(low)
            out.append(p)
    return sorted(out, key=lambda p: p.name.lower())


_QUANT = re.compile(r"(?:^|[-_.])(?:b?f16|f32|q\d(?:_[0-9a-z]+)*|iq\d\w*)(?=$|[-_.])", re.I)


def _projector_key(name: str) -> str:
    """The model a vision file is named after: mmproj-Qwen3.8-27B-Q8_0.gguf -> qwen3.8-27b ("" for mmproj-F16)."""
    low = re.sub(r"\.gguf$", "", name.lower()).replace("mmproj", "")
    return _QUANT.sub("", low).strip("-_. ")


def find_mmproj(model: Path, info: ModelInfo | None = None) -> Path | None:
    """The vision file next to the model that is made for it. A vision file for another model makes llama.cpp
    stop at start, so only files whose header fits the model's width count, and when several different ones fit
    and the names don't tell which is the right one, none is picked."""
    cands = sorted(model.parent.glob("*mmproj*.gguf"))
    if not cands:
        return None
    info = info or _safe_info(model)
    emb = info.embedding_length if info else 0
    fits, unknown = [], []
    for c in cands:
        try:
            meta = read_meta(c)
        except (OSError, GGUFError):
            continue
        if meta.get("clip.has_vision_encoder") is False:
            continue          # audio only
        proj = projection_dim(meta)
        if proj and emb:
            if proj % emb == 0:
                fits.append(c)
        else:
            unknown.append(c)
    pool = fits or unknown
    if len(pool) > 1:     # several fit: keep the ones named after this model, if any are
        names = [model.stem.lower()] + [b.lower() for b in vision.base_names(info, model.name)]
        named = [c for c in pool if (k := _projector_key(c.name)) and any(k in n or n in k for n in names)]
        pool = named or pool
    if not pool or len({_projector_key(c.name) for c in pool}) > 1:
        return None
    # the same vision file in several precisions: f16/bf16 are small and lossless enough
    pool.sort(key=lambda p: (0 if re.search(r"f16|bf16", p.name, re.I) else 1, p.name))
    return pool[0]


_help_cache: dict[str, str] = {}
_help_failed: dict[str, float] = {}     # a failed --help is tried again after a while, not kept


def _run_text(args: list[str]) -> str:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=30,
                             creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0,
                             encoding="utf-8", errors="replace")
        return (out.stdout or "") + (out.stderr or "")
    except (OSError, subprocess.SubprocessError):
        return ""


def server_help(exe: Path, wait: bool = True) -> str | None:
    """llama-server's --help (which options this build has) plus its --version line. A failed run (e.g. a DLL
    missing) is not kept, so the next call tries again. wait=False: None instead of running it (it can take
    seconds) when it isn't known yet."""
    key = f"{exe}:{exe.stat().st_mtime if exe.exists() else 0}"
    if key not in _help_cache:
        if time.time() - _help_failed.get(key, 0) < 30:
            return ""
        if not wait:
            return None
        text = _run_text([str(exe), "--help"])
        if "--" not in text:      # it didn't run (a DLL missing, a virus scan that took too long, ...)
            _help_failed[key] = time.time()
            return ""
        _help_cache[key] = text + "\n" + _run_text([str(exe), "--version"])
    return _help_cache[key]


def build_number(help_text: str, exe: Path | None = None) -> int | None:
    """llama.cpp's build number (b11548 -> 11548), from --version or the b1234 folder it was unpacked into."""
    m = re.search(r"\(build (\d+)", help_text) or re.search(r"^version: (\d+)", help_text, re.M)
    if m:
        return int(m.group(1))
    for part in reversed(Path(exe).parts if exe else ()):
        if re.fullmatch(r"b\d{3,}", part):
            return int(part[1:])
    return None


MTP_VISION_BUILD = 11548     # from this build on, MTP drafting sees the pictures too


def split_extra(text: str, windows: bool = WINDOWS) -> list[str]:
    """The Extra options as typed. On Windows backslashes are path separators, not escapes, and quotes only group
    words ("C:\\My Files\\t.jinja" arrives without its quotes, as it would from a command prompt)."""
    if not windows:
        return shlex.split(text)
    lex = shlex.shlex(text, posix=True)
    lex.whitespace_split, lex.escape, lex.commenters = True, "", ""
    return list(lex)


# ---------------------------------------------------------------------- building the command
@dataclass
class LaunchPlan:
    args: list[str]
    notes: list[str] = field(default_factory=list)
    plan: Plan | None = None
    info: ModelInfo | None = None


def build_launch(settings, exe: Path, help_text: str, info: ModelInfo | None = None,
                 vram_gb: float | None = None) -> LaunchPlan:
    s = settings
    model = Path(s.model_path)
    has = lambda flag: flag in help_text
    notes: list[str] = []
    if not help_text.strip():
        notes.append(f"Couldn't read the options of {Path(exe).name} (it gave no --help), so only basic settings are "
                     "used. If it doesn't start either, use 'Download / update llama.cpp'.")
    args = [str(exe), "-m", str(model), "--host", "127.0.0.1", "--port", str(s.port),
            "-c", str(int(s.ctx_size)), "-np", "1"]
    if has("--jinja"):
        args.append("--jinja")          # the model's own chat template: needed for proper tool calls
    if re.search(r"--flash-attn\s*\[?on\|off", help_text) or re.search(r"--flash-attn\s+\[?'?on", help_text):
        args += ["-fa", "on"]
    elif has("--flash-attn"):
        args.append("-fa")
    if s.kv_type and s.kv_type != "f16":
        args += ["-ctk", s.kv_type, "-ctv", s.kv_type]

    # vision
    info = info or _safe_info(model)
    mmproj = Path(s.mmproj_path) if s.mmproj_path else None
    if s.use_vision and mmproj is not None and not mmproj.exists():
        found = find_mmproj(model, info)
        notes.append(f"The vision file picked before ({mmproj}) isn't there any more, so "
                     + (f"{found.name} next to the model is used instead." if found else
                        "the model can't see screenshots. Pick it again with 'Find vision file online'."))
        mmproj = found
    elif mmproj is None and s.use_vision:
        mmproj = find_mmproj(model, info)
        if mmproj is None:
            notes.append("None of the vision files (mmproj) next to this model is made for it, so it can't see "
                         "screenshots. 'Find vision file online' gets the right one."
                         if any(model.parent.glob("*mmproj*.gguf")) else
                         "No vision file (mmproj) next to this model, so it can't see screenshots. Download the "
                         "matching mmproj-*.gguf into the same folder.")
    use_vision = s.use_vision and mmproj is not None and mmproj.exists()
    if use_vision:
        args += ["--mmproj", str(mmproj)]
    elif has("--no-mmproj"):
        args.append("--no-mmproj")

    # placement on GPU / RAM
    extra = (mmproj.stat().st_size / GiB if use_vision else 0.0)
    if s.speedup == "draft" and s.draft_path and Path(s.draft_path).exists():
        extra += Path(s.draft_path).stat().st_size / GiB
    p = None
    if info:
        p = plan(info, int(s.ctx_size), s.kv_type, vram_gb or 16.0, extra_gb=extra)
    auto_fit = s.placement == "auto" and has("--fit")
    if auto_fit:
        args += ["--fit", "on"]
        notes.append("llama.cpp places the model on the GPU and RAM by itself (--fit).")
    else:
        ngl = s.gpu_layers if s.placement == "manual" else (p.gpu_layers if p else 999)
        ncm = s.n_cpu_moe if s.placement == "manual" else (p.n_cpu_moe if p else 0)
        args += ["-ngl", str(ngl)]
        if ncm > 0 and has("--n-cpu-moe"):
            args += ["--n-cpu-moe", str(ncm)]
        elif ncm > 0 and has("--override-tensor"):     # older builds: the same placement spelled out
            args += ["-ot", f"blk\\.({'|'.join(str(i) for i in range(ncm))})\\.ffn_.*exps=CPU"]
        elif ncm > 0:
            notes.append("This llama.cpp can't keep MoE experts in RAM (no --n-cpu-moe), so whole layers move to "
                         "the CPU instead. Use 'Download / update llama.cpp'.")
        if has("--fit"):
            args += ["--fit", "off"]
    if info and info.is_moe and (auto_fit or (p and p.n_cpu_moe > 0) or s.n_cpu_moe > 0):
        args += ["-b", "2048", "-ub", "2048"]   # faster prompt reading when experts sit in RAM

    # speed-ups
    if s.speedup == "mtp":
        if info and not info.has_mtp:
            notes.append("This model file has no MTP layers, so the MTP speed-up was skipped.")
        elif has("draft-mtp"):
            args += ["--spec-type", "draft-mtp", "--spec-draft-n-max", "2"]   # 2 is the measured sweet spot
            if has("--spec-draft-sampling"):
                args += ["--spec-draft-sampling", "probabilistic"]
            build = build_number(help_text, exe)
            if use_vision and build is not None and build < MTP_VISION_BUILD:
                notes.append(f"This llama.cpp (b{build}) drafts MTP guesses without looking at pictures, so with a "
                             f"screenshot in the chat fewer guesses hit and it writes slower. b{MTP_VISION_BUILD} and "
                             "newer fix that: use 'Download / update llama.cpp'.")
        else:
            notes.append("This llama.cpp is too old for MTP. Use 'Download / update llama.cpp'.")
    elif s.speedup == "ngram":
        if has("ngram-mod"):
            args += ["--spec-type", "ngram-mod"]
        elif has("--spec-type") and has("ngram-simple"):
            args += ["--spec-type", "ngram-simple"]
        else:
            notes.append("This llama.cpp has no n-gram speculation; skipped.")
    elif s.speedup == "draft":
        if not s.draft_path or not Path(s.draft_path).exists():
            notes.append("Pick a draft model file first; skipped.")
        else:
            args += ["-md", s.draft_path]
            if has("-ngld"):
                args += ["-ngld", "99"]
            if has("--spec-type") and has("draft-simple"):
                args += ["--spec-type", "draft-simple"]
            if has("--spec-draft-n-max"):
                args += ["--spec-draft-n-max", "8"]
            elif has("--draft-max"):
                args += ["--draft-max", "8"]

    # Prompt cache in RAM (llama.cpp's default is 8 GB): when the whole model is on the GPU the RAM is free, so
    # keep more earlier prompts there. Not when MoE experts or layers sit in RAM.
    on_gpu = p is not None and p.fits_fully and not (
        s.placement == "manual" and (s.n_cpu_moe > 0 or (info is not None and s.gpu_layers < info.n_layers)))
    if on_gpu and has("--cache-ram"):
        args += ["--cache-ram", "24576"]
    if s.extra_args.strip():
        try:
            args += split_extra(s.extra_args)
        except ValueError:
            args += s.extra_args.split()
            notes.append("A quote in the Extra options isn't closed; they were split at the spaces.")
    if p:
        notes += p.notes
    return LaunchPlan(args, notes, p, info)


def _safe_info(model: Path) -> ModelInfo | None:
    try:
        return model_info(model)
    except Exception:
        return None


# ---------------------------------------------------------------------- running it
class ServerProcess:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.lines: collections.deque[str] = collections.deque(maxlen=3000)
        self._lock = threading.RLock()      # a start on a worker thread and stop() don't cross

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str], cwd: str | Path | None = None, clear: bool = True,
              cancel: threading.Event | None = None) -> bool:
        """cancel: the stop flag of the job starting it (a Strata start on a worker thread). Once it's set
        nothing starts, and nothing started since is stopped: False."""
        with self._lock:
            if cancel is not None and cancel.is_set():
                return False
            self.stop()
            if clear:
                self.lines.clear()
            self.lines.append("> " + " ".join(f'"{a}"' if " " in a else a for a in args))
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         stdin=subprocess.DEVNULL, cwd=str(cwd or Path(args[0]).parent),
                                         creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
            threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        return True

    def run_step(self, args: list[str], cwd: str | Path, cancel: threading.Event | None = None) -> int | None:
        """Run a setup command with its output in the log, and wait for it. Stop() ends it. None when
        cancel was set before it started."""
        with self._lock:
            if cancel is not None and cancel.is_set():
                return None
            self.lines.append("> " + " ".join(f'"{a}"' if " " in a else a for a in args))
            proc = self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                                stdin=subprocess.DEVNULL, cwd=str(cwd),
                                                creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0)
        for raw in iter(proc.stdout.readline, b""):
            self.lines.append(raw.decode("utf-8", "replace").rstrip())
        return proc.wait()

    def _pump(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            self.lines.append(raw.decode("utf-8", "replace").rstrip())
        code = proc.wait()
        self.lines.append(f"[server stopped, exit code {code}]")

    def stop(self):
        with self._lock:
            if self.running:
                if WINDOWS:    # the whole tree: a setup script's Python, or a server's engine
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    self.proc.terminate()
                try:
                    self.proc.wait(20)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def ready(self, port: int) -> bool:
        """llama.cpp answers /health with 503 while loading; Strata answers 200 with "loaded": false."""
        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2, proxies={"http": None, "https": None})
            if r.status_code != 200:
                return False
            try:
                return r.json().get("loaded", True) is not False
            except ValueError:
                return True
        except requests.RequestException:
            return False


# ---------------------------------------------------------------------- downloading llama.cpp
# Not /releases/latest: llama.cpp marks its build releases (b1234) as pre-releases, and the
# release GitHub calls "latest" (e.g. v0.6.0) has no Windows builds at all.
RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20"


CUDA13_DRIVER = 580      # CUDA 13 builds need an NVIDIA driver this new


def pick_assets(release: dict, prefer: str = "cuda", max_cuda: int | None = None) -> tuple[dict | None, dict | None]:
    """Choose the newest-CUDA Windows x64 build that has its CUDA runtime zip (or Vulkan as a fallback).
    max_cuda: the newest CUDA major version the driver runs. Assets still uploading are skipped."""
    assets = [a for a in release.get("assets", []) if a.get("state", "uploaded") == "uploaded"]
    if prefer == "cuda":
        builds = []
        for a in assets:
            m = re.search(r"bin-win-cuda-([\d.]+)-x64\.zip$", a["name"])
            if m and not a["name"].startswith("cudart"):
                ver = tuple(int(x) for x in m.group(1).split("."))
                if max_cuda is None or ver[0] <= max_cuda:
                    builds.append((ver, a))
        for ver, a in sorted(builds, key=lambda b: b[0], reverse=True):
            ver_s = ".".join(map(str, ver))
            cudart = next((x for x in assets if x["name"] == f"cudart-llama-bin-win-cuda-{ver_s}-x64.zip"), None)
            if cudart:              # without its runtime a CUDA build quietly runs on the CPU
                return a, cudart
    vk = next((a for a in assets if re.search(r"bin-win-vulkan-x64\.zip$", a["name"])), None)
    return vk, None


def choose_release(releases: list[dict], prefer: str = "cuda",
                   max_cuda: int | None = None) -> tuple[dict | None, dict | None, dict | None]:
    """The newest release that has a Windows build: (release, build zip, CUDA runtime zip)."""
    ordered = sorted((r for r in releases if not r.get("draft")),
                     key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
    for want in ([prefer, "vulkan"] if prefer != "vulkan" else ["vulkan"]):
        for rel in ordered:
            main, runtime = pick_assets(rel, want, max_cuda)
            if main and (want != "cuda" or ("cuda" in main["name"] and runtime)):
                return rel, main, runtime
    return None, None, None


def download_llamacpp(progress=lambda msg, frac: None, prefer: str = "cuda") -> Path:
    progress("Asking GitHub for the newest llama.cpp...", 0.0)
    r = requests.get(RELEASES, timeout=30, headers={"Accept": "application/vnd.github+json"})
    data = r.json()
    if not isinstance(data, list):  # an error such as GitHub's hourly rate limit
        raise RuntimeError(f"GitHub said: {data.get('message', data)}. Try again later, or download a "
                           "llama-*-bin-win-cuda-*-x64.zip from github.com/ggml-org/llama.cpp/releases "
                           "and pick llama-server.exe with Browse.")
    driver = driver_version() if prefer == "cuda" else None
    max_cuda = 12 if driver is not None and driver < CUDA13_DRIVER else None
    rel, main, runtime = choose_release(data, prefer, max_cuda)
    if not main:
        raise RuntimeError("Couldn't find a Windows build in the recent llama.cpp releases.")
    if max_cuda and "cuda" in main["name"]:
        progress(f"Your NVIDIA driver ({driver}) is older than {CUDA13_DRIVER}, so the CUDA 12 build is used.", 0.0)
    # unpacked into a .tmp folder and renamed when everything is there, so a broken download never leaves a
    # half install (a CUDA build without its runtime runs on the CPU without saying so)
    target = LLAMA_DIR / rel.get("tag_name", "latest")
    tmp = target.with_name(target.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    for i, asset in enumerate([a for a in (main, runtime) if a]):
        url = asset["browser_download_url"]
        progress(f"Downloading {asset['name']}...", 0.0)
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or asset.get("size") or 0)
            buf = io.BytesIO()
            for chunk in r.iter_content(1 << 20):
                buf.write(chunk)
                if total:
                    progress(f"Downloading {asset['name']}...", buf.tell() / total)
        progress(f"Unpacking {asset['name']}...", 1.0)
        with zipfile.ZipFile(buf) as z:
            z.extractall(tmp)
    if not next(tmp.rglob(EXE), None):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"Downloaded, but {EXE} wasn't in the zip.")
    _swap_in(tmp, target)
    exe = next(target.rglob(EXE))
    progress(f"llama.cpp {rel.get('tag_name', '')} is ready.", 1.0)
    return exe


def _swap_in(tmp: Path, target: Path):
    """Put the unpacked folder in place of target. The old one is moved aside first (Windows refuses that while
    its llama-server runs, and then nothing is changed) and deleted after."""
    old = target.with_name(target.name + ".old")
    shutil.rmtree(old, ignore_errors=True)
    for attempt in range(5):        # a virus scanner may hold the new files for a moment
        try:
            if target.exists():
                target.rename(old)
            try:
                tmp.rename(target)
            except OSError:
                if old.exists() and not target.exists():
                    old.rename(target)
                raise
            break
        except OSError as e:
            if attempt == 4:
                raise RuntimeError(f"Couldn't put llama.cpp into {target} ({e}). Stop the model server and try "
                                   "again.")
            time.sleep(1)
    shutil.rmtree(old, ignore_errors=True)


def describe_gpu() -> str:
    g = gpu_info()
    if not g:
        return "No NVIDIA GPU found by nvidia-smi (planning for 16 GB)."
    name, total, used = g
    return f"{name}: {total:.1f} GB, {used:.1f} GB in use right now"


if __name__ == "__main__":  # python -m localagent.launcher  -> download llama.cpp
    path = download_llamacpp(lambda m, f: print(f"{m} {f * 100:.0f}%" if 0 < f < 1 else m))
    print(path)
    if os.name == "nt":
        time.sleep(2)
