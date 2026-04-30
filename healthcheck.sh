#!/bin/bash
# Battle Buddy Health Check
# Usage: bash ./healthcheck.sh
#
# Public-safe defaults are placeholders. For a real deployment, set these
# environment variables locally or in a private environment file:
#   BATTLE_BUDDY_DB=/opt/battlebuddy/calls.db
#   BATTLE_BUDDY_SERVICE=battlebuddy
#   AUDIO_RECEIVER_PROCESS=audio_receiver
#   PI_HOST=radio-node.example.local
#   PI_USER=pi
#   NEXTCLOUD_HOST=nextcloud.example.com
#   NEXTCLOUD_OCC=/var/www/nextcloud/occ
#   NEXTCLOUD_DATA_DIR=/srv/nextcloud-data
#   GROQ_ENV_FILE=/opt/battlebuddy/.env

set -u

RED='\033[0;31m'
GRN='\033[0;32m'
YLW='\033[0;33m'
BLU='\033[0;34m'
CYN='\033[0;36m'
WHT='\033[1;37m'
RST='\033[0m'

ok()   { echo -e "  ${GRN}✓${RST} $1"; }
warn() { echo -e "  ${YLW}⚠${RST}  $1"; }
fail() { echo -e "  ${RED}✗${RST} $1"; }
hdr()  { echo -e "\n${BLU}━━━ ${WHT}$1${RST}${BLU} ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}"; }

BB_SERVICE="${BATTLE_BUDDY_SERVICE:-battlebuddy}"
BB_DB="${BATTLE_BUDDY_DB:-/opt/battlebuddy/calls.db}"
BB_PROCESS="${AUDIO_RECEIVER_PROCESS:-audio_receiver}"
PI_HOST="${PI_HOST:-radio-node.example.local}"
PI_USER="${PI_USER:-pi}"
NC_HOST="${NEXTCLOUD_HOST:-nextcloud.example.com}"
NC_OCC="${NEXTCLOUD_OCC:-/var/www/nextcloud/occ}"
NC_DATA_DIR="${NEXTCLOUD_DATA_DIR:-/srv/nextcloud-data}"
GROQ_ENV_FILE="${GROQ_ENV_FILE:-/opt/battlebuddy/.env}"

has_cmd() { command -v "$1" >/dev/null 2>&1; }

sqlite_count() {
  local sql="$1"
  if has_cmd sqlite3 && [ -f "$BB_DB" ]; then
    sqlite3 "$BB_DB" "$sql" 2>/dev/null || true
  fi
}

echo -e "${CYN}"
echo "  ██████╗  █████╗ ████████╗████████╗██╗     ███████╗"
echo "  ██╔══██╗██╔══██╗╚══██╔══╝╚══██╔══╝██║     ██╔════╝"
echo "  ██████╔╝███████║   ██║      ██║   ██║     █████╗  "
echo "  ██╔══██╗██╔══██║   ██║      ██║   ██║     ██╔══╝  "
echo "  ██████╔╝██║  ██║   ██║      ██║   ███████╗███████╗"
echo "  ╚═════╝ ╚═╝  ╚═╝   ╚═╝      ╚═╝   ╚══════╝╚══════╝"
echo -e "${RST}  ${WHT}Battle Buddy Health Check${RST}  —  $(date '+%Y-%m-%d %H:%M:%S')\n"

# ── System ───────────────────────────────────────────────────────────────────
hdr "SYSTEM"

if has_cmd free; then
  TOTAL=$(free -m | awk '/^Mem:/{print $2}')
  USED=$(free -m  | awk '/^Mem:/{print $3}')
  FREE=$(free -m  | awk '/^Mem:/{print $7}')
  SWAP_TOTAL=$(free -m | awk '/^Swap:/{print $2}')
  SWAP_USED=$(free -m  | awk '/^Swap:/{print $3}')
  MEM_PCT=$(( USED * 100 / TOTAL ))
  echo -e "  RAM:  ${USED}MB / ${TOTAL}MB used (${MEM_PCT}%)  —  ${FREE}MB available"
  if [ "$SWAP_TOTAL" -gt 0 ]; then
    echo -e "  Swap: ${SWAP_USED}MB / ${SWAP_TOTAL}MB used"
    [ "$SWAP_USED" -gt 1024 ] && warn "Swap usage high — consider RAM upgrade" || ok "Swap present and healthy"
  else
    warn "No swap configured"
  fi
  [ "$MEM_PCT" -gt 90 ] && fail "Memory critically high (${MEM_PCT}%)" || \
  [ "$MEM_PCT" -gt 75 ] && warn "Memory elevated (${MEM_PCT}%)" || ok "Memory OK (${MEM_PCT}%)"
