# Local Qwen3.5-27B operator guide

This is the ordered deployment, operation, upgrade, rollback, and
troubleshooting guide for the selected Q8_0 profile on the RX 6900 XT / 60 GiB
host. Run commands from the repository root unless stated otherwise.

## 1. Requirements and safety

The validated target is Linux with systemd user services, an AMD RX 6900 XT
(`1002:73BF`, 16 GiB), approximately 60 GiB RAM, Vulkan/RADV, Git, Python 3,
CMake, a C/C++ toolchain, curl, and at least 40 GiB free for source, build, and
Q8_0. The setup script can install tracked Arch packages with sudo; inference,
model download, and the service run as the user.

The fixed policy requires at least 8 GiB `MemAvailable`, 1 GiB free target-GPU
VRAM, zero swap-in during inference, and 32,768 context. Do not solve pressure
by silently reducing context. The service binds only `127.0.0.1`.

Clone and inspect the pinned inputs:

```bash
git clone https://github.com/markdlabrecque/local-ai-setup.git
cd local-ai-setup
git status --short
cat config/versions.env
python3 -m json.tool config/models.json >/dev/null
scripts/validate-production-profile.py
```

Never commit `HF_TOKEN`, API keys, `~/.pi/agent/auth.json`, model files,
runtime logs, or generated results containing prompts. Supply secrets through
the environment or Pi `/login` only.

## 2. Record the new-machine baseline

```bash
scripts/collect-baseline.sh --output-dir results/baseline
```

Review `docs/02-hardware-baseline-and-acceptance-criteria.md`. Stop if Vulkan
does not expose the RX 6900 XT, the target PCI ID differs, available RAM is
below the policy, or disk cannot hold the 28.6 GB artifact plus build data.
Useful independent checks are:

```bash
vulkaninfo --summary
free -h
swapon --show
findmnt -T ~/.local/share
```

## 3. Build the pinned llama.cpp runtime

Preview, then build `b10446` / `adb55e5` with Vulkan:

```bash
scripts/setup.sh --dry-run
scripts/setup.sh
llama-server --version
readlink -f ~/.local/bin/llama-server
```

The versioned runtime is installed under
`~/.local/share/local-ai/runtime/llama.cpp-b10446`; user-local executable
symlinks are placed in `~/.local/bin`. Add that directory to `PATH` if needed.
The build does not install ROCm.

## 4. Acquire and verify Q8_0

The downloader is resumable, holds an artifact lock, enforces free space, and
verifies the manifest filename, size, revision URL, and SHA-256 before atomic
publication:

```bash
python3 scripts/download-model.py --quantization Q8_0
sha256sum ~/.local/share/local-ai/models/Qwen3.5-27B-Q8_0.gguf
```

Expected SHA-256:

```text
6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450
```

For gated/private mirrors, export `HF_TOKEN` only in the invoking shell. The
official pinned artifact is public and needs no committed credential. Q6_K is
optional contingency data, not a selected profile; do not download or select
it merely for speed.

## 5. Validate direct inference and profile consistency

The tracked live evidence already selects 20 Vulkan layers, flash attention,
batch/ubatch 256/128, and q8_0 K/V. On a new target host, run the bounded direct
baseline before installing the service:

```bash
scripts/run-direct-baseline.sh \
  --model ~/.local/share/local-ai/models/Qwen3.5-27B-Q8_0.gguf \
  --sha256 6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450 \
  --llama-cli ~/.local/share/local-ai/runtime/llama.cpp-b10446/bin/llama-cli \
  --gpu-layers 20 --context 32768 --timeout 1800 \
  --expected-completion LOCAL_AI_BASELINE_OK \
  --prompt 'Respond with exactly: LOCAL_AI_BASELINE_OK' \
  --output results/direct-baseline.json
scripts/validate-production-profile.py
```

Do not retune casually. If hardware or runtime changes, repeat the documented
Issue #6, #11, and #12 evidence flow before changing production defaults.

## 6. Install and operate the user service

