"""Planning, memory, asking you things, and sub-agents."""

from __future__ import annotations

import datetime as dt
import time

from .base import ToolContext, ToolError, tool

STATUSES = ("pending", "in_progress", "done")


@tool("todo_write", """
Keep a short to-do list for multi-step work. Send the whole list every time (it replaces the
old one). Mark one item in_progress while you work on it and mark items done as you finish.
Use it for any task with 3 or more steps.""",
      {"todos": {"type": "array", "items": {"type": "object", "properties": {
          "content": {"type": "string"},
          "status": {"type": "string", "enum": list(STATUSES)}}, "required": ["content", "status"]}}},
      required=["todos"])
def todo_write(ctx: ToolContext, a: dict):
    todos = []
    for t in a.get("todos") or []:
        if isinstance(t, str):
            t = {"content": t, "status": "pending"}
        status = t.get("status", "pending")
        if status not in STATUSES:
            status = "done" if status in ("completed", "complete") else "pending"
        todos.append({"content": str(t.get("content", "")), "status": status})
    if ctx.agent is not None:
        ctx.agent.set_todos(todos)
    marks = {"pending": "[ ]", "in_progress": "[~]", "done": "[x]"}
    return "To-do list updated:\n" + "\n".join(f"{marks[t['status']]} {t['content']}" for t in todos)


@tool("remember", """
Save a fact to long-term memory so you still know it in future chats (the user's preferences,
where their projects are, things that worked). Keep notes short.""",
      {"note": {"type": "string"}}, required=["note"])
def remember(ctx: ToolContext, a: dict):
    from ..prompts import memory_path

    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- {a['note'].strip()} ({dt.date.today().isoformat()})\n")
    return f"Saved to memory ({path})."


@tool("ask_user", """
Ask the user a question and wait for the answer. Use it when you truly need a decision or
information you cannot find yourself. Give options when there are clear choices.""",
      {"question": {"type": "string"},
       "options": {"type": "array", "items": {"type": "string"}}},
      required=["question"])
def ask_user(ctx: ToolContext, a: dict):
    if ctx.ask_user is None:
        raise ToolError("No user is available to answer. Make a sensible choice and say which.")
    answer = ctx.ask_user(a["question"], list(a.get("options") or []))
    return f"The user answered: {answer}" if answer else "The user did not answer."


@tool("wait", "Wait a number of seconds (for a program to open or a page to load).",
      {"seconds": {"type": "number"}}, required=["seconds"])
def wait(ctx: ToolContext, a: dict):
    secs = min(120.0, max(0.0, float(a["seconds"])))
    end = time.time() + secs
    while time.time() < end and not ctx.stop.is_set():
        time.sleep(0.1)
    return f"Waited {secs:g} seconds."


@tool("delegate_task", """
Hand a self-contained job to a helper agent with a fresh, empty context, and get back its final
report. Good for research or searching through many files without filling up your own context.
Describe the job completely; the helper cannot see this conversation.""",
      {"task": {"type": "string"}}, required=["task"], group="subagents")
def delegate_task(ctx: ToolContext, a: dict):
    if ctx.agent is None:
        raise ToolError("Helpers are not available here.")
    return ctx.agent.run_subagent(a["task"])