else
  warn "free command not available"
fi

if has_cmd uptime; then
  LOAD=$(uptime | awk -F'load average:' '{print $2}' | awk '{print $1}' | tr -d ',')
  CORES=$(nproc 2>/dev/null || echo 1)
  echo -e "  Load: ${LOAD:-unknown} (${CORES} cores)"
fi

if has_cmd df; then
  DISK_PCT=$(df / | awk 'NR==2{print $5}' | tr -d '%')
  DISK_FREE=$(df -h / | awk 'NR==2{print $4}')
  [ "$DISK_PCT" -gt 90 ] && fail "Disk ${DISK_PCT}% full (${DISK_FREE} free)" || \
  [ "$DISK_PCT" -gt 75 ] && warn "Disk ${DISK_PCT}% full (${DISK_FREE} free)" || \
  ok "Disk ${DISK_PCT}% used (${DISK_FREE} free)"
fi

# ── Battle Buddy ─────────────────────────────────────────────────────────────
hdr "BATTLE BUDDY"

if has_cmd systemctl; then
  BB_STATUS=$(systemctl is-active "$BB_SERVICE" 2>/dev/null || true)
  [ "$BB_STATUS" = "active" ] && ok "${BB_SERVICE}.service running" || warn "${BB_SERVICE}.service is ${BB_STATUS:-unknown}"

  BB_START=$(systemctl show "$BB_SERVICE" --property=ActiveEnterTimestamp --value 2>/dev/null || true)
  [ -n "$BB_START" ] && echo -e "  Started: $BB_START"
else
  warn "systemctl not available"
fi

BB_MEM=$(ps aux | grep "$BB_PROCESS" | grep -v grep | awk '{sum+=$6} END {printf "%.0f", sum/1024}' 2>/dev/null || true)
BB_CPU=$(ps aux | grep "$BB_PROCESS" | grep -v grep | awk '{sum+=$3} END {printf "%.0f", sum}' 2>/dev/null || true)
if [ -n "${BB_MEM:-}" ]; then
  echo -e "  Process RAM: ${BB_MEM}MB  |  CPU: ${BB_CPU:-0}%"
  [ "${BB_MEM:-0}" -gt 3000 ] && warn "High RAM usage (${BB_MEM}MB)" || ok "RAM usage normal (${BB_MEM}MB)"
else
  warn "No ${BB_PROCESS} process found"
fi

if [ -f "$BB_DB" ]; then
  LAST_PI=$(sqlite_count "SELECT CAST((strftime('%s','now') - MAX(ts)) AS INTEGER) FROM calls WHERE node NOT LIKE '%broadcastify%';")
  if [ -n "${LAST_PI:-}" ] && [ "$LAST_PI" -ge 0 ]; then
    LAST_PI_MIN=$(( LAST_PI / 60 ))
    if   [ "$LAST_PI_MIN" -lt 10 ]; then ok "Capture-node intel: last call ${LAST_PI_MIN}m ago"
    elif [ "$LAST_PI_MIN" -lt 30 ]; then warn "Capture-node intel: last call ${LAST_PI_MIN}m ago"
    else                                  fail "Capture-node intel: no call in ${LAST_PI_MIN}m"
    fi
  fi

  LAST_BFY=$(sqlite_count "SELECT CAST((strftime('%s','now') - MAX(ts)) AS INTEGER) FROM calls WHERE node LIKE '%broadcastify%';")
  if [ -n "${LAST_BFY:-}" ] && [ "$LAST_BFY" -ge 0 ]; then
    LAST_BFY_MIN=$(( LAST_BFY / 60 ))
    [ "$LAST_BFY_MIN" -lt 5 ] && ok "Backup feed: last call ${LAST_BFY_MIN}m ago" || \
    warn "Backup feed: last call ${LAST_BFY_MIN}m ago"
  fi

  CALLS_TODAY=$(sqlite_count "SELECT COUNT(*) FROM calls WHERE date(ts,'unixepoch','localtime')=date('now','localtime');")
  PI_TODAY=$(sqlite_count "SELECT COUNT(*) FROM calls WHERE date(ts,'unixepoch','localtime')=date('now','localtime') AND node NOT LIKE '%broadcastify%';")
  echo -e "  Calls today: ${CALLS_TODAY:-0} total  (${PI_TODAY:-0} from capture node)"

  ACTIVE_INC=$(sqlite_count "SELECT COUNT(*) FROM incidents WHERE status='active';")
  echo -e "  Active incidents: ${ACTIVE_INC:-0}"
