"""The agent loop: ask the model, run the tools it calls, feed back results, repeat."""

from __future__ import annotations

import base64
import copy
import itertools
import json
import re
import threading
import time
from collections import deque
from pathlib import Path

from . import prompts
from .guide import sampling_for
from .llm import (ImagesUnsupported, LLMClient, ReplyLost, ServerError, Stopped, ToolCall, ToolsUnsupported, Turn,
                  extract_text_tool_calls, parse_args, strip_thinking)
from .permissions import ApprovalRequest, Decision, Policy, danger_text, is_dangerous, risk_text
from .tools import SAFE, ToolContext, ToolError, ToolResult, enabled_tools, shell
from .tools.base import truncate
from .tools.github import redact_all
from .tools.screen import coord_help
from .tools.shell import shell_kind

IMAGE_TOKENS = 1200          # rough cost of one image, for estimates only
MANY_IMAGES = 4             # pictures in one task after which the model is reminded to ask instead of polishing
MANY_IMAGES_NOTE = ("\n(That's picture {n} of this task. If it already runs and shows what was asked, stop "
                    "adjusting the look: show the user and ask what to change.)")
CHARS_PER_TOKEN = 3.5
MAX_AUTO_RETRIES = 3        # a lost or cut-off reply is retried this many times in a row before it gives up
CARRY_ON = ("(automatic) Your last reply was cut off by a length limit. Carry on exactly where it stopped; "
            "don't repeat what you already wrote.")
SUMMARY_TOKENS = 3072        # room a summary made inside the conversation may take
LIGHT_ARG = 600              # older steps: a tool call argument longer than this keeps only its start
LIGHT_RESULT = 3000          # older steps: a tool result longer than this keeps only its start and end
LIGHT_ENOUGH = 0.5           # compacting skips the summary when shortening older steps gets this low
LIGHT_KEEP = 6               # the latest steps kept word for word when older steps are shortened
NO_VISION = "[image not shown: this model can't see images]"
STOPPED = "Not run: stopped by the user."
CUT_OFF = ("Not run: this call was cut off. The reply reached its length limit before the call was complete. "
           "Do it in smaller pieces: for example write a long file in parts (write_file with the first part, "
           "then edit_file to add the rest).")


EFFORTS = ["low", "medium", "high", "xhigh"]


def effort_levels(template: str) -> list[str]:
    """Thinking levels a chat template understands, e.g. Qwen3.8: low, medium, xhigh;
    gpt-oss: low, medium, high. Older models with only on/off have none."""
    if "reasoning_effort" not in template:
        return []
    return [e for e in EFFORTS if re.search(rf"['\"]{e}['\"]", template)]


def pick_effort(want: str, levels: list[str]) -> str | None:
    """The model's own level closest to the one picked in Settings; "high" means its highest."""
    if not levels:
        return None
    if want in ("high", "xhigh"):
        return levels[-1]
    if want in levels:
        return want
    i = EFFORTS.index(want)
    return min(levels, key=lambda lv: (abs(EFFORTS.index(lv) - i), EFFORTS.index(lv)))


class AgentUI:
    """What the agent tells the outside world. The GUI and the terminal version fill these in."""

    def assistant_start(self): ...
    def text(self, s: str): ...
    def reasoning(self, s: str): ...
    def assistant_end(self, turn: Turn): ...
    def tool_start(self, call_id: str, name: str, args: dict, summary: str): ...
    def tool_end(self, call_id: str, name: str, result: ToolResult): ...
    def status(self, s: str): ...
    def notice(self, s: str): ...
    def todos(self, todos: list[dict]): ...
    def usage(self, used: int, limit: int, tokens_per_sec: float | None): ...

    def approve(self, req: ApprovalRequest) -> Decision:
        return Decision(False, note="No one was there to approve it.")

    def ask(self, question: str, options: list[str]) -> str:
        return ""


# Screen tools that end with a picture of the screen (list_windows, ui_elements and the clipboard
# only read): of several in one step, only the last one waits for the screen and takes it.
SHOWS_SCREEN = {"click", "move_mouse", "drag", "scroll", "type_text", "press_keys", "focus_window", "open",
                "click_element", "screenshot", "zoom"}


def png_part(png: bytes) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(png).decode()}}


def _count_images(msg: dict) -> int:
    c = msg.get("content")
    return sum(1 for p in c if p.get("type") == "image_url") if isinstance(c, list) else 0


def estimate_tokens(msg: dict) -> int:
    c = msg.get("content")
    if isinstance(c, list):
        chars = sum(len(p.get("text", "")) for p in c)
    else:
        chars = len(c or "")
    chars += len(json.dumps(msg.get("tool_calls", ""))) + len(msg.get("reasoning_content", "") or "")
    return int(chars / CHARS_PER_TOKEN) + 8 + IMAGE_TOKENS * _count_images(msg)


def _valid_json(text: str) -> bool:
    try:
        return isinstance(json.loads(text or "{}"), dict)
    except (ValueError, TypeError):
        return False


def _history_args(text: str) -> str:
    """Tool-call arguments as they go back to the server. It reads them as JSON, and one call it
    can't parse would fail every later request, so broken ones are kept repaired, or as {}."""
    if _valid_json(text):
        return text or "{}"
    try:
        return json.dumps(parse_args(text), ensure_ascii=False)
    except ValueError:
        return "{}"


