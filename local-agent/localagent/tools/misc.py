"""Planning, memory, asking you things, and sub-agents."""

from __future__ import annotations

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


def _mem():
    from ..memory import get_memory

    return get_memory()


@tool("remember", """
Save a note to long-term memory so you still know it in future chats: the user's preferences,
where their projects are, how their setup works, fixes that worked. One fact per note, short
and specific. Give it a short title. To change a note, pass its id as replaces. pinned=true
only for things needed in nearly every chat (it is then always shown to you).""",
      {"note": {"type": "string"},
       "title": {"type": "string", "description": "A few words, e.g. 'Python projects folder'"},
       "pinned": {"type": "boolean"},
       "replaces": {"type": "integer", "description": "Id of a note this one corrects or updates"}},
      required=["note"])
def remember(ctx: ToolContext, a: dict):
    mem = _mem()
    if a.get("replaces"):
        try:
            n = mem.update(int(a["replaces"]), body=a["note"], title=a.get("title"), pinned=a.get("pinned"))
        except KeyError as e:
            raise ToolError(str(e))
        return f"Updated note #{n.id}."
    n = mem.add(a["note"], a.get("title") or "", bool(a.get("pinned")))
    return f"Saved as note #{n.id}."


@tool("recall", """
Search long-term memory: your saved notes, and an index of the user's files (the work folder
and any folders they added). Use it when the user refers to something from before, or to find
a file or project by what's in it. Returns only the best matches.""",
      {"query": {"type": "string", "description": "A few key words"},
       "where": {"type": "string", "enum": ["notes", "files", "both"], "description": "Default both"},
       "limit": {"type": "integer", "description": "Default 8"}},
      required=["query"])
def recall(ctx: ToolContext, a: dict):
    mem = _mem()
    where = a.get("where") or "both"
    limit = max(1, min(30, int(a.get("limit") or 8)))
    out = []
    if where in ("notes", "both"):
        notes = mem.search_notes(a["query"], limit)
        out.append("Notes:\n" + ("\n".join("- " + n.line() for n in notes) if notes else "(none match)"))
    if where in ("files", "both"):
        files = mem.search_files(a["query"], limit)
        lines = [f"- {f.path}:{f.line}  {f.snippet[:200]}" for f in files]
        if not mem.file_count():
            lines = ["(the file index is empty or still being built)"]
        out.append("Files:\n" + ("\n".join(lines) if lines else "(none match)"))
    return "\n\n".join(out)


@tool("forget", "Delete a note from long-term memory by its id (when it is wrong or out of date).",
      {"id": {"type": "integer"}}, required=["id"])
def forget(ctx: ToolContext, a: dict):
    if not _mem().delete(int(a["id"])):
        raise ToolError(f"There is no note #{a['id']}.")
    return f"Deleted note #{a['id']}."


@tool("index_folder", """
Add a folder to the file index so recall can search it from now on (for example a projects or
documents folder the user mentions). Indexing runs now; big folders can take a minute.""",
      {"path": {"type": "string"}}, required=["path"])
def index_folder(ctx: ToolContext, a: dict):
    p = ctx.resolve(a["path"])
    if not p.is_dir():
        raise ToolError(f"Not a folder: {p}")
    folders = list(ctx.settings.index_folders or [])
    if str(p) not in folders:
        folders.append(str(p))
        ctx.settings.index_folders = folders
        try:
            ctx.settings.save()
        except OSError:
            pass
    st = _mem().index_folder(p, ctx.stop)
    return f"Indexed {p}: {st['seen']} files ({st['updated']} new or changed). recall can search it now."


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
