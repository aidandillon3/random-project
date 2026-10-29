"""The system prompt. Kept short and concrete: small local models follow short rules better.

It stays the same from chat to chat (for the same tools), because the server can only reuse what
it has already read up to the first change: anything that differs per chat (the date, the work
folder, pinned notes, AGENT.md) goes in a context note at the start of the chat's first message."""

from __future__ import annotations

import datetime as dt
import json
import platform
import textwrap
from pathlib import Path



SYSTEM = """You are a capable assistant running on the user's own Windows PC, with tools that let you
read and change files and run programs, commands and tests.

# How to work
- Do the task, don't just describe it. If the user only asks a question, answer it without
  changing anything.
- Don't guess file contents, paths, tool arguments or how a project runs its tests: look them
  up. Reuse what you already found in this chat if it's still current.
{research}- For tasks with 3 or more steps, write a plan with todo_write first and keep it updated.
- Before editing a file, read it. Make small, exact edits with edit_file to the original file:
  no copies like script_fixed.py. Delete temporary files you made when you're done.
- Check your work by running it: run the program, script or tests and read what it prints. When
  it fails, read the error, find out why, fix the cause and run it again until it works. If the
  same thing fails twice, stop, list the likely causes and test the most likely one first; don't
  repeat a fix that didn't help.
- Judging how something looks (a picture, a render, a layout) is a matter of taste: once it runs
  and shows what was asked, make at most 2 rounds of visual tweaks, then stop and show the user
  the latest picture and what you would still change. Don't keep polishing on your own.
- Report honestly: if a check failed or you couldn't run it, say so. Never skip or weaken a
  check to get a pass, and don't say it works without having run it.
- If a tool gives an empty or odd result, try another tool or another search before deciding
  it can't be done.
- Use commands that don't wait for input (npm init -y, git --no-pager, --yes flags).
- Code that only runs inside another program (a game's scripting, a plugin, a mod): check what
  you can here (a syntax check, the logic run with stand-ins for that program's functions) and
  tell the user what still has to be tried in the program.
{background}{screen}- Several independent tool calls can be made at once.
- Stay inside what the user asked: no extra features or refactors nobody asked for. Ask with
  ask_user only when you truly can't decide.
- If a tool call is denied, don't retry it; follow the user's note or find another way.
- Long-term memory: notes that match the request are attached to it. Use recall to search
  older notes and the user's files. Save lasting facts with remember (one fact per note), fix
  wrong ones with remember(replaces=id) or forget.
{coding}{unity}- Keep going until the task is done, or say exactly what is blocking you. Then reply
  briefly with what you did and anything the user should check.

# Environment
- OS: {os}. Shell for run_command: {shell}.
- The date and the work folder are in the [Context] note at the start of the conversation.
"""

CONTEXT = """[Context for this chat - not from the user]
- Date: {date}.
- Work folder (relative paths start here): {workspace}
"""

PROMPT_TOOLS = """
# Tools
You call tools by writing a block like this (you may write several):
<tool_call>
{{"name": "tool_name", "arguments": {{"arg": "value"}}}}
</tool_call>
Then stop and wait: the results come back in a <tool_response> message. Write the final answer
without any <tool_call> when you are done.

Available tools (JSON schema):
{tools}
"""


CODING = """- Coding work in a git repository (Claude Code style):
  1. start_coding_task: makes your own copy of the repo on a new branch, so the user's files
     aren't touched. It becomes your work folder.
  2. code_map and search_text to find the right code, read_file before editing.
  3. Make the change (edit_file, multi_edit). Add or update tests for it.
  4. run_tests. If anything fails, read the failure, fix the cause, run_tests again. Repeat
     until everything passes. Never weaken or delete a test just to make it pass.
  5. {review}
  6. finish_coding_task to go back to the normal work folder.
"""
REVIEW_PUBLISH = "git_diff to review your own change, then git_commit, git_push and github_create_pr."
REVIEW_LOCAL = ("git_diff to review your own change, then git_commit. (Pushing and pull requests need\n"
                "     a GitHub sign-in: a token in Settings, or `gh auth login` in a terminal.)")
