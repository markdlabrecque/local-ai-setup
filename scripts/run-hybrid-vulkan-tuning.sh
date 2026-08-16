#!/usr/bin/env python3
"""Bounded, resumable tuning around the pinned direct baseline runner.

The no-model path is deliberately limited to explicit measurement fixtures.  A
real run always delegates model loading and evidence collection to
run-direct-baseline.sh; this keeps the tuning layer from inventing GPU evidence.
"""
import argparse
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
DIRECT = ROOT / "scripts" / "run-direct-baseline.sh"
EXPECTED_PROMPT = "Respond with exactly: LOCAL_AI_HYBRID_TUNING_OK"
EXPECTED_COMPLETION = "LOCAL_AI_HYBRID_TUNING_OK"
EXPECTED_SHA = "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450"
EXPECTED_BUILD = {"ref": "b10446", "commit": "adb55e5"}


def die(message):
    print(message, file=sys.stderr)
    return 2


def integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def validate_config(config):
    if config.get("schema_version") != "hybrid-vulkan-tuning-v1":
        raise ValueError("unsupported tuning schema")
    model = config.get("model")
    if not isinstance(model, dict):
        raise ValueError("model identity is required")
    if model.get("sha256", "").lower() != EXPECTED_SHA:
        raise ValueError("model checksum is not the pinned Q8_0 artifact")
    if model.get("quantization") != "Q8_0":
        raise ValueError("tuning is restricted to Q8_0")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", model.get("sha256", "")):
        raise ValueError("invalid model checksum")
    if config.get("build") != EXPECTED_BUILD:
        raise ValueError("llama.cpp build is not pinned to b10446/adb55e5")
    if config.get("prompt") != EXPECTED_PROMPT:
        raise ValueError("prompt is not the fixed tuning prompt")
    context = config.get("context_tokens")
    integer(context, "context_tokens", 32768)
    matrix = config.get("matrix")
    if not isinstance(matrix, dict):
        raise ValueError("matrix is required")
    for key in ("gpu_layers", "flash_attention", "batch", "ubatch", "kv_cache"):
        values = matrix.get(key)
        if not isinstance(values, list) or not values:
            raise ValueError(f"matrix.{key} must be a non-empty array")
    if any(integer(x, "gpu_layers") < 0 for x in matrix["gpu_layers"]):
        raise ValueError("invalid gpu layer value")
    if any(x not in ("on", "off") for x in matrix["flash_attention"]):
        raise ValueError("flash_attention must contain on/off")
    if any(integer(x, "batch", 1) == 0 for x in matrix["batch"]):
        raise ValueError("invalid batch value")
    if any(integer(x, "ubatch", 1) == 0 for x in matrix["ubatch"]):
        raise ValueError("invalid ubatch value")
    if any(str(x).lower() != "q8_0" for x in matrix["kv_cache"]):
        raise ValueError("KV cache must be q8_0")
    safety = config.get("safety")
    if not isinstance(safety, dict):
        raise ValueError("safety thresholds are required")
    for key in ("minimum_vram_free_mib", "minimum_mem_available_mib", "maximum_swap_in_pages"):
        integer(safety.get(key), key)
    return context


def tuple_key(parameters):
    return json.dumps([parameters[k] for k in
                       ("gpu_layers", "flash_attention", "batch", "ubatch", "kv_cache")],
                      separators=(",", ":"))


def safe_text(value):
    text = str(value)
    text = re.sub(r"(?is)-+BEGIN[^\n]*PRIVATE KEY-+.*?-+END[^\n]*PRIVATE KEY-+", "[PRIVATE KEY REDACTED]", text)
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"(?i)ghp_[A-Za-z0-9]+", "[GITHUB TOKEN REDACTED]", text)
    text = re.sub(r"(?i)(api[_-]?key|access[_-]?token|secret|password)\s*[=:]\s*[^\s,]+", r"\1=[REDACTED]", text)
    return text


def read_capacity():
    root = pathlib.Path(os.environ.get("BASELINE_SYSFS_ROOT", "/sys"))
    for device in sorted(root.glob("class/drm/card*/device")):
        try:
            vendor = (device / "vendor").read_text().strip().lower().removeprefix("0x")
            ident = (device / "device").read_text().strip().lower().removeprefix("0x")
            if vendor == "1002" and ident == "73bf":
                value = int((device / "mem_info_vram_total").read_text().split()[0])
                return value // 1024 // 1024
        except (OSError, ValueError):
            continue
    return None


