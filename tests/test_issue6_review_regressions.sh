#!/usr/bin/env bash
# Focused red contracts for the Issue #6 review regressions (F1-F8).
# These tests use a bounded fake llama-cli and never load a model.
set -u
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HARNESS="$ROOT/scripts/run-hybrid-vulkan-tuning.sh"
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
[[ -x "$HARNESS" ]] || fail "missing executable harness"

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cat >"$tmp/fake-llama-cli" <<'EOF'
#!/usr/bin/env bash
set -u
if [[ ${1:-} == --version ]]; then
  printf '%s\n' 'llama-cli version b10446 (adb55e5)'
  exit 0
fi
if [[ -n ${FAKE_ARG_LOG:-} ]]; then
  prompt_words=0; previous=''
  for arg in "$@"; do
    if [[ $previous == --prompt ]]; then prompt_words=$(wc -w <<<"$arg"); fi
    previous=$arg
  done
  printf '%s\t%s\n' "$prompt_words" "$*" >>"$FAKE_ARG_LOG"
fi
if [[ ${FAKE_MODE:-} == signal ]]; then
  (trap '' TERM INT; while :; do sleep 1; done) & child=$!
  printf '%s\n' "$child" >"${FAKE_CHILD_PID:?}"
  trap 'exit 143' TERM INT
  wait "$child"
fi
printf '%s\n' 'Vulkan0 : AMD Radeon RX 6900 XT (1002:73BF)' >&2
printf '%s\n' 'llama_context: n_ctx = 32768' >&2
printf '%s\n' 'offloaded 20 layers' >&2
case ${FAKE_MODE:-} in
  preceding) printf 'UNEXPECTED_PREFIX\nLOCAL_AI_HYBRID_TUNING_OK\n' ;;
  trailing) printf 'LOCAL_AI_HYBRID_TUNING_OK\nUNEXPECTED_SUFFIX\n' ;;
  *) printf 'LOCAL_AI_HYBRID_TUNING_OK\n' ;;
esac
printf '%s\n' 'llama_perf_context_print: prompt eval time = 1000.00 ms / 32000 tokens (0.03 ms per token, 32000.00 tokens per second)' >&2
printf '%s\n' 'llama_perf_context_print: eval time = 500.00 ms / 10 runs (50.00 ms per token, 20.00 tokens per second)' >&2
printf '%s\n' 'finish_reason=stop' >&2
EOF
chmod +x "$tmp/fake-llama-cli"

make_config() {
  local output=$1 prompt_kind=${2:-short}
  python3 - "$output" "$prompt_kind" <<'PY'
import json, sys
path, kind = sys.argv[1:]
prompt = ("token " * 32000).strip() if kind == "long" else "Respond with exactly: LOCAL_AI_HYBRID_TUNING_OK"
json.dump({
    "schema_version": "hybrid-vulkan-tuning-v1",
    "model": {"id": "Qwen3.5-27B-Q8_0", "sha256": "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450", "quantization": "Q8_0"},
    "build": {"ref": "b10446", "commit": "adb55e5"},
    "prompt": prompt,
    "context_tokens": 32768,
    "matrix": {"gpu_layers": [20], "flash_attention": ["on"], "batch": [256],
                "ubatch": [128], "kv_cache": ["q8_0"]},
    "safety": {"minimum_vram_free_mib": 0, "minimum_mem_available_mib": 0,
               "maximum_swap_in_pages": 1000000000},
}, open(path, "w"), separators=(",", ":"))
PY
}
make_config "$tmp/config.json"
run_harness() {
  local config=$1 output=$2
  shift 2
  "$HARNESS" --config "$config" --llama-cli "$tmp/fake-llama-cli" --output "$output" --run-timeout 5 "$@"
}

# F1: fixture measurements cannot be presented as a live result; live runs keep
# sanitized, per-run measurement/PCI/offload/observed-command evidence.
if run_harness "$tmp/config.json" "$tmp/fixture.json" \
    --measurements '{"vram_capacity_mib":16384,"peak_vram_mib":1,"min_mem_available_mib":1,"swap_in_pages":0}' \
    >/dev/null 2>&1; then
  fail "F1 accepted injected fixture measurements"
fi
run_harness "$tmp/config.json" "$tmp/live.json" >"$tmp/live.stdout" 2>"$tmp/live.stderr" || {
  cat "$tmp/live.stderr" >&2
  fail "F1 live fake run failed"
}
python3 - "$tmp/live.json" <<'PY' || exit 1
import json, sys
result = json.load(open(sys.argv[1]))
for run in result["runs"]:
    evidence = run["evidence"]
    assert evidence["measurement_source"] == "live"
    assert evidence["vram_pci_id"] == "1002:73BF"
    assert evidence["vram_card"] == "card1"
    assert evidence["offload_evidence"] is True
    assert evidence["observed_command"]
PY

# F2: both quantized KV cache types must be forwarded, not just cache-type-k.
: >"$tmp/cache-args"
FAKE_ARG_LOG="$tmp/cache-args" run_harness "$tmp/config.json" "$tmp/cache.json" >/dev/null 2>&1 \
  || fail "F2 cache forwarding run failed"
