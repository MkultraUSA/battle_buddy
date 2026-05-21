#!/bin/bash
# Battle Buddy — Git Drift Guard
# Checks that /opt/battlebuddy repo is clean and on origin/main.
#
# Modes:
#   check        — report and exit 1 on drift (deploy gate)
#   check-quiet  — report and exit 1 on drift, no output when clean
#   report       — always print status (nightly cron)
#
# Usage:
#   bash scripts/guard_drift_check.sh [check|check-quiet|report]
set -euo pipefail

PROJECT_DIR="${BATTLE_BUDDY_HOME:-/opt/battlebuddy}"
MODE="${1:-check}"

RED='\033[0;31m'
GRN='\033[0;32m'
RST='\033[0m'

log_ok()    { echo -e "  ${GRN}✓${RST} $1"; }
log_fail()  { echo -e "  ${RED}✗${RST} $1"; }

# ── Check 1: working tree dirty? ──────────────────────────────────────────
dirty=false
if ! git -C "$PROJECT_DIR" diff --quiet 2>/dev/null; then
    dirty=true
elif ! git -C "$PROJECT_DIR" diff --cached --quiet 2>/dev/null; then
    dirty=true
fi

# Check for untracked files too (not in .gitignore)
if [ -z "$(git -C "$PROJECT_DIR" ls-files --others --exclude-standard 2>/dev/null)" ]; then
    :  # clean — no untracked files
else
    dirty=true
fi

# ── Check 2: on origin/main? ──────────────────────────────────────────────
at_main=true
LOCAL=$(git -C "$PROJECT_DIR" rev-parse HEAD 2>/dev/null || echo "unknown")
REMOTE=$(git -C "$PROJECT_DIR" rev-parse origin/main 2>/dev/null || echo "unknown")
if [ "$LOCAL" != "$REMOTE" ]; then
    at_main=false
elif [ "$LOCAL" = "unknown" ]; then
    at_main=false
fi

# ── Output ────────────────────────────────────────────────────────────────
case "$MODE" in
    check)
        if $dirty || ! $at_main; then
            echo "DRIFT DETECTED in $PROJECT_DIR:"
            $dirty && log_fail "working tree is dirty" || log_ok "working tree is clean"
            $at_main && log_ok "on origin/main ($REMOTE)" || log_fail "not on origin/main (local=$LOCAL remote=$REMOTE)"
            exit 1
        fi
        # deploy gate passed — silent success
        ;;

    check-quiet)
        if $dirty || ! $at_main; then
            echo "DRIFT DETECTED in $PROJECT_DIR:"
            $dirty && log_fail "working tree is dirty" || log_ok "working tree is clean"
            $at_main && log_ok "on origin/main ($REMOTE)" || log_fail "not on origin/main (local=$LOCAL remote=$REMOTE)"
            exit 1
        fi
        ;;

    report)
        echo "Git Drift Check — $(date '+%Y-%m-%d %H:%M:%S')"
        if $dirty || ! $at_main; then
            echo "DRIFT DETECTED in $PROJECT_DIR:"
            $dirty && log_fail "working tree is dirty" || log_ok "working tree is clean"
            $at_main && log_ok "on origin/main ($REMOTE)" || log_fail "not on origin/main (local=$LOCAL remote=$REMOTE)"
            exit 1
        else
            log_ok "working tree is clean"
            log_ok "on origin/main ($REMOTE)"
            exit 0
        fi
        ;;

    *)
        echo "guard_drift_check.sh: unknown mode '$MODE'. Use check, check-quiet, or report."
        exit 2
        ;;
esac
