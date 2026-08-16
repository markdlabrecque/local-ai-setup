"""Targeted red contracts for Issue #12 review regressions I12-001..009.

The fake is deliberately b10446-shaped and streams stdout.  It is not a
model, and the tests never permit its output to masquerade as live evidence.
"""
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
CONFIG = ROOT / "config" / "benchmark.json"
SCHEMA = ROOT / "schemas" / "benchmark-result.schema.json"
TUNING = ROOT / "docs" / "issue-6-hybrid-vulkan-tuning-result.json"
CASES = ROOT / "tests" / "fixtures" / "evaluation_cases.json"
RUNNER = Path(os.environ.get("BENCHMARK_RUNNER", ROOT / "scripts" / "run-benchmark.sh"))


class Issue12ReviewRegressions(unittest.TestCase):
    maxDiff = None

    def fake_runtime(self, directory):
        runtime = Path(directory) / "b10446-fake-runtime.py"
        runtime.write_text(textwrap.dedent(r'''
            #!/usr/bin/env python3
            import json, os, signal, subprocess, sys, time
            args = sys.argv[1:]
            log = os.environ.get("FAKE_RUNTIME_LOG")
            if log:
                with open(log, "a") as stream:
                    stream.write(json.dumps({"argv": args}) + "\n")
            if "--version" in args:
                print("llama-cli version b10446 (adb55e5)")
                raise SystemExit(0)
            # b10446 has no --prompt-tokens option.  A benchmark must derive
            # the observed prompt count from the runtime's perf line.
            if "--prompt-tokens" in args:
                print("error: unknown argument --prompt-tokens", file=sys.stderr)
                raise SystemExit(64)
            if os.environ.get("FAKE_RUNTIME_MODE") == "timeout":
                child = subprocess.Popen(["sh", "-c", "trap '' TERM INT; while :; do sleep 1; done"])
                with open(os.environ["FAKE_CHILD_PID"], "w") as stream:
                    stream.write(str(child.pid))
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                while True:
                    time.sleep(1)
            if os.environ.get("FAKE_RUNTIME_MODE") == "bad-output":
                print("llama_context: n_ctx = not-a-number", file=sys.stderr)
                raise SystemExit(0)
            if os.environ.get("FAKE_RUNTIME_MODE") != "no-gpu":
                print("Vulkan0 : AMD Radeon RX 6900 XT (RADV NAVI21) (16368 MiB, 15404 MiB free)", file=sys.stderr)
                print("BENCHMARK_HARDWARE {\"pci_id\":\"1002:73BF\",\"card\":\"card1\",\"vram_capacity_mib\":16368,\"vram_used_mib\":8520,\"ram_available_mib\":50000,\"swap_in_pages\":0}", file=sys.stderr)
                print("BENCHMARK_HARDWARE {\"pci_id\":\"1002:73BF\",\"card\":\"card1\",\"vram_capacity_mib\":16368,\"vram_used_mib\":8530,\"ram_available_mib\":49990,\"swap_in_pages\":0}", file=sys.stderr)
            print("llama_context: n_ctx = 32768", file=sys.stderr)
            print("load time = 1250.00 ms", file=sys.stderr)
            print("llama_perf_context_print: prompt eval time = 1000.00 ms / 16 tokens (62.50 ms per token, 16.00 tokens per second)", file=sys.stderr)
            # TTFT is deliberately before the prompt-eval timestamp and is
            # observable only by reading the first stdout byte.
            time.sleep(0.02)
            sys.stdout.write("L")
            sys.stdout.flush()
            time.sleep(0.01)
            sys.stdout.write("OCAL_AI_BENCHMARK_OK")
            sys.stdout.flush()
            print("", file=sys.stdout, flush=True)
            print("llama_perf_context_print: eval time = 500.00 ms / 8 runs (62.50 ms per token, 16.00 tokens per second)", file=sys.stderr)
            print("RAW_LOG_SECRET bearer TOPSECRET /private/raw/path", file=sys.stderr)
        ''').lstrip())
        runtime.chmod(0o755)
        return runtime

    def evaluation_report(self, path, *, synthetic=False, passed=True):
        manifest = json.loads(CASES.read_text())
        cases = [{"id": row["id"], "kind": row["kind"], "passed": passed,
                  "score": 1 if passed else 0,
                  "checks": {"measured": True}}
                 for row in manifest["cases"]]
        report = {
            "schema_version": 1,
            "suite": {"name": "issue-11-evaluation", "case_count": len(cases),
                       "context_target_tokens": 32000,
                       "all_required_cases_passed": passed, "score": 1 if passed else 0},
            "cases": cases,
            "scoring": {"method": "mean-case-score", "score": 1 if passed else 0,
                        "passed": passed},
            "provenance": {
                "request_id": "observed-evaluation-request",
                "transcript": [], "sanitized": True,
                "synthetic_fixture": synthetic,
                "model": {"id": "Qwen3.5-27B-Q8_0", "source": "verified-live"},
                "runtime": {"ref": "b10446", "commit": "adb55e5", "source": "observed"},
                "hashes": {name: hashlib.sha256((name + "-observed").encode()).hexdigest()
                           for name in ("inputs", "schema", "runner", "workspace", "request", "response")},
            },
            "safety": {"workspace_unchanged": True, "sandboxed": True,
                       "bounded_artifacts": True, "model_commands_executed": False},
        }
        path.write_text(json.dumps(report, sort_keys=True, separators=(",", ":")))
        return path

    def run_benchmark(self, config, tuning, report, runtime, output, directory,
                      *extra, env=None, timeout_arg="2"):
        command = [str(RUNNER), "--config", str(config), "--tuning-result", str(tuning),
                   "--evaluation-report", str(report), "--llama-cli", str(runtime),
                   "--output", str(output)]
        if timeout_arg is not None:
            command += ["--run-timeout", timeout_arg]
        command += list(extra)
        merged = os.environ.copy()
        merged.update({"FAKE_RUNTIME_LOG": str(Path(directory) / "runtime.log")})
        if env:
            merged.update(env)
        try:
            return subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                                  timeout=15, env=merged)
        except FileNotFoundError as error:
            return subprocess.CompletedProcess(command, 127, "",
                                               f"missing benchmark entrypoint: {error}")

    def setUp(self):
        self.assertTrue(RUNNER.is_file(), f"missing benchmark runner: {RUNNER}")
        self.assertTrue(SCHEMA.is_file(), f"missing benchmark schema: {SCHEMA}")

    def test_i12_001_b10446_streaming_rejects_prompt_tokens_and_separates_observations(self):
        """The unsupported b10446 flag must not replace first-byte TTFT."""
        with tempfile.TemporaryDirectory(prefix="i12-001-") as tmp:
            root = Path(tmp)
            runtime = self.fake_runtime(root)
            report = self.evaluation_report(root / "evaluation.json")
            output = root / "result.json"
            result = self.run_benchmark(CONFIG, TUNING, report, runtime, output, root)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            artifact = json.loads(output.read_text())
            self.assertEqual([row["mode"] for row in artifact["runs"]], ["cold", "warm"])
            for row in artifact["runs"]:
                metrics = row["metrics"]
                self.assertEqual(metrics["prompt_tokens"], 16)
                self.assertEqual(metrics["generation_tokens"], 8)
                self.assertEqual(metrics["prompt_eval_ms"], 1000.0)
                self.assertEqual(metrics["generation_eval_ms"], 500.0)
                self.assertGreater(metrics["ttft_ms"], 0)
                self.assertLess(metrics["ttft_ms"], 100.0)
                self.assertNotEqual(metrics["ttft_ms"], metrics["prompt_eval_ms"])
            calls = [json.loads(line)["argv"] for line in (root / "runtime.log").read_text().splitlines()]
            runs = [args for args in calls if "--version" not in args]
            self.assertEqual(len(runs), 2)
            self.assertTrue(all("--prompt-tokens" not in args for args in runs))

    def test_i12_002_cold_warm_evidence_is_ordered_and_no_warmup_is_explicit(self):
        with tempfile.TemporaryDirectory(prefix="i12-002-") as tmp:
            root = Path(tmp)
            config = json.loads(CONFIG.read_text())
            config["lifecycle"]["cold"]["warmup"] = False
            config["lifecycle"]["warm"]["warmup"] = False
            local_config = root / "benchmark.json"
            local_config.write_text(json.dumps(config))
            runtime = self.fake_runtime(root)
            report = self.evaluation_report(root / "evaluation.json")
            output = root / "result.json"
            result = self.run_benchmark(local_config, TUNING, report, runtime, output, root)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            artifact = json.loads(output.read_text())
            self.assertEqual([(r["mode"], r["cache"]) for r in artifact["runs"]],
                             [("cold", "miss"), ("warm", "hit")])
            self.assertEqual(artifact["inputs"]["lifecycle"]["warmup"], False)
            for row in artifact["runs"]:
                self.assertIs(row["lifecycle"]["warmup"], False)
                self.assertTrue(row["lifecycle"]["cache_evidence"])
                self.assertTrue(row["lifecycle"]["new_process"])

    def test_i12_003_missing_or_non_rx6900_live_samples_fail_closed(self):
        with tempfile.TemporaryDirectory(prefix="i12-003-") as tmp:
            root = Path(tmp)
            runtime = self.fake_runtime(root)
            report = self.evaluation_report(root / "evaluation.json")
            absent = self.run_benchmark(CONFIG, TUNING, report, runtime, root / "absent.json",
                                        root, env={"FAKE_RUNTIME_MODE": "no-gpu"})
            self.assertNotEqual(absent.returncode, 0)
            good = self.run_benchmark(CONFIG, TUNING, report, runtime, root / "good.json", root)
            self.assertEqual(good.returncode, 0, good.stderr or good.stdout)
            artifact = json.loads((root / "good.json").read_text())
            for row in artifact["runs"]:
                hardware = row["hardware"]
                self.assertEqual(hardware["selected_gpu"], "Vulkan0")
                self.assertEqual(hardware["pci_id"], "1002:73BF")
                self.assertGreaterEqual(len(hardware["samples"]), 2)
                self.assertTrue(all(sample["pci_id"] == "1002:73BF" for sample in hardware["samples"]))
                self.assertTrue(all("ram_available_mib" in sample and "swap_in_pages" in sample
                                    for sample in hardware["samples"]))

    def test_i12_004_issue6_selected_row_has_complete_passing_evidence_and_identity(self):
        with tempfile.TemporaryDirectory(prefix="i12-004-") as tmp:
            root = Path(tmp)
            runtime = self.fake_runtime(root)
            report = self.evaluation_report(root / "evaluation.json")
            output = root / "result.json"
            result = self.run_benchmark(CONFIG, TUNING, report, runtime, output, root)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            candidate = json.loads(output.read_text())["inputs"]["candidate"]
            self.assertEqual(candidate["status"], "pass")
            self.assertTrue(candidate["tuple_id"])
            self.assertEqual(candidate["parameters"]["quantization"], "Q8_0")
            self.assertEqual(candidate["parameters"]["gpu_layers"], 20)
            self.assertTrue(candidate["parameters"]["flash_attention"] == "on")
            for key in ("context_confirmed", "device_confirmed", "exact_completion",
                        "offload_confirmed", "stability_confirmed", "timing_confirmed"):
                self.assertIs(candidate["quality"][key], True)
            self.assertEqual(candidate["evidence"]["measurement_source"], "live")
            self.assertEqual(candidate["evidence"]["vram_pci_id"], "1002:73BF")
            self.assertEqual(candidate["evidence"]["attempt_count"], 3)
            self.assertEqual(candidate["evidence"]["attempt_ids"].__len__(), 3)
            self.assertRegex(candidate["evidence"]["attempts_digest"], r"^[0-9a-f]{64}$")
            self.assertRegex(candidate["evidence_id"], r"^[0-9a-f]{64}$")

    def test_i12_005_issue11_schema_semantics_hash_and_provenance_are_required(self):
        with tempfile.TemporaryDirectory(prefix="i12-005-") as tmp:
            root = Path(tmp)
            runtime = self.fake_runtime(root)
            report = self.evaluation_report(root / "evaluation.json", synthetic=True)
            result = self.run_benchmark(CONFIG, TUNING, report, runtime, root / "result.json", root)
            self.assertNotEqual(result.returncode, 0,
                                "a synthetic Issue 11 fixture must not be accepted as live quality evidence")
            report = self.evaluation_report(root / "real.json")
            result = self.run_benchmark(CONFIG, TUNING, report, runtime, root / "real-result.json", root)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            artifact = json.loads((root / "real-result.json").read_text())
            quality = artifact["runs"][0]["quality"]
            self.assertEqual(quality["suite"], "issue-11-evaluation")
            self.assertRegex(quality["report_sha256"], r"^[0-9a-f]{64}$")
            self.assertTrue(quality["passed"])
            provenance = artifact["provenance"]
            for name in ("config_sha256", "tuning_result_sha256", "evaluator_report_sha256"):
                self.assertRegex(provenance[name], r"^[0-9a-f]{64}$")
            self.assertEqual(artifact["inputs"]["model"]["id"], "Qwen3.5-27B-Q8_0")
            self.assertEqual(artifact["inputs"]["runtime"]["commit"], "adb55e5")
            self.assertFalse(artifact["inputs"]["runtime"].get("synthetic_fixture", False))

    def test_i12_006_timeout_and_signals_kill_entire_process_group_with_grace(self):
        with tempfile.TemporaryDirectory(prefix="i12-006-") as tmp:
            root = Path(tmp)
            runtime = self.fake_runtime(root)
            report = self.evaluation_report(root / "evaluation.json")
            child_file = root / "child.pid"
            result = self.run_benchmark(CONFIG, TUNING, report, runtime, root / "timeout.json", root,
                                        env={"FAKE_RUNTIME_MODE": "timeout", "FAKE_CHILD_PID": str(child_file)})
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(child_file.exists())
            child = int(child_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            self.assertLess(result.stderr.count("RAW_LOG_SECRET"), 1)

            for signum in (signal.SIGTERM, signal.SIGINT):
                child_file.unlink(missing_ok=True)
                proc = subprocess.Popen(
                    [str(RUNNER), "--config", str(CONFIG), "--tuning-result", str(TUNING),
                     "--evaluation-report", str(report), "--llama-cli", str(runtime),
                     "--output", str(root / f"signal-{signum}.json"), "--run-timeout", "30"],
                    cwd=ROOT, env={**os.environ, "FAKE_RUNTIME_LOG": str(root / "runtime.log"),
                                  "FAKE_RUNTIME_MODE": "timeout", "FAKE_CHILD_PID": str(child_file)},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    start_new_session=True)
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline and not child_file.exists():
                    time.sleep(0.02)
                self.assertTrue(child_file.exists())
                child = int(child_file.read_text())
                proc.send_signal(signum)
                proc.wait(timeout=3)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child, 0)

    def test_i12_007_runtime_and_result_schema_require_exact_two_ordered_runs(self):
        schema = json.loads(SCHEMA.read_text())
        self.assertEqual(schema["properties"]["runs"]["minItems"], 2)
        self.assertEqual(schema["properties"]["runs"]["items"]["properties"]["mode"]["enum"],
                         ["cold", "warm"])
        with tempfile.TemporaryDirectory(prefix="i12-007-") as tmp:
            root = Path(tmp)
            runtime = self.fake_runtime(root)
            report = self.evaluation_report(root / "evaluation.json")
            result = self.run_benchmark(CONFIG, TUNING, report, runtime, root / "result.json", root)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            artifact = json.loads((root / "result.json").read_text())
            self.assertEqual(set(artifact), {"schema_version", "benchmark", "inputs", "runs",
                                             "summary", "safety", "provenance"})
            self.assertEqual(len(artifact["runs"]), 2)
            self.assertEqual([r["mode"] for r in artifact["runs"]], ["cold", "warm"])
            self.assertEqual([r["cache"] for r in artifact["runs"]], ["miss", "hit"])
            self.assertEqual(artifact["inputs"]["runtime"]["ref"], "b10446")
            self.assertEqual(artifact["inputs"]["runtime"]["commit"], "adb55e5")

    def test_i12_008_resume_checks_deep_identity_tamper_and_sanitizes_fixture_fields(self):
        with tempfile.TemporaryDirectory(prefix="i12-008-") as tmp:
            root = Path(tmp)
            runtime = self.fake_runtime(root)
            report = self.evaluation_report(root / "evaluation.json")
            output = root / "result.json"
            first = self.run_benchmark(CONFIG, TUNING, report, runtime, output, root)
            self.assertEqual(first.returncode, 0, first.stderr or first.stdout)
            before = len((root / "runtime.log").read_text().splitlines())
            resumed = self.run_benchmark(CONFIG, TUNING, report, runtime, output, root, "--resume")
            self.assertEqual(resumed.returncode, 0, resumed.stderr or resumed.stdout)
            self.assertEqual(len((root / "runtime.log").read_text().splitlines()), before)
            data = json.loads(output.read_text())
            data["runs"][1]["settings"]["parameters"]["batch"] = 999
            output.write_text(json.dumps(data))
            tampered = self.run_benchmark(CONFIG, TUNING, report, runtime, output, root, "--resume")
            self.assertNotEqual(tampered.returncode, 0)
            self.assertEqual(len((root / "runtime.log").read_text().splitlines()), before)
            text = output.read_text()
            for secret in ("RAW_LOG_SECRET", "TOPSECRET", "/private/raw/path", "BENCHMARK_HARDWARE"):
                self.assertNotIn(secret, text)
            self.assertTrue(json.loads(text)["provenance"]["sanitized"])

    def test_i12_009_timeout_defaults_match_docs_and_maximum_is_fail_closed(self):
        config = json.loads(CONFIG.read_text())
        self.assertEqual(config["timeout_seconds"], 120)
        self.assertLessEqual(config["timeout_seconds"], 300)
        docs = (ROOT / "docs" / "issue-12-benchmark.md").read_text()
        self.assertIn("120 seconds per lifecycle", docs)
        self.assertIn("hard\nlimit is 300 seconds", docs)
        with tempfile.TemporaryDirectory(prefix="i12-009-") as tmp:
            root = Path(tmp)
            runtime = self.fake_runtime(root)
            report = self.evaluation_report(root / "evaluation.json")
            too_long = self.run_benchmark(CONFIG, TUNING, report, runtime, root / "bad.json", root,
                                          "--run-timeout", "301", timeout_arg=None)
            self.assertNotEqual(too_long.returncode, 0)
            self.assertFalse((root / "runtime.log").exists(), "invalid timeout started runtime")


if __name__ == "__main__":
    unittest.main()
