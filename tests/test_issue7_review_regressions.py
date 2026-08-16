"""Issue #7 review regression contracts (intentionally red until fixed).

The HTTP fixture models the b10446 router API: model identifiers are the
extensionless models-dir IDs, successful mutations return exactly
``{"success": true}``, and readiness is observed through /models statuses.
"""
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).parents[1]
LAUNCHER = ROOT / "scripts" / "run-router.sh"
HELPER = ROOT / "scripts" / "router-model.sh"
CONFIG = ROOT / "config" / "router.json"
PRESETS = ROOT / "config" / "router-presets.json"


class B10446Endpoint:
    """Small router fixture with the real response/status shape."""

    def __init__(self, *, mode="normal", model_id="Qwen3.5-27B-Q8_0", strict=True):
        self.mode = mode
        self.strict = strict
        self.model_id = model_id
        self.requests = []
        self.load_polls = 0
        self.unload_polls = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, payload, status=200):
                body = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path == "/health":
                    self.reply({"status": "ok"})
                    return
                if self.path != "/models":
                    self.reply({"error": "not found"}, 404)
                    return
                if owner.state == "loading":
                    owner.load_polls += 1
                    status = "loading" if owner.mode != "load-timeout" or owner.load_polls < 4 else "loading"
                    if owner.mode != "load-timeout" and owner.load_polls >= 2:
                        status = "loaded"
                elif owner.state == "unloading":
                    owner.unload_polls += 1
                    status = "unloading"
                    if owner.mode != "unload-timeout" and owner.unload_polls >= 2:
                        status = "unloaded"
                else:
                    status = "unloaded"
                self.reply({"object": "list", "data": [{"id": owner.model_id, "status": status}]})

            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append((self.path, body))
                if self.path not in ("/models/load", "/models/unload"):
                    self.reply({"error": "not found"}, 404)
                    return
                # b10446 accepts the extensionless ID and returns only this
                # success shape.  Any compatibility payload is a regression.
                expected = {"model": owner.model_id}
                accepted = {json.dumps(expected)}
                if not owner.strict:
                    accepted.add(json.dumps({"model": owner.model_id + ".gguf"}))
                if json.dumps(body) not in accepted:
                    self.reply({"success": False}, 400)
                    return
                owner.state = "loading" if self.path.endswith("load") else "unloading"
                owner.load_polls = owner.unload_polls = 0
                if owner.mode == "wrong-success":
                    self.reply({"success": 1})
                else:
                    self.reply({"success": True})

        self.state = "unloaded"
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


FAKE_SERVER = r'''#!/usr/bin/env python3
import json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

args = sys.argv[1:]
def value(flag):
    return args[args.index(flag) + 1]
log = Path(os.environ["FAKE_LOG"])
log.write_text(json.dumps({"args": args, "env": {k: os.environ.get(k) for k in
    ("LLAMA_ARG_MODEL", "LLAMA_ARG_CONFIG", "HOME", "XDG_CONFIG_HOME")}}, indent=2))
preset = Path(value("--models-preset"))
expected = os.environ.get("EXPECTED_SECTIONS")
if expected is not None:
    sections = [line[1:-1] for line in preset.read_text().splitlines()
                if line.startswith("[") and line.endswith("]")]
    if sections != json.loads(expected):
        raise SystemExit("unexpected preset sections: " + repr(sections))
if os.environ.get("REQUIRE_SANITIZED_ENV") == "1":
    if os.environ.get("LLAMA_ARG_MODEL") or os.environ.get("LLAMA_ARG_CONFIG"):
        raise SystemExit("inherited llama model/config escaped router mode")
    for candidate in (
        Path(os.environ["HOME"]) / ".config" / "llama-server" / "config.ini",
        Path(os.environ["XDG_CONFIG_HOME"]) / "llama-server" / "config.ini",
    ):
        if candidate.exists() and "model" in candidate.read_text().lower():
            raise SystemExit("user/system config selected a model")
child = os.environ.get("SPAWN_IGNORING_CHILD")
if child:
    pid = os.fork()
    if pid == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(child).write_text(str(os.getpid()))
        while True: time.sleep(1)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(os.environ.get("FAKE_PID", "/dev/null")).write_text(str(os.getpid()))
host = value("--host"); port = int(value("--port"))
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def do_GET(self):
        if self.path == "/health":
            body = b'{"status":"ok"}'
        else:
            models_dir = Path(value("--models-dir"))
            rows = [{"id": p.stem, "status": "unloaded"}
                    for p in sorted(models_dir.glob("*.gguf")) if p.is_file()]
            body = json.dumps({"object": "list", "data": rows}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body)))
        self.end_headers(); self.wfile.write(body)
HTTPServer((host, port), Handler).serve_forever()
'''


