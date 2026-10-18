"""The agent loop against a fake model server that each test scripts: Stop, server trouble,
history that must stay valid, compaction, and what keeps the server's prompt cache warm."""

import builtins
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

os.environ.setdefault("LOCALAGENT_DATA", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from localagent import cli, prompts, tools as tools_mod  # noqa: E402
from localagent.agent import NO_VISION, Agent, AgentUI  # noqa: E402
from localagent.config import Settings  # noqa: E402
from localagent.llm import (LOADING, LLMClient, ServerError, ThinkSplitter, ToolsUnsupported,  # noqa: E402
                            extract_text_tool_calls, strip_thinking)
from localagent.permissions import ApprovalRequest, Decision  # noqa: E402
from localagent.tools import REGISTRY, Tool, ToolResult, enabled_tools  # noqa: E402

QWEN38 = "{%- if reasoning_effort == 'xhigh' %}..{%- elif reasoning_effort == 'medium' %}..{%- elif reasoning_effort == 'low' %}"


# ---------------------------------------------------------------------- the fake server
def start_sse(h, chunked=False):
    h.send_response(200)
    h.send_header("Content-Type", "text/event-stream")
    if chunked:
        h.send_header("Transfer-Encoding", "chunked")
    h.end_headers()


def send(h, obj, chunked=False):
    data = b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n"
    if chunked:
        data = f"{len(data):x}\r\n".encode() + data + b"\r\n"
    h.wfile.write(data)
    h.wfile.flush()


def reply(h, text=None, calls=None, reasoning=None, finish=None, timings=None):
    """A whole streamed answer, the way llama-server sends it."""
    start_sse(h)
    if reasoning:
        send(h, {"choices": [{"delta": {"reasoning_content": reasoning}}]})
    if text:
        send(h, {"choices": [{"delta": {"content": text}}]})
    for i, (name, args) in enumerate(calls or []):
        send(h, {"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"c{time.time_ns()}{i}", "type": "function",
              "function": {"name": name, "arguments": args if isinstance(args, str) else json.dumps(args)}}]}}]})
    send(h, {"choices": [{"delta": {}, "finish_reason": finish or ("tool_calls" if calls else "stop")}]})
    final = {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 20}}
    if timings:
        final["timings"] = timings
    send(h, final)
    send(h, b"[DONE]")


def json_reply(h, code, obj):
    data = json.dumps(obj).encode()
    h.send_response(code)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(data)))
    h.end_headers()
    h.wfile.write(data)


def images_in(messages) -> int:
    return sum(1 for m in messages if isinstance(m.get("content"), list)
               for p in m["content"] if p.get("type") == "image_url")


def last_text(body) -> str:
    c = body["messages"][-1].get("content")
    return c if isinstance(c, str) else json.dumps(c)


class FakeServer:
    """chat(body, h) writes each chat answer; gets maps GET paths to callables giving (status, json)."""

    def __init__(self, chat=None, props=None, http11=False, model="test-model"):
        self.chat = chat or (lambda body, h: reply(h, "ok"))
        self.requests: list[dict] = []
        self.gets = {"/v1/models": lambda: (200, {"data": [{"id": model}]}),
                     "/props": props or (lambda: (200, {"default_generation_settings": {"n_ctx": 32768}}))}
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1" if http11 else "HTTP/1.0"

            def log_message(self, *a):
                pass

            def do_GET(self):
                fn = outer.gets.get(self.path)
                json_reply(self, *(fn() if fn else (404, {"error": "Unexpected endpoint"})))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                try:
                    outer.chat(body, self)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def chats(self) -> list[dict]:
        """The real chat requests (not the compaction or memory side questions)."""
        return [b for b in self.requests if b.get("stream") and "Context check" not in last_text(b)
                and "Memory check" not in last_text(b)]

    def close(self):
        self.httpd.shutdown()


def script(*answers, summary="SUMMARY: the user wants files written; f0 and f1 are done, the rest is left."):
    """chat() that gives the answers in turn, and answers the side questions (compaction, memory)."""
    queue = list(answers)

    def chat(body, h):
        if "Context check" in last_text(body):
            return reply(h, summary) if body.get("stream") else json_reply(
                h, 200, {"choices": [{"message": {"content": summary}}]})
        if "Memory check" in last_text(body):
            return reply(h, "[]")
        a = queue.pop(0) if queue else {"text": "done"}
        return a(body, h) if callable(a) else reply(h, **a)
    return chat


class RecUI(AgentUI):
    def __init__(self):
        self.notices, self.statuses, self.tools, self.turns = [], [], [], []

    def notice(self, s):
        self.notices.append(s)

    def status(self, s):
        self.statuses.append(s)

    def tool_end(self, call_id, name, result):
        self.tools.append((name, result))

    def assistant_end(self, turn):
        self.turns.append(turn)

    def approve(self, req):
        return Decision(True)


