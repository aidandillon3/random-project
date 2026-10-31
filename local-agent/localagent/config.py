"""Settings, saved as JSON next to the app so nothing is hidden in AppData."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LOCALAGENT_DATA", APP_DIR / "data"))

SERVER_PRESETS = {
    "llama.cpp (started from this app)": "http://127.0.0.1:8080/v1",
    "LM Studio": "http://127.0.0.1:1234/v1",
    "Ollama": "http://127.0.0.1:11434/v1",
}

# Approval modes, strictest first.
APPROVAL_MODES = {
    "read_only": "Read only (never change anything)",
    "ask": "Ask before risky actions",
    "auto_edit": "Auto-approve file edits in the work folder",
    "auto": "Full auto (still asks for dangerous commands)",
}


@dataclass
class Settings:
    # connection
    base_url: str = "http://127.0.0.1:8080/v1"
    api_key: str = "local"
    model: str = ""                    # empty = whatever the server has loaded
    tool_mode: str = "auto"            # auto | native | prompt

    # generation
    temperature: float = 0.6
    top_p: float = 0.95
    top_k: int = 20
    min_p: float = 0.0
    max_tokens: int = 32768            # thinking counts too, and Qwen3.8 at full effort thinks long
    auto_sampling: bool = True         # use the model maker's values, switched with thinking on/off
    thinking: str = "auto"             # auto | off | low | medium | high ("on" = high, from older versions)
    send_back_reasoning: bool = True

    # context
    context_limit: int = 0             # 0 = ask the server, else 32768
    keep_images: int = 2               # newest images kept when older ones are cleared
    max_images: int = 8                # clear older images only once more than this many pile up
                                       # (each clearing makes the server re-read the chat from there)
    max_tool_output: int = 20000       # characters per tool result
    max_steps: int = 0                 # tool rounds per message before it stops and checks in; 0 = no limit
    compact_at: float = 0.75           # compact the conversation at this share of the context

    # safety
    approval_mode: str = "ask"
    always_ask_dangerous: bool = True
    always_ask_publish: bool = True    # git push / pull requests ask even in full auto
    workspace: str = ""

    # tool groups. By default only pi's four (read_file, write_file, edit_file, run_command), like pi: a short
    # prompt and few tools are faster and keep a local model on track; the rest are turned on when needed
    enable_shell: bool = True
    enable_more_files: bool = False    # list_dir, find_files, search_text, multi_edit, move/copy/delete
    enable_more_shell: bool = False    # background commands (check_process, stop_process) and run_python
    enable_planning: bool = False      # todo_write, ask_user, wait
    enable_memory: bool = False        # remember/recall/forget, notes attached to requests, auto-save, file index
    enable_web: bool = False
    enable_subagents: bool = False
    enable_github: bool = False
    enable_code: bool = False          # coding mode: own git copy, test loop, code map
    enable_unity: bool = False         # work in the open Unity editor (needs the bridge script in the project)
    unity_project: str = ""            # which Unity project, when several are open
    github_token: str = ""             # only kept in data/settings.json on this PC; empty = use the gh CLI login

    # long-term memory
    auto_recall: bool = True           # show the model saved notes that match each request
    auto_memory: bool = True           # after a bigger task, save what's worth remembering
    index_files: bool = True           # keep a search index of the work folder + index_folders
    index_folders: list = field(default_factory=list)

    # images and the shell
    screenshot_max_side: int = 1280    # images the model reads are scaled to fit this (longest side, pixels)

    # Computer use: screenshots, mouse and keyboard on this PC (tools/screen.py). Off unless the
    # toggle next to the message box is turned on; while on, clicks and keys never ask.
    computer_use: bool = False
    monitor: int = 1                   # the screen it looks at first: 1, 2, 3...
    screen_max_side: int = 0           # its screenshots' longest side; 0 = automatic: about half of what the model's
                                       # vision takes without shrinking it again (see tools/screen.fit_size)
    coord_mode: str = "auto"           # auto | pixels | normalized (0-1000)
    grid_overlay: bool = False
    screenshot_after_action: bool = True
    screen_marks: bool = False         # number the things to click on every screenshot (else only when asked)
    browser_tools: bool = True         # with Computer use: a browser it drives directly (tools/browser.py)
    text_tools: bool = True            # with Computer use: click_text and numbered marks (tools/ocr.py)
    watch_tool: bool = True            # with Computer use: watch mode, a look about once a second and short action
                                       # lines instead of a tool call per action (tools/live.py)
    watch_size: str = "normal"         # its screenshots: small | normal | sharp (tools/live.SIZES)
    watch_frames: int = 1              # screenshots per look, 0.5 s apart (2-3 show what moves; each costs a read)
    watch_max_seconds: int = 300       # the longest one watch mode run may take
    watch_model: str = "chat"          # chat | eyes: watch mode looks with the chat's model or a small one of its own
                                       # on another GPU (localagent/eyes.py)
    watch_model_gpu: str = ""          # GPU UUID for that one; "" = the one the chat's model doesn't use
    watch_model_port: int = 8091
    shell: str = "auto"                # auto | powershell | cmd | bash

    # the window
    chat_text_pt: int = 11             # size of the chat's text (Ctrl + / Ctrl - / Ctrl 0 in the chat)
    sidebar_open: bool = True          # the chat list on the left (Ctrl+B)
    answer_font: str = "sans"          # sans | serif

    # model server launcher
    engine: str = "auto"               # auto (the mode's recommended one) | llama.cpp | strata (catalog.py)
    server_exe: str = ""
    models_dir: str = ""
    model_path: str = ""
    mmproj_path: str = ""
    draft_path: str = ""
    ctx_size: int = 32768
    kv_type: str = "q8_0"
    placement: str = "auto"            # auto (llama.cpp fit) | manual
    gpu_layers: int = 999
    n_cpu_moe: int = 0
    speedup: str = "off"               # off | mtp | ngram | draft
    port: int = 8080
    extra_args: str = ""
    use_vision: bool = True
    gpus: str = ""                     # GPUs the model uses: "" = the strongest, "all", or GPU UUIDs (gguf.chosen_gpus)
    basalt_models: list = field(default_factory=list)    # .basalt files used before: kept in the model lists
    monitors_on_ai_gpu: str = "auto"   # "auto" (gguf.monitors_on), "yes" or "no": VRAM kept free for the desktop
    strata_low_ram: bool = False       # Strata's low-RAM mode (setup --low-ram resident): only the experts the GPU
                                       # doesn't hold stay in RAM; the KV cache all in VRAM (strata.setup_args)
    strata_ram_gib: int = 0            # with it: the most GiB of experts kept in RAM (--resident-budget-gib), 0 = no cap

    extra: dict = field(default_factory=dict)
    settings_version: int = 7          # bumped when an old saved default needs moving on load

    # ---------------------------------------------------------------
    def workspace_path(self) -> Path:
        if self.workspace:
            return Path(self.workspace).expanduser()
        default = Path.home() / "AgentWorkspace"
        return default

    @classmethod
    def load(cls, path: Path | None = None) -> "Settings":
        path = path or DATA_DIR / "settings.json"
        s = cls()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
        if not isinstance(raw, dict):
            return s
        for f in fields(cls):
            # a value of the wrong kind (hand-edited, or from a broken save) keeps the default
            if f.name in raw and _same_kind(raw[f.name], getattr(s, f.name)):
                setattr(s, f.name, raw[f.name])
        version = raw.get("settings_version", 1) if isinstance(raw.get("settings_version"), int) else 1
        if version < 2 and raw.get("max_tokens") == 8192:
            s.max_tokens = 32768      # the old default cut long thinking short
        if version < 3 and raw.get("keep_images") == 3:
            s.keep_images = 2         # the old default; fewer images, cleared less often, is faster
        if version < 4 and raw.get("answer_font") == "serif":
            s.answer_font = "sans"    # the old default; serif looked odd on Windows
        if version < 5 and raw.get("max_steps") == 150:
            s.max_steps = 0           # the old default; a long job paused and waited for "continue"
        if version < 6:
            s.screen_max_side = 0     # tried at 1920 and 2560 the night it came: both too big for the vision part
        if version < 7:               # the pi-like default (2026-10-31): extra tools off, no thinking
            for name in ("enable_web", "enable_subagents", "enable_github", "enable_code", "enable_unity"):
                setattr(s, name, False)
            s.thinking = "off"
        s.settings_version = cls.settings_version   # saved as moved on, so a choice made later sticks
        return s

    def save(self, path: Path | None = None) -> None:
        """Written to a temporary file first, then swapped in: a crash halfway through never
        leaves a half-written settings file (which would lose every setting, the GitHub token too)."""
        path = path or DATA_DIR / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)


def _same_kind(value, default) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, (int, float)):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))