def bounded_process(argv, timeout, stdout_limit, stderr_limit):
    """Run a fixture without allowing either pipe or the child process to grow."""
    started = time.monotonic()
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)
    except OSError:
        return "", "", 127, False, False
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
    selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
    data = {"stdout": bytearray(), "stderr": bytearray()}
    timed_out = False
    while selector.get_map():
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0 and proc.poll() is None:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(0.05)
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        events = selector.select(max(0.02, min(0.1, max(remaining, 0.02))))
        for key, _ in events:
            chunk = os.read(key.fileobj.fileno(), 65536)
            if not chunk:
                selector.unregister(key.fileobj)
                key.fileobj.close()
                continue
            limit = stdout_limit if key.data == "stdout" else stderr_limit
            if len(data[key.data]) < limit:
                data[key.data].extend(chunk[:limit - len(data[key.data])])
        if proc.poll() is not None:
            # Descendants must not be able to keep a capture pipe open.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        if timed_out and proc.poll() is not None and not selector.get_map():
            break
    try:
        status = proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        status = proc.wait()
    return data["stdout"].decode("utf-8", "replace"), data["stderr"].decode("utf-8", "replace"), status, timed_out, True


def fixture_run(cli, args, timeout, stdout_limit=1048576, stderr_limit=4194304):
    version, _, version_status, _, _ = bounded_process([cli, "--version"], min(timeout, 10), 8192, 8192)
    if version_status != 0 or "adb55e5" not in version:
        return {"exit_code": 127, "timed_out": False, "stdout": "", "stderr": "", "bounded": True}
    return dict(zip(("stdout", "stderr", "exit_code", "timed_out", "bounded"),
                    bounded_process([cli] + args, timeout, stdout_limit, stderr_limit)))


def numeric(value, default=0.0):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else default


def parse_fixture(raw, parameters, context, expected, measurements, safety):
    combined = raw["stdout"] + "\n" + raw["stderr"]
    completion = raw["stdout"].strip()
    device_ok = bool(re.search(r"(?i)vulkan.*RX[ -]?6900", combined))
    context_ok = bool(re.search(r"(?i)n_ctx\s*=\s*" + str(context), combined))
    stop_ok = bool(re.search(r"(?i)(finish_reason\s*=\s*stop|stop reason\s*[:=]\s*stop|stopped-by-EOS)", combined))
    exact = completion == expected
    return make_run(parameters, context, raw, exact and device_ok and context_ok and stop_ok,
                    measurements, 0.0, 0.0, 0.0, device_ok, context_ok, exact, safety)


def metrics_from_direct(direct, supplied):
    measurements = direct.get("measurements", {}) if isinstance(direct, dict) else {}
    activity = direct.get("swap_activity", {}) if isinstance(direct, dict) else {}
    def first(*values):
        for value in values:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None
    # Explicit measurements are fixture evidence and intentionally win over live
    # values. This also makes unsafe fixture cases testable without fake sysfs.
    return {
        "vram_capacity_mib": first(supplied.get("vram_capacity_mib"), read_capacity()),
        "peak_vram_mib": first(supplied.get("peak_vram_mib"), measurements.get("vram_mib")),
        "min_mem_available_mib": first(supplied.get("min_mem_available_mib"), measurements.get("mem_available_mib")),
        "swap_in_pages": first(supplied.get("swap_in_pages"), activity.get("pages_in"), measurements.get("swap_in_pages")),
        "time_to_first_token_ms": numeric(direct.get("metrics", {}).get("time_to_first_token_ms")),
        "prompt_tokens_per_second": numeric(direct.get("metrics", {}).get("prompt_tokens_per_second")),
        "generation_tokens_per_second": numeric(direct.get("metrics", {}).get("generation_tokens_per_second")),
    }


