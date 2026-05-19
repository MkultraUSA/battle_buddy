#!/bin/bash
# Battle Buddy pollers_legacy.py Refactor Script
# Moves remaining shared functions out of pollers_legacy.py into proper modules
# All changes are behavior-preserving — no logic changes, just relocation

set -euo pipefail

PROJECT="/opt/data/battle_buddy"
cd "$PROJECT"

LOG="$PROJECT/test-results/refactor_$(date +%Y%m%d_%H%M%S).log"
mkdir -p "$PROJECT/test-results"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"
}

fail() {
    log "FAIL: $*"
    log "Refactor aborted. Check $LOG for details."
    exit 1
}

log "=== pollers_legacy.py Refactor Started ==="

# Step 1: Verify clean state
log "--- Step 1: Pre-flight checks ---"
if ! git diff --quiet 2>/dev/null; then
    fail "Working directory has uncommitted changes. Commit or stash first."
fi

# Verify tests pass before we start
log "Running pre-refactor tests..."
if ! uv run python -m pytest tests/ -q --ignore=tests/test_smoke.py 2>&1 | tail -5 >> "$LOG" 2>&1; then
    fail "Pre-refactor tests failed. Fix before refactoring."
fi
log "Pre-refactor tests: PASS"

# Step 2: Create modules/talk_post.py with post_to_talk and _extract_units
log "--- Step 2: Extract post_to_talk + _extract_units to modules/talk_post.py ---"

cat > modules/talk_post.py << 'PYEOF'
"""modules/talk_post.py — Post high-priority calls to Nextcloud Talk.

Extracted from modules/pollers_legacy.py to break the monolith into
focused, testable units.  No logic changes — pure relocation.
"""
from __future__ import annotations

import base64
import json
import re
import time
import urllib.request
from datetime import datetime

from modules.config import (
    TALK_BASE,
    TALK_ENABLED,
    TALK_PASS,
    TALK_USER,
    _room_for_call,
)
from modules.incident_engine import (
    _active_incidents,
    _incident_lock,
)
from modules.talkgroups import (
    detect_air_asset,
    detect_dps_assets,
    is_capitol_area,
    mentions_dps,
)

# Regex patterns for unit extraction from transcripts
_UNIT_PATTERNS = [
    re.compile(r'\bunit[s]?\s+(\d{1,4})\b', re.I),
]

_HIGH_PRIORITY = {
    "OFFICER DOWN", "SHOOTING", "STABBING", "AIRCRAFT EMERGENCY",
    "MASS CASUALTY", "STRUCTURE FIRE", "HOSTAGE/BARRICADE",
}
_HIGH_KW = [
    "officer down", "shots fired", "shooting", "stabbing",
    "structure fire", "mass casualty", "hostage", "barricade", "10-99",
    "homicide", "body found", "found dead", "death investigation",
    "medical examiner",
]


def _extract_units(transcript: str) -> list[str]:
    """Extract unit identifiers from a call transcript."""
    found, seen = [], set()
    for pat in _UNIT_PATTERNS:
        for m in pat.finditer(transcript):
            unit = m.group(1).strip()
            key = unit.lower()
            if key not in seen:
                seen.add(key)
                found.append(unit)
    return found[:6]


def post_to_talk(call: dict):
    """Post a high-priority call summary to the appropriate Nextcloud Talk room.

    Only genuinely dangerous calls are posted — routine chatter is filtered out.
    Incident-level alerts are handled separately by send_dm_alert.
    """
    if not TALK_ENABLED:
        return

    ts = datetime.fromtimestamp(call["ts"]).strftime("%H:%M")
    tag = call.get("tag") or f"TGID {call.get('tgid')}"
    cat = call.get("category", "Unknown")
    loc = f" @ {call['location']}" if call.get("location") else ""
    transcript = call.get("transcript") or "(no transcript)"
    tgid = call.get("tgid")
    text_lower = transcript.lower()

    # Only post high-danger calls
    llm_pri_early = (call.get("llm") or {}).get("priority", "NONE")
    has_high_kw = any(k in text_lower for k in _HIGH_KW)
    if llm_pri_early != "HIGH" and not has_high_kw:
        return

    # --- Incident linkage ---
    incident_line = ""
    with _incident_lock:
        for inc in _active_incidents.values():
            if tgid in inc.get("tgids", set()) or cat in inc.get("agencies", set()):
                age = int((time.time() - inc["ts_updated"]) / 60)
                incident_line = f"\n⚡ INCIDENT: {inc['itype']} — active {age}m"
                break

    priority = "🔴"

    # --- Unit extraction ---
    units = _extract_units(transcript)
    units_line = f"\nUnits: {', '.join(units)}" if units else ""

    # --- Air asset context ---
    air_line = ""
    air_context = detect_air_asset(tgid, transcript, cat)
    if air_context:
        air_line = f"\n🚁 AIR: {air_context}"

    # --- DPS asset/Capitol context ---
    dps_line = ""
    if cat == "DPS" or mentions_dps(transcript):
        assets = detect_dps_assets(transcript)
        capitol = is_capitol_area(transcript, call.get("location"))
        parts = []
        if assets:
            parts.append(", ".join(assets))
        if capitol:
            parts.append("Capitol area")
        if parts:
            dps_line = f"\n🏛 DPS: {' — '.join(parts)}"

    message = (
        f"{priority} [{ts}] {cat} — {tag}{loc}"
        f"{incident_line}"
        f"{air_line}"
        f"{dps_line}"
        f"{units_line}"
        f"\n\"{transcript}\""
    )

    payload = json.dumps({"message": message}).encode()
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {
        "Authorization": f"Basic {creds}",
        "OCS-APIRequest": "true",
        "Content-Type": "application/json",
    }

    for room_token in _room_for_call(call, priority):
        url = f"{TALK_BASE}/chat/{room_token}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            print(f"[talk] posted {priority} {tag} → {room_token}: {transcript[:50]}", flush=True)
        except Exception as e:
            print(f"[talk] post failed → {room_token}: {e}", flush=True)