class Base(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp())

    def make(self, chat=None, props=None, http11=False, model="test-model", **kw):
        self.srv = FakeServer(chat, props, http11, model)
        self.addCleanup(self.srv.close)
        kw = {"auto_memory": False, "auto_recall": False, "approval_mode": "auto", **kw}
        self.ui = RecUI()
        self.agent = Agent(Settings(base_url=self.srv.url, workspace=str(self.ws), **kw), self.ui)
        return self.agent

    def add_tool(self, name, func, group="core", **kw):
        tool = Tool(name=name, description=name, params={"text": {"type": "string"}}, func=func, group=group, **kw)
        self.agent.tools[name] = tool
        return tool

    def stop_after(self, seconds):
        t = threading.Timer(seconds, self.agent.stop.set)
        t.start()
        self.addCleanup(t.cancel)

    def assert_history_valid(self, wire):
        """Every tool call in the history has its result, and its arguments are JSON."""
        ids = [c["id"] for m in wire if m.get("tool_calls") for c in m["tool_calls"]]
        answered = [m["tool_call_id"] for m in wire if m["role"] == "tool"]
        self.assertEqual(sorted(ids), sorted(answered))
        for m in wire:
            for c in m.get("tool_calls") or []:
                json.loads(c["function"]["arguments"])


