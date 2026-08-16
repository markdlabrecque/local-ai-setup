#!/usr/bin/env python3
"""Deterministic, disposable OpenAI-compatible endpoint for evaluation tests."""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def _reply(self, payload, status=200):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._reply({"status": "ok", "model": "fixture-model"})
        elif self.path == "/v1/models":
            self._reply({"object": "list", "data": [{"id": "fixture-model"}]})
        else:
            self._reply({"error": "not found"}, 404)

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self._reply({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length) or b"{}")
        messages = request.get("messages", [])
        marker = json.dumps(messages, sort_keys=True)
        # This opt-in delay gives the cancellation case a deterministic
        # in-flight request while allowing /health to prove recovery.
        if request.get("eval_cancel_probe"):
            time.sleep(0.25)
        # The first overflow attempt must observe a real OpenAI-style context
        # error before the evaluator may claim a compacted retry.
        if "overflow " in marker and len(marker) > 200000 and "overflow compacted context" not in marker:
            self._reply({"error": {"message": "context length exceeded",
                                   "type": "context_length_exceeded",
                                   "code": "context_length_exceeded"}}, 400)
            return
        has_tool_result = any(m.get("role") == "tool" for m in messages if isinstance(m, dict))
        if "malformed" in marker:
            message = {"role": "assistant", "tool_calls": [{"id": "bad", "type": "function"}]}
        elif "parallel" in marker and not has_tool_result:
            message = {"role": "assistant", "tool_calls": [
                {"id": "call-a", "type": "function", "function": {"name": "read", "arguments": '{"path":"src/fixture_target.py"}'}},
                {"id": "call-b", "type": "function", "function": {"name": "read", "arguments": '{"path":"src/fixture_target.py"}'}},
            ]}
        elif has_tool_result and "sequential" not in marker:
            message = {"role": "assistant", "tool_calls": [
                {"id": "call-2", "type": "function", "function": {"name": "read", "arguments": '{"path":"src/fixture_target.py"}'}},
            ]}
        elif "sequential" in marker and not has_tool_result:
            message = {"role": "assistant", "tool_calls": [
                {"id": "call-1", "type": "function", "function": {"name": "read", "arguments": '{"path":"src/fixture_target.py"}'}},
            ]}
        elif "reasoning" in marker and request.get("reasoning"):
            message = {"role": "assistant", "content": "CONTRACT_OK",
                       "reasoning_content": "fixture reasoning"}
        elif "NEEDLE-31K-7F3A" in marker:
            message = {"role": "assistant", "content": "NEEDLE-31K-7F3A"}
        elif "code-generation" in marker or "named add" in marker:
            message = {"role": "assistant", "content": "def add(a, b):\\n    return a + b"}
        elif "fixture_target" in marker:
            message = {"role": "assistant", "content": "src/fixture_target.py line 3"}
        elif "PRESERVE-USER-CONTENT" in marker:
            message = {"role": "assistant", "content": "PRESERVE-USER-CONTENT"}
        else:
            message = {"role": "assistant", "content": "CONTRACT_OK"}
        prompt_tokens = 32000 if "NEEDLE-31K-7F3A" in marker else 1
        response = {"id": "chatcmpl-fixture", "object": "chat.completion",
                    "model": "fixture-model", "choices": [{"index": 0, "message": message,
                    "finish_reason": "tool_calls" if "tool_calls" in message else "stop"}],
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 1,
                              "total_tokens": prompt_tokens + 1}}
        if request.get("stream"):
            encoded = ("data: " + json.dumps({"choices": [{"delta": {"content": "CONTRACT_OK"}, "index": 0}]}) + "\n\n" 
                       "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        else:
            self._reply(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(server.server_address[1], flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