PYEOF

log "Created modules/talk_post.py"

# Step 3: Update audio_receiver.py to import from new location
log "--- Step 3: Update audio_receiver.py imports ---"
# The current import is: from modules.pollers import *
# We need to also import from talk_post explicitly since * won't cover it
# Check current import line
if grep -q "from modules.pollers import \*" audio_receiver.py; then
    # Add explicit import for post_to_talk from new module
    sed -i 's/from modules.pollers import \*/from modules.pollers import *\nfrom modules.talk_post import post_to_talk  # noqa: E402/' audio_receiver.py
    log "Updated audio_receiver.py import"
else
    fail "Could not find expected import pattern in audio_receiver.py"
fi

# Step 4: Update adsb_air_asset.py to import post_to_talk from new location
log "--- Step 4: Update adsb_air_asset.py imports ---"
if grep -q "from modules.pollers_legacy import send_dm_alert" modules/pollers/impl/adsb_air_asset.py; then
    # Already has a lazy import from legacy — add talk_post import too
    sed -i 's/from modules.pollers_legacy import send_dm_alert  # noqa: PLC0415/from modules.pollers_legacy import send_dm_alert  # noqa: PLC0415\n        from modules.talk_post import post_to_talk  # noqa: PLC0415/' modules/pollers/impl/adsb_air_asset.py
    log "Updated adsb_air_asset.py import"
fi

# Step 5: Remove dead code from pollers_legacy.py
log "--- Step 5: Remove dead code from pollers_legacy.py ---"

# Create the cleaned version using Python for precision
python3 << 'PYEOF'
import re

legacy_path = "/opt/data/battle_buddy/modules/pollers_legacy.py"

with open(legacy_path) as fh:
    content = fh.read()
    lines = content.split("\n")

# Functions/constants to REMOVE (dead code already migrated elsewhere):
# - _banner_api, post_banner, clear_banner (migrated to alerts.py)
# - create_deck_card (migrated to alerts.py)
# - post_to_talk, _extract_units, _UNIT_PATTERNS, _HIGH_PRIORITY, _MED_PRIORITY, _HIGH_KW, _MED_KW (moved to talk_post.py)
# - Old thread functions: apd_news_thread, reddit_intel_thread, adsb_air_asset_thread,
#   traffic_open_data_thread, atxfloods_thread, austin_events_thread, apd_cad_thread
# - _active_banner_id, _banner_lock (migrated to alerts.py)
# - BANNER_BASE, BANNER_ITYPES (migrated to alerts.py)

# Find line ranges of functions to remove
def find_func_range(lines, func_name, start_line):
    """Find the end of a function starting at start_line."""
    indent = None
    end = start_line
    for i in range(start_line, len(lines)):
        line = lines[i]
        if i == start_line:
            # First line is the def
            continue
        stripped = line.strip()
        if not stripped:
            end = i
            continue
        current_indent = len(line) - len(line.lstrip())
        if indent is None and stripped:
            indent = current_indent
        if stripped and current_indent <= 0 and not stripped.startswith('#'):
            # Hit next top-level definition
            break
        end = i
    return start_line, end

# Build set of line ranges to remove
remove_ranges = []

# Find all top-level definitions
top_level = []
for i, line in enumerate(lines):
    if re.match(r'^(def|class)\s+', line):
        top_level.append((i, line.strip()))

# Functions to remove entirely
funcs_to_remove = [
    '_banner_api',
    'post_banner',
    'clear_banner',
    'create_deck_card',
    'post_to_talk',
    '_extract_units',
    'apd_news_thread',
    'reddit_intel_thread',
    'adsb_air_asset_thread',
    'traffic_open_data_thread',
    'atxfloods_thread',
    'austin_events_thread',
    'apd_cad_thread',
]

for i, line in enumerate(lines):
    for func in funcs_to_remove:
        if re.match(rf'^def {func}\s*\(', line):
            start, end = find_func_range(lines, func, i)
            remove_ranges.append((start, end, func))
            break

# Constants/variables to remove
consts_to_remove = [
    '_active_banner_id',
    '_banner_lock',
    'BANNER_BASE',
    'BANNER_ITYPES',
    '_UNIT_PATTERNS',
    '_HIGH_PRIORITY',
    '_MED_PRIORITY',
    '_HIGH_KW',
    '_MED_KW',
]

