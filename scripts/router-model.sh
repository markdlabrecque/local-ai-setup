#!/usr/bin/env bash
# Verify a manifest artifact, then request router load/unload without deleting it.
set -Eeuo pipefail
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
ACTION=${1:-}
[[ "$ACTION" == load || "$ACTION" == unload ]] || {
  printf 'Usage: scripts/router-model.sh {load|unload} --model-id ID [--models-dir PATH] [--manifest PATH] [--base-url URL] [--timeout SECONDS]\n' >&2
  exit 2
}
shift
MODEL_ID=''; MODELS_DIR="${LOCAL_AI_MODEL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/local-ai/models}"
MANIFEST="$ROOT/config/models.json"; BASE_URL='http://127.0.0.1:8080'; POLL_TIMEOUT=3
while (($#)); do
  case "$1" in
    --model-id) (($# > 1)) || { echo 'error: --model-id requires a value' >&2; exit 2; }; MODEL_ID=$2; shift 2 ;;
    --models-dir) (($# > 1)) || { echo 'error: --models-dir requires a path' >&2; exit 2; }; MODELS_DIR=$2; shift 2 ;;
    --manifest) (($# > 1)) || { echo 'error: --manifest requires a path' >&2; exit 2; }; MANIFEST=$2; shift 2 ;;
    --base-url) (($# > 1)) || { echo 'error: --base-url requires a URL' >&2; exit 2; }; BASE_URL=$2; shift 2 ;;
    --timeout) (($# > 1)) || { echo 'error: --timeout requires a value' >&2; exit 2; }; POLL_TIMEOUT=$2; shift 2 ;;
    -h|--help) echo 'Usage: scripts/router-model.sh {load|unload} --model-id ID [--models-dir PATH] [--manifest PATH] [--base-url http://127.0.0.1:8080] [--timeout SECONDS]'; exit 0 ;;
    *) echo "error: unknown option: $1" >&2; exit 2 ;;
  esac
done
[[ -n "$MODEL_ID" ]] || { echo 'error: --model-id is required' >&2; exit 2; }

artifact=$(python3 - "$MANIFEST" "$MODEL_ID" "$BASE_URL" "$POLL_TIMEOUT" <<'PY'
import json, math, pathlib, sys
from urllib.parse import urlsplit
manifest, requested_id, base_url, timeout = sys.argv[1:]
try:
    parsed = urlsplit(base_url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or
            parsed.password or parsed.query or parsed.fragment):
        raise ValueError("base URL must be plain http on 127.0.0.1")
    poll_timeout = float(timeout)
    if not math.isfinite(poll_timeout) or poll_timeout <= 0:
        raise ValueError("timeout must be a finite positive number")
    data = json.loads(pathlib.Path(manifest).read_text())
    rows = [row for row in data["artifacts"] if row.get("id") == requested_id]
    if len(rows) != 1:
        raise ValueError("model ID is not present exactly once in the manifest")
    row = rows[0]
    filename, digest = row.get("filename", ""), row.get("sha256", "").lower()
    if (pathlib.Path(filename).name != filename or not filename.endswith(".gguf") or
            not filename or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in filename)):
        raise ValueError("manifest filename is unsafe")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("manifest checksum is invalid")
    size = row.get("size_bytes", "")
    if size != "" and (isinstance(size, bool) or not isinstance(size, int) or size <= 0):
        raise ValueError("manifest size is invalid")
    print(json.dumps({"filename": filename, "sha256": digest, "size": size,
                      "base_url": base_url.rstrip("/"), "timeout": poll_timeout}, separators=(",", ":")))
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
    print(f"error: {exc}", file=sys.stderr); raise SystemExit(1)
PY
)
filename=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["filename"])' <<<"$artifact")
expected=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["sha256"])' <<<"$artifact")
expected_size=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["size"])' <<<"$artifact")
base_url=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["base_url"])' <<<"$artifact")
poll_timeout=$(python3 -c 'import json,sys; print(json.load(sys.stdin)["timeout"])' <<<"$artifact")
model_path="$MODELS_DIR/$filename"
model_id="${filename%.gguf}"
lock_path="$MODELS_DIR/.${filename}.lock"
mkdir -p -- "$MODELS_DIR"

[[ -f "$model_path" && -r "$model_path" ]] || { echo "error: model artifact is missing: $filename" >&2; exit 1; }
command -v flock >/dev/null 2>&1 || { echo 'error: flock is required' >&2; exit 1; }
exec {lock_fd}>"$lock_path"
flock -x "$lock_fd"

identity_stat=''
verify_identity() {
  [[ -f "$model_path" && -r "$model_path" ]] || { echo "error: model artifact disappeared or was replaced: $filename" >&2; return 1; }
  local current_stat actual
  current_stat=$(stat -c '%s:%i:%Y' -- "$model_path") || return 1
  actual=$(sha256sum -- "$model_path" | awk '{print $1}') || return 1
  [[ "$actual" == "$expected" ]] || { echo "error: model identity changed (checksum): $filename" >&2; return 1; }
  [[ -z "$identity_stat" || "$current_stat" == "$identity_stat" ]] || {
    echo "error: model identity changed (stat): $filename" >&2; return 1;
  }
  [[ -n "$identity_stat" ]] || identity_stat=$current_stat
}
verify_identity || exit 1

payload=$(python3 - "$model_id" <<'PY'
import json, sys
print(json.dumps({"model": sys.argv[1]}, separators=(",", ":")))
PY
)
endpoint="$base_url/models/$ACTION"
printf '%s\n' "requesting router $ACTION for $model_id"
response=''
if ! response=$(curl -fsS --max-time 10 -X POST -H 'Content-Type: application/json' \
    -d "$payload" "$endpoint"); then
  echo "error: router $ACTION request failed" >&2
  verify_identity || true
  exit 1
fi
if ! python3 - "$response" <<'PY'
import json, sys
try:
    payload = json.loads(sys.argv[1])
    if (not isinstance(payload, dict) or set(payload) != {"success"} or
            payload["success"] is not True):
        raise ValueError
except (ValueError, TypeError, json.JSONDecodeError):
    raise SystemExit(1)
PY
then
  echo "error: router returned an unexpected $ACTION response" >&2
  verify_identity || true
  exit 1
fi

# Read the real b10446 /models state rather than trusting the mutation reply.
if ! python3 - "$base_url/models" "$model_id" "$ACTION" "$poll_timeout" <<'PY'
import json, sys, time, urllib.error, urllib.request
url, model_id, action, timeout = sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4])
desired = "loaded" if action == "load" else "unloaded"
deadline = time.monotonic() + timeout
last = "no status"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=min(2.0, timeout)) as response:
            body = json.loads(response.read() or b"{}")
        rows = body.get("data")
        if not isinstance(rows, list): raise ValueError("/models data is not a list")
        matching = [row for row in rows if isinstance(row, dict) and row.get("id") == model_id]
        if len(matching) == 1:
            last = str(matching[0].get("status", "missing status"))
            if last == desired:
                raise SystemExit(0)
        else:
            last = "model missing from /models"
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError, urllib.error.URLError) as exc:
        last = str(exc)
    time.sleep(0.05)
print(f"error: router did not report {model_id} as {desired} within {timeout:g}s (last: {last})", file=sys.stderr)
raise SystemExit(1)
PY
then
  verify_identity || true
  exit 1
fi
# Keep the shared downloader lock until both the asynchronous operation and
# this final identity check have completed.
verify_identity
