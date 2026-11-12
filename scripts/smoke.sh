#!/usr/bin/env bash
# Probes the running dev stack end to end. Exits non-zero on the first failure.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
api="http://127.0.0.1:${API_PORT:-8000}"
web="http://127.0.0.1:${WEB_PORT:-3000}"
fail() { echo "SMOKE FAIL: $*" >&2; exit 1; }

curl -fsS "$api/healthz" >/dev/null || fail "api /healthz"
ready=$(curl -fsS "$api/readyz") || fail "api /readyz not ready"
echo "api readyz: $ready"
curl -fsS "$web/api/health" >/dev/null || fail "web /api/health"
curl -fsS "$web/" | grep -q "Research Desk" || fail "web home page does not render the product name"
uv run --frozen python -m rd_worker.smoke || fail "worker pipeline probe"
for _ in $(seq 1 20); do
  status=$(curl -fsS "$api/v1/status")
  if echo "$status" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["ok"] and d["components"]["rd-scheduler"]["leader_elected"] else 1)'; then
    echo "pipeline status: $status"
    echo "SMOKE OK"
    exit 0
  fi
  sleep 1
done
fail "status endpoint never reported worker+scheduler healthy with an elected leader: $status"
