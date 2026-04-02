#!/bin/bash
# Battle Buddy + Nextcloud Health Check
# Usage: bash /opt/battlebuddy/healthcheck.sh

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

# Memory
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
  fail "No swap configured"
fi
[ "$MEM_PCT" -gt 90 ] && fail "Memory critically high (${MEM_PCT}%)" || \
[ "$MEM_PCT" -gt 75 ] && warn "Memory elevated (${MEM_PCT}%)" || ok "Memory OK (${MEM_PCT}%)"

# Load
LOAD=$(uptime | awk -F'load average:' '{print $2}' | awk '{print $1}' | tr -d ',')
CORES=$(nproc)
echo -e "  Load: ${LOAD} (${CORES} cores)"
LOAD_INT=${LOAD%.*}
[ "$LOAD_INT" -gt "$CORES" ] && fail "Load average above core count" || ok "Load OK"

# Disk
DISK_PCT=$(df / | awk 'NR==2{print $5}' | tr -d '%')
DISK_FREE=$(df -h / | awk 'NR==2{print $4}')
[ "$DISK_PCT" -gt 90 ] && fail "Disk ${DISK_PCT}% full (${DISK_FREE} free)" || \
[ "$DISK_PCT" -gt 75 ] && warn "Disk ${DISK_PCT}% full (${DISK_FREE} free)" || \
ok "Disk ${DISK_PCT}% used (${DISK_FREE} free)"

# ── Battle Buddy ─────────────────────────────────────────────────────────────
hdr "BATTLE BUDDY"

# Service status
BB_STATUS=$(systemctl is-active battlebuddy 2>/dev/null)
[ "$BB_STATUS" = "active" ] && ok "battlebuddy.service running" || fail "battlebuddy.service is $BB_STATUS"

# Uptime
BB_START=$(systemctl show battlebuddy --property=ActiveEnterTimestamp --value 2>/dev/null)
[ -n "$BB_START" ] && echo -e "  Started: $BB_START"

# Memory used by Battle Buddy
BB_MEM=$(ps aux | grep audio_receiver | grep -v grep | awk '{sum+=$6} END {printf "%.0f", sum/1024}')
BB_CPU=$(ps aux | grep audio_receiver | grep -v grep | awk '{sum+=$3} END {printf "%.0f", sum}')
[ -n "$BB_MEM" ] && echo -e "  Process RAM: ${BB_MEM}MB  |  CPU: ${BB_CPU}%"
[ "${BB_MEM:-0}" -gt 3000 ] && warn "High RAM usage (${BB_MEM}MB)" || ok "RAM usage normal (${BB_MEM}MB)"
[ "${BB_CPU:-0}" -gt 150 ]  && warn "CPU usage elevated (${BB_CPU}%) — transcription backlog?" || ok "CPU usage normal (${BB_CPU}%)"

# Last call from Pi (real TGID intel)
LAST_PI=$(sqlite3 /opt/battlebuddy/calls.db \
  "SELECT CAST((strftime('%s','now') - MAX(ts)) AS INTEGER) FROM calls WHERE node NOT LIKE '%broadcastify%';" 2>/dev/null)
if [ -n "$LAST_PI" ] && [ "$LAST_PI" -ge 0 ]; then
  LAST_PI_MIN=$(( LAST_PI / 60 ))
  if   [ "$LAST_PI_MIN" -lt 10 ]; then ok "Pi intel: last call ${LAST_PI_MIN}m ago"
  elif [ "$LAST_PI_MIN" -lt 30 ]; then warn "Pi intel: last call ${LAST_PI_MIN}m ago (quiet or OP25 issue?)"
  else                                  fail "Pi intel: no call in ${LAST_PI_MIN}m — check OP25"
  fi
fi

# Last call from Broadcastify (backup feed)
LAST_BFY=$(sqlite3 /opt/battlebuddy/calls.db \
  "SELECT CAST((strftime('%s','now') - MAX(ts)) AS INTEGER) FROM calls WHERE node LIKE '%broadcastify%';" 2>/dev/null)
if [ -n "$LAST_BFY" ] && [ "$LAST_BFY" -ge 0 ]; then
  LAST_BFY_MIN=$(( LAST_BFY / 60 ))
  [ "$LAST_BFY_MIN" -lt 5 ] && ok "Broadcastify feed: last call ${LAST_BFY_MIN}m ago" || \
  warn "Broadcastify feed: last call ${LAST_BFY_MIN}m ago"
fi

# Calls today
CALLS_TODAY=$(sqlite3 /opt/battlebuddy/calls.db \
  "SELECT COUNT(*) FROM calls WHERE date(ts,'unixepoch','localtime')=date('now','localtime');" 2>/dev/null)
PI_TODAY=$(sqlite3 /opt/battlebuddy/calls.db \
  "SELECT COUNT(*) FROM calls WHERE date(ts,'unixepoch','localtime')=date('now','localtime') AND node NOT LIKE '%broadcastify%';" 2>/dev/null)
