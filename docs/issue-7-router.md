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
server process group, including descendants left after an early leader exit.
The launcher clears inherited llama.cpp/Hugging Face cache variables, uses
private empty runtime caches and user configuration directories, and fails
closed if `/etc/llama.cpp/config.ini` is nonempty because b10446 provides no
switch to disable that system-level file.

`config/router-presets.json` is the reviewed portable contract. llama.cpp
b10446 consumes INI presets, so the launcher validates this JSON and generates
a unique mode-700-runtime/mode-600-file INI. Only non-empty, readable GGUF
files currently present in the models directory receive a section; placing the
manifest-named Q6_K artifact therefore makes Q6 appear without editing config.
The HTTP IDs are extensionless GGUF stems. Both Q8_0 and Q6_K have explicit
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

`router-model.sh` takes the downloader's shared `.filename.lock` before
verifying the exact manifest filename, SHA-256, and stat identity. It sends the
extensionless ID, requires exactly `{"success": true}`, polls `/models` until
it observes `loaded` or `unloaded`, and re-verifies identity before releasing
the lock. A timeout or identity change fails; neither operation removes a GGUF.
The helper accepts only plain HTTP to `127.0.0.1` and validates finite positive
polling timeouts.

## Optional real smoke

The portable gate uses a fake server and never starts the 28 GB artifact. After
starting a real router, an operator may explicitly run:

```bash
scripts/router-smoke.sh --real --base-url http://127.0.0.1:8080
```

This checks only `/health` and `/models`; it does not start llama-server or
load/delete a model. A model load can be requested separately with the helper.

## Target-host validation

The real pinned router was exercised on the target host with the verified Q8_0
artifact. `/health` returned `ok`; `/models` initially exposed only the
extensionless `Qwen3.5-27B-Q8_0` ID in `unloaded` state; no model was preloaded.
The checksum-gated helper then loaded it successfully. The router-reported child
arguments confirmed context 32768, `Vulkan0`, 20 GPU layers, flash attention,
batch 256, ubatch 128, and q8_0 K and V caches. The helper subsequently unloaded
the model, and launcher cleanup left no router process listening on port 8080.
No machine-specific paths or raw logs are committed.
