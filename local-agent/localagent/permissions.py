"""Deciding when to ask you before a tool runs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .tools import CONTROL, EDIT, RUN, SAFE

# Commands that always need your OK unless you switch that off in Settings, even in full auto.
DANGEROUS = [
    r"\bformat(\.com)?\s+[a-z]:", r"\bdiskpart\b", r"\bbcdedit\b", r"\bcipher\s+/w", r"\bmkfs\b",
    r"\bdd\s+if=", r"\brm\s+(-\w*\s+)*-\w*[rf]\w*\s+(/|~|\*|[a-z]:)", r"\brm\s+-rf\b",
    r"\b(del|erase)\b.*\s/s\b", r"\b(rd|rmdir)\b.*\s/s\b", r"remove-item\b.*-recurse",
    r"\bshutdown\b", r"\brestart-computer\b", r"\bstop-computer\b", r"\breg(\.exe)?\s+delete\b",
    r"remove-itemproperty", r"\bset-executionpolicy\b", r"\bnet\s+user\b", r"\btakeown\b",
    r"\bicacls\b.*\/grant", r"\bclear-disk\b", r"\bformat-volume\b", r"\binitialize-disk\b",
    r"\bvssadmin\b.*delete", r"\bwmic\b.*shadowcopy.*delete", r"\bsc(\.exe)?\s+delete\b",
    r"invoke-expression|\biex\b.*(iwr|invoke-webrequest|downloadstring)", r"curl[^|]*\|\s*(sh|bash)",
    r"\bgit\s+push\b.*--force", r"\bgit\s+(reset\s+--hard|clean\s+-\w*f)",
]
_DANGER_RX = [re.compile(p, re.IGNORECASE) for p in DANGEROUS]


def is_dangerous(text: str) -> bool:
    return any(rx.search(text) for rx in _DANGER_RX)


@dataclass
class ApprovalRequest:
    tool: str
    risk: str
    summary: str
    args: dict
    dangerous: bool = False

    def pretty_args(self) -> str:
        return json.dumps(self.args, indent=2, ensure_ascii=False)[:6000]


@dataclass
class Decision:
    allow: bool
    always: bool = False     # allow this kind of action for the rest of the chat
    note: str = ""           # what to tell the model instead, when denied


@dataclass
class Policy:
    mode: str = "ask"
    always_ask_dangerous: bool = True
    session_allowed: set = field(default_factory=set)   # risks allowed for this chat

    def needs_approval(self, risk: str, dangerous: bool) -> bool | None:
        """True = ask, False = go ahead, None = refuse (read-only mode)."""
        if risk == SAFE:
            return False
        if self.mode == "read_only":
            return None
        if dangerous and self.always_ask_dangerous:
            return True
        if risk in self.session_allowed:
            return False
        if self.mode == "auto":
            return False
        if self.mode == "auto_edit" and risk == EDIT:
            return False
        return True


def risk_text(risk: str) -> str:
    return {
        EDIT: "change files in the work folder",
        RUN: "run a command or change files outside the work folder",
        CONTROL: "use your mouse, keyboard or open programs",
    }.get(risk, risk)
