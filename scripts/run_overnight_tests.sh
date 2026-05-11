#!/bin/bash
# Battle Buddy Overnight Test Runner
# Runs full test suite: unit tests + smoke tests against battlebuddy.news
# Results logged to /opt/data/battle_buddy/test-results/

set -euo pipefail

PROJECT_DIR="/opt/data/battle_buddy"
RESULTS_DIR="$PROJECT_DIR/test-results"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULT_FILE="$RESULTS_DIR/test-run_$TIMESTAMP.log"
LATEST_LINK="$RESULTS_DIR/latest.log"

mkdir -p "$RESULTS_DIR"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$RESULT_FILE"
}

log "=== Battle Buddy Test Run Started ==="
log "Timestamp: $TIMESTAMP"

# --- Unit Tests ---
log "--- Running Unit Tests (local) ---"
cd "$PROJECT_DIR"
if uv run python -m pytest tests/ -v --ignore=tests/test_smoke.py 2>&1 | tee -a "$RESULT_FILE"; then
    UNIT_RESULT="PASS"
else
    UNIT_RESULT="FAIL"
fi
log "Unit tests result: $UNIT_RESULT"

# --- Smoke Tests ---
log "--- Running Smoke Tests (battlebuddy.news) ---"
if SMOKE_TEST_BASE_URL=https://battlebuddy.news uv run python -m pytest tests/test_smoke.py -v 2>&1 | tee -a "$RESULT_FILE"; then
    SMOKE_RESULT="PASS"
else
    SMOKE_RESULT="FAIL"
fi
log "Smoke tests result: $SMOKE_RESULT"

# --- Summary ---
log "=== Test Run Summary ==="
log "Unit tests:  $UNIT_RESULT"
log "Smoke tests: $SMOKE_RESULT"
log "Full log: $RESULT_FILE"

# Update latest symlink
ln -sf "$RESULT_FILE" "$LATEST_LINK"

# Keep only last 30 result files
ls -t "$RESULTS_DIR"/test-run_*.log 2>/dev/null | tail -n +31 | xargs -r rm

# Exit with failure if either suite failed
if [ "$UNIT_RESULT" = "FAIL" ] || [ "$SMOKE_RESULT" = "FAIL" ]; then
    log "OVERALL: FAIL"
    exit 1
fi

log "OVERALL: PASS"
exit 0
