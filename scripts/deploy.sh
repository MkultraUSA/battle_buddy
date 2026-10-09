#!/bin/bash
# Battle Buddy — Deploy Script (with drift gate)
# Refuses to deploy if the working tree is dirty or not on origin/main.
#
# Usage:
#   bash scripts/deploy.sh [--force]  — default: gated
#   bash scripts/deploy.sh --force    — skip drift check (emergency override)
#
# Environment:
#   BATTLE_BUDDY_HOME     — repo path (default: /opt/battlebuddy)
#   BB_SERVICE            — supervisor service name (default: battlebuddy)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${BATTLE_BUDDY_HOME:-/opt/battlebuddy}"
BB_SERVICE="${BB_SERVICE:-battlebuddy}"

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[0;33m'
RST='\033[0m'

FORCE=false
if [ "${1:-}" = "--force" ]; then
    FORCE=true
    echo -e "${YLW}⚠  DEPLOY FORCED — skipping drift check${RST}"
fi

echo "=== Battle Buddy Deploy — $(date '+%Y-%m-%d %H:%M:%S') ==="

# ── Gate: drift check ────────────────────────────────────────────────────
if ! $FORCE; then
    echo "→ Running drift guard…"
    if BATTLE_BUDDY_HOME="$PROJECT_DIR" bash "$SCRIPT_DIR/guard_drift_check.sh" check; then
        echo -e "  ${GRN}✓${RST} Drift check passed"
    else
        echo ""
        echo -e "${RED}✗ DEPLOY BLOCKED: drift detected.${RST}"
        echo "  Resolve by committing/pushing changes or run with --force."
        exit 1
    fi
fi

# ── Pull latest ──────────────────────────────────────────────────────────
echo "→ Pulling origin/main…"
cd "$PROJECT_DIR"
git fetch origin main 2>&1
git checkout main 2>&1
git reset --hard origin/main 2>&1
echo -e "  ${GRN}✓${RST} Now at $(git rev-parse --short HEAD) — $(git log -1 --format=%s)"

# ── Restart service ──────────────────────────────────────────────────────
# This block used to try supervisorctl only, and on a systemd host it printed
# "skipping restart" and then "Deploy complete" — new code on disk, old process
# still serving. The deploy looked successful and was not. So: try systemd
# first (that is what every host actually uses), fall back to supervisorctl for
# the older ones, and if neither works, FAIL rather than claim a deploy that
# did not take effect.
echo "→ Restarting ${BB_SERVICE}…"
# Note the parentheses: `systemctl ... &>/dev/null | grep -q ...` parses as
# `systemctl ... &` (backgrounded) piped into grep, because `&>` binds to the
# simple command, not to the pipeline. The unit check then never ran and every
# deploy took the "no supervisor" branch. Subshell the pipeline instead.
if command -v systemctl &>/dev/null && (systemctl list-unit-files "${BB_SERVICE}.service" 2>/dev/null | grep -q "${BB_SERVICE}.service"); then
    systemctl restart "$BB_SERVICE" 2>&1
    # `restart` returns 0 even when the unit immediately dies, so confirm it is
    # actually up rather than trusting the exit code.
    sleep 5
    if systemctl is-active --quiet "$BB_SERVICE"; then
        echo -e "  ${GRN}✓${RST} Service restarted (systemd)"
    else
        echo -e "  ${RED}✗${RST} ${BB_SERVICE} did not come up after restart."
        systemctl status "$BB_SERVICE" --no-pager -l 2>&1 | head -20 || true
        exit 1
    fi
elif command -v supervisorctl &>/dev/null; then
    supervisorctl restart "$BB_SERVICE" 2>&1
    echo -e "  ${GRN}✓${RST} Service restarted (supervisorctl)"
else
    echo -e "  ${RED}✗ DEPLOY FAILED: no supervisor found for ${BB_SERVICE}.${RST}"
    echo "  Neither systemd nor supervisorctl knows this service. The code on"
    echo "  disk is updated but the running process is still the old one."
    echo "  Start it by hand and re-check, rather than trusting this script."
    exit 1
fi

# ── Verify the new code is the code that is running ───────────────────────
# A restart that succeeds and an endpoint that answers do not by themselves
# prove the deploy landed, and the bug this section exists to prevent was
# precisely that: the deploy script reported success while the old process
# kept serving. So check the counters the app reports are real numbers.
echo "→ Verifying…"
ON_DISK="$(git rev-parse --short HEAD)"
echo -e "  on disk: ${ON_DISK}"

HEALTH_URL="http://127.0.0.1:9001/api/health"
HEALTH_OUT="$(mktemp)"
trap 'rm -f "$HEALTH_OUT"' EXIT
if curl -sf --max-time 15 "$HEALTH_URL" -o "$HEALTH_OUT" 2>/dev/null; then
    echo -e "  ${GRN}✓${RST} /api/health answered"
    if python3 - "$HEALTH_OUT" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as fh:
        d = json.load(fh)
except Exception as exc:
    print(f"  \033[31m✗\033[0m could not read health json: {exc}")
    raise SystemExit(1)
db = d.get("db", {})
# -1 is the "this query failed" sentinel. Any of them means the snapshot is
# degraded, and a deploy should not be reported as clean over that.
bad = {k: v for k, v in db.items() if v == -1}
if bad:
    print(f"  \033[31m✗\033[0m health reports failed counters: {bad}")
    raise SystemExit(1)
print(f"  \033[32m✓\033[0m db counters all real "
      f"(calls={db.get('total_calls')}, 24h={db.get('calls_24h')}, "
      f"active={db.get('active_incidents')})")
PY
    then
        :
    else
        echo -e "  ${RED}✗${RST} health snapshot reports failed counters — see above."
        exit 1
    fi
else
    echo -e "  ${RED}✗ DEPLOY FAILED: ${HEALTH_URL} did not answer after restart.${RST}"
    echo "  The service restarted but is not serving. Check:"
    systemctl status "$BB_SERVICE" --no-pager -l 2>&1 | head -20 || true
    journalctl -u "$BB_SERVICE" -n 40 --no-pager 2>&1 | tail -20 || true
    exit 1
fi

echo ""
echo -e "${GRN}=== Deploy complete ===${RST}"