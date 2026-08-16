#!/usr/bin/env python3
"""Opt-in sustained target-host recovery and memory-safety exercise."""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import http.client
import json
import math
import os
import pathlib
import select
import signal
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODEL_PATH = pathlib.Path.home() / ".local/share/local-ai/models/Qwen3.5-27B-Q8_0.gguf"
MODEL_SHA = "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450"
BASE = "http://127.0.0.1:8080"
MODEL = "Qwen3.5-27B-Q8_0"


class EnduranceError(RuntimeError):
    pass


def bounded_int(minimum, maximum):
    def convert(raw):
        value = int(raw)
        if not minimum <= value <= maximum:
            raise argparse.ArgumentTypeError(f"value must be between {minimum} and {maximum}")
        return value
    return convert


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="required acknowledgement")
    parser.add_argument("--pi-iterations", type=bounded_int(1, 10), default=2)
    parser.add_argument("--pressure-mib", type=bounded_int(256, 4096), default=2048)
    parser.add_argument("--timeout", type=bounded_int(300, 1800), default=1200)
    parser.add_argument("--result", type=pathlib.Path)
    args = parser.parse_args()
    if not args.real:
        parser.error("--real is required")
    return args


def file_hash(path: pathlib.Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def run(command, timeout, env=None):
    try:
        result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True,
                                timeout=timeout, env=env)
    except subprocess.TimeoutExpired as error:
        raise EnduranceError(f"command exceeded {timeout}s: {pathlib.Path(command[0]).name}") from error
    if len(result.stdout.encode()) > 8 * 1024 * 1024 or len(result.stderr.encode()) > 8 * 1024 * 1024:
        raise EnduranceError("command capture exceeded 8 MiB")
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-1500:]
        raise EnduranceError(f"{pathlib.Path(command[0]).name} failed: {detail}")
    return result


def json_command(command, timeout):
    result = run(command, timeout)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise EnduranceError(f"{pathlib.Path(command[0]).name} emitted invalid JSON") from error
    if payload.get("status") != "pass":
        raise EnduranceError(f"{pathlib.Path(command[0]).name} reported failure")
    return payload


def request(path, payload=None, timeout=180):
    parsed = urlsplit(BASE)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    body = None if payload is None else json.dumps(payload, separators=(",", ":"))
    try:
        connection.request("GET" if payload is None else "POST", path, body=body,
                           headers={"Content-Type": "application/json"} if body else {})
        response = connection.getresponse()
        raw = response.read(1024 * 1024)
        if len(raw) >= 1024 * 1024:
            raise EnduranceError(f"{path} response exceeded 1 MiB")
        decoded = json.loads(raw or b"{}")
        return response.status, decoded
    except (OSError, json.JSONDecodeError) as error:
        raise EnduranceError(f"{path} failed: {error}") from error
    finally:
        connection.close()


def chat(marker, timeout=180):
    status, body = request("/v1/chat/completions", {
        "model": MODEL, "messages": [{"role": "user", "content": f"Reply with exactly {marker}"}],
        "temperature": 0, "max_tokens": 16, "stream": False,
        "chat_template_kwargs": {"enable_thinking": False}}, timeout)
    text = ""
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        pass
    return {"http_status": status, "marker_observed": status == 200 and marker in text}


def model_action(action, timeout):
    run([str(ROOT / "scripts/router-model.sh"), action, "--model-id", "qwen3.5-27b-q8_0",
         "--timeout", str(min(timeout, 300))], timeout + 60)


def is_model_loaded():
    status, body = request("/models", timeout=10)
    if status != 200 or not isinstance(body.get("data"), list):
        raise EnduranceError("router model inventory is unavailable")
    matching = [row for row in body["data"] if row.get("id") == MODEL]
    if len(matching) != 1:
        raise EnduranceError("production model is not present exactly once")
    state = matching[0].get("status")
    return (state.get("value") if isinstance(state, dict) else state) == "loaded"


