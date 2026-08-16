#!/usr/bin/env python3
"""Opt-in destructive-free lifecycle/API smoke test for the real localhost router."""
from __future__ import annotations

import argparse
import http.client
import json
import math
import pathlib
import subprocess
import sys
import time
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]


class SmokeError(RuntimeError):
    pass


def bounded_timeout(value: str) -> float:
    timeout = float(value)
    if not math.isfinite(timeout) or not 1 <= timeout <= 600:
        raise argparse.ArgumentTypeError("timeout must be between 1 and 600 seconds")
    return timeout


def request(base: str, method: str, path: str, timeout: float, payload=None):
    parsed = urlsplit(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, parsed.path.rstrip("/") + path, body=body, headers=headers)
        response = connection.getresponse()
        raw = response.read(1024 * 1024)
        if len(raw) == 1024 * 1024:
            raise SmokeError(f"{path} response exceeded 1 MiB")
        try:
            decoded = json.loads(raw or b"{}")
        except json.JSONDecodeError as error:
            raise SmokeError(f"{path} returned invalid JSON: {error}") from error
        return response.status, decoded
    except OSError as error:
        raise SmokeError(f"{path} request failed: {error}") from error
    finally:
        connection.close()


def health(base: str, timeout: float) -> None:
    status, body = request(base, "GET", "/health", min(timeout, 10))
    if status != 200 or not isinstance(body, dict) or body.get("status") != "ok":
        raise SmokeError(f"/health unhealthy: HTTP {status}, {body!r}")


def models(base: str, timeout: float) -> list[dict]:
    status, body = request(base, "GET", "/models", min(timeout, 10))
    rows = body.get("data") if isinstance(body, dict) else None
    if status != 200 or not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SmokeError(f"/models invalid: HTTP {status}, {body!r}")
    return rows


def wait_health(base: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    last = "not attempted"
    while time.monotonic() < deadline:
        try:
            health(base, min(5, timeout))
            return
        except SmokeError as error:
            last = str(error)
            time.sleep(0.25)
    raise SmokeError(f"router did not recover within {timeout:g}s: {last}")


def model_action(action: str, args) -> None:
    command = [str(ROOT / "scripts/router-model.sh"), action,
               "--model-id", args.manifest_model_id,
               "--models-dir", str(args.models_dir),
               "--manifest", str(args.manifest),
               "--base-url", args.base_url,
               "--timeout", str(args.timeout)]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                            timeout=args.timeout + 60)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise SmokeError(f"model {action} failed: {detail}")


def chat_stream(base: str, model: str, prompt: str, timeout: float,
                max_tokens: int, cancel: bool = False) -> str:
    parsed = urlsplit(base)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    deadline = time.monotonic() + timeout
    payload = {"model": model, "messages": [{"role": "user", "content": prompt}],
               "temperature": 0, "stream": True, "max_tokens": max_tokens,
               "chat_template_kwargs": {"enable_thinking": False}}
    try:
        connection.request("POST", parsed.path.rstrip("/") + "/v1/chat/completions",
                           body=json.dumps(payload, separators=(",", ":")),
                           headers={"Content-Type": "application/json", "Accept": "text/event-stream"})
        response = connection.getresponse()
        if response.status != 200:
            detail = response.read(4096).decode("utf-8", "replace")
            raise SmokeError(f"streamed completion returned HTTP {response.status}: {detail}")
        content: list[str] = []
        events = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeError(f"stream exceeded the {timeout:g}s total timeout")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            line = response.readline(256 * 1024)
            if not line:
                break
            if len(line) >= 256 * 1024:
                raise SmokeError("stream event exceeded 256 KiB")
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                return "".join(content)
            try:
                event = json.loads(data)
                delta = event["choices"][0].get("delta", {})
            except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
                raise SmokeError(f"invalid stream event: {error}") from error
            text = delta.get("content", "")
            if isinstance(text, str):
                content.append(text)
            events += 1
            if cancel and events:
                connection.close()
                return "cancelled"
        raise SmokeError("stream ended without [DONE]")
    except OSError as error:
        raise SmokeError(f"streamed completion failed: {error}") from error
    finally:
        connection.close()


