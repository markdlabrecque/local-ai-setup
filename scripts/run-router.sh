#!/usr/bin/env bash
# Start the pinned llama.cpp router in a bounded, localhost-only foreground mode.
set -Eeuo pipefail
readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
CONFIG="$PROJECT_ROOT/config/router.json"
PRESETS="$PROJECT_ROOT/config/router-presets.json"
SERVER="${LOCAL_AI_SERVER:-${LOCAL_AI_BIN_DIR:-$HOME/.local/bin}/llama-server}"
MODELS_DIR="${LOCAL_AI_MODEL_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/local-ai/models}"
RUNTIME_DIR="${LOCAL_AI_RUNTIME_DIR:-${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/local-ai}"
usage() {
  cat <<'EOF'
Usage: scripts/run-router.sh [options]

Run llama-server's router mode on the pinned local endpoint. The launcher
waits for /health before handing control to the foreground server process.

Options:
  --server PATH       llama-server executable
  --models-dir PATH   GGUF directory (files are never removed)
  --runtime-dir PATH disposable generated-runtime directory
  --config PATH       router JSON configuration
  --presets PATH      tracked JSON preset contract
  --foreground        accepted explicitly; the router always stays foreground
  -h, --help          show this help
EOF
}
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
while (($#)); do
  case "$1" in
    --server) (($# > 1)) || die "--server requires a path"; SERVER=$2; shift 2 ;;
    --models-dir) (($# > 1)) || die "--models-dir requires a path"; MODELS_DIR=$2; shift 2 ;;
    --runtime-dir) (($# > 1)) || die "--runtime-dir requires a path"; RUNTIME_DIR=$2; shift 2 ;;
    --config) (($# > 1)) || die "--config requires a path"; CONFIG=$2; shift 2 ;;
    --presets) (($# > 1)) || die "--presets requires a path"; PRESETS=$2; shift 2 ;;
    --foreground) shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done
[[ -x "$SERVER" ]] || die "llama-server is not executable: $SERVER"
[[ -r "$CONFIG" ]] || die "router config is not readable: $CONFIG"
[[ -r "$PRESETS" ]] || die "router presets are not readable: $PRESETS"
mkdir -p -- "$MODELS_DIR" "$RUNTIME_DIR"
mapfile -t settings < <(python3 - "$CONFIG" <<'PY'
import json, pathlib, sys
try:
    data = json.loads(pathlib.Path(sys.argv[1]).read_text())
    if data.get("host") != "127.0.0.1": raise ValueError("router host must be 127.0.0.1")
    port = data.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535: raise ValueError("router port is invalid")
    startup, health = data.get("startup_timeout_seconds", 30), data.get("health_timeout_seconds", 5)
    if not isinstance(startup, (int, float)) or startup <= 0 or not isinstance(health, (int, float)) or health <= 0: raise ValueError("router timeouts must be positive")
    print(data["host"]); print(port); print(startup); print(health)
except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
    print(f"error: invalid router config: {exc}", file=sys.stderr); raise SystemExit(1)
PY
)
((${#settings[@]} == 4)) || die "could not read router config"
HOST=${settings[0]}; PORT=${settings[1]}; STARTUP_TIMEOUT=${settings[2]}; HEALTH_TIMEOUT=${settings[3]}
# b10446 consumes INI presets. Keep the reviewed portable JSON contract in Git
# and materialize exact b10446 sections in a disposable runtime directory.
PRESET_INI="$RUNTIME_DIR/router-presets.ini"
python3 - "$PRESETS" "$PRESET_INI" <<'PY'
import json, pathlib, sys
source, destination = map(pathlib.Path, sys.argv[1:])
data = json.loads(source.read_text())
if data.get("schema_version") != 1 or data.get("build") != {"ref": "b10446", "commit": "adb55e5"}: raise SystemExit("error: unsupported router preset contract")
if data.get("autoload") is not False: raise SystemExit("error: router presets must disable autoload")
rows = data.get("presets")
if not isinstance(rows, dict) or not rows: raise SystemExit("error: router presets are empty")
lines = ["version = 1", ""]
for row in rows.values():
    filename = row.get("filename")
    if not isinstance(filename, str) or pathlib.Path(filename).name != filename or not filename: raise SystemExit("error: unsafe preset filename")
    required = {"context": 32768, "device": "Vulkan0", "gpu_layers": 20, "flash_attention": "on", "batch": 256, "ubatch": 128, "cache_k": "q8_0", "cache_v": "q8_0", "autoload": False}
    if any(row.get(k) != v for k, v in required.items()): raise SystemExit("error: preset tuning or autoload policy is invalid")
    lines.extend([f"[{filename}]", "c = 32768", "device = Vulkan0", "n-gpu-layers = 20", "flash-attn = on", "b = 256", "ub = 128", "cache-type-k = q8_0", "cache-type-v = q8_0", "load-on-startup = false", "stop-timeout = 10", ""])
destination.write_text("\n".join(lines), encoding="utf-8")
PY
command -v setsid >/dev/null 2>&1 || die "setsid is required for process-group cleanup"
server_pid=''
cleanup() {
  local status=$?
  trap - EXIT TERM INT
  if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
    kill -TERM -- "-$server_pid" 2>/dev/null || kill -TERM "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -f -- "$PRESET_INI"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
setsid -- "$SERVER" --host "$HOST" --port "$PORT" --models-dir "$MODELS_DIR" --models-preset "$PRESET_INI" --no-models-autoload --jinja &
server_pid=$!
python3 - "$HOST" "$PORT" "$STARTUP_TIMEOUT" "$HEALTH_TIMEOUT" <<'PY'
import json, sys, time, urllib.error, urllib.request
host, port, startup, request_timeout = sys.argv[1], int(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
deadline, url, last = time.monotonic() + startup, f"http://{host}:{port}/health", "not ready"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=request_timeout) as response:
            if response.status == 200:
                body = json.loads(response.read() or b"{}")
                if body.get("status") in ("ok", "healthy"): raise SystemExit(0)
                last = "health response did not report ok"
    except (OSError, ValueError, urllib.error.URLError) as exc: last = str(exc)
    time.sleep(0.05)
print(f"error: router did not become healthy within {startup:g}s: {last}", file=sys.stderr)
raise SystemExit(1)
PY
wait "$server_pid"
