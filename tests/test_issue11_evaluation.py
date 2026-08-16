"""Issue #11 red contracts for the inference/coding-agent evaluation suite.

The evaluator is intentionally not implemented in this worktree.  These tests
specify its CLI, artifact schema, safety boundary, and deterministic endpoint.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
CASES = FIXTURES / "evaluation_cases.json"
ENDPOINT = FIXTURES / "fake_openai_endpoint.py"
RUNNER = Path(os.environ.get("EVALUATION_RUNNER", ROOT / "scripts" / "run-evaluation.sh"))


class Issue11EvaluationContracts(unittest.TestCase):
    def test_case_manifest_covers_required_contracts(self):
        manifest = json.loads(CASES.read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertGreaterEqual(manifest["context_target_tokens"], 32000)
        kinds = {case["kind"] for case in manifest["cases"]}
        self.assertTrue({"chat", "retrieval", "code", "navigation", "patch", "command",
                         "tool", "reasoning", "lifecycle", "overflow", "provenance"} <= kinds)
        ids = {case["id"] for case in manifest["cases"]}
        self.assertTrue({"malformed-tool-call", "sequential-tool-replay", "parallel-tool-replay",
                         "reasoning-off", "reasoning-on", "cancellation", "overflow-compaction"} <= ids)

    def test_fake_endpoint_is_openai_compatible_and_deterministic(self):
        proc = subprocess.Popen([sys.executable, str(ENDPOINT), "--port", "0"],
                                stdout=subprocess.PIPE, text=True)
        try:
            port = int(proc.stdout.readline().strip())
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                self.assertEqual(json.load(response)["status"], "ok")
            payload = json.dumps({"model": "fixture-model", "messages": [
                {"role": "user", "content": "Reply exactly"}]}).encode()
            # The fixture must accept the OpenAI chat-completions request shape.
            import urllib.request
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/v1/chat/completions", data=payload,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=2) as response:
                result = json.load(response)
            self.assertEqual(result["choices"][0]["message"]["content"], "CONTRACT_OK")
            self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        finally:
            proc.terminate()
            proc.wait(timeout=3)
            proc.stdout.close()

    def _run_evaluator(self, endpoint, workspace, artifact):
        # A missing runner is an intentional red result, not a skipped test.
        try:
            return subprocess.run([
                str(RUNNER), "--endpoint", endpoint, "--cases", str(CASES),
                "--workspace", str(workspace), "--artifacts", str(artifact),
            ], cwd=workspace, text=True, capture_output=True, timeout=30)
        except FileNotFoundError as error:
            # Keep the initial red result behavioral and actionable rather than
            # letting an absent future entrypoint become an import/environment error.
            return subprocess.CompletedProcess(
                args=[str(RUNNER)], returncode=127, stdout="",
                stderr=f"missing evaluator entrypoint: {error}")

    def test_evaluator_produces_safe_complete_artifact_without_repo_mutation(self):
        with tempfile.TemporaryDirectory(prefix="issue11-eval-") as directory:
            workspace = Path(directory) / "repo"
            workspace.mkdir()
            (workspace / "src").mkdir()
            (workspace / "src" / "fixture_target.py").write_text("def one():\n    return 1\n\n")
            (workspace / ".git").mkdir()  # disposable boundary marker, never the real repository
            before = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file())
            artifact = Path(directory) / "artifacts" / "evaluation.json"
            endpoint_proc = subprocess.Popen([sys.executable, str(ENDPOINT), "--port", "0"],
                                             stdout=subprocess.PIPE, text=True)
            try:
                port = int(endpoint_proc.stdout.readline().strip())
                result = self._run_evaluator(f"http://127.0.0.1:{port}", workspace, artifact)
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertTrue(artifact.is_file())
                report = json.loads(artifact.read_text())
                self.assertEqual(report["schema_version"], 1)
                self.assertTrue(report["suite"]["all_required_cases_passed"])
                self.assertIn("request_id", report["provenance"])
                self.assertIn("transcript", report["provenance"])
                self.assertTrue(report["provenance"]["sanitized"])
                self.assertIn("reasoning", report)
                self.assertIn("cancellation", report)
                self.assertIn("compaction", report)
                after = sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*") if p.is_file())
                self.assertEqual(after, before, "evaluation mutated its disposable workspace")
                self.assertNotIn(str(ROOT), artifact.read_text())
                self.assertNotRegex(artifact.read_text(), r"(?i)(api[_-]?key|bearer|password|private key)")
            finally:
                endpoint_proc.terminate()
                endpoint_proc.wait(timeout=3)
                endpoint_proc.stdout.close()


if __name__ == "__main__":
    unittest.main()
