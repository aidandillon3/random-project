"""Ready-made model modes: a known model file (downloaded from Hugging Face if it isn't on the
PC yet) plus the server settings that suit it on a 16 GB card with 64 GB of RAM.

Sampling and thinking levels are not set here: the agent reads them from the running model
(guide.sampling_for and the chat template), so they follow whichever mode is running.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .gguf import gguf_files

HF = "https://huggingface.co"
GB = 1000 ** 3
CHUNK = 1 << 20
SIZES_FILE = "localagent-sizes.json"   # sizes Hugging Face gave for files that changed since this app was made


@dataclass
class Preset:
    key: str
    title: str
    blurb: str
    repo: str
    files: list[str]                      # the first one is the file llama.cpp loads
    mmproj: tuple[str, str] | None        # (repo, file) of the vision file
    folder: str                           # sub-folder of the models folder it downloads into
    sizes: dict[str, int] = field(default_factory=dict)   # bytes, for the space check and progress
    launch: dict = field(default_factory=dict)            # Settings fields to apply
    strata: dict | None = None            # Strata's setup choice, when Strata can run it (strata.py)
    subdir: str = ""                      # the model files' folder in the repo, when they aren't at its top
    extra: list[tuple[str, str]] = field(default_factory=list)   # (repo, file) from other repos, beside the model
    odp: dict | None = None               # the 1-bit engine's prefill blocks, when it runs on it (odp.py)

    def remote(self, repo: str, name: str) -> str:
        """The file's path in its Hugging Face repo."""
        return f"{self.subdir}/{name}" if self.subdir and repo == self.repo and name in self.files else name

    def all_files(self) -> list[tuple[str, str]]:
        out = [(self.repo, f) for f in self.files] + list(self.extra)
        if self.mmproj:
            out.append(self.mmproj)
        return out

    def total_bytes(self) -> int:
        return sum(self.sizes.get(f, 0) for _, f in self.all_files())


FLASH_REPO = "ukisai/Swift-1.5-Qwen3.8-Flash-Next-GSQ-RCO-GGUF"
FLASH_MMPROJ = "mmproj-Swift-Qwen3.8-Flash-Next-BF16.gguf"


def _flash_next(key: str, size: str, note: str, blurb: str, shards: tuple[int, int],
                engine_args: dict | None = None) -> Preset:
    """A size of Swift 1.5 Flash Next: two shards named as Strata's setup expects for --model <size>, in one
    folder with the vision file all sizes share. engine_args: Strata options that differ from strata.ENGINE_ARGS."""
    files = [f"Swift-Qwen3.8-Flash-Next-GSQ-RCO-{size}-0000{i}-of-00002.gguf" for i in (1, 2)]
    return Preset(
        key=key, title=f"Swift 1.5 Qwen3.8 Flash Next {size} ({note})", blurb=blurb, repo=FLASH_REPO, files=files,
        mmproj=(FLASH_REPO, FLASH_MMPROJ), folder="Swift-1.5-Qwen3.8-Flash-Next",
        sizes={**dict(zip(files, shards)), FLASH_MMPROJ: 907543520},
        # no MTP layers in these files (Strata fetches its own draft layer); its sparse attention keeps
        # the context cache tiny
        launch={"ctx_size": 65536, "kv_type": "q8_0", "speedup": "off", "placement": "auto", "use_vision": True,
                "engine": "strata"},
        strata={"family": "swift", "model": size, "tag": "swift-", "engine_args": engine_args or {}},
    )


