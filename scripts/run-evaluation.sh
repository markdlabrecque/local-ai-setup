#!/usr/bin/env bash
# Version-controlled Issue #11 evaluation entrypoint.
set -euo pipefail
root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exec python3 "$root/scripts/run-evaluation.py" "$@"
