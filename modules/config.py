import os

try:
    import anthropic as _anthropic
except ImportError:
    _anthropic = None

# ---------------------------------------------------------------------------
# Paths and deployment defaults
# ---------------------------------------------------------------------------
# Keep the existing reference deployment defaults, but allow every deployment-
# specific value to be overridden through environment variables. This keeps the
# current install working while making the repository safer to publish and easier
# for other users to adapt.

BATTLE_BUDDY_HOME = os.environ.get("BATTLE_BUDDY_HOME", "/opt/battlebuddy")
BATTLE_BUDDY_LOG_DIR = os.environ.get("BATTLE_BUDDY_LOG_DIR", "/var/log/battlebuddy")
BATTLE_BUDDY_DATA_DIR = os.environ.get("BATTLE_BUDDY_DATA_DIR", BATTLE_BUDDY_HOME)
BATTLE_BUDDY_WEB_DIR = os.environ.get("BATTLE_BUDDY_WEB_DIR", os.path.join(BATTLE_BUDDY_HOME, "static"))

DB_PATH = os.environ.get("DB_PATH", os.path.join(BATTLE_BUDDY_HOME, "calls.db"))
TIPS_UPLOAD_DIR = os.environ.get("TIPS_UPLOAD_DIR", os.path.join(BATTLE_BUDDY_HOME, "static", "tips"))
TGID_TSV = os.environ.get("TGID_TSV", os.path.join(BATTLE_BUDDY_HOME, "gatrrs-tags.tsv"))
PI1_OP25_URL = os.environ.get("PI1_OP25_URL", "http://radio-node.example.local:8080/")