PRESETS = [
    Preset(
        key="swift-27b",
        title="Swift 1.5 Qwen3.8 27B (fast, fits on the GPU)",
        blurb="Dense 27B, IQ3_XXS with MTP layers. Everything stays on the GPU, so it's the quickest. "
              "Vision uses the base Qwen3.8-27B adapter.",
        repo="ukisai/Swift-1.5-Qwen3.8-27B-GSQ-RCO-GGUF",
        files=["Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_XXS-mtp.gguf"],
        mmproj=("ggml-org/Qwen3.8-27B-GGUF", "mmproj-Qwen3.8-27B-Q8_0.gguf"),
        folder="Swift-1.5-Qwen3.8-27B",
        sizes={"Swift-1.5-Qwen3.8-27B-GSQ-RCO-IQ3_XXS-mtp.gguf": 10442827776,
               "mmproj-Qwen3.8-27B-Q8_0.gguf": 629247008},
        launch={"ctx_size": 49152, "kv_type": "q8_0", "speedup": "mtp", "placement": "auto", "use_vision": True},
    ),
    _flash_next(
        "swift-flash-next", "IQ2_XS", "smart, uses RAM",
        "125B MoE with 6B active per token, IQ2_XS. The experts sit in RAM and the 28 GB "
        "n-gram table is read from the SSD as needed, so it needs about 40 GB of RAM + VRAM. "
        "Much more capable than the 27B. With Strata it's about as quick; on plain llama.cpp it's "
        "several times slower. Has its own vision file.",
        (39788473344, 28363693824)),
    # the larger sizes keep more experts in RAM (Strata: 43 / 50 GB of experts, vs 35.5 for IQ2_XS), so they
    # park fewer chats there; 1 GiB still holds one parked 64K chat (~0.9 GB of 8-bit KV)
    _flash_next(
        "swift-flash-next-iq3xxs", "IQ3_XXS", "smarter, a bit slower",
        "The same 125B model at 3 bits (IQ3_XXS): closer to the full model than IQ2_XS and about "
        "20% slower on Strata (62 vs 79 tokens/s on a 12 GB RTX 5070). Its 43 GB of experts stay in "
        "RAM, so keep about 10 GB free beside it. Shares the vision file and Strata's draft layer "
        "with IQ2_XS.",
        (39785790560, 36180282560), {"--conversation-cache-mib": "2048"}),
    _flash_next(
        "swift-flash-next-iq3s", "IQ3_S", "smartest, slowest, most RAM",
        "The same 125B model at 3.5 bits (IQ3_S): the closest to the full model (it matches it on "
        "the published tests) and about a third slower than IQ2_XS on Strata (53 vs 79 tokens/s on a "
        "12 GB RTX 5070). Its 50 GB of experts stay in RAM, which leaves a 64 GB PC about 10 GB for "
        "everything else: close big programs like Unity or games while it runs, or take IQ3_XXS.",
        (44900816736, 38842920384), {"--conversation-cache-mib": "1024"}),
    # ISTA-DASLab's pruned release: the original Qwen3.8 (not Swift), 256 of each layer's 512 experts kept, picked
    # on code, tool use and images. Strata: --family coder, its only size IQ1_M (the experts stored like IQ3_S)
    Preset(
        key="flash-next-coder",
        title="Qwen3.8 Flash Next Coder IQ1_M (for coding, half the RAM)",
        blurb="The original Flash Next with half its experts removed, keeping the ones code, tools and images "
              "use. The rest are stored like IQ3_S, so it codes nearly as well as the full model (by its makers: "
              "99% on LiveCodeBench, 91% on SWE-bench Verified) but is weaker at everything else. Its 23 GB of "
              "experts leave plenty of RAM for Unity or a game. It is the plain Qwen model, not Swift, so it "
              "thinks for longer before answering. Has its own vision file.",
        repo="ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF", subdir="IQ1_M",
        files=[f"Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-0000{i}-of-00002.gguf" for i in (1, 2)],
        mmproj=("ISTA-DASLab/Qwen3.8-Flash-Next-GSQ-RCO-Coder-GGUF", "mmproj-Qwen3.8-Flash-Next-BF16.gguf"),
        folder="Qwen3.8-Flash-Next-Coder",
        sizes={"Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00001-of-00002.gguf": 29608446496,
               "Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-00002-of-00002.gguf": 28800138432,
               "mmproj-Qwen3.8-Flash-Next-BF16.gguf": 907543008},
        launch={"ctx_size": 65536, "kv_type": "q8_0", "speedup": "off", "placement": "auto", "use_vision": True,
                "engine": "strata"},
        # only 23 GB of experts in RAM, so it can park more chats than the default 4 GiB
        strata={"family": "coder", "model": "IQ1_M", "tag": "coder-",
                "engine_args": {"--conversation-cache-mib": "8192"}},
    ),
    # IST-DASLab's disaggregated quantization (odp.py): Unsloth's 1-bit 27B writes, the NVFP4 prefill blocks
    # trained for exactly that file read the prompt, streamed from the SSD. 64 blocks, every 4th one smaller.
    Preset(
        key="qwen27b-1bit-odp",
        title="Qwen3.8 27B 1-bit + NVFP4 prefill (experimental)",
        blurb="Research mode from IST-DASLab: a 1-bit 27B (6 GB on the GPU) writes the answers, and a 4-bit "
              "copy of its layers trained to go with it reads your prompt, streamed from the SSD a layer at a "
              "time so it takes no VRAM. By its makers' tests that wins back much of what 1 bit loses, but it "
              "is still below the 3-bit Swift 27B. Plain Qwen (not Swift), no vision, no MTP, and every prompt "
              "streams the 13.7 GB of prefill weights (about 1-3 s per step). Runs on its own engine, built "
              "in your GitHub repo.",
        repo="unsloth/Qwen3.8-27B-GGUF",
        files=["Qwen3.8-27B-UD-IQ1_S.gguf"],
        extra=[("ISTA-DASLab/Qwen3.8-27B-NVFP4-prefiller", f"Qwen3.8-27B-ODP-IQ1_S-{i:05d}-of-00064.gguf")
               for i in range(1, 65)],
        mmproj=None,
        folder="Qwen3.8-27B-1bit-ODP",
        sizes={"Qwen3.8-27B-UD-IQ1_S.gguf": 6192222208,
               **{f"Qwen3.8-27B-ODP-IQ1_S-{i:05d}-of-00064.gguf": 209388416 if i % 4 == 0 else 216269632
                  for i in range(1, 65)}},
        # no MTP: ODP streams the prefill weights for every multi-token batch, a verify batch included
        launch={"ctx_size": 65536, "kv_type": "q8_0", "speedup": "off", "placement": "auto", "use_vision": False},
        odp={"blocks": "Qwen3.8-27B-ODP-IQ1_S-00001-of-00064.gguf"},
    ),
]
BY_KEY = {p.key: p for p in PRESETS}


