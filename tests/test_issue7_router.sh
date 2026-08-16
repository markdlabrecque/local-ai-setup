#!/usr/bin/env bash
# Issue #7 red contract: portable localhost router, model presets, and safe lifecycle.
set -Eeuo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
LAUNCHER="$ROOT/scripts/run-router.sh"
CONFIG="$ROOT/config/router.json"
PRESETS="$ROOT/config/router-presets.json"
MODEL_HELPER="$ROOT/scripts/router-model.sh"
SMOKE="$ROOT/scripts/router-smoke.sh"
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

[[ -x "$LAUNCHER" ]] || fail "missing executable router launcher: scripts/run-router.sh"
[[ -f "$CONFIG" ]] || fail "missing tracked router config: config/router.json"
[[ -f "$PRESETS" ]] || fail "missing tracked router presets: config/router-presets.json"
[[ -x "$MODEL_HELPER" ]] || fail "missing executable safe model helper: scripts/router-model.sh"
[[ -x "$SMOKE" ]] || fail "missing executable optional real smoke: scripts/router-smoke.sh"

# The tracked contract must be relocatable: no machine-specific home paths or
# model filenames copied into a launcher instead of being derived from the manifest.
if grep -RInE '(/home/|/root/|/Users/|[[:space:]]~\/)' \
    "$LAUNCHER" "$CONFIG" "$PRESETS" "$MODEL_HELPER" "$SMOKE"; then
  fail "router assets contain an embedded machine-specific path"
fi

python3 - "$ROOT/config/models.json" "$CONFIG" "$PRESETS" <<'PY' || exit 1
import json
import pathlib
import sys

manifest_path, config_path, presets_path = map(pathlib.Path, sys.argv[1:])
manifest = json.loads(manifest_path.read_text())
config = json.loads(config_path.read_text())
presets = json.loads(presets_path.read_text())
artifacts = {a["id"]: a for a in manifest["artifacts"]}
assert set(artifacts) == {"qwen3.5-27b-q8_0", "qwen3.5-27b-q6_k"}
assert config["host"] == "127.0.0.1" and config["port"] == 8080
assert config.get("models_dir_env") and config.get("runtime_dir_env")
assert presets.get("autoload") is False
rows = presets.get("presets", {})
assert set(rows) == set(artifacts), rows
for model_id, artifact in artifacts.items():
    row = rows[model_id]
    assert row["filename"] == artifact["filename"], (model_id, row, artifact)
    assert row["context"] == 32768
    assert row["device"] == "Vulkan0"
    assert row["gpu_layers"] == 20
    assert row["flash_attention"] == "on"
    assert row["batch"] == 256 and row["ubatch"] == 128
    assert row["cache_k"] == "q8_0" and row["cache_v"] == "q8_0"
    assert row["autoload"] is False
    assert row.get("quality_preserving") is True
PY

# Keep the integration bounded and independent of a real llama.cpp/Vulkan
# install.  The fake server exposes only the router API used by the helpers.
tmp=$(mktemp -d)
cleanup() {
  if [[ -n ${server_pid:-} ]]; then kill "$server_pid" 2>/dev/null || true; wait "$server_pid" 2>/dev/null || true; fi
  rm -rf "$tmp"
}
trap cleanup EXIT
mkdir -p "$tmp/bin" "$tmp/models" "$tmp/runtime"

cat >"$tmp/fake-llama-server" <<'EOF'
#!/usr/bin/env python3
import json, os, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
log = pathlib = os.environ["FAKE_SERVER_ARGS"]
with open(log, "w") as f:
    json.dump(args, f)
# Refuse to serve if the launcher silently chose a non-local or non-router mode.
if "-m" in args or "--model" in args:
    raise SystemExit("single-model flag used in router mode")
if "--host" not in args or args[args.index("--host") + 1] != "127.0.0.1":
    raise SystemExit("router is not localhost-only")
if "--port" not in args or args[args.index("--port") + 1] != "8080":
    raise SystemExit("router did not bind port 8080")
