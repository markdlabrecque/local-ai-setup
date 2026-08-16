#!/usr/bin/env bash
# Remove only the user service files installed by install-router-service.sh.
set -Eeuo pipefail

readonly SERVICE_NAME="local-ai-router.service"
readonly CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
readonly UNIT_PATH="$CONFIG_HOME/systemd/user/$SERVICE_NAME"
readonly ENV_PATH="$CONFIG_HOME/local-ai/router.env"
readonly LAUNCHER_PATH="$HOME/.local/bin/run-router.sh"

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# Validate every target before touching the manager or any file.  Symlinks are
# never acceptable here: rm would remove the link itself, but a later change
# could accidentally follow it, and refusing is the safer idempotent contract.
for path in "$UNIT_PATH" "$ENV_PATH" "$LAUNCHER_PATH"; do
  [[ ! -L "$path" ]] || die "refusing symlink target: $path"
  [[ ! -e "$path" || -f "$path" ]] || die "target is not a regular file: $path"
done

systemctl_user() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl is required"
  command -v timeout >/dev/null 2>&1 || die "timeout is required for bounded systemctl calls"
  timeout --foreground --signal=TERM --kill-after=5s 15s \
    systemctl --user "$@"
}

# stop/disable are intentionally tolerant of an already absent unit, making
# repeated uninstall safe.  daemon-reload is still required after file removal.
systemctl_user stop "$SERVICE_NAME" || true
systemctl_user disable "$SERVICE_NAME" || true

rm -f -- "$UNIT_PATH" "$ENV_PATH"
if [[ -f "$LAUNCHER_PATH" ]]; then
  # The installer refuses to overwrite a non-managed launcher.  The marker is
  # therefore sufficient to identify the disposable wrapper without deleting
  # a user's unrelated executable in ~/.local/bin.
  grep -Fqx '# Managed by install-router-service.sh; invokes the Issue #7 launcher.' \
    "$LAUNCHER_PATH" || die "refusing to remove non-managed launcher: $LAUNCHER_PATH"
  rm -f -- "$LAUNCHER_PATH"
fi
systemctl_user daemon-reload
printf 'Uninstalled %s (model and runtime data preserved)\n' "$SERVICE_NAME"