# ---------------------------------------------------------------------------
# LLM providers
# ---------------------------------------------------------------------------

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OPENROUTER_ENABLED = bool(OPENROUTER_API_KEY)
OPENROUTER_API_BASE = os.environ.get("OPENROUTER_API_BASE", "https://openrouter.ai/api/v1")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_ENABLED = bool(ANTHROPIC_API_KEY) and (_anthropic is not None)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_API_BASE = os.environ.get("GROQ_API_BASE", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_ENABLED = bool(GROQ_API_KEY)

# ---------------------------------------------------------------------------
# Nextcloud / Talk / Deck
# ---------------------------------------------------------------------------

TALK_BASE = os.environ.get("TALK_BASE", "https://kevcloud.ddns.net/ocs/v2.php/apps/spreed/api/v1")
TALK_USER = os.environ.get("TALK_USER", "battlebuddy")
TALK_PASS = os.environ.get("TALK_PASS", "")
TALK_ENABLED = os.environ.get("TALK_ENABLED", "true").lower() in ("1", "true", "yes", "on")
TALK_BOT_SECRET = os.environ.get("TALK_BOT_SECRET", "")

DECK_BASE = os.environ.get("DECK_BASE", "https://kevcloud.ddns.net/index.php/apps/deck/api/v1.0")
NC_WEBDAV = os.environ.get("NC_WEBDAV", "https://kevcloud.ddns.net/remote.php/dav/files/kevin")
NC_USER = os.environ.get("NC_USER", "")
NC_PASS = os.environ.get("NC_PASS", "")
NC_REPORT_DIR = os.environ.get("NC_REPORT_DIR", "PresentationNotes/FlaggedIncidents")

DECK_BOARD_ID = int(os.environ.get("DECK_BOARD_ID", "2"))
DECK_STACK_NEW = int(os.environ.get("DECK_STACK_NEW", "5"))
DECK_LABELS = {
    "SHOOTING":               int(os.environ.get("DECK_LABEL_SHOOTING", "10")),
    "OFFICER DOWN":           int(os.environ.get("DECK_LABEL_OFFICER_DOWN", "11")),
    "STRUCTURE FIRE":         int(os.environ.get("DECK_LABEL_STRUCTURE_FIRE", "12")),
    "MASS CASUALTY":          int(os.environ.get("DECK_LABEL_MASS_CASUALTY", "13")),
    "HAZMAT":                 int(os.environ.get("DECK_LABEL_HAZMAT", "14")),
    "CRASH/COLLISION":        int(os.environ.get("DECK_LABEL_CRASH_COLLISION", "15")),
    "MULTI-AGENCY RESPONSE":  int(os.environ.get("DECK_LABEL_MULTI_AGENCY_RESPONSE", "16")),
    "AIR ASSET ACTIVE":       int(os.environ.get("DECK_LABEL_AIR_ASSET_ACTIVE", "17")),
    "DPS CAPITOL ACTIVATION": int(os.environ.get("DECK_LABEL_DPS_CAPITOL_ACTIVATION", "18")),
}

TALK_ROOM = os.environ.get("TALK_ROOM", "")
TALK_ROOMS = {
    "incidents": os.environ.get("TALK_ROOM_INCIDENTS", ""),
    "apd":       os.environ.get("TALK_ROOM_APD", ""),
    "fire-ems":  os.environ.get("TALK_ROOM_FIRE_EMS", ""),
    "general":   os.environ.get("TALK_ROOM_GENERAL", TALK_ROOM),
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

# ---------------------------------------------------------------------------
# Mail, maps, TAK, and optional external services
# ---------------------------------------------------------------------------

MAILGUN_API_KEY = os.environ.get("MAILGUN_API_KEY", "")
MAILGUN_DOMAIN = os.environ.get("MAILGUN_DOMAIN", "")
MAILGUN_FROM = os.environ.get("MAILGUN_FROM", f"Battle Buddy <mailgun@{MAILGUN_DOMAIN}>") if MAILGUN_DOMAIN else "Battle Buddy <alerts@example.com>"
ALERT_EMAIL = os.environ.get("ALERT_EMAIL", "alerts@example.com")

GOOGLE_CSE_API_KEY = os.environ.get("GOOGLE_CSE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")
GOOGLE_ROUTES_KEY = os.environ.get("GOOGLE_ROUTES_KEY", "")
GOOGLE_MAPS_JS_KEY = os.environ.get("GOOGLE_MAPS_JS_KEY", "")

PI_FETCH_URL = os.environ.get("PI_FETCH_URL", "").rstrip("/")
PI_FETCH_TOKEN = os.environ.get("PI_FETCH_TOKEN", "")
PI_FETCH_ENABLED = bool(PI_FETCH_URL and PI_FETCH_TOKEN)

FTS_HOST = os.environ.get("FTS_HOST", "radiodesk.ddns.net")
FTS_REST_PORT = int(os.environ.get("FTS_REST_PORT", "19023"))
FTS_COT_PORT = int(os.environ.get("FTS_COT_PORT", "8089"))
FTS_TOKEN = os.environ.get("FTS_TOKEN", "")
FTS_ENABLED = os.environ.get("FTS_ENABLED", "false").lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Incident behavior
# ---------------------------------------------------------------------------

HOLD_ENABLED = os.environ.get("HOLD_ENABLED", "false").lower() in ("1", "true", "yes", "on")
HOLD_RELEASE_MINUTES = int(os.environ.get("HOLD_RELEASE_MINUTES", "5"))

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

_INCIDENT_TIMEOUT_DEFAULT = int(os.environ.get("INCIDENT_TIMEOUT_DEFAULT", "10"))
MULTIAGENCY_WINDOW_MIN = int(os.environ.get("MULTIAGENCY_WINDOW_MIN", "15"))
APD_SURGE_WINDOW_MIN = int(os.environ.get("APD_SURGE_WINDOW_MIN", "10"))
APD_SURGE_THRESHOLD = int(os.environ.get("APD_SURGE_THRESHOLD", "4"))

# ---------------------------------------------------------------------------
# Stripe, optional
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "")
STRIPE_PLANS = {
    "premium": {"price_id": os.environ.get("STRIPE_PRICE_ID_PREMIUM", ""), "tier": "premium"},
    "basic":   {"price_id": os.environ.get("STRIPE_PRICE_ID_BASIC",   ""), "tier": "basic"},
}
STRIPE_PRICE_TO_TIER = {v["price_id"]: v["tier"] for v in STRIPE_PLANS.values() if v["price_id"]}

# Shared mutable state — used across modules that cannot directly import each other.
# audio_receiver.py writes _state["last_call_ts"]; pollers.py reads it.
_state = {"last_call_ts": __import__("time").time()}
