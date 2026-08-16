"""Adversarial red contracts for the Issue #11 evaluation review.

The fixture is deliberately deterministic and bounded.  Every test gives the
runner a disposable workspace and keeps the endpoint outside the repository;
the tests are contracts, not an implementation of the evaluator.
"""
import copy
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).parents[1]
CASES = ROOT / "tests" / "fixtures" / "evaluation_cases.json"
SCHEMA = ROOT / "schemas" / "evaluation-report.schema.json"
RUNNER = Path(os.environ.get("EVALUATION_RUNNER", ROOT / "scripts" / "run-evaluation.sh"))

EXPECTED_KINDS = {
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


class ReviewFixture(BaseHTTPRequestHandler):
    """Small OpenAI-shaped endpoint with intentionally adversarial answers."""

    protocol_version = "HTTP/1.1"
    mode = "normal"
    requests = []
    cancelled_write = False
    delay = 0.0

    def log_message(self, *_args):
        pass

    def _reply(self, value, status=200):
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            type(self).cancelled_write = True

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._reply({"status": "ok", "model": "issue11-review-fixture"})
        elif self.path == "/v1/models":
            self._reply({"object": "list", "data": [{"id": "issue11-review-fixture"}]})
        else:
            self._reply({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._reply({"error": "not found"}, 404)
            return
        size = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(size) or b"{}")
        type(self).requests.append(copy.deepcopy(request))
        messages = request.get("messages", [])
        marker = json.dumps(messages, sort_keys=True)
        mode = type(self).mode

        if mode == "cancel" and request.get("eval_cancel_probe"):
            type(self).delay = 0.40
            time.sleep(type(self).delay)
            self._reply(self._completion("late"))
            return

        if mode == "overflow" and request.get("eval_overflow_probe"):
            if "PRESERVE-USER-CONTENT" in marker and len(marker) > 100000:
                self._reply({"error": {"type": "context_length_exceeded"}}, 400)
                return

        if mode == "canned":
            content = "CONTRACT_OK"
            response = self._completion(content, usage=self._usage(marker))
        elif mode == "tools":
            tool_results = {
                m.get("tool_call_id") for m in messages
                if isinstance(m, dict) and m.get("role") == "tool"
            }
            if "seq-1" not in tool_results:
                response = self._tool_completion("seq-1", 1)
            elif "seq-2" not in tool_results:
                response = self._tool_completion("seq-2", 3)
            else:
                response = self._completion("SEQUENTIAL_OK")
        elif mode == "nested":
            nested = "{}"
            for _ in range(96):
                nested = json.dumps({"next": json.loads(nested)})
            response = self._tool_completion("nested", 1, nested)
        elif mode == "navigation":
            response = self._completion("/etc/passwd ../outside")
        elif mode == "redaction":
            response = self._completion(json.dumps({
                "api_key": "VALUE_API_KEY",
                "password": "VALUE_PASSWORD",
                "client_secret": "VALUE_CLIENT_SECRET",
                "authorization": "Bearer VALUE_BEARER",
                "basic_auth": "Basic VALUE_BASIC",
                "token": "sk-VALUE_SK_TOKEN",
            }))
        elif "NEEDLE-31K-7F3A" in marker:
            response = self._completion("NEEDLE-31K-7F3A", usage={
                "prompt_tokens": 32000, "completion_tokens": 1, "total_tokens": 32001,
            })
        elif "PRESERVE-USER-CONTENT" in marker:
            response = self._completion("PRESERVE-USER-CONTENT")
        elif "code-generation" in marker:
            response = self._completion("def add(a, b):\n    return a + b")
        else:
            response = self._completion("CONTRACT_OK")
        self._reply(response)

    @staticmethod
    def _usage(marker):
        if "NEEDLE-31K-7F3A" in marker:
            return {"prompt_tokens": 32000, "completion_tokens": 1, "total_tokens": 32001}
        return {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}

    @staticmethod
    def _completion(content, usage=None):
        return {
            "id": "review-fixture-response",
            "object": "chat.completion",
            "model": "issue11-review-fixture",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": usage or {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
        }

    @staticmethod
    def _tool_completion(call_id, line, arguments=None):
        arguments = arguments or json.dumps({"path": "src/fixture_target.py", "line": line})
        return {
            "id": "review-fixture-response",
            "choices": [{"index": 0, "message": {"role": "assistant", "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": "read", "arguments": arguments},
            }]}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
        }


class Endpoint:
    def __init__(self, mode):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ReviewFixture)
        ReviewFixture.mode = mode
        ReviewFixture.requests = []
        ReviewFixture.cancelled_write = False
        ReviewFixture.delay = 0.0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def requests(self):
        return ReviewFixture.requests

    @property
    def cancelled_write(self):
        return ReviewFixture.cancelled_write

    @property
    def delay(self):
        return ReviewFixture.delay

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class Issue11AdversarialReview(unittest.TestCase):
    maxDiff = None

    def workspace(self, parent):
        root = Path(parent) / "workspace"
        (root / "src").mkdir(parents=True)
        (root / "src" / "fixture_target.py").write_text("one\ntwo\nthree\n")
        (root / ".git").mkdir()
        return root

    def run_evaluator(self, endpoint, workspace, artifact, cases=CASES):
        before = {p.relative_to(workspace).as_posix(): p.read_bytes()
                  for p in workspace.rglob("*") if p.is_file()}
        result = subprocess.run(
            [str(RUNNER), "--endpoint", endpoint, "--cases", str(cases),
             "--workspace", str(workspace), "--artifacts", str(artifact)],
            cwd=workspace, text=True, capture_output=True, timeout=15,
        )
        after = {p.relative_to(workspace).as_posix(): p.read_bytes()
                 for p in workspace.rglob("*") if p.is_file()}
        self.assertEqual(after, before, "evaluator mutated the supplied workspace")
        return result

    def report(self, artifact, result):
        self.assertTrue(artifact.is_file(), result.stderr or result.stdout)
        return json.loads(artifact.read_text())

    @staticmethod
    def case(report, case_id):
        return next(case for case in report["cases"] if case["id"] == case_id)

    def assert_report_schema(self, report):
        """Validate the externally committed schema's required contract."""
        schema = json.loads(SCHEMA.read_text())
        self.assertEqual(schema["properties"]["schema_version"]["const"], 1)
        self.assertTrue(set(schema["required"]) <= set(report))
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["suite"]["name"], "issue-11-evaluation")
        self.assertIsInstance(report["suite"]["case_count"], int)
        self.assertIsInstance(report["suite"]["all_required_cases_passed"], bool)
        self.assertTrue(0 <= report["suite"]["score"] <= 1)
        self.assertIsInstance(report["cases"], list)
        ids = []
        for case in report["cases"]:
            ids.append(case["id"])
            self.assertRegex(case["id"], r"^[a-z0-9][a-z0-9-]*$")
            self.assertIsInstance(case["passed"], bool)
            self.assertTrue(0 <= case["score"] <= 1)
            self.assertIsInstance(case["checks"], dict)
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(report["scoring"]["method"], "mean-case-score")
        self.assertIn(report["scoring"]["passed"], (True, False))
        self.assertTrue(report["provenance"]["sanitized"])
        self.assertLessEqual(len(report["provenance"]["transcript"]), 64)
        for key in ("workspace_unchanged", "sandboxed", "bounded_artifacts"):
            self.assertTrue(report["safety"][key])

    def test_canned_contract_ok_does_not_pass_capability_cases(self):
        with tempfile.TemporaryDirectory(prefix="issue11-review-") as tmp, Endpoint("canned") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            result = self.run_evaluator(endpoint.url, workspace, artifact)
            report = self.report(artifact, result)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.case(report, "code-generation")["passed"])
        self.assertFalse(self.case(report, "long-context-retrieval")["passed"])

    def test_usage_is_measured_near_32k_and_overflow_really_compacts(self):
        with tempfile.TemporaryDirectory(prefix="issue11-review-") as tmp, Endpoint("normal") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            result = self.run_evaluator(endpoint.url, workspace, artifact)
            report = self.report(artifact, result)
        requests = [r for r in endpoint.requests if "NEEDLE-31K-7F3A" in json.dumps(r)]
        self.assertTrue(requests)
        serialized = len(json.dumps(requests[0]["messages"]))
        # The wire payload is deliberately bounded but large enough to prove
        # that the evaluator sent a real near-context request; character
        # length is not itself a tokenizer, so do not treat chars/4 as usage.
        self.assertGreaterEqual(serialized, 120000)
        self.assertLess(serialized, 200000)
        checks = self.case(report, "long-context-retrieval")["checks"]
        usage = checks.get("usage", checks.get("measured_usage", {}))
        self.assertGreaterEqual(usage.get("prompt_tokens", 0), 30000)
        self.assertLessEqual(usage.get("prompt_tokens", 0), 32768)

        with tempfile.TemporaryDirectory(prefix="issue11-review-overflow-") as tmp, Endpoint("overflow") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            result = self.run_evaluator(endpoint.url, workspace, artifact)
            report = self.report(artifact, result)
        oversized = [r for r in endpoint.requests if len(json.dumps(r.get("messages", []))) > 100000]
        compacted = [r for r in endpoint.requests if "PRESERVE-USER-CONTENT" in json.dumps(r)]
        self.assertTrue(oversized)
        self.assertTrue(compacted)
        self.assertLess(len(json.dumps(compacted[-1]["messages"])),
                        len(json.dumps(oversized[0]["messages"])))
        checks = self.case(report, "overflow-compaction")["checks"]
        self.assertTrue(checks.get("compacted"))
        self.assertFalse(checks.get("lost_user_content", True))

    def test_cancellation_aborts_delayed_request(self):
        with tempfile.TemporaryDirectory(prefix="issue11-review-") as tmp, Endpoint("cancel") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            started = time.monotonic()
            result = self.run_evaluator(endpoint.url, workspace, artifact)
            elapsed = time.monotonic() - started
            report = self.report(artifact, result)
        self.assertLess(elapsed, 8)
        self.assertGreaterEqual(endpoint.delay, 0.35)
        self.assertTrue(endpoint.cancelled_write, "delayed request completed instead of being aborted")
        self.assertTrue(self.case(report, "cancellation")["checks"].get("cancelled"))

    def test_openai_tool_replay_has_strict_sequential_calls(self):
        with tempfile.TemporaryDirectory(prefix="issue11-review-") as tmp, Endpoint("tools") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            result = self.run_evaluator(endpoint.url, workspace, artifact)
            report = self.report(artifact, result)
        sequential = [r for r in endpoint.requests if "sequential-tool-replay" in json.dumps(r)]
        calls = [m_call for request in sequential for message in request.get("messages", [])
                 if message.get("role") == "assistant" for m_call in message.get("tool_calls", [])]
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual([call["id"] for call in calls[:2]], ["seq-1", "seq-2"])
        self.assertEqual(len({call["id"] for call in calls}), len(calls))
        self.assertTrue(all(call["type"] == "function" for call in calls))
        self.assertTrue(all(call["function"]["name"] == "read" for call in calls))
        for call in calls:
            arguments = json.loads(call["function"]["arguments"])
            self.assertIsInstance(arguments, dict)
            self.assertEqual(arguments["path"], "src/fixture_target.py")
        tool_results = [m for request in sequential for m in request.get("messages", [])
                        if m.get("role") == "tool"]
        self.assertIn("seq-1", {m.get("tool_call_id") for m in tool_results})
        second_request = next(request for request in sequential
                              if any(m.get("role") == "tool" for m in request.get("messages", [])))
        self.assertTrue(any(m.get("role") == "assistant" and
                            any(c.get("id") == "seq-2" for c in m.get("tool_calls", []))
                            for m in second_request["messages"]))
        self.assertGreaterEqual(self.case(report, "sequential-tool-replay")["checks"].get("tool_calls", 0), 2)

    def test_manifest_is_exact_unique_and_reports_validate_against_schema(self):
        manifest = json.loads(CASES.read_text())
        self.assertEqual({case["id"]: case["kind"] for case in manifest["cases"]}, EXPECTED_KINDS)
        self.assertEqual(len(manifest["cases"]), len({case["id"] for case in manifest["cases"]}))
        with tempfile.TemporaryDirectory(prefix="issue11-review-") as tmp, Endpoint("normal") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            result = self.run_evaluator(endpoint.url, workspace, artifact)
            report = self.report(artifact, result)
        self.assert_report_schema(report)

    def test_runner_rejects_duplicate_and_wrong_kind_manifests_before_model(self):
        manifest = json.loads(CASES.read_text())
        for bad_manifest in (self._duplicate(manifest), self._wrong_kind(manifest)):
            with tempfile.TemporaryDirectory(prefix="issue11-review-manifest-") as tmp, Endpoint("normal") as endpoint:
                cases = Path(tmp) / "bad-cases.json"
                cases.write_text(json.dumps(bad_manifest))
                workspace = self.workspace(tmp)
                result = self.run_evaluator(endpoint.url, workspace, Path(tmp) / "report.json", cases)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(endpoint.requests, "invalid manifest reached the model")

    @staticmethod
    def _duplicate(manifest):
        bad = copy.deepcopy(manifest)
        bad["cases"].append(copy.deepcopy(bad["cases"][0]))
        return bad

    @staticmethod
    def _wrong_kind(manifest):
        bad = copy.deepcopy(manifest)
        bad["cases"][0]["kind"] = "code"
        return bad

    def test_navigation_rejects_absolute_and_parent_paths(self):
        with tempfile.TemporaryDirectory(prefix="issue11-review-") as tmp, Endpoint("navigation") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            result = self.run_evaluator(endpoint.url, workspace, artifact)
            report = self.report(artifact, result)
        navigation = self.case(report, "repository-navigation")
        self.assertFalse(navigation["passed"])

    def test_sensitive_keys_and_bearer_basic_sk_forms_never_leave_artifact(self):
        with tempfile.TemporaryDirectory(prefix="issue11-review-") as tmp, Endpoint("redaction") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            result = self.run_evaluator(endpoint.url, workspace, artifact)
            self.report(artifact, result)
            text = artifact.read_text()
        for value in ("VALUE_API_KEY", "VALUE_PASSWORD", "VALUE_CLIENT_SECRET",
                      "VALUE_BEARER", "VALUE_BASIC", "VALUE_SK_TOKEN"):
            self.assertNotIn(value, text)
        self.assertNotRegex(text, r"(?i)bearer\s+VALUE|basic\s+VALUE|sk-[A-Za-z0-9_-]+")

    def test_provenance_hashes_are_complete_and_report_is_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="issue11-review-") as tmp, Endpoint("normal") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            first_result = self.run_evaluator(endpoint.url, workspace, artifact)
            first = self.report(artifact, first_result)
            second_result = self.run_evaluator(endpoint.url, workspace, artifact)
            second = self.report(artifact, second_result)
        hashes = first["provenance"].get("hashes", {})
        self.assertEqual(set(hashes), {"inputs", "schema", "runner", "workspace", "request", "response"})
        for name, value in hashes.items():
            self.assertRegex(value, r"^[0-9a-f]{64}$", name)
        self.assertEqual(first, second)
        self.assertFalse(re.search(r'"(?:timestamp|duration|elapsed|started_at|finished_at)"',
                                   json.dumps(first), re.I))

    def test_nested_json_produces_bounded_error_artifact(self):
        with tempfile.TemporaryDirectory(prefix="issue11-review-") as tmp, Endpoint("nested") as endpoint:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            result = self.run_evaluator(endpoint.url, workspace, artifact)
            report = self.report(artifact, result)
        malformed = self.case(report, "malformed-tool-call")
        self.assertFalse(malformed["passed"])
        serialized = json.dumps(malformed)
        self.assertLessEqual(len(serialized), 8192)
        self.assertRegex(serialized.lower(), r"error|artifact|depth|bounded")


if __name__ == "__main__":
    unittest.main()
