"""Talking to a local OpenAI-compatible server (llama.cpp's llama-server, LM Studio, Ollama...)."""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable

import requests


class ServerError(Exception):
    pass


class ToolsUnsupported(ServerError):
    """The server refused the 'tools' field; we fall back to tools-in-the-prompt."""


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
    usage: dict = field(default_factory=dict)
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


_TOOL_BLOCK = re.compile(r"<tool_call>\s*(.*?)\s*(?:</tool_call>|$)", re.S)
_FUNC_XML = re.compile(r"<function=([\w.-]+)>(.*?)</function>", re.S)
_PARAM_XML = re.compile(r"<parameter=([\w.-]+)>\s*(.*?)\s*</parameter>", re.S)


def extract_text_tool_calls(text: str) -> tuple[str, list[ToolCall]]:
    """Find tool calls written as text (<tool_call>{json}</tool_call>, or the XML style some
    Qwen coder templates use). Returns the text without them, and the calls."""
    calls: list[ToolCall] = []
    for block in _TOOL_BLOCK.findall(text):
        block = block.strip()
        xml = _FUNC_XML.search(block)
        if xml:
            args = {}
            for k, v in _PARAM_XML.findall(xml.group(2)):
                try:
                    args[k] = json.loads(v)
                except ValueError:
                    args[k] = v
            calls.append(ToolCall(_new_id(), xml.group(1), json.dumps(args)))
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
    clean = _TOOL_BLOCK.sub("", text).strip() if calls else text
    return clean, calls


def _new_id() -> str:
    return "call_" + uuid.uuid4().hex[:12]


class ThinkSplitter:
    """Moves <think>...</think> text out of the answer, even when tags arrive split across chunks."""

    def __init__(self):
        self.inside = False
        self.buf = ""

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self.buf += chunk
        out = []
        while self.buf:
            tag = "</think>" if self.inside else "<think>"
            i = self.buf.find(tag)
            if i >= 0:
                if i:
                    out.append(("reasoning" if self.inside else "text", self.buf[:i]))
                self.buf = self.buf[i + len(tag):]
                self.inside = not self.inside
                continue
            # keep a possible partial tag for the next chunk
            keep = 0
            for n in range(1, len(tag)):
                if self.buf.endswith(tag[:n]):
                    keep = n
            emit = self.buf[:len(self.buf) - keep]
            if emit:
                out.append(("reasoning" if self.inside else "text", emit))
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return out

    def flush(self) -> list[tuple[str, str]]:
        rest, self.buf = self.buf, ""
        return [("reasoning" if self.inside else "text", rest)] if rest else []


class LLMClient:
    def __init__(self, base_url: str, api_key: str = "local", model: str = "", timeout: float = 900):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "local"
        self.model = model
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False   # never send local traffic through a system proxy

    @property
    def root(self) -> str:
        return re.sub(r"/v1$", "", self.base_url)

    def _headers(self):
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    # -------------------------------------------------------------- info
    def models(self) -> list[str]:
        r = self.session.get(f"{self.base_url}/models", headers=self._headers(), timeout=10)
        r.raise_for_status()
        return [m.get("id") for m in r.json().get("data", []) if m.get("id")]

    def resolve_model(self) -> str:
        if not self.model:
            try:
                ids = self.models()
                self.model = ids[0] if ids else "local"
            except (requests.RequestException, ValueError):
                self.model = "local"
        return self.model

    def context_size(self) -> int | None:
        """How many tokens fit, when the server tells us (llama.cpp and LM Studio do)."""
        try:
            r = self.session.get(f"{self.root}/props", timeout=5)
            if r.ok:
                j = r.json()
                n = (j.get("default_generation_settings") or {}).get("n_ctx") or j.get("n_ctx")
                if n:
                    return int(n)
        except (requests.RequestException, ValueError):
            pass
        try:
            r = self.session.get(f"{self.root}/api/v0/models/{self.model}", timeout=5)
            if r.ok:
                j = r.json()
                n = j.get("loaded_context_length") or j.get("max_context_length")
                if n:
                    return int(n)
        except (requests.RequestException, ValueError):
            pass
        return None

    def health(self) -> bool:
        try:
            r = self.session.get(f"{self.base_url}/models", headers=self._headers(), timeout=3)
            return r.ok
        except requests.RequestException:
            return False

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
        except requests.ConnectionError:
            raise ServerError(f"Can't reach the model server at {self.base_url}. Is it running?")
        except requests.RequestException as e:
            raise ServerError(str(e))
        if r.status_code >= 400:
            text = r.text[:1500]
            if body.get("tools") and r.status_code in (400, 422, 500, 501) and re.search(r"tool|jinja|function", text, re.I):
                raise ToolsUnsupported(text)
            raise ServerError(f"Server error {r.status_code}: {text}")
        return r

    def complete(self, messages: list[dict], params: dict | None = None) -> str:
        r = self._post(self._body(messages, None, params or {}, False), False)
        msg = r.json()["choices"][0]["message"]
        content = msg.get("content") or ""
        return re.sub(r"<think>.*?</think>", "", content, flags=re.S).strip()

    def stream(self, messages: list[dict], tools: list[dict] | None = None, params: dict | None = None,
               on_text: Callable[[str], None] | None = None,
               on_reasoning: Callable[[str], None] | None = None,
               stop: threading.Event | None = None) -> Turn:
        r = self._post(self._body(messages, tools, params or {}, True), True)
        turn = Turn()
        calls: dict[int, dict] = {}
        splitter = ThinkSplitter()
        first_token = None
        n_chunks = 0
        on_text = on_text or (lambda s: None)
        on_reasoning = on_reasoning or (lambda s: None)

        def emit(kind, s):
            if kind == "text":
                turn.content += s
                on_text(s)
            else:
                turn.reasoning += s
                on_reasoning(s)

        try:
            for raw in r.iter_lines(decode_unicode=False):
                if stop is not None and stop.is_set():
                    turn.stopped = True
                    break
                if not raw:
                    continue
                line = raw.decode("utf-8", "replace")
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if chunk.get("error"):
                    raise ServerError(str(chunk["error"]))
                if chunk.get("usage"):
                    turn.usage = chunk["usage"]
                if chunk.get("timings"):
                    tps = chunk["timings"].get("predicted_per_second")
                    if tps:
                        turn.tokens_per_sec = float(tps)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        first_token = first_token or time.time()
                        emit("reasoning", reasoning)
                    if delta.get("content"):
                        first_token = first_token or time.time()
                        n_chunks += 1
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
        finally:
            r.close()
        for kind, s in splitter.flush():
            emit(kind, s)
        turn.tool_calls = [ToolCall(c["id"] or _new_id(), c["name"], c["args"] or "{}")
                           for _, c in sorted(calls.items()) if c["name"]]
        if turn.tokens_per_sec is None and first_token and turn.usage.get("completion_tokens"):
            dt = time.time() - first_token
            if dt > 0.2:
                turn.tokens_per_sec = turn.usage["completion_tokens"] / dt
        return turn
