"""All tools. Importing this package registers them."""

import os
import shutil
import sys
from pathlib import Path

from . import code, files, github, misc, screen, shell, unity, web  # noqa: F401  (registers tools)
from .base import CONTROL, EDIT, PUBLISH, REGISTRY, RISK_LABELS, RUN, SAFE, Tool, ToolContext, ToolError, ToolResult

# Every tool's definition goes with every request and costs prompt tokens, so two big groups are
# cut down unless they're likely to be used. Left in: what's needed to start using them.
UNITY_ALWAYS = {"unity_status", "unity_install_bridge"}
LOCAL_GIT = {"git_status", "git_diff", "git_log", "git_branch", "git_commit"}   # coding mode needs no sign-in


def _gh_hosts() -> Path:
    """Where the GitHub CLI keeps its logins."""
    if os.environ.get("GH_CONFIG_DIR"):
        return Path(os.environ["GH_CONFIG_DIR"]) / "hosts.yml"
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "GitHub CLI" / "hosts.yml"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gh" / "hosts.yml"


def github_signed_in(settings) -> bool:
    """A GitHub sign-in is likely: a token in Settings or the environment, or a GitHub CLI login
    (its hosts.yml is there; running gh to make sure would take a second)."""
    if getattr(settings, "github_token", "") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"):
        return True
    return bool(shutil.which("gh")) and _gh_hosts().is_file()


def unity_likely(workspace, settings) -> bool:
    detected = getattr(unity, "detected", None)
    if detected is None:      # an older unity.py: offer the whole group as before
        return True
    try:
        return bool(detected(Path(workspace) if workspace else None, settings))
    except Exception:  # noqa: BLE001 - a failed check must not break the chat
        return True


def enabled_tools(settings, depth: int = 0, workspace=None, full=()) -> list[Tool]:
    """The tools to offer. The Unity group is cut down to unity_status and unity_install_bridge
    unless the work folder is a Unity project (or one is set up), and GitHub to local git unless
    there is a sign-in. full names groups to give whole anyway (one turned on mid-chat)."""
    groups = {"core"}
    if settings.enable_shell:
        groups.add("shell")
    if settings.enable_control:
        groups.add("control")
    if settings.enable_web:
        groups.add("web")
    if getattr(settings, "enable_github", True):
        groups.add("github")
    if getattr(settings, "enable_code", True):
        groups.add("code")
    if getattr(settings, "enable_unity", True):
        groups.add("unity")
    if settings.enable_subagents and depth == 0:
        groups.add("subagents")
    groups.add("mcp")     # tools of the MCP servers in data/mcp.json (opt-in by being listed there)
    tools = [t for t in REGISTRY.values() if t.group in groups]
    if "unity" in groups and "unity" not in full and not unity_likely(workspace, settings):
        tools = [t for t in tools if t.group != "unity" or t.name in UNITY_ALWAYS]
    if "github" in groups and "github" not in full and not github_signed_in(settings):
        tools = [t for t in tools if t.group != "github" or t.name in LOCAL_GIT]
    if depth > 0:  # helpers work quietly: no questions to the user
        tools = [t for t in tools if t.name != "ask_user"]
    return tools


__all__ = ["REGISTRY", "Tool", "ToolContext", "ToolError", "ToolResult", "enabled_tools", "github_signed_in",
           "SAFE", "EDIT", "RUN", "CONTROL", "PUBLISH", "RISK_LABELS"]
