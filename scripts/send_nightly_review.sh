#!/bin/bash
# Run nightly classification review and post report to Nextcloud Talk

set -euo pipefail

cd /opt/battlebuddy

# Load env
export $(grep -v '^#' .env | xargs)

REPORT=$(python3 scripts/nightly_review.py 2>&1)
FLAGGED=$(echo "$REPORT" | grep "flagged:" | grep -o 'flagged: [0-9]*' | grep -o '[0-9]*' || echo "?")
DATE=$(date '+%Y-%m-%d %H:%M')

# Truncate to 4000 chars (Talk message limit)
MESSAGE="🔍 Nightly Classification Review — $DATE
Flagged incidents: $FLAGGED

$(echo "$REPORT" | head -80)"

# Post to Nextcloud Talk (incidents room)
NC_URL="${NC_URL:-https://nextcloud.example.com/ocs/v2.php/apps/spreed/api/v1/chat/ROOM_TOKEN}"
AUTH=$(echo -n "${NC_USER}:${NC_PASS}" | base64)

curl -s -X POST "$NC_URL" \
  -H "Authorization: Basic $AUTH" \
  -H "OCS-APIRequest: true" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -d "{\"message\": $(echo "$MESSAGE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}" \
  > /dev/null

echo "[nightly_review] posted to Talk at $DATE"
