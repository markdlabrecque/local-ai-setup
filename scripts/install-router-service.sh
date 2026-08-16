#!/usr/bin/env bash
# Install the Issue #7 router as a user-level systemd service.
set -Eeuo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
readonly UNIT_SOURCE="$PROJECT_ROOT/config/local-ai-router.service"
readonly SERVICE_NAME="local-ai-router.service"
readonly CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
readonly UNIT_DIR="$CONFIG_HOME/systemd/user"
readonly ENV_DIR="$CONFIG_HOME/local-ai"
readonly UNIT_PATH="$UNIT_DIR/$SERVICE_NAME"
readonly ENV_PATH="$ENV_DIR/router.env"
readonly LAUNCHER_PATH="$HOME/.local/bin/run-router.sh"

ENABLE=0
START=0

usage() {
  cat <<'EOF'
Usage: scripts/install-router-service.sh [--enable] [--start]

Install the portable local-ai-router.service and its quoted environment file
for the current user.  The service invokes the tracked Issue #7 router
launcher through ~/.local/bin/run-router.sh.  No model or runtime data is
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
[[ -x "$PROJECT_ROOT/scripts/run-router.sh" ]] || die "Issue #7 launcher is not executable"

# Defaults deliberately match scripts/run-router.sh and scripts/setup.sh.
SERVER="${LOCAL_AI_SERVER:-${LOCAL_AI_BIN_DIR:-$HOME/.local/bin}/llama-server}"
MODEL_DIR="${LOCAL_AI_MODEL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/local-ai/models}"
CONFIG_DIR="${LOCAL_AI_CONFIG_DIR:-$CONFIG_HOME/local-ai}"
RUNTIME_DIR="${LOCAL_AI_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/local-ai}"
RUN_ROUTER="${LOCAL_AI_RUN_ROUTER:-$PROJECT_ROOT/scripts/run-router.sh}"

[[ -f "$RUN_ROUTER" && -r "$RUN_ROUTER" && -x "$RUN_ROUTER" ]] ||
die "router launcher is not an executable regular file: $RUN_ROUTER"

# Refuse symlink targets before creating anything.  In particular, never let
# mv/rm or a future change follow a link supplied in a user config directory.
for path in "$UNIT_PATH" "$ENV_PATH" "$LAUNCHER_PATH"; do
  [[ ! -L "$path" ]] || die "refusing symlink target: $path"
  [[ ! -e "$path" || -f "$path" ]] || die "target is not a regular file: $path"
done

mkdir -p -- "$UNIT_DIR" "$ENV_DIR" "$(dirname -- "$LAUNCHER_PATH")"

replace_file() {
  local destination=$1 mode=$2 temporary
  temporary=$(mktemp "${destination}.tmp.XXXXXX")
  trap 'rm -f -- "$temporary"' RETURN
  chmod "$mode" -- "$temporary"
  cat >"$temporary"
  if [[ -f "$destination" ]] && cmp -s -- "$temporary" "$destination"; then
    rm -f -- "$temporary"
  else
    # rename(2) replaces the named file, not the file a symlink points at;
    # the symlink checks above make this operation fail closed for this run.
    mv -f -- "$temporary" "$destination"
  fi
  trap - RETURN
}

# EnvironmentFile uses systemd's own quoting rules, not shell eval.  Reject
# control characters and quote every value so spaces, $, ; and backslashes are
# data rather than syntax.  The allowlist is intentionally kept here.
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
# Keep the output order fixed; it is part of idempotent installation.
for name, value in zip(names, values):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    print(f'{name}="{escaped}"')
PY
}

# Install the unit and environment atomically.  The generated wrapper makes
# the unit usable from a fresh checkout while preserving an existing user's
# ~/.local/bin/run-router.sh rather than overwriting it.
replace_file "$UNIT_PATH" 0644 <"$UNIT_SOURCE"
replace_file "$ENV_PATH" 0600 <<EOF
# Managed by install-router-service.sh; paths are quoted for systemd.
$(write_environment)
EOF

wrapper_contents() {
  python3 - "$RUN_ROUTER" <<'PY'
import shlex, sys
print("#!/usr/bin/env bash")
print("# Managed by install-router-service.sh; invokes the Issue #7 launcher.")
print("set -Eeuo pipefail")
print("exec " + shlex.quote(sys.argv[1]) + ' "$@"')
PY
}
if [[ -e "$LAUNCHER_PATH" ]]; then
  expected=$(wrapper_contents)
  [[ "$(<"$LAUNCHER_PATH")" == "$expected" ]] ||
    die "refusing to overwrite existing launcher: $LAUNCHER_PATH"
else
  replace_file "$LAUNCHER_PATH" 0755 <<<"$(wrapper_contents)"
fi

# Every systemctl operation is bounded.  A user manager can be unavailable
# (for example in a headless session), so lifecycle actions are explicit and
# only requested when the operator asks for them.
systemctl_user() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl is required"
  command -v timeout >/dev/null 2>&1 || die "timeout is required for bounded systemctl calls"
  timeout --foreground --signal=TERM --kill-after=5s 15s \
    systemctl --user "$@"
}

systemctl_user daemon-reload
if (( ENABLE )); then
  systemctl_user enable "$SERVICE_NAME"
fi
if (( START )); then
  systemctl_user start "$SERVICE_NAME"
fi
printf 'Installed %s\n' "$SERVICE_NAME"