class Agent:
    def __init__(self, settings, ui: AgentUI | None = None, client: LLMClient | None = None,
                 depth: int = 0, policy: Policy | None = None, stop: threading.Event | None = None):
        self.settings = settings
        self.ui = ui or AgentUI()
        self.client = client or LLMClient(settings.base_url, settings.api_key, settings.model)
        self.depth = depth
        self.stop = stop or threading.Event()   # set it to stop: also cuts a request the server is busy with
        self.policy = policy or Policy(settings.approval_mode, settings.always_ask_dangerous,
                                     settings.always_ask_publish)
        self.workspace = settings.workspace_path()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.ctx = ToolContext(settings=settings, workspace=self.workspace, stop=self.stop,
                               ask_user=self.ui.ask, agent=self)
        self.ctx.enable_group = self.enable_group
        self.tools = {t.name: t for t in enabled_tools(settings, depth, self.workspace)}
        self.prompt_mode = settings.tool_mode == "prompt"
        self.messages: list[dict] = []
        self.todos: list[dict] = []
        self.system_extra = ""
        self._system: str | None = None
        self._fixed: int | None = None    # tokens of the system prompt + tool definitions
        self._fixed_key: tuple | None = None
        self._ctx_limit: int | None = None
        self._ctx_asked = 0.0
        self._known = (0, 0)          # (message count, tokens) from the server's last usage report
        self._task_images = 0          # pictures from tools since the user's last message
        self._recent: deque = deque(maxlen=8)
        self.steps_used = 0
        self._efforts: list[str] | None = None
        self._vision: bool | None = None
        self._ready = False           # the server answered once in this chat
        self._clamped = False         # the last request's max_tokens was cut down to fit the context

    # ------------------------------------------------------------------ setup
    def system_prompt(self) -> str:
        if self._system is None:
            self.client.resolve_model()
            self.ctx.screen["model_name"] = self.client.model    # decides the click coordinate style
            self._system = prompts.build_system_prompt(
                self.settings, self.workspace, list(self.tools.values()), shell_kind(self.ctx),
                self.prompt_mode, coord_help(self.ctx) if "screenshot" in self.tools else "") + self.system_extra
        return self._system

    def context_limit(self) -> int:
        if self.settings.context_limit:
            return int(self.settings.context_limit)
        if self._ctx_limit is None and time.time() >= self._ctx_asked + 20:
            self._ctx_asked = time.time()       # not told (still loading?): asked again a bit later
            self._ctx_limit = self.client.context_size()
        return self._ctx_limit or 32768

    def context_known(self) -> bool:
        """The context size is set in Settings or came from the server (not the 32768 guess)."""
        self.context_limit()
        return bool(self.settings.context_limit or self._ctx_limit)

    def set_todos(self, todos: list[dict]):
        self.todos = todos
        if self.depth == 0:
            self.ui.todos(todos)

    def reset(self):
        """A new chat. What the server told us is asked again (it may run another model now)."""
        self.messages.clear()
        self.todos = []
        self._known = (0, 0)
        self._system = None
        self._ctx_limit, self._ctx_asked, self._efforts, self._vision, self._ready = None, 0.0, None, None, False
        self.client.forget()
        self.client.model = self.settings.model
        self.prompt_mode = self.settings.tool_mode == "prompt"
        self.policy.session_allowed.clear()
        shell.stop_background(self.ctx)     # the old chat's background commands
        self.ctx.coding.clear()
        self.set_workspace(self.settings.workspace_path())
        self.tools = {t.name: t for t in enabled_tools(self.settings, self.depth, self.workspace)}
        self.ui.todos([])

    def enable_group(self, group: str) -> bool:
        """Add a tool group that was left out at the start of the chat (a tool calls this through
        ctx.enable_group, e.g. once a Unity editor is connected). The new definitions change the
        prompt from the tool list on, so the server reads it once more."""
        new = {t.name: t for t in enabled_tools(self.settings, self.depth, self.workspace, full=(group,))
               if t.group == group and t.name not in self.tools}
        if not new:
            return False
        self.tools.update(new)
        self._system = None     # its guidance follows the tools
        return True

    def _sync_computer_use(self):
        """The Computer use toggle can change between messages: its tools come and go with it."""
        on = bool(getattr(self.settings, "computer_use", False)) and self.depth == 0
        have = [n for n, t in self.tools.items() if t.group == "control"]
        if on and not have:
            self.enable_group("control")
        elif have and not on:
            for n in have:
                del self.tools[n]
            self._system = None

    # ------------------------------------------------------------------ main loop
    def set_workspace(self, path: Path):
        """Switch the work folder mid-chat (coding mode works in its own copy of a repo)."""
        self.workspace = Path(path)
        self.ctx.workspace = self.workspace

    def run(self, text: str, images: list[bytes] | None = None) -> str:
        self.stop.clear()
        if not self._ready:
            # a server that is still loading answers 503 for a while: wait, so the model's name,
            # context size and thinking levels are right from the first request
            try:
                self.client.wait_until_ready(self.stop, self.ui.status)
            except Stopped:
                self.ui.notice("Stopped.")
                return ""
            self._ready, self._ctx_asked = True, 0.0
        self._sync_computer_use()
        self.messages.append(self._user_message(text, images))
        self._task_images = 0
        final = ""
        tool_rounds = 0
        lost = cut = 0          # replies in a row lost to a server crash / cut off by a length limit
        limit = int(self.settings.max_steps)
        for step in (range(limit) if limit > 0 else itertools.count()):
            if self.stop.is_set():
                self.ui.notice("Stopped.")
                break
            self.steps_used = step + 1
            self._maybe_compact()
            if self.stop.is_set():
                self.ui.notice("Stopped.")
                break
            try:
                turn = self._call_model()
            except ReplyLost as e:
                lost += 1
                self.ui.assistant_end(Turn(stopped=True))
                if lost > MAX_AUTO_RETRIES:
                    raise
                if e.pause:
                    self.ui.notice(f"{e} Trying again in {e.pause:.0f} seconds ({lost} of {MAX_AUTO_RETRIES}); "
                                   "closing other programs gives the model more room.")
                    if self.stop.wait(e.pause):
                        self.ui.notice("Stopped.")
                        break
                else:
                    self.ui.notice(f"{e} Trying again ({lost} of {MAX_AUTO_RETRIES}).")
                try:
                    self.client.wait_until_ready(self.stop, self.ui.status)
                except Stopped:
                    self.ui.notice("Stopped.")
                    break
                continue
            except ToolsUnsupported:
                if self.prompt_mode or self.settings.tool_mode == "native":
                    raise
                self.ui.notice("This server doesn't take tools the standard way, so tools are now "
                               "described in the prompt instead. (Start llama.cpp with --jinja to fix that.)")
                self.prompt_mode = True
                self._system = None
                turn = self._call_model()
            calls = list(turn.tool_calls)
            content_text = turn.content
            if not calls and ("<tool_call>" in content_text or "<function=" in content_text):
                content_text, calls = extract_text_tool_calls(content_text, self._param_schemas(),
                                                              at_end=not self.prompt_mode)
            if turn.stopped:
                calls = []      # a call cut short by Stop is neither run nor kept half-written
            self._record_assistant(turn, content_text, calls)
            if turn.stopped:
                self.ui.notice("Stopped.")
                break
            lost = 0
            if not calls:
                if turn.finish_reason == "length" and cut < MAX_AUTO_RETRIES:
                    # cut off by a length limit: carry on by itself (older steps get summarized
                    # first if the context is what ran out) instead of waiting for "continue"
                    cut += 1
                    self.ui.notice("The reply was cut off by a length limit, so it's carrying on from there.")
                    self.messages.append({"role": "user", "content": CARRY_ON, "_kind": "carry_on"})
                    continue
                if turn.finish_reason == "length":
                    self.ui.notice("The reply was still being cut off after a few tries. Raise 'Max reply "
                                   "tokens' in Settings or the 'Context length' on the Model server page.")
                final = content_text
                break
            cut = 0
            tool_rounds += 1
            cut_off = turn.finish_reason == "length" and not all(_valid_json(c.arguments) for c in calls)
            if cut_off:
                self.ui.notice("The reply hit its length limit in the middle of a tool call, so that call "
                               "wasn't run; the model was asked to do it in smaller pieces.")
            self._run_calls(calls, cut_off)
        else:
            self.ui.notice(f"Paused after {limit} steps (Settings > 'Steps before it checks in'; 0 = no limit). "
                           "Say 'continue' to keep going.")
        if final and tool_rounds >= 3 and self.depth == 0 and self.settings.auto_memory and not self.stop.is_set():
            self._save_memories(text, final)
        return final

    def _run_calls(self, calls: list[ToolCall], cut_off: bool = False):
        """Run one step's tool calls. Every call gets a result in the history whatever happens
        (Stop, an error): a call without one would break every later request."""
        last_screen = max((i for i, c in enumerate(calls) if c.name in SHOWS_SCREEN), default=-1)
        results: list[tuple[ToolCall, ToolResult]] = []
        try:
            for i, call in enumerate(calls):
                if self.stop.is_set():
                    results.append((call, ToolResult(STOPPED, ok=False)))
                    continue
                if cut_off and not _valid_json(call.arguments):
                    results.append((call, ToolResult(CUT_OFF, ok=False)))
                    continue
                # several screen actions in one step: only the last waits and takes a screenshot
                self.ctx.capture_after = i >= last_screen
                results.append((call, self.execute(call)))
        finally:
            self.ctx.capture_after = True
            why = STOPPED if self.stop.is_set() else "Not run: the step was interrupted."
            results += [(c, ToolResult(why, ok=False)) for c in calls[len(results):]]
            self._record_results(results)

    def _user_message(self, text: str, images: list[bytes] | None) -> dict:
        """The user's message as the model gets it: the chat's context note first (in the chat's
        first message only), then the text, then notes from memory that match it."""
        first = not any(m.get("_context") for m in self.messages)
        context = self._context_note() if first else ""
        if not first and not self.ctx.coding and self._noted_workspace() not in (None, str(self.workspace)):
            context = prompts.workspace_update(self.workspace)     # a new folder was picked mid-chat
        notes, ids = self._recalled(text)
        body = text or ("(see image)" if images else "")
        if images and self._no_vision():
            self.ui.notice("This model can't see images (the model server has no vision file loaded), "
                           "so the attached image(s) were left out.")
            body = "\n".join([text] * bool(text) + [NO_VISION] * len(images))
            images = None
        sent = context + body + notes
        content = [{"type": "text", "text": sent}] + [png_part(p) for p in images] if images else sent
        msg = {"role": "user", "content": content}
        if sent != text:
            msg["_shown"] = text      # what the chat window shows: without the context and notes
        if context:
            msg["_ws"] = str(self.workspace)
        if context and first:
            msg["_context"] = context
        if ids:
            msg["_notes"] = ids       # not attached again while this message is in the chat
        return msg

    def _noted_workspace(self) -> str | None:
        """The work folder the model was last told about (None for chats saved before this was kept)."""
        noted = None
        for m in self.messages:
            noted = m.get("_ws", noted)
        return noted

    def _context_note(self) -> str:
        """What used to be in the system prompt but differs per chat (date, work folder...). It goes
        in the first message instead, so the system prompt stays the same and stays cached."""
        return prompts.build_context(self.workspace)

    # ------------------------------------------------------------------ long-term memory
    def _recalled(self, text: str) -> tuple[str, list[int]]:
        """Saved notes that match this request, attached to it so the model sees them without
        having to search. Only a few, only good matches, and none the chat already shows (the
        ones attached to earlier messages that are still in it), to keep the context lean."""
        if self.depth > 0 or not self.settings.auto_recall or not text.strip():
            return "", []
        shown = {i for m in self.messages for i in m.get("_notes", ())}
        try:
            from .memory import get_memory

            notes = get_memory().recall_for(text, exclude=shown)
        except Exception:  # noqa: BLE001 - memory problems must never break the chat
            return "", []
        if not notes:
            return "", []
        return ("\n\n[From your long-term memory, may be relevant - ignore what isn't]\n"
                + "\n".join("- " + n.line() for n in notes)), [n.id for n in notes]

    def _save_memories(self, request: str, final: str):
        """Ask the model, as one more turn on top of this same conversation, what is worth
        remembering. The tools and thinking settings stay exactly as in the chat, so the server
        reuses everything it has read and only reads the question; a small thinking budget keeps
        the answer short."""
        try:
            from .memory import get_memory

            mem = get_memory()
            existing = mem.search_notes(f"{request} {final}", limit=6)
            ask = prompts.MEMORY_PROMPT.format(existing="\n".join("- " + n.line() for n in existing) or "(none)")
            question = {"role": "user", "content": ask}
            params = dict(self._params(1024), reasoning_budget_tokens=256)
            if self.context_known():     # the question and its answer must fit too (Strata refuses otherwise)
                room = self.context_limit() - self.estimate_total() - estimate_tokens(question) - 64
                if room < 384:
                    return               # no room left: the next message compacts the chat first anyway
                params["max_tokens"] = min(params["max_tokens"], room)
            if self.client.is_strata():
                params["strata_checkpoint"] = False     # a side question no later request builds on
            self.ui.status("Checking what's worth remembering...")
            turn = self.client.stream(self.wire_messages() + [question],
                                      self._tool_schemas(), params, stop=self.stop)
            items = _json_list(turn.content) + _remember_calls(turn, self._param_schemas())
        except Exception:  # noqa: BLE001 - a failed memory check is not worth bothering the user
            return
        finally:
            self.ui.status("")
        saved = []
        for it in items[:3]:
            if not isinstance(it, dict) or not str(it.get("note") or "").strip():
                continue
            note, title = str(it["note"]).strip()[:500], str(it.get("title") or "").strip()[:80]
            try:
                if it.get("replaces"):
                    n = mem.update(int(it["replaces"]), body=note, title=title or None)
                else:
                    n = mem.add(note, title)
            except (KeyError, ValueError, TypeError):
                continue
            saved.append(n.title or n.body[:60])
        if saved:
            self.ui.notice("Remembered: " + "; ".join(saved))

    def _params(self, max_tokens: int | None = None) -> dict:
        s = self.settings
        p = {"temperature": s.temperature, "top_p": s.top_p, "max_tokens": self._fit(max_tokens or s.max_tokens)}
        if s.top_k:
            p["top_k"] = s.top_k
        if s.min_p:
            p["min_p"] = s.min_p
        if s.auto_sampling:
            rec = sampling_for(self.client.resolve_model(), thinking=s.thinking != "off")
            if rec:
                p.update(rec)
        want = "high" if s.thinking == "on" else s.thinking
        if want == "off":
            p["chat_template_kwargs"] = {"enable_thinking": False}
            if "low" in self.effort_levels():   # models that can't switch it off (gpt-oss) think least
                p["reasoning_effort"] = p["chat_template_kwargs"]["reasoning_effort"] = "low"
        elif want in EFFORTS:
            # preserve_thinking: Qwen's templates otherwise drop the thinking of earlier turns, which loses the
            # reasoning behind earlier tool calls (Qwen recommends keeping it for agents) and changes the
            # start of the prompt, so the server can't reuse what it already read
            p["chat_template_kwargs"] = {"enable_thinking": True, "preserve_thinking": s.send_back_reasoning}
            level = pick_effort(want, self.effort_levels())
            if level:  # sent both ways: llama.cpp passes either one to the template
                p["reasoning_effort"] = p["chat_template_kwargs"]["reasoning_effort"] = level
        if self.prompt_mode:
            p["stop"] = ["<tool_response"]    # a model that writes the tool's answer itself stops there
        return p

    def _fit(self, want: int) -> int:
        """max_tokens, cut down so prompt + reply fit the context: Strata refuses a request where
        they don't (llama.cpp would just stop at the end of the context)."""
        want = int(want)
        self._clamped = False
        if not self.context_known():
            return want
        room = self.context_limit() - self.estimate_total() - 64
        if room < want:
            self._clamped = True
            return max(1024, room)
        return want

    def effort_levels(self) -> list[str]:
        if self._efforts is None:
            template = self.client.chat_template()
            if template is None:      # the server isn't answering yet: ask again next time
                return []
            self._efforts = effort_levels(template)
        return self._efforts

    def wire_messages(self) -> list[dict]:
        """The messages as sent to the server: our private keys removed."""
        out = [{"role": "system", "content": self.system_prompt()}]
        for m in self.messages:
            w = {k: v for k, v in m.items() if not k.startswith("_")}
            if not self.settings.send_back_reasoning:
                w.pop("reasoning_content", None)
            out.append(w)
        return out

    def _tool_schemas(self) -> list[dict] | None:
        return None if self.prompt_mode else [t.schema() for t in self.tools.values()]

    def _param_schemas(self) -> dict:
        return {name: t.params for name, t in self.tools.items()}

    def _call_model(self) -> Turn:
        self.ui.status("Thinking...")
        self.ui.assistant_start()
        try:
            try:
                turn = self._stream()
            except ImagesUnsupported:
                self._vision = False
                if not self._drop_all_images():
                    raise
                self.ui.notice("This model can't see images: the model server was started without its vision "
                               "file (mmproj). The images in this chat were replaced with a note.")
                turn = self._stream()
        finally:
            self.ui.status("")
        self.ui.assistant_end(turn)
        if turn.usage and not turn.stopped:
            total = int(turn.usage.get("prompt_tokens", 0)) + int(turn.usage.get("completion_tokens", 0))
            self._known = (len(self.messages) + 1, total)
        self.ui.usage(self.estimate_total(), self.context_limit(), turn.tokens_per_sec)
        return turn

    def _stream(self) -> Turn:
        return self.client.stream(self.wire_messages(), self._tool_schemas(), self._params(),
                                  on_text=self.ui.text, on_reasoning=self.ui.reasoning, stop=self.stop,
                                  on_status=lambda s: self.ui.status(s or "Thinking..."))

    def _fixed_tokens(self) -> int:
        """The system prompt plus the tool definitions: sent with every request, but in no message."""
        key = (self.system_prompt(), tuple(self.tools), self.prompt_mode)    # a new chat may have other tools
        if self._fixed is None or self._fixed_key != key:
            chars = len(key[0]) + len(json.dumps(self._tool_schemas() or []))
            self._fixed, self._fixed_key = int(chars / CHARS_PER_TOKEN) + 8, key
        return self._fixed

    def estimate_total(self) -> int:
        """Tokens in use: the server's own count plus a guess for anything added since. The
        pure character-count guess is used as a floor, to stay on the safe side."""
        guess = self._fixed_tokens() + sum(estimate_tokens(m) for m in self.messages)
        count, tokens = self._known
        if count > len(self.messages) or count == 0:
            return guess
        return max(guess, tokens + sum(estimate_tokens(m) for m in self.messages[count:]))

    def _record_assistant(self, turn: Turn, content: str, calls: list[ToolCall]):
        if turn.stopped:
            # keep what was written, minus a tool call that was cut short
            content = re.split(r"<tool_call>|<function=", content, maxsplit=1)[0].rstrip()
            if not content.strip():
                return
            msg = {"role": "assistant", "content": content}
        elif self.prompt_mode:
            msg = {"role": "assistant", "content": turn.content.split("<tool_response", 1)[0]}
        else:
            msg = {"role": "assistant", "content": content or ""}
            if calls:
                msg["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": _history_args(c.arguments)}}
                                     for c in calls]
        if turn.reasoning and self.settings.send_back_reasoning:
            msg["reasoning_content"] = turn.reasoning
        self.messages.append(msg)

    def _record_results(self, results: list[tuple[ToolCall, ToolResult]]):
        if any(r.images for _, r in results) and self._no_vision():
            note = "\n(The image isn't shown: this model can't see images.)"
            results = [(c, ToolResult(r.text + note, ok=r.ok) if r.images else r) for c, r in results]
        results = self._note_many_images(results)
        images = [(c, png) for c, r in results for png in r.images]
        if self.prompt_mode:
            text = "\n".join(f'<tool_response name="{c.name}">\n{r.text}\n</tool_response>' for c, r in results)
            content = [{"type": "text", "text": text}] + [png_part(p) for _, p in images] if images else text
            self.messages.append({"role": "user", "content": content, "_kind": "tool_response"})
        else:
            for c, r in results:
                self.messages.append({"role": "tool", "tool_call_id": c.id, "name": c.name, "content": r.text})
            if images:
                parts = [{"type": "text", "text": "Image(s) from the tool result(s) above:"}]
                parts += [png_part(p) for _, p in images]
                self.messages.append({"role": "user", "content": parts, "_kind": "tool_images"})
        self._prune_images()

    # ------------------------------------------------------------------ images
    def _note_many_images(self, results: list[tuple[ToolCall, ToolResult]]) -> list[tuple[ToolCall, ToolResult]]:
        """From the MANY_IMAGES-th picture of one task on, remind the model that tweaking a look it isn't sure
        about is the user's call (one model took ~20 screenshot-and-adjust rounds on a render that already ran)."""
        out = []
        for c, r in results:
            if r.images:
                self._task_images += len(r.images)
                if self._task_images >= MANY_IMAGES:
                    r = ToolResult(r.text + MANY_IMAGES_NOTE.format(n=self._task_images), ok=r.ok, images=r.images)
            out.append((c, r))
        return out

    def _no_vision(self) -> bool:
        """The server said it can't take images (llama.cpp or Strata without the vision file)."""
        if self._vision is None:
            self._vision = self.client.vision()
        return self._vision is False

    def _drop_all_images(self) -> int:
        """Replace every image in the chat with a note (the server turned them down)."""
        n = 0
        for m in self.messages:
            c = m.get("content")
            if isinstance(c, list):
                for i, p in enumerate(c):
                    if p.get("type") == "image_url":
                        c[i] = {"type": "text", "text": NO_VISION}
                        n += 1
        if n:
            self._known = (0, 0)
        return n

    # ------------------------------------------------------------------ tools
    def execute(self, call: ToolCall) -> ToolResult:
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(f"Error: there is no tool called '{call.name}'. Available: {', '.join(self.tools)}",
                              ok=False)
        try:
            args = call.parsed()
        except ValueError as e:
            return ToolResult(f"Error: {e}. Call {call.name} again with valid JSON arguments.", ok=False)
        if not isinstance(args, dict):
            return ToolResult(f"Error: the arguments of {call.name} must be a JSON object.", ok=False)
        missing = [k for k in tool.required if k not in args]
        if missing:
            return ToolResult(f"Error: {call.name} needs {', '.join(missing)}.", ok=False)

        try:
            risk = tool.risk_for(self.ctx, args) if tool.risk_for else tool.risk
            summary = tool.describe(args) if tool.describe else json.dumps(args, ensure_ascii=False)[:300]
        except Exception as e:  # noqa: BLE001 - e.g. a number where the tool wants text
            return ToolResult(f"Error: bad arguments for {call.name}: {type(e).__name__}: {e}", ok=False)
        self.ui.tool_start(call.id, call.name, args, summary)
        if tool.group == "control" and not getattr(self.settings, "computer_use", False):
            # turned off mid-task: from the next screen action on, nothing more is clicked or typed
            result = ToolResult("Not done: the user turned Computer use off. Don't use the screen, mouse or "
                                "keyboard tools; carry on without them or say what is left to do.", ok=False)
            self.ui.tool_end(call.id, call.name, result)
            return result
        add_on = tool.group == "mcp" or not getattr(tool.func, "__module__", "").startswith("localagent.")
        dangerous = risk != SAFE and is_dangerous(danger_text(args, every=add_on))
        need = self.policy.needs_approval(risk, dangerous)
        if need is None:
            result = ToolResult(f"Not done: the app is in read-only mode, and this would {risk_text(risk)}. "
                                "Tell the user what you would do instead.", ok=False)
            self.ui.tool_end(call.id, call.name, result)
            return result
        if need:
            self.ui.status("Waiting for your OK...")
            decision = self.ui.approve(ApprovalRequest(call.name, risk, summary, args, dangerous))
            self.ui.status("")
            if not decision.allow:
                note = f" The user said: {decision.note}" if decision.note else ""
                result = ToolResult(f"The user denied this action.{note} Do not retry it; adjust your approach.",
                                    ok=False)
                self.ui.tool_end(call.id, call.name, result)
                return result
            if decision.always and not dangerous:
                self.policy.session_allowed.add(risk)

        key = (call.name, json.dumps(args, sort_keys=True))
        repeats = sum(1 for k in self._recent if k == key)
        self._recent.append(key)
        self.ui.status(f"Running {call.name}...")
        started = time.time()
        try:
            out = tool.func(self.ctx, args)
            result = out if isinstance(out, ToolResult) else ToolResult(str(out))
        except ToolError as e:
            result = ToolResult(f"Error: {e}", ok=False)
        except Exception as e:  # noqa: BLE001 - every failure goes back to the model
            if type(e).__name__ == "FailSafeException":
                self.stop.set()
                result = ToolResult("Stopped: the user moved the mouse into a screen corner (emergency stop).",
                                    ok=False)
            else:
                result = ToolResult(f"Error: {type(e).__name__}: {e}", ok=False)
        finally:
            self.ui.status("")
        result.text = truncate(redact_all(result.text, self.settings), self._output_limit())
        if repeats >= 2:
            result.text += ("\n\nNote: you have made this exact call several times. If it isn't working, "
                            "try a different approach.")
        if time.time() - started > 1:
            result.text += f"\n(took {time.time() - started:.1f}s)"
        self.ui.tool_end(call.id, call.name, result)
        return result

    def _output_limit(self) -> int:
        """Characters per tool result: the setting, but no more than about a tenth of the context."""
        limit = int(self.settings.max_tool_output)
        if self.context_known():
            limit = min(limit, int(self.context_limit() * 0.1 * CHARS_PER_TOKEN))
        return limit

    def run_subagent(self, task: str) -> str:
        if self.depth >= 1:
            raise ToolError("Helpers can't start their own helpers.")
        sub = Agent(self.settings, _HelperUI(self.ui),
                    client=LLMClient(self.client.base_url, self.client.api_key, self.client.model),
                    depth=self.depth + 1, policy=self.policy, stop=self.stop)
        sub.system_extra = prompts.SUBAGENT_NOTE
        sub.set_workspace(self.workspace)
        sub._ctx_limit, sub._ready = self._ctx_limit, self._ready
        self.ui.notice(f"Started a helper: {task[:200]}")
        report = sub.run(task)
        self.ui.notice("Helper finished.")
        return "Helper's report:\n" + (report or "(the helper returned no report)")

    # ------------------------------------------------------------------ context management
    def _prune_images(self):
        """Clear older tool images, keeping the newest keep_images, once more than max_images
        have piled up. Rarely and many at once, because the server has to re-read the chat from the
        first changed message on. Images the user attached are kept: they're part of the request."""
        keep = max(1, int(self.settings.keep_images))
        # at least a few clears apart (keep == max would rewrite the history on every image)
        most = max(keep + max(4, keep), int(getattr(self.settings, "max_images", 8)))
        shots = [m for m in self.messages if m.get("_kind") in ("tool_images", "tool_response")]
        total = sum(_count_images(m) for m in shots)
        if total <= most:
            return
        to_drop = total - keep
        for m in shots:
            if to_drop <= 0:
                break
            c = m.get("content")
            if not isinstance(c, list):
                continue
            for i, p in enumerate(c):
                if to_drop > 0 and p.get("type") == "image_url":
                    c[i] = {"type": "text", "text": "[older image removed to save space]"}
                    to_drop -= 1
        self._known = (0, 0)

    def _maybe_compact(self):
        limit = self.context_limit()
        if self.estimate_total() > limit * float(self.settings.compact_at):
            self.compact()

    def compact(self) -> bool:
        limit = self.context_limit()
        # Cut points: the start of an assistant step or a real user message.
        bounds = [i for i, m in enumerate(self.messages)
                  if i > 0 and (m["role"] == "assistant" or (m["role"] == "user" and "_kind" not in m))]
        if not bounds:
            return False
        # Keep recent steps worth up to 40% of the context, minus what every request carries
        # anyway (system prompt, tools) and room for the summary, so compacting frees enough.
        tail_budget = max(0.1 * limit, 0.4 * limit - self._fixed_tokens() - 2048)
        split = bounds[-1]
        for b in reversed(bounds):
            if sum(estimate_tokens(m) for m in self.messages[b:]) > tail_budget:
                break
            split = b
        head, tail = self.messages[:split], self.messages[split:]
        if len(head) < 2:
            return False
        # Most of a long coding chat is what older steps carry word for word but no longer need. Shortening
        # that first keeps every step (what was done, to which file) instead of a summary of them.
        # Not enough: the messages go back as they were, so a summary made inside the conversation still
        # reuses what the server has read.
        steps = [i for i, m in enumerate(self.messages) if m["role"] == "assistant"]
        if len(steps) > LIGHT_KEEP:
            before, known = self.messages, self._known
            self.messages = copy.deepcopy(before[:steps[-LIGHT_KEEP]]) + before[steps[-LIGHT_KEEP]:]
            if self._lighten(self.messages[:steps[-LIGHT_KEEP]]) and self.estimate_total() <= limit * LIGHT_ENOUGH:
                self.ui.notice("Shortened older steps (file contents already written, old thinking, long results) "
                               "to free up context.")
                return True
            self.messages, self._known = before, known
        self.ui.status("Summarizing older messages to free up space...")
        try:
            summary = self._summarize(head, limit)
        except Stopped:
            return False
        finally:
            self.ui.status("")
        context = next((m["_context"] for m in self.messages if m.get("_context")), "")
        context_ws = next((m.get("_ws") for m in self.messages if m.get("_context")), None)
        noted = self._noted_workspace()
        request = self._latest_request(head)
        update = prompts.workspace_update(Path(noted)) if noted and context_ws and noted != context_ws else ""
        text = f"{context}{update}[Summary of the earlier part of this conversation]\n{summary}"
        if request and not any(m["role"] == "user" and "_kind" not in m for m in tail):
            text += f"\n\n[The user's latest request, word for word]\n{request}"
        if self.todos:
            text += "\n\n[Current to-do list]\n" + "\n".join(f"- [{t['status']}] {t['content']}" for t in self.todos)
        msg = {"role": "user", "content": text, "_kind": "summary"}
        if request:
            msg["_request"] = request      # carried into the next summary word for word
        if context:
            msg["_context"] = context
        if noted:
            msg["_ws"] = noted
        self.messages = [msg] + tail
        self._known = (0, 0)
        self.ui.notice(f"Summarized {len(head)} older messages to free up context.")
        return True

    def _lighten(self, msgs: list[dict]) -> bool:
        """Shorten older steps in place: the long arguments of their tool calls (mostly contents of files
        already written: the files on disk have them), their thinking, and long tool results. Returns
        whether anything changed."""
        changed = False
        for m in msgs:
            if m["role"] == "assistant":
                if m.get("reasoning_content"):
                    m["reasoning_content"] = ""
                    changed = True
                for c in m.get("tool_calls") or []:
                    f = c.get("function") or {}
                    short = _shorten_args(f.get("arguments") or "")
                    if short is not None:
                        f["arguments"] = short
                        changed = True
            elif m["role"] == "tool" and isinstance(m.get("content"), str) and len(m["content"]) > LIGHT_RESULT:
                t = m["content"]
                m["content"] = (t[:LIGHT_RESULT * 2 // 3] + f"\n[... {len(t) - LIGHT_RESULT * 5 // 6} characters "
                                "cut from this older result to save space ...]\n" + t[-LIGHT_RESULT // 6:])
                changed = True
        if changed:
            self._known = (0, 0)      # the server's count was for the longer messages
        return changed

    def _summarize(self, head: list[dict], limit: int) -> str:
        """A summary of the conversation, made the cheapest way there's room for. Best: one more
        turn of the conversation itself, with the same tools and settings, so the server reuses all
        it has read and only reads the question. Otherwise (no room, or that failed): a transcript of
        the older messages in a fresh request. Raises Stopped on Stop."""
        def waiting(s):
            self.ui.status(s or "Summarizing older messages to free up space...")

        if self.estimate_total() + SUMMARY_TOKENS + 200 <= limit:
            params = dict(self._params(SUMMARY_TOKENS), reasoning_budget_tokens=512)
            if self.client.is_strata():
                params["strata_checkpoint"] = False     # a side branch no later request continues
            try:
                turn = self.client.stream(self.wire_messages() + [{"role": "user", "content": prompts.COMPACT_PROMPT}],
                                          self._tool_schemas(), params, stop=self.stop, on_status=waiting)
            except ServerError:
                turn = Turn()
            if turn.stopped or self.stop.is_set():
                raise Stopped()
            summary = strip_thinking(turn.content)
            if len(summary) >= 40:
                return summary
        transcript = self._transcript(head, max_chars=int(limit * 0.5 * CHARS_PER_TOKEN))
        try:
            summary = self.client.complete(
                [{"role": "system", "content": "You write precise summaries of agent work sessions."},
                 {"role": "user", "content": transcript + "\n\n" + prompts.COMPACT_PROMPT}],
                {"temperature": 0.3, "max_tokens": 2048, "chat_template_kwargs": {"enable_thinking": False},
                 "reasoning_effort": "low"}, stop=self.stop)
        except ServerError as e:
            self.ui.notice(f"Couldn't summarize older messages ({e}); keeping the end of them word for word.")
            summary = ""
        if len(summary) >= 40:
            return summary
        return "(No summary could be made. The end of the earlier messages, word for word:)\n" + transcript[-6000:]

    @staticmethod
    def _latest_request(head: list[dict]) -> str | None:
        """The user's latest request in head, in their own words; from an earlier summary if the
        request itself was summarized away already."""
        for m in reversed(head):
            if m["role"] == "user" and "_kind" not in m:
                if "_shown" in m:     # without the context note and recalled notes
                    return m["_shown"] or "(an image, no text)"
                return Agent._plain(m)
            if m.get("_kind") == "summary" and m.get("_request"):
                return m["_request"]
        return None

    @staticmethod
    def _plain(m: dict) -> str:
        c = m.get("content")
        if isinstance(c, list):
            return " ".join(p.get("text", "[image]") if p.get("type") == "text" else "[image]" for p in c)
        return c or ""

    def _transcript(self, msgs: list[dict], max_chars: int) -> str:
        lines = []
        for m in msgs:
            text = self._plain(m)
            if m["role"] == "assistant":
                calls = "; ".join(f"{c['function']['name']}({c['function']['arguments'][:400]})"
                                  for c in m.get("tool_calls", []))
                lines.append(f"ASSISTANT: {text[:3000]}" + (f"\n  [called: {calls}]" if calls else ""))
            elif m["role"] == "tool":
                lines.append(f"TOOL RESULT ({m.get('name', '')}): {text[:1500]}")
            elif m.get("_kind") == "summary":     # whole: it holds everything before it
                lines.append(f"EARLIER SUMMARY: {text[len(m.get('_context', '')):]}")
            else:
                lines.append(f"USER: {m.get('_shown', text)[:4000]}")
        out = "\n".join(lines)
        return out[-max_chars:]

    # ------------------------------------------------------------------ saving
    def to_json(self) -> dict:
        return {"messages": self.messages, "todos": self.todos, "model": self.client.model,
                "workspace": str(self.workspace), "coding": self.ctx.coding, "saved": time.strftime("%Y-%m-%d %H:%M")}

    def load_json(self, data: dict):
        self.messages = list(data.get("messages") or [])
        ws = Path(data.get("workspace") or "")
        if data.get("workspace") and ws.is_dir():
            self.set_workspace(ws)
        self.ctx.coding.clear()
        self.ctx.coding.update(data.get("coding") or {})
        self.set_todos(list(data.get("todos") or []))
        self._known = (0, 0)

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False), encoding="utf-8")


def _shorten_args(args: str) -> str | None:
    """Tool call arguments with each long text cut to its start, still valid JSON; None when there's
    nothing to cut (or they aren't a JSON object)."""
    if len(args) <= LIGHT_ARG:
        return None
    try:
        data = json.loads(args)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    cut = False
    for k, v in data.items():
        if isinstance(v, str) and len(v) > LIGHT_ARG:
            data[k] = v[:LIGHT_ARG // 3] + f"[... {len(v) - LIGHT_ARG // 3} more characters cut from this older step]"
            cut = True
    return json.dumps(data, ensure_ascii=False) if cut else None


def _json_list(text: str) -> list:
    """The first JSON list in a reply, forgiving code fences, thinking and chatter around it."""
    text = strip_thinking(text)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def _remember_calls(turn: Turn, schemas: dict) -> list[dict]:
    """remember calls the model made instead of answering with a list (the tools are offered)."""
    calls = list(turn.tool_calls)
    if not calls and ("<tool_call>" in turn.content or "<function=" in turn.content):
        calls = extract_text_tool_calls(turn.content, schemas)[1]
    out = []
    for c in calls:
        if c.name == "remember":
            try:
                out.append(c.parsed())
            except ValueError:
                pass
    return out


class _HelperUI(AgentUI):
    """A helper agent shows its progress as status lines and borrows the parent's approvals."""

    def __init__(self, parent: AgentUI):
        self.parent = parent

    def tool_start(self, call_id, name, args, summary):
        self.parent.status(f"Helper: {summary[:120]}")

    def approve(self, req):
        req.summary = "(helper) " + req.summary
        return self.parent.approve(req)