else
  warn "Battle Buddy database not found at ${BB_DB}"
fi

# ── AI Pipeline ──────────────────────────────────────────────────────────────
hdr "AI PIPELINE"

FW_CACHE=$(find /root/.cache/huggingface/hub -name "*.bin" -o -name "model.bin" 2>/dev/null | head -1 || true)
[ -n "$FW_CACHE" ] && ok "faster-whisper: model cached on disk" \
  || warn "faster-whisper: model not found in cache — first run may download it"

FW_RUNNING=$(ps aux | grep "$BB_PROCESS" | grep -v grep | wc -l | tr -d ' ')
[ "${FW_RUNNING:-0}" -gt 0 ] && ok "transcription process appears to be running" \
  || warn "transcription process not running"

if [ -f "$GROQ_ENV_FILE" ] && grep -q '^GROQ_API_KEY=' "$GROQ_ENV_FILE" && has_cmd curl; then
  GROQ_KEY=$(grep '^GROQ_API_KEY=' "$GROQ_ENV_FILE" | cut -d= -f2-)
  GROQ_RESULT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
    -H "Authorization: Bearer ${GROQ_KEY}" \
    -H "Content-Type: application/json" \
    -H "User-Agent: BattleBuddyHealthCheck/1.0" \
    -d '{"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' \
    "https://api.groq.com/openai/v1/chat/completions" 2>/dev/null || true)
  case "$GROQ_RESULT" in
    200) ok "Groq LLM API: reachable" ;;
    429) warn "Groq LLM API: reachable but rate-limited" ;;
    401) fail "Groq LLM API: auth failed — check API key" ;;
    403) fail "Groq LLM API: blocked by provider or network policy" ;;
    *)   warn "Groq LLM API: unexpected HTTP ${GROQ_RESULT:-timeout}" ;;
  esac
else
  warn "Groq live check skipped — no env file/key or curl unavailable"
fi

LAST_TRANSCRIPT=$(sqlite_count "SELECT CAST((strftime('%s','now') - MAX(ts)) AS INTEGER) FROM calls WHERE transcript != '' AND LENGTH(transcript) > 10;")
if [ -n "${LAST_TRANSCRIPT:-}" ] && [ "$LAST_TRANSCRIPT" -ge 0 ]; then
  LAST_TRANSCRIPT_MIN=$(( LAST_TRANSCRIPT / 60 ))
  [ "$LAST_TRANSCRIPT_MIN" -lt 15 ] && ok "Transcription pipeline: last transcript ${LAST_TRANSCRIPT_MIN}m ago" \
    || warn "Transcription pipeline: last transcript ${LAST_TRANSCRIPT_MIN}m ago"
fi

# ── Capture Node ─────────────────────────────────────────────────────────────
hdr "CAPTURE NODE (${PI_HOST})"

if has_cmd ssh; then
  if ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes "${PI_USER}@${PI_HOST}" "exit" &>/dev/null; then
    ok "Capture node reachable"

    PI_STATUS=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" \
      'echo op25=$(systemctl is-active op25-multi_rx 2>/dev/null) collector=$(systemctl --user is-active op25-collector 2>/dev/null) recorder=$(systemctl --user is-active call_recorder 2>/dev/null)' 2>/dev/null || true)

    for svc in op25 collector recorder; do
      VAL=$(echo "$PI_STATUS" | grep -o "${svc}=[a-z]*" | cut -d= -f2)
      case $svc in
        op25)      label="op25-multi_rx (P25 decoder)" ;;
        collector) label="op25-collector (talkgroup data)" ;;
        recorder)  label="call_recorder (audio → server)" ;;
      esac
      [ "$VAL" = "active" ] && ok "Capture node: ${label}" || warn "Capture node: ${label} is ${VAL:-unknown}"
    done

    OP25_PORT=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 "${PI_USER}@${PI_HOST}" \
      'ss -tlnp 2>/dev/null | grep -c ":8080"' 2>/dev/null || true)
    [ "${OP25_PORT:-0}" -gt 0 ] && ok "Capture node: OP25 HTTP port 8080 listening" \
      || warn "Capture node: OP25 port 8080 not listening"
  else
    warn "Capture node unreachable; set PI_HOST/PI_USER for your deployment"
  fi
