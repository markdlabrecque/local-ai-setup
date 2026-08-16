#!/usr/bin/env python3
"""Run the final, real, fail-closed readiness gate for the deployed stack."""
from __future__ import annotations

import argparse
import http.client
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
HOME = pathlib.Path.home()
BASE = "127.0.0.1:8080"
MODEL_ID = "Qwen3.5-27B-Q8_0"
MANIFEST_ID = "qwen3.5-27b-q8_0"
MODEL_SHA = "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450"
MODEL_SIZE = 28595763104
RUNTIME_REF = "b10446"
RUNTIME_COMMIT = "adb55e5"
SERVICE = "local-ai-router.service"
MAX_CAPTURE = 8 * 1024 * 1024


class VerificationError(RuntimeError):
    pass


REMEDIATION = {
    "pinned-config": "Restore the reviewed config and run scripts/validate-production-profile.py.",
    "pinned-binaries": "Run scripts/setup.sh, then verify the user-local runtime links.",
    "vulkan-gpu": "Fix RADV/Vulkan visibility and permissions; run vulkaninfo --summary.",
    "model-identity": "Re-download Q8_0 with scripts/download-model.py; never edit the checksum.",
    "service-and-router": "Install/start with scripts/install-router-service.sh --enable --start, then inspect journalctl --user -u local-ai-router.service.",
    "router-api-smoke": "Run scripts/router-api-smoke.py --real and inspect the named failing phase and service journal.",
    "production-model-load": "Check /models and the service journal, unload any failed entry, then retry router-model.sh load.",
    "pi-tool-smoke": "Check the reviewed local-qwen Pi provider, /llama connectivity, and rerun scripts/pi-integration-smoke.py --real.",
    "final-health-and-resources": "Stop competing workloads; preserve at least 8 GiB RAM and 1 GiB VRAM, then inspect service status and logs.",
}


def bounded_int(raw):
    value = int(raw)
    if not 600 <= value <= 1800:
        raise argparse.ArgumentTypeError("timeout must be between 600 and 1800 seconds")
    return value


def args_parse():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="required acknowledgement")
    parser.add_argument("--timeout", type=bounded_int, default=1200)
    parser.add_argument("--output", type=pathlib.Path)
    args = parser.parse_args()
    if not args.real:
        parser.error("--real is required")
    return args


