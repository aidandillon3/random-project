"""The server's prompt cache, end to end: real multi-step tasks run through Agent against the fake
model server, with every request body captured byte for byte.

llama.cpp and Strata reuse what they have already read of the prompt only up to the first byte
that differs from the previous request. The chat template renders, in order: the reasoning-effort
sentence (reasoning_effort / chat_template_kwargs), the tool definitions, the system message, then
the messages. So from one main-chat request to the next, the reasoning settings, the tool list and
messages[0..n] must stay byte for byte the same, with new content only added at the end.

Side questions (the compaction summary, the memory check, a helper agent's chat) may look different,
but afterwards the main chat must carry on from the same prefix as before. Only the request right
after a compaction may start a new prefix, once, and clearing old screenshots may break it once
per batch of screenshots, never on every step.
"""

import io
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localagent import prompts, tools as tools_mod  # noqa: E402
from localagent.agent import CHARS_PER_TOKEN, IMAGE_TOKENS, Agent, AgentUI  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.permissions import Decision  # noqa: E402
from localagent.tools import screen  # noqa: E402
from tests.mockserver import MockServer, calls, text  # noqa: E402

# A chat template that knows thinking levels (Qwen3.8 style), so reasoning_effort is really sent.
QWEN38 = ("{%- if reasoning_effort == 'xhigh' %}..{%- elif reasoning_effort == 'medium' %}.."
          "{%- elif reasoning_effort == 'low' %}")
REASONING_KEYS = ("reasoning_effort", "chat_template_kwargs")
MEMORY_Q = prompts.MEMORY_PROMPT.split("]")[0]       # "[Memory check - not from the user"
SUMMARY_Q = prompts.COMPACT_PROMPT.split("]")[0]     # "[Context check - not from the user"
SUMMARY = ("SUMMARY: the user wants the server logs checked for errors. day1 to day5 are read; ERRORs "
           "seen on day2 and day3 (disk full). Left: the rest of the logs, then a short report.")
PNG = b"\x89PNG\r\n\x1a\n"


def reasoning(effort):
    return {"reasoning_effort": effort, "chat_template_kwargs": {"enable_thinking": True, "reasoning_effort": effort}}


def thinking(reply: dict, thought: str) -> dict:
    """A scripted reply that also streams reasoning_content first."""
    return dict(reply, reasoning=thought)


def last_text(body) -> str:
    c = body["messages"][-1].get("content")
    return c if isinstance(c, str) else json.dumps(c)


def images_in(messages) -> int:
    return sum(1 for m in messages if isinstance(m.get("content"), list)
               for p in m["content"] if p.get("type") == "image_url")


def estimate(body) -> int:
    """Prompt tokens of a request by the app's own rough rule (3.5 characters a token, 8 a message,
    1200 an image), counted from what was actually sent."""
    total = int(len(json.dumps(body.get("tools") or [])) / CHARS_PER_TOKEN)
    for m in body["messages"]:
        c = m.get("content")
        parts = c if isinstance(c, list) else [{"type": "text", "text": c or ""}]
        chars = sum(len(p.get("text", "")) for p in parts)
        chars += len(json.dumps(m.get("tool_calls", ""))) + len(m.get("reasoning_content", "") or "")
        total += int(chars / CHARS_PER_TOKEN) + 8 + IMAGE_TOKENS * sum(p.get("type") == "image_url" for p in parts)
    return total


# ---------------------------------------------------------------------- comparing two prompts
def prompt_parts(body) -> list[tuple[str, str]]:
    """What the template turns into the prompt, in its order, each part as the exact JSON sent."""
    parts = [("reasoning settings", json.dumps({k: body[k] for k in REASONING_KEYS if k in body})),
             ("tools", json.dumps({"tools": body.get("tools"), "tool_choice": body.get("tool_choice")}))]
    return parts + [(f"messages[{i}]", json.dumps(m)) for i, m in enumerate(body["messages"])]


def diff_path(x, y, path="") -> str:
    """Where in two JSON values the first difference is, e.g. .content[1].image_url.url."""
    if type(x) is not type(y):
        return path or "(value)"
    if isinstance(x, dict):
        for k in list(x) + [k for k in y if k not in x]:
            if k not in x or k not in y:
                return f"{path}.{k} ({'added' if k not in x else 'removed'})"
            if x[k] != y[k]:
                return diff_path(x[k], y[k], f"{path}.{k}")
        return f"{path} (key order)"
    if isinstance(x, list):
        for i, (p, q) in enumerate(zip(x, y)):
            if p != q:
                return diff_path(p, q, f"{path}[{i}]")
        return f"{path} (length {len(x)} -> {len(y)})"
    return path or "(value)"


