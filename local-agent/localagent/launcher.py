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

from .config import APP_DIR
from .gguf import GiB, ModelInfo, Plan, gpu_info, model_info, plan

WINDOWS = sys.platform == "win32"
EXE = "llama-server.exe" if WINDOWS else "llama-server"
LLAMA_DIR = APP_DIR / "llama.cpp"


# ---------------------------------------------------------------------- finding things
def find_server(settings) -> Path | None:
    if settings.server_exe and Path(settings.server_exe).is_file():
        return Path(settings.server_exe)
    if LLAMA_DIR.exists():
        hits = sorted(LLAMA_DIR.rglob(EXE), key=lambda p: p.stat().st_mtime, reverse=True)
        if hits:
            return hits[0]
    found = shutil.which("llama-server")
    return Path(found) if found else None


def default_model_dirs() -> list[Path]:
    home = Path.home()
    dirs = [APP_DIR / "models", home / ".lmstudio" / "models", home / ".cache" / "lm-studio" / "models",
            home / "models", home / "Downloads"]
    return [d for d in dirs if d.is_dir()]


def find_models(dirs: list[Path]) -> list[Path]:
    seen, out = set(), []
    for d in dirs:
        try:
            for p in d.rglob("*.gguf"):
                low = p.name.lower()
                if "mmproj" in low or low in seen:
                    continue
                m = re.search(r"-(\d{5})-of-\d{5}\.gguf$", low)
                if m and m.group(1) != "00001":
                    continue
                seen.add(low)
                out.append(p)
        except OSError:
            continue
    return sorted(out, key=lambda p: p.name.lower())


def find_mmproj(model: Path) -> Path | None:
    cands = sorted(model.parent.glob("*mmproj*.gguf"))
    if not cands:
        return None
    # prefer f16/bf16 projectors, they are small and lossless enough
    cands.sort(key=lambda p: (0 if re.search(r"f16|bf16", p.name, re.I) else 1, p.name))
    return cands[0]


_help_cache: dict[str, str] = {}


def server_help(exe: Path) -> str:
    key = f"{exe}:{exe.stat().st_mtime if exe.exists() else 0}"
    if key not in _help_cache:
        try:
            out = subprocess.run([str(exe), "--help"], capture_output=True, text=True, timeout=30,
                                 creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0,
                                 encoding="utf-8", errors="replace")
            _help_cache[key] = (out.stdout or "") + (out.stderr or "")
        except (OSError, subprocess.SubprocessError):
            _help_cache[key] = ""
    return _help_cache[key]


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
    mmproj = Path(s.mmproj_path) if s.mmproj_path else find_mmproj(model)
    use_vision = s.use_vision and mmproj is not None and mmproj.exists()
    if s.speedup == "mtp" and use_vision:
        use_vision = False
        notes.append("MTP speed-up and vision were reported not to work together, so vision is off for "
                     "this launch. Switch the speed-up off to see the screen.")
    if use_vision:
        args += ["--mmproj", str(mmproj)]
    elif has("--no-mmproj"):
        args.append("--no-mmproj")
    if s.use_vision and mmproj is None:
        notes.append("No vision file (mmproj) next to this model, so it can't see screenshots. Download the "
                     "matching mmproj-*.gguf into the same folder.")

    # placement on GPU / RAM
    info = info or _safe_info(model)
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
        if has("--fit"):
            args += ["--fit", "off"]
    if info and info.is_moe and (auto_fit or (p and p.n_cpu_moe > 0) or s.n_cpu_moe > 0):
        args += ["-b", "2048", "-ub", "2048"]   # faster prompt reading when experts sit in RAM

    # speed-ups
    if s.speedup == "mtp":
        if info and not info.has_mtp:
            notes.append("This model file has no MTP layers, so the MTP speed-up was skipped.")
        elif has("draft-mtp"):
            args += ["--spec-type", "draft-mtp", "--spec-draft-n-max", "3"]
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

    if has("--cache-reuse"):
        args += ["--cache-reuse", "256"]
    if s.extra_args.strip():
        args += shlex.split(s.extra_args, posix=not WINDOWS)
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
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, args: list[str]):
        self.stop()
        self.lines.clear()
        self.lines.append("> " + " ".join(f'"{a}"' if " " in a else a for a in args))
        self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                     creationflags=subprocess.CREATE_NO_WINDOW if WINDOWS else 0,
                                     cwd=str(Path(args[0]).parent))
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for raw in iter(proc.stdout.readline, b""):
            self.lines.append(raw.decode("utf-8", "replace").rstrip())
        code = proc.wait()
        self.lines.append(f"[server stopped, exit code {code}]")

    def stop(self):
        if self.running:
            self.proc.terminate()
            try:
                self.proc.wait(8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def ready(self, port: int) -> bool:
        try:
            r = requests.get(f"http://127.0.0.1:{port}/health", timeout=2, proxies={"http": None, "https": None})
            return r.status_code == 200
        except requests.RequestException:
            return False


# ---------------------------------------------------------------------- downloading llama.cpp
# Not /releases/latest: llama.cpp marks its build releases (b1234) as pre-releases, and the
# release GitHub calls "latest" (e.g. v0.6.0) has no Windows builds at all.
RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=20"


def pick_assets(release: dict, prefer: str = "cuda") -> tuple[dict | None, dict | None]:
    """Choose the newest-CUDA Windows x64 build and its CUDA runtime zip (or Vulkan as a fallback)."""
    assets = release.get("assets", [])
    best, best_ver = None, ()
    if prefer == "cuda":
        for a in assets:
            m = re.search(r"bin-win-cuda-([\d.]+)-x64\.zip$", a["name"])
            if m and not a["name"].startswith("cudart"):
                ver = tuple(int(x) for x in m.group(1).split("."))
                if ver > best_ver:
                    best, best_ver = a, ver
        if best:
            ver_s = ".".join(map(str, best_ver))
            cudart = next((a for a in assets if a["name"] == f"cudart-llama-bin-win-cuda-{ver_s}-x64.zip"), None)
            return best, cudart
    vk = next((a for a in assets if re.search(r"bin-win-vulkan-x64\.zip$", a["name"])), None)
    return vk, None


def choose_release(releases: list[dict], prefer: str = "cuda") -> tuple[dict | None, dict | None, dict | None]:
    """The newest release that has a Windows build: (release, build zip, CUDA runtime zip)."""
    ordered = sorted((r for r in releases if not r.get("draft")),
                     key=lambda r: r.get("published_at") or r.get("created_at") or "", reverse=True)
    for want in ([prefer, "vulkan"] if prefer != "vulkan" else ["vulkan"]):
        for rel in ordered:
            main, runtime = pick_assets(rel, want)
            if main and (want != "cuda" or "cuda" in main["name"]):
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
    rel, main, runtime = choose_release(data, prefer)
    if not main:
        raise RuntimeError("Couldn't find a Windows build in the recent llama.cpp releases.")
    target = LLAMA_DIR / rel.get("tag_name", "latest")
    target.mkdir(parents=True, exist_ok=True)
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
            z.extractall(target)
    exe = next(target.rglob(EXE), None)
    if not exe:
        raise RuntimeError(f"Downloaded, but {EXE} wasn't in the zip.")
    progress(f"llama.cpp {rel.get('tag_name', '')} is ready.", 1.0)
    return exe


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
