# Issue #13: production inference profile

## Selection

The production selection is **quality-first Q8_0** with the tracked 20-layer
Vulkan router preset. [`config/production-profile.json`](../config/production-profile.json)
is the machine-readable decision and pins its evidence digests.

Q8_0 meets every recorded acceptance criterion:

- the 32,768 context and exact model/runtime identity passed direct baseline,
  tuning, evaluation, and cold/warm benchmark runs;
- all 14 Issue #11 quality and agent cases passed with score 1.000;
- Pi text streaming, thinking off/high, sequential replay, and parallel tools
  passed against the production endpoint;
- live benchmark samples peaked at 10,173 MiB of 16,368 MiB VRAM, retained at
  least 54,346 MiB `MemAvailable`, and observed zero swap-in pages; and
- the direct baseline's process resident memory was approximately 27.5 GiB,
  within the service's 48 GiB soft and 56 GiB hard bounds.

The production router values remain:

| Setting | Value |
|---|---:|
| Model | `Qwen3.5-27B-Q8_0` |
| Context | 32,768 |
| Vulkan device | `Vulkan0` (`1002:73BF`) |
| GPU layers | 20 |
| Flash attention | on |
| Batch / ubatch | 256 / 128 |
| K/V cache | q8_0 / q8_0 |
| Autoload | false |
| Pi output limit | 4,096 |

## Expected performance

The fixed live benchmark observed:

| Lifecycle | Load | TTFT | Prompt | Generation |
|---|---:|---:|---:|---:|
| Explicitly evicted cold | 8.78 s | 12.36 s | 18.86 tok/s | 2.95 tok/s |
| Warm | 3.43 s | 7.20 s | 18.89 tok/s | 2.92 tok/s |

These are target-host observations, not universal guarantees. A near-32K Pi
request took about 379 seconds on the hybrid profile; ordinary short coding
turns are much faster, but dense 27B generation remains approximately 3 tok/s.

## Alternatives

No separate faster interactive profile is selected. The tested 16-layer Q8_0
row reduced peak VRAM by roughly 1.6 GiB but was slower than 20 layers (2.24
versus 2.38 tok/s in the tuning aggregate), so it does not satisfy the reason
for an interactive profile. Flash-attention-off rows were incompatible.

Q6_K is a **contingency artifact, not a production profile**. Q8_0 did not hit
the RAM, VRAM, swap, quality, or stability fallback threshold, and Q6_K has not
received the required comparative Issue #11 evaluation. It must not be called
quality-preserving or selected until its checksum is verified and equivalent
quality and benchmark evidence is committed.

## Operation and switching

The selected settings are already in `config/router-presets.json`; no source
edit or generated INI edit is required:

```bash
systemctl --user restart local-ai-router.service
scripts/router-model.sh load --model-id qwen3.5-27b-q8_0 --timeout 180
```

In Pi, use `/llama` to load/unload and `/model
local-qwen/Qwen3.5-27B-Q8_0` to select the reviewed metadata entry. Autoload
stays disabled so restart and profile changes remain explicit.

If future evidence activates Q6_K, switching likewise uses `/llama` or the
manifest ID with `router-model.sh`; model files are never renamed or deleted
and no source edit is needed. First update the profile decision and evidence,
then use:

```bash
scripts/router-model.sh unload --model-id qwen3.5-27b-q8_0 --timeout 180
scripts/router-model.sh load --model-id qwen3.5-27b-q6_k --timeout 180
```

The second command is intentionally expected to fail today when the optional
artifact is absent. That is safer than silently downgrading quality.

Validate the tracked decision and its cross-file settings at any time:

```bash
scripts/validate-production-profile.py
```