def check_failed_load(base: str, timeout: float) -> None:
    status, body = request(base, "POST", "/models/load", min(timeout, 15),
                           {"model": "LOCAL_AI_INTENTIONALLY_MISSING_MODEL"})
    # b10446 may reject immediately or acknowledge before exposing an async
    # failed state. It must never make the missing ID a loaded model.
    if status < 400 and isinstance(body, dict) and body.get("success") is True:
        deadline = time.monotonic() + min(timeout, 30)
        while time.monotonic() < deadline:
            matching = [row for row in models(base, timeout)
                        if row.get("id") == "LOCAL_AI_INTENTIONALLY_MISSING_MODEL"]
            if matching:
                state = matching[0].get("status")
                value = state.get("value") if isinstance(state, dict) else state
                failed = isinstance(state, dict) and state.get("failed") is True
                if value == "loaded":
                    raise SmokeError("missing model unexpectedly became loaded")
                if failed or value in {"unloaded", "failed"}:
                    break
            time.sleep(0.2)
    health(base, timeout)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="required acknowledgement")
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="Qwen3.5-27B-Q8_0", help="router/API model ID")
    parser.add_argument("--manifest-model-id", default="qwen3.5-27b-q8_0")
    parser.add_argument("--models-dir", type=pathlib.Path,
                        default=pathlib.Path.home() / ".local/share/local-ai/models")
    parser.add_argument("--manifest", type=pathlib.Path, default=ROOT / "config/models.json")
    parser.add_argument("--service", default="local-ai-router.service")
    parser.add_argument("--timeout", type=bounded_timeout, default=180.0)
    parser.add_argument("--result", type=pathlib.Path)
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if (not args.real or parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or
            parsed.username or parsed.password or parsed.query or parsed.fragment):
        parser.error("--real and a plain-http 127.0.0.1 base URL are required")
    if not args.service or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789@_.-" for ch in args.service):
        parser.error("service name is invalid")
    args.base_url = args.base_url.rstrip("/")
    return args


def main() -> int:
    args = parse_args()
    checks: list[dict] = []
    started = time.monotonic()

    def checked(name, operation):
        before = time.monotonic()
        operation()
        checks.append({"name": name, "passed": True,
                       "duration_ms": round((time.monotonic() - before) * 1000, 3)})
        print(f"ok: {name}", file=sys.stderr)

    loaded = False
    try:
        checked("health-and-model-inventory", lambda: (health(args.base_url, args.timeout), models(args.base_url, args.timeout)))
        checked("failed-model-load-recovery", lambda: check_failed_load(args.base_url, args.timeout))
        checked("verified-model-load", lambda: model_action("load", args))
        loaded = True
        def exact_chat(marker: str, prompt: str, max_tokens: int = 16):
            content = chat_stream(args.base_url, args.model, prompt, args.timeout, max_tokens)
            if marker not in content:
                raise SmokeError(f"stream omitted required marker {marker}")

        checked("streamed-chat", lambda: exact_chat(
            "SMOKE_OK", "Reply with exactly SMOKE_OK"))
        checked("stream-cancellation", lambda: (
            chat_stream(args.base_url, args.model,
                        "Write the integers from one to one thousand in words.", args.timeout, 256, True),
            wait_health(args.base_url, args.timeout)))
        boundary_prompt = (("boundary " * 30000) +
                           " End of preserved near-context request. Reply with exactly BOUNDARY_OK.")
        checked("near-32k-context", lambda: exact_chat(
            "BOUNDARY_OK", boundary_prompt, 8))
        checked("verified-model-unload", lambda: model_action("unload", args))
        loaded = False

        def restart():
            result = subprocess.run(["systemctl", "--user", "restart", args.service],
                                    text=True, capture_output=True, timeout=30)
            if result.returncode:
                raise SmokeError(f"service restart failed: {(result.stderr or result.stdout).strip()}")
            wait_health(args.base_url, args.timeout)
            if any(row.get("id") == args.model and
                   (row.get("status", {}).get("value") if isinstance(row.get("status"), dict) else row.get("status")) == "loaded"
                   for row in models(args.base_url, args.timeout)):
                raise SmokeError("router violated no-autoload policy after restart")
        checked("service-restart-recovery", restart)
        checked("post-restart-model-load", lambda: model_action("load", args))
        loaded = True
        checked("post-restart-streamed-chat", lambda: exact_chat(
            "RECOVERY_OK", "Reply with exactly RECOVERY_OK"))
        checked("final-model-unload", lambda: model_action("unload", args))
        loaded = False
    except (SmokeError, OSError, subprocess.SubprocessError) as error:
        checks.append({"name": "failure", "passed": False, "error": str(error)[:1000]})
        print(f"error: {error}", file=sys.stderr)
        return_code = 1
    else:
        return_code = 0
    finally:
        if loaded:
            try:
                model_action("unload", args)
            except Exception as error:  # preserve original error while reporting cleanup
                print(f"error: cleanup unload failed: {error}", file=sys.stderr)
                return_code = 1
        result = {"schema_version": 1, "suite": "issue-9-router-api-smoke",
                  "status": "pass" if return_code == 0 else "fail",
                  "duration_ms": round((time.monotonic() - started) * 1000, 3),
                  "endpoint": "http://127.0.0.1:8080", "model": args.model,
                  "runtime": {"ref": "b10446", "commit": "adb55e5"},
                  "context_tokens": 32768,
                  "checks": checks, "model_files_deleted": False}
        encoded = json.dumps(result, sort_keys=True, indent=2) + "\n"
        if args.result:
            args.result.parent.mkdir(parents=True, exist_ok=True)
            args.result.write_text(encoded)
        print(encoded, end="")
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
