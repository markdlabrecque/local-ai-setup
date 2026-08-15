#!/usr/bin/env bash
# Issue #5 red contract: a portable, sanitized direct llama.cpp baseline runner.
set -u
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUNNER="$ROOT/scripts/run-direct-baseline.sh"
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
[[ -x "$RUNNER" ]] || fail "missing executable direct-baseline runner: scripts/run-direct-baseline.sh"

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
model="$tmp/qwen-Q8_0.gguf"; printf 'fake model bytes\n' >"$model"
sha=$(sha256sum "$model" | awk '{print $1}')
cat >"$tmp/fake-llama-cli" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
printf 'load: Vulkan0 device=AMD Radeon RX 6900 XT\n' >&2
printf 'llama_context: n_ctx = 32768\n' >&2
printf 'Hello streamed completion' | fold -w 5
printf 'stop reason: stop\n' >&2
EOF
chmod +x "$tmp/fake-llama-cli"
cat >"$tmp/measure.json" <<'EOF'
{"ram_mib": 1234, "vram_mib": 5678, "swap_mib": 0}
EOF
out="$tmp/result.json"
BASELINE_MEASURE_FILE="$tmp/measure.json" timeout 10 "$RUNNER" \
  --model "$model" --sha256 "$sha" --llama-cli "$tmp/fake-llama-cli" \
  --prompt 'Say hello.' --context 32768 --timeout 5 --output "$out" \
  || fail "runner failed with valid fake fixtures"

python3 - "$out" <<'PY' || exit 1
import json, pathlib, re, sys
p = pathlib.Path(sys.argv[1]); data = json.loads(p.read_text())
for key in ("schema_version", "model", "model_sha256", "command", "device", "context_tokens",
            "stream", "stop_reason", "exit_code", "timed_out", "measurements"):
    assert key in data, f"missing result field: {key}"
assert data["context_tokens"] == 32768
assert "Vulkan" in data["device"] and "RX 6900 XT" in data["device"]
assert "32768" in " ".join(data["command"] if isinstance(data["command"], list) else [data["command"]])
assert "Vulkan" in " ".join(data["command"] if isinstance(data["command"], list) else [data["command"]])
assert data["stream"]["completion"]
assert data["stream"].get("chunks") and len(data["stream"]["chunks"]) > 1
assert data["stop_reason"] == "stop" and data["stop_event"] in ("stopped-by-EOS", "finish_reason=stop", "stop")
assert data["exit_code"] == 0
assert data["timed_out"] is False
assert data["measurements"] == {"ram_mib": 1234, "vram_mib": 5678, "swap_mib": 0}
assert all(isinstance(data["measurements"][k], int) and data["measurements"][k] >= 0
           for k in ("ram_mib", "vram_mib", "swap_mib"))
assert data.get("vram_device") == "AMD Radeon RX 6900 XT"
assert data.get("swap_activity", {}).get("peak_mib", 0) >= 0
text = p.read_text()
assert "/home/" not in text and "BEGIN " not in text
assert not re.search(r"(api[_-]?key|access[_-]?token|secret|password|ghp_)", text, re.I)
PY
expect_reject() {
  local name=$1; shift
  if "$@" >"$tmp/$name.stdout" 2>"$tmp/$name.stderr"; then
    fail "$name was accepted"
  fi
}

# A zero timeout is invalid rather than an immediate, ambiguous timeout.
expect_reject timeout-zero "$RUNNER" --model "$model" --sha256 "$sha" \
  --llama-cli "$tmp/fake-llama-cli" --timeout 0 --output "$tmp/zero.json"

# Exit 137 from the CLI is not evidence that the supervisor timed out.
cat >"$tmp/exit-137" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
exit 137
EOF
chmod +x "$tmp/exit-137"
expect_reject direct-137 "$RUNNER" --model "$model" --sha256 "$sha" \
  --llama-cli "$tmp/exit-137" --timeout 5 --output "$tmp/137.json"
python3 - "$tmp/137.json" <<'PY' || exit 1
import json, sys
r = json.load(open(sys.argv[1]))
assert r["exit_code"] == 137 and r["timed_out"] is False
PY

# A plausible banner must not satisfy device/context/offload evidence.
cat >"$tmp/no-offload" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
printf 'Vulkan banner only; device=AMD Radeon RX 6900 XT\nllama_context: n_ctx = 32768\nanswer\nstop reason: stop\n' >&2
EOF
chmod +x "$tmp/no-offload"
expect_reject no-offload "$RUNNER" --model "$model" --sha256 "$sha" \
  --llama-cli "$tmp/no-offload" --expected-completion answer --output "$tmp/no-offload.json"