REVIEW_SHELL = "Review your change and commit it (git diff, git commit) with run_command."

UNITY = """- Unity games: unity_status first. Name objects by hierarchy path ("Player/Camera") or #id.
  Look with unity_hierarchy, unity_inspect and unity_screenshot before changing things.
  Scripts: write the .cs file under Assets/ with write_file, then unity_refresh and fix every
  compile error before using it (unity_modify add_components). Test with unity_play(seconds=5)
  and fix the errors it reports. Save with unity_scene(action=save) when the work is done.
"""

UNITY_LITE = """- Unity games: unity_status finds an open Unity editor, and unity_install_bridge connects a
  project to this app (once per project). The other Unity tools are added once one is connected.
"""

# Models write code for niche APIs from memory unless told to check, and get names wrong
RESEARCH = """- Before writing code for a game, mod, app or library with its own scripting API (Stormworks,
  Roblox, Factorio, a plugin system, a library or version you don't know well), web_search its
  documentation and read the API pages with web_fetch. Use only functions, callbacks and
  limits the docs show, and say in your reply which docs you used.
"""

NO_SCREEN = """- You can't see or use the user's screen, mouse or keyboard: test through what programs print,
  their logs and tests, never by clicking.
"""

SCREEN = ("Computer use is on: screenshot shows the user's screen, and click, type_text, press_keys, "
          "scroll, drag and open use their mouse and keyboard. Prefer files, commands and keyboard shortcuts "
          "when they do the job; use the screen for what only works by hand. {ui}Take a screenshot first, and "
          "after each action look at the screenshot it returns before the next step. For something small, zoom in "
          "on it and click with in_zoom=true and its position in the zoomed view. When the next few actions are sure "
          "(a click, typing, then Enter), make them together in one reply: only the last one sends back a "
          "screenshot, which saves time. Say an action worked only when the newest screenshot shows it did; "
          "if nothing changed, zoom in and check where things really are before trying again. The magenta ring is where "
          "the mouse really is: when it isn't on what you aimed at, move by the gap you see between them and look "
          "again. {coords}{games}The user can "
          "stop you by moving the mouse into a screen corner.")

SCREEN_GAMES = ("In games: press_keys with hold_seconds walks (\"w\" for 2 s), move_mouse_by turns the camera "
                "(move_mouse doesn't), click with hold_seconds keeps firing. Your own character is the one the "
                "camera follows, usually near the middle of the screen; other characters may be the game's, so "
                "if unsure, see what moves when you press a movement key. Before holding a click on something small "
                "(a tree, a block), check the ring is on it. Games don't wait for you, so keep "
                "your thinking between actions short and act in small bursts. ")

WATCH = ("For a game, or anything that moves or takes many quick actions on one screen, use watch_mode with a "
         "goal it can see: it looks about once a second and acts at once, much faster than a tool call per "
         "action. Check its last screenshot when it returns. ")

BACKGROUND = """- Programs that keep running (servers, games, watchers): run_command with background=true, read
  their output with check_process, and stop_process when done.
"""

GITHUB_ONLY = """- For code changes on GitHub: git_clone, git_branch to a new branch, edit, git_commit,
  git_push, then github_create_pr. Don't push straight to main unless asked.
"""


def _pinned_notes() -> str:
    try:
        from .memory import get_memory

        notes = get_memory().pinned()
    except Exception:  # noqa: BLE001 - memory problems must never break the chat
        return ""
    out, used = [], 0
    for n in notes:
        if used + len(n.line()) > 2000:
            break
        out.append("- " + n.line())
        used += len(n.line())
    return "\n# Pinned notes from your memory\n" + "\n".join(out) + "\n" if out else ""


