"""Talking to a local OpenAI-compatible server (llama.cpp's llama-server, Strata, LM Studio, Ollama...)."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.exceptions import ReadTimeoutError

RETRY_FOR = 60.0      # seconds to keep asking a server that is loading its model or restarting
LOADING = "Waiting for the model to finish loading..."
STARTING = "Waiting for the model server to start..."


class ServerError(Exception):
    pass


class ReplyLost(ServerError):
    """The server stopped answering or dropped the connection partway through a reply (it crashed or
    restarted). Asking again usually works: the agent retries on its own. `pause` is how many seconds
    to wait first (the server is up but needs a moment, say for RAM to free up)."""
    pause = 0.0


class RamLow(ReplyLost):
    """Basalt stopped the reply because the PC's free RAM fell below its guard. The engine stays up,
    so a retry after a pause usually goes through once other programs give some RAM back."""
    pause = 20.0


class ToolsUnsupported(ServerError):
    """The server refused the 'tools' field; we fall back to tools-in-the-prompt."""


class ImagesUnsupported(ServerError):
    """The server can't take images (llama.cpp started without the vision file, --mmproj)."""


class Stopped(Exception):
    """The user pressed Stop before the server answered."""


class _Again(Exception):
    """Worth another try in a moment: the server is loading its model, restarting, or dropped the
    connection before answering. why is the status to show while waiting."""

    def __init__(self, why: str, error: ServerError):
        super().__init__(str(error))
        self.why, self.error = why, error


# Only wording that really means "no tools here": other errors that merely mention a tool (say, a
# history the server couldn't parse) must not switch the chat to tools-in-the-prompt for good.
_NO_TOOLS = re.compile(r"jinja|tools? (?:are |is )?not supported|does not support tools", re.I)
# Strata's words when its engine process died: the next request starts a fresh one.
_ENGINE_DIED = re.compile(r"engine stopped unexpectedly|next request restarts", re.I)
# Basalt's words when free RAM dipped below its guard: that reply stopped, the engine is still up.
_RAM_LOW = re.compile(r"RAM low\b.*request stopped", re.I | re.S)


def _lost_error(text: str):
    """The exception class for a server error: retryable (ReplyLost/RamLow) or not (ServerError)."""
    if _RAM_LOW.search(text):
        return RamLow
    return ReplyLost if _ENGINE_DIED.search(text) else ServerError
_NO_IMAGES = re.compile(r"image input is not supported|mmproj|(?:does not|doesn't) support (?:images|image input|vision)"
                        r"|vision is not (?:supported|enabled)", re.I)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # JSON text as the model wrote it

    def parsed(self) -> dict:
        return parse_args(self.arguments)


@dataclass
class Turn:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)   # + llama.cpp's timings: cache_n, prompt_n, prompt_ms, draft_n...
    tokens_per_sec: float | None = None
    stopped: bool = False


def parse_args(text: str) -> dict:
    """Parse tool arguments, forgiving the usual small-model mistakes."""
    if isinstance(text, dict):
        return text
    text = (text or "").strip()
    if not text:
        return {}
    try:
        val = json.loads(text)
        if isinstance(val, str):  # double-encoded
            val = json.loads(val)
        if isinstance(val, dict):
            return val
    except (ValueError, TypeError):
        pass
    fixed = re.sub(r"^```(?:json)?|```$", "", text).strip()
    fixed = re.sub(r",\s*([}\]])", r"\1", fixed)          # trailing commas
    if fixed.count("{") > fixed.count("}"):
        fixed += "}" * (fixed.count("{") - fixed.count("}"))
    try:
        val = json.loads(fixed)
        if isinstance(val, dict):
            return val
    except ValueError as e:
        raise ValueError(f"arguments are not valid JSON ({e}): {text[:300]}")
    raise ValueError(f"arguments must be a JSON object: {text[:300]}")


