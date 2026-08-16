# Issue #12 benchmark

This is the fixed, auditable benchmark for the Issue #6 Q8_0 hybrid Vulkan
candidate and the passing Issue #11 `issue-11-evaluation` quality report. It
runs exactly one **cold** (cache miss) and one **warm** (cache hit) lifecycle,
in that order, in a new process group each time. The selected GPU is the RX 6900 XT
(`Vulkan0`, PCI `1002:73BF`). The runtime is pinned to
llama.cpp `b10446` / `adb55e5`.

## Portable fake gate

The committed fake runtime is used by tests; it does not load a model or use a
GPU. It is safe to run:

```text
python3 -m unittest tests.test_issue12_review_regressions
```

The fake is not live evidence and must never be used for a real evaluation
report.

## Safe live command (do not run in the child)

A verified Q8_0 model file and the pinned executable are prerequisites. Verify
the model SHA-256 is
`6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450`, verify
that the Issue #11 report is a real passing report with actual model/runtime
provenance (not `synthetic_fixture`), and map the RX 6900 XT PCI device
`1002:73BF` to `Vulkan0` before running:

```bash
scripts/run-benchmark.sh --config config/benchmark.json \
  --tuning-result docs/issue-6-hybrid-vulkan-tuning-result.json \
  --evaluation-report results/evaluation.json \
  --model "$HOME/.local/share/local-ai/models/Qwen3.5-27B-Q8_0.gguf" \
  --llama-cli "$HOME/.local/share/local-ai/runtime/llama-cli" \
  --output results/issue-12-benchmark.json
```

This is a real Q8_0 benchmark and is intentionally not run by the child. The
command has a default of **120 seconds per lifecycle**; the hard
limit is 300 seconds. The model load plus two bounded runs may take several
minutes. A real evaluation report cannot be produced until the pinned model,
CLI, RX 6900 XT mapping, and passing non-synthetic Issue #11 report are all
available.

## Fixed inputs and observations

The selected Issue #6 row must be `status: pass` with complete quality and
live evidence, including its tuple identity, three attempts, and PCI identity.
The evaluator report is checked against the Issue #11 schema and manifest, all
14 cases must pass, all six provenance hashes must be present, and the actual
model and runtime provenance must match this benchmark. Reports marked
synthetic or unsanitized are rejected.

The b10446 command uses supported flags only: notably it does **not** send the
unsupported `--prompt-tokens` flag. Prompt and output lengths are fixed at 16
and 8 tokens; their prompt tokens and generation tokens counts must be observed in distinct prompt-eval and eval
lines. `load time`, prompt evaluation, generation evaluation, and TTFT are
separate observations. **TTFT** is the timestamp of the first actual stdout
byte, never prompt evaluation.

Each lifecycle passes `--no-warmup`. Cold and warm cache preparation/evidence
is recorded in order, with a fresh process for each. Hardware evidence is a
continuous set of live runtime samples: every sample must identify PCI
`1002:73BF`, and the runner fails closed if samples, RX 6900 XT identity, RAM,
VRAM, or swap observations are absent or unsafe. Raw logs are never retained
in the result; captures are bounded and sanitized.

The result schema is `schemas/benchmark-result.schema.json`. Provenance hashes
cover the config, Issue #6 result, Issue #11 report, and result itself.
`--resume` revalidates the complete artifact, deep candidate/runtime/model
identity, ordered passing runs, and artifact hash before starting `llama-cli`.
All process groups are terminated with the configured grace period on timeout,
normal cleanup, and TERM/INT exits. No raw logs, secrets, or paths enter the
sanitized artifact.
