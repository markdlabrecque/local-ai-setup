#!/usr/bin/env bash
# Focused red contracts for the Issue #6 review cycle 2 findings.
# These use bounded fakes and invoke production runners, not fixture parsers.
set -u
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
DIRECT_RUNNER="$ROOT/scripts/run-direct-baseline.sh"
HARNESS="$ROOT/scripts/run-hybrid-vulkan-tuning.sh"
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
failures=0
record_failure() { printf 'FAIL: %s\n' "$1" >&2; failures=$((failures + 1)); }
[[ -x "$DIRECT_RUNNER" ]] || fail "missing executable direct runner"
[[ -x "$HARNESS" ]] || fail "missing executable hybrid harness"

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
model="$tmp/model.gguf"
printf 'bounded model fixture\n' >"$model"
model_sha=$(sha256sum "$model" | awk '{print $1}')
hybrid_sha=6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450
printf '{"ram_mib":100,"vram_mib":200,"swap_mib":0}\n' >"$tmp/measure.json"

cat >"$tmp/fake-llama-cli" <<'EOF'
#!/usr/bin/env bash
set -u
if [[ -n ${FAKE_INVOCATION_LOG:-} ]]; then
  printf '%s\n' "$*" >>"$FAKE_INVOCATION_LOG"
fi
if [[ ${1:-} == --version ]]; then
  printf '%s\n' 'llama-cli version b10446 (adb55e5)'
  exit 0
fi

flash=''
previous=''
for arg in "$@"; do
  if [[ $previous == --flash-attn ]]; then flash=$arg; fi
  [[ $arg == --flash-attn=off ]] && flash=off
  previous=$arg
done

if [[ $flash == off ]]; then
  printf '%s\n' 'quantized V cache requires flash_attn to be enabled' >&2
  printf '%s\n' 'ARBITRARY_STDERR_SECRET /very/private/path bearer TOPSECRET' >&2
  exit 1
fi

printf '%s\n' 'load: Vulkan0 device=AMD Radeon RX 6900 XT (1002:73BF)' >&2
printf '%s\n' 'llama_context: n_ctx = 32768' >&2
printf '%s\n' 'offloaded 20 layers' >&2
printf '%s\n' 'llama_perf_context_print: prompt eval time = 1000.00 ms / 32000 tokens (0.03 ms per token, 32000.00 tokens per second)' >&2
printf '%s\n' 'llama_perf_context_print: eval time = 500.00 ms / 10 runs (50.00 ms per token, 20.00 tokens per second)' >&2
printf '%s\n' 'finish_reason=stop' >&2
if [[ ${FAKE_BAD_PREFIX:-} == 1 ]]; then
  printf 'UNEXPECTED_PREFIX\nLOCAL_AI_HYBRID_TUNING_OK\n'
else
  printf 'LOCAL_AI_HYBRID_TUNING_OK\n'
fi
EOF
chmod +x "$tmp/fake-llama-cli"
export FAKE_INVOCATION_LOG="$tmp/invocations"

# Review finding 1: the actual production --model/direct-runner path must
# enforce an exact completion. A fixture-only parser check would miss this.
if FAKE_BAD_PREFIX=1 BASELINE_MEASURE_FILE="$tmp/measure.json" \
    "$DIRECT_RUNNER" --model "$model" --sha256 "$model_sha" \
    --llama-cli "$tmp/fake-llama-cli" --prompt 'Say hello.' --context 32768 \
    --expected-completion LOCAL_AI_HYBRID_TUNING_OK --timeout 5 \
    --output "$tmp/prefix.json" >"$tmp/prefix.stdout" 2>"$tmp/prefix.stderr"; then
  record_failure "production --model/direct-runner path accepted a prefixed completion"
fi
[[ -s "$FAKE_INVOCATION_LOG" ]] || fail "direct-runner test did not invoke bounded fake"

make_config() {
  local output=$1 flash=$2
  python3 - "$output" "$flash" "$hybrid_sha" <<'PY'
import json, sys
path, flash, model_sha = sys.argv[1:]
flash_values = ["on", "off"] if flash == "both" else [flash]
json.dump({
    "schema_version": "hybrid-vulkan-tuning-v1",
    "model": {"id": "Qwen3.5-27B-Q8_0", "sha256": model_sha, "quantization": "Q8_0"},
    "build": {"ref": "b10446", "commit": "adb55e5"},
    "prompt": ("token " * 32000).strip(),
    "context_tokens": 32768,
    "stability": {"consecutive_requests": 1, "long_context_canary_tokens": 32000},
    "matrix": {"gpu_layers": [20], "flash_attention": flash_values, "batch": [256],
                "ubatch": [128], "kv_cache": ["q8_0"]},
    "safety": {"minimum_vram_free_mib": 0, "minimum_mem_available_mib": 0,
               "maximum_swap_in_pages": 1000000000},
}, open(path, "w"), separators=(",", ":"))
PY
}

# Review finding 2: an expected q8 K/V incompatibility is aggregate evidence,
# not an opaque failed run; only its allowlisted reason may survive sanitizing.
make_config "$tmp/flash-off-config.json" both
if ! "$HARNESS" --config "$tmp/flash-off-config.json" \
  --llama-cli "$tmp/fake-llama-cli" --output "$tmp/flash-off.json" --run-timeout 5 \
  >"$tmp/flash-off.stdout" 2>"$tmp/flash-off.stderr"; then
  record_failure "flash-off incompatibility was not aggregated"
fi
if [[ -s "$tmp/flash-off.json" ]]; then
python3 - "$tmp/flash-off.json" <<'PY'

import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
result = json.loads(path.read_text())
assert len(result["runs"]) == 2
run = next(r for r in result["runs"] if r["parameters"]["flash_attention"] == "off")
assert run["status"] == "incompatible"
assert run["metrics"]["exit_code"] == 1
assert run["metrics"]["timed_out"] is False
assert run["evidence"]["failure_reason"] == "quantized V cache requires flash_attn to be enabled"
text = path.read_text()
for secret in ("ARBITRARY_STDERR_SECRET", "/very/private/path", "TOPSECRET"):
    assert secret not in text, secret
assert "stderr" not in run["evidence"]
PY
  if [[ $? -ne 0 ]]; then
    record_failure "aggregate evidence omitted or retained an unsafe flash-off failure reason"
  fi
else
  record_failure "flash-off aggregate output was not written"
fi

if [[ $failures -ne 0 ]]; then
  printf '%s focused regressions failed\n' "$failures" >&2
  exit 1
fi
printf 'ok\n'