```bash
scripts/install-router-service.sh --enable --start
systemctl --user status local-ai-router.service --no-pager
curl -fsS http://127.0.0.1:8080/health
curl -fsS http://127.0.0.1:8080/models | python3 -m json.tool
```

The unit starts the router but never autoloads a model. Load and unload only by
manifest ID; both operations checksum the GGUF and never delete it:

```bash
scripts/router-model.sh load --model-id qwen3.5-27b-q8_0 --timeout 180
scripts/router-model.sh unload --model-id qwen3.5-27b-q8_0 --timeout 180
```

Routine commands:

```bash
systemctl --user start local-ai-router.service
systemctl --user stop local-ai-router.service
systemctl --user restart local-ai-router.service
systemctl --user status local-ai-router.service --no-pager
journalctl --user -u local-ai-router.service -f
journalctl --user -u local-ai-router.service --since today
```

A restart intentionally returns models to unloaded state. Reload explicitly.
To remove only the managed unit, environment file, wrapper, and manifest:

```bash
scripts/uninstall-router-service.sh
```

Uninstall preserves GGUFs, versioned runtimes, configuration, and Pi data.

## 7. Install and configure Pi

Install the validated Pi version without npm lifecycle scripts:

```bash
npm install -g --ignore-scripts @earendil-works/pi-coding-agent@0.84.2
pi --version
```

Configure native router lifecycle in an interactive Pi session:

```text
/login llama.cpp
```

Use `http://127.0.0.1:8080`, leave the key empty, then use `/llama` to load or
unload. `LLAMA_BASE_URL=http://127.0.0.1:8080 pi` is the per-process URL
equivalent.

Native discovery cannot currently express the verified Qwen thinking metadata
or conservative output budget. Install the reviewed fallback only if no
existing models file is present:

```bash
install -d -m 700 ~/.pi/agent
install -m 600 config/pi-models.example.json ~/.pi/agent/models.json
```

If `~/.pi/agent/models.json` exists, merge its
`providers.local-qwen` object—do not overwrite other providers. Load Q8_0 via
`/llama`, then select `/model local-qwen/Qwen3.5-27B-Q8_0`. Start a project:

```bash
cd /path/to/project
pi --provider local-qwen --model Qwen3.5-27B-Q8_0 --thinking high
```

Use `/settings` or `--thinking off` for direct responses. Pi tools can modify
the current project; use version control and project instructions. Validate
the installed path with:

```bash
scripts/pi-integration-smoke.py --real --timeout 300 \
  --result results/pi-integration.json
```

## 8. Health, smoke, benchmark, and endurance checks

After completing the clean-machine path, run the single final readiness gate:

```bash
scripts/verify-deployment.py --real --timeout 1200 \
  --output results/deployment-verification.json
```

It exits nonzero with a named remediation on failure. On success it leaves the
service active and pinned Q8_0 loaded for Pi. See
[`issue-16-deployment-verification.md`](issue-16-deployment-verification.md).

Use the least expensive component check when the full gate is unnecessary:

```bash
# Router only; no load
scripts/router-smoke.sh --real

# Full lifecycle, stream, cancellation, near-context, restart
scripts/router-api-smoke.py --real --timeout 600 \
  --result results/router-api-smoke.json

# Full sustained recovery/resource run
scripts/run-endurance.py --real --pi-iterations 2 --pressure-mib 2048 \
  --timeout 1200 --result results/endurance.json
```

The fixed benchmark needs a current non-synthetic passing Issue #11 report and
explicit model-file cache eviction. With Q8_0 loaded, generate the report in a
clean disposable workspace (the runner copies it and never edits the source):

```bash
mkdir -p results/evaluation-workspace
printf 'local evaluation fixture\n' > results/evaluation-workspace/README.md
scripts/run-evaluation.sh \
  --endpoint http://127.0.0.1:8080 \
  --cases tests/fixtures/evaluation_cases.json \
  --workspace results/evaluation-workspace \
  --artifacts results/evaluation.json \
  --model Qwen3.5-27B-Q8_0 \
  --model-sha256 6b0a101b0a86697fe11eabcc1a7db72699a9f3d4b18b6a1ac75ea3fb2c26c450 \
  --runtime-ref b10446 --runtime-commit adb55e5
```

