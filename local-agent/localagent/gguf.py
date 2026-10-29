"""Read a GGUF model file's header (no weights are loaded) and plan how to fit it on the GPU."""

from __future__ import annotations

import os
import re
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

GiB = 1024 ** 3
_SCALAR = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
KV_BYTES = {"f16": 2.0, "bf16": 2.0, "f32": 4.0, "q8_0": 34 / 32, "q5_1": 24 / 32, "q5_0": 22 / 32,
            "q4_1": 20 / 32, "q4_0": 18 / 32, "iq4_nl": 18 / 32}


class GGUFError(Exception):
    pass


class _Reader:
    def __init__(self, f):
        self.f = f

    def take(self, fmt):
        size = struct.calcsize(fmt)
        data = self.f.read(size)
        if len(data) < size:
            raise GGUFError("file ended early")
        return struct.unpack(fmt, data)[0]

    def string(self):
        n = self.take("<Q")
        if n > 1 << 24:
            raise GGUFError("bad string length")
        return self.f.read(n).decode("utf-8", "replace")

    def value(self, t, keep_arrays: bool):
        if t in _SCALAR:
            return self.take(_SCALAR[t])
        if t == 8:
            return self.string()
        if t == 9:
            sub, n = self.take("<I"), self.take("<Q")
            if sub in _SCALAR and not keep_arrays:
                self.f.seek(struct.calcsize(_SCALAR[sub]) * n, 1)
                return None
            if sub == 8 and not keep_arrays:
                for _ in range(n):
                    self.f.seek(self.take("<Q"), 1)
                return None
            return [self.value(sub, True) for _ in range(n)]
        raise GGUFError(f"unknown value type {t}")


# arrays we actually want (small, per-layer numbers)
_KEEP_ARRAYS = re.compile(r"\.(head_count_kv|head_count|feed_forward_length|recurrent_layers|sliding_window_pattern)$")


def _read_meta(f, label: str) -> tuple[_Reader, dict, int]:
    r = _Reader(f)
    if f.read(4) != b"GGUF":
        raise GGUFError(f"{label} is not a GGUF file")
    version = r.take("<I")
    if version < 2:
        raise GGUFError("GGUF version 1 is too old")
    n_tensors, n_kv = r.take("<Q"), r.take("<Q")
    meta = {}
    for _ in range(n_kv):
        key = r.string()
        t = r.take("<I")
        meta[key] = r.value(t, bool(_KEEP_ARRAYS.search(key)))
    return r, meta, n_tensors


def read_meta_bytes(data: bytes) -> dict:
    """Metadata from the first part of a GGUF file (e.g. a partial download)."""
    import io

    return _read_meta(io.BytesIO(data), "This file")[1]


def read_meta(path: Path) -> dict:
    """Just the metadata of a GGUF file (no tensor list)."""
    with open(path, "rb") as f:
        return _read_meta(f, Path(path).name)[1]


def projection_dim(meta: dict) -> int | None:
    """The width a vision file (mmproj) hands to the model: it has to fit the model's embedding_length."""
    v = meta.get("clip.vision.projection_dim") or next(
        (v for k, v in meta.items() if k.endswith("projection_dim") and isinstance(v, int)), None)
    return int(v) if v else None


# Strata's own data (its MTP draft layer is a .gguf but not a chat model) and unfinished downloads
_SKIP_MODEL_DIRS = {"strata-data", "mtp"}


def gguf_files(root: Path):
    """Every .gguf file under root, except in Strata's data folder and unfinished (.part) folders."""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in _SKIP_MODEL_DIRS and not d.lower().endswith(".part")]
        for name in filenames:
            if name.lower().endswith(".gguf"):
                yield Path(dirpath) / name