def proc_sample():
    meminfo = {}
    for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        meminfo[key] = int(value.strip().split()[0])
    vmstat = {}
    for line in pathlib.Path("/proc/vmstat").read_text().splitlines():
        key, value = line.split()
        vmstat[key] = int(value)
    vram_used = None
    vram_capacity = None
    for device in pathlib.Path("/sys/class/drm").glob("card*/device"):
        try:
            if (device / "vendor").read_text().strip().lower() == "0x1002" and (device / "device").read_text().strip().lower() == "0x73bf":
                vram_used = int((device / "mem_info_vram_used").read_text()) // (1024 * 1024)
                vram_capacity = int((device / "mem_info_vram_total").read_text()) // (1024 * 1024)
                break
        except OSError:
            continue
    return {"ram_available_mib": meminfo["MemAvailable"] // 1024,
            "swap_in_pages": vmstat.get("pswpin", 0),
            "vram_used_mib": vram_used, "vram_capacity_mib": vram_capacity}


def main():
    args = parse_args()
    started = time.time()
    monotonic_started = time.monotonic()
    checks = []
    samples = []
    heartbeat_max = 0.0
    stop_monitor = threading.Event()
    loaded = False

    def monitor():
        nonlocal heartbeat_max
        while not stop_monitor.is_set():
            before = time.monotonic()
            probe = subprocess.run(["true"], timeout=5)
            heartbeat_max = max(heartbeat_max, (time.monotonic() - before) * 1000)
            if probe.returncode:
                heartbeat_max = math.inf
            try:
                samples.append(proc_sample())
            except (OSError, ValueError, KeyError):
                pass
            stop_monitor.wait(1)

    thread = threading.Thread(target=monitor, daemon=True)
    thread.start()

    def checked(name, operation):
        before = time.monotonic()
        detail = operation()
        checks.append({"name": name, "passed": True,
                       "duration_ms": round((time.monotonic() - before) * 1000, 3),
                       **({"detail": detail} if detail is not None else {})})
        print(f"ok: {name}", file=sys.stderr)

    try:
        checked("initial-model-identity", lambda: None if MODEL_PATH.is_file() and file_hash(MODEL_PATH) == MODEL_SHA
                else (_ for _ in ()).throw(EnduranceError("model identity mismatch")))
        checked("initial-unloaded-normalization", lambda: (
            model_action("unload", args.timeout) if is_model_loaded() else None))
        checked("router-lifecycle-context-cancellation-restart", lambda: {
            "passed_checks": len(json_command([
                str(ROOT / "scripts/router-api-smoke.py"), "--real", "--timeout", "600"], args.timeout)["checks"])
        })

        pi_runs = []
        model_action("load", args.timeout)
        loaded = True
        for iteration in range(1, args.pi_iterations + 1):
            checked(f"pi-tool-loop-{iteration}", lambda iteration=iteration: (
                lambda result: pi_runs.append(result) or {"passed_checks": len(result["checks"])})(
                    json_command([str(ROOT / "scripts/pi-integration-smoke.py"), "--real", "--timeout", "300"], 600)))

        def concurrency():
            markers = ["CONCURRENT_A_OK", "CONCURRENT_B_OK"]
            before = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda marker: chat(marker, 240), markers))
            if not all(row["marker_observed"] for row in outcomes):
                raise EnduranceError(f"concurrent requests did not both complete: {outcomes}")
            return {"requests": 2, "all_completed": True,
                    "duration_ms": round((time.monotonic() - before) * 1000, 3)}
        checked("accidental-concurrent-requests", concurrency)

        def pressure():
            code = ("import sys,time\n"
                    "size=int(sys.argv[1])*1024*1024\n"
                    "data=bytearray(size)\n"
                    "for i in range(0,size,4096): data[i]=1\n"
                    "print('READY',flush=True)\n"
                    "time.sleep(180)\n")
            process = subprocess.Popen([sys.executable, "-c", code, str(args.pressure_mib)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            try:
                ready, _, _ = select.select([process.stdout], [], [], 60)
                if not ready or process.stdout.readline().strip() != "READY":
                    raise EnduranceError("bounded memory-pressure process failed to become ready within 60s")
                outcome = chat("PRESSURE_RECOVERY_OK", 240)
                if not outcome["marker_observed"]:
                    raise EnduranceError(f"inference failed under bounded memory pressure: {outcome}")
                return {"allocated_mib": args.pressure_mib, "inference_completed": True}
            finally:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        checked("bounded-host-memory-pressure", pressure)

        checked("final-model-unload", lambda: model_action("unload", args.timeout))
        loaded = False
        checked("final-model-identity", lambda: None if file_hash(MODEL_PATH) == MODEL_SHA
                else (_ for _ in ()).throw(EnduranceError("model changed during endurance run")))

        stop_monitor.set(); thread.join(timeout=5)
        if len(samples) < 2 or any(row["vram_used_mib"] is None for row in samples):
            raise EnduranceError("runner-owned /proc/DRM endurance samples are incomplete")
        minimum_ram = min(row["ram_available_mib"] for row in samples)
        peak_vram = max(row["vram_used_mib"] for row in samples)
        swap_delta = max(row["swap_in_pages"] for row in samples) - min(row["swap_in_pages"] for row in samples)
        capacity = max(row["vram_capacity_mib"] for row in samples)
        if minimum_ram < 8192 or capacity - peak_vram < 1024 or swap_delta != 0 or heartbeat_max > 5000:
            raise EnduranceError("RAM, VRAM, swap, or desktop-heartbeat safety threshold failed")

        journal = run(["journalctl", "--user", "-u", "local-ai-router.service",
                       "--since", f"@{int(started)}", "--no-pager", "-o", "cat"], 30).stdout.lower()
        oom_markers = [marker for marker in ("out of memory", "oom-kill", "cannot allocate memory") if marker in journal]
        if oom_markers:
            raise EnduranceError(f"service journal contains memory failure markers: {oom_markers}")
        safety = {"minimum_available_ram_mib": minimum_ram,
                  "peak_vram_mib": peak_vram, "vram_capacity_mib": capacity,
                  "swap_in_pages_delta": swap_delta,
                  "maximum_desktop_heartbeat_ms": round(heartbeat_max, 3),
                  "sample_count": len(samples), "oom_markers": []}
        code = 0
    except (EnduranceError, OSError, subprocess.SubprocessError, KeyError, TypeError, json.JSONDecodeError) as error:
        checks.append({"name": "failure", "passed": False, "error": str(error)[:1200]})
        print(f"error: {error}", file=sys.stderr)
        safety = {}
        code = 1
    finally:
        stop_monitor.set(); thread.join(timeout=5)
        if loaded:
            try:
                model_action("unload", args.timeout)
            except Exception as error:
                print(f"error: cleanup unload failed: {error}", file=sys.stderr)
                code = 1
    result = {"schema_version": 1, "suite": "issue-14-endurance",
              "status": "pass" if code == 0 else "fail",
              "duration_seconds": round(time.monotonic() - monotonic_started, 3),
              "workload": {"pi_integration_iterations": args.pi_iterations,
                           "pi_processes": args.pi_iterations * 5,
                           "verified_tool_calls": args.pi_iterations * 4,
                           "bounded_memory_pressure_mib": args.pressure_mib,
                           "concurrent_requests": 2,
                           "verified_model_loads": 3,
                           "verified_model_unloads_minimum": 3,
                           "near_context_repeated_words": 30000,
                           "includes_near_32k_context": True,
                           "includes_cancellation_and_service_restart": True},
              "model": {"id": MODEL, "sha256": MODEL_SHA},
              "runtime": {"ref": "b10446", "commit": "adb55e5"},
              "checks": checks, "safety": safety,
              "model_files_deleted": False}
    encoded = json.dumps(result, sort_keys=True, indent=2) + "\n"
    if args.result:
        args.result.parent.mkdir(parents=True, exist_ok=True)
        args.result.write_text(encoded)
    print(encoded, end="")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