def prefix_break(prev, nxt) -> str | None:
    """None when nxt carries prev's whole prompt byte for byte and adds to it; otherwise where and
    how it differs (the field, the byte offset in that part, and the bytes around it)."""
    a, b = prompt_parts(prev), prompt_parts(nxt)
    for (name, x), (_, y) in zip(a, b):
        if x != y:
            at = next((i for i, (p, q) in enumerate(zip(x, y)) if p != q), min(len(x), len(y)))
            field = diff_path(json.loads(x), json.loads(y))
            return (f"{name}{field} differs at byte {at}: before {x[max(0, at - 50):at + 70]!r}, "
                    f"after {y[max(0, at - 50):at + 70]!r}")
    if len(b) <= len(a):
        return "no new messages"
    return None


def first_changed_message(prev, nxt) -> int | None:
    for i, (m, n) in enumerate(zip(prev["messages"], nxt["messages"])):
        if json.dumps(m) != json.dumps(n):
            return i
    return None


# ---------------------------------------------------------------------- the fake server
class _Script(list):
    """The main chat's scripted replies, in order. The side questions get their own answers: the
    compaction summary and the memory check are answered whenever they come."""

    def __init__(self, srv, replies):
        super().__init__(replies)
        self.srv = srv

    def __bool__(self):
        return True          # MockServer asks `if replies` before popping: always come here

    def pop(self, index=0):
        t = last_text(self.srv.requests[-1])
        if t.startswith(SUMMARY_Q):
            return text(self.srv.summary, reasoning="What matters for going on?")
        if t.startswith(MEMORY_Q):
            return text(self.srv.memories.pop(0) if self.srv.memories else "[]")
        return super().pop(0) if len(self) else text("done")


class CacheServer(MockServer):
    """tests/mockserver.py's server plus: /props with the context size, a chat template that knows
    thinking levels and vision; the raw bytes of every request; side questions answered apart."""

    def __init__(self, replies, n_ctx=32768, summary=SUMMARY, memories=()):
        super().__init__([], n_ctx=n_ctx)
        self.replies = _Script(self, replies)
        self.complete_reply = summary          # the transcript summary (no room for one in the chat)
        self.summary, self.memories, self.raw = summary, list(memories), []
        props = {"default_generation_settings": {"n_ctx": n_ctx}, "chat_template": QWEN38,
                 "modalities": {"vision": True}}
        outer, base = self, self.httpd.RequestHandlerClass

        class H(base):
            def do_GET(self):
                if self.path == "/props":
                    return self._json(200, props)
                return super().do_GET()

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                outer.raw.append(raw)
                real, self.rfile = self.rfile, io.BytesIO(raw)
                try:
                    super().do_POST()
                finally:
                    self.rfile = real

        self.httpd.RequestHandlerClass = H


class RecUI(AgentUI):
    def __init__(self):
        self.notices = []

    def notice(self, s):
        self.notices.append(s)

    def approve(self, req):
        return Decision(True)