def read_header(path: Path) -> tuple[dict, list[tuple[str, int]], int]:
    """Returns (metadata, [(tensor name, byte size)], data start)."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        r, meta, n_tensors = _read_meta(f, path.name)
        infos = []
        for _ in range(n_tensors):
            name = r.string()
            n_dims = r.take("<I")
            for _ in range(n_dims):
                r.take("<Q")
            r.take("<I")              # type
            infos.append((name, r.take("<Q")))
        align = int(meta.get("general.alignment") or 32)
        pos = f.tell()
        data_start = (pos + align - 1) // align * align
    order = sorted(infos, key=lambda x: x[1])
    sizes = {}
    for i, (name, off) in enumerate(order):
        end = order[i + 1][1] if i + 1 < len(order) else size - data_start
        sizes[name] = max(0, end - off)
    return meta, [(n, sizes[n]) for n, _ in infos], data_start


SPLIT = re.compile(r"-(\d{5})-of-(\d{5})\.gguf$", re.I)
# llama.cpp's sliding-window layout for models whose files don't say (n: every n-th layer sees everything)
_SWA_PATTERN = {"gemma2": 2, "gemma3": 6, "gpt-oss": 2}


@dataclass
class ModelInfo:
    path: Path
    file_bytes: int
    arch: str = "?"
    name: str = ""
    n_layers: int = 0
    n_experts: int = 0
    n_experts_used: int = 0
    ctx_train: int = 0
    kv_bytes_per_token_f16: float = 0.0     # layers that keep the whole context
    swa_kv_bytes_per_token_f16: float = 0.0  # sliding-window layers: they keep only the last swa_window tokens
    swa_window: int = 0
    expert_bytes: dict = field(default_factory=dict)    # layer -> bytes of MoE expert weights
    layer_bytes: dict = field(default_factory=dict)     # layer -> all bytes in that layer
    has_mtp: bool = False
    lazy_bytes: int = 0       # huge lookup tables (n-gram / per-layer embeddings) read from disk on demand
    quant: str = ""
    embedding_length: int = 0
    meta: dict = field(default_factory=dict, repr=False)

    @property
    def is_moe(self) -> bool:
        return self.n_experts > 1 and bool(self.expert_bytes)

    @property
    def gb(self) -> float:
        return self.file_bytes / GiB

    def summary(self) -> str:
        bits = [f"{self.arch}", f"{self.gb:.1f} GB"]
        if self.quant:
            bits.append(self.quant)
        bits.append(f"{self.n_layers} layers")
        if self.is_moe:
            bits.append(f"MoE: {self.n_experts} experts, {self.n_experts_used} active")
        if self.ctx_train:
            bits.append(f"trained context {self.ctx_train:,}")
        if self.has_mtp:
            bits.append("has MTP speed-up layers")
        return ", ".join(bits)


def model_info(path: str | Path) -> ModelInfo:
    path = Path(path)
    parts = [path]
    m = SPLIT.search(path.name)
    if m:
        total = int(m.group(2))
        stem = path.name[:m.start()]
        parts = [path.with_name(f"{stem}-{i:05d}-of-{total:05d}.gguf") for i in range(1, total + 1)]
        parts = [p for p in parts if p.exists()]
    meta, tensors = {}, []
    for p in parts:
        md, ts, _ = read_header(p)
        meta = meta or md
        tensors += ts
    arch = str(meta.get("general.architecture", "?"))
    g = lambda k, d=0: meta.get(f"{arch}.{k}", d)
    info = ModelInfo(path=path, file_bytes=sum(p.stat().st_size for p in parts), arch=arch,
                     name=str(meta.get("general.name", path.stem)))
    info.n_layers = int(g("block_count", 0) or 0)
    info.n_experts = int(g("expert_count", 0) or 0)
    info.n_experts_used = int(g("expert_used_count", 0) or 0)
    info.ctx_train = int(g("context_length", 0) or 0)
    q = re.search(r"(IQ\d_\w+|Q\d_K_[SML]|Q\d_K|Q\d_\d|Q\d_K_XL|MXFP4|BF16|F16|Q8_0)", path.name, re.I)
    info.quant = q.group(1).upper() if q else ""

    # KV cache size per token
    n_head = g("attention.head_count", 0)
    n_head = max(n_head) if isinstance(n_head, list) else int(n_head or 0)
    kv = g("attention.head_count_kv", n_head)
    emb = int(g("embedding_length", 0) or 0)
    info.embedding_length = emb
    info.meta = meta
    k_len = int(g("attention.key_length", 0) or (emb // n_head if n_head else 0))
    v_len = int(g("attention.value_length", 0) or k_len)
    n_all = info.n_layers                                   # block_count includes the MTP layers at the end
    n_main = max(0, n_all - int(g("nextn_predict_layers", 0) or 0))
    heads = [int(x) for x in kv] if isinstance(kv, list) else [int(kv or 0)] * n_all
    # Which layers keep a KV cache. Hybrid models (Qwen3.5 and later) keep it only in their full-attention
    # layers; llama.cpp reads attention.recurrent_layers first, else every full_attention_interval-th layer
    # (4 when the key is missing). The MTP layers are attention layers (their cache is used with MTP on).
    rec = g("attention.recurrent_layers", None)
    if isinstance(rec, list) and rec:
        attn = [i >= len(rec) or not rec[i] for i in range(n_all)]
    else:
        interval = int(g("full_attention_interval", 0) or 0) or (4 if arch.startswith(("qwen35", "qwen3next")) else 1)
        attn = [i >= n_main or (i + 1) % interval == 0 for i in range(n_all)]
    # sliding-window layers only keep the last few thousand tokens
    window = int(g("attention.sliding_window", 0) or 0)
    pattern = g("attention.sliding_window_pattern", _SWA_PATTERN.get(arch))
    if window and isinstance(pattern, list):
        swa = [i < len(pattern) and bool(pattern[i]) for i in range(n_all)]
    elif window and pattern is not None:
        n = int(pattern)
        swa = [i < n_main and (n == 0 or i % n < n - 1) for i in range(n_all)]
    else:
        swa = [False] * n_all
    full_heads = sum(heads[i] for i in range(min(n_all, len(heads))) if attn[i] and not swa[i])
    swa_heads = sum(heads[i] for i in range(min(n_all, len(heads))) if attn[i] and swa[i])
    info.kv_bytes_per_token_f16 = full_heads * (k_len + v_len) * 2.0
    if swa_heads:
        k_swa = int(g("attention.key_length_swa", 0) or k_len)
        v_swa = int(g("attention.value_length_swa", 0) or k_swa)
        info.swa_kv_bytes_per_token_f16 = swa_heads * (k_swa + v_swa) * 2.0
        info.swa_window = window

    for name, size in tensors:
        lm = re.match(r"blk\.(\d+)\.", name)
        if not lm:
            # llama.cpp's --lazy-mode (on by default for tables over 4 GiB) leaves these on disk
            if size > LAZY_MIN and "embd" in name:
                info.lazy_bytes += size
            continue
        layer = int(lm.group(1))
        info.layer_bytes[layer] = info.layer_bytes.get(layer, 0) + size
        if "_exps" in name or "_chexps" in name:
            info.expert_bytes[layer] = info.expert_bytes.get(layer, 0) + size
    nextn = any(k.endswith("nextn_predict_layers") and v for k, v in meta.items())
    info.has_mtp = nextn or any("nextn" in n or ".mtp" in n for n, _ in tensors)
    return info


# ---------------------------------------------------------------------- GPU
@dataclass
class Gpu:
    index: int          # nvidia-smi's number (PCI bus order; CUDA uses it too with CUDA_DEVICE_ORDER=PCI_BUS_ID)
    uuid: str
    name: str
    total_gb: float
    used_gb: float
    cc: float = 0.0     # CUDA compute capability (12.0 = RTX 50 series); 0 when the driver doesn't say
    display: bool | None = None     # it shows the desktop (nvidia-smi display_active); None when it doesn't say


def _smi(fields: str) -> str:
    return subprocess.run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                          capture_output=True, text=True, timeout=10,
                          creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0).stdout


def list_gpus() -> list[Gpu]:
    """Every NVIDIA GPU nvidia-smi sees, in its order. [] when there's none (or no nvidia-smi)."""
    for fields in ("index,uuid,name,memory.total,memory.used,compute_cap,display_active",
                   "index,uuid,name,memory.total,memory.used,compute_cap", "index,uuid,name,memory.total,memory.used"):
        try:
            out = _smi(fields)
            gpus = []
            for line in out.splitlines():
                v = [x.strip() for x in line.split(",")]
                if len(v) < 5:
                    continue
                cc = float(v[5]) if len(v) > 5 and re.fullmatch(r"\d+(\.\d+)?", v[5]) else 0.0
                shows = {"enabled": True, "disabled": False}.get(v[6].lower()) if len(v) > 6 else None
                gpus.append(Gpu(int(v[0]), v[1], v[2], float(v[3]) / 1024, float(v[4]) / 1024, cc, shows))
            if gpus:
                return gpus
        except Exception:
            continue
    return []