Unload the router model so RAM/cache state is controlled, then follow
`docs/issue-12-benchmark.md`. Never run the full benchmark with sudo:

```bash
scripts/router-model.sh unload --model-id qwen3.5-27b-q8_0 --timeout 180
scripts/run-benchmark.sh --config config/benchmark.json \
  --tuning-result docs/issue-6-hybrid-vulkan-tuning-result.json \
  --evaluation-report results/evaluation.json \
  --model ~/.local/share/local-ai/models/Qwen3.5-27B-Q8_0.gguf \
  --llama-cli ~/.local/share/local-ai/runtime/llama.cpp-b10446/bin/llama-cli \
  --output results/benchmark.json --evict-cache
```

## 9. Profile selection and switching

`config/production-profile.json` selects only Q8_0. Expected short fixed-run
performance is approximately 18.9 prompt tok/s, 2.9 generation tok/s, 8.8 s
cold load, 3.4 s warm load, 12.4 s cold TTFT, and 7.2 s warm TTFT. Peak live
VRAM was about 10.2 GiB; direct resident RAM was about 27.5 GiB. A near-32K
turn took about 379 seconds.

No faster interactive profile was justified. Two concurrent short requests
passed, but one active coding session is the supported operating target.
Q6_K may be selected only after the quality comparison listed in
`docs/issue-13-production-profile.md`. Once approved and downloaded, switching
uses `/llama` or `router-model.sh`; no source or generated INI edit is needed.

## 10. Pinned upgrade procedure

Treat an upgrade as a new evidence-producing change, not `git pull` followed by
an unattended restart.

1. Stop and unload cleanly; record current commit and resolved runtime links:
   ```bash
   scripts/router-model.sh unload --model-id qwen3.5-27b-q8_0 --timeout 180 || true
   systemctl --user stop local-ai-router.service
   git rev-parse HEAD > ~/local-ai-known-good.git-revision
   readlink -f ~/.local/bin/llama-server > ~/local-ai-known-good.runtime
   ```
2. Create a branch. Update `config/versions.env` to an immutable llama.cpp ref
   and commit, and update model manifest revisions/checksums only from verified
   upstream metadata. Never replace a checksum merely to match a download.
3. Run `scripts/setup.sh`. It installs a new versioned runtime and leaves other
   version directories and all GGUFs intact.
4. Run direct baseline, tuning if flags/settings changed, the complete Issue
   #11 evaluation, cold/warm benchmark, router/API smoke, Pi smoke, and
   endurance checks. Reject the upgrade on quality, tool, memory, swap,
   context, or recovery regression.
5. Reinstall the service so its managed wrapper and hashes match the reviewed
   checkout, then start and explicitly load:
   ```bash
   scripts/install-router-service.sh --enable --start
   scripts/router-model.sh load --model-id qwen3.5-27b-q8_0 --timeout 180
   ```
6. Commit only sanitized bounded evidence; do not commit GGUFs, secrets, or raw
   logs.

## 11. Rollback

Stop the service first. Check out the recorded known-good Git revision and
repoint all three user-local links to the retained versioned runtime:

```bash
systemctl --user stop local-ai-router.service
git checkout "$(cat ~/local-ai-known-good.git-revision)"
old_runtime=$(dirname "$(cat ~/local-ai-known-good.runtime)")
for name in llama-server llama-cli llama-bench; do
  test -x "$old_runtime/$name" || exit 1
  ln -sfn "$old_runtime/$name" "$HOME/.local/bin/$name"
done
scripts/install-router-service.sh --enable --start
scripts/router-model.sh load --model-id qwen3.5-27b-q8_0 --timeout 180
```

Verify `llama-server --version`, `/health`, `/models`, and a Pi text/tool smoke.
Do not delete the failed runtime until diagnosis is complete. Model rollback is
analogous: keep separately named, checksum-pinned artifacts, update the
reviewed manifest/profile first, and never overwrite the known-good GGUF.

