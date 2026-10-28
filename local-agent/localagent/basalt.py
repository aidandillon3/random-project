"""Basalt (github.com/jesdga95/basalt): a Strata fork for Qwen3.8 Flash Next that runs one .basalt model file.

The app doesn't download or build Basalt (it is built from source with Visual Studio and CUDA). It runs a copy the
user built: picking a .basalt file finds bin/basalt.exe in a folder above it (the models/ folder of the checkout),
and starts `basalt serve` with that checkout's basalt_config.json, the picked file and the app's port."""

from __future__ import annotations

import os
from pathlib import Path

EXE = "basalt.exe" if os.name == "nt" else "basalt"
CONFIG = "basalt_config.json"


def is_model(path) -> bool:
    return bool(path) and str(path).lower().endswith(".basalt")


def find_root(model: Path) -> Path | None:
    """The Basalt checkout the model sits in: the nearest folder above it with bin/basalt(.exe)."""
    for d in Path(model).parents:
        if (d / "bin" / EXE).is_file():
            return d
    return None


def serve_args(model: Path, settings, root: Path | None = None, monitors: bool = True) -> list[str]:
    """monitors: the desktop is on the GPU, so 1728 MiB of its VRAM stays free for it (with less, Windows pages GPU
    memory to RAM and it slows down); without, Basalt's own 700. Given here, it wins over the config file's.
    --relaxed-drafting 0.3: with the sampling the app uses (temperature 1.0), a guessed token the model gives 30% or
    more is kept even when its own draw was another one. Basalt's docs (precision/features.md): IQ3_S at temperature
    1.0 about +10% decode, no measurable loss on gsm8k or MMLU-Pro; tool calls and control tokens stay exact."""
    root = root or find_root(model)
    if root is None:
        raise FileNotFoundError(f"no Basalt build (bin\\{EXE}) in a folder above {model}")
    args = [str(root / "bin" / EXE), "serve"]
    if (root / CONFIG).is_file():
        args += ["--config", str(root / CONFIG)]     # the engine settings; the file and port below win over it
    return args + ["--model", str(model), "--port", str(int(settings.port)),
                   "--vram-reserve-mib", "1728" if monitors else "700", "--relaxed-drafting", "0.3"]
