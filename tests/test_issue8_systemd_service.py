"""Issue #8 red contract: portable user service and safe lifecycle.

The service is deliberately tested through a bounded fake ``systemctl``.  No
real user manager, router, model, or runtime binary is started by this suite.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
UNIT_SOURCE = ROOT / "config" / "local-ai-router.service"
INSTALL = ROOT / "scripts" / "install-router-service.sh"
UNINSTALL = ROOT / "scripts" / "uninstall-router-service.sh"
DOC = ROOT / "docs" / "issue-8-systemd.md"
SERVICE = "local-ai-router.service"

BAD_HOME_PATH = re.compile(r"(?:/home/|/root/|/Users/|(?:^|\s)~/)")


def directives(text):
    """Return systemd directives without configparser's % interpolation."""
    section = None
    result = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section and "=" in line:
            key, value = line.split("=", 1)
            result.setdefault(section, {}).setdefault(key, []).append(value)
    return result


def systemd_bytes(value):
    value = value.strip().lower()
    if value in {"infinity", "inf", "max"}:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([kmgtpe]?i?b)?", value)
    if not match:
        raise AssertionError(f"unparseable resource limit: {value!r}")
    number = float(match.group(1))
    suffix = match.group(2) or "b"
    factors = {"b": 1, "k": 1000, "m": 1000**2, "g": 1000**3,
               "t": 1000**4, "p": 1000**5, "e": 1000**6,
               "kb": 1000, "mb": 1000**2, "gb": 1000**3,
               "tb": 1000**4, "pb": 1000**5, "eb": 1000**6,
               "kib": 1024, "mib": 1024**2, "gib": 1024**3,
               "tib": 1024**4, "pib": 1024**5, "eib": 1024**6}
    return int(number * factors[suffix])


