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
echo "→ Restarting ${BB_SERVICE}…"
if command -v supervisorctl &>/dev/null; then
    supervisorctl restart "$BB_SERVICE" 2>&1
    echo -e "  ${GRN}✓${RST} Service restarted"
else
    echo -e "  ${YLW}⚠${RST}  supervisorctl not found — skipping restart"
fi

echo ""
echo -e "${GRN}=== Deploy complete ===${RST}"