# Issue #8: user systemd router service

Issue #7 remains the owner of router policy. The service starts its
`run-router.sh` launcher in the foreground; it does not add model flags, open a
network listener, or delete model/runtime data. The unit uses systemd's `%h`
home and `%E` XDG configuration specifiers, so it contains no machine-specific
home path.

## Install

From this checkout, install the unit for the current user:

```bash
scripts/install-router-service.sh
# or install and register it with the user manager:
scripts/install-router-service.sh --enable --start
```

The installer writes `~/.config/systemd/user/local-ai-router.service`, a
quoted `~/.config/local-ai/router.env`, a small managed
`~/.local/bin/run-router.sh` wrapper, and the ownership manifest
`~/.config/local-ai/router.manifest`. Existing files are unowned unless the
complete manifest still matches their recorded SHA-256 hashes; such files are
refused rather than overwritten. Symlink targets and symlinked parent
components are refused, and writes use temporary files plus no-follow
directory descriptors where available. Model files and runtime files are
never followed, overwritten, or removed. Paths can be overridden before
installation with `LOCAL_AI_SERVER`, `LOCAL_AI_MODEL_DIR`,
`LOCAL_AI_CONFIG_DIR`, `LOCAL_AI_RUNTIME_DIR`, and `LOCAL_AI_RUN_ROUTER`.
`LOCAL_AI_RUN_ROUTER` must be an absolute, already-normalized executable path.
Values are written to an allowlisted, systemd-escaped environment file; shell
expansion is not used.

The service is localhost-only through the Issue #7 launcher, uses a private
`PrivateTmp`, `NoNewPrivileges`, a control-group kill mode, a 30-second stop
timeout, and bounded `on-failure` restarts (five starts in five minutes).
`MemoryHigh=51539607552`, `MemoryMax=60129542144`, and
`MemorySwapMax=8589934592` are finite systemd byte values (48, 56, and 8
GiB). They admit the measured Q8_0 baseline (>27.5 GiB), keep the hard cap
below 60 GiB, preserve at least an 8 GiB host reserve, and bound swap at 8
GiB. No model is autoloaded by the unit.

## Operate

```bash
systemctl --user daemon-reload
systemctl --user enable local-ai-router.service
systemctl --user start local-ai-router.service
systemctl --user status local-ai-router.service
systemctl --user restart local-ai-router.service
systemctl --user stop local-ai-router.service
journalctl --user -u local-ai-router.service -f
```

The installer performs a bounded `daemon-reload`; use the commands above after
editing the environment file or unit. `systemctl --user restart` exercises the
launcher’s complete process-group cleanup, while `KillMode=control-group` also
ensures systemd does not leave router descendants behind.

To remove the service files and wrapper without touching model, configuration,
compiled runtime, or other user data:

```bash
scripts/uninstall-router-service.sh
```

Uninstall validates the manifest, markers, exact paths, and hashes before
stopping or disabling anything. It is idempotent once all owned artifacts are
gone, refuses unowned or symlinked artifacts, and removes only the unit,
environment file, wrapper, and manifest. It never removes model, config,
runtime, or compiled-binary data. It tolerates an absent or already stopped
user service and bounds all user-manager calls. Use `journalctl --user -u
local-ai-router.service` to diagnose a failed startup; model loading remains an
explicit `router-model.sh load` operation.

## Portable/fake verification

The repository test uses a disposable `systemctl` found first on `PATH`, so it
does not contact the real user manager or start llama.cpp:

```bash
python3 tests/test_issue8_systemd_service.py
```
