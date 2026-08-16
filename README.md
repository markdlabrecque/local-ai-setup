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

## Security

Do not store sudo passwords, API keys, or model-access tokens in this repository. Model files and local `.env` files are ignored by Git.

## Plan

See [`docs/01-local-qwen3-5-27b-inference-and-pi-coding-agent-deployment.md`](docs/01-local-qwen3-5-27b-inference-and-pi-coding-agent-deployment.md).