echo -e "  Calls today: ${CALLS_TODAY:-0} total  (${PI_TODAY:-0} from Pi, $((${CALLS_TODAY:-0} - ${PI_TODAY:-0})) broadcastify)"

# Active incidents
ACTIVE_INC=$(sqlite3 /opt/battlebuddy/calls.db \
  "SELECT COUNT(*) FROM incidents WHERE status='active';" 2>/dev/null)
INC_TODAY=$(sqlite3 /opt/battlebuddy/calls.db \
  "SELECT COUNT(*) FROM incidents WHERE date(created_at,'unixepoch','localtime')=date('now','localtime');" 2>/dev/null)
echo -e "  Active incidents: ${ACTIVE_INC:-0}  |  Incidents today: ${INC_TODAY:-0}"

# ── AI Pipeline ──────────────────────────────────────────────────────────────
hdr "AI PIPELINE"

# faster-whisper model cached on disk
FW_CACHE=$(find /root/.cache/huggingface/hub -name "*.bin" -o -name "model.bin" 2>/dev/null | head -1)
[ -n "$FW_CACHE" ] && ok "faster-whisper: model cached on disk (offline-ready)" \
  || warn "faster-whisper: model not found in cache — will download on first run"

# faster-whisper process check (model loaded in-process, not a separate binary)
FW_RUNNING=$(ps aux | grep audio_receiver | grep -v grep | wc -l)
[ "${FW_RUNNING:-0}" -gt 0 ] && ok "faster-whisper: loaded in battlebuddy process (base.en INT8)" \
  || warn "faster-whisper: battlebuddy process not running"

# Groq API live connectivity test (LLM analysis — chat/completions endpoint)
GROQ_RESULT=$(curl -s -o /dev/null -w "%{http_code}" --max-time 8 \
  -H "Authorization: Bearer GROQ_API_KEY_REMOVED" \
  -H "Content-Type: application/json" \
  -H "User-Agent: Mozilla/5.0" \
  -d '{"model":"llama-3.3-70b-versatile","messages":[{"role":"user","content":"ping"}],"max_tokens":1}' \
  "https://api.groq.com/openai/v1/chat/completions" 2>/dev/null)
if   [ "$GROQ_RESULT" = "200" ]; then ok "Groq LLM API: reachable (direct from server)"
elif [ "$GROQ_RESULT" = "429" ]; then warn "Groq LLM API: reachable but rate-limited (free tier quota)"
elif [ "$GROQ_RESULT" = "401" ]; then fail "Groq LLM API: auth failed — check API key"
elif [ "$GROQ_RESULT" = "403" ]; then fail "Groq LLM API: blocked (Cloudflare 403) — Pi relay required"
else                                   fail "Groq LLM API: unreachable (HTTP ${GROQ_RESULT:-timeout})"
fi

# Last successful Groq-analyzed call (incident_type not from keyword detection)
LAST_GROQ=$(sqlite3 /opt/battlebuddy/calls.db \
  "SELECT CAST((strftime('%s','now') - MAX(ts)) AS INTEGER) FROM calls WHERE transcript != '' AND LENGTH(transcript) > 10;" 2>/dev/null)
if [ -n "$LAST_GROQ" ] && [ "$LAST_GROQ" -ge 0 ]; then
  LAST_GROQ_MIN=$(( LAST_GROQ / 60 ))
  [ "$LAST_GROQ_MIN" -lt 15 ] && ok "Transcription pipeline: last transcript ${LAST_GROQ_MIN}m ago" \
    || warn "Transcription pipeline: last transcript ${LAST_GROQ_MIN}m ago"
fi

# ── Pi 5 ─────────────────────────────────────────────────────────────────────
hdr "PI 5 (192.168.1.158)"

PI_REACHABLE=false
if ping -c 1 -W 3 192.168.1.158 &>/dev/null; then
  ok "Pi 5 reachable"
  PI_REACHABLE=true
else
  fail "Pi 5 unreachable"
fi

