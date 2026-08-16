#!/usr/bin/env bash
# Remove only the user service files installed by install-router-service.sh.
set -Eeuo pipefail

readonly SERVICE_NAME="local-ai-router.service"
readonly CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
readonly UNIT_PATH="$CONFIG_HOME/systemd/user/$SERVICE_NAME"
readonly ENV_PATH="$CONFIG_HOME/local-ai/router.env"
readonly MANIFEST_PATH="$CONFIG_HOME/local-ai/router.manifest"
readonly LAUNCHER_PATH="$HOME/.local/bin/run-router.sh"
readonly OWNERSHIP_MARKER="# Managed by install-router-service.sh"
readonly MANIFEST_MARKER="# Managed by install-router-service.sh; ownership manifest."

die() { printf 'error: %s\n' "$*" >&2; exit 1; }
[[ "$CONFIG_HOME" == /* && "$HOME" == /* ]] || die "HOME and XDG_CONFIG_HOME must be absolute paths"

# Inspect every existing parent with O_NOFOLLOW. Missing parents are harmless,
# but an existing symlink anywhere below HOME or XDG_CONFIG_HOME is rejected.
safe_check_parents() {
  python3 - "$@" <<'PY'
import os, sys
for raw in sys.argv[1:]:
    if not os.path.isabs(raw) or any(part in (".", "..") for part in raw.split("/") if part):
        raise SystemExit(f"unsafe non-normalized path: {raw}")
    parts = raw.split("/")[1:-1]
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                nxt = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                break
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)
PY
}

safe_check_parents "$UNIT_PATH" "$ENV_PATH" "$MANIFEST_PATH" "$LAUNCHER_PATH" ||
  die "refusing symlinked or unsafe parent component"

for path in "$UNIT_PATH" "$ENV_PATH" "$MANIFEST_PATH" "$LAUNCHER_PATH"; do
  [[ ! -L "$path" ]] || die "refusing symlink target: $path"
  [[ ! -e "$path" || -f "$path" ]] || die "target is not a regular file: $path"
done

any_artifact=0
for path in "$UNIT_PATH" "$ENV_PATH" "$MANIFEST_PATH" "$LAUNCHER_PATH"; do
  [[ ! -e "$path" ]] || any_artifact=1
done
if (( ! any_artifact )); then
  printf 'No installed %s artifacts found; nothing to uninstall\n' "$SERVICE_NAME"
  exit 0
fi

[[ -e "$MANIFEST_PATH" ]] || die "ownership manifest is missing; refusing to remove files"

validate_manifest() {
  python3 - "$MANIFEST_PATH" "$UNIT_PATH" "$ENV_PATH" "$LAUNCHER_PATH" "$OWNERSHIP_MARKER" "$MANIFEST_MARKER" <<'PY'
import hashlib, json, os, re, stat, sys
manifest, unit, env, launcher, marker, manifest_marker = sys.argv[1:]
try:
    raw = open(manifest, "rb").read()
    lines = raw.decode("utf-8").splitlines()
    if not lines or lines[0] != manifest_marker or len(lines) != 2:
        raise ValueError("invalid ownership manifest marker")
    data = json.loads(lines[1])
    expected = [("unit", unit), ("environment", env), ("launcher", launcher)]
    if data.get("schema_version") != 1 or data.get("managed_by") != "install-router-service.sh":
        raise ValueError("invalid ownership manifest header")
    rows = data.get("artifacts")
    if not isinstance(rows, list) or len(rows) != len(expected):
        raise ValueError("invalid ownership artifact list")
    for row, (ident, path) in zip(rows, expected):
        if not isinstance(row, dict) or set(row) != {"id", "path", "sha256"}:
            raise ValueError("invalid ownership artifact entry")
        if row["id"] != ident or row["path"] != path or not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise ValueError("ownership manifest does not describe the expected artifact")
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("owned artifact is not a regular file")
        content = open(path, "rb").read()
        if hashlib.sha256(content).hexdigest() != row["sha256"]:
            raise ValueError(f"owned artifact was changed: {path}")
        if marker.encode() not in content:
            raise ValueError(f"owned artifact marker missing: {path}")
except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
    print(f"error: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}
validate_manifest || die "refusing to uninstall artifacts that are not exactly installer-owned"

systemctl_user() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl is required"
  command -v timeout >/dev/null 2>&1 || die "timeout is required for bounded systemctl calls"
  timeout --foreground --signal=TERM --kill-after=5s 15s systemctl --user "$@"
}

# Ownership is fully validated before these lifecycle actions. An absent unit
# is normal on repeated uninstall; no manager call is made for an unowned set.
systemctl_user stop "$SERVICE_NAME" || true
systemctl_user disable "$SERVICE_NAME" || true

remove_exact() {
  python3 - "$@" <<'PY'
import os, sys
for path in sys.argv[1:]:
    parent, name = os.path.split(path)
    fd = os.open(parent, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)
PY
}
remove_exact "$UNIT_PATH" "$ENV_PATH" "$LAUNCHER_PATH" "$MANIFEST_PATH"
systemctl_user daemon-reload
printf 'Uninstalled %s (model and runtime data preserved)\n' "$SERVICE_NAME"