# ---------------------------------------------------------------------- Stop
class StopTest(Base):
    def test_stop_while_the_server_reads_the_prompt(self):
        def slow(body, h):          # no answer at all yet, not even headers (prompt processing)
            time.sleep(5)
            reply(h, "late")
        agent = self.make(slow)
        self.stop_after(0.3)
        t0 = time.time()
        self.assertEqual(agent.run("hi"), "")
        self.assertLess(time.time() - t0, 1.5)
        self.assertIn("Stopped.", self.ui.notices)
        self.assertEqual([m["role"] for m in agent.messages], ["user"])

    def test_stop_after_the_headers_and_mid_reply(self):
        def headers_then_wait(body, h):
            start_sse(h, chunked=True)
            send(h, {"choices": [{"delta": {"content": "Let me "}}]}, chunked=True)
            time.sleep(5)
        for http11 in (False, True):
            self.make(headers_then_wait, http11=http11)
            client = LLMClient(self.srv.url)
            stop = threading.Event()
            threading.Timer(0.3, stop.set).start()
            t0 = time.time()
            turn = client.stream([{"role": "user", "content": "x"}], stop=stop)
            self.assertLess(time.time() - t0, 1.5)
            self.assertTrue(turn.stopped)
            # the client still works afterwards (the cut connection isn't reused)
            self.srv.chat = lambda body, h: reply(h, "again") if not http11 else self._chunked_reply(h, "again")
            self.assertEqual(client.stream([{"role": "user", "content": "x"}], stop=threading.Event()).content, "again")

    @staticmethod
    def _chunked_reply(h, text):
        start_sse(h, chunked=True)
        send(h, {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}, chunked=True)
        h.wfile.write(b"0\r\n\r\n")

    def test_stop_mid_tool_call_keeps_the_history_valid(self):
        def slow_call(body, h):
            start_sse(h)
            send(h, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                  "function": {"name": "write_file", "arguments": ""}}]}}]})
            args = json.dumps({"path": "a.py", "content": "x = 1\n" * 200})
            for i in range(0, len(args), 20):
                send(h, {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": args[i:i + 20]}}]}}]})
                time.sleep(0.02)
        agent = self.make(script(slow_call, {"text": "hi"}))
        self.stop_after(0.3)
        agent.run("write a.py")
        self.assertFalse((self.ws / "a.py").exists())
        self.assertFalse(any(m.get("tool_calls") for m in agent.messages))
        agent.run("never mind, just say hi")
        self.assert_history_valid(self.srv.requests[-1]["messages"])

    def test_stop_while_summarizing(self):
        def slow(body, h):
            time.sleep(5)
            reply(h, "summary")
        for limit in (32768, 9000):     # a summary inside the conversation, and from a transcript
            agent = self.make(slow, context_limit=limit)
            agent.messages = [{"role": "user", "content": "do it"}] + [
                m for i in range(8) for m in ({"role": "assistant", "content": f"step {i}"},
                                              {"role": "user", "content": "go on " + "z" * 3000})]
            before = list(agent.messages)
            self.stop_after(0.3)
            t0 = time.time()
            self.assertFalse(agent.compact())
            self.assertLess(time.time() - t0, 1.5)
            self.assertEqual(len(self.srv.requests), 1)
            self.assertEqual(self.srv.requests[0]["stream"], limit > 9000)
            self.assertEqual(agent.messages, before)


# ---------------------------------------------------------------------- tool calls and their results
class ResultsTest(Base):
    def test_odd_argument_types_are_an_error_not_a_crash(self):
        agent = self.make(script({"calls": [("shout", '{"text": 3}')]}, {"text": "ok"}))
        self.add_tool("shout", lambda ctx, a: "x", describe=lambda a: "Shout " + a["text"])   # raises on 3
        self.assertEqual(agent.run("go"), "ok")
        tool_msg = [m for m in agent.messages if m["role"] == "tool"][0]
        self.assertIn("bad arguments for shout", tool_msg["content"])

    def test_every_call_gets_a_result_even_when_a_step_breaks(self):
        agent = self.make(script({"calls": [("list_dir", {}), ("list_dir", {"path": "."})]}))
        with mock.patch.object(agent, "execute", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                agent.run("go")
        self.assert_history_valid(agent.wire_messages())
        self.assertIn("interrupted", agent.messages[-1]["content"])

    def test_a_call_cut_off_by_the_length_limit_is_not_run(self):
        agent = self.make(script({"calls": [("write_file", '{"path": "a.txt", "content": "abc')], "finish": "length"},
                                 {"text": "ok"}))
        agent.run("go")
        self.assertFalse((self.ws / "a.txt").exists())
        self.assertIn("cut off", [m for m in agent.messages if m["role"] == "tool"][0]["content"])
        self.assert_history_valid(self.srv.requests[-1]["messages"])
        self.assertTrue(any("length limit" in n for n in self.ui.notices))

    def test_tool_output_is_scaled_to_the_context(self):
        agent = self.make(context_limit=8192)
        self.assertEqual(agent._output_limit(), int(8192 * 0.1 * 3.5))
        agent.settings.context_limit = 1_000_000
        self.assertEqual(agent._output_limit(), agent.settings.max_tool_output)
        agent = self.make(props=lambda: (404, {}))      # the server doesn't say: the setting as is
        self.assertEqual(agent._output_limit(), agent.settings.max_tool_output)

    def test_add_on_tools_are_checked_for_dangerous_commands(self):
        args = {"script": "Remove-Item -Recurse -Force C:\\Users\\me\\Documents"}
        asked = []
        agent = self.make(script({"calls": [("pwsh__run", args)]}, {"text": "ok"}), always_ask_dangerous=True)
        self.ui.approve = lambda req: asked.append(req.dangerous) or Decision(False, note="no")
        ran = []
        self.add_tool("pwsh__run", lambda ctx, a: ran.append(a) or "ran", group="mcp", risk="run")
        agent.run("clean up my documents")
        self.assertEqual((asked, ran), ([True], []))      # Full auto still asks for this one

# ---------------------------------------------------------------------- images
class ImagesTest(Base):
    def test_a_server_without_vision_doesnt_break_the_chat(self):
        def chat(body, h):
            if any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
                   for m in body["messages"]):
                return json_reply(h, 500, {"error": {"code": 500, "message": "image input is not supported - hint: "
                                                     "if this is unexpected, you may need to provide the mmproj"}})
            done = any(m["role"] == "tool" for m in body["messages"])
            reply(h, "I can't see it." if done else None, calls=None if done else [("shot", {})])
        agent = self.make(chat, props=lambda: (404, {}))      # can't tell up front
        self.add_tool("shot", lambda ctx, a: ToolResult("Screenshot 1280x720", images=[b"png"]))
        self.assertEqual(agent.run("look at my screen"), "I can't see it.")
        self.assertTrue(any("can't see images" in n for n in self.ui.notices))
        self.assertEqual(agent.run("ok, just say hi"), "I can't see it.")
        self.assertIn(NO_VISION, json.dumps(self.srv.requests[-1]["messages"]))

    def test_images_are_left_out_when_the_server_says_it_has_no_vision(self):
        agent = self.make(props=lambda: (200, {"default_generation_settings": {"n_ctx": 32768},
                                               "modalities": {"vision": False}}))
        agent.run("what is this?", images=[b"png"])
        sent = json.dumps(self.srv.requests[-1]["messages"])
        self.assertNotIn("image_url", sent)
        self.assertIn(NO_VISION, sent)
        self.assertTrue(any("can't see images" in n for n in self.ui.notices))

    def test_attached_images_stay_for_the_whole_request(self):
        agent = self.make(script({"calls": [("list_dir", {})]}, {"text": "compared"}))
        agent.run("compare these mockups", images=[b"png"] * 6)
        counts = [images_in(b["messages"]) for b in self.srv.requests]
        self.assertEqual(counts, [6, 6])

    def test_screenshots_are_cleared_rarely_and_in_big_batches(self):
        agent = self.make()
        C = type("C", (), {})
        for i in range(9):
            c = C()
            c.id, c.name = f"i{i}", "screenshot"
            agent._record_results([(c, ToolResult("shot", images=[b"png"]))])
            if i == 7:     # 8 = max_images: nothing rewritten yet
                self.assertEqual(images_in(agent.messages), 8)
        self.assertEqual(images_in(agent.messages), agent.settings.keep_images)
        self.assertEqual((Settings().keep_images, Settings().max_images), (2, 8))


# ---------------------------------------------------------------------- the server loading, restarting, failing
class ServerTest(Base):
    def test_waits_for_the_model_to_load(self):
        loading = {"on": True}

        def props():
            return (503, {"error": {"message": "Loading model"}}) if loading["on"] else (
                200, {"default_generation_settings": {"n_ctx": 16384}, "chat_template": QWEN38})

        def chat(body, h):
            if loading["on"]:
                return json_reply(h, 503, {"error": {"message": "Loading model"}})
            reply(h, "hi")
        agent = self.make(chat, props, thinking="low")
        models = self.srv.gets["/v1/models"]
        self.srv.gets["/v1/models"] = lambda: (503, {"error": {"message": "Loading model"}}) if loading["on"] else models()
        threading.Timer(1.2, lambda: loading.update(on=False)).start()
        self.assertEqual(agent.run("hello"), "hi")
        self.assertIn(LOADING, self.ui.statuses)
        self.assertEqual(self.srv.requests[0]["reasoning_effort"], "low")
        self.assertEqual(agent.context_limit(), 16384)
        self.assertEqual(agent.client.model, "test-model")

    def test_a_503_mid_chat_is_waited_out(self):
        calls = {"n": 0}

        def chat(body, h):
            calls["n"] += 1
            if calls["n"] == 1:
                return json_reply(h, 503, {"error": {"message": "Loading model"}})
            reply(h, "back")
        agent = self.make(chat)
        self.assertEqual(agent.run("hi"), "back")
        self.assertIn(LOADING, self.ui.statuses)

    def test_new_chat_asks_the_server_again(self):
        info = {"n_ctx": 16384, "template": ""}
        agent = self.make(props=lambda: (200, {"default_generation_settings": {"n_ctx": info["n_ctx"]},
                                               "chat_template": info["template"]}), thinking="low")
        agent.run("hi")
        self.assertNotIn("reasoning_effort", self.srv.requests[-1])
        info.update(n_ctx=65536, template=QWEN38)        # the server restarted with another model
        agent.client.model = "other"
        agent.reset()
        agent.run("hi")
        self.assertEqual(agent.context_limit(), 65536)
        self.assertEqual(self.srv.requests[-1]["reasoning_effort"], "low")
        self.assertEqual(agent.client.model, "test-model")

    def test_lm_studio_and_ollama_context_sizes(self):
        agent = self.make(props=lambda: (404, {}), model="qwen3-8b")
        self.srv.gets["/api/v0/models/qwen3-8b"] = lambda: (200, {"id": "qwen3-8b", "loaded_context_length": 8192})
        agent.run("hi")
        self.assertEqual(agent.context_limit(), 8192)
        agent = self.make(props=lambda: (404, {}), model="qwen3:8b")
        self.srv.gets["/api/ps"] = lambda: (200, {"models": [{"name": "qwen3:8b", "model": "qwen3:8b",
                                                              "context_length": 4096}]})
        agent.run("hi")
        self.assertEqual(agent.context_limit(), 4096)

    def test_a_dropped_reply_is_an_error_not_an_answer(self):
        def close_mid_reply(body, h):        # the server dies: the plain connection just ends
            start_sse(h)
            send(h, {"choices": [{"delta": {"content": "The fix is to chan"}}]})
        agent = self.make(close_mid_reply)
        with self.assertRaises(ServerError) as e:
            agent.run("fix it")
        self.assertIn("middle of the reply", str(e.exception))

        def broken_chunks(body, h):
            start_sse(h, chunked=True)
            send(h, {"choices": [{"delta": {"content": "partial"}}]}, chunked=True)
            h.wfile.write(b"5\r\nab")
            h.wfile.flush()
            h.connection.shutdown(socket.SHUT_RDWR)
        self.make(broken_chunks, http11=True)
        with self.assertRaises(ServerError):
            LLMClient(self.srv.url).stream([{"role": "user", "content": "x"}])

    def test_a_reply_dropped_before_it_started_is_asked_again(self):
        calls = {"n": 0}

        def chat(body, h):
            calls["n"] += 1
            if calls["n"] == 1:
                start_sse(h)           # headers, then the connection closes
                return
            reply(h, "ok")
        agent = self.make(chat)
        self.assertEqual(agent.run("hi"), "ok")

    def test_an_unrelated_tool_error_keeps_native_tools(self):
        def chat(body, h):
            json_reply(h, 500, {"error": {"message": "Failed to parse tool call arguments as JSON"}})
        agent = self.make(chat)
        with self.assertRaises(ServerError) as e:
            agent.run("hi")
        self.assertNotIsInstance(e.exception, ToolsUnsupported)
        self.assertFalse(agent.prompt_mode)

    def test_native_tool_mode_never_switches_to_prompt_mode(self):
        def chat(body, h):
            if body.get("tools"):
                return json_reply(h, 500, {"error": {"message": "tools param requires --jinja flag"}})
            reply(h, "ok")
        agent = self.make(chat, tool_mode="native")
        with self.assertRaises(ToolsUnsupported):
            agent.run("hi")
        self.assertFalse(agent.prompt_mode)
        agent.settings.tool_mode = "auto"
        self.assertEqual(agent.run("hi"), "ok")
        self.assertTrue(agent.prompt_mode)
        agent.reset()
        self.assertFalse(agent.prompt_mode)

    def test_timings_reach_the_turn(self):
        timings = {"cache_n": 900, "prompt_n": 100, "prompt_ms": 250.5, "predicted_per_second": 42.0,
                   "draft_n": 30, "draft_n_accepted": 21}
        self.make(lambda body, h: reply(h, "ok", timings=timings)).run("hi")
        usage = self.ui.turns[-1].usage
        self.assertEqual((usage["prompt_tokens"], usage["cache_n"], usage["prompt_n"], usage["prompt_ms"],
                          usage["draft_n"], usage["draft_accepted"]), (1000, 900, 100, 250.5, 30, 21))
        self.assertEqual(self.ui.turns[-1].tokens_per_sec, 42.0)


# ---------------------------------------------------------------------- reading what the model wrote
class ParsingTest(unittest.TestCase):
    SCHEMAS = {"write_file": {"path": {"type": "string"}, "content": {"type": "string"}},
               "read_file": {"path": {"type": "string"}, "limit": {"type": "integer"}}}

    def call(self, text):
        clean, calls = extract_text_tool_calls(text, self.SCHEMAS)
        return clean, [(c.name, json.loads(c.arguments)) for c in calls]

    def test_xml_values_stay_text_where_the_tool_wants_text(self):
        x = ('<tool_call>\n<function=write_file>\n<parameter=path>\npackage.json\n</parameter>\n<parameter=content>\n'
             '{"name": "app", "version": "1.0.0"}\n</parameter>\n</function>\n</tool_call>')
        self.assertEqual(self.call(x)[1], [("write_file", {"path": "package.json",
                                                           "content": '{"name": "app", "version": "1.0.0"}'})])
        x = '<tool_call><function=write_file><parameter=path>VERSION</parameter><parameter=content>3</parameter></function></tool_call>'
        self.assertEqual(self.call(x)[1][0][1]["content"], "3")
        x = '<tool_call><function=read_file><parameter=path>a.py</parameter><parameter=limit>5</parameter></function></tool_call>'
        self.assertEqual(self.call(x)[1][0][1], {"path": "a.py", "limit": 5})

    def test_xml_values_keep_their_whitespace(self):
        x = ('<tool_call>\n<function=write_file>\n<parameter=path>\nmain.py\n</parameter>\n<parameter=content>\n'
             '    indented()\nprint(1)\n\n</parameter>\n</function>\n</tool_call>')
        self.assertEqual(self.call(x)[1][0][1]["content"], "    indented()\nprint(1)\n")

    def test_bare_function_calls_and_made_up_tool_responses(self):
        clean, calls = self.call("Reading it.\n<function=read_file>\n<parameter=path>\na.py\n</parameter>\n</function>")
        self.assertEqual((clean, calls), ("Reading it.", [("read_file", {"path": "a.py"})]))
        fake = ('<tool_call>\n{"name": "list_dir", "arguments": {}}\n</tool_call>\n<tool_response>\nempty\n'
                '</tool_response>\n<tool_call>\n{"name": "write_file", "arguments": {"path": "x", "content": "y"}}\n</tool_call>')
        self.assertEqual([n for n, _ in self.call(fake)[1]], ["list_dir"])

    def test_a_lone_closing_think_tag(self):
        sp = ThinkSplitter()
        text = thinking = ""
        for chunk in ["Let me think", " about it</th", "ink>The answer"]:
            for kind, s in sp.feed(chunk):
                if kind == "retract":     # what came as the answer was thinking
                    thinking, text = thinking + text, ""
                elif kind == "text":
                    text += s
                else:
                    thinking += s
        self.assertEqual((text, thinking), ("The answer", "Let me think about it"))
        self.assertEqual(strip_thinking("<think>\nOkay, the user wants a summary. First I"), "")
        self.assertEqual(strip_thinking("planning...</think>\nThe summary"), "The summary")

    def test_a_lone_closing_think_tag_in_a_stream(self):
        srv = FakeServer(lambda body, h: reply(h, "Plan: check the file.</think>It's fine."))
        self.addCleanup(srv.close)
        turn = LLMClient(srv.url).stream([{"role": "user", "content": "x"}])
        self.assertEqual((turn.content, turn.reasoning), ("It's fine.", "Plan: check the file."))


    def test_calls_shown_as_examples_are_not_calls(self):
        shown = ("Qwen writes a call like this:\n\n```\n<function=write_file>\n<parameter=path>\nhello.txt\n"
                 "</parameter>\n</function>\n```\n\nllama.cpp turns that into tool_calls.")
        self.assertEqual(self.call(shown), (shown, []))
        call = "<function=read_file>\n<parameter=path>\na.py\n</parameter>\n</function>"
        # with real tool calling, only a call that ends the reply is one
        clean, calls = extract_text_tool_calls("Like this: " + call + " and then it runs.", self.SCHEMAS, at_end=True)
        self.assertEqual(calls, [])
        clean, calls = extract_text_tool_calls("Reading it.\n" + call, self.SCHEMAS, at_end=True)
        self.assertEqual((clean, [c.name for c in calls]), ("Reading it.", ["read_file"]))
        # a real call after a code block still counts, and the code block stays in the text
        clean, calls = self.call("```py\nx = 1\n```\n<tool_call>\n" + call + "\n</tool_call>")
        self.assertEqual((clean, [n for n, _ in calls]), ("```py\nx = 1\n```", ["read_file"]))

    def test_a_closing_think_tag_is_text_when_the_thinking_came_separately(self):
        answer = "The fix: split the reply on `</think>` and keep only what comes after it."

        def chat(body, h):
            start_sse(h)
            send(h, {"choices": [{"delta": {"reasoning_content": "The user asks how to drop the thinking."}}]})
            for i in range(0, len(answer), 9):
                send(h, {"choices": [{"delta": {"content": answer[i:i + 9]}}]})
            send(h, {"choices": [{"delta": {}, "finish_reason": "stop"}]})
            send(h, b"[DONE]")
        srv = FakeServer(chat)
        self.addCleanup(srv.close)
        turn = LLMClient(srv.url).stream([{"role": "user", "content": "x"}])
        self.assertEqual((turn.content, turn.reasoning), (answer, "The user asks how to drop the thinking."))

# ---------------------------------------------------------------------- compaction
class CompactionTest(Base):
    def fill(self, agent, steps, request="Find every file that mentions TOKEN-XYZZY", size=3000):
        if request:
            agent.messages.append(agent._user_message(request, None))
        for i in range(steps):
            n = len(agent.messages)
            agent.messages += [{"role": "assistant", "content": "", "tool_calls": [{"id": f"c{n}", "type": "function",
                                "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{n}.txt"})}}]},
                               {"role": "tool", "tool_call_id": f"c{n}", "name": "read_file", "content": "z" * size}]

    def test_the_summary_is_one_more_turn_of_the_conversation(self):
        agent = self.make(script(summary="<think>what matters</think>SUMMARY: read f1 to f8; TOKEN-XYZZY is in f3."),
                          context_limit=32768, thinking="low")
        agent.client._template = QWEN38
        self.fill(agent, 8)
        wire = agent.wire_messages()
        normal = agent._params()
        self.assertTrue(agent.compact())
        req = self.srv.requests[-1]
        self.assertEqual(req["messages"][:-1], wire)             # the server has all of it cached already
        self.assertIn("Context check", req["messages"][-1]["content"])
        self.assertEqual(req["tools"], agent._tool_schemas())
        self.assertEqual((req["reasoning_effort"], req["chat_template_kwargs"]),
                         (normal["reasoning_effort"], normal["chat_template_kwargs"]))
        self.assertEqual((req["max_tokens"], req["reasoning_budget_tokens"]), (3072, 512))
        self.assertNotIn("strata_checkpoint", req)
        first = agent.messages[0]
        self.assertEqual(first["_kind"], "summary")
        self.assertIn("SUMMARY: read f1 to f8", first["content"])
        self.assertNotIn("what matters", first["content"])
        self.assertTrue(first["content"].startswith("[Context for this chat"))
        self.assertEqual(first["_request"], "Find every file that mentions TOKEN-XYZZY")
        self.assert_history_valid(agent.wire_messages())
        self.assertLess(agent.estimate_total(), agent.context_limit() * 0.5)

    def test_strata_gets_no_checkpoint_for_side_questions(self):
        agent = self.make(script(), context_limit=32768)
        self.srv.gets["/health"] = lambda: (200, {"status": "ok", "service": "strata"})
        self.fill(agent, 8)
        agent.compact()
        self.assertIs(self.srv.requests[-1]["strata_checkpoint"], False)

    def test_a_transcript_when_there_is_no_room(self):
        agent = self.make(script(), context_limit=16000)
        self.fill(agent, 14, size=2500)
        self.assertGreater(agent.estimate_total() + 3072, 16000)
        self.assertTrue(agent.compact())
        req = self.srv.requests[-1]
        self.assertFalse(req["stream"])
        self.assertEqual(req["reasoning_effort"], "low")
        self.assertIn("TOKEN-XYZZY", req["messages"][1]["content"])

    def test_an_empty_summary_keeps_the_messages_word_for_word(self):
        def chat(body, h):
            json_reply(h, 200, {"choices": [{"finish_reason": "length", "message": {
                "content": "<think>\nOkay, the user wants a summary. First I", "reasoning_content": "hmm" * 50}}]})
        agent = self.make(chat, context_limit=16000)
        self.fill(agent, 14, size=2500)
        agent.compact()
        text = agent.messages[0]["content"]
        self.assertNotIn("<think>", text)
        self.assertIn("word for word", text)
        self.assertIn("TOOL RESULT (read_file)", text)

    def test_the_request_survives_repeated_compactions(self):
        agent = self.make(script(summary="SUMMARY: read several files, nothing found yet, keep reading."),
                          context_limit=32768)
        self.fill(agent, 8)
        agent.compact()
        for _ in range(3):
            self.fill(agent, 8, request=None)
            self.assertTrue(agent.compact())
            self.assertEqual(agent.messages[0]["_request"], "Find every file that mentions TOKEN-XYZZY")
            self.assertIn("TOKEN-XYZZY", agent.messages[0]["content"])
        self.assertEqual(agent.wire_messages()[1]["content"].count("[Context for this chat"), 1)

    def test_the_memory_check_still_runs_after_compaction(self):
        steps = [{"calls": [("read_file", {"path": "big.txt"})]}] * 6
        agent = self.make(script(*steps, {"text": "All done."}), context_limit=16000, auto_memory=True)
        (self.ws / "big.txt").write_text("line of text\n" * 1500)
        self.assertEqual(agent.run("read big.txt a few times"), "All done.")
        self.assertTrue(any("Summarized" in n for n in self.ui.notices))
        self.assertIn("Memory check", last_text(self.srv.requests[-1]))


    def test_a_request_that_is_only_a_picture(self):
        (self.ws / "AGENT.md").write_text("House rules: use tabs.")
        agent = self.make(script(), context_limit=32768)
        agent.messages.append(agent._user_message("", [b"png"]))
        self.fill(agent, 8, request=None)
        self.assertTrue(agent.compact())
        first = agent.messages[0]
        self.assertEqual(first["content"].count("[Context for this chat"), 1)
        self.assertEqual(first["content"].count("House rules"), 1)
        self.assertEqual(first["_request"], "(an image, no text)")

