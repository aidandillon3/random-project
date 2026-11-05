"""Model picks and speed tips, shown in the app's Guide page (and mirrored in the README)."""

from __future__ import annotations

# Sampling the model makers recommend, for thinking and for non-thinking answers. Matched by
# name, first hit wins, so specific names come before family names.
_QWEN38_THINK = dict(temperature=1.0, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=0.0)
_QWEN38_FAST = dict(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, presence_penalty=1.5)
SAMPLING = [
    # Qwen3.8 model page, October 2026
    ("qwen3.8", _QWEN38_THINK, _QWEN38_FAST),
    ("swift-1.5", _QWEN38_THINK, _QWEN38_FAST),      # Strata calls the Swift Flash Next model "swift-1.5-iq2_xs"
    # Ornith 1.5 model card (2026-10-20): its "precise coding" values (the general ones add presence_penalty 1.5
    # at temperature 1.0), the closest to what this app asks of it; before "qwen" (it is Qwen3.5-based)
    ("ornith", dict(temperature=0.6, top_p=0.95, top_k=20, min_p=0.0, presence_penalty=0.0), None),
    # Qwen3 family (model cards): thinking vs. non-thinking
    ("qwen", dict(temperature=0.6, top_p=0.95, top_k=20, min_p=0.0),
             dict(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0)),
    ("gpt-oss", dict(temperature=1.0, top_p=1.0, top_k=0, min_p=0.0), None),
    ("gemma", dict(temperature=1.0, top_p=0.95, top_k=64, min_p=0.0), None),
    # GLM-5.3 / GLM-5.3-Flash (Unsloth's guides, 2026): it always thinks
    ("glm-5.3", dict(temperature=1.0, top_p=0.95, top_k=0, min_p=0.01), None),
    ("glm", dict(temperature=0.7, top_p=0.95, top_k=40, min_p=0.0), None),
    ("mistral", dict(temperature=0.15, top_p=1.0, top_k=0, min_p=0.0), None),
    ("devstral", dict(temperature=0.15, top_p=1.0, top_k=0, min_p=0.0), None),
]


def sampling_for(model_name: str, thinking: bool = True) -> dict | None:
    """The maker's recommended sampling for this model, in thinking or non-thinking mode."""
    low = (model_name or "").lower()
    for key, think, fast in SAMPLING:
        if key in low:
            return dict(think if thinking or fast is None else fast)
    return None


GUIDE = """
# Getting the best out of a 16 GB card

Your PC: **RTX 5080 (16 GB VRAM) + 64 GB RAM**. The GPU is very fast but small, the RAM is big but
about 12x slower. Everything below is about putting the right parts of a model in the right place.

## Recommended models

| Model | Type | Sees images | How it runs on your PC | Pick it for |
|---|---|---|---|---|
| **Qwen3.6-35B-A3B** (Q4_K_M) | MoE, 3B active | Yes | ~21 GB: attention on GPU, some experts in RAM. Fast. | **Best all-rounder for this app.** Quick tool and test loops |
| **Qwen3.8-27B** (IQ4_XS or Q3_K_M) | Dense | Check the model page | Q4_K_M (~18 GB) spills to RAM and slows down; IQ4_XS/Q3 fit | The smartest model that runs locally; slower |
| **Qwen3.6-27B** (IQ4_XS, MTP version) | Dense | Yes | ~14 GB, fits on the GPU | Hard coding and reasoning; turn on MTP for about 1.3-2x faster writing |
| **gpt-oss-20b** (MXFP4) | MoE, 3.6B active | No | ~13 GB, fully on the GPU. Very fast | Fast text and coding work with strong tool calling |

Get them from Hugging Face (search the name plus "GGUF"; bartowski and unsloth upload good ones).
For vision you also need the matching **mmproj** file: the Model server page can find and
download it for you (**Find vision file online**), also for fine-tunes of these models.

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
| **MTP speculative decoding** (`--spec-type draft-mtp`) | Models with built-in "multi-token prediction" layers guess several tokens and check them at once: about 1.3-2x faster text | Speed-up option. Works with vision |
| **Draft-model speculation** (`-md`) | A tiny model of the same family guesses tokens for the big one | Speed-up option |
| **N-gram speculation** (`--spec-type ngram-mod`) | Reuses text already in the conversation; great when editing code, which repeats a lot | Speed-up option, no extra memory |
| **Prompt caching** (one slot `-np 1`, `--cache-ram`) | The server keeps the conversation it already read, so each new step only reads the part after the first change. The app only rewrites old messages occasionally so the cache stays valid. Models with linear-attention layers (Qwen3.5 and later) resume from saved snapshots of the context instead. Earlier prompts are kept in RAM too (`--cache-ram`: 24 GB when the model fits on the GPU, else llama.cpp's 8 GB). (`--cache-reuse` isn't used: llama.cpp turns it off with a vision file and for those hybrid models.) | Always on |
| **Quantization choice** | Q4_K_M is the sweet spot; IQ4_XS is ~10% smaller with similar quality; below Q3 models get noticeably dumber | Pick when downloading |

What doesn't help much: running two models at once on 16 GB, very long contexts (each token of
context costs memory and slows every step), and Q2 quants of big models.

## Making the model smarter

- **Thinking** (Settings): High for hard tasks (slower, plans better), Low or Off for quick clicks.
  Models with their own levels (Qwen3.8: low / medium / xhigh) get the closest one.
- **Recommended sampling**: on by default. The app uses the model maker's values and switches
  them when thinking goes on or off (Qwen3.8 wants different ones for each).
- **Context**: 32K is a good default; 64K for big codebases (uses more memory).
- **AGENT.md**: put instructions in a file called AGENT.md in your work folder and the AI reads
  them every time (like a briefing).
- **Memory**: the AI saves lasting facts as short notes and is shown only the ones that match
  your message, so a big memory doesn't slow it down. See and edit them on the Memory page.
- **Coding**: ask for code changes in a git repo and it works in a private copy on a new branch,
  running the tests until they pass before it commits.
"""
