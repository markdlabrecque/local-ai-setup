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
if [[ -n ${LOCAL_AI_TEST_HOOK:-} ]]; then
  [[ ${LOCAL_AI_TEST_MODE:-0} == 1 ]] || die "test race hook requires LOCAL_AI_TEST_MODE=1"
  [[ -x "$LOCAL_AI_TEST_HOOK" ]] || die "test race hook is not executable"
fi

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

# Hold the unit's parent directory while the lifecycle decision is made.  The
# final path and content are checked again after the optional, explicitly gated
# test hook, so a swapped artifact cannot turn an owned service into an
# unowned one between validation and stop/disable.
revalidate_before_lifecycle() {
  python3 - "$UNIT_PATH" <<'PY'
import hashlib, os, stat, subprocess, sys
path = sys.argv[1]
parent, name = os.path.split(path)
fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
try:
    for component in parent.split("/")[1:]:
        if component in ("", ".", ".."):
            raise SystemExit(f"unsafe lifecycle parent: {parent}")
        nxt = os.open(component, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW,
                      dir_fd=fd)
        os.close(fd)
        fd = nxt
except BaseException:
    os.close(fd)
    raise

def identity(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns)

def state():
    st = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if not stat.S_ISREG(st.st_mode):
        raise SystemExit(f"unowned lifecycle target: {path}")
    handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
    try:
        before = os.fstat(handle)
        digest = hashlib.sha256()
        while chunk := os.read(handle, 1024 * 1024):
            digest.update(chunk)
        after = os.fstat(handle)
        if identity(before) != identity(after):
            raise SystemExit(f"lifecycle target changed while hashing: {path}")
        return identity(after), digest.hexdigest()
    finally:
        os.close(handle)

def parent_is_same():
    visible = os.stat(parent, follow_symlinks=False)
    held = os.fstat(fd)
    return stat.S_ISDIR(visible.st_mode) and identity(visible)[:2] == identity(held)[:2]

before = state()
hook = os.environ.get("LOCAL_AI_TEST_HOOK")
if hook:
    if os.environ.get("LOCAL_AI_TEST_MODE") != "1":
        raise SystemExit("test race hook requires LOCAL_AI_TEST_MODE=1")
    subprocess.run([hook, "before-stop-disable", path], check=True)
if not parent_is_same() or state() != before:
    raise SystemExit(f"lifecycle ownership changed: {path}")
os.close(fd)
PY
}
revalidate_before_lifecycle || die "ownership changed before service lifecycle action"
validate_manifest || die "ownership changed before service lifecycle action"

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
import hashlib, os, stat, subprocess, sys
for path in sys.argv[1:]:
    parent, name = os.path.split(path)
    fd = os.open("/", os.O_PATH | os.O_DIRECTORY)
    try:
        for component in parent.split("/")[1:]:
            if component in ("", ".", ".."):
                raise SystemExit(f"unsafe removal parent: {parent}")
            nxt = os.open(component, os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW,
                          dir_fd=fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise

    def identity(st):
        return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns)

    def state():
        st = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            raise SystemExit(f"refusing unsafe removal target: {path}")
        handle = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        try:
            before = os.fstat(handle)
            digest = hashlib.sha256()
            while chunk := os.read(handle, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(handle)
            if identity(before) != identity(after):
                raise SystemExit(f"target changed while hashing: {path}")
            return identity(after), digest.hexdigest()
        finally:
            os.close(handle)

    def parent_is_same():
        visible = os.stat(parent, follow_symlinks=False)
        held = os.fstat(fd)
        return stat.S_ISDIR(visible.st_mode) and identity(visible)[:2] == identity(held)[:2]

    try:
        before = state()
        hook = os.environ.get("LOCAL_AI_TEST_HOOK")
        if hook:
            if os.environ.get("LOCAL_AI_TEST_MODE") != "1":
                raise SystemExit("test race hook requires LOCAL_AI_TEST_MODE=1")
            subprocess.run([hook, "before-remove", path], check=True)
        if not parent_is_same() or state() != before:
            raise SystemExit(f"removal ownership changed: {path}")
        os.unlink(name, dir_fd=fd)
    finally:
        os.close(fd)
PY
}
remove_exact "$UNIT_PATH" "$ENV_PATH" "$LAUNCHER_PATH" "$MANIFEST_PATH"
systemctl_user daemon-reload
printf 'Uninstalled %s (model and runtime data preserved)\n' "$SERVICE_NAME"
