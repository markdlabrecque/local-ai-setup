#!/usr/bin/env bash
# Opt-in, bounded smoke check for an already-running real router.
set -Eeuo pipefail
REAL=0; BASE_URL='http://127.0.0.1:8080'; TIMEOUT=10
usage() {
  cat <<'EOF'
Usage: scripts/router-smoke.sh --real [--base-url http://127.0.0.1:8080]

--real is mandatory. This command never starts llama-server and never deletes
model files; it only checks the localhost router's health and model inventory.
EOF
}
die() { printf 'error: %s\n' "$*" >&2; exit 1; }
while (($#)); do
  case "$1" in
    --real) REAL=1; shift ;;
    --base-url) (($# > 1)) || die '--base-url requires a URL'; BASE_URL=$2; shift 2 ;;
    --timeout) (($# > 1)) || die '--timeout requires seconds'; TIMEOUT=$2; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done
(( REAL )) || die 'refusing smoke check without explicit --real'
python3 - "$BASE_URL" "$TIMEOUT" <<'PY'
import json, sys, urllib.error, urllib.request
from urllib.parse import urlsplit
base, timeout = sys.argv[1].rstrip("/"), float(sys.argv[2])
parsed = urlsplit(base)
if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.query or parsed.fragment:
    raise SystemExit("error: smoke base URL must be plain http on 127.0.0.1")
for path in ("/health", "/models"):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as response:
            body = json.loads(response.read() or b"{}")
            if response.status != 200: raise SystemExit(f"error: {path} returned HTTP {response.status}")
            print(json.dumps(body, sort_keys=True))
    except (OSError, ValueError, urllib.error.URLError) as exc: raise SystemExit(f"error: {path} failed: {exc}")
PY
