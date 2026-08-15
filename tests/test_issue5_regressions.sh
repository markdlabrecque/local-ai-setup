#!/usr/bin/env bash
# Post-review regression contracts for Issue #5.  These are intentionally red
# until the baseline artifact owns the additional provenance and safety claims.
set -u
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
RUNNER="$ROOT/scripts/run-direct-baseline.sh"
fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }
[[ -x "$RUNNER" ]] || fail "missing runner"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
model="$tmp/model.gguf"; printf 'fixture model\n' >"$model"
sha=$(sha256sum "$model" | awk '{print $1}')
cat >"$tmp/measure.json" <<'EOF'
{"ram_mib":100,"vram_mib":200,"swap_mib":0}
EOF
cat >"$tmp/cli" <<'EOF'
#!/usr/bin/env bash
if [[ ${1:-} == --version ]]; then
  printf '%s\n' "llama-cli version b10446 (adb55e5)"
  exit 0
fi
printf 'build 1 (adb55e5) Vulkan0 device=AMD Radeon RX 6900 XT\nllama_context: n_ctx = 32768\noffloaded 20 layers\n' >&2
printf 'LOCAL_AI_BASELINE_OK\n'
printf 'finish_reason=stop\n' >&2
EOF
chmod +x "$tmp/cli"
run() { BASELINE_MEASURE_FILE="$tmp/measure.json" "$RUNNER" --model "$model" --sha256 "$sha" --llama-cli "$tmp/cli" --expected-completion LOCAL_AI_BASELINE_OK --output "$1" --timeout 5; }

# The release tag is a reproducible identity, not merely a human-readable tag.
grep -Eq '^LLAMA_CPP_REF=b10446$' "$ROOT/config/versions.env" || fail "release is not pinned to b10446"
grep -Eiq '^LLAMA_CPP_COMMIT=adb55e5([0-9a-f]{0,37})?$' "$ROOT/config/versions.env" || fail "b10446 is not mapped to commit adb55e5"

# A CLI reporting a different version/commit must be rejected before baseline capture.
cat >"$tmp/wrong-version" <<'EOF'
#!/usr/bin/env bash
if [[ ${1:-} == --version ]]; then printf '%s\n' 'llama-cli version b99999 (deadbee)'; exit 0; fi
exit 99
EOF
chmod +x "$tmp/wrong-version"
if BASELINE_MEASURE_FILE="$tmp/measure.json" "$RUNNER" --model "$model" --sha256 "$sha" --llama-cli "$tmp/wrong-version" --output "$tmp/wrong.json"; then
  fail "mismatched llama-cli version was accepted"
fi

# Exact expected output followed by another response line is not an exact match.
cat >"$tmp/extra-line" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
printf 'Vulkan0 device=AMD Radeon RX 6900 XT\nllama_context: n_ctx = 32768\noffloaded 20 layers\n' >&2
printf 'LOCAL_AI_BASELINE_OK\nUNEXPECTED_RESPONSE\n'
printf 'finish_reason=stop\n' >&2
EOF
chmod +x "$tmp/extra-line"
if BASELINE_MEASURE_FILE="$tmp/measure.json" "$RUNNER" --model "$model" --sha256 "$sha" --llama-cli "$tmp/extra-line" --expected-completion LOCAL_AI_BASELINE_OK --output "$tmp/extra.json" --timeout 5 2>/dev/null; then
  fail "exact completion plus extra line was accepted"
fi

# Stream chunks must not be asserted as transport chunks unless read boundaries
# and timestamps (or an equivalent honest provenance field) were captured.
run "$tmp/stream.json" || fail "valid stream fixture failed"
python3 - "$tmp/stream.json" <<'PY' || exit 1
import json, sys
r = json.load(open(sys.argv[1]))
s = r["stream"]
if len(s.get("chunks", [])) > 1:
    assert s.get("chunk_evidence"), "chunks lack transport evidence"
    assert all("timestamp" in x and "read_boundary" in x for x in s["chunk_evidence"])
