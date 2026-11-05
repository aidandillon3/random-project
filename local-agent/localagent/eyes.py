"""Watch mode's own small vision model: Qwen3.5 4B on llama.cpp, on a GPU the chat's model doesn't use (the
3080 Ti beside a 5080), so a look doesn't wait for the big model.

Why: with the chat's model on Strata a look took about 1.2 s however small the picture (it reads every look
whole: Strata keeps a conversation only from 8192 tokens on, and its MoE experts stream from RAM for each
prompt). A 4B model that sits whole on its own GPU reads ~1000 tokens in a fraction of that. Qwen3.5 4B scored
60.0 on ScreenSpot-Pro (GUI grounding) as reported in arXiv 2606.25964, more than many bigger models.

Qwen's vision models give click positions on a 0-1000 grid over the picture, so watch mode uses the grid
with it whatever the chat uses. It starts when watch mode first needs it and keeps running until the app
closes; a server of it left from before (same port, same model) is used as it is.
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

import requests

from . import catalog, launcher
from .config import APP_DIR
from .gguf import chosen_gpus, list_gpus
from .llm import LLMClient

REPO = "unsloth/Qwen3.5-4B-GGUF"
PRESET = catalog.Preset(
    key="eyes-qwen3.5-4b", title="Qwen3.5 4B (watch mode's eyes)", family="Watch mode", variant="Q6_K",
    blurb="A small vision model for watch mode, on a GPU of its own.",
    repo=REPO, files=["Qwen3.5-4B-Q6_K.gguf"], mmproj=(REPO, "mmproj-F16.gguf"), folder="Qwen3.5-4B",
    sizes={},           # Hugging Face's sizes are used (the download checks them and the SHA-256)
)
DOWNLOAD_GB = 4.2       # 3.53 GB model + 0.67 GB vision file
CTX = 8192              # a look is ~1000 tokens; room for a few without the GPU memory a big context takes
LOAD_SECONDS = 120      # most a start may take (the first one reads 4 GB from disk)
# Qwen's non-thinking sampling, a bit cooler: an action line should aim, not vary
PARAMS = {"temperature": 0.3, "top_p": 0.8, "top_k": 20, "chat_template_kwargs": {"enable_thinking": False}}


def dirs(settings) -> list[Path]:
    models = Path(settings.models_dir) if settings.models_dir else APP_DIR / "models"
    return [models] + [d for d in launcher.default_model_dirs() if d != models]


def files(settings) -> tuple[Path | None, Path | None, list[str]]:
    """(model, vision file, missing file names) on this PC."""
    return catalog.model_path(PRESET, dirs(settings))


def download_folder(settings) -> Path:
    return dirs(settings)[0] / PRESET.folder


def pick_gpu(settings, gpus=None):
    """The GPU to run it on: the one picked in Settings, else the one with the most free memory that the chat's
    model doesn't use, else (one GPU) the chat's. None without NVIDIA GPUs."""
    gpus = list_gpus() if gpus is None else gpus
    if not gpus:
        return None
    want = getattr(settings, "watch_model_gpu", "") or ""
    for g in gpus:
        if g.uuid == want:
            return g
    if (settings.gpus or "") in ("all",):
        busy = set()
    else:
        busy = {g.uuid for g in chosen_gpus(settings.gpus or "", gpus)}
    free = [g for g in gpus if g.uuid not in busy] or gpus
    return max(free, key=lambda g: (g.total_gb - g.used_gb, -g.index))


def build_args(settings, exe: Path, model: Path, mmproj: Path, help_text: str) -> list[str]:
    args = [str(exe), "-m", str(model), "--mmproj", str(mmproj), "--host", "127.0.0.1",
            "--port", str(port(settings)), "-c", str(CTX), "-np", "1", "-ngl", "99"]
    if "--jinja" in help_text:
        args.append("--jinja")
    if re.search(r"--flash-attn\s*\[?on\|off", help_text) or re.search(r"--flash-attn\s+\[?'?on", help_text):
        args += ["-fa", "on"]
    elif "--flash-attn" in help_text:
        args.append("-fa")
    if "--fit" in help_text:
        args += ["--fit", "off"]        # it fits whole; the chat's model is on the other GPU
    return args


def port(settings) -> int:
    return int(getattr(settings, "watch_model_port", 0) or 8091)


class Eyes:
    def __init__(self):
        self.proc = launcher.ServerProcess()
        self.lock = threading.Lock()
        self.gpu_name = ""

    def _ours(self, settings) -> bool:
        """A server answers on its port with this model loaded (also one left from an earlier run)."""
        if not self.proc.ready(port(settings)):
            return False
        try:
            r = requests.get(f"http://127.0.0.1:{port(settings)}/v1/models", timeout=3,
                             proxies={"http": None, "https": None})
            ids = " ".join(str(m.get("id", "")) for m in r.json().get("data", []))
        except (requests.RequestException, ValueError, AttributeError):
            return False
        return Path(PRESET.files[0]).stem.lower() in ids.lower() or "qwen3.5-4b" in ids.lower()

    def client(self, settings, say=lambda msg: None, stop: threading.Event | None = None) -> LLMClient:
        """A client for it, started first if it isn't running. Raises RuntimeError saying what's missing."""
        with self.lock:
            if not self._ours(settings):
                self._start(settings, say, stop)
            return LLMClient(f"http://127.0.0.1:{port(settings)}/v1", model=PRESET.files[0], timeout=60)

    def _start(self, settings, say, stop):
        model, mm, missing = files(settings)
        if missing or model is None or mm is None:
            raise RuntimeError("Watch mode's own model isn't downloaded yet (Settings > Computer use > Watch "
                               f"mode's model: Get it ready, {DOWNLOAD_GB:.1f} GB).")
        exe = launcher.find_server(settings)
        if exe is None:
            raise RuntimeError("llama.cpp isn't on this PC: use 'Download / update llama.cpp' on the Model "
                               "server page.")
        if launcher.port_taken(port(settings)):
            raise RuntimeError(f"Something else is using port {port(settings)}, which watch mode's model needs.")
        g = pick_gpu(settings)
        self.gpu_name = g.name if g else ""
        self.proc.env = {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": str(g.index)} if g else {}
        say(f"Starting watch mode's model on the {self.gpu_name or 'GPU'}...")
        self.proc.start(build_args(settings, exe, model, mm, launcher.server_help(exe) or ""), cwd=exe.parent)
        end = time.time() + LOAD_SECONDS
        while time.time() < end:
            if stop is not None and stop.is_set():
                self.proc.stop()
                raise RuntimeError("Stopped while watch mode's model was loading.")
            if not self.proc.running:
                tail = " | ".join(list(self.proc.lines)[-4:])
                raise RuntimeError(f"Watch mode's model stopped while loading: {tail[-400:]}")
            if self._ours(settings):
                return
            time.sleep(0.5)
        self.proc.stop()
        raise RuntimeError(f"Watch mode's model didn't load within {LOAD_SECONDS} s.")

    def stop(self):
        self.proc.stop()


EYES = Eyes()