def strip_thinking(text: str) -> str:
    """The answer without the model's thinking: <think> blocks (also one left open when the reply
    was cut off), and everything before a lone </think> (templates that open the block themselves)."""
    text = re.sub(r"<think>.*?(?:</think>|$)", "", text or "", flags=re.S)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    return text.strip()


_TOOL_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)
_FUNC_XML = re.compile(r"<function=([\w.-]+)>(.*?)(?:</function>|$)", re.S)
# The newline after the opening tag and the one before the closing tag are part of the format;
# anything else (a first line's indent, a file's final newline) is part of the value.
_PARAM_XML = re.compile(r"<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>", re.S)


def _xml_value(text: str, prop: dict | None):
    """A <parameter> value. Text stays text where the tool wants a string (file content that happens
    to be JSON, a version number like 3); elsewhere numbers, true/false and lists are read as JSON."""
    kind = (prop or {}).get("type")
    if kind == "string" or (isinstance(kind, list) and "string" in kind):
        return text
    try:
        return json.loads(text)
    except ValueError:
        return text


def _xml_call(name: str, body: str, schemas: dict | None) -> ToolCall:
    props = (schemas or {}).get(name) or {}
    args = {k: _xml_value(v, props.get(k)) for k, v in _PARAM_XML.findall(body)}
    return ToolCall(_new_id(), name, json.dumps(args, ensure_ascii=False))


_FENCE = re.compile(r"```.*?(?:```|$)", re.S)


def extract_text_tool_calls(text: str, schemas: dict | None = None,
                            at_end: bool = False) -> tuple[str, list[ToolCall]]:
    """Find tool calls written as text (<tool_call>{json}</tool_call>, or the XML style of Qwen's
    templates, <function=name><parameter=x>..., with or without <tool_call> around it). Returns the
    text without them, and the calls. Anything from a <tool_response> on is dropped: the model wrote
    it itself instead of waiting for the real one. schemas: {tool name: its parameters' properties},
    so XML values are only turned into numbers, lists etc. where the tool expects that.
    Calls shown inside ``` code blocks are examples, not calls. at_end: only calls that end the reply
    count (a model with real tool calling only falls back to text for a call it makes last)."""
    text = text.split("<tool_response", 1)[0]
    masked = _FENCE.sub(lambda m: " " * len(m.group(0)), text)     # same length: positions still match
    found = list(_TOOL_BLOCK.finditer(masked))
    bare = not found
    if bare:   # <function=...> without the <tool_call> around it
        found = list(_FUNC_XML.finditer(masked))
    if not found or (at_end and text[found[-1].end():].strip()):
        return text, []
    calls: list[ToolCall] = []
    for m in found:
        if bare:
            calls.append(_xml_call(text[m.start(1):m.end(1)], text[m.start(2):m.end(2)], schemas))
            continue
        block = text[m.start(1):m.end(1)].strip()
        xml = _FUNC_XML.search(block)
        if xml:
            calls.append(_xml_call(xml.group(1), xml.group(2), schemas))
            continue
        try:
            obj = parse_args(block)
        except ValueError:
            continue
        name = obj.get("name") or obj.get("tool") or obj.get("function")
        args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
        if isinstance(name, dict):  # {"function": {"name":..., "arguments":...}}
            args, name = name.get("arguments", {}), name.get("name")
        if name:
            calls.append(ToolCall(_new_id(), str(name), args if isinstance(args, str) else json.dumps(args)))
    if not calls:
        return text, []
    clean, last = [], 0
    for m in found:
        clean.append(text[last:m.start()])
        last = m.end()
    clean.append(text[last:])
    return "".join(clean).strip(), calls


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