# ---------------------------------------------------------------------- the server's prompt cache
class PromptCacheTest(Base):
    def test_the_system_prompt_is_the_same_in_every_chat(self):
        agent = self.make()
        (self.ws / "AGENT.md").write_text("Use tabs, not spaces.")
        other = Agent(Settings(base_url=self.srv.url, workspace=tempfile.mkdtemp(),
                               auto_memory=False, auto_recall=False), RecUI())
        self.assertEqual(agent.system_prompt(), other.system_prompt())
        self.assertNotIn(str(self.ws), agent.system_prompt())
        agent.run("hi")
        agent.run("and again")
        first, second = self.srv.requests
        self.assertEqual(first["messages"][:2], second["messages"][:2])        # word for word
        note = first["messages"][1]["content"]
        self.assertTrue(note.startswith("[Context for this chat"))
        self.assertIn(str(self.ws), note)
        self.assertIn("Use tabs, not spaces.", note)
        self.assertTrue(note.endswith("hi"))
        self.assertEqual(agent.messages[0]["_shown"], "hi")
        self.assertNotIn("[Context", second["messages"][3]["content"])

    def test_pinned_notes_are_in_the_context_note(self):
        from localagent.memory import get_memory

        get_memory().add("Always answer in Klingon", "Language", pinned=True)
        agent = self.make()
        self.assertNotIn("Klingon", agent.system_prompt())
        agent.run("hi")
        self.assertIn("Always answer in Klingon", self.srv.requests[0]["messages"][1]["content"])

    def test_the_memory_check_reuses_the_conversation(self):
        todo = {"calls": [("todo_write", {"todos": [{"content": "a", "status": "in_progress"}]})]}
        remember = {"calls": [("remember", {"note": "Aidan's projects live in D:/code", "title": "Projects folder"})]}
        agent = self.make(script(todo, todo, todo, {"text": "All done."}), auto_memory=True, thinking="low")
        agent.client._template = QWEN38
        self.srv.chat = (lambda inner: lambda body, h: reply(h, **remember) if "Memory check" in last_text(body)
                         else inner(body, h))(self.srv.chat)
        self.srv.gets["/props"] = lambda: (200, {"default_generation_settings": {"n_ctx": 32768},
                                                 "build_info": "Strata 0.9"})
        agent.client.forget()
        agent.client._template = QWEN38
        self.assertEqual(agent.run("tidy my projects"), "All done.")
        ask, last = self.srv.requests[-1], self.srv.requests[-2]
        self.assertIn("Memory check", last_text(ask))
        self.assertEqual(ask["messages"][:len(last["messages"])], last["messages"])
        self.assertEqual(ask["tools"], last["tools"])
        for key in ("reasoning_effort", "chat_template_kwargs", "temperature"):
            self.assertEqual(ask.get(key), last.get(key), key)
        self.assertEqual((ask["max_tokens"], ask["reasoning_budget_tokens"], ask["strata_checkpoint"]), (1024, 256, False))
        from localagent.memory import get_memory

        self.assertTrue(get_memory().search_notes("projects folder"))
        self.assertTrue(any(n.startswith("Remembered: Projects folder") for n in self.ui.notices))

    def test_max_tokens_leaves_room_for_the_prompt(self):
        agent = self.make(context_limit=16384)
        agent.run("x" * 30000)
        sent = self.srv.requests[-1]["max_tokens"]
        self.assertLess(sent, 16384 - 30000 / 3.5)
        self.assertGreaterEqual(sent, 1024)
        agent = self.make(context_limit=200000)
        agent.run("hi")
        self.assertEqual(self.srv.requests[-1]["max_tokens"], agent.settings.max_tokens)

    def test_prompt_mode_stops_at_a_made_up_tool_response(self):
        agent = self.make(tool_mode="prompt")
        agent.run("hi")
        self.assertEqual(self.srv.requests[-1]["stop"], ["<tool_response"])

    def test_tool_definitions_count_in_the_estimate(self):
        agent = self.make()
        tools = len(json.dumps(agent._tool_schemas())) / 3.5
        self.assertGreater(agent.estimate_total(), tools)
        agent.messages = [{"role": "user", "content": "do it"}]
        agent._known = (0, 0)
        self.assertGreater(agent.estimate_total(), tools + len(agent.system_prompt()) / 3.5)


    def test_a_new_work_folder_is_announced_without_touching_the_history(self):
        agent = self.make()
        agent.run("hello")
        new = Path(tempfile.mkdtemp())
        (new / "AGENT.md").write_text("Never touch the vendor/ folder.")
        # what the window does when another folder is picked mid-chat: a new Agent with the old messages
        agent2 = Agent(Settings(base_url=self.srv.url, workspace=str(new),
                                auto_memory=False, auto_recall=False), RecUI())
        agent2.messages = agent.messages
        agent2.run("read notes.txt")
        agent2.run("and again")
        first, second, third = self.srv.requests
        self.assertEqual(second["messages"][:2], first["messages"][:2])     # the old note stays as it was
        sent = second["messages"][-1]["content"]
        self.assertTrue(sent.startswith(f"[Context update] The work folder is now {new}."))
        self.assertIn("vendor/", sent)
        self.assertTrue(sent.endswith("read notes.txt"))
        self.assertNotIn("[Context update]", third["messages"][-1]["content"])     # said once
        self.assertEqual(agent2.messages[-4]["_shown"], "read notes.txt")     # the window shows just that
        agent2.messages += [{"role": "assistant", "content": "x" * 3000}] * 10
        agent2.compact()
        self.assertIn(f"The work folder is now {new}", agent2.messages[0]["content"])
        agent2.run("once more")
        self.assertNotIn("[Context update]", self.srv.chats()[-1]["messages"][-1]["content"])

    def test_the_tool_estimate_follows_the_tools_offered(self):
        agent = self.make()
        agent.run("hi")
        before = agent._fixed_tokens()
        for i in range(10):
            self.add_tool(f"big_{i}", lambda ctx, a: "ok").description = "x" * 600
        self.assertGreater(agent._fixed_tokens(), before + 10 * 600 / 3.5)