def strongest(gpus: list[Gpu]) -> list[Gpu]:
    """Best first: the newest kind of card, then the most VRAM."""
    return sorted(gpus, key=lambda g: (-g.cc, -g.total_gb, g.index))


def chosen_gpus(choice: str, gpus: list[Gpu]) -> list[Gpu]:
    """The GPUs a model uses, best first (the first one is CUDA's device 0, where the main work goes).
    choice: "" = the strongest one only, "all" = every GPU, otherwise GPU UUIDs joined by commas. A choice
    naming a GPU that isn't there any more falls back to the strongest one."""
    if not gpus:
        return []
    if choice == "all":
        return strongest(gpus)
    picked = [g for g in gpus if g.uuid in {c.strip() for c in choice.split(",") if c.strip()}]
    return strongest(picked) if picked else strongest(gpus)[:1]


def gpu_env(choice: str, gpus: list[Gpu]) -> dict[str, str]:
    """What a model server needs in its environment to see only the chosen GPUs, best one first."""
    use = chosen_gpus(choice, gpus)
    if not use:
        return {}
    return {"CUDA_DEVICE_ORDER": "PCI_BUS_ID", "CUDA_VISIBLE_DEVICES": ",".join(str(g.index) for g in use)}


def monitors_on(choice: str, gpus: list[Gpu]) -> bool:
    """Whether the desktop is on a GPU the model uses, so it needs VRAM left free there. When no card says it shows
    the desktop (an old driver, or monitors on the motherboard), it's taken that one does: the safe guess."""
    if not any(g.display for g in gpus):
        return True
    return any(g.display for g in chosen_gpus(choice, gpus))


