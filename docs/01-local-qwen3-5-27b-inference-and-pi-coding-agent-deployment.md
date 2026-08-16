# Workplan: Local Qwen3.5-27B inference and Pi coding-agent deployment

> System of record: **Filesystem**

## Objective

Deploy the official dense Qwen/Qwen3.5-27B model locally on the RX 6900 XT (16 GiB VRAM), Ryzen 7 9800X3D, and 60 GiB RAM, prioritizing output quality and a minimum 32K context window. Use a current Vulkan-enabled llama.cpp build and its router because Pi 0.84.1 supports it natively. Start with an official-model GGUF Q8_0 quantization (for example unsloth/Qwen3.5-27B-GGUF or bartowski/Qwen_Qwen3.5-27B-GGUF after verifying provenance, tokenizer/chat template, and checksums); retain Q6_K as a tested fallback if Q8_0 plus 32K context cannot operate reliably within system memory. Offload as many layers as fit in VRAM while leaving desktop headroom, place remaining weights and KV cache in RAM, bind the API to localhost, and validate general inference, reasoning controls, streaming, and multi-turn tool calling through Pi. The work includes reproducible scripts, a user-level systemd service, Pi configuration, health checks, benchmarks, tuning records, and operational documentation.

## GitHub Issue Tracker

Implementation is tracked in [GitHub issues](https://github.com/markdlabrecque/local-ai-setup/issues):

- [#1](https://github.com/markdlabrecque/local-ai-setup/issues/1) — Vulkan-enabled llama.cpp setup script (completed)
- [#2](https://github.com/markdlabrecque/local-ai-setup/issues/2) — Hardware baseline and acceptance criteria
- [#3](https://github.com/markdlabrecque/local-ai-setup/issues/3) — Model artifacts and memory budget
- [#4](https://github.com/markdlabrecque/local-ai-setup/issues/4) — Deterministic model downloads
- [#5](https://github.com/markdlabrecque/local-ai-setup/issues/5) — Direct llama.cpp baseline
- [#6](https://github.com/markdlabrecque/local-ai-setup/issues/6) — Hybrid inference tuning
- [#7](https://github.com/markdlabrecque/local-ai-setup/issues/7) — Router configuration and presets
- [#8](https://github.com/markdlabrecque/local-ai-setup/issues/8) — systemd user service
- [#9](https://github.com/markdlabrecque/local-ai-setup/issues/9) — Health checks and API smoke tests
- [#10](https://github.com/markdlabrecque/local-ai-setup/issues/10) — Pi integration
- [#11](https://github.com/markdlabrecque/local-ai-setup/issues/11) — Evaluation suite
- [#12](https://github.com/markdlabrecque/local-ai-setup/issues/12) — Configuration benchmarks
- [#13](https://github.com/markdlabrecque/local-ai-setup/issues/13) — Production profiles
- [#14](https://github.com/markdlabrecque/local-ai-setup/issues/14) — Endurance and recovery testing
- [#15](https://github.com/markdlabrecque/local-ai-setup/issues/15) — Operator guide
- [#16](https://github.com/markdlabrecque/local-ai-setup/issues/16) — End-to-end verification

## Action Plan

1. [ ] Record a reproducible hardware and software baseline: RX 6900 XT identity and 16 GiB VRAM, RADV/Mesa and Vulkan versions, CPU/RAM/swap/disk, kernel, Pi version, and idle desktop VRAM usage; reserve explicit RAM, VRAM, and disk safety margins before testing.
2. [ ] Define acceptance criteria in project documentation: successful 32K-context startup, stable streamed generation, valid parallel/sequential tool calls in Pi, no OOM or swap thrashing during the target workload, acceptable response quality on a fixed prompt suite, and measured prompt-processing/generation throughput and time-to-first-token.
3. [x] Select and pin the official dense `Qwen/Qwen3.5-27B` base/instruct release and a trusted GGUF conversion. Prefer Q8_0 as the highest practical quantized quality tier on this 60 GiB system, document repository revision and SHA-256 checksums, and also identify a Q6_K artifact as the fallback rather than using an MoE, abliterated, distilled, or otherwise modified derivative. The single source of truth is [`config/models.json`](../config/models.json); it records the immutable Hugging Face revision, exact filenames, byte sizes, LFS SHA-256 object IDs, download URLs, and lineage.
4. [x] Estimate and document memory requirements for Q8_0 weights, llama.cpp runtime overhead, a 32K KV cache, and the OS/desktop. Verify the estimate against available RAM and VRAM; reject BF16 as the default if it leaves inadequate runtime headroom, and establish explicit thresholds for falling back from Q8_0 to Q6_K or reducing GPU layers without dropping below 32K context. The executable calculation is [`config/memory-budget.json`](../config/memory-budget.json); it is the policy source and must be consumed rather than duplicated by deployment scripts.
5. [x] Create a reproducible installer/build script that obtains a pinned current llama.cpp release or source revision with Vulkan enabled, verifies the resulting `llama-server` backend and GPU discovery, and avoids introducing ROCm unless Vulkan benchmarking demonstrates a concrete deficiency. Record all package and build dependencies. ([#1](https://github.com/markdlabrecque/local-ai-setup/issues/1))
6. [ ] Create a deterministic model-download script and model directory layout compatible with llama.cpp router mode. Support resumable downloads, checksum verification, shard placement, configurable model/cache paths, and sufficient free-space checks before downloading Q8_0 and optional Q6_K artifacts.
7. [ ] Establish a direct llama.cpp baseline outside Pi using `llama-cli` or single-model `llama-server`: confirm the official chat template, Jinja support, thinking/non-thinking behavior, stop tokens, 32K context allocation, streaming, and coherent responses before adding service or Pi integration.
8. [ ] Tune hybrid GPU/CPU inference systematically. Begin with Vulkan, 32K context, flash attention where supported, conservative batch/ubatch values, and enough GPU layers to leave desktop VRAM headroom; measure several layer-offload and cache-type configurations, monitor VRAM/RAM/swap, and save the best stable quality-preserving settings rather than optimizing only peak tokens per second.
9. [x] Configure llama.cpp in router mode on `127.0.0.1:8080` with `--models-dir`, `--no-models-autoload`, `--jinja`, the tuned GPU offload settings, and a 32768-token context. Add per-model presets where necessary so Q8_0 and Q6_K have explicit context, cache, batch, and offload settings. See [`docs/issue-7-router.md`](issue-7-router.md).
10. [x] Add a hardened user-level systemd service for the llama.cpp router with restart policy, controlled environment/config files, log visibility through `journalctl --user`, startup ordering, resource limits appropriate to the workload, and localhost-only exposure. Include enable/start/stop/restart/status commands and ensure model files are never deleted by service lifecycle operations. See [`docs/issue-8-systemd.md`](issue-8-systemd.md).
11. [x] Add health and smoke-test scripts for `/health`, `/models`, model load/unload, OpenAI-compatible streamed chat completions, 32K-context boundary behavior, cancellation, and clean recovery after server restart or failed model loading. See [`docs/issue-9-router-smoke.md`](issue-9-router-smoke.md).
12. [x] Integrate the server with Pi using its native llama.cpp provider: document `/login llama.cpp` with `http://127.0.0.1:8080` (or equivalent `LLAMA_BASE_URL`), `/llama` model loading, and `/model` selection. Keep a documented `~/.pi/agent/models.json` OpenAI-compatible fallback configuration only if native router integration lacks a required Qwen reasoning or compatibility control. See [`docs/issue-10-pi-integration.md`](issue-10-pi-integration.md).
13. [x] Set Pi model metadata and compatibility only where verified: correct model ID, 32K context, output limit, zero local cost, reasoning capability, system/developer-role handling, token field, streaming usage behavior, strict tool support, and Qwen chat-template thinking controls such as `thinkingFormat: "qwen-chat-template"` when the selected llama.cpp endpoint requires them. See [`config/pi-models.example.json`](../config/pi-models.example.json).
14. [x] Build a version-controlled evaluation suite covering general inference, instruction following, long-context retrieval near 32K, coding generation, repository navigation, patching, command execution, malformed/parallel tool-call resistance, multi-turn tool-result replay, reasoning on/off, cancellation, and context overflow/compaction behavior. Use non-destructive fixtures or a disposable Git repository for agent tests. (See [`docs/issue-11-evaluation.md`](issue-11-evaluation.md).)
15. [x] Benchmark each viable configuration with warm and cold runs. Capture model/quantization, llama.cpp revision, Vulkan device, context and output lengths, GPU layers, batch settings, KV-cache types, peak VRAM/RAM/swap, prompt tokens per second, generation tokens per second, time-to-first-token, load time, and pass/fail results from the quality and tool-calling suite. See [`docs/issue-12-benchmark-result.md`](issue-12-benchmark-result.md).
16. [ ] Choose and document the production profile: retain Q8_0 if it meets stability criteria at 32K; otherwise adopt Q6_K only after comparing quality results. Preserve a slower quality-first profile and an optional faster interactive profile if distinct settings materially improve coding-agent usability.
17. [ ] Run endurance and failure testing: repeated Pi tool-call loops, long sessions approaching context limits, unload/reload cycles, concurrent accidental requests, server cancellation, Pi reconnects, systemd restarts, and memory-pressure scenarios. Confirm that failures are bounded, logs are actionable, and the desktop remains usable.
18. [ ] Write an operator guide covering installation, model acquisition, service operation, Pi setup, profile switching, logs, upgrades and rollback, checksum/revision updates, troubleshooting OOM and malformed tool calls, benchmark reproduction, expected performance, and known limitations of running a 27B dense model with 16 GiB VRAM.
19. [ ] Add a final verification script or checklist that can rebuild or validate the deployment from a clean state, starts the service, loads the pinned model, executes API and Pi smoke tests, checks 32K context configuration, reports resource usage, and emits a concise pass/fail summary.

## Issue #3 artifact and memory policy

`config/models.json` is the machine-readable artifact manifest for future download tooling. The official source baseline revision and conversion repository revision are pinned independently, and each file checksum is the SHA-256 LFS object ID returned by the Hugging Face file metadata API. The converter-declared base-model lineage is recorded separately and does not claim an independently proven conversion-source commit; no BF16, MoE, distilled, or other derivative artifact is selected.

`config/memory-budget.json` mechanically evaluates the target context against the host RAM/VRAM baseline and a reserved operating-system allowance. It records the model architecture, full-attention KV formula, recurrent-state allowance, runtime components, and formulas deriving weights from `config/models.json`; calculated totals and margins are the acceptance values. Q8_0 and Q6_K remain under the conservative safety limit, while BF16 is explicitly rejected. The Q8_0 result has a smaller margin, so startup measurements must preserve the documented reserve; if they do not, use Q6_K or reduce offload while retaining the context target.

## Completion Checks

- [ ] Each action-plan item is complete or explicitly deferred.
- [ ] Targeted validation or tests have been run and recorded.
- [ ] Any remaining follow-up is linked to the system of record.