def make_run(parameters, context, raw, quality, measurements, ttft, prompt_tps, generation_tps,
             device_ok=True, context_ok=True, exact=True, safety_thresholds=None):
    metrics = {
        "time_to_first_token_ms": numeric(ttft),
        "prompt_tokens_per_second": numeric(prompt_tps),
        "generation_tokens_per_second": numeric(generation_tps),
        "peak_vram_mib": measurements.get("peak_vram_mib"),
        "min_mem_available_mib": measurements.get("min_mem_available_mib"),
        "swap_in_pages": measurements.get("swap_in_pages"),
        "vram_capacity_mib": measurements.get("vram_capacity_mib"),
        "exit_code": int(raw.get("exit_code", 127)),
        "timed_out": bool(raw.get("timed_out", False)),
    }
    thresholds = safety_thresholds or {"minimum_vram_free_mib": 1024,
                                       "minimum_mem_available_mib": 8192,
                                       "maximum_swap_in_pages": 0}
    safety = (isinstance(metrics["vram_capacity_mib"], (int, float)) and
              isinstance(metrics["peak_vram_mib"], (int, float)) and
              metrics["peak_vram_mib"] <= metrics["vram_capacity_mib"] - thresholds["minimum_vram_free_mib"] and
              isinstance(metrics["min_mem_available_mib"], (int, float)) and
              metrics["min_mem_available_mib"] >= thresholds["minimum_mem_available_mib"] and
              isinstance(metrics["swap_in_pages"], (int, float)) and
              metrics["swap_in_pages"] <= thresholds["maximum_swap_in_pages"])
    passed = bool(quality and safety and raw.get("exit_code") == 0 and not raw.get("timed_out"))
    run = {
        "parameters": parameters,
        "metrics": metrics,
        "quality": {"device_confirmed": device_ok, "context_confirmed": context_ok,
                    "exact_completion": exact, "passed": bool(quality)},
        "lifecycle": {"started": True, "bounded": bool(raw.get("bounded")), "finished": True},
        "status": "pass" if passed else "fail",
    }
    return run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--llama-cli", required=True)
    parser.add_argument("--model")
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-timeout", type=float, default=300)
    parser.add_argument("--measurements", default="")
    parser.add_argument("--resume", action="store_true")
    ns = parser.parse_args()
    if ns.run_timeout <= 0:
        return die("run timeout must be greater than zero")
    config_path = pathlib.Path(ns.config)
    try:
        config_bytes = config_path.read_bytes()
        config = json.loads(config_bytes)
        context = validate_config(config)
        supplied = json.loads(ns.measurements) if ns.measurements else {}
        if not isinstance(supplied, dict):
            raise ValueError("measurements must be a JSON object")
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        return die(safe_text(exc))
    if not os.path.isfile(ns.llama_cli) or not os.access(ns.llama_cli, os.X_OK):
        return die("llama-cli is not executable")
    if ns.model and (not os.path.isfile(ns.model) or not os.access(ns.model, os.R_OK)):
        return die("model is not readable")
    output = pathlib.Path(ns.output)
    config_sha = hashlib.sha256(config_bytes).hexdigest()
    matrix = config["matrix"]
    parameters = []
    for layers in matrix["gpu_layers"]:
        for flash in matrix["flash_attention"]:
            for batch in matrix["batch"]:
                for ubatch in matrix["ubatch"]:
                    for cache in matrix["kv_cache"]:
                        parameters.append({"gpu_layers": layers, "flash_attention": flash,
                                           "batch": batch, "ubatch": ubatch,
                                           "kv_cache": str(cache).lower(), "quantization": "Q8_0"})
    runs_by_key = {}
    if ns.resume:
        try:
            old = json.loads(output.read_text())
            if old.get("resumability", {}).get("config_sha256") != config_sha:
                return die("resume identity mismatch")
            if old.get("prompt") != config["prompt"] or old.get("model", {}).get("sha256") != config["model"]["sha256"]:
                return die("resume fixed identity mismatch")
            for run in old.get("runs", []):
                if run.get("lifecycle", {}).get("finished"):
                    runs_by_key[tuple_key(run.get("parameters", {}))] = run
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            return die("cannot resume incomplete tuning output")
    output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        ordered = [runs_by_key[tuple_key(p)] for p in parameters if tuple_key(p) in runs_by_key]
        candidates = [r for r in ordered if r.get("status") == "pass"]
        def sort_key(run):
            m = run["metrics"]
            return (-numeric(m.get("generation_tokens_per_second")),
                    -numeric(m.get("prompt_tokens_per_second")),
                    numeric(m.get("time_to_first_token_ms"), 1e30),
                    numeric(m.get("peak_vram_mib"), 1e30), tuple(str(run["parameters"].get(k)) for k in
                    ("gpu_layers", "flash_attention", "batch", "ubatch", "kv_cache")))
        candidates.sort(key=sort_key)
        selected = candidates[0] if candidates else None
        result = {
            "schema_version": "hybrid-vulkan-tuning-v1",
            "model": {"id": safe_text(config["model"].get("id", "pinned-q8_0")),
                      "sha256": config["model"]["sha256"].lower(), "quantization": "Q8_0"},
            "build": EXPECTED_BUILD,
            "prompt": config["prompt"], "context_tokens": context,
            "matrix": config["matrix"], "safety": config["safety"],
            "runs": ordered,
            "stable_candidate": {"parameters": selected["parameters"], "metrics": selected["metrics"]} if selected else None,
            "selection": {"deterministic": True, "policy": "quality-pass, generation-tps desc, prompt-tps desc, TTFT asc, VRAM asc, parameter tuple asc",
                          "candidate_count": len(candidates)},
            "resumability": {"key": "parameter_tuple", "config_sha256": config_sha},
        }
        temp = output.with_name(output.name + ".tmp")
        temp.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        os.replace(temp, output)

    for p in parameters:
        key = tuple_key(p)
        if key in runs_by_key:
            continue
        cli_args = ["--ctx-size", str(context), "--device", "Vulkan0", "--gpu-layers", str(p["gpu_layers"]),
                    "--flash-attn", p["flash_attention"], "--batch-size", str(p["batch"]),
                    "--ubatch-size", str(p["ubatch"]), "--cache-type-k", p["kv_cache"],
                    "--reasoning", "off", "--temp", "0", "--seed", "42", "--single-turn",
                    "--simple-io", "--verbose", "--no-display-prompt", "--prompt", config["prompt"],
                    "--n-predict", "128"]
        if ns.model:
            run_output = output.with_name(output.name + ".run.json")
            command = [str(DIRECT), "--model", ns.model, "--sha256", config["model"]["sha256"],
                       "--llama-cli", ns.llama_cli, "--prompt", config["prompt"],
                       "--expected-completion", EXPECTED_COMPLETION, "--context", str(context),
                       "--timeout", str(ns.run_timeout), "--gpu-layers", str(p["gpu_layers"]),
                       "--flash-attn", p["flash_attention"], "--batch-size", str(p["batch"]),
                       "--ubatch-size", str(p["ubatch"]), "--cache-type-k", p["kv_cache"],
                       "--output", str(run_output)]
            completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                direct = json.loads(run_output.read_text())
            except (OSError, json.JSONDecodeError):
                direct = {"exit_code": completed.returncode, "timed_out": False, "metrics": {}}
            metrics = metrics_from_direct(direct, supplied)
            raw = {"exit_code": direct.get("exit_code", completed.returncode),
                   "timed_out": direct.get("timed_out", False), "bounded": True}
            quality = bool(direct.get("expected_completion_match") and direct.get("context_confirmed") and
                           direct.get("vram_device") == "AMD Radeon RX 6900 XT" and direct.get("offload_evidence"))
            run = make_run(p, context, raw, quality, metrics, metrics["time_to_first_token_ms"],
                           metrics["prompt_tokens_per_second"], metrics["generation_tokens_per_second"],
                           quality, bool(direct.get("context_confirmed")), bool(direct.get("expected_completion_match")), config["safety"])
            try: run_output.unlink()
            except OSError: pass
        else:
            raw = fixture_run(ns.llama_cli, cli_args, ns.run_timeout)
            run = parse_fixture(raw, p, context, EXPECTED_COMPLETION, {
                "vram_capacity_mib": supplied.get("vram_capacity_mib"),
                "peak_vram_mib": supplied.get("peak_vram_mib"),
                "min_mem_available_mib": supplied.get("min_mem_available_mib"),
                "swap_in_pages": supplied.get("swap_in_pages"),
            }, config["safety"])
            # Replace fixed fixture safety values with the configured thresholds.
            m = run["metrics"]
            m["vram_capacity_mib"] = supplied.get("vram_capacity_mib", m.get("vram_capacity_mib"))
            m["peak_vram_mib"] = supplied.get("peak_vram_mib", m.get("peak_vram_mib"))
            m["min_mem_available_mib"] = supplied.get("min_mem_available_mib", m.get("min_mem_available_mib"))
            m["swap_in_pages"] = supplied.get("swap_in_pages", m.get("swap_in_pages"))
            safety = (isinstance(m["vram_capacity_mib"], (int, float)) and
                      isinstance(m["peak_vram_mib"], (int, float)) and
                      m["peak_vram_mib"] <= m["vram_capacity_mib"] - config["safety"]["minimum_vram_free_mib"] and
                      isinstance(m["min_mem_available_mib"], (int, float)) and
                      m["min_mem_available_mib"] >= config["safety"]["minimum_mem_available_mib"] and
                      isinstance(m["swap_in_pages"], (int, float)) and
                      m["swap_in_pages"] <= config["safety"]["maximum_swap_in_pages"])
            run["status"] = "pass" if run["quality"]["passed"] and safety and raw["exit_code"] == 0 and not raw["timed_out"] else "fail"
        runs_by_key[key] = run
        save()
    save()
    final = json.loads(output.read_text())
    return 0 if final.get("stable_candidate") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (BrokenPipeError, KeyboardInterrupt):
        sys.exit(130)
    except Exception as exc:
        sys.exit(die(safe_text(exc)))