# Expected completion is an exact final stream, not a substring or extra text.
cat >"$tmp/extra-completion" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
printf 'load Vulkan0 device=AMD Radeon RX 6900 XT\nllama_context: n_ctx = 32768\noffloaded 20 layers\n' >&2
printf 'answer with extra text'
printf 'finish_reason=stop\n' >&2
EOF
chmod +x "$tmp/extra-completion"
expect_reject extra-completion "$RUNNER" --model "$model" --sha256 "$sha" \
  --llama-cli "$tmp/extra-completion" --expected-completion answer --output "$tmp/extra.json"

# Resource capture is mandatory and must include safe values plus system swap activity.
cat >"$tmp/no-resources" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
printf 'Vulkan0 device=AMD Radeon RX 6900 XT\nllama_context: n_ctx = 32768\noffloaded 20 layers\n' >&2
printf 'answer'; printf 'stopped-by-EOS\n' >&2
EOF
chmod +x "$tmp/no-resources"
expect_reject no-resources "$RUNNER" --model "$model" --sha256 "$sha" \
  --llama-cli "$tmp/no-resources" --expected-completion answer --output "$tmp/no-resources.json"

# Sanitization must cover arbitrary absolute paths, bearer/GitHub credentials,
# credential URLs, and private-key bodies—not only /home and token key names.
cat >"$tmp/secret-output" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
printf 'Vulkan0 device=AMD Radeon RX 6900 XT\nllama_context: n_ctx = 32768\noffloaded 20 layers\n' >&2
printf 'answer'; printf 'stopped-by-EOS\n/path/to/private/file bearer ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 https://user:password@example.invalid/x\n-----BEGIN OPENSSH PRIVATE KEY-----\nsecret-body\n-----END OPENSSH PRIVATE KEY-----\n' >&2
EOF
chmod +x "$tmp/secret-output"
expect_reject secrets "$RUNNER" --model "$model" --sha256 "$sha" \
  --llama-cli "$tmp/secret-output" --expected-completion answer --output "$tmp/secrets.json"

# A child that inherits the capture FIFO must not leave the parent waiting forever.
cat >"$tmp/orphan-writer" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
(sleep 30) &
exit 0
EOF
chmod +x "$tmp/orphan-writer"
expect_reject orphan-fifo timeout 5 "$RUNNER" --model "$model" --sha256 "$sha" \
  --llama-cli "$tmp/orphan-writer" --output "$tmp/orphan.json"

# A hung child must be terminated and represented as a timed-out lifecycle result.
cat >"$tmp/hanging-llama-cli" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
sleep 30
EOF
chmod +x "$tmp/hanging-llama-cli"

# TERM/INT must clean up the supervisor and its capture resources promptly.
for signal in TERM INT; do
  (timeout 5 "$RUNNER" --model "$model" --sha256 "$sha" --llama-cli "$tmp/hanging-llama-cli" \
    --timeout 30 --output "$tmp/interrupted-$signal.json") & supervisor=$!
  sleep 0.2; kill -"$signal" "$supervisor" 2>/dev/null || true
  wait "$supervisor" 2>/dev/null && fail "$signal did not stop runner" || true
done
if BASELINE_MEASURE_FILE="$tmp/measure.json" "$RUNNER" --model "$model" --sha256 "$(sha256sum "$model" | awk '{print $1}')" \
  --llama-cli "$tmp/hanging-llama-cli" --timeout 1 --output "$tmp/timeout.json"; then
  fail "hung llama-cli unexpectedly succeeded"
fi
python3 - "$tmp/timeout.json" <<'PY' || exit 1
import json, sys
result = json.load(open(sys.argv[1]))
assert result["timed_out"] is True
assert result["exit_code"] != 0
PY

# A committed structured fixture is the portable contract; it must be sanitized.
fixture="$ROOT/tests/fixtures/direct-baseline-result.json"
python3 - "$fixture" <<'PY' || exit 1
import json, pathlib, re, sys
p = pathlib.Path(sys.argv[1]); assert p.is_file(), f"missing committed fixture: {p}"
r = json.loads(p.read_text())
for key in ("schema_version", "command", "device", "context_tokens", "stream", "stop_event",
            "exit_code", "timed_out", "measurements", "model_metadata"):
    assert key in r, f"fixture missing {key}"
assert r["context_tokens"] == 32768 and r["timed_out"] is False
assert all(k in r["measurements"] for k in ("ram_mib", "vram_mib", "swap_mib"))
text = p.read_text()
assert not re.search(r"/(?:home|root|tmp|opt|var)/", text)
assert not re.search(r"(?i)(bearer\\s+|ghp_[A-Za-z0-9]+|https?://[^ ]*:[^ ]*@|BEGIN .*PRIVATE KEY|secret-body)", text)
PY

# Check checksum preflight prevents invoking llama.cpp on a changed artifact.
printf 'tampered\n' >>"$model"
if "$RUNNER" --model "$model" --sha256 "$sha" --llama-cli "$tmp/fake-llama-cli" --output "$tmp/bad.json"; then
  fail "checksum mismatch was accepted"
fi
printf 'ok\n'
