"""The agent loop: ask the model, run the tools it calls, feed back results, repeat."""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections import deque
from pathlib import Path

from . import prompts
from .guide import sampling_for
from .llm import LLMClient, ServerError, ToolCall, ToolsUnsupported, Turn, extract_text_tool_calls
from .permissions import ApprovalRequest, Decision, Policy, is_dangerous, risk_text
from .tools import SAFE, ToolContext, ToolError, ToolResult, enabled_tools
from .tools.base import truncate
from .tools.screen import coord_help
from .tools.shell import shell_kind

IMAGE_TOKENS = 1200          # rough cost of one screenshot, for estimates only
CHARS_PER_TOKEN = 3.5


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


class Agent:
    def __init__(self, settings, ui: AgentUI | None = None, client: LLMClient | None = None,
                 depth: int = 0, policy: Policy | None = None, stop: threading.Event | None = None):
        self.settings = settings
        self.ui = ui or AgentUI()
        self.client = client or LLMClient(settings.base_url, settings.api_key, settings.model)
        self.depth = depth
        self.stop = stop or threading.Event()
        self.policy = policy or Policy(settings.approval_mode, settings.always_ask_dangerous,
                                     settings.always_ask_publish)
        self.workspace = settings.workspace_path()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.ctx = ToolContext(settings=settings, workspace=self.workspace, stop=self.stop,
                               ask_user=self.ui.ask, agent=self)
        self.tools = {t.name: t for t in enabled_tools(settings, depth)}
        self.prompt_mode = settings.tool_mode == "prompt"
        self.messages: list[dict] = []
        self.todos: list[dict] = []
        self.system_extra = ""
        self._system: str | None = None
        self._ctx_limit: int | None = None
        self._known = (0, 0)          # (message count, tokens) from the server's last usage report
        self._recent: deque = deque(maxlen=8)
        self.steps_used = 0
        self._efforts: list[str] | None = None

    # ------------------------------------------------------------------ setup
    def system_prompt(self) -> str:
        if self._system is None:
            self.client.resolve_model()
            self.ctx.screen["model_name"] = self.client.model
            self._system = prompts.build_system_prompt(
                self.settings, self.workspace, list(self.tools.values()), coord_help(self.ctx),
                shell_kind(self.ctx), self.prompt_mode) + self.system_extra
        return self._system

    def context_limit(self) -> int:
        if self.settings.context_limit:
            return int(self.settings.context_limit)
        if self._ctx_limit is None:
            self._ctx_limit = self.client.context_size() or 32768
        return self._ctx_limit

    def set_todos(self, todos: list[dict]):
        self.todos = todos
        if self.depth == 0:
            self.ui.todos(todos)

    def reset(self):
        self.messages.clear()
        self.todos = []
        self._known = (0, 0)
        self._system = None
        self.policy.session_allowed.clear()
        self.ctx.coding.clear()
        self.set_workspace(self.settings.workspace_path())
        self.ui.todos([])

    # ------------------------------------------------------------------ main loop
    def set_workspace(self, path: Path):
        """Switch the work folder mid-chat (coding mode works in its own copy of a repo)."""
        self.workspace = Path(path)
        self.ctx.workspace = self.workspace

    def run(self, text: str, images: list[bytes] | None = None) -> str:
        self.stop.clear()
        sent = text + self._recalled(text)
        if images:
            content = [{"type": "text", "text": sent or "(see image)"}] + [png_part(p) for p in images]
        else:
            content = sent
        msg = {"role": "user", "content": content}
        if sent != text:
            msg["_shown"] = text      # what the chat window shows: without the attached notes
        self.messages.append(msg)
        first_step = len(self.messages)
        final = ""
        for step in range(int(self.settings.max_steps)):
            if self.stop.is_set():
                self.ui.notice("Stopped.")
                break
            self.steps_used = step + 1
            self._maybe_compact()
            try:
                turn = self._call_model()
            except ToolsUnsupported:
                if self.prompt_mode:
                    raise
                self.ui.notice("This server doesn't take tools the standard way, so tools are now "
                               "described in the prompt instead. (Start llama.cpp with --jinja to fix that.)")
                self.prompt_mode = True
                self._system = None
                turn = self._call_model()
            calls = list(turn.tool_calls)
            content_text = turn.content
            if not calls and ("<tool_call>" in content_text or "<function=" in content_text):
                content_text, calls = extract_text_tool_calls(content_text)
            self._record_assistant(turn, content_text, calls)
            if turn.stopped:
                self.ui.notice("Stopped.")
                break
            if not calls:
                if turn.finish_reason == "length":
                    self.ui.notice("The reply hit the max-tokens limit and was cut off. Raise "
                                   "'Max reply tokens' in Settings, or say 'continue'.")
                final = content_text
                break
            results = []
            for call in calls:
                if self.stop.is_set():
                    results.append((call, ToolResult("Not run: the user pressed Stop.", ok=False)))
                    continue
                results.append((call, self.execute(call)))
            self._record_results(results)
        else:
            self.ui.notice(f"Paused after {self.settings.max_steps} steps. Say 'continue' to keep going.")
        tool_rounds = sum(1 for m in self.messages[first_step:] if m["role"] == "assistant" and
                          (m.get("tool_calls") or "<tool_call>" in str(m.get("content", ""))))
        if final and tool_rounds >= 3 and self.depth == 0 and self.settings.auto_memory and not self.stop.is_set():
            self._save_memories(text, final)
        return final

    # ------------------------------------------------------------------ long-term memory
    def _recalled(self, text: str) -> str:
        """Saved notes that match this request, attached to it so the model sees them without
        having to search. Only a few, and only good matches, to keep the context lean."""
        if self.depth > 0 or not self.settings.auto_recall or not text.strip():
            return ""
        try:
            from .memory import get_memory

            notes = get_memory().recall_for(text)
        except Exception:  # noqa: BLE001 - memory problems must never break the chat
            return ""
        if not notes:
            return ""
        return ("\n\n[From your long-term memory, may be relevant - ignore what isn't]\n"
                + "\n".join("- " + n.line() for n in notes))

    def _save_memories(self, request: str, final: str):
        """Ask the model, as one more turn on top of this same conversation (so the server's
        prompt cache is reused and it costs only a short reply), what is worth remembering."""
        try:
            from .memory import get_memory

            mem = get_memory()
            existing = mem.search_notes(f"{request} {final}", limit=6)
            ask = prompts.MEMORY_PROMPT.format(existing="\n".join("- " + n.line() for n in existing) or "(none)")
            tools = None if self.prompt_mode else [t.schema() for t in self.tools.values()]
            params = dict(self._params(), max_tokens=700, chat_template_kwargs={"enable_thinking": False},
                          reasoning_effort="low")
            self.ui.status("Checking what's worth remembering...")
            turn = self.client.stream(self.wire_messages() + [{"role": "user", "content": ask}], tools, params,
                                      stop=self.stop)
            items = _json_list(turn.content)
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

    def _params(self) -> dict:
        s = self.settings
        p = {"temperature": s.temperature, "top_p": s.top_p, "max_tokens": s.max_tokens}
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
            p["chat_template_kwargs"] = {"enable_thinking": True}
            level = pick_effort(want, self.effort_levels())
            if level:  # sent both ways: llama.cpp passes either one to the template
                p["reasoning_effort"] = p["chat_template_kwargs"]["reasoning_effort"] = level
        return p

    def effort_levels(self) -> list[str]:
        if self._efforts is None:
            self._efforts = effort_levels(self.client.chat_template())
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

    def _call_model(self) -> Turn:
        self.ui.status("Thinking...")
        self.ui.assistant_start()
        tools = None if self.prompt_mode else [t.schema() for t in self.tools.values()]
        try:
            turn = self.client.stream(self.wire_messages(), tools, self._params(),
                                      on_text=self.ui.text, on_reasoning=self.ui.reasoning, stop=self.stop)
        finally:
            self.ui.status("")
        self.ui.assistant_end(turn)
        if turn.usage:
            total = int(turn.usage.get("prompt_tokens", 0)) + int(turn.usage.get("completion_tokens", 0))
            self._known = (len(self.messages) + 1, total)
        self.ui.usage(self.estimate_total(), self.context_limit(), turn.tokens_per_sec)
        return turn

    def estimate_total(self) -> int:
        """Tokens in use: the server's own count plus a guess for anything added since. The
        pure character-count guess is used as a floor, to stay on the safe side."""
        guess = estimate_tokens({"content": self.system_prompt()}) + sum(estimate_tokens(m) for m in self.messages)
        count, tokens = self._known
        if count > len(self.messages) or count == 0:
            return guess
        return max(guess, tokens + sum(estimate_tokens(m) for m in self.messages[count:]))

    def _record_assistant(self, turn: Turn, content: str, calls: list[ToolCall]):
        if self.prompt_mode:
            msg = {"role": "assistant", "content": turn.content}
        else:
            msg = {"role": "assistant", "content": content or ""}
            if calls:
                msg["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": c.arguments}} for c in calls]
        if turn.reasoning and self.settings.send_back_reasoning:
            msg["reasoning_content"] = turn.reasoning
        self.messages.append(msg)

    def _record_results(self, results: list[tuple[ToolCall, ToolResult]]):
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
        missing = [k for k in tool.required if k not in args]
        if missing:
            return ToolResult(f"Error: {call.name} needs {', '.join(missing)}.", ok=False)

        risk = tool.risk_for(self.ctx, args) if tool.risk_for else tool.risk
        summary = tool.describe(args) if tool.describe else json.dumps(args, ensure_ascii=False)[:300]
        self.ui.tool_start(call.id, call.name, args, summary)
        dangerous = risk != SAFE and is_dangerous(" ".join(str(v) for v in args.values()))
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
        result.text = truncate(result.text, int(self.settings.max_tool_output))
        if repeats >= 2:
            result.text += ("\n\nNote: you have made this exact call several times. If it isn't working, "
                            "try a different approach.")
        if time.time() - started > 1:
            result.text += f"\n(took {time.time() - started:.1f}s)"
        self.ui.tool_end(call.id, call.name, result)
        return result

    def run_subagent(self, task: str) -> str:
        if self.depth >= 1:
            raise ToolError("Helpers can't start their own helpers.")
        sub = Agent(self.settings, _HelperUI(self.ui),
                    client=LLMClient(self.client.base_url, self.client.api_key, self.client.model),
                    depth=self.depth + 1, policy=self.policy, stop=self.stop)
        sub.system_extra = prompts.SUBAGENT_NOTE
        sub.set_workspace(self.workspace)
        sub._ctx_limit = self._ctx_limit
        self.ui.notice(f"Started a helper: {task[:200]}")
        report = sub.run(task)
        self.ui.notice("Helper finished.")
        return "Helper's report:\n" + (report or "(the helper returned no report)")

    # ------------------------------------------------------------------ context management
    def _prune_images(self):
        """Keep only the newest few screenshots. Done in batches so the server's prompt cache
        stays valid most of the time (rewriting old messages forces it to re-read them)."""
        keep = max(1, int(self.settings.keep_images))
        total = sum(_count_images(m) for m in self.messages)
        if total <= keep + 2:
            return
        to_drop = total - keep
        for m in self.messages:
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
        tail_budget = limit * 0.3
        split = bounds[-1]
        for b in reversed(bounds):
            if sum(estimate_tokens(m) for m in self.messages[b:]) > tail_budget:
                break
            split = b
        head, tail = self.messages[:split], self.messages[split:]
        if len(head) < 2:
            return False
        self.ui.status("Summarizing older messages to free up space...")
        transcript = self._transcript(head, max_chars=int(limit * 0.5 * CHARS_PER_TOKEN))
        try:
            summary = self.client.complete(
                [{"role": "system", "content": "You write precise summaries of agent work sessions."},
                 {"role": "user", "content": transcript + "\n\n" + prompts.COMPACT_PROMPT}],
                {"temperature": 0.3, "max_tokens": 2048, "chat_template_kwargs": {"enable_thinking": False}})
        except ServerError as e:
            self.ui.notice(f"Couldn't summarize older messages ({e}); dropping them instead.")
            summary = "(earlier messages were dropped)"
        finally:
            self.ui.status("")
        last_user = next((m for m in reversed(head) if m["role"] == "user" and "_kind" not in m), None)
        text = f"[Summary of the earlier part of this conversation]\n{summary}"
        if last_user and not any(m["role"] == "user" and "_kind" not in m for m in tail):
            text += f"\n\n[The user's latest request, word for word]\n{self._plain(last_user)}"
        if self.todos:
            text += "\n\n[Current to-do list]\n" + "\n".join(f"- [{t['status']}] {t['content']}" for t in self.todos)
        self.messages = [{"role": "user", "content": text, "_kind": "summary"}] + tail
        self._known = (0, 0)
        self.ui.notice(f"Summarized {len(head)} older messages to free up context.")
        return True

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
            else:
                lines.append(f"USER: {text[:4000]}")
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


def _json_list(text: str) -> list:
    """The first JSON list in a reply, forgiving code fences and chatter around it."""
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S)
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    return data if isinstance(data, list) else []


class _HelperUI(AgentUI):
    """A helper agent shows its progress as status lines and borrows the parent's approvals."""

    def __init__(self, parent: AgentUI):
        self.parent = parent

    def tool_start(self, call_id, name, args, summary):
        self.parent.status(f"Helper: {summary[:120]}")

    def approve(self, req):
        req.summary = "(helper) " + req.summary
        return self.parent.approve(req)