## 12. Troubleshooting

### Router will not start

```bash
systemctl --user status local-ai-router.service --no-pager
journalctl --user -u local-ai-router.service -n 200 --no-pager
ss -ltnp | grep ':8080'
~/.local/bin/llama-server --version
```

Stop the unexpected port owner; do not change the bind to a public address.
A nonempty `/etc/llama.cpp/config.ini` is rejected because b10446 cannot disable
that implicit config. Correct or empty the administrator-owned file after
review. Re-run the installer if managed-file hashes no longer match; it refuses
unowned replacements rather than overwriting them.

### Model load fails or model is missing

```bash
sha256sum ~/.local/share/local-ai/models/Qwen3.5-27B-Q8_0.gguf
curl -fsS http://127.0.0.1:8080/models | python3 -m json.tool
journalctl --user -u local-ai-router.service -n 200 --no-pager
```

A checksum mismatch requires a fresh manifest-pinned download, not checksum
editing. An unloaded model is normal after restart. A failed status identifies
child startup failure; unload, check Vulkan/memory/logs, and load again.

### Vulkan device missing or wrong

```bash
vulkaninfo --summary
ls -l /dev/dri
cat /sys/class/drm/card*/device/{vendor,device} 2>/dev/null
```

The selected adapter must be AMD `1002:73BF` and appear as `Vulkan0` to the
pinned runtime. Fix RADV/Mesa packages, device permissions, or competing Vulkan
ICD environment variables. Do not change the preset to a different adapter
without repeating tuning and safety evidence.

### OOM, memory limit, or swapping

```bash
systemctl --user show local-ai-router.service \
  -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax -p MemorySwapMax
free -h
vmstat 1
journalctl --user -u local-ai-router.service -n 300 --no-pager | grep -Ei 'oom|memory|killed'
```

Cancel requests, unload Q8_0, and stop other large workloads. Preserve 32K and
the desktop margins. Do not raise the 56 GiB hard cap casually. Q6_K is not an
automatic remedy: verify/download it and run the comparative quality flow
before changing production. Repeated swap-in is a failed profile, not an
acceptable slow mode.

### Pi cannot discover or select the model

Confirm service health, load Q8_0 with `/llama` or `router-model.sh`, verify
`~/.pi/agent/models.json` contains the reviewed `local-qwen` provider, then:

```bash
pi --list-models Qwen
```

Use provider/model `local-qwen/Qwen3.5-27B-Q8_0`. Re-run `/login llama.cpp` if
`/llama` cannot connect. Do not put a local API placeholder into a public
router configuration; this deployment is localhost-only.

### Malformed, missing, or repeated tool calls

Confirm the model is selected through `local-qwen`, the router has `--jinja`,
and the config retains `thinkingFormat: "qwen-chat-template"`, non-strict
schemas, `system` role, and `max_tokens`. Restarting Pi clears a damaged
conversation replay. Reproduce in a disposable workspace with
`scripts/pi-integration-smoke.py --real`; inspect JSON mode rather than raw
terminal formatting. Do not enable strict tools, developer role, or a different
thinking flag without endpoint evidence.

### Slow responses, cancellation, or apparent hangs

Generation near 3 tok/s and a 379-second near-context turn are expected. Use a
600-second bound for near-32K work, but cancel abandoned streams. Check service
and GPU activity:

```bash
systemctl --user status local-ai-router.service --no-pager
watch -n1 'cat /sys/class/drm/card*/device/mem_info_vram_used 2>/dev/null'
```

If a cancelled client leaves no recovery, run the Issue #9 smoke. A service
restart kills the full control group and returns to unloaded state; explicitly
load again.

## 13. Data preservation and cleanup

Back up only small tracked/configuration state and record checksums; GGUFs can
be re-fetched from immutable URLs. Service uninstall and ordinary profile
switching never remove models. Before manually deleting any runtime or model,
stop the service, resolve symlinks, verify the path is under the intended
user-local directory, and retain the current known-good version until its
replacement has passed final verification.
