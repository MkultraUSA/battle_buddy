#!/bin/bash
# Battle Buddy Watchdog — zero-LLM health checks. Silent when healthy.

LOG_SINCE="10 min ago"
ERRORS=0
AIRCRAFT=""
LEO_COUNT=""
INCIDENTS=""
CALLS=""

# 1. Service alive?
if ! systemctl -q is-active battlebuddy.service; then
    echo "CRITICAL: battlebuddy.service DOWN. Restarting..."
    systemctl restart battlebuddy.service
    sleep 3
    systemctl -q is-active battlebuddy.service && echo "RECOVERED" || echo "FAILED: still down"
    ERRORS=$((ERRORS+1))
fi

# 2. Import/Name errors?
IMPORT_ERRS=$(journalctl -u battlebuddy.service --since "$LOG_SINCE" --no-pager 2>/dev/null | grep -ciE "ImportError|NameError" || echo 0)
if [ "$IMPORT_ERRS" -gt 0 ] 2>/dev/null; then
    echo "CRITICAL: $IMPORT_ERRS ImportError/NameError(s):"
    journalctl -u battlebuddy.service --since "$LOG_SINCE" --no-pager | grep -A2 -E "ImportError|NameError" | tail -20
    ERRORS=$((ERRORS+1))
fi

# 3. PROCESS ERRORs?
PROC_ERRS=$(journalctl -u battlebuddy.service --since "$LOG_SINCE" --no-pager 2>/dev/null | grep -c "PROCESS ERROR" || echo 0)
if [ "$PROC_ERRS" -gt 5 ] 2>/dev/null; then
    echo "CRITICAL: $PROC_ERRS PROCESS ERRORs:"
    journalctl -u battlebuddy.service --since "$LOG_SINCE" --no-pager | grep "PROCESS ERROR" | tail -5
    ERRORS=$((ERRORS+1))
fi

# 4. ADS-B API?
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:9001/api/adsb 2>/dev/null)
if [ "$HTTP_CODE" != "200" ]; then
    echo "HIGH: /api/adsb HTTP $HTTP_CODE"
    ERRORS=$((ERRORS+1))
fi

# 5. Poller errors?
POLLER_ERRS=$(journalctl -u battlebuddy.service --since "$LOG_SINCE" --no-pager 2>/dev/null | grep -c "Poller error" || echo 0)
if [ "$POLLER_ERRS" -gt 2 ] 2>/dev/null; then
    echo "MEDIUM: $POLLER_ERRS poller errors:"
    journalctl -u battlebuddy.service --since "$LOG_SINCE" --no-pager | grep "Poller error" | tail -5
    ERRORS=$((ERRORS+1))
fi

# 6. Incident gap?
read INCIDENTS CALLS <<< $(sqlite3 /opt/battlebuddy/calls.db "SELECT (SELECT COUNT(*) FROM incidents WHERE ts_start > strftime(%s,now,-30 minutes)), (SELECT COUNT(*) FROM calls WHERE ts > strftime(%s,now,-30 minutes));" 2>/dev/null)
if [ "${CALLS:-0}" -gt 50 ] 2>/dev/null && [ "${INCIDENTS:-0}" -lt 2 ] 2>/dev/null; then
    echo "CRITICAL: $CALLS calls but $INCIDENTS incidents in 30min — detection dead."
    ERRORS=$((ERRORS+1))
fi

# 7. Aircraft
AIRCRAFT=$(curl -s http://localhost:9001/api/adsb 2>/dev/null | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null)
LEO_COUNT=$(curl -s http://localhost:9001/api/adsb 2>/dev/null | python3 -c "import json,sys; print(sum(1 for a in json.load(sys.stdin) if a.get(is_leo)))" 2>/dev/null)

# Report
if [ "$ERRORS" -gt 0 ] 2>/dev/null; then
    echo "Watchdog: $ERRORS issue(s). $(date)"
    exit 1
fi

# Heartbeat every hour at :00
if [ "$(date +%M)" = "00" ]; then
    echo "OK: ${CALLS:-?} calls, ${INCIDENTS:-?} incidents, ${AIRCRAFT:-?} aircraft (${LEO_COUNT:-?} LEO). $(date)"
fi