PY

# VRAM selection is injectable and must follow the target PCI identity, not max
# all cards (the decoy card intentionally has the larger allocation).
sys="$tmp/sys/class/drm"
for card in card0 card1 card2; do mkdir -p "$sys/$card/device"; done
printf '0x1002\n' >"$sys/card0/device/vendor"; printf '0x13c0\n' >"$sys/card0/device/device"
printf '0x1002\n' >"$sys/card1/device/vendor"; printf '0x73bf\n' >"$sys/card1/device/device"
printf '0x1002\n' >"$sys/card2/device/vendor"; printf '0x9999\n' >"$sys/card2/device/device"
printf '100\n' >"$sys/card0/device/mem_info_vram_used"
printf '200\n' >"$sys/card1/device/mem_info_vram_used"
printf '999999999\n' >"$sys/card2/device/mem_info_vram_used"
BASELINE_SYSFS_ROOT="$tmp/sys" run "$tmp/vram.json" || fail "injectable VRAM fixture failed"
python3 - "$tmp/vram.json" <<'PY' || exit 1
import json, sys
r = json.load(open(sys.argv[1]))
assert r["vram_card"] == "card1"
assert r["vram_pci_id"] == "1002:73BF"
PY

# Sanitizer succeeds with supplied measurements while excluding arbitrary paths,
# bearer/GitHub tokens, credential URLs, and private-key bodies from JSON.
cat >"$tmp/secrets" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
printf 'Vulkan0 device=AMD Radeon RX 6900 XT\nllama_context: n_ctx = 32768\noffloaded 20 layers\n/path/arbitrary/private\n' >&2
printf 'LOCAL_AI_BASELINE_OK\n'; printf 'finish_reason=stop bearer TOPSECRET ghp_ABC123 https://u:p@example.invalid/x\n-----BEGIN OPENSSH PRIVATE KEY-----\nKEYBODY\n-----END OPENSSH PRIVATE KEY-----\n' >&2
EOF
chmod +x "$tmp/secrets"
run_secret() { BASELINE_MEASURE_FILE="$tmp/measure.json" "$RUNNER" --model "$model" --sha256 "$sha" --llama-cli "$tmp/secrets" --expected-completion LOCAL_AI_BASELINE_OK --output "$tmp/safe.json"; }
run_secret || fail "sanitizer fixture was not accepted"
python3 - "$tmp/safe.json" <<'PY' || exit 1
import json, pathlib, sys
text = pathlib.Path(sys.argv[1]).read_text()
for secret in ("/path/arbitrary/private", "TOPSECRET", "ghp_ABC123", "https://u:p@example.invalid", "KEYBODY"):
    assert secret not in text, secret
json.load(open(sys.argv[1]))
PY

# Child exit 124 is distinct from timeout(1)'s 124 status and must be recorded.
cat >"$tmp/natural-124" <<'EOF'
#!/usr/bin/env bash
[[ ${1:-} == --version ]] && { printf 'llama-cli version b10446 (adb55e5)\n'; exit 0; }
printf 'Vulkan0 device=AMD Radeon RX 6900 XT\nllama_context: n_ctx = 32768\noffloaded 20 layers\n' >&2
printf 'LOCAL_AI_BASELINE_OK\n'; exit 124
EOF
chmod +x "$tmp/natural-124"
BASELINE_MEASURE_FILE="$tmp/measure.json" "$RUNNER" --model "$model" --sha256 "$sha" --llama-cli "$tmp/natural-124" --expected-completion LOCAL_AI_BASELINE_OK --output "$tmp/natural.json" >/dev/null 2>&1 || true
python3 - "$tmp/natural.json" <<'PY' || exit 1
import json, sys
r=json.load(open(sys.argv[1]))
assert r["exit_code"] == 124 and r["timed_out"] is False
PY
printf 'ok\n'