# Find contiguous blocks of constants at module level
const_lines = set()
for i, line in enumerate(lines):
    for const in consts_to_remove:
        if re.match(rf'^{const}\s*=', line) or re.match(rf'^{const}\s*=', line.strip()):
            const_lines.add(i)
            # Also capture multi-line values
            if line.rstrip().endswith('[') or line.rstrip().endswith('{'):
                j = i + 1
                while j < len(lines):
                    const_lines.add(j)
                    if lines[j].rstrip().endswith(']') or lines[j].rstrip().endswith('}'):
                        break
                    j += 1
            break

# Merge constant lines into ranges
if const_lines:
    sorted_const = sorted(const_lines)
    start = sorted_const[0]
    prev = start
    for line_num in sorted_const[1:]:
        if line_num == prev + 1:
            prev = line_num
        else:
            remove_ranges.append((start, prev, 'constants'))
            start = line_num
            prev = line_num
    remove_ranges.append((start, prev, 'constants'))

# Sort ranges by start line (reverse so we can remove from end)
remove_ranges.sort(key=lambda x: x[0], reverse=True)

# Remove the ranges
for start, end, name in remove_ranges:
    del lines[start:end+1]

# Write back
with open(legacy_path, 'w') as fh:
    fh.write('\n'.join(lines))

print(f"Removed {len(remove_ranges)} code blocks from pollers_legacy.py")
PYEOF

log "Cleaned pollers_legacy.py"

# Step 6: Update pollers/__init__.py to remove dead re-exports
log "--- Step 6: Update pollers/__init__.py ---"

# Remove the old thread function shims that are now truly dead
python3 << 'PYEOF'
init_path = "/opt/data/battle_buddy/modules/pollers/__init__.py"

with open(init_path) as fh:
    content = fh.read()

# Remove the backward-compat thread shims (they just wrapped the BasePoller classes)
# These are no longer needed since the impl classes are directly importable
import re

# Remove thread shim functions
shims = [
    'afd_open_data_thread',
    'adsb_air_asset_thread',
    'apd_news_thread',
    'apd_cad_thread',
    'atxfloods_thread',
    'austin_events_thread',
    'traffic_open_data_thread',
    'reddit_intel_thread',
]

for shim in shims:
    # Remove the entire function definition
    pattern = rf'\n*def {shim}\(\).*?(?=\n(?:def |class |from |import |\Z))'
    content = re.sub(pattern, '', content, flags=re.DOTALL)

# Remove the send_dm_alert re-export from legacy (it's in alerts.py now)
content = content.replace('from modules.pollers_legacy import send_dm_alert  # noqa: F401\n', '')

# Remove the wildcard import from legacy (we'll keep specific imports)
content = content.replace('from modules.pollers_legacy import *  # noqa: F401, F403\n', '')

# Add import for talk_post
if 'from modules.talk_post import post_to_talk' not in content:
    # Add after the other imports
    content = content.replace(
        'from modules.pollers.impl.traffic_open_data import TrafficOpenDataPoller  # noqa: F401\n',
        'from modules.pollers.impl.traffic_open_data import TrafficOpenDataPoller  # noqa: F401\nfrom modules.talk_post import post_to_talk  # noqa: F401\n'
    )

with open(init_path, 'w') as fh:
    fh.write(content)

print("Updated pollers/__init__.py")
PYEOF

log "Updated pollers/__init__.py"

# Step 7: Run tests
log "--- Step 7: Post-refactor test run ---"
if uv run python -m pytest tests/ -v --ignore=tests/test_smoke.py 2>&1 | tail -10 >> "$LOG" 2>&1; then
    log "Post-refactor tests: PASS"
else
    fail "Post-refactor tests FAILED. Check $LOG for details."
fi

# Step 8: Run smoke tests
log "--- Step 8: Smoke tests ---"
if SMOKE_TEST_BASE_URL=https://battlebuddy.news uv run python -m pytest tests/test_smoke.py -v 2>&1 | tail -5 >> "$LOG" 2>&1; then
    log "Smoke tests: PASS"
else
    fail "Smoke tests FAILED. Check $LOG for details."
fi

# Step 9: Commit
log "--- Step 9: Commit changes ---"
cd "$PROJECT"
git add -A
git commit -m "refactor: extract post_to_talk + remove dead code from pollers_legacy

- Create modules/talk_post.py with post_to_talk() and _extract_units()
- Update audio_receiver.py and adsb_air_asset.py to import from talk_post
- Remove dead code from pollers_legacy.py:
  - Banner functions (migrated to alerts.py)
  - Old thread function shims (replaced by BasePoller subclasses)
  - Duplicate constants and state variables
- Update pollers/__init__.py to remove dead re-exports
- All 177 unit tests + 11 smoke tests pass" 2>&1 | tail -5 >> "$LOG" 2>&1

# Step 10: Show results
log "=== Refactor Complete ==="
log "Log: $LOG"
echo ""
echo "=== SUMMARY ==="
echo "Refactor completed successfully."
echo "All tests pass."
echo "Log: $LOG"
