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
    coding: dict = field(default_factory=dict)   # the active coding task (see tools/code.py)
    unity: dict = field(default_factory=dict)    # the Unity project in use (see tools/unity.py)
    capture_after: bool = True   # control actions attach a screenshot (the agent sets False for all but the last)

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


def path_risk(ctx: ToolContext, *paths, inside: str = EDIT) -> str:
    """inside for paths in the work folder; RUN outside it, so 'auto-approve edits' still asks."""
    for p in paths:
        if p and not ctx.inside_workspace(ctx.resolve(str(p))):
            return RUN
    return inside


REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, params: dict | None = None, *, required=None,
         risk: str = SAFE, group: str = "core", risk_for=None, describe=None):
    """Decorator that registers a tool."""
    def wrap(fn):
        REGISTRY[name] = Tool(name=name, description=description.strip(), params=params or {},
                              func=fn, risk=risk, required=list(required or []), group=group,
                              risk_for=_guard_risk(risk_for, risk), describe=_guard_describe(describe))
        return fn
    return wrap


# The model can send any JSON (null, numbers, lists) where a string is expected. Deciding the
# risk and describing the call happen before the tool runs, so they must never raise.
def _guard_risk(fn, risk: str):
    if fn is None:
        return None

    def safe_risk(ctx, a):
        try:
            return fn(ctx, a)
        except Exception:  # noqa: BLE001 - odd arguments: ask, never wave it through
            return PUBLISH if risk == PUBLISH else RUN
    return safe_risk


def _guard_describe(fn):
    if fn is None:
        return None

    def safe_describe(a):
        try:
            return str(fn(a))
        except Exception:  # noqa: BLE001
            return repr(a)[:300]
    return safe_describe


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    marker = "\n\n[... {} characters cut to save context ...]\n\n"
    # Leave room for the marker, so the result fits in limit and cutting it again changes nothing.
    room = limit - len(marker.format(len(text)))
    if room <= 0:
        return text[:limit]
    head = int(room * 0.7)
    tail = room - head
    return text[:head] + marker.format(len(text) - head - tail) + text[len(text) - tail:]
