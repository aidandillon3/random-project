"""Deciding when to ask you before a tool runs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .tools import EDIT, PUBLISH, RUN, SAFE
from .tools.shell import GIT_CMD

# Commands that always need your OK unless you switch that off in Settings, even in full auto.
# Only the command fields of a call are checked (see danger_text), never file contents.
_DEL = r"\b(?:rm|ri|rd|rmdir|del|erase|remove-item)\b[^|;&\n]*"   # a delete command, up to the next | ; & or line
DANGEROUS = [
    r"\bformat(\.com)?\s+[a-z]:", r"\bdiskpart\b", r"\bbcdedit\b", r"\bcipher\s+/w", r"\bmkfs\b",
    r"\bdd\s+if=", r"\brm\s+(-\w*\s+)*-\w*[rf]\w*\s+(/|~|\*|[a-z]:)", r"\brm\s+-rf\b",
    r"\b(del|erase)\b.*\s/s\b", r"\b(rd|rmdir)\b.*\s/s\b", r"remove-item\b.*-recurse",
    r"(?<![.\w])shutdown(\.exe)?\b(?!\s*\()", r"\brestart-computer\b", r"\bstop-computer\b",
    r"\breg(\.exe)?\s+delete\b",
    r"remove-itemproperty", r"\bset-executionpolicy\b", r"\bnet\s+user\b", r"\btakeown\b",
    r"\bicacls\b.*\/grant", r"\bclear-disk\b", r"\bformat-volume\b", r"\binitialize-disk\b",
    r"\bvssadmin\b.*delete", r"\bwmic\b.*shadowcopy.*delete", r"\bsc(\.exe)?\s+delete\b",
    r"invoke-expression|\biex\b.*(iwr|invoke-webrequest|downloadstring)", r"curl[^|]*\|\s*(sh|bash)",
    r"\bgit\s+push\b.*--force", r"\bgit\s+(reset\s+--hard|clean\s+-\w*f)",
    # recursive deletes: rm -r / -fr / -Rf, PowerShell -r -re -rec... -Recurse (any order), --recursive
    _DEL + r"\s-(r(e(c(u(r(s(e)?)?)?)?)?)?|[fvid]*r[rfvid]*|-recursive)\b",
    _DEL + r"[\s\\/'\"]\*(\.\*)?(?=\s|$|['\"])",                    # everything in a folder: X\* or *.*
    r"\|\s*(xargs\s+(-\w+\s+)*)?(remove-item|ri|rm|rmdir|rd|del|erase)\b",  # gci -r | Remove-Item
    r"\|\s*(iex|invoke-expression)\b",                                 # irm URL | iex
    r"\bclear-recyclebin\b", r"\[(system\.)?io\.(directory|file)\]::delete\b",
    r"shutil\.rmtree|\bos\.(remove|unlink|rmdir|removedirs)\s*\(",
    r"\bgh(\.exe)?\s+repo\s+delete\b",
    GIT_CMD + r"push\b.*(\s-[a-z]*f[a-z]*\b|--force|\s\+\S|--mirror\b|--delete\b|\s:\S)",   # force, deleting
    GIT_CMD + r"clean\b[^|;&\n]*\s(-\w*f|--force)",
    GIT_CMD + r"branch\b[^|;&\n]*\s(?-i:-[a-zA-Z]*D)\b",
    GIT_CMD + r"(checkout|restore)\b[^|;&\n]*\s(\.|:/)(?=\s|$|[|;&])",   # throw away every change
]
_DANGER_RX = [re.compile(p, re.IGNORECASE) for p in DANGEROUS]

# The arguments that are commands or code to run. File contents (write_file, edit_file) are left
# out: a Makefile with rm -rf build or a README about git reset --hard is not a dangerous call.
COMMAND_FIELDS = ("command", "code", "target", "text")


def is_dangerous(text: str) -> bool:
    return any(rx.search(text) for rx in _DANGER_RX)


def danger_text(args, every: bool = False) -> str:
    """The part of a tool call's arguments to check with is_dangerous. every: all of them, for tools
    from MCP servers and plugins, whose fields can be named anything ("script", "cmd"...)."""
    if not isinstance(args, dict):
        return str(args)
    if every:
        return json.dumps(args, ensure_ascii=False).replace("\\\\", "\\")   # C:\\x back to C:\x
    return "\n".join(str(args[k]) for k in COMMAND_FIELDS if args.get(k) is not None)


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
    always_ask_publish: bool = True      # pushes and pull requests ask even in full auto
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
            return risk == PUBLISH and self.always_ask_publish
        if self.mode == "auto_edit" and risk == EDIT:
            return False
        return True


def risk_text(risk: str) -> str:
    return {
        EDIT: "change files in the work folder",
        RUN: "run a command or change files outside the work folder",
        PUBLISH: "push code or post to GitHub under your account",
    }.get(risk, risk)