class Issue8SystemdService(unittest.TestCase):
    maxDiff = None

    def test_portable_hardened_unit_has_bounded_router_contract(self):
        self.assertTrue(UNIT_SOURCE.is_file(), "missing tracked user service unit")
        text = UNIT_SOURCE.read_text()
        self.assertIsNone(BAD_HOME_PATH.search(text), "unit embeds a machine home path")
        self.assertRegex(text, r"(?m)^EnvironmentFile=-%E/.+")
        self.assertRegex(text, r"(?m)^ExecStart=.*%h.*run-router\.sh")
        self.assertRegex(text, r"(?m)^ExecStart=.*(?:run-router\.sh|LOCAL_AI_RUN_ROUTER)")
        self.assertRegex(text, r"(?m)^ExecStart=.*--foreground(?:\s|$)")

        parsed = directives(text)
        unit = parsed.get("Unit", {})
        service = parsed.get("Service", {})
        self.assertIn("network-online.target", " ".join(unit.get("After", [])))
        self.assertIn("network-online.target", " ".join(unit.get("Wants", [])))
        self.assertEqual(parsed.get("Install", {}).get("WantedBy", [None])[-1], "default.target")
        self.assertEqual(service.get("Restart", [None])[-1], ["on-failure"][-1])
        self.assertTrue(1 <= int(float(service["RestartSec"][-1].rstrip("s"))) <= 30)
        self.assertTrue(1 <= int(float(unit["StartLimitBurst"][-1])) <= 10)
        interval = unit["StartLimitIntervalSec"][-1].rstrip("s")
        self.assertTrue(60 <= int(float(interval)) <= 3600)
        self.assertTrue(1 <= int(float(service["TimeoutStopSec"][-1].rstrip("s"))) <= 60)
        self.assertEqual(service.get("KillMode", [None])[-1], "control-group")

        # The measured baseline needs at least 8 GiB of RAM headroom.  The
        # hard ceiling must still admit the observed ~27.5 GiB peak, while
        # swap remains finite and materially below the host's full 121 GiB.
        ram_floor = 8 * 1024**3
        peak_floor = int(27.5 * 1024**3)
        for key in ("MemoryHigh", "MemoryMax", "MemoryLimit"):
            for value in service.get(key, []):
                actual = systemd_bytes(value)
                self.assertIsNotNone(actual, f"{key} must be finite: {value}")
                self.assertGreaterEqual(actual, ram_floor,
                                        f"{key} caps below RAM baseline: {value}")
        hard_caps = service.get("MemoryMax", [])
        self.assertTrue(hard_caps, "service needs an explicit MemoryMax ceiling")
        self.assertTrue(any(systemd_bytes(value) >= peak_floor for value in hard_caps),
                        "MemoryMax rejects the measured 27.5 GiB peak")
        swap_caps = service.get("MemorySwapMax", [])
        self.assertTrue(swap_caps, "service needs an explicit swap ceiling")
        for value in swap_caps:
            actual = systemd_bytes(value)
            self.assertIsNotNone(actual, "swap must not be unlimited")
            self.assertGreater(actual, 0, "swap ceiling must retain a finite emergency reserve")
            self.assertLess(actual, 121 * 1024**3,
                            "swap ceiling must prevent swapping across the full host")

        output = " ".join(service.get("StandardOutput", []) + service.get("StandardError", []))
        self.assertIn("journal", output, "service logs must remain visible in the journal")
        self.assertEqual(service.get("NoNewPrivileges", [None])[-1], "yes")
        self.assertEqual(service.get("PrivateTmp", [None])[-1], "yes")

        # The tracked launcher is the owner of localhost and no-autoload
        # policy; the unit must not replace that policy with model flags.
        launcher = (ROOT / "scripts" / "run-router.sh").read_text()
        self.assertIn('127.0.0.1', launcher)
        self.assertIn('--no-models-autoload', launcher)
        self.assertNotRegex(text, r"(?m)^ExecStart=.*(?:--model(?:=|\s)|\s-m\s)")

    def fake_systemctl(self, root):
        bindir = root / "bin"
        bindir.mkdir(exist_ok=True)
        log = root / "systemctl.jsonl"
        fake = bindir / "systemctl"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"with open({str(log)!r}, 'a') as f: f.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if os.environ.get('FAKE_SYSTEMCTL_SLEEP'): raise SystemExit('unbounded fake')\n"
        )
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        return bindir, log

    def run_lifecycle(self, script, root, *args):
        env = os.environ.copy()
        bindir, _ = self.fake_systemctl(root)
        # Spaces and shell punctuation exercise path escaping without ever
        # allowing a test fixture to touch the real XDG tree.
        home = root / "home with spaces;literal"
        xdg = root / "xdg config;literal"
        data = root / "data with spaces"
        home.mkdir(exist_ok=True)
        xdg.mkdir(exist_ok=True)
        data.mkdir(exist_ok=True)
        env.update({"HOME": str(home), "XDG_CONFIG_HOME": str(xdg),
                    "XDG_DATA_HOME": str(data),
                    "PATH": f"{bindir}:{env['PATH']}"})
        return subprocess.run([str(script), *args], cwd=ROOT, env=env,
                              text=True, capture_output=True, timeout=5)

    def installed_paths(self, root):
        xdg = next(root.glob("xdg config*"))
        unit = xdg / "systemd" / "user" / SERVICE
        envs = list(xdg.rglob("*.env"))
        return unit, envs

    def test_install_is_bounded_idempotent_and_uses_safe_escaped_environment(self):
        self.assertTrue(INSTALL.is_file(), "missing service installer")
        with tempfile.TemporaryDirectory(prefix="issue8-install-") as tmp:
            root = Path(tmp)
            first = self.run_lifecycle(INSTALL, root, "--enable", "--start")
            self.assertEqual(first.returncode, 0, first.stderr)
            unit, envs = self.installed_paths(root)
            self.assertTrue(unit.is_file(), "installer did not install the unit")
            self.assertEqual(len(envs), 1, "installer did not create one environment file")
            unit_before, env_before = unit.read_bytes(), envs[0].read_bytes()
            self.assertNotIn(b"LD_PRELOAD", env_before)
            self.assertNotIn(b"LLAMA_ARG_MODEL", env_before)
            allowed = {"LOCAL_AI_SERVER", "LOCAL_AI_MODEL_DIR", "LOCAL_AI_CONFIG_DIR",
                       "LOCAL_AI_RUNTIME_DIR", "LOCAL_AI_RUN_ROUTER"}
            names = {line.split("=", 1)[0] for line in env_before.decode().splitlines()
                     if line.strip() and not line.startswith("#")}
            self.assertTrue(names <= allowed, f"uncontrolled environment keys: {names - allowed}")
            self.assertTrue(any('"' in line or "\\" in line for line in env_before.decode().splitlines()),
                            "path values are not escaped for EnvironmentFile")

            second = self.run_lifecycle(INSTALL, root, "--enable", "--start")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(unit_before, unit.read_bytes(), "install is not idempotent")
            self.assertEqual(env_before, envs[0].read_bytes(), "env install is not idempotent")

            calls = [json.loads(line) for line in (root / "systemctl.jsonl").read_text().splitlines()]
            flat = [" ".join(call) for call in calls]
            self.assertTrue(any("--user daemon-reload" in call for call in flat))
            self.assertTrue(any("--user enable" in call for call in flat))
            self.assertTrue(any("--user start" in call or "--user enable --now" in call for call in flat))

    def test_uninstall_is_data_preserving_and_idempotent(self):
        self.assertTrue(UNINSTALL.is_file(), "missing service uninstaller")
        with tempfile.TemporaryDirectory(prefix="issue8-uninstall-") as tmp:
            root = Path(tmp)
            installed = self.run_lifecycle(INSTALL, root)
            self.assertEqual(installed.returncode, 0, installed.stderr)
            unit, envs = self.installed_paths(root)
            model = root / "data with spaces" / "local-ai" / "models" / "28GB-model.gguf"
            config = root / "xdg config;literal" / "local-ai" / "config.json"
            runtime = root / "data with spaces" / "local-ai" / "runtime" / "state"
            binary = root / "data with spaces" / "local-ai" / "runtime" / "bin" / "llama-server"
            for path in (model, config, runtime, binary):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("must survive uninstall")
            before = {path: path.read_bytes() for path in (model, config, runtime, binary)}

            result = self.run_lifecycle(UNINSTALL, root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(unit.exists(), "uninstaller left installed unit")
            self.assertFalse(envs[0].exists(), "uninstaller left installed env file")
            for path, content in before.items():
                self.assertTrue(path.exists(), f"uninstaller removed user data: {path}")
                self.assertEqual(path.read_bytes(), content)
            again = self.run_lifecycle(UNINSTALL, root)
            self.assertEqual(again.returncode, 0, again.stderr)
            calls = [json.loads(line) for line in (root / "systemctl.jsonl").read_text().splitlines()]
            flat = [" ".join(call) for call in calls]
            self.assertTrue(any("--user stop" in call for call in flat))
            self.assertTrue(any("--user disable" in call for call in flat))

    def test_install_and_uninstall_refuse_symlink_targets(self):
        self.assertTrue(INSTALL.is_file(), "missing service installer")
        self.assertTrue(UNINSTALL.is_file(), "missing service uninstaller")
        with tempfile.TemporaryDirectory(prefix="issue8-symlink-") as tmp:
            root = Path(tmp)
            first = self.run_lifecycle(INSTALL, root)
            self.assertEqual(first.returncode, 0, first.stderr)
            unit, envs = self.installed_paths(root)
            outside_unit = root / "outside-unit"
            outside_env = root / "outside-env"
            outside_unit.write_text("outside unit sentinel")
            outside_env.write_text("outside env sentinel")
            unit.unlink()
            envs[0].unlink()
            unit.symlink_to(outside_unit)
            envs[0].symlink_to(outside_env)

            rejected_install = self.run_lifecycle(INSTALL, root)
            self.assertNotEqual(rejected_install.returncode, 0,
                                "installer followed a unit/env symlink")
            self.assertEqual(outside_unit.read_text(), "outside unit sentinel")
            self.assertEqual(outside_env.read_text(), "outside env sentinel")
            rejected_uninstall = self.run_lifecycle(UNINSTALL, root)
            self.assertNotEqual(rejected_uninstall.returncode, 0,
                                "uninstaller removed symlink targets")
            self.assertTrue(unit.is_symlink() and envs[0].is_symlink())
            self.assertEqual(outside_unit.read_text(), "outside unit sentinel")
            self.assertEqual(outside_env.read_text(), "outside env sentinel")

    def test_systemd_analyze_verify_is_a_real_gate_when_available(self):
        analyzer = shutil.which("systemd-analyze")
        if analyzer is None:
            self.skipTest("systemd-analyze is unavailable")
        # verify also checks ExecStart existence.  Supply only that disposable
        # runtime fixture so the gate validates the tracked unit's actual
        # systemd syntax without writing to the host user's home.
        with tempfile.TemporaryDirectory(prefix="issue8-verify-") as tmp:
            root = Path(tmp)
            launcher = root / "run-router.sh"
            launcher.write_text("#!/bin/sh\\n")
            launcher.chmod(0o755)
            text = UNIT_SOURCE.read_text()
            self.assertEqual(text.count("%h/.local/bin/run-router.sh"), 1)
            checked = root / SERVICE
            checked.write_text(text.replace("%h/.local/bin/run-router.sh", str(launcher)))
            result = subprocess.run(
                [analyzer, "verify", str(checked)],
                cwd=ROOT, text=True, capture_output=True, timeout=10,
            )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertNotRegex(output, r"(?i)invalid|ignoring|failed",
                             "systemd-analyze verify reported a unit error")

    def test_all_resource_byte_values_use_systemd_byte_syntax(self):
        text = UNIT_SOURCE.read_text()
        parsed = directives(text).get("Service", {})
        byte_keys = {"MemoryHigh", "MemoryMax", "MemoryLimit", "MemorySwapMax"}
        # systemd accepts integer quantities with its documented binary/decimal
        # suffixes; spellings outside that grammar must not be silently treated
        # as a different unit by a hand-rolled parser.
        syntax = re.compile(r"^[0-9]+(?:[KMGTPE](?:B)?|[kmgtpe](?:b)?|B|b)?$")
        for key in byte_keys:
            for value in parsed.get(key, []):
                self.assertRegex(value, syntax, f"invalid systemd byte syntax: {key}={value}")

    def test_installer_refuses_unowned_regular_files_and_marks_owned_artifacts(self):
        artifacts = {
            "unit": lambda root: root / "xdg config;literal" / "systemd" / "user" / SERVICE,
            "environment": lambda root: root / "xdg config;literal" / "local-ai" / "router.env",
            "launcher": lambda root: root / "home with spaces;literal" / ".local" / "bin" / "run-router.sh",
        }
        for name, path_for in artifacts.items():
            with self.subTest(artifact=name), tempfile.TemporaryDirectory(prefix="issue8-owned-") as tmp:
                root = Path(tmp)
                path = path_for(root)
                path.parent.mkdir(parents=True)
                path.write_text(f"unowned {name} sentinel\\n")
                before = path.read_bytes()
                result = self.run_lifecycle(INSTALL, root)
                self.assertNotEqual(result.returncode, 0,
                                    f"installer overwrote unowned {name}")
                self.assertEqual(path.read_bytes(), before)

        with tempfile.TemporaryDirectory(prefix="issue8-owned-markers-") as tmp:
            root = Path(tmp)
            result = self.run_lifecycle(INSTALL, root)
            self.assertEqual(result.returncode, 0, result.stderr)
            unit, envs = self.installed_paths(root)
            launcher = root / "home with spaces;literal" / ".local" / "bin" / "run-router.sh"
            for artifact in (unit, envs[0], launcher):
                self.assertIn("Managed by install-router-service.sh", artifact.read_text(),
                              f"{artifact} has no installer ownership marker")

    def test_uninstall_refuses_and_preserves_unowned_artifacts(self):
        targets = ("unit", "environment", "launcher")
        for target in targets:
            with self.subTest(artifact=target), tempfile.TemporaryDirectory(prefix="issue8-unowned-") as tmp:
                root = Path(tmp)
                installed = self.run_lifecycle(INSTALL, root)
                self.assertEqual(installed.returncode, 0, installed.stderr)
                unit, envs = self.installed_paths(root)
                paths = {"unit": unit, "environment": envs[0],
                         "launcher": root / "home with spaces;literal" / ".local" / "bin" / "run-router.sh"}
                paths[target].write_text(f"unowned replacement {target}\\n")
                result = self.run_lifecycle(UNINSTALL, root)
                self.assertNotEqual(result.returncode, 0,
                                    f"uninstaller accepted unowned {target}")
                self.assertEqual(paths[target].read_text(), f"unowned replacement {target}\\n")
                self.assertTrue(all(path.exists() for path in paths.values()),
                                "uninstall removed an artifact it did not own")
                calls = (root / "systemctl.jsonl").read_text()
                self.assertNotRegex(calls, r'"stop"|"disable"',
                                    "uninstall acted on the service before ownership validation")

    def test_parent_symlinks_are_rejected_without_outside_writes(self):
        cases = ("xdg", "launcher")
        for parent in cases:
            with self.subTest(parent=parent), tempfile.TemporaryDirectory(prefix="issue8-parent-") as tmp:
                root = Path(tmp)
                outside = root / "outside"
                outside.mkdir()
                home = root / "home with spaces;literal"
                xdg = root / "xdg config;literal"
                data = root / "data with spaces"
                home.mkdir(); data.mkdir()
                if parent == "xdg":
                    xdg.symlink_to(outside, target_is_directory=True)
                else:
                    xdg.mkdir()
                    local = home / ".local"
                    local.mkdir()
                    (local / "bin").symlink_to(outside, target_is_directory=True)
                before = sorted(p.relative_to(outside).as_posix() for p in outside.rglob("*"))
                result = self.run_lifecycle(INSTALL, root)
                after = sorted(p.relative_to(outside).as_posix() for p in outside.rglob("*"))
                self.assertNotEqual(result.returncode, 0,
                                    f"installer followed {parent} parent symlink")
                self.assertEqual(after, before, "installer wrote outside its configured roots")

    def test_uninstall_rejects_parent_symlinks_without_deleting_outside_files(self):
        for parent in ("xdg", "launcher"):
            with self.subTest(parent=parent), tempfile.TemporaryDirectory(prefix="issue8-uninstall-parent-") as tmp:
                root = Path(tmp)
                outside = root / "outside"
                outside.mkdir()
                home = root / "home with spaces;literal"; home.mkdir()
                data = root / "data with spaces"; data.mkdir()
                xdg = root / "xdg config;literal"
                if parent == "xdg":
                    xdg_target = outside / "config-root"
                    (xdg_target / "systemd" / "user").mkdir(parents=True)
                    (xdg_target / "local-ai").mkdir()
                    (xdg_target / "systemd" / "user" / SERVICE).write_text(
                        "# Managed by install-router-service.sh\\nowned-looking unit\\n")
                    (xdg_target / "local-ai" / "router.env").write_text(
                        "# Managed by install-router-service.sh\\nowned-looking env\\n")
                    xdg.symlink_to(xdg_target, target_is_directory=True)
                else:
                    xdg.mkdir()
                    local = home / ".local"
                    local.mkdir()
                    (local / "bin").symlink_to(outside, target_is_directory=True)
                    (outside / "run-router.sh").write_text(
                        "#!/bin/sh\\n# Managed by install-router-service.sh; invokes the Issue #7 launcher.\\n")
                before = {p.relative_to(outside).as_posix(): p.read_bytes()
                          for p in outside.rglob("*") if p.is_file()}
                result = self.run_lifecycle(UNINSTALL, root)
                after = {p.relative_to(outside).as_posix(): p.read_bytes()
                         for p in outside.rglob("*") if p.is_file()}
                self.assertNotEqual(result.returncode, 0,
                                    "uninstaller followed a configured parent symlink")
                self.assertEqual(after, before, "uninstaller deleted outside files")

    def test_launcher_override_must_be_absolute_and_normalized(self):
        for override in ("scripts/run-router.sh", str(ROOT / "scripts" / ".." / "scripts" / "run-router.sh")):
            with self.subTest(override=override), tempfile.TemporaryDirectory(prefix="issue8-launcher-path-") as tmp:
                result = self.run_lifecycle(INSTALL, Path(tmp),)
                self.assertEqual(result.returncode, 0, result.stderr)
                env = os.environ.copy()
                bindir, _ = self.fake_systemctl(Path(tmp))
                env.update({"HOME": str(Path(tmp) / "home"),
                            "XDG_CONFIG_HOME": str(Path(tmp) / "xdg"),
                            "XDG_DATA_HOME": str(Path(tmp) / "data"),
                            "PATH": f"{bindir}:{env['PATH']}",
                            "LOCAL_AI_RUN_ROUTER": override})
                result = subprocess.run([str(INSTALL)], cwd=ROOT, env=env,
                                        text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0,
                                    f"accepted non-normalized launcher override {override!r}")

    def test_operator_documentation_covers_complete_lifecycle(self):
        self.assertTrue(DOC.is_file(), "missing Issue #8 operator documentation")
        text = DOC.read_text()
        self.assertRegex(text, r"install-router-service\.sh", "documentation omits install")
        for command in ("daemon-reload", "enable", "start", "stop", "restart", "status"):
            self.assertRegex(text, rf"systemctl\s+--user\s+{command}\b",
                             f"documentation omits systemctl --user {command}")
        self.assertRegex(text, r"journalctl\s+--user.*(?:-u|--unit)|(?:-u|--unit).*journalctl\s+--user")
        self.assertIn("uninstall", text.lower())


if __name__ == "__main__":
    unittest.main()
