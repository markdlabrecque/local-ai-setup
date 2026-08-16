"""Issue #7 cycle-2 red contracts for findings 006 and 007.

These tests use only disposable files and a bounded fake server.  The system
configuration check is exercised through a test-only path override named
``LOCAL_AI_TEST_SYSTEM_CONFIG_PATH``; without ``LOCAL_AI_TEST_MODE=1`` that
override is unsafe and must be rejected.  In production the check therefore
has no override and its default remains exactly ``/etc/llama.cpp/config.ini``.
"""
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).parents[1]
LAUNCHER = ROOT / "scripts" / "run-router.sh"
CONFIG = ROOT / "config" / "router.json"
PRESETS = ROOT / "config" / "router-presets.json"

CACHE_ENV_NAMES = {
    "LLAMA_CACHE",
    "HF_HOME",
    "HF_HUB_CACHE",
    "HUGGINGFACE_HUB_CACHE",
    "TRANSFORMERS_CACHE",
    "HF_DATASETS_CACHE",
    "HF_MODULES_CACHE",
    "HF_ASSETS_CACHE",
    "HF_TOKEN_PATH",
    "XDG_CACHE_HOME",
}
FAKE_SERVER = r'''#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

log = Path(os.environ["FAKE_LOG"])
names = sorted({key for key in os.environ if key.startswith("HF_")} |
               {"LLAMA_CACHE", "XDG_CACHE_HOME"})
log.write_text(json.dumps({"env": {key: os.environ.get(key) for key in names}},
                          sort_keys=True))

# This mode deliberately lets the setsid leader exit before the launcher is
# terminated.  The child remains in the leader's process group and ignores
# TERM, so cleanup must target the complete group rather than the leader PID.
if os.environ.get("EARLY_EXIT_LEADER") == "1":
    pid = os.fork()
    if pid == 0:
        import signal, time
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path(os.environ["DESCENDANT_PID"]).write_text(str(os.getpid()))
        while True:
            time.sleep(1)
    raise SystemExit(0)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass
    def do_GET(self):  # noqa: N802
        body = b'{"status":"ok"}' if self.path == "/health" else b'{}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

host = sys.argv[sys.argv.index("--host") + 1]
port = int(sys.argv[sys.argv.index("--port") + 1])
HTTPServer((host, port), Handler).serve_forever()
'''