# ---------------------------------------------------------------------- shared checks
class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())
        # the same tool set on every machine (a GitHub CLI login or a Unity project would add tools)
        for p in (mock.patch.object(tools_mod, "github_signed_in", return_value=False),
                  mock.patch.object(tools_mod.unity, "detected", return_value=False, create=True)):
            p.start()
            self.addCleanup(p.stop)

    def make(self, replies, n_ctx=32768, summary=SUMMARY, memories=(), **settings) -> Agent:
        self.n_ctx = n_ctx
        self.srv = CacheServer(replies, n_ctx, summary, memories)
        self.addCleanup(self.srv.close)
        kw = {"enable_control": False, "approval_mode": "auto", "thinking": "medium", **settings}
        self.ui = RecUI()
        self.agent = Agent(Settings(base_url=self.srv.url, workspace=str(self.ws), **kw), self.ui)
        return self.agent

    # what each request was
    def kind(self, body) -> str:
        if not body.get("stream"):
            return "transcript summary"
        t = last_text(body)
        if t.startswith(MEMORY_Q):
            return "memory check"
        if t.startswith(SUMMARY_Q):
            return "summary"
        if body["messages"][0]["content"] != self.agent.system_prompt():
            return "helper agent"
        return "main"

    def kinds(self) -> list[str]:
        return [self.kind(b) for b in self.srv.requests]

    def mains(self) -> list[int]:
        return [i for i, b in enumerate(self.srv.requests) if self.kind(b) == "main"]

    def main_breaks(self) -> list[dict]:
        """Every pair of consecutive main-chat requests where the later one doesn't carry the earlier
        one's whole prompt, with what was asked in between and where they differ."""
        bodies, out = self.srv.requests, []
        mains = self.mains()
        for p, n in zip(mains, mains[1:]):
            why = prefix_break(bodies[p], bodies[n])
            if why:
                out.append({"after": p, "at": n, "between": [self.kind(b) for b in bodies[p + 1:n]], "why": why,
                            "message": first_changed_message(bodies[p], bodies[n])})
        return out

    def assert_cache_friendly(self, effort: str):
        """What holds in every scenario: the bytes compared are the bytes sent; one tool list and one
        thinking level for the main chat and its side questions; side questions carry the main chat's
        whole prompt; a helper agent's chat keeps its own prefix; max_tokens fits the context."""
        self.assert_same_prompt_settings(effort)
        self.assert_max_tokens_fit()

    def assert_same_prompt_settings(self, effort: str):
        bodies = self.srv.requests
        self.assertEqual(len(self.srv.raw), len(bodies))
        for raw, body in zip(self.srv.raw, bodies):
            self.assertEqual(raw, json.dumps(body).encode())      # re-serialized JSON == the bytes on the wire

        tools = json.dumps(self.agent._tool_schemas())
        for i, b in enumerate(bodies):
            k = self.kind(b)
            if k in ("main", "memory check", "summary"):
                self.assertEqual(json.dumps(b.get("tools")), tools, f"request {i} ({k}): another tool list")
                self.assertEqual(b.get("tool_choice"), "auto", f"request {i} ({k})")
                self.assertEqual({key: b.get(key) for key in REASONING_KEYS}, reasoning(effort),
                                 f"request {i} ({k}): another thinking level")

        last_main = None
        for i, b in enumerate(bodies):
            k = self.kind(b)
            if k == "main":
                last_main = b
            elif k in ("memory check", "summary"):
                self.assertIsNotNone(last_main)
                self.assertIsNone(prefix_break(last_main, b),
                                  f"request {i} ({k}) doesn't carry on from the main chat's prompt")

        helper_chats: list[list[dict]] = []
        for b in bodies:
            if self.kind(b) == "helper agent":
                if len(b["messages"]) == 2:          # system + task: a new helper chat
                    helper_chats.append([])
                helper_chats[-1].append(b)
        for chat in helper_chats:
            self.assertEqual(len({json.dumps(b.get("tools")) for b in chat}), 1)
            for a, b in zip(chat, chat[1:]):
                self.assertIsNone(prefix_break(a, b), "the helper agent's own chat lost its prefix")

    def assert_max_tokens_fit(self):
        """Every request leaves its prompt room: max_tokens <= context - estimated prompt, and at
        least 1024 (384 for the memory check, whose answer is a short list)."""
        for i, b in enumerate(self.srv.requests):
            room = self.n_ctx - estimate(b)
            floor = 384 if self.kind(b) == "memory check" else 1024
            self.assertGreaterEqual(b["max_tokens"], floor, f"request {i} ({self.kind(b)})")
            self.assertLessEqual(b["max_tokens"], room,
                                 f"request {i} ({self.kind(b)}): max_tokens {b['max_tokens']} + about "
                                 f"{estimate(b)} prompt tokens > the {self.n_ctx}-token context")


