#!/usr/bin/env bash
# Install the Issue #7 router as a user-level systemd service.
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
readonly UNIT_SOURCE="$PROJECT_ROOT/config/local-ai-router.service"
readonly SERVICE_NAME="local-ai-router.service"
readonly CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
readonly UNIT_DIR="$CONFIG_HOME/systemd/user"
readonly ENV_DIR="$CONFIG_HOME/local-ai"
readonly UNIT_PATH="$UNIT_DIR/$SERVICE_NAME"
readonly ENV_PATH="$ENV_DIR/router.env"
readonly MANIFEST_PATH="$ENV_DIR/router.manifest"
readonly LAUNCHER_PATH="$HOME/.local/bin/run-router.sh"
readonly OWNERSHIP_MARKER="# Managed by install-router-service.sh"
readonly MANIFEST_MARKER="# Managed by install-router-service.sh; ownership manifest."

ENABLE=0
START=0

usage() {
  cat <<'EOF'
Usage: scripts/install-router-service.sh [--enable] [--start]

Install the portable local-ai-router.service and its quoted environment file
for the current user. The service invokes the tracked Issue #7 router
launcher through ~/.local/bin/run-router.sh. No model or runtime data is
created or removed.

Options:
  --enable  enable the user service
  --start   start the user service
  -h, --help
EOF
}

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

while (($#)); do
  case "$1" in
    --enable) ENABLE=1 ;;
    --start) START=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
  shift
done

[[ -r "$UNIT_SOURCE" ]] || die "missing service unit: $UNIT_SOURCE"
[[ -f "$PROJECT_ROOT/scripts/run-router.sh" && -x "$PROJECT_ROOT/scripts/run-router.sh" ]] ||
  die "Issue #7 launcher is not an executable regular file"
[[ "$CONFIG_HOME" == /* && "$HOME" == /* ]] || die "HOME and XDG_CONFIG_HOME must be absolute paths"

# Directory creation and traversal use directory descriptors opened with
# O_NOFOLLOW. This rejects an existing symlinked component rather than letting
# mkdir -p or a later rename escape the configured root.
safe_mkdir() {
  python3 - "$@" <<'PY'
import errno, os, stat, sys
for raw in sys.argv[1:]:
    if not os.path.isabs(raw) or any(part in (".", "..") for part in raw.split("/") if part):
        raise SystemExit(f"unsafe non-normalized directory: {raw}")
    parts = raw.split("/")[1:]
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            try:
                nxt = os.open(part, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except OSError as exc:
                raise SystemExit(f"unsafe directory component {raw}: {exc}")
            os.close(fd)
            fd = nxt
    finally:
        os.close(fd)
PY
}

safe_mkdir "$UNIT_DIR" "$ENV_DIR" "$(dirname -- "$LAUNCHER_PATH")"

SERVER="${LOCAL_AI_SERVER:-${LOCAL_AI_BIN_DIR:-$HOME/.local/bin}/llama-server}"
MODEL_DIR="${LOCAL_AI_MODEL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/local-ai/models}"
CONFIG_DIR="${LOCAL_AI_CONFIG_DIR:-$CONFIG_HOME/local-ai}"
RUNTIME_DIR="${LOCAL_AI_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/local-ai}"
RUN_ROUTER="${LOCAL_AI_RUN_ROUTER:-$PROJECT_ROOT/scripts/run-router.sh}"

# A launcher path is part of the persisted service contract. Require the
# supplied spelling to already be absolute and canonical; silently accepting
# ./, .., or a symlink would make the manifest refer to a different artifact.
[[ "$RUN_ROUTER" == /* ]] || die "LOCAL_AI_RUN_ROUTER must be an absolute normalized path"
[[ "$RUN_ROUTER" != *"/./"* && "$RUN_ROUTER" != *"/../"* && "$RUN_ROUTER" != */. && "$RUN_ROUTER" != */.. ]] ||
  die "LOCAL_AI_RUN_ROUTER must be an absolute normalized path"
[[ ! -L "$RUN_ROUTER" ]] || die "router launcher must not be a symlink: $RUN_ROUTER"
[[ -f "$RUN_ROUTER" && -r "$RUN_ROUTER" && -x "$RUN_ROUTER" ]] ||
  die "router launcher is not an executable regular file: $RUN_ROUTER"
[[ "$(realpath -e -- "$RUN_ROUTER")" == "$RUN_ROUTER" ]] ||
  die "LOCAL_AI_RUN_ROUTER is not a canonical path: $RUN_ROUTER"

# EnvironmentFile uses systemd's own quoting rules, not shell eval. Quote
# every value and keep the allowlist fixed.
write_environment() {
  python3 - "$SERVER" "$MODEL_DIR" "$CONFIG_DIR" "$RUNTIME_DIR" "$RUN_ROUTER" <<'PY'
import sys
names = ("LOCAL_AI_SERVER", "LOCAL_AI_MODEL_DIR", "LOCAL_AI_CONFIG_DIR",
         "LOCAL_AI_RUNTIME_DIR", "LOCAL_AI_RUN_ROUTER")
values = sys.argv[1:]
if len(values) != len(names):
    raise SystemExit("invalid environment values")
for value in values:
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in value):
        raise SystemExit("environment paths contain a control character")
for name, value in zip(names, values):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    print(f'{name}="{escaped}"')
PY
}

wrapper_contents() {
  python3 - "$RUN_ROUTER" <<'PY'
import shlex, sys
print("#!/usr/bin/env bash")
print("# Managed by install-router-service.sh; invokes the Issue #7 launcher.")
print("set -Eeuo pipefail")
print("exec " + shlex.quote(sys.argv[1]) + ' "$@"')
PY
}

