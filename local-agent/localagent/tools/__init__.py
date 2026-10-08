"""All tools. Importing this package registers them."""

from . import files, github, misc, screen, shell, web  # noqa: F401  (registers tools)
from .base import CONTROL, EDIT, PUBLISH, REGISTRY, RISK_LABELS, RUN, SAFE, Tool, ToolContext, ToolError, ToolResult


def enabled_tools(settings, depth: int = 0) -> list[Tool]:
    groups = {"core"}
    if settings.enable_shell:
        groups.add("shell")
    if settings.enable_control:
        groups.add("control")
    if settings.enable_web:
        groups.add("web")
    if getattr(settings, "enable_github", True):
        groups.add("github")
    if settings.enable_subagents and depth == 0:
        groups.add("subagents")
    tools = [t for t in REGISTRY.values() if t.group in groups]
    if depth > 0:  # helpers work quietly: no questions to the user
        tools = [t for t in tools if t.name != "ask_user"]
    return tools


__all__ = ["REGISTRY", "Tool", "ToolContext", "ToolError", "ToolResult", "enabled_tools",
           "SAFE", "EDIT", "RUN", "CONTROL", "PUBLISH", "RISK_LABELS"]
