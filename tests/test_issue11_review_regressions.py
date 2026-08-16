"""Adversarial Issue #11 review contracts.

These tests deliberately use a small, deterministic HTTP fixture rather than a
model.  They are red until the evaluator proves the reviewed safety and
provenance claims; they never write to the checkout under test.
"""
import copy
import hashlib
import json
import os
import re
import subprocess
import sys
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


class AdversarialEndpoint(BaseHTTPRequestHandler):
    """A deterministic endpoint with deliberately unhelpful responses."""

    protocol_version = "HTTP/1.1"
    mode = "normal"
    requests = []
    cancel_write_failed = False
    cancel_delay = 0.0

    def log_message(self, *_args):
        pass

    def _reply(self, payload, status=200):
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            type(self).cancel_write_failed = True

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            self._reply({"status": "ok", "model": "adversarial-fixture"})
        elif self.path == "/v1/models":
            self._reply({"object": "list", "data": [{"id": "adversarial-fixture"}]})
        else:
            self._reply({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._reply({"error": "not found"}, 404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).requests.append(copy.deepcopy(payload))
        messages = payload.get("messages", [])
        marker = json.dumps(messages, sort_keys=True)
        mode = type(self).mode

        if mode == "cancel" and payload.get("eval_cancel_probe"):
            type(self).cancel_delay = 0.35
            time.sleep(type(self).cancel_delay)
            self._reply({"choices": [{"message": {"role": "assistant", "content": "late"},
                                      "finish_reason": "stop"}]})
            return

        if mode == "overflow" and payload.get("eval_overflow_probe"):
            # The first oversized request must contain current user content
            # and cause a real retry, not a report-only compaction flag.
            if "PRESERVE-USER-CONTENT" in marker and len(marker) > 30000:
                self._reply({"error": {"type": "context_length_exceeded"}}, 400)
                return

        if mode == "tools":
            has_tool_result = any(m.get("role") == "tool" for m in messages
                                  if isinstance(m, dict))
            if not has_tool_result:
                message = {"role": "assistant", "tool_calls": [{
                    "id": "seq-call-1", "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"src/fixture_target.py"}'},
                }]}
            else:
                message = {"role": "assistant", "tool_calls": [{
                    "id": "seq-call-2", "type": "function",
                    "function": {"name": "read", "arguments": '{"path":"src/fixture_target.py","line":3}'},
                }]}
            self._reply({"id": "fixture", "choices": [{"message": message,
                       "finish_reason": "tool_calls"}],
                       "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5}})
            return

        if mode == "nested":
            nested = "{}"
            for _ in range(100):
                nested = json.dumps({"next": json.loads(nested)})
            message = {"role": "assistant", "tool_calls": [{
                "id": "nested", "type": "function",
                "function": {"name": "read", "arguments": nested},
            }]}
            self._reply({"id": "fixture", "choices": [{"message": message,
                       "finish_reason": "tool_calls"}]})
            return

        if mode == "navigation" and "fixture_target" in marker:
            content = "/etc/passwd ../outside"
        elif mode == "redaction" and "instruction" in marker:
            content = json.dumps({
                "api_key": "VALUE_API_KEY",
                "password": "VALUE_PASSWORD",
                "client_secret": "VALUE_CLIENT_SECRET",
                "authorization": "Bearer VALUE_BEARER",
                "basic_auth": "Basic VALUE_BASIC",
                "token": "sk-VALUE_SK_TOKEN",
            })
        elif "NEEDLE-31K-7F3A" in marker:
            content = "NEEDLE-31K-7F3A"
        elif "code-generation" in marker:
            content = "def add(a, b):\n    return a + b"
        elif "PRESERVE-USER-CONTENT" in marker:
            content = "PRESERVE-USER-CONTENT"
        elif mode == "universal":
            content = "CONTRACT_OK"
        else:
            content = "CONTRACT_OK"

        usage = {"prompt_tokens": 31500, "completion_tokens": 1, "total_tokens": 31501}
        if "NEEDLE-31K-7F3A" not in marker:
            usage = {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}
        self._reply({"id": "fixture", "object": "chat.completion",
                     "model": "adversarial-fixture",
                     "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                  "finish_reason": "stop"}],
                     "usage": usage})


class Endpoint:
    def __init__(self, mode):
        self.mode = mode
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AdversarialEndpoint)
        AdversarialEndpoint.mode = mode
        AdversarialEndpoint.requests = []
        AdversarialEndpoint.cancel_write_failed = False
        AdversarialEndpoint.cancel_delay = 0.0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def requests(self):
        return AdversarialEndpoint.requests

    @property
    def cancel_delay(self):
        return AdversarialEndpoint.cancel_delay

    @property
    def cancel_write_failed(self):
        return AdversarialEndpoint.cancel_write_failed

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def validate_report_shape(report):
    """Small stdlib-only validator for the committed JSON schema's contract."""
    declared = json.loads(SCHEMA.read_text())
    assert set(declared["required"]) <= set(report), "report violates declared required fields"
    assert report.get("schema_version") == 1
    for key in ("suite", "cases", "scoring", "provenance", "safety"):
        assert key in report, f"report missing {key}"
    suite = report["suite"]
    assert suite["name"] == "issue-11-evaluation"
    assert isinstance(suite["case_count"], int) and suite["case_count"] >= 1
    assert isinstance(suite["all_required_cases_passed"], bool)
    assert 0 <= suite["score"] <= 1
    assert isinstance(report["cases"], list) and report["cases"]
    for case in report["cases"]:
        for key in ("id", "kind", "passed", "score", "checks"):
            assert key in case, f"case missing {key}"
        assert re.fullmatch(r"[a-z0-9][a-z0-9-]*", case["id"])
        assert isinstance(case["passed"], bool) and 0 <= case["score"] <= 1
        assert isinstance(case["checks"], dict)
    assert report["scoring"]["method"] == "mean-case-score"
    assert report["scoring"]["passed"] in (True, False)
    for key in ("request_id", "transcript", "sanitized"):
        assert key in report["provenance"]
    assert report["provenance"]["sanitized"] is True
    for key in ("workspace_unchanged", "sandboxed", "bounded_artifacts"):
        assert report["safety"][key] is True


class Issue11ReviewRegressions(unittest.TestCase):
    maxDiff = None

    def run_evaluator(self, endpoint, workspace, artifact, cases=CASES):
        before = {p.relative_to(workspace).as_posix(): p.read_bytes()
                  for p in workspace.rglob("*") if p.is_file()}
        result = subprocess.run(
            [str(RUNNER), "--endpoint", endpoint, "--cases", str(cases),
             "--workspace", str(workspace), "--artifacts", str(artifact)],
            cwd=workspace, text=True, capture_output=True, timeout=12,
        )
        after = {p.relative_to(workspace).as_posix(): p.read_bytes()
                 for p in workspace.rglob("*") if p.is_file()}
        self.assertEqual(after, before, "evaluator mutated the supplied workspace")
        return result

    def workspace(self, directory):
        root = Path(directory) / "workspace"
        (root / "src").mkdir(parents=True)
        (root / "src" / "fixture_target.py").write_text("one\ntwo\nthree\n")
        (root / ".git").mkdir()
        return root

    def report(self, artifact, result):
        self.assertTrue(artifact.is_file(), result.stderr or result.stdout)
        value = json.loads(artifact.read_text())
        validate_report_shape(value)
        return value

    def case(self, report, case_id):
        return next(case for case in report["cases"] if case["id"] == case_id)

    def test_canned_contract_ok_cannot_pass_capability_cases(self):
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            with Endpoint("universal") as endpoint:
                workspace = self.workspace(tmp)
                artifact = Path(tmp) / "report.json"
                result = self.run_evaluator(endpoint.url, workspace, artifact)
                report = self.report(artifact, result)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(self.case(report, "code-generation")["passed"])
            self.assertFalse(self.case(report, "long-context-retrieval")["passed"])

    def test_long_context_records_measured_request_near_32k(self):
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            with Endpoint("normal") as endpoint:
                workspace = self.workspace(tmp)
                artifact = Path(tmp) / "report.json"
                result = self.run_evaluator(endpoint.url, workspace, artifact)
                report = self.report(artifact, result)
            long_requests = [r for r in endpoint.requests if "NEEDLE-31K-7F3A" in json.dumps(r)]
            self.assertTrue(long_requests, "retrieval request was not sent")
            serialized = json.dumps(long_requests[0]["messages"])
            self.assertGreaterEqual(len(serialized) // 4, 30000)
            checks = self.case(report, "long-context-retrieval")["checks"]
            usage = checks.get("usage", checks.get("measured_usage", {}))
            self.assertGreaterEqual(usage.get("prompt_tokens", 0), 30000)
            self.assertLessEqual(usage.get("prompt_tokens", 0), 32768)

    def test_overflow_requires_a_real_compaction_retry(self):
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            with Endpoint("overflow") as endpoint:
                workspace = self.workspace(tmp)
                artifact = Path(tmp) / "report.json"
                result = self.run_evaluator(endpoint.url, workspace, artifact)
                report = self.report(artifact, result)
            oversized = [r for r in endpoint.requests if len(json.dumps(r["messages"])) > 30000]
            self.assertTrue(oversized, "no oversized request reached the endpoint")
            compacted = [r for r in endpoint.requests if "PRESERVE-USER-CONTENT" in json.dumps(r)]
            self.assertTrue(compacted, "overflow was flagged without a compacted retry")
            self.assertLess(len(json.dumps(compacted[-1]["messages"])),
                            len(json.dumps(oversized[0]["messages"])))
            checks = self.case(report, "overflow-compaction")["checks"]
            self.assertTrue(checks.get("compacted"))
            self.assertFalse(checks.get("lost_user_content", True))

    def test_cancellation_is_delayed_and_client_aborted(self):
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            with Endpoint("cancel") as endpoint:
                workspace = self.workspace(tmp)
                artifact = Path(tmp) / "report.json"
                started = time.monotonic()
                result = self.run_evaluator(endpoint.url, workspace, artifact)
                elapsed = time.monotonic() - started
                report = self.report(artifact, result)
            self.assertLess(elapsed, 5)
            self.assertGreaterEqual(endpoint.cancel_delay, 0.30,
                                    "cancellation endpoint was not actually delayed")
            self.assertTrue(endpoint.cancel_write_failed,
                            "cancellation completed normally instead of aborting in-flight work")
            self.assertTrue(self.case(report, "cancellation")["checks"].get("cancelled"))

    def test_tool_replay_preserves_and_validates_sequential_calls(self):
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            with Endpoint("tools") as endpoint:
                workspace = self.workspace(tmp)
                artifact = Path(tmp) / "report.json"
                result = self.run_evaluator(endpoint.url, workspace, artifact)
                report = self.report(artifact, result)
            sequential = [r for r in endpoint.requests if "sequential" in json.dumps(r)]
            assistant_calls = []
            for request in sequential:
                for message in request.get("messages", []):
                    if message.get("role") == "assistant" and message.get("tool_calls"):
                        assistant_calls.extend(message["tool_calls"])
            self.assertGreaterEqual(len(assistant_calls), 2)
            self.assertEqual(len({c["id"] for c in assistant_calls}), len(assistant_calls))
            self.assertEqual({c["function"]["name"] for c in assistant_calls}, {"read"})
            for call in assistant_calls:
                self.assertIsInstance(json.loads(call["function"]["arguments"]), dict)
            self.assertIn("seq-call-2", {c["id"] for c in assistant_calls})
            tool_results = [m for request in sequential for m in request.get("messages", [])
                            if m.get("role") == "tool"]
            self.assertIn("seq-call-1", {m.get("tool_call_id") for m in tool_results})
            checks = self.case(report, "sequential-tool-replay")["checks"]
            self.assertGreaterEqual(checks.get("tool_calls", 0), 2)

    def test_manifest_mapping_is_exact_and_runner_rejects_duplicates(self):
        manifest = json.loads(CASES.read_text())
        self.assertEqual({case["id"]: case["kind"] for case in manifest["cases"]}, EXPECTED_KINDS)
        self.assertEqual(len(manifest["cases"]), len({case["id"] for case in manifest["cases"]}))
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            bad = copy.deepcopy(manifest)
            bad["cases"].append(copy.deepcopy(bad["cases"][0]))
            bad_cases = Path(tmp) / "duplicate.json"
            bad_cases.write_text(json.dumps(bad))
            workspace = self.workspace(tmp)
            with Endpoint("normal") as endpoint:
                result = self.run_evaluator(endpoint.url, workspace, Path(tmp) / "report.json", bad_cases)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(endpoint.requests, "duplicate manifest reached the model")

    def test_runner_rejects_wrong_manifest_kind(self):
        manifest = json.loads(CASES.read_text())
        manifest["cases"][0]["kind"] = "code"
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            bad_cases = Path(tmp) / "wrong-kind.json"
            bad_cases.write_text(json.dumps(manifest))
            workspace = self.workspace(tmp)
            with Endpoint("normal") as endpoint:
                result = self.run_evaluator(endpoint.url, workspace, Path(tmp) / "report.json", bad_cases)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(endpoint.requests, "wrong ID-kind mapping reached the model")

    def test_report_is_validated_and_navigation_rejects_absolute_and_parent_paths(self):
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            with Endpoint("navigation") as endpoint:
                workspace = self.workspace(tmp)
                artifact = Path(tmp) / "report.json"
                result = self.run_evaluator(endpoint.url, workspace, artifact)
                report = self.report(artifact, result)
            navigation = self.case(report, "repository-navigation")
            self.assertFalse(navigation["passed"])
            self.assertRegex(json.dumps(navigation).lower(), r"reject|unsafe|outside")

    def test_sensitive_values_are_redacted_by_key_and_token_form(self):
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            with Endpoint("redaction") as endpoint:
                workspace = self.workspace(tmp)
                artifact = Path(tmp) / "report.json"
                result = self.run_evaluator(endpoint.url, workspace, artifact)
                self.report(artifact, result)
            text = artifact.read_text()
            for value in ("VALUE_API_KEY", "VALUE_PASSWORD", "VALUE_CLIENT_SECRET",
                          "VALUE_BEARER", "VALUE_BASIC", "VALUE_SK_TOKEN"):
                self.assertNotIn(value, text)
            self.assertNotRegex(text, r"(?i)bearer\s+VALUE|basic\s+VALUE|sk-[A-Za-z0-9_-]+")

    def test_provenance_hashes_inputs_and_reports_are_deterministic(self):
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            workspace = self.workspace(tmp)
            artifact = Path(tmp) / "report.json"
            with Endpoint("normal") as endpoint:
                first = self.run_evaluator(endpoint.url, workspace, artifact)
                first_report = self.report(artifact, first)
                second = self.run_evaluator(endpoint.url, workspace, artifact)
                second_report = self.report(artifact, second)
            hashes = first_report["provenance"].get("hashes", {})
            for name in ("inputs", "schema", "runner", "workspace", "request", "response"):
                self.assertRegex(hashes.get(name, ""), r"^[0-9a-f]{64}$", name)
            self.assertEqual(first_report, second_report)
            volatile = re.compile(r"(timestamp|duration|elapsed|started_at|finished_at)", re.I)
            self.assertFalse(any(volatile.search(key)
                                 for key in json.dumps(first_report).split('"')
                                 if key and not key.startswith("http")))

    def test_nested_json_is_a_bounded_case_error_artifact(self):
        with tempfile.TemporaryDirectory(prefix="issue11-adversarial-") as tmp:
            with Endpoint("nested") as endpoint:
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
