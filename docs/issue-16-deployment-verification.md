# Issue #16: end-to-end deployment verification

`scripts/verify-deployment.py` is the final readiness command for a clean
deployment completed with [`operator-guide.md`](operator-guide.md). It performs
real inference and leaves the service active with the verified production
model loaded only after every gate passes.

## Run

```bash
scripts/verify-deployment.py --real --timeout 1200 \
  --output results/deployment-verification.json
```

`--real` is mandatory. The overall child timeout is bounded to 600–1,800
seconds. Binary probes, Vulkan, systemd, health, journal, model lifecycle, API,
and Pi subprocesses each have tighter explicit bounds. A failed check exits
nonzero, prints its named remediation without child transcripts or secrets,
and best-effort unloads a model loaded by the verifier.

The verifier checks, in order:

1. cross-file production profile/evidence consistency and at least 32,768
   configured context;
2. all three executable links resolve into `llama.cpp-b10446`, with server and
   CLI independently reporting commit `adb55e5`;
3. Vulkan and runner-owned DRM identify RX 6900 XT PCI `1002:73BF`;
4. Q8_0 has the exact 28,595,763,104-byte size and pinned SHA-256;
5. the installed user service starts within its timeout and the localhost router becomes healthy;
6. all 11 API lifecycle checks pass, including load failure recovery,
   streaming, cancellation, near-32K context, restart, and no autoload;
7. the checksum-gated production model loads;
8. all five Pi checks pass, including thinking modes and sequential/parallel
   tool calls; and
9. final health, loaded state, RAM, VRAM, swap-in, sampling, and OOM journal
   policy pass.

Only bounded sanitized JSON is published. The report contains runtime and model
revisions, check durations, context, final service/model state, and resource
usage. It contains no prompts, raw model output, environment, credentials, or
raw journal entries.

## Verified target-host result

The committed
[`issue-16-deployment-verification-result.json`](issue-16-deployment-verification-result.json)
records a fresh **667.568-second** passing run:

- 9/9 top-level gates passed;
- 11/11 router/API and 5/5 Pi checks passed;
- context: 32,768;
- minimum `MemAvailable`: 52,895 MiB;
- peak VRAM: 10,341 / 16,368 MiB;
- swap-in delta: 0 pages across 668 samples;
- no OOM journal markers; and
- final state: service active, pinned Q8_0 loaded.

The result proves readiness on the named target host, not performance on an
arbitrary machine. See the operator guide for deployment order, expected
performance, rollback, and symptom-specific remediation.
