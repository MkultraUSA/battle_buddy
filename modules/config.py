import os
try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None

DB_PATH         = "/opt/battlebuddy/calls.db"
TIPS_UPLOAD_DIR = "/opt/battlebuddy/static/tips"
TGID_TSV        = "/opt/battlebuddy/gatrrs-tags.tsv"
PI1_OP25_URL    = "http://radiodesk.ddns.net:8080/"

GROQ_API_KEY  = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL    = "llama-3.3-70b-versatile"
GROQ_ENABLED  = bool(GROQ_API_KEY)
GROQ_API_BASE = "https://api.groq.com/openai/v1"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_ENABLED = bool(ANTHROPIC_API_KEY) and (_anthropic is not None)

TALK_BASE    = "https://kevcloud.ddns.net/ocs/v2.php/apps/spreed/api/v1"
TALK_USER    = "battlebuddy"
TALK_PASS    = os.environ.get("TALK_PASS", "")
TALK_ENABLED = True

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN  = os.environ.get("MAILGUN_DOMAIN", "")
MAILGUN_FROM    = f"Battle Buddy <mailgun@{MAILGUN_DOMAIN}>"
ALERT_EMAIL     = "k.watkins@me.com"

GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_ID      = os.environ.get("GOOGLE_CSE_ID", "")
GOOGLE_ROUTES_KEY  = os.environ.get("GOOGLE_ROUTES_KEY", "")
GOOGLE_MAPS_JS_KEY = os.environ.get("GOOGLE_MAPS_JS_KEY", "")

PI_FETCH_URL     = os.environ.get("PI_FETCH_URL", "").rstrip("/")
PI_FETCH_TOKEN   = os.environ.get("PI_FETCH_TOKEN", "")
PI_FETCH_ENABLED = bool(PI_FETCH_URL and PI_FETCH_TOKEN)

FTS_HOST      = "radiodesk.ddns.net"
FTS_REST_PORT = 19023
FTS_COT_PORT  = 8089
FTS_TOKEN     = "token"
FTS_ENABLED   = True

DECK_BASE      = "https://kevcloud.ddns.net/index.php/apps/deck/api/v1.0"
NC_WEBDAV      = "https://kevcloud.ddns.net/remote.php/dav/files/kevin"
NC_USER        = os.environ.get("NC_USER", "")
NC_PASS        = os.environ.get("NC_PASS", "")
NC_REPORT_DIR  = "PresentationNotes/FlaggedIncidents"
DECK_BOARD_ID  = 2
DECK_STACK_NEW = 5
DECK_LABELS    = {
    "SHOOTING":               10,
    "OFFICER DOWN":           11,
    "STRUCTURE FIRE":         12,
    "MASS CASUALTY":          13,
    "HAZMAT":                 14,
    "CRASH/COLLISION":        15,
    "MULTI-AGENCY RESPONSE":  16,
    "AIR ASSET ACTIVE":       17,
    "DPS CAPITOL ACTIVATION": 18,
}

TALK_ROOM  = "iyidr3xy"
TALK_ROOMS = {
    "incidents": "89q5fnh5",
    "apd":       "m38srso2",
    "fire-ems":  "ee6si4vj",
    "general":   "iyidr3xy",
}

CATEGORY_ROOM = {
    "APD":   "apd",
    "AFD":   "fire-ems",
    "TCFD":  "fire-ems",
    "TCEMS": "fire-ems",
    "TCSO":  "apd",
    "UTPD":  "apd",
    "DPS":   "apd",
}

def _room_for_call(call: dict, priority: str) -> list:
    cat   = call.get("category", "Unknown")
    rooms = set()
    beat  = CATEGORY_ROOM.get(cat, "general")
    rooms.add(TALK_ROOMS[beat])
    if priority in ("🔴", "🟡"):
        rooms.add(TALK_ROOMS["incidents"])
    return list(rooms)

TALK_BOT_SECRET = os.environ.get("TALK_BOT_SECRET", "")

HOLD_ENABLED           = False
HOLD_RELEASE_MINUTES   = 5

INCIDENT_TIMEOUT_MINUTES = {
    "OFFICER DOWN":           30,
    "SHOOTING":               20,
    "STABBING":               15,
    "AIRCRAFT EMERGENCY":     30,
    "MASS CASUALTY":          30,
    "STRUCTURE FIRE":         20,
    "HAZMAT":                 30,
    "HOSTAGE/BARRICADE":      45,
    "CRASH/COLLISION":        15,
    "FATAL CRASH":            20,
    "FIRE DISPATCH":          20,
    "TRANSIT INCIDENT":       10,
    "AIRPORT EMERGENCY":      20,
    "MULTI-AGENCY RESPONSE":  20,
    "APD SURGE":              15,
    "AIR ASSET ACTIVE":       20,
    "DPS CAPITOL ACTIVATION": 30,
    "DEATH INVESTIGATION":    20,
    "EMS DISPATCH":           10,
}

_INCIDENT_TIMEOUT_DEFAULT = 10
MULTIAGENCY_WINDOW_MIN    = 15
APD_SURGE_WINDOW_MIN      = 10
APD_SURGE_THRESHOLD       = 4

STRIPE_SECRET_KEY     = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID       = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_PLANS = {
    "premium": {"price_id": os.environ.get("STRIPE_PRICE_ID_PREMIUM", ""), "tier": "premium"},
    "basic":   {"price_id": os.environ.get("STRIPE_PRICE_ID_BASIC",   ""), "tier": "basic"},
}
STRIPE_PRICE_TO_TIER = {v["price_id"]: v["tier"] for v in STRIPE_PLANS.values()}

# Shared mutable state — used across modules that cannot directly import each other
# audio_receiver.py writes _state["last_call_ts"]; pollers.py reads it
_state = {"last_call_ts": __import__("time").time()}
