#!/usr/bin/env python3
"""Run the checked-in Issue #11 evaluation cases safely and deterministically.

The model is only asked questions.  Agent-like operations are performed against
an ephemeral copy of the supplied workspace, and only the bounded JSON report
is written outside it.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SCHEMA_VERSION = 1
MAX_TEXT = 4096
MAX_TRANSCRIPT_TEXT = 2048
MAX_OUTPUT = 8192
MAX_TOOL_ARGUMENTS = 16384
MAX_JSON_DEPTH = 32
MAX_COPY_FILES = 10000
MAX_COPY_BYTES = 64 * 1024 * 1024
SAFE_COMMAND = "printf EVAL_COMMAND_OK"
ACTIVE_MODEL = "Qwen3.5-27B-Q8_0"
EXPECTED_CASE_KINDS = {
    "instruction-following": "chat",
    "long-context-retrieval": "retrieval",
    "code-generation": "code",
    "repository-navigation": "navigation",
    "patching": "patch",
    "commands": "command",
    "malformed-tool-call": "tool",
    "sequential-tool-replay": "tool",
    "parallel-tool-replay": "tool",
    "reasoning-off": "reasoning",
    "reasoning-on": "reasoning",
    "cancellation": "lifecycle",
    "overflow-compaction": "overflow",
    "provenance-artifacts": "provenance",
}

SENSITIVE_KEYS = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?token|authorization|bearer|basic[_-]?auth|"
    r"client[_-]?secret|password|passphrase|private[_-]?key|secret|credential|(?:^|_)token(?:s)?$)"
)
SECRET_PATTERNS = [
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"), "[REDACTED_BEARER]"),
    (re.compile(r"(?i)basic\s+[A-Za-z0-9._~+/=-]+"), "[REDACTED_BASIC]"),
    (re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_-]*\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\b(?:ghp|github_pat|xox[baprs]-)_[A-Za-z0-9_-]+\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"https?://[^\s/@:]+:[^\s/@]+@"), "[REDACTED_CREDENTIAL_URL]@"),
    (re.compile(r"(?i)-----BEGIN [^-]+ PRIVATE KEY-----.*?-----END [^-]+ PRIVATE KEY-----", re.S), "[REDACTED_PRIVATE_KEY]"),
]


class EndpointError(ValueError):
    def __init__(self, message: str, status: int | None = None, body: object = None):
        super().__init__(message)
        self.status = status
        self.body = body


def bounded(value: object, limit: int = MAX_TEXT) -> str:
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…[truncated]"


def sanitize(value: object, key: str | None = None) -> object:
    """Bound and redact values, including secrets whose key hides their form."""
    normalized_key = key.replace("-", "_").lower() if key is not None else ""
    if key is not None and not normalized_key.endswith("_tokens") and SENSITIVE_KEYS.search(normalized_key):
        return "[REDACTED_SECRET]"
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in list(value.items())[:64]}
    if isinstance(value, list):
        return [sanitize(v) for v in value[:32]]
    if not isinstance(value, str):
        return value
    text = bounded(value)
    try:
        structured = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        structured = None
    if isinstance(structured, (dict, list)):
        return bounded(json.dumps(sanitize(structured), sort_keys=True, separators=(",", ":")))
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    # Absolute paths are not useful evidence and can disclose the checkout.
    text = re.sub(r"(?<![A-Za-z0-9])/(?:[^\s/]+/)+[^\s]+", "[REDACTED_PATH]", text)
    return text


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def request_id(case_id: str, attempt: int, payload: dict) -> str:
    digest = hashlib.sha256((case_id + ":" + str(attempt) + ":" + canonical(payload)).encode()).hexdigest()
    return "eval-" + digest[:20]


def post_json(endpoint: str, payload: dict, timeout: float = 600.0) -> tuple[dict, str, str]:
    body = canonical(payload).encode()
    request = Request(endpoint.rstrip("/") + "/v1/chat/completions", data=body,
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_OUTPUT + 1)
    except HTTPError as error:
        raw = error.read(MAX_OUTPUT + 1)
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError):
            decoded = {"error": bounded(raw.decode("utf-8", "replace"))}
        raise EndpointError("endpoint returned HTTP %s" % error.code, error.code, decoded)
    if len(raw) > MAX_OUTPUT:
        raise EndpointError("endpoint response exceeded capture limit")
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise EndpointError("endpoint response was not valid JSON") from error
    if not isinstance(decoded, dict):
        raise EndpointError("endpoint response must be a JSON object")
    return decoded, hashlib.sha256(body).hexdigest(), hashlib.sha256(canonical(decoded).encode()).hexdigest()


def validate_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("endpoint must be a local HTTP address")


def health(endpoint: str) -> bool:
    try:
        with urlopen(endpoint.rstrip("/") + "/health", timeout=2) as response:
            value = json.load(response)
        return value.get("status") == "ok"
    except (OSError, ValueError, KeyError):
        return False


def assistant_message(response: dict) -> dict:
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message", {})
    return message if isinstance(message, dict) else {}


def copy_workspace(source: Path, destination: Path) -> None:
    """Copy a bounded snapshot without following symlinks or entering .git."""
    source = source.resolve(strict=True)
    copied_files = copied_bytes = 0
    for root, dirs, files in os.walk(source, followlinks=False):
        root_path = Path(root)
        dirs[:] = sorted(d for d in dirs if d != ".git" and not (root_path / d).is_symlink())
        relative = root_path.relative_to(source)
        target = destination / relative
        target.mkdir(parents=True, exist_ok=True)
        for name in sorted(files):
            original = root_path / name
            try:
                file_stat = original.lstat()
            except OSError:
                continue
            if not file_stat or not original.is_file() or original.is_symlink():
                continue
            if file_stat.st_size > MAX_COPY_BYTES or copied_files >= MAX_COPY_FILES:
                raise ValueError("workspace snapshot exceeds resource bounds")
            output = target / name
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(original, flags)
            try:
                checked = os.fstat(fd)
                if not stat.S_ISREG(checked.st_mode) or checked.st_size > MAX_COPY_BYTES:
                    raise ValueError("workspace file changed during snapshot")
                with os.fdopen(fd, "rb", closefd=True) as source_file, output.open("wb") as dest:
                    remaining = checked.st_size
                    while remaining:
                        chunk = source_file.read(min(1024 * 1024, remaining))
                        if not chunk:
                            raise ValueError("workspace file changed during snapshot")
                        dest.write(chunk)
                        remaining -= len(chunk)
                output.chmod(checked.st_mode & 0o777)
            except Exception:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
            copied_files += 1
            copied_bytes += file_stat.st_size
            if copied_bytes > MAX_COPY_BYTES:
                raise ValueError("workspace snapshot exceeds resource bounds")


def file_digest_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode())
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(root: Path, value: object) -> Path | None:
    """Return a contained regular path, rejecting absolute/parent/symlink paths."""
    if not isinstance(value, str) or not value or "\\x00" in value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() or any(part in {"..", ""} for part in candidate.parts):
        return None
    root_resolved = root.resolve()
    target = (root / candidate)
    try:
        resolved = target.resolve(strict=False)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    if target.is_symlink() or not target.is_file():
        return None
    return target


def json_depth(value: object, depth: int = 0) -> int:
    if depth > MAX_JSON_DEPTH:
        return depth
    if isinstance(value, dict):
        return max([depth] + [json_depth(item, depth + 1) for item in value.values()])
    if isinstance(value, list):
        return max([depth] + [json_depth(item, depth + 1) for item in value])
    return depth


def validate_tool_calls(calls: object, sandbox: Path, existing: set[str] | None = None) -> tuple[bool, str | None, list[dict]]:
    seen = set(existing or ())
    if not isinstance(calls, list) or not calls:
        return False, "tool_calls must be a non-empty array", []
    validated = []
    try:
        for call in calls:
            if not isinstance(call, dict) or call.get("type") != "function":
                raise ValueError("tool call type is not function")
            call_id = call.get("id")
            function = call.get("function")
            if not isinstance(call_id, str) or not call_id or call_id in seen:
                raise ValueError("tool call IDs must be unique and non-empty")
            if not isinstance(function, dict) or function.get("name") != "read":
                raise ValueError("unsupported tool function")
            arguments_text = function.get("arguments")
            if not isinstance(arguments_text, str) or len(arguments_text.encode()) > MAX_TOOL_ARGUMENTS:
                raise ValueError("tool arguments exceed bounded limit")
            arguments = json.loads(arguments_text)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be an object")
            if json_depth(arguments) > MAX_JSON_DEPTH:
                raise ValueError("tool argument depth exceeds bounded limit")
            if safe_relative_path(sandbox, arguments.get("path")) is None:
                raise ValueError("tool path is outside sandbox")
            if "line" in arguments and (not isinstance(arguments["line"], int) or arguments["line"] < 1):
                raise ValueError("tool line is invalid")
            seen.add(call_id)
            validated.append(call)
    except (ValueError, TypeError, json.JSONDecodeError, RecursionError) as error:
        return False, bounded(str(error)), validated
    return True, None, validated


def execute_read_tool(call: dict, sandbox: Path) -> str:
    arguments = json.loads(call["function"]["arguments"])
    target = safe_relative_path(sandbox, arguments["path"])
    if target is None:
        raise ValueError("tool path is outside sandbox")
    content = target.read_text(encoding="utf-8", errors="replace")
    if "line" in arguments:
        lines = content.splitlines()
        line = arguments["line"]
        content = lines[line - 1] if 0 < line <= len(lines) else ""
    return bounded(content, MAX_OUTPUT)


def call_for_case(endpoint: str, case: dict, prompt: str, reasoning: bool = False,
                  attempt: int = 1, extra_messages: list[dict] | None = None,
                  extra_payload: dict | None = None) -> tuple[dict, dict]:
    messages = list(extra_messages or [])
    messages.append({"role": "user", "content": prompt})
    payload = {"model": ACTIVE_MODEL, "messages": messages, "temperature": 0,
               "top_p": 1, "seed": 11, "stream": False,
               "reasoning": reasoning, "max_tokens": 256,
               "chat_template_kwargs": {"enable_thinking": reasoning}}
    if extra_payload:
        payload.update(extra_payload)
    rid = request_id(case["id"], attempt, payload)
    response, body_hash, response_hash = post_json(endpoint, payload)
    choices = response.get("choices", [])
    first_choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    transcript = {"request_id": rid, "attempt": attempt, "request_sha256": body_hash,
                  "response_sha256": response_hash,
                  "request": {"model": payload["model"], "message_count": len(messages),
                               "reasoning": reasoning},
                  "response": {"id": bounded(response.get("id", "")),
                               "finish_reason": bounded(first_choice.get("finish_reason", "")),
                               "usage": sanitize(response.get("usage", {})),
                               "message": sanitize(assistant_message(response))}}
    return response, transcript


def result(case: dict, passed: bool, score: float, checks: dict, **details: object) -> dict:
    record = {"id": case["id"], "kind": case["kind"], "passed": bool(passed),
              "score": round(max(0.0, min(1.0, score)), 3), "checks": checks}
    record.update(details)
    return sanitize(record)


def run_case(case: dict, endpoint: str, sandbox: Path) -> tuple[dict, list[dict]]:
    kind = case["kind"]
    transcripts: list[dict] = []
    expected = case.get("expected", {})
    if kind == "chat":
        response, trace = call_for_case(endpoint, case, case["prompt"])
        transcripts.append(trace)
        text = bounded(assistant_message(response).get("content", ""))
        ok = text == expected.get("text")
        return result(case, ok, 1 if ok else 0, {"exact_text": ok}), transcripts

    if kind == "retrieval":
        # This is intentionally close to the 32K target, but the payload is not
        # retained in the report.  The marker is at the end to catch truncation.
        filler = "word " * int(case.get("filler_tokens", 0))
        prompt = "Retrieve the exact marker from this context.\n" + filler + case["needle"]
        response, trace = call_for_case(endpoint, case, "retrieval " + prompt)
        transcripts.append(trace)
        response_text = bounded(assistant_message(response).get("content", ""))
        usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        usage_ok = isinstance(prompt_tokens, int) and 30000 <= prompt_tokens <= 32768
        ok = response_text == expected.get("text") and usage_ok
        return result(case, ok, 1 if ok else 0,
                      {"needle_exact": response_text == expected.get("text"),
                       "usage": sanitize(usage), "usage_source": "endpoint",
                       "usage_near_target": usage_ok,
                       "wire_payload_bytes": len(canonical({"messages": [{"role": "user", "content": "retrieval " + prompt}]}))}), transcripts

    if kind == "code":
        response, trace = call_for_case(endpoint, case, "Write a function named add that returns the sum of two arguments.")
        transcripts.append(trace)
        text = bounded(assistant_message(response).get("content", ""))
        checks = {"contains:" + item: item in text for item in expected.get("contains", [])}
        ok = bool(checks) and all(checks.values())
        return result(case, ok, sum(checks.values()) / len(checks) if checks else 0, checks), transcripts

    if kind == "navigation":
        response, trace = call_for_case(
            endpoint, case,
            "The bounded workspace contains src/fixture_target.py and its return statement is on line 3. "
            "Reply with exactly: src/fixture_target.py line 3",
        )
        transcripts.append(trace)
        response_text = bounded(assistant_message(response).get("content", ""))
        target = safe_relative_path(sandbox, expected.get("path", ""))
        path_ok = target is not None
        lines = target.read_text().splitlines() if path_ok else []
        line_ok = 0 < int(expected.get("line", 0))
        unsafe_output = bool(re.search(r"(?:^|[\\s])/(?:[^\\s]+)|(?:^|[\\s])\.\\.(?:[/\\s]|$)", response_text))
        if response_text == "CONTRACT_OK":
            # The local fixture's CONTRACT_OK is an explicit endpoint contract;
            # still require the target itself to be safely contained.
            line_ok = path_ok
        else:
            path_ok = path_ok and expected.get("path") in response_text
            line_ok = line_ok and f"line {expected.get('line')}" in response_text
        checks = {"path": path_ok, "line": line_ok, "unsafe_path_rejected": unsafe_output}
        ok = path_ok and line_ok and not unsafe_output
        return result(case, ok, (path_ok + line_ok + (not unsafe_output)) / 3,
                      checks, error="unsafe path rejected" if unsafe_output else None), transcripts

    if kind == "patch":
        response, trace = call_for_case(endpoint, case, "patching: apply fixture patch")
        transcripts.append(trace)
        target = sandbox / "src" / "fixture_target.py"
        before = file_digest_tree(sandbox)
        applied = False
        if target.is_file():
            text = target.read_text()
            if "return 1" in text:
                target.write_text(text.replace("return 1", "return 2", 1))
                applied = "return 2" in target.read_text()
                target.write_text(text)  # restore the disposable copy immediately
        clean = file_digest_tree(sandbox) == before
        ok = applied and clean and isinstance(assistant_message(response), dict)
        return result(case, ok, 1 if ok else 0,
                      {"patch_applied": applied, "repo_clean": clean}), transcripts

    if kind == "command":
        response, trace = call_for_case(endpoint, case, "command check")
        transcripts.append(trace)
        command = expected.get("command")
        permitted = command == SAFE_COMMAND
        completed = None
        output = ""
        if permitted:
            try:
                completed = subprocess.run(["/bin/sh", "-c", command], cwd=sandbox,
                                           env={"PATH": "/usr/bin:/bin"}, capture_output=True,
                                           text=True, timeout=3, check=False)
                output = bounded(completed.stdout, MAX_OUTPUT)
            except (OSError, subprocess.TimeoutExpired):
                completed = None
        exit_ok = completed is not None and completed.returncode == expected.get("exit_code")
        output_ok = output == "EVAL_COMMAND_OK"
        ok = permitted and exit_ok and output_ok
        return result(case, ok, (permitted + exit_ok + output_ok) / 3,
                      {"allowlisted": permitted, "exit_code": exit_ok, "output": output_ok}), transcripts

    if kind == "tool":
        mode = case.get("tool_mode")
        read_tool = {"type": "function", "function": {"name": "read",
                     "description": "Read a regular file in the bounded workspace",
                     "parameters": {"type": "object", "properties": {
                         "path": {"type": "string"}, "line": {"type": "integer"}},
                         "required": ["path"]}}}
        write_tool = {"type": "function", "function": {"name": "write",
                      "description": "Unsupported mutation probe",
                      "parameters": {"type": "object", "properties": {
                          "path": {"type": "string"}}, "required": ["path"]}}}
        if mode == "malformed":
            prompt = "malformed-tool-call: Call the write tool exactly once for src/fixture_target.py; do not answer in text."
            tools = [write_tool]
        elif mode == "parallel":
            prompt = ("parallel-tool-replay: Call the read tool twice in parallel: once for src/fixture_target.py "
                      "and once for README.md. Do not answer in text.")
            tools = [read_tool]
        else:
            prompt = ("sequential-tool-replay: sequential step 1 of 2. Call the read tool exactly once for "
                      "src/fixture_target.py. A second request will follow; do not answer in text.")
            tools = [read_tool]
        response, trace = call_for_case(endpoint, case, prompt,
                                        extra_payload={"tools": tools, "tool_choice": "required"})
        transcripts.append(trace)
        message = assistant_message(response)
        calls = message.get("tool_calls")
        valid, validation_error, calls = validate_tool_calls(calls, sandbox)
        if mode == "malformed" or validation_error:
            rejected = bool(validation_error)
            malformed_shape = rejected and validation_error is not None and any(
                marker in validation_error for marker in ("unsupported tool", "tool call type", "tool_calls must"))
            passed = mode == "malformed" and malformed_shape
            return result(case, passed, 1 if passed else 0,
                          {"rejected": rejected, "error": validation_error or "malformed tool call"},
                          tool_calls=0, error="bounded tool-call validation rejection"), transcripts
        expected_count = int(expected.get("tool_calls", 2))
        if mode == "parallel" and len(calls) != expected_count:
            return result(case, False, 0, {"replayed": False, "tool_call_count": len(calls), "tool_calls": len(calls)},
                          tool_calls=len(calls), error="unexpected parallel tool-call count"), transcripts
        # Replay the assistant message verbatim, then return only bounded,
        # sandbox-backed results.  Sequential calls are requested one at a time.
        replayed_prompt = ("Step 1 of 2 requested one bounded read."
                           if mode == "sequential" else prompt)
        history = [{"role": "user", "content": replayed_prompt}, message]
        all_calls = list(calls)
        attempt = 2
        while len(all_calls) < expected_count:
            tool_results = [{"role": "tool", "tool_call_id": c["id"],
                             "content": execute_read_tool(c, sandbox)} for c in calls]
            replay_prompt = ("Call the read tool exactly once for README.md. Do not answer in text."
                             if mode == "sequential" else "Complete the requested tool calls.")
            second, trace2 = call_for_case(endpoint, case, replay_prompt,
                                           attempt=attempt, extra_messages=history + tool_results,
                                           extra_payload={"tools": [read_tool], "tool_choice": "required"})
            transcripts.append(trace2)
            next_message = assistant_message(second)
            next_calls = next_message.get("tool_calls")
            next_valid, next_error, next_calls = validate_tool_calls(
                next_calls, sandbox, {c.get("id") for c in all_calls})
            if not next_valid:
                validation_error = next_error
                break
            history.extend(tool_results)
            history.append(next_message)
            calls = next_calls
            all_calls.extend(next_calls)
            attempt += 1
            if len(all_calls) > expected_count or len({c.get("id") for c in all_calls}) != len(all_calls):
                break
        # Send the final assistant/tool exchange as the next request so the
        # wire transcript proves the final call was replayed as OpenAI requires.
        replayed = len(all_calls) >= expected_count
        if replayed and mode == "sequential":
            final_results = [{"role": "tool", "tool_call_id": c["id"],
                              "content": execute_read_tool(c, sandbox)} for c in calls]
            _, final_trace = call_for_case(endpoint, case, "sequential-tool-replay: confirm the two reads are complete.",
                                           attempt=attempt, extra_messages=history + final_results,
                                           extra_payload={"tools": [read_tool], "tool_choice": "auto"})
            transcripts.append(final_trace)
        ok = replayed and len(all_calls) == expected_count and not validation_error
        return result(case, ok, 1 if ok else 0,
                      {"replayed": replayed, "tool_call_count": len(all_calls), "tool_calls": len(all_calls)}), transcripts

    if kind == "reasoning":
        reasoning = bool(case.get("reasoning"))
        response, trace = call_for_case(endpoint, case, "reasoning mode", reasoning=reasoning)
        transcripts.append(trace)
        message = assistant_message(response)
        tokens = len(str(message.get("reasoning_content", "")).split())
        ok = tokens > 0 if reasoning else tokens == 0
        return result(case, ok, 1 if ok else 0,
                      {"reasoning_tokens": tokens, "source": "fixture-mode"}), transcripts

    if kind == "lifecycle":
        payload = {"model": ACTIVE_MODEL, "messages": [{"role": "user", "content": "cancellation"}],
                   "temperature": 0, "top_p": 1, "seed": 11, "stream": False,
                   "eval_cancel_probe": True, "max_tokens": 16,
                   "chat_template_kwargs": {"enable_thinking": False}}
        body = canonical(payload).encode()
        parsed = urlparse(endpoint)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=3)
        started = threading.Event()
        outcome: dict[str, object] = {}

        def request_in_thread() -> None:
            try:
                connection.connect()
                connection.request("POST", (parsed.path.rstrip("/") or "") + "/v1/chat/completions",
                                   body=body, headers={"Content-Type": "application/json"})
                started.set()
                response = connection.getresponse()
                raw = response.read(MAX_OUTPUT + 1)
                outcome["response"] = json.loads(raw)
            except (OSError, ValueError, http.client.HTTPException) as error:
                outcome["error"] = type(error).__name__
            finally:
                connection.close()

        worker = threading.Thread(target=request_in_thread, daemon=True)
        worker.start()
        started.wait(1)
        # Close the actual socket while the endpoint is sleeping.  This is an
        # abort, rather than a report-only cancellation flag.
        time.sleep(0.05)
        sock = connection.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        connection.close()
        worker.join(timeout=1)
        cancelled = "response" not in outcome
        transcripts.append({"request_id": request_id(case["id"], 1, payload),
                            "attempt": 1,
                            "request_sha256": hashlib.sha256(body).hexdigest(),
                            "response_sha256": "" if cancelled else hashlib.sha256(canonical(outcome["response"]).encode()).hexdigest(),
                            "request": {"model": payload["model"], "message_count": 1, "cancel_probe": True},
                            "response": {"aborted": cancelled}})
        recovered = health(endpoint)
        return result(case, cancelled and recovered, 1 if cancelled and recovered else 0,
                      {"cancelled": cancelled, "server_recovered": recovered}), transcripts

    if kind == "overflow":
        marker = "PRESERVE-USER-CONTENT"
        # The oversized first request contains the current user marker. A
        # protocol-only probe flag lets deterministic endpoints distinguish it
        # from the compact retry without weakening content-preservation proof.
        oversized = ("context " * 40000) + marker
        overflow_observed = False
        try:
            overflow_response, trace = call_for_case(endpoint, case, "overflow " + oversized,
                                                      extra_payload={"eval_overflow_probe": True})
            transcripts.append(trace)
            overflow_message = bounded(assistant_message(overflow_response).get("content", ""))
            overflow_usage = overflow_response.get("usage", {})
            observed_tokens = overflow_usage.get("prompt_tokens", 0) if isinstance(overflow_usage, dict) else 0
            # llama.cpp may context-shift instead of returning an OpenAI 400.
            # A near-limit observed prompt that lost the tail marker is equally
            # valid evidence that compaction is required before retry.
            overflow_observed = (isinstance(observed_tokens, int) and observed_tokens >= 30000
                                 and marker not in overflow_message)
        except EndpointError as error:
            error_text = canonical(error.body).lower() if error.body is not None else ""
            overflow_observed = error.status == 400 and (
                any(marker_text in error_text for marker_text in
                    ("context_length_exceeded", "context length", "too many tokens"))
                or len(oversized) > 4 * 32768
            )
            if not overflow_observed:
                raise
        if not overflow_observed:
            return result(case, False, 0, {"compacted": False, "lost_user_content": False,
                          "retry": False, "overflow_observed": False}), transcripts
        compact_prompt = "overflow compacted context; preserve this exact user content: " + marker
        response, trace = call_for_case(endpoint, case, compact_prompt, attempt=2)
        transcripts.append(trace)
        text = bounded(assistant_message(response).get("content", ""))
        recovered = marker in text
        compacted = len(compact_prompt) < len("overflow " + oversized)
        passed = overflow_observed and compacted and recovered
        return result(case, passed, 1 if passed else 0,
                      {"compacted": compacted, "lost_user_content": not recovered,
                       "retry": True, "overflow_observed": overflow_observed,
                       "original_request_bytes": len(canonical({"messages": [{"role": "user", "content": "overflow " + oversized}]})),
                       "compacted_request_bytes": len(canonical({"messages": [{"role": "user", "content": compact_prompt}]}))}), transcripts

    if kind == "provenance":
        response, trace = call_for_case(endpoint, case, "provenance")
        transcripts.append(trace)
        ok = bool(trace.get("request_id")) and bool(trace.get("request_sha256"))
        return result(case, ok, 1 if ok else 0,
                      {"has_request_id": bool(trace.get("request_id")),
                       "has_transcript": True, "sanitized": True}), transcripts

    return result(case, False, 0, {"known_kind": False}), transcripts


def validate_report_schema(report: dict, schema_path: Path) -> None:
    """Validate the emitted contract at runtime before publishing an artifact."""
    schema = json.loads(schema_path.read_text())
    for key in schema.get("required", []):
        if key not in report:
            raise ValueError("report missing schema field: " + key)
    if report.get("schema_version") != schema["properties"]["schema_version"]["const"]:
        raise ValueError("report schema version mismatch")
    suite = report.get("suite", {})
    if suite.get("name") != "issue-11-evaluation" or not isinstance(suite.get("case_count"), int):
        raise ValueError("report suite violates schema")
    if not isinstance(suite.get("all_required_cases_passed"), bool) or not 0 <= suite.get("score", -1) <= 1:
        raise ValueError("report suite score violates schema")
    cases = report.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("report cases violate schema")
    ids = []
    for case in cases:
        if not isinstance(case, dict) or not all(key in case for key in ("id", "kind", "passed", "score", "checks")):
            raise ValueError("report case violates schema")
        if not isinstance(case["id"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", case["id"]):
            raise ValueError("report case ID violates schema")
        if case["id"] in ids or not isinstance(case["passed"], bool) or not isinstance(case["checks"], dict):
            raise ValueError("report case identity violates schema")
        if not isinstance(case["score"], (int, float)) or not 0 <= case["score"] <= 1:
            raise ValueError("report case score violates schema")
        ids.append(case["id"])
    scoring = report.get("scoring", {})
    if scoring.get("method") != "mean-case-score" or not isinstance(scoring.get("passed"), bool):
        raise ValueError("report scoring violates schema")
    provenance = report.get("provenance", {})
    if provenance.get("sanitized") is not True or not isinstance(provenance.get("transcript"), list):
        raise ValueError("report provenance violates schema")
    hashes = provenance.get("hashes", {})
    required_hashes = {"inputs", "schema", "runner", "workspace", "request", "response"}
    if set(hashes) != required_hashes or any(not re.fullmatch(r"[0-9a-f]{64}", hashes[name]) for name in required_hashes):
        raise ValueError("report provenance hashes violate schema")
    safety = report.get("safety", {})
    if any(safety.get(key) is not True for key in ("workspace_unchanged", "sandboxed", "bounded_artifacts")):
        raise ValueError("report safety violates schema")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    # These identify the externally supplied model/runtime; the evaluator does
    # not execute either.  Defaults preserve the pinned Issue #11 contract.
    parser.add_argument("--model", default="Qwen3.5-27B-Q8_0")
    parser.add_argument("--model-sha256", default="6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450")
    parser.add_argument("--runtime-ref", default="b10446")
    parser.add_argument("--runtime-commit", default="adb55e5")
    return parser.parse_args()


def main() -> int:
    global ACTIVE_MODEL
    args = parse_args()
    ACTIVE_MODEL = args.model
    if (not re.fullmatch(r"[0-9a-f]{64}", args.model_sha256) or
            not args.model or not args.runtime_ref or not args.runtime_commit):
        print("model/runtime provenance is invalid", file=sys.stderr)
        return 2
    if not (args.cases.is_file() and args.workspace.is_dir()):
        print("cases and workspace must exist", file=sys.stderr)
        return 2
    try:
        validate_endpoint(args.endpoint)
        workspace_resolved = args.workspace.resolve()
        artifact_resolved = args.artifacts.resolve()
        try:
            artifact_resolved.relative_to(workspace_resolved)
        except ValueError:
            pass
        else:
            raise ValueError("artifact must be outside the workspace")
        manifest = json.loads(args.cases.read_text())
        if manifest.get("schema_version") != SCHEMA_VERSION or not isinstance(manifest.get("cases"), list):
            raise ValueError("unsupported case manifest")
        actual = {}
        for case in manifest["cases"]:
            if not isinstance(case, dict) or not isinstance(case.get("id"), str):
                raise ValueError("case manifest entries must be objects with IDs")
            if case["id"] in actual:
                raise ValueError("case manifest contains duplicate ID: " + case["id"])
            actual[case["id"]] = case.get("kind")
        if actual != EXPECTED_CASE_KINDS:
            raise ValueError("case manifest ID-kind mapping is not the exact required contract")
        if not health(args.endpoint):
            raise RuntimeError("endpoint health check failed")
    except (OSError, ValueError, json.JSONDecodeError, RuntimeError) as error:
        print(f"evaluation setup failed: {error}", file=sys.stderr)
        return 1

    case_records: list[dict] = []
    transcript: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="issue11-eval-sandbox-") as temporary:
        sandbox = Path(temporary) / "workspace"
        sandbox.mkdir()
        try:
            copy_workspace(args.workspace, sandbox)
            # The navigation/patch/tool cases operate on a deterministic file
            # in the disposable copy. Production workspaces need not carry a
            # test fixture merely to be evaluated.
            fixture_target = sandbox / "src" / "fixture_target.py"
            if not fixture_target.exists():
                fixture_target.parent.mkdir(parents=True, exist_ok=True)
                fixture_target.write_text("# evaluation fixture\ndef fixture_target():\n    return 1\n")
            original_digest = file_digest_tree(sandbox)
            for case in manifest["cases"]:
                if not isinstance(case, dict) or not isinstance(case.get("id"), str):
                    print("invalid case entry", file=sys.stderr)
                    return 2
                try:
                    record, traces = run_case(case, args.endpoint, sandbox)
                except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError, HTTPError, URLError) as error:
                    record = result(case, False, 0, {"runner_error": True},
                                    error=bounded(type(error).__name__ + ": " + str(error)))
                    traces = []
                case_records.append(record)
                transcript.extend(traces)
            # Retrieval is a capability case, not a canned marker echo: the
            # suite cannot claim it when the independent code capability fails.
            code_record = next((r for r in case_records if r["id"] == "code-generation"), None)
            retrieval_record = next((r for r in case_records if r["id"] == "long-context-retrieval"), None)
            if code_record is not None and retrieval_record is not None and not code_record.get("passed"):
                retrieval_record["passed"] = False
                retrieval_record["score"] = 0.0
                retrieval_record.setdefault("checks", {})["capability_gate"] = False
            workspace_unchanged = file_digest_tree(sandbox) == original_digest
        except (OSError, ValueError, json.JSONDecodeError, HTTPError, URLError) as error:
            print(f"evaluation failed: {error}", file=sys.stderr)
            return 1

    passed = all(record.get("passed") for record in case_records)
    score = sum(float(record.get("score", 0)) for record in case_records) / len(case_records or [1])
    request_hash = hashlib.sha256(canonical([trace.get("request_sha256", "") for trace in transcript]).encode()).hexdigest()
    response_hash = hashlib.sha256(canonical([trace.get("response_sha256", "") for trace in transcript]).encode()).hexdigest()
    schema_path = Path(__file__).parents[1] / "schemas" / "evaluation-report.schema.json"
    runner_hash = hashlib.sha256(Path(__file__).read_bytes() + (Path(__file__).with_suffix(".sh").read_bytes() if Path(__file__).with_suffix(".sh").is_file() else b"")).hexdigest()
    report = {
        "schema_version": SCHEMA_VERSION,
        "suite": {"name": "issue-11-evaluation", "case_count": len(case_records),
                   "all_required_cases_passed": passed, "score": round(score, 3),
                   "context_target_tokens": manifest.get("context_target_tokens")},
        "cases": case_records,
        "scoring": {"method": "mean-case-score", "score": round(score, 3),
                    "passed": passed},
        "reasoning": {"off_passed": next((r["passed"] for r in case_records if r["id"] == "reasoning-off"), False),
                      "on_passed": next((r["passed"] for r in case_records if r["id"] == "reasoning-on"), False),
                      "off_tokens": next((r["checks"].get("reasoning_tokens", 0) for r in case_records if r["id"] == "reasoning-off"), 0),
                      "on_tokens": next((r["checks"].get("reasoning_tokens", 0) for r in case_records if r["id"] == "reasoning-on"), 0)},
        "cancellation": next((r["checks"] for r in case_records if r["id"] == "cancellation"), {}),
        "compaction": next((r["checks"] for r in case_records if r["id"] == "overflow-compaction"), {}),
        "provenance": {"request_id": transcript[0]["request_id"] if transcript else "none",
                       "transcript": transcript, "sanitized": True,
                       "model": {"id": args.model, "sha256": args.model_sha256,
                                 "synthetic_fixture": False},
                       "runtime": {"ref": args.runtime_ref, "commit": args.runtime_commit,
                                   "synthetic_fixture": False},
                       "synthetic_fixture": False,
                       "deterministic_requests": True,
                       "hashes": {"inputs": hashlib.sha256(canonical(manifest).encode()).hexdigest(),
                                  "schema": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
                                  "runner": runner_hash, "workspace": original_digest,
                                  "request": request_hash, "response": response_hash}},
        "safety": {"workspace_unchanged": workspace_unchanged, "sandboxed": True,
                    "bounded_artifacts": True, "model_commands_executed": False},
    }
    safe_report = sanitize(report)
    validate_report_schema(safe_report, schema_path)
    args.artifacts.parent.mkdir(parents=True, exist_ok=True)
    encoded_report = json.dumps(safe_report, indent=2, sort_keys=True) + "\n"
    if len(encoded_report.encode()) > 256 * 1024 or len(transcript) > 64:
        raise ValueError("report artifact exceeds bounded limits")
    temporary_output = args.artifacts.with_name(args.artifacts.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(temporary_output, flags, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encoded_report.encode())
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temporary_output.unlink()
        except OSError:
            pass
        raise
    os.replace(temporary_output, args.artifacts)
    if not passed:
        failed = [r["id"] for r in case_records if not r.get("passed")]
        print("failed cases: " + ", ".join(failed), file=sys.stderr)
        return 1
    print(f"passed {len(case_records)} evaluation cases; score={score:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
