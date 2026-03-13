#!/bin/bash
# run_parser.sh — finds today's radio log and runs the incident pipeline
# Designed to be called by cron every 30 minutes:
#   */30 * * * * /home/pi/battle_buddy/run_parser.sh

cd "$(dirname "$0")"

LOG_DIR="logs"
DATE=$(date +%Y%m%d)
PARSER_LOG="$LOG_DIR/parser_${DATE}.log"

# Find today's log files for all active streams
LAW_LOG=$(ls "$LOG_DIR"/radio_law_${DATE}.log 2>/dev/null | head -1)
CALLS_LOG=$(ls "$LOG_DIR"/radio_calls_${DATE}.log 2>/dev/null | head -1)

if [ -z "$LAW_LOG" ] && [ -z "$CALLS_LOG" ]; then
    echo "[run_parser] No log files found for $DATE — skipping"
    exit 0
fi

# Extract incidents via Claude — run each stream log found
for LOG_FILE in $LAW_LOG $CALLS_LOG; do
    [ -z "$LOG_FILE" ] && continue
    echo "[run_parser] $(date '+%Y-%m-%d %H:%M:%S') — Processing $LOG_FILE"
    python3 radio_parser.py --log "$LOG_FILE" >> "$PARSER_LOG" 2>&1
done

# Poll Broadcastify IPN (Incident Page Network) for Travis County
python3 ipn_poller.py >> "$PARSER_LOG" 2>&1

# Regenerate public heatmap → logs/map/index.html
python3 make_heatmap.py >> "$PARSER_LOG" 2>&1

echo "[run_parser] Done — $(date '+%H:%M:%S')"
