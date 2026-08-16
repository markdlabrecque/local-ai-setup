"""Issue #12 cycle-2 red contracts (C2-001..C2-005).

These tests are contracts only.  They use the committed JSON contracts and
bounded disposable fakes; they never load a model or sample the host GPU.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "benchmark.json"
BENCHMARK_SCHEMA = ROOT / "schemas" / "benchmark-result.schema.json"
EVALUATION_SCHEMA = ROOT / "schemas" / "evaluation-report.schema.json"
BENCHMARK_FIXTURE = ROOT / "tests" / "fixtures" / "benchmark-result.json"
CASES = ROOT / "tests" / "fixtures" / "evaluation_cases.json"
ENDPOINT = ROOT / "tests" / "fixtures" / "fake_openai_endpoint.py"
EVALUATOR = Path(os.environ.get("EVALUATION_RUNNER", ROOT / "scripts" / "run-evaluation.sh"))
BENCHMARK = Path(os.environ.get("BENCHMARK_RUNNER", ROOT / "scripts" / "run-benchmark.sh"))
TUNING = ROOT / "docs" / "issue-6-hybrid-vulkan-tuning-result.json"

EXPECTED_MODEL_SHA = "a" * 64
EXPECTED_RUNTIME = {"ref": "real-runtime", "commit": "real-commit"}


def _resolve(schema, root):
    ref = schema.get("$ref")
    if not ref:
        return schema
    assert ref.startswith("#/")
    value = root
    for part in ref[2:].split("/"):
        value = value[part]
    return value


def validate_json_schema(value, schema, root=None, path="$"):
    """Small stdlib-only validator for the committed schemas.

    The project deliberately does not require jsonschema just to validate a
    committed fixture.  This covers the draft features used by these two
    schemas, including required/const, nested additionalProperties, refs,
    allOf, prefixItems, arrays, and scalar bounds.
    """
    root = schema if root is None else root
    schema = _resolve(schema, root)
    for branch in schema.get("allOf", []):
        validate_json_schema(value, branch, root, path)
    if "const" in schema:
        assert value == schema["const"], f"{path}: expected const {schema['const']!r}"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: {value!r} not in enum"
    kind = schema.get("type")
    if kind == "object":
        assert isinstance(value, dict), f"{path}: expected object"
    elif kind == "array":
        assert isinstance(value, list), f"{path}: expected array"
    elif kind == "string":
        assert isinstance(value, str), f"{path}: expected string"
    elif kind == "integer":
        assert isinstance(value, int) and not isinstance(value, bool), f"{path}: expected integer"
    elif kind == "number":
        assert isinstance(value, (int, float)) and not isinstance(value, bool), f"{path}: expected number"
    elif kind == "boolean":
        assert isinstance(value, bool), f"{path}: expected boolean"
    if isinstance(value, dict):
        for key in schema.get("required", []):
            assert key in value, f"{path}: missing required {key!r}"
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            assert set(value) <= set(properties), f"{path}: unexpected properties {set(value) - set(properties)}"
        for key, child in properties.items():
            if key in value:
                validate_json_schema(value[key], child, root, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(schema.get("prefixItems", [])):
            if index < len(value):
                validate_json_schema(value[index], child, root, f"{path}[{index}]")
        if "items" in schema and not isinstance(schema["items"], bool):
            start = len(schema.get("prefixItems", []))
            for index in range(start, len(value)):
                validate_json_schema(value[index], schema["items"], root, f"{path}[{index}]")
        if "minItems" in schema:
            assert len(value) >= schema["minItems"], f"{path}: too few items"
        if "maxItems" in schema:
            assert len(value) <= schema["maxItems"], f"{path}: too many items"
    elif isinstance(value, str) and "pattern" in schema:
        assert re.fullmatch(schema["pattern"], value), f"{path}: pattern mismatch"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema:
            assert value >= schema["minimum"], f"{path}: below minimum"
        if "exclusiveMinimum" in schema:
            assert value > schema["exclusiveMinimum"], f"{path}: not above exclusive minimum"


class Issue12Cycle2Regressions(unittest.TestCase):
    maxDiff = None

    def _workspace(self, root):
        workspace = root / "workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "fixture_target.py").write_text("def one():\n    return 1\n")
        (workspace / ".git").mkdir()
        return workspace

    def _run_evaluator(self, root, *extra):
        workspace = self._workspace(root)
        artifact = root / "evaluation.json"
        endpoint = subprocess.Popen(
            [sys.executable, str(ENDPOINT), "--port", "0"],
            stdout=subprocess.PIPE, text=True,
        )
        try:
            port = int(endpoint.stdout.readline().strip())
            command = [str(EVALUATOR), "--endpoint", f"http://127.0.0.1:{port}",
                       "--cases", str(CASES), "--workspace", str(workspace),
                       "--artifacts", str(artifact), "--model", "real-model-c2",
                       "--model-sha256", EXPECTED_MODEL_SHA,
                       "--runtime-ref", EXPECTED_RUNTIME["ref"],
                       "--runtime-commit", EXPECTED_RUNTIME["commit"], *extra]
            return subprocess.run(command, cwd=ROOT, text=True, capture_output=True, timeout=20), artifact
        finally:
            endpoint.terminate()
            endpoint.wait(timeout=3)
            endpoint.stdout.close()

    def _evaluation_report(self, path):
        manifest = json.loads(CASES.read_text())
        cases = [{"id": row["id"], "kind": row["kind"], "passed": True,
                  "score": 1, "checks": {"measured": True}}
                 for row in manifest["cases"]]
        report = {
            "schema_version": 1,
            "suite": {"name": "issue-11-evaluation", "case_count": len(cases),
                       "context_target_tokens": manifest["context_target_tokens"],
                       "all_required_cases_passed": True, "score": 1},
            "cases": cases,
            "scoring": {"method": "mean-case-score", "score": 1, "passed": True},
            "provenance": {
                "request_id": "c2-test-report", "transcript": [], "sanitized": True,
                "model": {"id": "real-model-c2", "sha256": EXPECTED_MODEL_SHA,
                          "synthetic_fixture": False},
                "runtime": {**EXPECTED_RUNTIME, "synthetic_fixture": False},
                "synthetic_fixture": False,
                "hashes": {name: hashlib.sha256(name.encode()).hexdigest()
                           for name in ("inputs", "schema", "runner", "workspace",
                                        "request", "response")},
            },
            "safety": {"workspace_unchanged": True, "sandboxed": True,
                       "bounded_artifacts": True, "model_commands_executed": False},
        }
        path.write_text(json.dumps(report, sort_keys=True))
        return path

    def _fake_runtime(self, root, *, protocol_only=False):
        runtime = root / "runtime.py"
        body = [
            "#!/usr/bin/env python3",
            "import sys",
            "if '--version' in sys.argv:",
            "    print('llama-cli version b10446 (adb55e5)')",
            "    raise SystemExit(0)",
        ]
        if protocol_only:
            body += [
                "print('Vulkan0 : AMD Radeon RX 6900 XT', file=sys.stderr)",
                "print('BENCHMARK_HARDWARE {\"pci_id\":\"1002:73BF\",\"vram_capacity_mib\":16368,\"vram_used_mib\":1,\"ram_available_mib\":50000,\"swap_in_pages\":0}', file=sys.stderr)",
                "print('BENCHMARK_HARDWARE {\"pci_id\":\"1002:73BF\",\"vram_capacity_mib\":16368,\"vram_used_mib\":2,\"ram_available_mib\":50000,\"swap_in_pages\":0}', file=sys.stderr)",
                "print('llama_context: n_ctx = 32768', file=sys.stderr)",
                "print('llama_perf_context_print: prompt eval time = 1 ms / 25 tokens (25 tokens per second)', file=sys.stderr)",
                "print('llama_perf_context_print: eval time = 1 ms / 8 runs (8 tokens per second)', file=sys.stderr)",
                "print('LOCAL_AI_BENCHMARK_OK', flush=True)",
            ]
        runtime.write_text("\n".join(body) + "\n")
        runtime.chmod(0o755)
        return runtime

    def _run_benchmark(self, root, report, runtime, *extra):
        return subprocess.run(
            [str(BENCHMARK), "--config", str(CONFIG), "--tuning-result", str(TUNING),
             "--evaluation-report", str(report), "--llama-cli", str(runtime),
             "--output", str(root / "benchmark.json"), "--run-timeout", "2", *extra],
            cwd=ROOT, text=True, capture_output=True, timeout=15,
        )

    def test_c2_001_real_model_is_configurable_and_provenance_is_semantic(self):
        """A passing report must identify the selected real model and runtime."""
        schema = json.loads(EVALUATION_SCHEMA.read_text())
        provenance = schema["properties"]["provenance"]
        self.assertTrue({"model", "runtime", "synthetic_fixture", "hashes"} <=
                        set(provenance["required"]),
                        "Issue 11 schema does not require model/runtime provenance")
        with tempfile.TemporaryDirectory(prefix="issue12-c2-001-") as directory:
            result, artifact = self._run_evaluator(Path(directory))
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            report = json.loads(artifact.read_text())
            self.assertEqual(report["provenance"]["model"], {
                "id": "real-model-c2", "sha256": EXPECTED_MODEL_SHA,
                "synthetic_fixture": False,
            })
            self.assertEqual(report["provenance"]["runtime"], {
                **EXPECTED_RUNTIME, "synthetic_fixture": False,
            })
            self.assertFalse(report["provenance"]["synthetic_fixture"])
            hashes = report["provenance"]["hashes"]
            for name in ("inputs", "schema", "runner", "workspace", "request", "response"):
                self.assertRegex(hashes[name], r"^[0-9a-f]{64}$")
            self.assertEqual(report["suite"]["case_count"], len(report["cases"]))
            self.assertEqual(report["suite"]["all_required_cases_passed"],
                             all(case["passed"] for case in report["cases"]))

    def test_c2_002_benchmark_rejects_report_that_fails_any_schema_branch(self):
        """Validation must cover the complete Issue 11 schema before runtime."""
        schema = json.loads(EVALUATION_SCHEMA.read_text())
        self.assertIn("hashes", schema["properties"]["provenance"]["required"])
        with tempfile.TemporaryDirectory(prefix="issue12-c2-002-") as directory:
            root = Path(directory)
            # This is valid under the report's top-level/semantic contract,
            # but the schema forbids unknown hash keys.  A subset validator
            # silently accepts the extra field.
            report = root / "invalid-evaluation.json"
            self._evaluation_report(report)
            invalid = json.loads(report.read_text())
            invalid["provenance"]["hashes"]["unexpected"] = "not-a-schema-field"
            report.write_text(json.dumps(invalid))
            runtime = self._fake_runtime(root, protocol_only=True)
            result = self._run_benchmark(root, report, runtime)
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr, r"(?i)(schema|validation|hash)")
            self.assertFalse((root / "benchmark.json").exists(),
                             "invalid evaluation reached benchmark publication")

    def test_c2_003_live_hardware_requires_runner_owned_proc_drm_samples(self):
        """Runtime-emitted BENCHMARK_HARDWARE is not live DRM evidence."""
        schema = json.loads(BENCHMARK_SCHEMA.read_text())
        hardware = schema["$defs"]["run"]["properties"]["hardware"]
        self.assertIn("sampling", hardware["required"])
        sampling = hardware["properties"]["sampling"]
        self.assertTrue({"owner", "source", "continuous", "proc_root"} <=
                        set(sampling["required"]))
        self.assertEqual(sampling["properties"]["owner"]["const"], "runner")
        self.assertEqual(sampling["properties"]["source"]["const"], "drm")
        self.assertEqual(sampling["properties"]["continuous"]["const"], True)
        sample = hardware["properties"]["samples"]["items"]
        self.assertTrue({"pci_id", "drm_device", "source"} <= set(sample["required"]))
        self.assertEqual(sample["properties"]["pci_id"]["const"], "1002:73BF")
        self.assertEqual(sample["properties"]["source"]["const"], "drm")
        with tempfile.TemporaryDirectory(prefix="issue12-c2-003-") as directory:
            root = Path(directory)
            report = self._evaluation_report(root / "evaluation.json")
            result = self._run_benchmark(root, report,
                                         self._fake_runtime(root, protocol_only=True))
            self.assertNotEqual(result.returncode, 0)
            self.assertRegex(result.stderr, r"(?i)(proc|drm|hardware|live)")

    def test_c2_004_cache_claims_are_ordered_and_unprivileged_cold_is_honest(self):
        config = json.loads(CONFIG.read_text())
        lifecycle = config["lifecycle"]
        cold = lifecycle["cold"]
        warm = lifecycle["warm"]
        self.assertTrue(cold.get("checksum_before_preparation"),
                        "model checksum must precede cache-state preparation")
        self.assertIn(cold.get("cache_preparation"), {"page-cache-eviction", "deferred", "unsupported"})
        self.assertTrue(cold.get("verifiable_or_deferred"),
                        "cold cache must not be mislabeled when eviction is unavailable")
        self.assertEqual(warm.get("observed_after"), "cold")
        self.assertTrue(warm.get("follows_observed_first_run"))
        schema = json.loads(BENCHMARK_SCHEMA.read_text())
        lifecycle_schema = schema["$defs"]["run"]["properties"]["lifecycle"]
        evidence = lifecycle_schema["properties"]["cache_evidence"]
        self.assertTrue({"checksum_before_preparation", "preparation", "honest_status"} <=
                        set(evidence["required"]))
        self.assertIn(evidence["properties"]["honest_status"]["enum"],
                      [["verified-miss", "verified-hit", "unsupported", "deferred"]])

    def test_c2_005_pinned_tokenizer_count_and_committed_fixture_validate_without_dependency(self):
        config = json.loads(CONFIG.read_text())
        prompt = config["prompt"]
        self.assertEqual(prompt["token_count"], 25)
        self.assertEqual(prompt["observed_token_count"], 25)
        self.assertTrue(prompt["tokenizer"]["pinned"])
        self.assertTrue(prompt["tokenizer"]["preflight"])
        schema = json.loads(BENCHMARK_SCHEMA.read_text())
        self.assertEqual(schema["properties"]["inputs"]["properties"]["prompt"]["properties"]["token_count"]["const"], 25)
        self.assertEqual(schema["$defs"]["run"]["properties"]["metrics"]["properties"]["prompt_tokens"]["const"], 25)
        fixture = json.loads(BENCHMARK_FIXTURE.read_text())
        validate_json_schema(fixture, schema)
        self.assertIs(fixture["inputs"]["lifecycle"]["warmup"], False)
        for run in fixture["runs"]:
            self.assertEqual(run["metrics"]["prompt_tokens"], 25)
            self.assertIs(run["lifecycle"]["warmup"], False)


if __name__ == "__main__":
    unittest.main()
