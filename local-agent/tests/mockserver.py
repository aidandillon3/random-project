"""A fake OpenAI-compatible model server for tests. Each scripted reply is either text or a list
of tool calls, streamed back the way llama-server does it."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def text(s: str, reasoning: str = "") -> dict:
    return {"text": s, "reasoning": reasoning}


def calls(*items: tuple[str, dict]) -> dict:
    return {"calls": [{"name": n, "arguments": a} for n, a in items]}


class MockServer:
    def __init__(self, replies=None, n_ctx=32768, reject_tools=False):
        self.replies = list(replies or [])
        self.requests: list[dict] = []
        self.n_ctx = n_ctx
        self.reject_tools = reject_tools
        self.complete_reply = "SUMMARY: earlier work"
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.endswith("/models"):
                    self._json(200, {"data": [{"id": "test-model"}]})
                elif self.path == "/props":
                    self._json(200, {"default_generation_settings": {"n_ctx": outer.n_ctx}})
                else:
                    self._json(404, {"error": "nope"})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                if body.get("tools") and outer.reject_tools:
                    self._json(500, {"error": {"message": "tools param requires --jinja flag"}})
                    return
                if not body.get("stream"):
                    self._json(200, {"choices": [{"message": {"role": "assistant", "content": outer.complete_reply}}]})
                    return
                reply = outer.replies.pop(0) if outer.replies else text("done")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(obj):
                    self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                    self.wfile.flush()

                if reply.get("error"):          # what Strata sends when its engine process died
                    send({"error": {"type": "server_error", "message": reply["error"]}})
                    self.wfile.write(b"data: [DONE]\n\n")
                    return
                if reply.get("reasoning"):
                    send({"choices": [{"delta": {"reasoning_content": reply["reasoning"]}}]})
                if "text" in reply:
                    s = reply["text"]
                    for i in range(0, len(s), 7):
                        send({"choices": [{"delta": {"content": s[i:i + 7]}}]})
                    finish = reply.get("finish", "stop")      # "length": cut off by a limit
                    if reply.get("die"):              # the server crashed partway through the reply
                        return
                else:
                    for i, c in enumerate(reply["calls"]):
                        args = json.dumps(c["arguments"]) if not isinstance(c["arguments"], str) else c["arguments"]
                        send({"choices": [{"delta": {"tool_calls": [{"index": i, "id": f"c{len(outer.requests)}_{i}",
                              "type": "function", "function": {"name": c["name"], "arguments": ""}}]}}]})
                        for j in range(0, len(args), 5):
                            send({"choices": [{"delta": {"tool_calls": [{"index": i, "function": {"arguments": args[j:j + 5]}}]}}]})
                    finish = "tool_calls"
                send({"choices": [{"delta": {}, "finish_reason": finish}]})
                send({"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 50}})
                self.wfile.write(b"data: [DONE]\n\n")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