for flag in ("--models-dir", "--models-preset", "--no-models-autoload", "--jinja"):
    if flag not in args:
        raise SystemExit("missing router flag " + flag)
state = {"loaded": None}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_): pass
    def send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path == "/health": self.send(200, {"status": "ok"})
        elif self.path == "/models":
            self.send(200, {"data": [] if state["loaded"] is None else [{"id": state["loaded"]}]})
        else: self.send(404, {})
    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.path in ("/models/load", "/models/unload"):
            state["loaded"] = body.get("model") if self.path.endswith("load") else None
            self.send(200, {"loaded": state["loaded"]})
        else: self.send(404, {})

HTTPServer(("127.0.0.1", 8080), Handler).serve_forever()
EOF
chmod +x "$tmp/fake-llama-server"

cat >"$tmp/bin/curl" <<'EOF'
#!/usr/bin/env python3
# Bounded curl substitute: enough of -fsS/-X/-d and a URL for this contract.
import json, sys, urllib.request
args = sys.argv[1:]; method = "GET"; data = None; url = None; i = 0
while i < len(args):
    a = args[i]
    if a in ("-X", "--request"): method = args[i + 1]; i += 2; continue
    if a in ("-d", "--data", "--data-raw"): data = args[i + 1].encode(); i += 2; continue
    if a in ("-H", "--header"): i += 2; continue
    if not a.startswith("-"): url = a
    i += 1
if not url: raise SystemExit("fake curl: missing URL")
request = urllib.request.Request(url, data=data, method=method)
request.add_header("Content-Type", "application/json")
try:
    with urllib.request.urlopen(request, timeout=3) as response: sys.stdout.buffer.write(response.read())
except Exception as exc:
    print(exc, file=sys.stderr); raise SystemExit(22)
EOF
chmod +x "$tmp/bin/curl"

# Static/argument contract for the actual router process.  The launcher must
# accept disposable paths and a disposable server binary; no PATH or $HOME
# assumption may be needed to test it.
export FAKE_SERVER_ARGS="$tmp/server-args.json"
"$LAUNCHER" --server "$tmp/fake-llama-server" --models-dir "$tmp/models" \
  --runtime-dir "$tmp/runtime" --foreground >"$tmp/launcher.out" 2>"$tmp/launcher.err" &
server_pid=$!
for _ in $(seq 1 100); do
  if PATH="$tmp/bin:$PATH" curl -fsS http://127.0.0.1:8080/health >"$tmp/health.json" 2>/dev/null; then break; fi
  kill -0 "$server_pid" 2>/dev/null || { cat "$tmp/launcher.err" >&2; fail "router launcher exited before health"; }
  sleep 0.02
done
[[ -s "$tmp/health.json" ]] || fail "bounded router never served /health"
PATH="$tmp/bin:$PATH" curl -fsS http://127.0.0.1:8080/models >"$tmp/initial-models.json"
python3 - "$tmp/server-args.json" "$tmp/health.json" "$tmp/initial-models.json" "$tmp/models" <<'PY' || exit 1
import json, sys
args = json.load(open(sys.argv[1]))
health = json.load(open(sys.argv[2])); models = json.load(open(sys.argv[3]))
assert health["status"] == "ok"
assert models["data"] == [], models
assert args.count("--host") == 1 and args[args.index("--host") + 1] == "127.0.0.1"
assert args.count("--port") == 1 and args[args.index("--port") + 1] == "8080"
assert "-m" not in args and "--model" not in args
for flag in ("--models-dir", "--models-preset", "--no-models-autoload", "--jinja"):
    assert flag in args
assert args[args.index("--models-dir") + 1] == sys.argv[4]
PY

# Use the manifest's exact filenames.  A checksum mismatch must fail before a
# POST reaches the server; the bounded hash shim then permits both fixtures so
# this test remains small and never downloads a model.
python3 - "$ROOT/config/models.json" "$tmp/models" <<'PY'
import json, pathlib, sys
m = json.load(open(sys.argv[1]))
for a in m["artifacts"]:
    pathlib.Path(sys.argv[2], a["filename"]).write_text("bounded fixture for " + a["id"])
