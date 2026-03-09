#!/bin/bash
# run_sitrep.sh — generates 4h sitrep, saves audio to public map, speaks locally
# Called by cron every 4 hours:
#   15 */4 * * * nice -n 19 flock -n /tmp/battle_buddy_sitrep.lock /home/pi/battle_buddy/run_sitrep.sh

cd "$(dirname "$0")"
source config.env 2>/dev/null

LOG="logs/parser_$(date +%Y%m%d).log"

# Skip if Ollama is actively processing a request
OLLAMA_BUSY=$(curl -sf http://localhost:11434/api/ps 2>/dev/null | grep -c '"model"' || true)
if [ "${OLLAMA_BUSY:-0}" -gt 0 ]; then
    echo "[run_sitrep] $(date '+%H:%M:%S') — Ollama busy, skipping sitrep" >> "$LOG"
    exit 0
fi

echo "[run_sitrep] $(date '+%Y-%m-%d %H:%M:%S') — Generating 4h sitrep" >> "$LOG"
python3 battle_buddy_summary.py --hours 4 --speak >> "$LOG" 2>&1
echo "[run_sitrep] Done — $(date '+%H:%M:%S')" >> "$LOG"
