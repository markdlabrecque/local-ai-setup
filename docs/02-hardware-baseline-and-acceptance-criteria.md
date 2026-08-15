# Hardware baseline and acceptance criteria

## Collecting a baseline

Run the collector from the repository root on the target host:

```bash
./scripts/collect-baseline.sh --output-dir /tmp/local-ai-baseline
```

The collector is portable Bash and read-only with respect to the host: it reads
`/proc`, `lscpu`, `lspci`, `vulkaninfo`, `df`, `uname`, and `pi` when available.
It only creates the requested output directory and a versioned
`baseline-YYYYMMDDTHHMMSSZ.txt` report. Missing optional tools are recorded as
`unavailable`, rather than causing a misleading partial failure. The report
version is included in the file so later schema changes are identifiable.

A generated report is a machine-specific data artifact, not reusable
configuration. Review it for credentials and other sensitive host details
before committing it. Prefer a temporary output directory; generated reports
are not required in the source tree.

The report must include GPU, target GPU PCI identity, target-GPU VRAM,
target-GPU idle VRAM usage, target-GPU Vulkan device/driver details (or an
explicit unavailable value), CPU, RAM, swap, disk, kernel, and Pi fields.
The DRM VRAM counters are selected from the device with the largest non-zero
VRAM heap so an integrated adapter is not mistaken for the target discrete GPU.
The values are
observations, not claims that unavailable hardware is present.

## Resource safety margins

These are portable planning constraints for the 16 GiB VRAM / 60 GiB RAM target;
replace the observed baseline values when hardware differs:

- Keep at least **1 GiB VRAM free** while the desktop is running. Configure
  offload below the measured VRAM ceiling rather than consuming the final GiB.
- Keep at least **8 GiB of minimum available RAM** for the OS, desktop,
  llama.cpp runtime, and transient allocations. Do not begin a 32K run if the
  baseline reports less than 12 GiB available RAM.
- Keep swap enabled as an emergency guard, but treat any sustained swap-in
  during inference as a failure, not as usable model capacity.
- Keep at least **20 GiB free disk space** on the model filesystem before
  downloading or converting an artifact. Do not place generated reports or
  caches in the repository unless intentionally reviewed.

## Testable acceptance thresholds

A configuration is accepted only when all applicable measurements below pass:

| Area | Pass condition |
|---|---|
| Hardware evidence | Baseline report has the required hardware/software fields, target-GPU VRAM and idle-VRAM association, and no credential-like pattern after review. |
| Context | Server starts with `--ctx-size 32768` and completes a request using a context of at least 32,000 tokens. |
| Memory safety | Peak target-GPU VRAM remains at least 1 GiB below that GPU's capacity; the minimum available RAM during the workload remains at least 8 GiB; no OOM event. |
| Swap | Swap-in remains zero during the target workload; any swap thrashing is a fail and requires a lower-memory profile. |
| Streaming | Three consecutive streamed requests finish without truncation, transport error, or server crash. |
| Tool calls | Pi completes one sequential and one parallel tool-call turn with valid arguments and returned tool results. |
| Responsiveness | Record time-to-first-token, prompt tokens/second, and generation tokens/second for the fixed benchmark; no unexplained regression greater than 20% between repeated runs. |
| Quality | Every fixed prompt-suite case meets its documented expected-behavior check; a coherent answer alone is not sufficient for tool-call cases. |

Record measurements, llama.cpp revision, model checksum, context size, GPU
layers, cache settings, and pass/fail results with each benchmark. A failed
threshold must be resolved by tuning or selecting the documented fallback; it
must not be hidden by omitting the measurement.