# ---------------------------------------------------------------------- the checker itself
class CheckerTest(unittest.TestCase):
    def test_a_one_byte_change_anywhere_in_the_prefix_is_seen(self):
        prev = {"reasoning_effort": "low", "tools": [{"a": 1}], "tool_choice": "auto",
                "messages": [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]}
        nxt = json.loads(json.dumps(prev))
        nxt["messages"].append({"role": "assistant", "content": "ok"})
        self.assertIsNone(prefix_break(prev, nxt))
        self.assertEqual(prefix_break(prev, prev), "no new messages")
        for change in (lambda b: b.update(reasoning_effort="medium"), lambda b: b["tools"][0].update(a=2),
                       lambda b: b["messages"][1].update(content="hj"),
                       lambda b: b["messages"][1].update(content="hi", _x=1)):
            other = json.loads(json.dumps(nxt))
            change(other)
            self.assertIsNotNone(prefix_break(prev, other))
        self.assertIn("messages[1].content differs at byte 30", prefix_break(prev, dict(
            nxt, messages=[nxt["messages"][0], {"role": "user", "content": "hj"}, nxt["messages"][2]])))


# ---------------------------------------------------------------------- a long tool-using task
class ToolTaskTest(Base):
    APP = 'import os\n\nPORT = 8080\n\n\ndef main():\n    print(f"listening on {PORT}")\n'

    def test_a_ten_step_coding_task_keeps_one_growing_prefix(self):
        (self.ws / "app.py").write_text(self.APP)
        (self.ws / "config.ini").write_text("[server]\nport = 8080\nhost = 127.0.0.1\n")
        test_code = ('import os\nos.environ["PORT"] = "9999"\nimport app\nassert app.PORT == 9999, app.PORT\n'
                     'print("ok")\n')
        plan = [{"content": "Read app.py", "status": "in_progress"},
                {"content": "Let PORT come from the environment", "status": "pending"},
                {"content": "Test it", "status": "pending"}]
        agent = self.make([
            thinking(calls(("todo_write", {"todos": plan})), "Three steps: read, change, test."),
            calls(("list_dir", {})),
            calls(("read_file", {"path": "app.py"})),
            calls(("delegate_task", {"task": "Find every place in the work folder that sets the port; "
                                             "report file:line for each."})),
            calls(("search_text", {"pattern": "(?i)port"})),                      # the helper's step
            text("The port is set in app.py:3 (PORT = 8080) and config.ini:2."),  # the helper's report
            thinking(calls(("edit_file", {"path": "app.py", "old_text": "PORT = 8080",
                                          "new_text": 'PORT = int(os.environ.get("PORT", 8080))'})),
                     "Read it from the environment with 8080 as the default."),
            calls(("write_file", {"path": "test_app.py", "content": test_code}),
                  ("read_file", {"path": "config.ini"})),
            calls(("run_command", {"command": f'"{sys.executable}" test_app.py'})),
            calls(("todo_write", {"todos": [dict(t, status="done") for t in plan]})),
            thinking(text("PORT now comes from the PORT environment variable (default 8080); test_app.py "
                          "checks it and passes. config.ini still says 8080 - not used by app.py."),
                     "All done, the test printed ok."),
        ], thinking="medium")
        out = agent.run("Make the server port configurable with a PORT environment variable, and test it.")

        self.assertIn("PORT now comes from", out)
        self.assertIn('os.environ.get("PORT", 8080)', (self.ws / "app.py").read_text())
        tool_results = [m["content"] for m in agent.messages if m["role"] == "tool"]
        self.assertTrue(any(r.startswith("[exit code 0]\nok") for r in tool_results), tool_results)
        self.assertIn("Helper's report:", tool_results[3])
        self.assertEqual(self.kinds(), ["main"] * 4 + ["helper agent"] * 2 + ["main"] * 5 + ["memory check"])

        self.assertEqual(self.main_breaks(), [])
        self.assert_cache_friendly("medium")
        # the request after the helper's chat carries on from the one before it
        bodies = self.srv.requests
        self.assertIsNone(prefix_break(bodies[3], bodies[6]))