def gpu_info(choice: str = "", gpus: list[Gpu] | None = None) -> tuple[str, float, float] | None:
    """(name, total GB, used GB) of the GPU(s) a model uses, via nvidia-smi. Several add up."""
    use = chosen_gpus(choice, list_gpus() if gpus is None else gpus)
    if not use:
        return None
    return " + ".join(g.name for g in use), sum(g.total_gb for g in use), sum(g.used_gb for g in use)


def driver_version() -> int | None:
    """The NVIDIA driver's major version (e.g. 581), via nvidia-smi."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0).stdout
        return int(out.splitlines()[0].strip().split(".")[0])
    except Exception:
        return None


@dataclass
class Plan:
    gpu_layers: int
    n_cpu_moe: int
    kv_gb: float
    gpu_gb: float
    ram_gb: float
    fits_fully: bool
    tokens_per_sec: float
    notes: list[str] = field(default_factory=list)
    ctx_fit: int = 0          # the longest context that still fits fully on the GPU (0 = none)


LAZY_MIN = 4 * 1024 ** 3
GPU_BW, RAM_BW = 960e9, 70e9   # RTX 5080 memory bandwidth, typical dual-channel DDR5 (bytes/s)


def plan(info: ModelInfo, ctx: int, kv_type: str = "q8_0", vram_gb: float = 16.0,
         extra_gb: float = 0.0, reserve_gb: float = 1.4) -> Plan:
    """Work out how much goes on the GPU. reserve_gb covers Windows, the desktop and CUDA itself.
    extra_gb is anything else on the GPU (vision projector, draft model)."""
    kv_gb = kv_cache_gb(info, ctx, kv_type)
    compute_gb = 0.7 + (0.5 if info.is_moe else 0.0)
    budget = vram_gb - reserve_gb - compute_gb - kv_gb - extra_gb
    weights = (info.file_bytes - info.lazy_bytes) / GiB
    notes = []
    if info.lazy_bytes:
        notes.append(f"{info.lazy_bytes / GiB:.0f} GB of n-gram/embedding tables stay on the drive and are read as "
                     "needed (best on an SSD).")
    n_layers = max(1, info.n_layers)
    fit = context_that_fits(info, kv_type, vram_gb - reserve_gb - compute_gb - extra_gb - weights)
    if weights <= budget:
        p = Plan(999, 0, kv_gb, weights + kv_gb + compute_gb + extra_gb, 0.0, True, 0.0, notes, fit)
        active = _active_gb(info, 0)
        p.tokens_per_sec = _speed(active, 0.0)
        if fit >= ctx * 2 and fit >= 16384:
            p.notes.append(f"There's room for about {fit // 1024}K tokens of context on the GPU with this cache "
                           f"type (now {ctx // 1024}K). More context leaves the model more room to think "
                           "before the chat has to be summarized.")
        return p
    if info.is_moe:
        layers = sorted(info.expert_bytes)
        moved, n = 0.0, 0
        for layer in layers:
            if weights - moved <= budget:
                break
            moved += info.expert_bytes[layer] / GiB
            n = layer + 1
        if weights - moved <= budget:
            gpu = weights - moved
            p = Plan(999, n, kv_gb, gpu + kv_gb + compute_gb + extra_gb, moved, False, 0.0)
            frac = info.n_experts_used / info.n_experts if info.n_experts else 1
            p.tokens_per_sec = _speed(_active_gb(info, n) - moved * frac, moved * frac)
            notes.append(f"Estimate: the experts of about {n} layers go to system RAM; everything else stays on the GPU.")
            p.notes = notes
            return p
        notes.append("Even with every expert in system RAM this does not fit; some whole layers move too.")
    per_layer = (sum(info.layer_bytes.values()) / GiB / n_layers) if info.layer_bytes else weights / n_layers
    other = max(0.0, weights - per_layer * n_layers)
    ngl = int(max(0, (budget - other) // per_layer)) if per_layer else 0
    ngl = min(ngl, n_layers)
    cpu_gb = (n_layers - ngl) * per_layer
    p = Plan(ngl, 0, kv_gb, weights - cpu_gb + kv_gb + compute_gb + extra_gb, cpu_gb, False, 0.0, notes, fit)
    p.tokens_per_sec = _speed(weights - cpu_gb, cpu_gb)
    shorter = f"a context of {fit // 1024}K" if fit >= 8192 else "a shorter context"
    p.notes.append(f"{n_layers - ngl} of {n_layers} layers run on the CPU, which is much slower. A smaller "
                   f"quant (e.g. IQ4_XS or Q3_K_M) or {shorter} would fit it fully on the GPU.")
    return p


SWA_EXTRA = 2048     # llama.cpp keeps a sliding-window layer's window plus one batch (-ub) of tokens


def kv_cache_gb(info: ModelInfo, ctx: int, kv_type: str) -> float:
    per = KV_BYTES.get(kv_type, 2.0) / 2.0
    swa = info.swa_kv_bytes_per_token_f16 * min(ctx, info.swa_window + SWA_EXTRA)
    return (info.kv_bytes_per_token_f16 * ctx + swa) * per / GiB


def context_that_fits(info: ModelInfo, kv_type: str, free_gb: float) -> int:
    """The longest context (a multiple of 8K, at most what the model was trained on) whose cache fits
    in free_gb."""
    per = KV_BYTES.get(kv_type, 2.0) / 2.0
    per_token = info.kv_bytes_per_token_f16 * per
    free = free_gb * GiB - info.swa_kv_bytes_per_token_f16 * (info.swa_window + SWA_EXTRA) * per
    if free <= 0 or per_token <= 0:
        return 0
    n = int(free / per_token) // 8192 * 8192
    return min(n, info.ctx_train) if info.ctx_train else n


def _active_gb(info: ModelInfo, n_cpu_moe: int) -> float:
    total = (info.file_bytes - info.lazy_bytes) / GiB
    if not info.is_moe:
        return total
    exp = sum(info.expert_bytes.values()) / GiB
    frac = info.n_experts_used / info.n_experts if info.n_experts else 1
    return total - exp + exp * frac


def _speed(gpu_gb: float, cpu_gb: float) -> float:
    """Very rough tokens/second: generation is limited by how fast weights can be read."""
    t = gpu_gb * GiB / GPU_BW + cpu_gb * GiB / RAM_BW
    return 0.65 / t if t > 0 else 0.0
