# Issue #5 direct llama.cpp baseline

Verified foreground run (sanitized summary; 2025-08-15) used the pinned Q8_0 artifact:

- **Build:** llama.cpp b10446
- **Model SHA-256:** `6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450`
- **Device:** Vulkan0 — AMD Radeon RX 6900 XT
- **Context:** 32768 tokens
- **GPU layers:** 20
- **Completion:** `LOCAL_AI_BASELINE_OK`
- **Timing:** prompt 18.3 t/s; generation 2.7 t/s
- **Peak resources:** RAM 27500 MiB; VRAM 10688 MiB; swap 0 MiB
- **Minimum MemAvailable:** 53296 MiB
- **Lifecycle:** exit 0; timeout false

The run met the model-load, Vulkan-device, 32K-context, streaming, and memory-safety acceptance checks. `llama-cli` reported `stop_reason` as unknown, so terminal timing evidence plus clean exit—not an explicit stop reason—was used for completion assessment.

## Reproduction

Set the placeholders to local paths; the model is never stored in this repository:

```bash
MODEL="${MODEL:-$HOME/.local/share/local-ai/models/Qwen3.5-27B-Q8_0.gguf}"
LLAMA_CLI="${LLAMA_CLI:-$HOME/.local/bin/llama-cli}"
scripts/run-direct-baseline.sh \
  --model "$MODEL" \
  --sha256 6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450 \
  --llama-cli "$LLAMA_CLI" \
  --gpu-layers 20 --context 32768 --timeout 1800 \
  --expected-completion LOCAL_AI_BASELINE_OK \
  --prompt 'Respond with exactly: LOCAL_AI_BASELINE_OK' \
  --output "${OUTPUT:-docs/issue-5-direct-baseline-result.json}"
```

The runner output is bounded, sanitized structured evidence and may be written to the tracked JSON path above. Machine-specific raw verbose logs remain outside Git; this document is the portable evidence summary.
