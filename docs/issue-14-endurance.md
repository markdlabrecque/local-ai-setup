# Issue #14: endurance and failure recovery

## Workload and result

The target-host run in
[`issue-14-endurance-result.json`](issue-14-endurance-result.json) lasted
**792.514 seconds (13 minutes 12.514 seconds)** against the selected Q8_0
profile. `scripts/run-endurance.py` composed the real Issue #9 and #10 runners
rather than substituting synthetic endpoint output.

The sustained workload included:

- three checksum-gated model loads and at least three unloads, with SHA-256
  verification before and after the run;
- one 30,000-word near-32K request, streamed cancellation, failed-model-load
  recovery, and a user-service restart/no-autoload recovery;
- two complete Pi integration loops (10 fresh Pi processes and eight verified
  sequential/parallel tool calls), which exercised tool-result replay and Pi
  reconnects repeatedly;
- two accidental simultaneous chat requests; both completed with their own
  markers in 3.753 seconds;
- inference while a separate bounded process held and touched 2,048 MiB of
  anonymous host memory; and
- continuous runner-owned `/proc` and RX 6900 XT DRM sampling plus a one-second
  desktop heartbeat command.

All nine top-level endurance phases passed. The full child suites contributed
11 router/API checks and 10 Pi checks. The router was healthy after the
intentional missing-model failure, cancellation, model cycles, and systemd
restart. The final state was healthy with Q8_0 explicitly unloaded.

## Resource and desktop safety

Across 791 samples:

| Observation | Result | Policy |
|---|---:|---:|
| Minimum `MemAvailable` | 50,087 MiB | at least 8,192 MiB |
| Peak RX 6900 XT VRAM | 10,341 / 16,368 MiB | at least 1,024 MiB free |
| Swap-in delta | 0 pages | 0 |
| Maximum desktop heartbeat | 2.136 ms | below 5,000 ms |
| OOM journal markers | none | none |

No process ran as root, no OOM or runaway swap occurred, and the service's
48 GiB `MemoryHigh`, 56 GiB `MemoryMax`, and 8 GiB `MemorySwapMax` limits
remained in force. The heartbeat, large RAM/VRAM margins, absence of OOM
markers, and continued interactive terminal operation provide the desktop
usability evidence; this does not claim that an arbitrary larger pressure load
is safe.

## Known limits

- **Dense-model speed is the practical endurance limit.** The near-boundary
  request took about 379 seconds. Long-context work needs a 600-second request
  bound even though ordinary short turns finish in seconds.
- **Concurrency was validated at two requests only.** Both completed, but the
  profile is sized for one active coding session. More clients can queue,
  increase latency, or exceed the fixed memory margins and are unsupported.
- **The memory-pressure result is bounded, not an OOM search.** It proves an
  additional 2 GiB allocation while inferring; deliberately exhausting the
  desktop's 60 GiB would be destructive and was not attempted.
- **Autoload stays disabled.** After restart Pi must reconnect, then the
  operator explicitly reloads Q8_0. This is expected recovery, not an outage.
- **A missing model is actionable.** llama.cpp returns a failed/not-loaded
  model status, `router-model.sh` exits nonzero with the model operation, and
  `/health` remains available. Service diagnostics use:
  `journalctl --user -u local-ai-router.service`.
- **Cancellation is cooperative.** Closing a stream frees the request and the
  next health/completion check succeeds; clients must use bounded timeouts and
  close abandoned responses.

## Reproduction

Start the installed service. The runner accepts an initially loaded or unloaded
Q8_0 model and normalizes to the required lifecycle:

```bash
scripts/run-endurance.py --real --pi-iterations 2 --pressure-mib 2048 \
  --timeout 1200 --result results/issue-14-endurance.json
```

`--real` is mandatory. Pi iterations are bounded to 1–10, memory pressure to
256–4,096 MiB, command timeout to 300–1,800 seconds, captures to 8 MiB, and the
pressure process group is terminated in cleanup. Failure exits nonzero and a
best-effort checksum-gated unload runs before exit. The script never removes,
renames, or writes a GGUF.
