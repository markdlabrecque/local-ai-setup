# Local AI deployment

Reproducible deployment assets for running the dense Qwen3.5-27B model through a Vulkan-enabled llama.cpp server and the Pi agent harness.

## Bootstrap llama.cpp

The setup script installs missing build dependencies, checks out the pinned llama.cpp release from [`config/versions.env`](config/versions.env), builds it with Vulkan, and installs it under the current user's home directory.

```bash
./scripts/setup.sh
```

Sudo is used only when distribution packages are missing. The script calls `sudo -v` when authentication is needed, allowing sudo to prompt through the terminal without putting a password in the script.

Preview operations without changing the machine:

```bash
./scripts/setup.sh --dry-run
```

Other useful invocations:

```bash
# Dependencies are already managed externally
./scripts/setup.sh --skip-packages

# Discard and rebuild the pinned source tree
./scripts/setup.sh --force-rebuild

# Limit compilation concurrency
./scripts/setup.sh --jobs 8
```

Default paths:

| Purpose | Path |
|---|---|
| Source checkout | `~/.cache/local-ai/src` |
| Versioned runtime | `~/.local/share/local-ai/runtime` |
| Model storage | `~/.local/share/local-ai/models` |
| Configuration | `~/.config/local-ai` |
| Executable links | `~/.local/bin` |

All paths can be overridden with the environment variables shown by `./scripts/setup.sh --help`.

The script currently supports dependency installation on Arch Linux and Debian/Ubuntu. On another distribution, install the prerequisites yourself and use `--skip-packages`.

## Direct baseline

The verified Issue #5 Vulkan/Q8_0 baseline and reproducible command are documented in [`docs/issue-5-direct-baseline-result.md`](docs/issue-5-direct-baseline-result.md).

## Hybrid Vulkan tuning

The resumable Q8_0 tuning harness, pinned matrix, and sanitized RX 6900 XT result are documented in [`docs/issue-6-hybrid-vulkan-tuning-result.md`](docs/issue-6-hybrid-vulkan-tuning-result.md).

## Router mode (Issue #7)

The pinned localhost router launcher, explicit Q8_0/Q6_K presets, checksum-gated model lifecycle helper, and opt-in smoke command are documented in [`docs/issue-7-router.md`](docs/issue-7-router.md). The portable contract is [`config/router.json`](config/router.json) plus [`config/router-presets.json`](config/router-presets.json).

```bash
scripts/run-router.sh --foreground
scripts/router-model.sh load --model-id qwen3.5-27b-q8_0
# Optional, against an already-running real router:
scripts/router-smoke.sh --real
```

The launcher binds only to `127.0.0.1:8080`, passes `--no-models-autoload`, isolates router configuration, and cleans its complete server process group with a finite TERM/KILL grace period. It generates private presets only for present GGUF artifacts and never deletes model files. The lifecycle helper shares the downloader lock, verifies identity before and after asynchronous load/unload, sends extensionless b10446 IDs, and requires the exact success response. The portable test uses a fake server; do not start the real 28 GB model for that gate.

## Issue #11 evaluation

Run the portable evaluation gate with the deterministic OpenAI-compatible
fixture; no large model is needed:

```bash
python3 tests/fixtures/fake_openai_endpoint.py --port 8089
scripts/run-evaluation.sh --endpoint http://127.0.0.1:8089 \
  --cases tests/fixtures/evaluation_cases.json \
  --workspace /path/to/disposable-repo --artifacts results/evaluation.json
```

The runner uses a temporary workspace copy, an allowlisted command fixture,
bounded sanitized artifacts, and the report contract in
[`schemas/evaluation-report.schema.json`](schemas/evaluation-report.schema.json).
See [`docs/issue-11-evaluation.md`](docs/issue-11-evaluation.md).

## Issue #12 benchmark

The fixed cold/warm benchmark consumes the Issue #6 stable candidate and the
passing Issue #11 report. It records streaming TTFT separately from prompt
evaluation, bounds process groups/captures, and supports tamper-checked
`--resume`. Use the portable gate and safe live command in
[`docs/issue-12-benchmark.md`](docs/issue-12-benchmark.md). The completed live
measurements and machine-readable evidence are in
[`docs/issue-12-benchmark-result.md`](docs/issue-12-benchmark-result.md) and
[`docs/issue-12-benchmark-result.json`](docs/issue-12-benchmark-result.json).

## Security

Do not store sudo passwords, API keys, or model-access tokens in this repository. Model files and local `.env` files are ignored by Git.

## Plan

See [`docs/01-local-qwen3-5-27b-inference-and-pi-coding-agent-deployment.md`](docs/01-local-qwen3-5-27b-inference-and-pi-coding-agent-deployment.md).