def url(repo: str, file: str, base: str = HF) -> str:
    return f"{base}/{repo}/resolve/main/{file}"


def locate(preset: Preset, dirs: list[Path]) -> dict[str, Path]:
    """Where each of the preset's files already is on this PC (by file name, in any models folder)."""
    want = {f for _, f in preset.all_files()}
    found: dict[str, Path] = {}
    for d in dirs:
        for name in list(want - found.keys()):
            p = d / preset.folder / name
            if p.is_file() and _complete(preset, p):
                found[name] = p
        if want <= found.keys():
            break
        for p in gguf_files(d):       # not in Strata's data folder or unfinished downloads
            if p.name in want and p.name not in found and _complete(preset, p):
                found[p.name] = p
    return found


def _expected_size(preset: Preset, p: Path) -> int | None:
    """The size the file should have: what Hugging Face said when it was downloaded, else the preset's."""
    size = preset.sizes.get(p.name)
    try:
        return int(json.loads((p.parent / SIZES_FILE).read_text(encoding="utf-8")).get(p.name, size) or 0) or None
    except (OSError, ValueError, TypeError, AttributeError):
        return size


def _remember_size(folder: Path, name: str, size: int):
    path = folder / SIZES_FILE
    try:
        sizes = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sizes = {}
    sizes = sizes if isinstance(sizes, dict) else {}
    sizes[name] = size
    path.write_text(json.dumps(sizes, indent=1), encoding="utf-8")


def _complete(preset: Preset, p: Path) -> bool:
    size = _expected_size(preset, p)
    try:
        return not size or p.stat().st_size == size
    except OSError:
        return False


def _hub_header(r, name: str) -> str:
    """A header of the response or of Hugging Face's redirect before it (X-Linked-* are on the redirect)."""
    for resp in [r] + list(reversed(getattr(r, "history", None) or [])):
        v = (getattr(resp, "headers", None) or {}).get(name)
        if v:
            return str(v)
    return ""


def _sha256_of(r) -> str:
    """The file's SHA-256 as Hugging Face gives it for large (LFS) files, or ""."""
    tag = _hub_header(r, "X-Linked-Etag").strip().removeprefix("W/").strip('"').lower()
    return tag if re.fullmatch(r"[0-9a-f]{64}", tag) else ""


def _server_size(r) -> int:
    """The whole file's size by the server's word, for a response that sends it from the start."""
    v = _hub_header(r, "X-Linked-Size")
    headers = getattr(r, "headers", None) or {}
    if not v and headers.get("Content-Encoding", "identity") == "identity":
        v = headers.get("Content-Length", "")
    return int(v) if str(v).isdigit() else 0


def _hash_part(h, part: Path, stop: threading.Event, say):
    """Feed the part downloaded before into the checksum."""
    with open(part, "rb") as f:
        while chunk := f.read(CHUNK * 8):
            if stop.is_set():
                raise DownloadError("Download paused. Press the button again to carry on where it stopped.")
            h.update(chunk)
            say(f"Checking the part of {part.name[:-5]} downloaded before...")


def model_path(preset: Preset, dirs: list[Path]) -> tuple[Path | None, Path | None, list[str]]:
    """(model file, vision file, missing file names)."""
    found = locate(preset, dirs)
    missing = [f for _, f in preset.all_files() if f not in found]
    side = preset.files + [f for _, f in preset.extra]
    model = found.get(preset.files[0]) if all(f in found for f in side) else None
    mm = found.get(preset.mmproj[1]) if preset.mmproj else None
    if model and any(found[f].parent != model.parent for f in side):
        missing = [f for f in side[1:] if found[f].parent != model.parent] + missing
        model = None      # split files must sit side by side
    return model, mm, missing


def matching(path: str | Path) -> Preset | None:
    name = Path(path).name if path else ""
    return next((p for p in PRESETS if name == p.files[0]), None)