class ThinkSplitter:
    """Moves <think>...</think> text out of the answer, even when tags arrive split across chunks.
    A </think> before any <think> (the template opened the block in the prompt) means the text so
    far was thinking too: it yields ("retract", "") so the caller can move that text over."""

    def __init__(self):
        self.inside = False
        self.seen = False      # any think tag yet
        self.buf = ""

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self.buf += chunk
        out = []
        while self.buf:
            tags = ["</think>"] if self.inside else ["<think>"] if self.seen else ["<think>", "</think>"]
            hits = [(self.buf.find(t), t) for t in tags if t in self.buf]
            if hits:
                i, tag = min(hits)
                if tag == "</think>" and not self.inside:      # a lone closing tag
                    out.append(("retract", ""))
                    if i:
                        out.append(("reasoning", self.buf[:i]))
                else:
                    if i:
                        out.append(("reasoning" if self.inside else "text", self.buf[:i]))
                    self.inside = not self.inside
                self.buf = self.buf[i + len(tag):]
                self.seen = True
                continue
            # keep a possible partial tag for the next chunk
            keep = max((n for t in tags for n in range(1, len(t)) if self.buf.endswith(t[:n])), default=0)
            emit = self.buf[:len(self.buf) - keep]
            if emit:
                out.append(("reasoning" if self.inside else "text", emit))
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buf = self.buf, ""
        return [("reasoning" if self.inside else "text", rest)] if rest else []


# ---------------------------------------------------------------------- Stop
# Stop has to work while the server is still reading the prompt or loading the model, when no
# answer (not even its headers) has arrived and the agent's thread is stuck waiting on the socket.
# So the connections of LLMClient's session note which socket each thread's request is on, and a
# watcher thread cuts that socket when Stop is pressed: the waiting read then fails at once, and
# llama-server, seeing the connection gone, stops working on the request.
_ACTIVE: dict[int, object] = {}       # watched thread id -> the connection its current request is on


class _Tracked:
    raw_sock = None    # kept: http.client lets go of .sock once a close-delimited reply starts

    def connect(self):
        super().connect()
        self.raw_sock = self.sock

    def request(self, *args, **kwargs):
        self.raw_sock = self.sock
        me = threading.get_ident()
        if me in _ACTIVE:          # a request Stop can cut (LLMClient._cut_on_stop is watching)
            _ACTIVE[me] = self
        return super().request(*args, **kwargs)


class _Conn(_Tracked, HTTPConnection):
    pass


class _TLSConn(_Tracked, HTTPSConnection):
    pass


class _Pool(HTTPConnectionPool):
    ConnectionCls = _Conn


class _TLSPool(HTTPSConnectionPool):
    ConnectionCls = _TLSConn


class _Adapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {"http": _Pool, "https": _TLSPool}