if $PI_REACHABLE; then
  PI_STATUS=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 pi@192.168.1.158 \
    'echo op25=$(systemctl is-active op25-multi_rx) collector=$(systemctl --user is-active op25-collector) recorder=$(systemctl --user is-active call_recorder)' 2>/dev/null)

  for svc in op25 collector recorder; do
    VAL=$(echo "$PI_STATUS" | grep -o "${svc}=[a-z]*" | cut -d= -f2)
    case $svc in
      op25)      label="op25-multi_rx (P25 decoder)" ;;
      collector) label="op25-collector (talkgroup data)" ;;
      recorder)  label="call_recorder (audio → server)" ;;
    esac
    [ "$VAL" = "active" ] && ok "Pi: ${label}" || fail "Pi: ${label} is ${VAL:-unknown}"
  done

  # OP25 HTTP port 8080 — confirms RTL-SDR lock and trunking active
  OP25_PORT=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 pi@192.168.1.158 \
    'ss -tlnp | grep -c ":8080"' 2>/dev/null)
  [ "${OP25_PORT:-0}" -gt 0 ] && ok "Pi: OP25 HTTP port 8080 listening (RTL-SDR active)" \
    || warn "Pi: OP25 port 8080 not listening — RTL-SDR may not have lock"

  # Groq relay — still running on Pi (not required for LLM anymore, kept as backup path)
  RELAY_STATUS=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 pi@192.168.1.158 \
    'systemctl --user is-active groq-relay' 2>/dev/null)
  [ "$RELAY_STATUS" = "active" ] && ok "Pi: groq-relay running (backup path)" \
    || warn "Pi: groq-relay inactive"

  # Pi CPU/RAM snapshot
  PI_LOAD=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 pi@192.168.1.158 \
    'uptime | awk -F"load average:" "{print \$2}" | awk "{print \$1}" | tr -d ","' 2>/dev/null)
  PI_MEM=$(ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 pi@192.168.1.158 \
    'free -m | awk "/^Mem:/{printf \"%d/%dMB\", \$3, \$2}"' 2>/dev/null)
  [ -n "$PI_LOAD" ] && echo -e "  Pi load: ${PI_LOAD}  |  RAM: ${PI_MEM}"
fi

# ── Nextcloud ─────────────────────────────────────────────────────────────────
hdr "NEXTCLOUD"

NC_STATUS=$(systemctl is-active snap.nextcloud.apache 2>/dev/null)
[ "$NC_STATUS" = "active" ] && ok "Nextcloud Apache running" || fail "Nextcloud Apache is $NC_STATUS"

MYSQL_STATUS=$(systemctl is-active snap.nextcloud.mysql 2>/dev/null)
[ "$MYSQL_STATUS" = "active" ] && ok "Nextcloud MySQL running" || fail "Nextcloud MySQL is $MYSQL_STATUS"

PHP_STATUS=$(systemctl is-active snap.nextcloud.php-fpm 2>/dev/null)
[ "$PHP_STATUS" = "active" ] && ok "Nextcloud PHP-FPM running" || fail "Nextcloud PHP-FPM is $PHP_STATUS"

# HTTP response time
NC_TIME=$(curl -s -k -o /dev/null -w "%{time_total}" --max-time 10 "https://kevcloud.ddns.net/" 2>/dev/null)
NC_MS=$(echo "$NC_TIME * 1000" | bc 2>/dev/null | cut -d. -f1)
if [ -n "$NC_MS" ] && [ "$NC_MS" -gt 0 ]; then
  [ "$NC_MS" -ge 3000 ] && fail "Nextcloud very slow: ${NC_MS}ms" || \
  [ "$NC_MS" -ge 1000 ] && warn "Nextcloud slow: ${NC_MS}ms" || \
  ok "Nextcloud response time: ${NC_MS}ms"
else
  fail "Nextcloud not responding"
fi

# OCC status
NC_MAINTENANCE=$(sudo snap run nextcloud.occ status 2>/dev/null | grep -i "maintenance.*true")
[ -n "$NC_MAINTENANCE" ] && warn "Nextcloud in maintenance mode" || ok "Nextcloud not in maintenance mode"

# Nextcloud DB size
NC_DB_SIZE=$(du -sh /var/snap/nextcloud/current/nextcloud/data 2>/dev/null | cut -f1)
[ -n "$NC_DB_SIZE" ] && echo -e "  Data directory: ${NC_DB_SIZE}"

# ── Nginx ────────────────────────────────────────────────────────────────────
hdr "NGINX"
NGX_STATUS=$(systemctl is-active nginx 2>/dev/null)
[ "$NGX_STATUS" = "active" ] && ok "nginx running" || fail "nginx is $NGX_STATUS"

# SSL cert expiry
CERT_EXPIRY=$(echo | openssl s_client -servername kevcloud.ddns.net -connect kevcloud.ddns.net:443 2>/dev/null \
  | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$CERT_EXPIRY" ]; then
  EXPIRY_EPOCH=$(date -d "$CERT_EXPIRY" +%s 2>/dev/null)
  NOW_EPOCH=$(date +%s)
  DAYS_LEFT=$(( (EXPIRY_EPOCH - NOW_EPOCH) / 86400 ))
  [ "$DAYS_LEFT" -lt 14 ] && fail "SSL cert expires in ${DAYS_LEFT} days!" || \
  [ "$DAYS_LEFT" -lt 30 ] && warn "SSL cert expires in ${DAYS_LEFT} days" || \
  ok "SSL cert valid for ${DAYS_LEFT} days"
fi

echo -e "\n${BLU}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RST}\n"
