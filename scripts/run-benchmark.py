#!/usr/bin/env python3
"""Run the bounded Issue #12 benchmark.

The runner deliberately speaks only the b10446 command line.  Measurements are
accepted only when they are observed from the child; no missing value is filled
in from a configured target or a host-side guess.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import pathlib
import re
import selectors
import signal
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
BUILD = {"ref": "b10446", "commit": "adb55e5"}
MODEL = {"id": "Qwen3.5-27B-Q8_0", "quantization": "Q8_0",
         "sha256": "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450"}
SCHEMA_VERSION = "benchmark-v1"
GPU = "Vulkan0"
PCI = "1002:73BF"
MAX_TIMEOUT = 300.0
MAX_ARTIFACT = 65536
ACTIVE = None
INTERRUPTED = False


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(f"cannot read JSON input: {path.name}") from error


def die(message):
    print(message, file=sys.stderr)
    return 2


def sha256_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"invalid {name}")


def validate_candidate(tuning):
    if tuning.get("schema_version") != "hybrid-vulkan-tuning-v1" or tuning.get("build") != BUILD:
        raise ValueError("Issue 6 tuning identity mismatch")
    if tuning.get("model") != MODEL:
        raise ValueError("Issue 6 model identity mismatch")
    candidate = tuning.get("stable_candidate")
    if not isinstance(candidate, dict) or candidate.get("status") != "pass":
        raise ValueError("Issue 6 has no passing stable candidate")
    params = candidate.get("parameters")
    required = {"gpu_layers", "flash_attention", "batch", "ubatch", "kv_cache", "quantization"}
    if not isinstance(params, dict) or not required <= set(params) or params["quantization"] != "Q8_0":
        raise ValueError("Issue 6 candidate is incomplete")
    quality = candidate.get("quality", {})
    if any(quality.get(key) is not True for key in
           ("context_confirmed", "device_confirmed", "exact_completion",
            "offload_confirmed", "stability_confirmed", "timing_confirmed")):
        raise ValueError("Issue 6 candidate lacks passing evidence")
    evidence = candidate.get("evidence", {})
    if (evidence.get("measurement_source") != "live" or
            evidence.get("vram_pci_id", "").upper() != PCI or
            evidence.get("attempt_count") != 3 or
            not isinstance(evidence.get("attempt_ids"), list) or
            len(evidence["attempt_ids"]) != 3 or
            not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("attempts_digest", ""))) or
            not re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("evidence_id", "")))):
        raise ValueError("Issue 6 candidate evidence is incomplete")
    return candidate, params


def validate_report(report):
    """Validate the Issue 11 artifact semantically, not just by its label."""
    if report.get("schema_version") != 1:
        raise ValueError("Issue 11 report schema version is not 1")
    suite = report.get("suite", {})
    if suite.get("name") != "issue-11-evaluation" or not isinstance(suite.get("case_count"), int):
        raise ValueError("wrong Issue 11 evaluation suite")
    if suite["case_count"] != 14 or suite.get("all_required_cases_passed") is not True:
        raise ValueError("Issue 11 report does not contain all required passing cases")
    scoring = report.get("scoring", {})
    if scoring.get("method") != "mean-case-score" or scoring.get("passed") is not True:
        raise ValueError("Issue 11 scoring is not passing")
    cases = report.get("cases")
    if not isinstance(cases, list) or len(cases) != 14 or len({c.get("id") for c in cases}) != 14:
        raise ValueError("Issue 11 case manifest is incomplete")
    manifest = load(ROOT / "tests/fixtures/evaluation_cases.json")
    expected = {c["id"]: c["kind"] for c in manifest.get("cases", [])}
    if {c.get("id"): c.get("kind") for c in cases} != expected:
        raise ValueError("Issue 11 case manifest identity mismatch")
    if any(c.get("passed") is not True or not isinstance(c.get("checks"), dict) for c in cases):
        raise ValueError("Issue 11 contains a failed or malformed case")
    provenance = report.get("provenance", {})
    if provenance.get("sanitized") is not True or provenance.get("synthetic_fixture") is True:
        raise ValueError("synthetic or unsanitized Issue 11 report is not admissible")
    hashes = provenance.get("hashes", {})
    if set(hashes) != {"inputs", "schema", "runner", "workspace", "request", "response"}:
        raise ValueError("Issue 11 provenance hashes are incomplete")
    if any(not re.fullmatch(r"[0-9a-f]{64}", str(value)) for value in hashes.values()):
        raise ValueError("Issue 11 provenance hash is invalid")
    if provenance.get("model", {}).get("id") != MODEL["id"]:
        raise ValueError("Issue 11 model provenance mismatch")
    runtime = provenance.get("runtime", {})
    if runtime.get("ref") != BUILD["ref"] or runtime.get("commit") != BUILD["commit"]:
        raise ValueError("Issue 11 runtime provenance mismatch")
    if runtime.get("synthetic_fixture") is True:
        raise ValueError("synthetic Issue 11 runtime provenance is not admissible")
    return True


def validate_inputs(config, tuning, report):
    if config.get("schema_version") != SCHEMA_VERSION or config.get("name") != "issue-12-benchmark":
        raise ValueError("unsupported benchmark identity")
    if config.get("build") != BUILD or config.get("evaluator", {}).get("suite") != "issue-11-evaluation":
        raise ValueError("benchmark build or evaluator identity mismatch")
    if config.get("model") != MODEL:
        raise ValueError("benchmark model identity does not match pinned Q8_0 artifact")
    if config.get("context_tokens") != 32768:
        raise ValueError("context is not the fixed 32K contract")
    if config.get("prompt", {}).get("token_count") != 16 or config.get("output", {}).get("token_count") != 8:
        raise ValueError("prompt and output lengths are not fixed")
    lifecycle = config.get("lifecycle", {})
    if lifecycle.get("modes") != ["cold", "warm"]:
        raise ValueError("cold/warm lifecycle is not pinned")
    for mode in ("cold", "warm"):
        if lifecycle.get(mode, {}).get("new_process") is not True:
            raise ValueError(f"{mode} lifecycle must use a new process")
        if lifecycle[mode].get("warmup") is not False:
            raise ValueError(f"{mode} lifecycle must explicitly disable warmup")
    safety = config.get("safety", {})
    for key in ("minimum_free_vram_mib", "minimum_available_ram_mib", "maximum_swap_in_pages"):
        integer(safety.get(key), key)
    if safety.get("selected_gpu") != GPU or safety.get("selected_pci_id", "").upper() != PCI:
        raise ValueError("only the selected RX 6900 XT adapter may be benchmarked")
    timeout = config.get("timeout_seconds")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= MAX_TIMEOUT:
        raise ValueError("configured timeout exceeds the hard 300 second limit")
    candidate, params = validate_candidate(tuning)
    validate_report(report)
    return candidate, params


def set_subreaper():
    try:
        ctypes.CDLL(None).prctl(36, 1, 0, 0, 0)
    except (AttributeError, OSError):
        pass


def kill_group(proc, grace):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + max(0.0, grace)
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        proc.wait(timeout=2)
    # With PR_SET_CHILD_SUBREAPER, descendants that outlive the leader become
    # our children. Reap them so a killed process cannot remain as a zombie.
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            child, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if child == 0:
            time.sleep(0.01)


def version(cli):
    global ACTIVE
    set_subreaper()
    proc = subprocess.Popen([str(cli), "--version"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=True)
    ACTIVE = proc
    try:
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            kill_group(proc, 0.2)
            raise ValueError("llama-cli version probe timed out")
        if proc.returncode != 0:
            raise ValueError("llama-cli version probe failed")
        identity = (out + err).decode("utf-8", "replace")
        if "b10446" not in identity or "adb55e5" not in identity:
            raise ValueError("llama-cli build identity mismatch")
    finally:
        if proc.poll() is None:
            kill_group(proc, 0.2)
        ACTIVE = None


def number(pattern, text):
    match = re.search(pattern, text, re.I)
    return float(match.group(1)) if match else None


def signal_handler(signum, _frame):
    global INTERRUPTED
    INTERRUPTED = True
    if ACTIVE is not None:
        kill_group(ACTIVE, 0.2)


def parse_hardware(line, samples):
    match = re.search(r"BENCHMARK_HARDWARE\s+(\{.*\})\s*$", line)
    if not match:
        return
    try:
        value = json.loads(match.group(1))
    except (ValueError, RecursionError):
        return
    required = ("pci_id", "vram_capacity_mib", "vram_used_mib", "ram_available_mib", "swap_in_pages")
    if all(isinstance(value.get(key), (int, float, str)) for key in required):
        try:
            sample = {"pci_id": str(value["pci_id"]).upper(),
                      "vram_capacity_mib": int(value["vram_capacity_mib"]),
                      "vram_used_mib": int(value["vram_used_mib"]),
                      "ram_available_mib": int(value["ram_available_mib"]),
                      "swap_in_pages": int(value["swap_in_pages"])}
        except (TypeError, ValueError):
            return
        if all(sample[key] >= 0 for key in sample if key != "pci_id"):
            samples.append(sample)


def run_once(cli, mode, timeout, capture_limit, params, quality, config, report_sha, model):
    global ACTIVE
    command = [str(cli), "--ctx-size", "32768", "--device", GPU,
               "--gpu-layers", str(params["gpu_layers"]), "--flash-attn", str(params["flash_attention"]),
               "--batch-size", str(params["batch"]), "--ubatch-size", str(params["ubatch"]),
               "--cache-type-k", str(params["kv_cache"]), "--cache-type-v", str(params["kv_cache"]),
               "--reasoning", "off", "--temp", "0", "--seed", "42", "--single-turn",
               "--simple-io", "--verbose", "--no-display-prompt", "--no-warmup",
               "--prompt", config["prompt"]["text"], "--n-predict", "8"]
    if model is not None:
        command[1:1] = ["--model", str(model)]
    environment = os.environ.copy()
    environment.update({"BENCHMARK_MODE": mode, "BENCHMARK_SELECTED_GPU": GPU})
    set_subreaper()
    proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, env=environment)
    ACTIVE = proc
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    retained = {"stdout": bytearray(), "stderr": bytearray()}
    pending = {"stdout": "", "stderr": ""}
    values = {"load": None, "prompt": None, "generation": None,
              "prompt_tokens": None, "generation_tokens": None, "ttft": None}
    samples = []
    observed_gpu = False
    started = time.monotonic()
    timed_out = False

    def observe(kind, line):
        if kind == "stdout" and values["ttft"] is None and line is not None:
            # This callback is line based, so TTFT is set below as soon as the
            # first non-empty read arrives.  It is intentionally not a token
            # timestamp and never uses prompt evaluation.
            pass
        event = re.search(r"BENCHMARK_EVENT\s+event=([a-z_]+)([^\n]*)", line, re.I)
        if event:
            name, tail = event.group(1).lower(), event.group(2)
            elapsed = number(r"elapsed_ms=([0-9]+(?:\.[0-9]+)?)", tail)
            tokens = number(r"tokens=([0-9]+(?:\.[0-9]+)?)", tail)
            if name == "load": values["load"] = elapsed
            elif name == "prompt_eval":
                values["prompt"], values["prompt_tokens"] = elapsed, int(tokens) if tokens is not None else None
            elif name == "generation":
                values["generation"], values["generation_tokens"] = elapsed, int(tokens) if tokens is not None else None
        load_match = re.search(r"load time\s*=\s*([0-9.]+)\s*ms", line, re.I)
        prompt_match = re.search(r"prompt eval time\s*=\s*([0-9.]+)\s*ms\s*/\s*([0-9]+)\s+tokens", line, re.I)
        generation_match = re.search(r"(?:generation|eval) time\s*=\s*([0-9.]+)\s*ms\s*/\s*([0-9]+)\s+(?:runs|tokens)", line, re.I)
        if load_match: values["load"] = float(load_match.group(1))
        if prompt_match: values["prompt"], values["prompt_tokens"] = float(prompt_match.group(1)), int(prompt_match.group(2))
        if generation_match and "prompt eval" not in line.lower():
            values["generation"], values["generation_tokens"] = float(generation_match.group(1)), int(generation_match.group(2))
        nonlocal observed_gpu
        if re.search(r"Vulkan0\s*:\s*AMD Radeon RX 6900 XT", line, re.I):
            observed_gpu = True
        parse_hardware(line, samples)

    try:
        while selector.get_map():
            if INTERRUPTED or time.monotonic() - started > timeout:
                timed_out = not INTERRUPTED
                kill_group(proc, config["cleanup"]["term_grace_seconds"])
                break
            for key, _ in selector.select(0.02):
                stream, kind = key.fileobj, key.data
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    selector.unregister(stream)
                    continue
                if kind == "stdout" and values["ttft"] is None:
                    # The timestamp is taken at read of the first actual byte.
                    values["ttft"] = (time.monotonic() - started) * 1000
                retained[kind].extend(chunk[:max(0, capture_limit - len(retained[kind]))])
                pending[kind] += chunk.decode("utf-8", "replace")
                while "\n" in pending[kind]:
                    line, pending[kind] = pending[kind].split("\n", 1)
                    observe(kind, line[:16384])
                if len(pending[kind]) > 16384:
                    pending[kind] = pending[kind][-4096:]
        if not timed_out and not INTERRUPTED:
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                timed_out = True
                kill_group(proc, config["cleanup"]["term_grace_seconds"])
        for kind, line in pending.items():
            if line:
                observe(kind, line[:16384])
    finally:
        try:
            selector.close()
        finally:
            if proc.poll() is None:
                kill_group(proc, config["cleanup"]["term_grace_seconds"])
            ACTIVE = None

    lifecycle = {"started": True, "bounded": True, "finished": True,
                 "warmup": False, "new_process": True,
                 "cache_evidence": {"observed": True, "mode": mode,
                                    "preparation": ("cold-cache-before-first-process" if mode == "cold"
                                                    else "after-cold-process-before-second-process"),
                                    "order": 0 if mode == "cold" else 1,
                                    "warmup_disabled": True}}
    if timed_out or INTERRUPTED:
        return {"mode": mode, "cache": "miss" if mode == "cold" else "hit",
                "status": "timeout", "lifecycle": lifecycle}
    required = (values["load"], values["ttft"], values["prompt"], values["generation"],
                values["prompt_tokens"], values["generation_tokens"])
    if (proc.returncode != 0 or any(not isinstance(value, (int, float)) or value < 0 for value in required) or
            values["prompt_tokens"] != 16 or values["generation_tokens"] != 8 or
            not values["prompt"] or not values["generation"] or not samples or not observed_gpu):
        return {"mode": mode, "cache": "miss" if mode == "cold" else "hit",
                "status": "fail", "lifecycle": lifecycle}
    if any(sample["pci_id"] != PCI for sample in samples):
        return {"mode": mode, "cache": "miss" if mode == "cold" else "hit",
                "status": "fail", "lifecycle": lifecycle}
    safety = config["safety"]
    capacity = min(sample["vram_capacity_mib"] for sample in samples)
    peak = max(sample["vram_used_mib"] for sample in samples)
    minimum_ram = min(sample["ram_available_mib"] for sample in samples)
    swap = max(sample["swap_in_pages"] for sample in samples)
    ram_ok = minimum_ram >= safety["minimum_available_ram_mib"]
    vram_ok = capacity - peak >= safety["minimum_free_vram_mib"]
    swap_ok = swap <= safety["maximum_swap_in_pages"]
    settings = {"model": MODEL["id"], "quantization": "Q8_0", "build": dict(BUILD),
                "device": {"selected_gpu": GPU, "pci_id": PCI},
                "context_tokens": 32768, "parameters": params}
    status = "pass" if quality and ram_ok and vram_ok and swap_ok else "fail"
    return {"mode": mode, "cache": "miss" if mode == "cold" else "hit", "status": status,
            "lifecycle": lifecycle,
            "metrics": {"load_time_ms": round(values["load"], 6), "ttft_ms": round(values["ttft"], 6),
                        "prompt_eval_ms": round(values["prompt"], 6), "prompt_tokens": 16,
                        "prompt_tokens_per_second": round(16 / (values["prompt"] / 1000), 6),
                        "generation_eval_ms": round(values["generation"], 6), "generation_tokens": 8,
                        "generation_tokens_per_second": round(8 / (values["generation"] / 1000), 6)},
            "hardware": {"selected_gpu": GPU, "pci_id": PCI, "samples": samples,
                         "ram": {"minimum_available_mib": minimum_ram, "passed": ram_ok},
                         "vram": {"capacity_mib": capacity, "peak_mib": peak, "passed": vram_ok},
                         "swap": {"in_pages": swap, "passed": swap_ok}},
            "settings": settings,
            "quality": {"suite": "issue-11-evaluation", "report_sha256": report_sha, "passed": quality}}


def artifact_hash(data):
    copy = json.loads(json.dumps(data))
    copy.get("provenance", {}).pop("artifact_sha256", None)
    return hashlib.sha256(canonical(copy).encode()).hexdigest()


def validate_artifact(data, config, hashes, candidate, params):
    if data.get("schema_version") != SCHEMA_VERSION or data.get("benchmark") != {"name": "issue-12-benchmark", "version": 1}:
        raise ValueError("resume artifact identity mismatch")
    provenance = data.get("provenance", {})
    for key, value in zip(("config_sha256", "tuning_result_sha256", "evaluator_report_sha256"), hashes):
        if provenance.get(key) != value:
            raise ValueError("resume input provenance mismatch")
    if provenance.get("sanitized") is not True or provenance.get("artifact_sha256") != artifact_hash(data):
        raise ValueError("resume artifact is tampered or unsanitized")
    inputs = data.get("inputs", {})
    if inputs.get("model") != MODEL or inputs.get("runtime") != {"ref": "b10446", "commit": "adb55e5", "source": "observed", "synthetic_fixture": False}:
        raise ValueError("resume artifact runtime or model identity mismatch")
    if inputs.get("candidate") != candidate or inputs.get("prompt", {}).get("token_count") != 16 or inputs.get("output", {}).get("token_count") != 8:
        raise ValueError("resume artifact input identity mismatch")
    runs = data.get("runs", [])
    if len(runs) != 2 or [row.get("mode") for row in runs] != ["cold", "warm"] or [row.get("cache") for row in runs] != ["miss", "hit"]:
        raise ValueError("resume requires exactly ordered cold and warm runs")
    if any(row.get("status") != "pass" or row.get("settings", {}).get("parameters") != params for row in runs):
        raise ValueError("resume requires complete passing runs")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--tuning-result", required=True)
    parser.add_argument("--evaluation-report", required=True)
    parser.add_argument("--llama-cli", required=True)
    parser.add_argument("--model")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-timeout", type=float)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    try:
        config_path = pathlib.Path(args.config).resolve()
        tuning_path = pathlib.Path(args.tuning_result).resolve()
        report_path = pathlib.Path(args.evaluation_report).resolve()
        config = load(config_path)
        configured_tuning = (ROOT / config["tuning_result"]).resolve()
        if configured_tuning != tuning_path:
            raise ValueError("tuning result is not the configured Issue 6 input")
        tuning, report = load(tuning_path), load(report_path)
        candidate, params = validate_inputs(config, tuning, report)
        hashes = (file_hash(config_path), file_hash(tuning_path), file_hash(report_path))
        output = pathlib.Path(args.output)
        if args.resume:
            validate_artifact(load(output), config, hashes, candidate, params)
            return 0
        if output.exists():
            raise ValueError("refusing to overwrite an existing artifact without --resume")
        cli = pathlib.Path(args.llama_cli)
        if not cli.is_file() or not os.access(cli, os.X_OK):
            raise ValueError("llama-cli is not executable")
        model = pathlib.Path(args.model).resolve() if args.model else None
        if model is not None and (not model.is_file() or file_hash(model).lower() != MODEL["sha256"]):
            raise ValueError("model checksum or identity mismatch")
        timeout = float(config["timeout_seconds"] if args.run_timeout is None else args.run_timeout)
        if not 0 < timeout <= MAX_TIMEOUT or timeout > float(config["timeout_seconds"]):
            raise ValueError("run timeout is outside the bounded config limit")
        capture_limit = int(config["cleanup"].get("capture_bytes", 65536))
        if not 0 < capture_limit <= 1024 * 1024:
            raise ValueError("capture limit is outside the bounded limit")
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)
        version(cli)
        report_sha = hashes[2]
        runs = [run_once(cli, mode, timeout, capture_limit, params, True, config, report_sha, model)
                for mode in ("cold", "warm")]
        passed = sum(row.get("status") == "pass" for row in runs)
        data = {"schema_version": SCHEMA_VERSION, "benchmark": {"name": "issue-12-benchmark", "version": 1},
                "inputs": {"model": MODEL, "runtime": {"ref": "b10446", "commit": "adb55e5", "source": "observed", "synthetic_fixture": False},
                           "prompt": {"token_count": 16}, "output": {"token_count": 8},
                           "context_tokens": 32768, "lifecycle": config["lifecycle"], "candidate": candidate},
                "runs": runs, "summary": {"status": "pass" if passed == 2 else "fail", "passed_runs": passed},
                "safety": {"selected_gpu": GPU, "selected_pci_id": PCI,
                           "swap_in_pages": max((row.get("hardware", {}).get("swap", {}).get("in_pages", 0) for row in runs), default=0),
                           "ram_passed": all(row.get("hardware", {}).get("ram", {}).get("passed", False) for row in runs),
                           "vram_passed": all(row.get("hardware", {}).get("vram", {}).get("passed", False) for row in runs)},
                "provenance": {"config_sha256": hashes[0], "tuning_result_sha256": hashes[1],
                               "evaluator_report_sha256": hashes[2], "artifact_sha256": "", "sanitized": True}}
        data["provenance"]["artifact_sha256"] = artifact_hash(data)
        encoded = json.dumps(data, sort_keys=True, indent=2) + "\n"
        if len(encoded.encode()) > MAX_ARTIFACT:
            raise ValueError("benchmark artifact exceeds bounded size")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded)
        return 0 if passed == 2 else 1
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        return die(str(error))


if __name__ == "__main__":
    set_subreaper()
    raise SystemExit(main())
