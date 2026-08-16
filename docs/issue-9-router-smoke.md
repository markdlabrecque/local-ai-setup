# Issue #9 router lifecycle and API smoke test

`scripts/router-api-smoke.py` performs the opt-in target-host validation that is
intentionally excluded from portable gates because it loads the 28 GB Q8_0
model. It only accepts plain HTTP on `127.0.0.1`, never deletes or modifies a
model file, and uses the checksum-gated `router-model.sh` helper for every real
load and unload.

## Prerequisites

Install and start the hardened user service, then confirm the pinned model and
runtime are present:

```bash
scripts/install-router-service.sh --enable --start
systemctl --user status local-ai-router.service
```

## Run

```bash
scripts/router-api-smoke.py --real --timeout 600 \
  --result results/issue-9-router-smoke.json
```

The explicit acknowledgement is mandatory. Each HTTP operation and subprocess
has a bound, streamed requests have a total deadline and bounded event size,
and any failed check makes the process exit nonzero. The script:

1. validates `/health` and the `/models` envelope;
2. verifies that an intentionally missing model cannot become loaded and that
   the router remains healthy;
3. checksum-verifies and loads Q8_0;
4. validates a completed OpenAI-compatible SSE chat stream;
5. closes an in-flight stream and confirms cancellation recovery;
6. submits a 30,000-token repeated-word request near the 32,768 context limit;
7. unloads the model;
8. restarts `local-ai-router.service`, waits for health, and confirms the
   no-autoload state; and
9. reloads, streams another completion, and performs a final unload.

The bounded JSON result contains check names, durations, status, and fixed
model/endpoint identities. It contains no prompts, responses, raw logs, private
paths, or model data. A best-effort checksum-gated unload runs after failures.

## Observed target-host result

The tracked result in
[`issue-9-router-smoke-result.json`](issue-9-router-smoke-result.json) passed all
checks against llama.cpp `b10446` / `adb55e5` and the verified
`Qwen3.5-27B-Q8_0` model. The near-boundary request was the longest check; use a
600-second per-operation bound on this hybrid 20-layer Vulkan profile.
