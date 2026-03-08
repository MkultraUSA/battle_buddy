#!/bin/bash
# run_parser.sh — finds today's radio log and runs the incident parser
# Designed to be called by cron every 30 minutes

cd "$(dirname "$0")"

LOG_DIR="logs"
DATE=$(date +%Y%m%d)

# Find today's log file (supports law, fire, ems streams)
LOG_FILE=$(ls "$LOG_DIR"/radio_*_${DATE}.log 2>/dev/null | head -1)

if [ -z "$LOG_FILE" ]; then
    echo "[run_parser] No log file found for $DATE — skipping"
    exit 0
fi

echo "[run_parser] $(date '+%Y-%m-%d %H:%M:%S') — Processing $LOG_FILE"
python3 radio_parser_v1.3.py --log "$LOG_FILE" --push >> "$LOG_DIR/parser_$(date +%Y%m%d).log" 2>&1