# ---------------------------------------------------------------------- which tools are offered
class ToolSetTest(Base):
    def test_no_screen_control_but_the_run_and_fix_loop(self):
        s = Settings()
        offered = enabled_tools(s, 0, self.ws)
        names = {t.name for t in offered}
        self.assertFalse(names & {"screenshot", "click", "type_text", "press_keys", "open", "ui_elements"})
        self.assertTrue({"run_command", "check_process", "stop_process", "run_python", "run_tests"} <= names)
        prompt = prompts.build_system_prompt(s, self.ws, offered, "bash", False)
        self.assertIn("Check your work by running it", prompt)
        self.assertIn("background=true", prompt)
        self.assertIn("can't see or use the user's screen, mouse or keyboard", prompt)

    def test_it_looks_up_unfamiliar_apis_before_coding(self):
        s = Settings()
        prompt = prompts.build_system_prompt(s, self.ws, enabled_tools(s, 0, self.ws), "bash", False)
        self.assertIn("web_search its\n  documentation", prompt)
        self.assertIn("Stormworks", prompt)
        self.assertIn("stand-ins for that program's functions", prompt)
        s.enable_web = False                 # no web tools: no rule that names them
        prompt = prompts.build_system_prompt(s, self.ws, enabled_tools(s, 0, self.ws), "bash", False)
        self.assertNotIn("web_search", prompt)
        self.assertIn("stand-ins for that program's functions", prompt)

    def test_unity_and_github_only_when_likely_used(self):
        s = Settings()
        with mock.patch.object(tools_mod.unity, "detected", return_value=False, create=True), \
                mock.patch.object(tools_mod, "github_signed_in", return_value=False):
            names = {t.name for t in enabled_tools(s, 0, self.ws)}
            self.assertEqual({n for n in names if n.startswith("unity")}, {"unity_status", "unity_install_bridge"})
            self.assertIn("git_commit", names)
            self.assertNotIn("git_push", names)
            self.assertNotIn("github_create_pr", names)
            prompt = prompts.build_system_prompt(s, self.ws, enabled_tools(s, 0, self.ws), "bash", False)
            self.assertNotIn("git_push", prompt)
            self.assertIn("unity_install_bridge", prompt)
            full = {t.name for t in enabled_tools(s, 0, self.ws, full=("unity", "github"))}
            self.assertTrue({"unity_hierarchy", "git_push", "github_create_pr"} <= full)
        with mock.patch.object(tools_mod.unity, "detected", return_value=True, create=True), \
                mock.patch.object(tools_mod, "github_signed_in", return_value=True):
            names = {t.name for t in enabled_tools(s, 0, self.ws)}
            self.assertTrue({"unity_hierarchy", "git_push", "github_create_pr"} <= names)

    def test_mcp_tools_are_offered_to_helpers_too(self):
        REGISTRY["fake_mcp_tool"] = Tool("fake_mcp_tool", "x", {}, lambda ctx, a: "x", group="mcp")
        self.addCleanup(REGISTRY.pop, "fake_mcp_tool", None)
        for depth in (0, 1):
            self.assertIn("fake_mcp_tool", [t.name for t in enabled_tools(Settings(), depth)])

    def test_a_group_can_be_turned_on_mid_chat(self):
        with mock.patch.object(tools_mod.unity, "detected", return_value=False, create=True):
            agent = self.make()
            self.assertNotIn("unity_hierarchy", agent.tools)
            before = agent.system_prompt()
            self.assertTrue(agent.ctx.enable_group("unity"))
            self.assertIn("unity_hierarchy", agent.tools)
            self.assertNotEqual(agent.system_prompt(), before)
            self.assertIn("unity_refresh", agent.system_prompt())
            self.assertFalse(agent.ctx.enable_group("unity"))