UNIT_CONTENT="$OWNERSHIP_MARKER; generated service unit."$'\n'"$(<"$UNIT_SOURCE")"$'\n'
ENV_CONTENT="# Managed by install-router-service.sh; paths are quoted for systemd."$'\n'"$(write_environment)"$'\n'
WRAPPER_CONTENT="$(wrapper_contents)"$'\n'

sha256_text() { printf %s "$1" | sha256sum | awk '{print $1}'; }
UNIT_SHA=$(sha256_text "$UNIT_CONTENT")
ENV_SHA=$(sha256_text "$ENV_CONTENT")
WRAPPER_SHA=$(sha256_text "$WRAPPER_CONTENT")

manifest_contents() {
  python3 - "$UNIT_PATH" "$UNIT_SHA" "$ENV_PATH" "$ENV_SHA" "$LAUNCHER_PATH" "$WRAPPER_SHA" <<'PY'
import json, sys
paths = sys.argv[1::2]
hashes = sys.argv[2::2]
rows = [{"id": ident, "path": path, "sha256": digest}
        for ident, path, digest in zip(("unit", "environment", "launcher"), paths, hashes)]
data = {"schema_version": 1, "managed_by": "install-router-service.sh", "artifacts": rows}
print(json.dumps(data, sort_keys=True, separators=(",", ":")))
PY
}
MANIFEST_JSON="$(manifest_contents)"
MANIFEST_CONTENT="$MANIFEST_MARKER"$'\n'"$MANIFEST_JSON"$'\n'

# Validate an existing manifest and all of its exact artifacts before any
# systemd call or file replacement. A marker alone is not ownership proof:
# the manifest binds the fixed paths to their recorded content hashes.
validate_manifest() {
  python3 - "$MANIFEST_PATH" "$UNIT_PATH" "$ENV_PATH" "$LAUNCHER_PATH" "$OWNERSHIP_MARKER" "$MANIFEST_MARKER" <<'PY'
import hashlib, json, os, re, stat, sys
manifest, unit, env, launcher, marker, manifest_marker = sys.argv[1:]
try:
    with open(manifest, "rb") as stream:
        raw = stream.read()
    lines = raw.decode("utf-8").splitlines()
    if not lines or lines[0] != manifest_marker or len(lines) != 2:
        raise ValueError("invalid ownership manifest marker")
    data = json.loads(lines[1])
    expected = [("unit", unit), ("environment", env), ("launcher", launcher)]
    if data != {"artifacts": data.get("artifacts"), "managed_by": "install-router-service.sh", "schema_version": 1}:
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
        if ident != "launcher" and marker.encode() not in content:
            raise ValueError(f"owned artifact marker missing: {path}")
        if ident == "launcher" and b"# Managed by install-router-service.sh" not in content:
            raise ValueError(f"owned launcher marker missing: {path}")
except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
    print(f"error: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

# Refuse every pre-existing regular file unless the complete prior install is
# intact and owned. This preflight prevents partial replacement of a mixed set.
if [[ -e "$MANIFEST_PATH" || -L "$MANIFEST_PATH" ]]; then
  [[ ! -L "$MANIFEST_PATH" ]] || die "refusing symlink manifest: $MANIFEST_PATH"
  validate_manifest || die "refusing invalid or tampered ownership manifest"
else
  for path in "$UNIT_PATH" "$ENV_PATH" "$LAUNCHER_PATH"; do
    [[ ! -L "$path" ]] || die "refusing symlink target: $path"
    [[ ! -e "$path" ]] || die "refusing to overwrite unowned existing file: $path"
  done
fi

atomic_write() {
  local destination=$1 mode=$2 content=$3
  python3 - "$destination" "$mode" "$content" <<'PY'
import os, stat, sys, uuid
path, mode, content = sys.argv[1], int(sys.argv[2], 8), sys.argv[3].encode()
parent, name = os.path.split(path)
fd = os.open(parent, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
temp = f".{name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
try:
    try:
        current = os.lstat(name, dir_fd=fd)
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
            raise SystemExit(f"refusing unsafe destination: {path}")
    except FileNotFoundError:
        pass
    out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=fd)
    try:
        os.write(out, content)
        os.fchmod(out, mode)
        os.fsync(out)
    finally:
        os.close(out)
    os.replace(temp, name, src_dir_fd=fd, dst_dir_fd=fd)
finally:
    try: os.unlink(temp, dir_fd=fd)
    except FileNotFoundError: pass
    os.close(fd)
PY
}

atomic_write "$UNIT_PATH" 0644 "$UNIT_CONTENT"
atomic_write "$ENV_PATH" 0600 "$ENV_CONTENT"
atomic_write "$LAUNCHER_PATH" 0755 "$WRAPPER_CONTENT"
# Publish the manifest last, so it never claims ownership of an incomplete set.
atomic_write "$MANIFEST_PATH" 0600 "$MANIFEST_CONTENT"

systemctl_user() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl is required"
  command -v timeout >/dev/null 2>&1 || die "timeout is required for bounded systemctl calls"
  timeout --foreground --signal=TERM --kill-after=5s 15s systemctl --user "$@"
}

systemctl_user daemon-reload
if (( ENABLE )); then systemctl_user enable "$SERVICE_NAME"; fi
if (( START )); then systemctl_user start "$SERVICE_NAME"; fi
printf 'Installed %s\n' "$SERVICE_NAME"
