"""Model picks and speed tips, shown in the app's Guide page (and mirrored in the README)."""

from __future__ import annotations

# Sampling the model makers recommend. Matched by name, first hit wins.
SAMPLING = [
    ("gpt-oss", dict(temperature=1.0, top_p=1.0, top_k=0, min_p=0.0)),
    ("qwen", dict(temperature=0.6, top_p=0.95, top_k=20, min_p=0.0)),
    ("gemma", dict(temperature=1.0, top_p=0.95, top_k=64, min_p=0.0)),
    ("glm", dict(temperature=0.7, top_p=0.95, top_k=40, min_p=0.0)),
    ("mistral", dict(temperature=0.15, top_p=1.0, top_k=0, min_p=0.0)),
    ("devstral", dict(temperature=0.15, top_p=1.0, top_k=0, min_p=0.0)),
]


def sampling_for(model_name: str) -> dict | None:
    low = (model_name or "").lower()
    for key, vals in SAMPLING:
        if key in low:
            return vals
    return None


GUIDE = """
# Getting the best out of a 16 GB card

Your PC: **RTX 5080 (16 GB VRAM) + 64 GB RAM**. The GPU is very fast but small, the RAM is big but
about 12x slower. Everything below is about putting the right parts of a model in the right place.

## Recommended models

| Model | Type | Sees the screen | How it runs on your PC | Pick it for |
|---|---|---|---|---|
| **Qwen3.6-35B-A3B** (Q4_K_M) | MoE, 3B active | Yes | ~21 GB: attention on GPU, some experts in RAM. Fast. | **Best all-rounder for this app.** Quick tool loops and screen work |
| **Qwen3.8-27B** (IQ4_XS or Q3_K_M) | Dense | Check the model page | Q4_K_M (~18 GB) spills to RAM and slows down; IQ4_XS/Q3 fit | The smartest model that runs locally; slower |
| **Qwen3.6-27B** (IQ4_XS, MTP version) | Dense | Yes | ~14 GB, fits on the GPU | Hard coding and reasoning; turn on MTP for ~2x speed when you don't need vision |
| **gpt-oss-20b** (MXFP4) | MoE, 3.6B active | No | ~13 GB, fully on the GPU. Very fast | Fast text and coding work with strong tool calling |

Get them from Hugging Face (search the name plus "GGUF"; bartowski and unsloth upload good ones).
For screen vision, also download the matching **mmproj** file into the same folder.

How sure is this list? Qwen3.6/3.8 were checked against model pages in October 2026; the speeds
are estimates from memory bandwidth, not measurements on your PC. New models come out monthly, so
any model that supports tool calling will work here.

## The speed tricks, and which ones this app turns on

| Trick | What it does | Here |
|---|---|---|
| **Flash attention** (`-fa on`) | Faster attention and less memory | Always on |
| **KV cache quantization** (`-ctk/-ctv q8_0`) | Halves the memory the context uses, almost no quality loss | On (q8_0). q4_0 doubles context again with some loss |
| **MoE expert offload** (`--n-cpu-moe`, `--fit`) | Keeps the small, always-used parts on the GPU and moves rarely-used experts to RAM. A 35B MoE then runs nearly as fast as if it fit | Automatic |
| **Auto-fit** (`--fit on`) | llama.cpp measures your free VRAM and places layers itself | Automatic placement uses it |
| **MTP speculative decoding** (`--spec-type draft-mtp`) | Models with built-in "multi-token prediction" layers guess several tokens and check them at once: about 2x faster text | Speed-up option. Turns vision off |
| **Draft-model speculation** (`-md`) | A tiny model of the same family guesses tokens for the big one | Speed-up option |
| **N-gram speculation** (`--spec-type ngram-mod`) | Reuses text already in the conversation; great when editing code, which repeats a lot | Speed-up option, no extra memory |
| **Prompt caching** (`--cache-reuse`, one slot `-np 1`) | The server keeps the conversation it already read, so each new step only reads the new part. The app only rewrites old messages occasionally so the cache stays valid | Always on |
| **Quantization choice** | Q4_K_M is the sweet spot; IQ4_XS is ~10% smaller with similar quality; below Q3 models get noticeably dumber | Pick when downloading |

What doesn't help much: running two models at once on 16 GB, very long contexts (each token of
context costs memory and slows every step), and Q2 quants of big models.

## Making the model smarter

- **Thinking on** (Settings) for hard tasks: slower, but plans better. Off for quick clicks.
- **Recommended sampling**: Settings has a button that sets the model maker's values.
- **Context**: 32K is a good default; 64K for big codebases (uses more memory).
- **AGENT.md**: put instructions in a file called AGENT.md in your work folder and the AI reads
  them every time (like a briefing).
- **Memory**: the AI saves lasting facts with its remember tool (data/memory.md, you can edit it).
"""
