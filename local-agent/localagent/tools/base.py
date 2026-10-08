"""Tool registry: every tool has a JSON schema for the model, a risk level for approvals,
and a plain Python function that does the work."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Risk levels. "safe" never asks. The others follow the approval mode.
SAFE = "safe"        # reading, looking, planning
EDIT = "edit"        # changes files
RUN = "run"          # runs programs or code
CONTROL = "control"  # moves your mouse, types, opens apps, focuses windows
PUBLISH = "publish"  # sends something to the internet under your name (git push, pull requests)

RISK_LABELS = {
    SAFE: "look around",
    EDIT: "change files",
    RUN: "run commands",
    CONTROL: "use your mouse and keyboard",
    PUBLISH: "post to GitHub",
}


class ToolError(Exception):
    """A problem the model should read and recover from."""


@dataclass
class ToolResult:
    text: str
    images: list[bytes] = field(default_factory=list)   # PNG bytes shown to the model
    ok: bool = True


@dataclass
class Tool:
    name: str
    description: str
    params: dict[str, Any]
    func: Callable[["ToolContext", dict], "ToolResult | str"]
    risk: str = SAFE
    required: list[str] = field(default_factory=list)
    group: str = "core"          # lets settings switch whole groups off
    # Optional: decide the risk from the arguments (e.g. an edit outside the work folder).
    risk_for: Callable[["ToolContext", dict], str] | None = None
    # Optional: a one-line description of what this call will do, for the approval box.
    describe: Callable[[dict], str] | None = None

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.params,
                    "required": self.required,
                },
            },
        }


@dataclass
class ToolContext:
    """What a tool can see while it runs."""
    settings: Any
    workspace: Path
    stop: threading.Event = field(default_factory=threading.Event)
    ask_user: Callable[[str, list[str]], str] | None = None
    agent: Any = None                 # the running Agent (todos, sub-agents)
    screen: dict = field(default_factory=dict)   # last screenshot geometry
    ui_cache: list = field(default_factory=list)  # last ui_elements result
    processes: dict = field(default_factory=dict)

    def resolve(self, path: str) -> Path:
        p = Path(str(path).strip().strip('"')).expanduser()
        if not p.is_absolute():
            p = self.workspace / p
        return p.resolve()

    def inside_workspace(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.workspace.resolve())
            return True
        except ValueError:
            return False


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict | None = None, *, required=None,
         risk: str = SAFE, group: str = "core", risk_for=None, describe=None):
    """Decorator that registers a tool."""
    def wrap(fn):
        REGISTRY[name] = Tool(name=name, description=description.strip(), params=params or {},
                              func=fn, risk=risk, required=list(required or []), group=group,
                              risk_for=risk_for, describe=describe)
        return fn
    return wrap


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.7)
    tail = limit - head
    cut = len(text) - head - tail
    return f"{text[:head]}\n\n[... {cut} characters cut to save context ...]\n\n{text[-tail:]}"
