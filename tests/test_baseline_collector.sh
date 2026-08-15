#!/usr/bin/env bash
set -u

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
COLLECTOR="$ROOT/scripts/collect-baseline.sh"
CRITERIA="$ROOT/docs/02-hardware-baseline-and-acceptance-criteria.md"

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

[[ -x "$COLLECTOR" ]] || fail "missing executable collector: scripts/collect-baseline.sh"
[[ -f "$CRITERIA" ]] || fail "missing portable acceptance-criteria documentation"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
"$COLLECTOR" --output-dir "$tmp" >/dev/null || fail "collector exited unsuccessfully"
report=$(find "$tmp" -maxdepth 1 -type f -name '*baseline*.txt' -o -name '*baseline*.md' | head -n1)
[[ -n "$report" ]] || fail "collector did not write a versioned baseline report"

for field in GPU VRAM Vulkan CPU RAM swap disk kernel Pi; do
  grep -Eiq "(^|[^[:alnum:]])${field}([^[:alnum:]]|$)" "$report" || fail "report lacks $field field"
done

# Reports must be safe to commit: reject common credential/token patterns.
! grep -Eiq '(api[_-]?key|access[_-]?token|secret|password|BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|ghp_[[:alnum:]]+)' "$report" \
  || fail "report contains a secret-like pattern"

grep -Eiq 'margin|threshold|32[[:space:]]*K|swap' "$CRITERIA" \
  || fail "criteria documentation lacks explicit resource margins/thresholds"
printf 'ok\n'