else
  warn "ssh not available"
fi

# ── Nextcloud ────────────────────────────────────────────────────────────────
hdr "NEXTCLOUD (${NC_HOST})"

if has_cmd systemctl; then
  NC_STATUS=$(systemctl is-active nginx 2>/dev/null || true)
  [ "$NC_STATUS" = "active" ] && ok "nginx running" || warn "nginx is ${NC_STATUS:-unknown}"

  MYSQL_STATUS=$(systemctl is-active mysql 2>/dev/null || true)
  [ "$MYSQL_STATUS" = "active" ] && ok "MySQL running" || warn "MySQL is ${MYSQL_STATUS:-unknown}"

  PHP_STATUS=$(systemctl is-active php8.3-fpm 2>/dev/null || true)
  [ "$PHP_STATUS" = "active" ] && ok "PHP-FPM running" || warn "PHP-FPM is ${PHP_STATUS:-unknown}"
fi

if has_cmd curl; then
  NC_TIME=$(curl -s -o /dev/null -w "%{time_total}" --max-time 10 "https://${NC_HOST}/" 2>/dev/null || true)
  if [ -n "$NC_TIME" ] && has_cmd bc; then
    NC_MS=$(echo "$NC_TIME * 1000" | bc 2>/dev/null | cut -d. -f1)
    if [ -n "$NC_MS" ] && [ "$NC_MS" -gt 0 ]; then
      [ "$NC_MS" -ge 3000 ] && fail "Nextcloud very slow: ${NC_MS}ms" || \
      [ "$NC_MS" -ge 1000 ] && warn "Nextcloud slow: ${NC_MS}ms" || \
      ok "Nextcloud response time: ${NC_MS}ms"
    else
      warn "Nextcloud response check did not return timing"
    fi
  fi
fi

if [ -f "$NC_OCC" ]; then
  NC_MAINTENANCE=$(php "$NC_OCC" status 2>/dev/null | grep -i "maintenance.*true" || true)
  [ -n "$NC_MAINTENANCE" ] && warn "Nextcloud in maintenance mode" || ok "Nextcloud not in maintenance mode"
else
  warn "Nextcloud occ not found at ${NC_OCC}"
fi

if [ -d "$NC_DATA_DIR" ]; then
  NC_DB_SIZE=$(du -sh "$NC_DATA_DIR" 2>/dev/null | cut -f1)
  [ -n "$NC_DB_SIZE" ] && echo -e "  Data directory: ${NC_DB_SIZE}"
fi

# ── TLS Certificate ─────────────────────────────────────────────────────────
hdr "TLS CERTIFICATE"

if has_cmd openssl; then
  CERT_EXPIRY=$(echo | openssl s_client -servername "$NC_HOST" -connect "${NC_HOST}:443" 2>/dev/null \
    | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2 || true)
  if [ -n "$CERT_EXPIRY" ]; then
    EXPIRY_EPOCH=$(date -d "$CERT_EXPIRY" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    DAYS_LEFT=$(( (EXPIRY_EPOCH - NOW_EPOCH) / 86400 ))
    [ "$DAYS_LEFT" -lt 14 ] && fail "TLS cert expires in ${DAYS_LEFT} days" || \
    [ "$DAYS_LEFT" -lt 30 ] && warn "TLS cert expires in ${DAYS_LEFT} days" || \
    ok "TLS cert valid for ${DAYS_LEFT} days"
  else
    warn "TLS cert check skipped or failed for ${NC_HOST}"
  fi
fi

echo -e "\n${BLU}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}\n"
