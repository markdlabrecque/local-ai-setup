"""Issue #12 red contracts for the reproducible benchmark artifact.

The runtime below is a deterministic process fake.  It emits observations that
are deliberately different for load, prompt evaluation, and first-token
latency; no model or real GPU is used by this test.
"""
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "benchmark.json"
SCHEMA = ROOT / "schemas" / "benchmark-result.schema.json"
DOC = ROOT / "docs" / "issue-12-benchmark.md"
TUNING = ROOT / "docs" / "issue-6-hybrid-vulkan-tuning-result.json"
CASES = ROOT / "tests" / "fixtures" / "evaluation_cases.json"
RUNNER = Path(os.environ.get("BENCHMARK_RUNNER", ROOT / "scripts" / "run-benchmark.sh"))


class Issue12BenchmarkContracts(unittest.TestCase):
    """Tests are intentionally red until the Issue #12 runner is supplied."""

    def test_config_is_pinned_to_issue6_and_issue11_with_bounded_lifecycle(self):
        self.assertTrue(CONFIG.is_file(), f"missing benchmark config: {CONFIG}")
        config = json.loads(CONFIG.read_text())
        self.assertEqual(config["schema_version"], "benchmark-v1")
        self.assertEqual(Path(ROOT / config["tuning_result"]).resolve(), TUNING.resolve())
        self.assertEqual(config["evaluator"]["suite"], "issue-11-evaluation")
        self.assertEqual(config["lifecycle"]["modes"], ["cold", "warm"])
        self.assertTrue(config["lifecycle"]["cold"]["cache_miss"])
        self.assertTrue(config["lifecycle"]["warm"]["cache_hit"])
        self.assertGreater(config["timeout_seconds"], 0)
        self.assertLessEqual(config["timeout_seconds"], 300)
        self.assertEqual(config["cleanup"]["process_group"], True)
        self.assertEqual(config["prompt"]["token_count"], 23)
        self.assertEqual(config["output"]["token_count"], 8)
        self.assertGreaterEqual(config["context_tokens"], 32768)
        self.assertEqual(config["lifecycle"]["warmup"], False)
        self.assertTrue(config["lifecycle"]["cold"]["new_process"])
        self.assertTrue(config["lifecycle"]["warm"]["new_process"])
        self.assertEqual(config["safety"]["selected_gpu"], "Vulkan0")
        self.assertEqual(config["safety"]["selected_pci_id"], "1002:73BF")
        self.assertIn("selected_gpu", config["safety"])
        self.assertIn("minimum_free_vram_mib", config["safety"])
        self.assertIn("minimum_available_ram_mib", config["safety"])
        self.assertIn("maximum_swap_in_pages", config["safety"])

    def test_schema_requires_observed_metrics_provenance_and_audit_fields(self):
        self.assertTrue(SCHEMA.is_file(), f"missing benchmark schema: {SCHEMA}")
        schema = json.loads(SCHEMA.read_text())
        self.assertEqual(schema.get("$id"), "https://github.com/markdlabrecque/local-ai-setup/schemas/benchmark-result.schema.json")
        self.assertEqual(
            set(schema["required"]),
            {"schema_version", "benchmark", "inputs", "runs", "summary", "safety", "provenance"},
        )
        run = schema["$defs"]["run"]
        self.assertEqual(schema["properties"]["runs"]["minItems"], 2)
        self.assertEqual(schema["properties"]["runs"]["maxItems"], 2)
        self.assertEqual(schema["properties"]["runs"]["items"]["properties"]["mode"]["enum"], ["cold", "warm"])
        self.assertTrue({"mode", "cache", "status", "lifecycle", "metrics", "hardware", "settings", "quality"} <= set(run["required"]))
        self.assertTrue({"warmup", "new_process", "cache_evidence"} <= set(run["properties"]["lifecycle"]["required"]))
        metrics = run["properties"]["metrics"]
        required_metrics = {
            "load_time_ms", "ttft_ms", "prompt_eval_ms", "prompt_tokens",
            "prompt_tokens_per_second", "generation_tokens", "generation_eval_ms",
            "generation_tokens_per_second",
        }
        self.assertTrue(required_metrics <= set(metrics["required"]))
        self.assertNotEqual("ttft_ms", "prompt_eval_ms")
        self.assertTrue({"model", "quantization", "build", "device", "context_tokens", "parameters"} <= set(run["properties"]["settings"]["required"]))
        hardware = run["properties"]["hardware"]
        self.assertTrue({"selected_gpu", "pci_id", "samples", "ram", "vram", "swap"} <= set(hardware["required"]))
        self.assertTrue({"minimum_available_mib", "passed"} <= set(hardware["properties"]["ram"]["required"]))
        self.assertTrue({"capacity_mib", "peak_mib", "passed"} <= set(hardware["properties"]["vram"]["required"]))
        self.assertTrue({"in_pages", "passed"} <= set(hardware["properties"]["swap"]["required"]))
        self.assertTrue({"suite", "report_sha256", "passed"} <= set(run["properties"]["quality"]["required"]))
        self.assertTrue({"config_sha256", "tuning_result_sha256", "evaluator_report_sha256", "artifact_sha256", "sanitized"} <= set(schema["properties"]["provenance"]["required"]))

    def test_docs_define_command_inputs_fixed_lengths_and_auditable_artifact(self):
        self.assertTrue(DOC.is_file(), f"missing benchmark documentation: {DOC}")
        text = DOC.read_text()
        for required in (
            "run-benchmark.sh", "issue-6", "issue-11-evaluation", "--resume",
            "cold", "warm", "cache miss", "cache hit", "TTFT", "load time",
            "prompt tokens", "generation tokens", "Q8_0", "selected GPU",
            "VRAM", "RAM", "swap", "timeout", "process group", "sanitized",
            "raw logs", "fixed",
        ):
            self.assertIn(required, text, f"documentation omits {required!r}")

    def _fake_runtime(self, directory):
        log = Path(directory) / "runtime.log"
        child_pid = Path(directory) / "child.pid"
        runtime = Path(directory) / "fake-runtime.py"
        runtime.write_text(textwrap.dedent(r"""
            #!/usr/bin/env python3
            import json, os, signal, subprocess, sys, time
            log = os.environ["FAKE_RUNTIME_LOG"]
            with open(log, "a") as f:
                f.write(json.dumps({"argv": sys.argv[1:]}) + "\n")
            if "--version" in sys.argv:
                print("llama-cli version b10446 (adb55e5)")
                raise SystemExit(0)
            if "--prompt-tokens" in sys.argv:
                print("error: unknown argument --prompt-tokens", file=sys.stderr)
                raise SystemExit(64)
            if os.environ.get("FAKE_RUNTIME_MODE") == "timeout":
                child = subprocess.Popen(["sh", "-c", "trap '' TERM INT; while :; do sleep 1; done"])
                with open(os.environ["FAKE_CHILD_PID"], "w") as f:
                    f.write(str(child.pid))
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                while True:
                    time.sleep(1)
            # These are separate observations. In particular, prompt eval is
            # 1000 ms while the first streamed byte is observed independently.
            print("Vulkan0 : AMD Radeon RX 6900 XT (RADV NAVI21) (16368 MiB, 15404 MiB free)", file=sys.stderr)
            print("BENCHMARK_HARDWARE {\"pci_id\":\"1002:73BF\",\"card\":\"card1\",\"vram_capacity_mib\":16368,\"vram_used_mib\":8520,\"ram_available_mib\":50000,\"swap_in_pages\":0}", file=sys.stderr)
            print("BENCHMARK_HARDWARE {\"pci_id\":\"1002:73BF\",\"card\":\"card1\",\"vram_capacity_mib\":16368,\"vram_used_mib\":8530,\"ram_available_mib\":49990,\"swap_in_pages\":0}", file=sys.stderr)
            print("llama_context: n_ctx = 32768", file=sys.stderr)
            print("BENCHMARK_EVENT event=load elapsed_ms=1250.0", file=sys.stderr)
            print("BENCHMARK_EVENT event=prompt_eval elapsed_ms=1000.0 tokens=23", file=sys.stderr)
            time.sleep(0.02)
            print("BENCHMARK_EVENT event=token index=0", flush=True)
            print("BENCHMARK_EVENT event=generation elapsed_ms=500.0 tokens=8", file=sys.stderr)
            print("RAW_LOG_SECRET bearer TOPSECRET /private/raw/path " + ("X" * 100000), file=sys.stderr)
        """).lstrip())
        runtime.chmod(0o755)
        return runtime, log, child_pid

    @staticmethod
    def _report(path, passed=True):
        manifest = json.loads(CASES.read_text())
        tuning = json.loads(TUNING.read_text())
        candidate = tuning["stable_candidate"]
        cases = [{"id": row["id"], "kind": row["kind"], "passed": passed,
                  "score": 1 if passed else 0, "checks": {"measured": True}}
                 for row in manifest["cases"]]
        report = {
            "schema_version": 1,
            "suite": {"name": "issue-11-evaluation", "case_count": len(cases),
                       "context_target_tokens": manifest["context_target_tokens"],
                       "all_required_cases_passed": passed, "score": 1 if passed else 0},
            "cases": cases,
            "scoring": {"method": "mean-case-score", "score": 1 if passed else 0,
                        "passed": passed},
            "provenance": {
                "request_id": "observed-evaluation-request",
                "transcript": [], "sanitized": True, "synthetic_fixture": False,
                "model": {"id": "Qwen3.5-27B-Q8_0",
                          "sha256": "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450",
                          "synthetic_fixture": False},
                "runtime": {"ref": "b10446", "commit": "adb55e5",
                            "synthetic_fixture": False},
                "candidate": {"tuple_id": candidate["tuple_id"],
                              "config_id": candidate["config_id"],
                              "evidence_id": candidate["evidence_id"],
                              "parameters": candidate["parameters"], "source": "issue-6-stable-candidate"},
                "hashes": {name: hashlib.sha256((name + "-observed").encode()).hexdigest()
                           for name in ("inputs", "schema", "runner", "workspace", "request", "response")},
            },
            "safety": {"workspace_unchanged": True, "sandboxed": True,
                       "bounded_artifacts": True, "model_commands_executed": False},
        }
        path.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")))

    def _require_runner(self):
        self.assertTrue(RUNNER.is_file(), f"missing benchmark runner: {RUNNER}")

    def _run(self, config, tuning, report, runtime, output, directory, *extra, env=None):
        command = [str(RUNNER), "--config", str(config), "--tuning-result", str(tuning),
                   "--evaluation-report", str(report), "--llama-cli", str(runtime),
                   "--output", str(output), "--run-timeout", "2", *extra]
        merged = os.environ.copy()
        merged.update({"FAKE_RUNTIME_LOG": str(directory / "runtime.log")})
        if env:
            merged.update(env)
        try:
            return subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                                  timeout=15, env=merged)
        except FileNotFoundError as error:
            return subprocess.CompletedProcess(command, 127, "", f"missing benchmark entrypoint: {error}")

    def test_fake_run_records_cold_warm_observations_and_issue6_issue11_identity(self):
        self._require_runner()
        with tempfile.TemporaryDirectory(prefix="issue12-benchmark-") as tmp:
            directory = Path(tmp)
            runtime, log, _ = self._fake_runtime(directory)
            report = directory / "evaluation.json"
            self._report(report)
            output = directory / "benchmark.json"
            result = self._run(CONFIG, TUNING, report, runtime, output, directory)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            artifact = json.loads(output.read_text())
            self.assertEqual(artifact["benchmark"]["name"], "issue-12-benchmark")
            self.assertEqual([r["mode"] for r in artifact["runs"]], ["cold", "warm"])
            self.assertEqual(artifact["inputs"]["prompt"]["token_count"], 23)
            self.assertEqual(artifact["inputs"]["output"]["token_count"], 8)
            self.assertEqual(artifact["inputs"]["model"], json.loads(CONFIG.read_text())["model"])
            tuning = json.loads(TUNING.read_text())
            candidate = tuning["stable_candidate"]
            self.assertEqual(artifact["inputs"]["runtime"], {
                "ref": "b10446", "commit": "adb55e5", "source": "observed",
                "synthetic_fixture": False,
            })
            self.assertEqual(artifact["inputs"]["candidate"], candidate)
            self.assertEqual(candidate["status"], "pass")
            self.assertEqual(candidate["parameters"]["quantization"], "Q8_0")
            self.assertEqual(candidate["evidence"]["measurement_source"], "live")
            self.assertEqual(candidate["evidence"]["vram_pci_id"], "1002:73BF")
            self.assertEqual(candidate["evidence"]["attempt_count"], 3)
            self.assertEqual(len(candidate["evidence"]["attempt_ids"]), 3)
            for row in artifact["runs"]:
                self.assertEqual(row["lifecycle"]["started"], True)
                self.assertEqual(row["lifecycle"]["bounded"], True)
                self.assertEqual(row["lifecycle"]["finished"], True)
                self.assertEqual(row["lifecycle"]["warmup"], False)
                self.assertEqual(row["lifecycle"]["new_process"], True)
                self.assertEqual(row["lifecycle"]["cache_evidence"]["observed"], True)
                self.assertEqual(row["cache"], "miss" if row["mode"] == "cold" else "hit")
                metrics = row["metrics"]
                self.assertEqual(metrics["load_time_ms"], 1250.0)
                self.assertGreater(metrics["ttft_ms"], 0)
                self.assertLess(metrics["ttft_ms"], 100.0)
                self.assertEqual(metrics["prompt_eval_ms"], 1000.0)
                self.assertNotEqual(metrics["ttft_ms"], metrics["prompt_eval_ms"])
                self.assertEqual(metrics["prompt_tokens"], 23)
                self.assertEqual(metrics["generation_tokens"], 8)
                self.assertGreater(metrics["prompt_tokens_per_second"], 0)
                self.assertGreater(metrics["generation_tokens_per_second"], 0)
                self.assertEqual(row["settings"]["quantization"], "Q8_0")
                self.assertEqual(row["settings"]["model"], "Qwen3.5-27B-Q8_0")
                self.assertEqual(row["settings"]["build"]["commit"], "adb55e5")
                self.assertEqual(row["settings"]["device"]["selected_gpu"], "Vulkan0")
                self.assertEqual(row["settings"]["context_tokens"], 32768)
                self.assertEqual(row["settings"]["parameters"], candidate["parameters"])
                self.assertEqual(row["hardware"]["selected_gpu"], "Vulkan0")
                self.assertEqual(row["hardware"]["pci_id"], "1002:73BF")
                self.assertGreaterEqual(len(row["hardware"]["samples"]), 2)
                self.assertTrue(all(sample["pci_id"] == "1002:73BF"
                                    for sample in row["hardware"]["samples"]))
                self.assertGreater(row["hardware"]["ram"]["minimum_available_mib"], 0)
                self.assertGreater(row["hardware"]["vram"]["capacity_mib"], row["hardware"]["vram"]["peak_mib"])
                self.assertEqual(row["hardware"]["swap"]["in_pages"], 0)
                self.assertTrue(row["hardware"]["ram"]["passed"])
                self.assertTrue(row["hardware"]["vram"]["passed"])
                self.assertTrue(row["hardware"]["swap"]["passed"])
                self.assertEqual(row["quality"]["suite"], "issue-11-evaluation")
                self.assertTrue(row["quality"]["passed"])
                self.assertEqual(row["quality"]["report_sha256"], hashlib.sha256(report.read_bytes()).hexdigest())
            self.assertEqual(artifact["safety"]["selected_gpu"], "Vulkan0")
            self.assertEqual(artifact["safety"]["swap_in_pages"], 0)
            self.assertTrue(artifact["safety"]["ram_passed"])
            self.assertTrue(artifact["safety"]["vram_passed"])
            self.assertLessEqual(output.stat().st_size, 65536)
            text = output.read_text()
            for secret in ("RAW_LOG_SECRET", "TOPSECRET", "/private/raw/path", "BENCHMARK_EVENT"):
                self.assertNotIn(secret, text)
            calls = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
            runs = [args for args in calls if "--version" not in args]
            self.assertEqual(len(runs), 2)
            self.assertTrue(all("--prompt-tokens" not in args for args in runs))
            self.assertTrue(all(args[args.index("--n-predict") + 1] == "8" for args in runs))

    def test_fixed_lengths_and_failed_tool_suite_cannot_be_reported_as_pass(self):
        self._require_runner()
        with tempfile.TemporaryDirectory(prefix="issue12-benchmark-quality-") as tmp:
            directory = Path(tmp)
            runtime, log, _ = self._fake_runtime(directory)
            report = directory / "failed-evaluation.json"
            self._report(report, passed=False)
            output = directory / "failed-benchmark.json"
            result = self._run(CONFIG, TUNING, report, runtime, output, directory)
            self.assertNotEqual(result.returncode, 0)
            if output.is_file():
                artifact = json.loads(output.read_text())
                self.assertFalse(artifact["runs"][0]["quality"]["passed"])
                self.assertNotEqual(artifact["summary"]["status"], "pass")
            # Strict input validation rejects a failed Issue 11 suite before
            # starting the runtime; a failed report cannot become benchmark quality.
            self.assertFalse(log.is_file(), "failed quality input started the runtime")

    def test_timeout_kills_process_group_and_resume_is_fail_closed_on_tamper(self):
        self._require_runner()
        with tempfile.TemporaryDirectory(prefix="issue12-benchmark-safety-") as tmp:
            directory = Path(tmp)
            runtime, log, child_pid = self._fake_runtime(directory)
            report = directory / "evaluation.json"
            self._report(report)
            timed = directory / "timed.json"
            result = self._run(CONFIG, TUNING, report, runtime, timed, directory,
                               env={"FAKE_RUNTIME_MODE": "timeout", "FAKE_CHILD_PID": str(child_pid)})
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(child_pid.is_file(), "timeout fake did not start")
            child = int(child_pid.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)

            # A completed run must resume without invoking the fake again.
            good = directory / "good.json"
            normal = self._run(CONFIG, TUNING, report, runtime, good, directory)
            self.assertEqual(normal.returncode, 0, normal.stderr or normal.stdout)
            before = len(log.read_text().splitlines())
            resumed = self._run(CONFIG, TUNING, report, runtime, good, directory, "--resume")
            self.assertEqual(resumed.returncode, 0, resumed.stderr or resumed.stdout)
            self.assertEqual(len(log.read_text().splitlines()), before)

            data = json.loads(good.read_text())
            data["runs"][0]["metrics"]["ttft_ms"] = 999
            good.write_text(json.dumps(data))
            tampered = self._run(CONFIG, TUNING, report, runtime, good, directory, "--resume")
            self.assertNotEqual(tampered.returncode, 0)
            self.assertEqual(len(log.read_text().splitlines()), before)


if __name__ == "__main__":
    unittest.main()
