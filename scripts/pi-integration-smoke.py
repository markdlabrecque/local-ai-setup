#!/usr/bin/env python3
"""Opt-in real Pi/Qwen streaming, reasoning, and tool-replay validation."""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODEL = "Qwen3.5-27B-Q8_0"
PROVIDER = "local-qwen"
MAX_CAPTURE = 8 * 1024 * 1024


class SmokeError(RuntimeError):
    pass


def timeout_value(raw: str) -> float:
    value = float(raw)
    if not math.isfinite(value) or not 10 <= value <= 600:
        raise argparse.ArgumentTypeError("timeout must be between 10 and 600 seconds")
    return value


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="required acknowledgement")
    parser.add_argument("--config", type=pathlib.Path, default=ROOT / "config/pi-models.example.json")
    parser.add_argument("--pi", default="pi")
    parser.add_argument("--timeout", type=timeout_value, default=300.0)
    parser.add_argument("--result", type=pathlib.Path)
    args = parser.parse_args()
    if not args.real:
        parser.error("--real is required")
    return args


def run(command: list[str], cwd: pathlib.Path, env: dict[str, str], timeout: float):
    try:
        completed = subprocess.run(command, cwd=cwd, env=env, text=True,
                                   capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise SmokeError(f"Pi invocation exceeded {timeout:g}s") from error
    if len(completed.stdout.encode()) > MAX_CAPTURE or len(completed.stderr.encode()) > MAX_CAPTURE:
        raise SmokeError("Pi output exceeded the 8 MiB capture bound")
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()[-1500:]
        raise SmokeError(f"Pi exited {completed.returncode}: {detail}")
    return completed


def events(output: str) -> list[dict]:
    try:
        rows = [json.loads(line) for line in output.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise SmokeError(f"Pi emitted invalid JSONL: {error}") from error
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise SmokeError("Pi emitted no usable events")
    return rows


def assistant_messages(rows: list[dict]) -> list[dict]:
    return [row["message"] for row in rows if row.get("type") == "message_end" and
            isinstance(row.get("message"), dict) and row["message"].get("role") == "assistant"]


def content_blocks(message: dict, kind: str) -> list[dict]:
    return [block for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == kind]


def invoke(pi: str, agent_dir: pathlib.Path, workspace: pathlib.Path,
           timeout: float, thinking: str, prompt: str, tools: str):
    command = [pi, "--provider", PROVIDER, "--model", MODEL,
               "--thinking", thinking, "--mode", "json", "--no-session",
               "--no-context-files", "--no-skills", "--no-extensions",
               "--no-prompt-templates"]
    command += ["--tools", tools] if tools else ["--no-tools"]
    command += ["-p", prompt]
    env = os.environ.copy()
    env.update({"PI_CODING_AGENT_DIR": str(agent_dir),
                "PI_SKIP_VERSION_CHECK": "1"})
    return events(run(command, workspace, env, timeout).stdout)


def main() -> int:
    args = parse_args()
    checks: list[dict] = []
    started = time.monotonic()
    pi_version = "unknown"

    def checked(name, operation):
        before = time.monotonic()
        operation()
        checks.append({"name": name, "passed": True,
                       "duration_ms": round((time.monotonic() - before) * 1000, 3)})
        print(f"ok: {name}", file=sys.stderr)

    try:
        pi_version = run([args.pi, "--version"], ROOT, os.environ.copy(), 30).stdout.strip()
        if not pi_version:
            raise SmokeError("Pi version probe returned no version")
        config = json.loads(args.config.read_text())
        model = config["providers"][PROVIDER]["models"][0]
        if (model.get("id") != MODEL or model.get("contextWindow") != 32768 or
                model.get("maxTokens") != 4096 or model.get("reasoning") is not True or
                model.get("compat", {}).get("thinkingFormat") != "qwen-chat-template"):
            raise SmokeError("Pi model metadata is not the reviewed Qwen contract")
        with tempfile.TemporaryDirectory(prefix="local-ai-pi-smoke-") as temporary:
            root = pathlib.Path(temporary)
            agent_dir, workspace = root / "agent", root / "workspace"
            agent_dir.mkdir(mode=0o700)
            workspace.mkdir()
            shutil.copyfile(args.config, agent_dir / "models.json")
            (workspace / "alpha.txt").write_text("ALPHA_OK\n")
            (workspace / "beta.txt").write_text("BETA_OK\n")
            env = os.environ.copy()
            env.update({"PI_CODING_AGENT_DIR": str(agent_dir), "PI_SKIP_VERSION_CHECK": "1"})

            def discovery():
                listing = run([args.pi, "--list-models", MODEL], workspace, env, args.timeout).stdout
                required = (PROVIDER, MODEL, "32.8K", "4.1K", "yes")
                if not all(value in listing for value in required):
                    raise SmokeError("Pi model discovery omitted reviewed metadata")
            checked("model-discovery-and-metadata", discovery)

            def text_off():
                rows = invoke(args.pi, agent_dir, workspace, args.timeout, "off",
                              "Reply with exactly PI_TEXT_OK", "")
                messages = assistant_messages(rows)
                if not messages or "PI_TEXT_OK" not in "".join(b.get("text", "") for b in content_blocks(messages[-1], "text")):
                    raise SmokeError("thinking-off response omitted PI_TEXT_OK")
                if any(content_blocks(message, "thinking") for message in messages):
                    raise SmokeError("thinking-off response exposed a thinking block")
                if not any(row.get("type") == "message_update" and
                           row.get("assistantMessageEvent", {}).get("type") == "text_delta" for row in rows):
                    raise SmokeError("Pi did not expose streamed text deltas")
                usage = messages[-1].get("usage", {})
                if not all(isinstance(usage.get(key), int) and usage[key] > 0 for key in ("input", "output", "totalTokens")):
                    raise SmokeError("streaming usage was not populated")
            checked("text-streaming-thinking-off", text_off)

            def thinking_on():
                rows = invoke(args.pi, agent_dir, workspace, args.timeout, "high",
                              "Compute 17 times 19, then reply with exactly RESULT=323.", "")
                messages = assistant_messages(rows)
                if not messages or not content_blocks(messages[-1], "thinking"):
                    raise SmokeError("thinking-high response omitted its thinking block")
                text = "".join(b.get("text", "") for b in content_blocks(messages[-1], "text"))
                if "RESULT=323" not in text:
                    raise SmokeError("thinking-high response omitted RESULT=323")
            checked("qwen-thinking-high", thinking_on)

            def sequential():
                rows = invoke(args.pi, agent_dir, workspace, args.timeout, "off",
                              "Use read for alpha.txt. After that result returns, use read for beta.txt in a separate second tool-call turn. Then reply exactly SEQUENTIAL_PI_OK. Do not call both together.", "read")
                messages = assistant_messages(rows)
                tool_turns = [content_blocks(message, "toolCall") for message in messages
                              if content_blocks(message, "toolCall")]
                paths = [[call.get("arguments", {}).get("path") for call in turn] for turn in tool_turns]
                if paths[:2] != [["alpha.txt"], ["beta.txt"]]:
                    raise SmokeError(f"sequential tool replay was not ordered: {paths!r}")
                final = "".join(b.get("text", "") for b in content_blocks(messages[-1], "text"))
                if "SEQUENTIAL_PI_OK" not in final:
                    raise SmokeError("sequential tool replay omitted completion marker")
            checked("sequential-tool-result-replay", sequential)

            def parallel():
                rows = invoke(args.pi, agent_dir, workspace, args.timeout, "off",
                              "Call read for alpha.txt and beta.txt in parallel in the same assistant turn. After both results, reply exactly PARALLEL_PI_OK.", "read")
                messages = assistant_messages(rows)
                calls = content_blocks(messages[0], "toolCall") if messages else []
                if {call.get("arguments", {}).get("path") for call in calls} != {"alpha.txt", "beta.txt"} or len(calls) != 2:
                    raise SmokeError("parallel tool calls were not emitted in one turn")
                sequence = [row.get("type") for row in rows if row.get("type", "").startswith("tool_execution_")]
                if sequence[:2] != ["tool_execution_start", "tool_execution_start"]:
                    raise SmokeError("Pi did not begin both parallel calls before returning results")
                final = "".join(b.get("text", "") for b in content_blocks(messages[-1], "text"))
                if "PARALLEL_PI_OK" not in final:
                    raise SmokeError("parallel tool replay omitted completion marker")
            checked("parallel-tool-result-replay", parallel)
    except (SmokeError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        checks.append({"name": "failure", "passed": False, "error": str(error)[:1000]})
        print(f"error: {error}", file=sys.stderr)
        code = 1
    else:
        code = 0
    result = {"schema_version": 1, "suite": "issue-10-pi-integration-smoke",
              "status": "pass" if code == 0 else "fail",
              "duration_ms": round((time.monotonic() - started) * 1000, 3),
              "pi_version": pi_version, "provider": PROVIDER, "model": MODEL,
              "context_tokens": 32768, "max_output_tokens": 4096,
              "checks": checks}
    encoded = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(encoded)
    print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
