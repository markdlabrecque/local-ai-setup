#!/usr/bin/env bash
# Issue #6 red contract: deterministic, resumable hybrid Vulkan tuning.
set -u
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
HARNESS="$ROOT/scripts/run-hybrid-vulkan-tuning.sh"
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
[[ -x "$HARNESS" ]] || fail "missing executable hybrid Vulkan tuning harness: scripts/run-hybrid-vulkan-tuning.sh"

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
cat >"$tmp/config.json" <<'EOF'
{
  "schema_version": "hybrid-vulkan-tuning-v1",
  "model": {
    "id": "Qwen3.5-27B-Q8_0",
    "sha256": "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450",
    "quantization": "Q8_0"
  },
  "build": {"ref": "b10446", "commit": "adb55e5"},
  "prompt": "token token token token token token token token token token",
  "context_tokens": 32768,
  "matrix": {
    "gpu_layers": [16, 20],
    "flash_attention": ["on", "off"],
    "batch": [256],
    "ubatch": [128],
    "kv_cache": ["q8_0"]
  },
  "safety": {
    "minimum_vram_free_mib": 1024,
    "minimum_mem_available_mib": 8192,
    "maximum_swap_in_pages": 0
  }
}
EOF
cat >"$tmp/fake-llama-cli" <<'EOF'
#!/usr/bin/env bash
set -u
if [[ ${1:-} == --version ]]; then printf '%s\n' 'llama-cli version b10446 (adb55e5)'; exit 0; fi
printf '%s\n' "$*" >>"${HYBRID_FAKE_ARG_LOG:?}"
printf 'Vulkan0 device=AMD Radeon RX 6900 XT\nllama_context: n_ctx = 32768\noffloaded layers\n' >&2
printf 'LOCAL_AI_HYBRID_TUNING_OK\n'
printf 'llama_perf_context_print: prompt eval time = 1000.00 ms / 32000 tokens (0.03 ms per token, 32000.00 tokens per second)\n' >&2
printf 'llama_perf_context_print: eval time = 500.00 ms / 10 runs (50.00 ms per token, 20.00 tokens per second)\n' >&2
printf 'finish_reason=stop\n' >&2
EOF
chmod +x "$tmp/fake-llama-cli"
python3 - "$tmp/config.json" <<'PY'
import json, sys
path = sys.argv[1]
data = json.load(open(path))
data["prompt"] = ("token " * 32000).strip()
json.dump(data, open(path, "w"), indent=2)
PY
export HYBRID_FAKE_ARG_LOG="$tmp/args"
out="$tmp/results.json"

"$HARNESS" --config "$tmp/config.json" --llama-cli "$tmp/fake-llama-cli" \
  --output "$out" --run-timeout 5 \
  || fail "valid fixed harness fixture was rejected"

python3 - "$out" "$tmp/args" <<'PY' || exit 1
import json, pathlib, re, sys
result = json.loads(pathlib.Path(sys.argv[1]).read_text())
text = pathlib.Path(sys.argv[1]).read_text()
assert result.get("schema_version") == "hybrid-vulkan-tuning-v1"
assert result["model"]["sha256"] == "6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450"
assert result["model"]["quantization"] == "Q8_0"
assert result["build"] == {"ref": "b10446", "commit": "adb55e5"}
assert len(result["prompt"].split()) == 32000
assert result["context_tokens"] >= 32768
runs = result["runs"]
assert len(runs) == 4, runs
assert all(len(r["attempts"]) == 3 for r in runs)
assert all(a["metrics"]["prompt_tokens"] >= 32000 for r in runs for a in r["attempts"])
expected = {(16, "on"), (16, "off"), (20, "on"), (20, "off")}
assert {(r["parameters"]["gpu_layers"], r["parameters"]["flash_attention"]) for r in runs} == expected
for run in runs:
    p, m = run["parameters"], run["metrics"]
    assert p["batch"] == 256 and p["ubatch"] == 128 and p["kv_cache"] == "q8_0"
    for key in ("time_to_first_token_ms", "prompt_tokens_per_second", "generation_tokens_per_second",
                "peak_vram_mib", "min_mem_available_mib", "swap_in_pages", "exit_code", "timed_out"):
        assert key in m, (key, run)
    assert m["timed_out"] is False and m["exit_code"] == 0
    assert m["peak_vram_mib"] <= 15360       # desktop VRAM headroom: >= 1 GiB
    assert m["min_mem_available_mib"] >= 8192
    assert m["swap_in_pages"] == 0
    assert run["lifecycle"] == {"started": True, "bounded": True, "finished": True}
    assert run["status"] == "pass"
assert result["stable_candidate"]["parameters"] in [r["parameters"] for r in runs]
assert result["stable_candidate"]["parameters"]["kv_cache"] == "q8_0"
assert result["stable_candidate"]["parameters"]["quantization"] == "Q8_0"
assert result["selection"]["deterministic"] is True
assert result["resumability"]["key"] == "parameter_tuple"
assert not re.search(r'(?i)(?:\\.gguf|/home/|/root/|/tmp/|api[_-]?key|access[_-]?token|secret|password|bearer\\s+|ghp_)', text)
args = pathlib.Path(sys.argv[2]).read_text().splitlines()
assert len(args) == 12
for line in args:
    for flag in ("--ctx-size 32768", "--gpu-layers", "--flash-attn", "--batch-size 256", "--ubatch-size 128"):
        assert flag in line, (flag, line)
    assert "--cache-type-k q8_0" in line or "q8_0" in line
PY

# Resume must not rerun completed tuples, and must reject a changed fixed identity.
cp "$out" "$tmp/copied-output.json"
rm "$out"
HYBRID_FAKE_ARG_LOG="$tmp/resume-args" "$HARNESS" --config "$tmp/config.json" --llama-cli "$tmp/fake-llama-cli" \
  --output "$tmp/copied-output.json" --resume --run-timeout 5 \
  || fail "resume of completed matrix failed"
[[ ! -s "$tmp/resume-args" ]] || fail "resume reran completed parameter tuples"
printf '\n' >>"$tmp/config.json"
if "$HARNESS" --config "$tmp/config.json" --llama-cli "$tmp/fake-llama-cli" --output "$tmp/copied-output.json" --resume >/dev/null 2>&1; then
  fail "changed fixed prompt/model/build identity was accepted on resume"
fi
printf 'ok\n'
