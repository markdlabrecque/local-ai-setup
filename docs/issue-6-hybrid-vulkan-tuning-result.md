# Issue #6 hybrid Vulkan tuning

Sanitized result: [`issue-6-hybrid-vulkan-tuning-result.json`](issue-6-hybrid-vulkan-tuning-result.json)

The practical Q8_0 matrix ran on Vulkan0, the RX 6900 XT at PCI ID
`1002:73BF`, with the pinned llama.cpp `b10446` / `adb55e5` build and a
32768-token context. Every passing tuple completed three consecutive streamed
requests exactly as `LOCAL_AI_HYBRID_TUNING_OK`; the third request processed a
measured 32,025-token prompt canary. Measurements came from live `/proc` and
the selected DRM card rather than injected fixtures.

| GPU layers | Flash attention | Batch / ubatch | K/V cache | Result | Peak VRAM MiB | Minimum available RAM MiB | Generation tok/s |
|---:|:---:|---:|:---:|:---:|---:|---:|---:|
| 16 | on | 256 / 128 | q8_0 / q8_0 | pass (3/3) | 8552 | 54428 | 2.24 |
| 16 | off | 256 / 128 | q8_0 / q8_0 | incompatible | — | — | — |
| 20 | on | 256 / 128 | q8_0 / q8_0 | pass (3/3) | 10177 | 54423 | 2.38 |
| 20 | off | 256 / 128 | q8_0 / q8_0 | incompatible | — | — | — |

The pinned runtime rejects quantized V cache without flash attention
(`quantized V cache requires flash_attn to be enabled`), so the two flash-off
probes exited 1 without timing out and are not candidates. This is an observed
compatibility result, not a stability failure.

The deterministic stable candidate is **20 GPU layers, flash attention on,
batch 256, ubatch 128, q8_0 K and V caches**. VRAM capacity was 16368 MiB and
the peak was 10177 MiB, preserving more than the required 1 GiB desktop
headroom. All passing attempts recorded zero swap-in pages. The selection
policy considers only passing candidates, then orders by generation
throughput, prompt throughput, prompt-evaluation duration, generation duration,
VRAM, and parameter tuple.

`prompt_eval_ms` is llama.cpp's prompt-evaluation duration; it is not presented
as time to first token. Aggregate timing values are arithmetic means across the
two short requests and the long-context canary, while `prompt_tokens` records
the largest measured prompt.

## Reproduction

The model remains outside the repository. Set local paths before running:

```bash
MODEL="${MODEL:?set the local Q8_0 model path}"
LLAMA_CLI="${LLAMA_CLI:?set the local llama-cli path}"
OUTPUT="${OUTPUT:?set a writable output path outside the repository}"
scripts/run-hybrid-vulkan-tuning.sh \
  --config config/hybrid-vulkan-tuning.json \
  --model "$MODEL" --llama-cli "$LLAMA_CLI" \
  --output "$OUTPUT" --run-timeout 3000
```

Resume uses the same output and fails closed if configuration, tuple, lifecycle,
metrics, quality, attempts, or evidence identities differ:

```bash
scripts/run-hybrid-vulkan-tuning.sh \
  --config config/hybrid-vulkan-tuning.json \
  --model "$MODEL" --llama-cli "$LLAMA_CLI" \
  --output "$OUTPUT" --resume --run-timeout 3000
```

Only the bounded sanitized aggregate JSON and this summary are tracked. The
model, transient per-attempt files, and raw verbose logs are not committed.