# ---------------------------------------------------------------------- memory check, then more chat
class MemoryCheckTest(Base):
    def test_the_memory_check_and_a_second_message_keep_the_prefix(self):
        (self.ws / "config.ini").write_text("[server]\nport = 8080\nhost = 127.0.0.1\n")
        (self.ws / "README.md").write_text("# Dev server\n\nStart it with `python app.py`.\n")
        note = json.dumps([{"title": "Dev server port", "replaces": None,
                            "note": "The dev server port is set in config.ini (9090 since the fix)"}])
        agent = self.make([
            calls(("search_text", {"pattern": "8080"})),
            thinking(calls(("read_file", {"path": "config.ini"})), "Only config.ini has it."),
            calls(("edit_file", {"path": "config.ini", "old_text": "port = 8080", "new_text": "port = 9090"})),
            calls(("run_command", {"command": "grep -n port config.ini"})),
            text("Changed the dev server port to 9090 in config.ini."),
            # the second message
            calls(("read_file", {"path": "README.md"})),
            calls(("edit_file", {"path": "README.md", "old_text": "Start it with `python app.py`.",
                                 "new_text": "Start it with `python app.py`; it listens on port 9090."})),
            calls(("read_file", {"path": "README.md"})),
            thinking(text("The README now says the dev server listens on port 9090."), "Done."),
        ], memories=[note, "[]"], thinking="low")

        self.assertIn("9090", agent.run("The dev server should listen on port 9090 instead of 8080. Fix it."))
        self.assertTrue(any(n.startswith("Remembered: Dev server port") for n in self.ui.notices), self.ui.notices)
        first_turn = len(self.srv.requests)
        self.assertIn("9090", agent.run("Also mention the new dev server port in the README."))
        self.assertIn("port 9090", (self.ws / "README.md").read_text())

        self.assertEqual(self.kinds(), ["main"] * 5 + ["memory check"] + ["main"] * 4 + ["memory check"])
        self.assertEqual(first_turn, 6)
        self.assertEqual(self.main_breaks(), [])
        self.assert_cache_friendly("low")

        bodies = self.srv.requests
        before, check, after = bodies[4], bodies[5], bodies[6]
        # the memory check is the main chat plus the answer plus the question...
        self.assertEqual(check["messages"][:-1], after["messages"][:len(check["messages"]) - 1])
        self.assertEqual(check["messages"][-2]["content"], "Changed the dev server port to 9090 in config.ini.")
        # ...and the second message's request is the same chat plus the answer plus the new message
        self.assertEqual(after["messages"][:len(before["messages"]) + 1], check["messages"][:-1])
        self.assertTrue(after["messages"][-1]["content"].startswith("Also mention the new dev server port"))
        self.assertNotIn("[Context for this chat", after["messages"][-1]["content"])
        # the reply of the memory check isn't part of the conversation
        self.assertNotIn(MEMORY_Q, json.dumps(agent.messages))

    # The memory check runs after the last step without a compaction check of its own, so its
    # max_tokens must leave room for its question too (it once asked for 1024 with ~850 left).
    def test_the_memory_check_fits_the_context_after_a_long_final_answer(self):
        # A log review that ends just under the compaction point (75% of a 16k context) with a long,
        # well-thought-out report: the reply fits the room the last step was given, so the chat is
        # fine, but the memory check right after it must also fit (Strata refuses prompt + max_tokens
        # over the context, see Agent._fit).
        for d in range(1, 4):
            (self.ws / f"day{d}.log").write_text("".join(
                f"2026-10-0{d} 12:{s // 60:02d}:{s % 60:02d} INFO GET /api/orders/{s} status=200 took={s}ms\n"
                for s in range(100)))
        report = "## Findings\n" + "".join(f"- day{d % 3 + 1}: request {d} took {d}ms, within limits.\n"
                                          for d in range(70))
        agent = self.make([calls(("read_file", {"path": f"day{d}.log"})) for d in range(1, 4)]
                          + [thinking(text(report), "Go through every request of every day. " * 380)],
                          n_ctx=16384, thinking="medium")
        self.assertEqual(agent.run("Go through day1.log to day3.log and report anything slow."), report)
        bodies = self.srv.requests
        self.assertEqual(self.kinds(), ["main"] * 4 + ["memory check"])
        last, check = bodies[3], bodies[4]
        self.assertLess(estimate(last), 16384 * 0.75)                      # no compaction was due
        self.assertLessEqual(len(report + "Go through every request of every day. " * 380) / CHARS_PER_TOKEN,
                             last["max_tokens"])                           # the reply fit its room
        self.assertEqual(self.main_breaks(), [])
        self.assert_same_prompt_settings("medium")                      # passes
        self.assertLessEqual(check["max_tokens"], 16384 - estimate(check),
                             f"memory check: max_tokens {check['max_tokens']} + about {estimate(check)} "
                             "prompt tokens > the 16384-token context")
        self.assert_max_tokens_fit()


