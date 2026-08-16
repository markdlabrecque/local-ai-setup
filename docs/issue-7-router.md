# Issue #7: localhost llama.cpp router

The router assets target the pinned `b10446` / `adb55e5` llama.cpp build in
[`config/versions.env`](../config/versions.env). The server is deliberately
bound to `127.0.0.1:8080`, uses router mode, does not autoload a model, and
never owns model-file deletion.

## Start

After `scripts/setup.sh` and model download, run in a foreground terminal:

```bash
scripts/run-router.sh --foreground
```

The model and runtime paths are optional and are derived from `LOCAL_AI_MODEL_DIR`,
`LOCAL_AI_RUNTIME_DIR`, `LOCAL_AI_SERVER`, `LOCAL_AI_BIN_DIR`, and XDG defaults.
For a disposable or test server, pass `--server PATH`. Startup is bounded by
the config timeout and waits for `/health`; termination kills the complete
server process group.

`config/router-presets.json` is the reviewed portable contract. llama.cpp
b10446 consumes INI presets, so the launcher validates this JSON and generates
a disposable INI in the runtime directory. Both Q8_0 and Q6_K have explicit
32,768 context, `Vulkan0`, 20 GPU layers, flash attention, 256/128 batch and
micro-batch, and q8_0 K/V cache settings. `load-on-startup = false` is emitted
for every section and `--no-models-autoload` is always passed on the command
line.

## Model lifecycle

Download artifacts with the existing checksum-verifying downloader, then use
the manifest ID:

```bash
scripts/router-model.sh load \
  --model-id qwen3.5-27b-q8_0 \
  --models-dir "$LOCAL_AI_MODEL_DIR" \
  --manifest config/models.json
scripts/router-model.sh unload --model-id qwen3.5-27b-q8_0 \
  --models-dir "$LOCAL_AI_MODEL_DIR"
```

`router-model.sh load` verifies the exact manifest filename and SHA-256 before
making a POST to `/models/load`; a mismatch never reaches the server.
`unload` only requests runtime unloading. Neither operation removes a GGUF.
The helper accepts only plain HTTP to `127.0.0.1`.

## Optional real smoke

The portable gate uses a fake server and never starts the 28 GB artifact. After
starting a real router, an operator may explicitly run:

```bash
scripts/router-smoke.sh --real --base-url http://127.0.0.1:8080
```

This checks only `/health` and `/models`; it does not start llama-server or
load/delete a model. A model load can be requested separately with the helper.