def _cut(sock) -> None:
    """Cut a connection another thread is waiting on. shutdown() wakes that thread on Linux and
    tells the server we're gone; closing the handle is what wakes it on Windows."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except (OSError, ValueError):
        pass
    try:
        socket.close(sock.detach())
    except (OSError, ValueError):
        pass


def _is_set(stop: threading.Event | None) -> bool:
    return stop is not None and stop.is_set()


class LLMClient:
    def __init__(self, base_url: str, api_key: str = "local", model: str = "", timeout: float = 900):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "local"
        self.model = model
        self.timeout = timeout
        self.retry_for = RETRY_FOR
        self.session = requests.Session()
        self.session.trust_env = False   # never send local traffic through a system proxy
        for prefix in ("http://", "https://"):
            self.session.mount(prefix, _Adapter())
        self._template: str | None = None
        self._props: dict | None = None
        self._strata: bool | None = None

    @property
    def root(self) -> str:
        return re.sub(r"/v1$", "", self.base_url)

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def forget(self) -> None:
        """Forget what the server told us (it may have restarted with another model)."""
        self._template = self._props = self._strata = None

    # -------------------------------------------------------------- info
    def models(self) -> list[str]:
        r = self.session.get(f"{self.base_url}/models", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", []) if m.get("id")]

    def resolve_model(self) -> str:
        if not self.model:
            try:
                ids = self.models()
            except (requests.RequestException, ValueError):
                return "local"     # not kept: asked again once the server answers
            self.model = ids[0] if ids else "local"
        return self.model

    def _get_json(self, url: str) -> dict | None:
        """A GET's JSON object; {} when the server answered without one (no such endpoint), None
        when it didn't answer properly (not running, or 503 while loading the model)."""
        try:
            r = self.session.get(url, headers=self._headers(), timeout=5)
        except requests.RequestException:
            return None
        if r.status_code == 503 or r.status_code >= 500:
            return None
        try:
            j = r.json() if r.ok else {}
        except ValueError:
            j = {}
        return j if isinstance(j, dict) else {}

    def props(self) -> dict:
        """llama.cpp's and Strata's /props (context size, chat template, vision); {} for servers
        without it. Only an answer is kept: while the model is still loading it is asked again."""
        if self._props is None:
            j = self._get_json(f"{self.root}/props")
            if j is None:
                return {}
            self._props = j
        return self._props

    def context_size(self) -> int | None:
        """How many tokens fit, when the server tells us (llama.cpp, Strata, LM Studio and Ollama do)."""
        model = self.resolve_model()
        p = self.props()
        n = (p.get("default_generation_settings") or {}).get("n_ctx") or p.get("n_ctx")
        if n:
            return int(n)
        j = self._get_json(f"{self.root}/api/v0/models/{model}") or {}          # LM Studio
        n = j.get("loaded_context_length") or j.get("max_context_length")
        if n:
            return int(n)
        loaded = (self._get_json(f"{self.root}/api/ps") or {}).get("models") or []   # Ollama
        for m in loaded:
            if isinstance(m, dict) and m.get("context_length") and (
                    model in (m.get("name"), m.get("model")) or len(loaded) == 1):
                return int(m["context_length"])
        return None

    def chat_template(self) -> str | None:
        """The model's chat template, when the server shares it (llama.cpp and Strata do): "" when
        it doesn't, None while it isn't answering yet. Shows which thinking levels the model knows."""
        if self._template is None:
            p = self.props()
            if self._props is None:
                return None
            self._template = p.get("chat_template") or ""
        return self._template

    def vision(self) -> bool | None:
        """Whether the server can take images (llama.cpp and Strata say so in /props); None = can't tell."""
        m = self.props().get("modalities")
        return bool(m["vision"]) if isinstance(m, dict) and "vision" in m else None

    def is_strata(self) -> bool:
        """Strata names itself in /props ("build_info": "Strata ...") and /health ("service": "strata")."""
        if self._strata is None:
            if str(self.props().get("build_info", "")).lower().startswith("strata"):
                self._strata = True
            else:
                j = self._get_json(f"{self.root}/health")
                if j is None:
                    return False
                self._strata = j.get("service") == "strata"
        return self._strata

    def health(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/models", headers=self._headers(), timeout=3)
            return r.ok
        except requests.RequestException:
            return False

    def wait_until_ready(self, stop: threading.Event | None = None,
                         on_status: Callable[[str], None] | None = None) -> None:
        """Wait while the server is still loading its model (503) or isn't listening yet, for up to
        RETRY_FOR seconds, so the model's name, context size and thinking levels can be read before
        a chat starts. Raises ServerError if it doesn't come up, Stopped on Stop."""
        def attempt():
            try:
                r = self.session.get(f"{self.base_url}/models", headers=self._headers(), timeout=10)
            except requests.ConnectionError:
                raise _Again(STARTING, ServerError(f"Can't reach the model server at {self.base_url}. Is it running?"))
            except requests.RequestException as e:
                raise ServerError(str(e))
            if r.status_code == 503:
                raise _Again(LOADING, ServerError(f"The model server is still loading the model: {r.text[:300]}"))
        self._retrying(attempt, stop, on_status)

    # -------------------------------------------------------------- chat
    def _body(self, messages, tools, params, stream) -> dict:
        body = {"model": self.resolve_model(), "messages": messages, "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        body.update({k: v for k, v in params.items() if v is not None})
        return body

    def _post(self, body, stream):
        try:
            r = self.session.post(f"{self.base_url}/chat/completions", headers=self._headers(),
                                  data=json.dumps(body), stream=stream, timeout=(10, self.timeout))
        except requests.ReadTimeout:
            raise ServerError(f"The model server didn't answer within {self.timeout:.0f} seconds.")
        except requests.ConnectionError:
            raise _Again(STARTING, ServerError(f"Can't reach the model server at {self.base_url}. Is it running?"))
        except requests.RequestException as e:
            raise ServerError(str(e))
        if r.status_code == 503:     # llama-server while it loads the model; Strata while it restarts
            text = r.text[:500]
            r.close()
            raise _Again(LOADING, ServerError(f"Server error 503: {text}"))
        if r.status_code >= 400:
            text = r.text[:1500]
            r.close()
            if body.get("tools") and r.status_code in (400, 422, 500, 501) and _NO_TOOLS.search(text):
                raise ToolsUnsupported(text)
            if _NO_IMAGES.search(text) and _has_images(body["messages"]):
                raise ImagesUnsupported(f"Server error {r.status_code}: {text}")
            if r.status_code >= 500 and _lost_error(text) is not ServerError:
                raise _lost_error(text)(f"Server error {r.status_code}: {text}")
            raise ServerError(f"Server error {r.status_code}: {text}")
        return r

    def _retrying(self, attempt, stop, on_status):
        """attempt(), tried again while the server is loading its model, not listening yet, or
        dropped the connection before answering: with growing pauses, for up to retry_for seconds."""
        deadline, pause = None, 1.0
        try:
            while True:
                try:
                    return attempt()
                except _Again as e:
                    if _is_set(stop):
                        raise Stopped() from None
                    deadline = deadline or time.time() + self.retry_for
                    if time.time() + pause > deadline:
                        raise e.error from None
                    if on_status:
                        on_status(e.why)
                    if stop.wait(pause) if stop is not None else time.sleep(pause):
                        raise Stopped() from None
                    pause = min(pause * 2, 8.0)
        finally:
            if deadline and on_status:
                on_status("")

    @contextmanager
    def _cut_on_stop(self, stop: threading.Event | None):
        """While the block runs, a watcher thread cuts this thread's connection once stop is set."""
        if stop is None:
            yield
            return
        me, done, lock, cut = threading.get_ident(), threading.Event(), threading.Lock(), []
        _ACTIVE[me] = None

        def watch():
            while not done.is_set():
                if stop.wait(0.1):
                    with lock:
                        sock = getattr(_ACTIVE.get(me), "raw_sock", None)
                        if done.is_set():
                            break
                        if sock is not None:   # none yet: still connecting, try again shortly
                            _cut(sock)
                            cut.append(sock)
                    done.wait(0.1)

        threading.Thread(target=watch, daemon=True, name="stop-watch").start()
        try:
            yield
        finally:
            with lock:
                done.set()
            _ACTIVE.pop(me, None)
            if cut:
                self.session.close()      # never reuse a connection that was cut

    def complete(self, messages: list[dict], params: dict | None = None,
                 stop: threading.Event | None = None) -> str:
        """One reply without streaming, thinking removed. Raises Stopped on Stop."""
        with self._cut_on_stop(stop):
            r = self._retrying(lambda: self._post(self._body(messages, None, params or {}, False), False),
                               stop, None)
        try:
            msg = r.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ServerError(f"Unexpected reply from the model server: {r.text[:300]}")
        return strip_thinking(msg.get("content") or "")

    def stream(self, messages: list[dict], tools: list[dict] | None = None, params: dict | None = None,
               on_text: Callable[[str], None] | None = None,
               on_reasoning: Callable[[str], None] | None = None,
               stop: threading.Event | None = None,
               on_status: Callable[[str], None] | None = None) -> Turn:
        """One streamed reply. Stop (stop set) ends it at any point, also before the server answers:
        the turn then has stopped=True and whatever came so far. on_status shows why it waits when
        the server is still loading or restarting ("" when it's done waiting)."""
        def attempt():
            r = self._post(self._body(messages, tools, params or {}, True), True)
            return self._read(r, on_text or (lambda s: None), on_reasoning or (lambda s: None), stop)

        with self._cut_on_stop(stop):
            try:
                return self._retrying(attempt, stop, on_status)
            except Stopped:
                return Turn(stopped=True)

    def _read(self, r, on_text, on_reasoning, stop) -> Turn:
        turn = Turn()
        calls: dict[int, dict] = {}
        splitter = ThinkSplitter()
        first_token = None
        usage, stats = {}, {}
        done = False

        def emit(kind, s):
            if kind == "retract":       # a lone </think>: what came as the answer was thinking
                turn.reasoning, turn.content = turn.reasoning + turn.content, ""
            elif kind == "text":
                turn.content += s
                on_text(s)
            else:
                turn.reasoning += s
                on_reasoning(s)

        try:
            for raw in r.iter_lines(decode_unicode=False):
                if _is_set(stop):
                    turn.stopped = True
                    break
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    done = True
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("error"):
                    err = str(chunk["error"])
                    raise _lost_error(err)(err)
                if chunk.get("usage"):
                    usage = chunk["usage"]
                if isinstance(chunk.get("timings"), dict):
                    t = chunk["timings"]
                    if t.get("predicted_per_second"):
                        turn.tokens_per_sec = float(t["predicted_per_second"])
                    for key, name in (("cache_n", "cache_n"), ("prompt_n", "prompt_n"), ("prompt_ms", "prompt_ms"),
                                      ("draft_n", "draft_n"), ("draft_n_accepted", "draft_accepted")):
                        if isinstance(t.get(key), (int, float)):
                            stats[name] = t[key]
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        first_token = first_token or time.time()
                        splitter.seen = True    # thinking comes separately: a lone </think> in the answer is text
                        emit("reasoning", reasoning)
                    if delta.get("content"):
                        first_token = first_token or time.time()
                        for kind, s in splitter.feed(delta["content"]):
                            emit(kind, s)
                    for tc in delta.get("tool_calls") or []:
                        first_token = first_token or time.time()
                        slot = calls.setdefault(tc.get("index", len(calls)), {"id": None, "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] += fn["name"]
                        if fn.get("arguments"):
                            a = fn["arguments"]
                            slot["args"] += a if isinstance(a, str) else json.dumps(a)
                    if choice.get("finish_reason"):
                        turn.finish_reason = choice["finish_reason"]
        except requests.RequestException as e:
            if _is_set(stop):
                turn.stopped = True
            elif isinstance(e, requests.Timeout) or (e.args and isinstance(e.args[0], ReadTimeoutError)):
                raise ReplyLost(f"The model server sent nothing for {self.timeout:.0f} seconds.")
            elif not (first_token or calls):
                raise _Again(STARTING, ServerError(f"The model server closed the connection before answering ({e})."))
            else:
                raise ReplyLost(f"The connection to the model server broke in the middle of the reply ({e}).")
        finally:
            r.close()
        if not done and not turn.finish_reason and not turn.stopped:
            if _is_set(stop):
                turn.stopped = True
            elif not (first_token or calls):
                raise _Again(STARTING, ServerError("The model server closed the connection before answering."))
            else:     # a plain connection that just ended: the server died mid-reply
                raise ReplyLost("The model server closed the connection in the middle of the reply (did it "
                                "crash or restart?).")
        for kind, s in splitter.flush():
            emit(kind, s)
        turn.usage = {**usage, **stats}
        turn.tool_calls = [ToolCall(c["id"] or _new_id(), c["name"], c["args"] or "{}")
                           for _, c in sorted(calls.items()) if c["name"]]
        if turn.tokens_per_sec is None and first_token and usage.get("completion_tokens"):
            dt = time.time() - first_token
            if dt > 0.2:
                turn.tokens_per_sec = usage["completion_tokens"] / dt
        return turn


def _has_images(messages: list[dict]) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"])
               for m in messages)