# ---------------------------------------------------------------------- compaction
class CompactionTest(Base):
    LINE = "2026-10-0{d} 12:00:{s:02d} INFO GET /api/items/{s} status=200 took={s}ms worker=w{d}\n"

    def logs(self, days=8):
        for d in range(1, days + 1):
            lines = [self.LINE.format(d=d, s=s) for s in range(75)]
            if d in (2, 3):
                lines[40] = f"2026-10-0{d} 12:00:40 ERROR write failed: disk full\n"
            (self.ws / "logs").mkdir(exist_ok=True)
            (self.ws / "logs" / f"day{d}.log").write_text("".join(lines))

    def script(self):
        plan = [{"content": "Read the logs", "status": "in_progress"},
                {"content": "Write report.md", "status": "pending"}]
        return ([thinking(calls(("todo_write", {"todos": plan})), "One log at a time.")]
                + [calls(("read_file", {"path": f"logs/day{d}.log"})) for d in range(1, 7)]
                + [calls(("search_text", {"pattern": "ERROR", "path": "logs"})),
                   calls(("read_file", {"path": "logs/day7.log", "offset": 30, "limit": 20})),
                   calls(("write_file", {"path": "report.md", "content": "# Log check\n\nERROR: disk full on "
                                                                         "day2 and day3 at 12:00:40.\n"})),
                   thinking(text("Checked all logs: disk-full errors on day2 and day3. Details in report.md."),
                            "Report written."),
                   # the second message
                   calls(("read_file", {"path": "report.md"})),
                   text("Day 2 and day 3 had one error each (disk full).")])

    def run_compacting_chat(self, n_ctx):
        self.logs()
        agent = self.make(self.script(), n_ctx=n_ctx, thinking="low")
        out = agent.run("Check the server logs in logs/ for errors and write a short report.md.")
        self.assertIn("disk-full errors", out)
        self.assertEqual(agent.run("Which days had errors?"), "Day 2 and day 3 had one error each (disk full).")
        self.assertTrue((self.ws / "report.md").is_file())
        notices = [n for n in self.ui.notices if n.startswith("Summarized")]
        self.assertEqual(len(notices), 1, self.ui.notices)          # once, and then it kept going

        bodies, kinds = self.srv.requests, self.kinds()
        side = [i for i, k in enumerate(kinds) if k in ("summary", "transcript summary")]
        self.assertEqual(len(side), 1, kinds)
        mains = self.mains()
        after = [i for i in mains if i > side[0]]
        self.assertGreaterEqual(len(after), 5, kinds)               # it kept going after compacting

        # the one break: the request right after the compaction, nowhere else
        breaks = self.main_breaks()
        self.assertEqual([(b["after"], b["at"]) for b in breaks], [(side[0] - 1, side[0] + 1)], breaks)
        self.assertEqual(breaks[0]["message"], 1)                   # system prompt still reused

        # the [Context] note starts the first user message, the same bytes before and after
        context = next(m["_context"] for m in agent.messages if m.get("_context"))
        self.assertTrue(context.startswith("[Context for this chat"))
        for i in mains:
            first_user = bodies[i]["messages"][1]
            self.assertEqual(first_user["role"], "user")
            self.assertTrue(first_user["content"].startswith(context), f"request {i}")
        self.assertIn("[Summary of the earlier part of this conversation]",
                      bodies[side[0] + 1]["messages"][1]["content"][len(context):len(context) + 60])
        self.assertEqual(json.dumps(bodies[-1]["messages"]).count("[Context for this chat"), 1)   # never twice
        self.assertEqual(kinds[-1], "main")
        return kinds[side[0]]

    def test_compacting_with_a_summary_inside_the_conversation(self):
        self.assertEqual(self.run_compacting_chat(20000), "summary")
        self.assert_cache_friendly("low")

    def test_compacting_with_a_transcript_summary(self):
        self.assertEqual(self.run_compacting_chat(16384), "transcript summary")
        self.assert_cache_friendly("low")


# ---------------------------------------------------------------------- screen control and screenshots
class FakeGui:
    """Stands in for pyautogui: records what would have been done."""
    KEYBOARD_KEYS = ["ctrl", "alt", "shift", "tab", "enter", "esc", "s", "a", "l", "win", "f5"]

    def __init__(self):
        self.done = []

    def __getattr__(self, name):
        return lambda *a, **kw: self.done.append(name)