python3 - "$tmp/cache-args" <<'PY' || exit 1
import sys
line = open(sys.argv[1]).read()
assert "--cache-type-k q8_0" in line
assert "--cache-type-v q8_0" in line
PY

# F3: each successful row has tuple/config/evidence identities; stale and
# tampered rows are fail-closed on resume.
run_harness "$tmp/config.json" "$tmp/identity.json" >/dev/null 2>&1 \
  || fail "F3 identity fixture run failed"
python3 - "$tmp/identity.json" <<'PY' || exit 1
import json, sys
result = json.load(open(sys.argv[1]))
for row in result["runs"]:
    assert row["status"] == "pass"
    assert row.get("tuple_id")
    assert row.get("config_id")
    assert row.get("evidence_id")
PY
cp "$tmp/identity.json" "$tmp/tampered.json"
python3 - "$tmp/tampered.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["runs"][0]["metrics"]["peak_vram_mib"] = 999999
json.dump(d, open(p, "w"))
PY
if run_harness "$tmp/config.json" "$tmp/tampered.json" --resume >/dev/null 2>&1; then
  fail "F3 accepted a tampered resume row"
fi
cp "$tmp/identity.json" "$tmp/stale.json"
python3 - "$tmp/stale.json" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d["runs"][0]["config_id"] = "stale-config-id"
json.dump(d, open(p, "w"))
PY
if run_harness "$tmp/config.json" "$tmp/stale.json" --resume >/dev/null 2>&1; then
  fail "F3 accepted a stale resume row"
fi

# F4: exact completion is the complete response, so either preceding or
# trailing response text rejects the tuple.
for mode in preceding trailing; do
  if FAKE_MODE=$mode run_harness "$tmp/config.json" "$tmp/$mode.json" >/dev/null 2>&1; then
    fail "F4 accepted $mode response text"
  fi
done

# F5: timing fields must be measured prompt/gen metrics, with honest names and
# non-zero values; TTFT is not a substitute for prompt_eval_ms.
run_harness "$tmp/config.json" "$tmp/metrics.json" >/dev/null 2>&1 \
  || fail "F5 metrics fixture run failed"
python3 - "$tmp/metrics.json" <<'PY' || exit 1
import json, sys
result = json.load(open(sys.argv[1]))
for row in result["runs"]:
    m = row["metrics"]
    assert m["prompt_eval_ms"] > 0
    assert m["generation_eval_ms"] > 0
    assert m["prompt_tokens"] >= 32000
    assert m["generation_tokens"] > 0
    assert m["prompt_tokens_per_second"] > 0
    assert m["generation_tokens_per_second"] > 0
PY

# F6: TERM and INT clean up the entire descendant process group, not only the
# supervisor process.
for signal in TERM INT; do
  rm -f "$tmp/child-$signal"
  FAKE_MODE=signal FAKE_CHILD_PID="$tmp/child-$signal" "$HARNESS" \
    --config "$tmp/config.json" --llama-cli "$tmp/fake-llama-cli" \
    --output "$tmp/signal-$signal.json" --run-timeout 5 >/dev/null 2>&1 & supervisor=$!
  for _ in $(seq 1 50); do [[ -s "$tmp/child-$signal" ]] && break; sleep 0.02; done
  [[ -s "$tmp/child-$signal" ]] || fail "F6 fake descendant did not start for $signal"
  child=$(<"$tmp/child-$signal")
  kill -"$signal" "$supervisor" 2>/dev/null || true
  wait "$supervisor" 2>/dev/null || true
  if kill -0 "$child" 2>/dev/null; then
    kill -KILL "$child" 2>/dev/null || true
    fail "F6 $signal left a descendant alive"
  fi
done

# F7: stability is three consecutive requests per tuple and includes a real
# >=32K-token prompt/context measurement, rather than one short smoke request.
make_config "$tmp/long-config.json" long
: >"$tmp/stability-requests"
FAKE_ARG_LOG="$tmp/stability-requests" run_harness "$tmp/long-config.json" "$tmp/stability.json" >/dev/null 2>&1 \
  || fail "F7 stability run failed"
python3 - "$tmp/stability.json" "$tmp/stability-requests" <<'PY' || exit 1
import json, sys
result = json.load(open(sys.argv[1]))
requests = [int(x.split("\t", 1)[0]) for x in open(sys.argv[2]).read().splitlines() if x.strip()]
assert len(result["runs"]) == 1
assert len(requests) == 3
assert all(n >= 32000 for n in requests)
row = result["runs"][0]
assert row["status"] == "pass"
assert row["metrics"]["prompt_tokens"] >= 32000
PY

# F8: resume the copied output itself (the original is removed), and do not
# accidentally test resuming the original output path.
cp "$tmp/stability.json" "$tmp/copied-output.json"
rm "$tmp/stability.json"
: >"$tmp/resume-requests"
FAKE_ARG_LOG="$tmp/resume-requests" run_harness "$tmp/long-config.json" "$tmp/copied-output.json" --resume >/dev/null 2>&1 \
  || fail "F8 copied output did not resume"
[[ ! -s "$tmp/resume-requests" ]] || fail "F8 copied-output resume reran completed tuples"

printf 'ok\n'