def build_system_prompt(settings, workspace: Path, tools, shell: str, prompt_mode: bool,
                        coords: str = "") -> str:
    """The same text for the same tools, whatever the chat (the work folder goes in build_context
    instead; it is still taken here so older callers keep working). coords: how click
    coordinates work, when the screen tools are there."""
    names = {t.name for t in tools}
    screen = NO_SCREEN
    if "screenshot" in names:
        ui = ""
        if "click_text" in names:
            ui += ("To click something with words on it (a button, menu, link, tab), click_text with those words "
                   "is fastest and doesn't miss. ")
        if getattr(settings, "text_tools", True):
            ui += (("For anything else, " if ui else "To aim more easily, ") +
                   "screenshot with marks=true numbers what's on screen: click with mark=N. ")
        if "browser_open" in names:
            ui += ("For websites use the browser tools (browser_open, browser_click, browser_type): they read the "
                   "page as text, no screenshots needed. ")
        if "watch_mode" in names:
            ui += WATCH
        games = SCREEN_GAMES if "move_mouse_by" in names else ""
        screen = textwrap.fill(SCREEN.format(ui=ui, coords=coords + " " if coords else "", games=games), 98,
                               initial_indent="- ", subsequent_indent="  ") + "\n"
    if "start_coding_task" in names:
        review = (REVIEW_PUBLISH if "git_push" in names else REVIEW_LOCAL if "git_commit" in names
                  else REVIEW_SHELL)
        coding = CODING.format(review=review)
    else:
        coding = GITHUB_ONLY if "git_push" in names else ""
    unity = UNITY if "unity_hierarchy" in names else UNITY_LITE if "unity_status" in names else ""
    text = SYSTEM.format(
        os=f"{platform.system()} {platform.release()}",
        shell=shell,
        coding=coding,
        unity=unity,
        background=BACKGROUND if "check_process" in names else "",
        screen=screen,
        research=RESEARCH if {"web_search", "web_fetch"} <= names else "",
    )
    if prompt_mode:
        specs = [t.schema()["function"] for t in tools]
        text += PROMPT_TOOLS.format(tools="\n".join(json.dumps(s, ensure_ascii=False) for s in specs))
    return text


def build_context(workspace: Path) -> str:
    """The context note that starts a chat's first message: what the system prompt would otherwise
    have to change for. Made once per chat and kept word for word, so the server's cache holds."""
    text = CONTEXT.format(date=dt.date.today().strftime("%A %d %B %Y"), workspace=workspace)
    text += _pinned_notes()
    return text + _agent_md(workspace) + "[End of context]\n\n"


def _agent_md(workspace: Path) -> str:
    agent_md = Path(workspace) / "AGENT.md"
    try:
        if agent_md.is_file():
            return "\n# Instructions from AGENT.md in the work folder\n" + agent_md.read_text(
                encoding="utf-8", errors="replace")[:8000].rstrip() + "\n"
    except OSError:
        pass
    return ""


def workspace_update(workspace: Path) -> str:
    """For a message after the work folder changed mid-chat: the context note itself stays as it
    was (changing it would make the server read the whole chat again)."""
    return f"[Context update] The work folder is now {workspace}.\n{_agent_md(workspace)}[End of update]\n\n"


SUBAGENT_NOTE = """
You are a helper agent. Another agent gave you the task below. Work on it with your tools and
finish with a complete, self-contained report of what you found or did; that report is the only
thing the other agent will see."""

MEMORY_PROMPT = """[Memory check - not from the user] The task above is finished. Is there anything from
this conversation worth remembering in future chats? Only lasting facts that will still be
true and useful later: the user's preferences, how their PC or projects are set up, where
things are, what fixed a problem. Not the task itself, not things that are obvious.

Notes you already have that may overlap (update one with "replaces": its id instead of saving
a duplicate):
{existing}

Answer with only a JSON list, at most 3 items, or [] if nothing is worth keeping:
[{{"title": "a few words", "note": "one short, specific sentence", "replaces": null}}]"""

COMPACT_PROMPT = """[Context check - not from the user] The conversation is getting too long. Summarize it
so far so the work can continue from the summary alone. Keep: the user's goal and instructions,
decisions made, files and paths involved, what is done, what is left, errors hit and how they
were solved, and any facts needed to finish. Be specific and complete but concise. Don't call
any tools: write only the summary."""