def apply(preset: Preset, settings, model: Path, mmproj: Path | None) -> None:
    settings.model_path = str(model)
    settings.mmproj_path = str(mmproj) if mmproj else ""
    for k, v in preset.launch.items():
        setattr(settings, k, v)
    settings.save()


class DownloadError(Exception):
    pass


def download(preset: Preset, dest: Path, progress=lambda msg, frac: None, stop: threading.Event | None = None,
             base: str = HF, session=None) -> tuple[Path, Path | None]:
    """Download the preset's files that aren't complete in dest yet. Interrupted downloads resume
    from their .part file. Each file is checked against the size and SHA-256 Hugging Face gives for it;
    when Hugging Face's size differs from the one this app knows (the uploader updated the file),
    Hugging Face's is used and that is said in the progress message."""
    session = session or requests.Session()
    stop = stop or threading.Event()
    dest.mkdir(parents=True, exist_ok=True)
    todo = [(r, f) for r, f in preset.all_files() if not ((dest / f).is_file() and _complete(preset, dest / f))]
    need = sum((_expected_size(preset, dest / f) or 0) - _part_size(dest / f) for _, f in todo)
    free = shutil.disk_usage(dest).free
    if need > free - 2 * GB:
        raise DownloadError(f"Not enough free space on that drive: this needs {need / GB:.1f} GB and "
                            f"{free / GB:.1f} GB is free. Pick a models folder on a bigger drive.")
    total = max(1, sum(_expected_size(preset, dest / f) or 0 for _, f in todo))
    done_before = 0
    notes: list[str] = []

    def say(msg: str, got: int = 0):
        progress(" ".join([msg] + notes), min(1.0, (done_before + got) / total))

    for repo, name in todo:
        target = dest / name
        part = target.with_name(name + ".part")
        size = _expected_size(preset, target)
        start = _part_size(target)
        headers = {"Range": f"bytes={start}-"} if start else {}
        h = None
        try:
            link = url(repo, preset.remote(repo, name), base)
            with session.get(link, headers=headers, stream=True, timeout=60) as r:
                sha = _sha256_of(r)
                h = hashlib.sha256() if sha else None
                if r.status_code == 416:          # the .part is already complete
                    if h:
                        _hash_part(h, part, stop, say)
                elif r.status_code in (401, 403):
                    raise DownloadError(f"Hugging Face won't let this app download {name} ({r.status_code}). "
                                        f"Open {base}/{repo}, accept the terms if it asks, and try again.")
                else:
                    r.raise_for_status()
                    if start and r.status_code != 206:
                        start = 0                 # the server ignored the resume: start over
                    server = 0 if start else _server_size(r)
                    if server and server != size:
                        if size:
                            notes.append(f"Note: {name} on Hugging Face is {server:,} bytes, not the {size:,} this "
                                         "app expected (the uploader has updated it), so the current one is "
                                         "downloaded.")
                            _remember_size(dest, name, server)
                        size = server
                    if h and start:
                        _hash_part(h, part, stop, say)
                    with open(part, "ab" if start else "wb") as f:
                        got = start
                        for chunk in r.iter_content(CHUNK):
                            if stop.is_set():
                                raise DownloadError("Download paused. Press the button again to carry on "
                                                    "where it stopped.")
                            f.write(chunk)
                            if h:
                                h.update(chunk)
                            got += len(chunk)
                            say(f"Downloading {name} ({got / GB:.1f} of {(size or got) / GB:.1f} GB)", got)
        except requests.RequestException as e:
            raise DownloadError(f"The download broke off ({e}). Press the button again to resume it.")
        if size and part.stat().st_size != size:
            raise DownloadError(f"{name} came out the wrong size ({part.stat().st_size} instead of {size} bytes). "
                                "Delete the .part file next to it and try again.")
        if h and h.hexdigest() != sha:
            part.unlink()
            raise DownloadError(f"{name} doesn't match the checksum Hugging Face gives for it, so it was damaged on "
                                "the way and has been deleted. Press the button again to download it again.")
        part.replace(target)
        done_before += size or 0
    progress(" ".join(["Download complete."] + notes), 1.0)
    files = {f: dest / f for _, f in preset.all_files()}
    mm = files.get(preset.mmproj[1]) if preset.mmproj else None
    return files[preset.files[0]], mm


def download_folder(preset: Preset, models_root: Path, dirs: list[Path]) -> Path:
    """Where to download: next to the model if it's already on the PC (only the vision file is
    missing), else a new folder for it in the models folder."""
    model, _, _ = model_path(preset, dirs)
    return model.parent if model else models_root / preset.folder


def _part_size(target: Path) -> int:
    part = target.with_name(target.name + ".part")
    try:
        return part.stat().st_size
    except OSError:
        return 0
