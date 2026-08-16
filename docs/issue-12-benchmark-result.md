# Issue #12 live benchmark result

The pinned Q8_0 candidate completed one explicitly evicted cold run followed by one warm run. Both runs used llama.cpp `b10446` / `adb55e5`, `Vulkan0` (`1002:73BF`), 32,768 context, 20 GPU layers, flash attention, batch 256, ubatch 128, and q8_0 K/V cache. The complete sanitized machine-readable evidence is [`issue-12-benchmark-result.json`](issue-12-benchmark-result.json).

| Mode | Load ms | TTFT ms | Prompt tok/s | Generation tok/s | Peak VRAM MiB | Min available RAM MiB | Swap-in pages | Status |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| cold | 8782.61 | 12364.38 | 18.86 | 2.95 | 10173 | 54444 | 0 | pass |
| warm | 3427.75 | 7203.76 | 18.89 | 2.92 | 10173 | 54346 | 0 | pass |

The non-synthetic Issue #11 evaluation passed all 14 required cases with score 1.000 before benchmarking. The benchmark records its exact report SHA-256. Runner-owned `/proc` and DRM samples remained within the configured margins; no swap-in occurred, and the RX 6900 XT retained more than the required 1 GiB VRAM reserve. Cold cache preparation used unprivileged model-file `POSIX_FADV_DONTNEED` after checksum verification.
