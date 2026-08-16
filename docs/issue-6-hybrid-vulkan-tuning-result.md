# Issue #6 hybrid Vulkan tuning

Sanitized result: [`issue-6-hybrid-vulkan-tuning-result.json`](issue-6-hybrid-vulkan-tuning-result.json)

The practical Q8_0 matrix ran on the RX 6900 XT with the pinned llama.cpp
`b10446` / `adb55e5` build, the fixed exact-completion prompt, and a 32768-token
context. All four candidates passed exact completion, device/context/offload,
VRAM-headroom, RAM, swap, exit, timeout, and measured timing checks. Each
parameter tuple is exercised by three bounded consecutive requests; the final
request is a measured >=32,000-token long-context canary before selection.

| GPU layers | Flash attention | Batch / ubatch | KV cache | Result | Peak VRAM MiB | Minimum available MiB | Generation tok/s |
|---:|:---:|---:|:---:|:---:|---:|---:|---:|
| 16 | on | 256 / 128 | q8_0 | pass | 8643 | 54101 | 2.40 |
| 16 | off | 256 / 128 | q8_0 | pass | 9003 | 53745 | 2.43 |
| 20 | on | 256 / 128 | q8_0 | pass | 10293 | 54250 | 2.57 |
| 20 | off | 256 / 128 | q8_0 | pass | 10653 | 53849 | 2.57 |

The deterministic stable candidate is **20 GPU layers, flash attention on,
batch 256, ubatch 128, q8_0 KV cache**. VRAM capacity was 16368 MiB, leaving
at least the required 1 GiB desktop headroom; swap-in pages were zero for every
run. Selection considers only fully stable quality/safety passes, then orders
by generation throughput descending, prompt throughput descending, prompt and
generation evaluation time ascending, peak VRAM ascending, and tuple ID
ascending. Timing fields are measured `prompt_eval_ms` and
`generation_eval_ms`; they are never substituted with zero defaults.

## Reproduction

The model remains outside the repository. Set local paths before running:

```bash
MODEL="${MODEL:?set the local Q8_0 model path}"
LLAMA_CLI="${LLAMA_CLI:?set the local llama-cli path}"
OUTPUT="${OUTPUT:?set a writable output path outside the repository}"
scripts/run-hybrid-vulkan-tuning.sh \
  --config config/hybrid-vulkan-tuning.json \
  --model "$MODEL" --llama-cli "$LLAMA_CLI" \
  --output "$OUTPUT" --run-timeout 1800
# The harness forwards both --cache-type-k q8_0 and --cache-type-v q8_0;
# do not pass --measurements (injected resource values are rejected).
```

Resume uses the same output and rejects a changed configuration identity:

```bash
scripts/run-hybrid-vulkan-tuning.sh \
  --config config/hybrid-vulkan-tuning.json \
  --model "$MODEL" --llama-cli "$LLAMA_CLI" \
  --output "$OUTPUT" --resume --run-timeout 1800

Resume is fail-closed: it validates the schema, config identity, tuple and
attempt identities, lifecycle, exact completion/timing evidence, and the
metrics-bound evidence ID before skipping a tuple.
```

Only the sanitized JSON result and this summary are tracked; raw logs and the
model are not. Live runs reject `--measurements` injection. Each run retains
bounded sanitized command/device/PCI/offload/provenance evidence and atomic
per-tuple attempt records. Resume validates schema, config ID, tuple ID,
completion evidence, lifecycle, and the evidence-bound metrics before skipping
a completed tuple.
