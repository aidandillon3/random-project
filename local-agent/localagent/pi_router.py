"""pi-local.bat: the pi coding agent (pi.dev) with this app's models, without the app.

Starts one llama.cpp server in router mode with a preset for each catalog model that's on this PC and runs on
llama.cpp (the same options the Model server page would use), plus the model picked in the app if it isn't one
of them. pi's own llama.cpp provider lists them in /model and the router loads the one picked, one at a time.
Strata modes (Flash Next, the Qwen3.6 fork) and the 1-bit engine can't run in llama.cpp's router: those stay in
the app.
"""

from __future__ import annotations

import copy
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import catalog, eyes, launcher
from .config import DATA_DIR, Settings
from .gguf import list_gpus, strongest

PORT = 8077
# llama.cpp's preset files name options by their long names
LONG = {"-m": "model", "-c": "ctx-size", "-np": "parallel", "-fa": "flash-attn", "-ctk": "cache-type-k",
        "-ctv": "cache-type-v", "-ngl": "n-gpu-layers", "-b": "batch-size", "-ub": "ubatch-size",
        "-ot": "override-tensor", "-md": "model-draft", "-ngld": "n-gpu-layers-draft"}
SKIP = {"--host", "--port"}           # the router's own


def _name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")


def to_ini(args: list[str]) -> list[str]:
    """A llama-server command (without the exe) as preset lines: '-c 32768' -> 'ctx-size = 32768',
    '--jinja' -> 'jinja = true'."""
    out, i = [], 0
    while i < len(args):
        flag = args[i]
        value = args[i + 1] if i + 1 < len(args) and not re.match(r"--?[A-Za-z]", args[i + 1]) else None
        i += 2 if value is not None else 1
        if flag in SKIP:
            continue
        key = LONG.get(flag, flag.lstrip("-"))
        out.append(f"{key} = {'true' if value is None else value}")
    return out


def entries(settings: Settings, exe: Path, help_text: str) -> list[tuple[str, list[str]]]:
    """(model name in pi, preset lines) for each model on this PC that the router can run."""
    out, seen = [], set()
    dirs = eyes.dirs(settings)

    def add(name: str, s: Settings):
        lp = launcher.build_launch(s, exe, help_text)
        out.append((_name(name), to_ini(lp.args[1:])))
        seen.add(Path(s.model_path).name.lower())

    for p in catalog.PRESETS:
        if p.strata or p.odp:
            continue
        model, mm, _missing = catalog.model_path(p, dirs)
        if model is None:
            continue
        s = copy.deepcopy(settings)
        s.model_path, s.mmproj_path, s.extra_args = str(model), str(mm) if mm else "", ""
        s.speedup, s.placement = "off", "auto"
        for k, v in p.launch.items():
            setattr(s, k, v)
        add(p.key, s)
    current = Path(settings.model_path) if settings.model_path else None
    match = catalog.matching(current) if current else None
    if current and current.is_file() and current.name.lower() not in seen and not (match and (match.strata or match.odp)):
        s = copy.deepcopy(settings)
        if match:          # a catalog model kept outside the models folders: its options, its name
            for k, v in match.launch.items():
                setattr(s, k, v)
        add(match.key if match else re.sub(r"(-\d{5}-of-\d{5})?\.gguf$", "", current.name, flags=re.I), s)
    return out


def _label(gpu) -> str:
    """RTX 5080 -> 5080, RTX 3080 Ti -> 3080ti."""
    m = re.search(r"(\d{3,4})\s*(Ti|Super)?\b", gpu.name, re.I)
    return (m.group(1) + (m.group(2) or "")).lower() if m else f"gpu{gpu.index}"


def per_gpu(items: list[tuple[str, list[str]]], gpus) -> list[tuple[str, list[str]]]:
    """Each model once per GPU and once on all of them (pi has no GPU picker, so the name picks it):
    swift-27b-5080, swift-27b-3080ti, swift-27b-both. One GPU: the names as they are."""
    if len(gpus) < 2:
        return items
    choices = [(_label(g), [g]) for g in strongest(gpus)] + [("both" if len(gpus) == 2 else "all", strongest(gpus))]
    return [(f"{name}-{label}", [*lines, "device = " + ",".join(f"CUDA{g.index}" for g in use)])
            for name, lines in items for label, use in choices]


def write_ini(items: list[tuple[str, list[str]]], path: Path) -> Path:
    lines = ["; written by pi-local.bat from the Local Agent app's models; rewritten each start", ""]
    for name, opts in items:
        lines += [f"[{name}]", *opts, ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main(argv: list[str]) -> int:
    settings = Settings.load()
    exe = launcher.find_server(settings)
    if exe is None:
        print("llama.cpp isn't on this PC: run the app once and use 'Download / update llama.cpp'.")
        return 1
    help_text = launcher.server_help(exe) or ""
    if "--models-preset" not in help_text:
        print(f"This llama.cpp ({exe.parent.name}) has no router mode: use 'Download / update llama.cpp' in the app.")
        return 1
    pi = shutil.which("pi")
    if pi is None:
        print("pi isn't installed. Install it with: npm install -g @earendil-works/pi-coding-agent")
        return 1
    items = per_gpu(entries(settings, exe, help_text), list_gpus())
    if not items:
        print("None of the app's llama.cpp models is on this PC yet (Strata modes run only in the app).")
        return 1
    if launcher.port_taken(settings.port):
        print(f"Note: the app's model server is running on port {settings.port} and holds GPU memory. "
              "Stop it in the app (or close the app) for the fastest start here.")
    ini = write_ini(items, DATA_DIR / "pi-models.ini")
    port = PORT
    if launcher.port_taken(port):
        for pid in launcher.port_owners(port):      # a router left from an earlier run
            launcher.kill_tree(pid)
        time.sleep(1)
    env = {**os.environ, "CUDA_DEVICE_ORDER": "PCI_BUS_ID"}     # CUDA0/1 = nvidia-smi's numbers
    env.pop("CUDA_VISIBLE_DEVICES", None)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    log = open(DATA_DIR / "pi-router.log", "w", encoding="utf-8", errors="replace")
    router = subprocess.Popen([str(exe), "--models-preset", str(ini), "--models-max", "1",
                               "--host", "127.0.0.1", "--port", str(port)],
                              cwd=str(exe.parent), env=env, stdout=log, stderr=subprocess.STDOUT,
                              creationflags=flags)
    print("Models for pi: " + ", ".join(n for n, _ in items))
    print("Pick one with /model in pi (the end of the name is the GPU); it loads when picked (the first message waits for it). "
          f"Server log: {DATA_DIR / 'pi-router.log'}")
    try:
        end = time.time() + 30
        while time.time() < end and router.poll() is None and not launcher.port_taken(port):
            time.sleep(0.3)
        if router.poll() is not None:
            print("llama.cpp's router stopped at start; see the server log.")
            return 1
        pi_env = {**os.environ, "LLAMA_BASE_URL": f"http://127.0.0.1:{port}"}
        return subprocess.call([pi, *argv], env=pi_env, shell=os.name == "nt")
    finally:
        if router.poll() is None:
            launcher.kill_tree(router.pid)
        log.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
