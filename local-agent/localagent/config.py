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
    max_tokens: int = 8192
    thinking: str = "auto"             # auto | off | low | medium | high ("on" = high, from older versions)
    send_back_reasoning: bool = True

    # context
    context_limit: int = 0             # 0 = ask the server, else 32768
    keep_images: int = 3               # newest screenshots kept in the conversation
    max_tool_output: int = 20000       # characters per tool result
    max_steps: int = 150               # tool rounds per message before it stops and checks in
    compact_at: float = 0.75           # compact the conversation at this share of the context

    # safety
    approval_mode: str = "ask"
    always_ask_dangerous: bool = True
    always_ask_publish: bool = True    # git push / pull requests ask even in full auto
    workspace: str = ""

    # tool groups
    enable_shell: bool = True
    enable_control: bool = True
    enable_web: bool = True
    enable_subagents: bool = True
    enable_github: bool = True
    enable_code: bool = True           # coding mode: own git copy, test loop, code map
    github_token: str = ""             # only kept in data/settings.json on this PC; empty = use the gh CLI login

    # long-term memory
    auto_recall: bool = True           # show the model saved notes that match each request
    auto_memory: bool = True           # after a bigger task, save what's worth remembering
    index_files: bool = True           # keep a search index of the work folder + index_folders
    index_folders: list = field(default_factory=list)

    # screen
    screenshot_max_side: int = 1280
    coord_mode: str = "auto"           # auto | pixels | normalized (0-1000)
    grid_overlay: bool = False
    screenshot_after_action: bool = True
    monitor: int = 1                   # 1 = main screen, 2 = second ...
    shell: str = "auto"                # auto | powershell | cmd | bash

    # model server launcher
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

    extra: dict = field(default_factory=dict)

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
            names = {f.name for f in fields(cls)}
            for k, v in raw.items():
                if k in names:
                    setattr(s, k, v)
        except (OSError, ValueError):
            pass
        return s

    def save(self, path: Path | None = None) -> None:
        path = path or DATA_DIR / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
