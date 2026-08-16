# Issue #10: Pi integration for local Qwen3.5

The router remains Pi's native llama.cpp lifecycle provider. The reviewed
`local-qwen` model entry supplies the Qwen-specific inference metadata that the
native dynamically discovered entry cannot currently express.

## Native router setup

Start Pi and run:

```text
/login llama.cpp
```

Accept `http://127.0.0.1:8080` and leave the API key empty. The equivalent
per-process configuration is:

```bash
LLAMA_BASE_URL=http://127.0.0.1:8080 pi
```

In Pi, use `/llama` to load or unload `Qwen3.5-27B-Q8_0`; Pi never silently
unloads another model and does not delete model files. Only loaded native
models appear in `/model`. Use `/model` after loading to select an inference
entry.

For non-interactive service preparation, the checksum-gated equivalent is:

```bash
scripts/router-model.sh load --model-id qwen3.5-27b-q8_0 --timeout 180
```

## Reviewed model metadata

Pi 0.84.2's native llama.cpp discovery correctly derives the loaded ID,
OpenAI-compatible endpoint, text input, zero cost, and 32,768 context from the
router. Its dynamic entry currently declares `reasoning: false` and sets the
output limit equal to the whole context. That is insufficient for the
confirmed Qwen thinking control and a useful coding-agent input budget, so the
fallback permitted by the workplan is required.

Install [`config/pi-models.example.json`](../config/pi-models.example.json) as
`~/.pi/agent/models.json`. If that file already exists, merge only its
`providers.local-qwen` object instead of overwriting other providers:

```bash
install -d -m 700 ~/.pi/agent
install -m 600 config/pi-models.example.json ~/.pi/agent/models.json
```

After Q8_0 is loaded, select:

```text
/model local-qwen/Qwen3.5-27B-Q8_0
```

The reviewed entry declares only observed behavior:

- model ID `Qwen3.5-27B-Q8_0`, text input, zero local cost;
- 32,768 context and a conservative 4,096-token maximum output;
- OpenAI Chat Completions at `http://127.0.0.1:8080/v1`;
- `system` rather than `developer`, `max_tokens`, streaming usage, no `store`,
  and non-strict function schemas, matching Pi's native llama.cpp defaults;
- `reasoning: true` with `thinkingFormat: "qwen-chat-template"`, which sends
  `chat_template_kwargs.enable_thinking` and preserves Qwen reasoning replay.

No role, strict-tool, token-field, or thinking compatibility switch beyond
those observed values is enabled.

## Thinking and tool use

Use `/settings` or `--thinking off` for direct answers. A non-off level enables
Qwen chat-template thinking; `high` was validated and produced a distinct Pi
`thinking` block. The endpoint does not report a separate reasoning-token count,
so do not infer one from the text block.

Sequential and parallel `read` calls were validated through Pi itself. In the
sequential case Pi replayed the first tool result before Qwen issued the second
call. In the parallel case Qwen emitted two calls in one assistant turn and Pi
started both before either result was returned.

## Reproduce the real validation

With the service running and Q8_0 loaded:

```bash
scripts/pi-integration-smoke.py --real --timeout 300 \
  --result results/issue-10-pi-result.json
```

The runner uses a private temporary Pi config and disposable read-only tool
fixtures. It checks model discovery/metadata, JSON-event text streaming and
usage, thinking off/high, sequential replay, and parallel calls. Each Pi
process has a hard timeout and 8 MiB output bound; failure exits nonzero. The
sanitized target-host result is committed as
[`issue-10-pi-result.json`](issue-10-pi-result.json).

For a normal interactive coding session:

```bash
cd /path/to/project
pi --provider local-qwen --model Qwen3.5-27B-Q8_0 --thinking high
```