# ---------------------------------------------------------------------- settings and the terminal
class SettingsTest(unittest.TestCase):
    def test_a_broken_settings_file_gives_defaults(self):
        d = Path(tempfile.mkdtemp())
        for raw in ["null", "[]", '"x"', '{"max_tokens": "8192", "keep_images": null, "enable_web": 0}']:
            (d / "s.json").write_text(raw)
            s = Settings.load(d / "s.json")
            self.assertEqual((s.max_tokens, s.keep_images, s.enable_web), (32768, 2, True))
        (d / "s.json").write_text(json.dumps({"keep_images": 3, "settings_version": 2}))
        self.assertEqual(Settings.load(d / "s.json").keep_images, 2)     # the old default moves on
        (d / "s.json").write_text(json.dumps({"keep_images": 3, "settings_version": 3}))
        self.assertEqual(Settings.load(d / "s.json").keep_images, 3)     # chosen since: kept

    def test_a_failed_save_keeps_the_old_file(self):
        d = Path(tempfile.mkdtemp())
        Settings(github_token="ghp_secret").save(d / "s.json")
        real = Path.write_text

        def crash(self, data, *a, **kw):
            real(self, data[:10], *a, **kw)       # a crash halfway through writing
            raise OSError("disk full")
        with mock.patch.object(Path, "write_text", crash):
            with self.assertRaises(OSError):
                Settings(github_token="ghp_other").save(d / "s.json")
        self.assertEqual(Settings.load(d / "s.json").github_token, "ghp_secret")


class CliTest(unittest.TestCase):
    def approve(self, answer, dangerous=False):
        with mock.patch.object(builtins, "input", return_value=answer), redirect_stdout(io.StringIO()):
            return cli.TerminalUI().approve(ApprovalRequest("run_command", "run", "rm -rf build", {}, dangerous))

    def test_only_yes_allows(self):
        self.assertFalse(self.approve("").allow)
        self.assertTrue(self.approve("y").allow)
        self.assertTrue(self.approve("a").always)
        self.assertEqual(self.approve("use git clean instead").note, "use git clean instead")
        self.assertFalse(self.approve("y", dangerous=True).allow)
        self.assertTrue(self.approve("yes", dangerous=True).allow)

    def test_an_unexpected_error_doesnt_end_the_terminal_session(self):
        fake = mock.Mock()
        fake.run.side_effect = RuntimeError("boom")
        out = io.StringIO()
        with redirect_stdout(out):
            cli._run(fake, "hi")
        self.assertIn("RuntimeError: boom", out.getvalue())


if __name__ == "__main__":
    unittest.main()