class Issue7Cycle2Regressions(unittest.TestCase):
    maxDiff = None

    @staticmethod
    def free_port():
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def write_fake(self, root):
        fake = root / "fake-server.py"
        fake.write_text(FAKE_SERVER)
        fake.chmod(0o755)
        return fake

    def write_config(self, root, **changes):
        data = json.loads(CONFIG.read_text())
        data.update(changes)
        config = root / "router.json"
        config.write_text(json.dumps(data))
        return config

    def launch(self, root, *, environment=None, startup_timeout=1):
        root = Path(root)
        runtime = root / "runtime"
        models = root / "models"
        runtime.mkdir(exist_ok=True)
        models.mkdir(exist_ok=True)
        (root / "home").mkdir(exist_ok=True)
        (root / "xdg").mkdir(exist_ok=True)
        port = self.free_port()
        config = self.write_config(root, port=port,
                                   startup_timeout_seconds=startup_timeout)
        fake = self.write_fake(root)
        env = os.environ.copy()
        env.update({"FAKE_LOG": str(root / "fake.log"),
                    "HOME": str(root / "home"),
                    "XDG_CONFIG_HOME": str(root / "xdg")})
        if environment:
            env.update(environment)
        process = subprocess.Popen(
            [str(LAUNCHER), "--server", str(fake), "--models-dir", str(models),
             "--runtime-dir", str(runtime), "--config", str(config),
             "--presets", str(PRESETS), "--foreground"],
            cwd=ROOT, env=env, text=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
        )
        return process, port, runtime

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)

    def wait_for_health(self, process, port, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urlopen(f"http://127.0.0.1:{port}/health", timeout=.1) as response:
                    if response.status == 200:
                        return
            except OSError:
                pass
            if process.poll() is not None:
                break
            time.sleep(.02)
        self.fail("bounded fake router did not become healthy")

    def test_inherited_llama_and_huggingface_caches_are_private_or_unset(self):
        with tempfile.TemporaryDirectory(prefix="issue7-cache-") as tmp:
            root = Path(tmp)
            poison = root / "inherited-poison"
            poison.mkdir()
            env = {name: str(poison / name.lower()) for name in CACHE_ENV_NAMES}
            # Include every inherited HF_* name as a poisoned value too; this
            # catches a sanitizer that only handles a short allowlist.
            env.update({name: str(poison / name.lower()) for name in os.environ
                        if name.startswith("HF_")})
            process, port, runtime = self.launch(root, environment=env)
            try:
                self.wait_for_health(process, port)
                snapshot = json.loads((root / "fake.log").read_text())["env"]
            finally:
                self.stop(process)

            for name, value in snapshot.items():
                if value is None:
                    continue
                path = Path(value).resolve()
                self.assertTrue(
                    runtime.resolve() == path or runtime.resolve() in path.parents,
                    f"{name} escaped private runtime cache root: {value}",
                )
                self.assertTrue(path.is_dir(), f"{name} cache is not a private directory")
                self.assertEqual(list(path.iterdir()), [], f"{name} inherited nonempty cache")
            self.assertNotEqual(snapshot.get("LLAMA_CACHE"), str(poison / "llama_cache"))
            self.assertNotEqual(snapshot.get("XDG_CACHE_HOME"), str(poison / "xdg_cache_home"))

    def test_nonempty_system_config_fails_before_server_without_mutating_etc(self):
        with tempfile.TemporaryDirectory(prefix="issue7-system-config-") as tmp:
            root = Path(tmp)
            system_config = root / "etc-llama.cpp-config.ini"
            system_config.write_text("model = /poison/model.gguf\n")
            process, _port, _runtime = self.launch(
                root,
                startup_timeout=.2,
                environment={"LOCAL_AI_TEST_MODE": "1",
                             "LOCAL_AI_TEST_SYSTEM_CONFIG_PATH": str(system_config)},
            )
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.stop(process)
            self.assertNotEqual(process.returncode, 0,
                                "nonempty system config was accepted")
            self.assertFalse((root / "fake.log").exists(),
                             "server started despite nonempty system config")
            self.assertEqual(system_config.read_text(), "model = /poison/model.gguf\n")

    def test_system_config_override_is_rejected_outside_explicit_test_mode(self):
        with tempfile.TemporaryDirectory(prefix="issue7-system-config-production-") as tmp:
            root = Path(tmp)
            system_config = root / "unsafe-override.ini"
            system_config.write_text("model = /poison/model.gguf\n")
            process, _port, _runtime = self.launch(
                root, startup_timeout=.2,
                environment={"LOCAL_AI_TEST_SYSTEM_CONFIG_PATH": str(system_config)},
            )
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.stop(process)
            self.assertNotEqual(process.returncode, 0,
                                "unsafe system-config override was accepted")
            self.assertFalse((root / "fake.log").exists(),
                             "unsafe system-config override reached server")

    def test_cleanup_kills_ignoring_descendant_after_setsid_leader_exits(self):
        with tempfile.TemporaryDirectory(prefix="issue7-early-leader-") as tmp:
            root = Path(tmp)
            descendant_file = root / "descendant.pid"
            process, _port, _runtime = self.launch(
                root, startup_timeout=.2,
                environment={"EARLY_EXIT_LEADER": "1",
                             "DESCENDANT_PID": str(descendant_file)},
            )
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not descendant_file.exists():
                time.sleep(.02)
            self.assertTrue(descendant_file.exists(), "fake descendant did not start")
            descendant = int(descendant_file.read_text())
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.stop(process)
            self.assertNotEqual(process.returncode, 0,
                                "early-leader startup did not fail closed")
            for _ in range(50):
                try:
                    os.kill(descendant, 0)
                except ProcessLookupError:
                    break
                time.sleep(.02)
            else:
                os.kill(descendant, signal.SIGKILL)
                self.fail("cleanup left an ignoring descendant after leader exit")


if __name__ == "__main__":
    unittest.main()
