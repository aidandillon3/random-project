"""Find and download the vision file (mmproj) that lets a model see images.

Fine-tunes usually ship without one, but they keep the base model's image "adapter", so the
base model's mmproj works for them. This works out the base model from the GGUF metadata and
the file name (e.g. "Swift-1.5-Qwen3.8-27B-...gguf" -> "Qwen3.8-27B"), searches Hugging Face for
GGUF repos of that base that include an mmproj file, checks the file's header fits the model,
and downloads it next to the model, where the launcher picks it up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import requests

from .gguf import GGUFError, ModelInfo, projection_dim, read_meta_bytes

HF = "https://huggingface.co"
# Uploaders whose mmproj files are made with llama.cpp's own converter, best first.
TRUSTED = ["ggml-org", "unsloth", "bartowski", "lmstudio-community", "Qwen", "google", "mistralai"]
FAMILY = re.compile(
    r"((?:qwen|gemma|llama|mistral|ministral|magistral|devstral|pixtral|glm|internvl|minicpm|phi|kimi|smolvlm)"
    r"[\w.]*?(?:-(?:vl|omni|small|medium|large|\d+(?:\.\d+)?))*-\d+(?:\.\d+)?[bB](?:-A\d+(?:\.\d+)?[bB])?)"
    r"(?![a-zA-Z0-9])", re.I)


@dataclass
class Candidate:
    repo: str
    file: str
    size: int = 0
    note: str = ""
    base: str = HF
    base_name: str = ""

    @property
    def url(self) -> str:
        return f"{self.base}/{self.repo}/resolve/main/{self.file}"

    @property
    def mb(self) -> int:
        return round(self.size / 1024 ** 2)


def base_names(info: ModelInfo | None, filename: str) -> list[str]:
    """Names to search for, most specific first."""
    texts = [Path(filename).stem]
    meta = info.meta if info else {}
    for k in ("general.base_model.0.repo_url", "general.base_model.0.name", "general.name",
              "general.finetune", "general.basename"):
        if meta.get(k):
            texts.append(str(meta[k]).replace(" ", "-"))
    if meta.get("general.basename") and meta.get("general.size_label"):
        texts.append(f"{meta['general.basename']}-{meta['general.size_label']}")
    out: list[str] = []
    for t in texts:
        for m in FAMILY.findall(t):
            if m.lower() not in (o.lower() for o in out):
                out.append(m)
    return out


def _quant_rank(name: str) -> int:
    """Q8_0 is about half the size of F16 with practically the same picture quality."""
    low = name.lower()
    for i, q in enumerate(["q8_0", "f16", "bf16", "f32"]):
        if q in low:
            return i
    return 5


def _get(url, session, **kw):
    r = session.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r


def search(info: ModelInfo | None, model_path: Path, base: str = HF, session=None,
           progress=lambda msg: None) -> list[Candidate]:
    session = session or requests.Session()
    names = base_names(info, model_path.name)
    if not names:
        raise RuntimeError("Couldn't tell which base model this is from its name. Look for a file called "
                           "mmproj-*.gguf on the model's Hugging Face page and put it next to the model.")
    seen, found = set(), []
    for name in names:
        progress(f"Searching Hugging Face for {name}...")
        repos = _get(f"{base}/api/models", session,
                     params={"search": name, "filter": "gguf", "sort": "downloads", "direction": "-1",
                             "limit": 40}).json()
        want = name.lower()
        repos = [r for r in repos if want in r["id"].split("/")[-1].lower() and r["id"] not in seen]
        org = lambda rid: TRUSTED.index(rid.split("/")[0]) if rid.split("/")[0] in TRUSTED else len(TRUSTED)
        repos.sort(key=lambda r: (org(r["id"]), -(r.get("downloads") or 0)))
        for r in repos[:8]:
            seen.add(r["id"])
            try:
                detail = _get(f"{base}/api/models/{r['id']}", session, params={"blobs": "true"}).json()
            except requests.RequestException:
                continue
            files = [s for s in detail.get("siblings", [])
                     if "mmproj" in s["rfilename"].lower() and s["rfilename"].lower().endswith(".gguf")]
            files.sort(key=lambda s: _quant_rank(s["rfilename"]))
            for s in files[:2]:
                found.append(Candidate(r["id"], s["rfilename"], int(s.get("size") or 0), "", base, name))
        if found:
            break
    return found


def check(c: Candidate, info: ModelInfo | None, session=None) -> tuple[bool, str]:
    """Read the start of the file and check it's a vision projector sized for this model."""
    session = session or requests.Session()
    try:
        r = session.get(c.url, headers={"Range": "bytes=0-4194303"}, timeout=60, stream=True)
        r.raise_for_status()
        data = r.raw.read(4 * 1024 * 1024, decode_content=True)
        r.close()
        meta = read_meta_bytes(data)
    except (requests.RequestException, GGUFError) as e:
        return False, f"couldn't read it ({e})"
    if meta.get("clip.has_vision_encoder") is False:
        return False, "it has no vision part (audio only)"
    proj = projection_dim(meta)
    emb = info.embedding_length if info else 0
    if proj and emb and proj % emb != 0:
        return False, f"it's made for a different model size ({proj} vs {emb})"
    return True, "fits this model" if proj and emb else "looks right (size not checked)"


def find(info: ModelInfo | None, model_path: Path, base: str = HF, progress=lambda msg: None) -> Candidate | None:
    session = requests.Session()
    for c in search(info, model_path, base, session, progress):
        progress(f"Checking {c.repo}/{c.file}...")
        ok, why = check(c, info, session)
        c.note = why
        if ok:
            return c
    return None


def local_name(c: Candidate) -> str:
    """The file name to save it as. Generic names such as unsloth's mmproj-F16.gguf get the base model's
    name, so they don't overwrite another model's vision file in the same folder."""
    name = Path(c.file).name
    if c.base_name and c.base_name.lower() not in name.lower():
        name = f"mmproj-{c.base_name}-{re.sub(r'^mmproj[-_.]?', '', name, flags=re.I)}"
    return name


def download(c: Candidate, folder: Path, progress=lambda msg, frac: None) -> Path:
    dest = folder / local_name(c)
    tmp = dest.with_suffix(".part")
    with requests.get(c.url, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length") or c.size or 0)
        done = 0
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if total:
                    progress(f"Downloading {dest.name}...", done / total)
    tmp.replace(dest)
    progress(f"Saved {dest.name}", 1.0)
    return dest
