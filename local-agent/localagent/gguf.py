"""Read a GGUF model file's header (no weights are loaded) and plan how to fit it on the GPU."""

from __future__ import annotations

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
_KEEP_ARRAYS = re.compile(r"\.(head_count_kv|head_count|feed_forward_length)$")


def read_header(path: Path) -> tuple[dict, list[tuple[str, int]], int]:
    """Returns (metadata, [(tensor name, byte size)], data start)."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        r = _Reader(f)
        if f.read(4) != b"GGUF":
            raise GGUFError(f"{path.name} is not a GGUF file")
        version = r.take("<I")
        if version < 2:
            raise GGUFError("GGUF version 1 is too old")
        n_tensors, n_kv = r.take("<Q"), r.take("<Q")
        meta = {}
        for _ in range(n_kv):
            key = r.string()
            t = r.take("<I")
            meta[key] = r.value(t, bool(_KEEP_ARRAYS.search(key)))
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
    kv_bytes_per_token_f16: float = 0.0
    expert_bytes: dict = field(default_factory=dict)    # layer -> bytes of MoE expert weights
    layer_bytes: dict = field(default_factory=dict)     # layer -> all bytes in that layer
    has_mtp: bool = False
    quant: str = ""

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
    k_len = int(g("attention.key_length", 0) or (emb // n_head if n_head else 0))
    v_len = int(g("attention.value_length", 0) or k_len)
    if isinstance(kv, list):
        heads_total = sum(int(x) for x in kv)
    else:
        interval = int(g("full_attention_interval", 0) or 0)
        attn_layers = info.n_layers // interval if interval > 1 else info.n_layers
        heads_total = int(kv or 0) * attn_layers
    info.kv_bytes_per_token_f16 = heads_total * (k_len + v_len) * 2.0

    for name, size in tensors:
        lm = re.match(r"blk\.(\d+)\.", name)
        if not lm:
            continue
        layer = int(lm.group(1))
        info.layer_bytes[layer] = info.layer_bytes.get(layer, 0) + size
        if "_exps" in name or "_chexps" in name:
            info.expert_bytes[layer] = info.expert_bytes.get(layer, 0) + size
    nextn = any(k.endswith("nextn_predict_layers") and v for k, v in meta.items())
    info.has_mtp = nextn or any("nextn" in n or ".mtp" in n for n, _ in tensors)
    return info


# ---------------------------------------------------------------------- GPU
def gpu_info() -> tuple[str, float, float] | None:
    """(name, total GB, used GB) of the first NVIDIA GPU, via nvidia-smi."""
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,memory.used",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0).stdout
        name, total, used = [x.strip() for x in out.splitlines()[0].split(",")]
        return name, float(total) / 1024, float(used) / 1024
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


GPU_BW, RAM_BW = 960e9, 70e9   # RTX 5080 memory bandwidth, typical dual-channel DDR5 (bytes/s)


def plan(info: ModelInfo, ctx: int, kv_type: str = "q8_0", vram_gb: float = 16.0,
         extra_gb: float = 0.0, reserve_gb: float = 1.4) -> Plan:
    """Work out how much goes on the GPU. reserve_gb covers Windows, the desktop and CUDA itself.
    extra_gb is anything else on the GPU (vision projector, draft model)."""
    kv_gb = info.kv_bytes_per_token_f16 / 2.0 * KV_BYTES.get(kv_type, 2.0) * ctx / GiB
    compute_gb = 0.7 + (0.5 if info.is_moe else 0.0)
    budget = vram_gb - reserve_gb - compute_gb - kv_gb - extra_gb
    weights = info.file_bytes / GiB
    notes = []
    n_layers = max(1, info.n_layers)
    if weights <= budget:
        p = Plan(999, 0, kv_gb, weights + kv_gb + compute_gb + extra_gb, 0.0, True, 0.0)
        active = _active_gb(info, 0)
        p.tokens_per_sec = _speed(active, 0.0)
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
    p = Plan(ngl, 0, kv_gb, weights - cpu_gb + kv_gb + compute_gb + extra_gb, cpu_gb, False, 0.0, notes)
    p.tokens_per_sec = _speed(weights - cpu_gb, cpu_gb)
    p.notes.append(f"{n_layers - ngl} of {n_layers} layers run on the CPU, which is much slower. A smaller "
                   "quant (e.g. IQ4_XS or Q3_K_M) or a shorter context would fit it fully on the GPU.")
    return p


def _active_gb(info: ModelInfo, n_cpu_moe: int) -> float:
    total = info.file_bytes / GiB
    if not info.is_moe:
        return total
    exp = sum(info.expert_bytes.values()) / GiB
    frac = info.n_experts_used / info.n_experts if info.n_experts else 1
    return total - exp + exp * frac


def _speed(gpu_gb: float, cpu_gb: float) -> float:
    """Very rough tokens/second: generation is limited by how fast weights can be read."""
    t = gpu_gb * GiB / GPU_BW + cpu_gb * GiB / RAM_BW
    return 0.65 / t if t > 0 else 0.0
