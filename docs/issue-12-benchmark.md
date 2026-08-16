# Issue #12 benchmark

This is the fixed, auditable benchmark for the Issue #6 Q8_0 hybrid Vulkan
candidate and the Issue #11 `issue-11-evaluation` quality report. It measures
one **cold** (cache miss) and one **warm** (cache hit) lifecycle with a new,
bounded process group for each run.

## Portable gate

The committed fake runtime is used by the tests; it does not load a model or
use a GPU. The safe gate is:

```text
python3 -m unittest tests.test_issue12_benchmark
```

## Safe live command (do not run in the child)

After installing the pinned `llama-cli` and verifying the 28 GB model checksum,
run this command from the repository. Replace the paths, and select only the
RX 6900 XT adapter as `Vulkan0`:

```bash
scripts/run-benchmark.sh --config config/benchmark.json \
  --tuning-result docs/issue-6-hybrid-vulkan-tuning-result.json \
  --evaluation-report results/evaluation.json \
  --model "$HOME/.local/share/local-ai/models/Qwen3.5-27B-Q8_0.gguf" \
  --llama-cli "$HOME/.local/share/local-ai/runtime/llama-cli" \
  --output results/issue-12-benchmark.json
```

This is a real 28 GB benchmark and is intentionally not run by the child.
At the observed Issue #6 prompt rate (about 40 prompt tokens/second), a 16
prompt-token request is negligible; model load plus two bounded runs is
expected to take roughly **5–15 minutes** on this machine, with peak memory
and VRAM varying by the desktop state. The command's `--run-timeout` default is
120 seconds per lifecycle; increase it only deliberately (the config hard
limit is 300 seconds).

## Inputs and observations

The candidate is read from Issue #6's `stable_candidate`, including Q8_0,
20 GPU layers, flash attention on, batch 256, ubatch 128, and q8_0 KV cache.
The evaluator report must be the passing Issue #11 suite. Prompt and output
lengths are fixed at 16 and 8 tokens, with a 32768-token context.

`load time` and `prompt eval` are separate observations. **TTFT** is the
first-token timestamp observed while consuming the streaming stdout; it is
never substituted with prompt eval. The runner records prompt tokens/second
and generation tokens/second from observed timings.

Each run records selected GPU, VRAM capacity/peak, minimum available RAM, and
swap-in pages. It fails closed when the selected GPU or resource thresholds do
not pass. Timeout kills the complete process group, including descendants;
stdout/stderr are drained into bounded captures so a noisy process cannot fill
a pipe. raw logs are never placed in the result. Artifacts are sanitized and
contain no secrets or paths.

The result schema is `schemas/benchmark-result.schema.json`. Provenance hashes
cover the config, Issue #6 tuning result, Issue #11 evaluator report, and the
result itself. `--resume` only accepts an intact, matching artifact: any
identity mismatch or tamper is rejected before starting `llama-cli`.
