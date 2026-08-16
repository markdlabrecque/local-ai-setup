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


def validate_json_schema(value, schema, root=None, path="$"):
    """Small dependency-free validator for the committed JSON contracts."""
    root = schema if root is None else root
    if "$ref" in schema:
        target = root
        for part in schema["$ref"][2:].split("/"):
            target = target[part]
        validate_json_schema(value, target, root, path)
        return
    for branch in schema.get("allOf", []):
        validate_json_schema(value, branch, root, path)
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"Issue 11 schema validation failed at {path}: const")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"Issue 11 schema validation failed at {path}: enum")
    kind = schema.get("type")
    valid = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }
    if kind in valid and not valid[kind]:
        raise ValueError(f"Issue 11 schema validation failed at {path}: type")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"Issue 11 schema validation failed at {path}: missing {key}")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError(f"Issue 11 schema validation failed at {path}: unknown property")
        for key, child in properties.items():
            if key in value:
                validate_json_schema(value[key], child, root, path + "." + key)
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or ("maxItems" in schema and len(value) > schema["maxItems"]):
            raise ValueError(f"Issue 11 schema validation failed at {path}: array bounds")
        for index, child in enumerate(schema.get("prefixItems", [])):
            if index < len(value):
                validate_json_schema(value[index], child, root, f"{path}[{index}]")
        items = schema.get("items")
        if isinstance(items, dict):
            start = len(schema.get("prefixItems", []))
            for index in range(start, len(value)):
                validate_json_schema(value[index], items, root, f"{path}[{index}]")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise ValueError(f"Issue 11 schema validation failed at {path}: string length")
        if "pattern" in schema and not re.fullmatch(schema["pattern"], value):
            raise ValueError(f"Issue 11 schema validation failed at {path}: pattern")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ValueError(f"Issue 11 schema validation failed at {path}: minimum")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise ValueError(f"Issue 11 schema validation failed at {path}: exclusive minimum")


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
    """Validate the complete Issue 11 schema, then its semantic contract."""
    schema = load(ROOT / "schemas" / "evaluation-report.schema.json")
    validate_json_schema(report, schema, schema)
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
    model = provenance.get("model", {})
    runtime = provenance.get("runtime", {})
    if (not isinstance(model.get("id"), str) or not re.fullmatch(r"[0-9a-f]{64}", str(model.get("sha256"))) or
            model.get("synthetic_fixture") is not False or
            not isinstance(runtime.get("ref"), str) or not isinstance(runtime.get("commit"), str) or
            runtime.get("synthetic_fixture") is not False):
        raise ValueError("Issue 11 model/runtime provenance is incomplete or synthetic")
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
    prompt = config.get("prompt", {})
    if (prompt.get("token_count") != 25 or prompt.get("observed_token_count") != 25 or
            prompt.get("tokenizer", {}).get("pinned") is not True or
            prompt.get("tokenizer", {}).get("preflight") is not True):
        raise ValueError("prompt tokenizer preflight/count is not the observed pinned contract")
    if config.get("output", {}).get("token_count") != 8:
        raise ValueError("output length is not fixed")
    lifecycle = config.get("lifecycle", {})
    if lifecycle.get("modes") != ["cold", "warm"]:
        raise ValueError("cold/warm lifecycle is not pinned")
    for mode in ("cold", "warm"):
        if lifecycle.get(mode, {}).get("new_process") is not True:
            raise ValueError(f"{mode} lifecycle must use a new process")
        if lifecycle[mode].get("warmup") is not False:
            raise ValueError(f"{mode} lifecycle must explicitly disable warmup")
    cold = lifecycle.get("cold", {})
    warm = lifecycle.get("warm", {})
    if (cold.get("checksum_before_preparation") is not True or
            cold.get("cache_preparation") not in {"page-cache-eviction", "deferred", "unsupported"} or
            cold.get("verifiable_or_deferred") is not True or
            warm.get("observed_after") != "cold" or warm.get("follows_observed_first_run") is not True):
        raise ValueError("cache lifecycle is not honest or ordered")
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


def _read_int(path):
    try:
        return int(path.read_text().strip(), 0)
    except (OSError, ValueError):
        return None


def drm_device(sysfs_root):
    """Select the exact PCI function from DRM sysfs, never runtime text."""
    root = pathlib.Path(sysfs_root)
    for card in sorted(root.glob("card*/device")):
        vendor = _read_int(card / "vendor")
        device = _read_int(card / "device")
        if vendor == 0x1002 and device == 0x73BF:
            return card
    return None