def command(argv, timeout, include_stderr=False):
    try:
        result = subprocess.run(argv, cwd=ROOT, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise VerificationError(f"{pathlib.Path(argv[0]).name} exceeded its {timeout}s timeout") from error
    if len(result.stdout.encode()) > MAX_CAPTURE or len(result.stderr.encode()) > MAX_CAPTURE:
        raise VerificationError(f"{pathlib.Path(argv[0]).name} exceeded the bounded capture")
    if result.returncode:
        raise VerificationError(f"{pathlib.Path(argv[0]).name} exited {result.returncode}")
    return result.stdout + (result.stderr if include_stderr else "")


def json_command(argv, timeout):
    raw = command(argv, timeout)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise VerificationError(f"{pathlib.Path(argv[0]).name} returned malformed JSON") from error
    if value.get("status") != "pass":
        raise VerificationError(f"{pathlib.Path(argv[0]).name} reported failure")
    return value


def http_json(path, timeout=10):
    connection = http.client.HTTPConnection("127.0.0.1", 8080, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read(1024 * 1024)
        if response.status != 200 or len(raw) >= 1024 * 1024:
            raise VerificationError(f"{path} returned bounded HTTP failure")
        return json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise VerificationError(f"{path} is unavailable") from error
    finally:
        connection.close()


def model_loaded():
    rows = http_json("/models").get("data")
    if not isinstance(rows, list):
        raise VerificationError("router model inventory is malformed")
    matches = [row for row in rows if row.get("id") == MODEL_ID]
    if len(matches) != 1:
        raise VerificationError("production model is not present exactly once")
    state = matches[0].get("status")
    return (state.get("value") if isinstance(state, dict) else state) == "loaded"


def model_action(action, timeout):
    command([str(ROOT / "scripts/router-model.sh"), action, "--model-id", MANIFEST_ID,
             "--timeout", "300"], min(timeout, 420))


def resource_sample():
    meminfo = {}
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        meminfo[key] = int(value.strip().split()[0])
    vmstat = dict(line.split()[:2] for line in pathlib.Path("/proc/vmstat").read_text().splitlines())
    for device in pathlib.Path("/sys/class/drm").glob("card*/device"):
        try:
            if ((device / "vendor").read_text().strip().lower() == "0x1002" and
                    (device / "device").read_text().strip().lower() == "0x73bf"):
                return {"ram_available_mib": meminfo["MemAvailable"] // 1024,
                        "swap_in_pages": int(vmstat["pswpin"]),
                        "vram_used_mib": int((device / "mem_info_vram_used").read_text()) // (1024 * 1024),
                        "vram_capacity_mib": int((device / "mem_info_vram_total").read_text()) // (1024 * 1024)}
        except (OSError, ValueError, KeyError):
            continue
    raise VerificationError("RX 6900 XT DRM resource counters are unavailable")


def main():
    args = args_parse()
    began_wall = int(time.time())
    began = time.monotonic()
    checks = []
    samples = []
    stop = threading.Event()
    leave_loaded = False

    def monitor():
        while not stop.is_set():
            try:
                samples.append(resource_sample())
            except VerificationError:
                pass
            stop.wait(1)

    monitor_thread = threading.Thread(target=monitor, daemon=True)
    monitor_thread.start()

    def check(name, operation):
        start = time.monotonic()
        try:
            detail = operation()
        except Exception as error:
            checks.append({"name": name, "passed": False,
                           "duration_ms": round((time.monotonic() - start) * 1000, 3),
                           "remediation": REMEDIATION[name]})
            raise VerificationError(f"{name} failed; {REMEDIATION[name]}") from error
        row = {"name": name, "passed": True,
               "duration_ms": round((time.monotonic() - start) * 1000, 3)}
        if detail is not None:
            row["detail"] = detail
        checks.append(row)
        print(f"PASS {name}", file=sys.stderr)
        return detail

    try:
        def pinned_config():
            command([str(ROOT / "scripts/validate-production-profile.py")], 30)
            profile = json.loads((ROOT / "config/production-profile.json").read_text())
            profile_id = profile["selected_profile"]
            selected = profile["profiles"][profile_id]
            if (selected["model"]["id"] != MODEL_ID or selected["model"]["sha256"] != MODEL_SHA or
                    selected["router"]["context"] < 32768):
                raise VerificationError("production profile identity/context mismatch")
            return {"profile": profile_id, "context_tokens": selected["router"]["context"]}
        check("pinned-config", pinned_config)

        def pinned_binaries():
            expected = (HOME / ".local/share/local-ai/runtime" / f"llama.cpp-{RUNTIME_REF}" / "bin").resolve()
            resolved = {}
            for name in ("llama-server", "llama-cli", "llama-bench"):
                path = HOME / ".local/bin" / name
                target = path.resolve(strict=True)
                if target.parent != expected or not os.access(target, os.X_OK):
                    raise VerificationError(f"{name} does not resolve to the pinned runtime")
                # b10446's bench has no --version; its immutable directory and
                # executable ownership pin it, while both runtime entrypoints
                # independently report the source commit.
                output = command([str(target), "--help" if name == "llama-bench" else "--version"],
                                 15, include_stderr=True)
                if name != "llama-bench" and RUNTIME_COMMIT not in output:
                    raise VerificationError(f"{name} commit mismatch")
                resolved[name] = str(target.relative_to(HOME))
            return {"ref": RUNTIME_REF, "commit": RUNTIME_COMMIT, "executables": resolved}
        check("pinned-binaries", pinned_binaries)

        def vulkan_gpu():
            output = command(["vulkaninfo", "--summary"], 30)
            if "AMD Radeon RX 6900 XT" not in output:
                raise VerificationError("vulkaninfo does not expose the selected adapter")
            sample = resource_sample()
            return {"adapter": "AMD Radeon RX 6900 XT", "pci_id": "1002:73BF",
                    "vram_capacity_mib": sample["vram_capacity_mib"]}
        check("vulkan-gpu", vulkan_gpu)

        model_path = HOME / ".local/share/local-ai/models/Qwen3.5-27B-Q8_0.gguf"
        def model_identity():
            observed = command(["sha256sum", "--binary", str(model_path)], 180).split(maxsplit=1)[0]
            if not model_path.is_file() or model_path.stat().st_size != MODEL_SIZE or observed != MODEL_SHA:
                raise VerificationError("model size/checksum mismatch")
            return {"id": MODEL_ID, "manifest_id": MANIFEST_ID,
                    "size_bytes": MODEL_SIZE, "sha256": MODEL_SHA}
        check("model-identity", model_identity)

        def service_router():
            command(["systemctl", "--user", "start", SERVICE], 30)
            if command(["systemctl", "--user", "is-active", SERVICE], 15).strip() != "active":
                raise VerificationError("user service is not active")
            deadline = time.monotonic() + 30
            while True:
                try:
                    healthy = http_json("/health").get("status") == "ok"
                except VerificationError:
                    healthy = False
                if healthy:
                    break
                if time.monotonic() >= deadline:
                    raise VerificationError("router health did not become ready within 30 seconds")
                time.sleep(0.25)
            if model_loaded():
                model_action("unload", args.timeout)
            return {"service": SERVICE, "state": "active", "endpoint": "http://" + BASE,
                    "initial_model_state": "unloaded"}
        check("service-and-router", service_router)

        def router_smoke():
            result = json_command([str(ROOT / "scripts/router-api-smoke.py"),
                                   "--real", "--timeout", "600"], args.timeout)
            if result.get("context_tokens", 0) < 32768 or len(result.get("checks", [])) != 11:
                raise VerificationError("router smoke did not prove the required context/lifecycle")
            return {"passed_checks": 11, "context_tokens": result["context_tokens"]}
        check("router-api-smoke", router_smoke)

        check("production-model-load", lambda: (model_action("load", args.timeout),
              {"model": MODEL_ID, "state": "loaded"})[1])
        leave_loaded = True

        def pi_smoke():
            result = json_command([str(ROOT / "scripts/pi-integration-smoke.py"),
                                   "--real", "--timeout", "300"], 600)
            if len(result.get("checks", [])) != 5:
                raise VerificationError("Pi smoke did not prove all text/thinking/tool phases")
            return {"passed_checks": 5, "sequential_tools": True, "parallel_tools": True}
        check("pi-tool-smoke", pi_smoke)

        def final_state():
            if http_json("/health").get("status") != "ok" or not model_loaded():
                raise VerificationError("router/model did not remain ready")
            if len(samples) < 2:
                raise VerificationError("resource sampling is incomplete")
            minimum_ram = min(row["ram_available_mib"] for row in samples)
            peak_vram = max(row["vram_used_mib"] for row in samples)
            capacity = max(row["vram_capacity_mib"] for row in samples)
            swap_delta = max(row["swap_in_pages"] for row in samples) - min(row["swap_in_pages"] for row in samples)
            if minimum_ram < 8192 or capacity - peak_vram < 1024 or swap_delta != 0:
                raise VerificationError("resource readiness policy failed")
            journal = command(["journalctl", "--user", "-u", SERVICE, "--since",
                               f"@{began_wall}", "--no-pager", "-o", "cat"], 30).lower()
            if any(marker in journal for marker in ("oom-kill", "out of memory", "cannot allocate memory")):
                raise VerificationError("OOM marker found in service journal")
            return {"minimum_available_ram_mib": minimum_ram,
                    "peak_vram_mib": peak_vram, "vram_capacity_mib": capacity,
                    "swap_in_pages_delta": swap_delta, "sample_count": len(samples),
                    "final_model_state": "loaded", "oom_markers": []}
        resources = check("final-health-and-resources", final_state)
        status = "pass"
        code = 0
    except (VerificationError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        print(f"FAIL {error}", file=sys.stderr)
        resources = {}
        status = "fail"
        code = 1
    finally:
        stop.set()
        monitor_thread.join(timeout=5)
        if code and leave_loaded:
            try:
                model_action("unload", args.timeout)
            except Exception:
                print("FAIL cleanup unload; inspect router status and journal", file=sys.stderr)

    report = {"schema_version": 1, "suite": "issue-16-deployment-verification",
              "status": status, "duration_seconds": round(time.monotonic() - began, 3),
              "endpoint": "http://" + BASE, "context_tokens": 32768,
              "runtime": {"ref": RUNTIME_REF, "commit": RUNTIME_COMMIT},
              "model": {"id": MODEL_ID, "manifest_id": MANIFEST_ID,
                        "sha256": MODEL_SHA, "size_bytes": MODEL_SIZE},
              "checks": checks, "resources": resources,
              "final_state": {"service": "active" if status == "pass" else "unknown",
                              "model": "loaded" if status == "pass" else "unloaded-by-cleanup"},
              "sanitized": True}
    encoded = json.dumps(report, sort_keys=True, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded)
    print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