class ScreenTest(Base):
    def setUp(self):
        super().setUp()
        self.shots, self.gui = 0, FakeGui()

        def shot(ctx, monitor=None):
            self.shots += 1
            ctx.screen.update(left=0, top=0, width=1920, height=1080, img_w=1280, img_h=720, monitor=monitor or 1)
            return PNG + f"screen {self.shots}".encode()
        for p in (mock.patch.object(screen, "_shot", shot), mock.patch.object(screen, "_gui", lambda: self.gui),
                  mock.patch.object(screen, "time", types.SimpleNamespace(sleep=lambda s: None, time=time.time))):
            p.start()
            self.addCleanup(p.stop)

    STEPS = [
        [("screenshot", {})],
        [("click", {"x": 24, "y": 700})],
        [("type_text", {"text": "notepad", "enter": True})],
        [("click", {"x": 640, "y": 360})],
        [("type_text", {"text": "milk\neggs\nbread"})],
        [("press_keys", {"keys": "ctrl+s"}), ("type_text", {"text": "shopping.txt"})],   # one screenshot
        [("press_keys", {"keys": "enter"})],
        [("todo_write", {"todos": [{"content": "Save the list", "status": "done"},
                                   {"content": "Check it in the folder", "status": "in_progress"}]})],
        [("screenshot", {})],
        [("press_keys", {"keys": "win"})],
        [("type_text", {"text": "documents", "enter": True})],
        [("scroll", {"amount": 3, "x": 640, "y": 400})],
        [("move_mouse", {"x": 300, "y": 220})],
        [("drag", {"from_x": 300, "from_y": 220, "to_x": 600, "to_y": 220})],
        [("click", {"x": 300, "y": 220, "clicks": 2})],
        [("screenshot", {})],
        [("press_keys", {"keys": "ctrl+a"})],
        [("press_keys", {"keys": "alt+tab"})],
        [("screenshot", {})],
        [("click", {"x": 1200, "y": 10})],
    ]

    def test_screenshots_are_cleared_once_per_batch_not_every_step(self):
        replies = [calls(*step) for step in self.STEPS]
        replies[0] = thinking(replies[0], "First look at the screen.")
        replies.append(thinking(text("Saved shopping.txt in Documents with milk, eggs and bread."), "Done."))
        agent = self.make(replies, enable_control=True, computer="desktop", keep_images=2, max_images=8,
                          thinking="high")
        out = agent.run("Write my shopping list in Notepad and save it as shopping.txt. It should look like this:",
                        images=[PNG + b"the user's mockup"])
        self.assertIn("shopping.txt", out)
        self.assertIn("click", self.gui.done)
        self.assertIn("screenshot", json.dumps(agent._tool_schemas()))

        bodies, mains = self.srv.requests, self.mains()
        self.assertEqual(len(mains), len(self.STEPS) + 1)
        self.assertEqual(self.kinds(), ["main"] * len(mains) + ["memory check"])

        # where clearing is due: count the screenshots step by step (several screen actions in one
        # step give one; todo_write none)
        per_step = [int(any(n in ("screenshot", "click", "type_text", "press_keys", "scroll", "move_mouse",
                                  "drag") for n, _ in step)) for step in self.STEPS]
        expect, held = [], 0
        for step, n in enumerate(per_step):
            held += n
            if held > 8:
                expect.append(mains[step + 1])       # the request after that step
                held = 2
        self.assertEqual(len(expect), 2)

        # the attached image stays; tool screenshots: never more than 8, back to 2 after clearing
        tool_images = [images_in(bodies[i]["messages"]) - 1 for i in mains]
        self.assertEqual(images_in(bodies[mains[0]]["messages"]), 1)
        self.assertLessEqual(max(tool_images), 8)
        for i in expect:
            self.assertEqual(images_in(bodies[i]["messages"]) - 1, 2)

        breaks = self.main_breaks()
        self.assertEqual([b["at"] for b in breaks], expect, breaks)
        self.assertGreaterEqual(expect[1] - expect[0], 8 - 2)        # a batch, not every step
        for b in breaks:
            self.assertEqual(b["between"], [])
            self.assertIn("[older image removed to save space]", b["why"])
        # the second clearing starts later in the chat than the first: more of it stays cached
        self.assertGreater(breaks[1]["message"], breaks[0]["message"])
        self.assert_cache_friendly("xhigh")


if __name__ == "__main__":
    unittest.main()