PY
q8=$(python3 - "$ROOT/config/models.json" <<'PY'
import json, sys
m=json.load(open(sys.argv[1])); print(next(a["filename"] for a in m["artifacts"] if a["quantization"] == "Q8_0"))
PY
)
q6=$(python3 - "$ROOT/config/models.json" <<'PY'
import json, sys
m=json.load(open(sys.argv[1])); print(next(a["filename"] for a in m["artifacts"] if a["quantization"] == "Q6_K"))
PY
)
# A normal checksum check must reject the tiny fixture and leave /models empty.
if PATH="$tmp/bin:$PATH" "$MODEL_HELPER" load --model-id qwen3.5-27b-q8_0 \
    --models-dir "$tmp/models" --manifest "$ROOT/config/models.json" \
    --base-url http://127.0.0.1:8080; then
  fail "checksum-mismatched model was loaded"
fi
[[ "$(PATH="$tmp/bin:$PATH" curl -fsS http://127.0.0.1:8080/models)" == *'"data": []'* ]] || fail "checksum preflight reached model load"

cat >"$tmp/bin/sha256sum" <<EOF
#!/usr/bin/env bash
case "\$(basename "\${!#}")" in
  "$q8") printf '%s  %s\\n' '6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450' "\${!#}" ;;
  "$q6") printf '%s  %s\\n' '69e0f8527e0d937097cbcd486b51e2effaed963f49ef7962c9ef3eab45164ff8' "\${!#}" ;;
  *) exec /usr/bin/sha256sum "\$@" ;;
esac
EOF
chmod +x "$tmp/bin/sha256sum"

PATH="$tmp/bin:$PATH" "$MODEL_HELPER" load --model-id qwen3.5-27b-q8_0 \
  --models-dir "$tmp/models" --manifest "$ROOT/config/models.json" \
  --base-url http://127.0.0.1:8080
[[ "$(PATH="$tmp/bin:$PATH" curl -fsS http://127.0.0.1:8080/models)" == *"$q8"* ]] || fail "Q8 preset did not load"
PATH="$tmp/bin:$PATH" "$MODEL_HELPER" unload --model-id qwen3.5-27b-q8_0 \
  --models-dir "$tmp/models" --manifest "$ROOT/config/models.json" \
  --base-url http://127.0.0.1:8080
[[ "$(PATH="$tmp/bin:$PATH" curl -fsS http://127.0.0.1:8080/models)" == *'"data": []'* ]] || fail "unload did not clear runtime model"

# Q6 is selected by adding the manifest-named file, not by editing launcher or
# helper code.  The same helper invocation must select its manifest preset.
PATH="$tmp/bin:$PATH" "$MODEL_HELPER" load --model-id qwen3.5-27b-q6_k \
  --models-dir "$tmp/models" --manifest "$ROOT/config/models.json" \
  --base-url http://127.0.0.1:8080
[[ "$(PATH="$tmp/bin:$PATH" curl -fsS http://127.0.0.1:8080/models)" == *"$q6"* ]] || fail "Q6 manifest-named fixture was not selectable"
PATH="$tmp/bin:$PATH" "$MODEL_HELPER" unload --model-id qwen3.5-27b-q6_k \
  --models-dir "$tmp/models" --manifest "$ROOT/config/models.json" \
  --base-url http://127.0.0.1:8080
[[ -f "$tmp/models/$q8" && -f "$tmp/models/$q6" ]] || fail "model lifecycle deleted a model artifact"

# Real smoke is opt-in and must not be confused with this fake integration.
help=$($SMOKE --help 2>&1) || fail "optional smoke mode has no usable help"
grep -q -- '--real' <<<"$help" || fail "smoke helper lacks explicit real/opt-in mode"
if [[ ${ISSUE7_REAL_SMOKE:-0} == 1 ]]; then
  "$SMOKE" --real --base-url "${ISSUE7_BASE_URL:-http://127.0.0.1:8080}"
fi
printf 'ok\n'