def proc_drm_sample(proc_root, sysfs_root):
    card = drm_device(sysfs_root)
    if card is None:
        return None
    try:
        meminfo = {}
        for line in (pathlib.Path(proc_root) / "meminfo").read_text().splitlines():
            key, _, rest = line.partition(":")
            fields = rest.strip().split()
            if fields:
                meminfo[key] = int(fields[0])
        vmstat = {}
        for line in (pathlib.Path(proc_root) / "vmstat").read_text().splitlines():
            fields = line.split()
            if len(fields) >= 2:
                vmstat[fields[0]] = int(fields[1])
        total = _read_int(card / "mem_info_vram_total")
        used = _read_int(card / "mem_info_vram_used")
        available_kib = meminfo.get("MemAvailable")
        swap_in = vmstat.get("pswpin")
        if None in (total, used, available_kib, swap_in) or min(total, used, available_kib, swap_in) < 0:
            return None
        return {"pci_id": PCI, "drm_device": "/dev/dri/" + card.parent.name,
                "source": "drm", "vram_capacity_mib": total // (1024 * 1024),
                "vram_used_mib": used // (1024 * 1024),
                "ram_available_mib": available_kib // 1024, "swap_in_pages": swap_in}
    except (OSError, ValueError):
        return None


def run_once(cli, mode, timeout, capture_limit, params, quality, config, report_sha, model,
             proc_root="/proc", sysfs_root="/sys/class/drm", cache_evidence=None):
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
    sample = proc_drm_sample(proc_root, sysfs_root)
    if sample is not None:
        samples.append(sample)
    last_sample = time.monotonic()
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

    try:
        while selector.get_map():
            if INTERRUPTED or time.monotonic() - started > timeout:
                timed_out = not INTERRUPTED
                kill_group(proc, config["cleanup"]["term_grace_seconds"])
                break
            now = time.monotonic()
            if now - last_sample >= 0.01:
                sample = proc_drm_sample(proc_root, sysfs_root)
                if sample is not None:
                    samples.append(sample)
                last_sample = now
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
        sample = proc_drm_sample(proc_root, sysfs_root)
        if sample is not None:
            samples.append(sample)
    finally:
        try:
            selector.close()
        finally:
            if proc.poll() is None:
                kill_group(proc, config["cleanup"]["term_grace_seconds"])
            ACTIVE = None

    evidence = dict(cache_evidence or {})
    evidence.setdefault("checksum_before_preparation", True)
    evidence.setdefault("preparation", "cold-cache-before-first-process" if mode == "cold" else "after-cold-process-before-second-process")
    evidence.setdefault("honest_status", "deferred" if mode == "cold" else "unsupported")
    evidence.update({"mode": mode, "order": 0 if mode == "cold" else 1,
                     "warmup_disabled": True})
    lifecycle = {"started": True, "bounded": True, "finished": True,
                 "warmup": False, "new_process": True,
                 "cache_evidence": evidence}
    if timed_out or INTERRUPTED:
        return {"mode": mode, "cache": "miss" if mode == "cold" else "hit",
                "status": "timeout", "lifecycle": lifecycle}
    required = (values["load"], values["ttft"], values["prompt"], values["generation"],
                values["prompt_tokens"], values["generation_tokens"])
    if (proc.returncode != 0 or any(not isinstance(value, (int, float)) or value < 0 for value in required) or
            values["prompt_tokens"] != config["prompt"]["observed_token_count"] or values["generation_tokens"] != 8 or
            not values["prompt"] or not values["generation"] or len(samples) < 2 or not observed_gpu):
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
    expected_cache_status = "verified-miss" if mode == "cold" else "verified-hit"
    cache_ok = lifecycle["cache_evidence"].get("honest_status") == expected_cache_status
    status = "pass" if quality and ram_ok and vram_ok and swap_ok and cache_ok else "fail"
    return {"mode": mode, "cache": "miss" if mode == "cold" else "hit", "status": status,
            "lifecycle": lifecycle,
            "metrics": {"load_time_ms": round(values["load"], 6), "ttft_ms": round(values["ttft"], 6),
                        "prompt_eval_ms": round(values["prompt"], 6), "prompt_tokens": config["prompt"]["observed_token_count"],
                        "prompt_tokens_per_second": round(config["prompt"]["observed_token_count"] / (values["prompt"] / 1000), 6),
                        "generation_eval_ms": round(values["generation"], 6), "generation_tokens": 8,
                        "generation_tokens_per_second": round(8 / (values["generation"] / 1000), 6)},
            "hardware": {"selected_gpu": GPU, "pci_id": PCI, "samples": samples,
                         "sampling": {"owner": "runner", "source": "drm", "continuous": True,
                                      "proc_root": str(proc_root)},
                         "ram": {"minimum_available_mib": minimum_ram, "passed": ram_ok},
                         "vram": {"capacity_mib": capacity, "peak_mib": peak, "passed": vram_ok},
                         "swap": {"in_pages": swap, "passed": swap_ok}},
            "settings": settings,
            "quality": {"suite": "issue-11-evaluation", "report_sha256": report_sha, "passed": quality}}


def prepare_cache(model, proc_root, requested):
    """Prepare cold cache only after checksum and only with explicit opt-in."""
    checksum = file_hash(model) if model is not None else None
    evidence = {"checksum_before_preparation": True,
                "model_checksum": checksum or "unavailable"}
    if not requested:
        evidence.update({"preparation": "not-requested", "honest_status": "deferred"})
        return evidence
    if model is None:
        evidence.update({"preparation": "model-not-supplied", "honest_status": "unsupported"})
        return evidence
    drop_caches = pathlib.Path(proc_root) / "sys/vm/drop_caches"
    if os.geteuid() != 0 or not drop_caches.is_file() or not os.access(drop_caches, os.W_OK):
        evidence.update({"preparation": "privileged-eviction-unavailable", "honest_status": "deferred"})
        return evidence
    try:
        # sync plus drop_caches=3 is the documented Linux page-cache operation;
        # no shell or arbitrary privileged command is involved.
        os.sync()
        drop_caches.write_text("3")
    except OSError:
        evidence.update({"preparation": "privileged-eviction-failed", "honest_status": "deferred"})
        return evidence
    evidence.update({"preparation": "page-cache-eviction", "honest_status": "verified-miss"})
    return evidence


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
    if (inputs.get("candidate") != candidate or inputs.get("prompt", {}).get("token_count") != 25 or
            inputs.get("prompt", {}).get("observed_token_count") != 25 or inputs.get("output", {}).get("token_count") != 8):
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
    parser.add_argument("--evict-cache", action="store_true",
                        help="explicitly request privileged page-cache eviction")
    parser.add_argument("--proc-root", default="/proc")
    parser.add_argument("--sysfs-root", default="/sys/class/drm")
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
        cold_cache = prepare_cache(model, args.proc_root, args.evict_cache)
        warm_cache = dict(cold_cache)
        warm_cache.update({"preparation": "after-cold-process-before-second-process",
                           "honest_status": "verified-hit" if cold_cache["honest_status"] == "verified-miss" else "deferred"})
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
        runs = [run_once(cli, "cold", timeout, capture_limit, params, True, config, report_sha, model,
                         args.proc_root, args.sysfs_root, cold_cache),
                run_once(cli, "warm", timeout, capture_limit, params, True, config, report_sha, model,
                         args.proc_root, args.sysfs_root, warm_cache)]
        passed = sum(row.get("status") == "pass" for row in runs)
        lifecycle = json.loads(json.dumps(config["lifecycle"]))
        lifecycle["cold"]["cache_preparation"] = ("page-cache-eviction" if cold_cache["honest_status"] == "verified-miss"
                                                   else cold_cache["honest_status"])
        lifecycle["warm"]["observed_after"] = "cold"
        data = {"schema_version": SCHEMA_VERSION, "benchmark": {"name": "issue-12-benchmark", "version": 1},
                "inputs": {"model": MODEL, "runtime": {"ref": "b10446", "commit": "adb55e5", "source": "observed", "synthetic_fixture": False},
                           "prompt": {"token_count": 25, "observed_token_count": 25,
                                       "tokenizer": config["prompt"]["tokenizer"]}, "output": {"token_count": 8},
                           "context_tokens": 32768, "lifecycle": lifecycle, "candidate": candidate},
                "runs": runs, "summary": {"status": "pass" if passed == 2 else "fail", "passed_runs": passed},
                "safety": {"selected_gpu": GPU, "selected_pci_id": PCI,
                           "swap_in_pages": max((row.get("hardware", {}).get("swap", {}).get("in_pages", 0) for row in runs), default=0),
                           "ram_passed": all(row.get("hardware", {}).get("ram", {}).get("passed", False) for row in runs),
                           "vram_passed": all(row.get("hardware", {}).get("vram", {}).get("passed", False) for row in runs)},
                "provenance": {"config_sha256": hashes[0], "tuning_result_sha256": hashes[1],
                               "evaluator_report_sha256": hashes[2], "artifact_sha256": "", "sanitized": True}}
        if passed != 2 and any("hardware" not in row or len(row.get("hardware", {}).get("samples", [])) < 2
                               for row in runs):
            raise ValueError("runner-owned /proc DRM hardware samples are unavailable; refusing publication")
        data["provenance"]["artifact_sha256"] = artifact_hash(data)
        validate_json_schema(data, load(ROOT / "schemas" / "benchmark-result.schema.json"))
        encoded = json.dumps(data, sort_keys=True, indent=2) + "\n"
        if len(encoded.encode()) > MAX_ARTIFACT:
            raise ValueError("benchmark artifact exceeds bounded size")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded)
        if passed != 2:
            print("benchmark did not pass: runner-owned /proc DRM hardware samples and honest cache evidence are required", file=sys.stderr)
        return 0 if passed == 2 else 1
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        return die(str(error))


if __name__ == "__main__":
    set_subreaper()
    raise SystemExit(main())