class Issue7ReviewRegressions(unittest.TestCase):
    maxDiff = None

    def fake_server(self, directory):
        path = Path(directory) / "fake-server.py"
        path.write_text(FAKE_SERVER)
        path.chmod(0o755)
        return path

    def config(self, directory, **changes):
        value = json.loads(CONFIG.read_text())
        value.update(changes)
        path = Path(directory) / "router.json"
        path.write_text(json.dumps(value, allow_nan=True))
        return path

    def start_router(self, directory, *, models=None, runtime=None, presets=PRESETS,
                     environment=None, config_changes=None):
        directory = Path(directory)
        models = models or directory / "models"
        runtime = runtime or directory / "runtime"
        models.mkdir(parents=True, exist_ok=True)
        runtime.mkdir(parents=True, exist_ok=True)
        port = self.free_port()
        config = self.config(directory, port=port, **(config_changes or {}))
        fake = self.fake_server(directory)
        log = directory / "server-log.json"
        env = os.environ.copy()
        env.update({"FAKE_LOG": str(log), "FAKE_PID": str(directory / "server.pid"),
                    "HOME": str(directory / "home"),
                    "XDG_CONFIG_HOME": str(directory / "xdg")})
        if environment:
            env.update(environment)
        (directory / "home").mkdir(exist_ok=True)
        (directory / "xdg").mkdir(exist_ok=True)
        process = subprocess.Popen(
            [str(LAUNCHER), "--server", str(fake), "--models-dir", str(models),
             "--runtime-dir", str(runtime), "--config", str(config),
             "--presets", str(presets), "--foreground"],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            text=True, start_new_session=True,
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if log.exists():
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=.1) as response:
                        if response.status == 200:
                            return process, port, log, runtime
                except OSError:
                    pass
            if process.poll() is not None:
                break
            time.sleep(.02)
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=1)
        pid_file = directory / "server.pid"
        if pid_file.exists():
            try:
                os.killpg(int(pid_file.read_text()), signal.SIGKILL)
            except (ProcessLookupError, ValueError):
                pass
        self.fail("router fixture did not start")

    @staticmethod
    def free_port():
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def stop_router(process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)

    @staticmethod
    def manifest(directory, filename, content=b"original model"):
        model = Path(directory) / filename
        model.write_bytes(content)
        row = {"id": "fixture-q8", "filename": filename,
               "sha256": hashlib.sha256(content).hexdigest()}
        path = Path(directory) / "models.json"
        path.write_text(json.dumps({"schema_version": 1, "artifacts": [row]}))
        return path, model

    def run_helper(self, action, manifest, models, endpoint):
        return subprocess.run(
            [str(HELPER), action, "--model-id", "fixture-q8", "--models-dir", str(models),
             "--manifest", str(manifest), "--base-url", endpoint],
            cwd=ROOT, text=True, capture_output=True, timeout=5,
        )

    def test_models_dir_uses_extensionless_ids_and_optional_q6_is_file_driven(self):
        with tempfile.TemporaryDirectory(prefix="issue7-presets-") as tmp:
            root = Path(tmp)
            models = root / "models"
            models.mkdir()
            (models / "Qwen3.5-27B-Q8_0.gguf").write_bytes(b"q8")
            process, _port, log, _runtime = self.start_router(
                root, models=models,
                environment={"EXPECTED_SECTIONS": json.dumps(["Qwen3.5-27B-Q8_0.gguf"])},
            )
            try:
                with urlopen(f"http://127.0.0.1:{_port}/models") as response:
                    ids = {row["id"] for row in json.loads(response.read())["data"]}
                self.assertEqual(ids, {"Qwen3.5-27B-Q8_0"})
            finally:
                self.stop_router(process)

            # Adding the manifest-named file makes Q6 appear without changing
            # launcher/helper source or the tracked preset JSON.
            (models / "Qwen3.5-27B-Q6_K.gguf").write_bytes(b"q6")
            process, _port, _log, _runtime = self.start_router(
                root, models=models,
                environment={"EXPECTED_SECTIONS": json.dumps([
                    "Qwen3.5-27B-Q8_0.gguf", "Qwen3.5-27B-Q6_K.gguf"])},
            )
            self.stop_router(process)
            self.assertTrue(log.exists())

    def test_inherited_model_and_user_system_configs_cannot_escape_router_mode(self):
        with tempfile.TemporaryDirectory(prefix="issue7-env-") as tmp:
            root = Path(tmp)
            for base in (root / "home" / ".config" / "llama-server",
                         root / "xdg" / "llama-server"):
                base.mkdir(parents=True)
                (base / "config.ini").write_text("model = /poison/model.gguf\n")
            process, _port, _log, _runtime = self.start_router(
                root, environment={"LLAMA_ARG_MODEL": "/poison/model.gguf",
                                   "LLAMA_ARG_CONFIG": "/poison/config.ini",
                                   "REQUIRE_SANITIZED_ENV": "1"},
            )
            self.stop_router(process)

    def test_lifecycle_requires_exact_success_and_polls_to_loaded_and_unloaded(self):
        with tempfile.TemporaryDirectory(prefix="issue7-lifecycle-") as tmp:
            root = Path(tmp)
            manifest, model = self.manifest(root, "Qwen3.5-27B-Q8_0.gguf")
            with B10446Endpoint() as endpoint:
                loaded = self.run_helper("load", manifest, root, endpoint.url)
                self.assertEqual(loaded.returncode, 0, loaded.stderr)
                self.assertGreaterEqual(endpoint.load_polls, 2)
                self.assertEqual(endpoint.requests[0], ("/models/load", {"model": "Qwen3.5-27B-Q8_0"}))
                unloaded = self.run_helper("unload", manifest, root, endpoint.url)
                self.assertEqual(unloaded.returncode, 0, unloaded.stderr)
                self.assertGreaterEqual(endpoint.unload_polls, 2)
                self.assertEqual(endpoint.requests[-1], ("/models/unload", {"model": "Qwen3.5-27B-Q8_0"}))
            self.assertEqual(model.read_bytes(), b"original model")

    def test_lifecycle_rejects_non_boolean_success(self):
        with tempfile.TemporaryDirectory(prefix="issue7-lifecycle-response-") as tmp:
            root = Path(tmp)
            manifest, _model = self.manifest(root, "Qwen3.5-27B-Q8_0.gguf")
            with B10446Endpoint(mode="wrong-success") as endpoint:
                result = self.run_helper("load", manifest, root, endpoint.url)
                self.assertNotEqual(result.returncode, 0)

    def test_lifecycle_timeout_and_false_success_are_failures(self):
        with tempfile.TemporaryDirectory(prefix="issue7-lifecycle-timeout-") as tmp:
            root = Path(tmp)
            manifest, _model = self.manifest(root, "Qwen3.5-27B-Q8_0.gguf")
            with B10446Endpoint(mode="load-timeout", strict=False) as endpoint:
                result = self.run_helper("load", manifest, root, endpoint.url)
                self.assertNotEqual(result.returncode, 0)
                self.assertGreaterEqual(endpoint.load_polls, 2)
            with B10446Endpoint(mode="unload-timeout", strict=False) as endpoint:
                result = self.run_helper("unload", manifest, root, endpoint.url)
                self.assertNotEqual(result.returncode, 0)
                self.assertGreaterEqual(endpoint.unload_polls, 2)

    def test_shared_downloader_lock_and_identity_are_held_across_async_load(self):
        with tempfile.TemporaryDirectory(prefix="issue7-identity-") as tmp:
            root = Path(tmp)
            manifest, model = self.manifest(root, "Qwen3.5-27B-Q8_0.gguf")
            lock_path = model.parent / ("." + model.name + ".lock")
            lock_holder = subprocess.Popen(
                [sys.executable, "-c", textwrap.dedent("""
                    import fcntl, sys, time
                    with open(sys.argv[1], 'w') as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX)
                        print('locked', flush=True)
                        time.sleep(1.5)
                """), str(lock_path)], stdout=subprocess.PIPE, text=True,
            )
            self.assertEqual(lock_holder.stdout.readline().strip(), "locked")
            with B10446Endpoint() as endpoint:
                pending = subprocess.Popen(
                    [str(HELPER), "load", "--model-id", "fixture-q8", "--models-dir", str(root),
                     "--manifest", str(manifest), "--base-url", endpoint.url],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                time.sleep(.25)
                self.assertFalse(endpoint.requests, "load started while downloader held its lock")
                lock_holder.terminate(); lock_holder.wait(timeout=1)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline and not endpoint.requests:
                    time.sleep(.02)
                self.assertTrue(endpoint.requests, "load request was never sent")
                model.write_bytes(b"replacement model with a different identity")
                stdout, stderr = pending.communicate(timeout=5)
                self.assertNotEqual(pending.returncode, 0, stdout + stderr)
                self.assertRegex((stdout + stderr).lower(), r"identity|changed|replaced|checksum")

    def test_term_cleanup_has_finite_grace_then_kill(self):
        with tempfile.TemporaryDirectory(prefix="issue7-term-") as tmp:
            root = Path(tmp)
            child_file = root / "child.pid"
            server_pid_file = root / "server.pid"
            process, _port, _log, _runtime = self.start_router(
                root, environment={"SPAWN_IGNORING_CHILD": str(child_file),
                                   "FAKE_PID": str(server_pid_file)},
            )
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=1)
                if server_pid_file.exists():
                    try:
                        os.killpg(int(server_pid_file.read_text()), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                self.fail("TERM cleanup waited forever for an uncooperative server")
            if child_file.exists():
                child_pid = int(child_file.read_text())
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)

    def test_nan_infinity_and_invalid_timeouts_are_rejected_before_start(self):
        bad_values = [float("nan"), float("inf"), 0, -1, "not-a-timeout"]
        with tempfile.TemporaryDirectory(prefix="issue7-timeouts-") as tmp:
            root = Path(tmp)
            for field in ("startup_timeout_seconds", "health_timeout_seconds"):
                for value in bad_values:
                    log = root / f"{field}-{str(value)}.log"
                    config = self.config(root, **{field: value})
                    fake = self.fake_server(root)
                    env = os.environ.copy(); env["FAKE_LOG"] = str(log)
                    env["FAKE_PID"] = str(root / f"{field}-{str(value)}.pid")
                    result = subprocess.run(
                        [str(LAUNCHER), "--server", str(fake), "--config", str(config),
                         "--presets", str(PRESETS), "--models-dir", str(root / "models"),
                         "--runtime-dir", str(root / "runtime")],
                        cwd=ROOT, env=env, text=True, capture_output=True, timeout=3,
                        start_new_session=True,
                    )
                    pid_file = root / f"{field}-{str(value)}.pid"
                    if pid_file.exists():
                        try:
                            os.killpg(int(pid_file.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    self.assertNotEqual(result.returncode, 0, f"accepted {field}={value!r}")
                    self.assertFalse(log.exists(), result.stderr)

    def test_newline_and_ini_injection_filenames_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="issue7-ini-") as tmp:
            root = Path(tmp)
            data = json.loads(PRESETS.read_text())
            data["presets"]["qwen3.5-27b-q8_0"]["filename"] = "ok.gguf\n[evil]"
            presets = root / "malicious-presets.json"
            presets.write_text(json.dumps(data))
            fake = self.fake_server(root)
            env = os.environ.copy(); env["FAKE_LOG"] = str(root / "server.log")
            env["FAKE_PID"] = str(root / "server.pid")
            env["EXPECTED_SECTIONS"] = "[]"
            result = subprocess.run(
                [str(LAUNCHER), "--server", str(fake), "--config",
                 str(self.config(root, startup_timeout_seconds=0.2)),
                 "--presets", str(presets), "--models-dir", str(root / "models"),
                 "--runtime-dir", str(root / "runtime")],
                cwd=ROOT, env=env, text=True, capture_output=True, timeout=3,
                start_new_session=True,
            )
            if (root / "server.pid").exists():
                try:
                    os.killpg(int((root / "server.pid").read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / "server.log").exists(),
                             "newline filename reached the router process")

    def test_runtime_preset_is_unique_private_and_existing_race_targets_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix="issue7-runtime-") as tmp:
            root = Path(tmp); runtime = root / "runtime"; runtime.mkdir()
            process, _port, log, runtime = self.start_router(root, runtime=runtime)
            preset_path = Path(json.loads(log.read_text())["args"][json.loads(log.read_text())["args"].index("--models-preset") + 1])
            self.assertNotEqual(preset_path, runtime / "router-presets.ini")
            self.assertEqual(stat.S_IMODE(preset_path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(preset_path.stat().st_mode), 0o600)
            self.stop_router(process)
            process, _port, log, _runtime = self.start_router(root, runtime=runtime)
            second = Path(json.loads(log.read_text())["args"][json.loads(log.read_text())["args"].index("--models-preset") + 1])
            self.assertNotEqual(preset_path, second)
            self.stop_router(process)

            outside = root / "outside.ini"; outside.write_text("sentinel")
            (runtime / "router-presets.ini").symlink_to(outside)
            process = subprocess.run(
                [str(LAUNCHER), "--server", str(self.fake_server(root)), "--config", str(self.config(root)),
                 "--presets", str(PRESETS), "--models-dir", str(root / "models"),
                 "--runtime-dir", str(runtime)],
                cwd=ROOT, text=True, capture_output=True, timeout=3,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertEqual(outside.read_text(), "sentinel")


if __name__ == "__main__":
    unittest.main()
