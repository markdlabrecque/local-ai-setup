#!/usr/bin/env bash
# Version-controlled Issue #12 benchmark entrypoint.
set -euo pipefail
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$root/scripts/run-benchmark.py" "$@"
