import json
import sqlite3
import time

from modules.config import _INCIDENT_TIMEOUT_DEFAULT, DB_PATH, INCIDENT_TIMEOUT_MINUTES
from modules.talkgroups import CAT_COORDS


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            tgid        INTEGER,
            tag         TEXT,
            category    TEXT,
            node        TEXT,
            duration    REAL,
            transcript  TEXT,
            lat         REAL,
            lon         REAL,
            location    TEXT,
            is_test     INTEGER DEFAULT 0
        )
    """)
    conn.execute("ALTER TABLE calls ADD COLUMN coords_approx INTEGER DEFAULT 0") if False else None
    try:
        # is_test marks synthetic injections so they can be excluded from
        # quality metrics and from the incident map. /test_call writes rows with
        # node='test', but node is caller-influenced and nothing filtered on it,
        # so a test call was indistinguishable from a real dispatch in every
        # aggregate. incidents.is_test already existed (added by hand -- see
        # init_db) and is excluded by a dozen queries; calls had nothing.
        try:
            conn.execute("ALTER TABLE calls ADD COLUMN is_test INTEGER DEFAULT 0")
        except Exception:
            pass
        conn.execute("ALTER TABLE calls ADD COLUMN coords_approx INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE calls ADD COLUMN accuracy REAL")
    except Exception:
        pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_start    REAL NOT NULL,
            ts_updated  REAL NOT NULL,
            ts_cleared  REAL,
            itype       TEXT,
            description TEXT,
            agencies    TEXT,
            tgids       TEXT,
            location    TEXT,
            lat         REAL,
            lon         REAL,
            status      TEXT DEFAULT 'active',
            is_test     INTEGER DEFAULT 0,
            flagged     INTEGER DEFAULT 0
        )
    """)
    # is_test and flagged are read and written by audio_receiver but were never
    # created here, so they exist only where someone ran the ALTER by hand. A
    # from-scratch database therefore had no such columns: /metrics aborted with
    # "no such column: is_test" and returned an EMPTY body under HTTP 200, so
    # every Grafana panel went blank and the ops_verify metric gates went blind
    # with no error anywhere; and `UPDATE incidents SET flagged=1` -- the flag
    # endpoint -- raised a 500. Production survived only by accident.
    #
    # Idempotent, because the table already exists on every real deployment.
    for _col, _decl in (("is_test", "INTEGER DEFAULT 0"),
                        ("flagged", "INTEGER DEFAULT 0")):
        try:
            conn.execute(f"ALTER TABLE incidents ADD COLUMN {_col} {_decl}")
        except Exception:
            pass
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            username    TEXT    NOT NULL,
            beat        TEXT    NOT NULL DEFAULT 'all',
            PRIMARY KEY (username, beat)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_calls (
            incident_id INTEGER NOT NULL,
            call_id     INTEGER NOT NULL,
            PRIMARY KEY (incident_id, call_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_escalations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER NOT NULL,
            ts          REAL    NOT NULL,
            stage       TEXT    NOT NULL,
            description TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tgid_guesses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tgid        INTEGER NOT NULL,
            ts          REAL    NOT NULL,
            guess       TEXT    NOT NULL,
            category    TEXT,
            confidence  TEXT,
            reasoning   TEXT,
            transcript  TEXT,
            confirmed   INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS drone_sightings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            serial      TEXT    NOT NULL,
            ua_type     INTEGER DEFAULT 0,
            lat         REAL    NOT NULL,
            lon         REAL    NOT NULL,
            alt_geo     REAL,
            alt_agl     REAL,
            speed_ms    REAL,
            heading     INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tips (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL NOT NULL,
            location_text   TEXT,
            lat             REAL,
            lon             REAL,
            description     TEXT,
            photo_path      TEXT,
            status          TEXT DEFAULT 'pending',
            source          TEXT DEFAULT 'web',
            incident_id     INTEGER,
            reviewer_note   TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tip_audit (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_username  TEXT NOT NULL,
            tip_id          INTEGER NOT NULL,
            action          TEXT NOT NULL,
            ts              REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS incident_articles (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            incident_id INTEGER,
            ts          REAL NOT NULL,
            headline    TEXT NOT NULL,
            url         TEXT NOT NULL,
            source      TEXT,
            snippet     TEXT,
            match_score REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS aircraft_positions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL    NOT NULL,
            icao24      TEXT    NOT NULL,
            callsign    TEXT,
            lat         REAL    NOT NULL,
            lon         REAL    NOT NULL,
            alt_ft      INTEGER,
            heading     REAL,
            speed_kts   REAL,
            is_leo      INTEGER DEFAULT 0,
            label       TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS premium_checkout_intents (
            intent_id         TEXT PRIMARY KEY,
            username          TEXT NOT NULL,
            display_name      TEXT,
            nc_password       TEXT NOT NULL,
            tier              TEXT,
            plan              TEXT,
            created_ts        REAL NOT NULL,
            expires_ts        REAL NOT NULL,
            consumed_ts       REAL,
            stripe_session_id TEXT
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_checkout_intents_username
            ON premium_checkout_intents(username)
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stripe_processed_events (
            event_id TEXT PRIMARY KEY,
            ts       REAL NOT NULL,
            type     TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_ts   ON aircraft_positions(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_icao ON aircraft_positions(icao24, ts)")
    conn.commit()
    conn.close()


def get_subscribers(itype: str, category: str) -> list:
    beat_map = {
        "APD": "apd", "TCSO": "apd", "UTPD": "apd", "DPS": "apd",
        "AFD": "fire-ems", "TCFD": "fire-ems", "TCEMS": "fire-ems",
    }
    beat = beat_map.get(category, "general")
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    rows = conn.execute(
        "SELECT DISTINCT username FROM subscriptions WHERE beat='all' OR beat=?", (beat,)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def add_subscription(username: str, beat: str = "all"):
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("INSERT OR IGNORE INTO subscriptions (username, beat) VALUES (?,?)", (username, beat))
    conn.commit()
    conn.close()


def remove_subscription(username: str, beat: str = "all"):
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.execute("DELETE FROM subscriptions WHERE username=? AND beat=?", (username, beat))
    conn.commit()
    conn.close()


def insert_call(ts, tgid, tag, category, node, duration, transcript, lat, lon, location,
               coords_approx=0, accuracy=None, is_test=0) -> int:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    cur  = conn.execute(
        "INSERT INTO calls (ts,tgid,tag,category,node,duration,transcript,lat,lon,"
        "location,coords_approx,accuracy,is_test) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, tgid, tag, category, node, duration, transcript, lat, lon, location,
         coords_approx, accuracy, 1 if is_test else 0)
    )
    row_id = cur.lastrowid
    conn.commit()
    conn.close()
    return row_id


def recent_calls(limit=200):
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM calls WHERE (is_test IS NULL OR is_test = 0) "
        "ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def calls_since(since_ts: float) -> list:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM calls WHERE ts > ? AND (is_test IS NULL OR is_test = 0) "
        "ORDER BY ts DESC", (since_ts,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def calls_for_sitrep(minutes=60):
    return calls_since(time.time() - minutes * 60)


def _fill_incident_coords(inc: dict) -> dict:
    if inc.get("lat") is None or inc.get("lon") is None:
        try:
            agencies = json.loads(inc.get("agencies") or "[]")
            cat = agencies[0] if agencies else "Unknown"
        except Exception:
            cat = "Unknown"
        lat, lon = CAT_COORDS.get(cat, CAT_COORDS["Unknown"])
        inc["lat"] = lat
        inc["lon"] = lon
        inc["_coords_approx"] = True
    return inc


# Legacy flat window, retained for compatibility only. The published
# population no longer uses it; see ACTIVE_INCIDENT_POPULATION_SQL below,
# which evaluates the staleness window per row from the row itype.
ACTIVE_INCIDENT_WINDOW_S = 30 * 60


def _active_incident_timeout_seconds_sql() -> str:
    """SQL CASE mapping each itype to its engine timeout in seconds.

    Mirrors modules.config.INCIDENT_TIMEOUT_MINUTES with fallback to
    modules.config._INCIDENT_TIMEOUT_DEFAULT, so the published population
    never excludes a row the incident engine still considers active.
    """
    whens = " ".join(
        f"WHEN '{itype.replace(chr(39), chr(39) * 2)}' "
        f"THEN {int(INCIDENT_TIMEOUT_MINUTES[itype] * 60)}"
        for itype in sorted(INCIDENT_TIMEOUT_MINUTES)
    )
    return f"(CASE itype {whens} ELSE {int(_INCIDENT_TIMEOUT_DEFAULT * 60)} END)"


# Per-row staleness budget in seconds, derived from the same map the incident
# engine's cleanup thread reads. Single definition so the two mechanisms
# cannot drift apart again.
ACTIVE_INCIDENT_TIMEOUT_S_SQL = _active_incident_timeout_seconds_sql()

# The published active population, as one SQL fragment with one trailing
# placeholder (the current time, in the same epoch-seconds domain as
# ts_updated). This is the single definition of "an incident is active right
# now, publicly":
#
#   * public_active_incidents() appends it to the query behind
#     /api/incidents/active, which is what the live map counts and pins;
#   * the Prometheus collector in audio_receiver.py appends the same fragment to
#     the single statement behind battlebuddy_active_incidents,
#     battlebuddy_active_incidents_unlocated and
#     battlebuddy_active_incidents_out_of_scope.
#
# Because both sides read the same fragment, the number a reader sees on the map
# and the number an operator sees in /metrics are the same measurement of the
# same rows, not two queries that happen to look alike.
#
#   - status='active'    the incident is open;
#   - is_test            test rows are never published, so they are not counted;
#   - press release      "[APD Press Release]" rows are aggregate press
#                        summaries of many incidents, not incidents, and were
#                        already excluded from the gauges;
#   - ts_updated window  per-row staleness: a row is kept while its age
#                        (? - ts_updated) is within its own per-type timeout
#                        from INCIDENT_TIMEOUT_MINUTES (default
#                        _INCIDENT_TIMEOUT_DEFAULT). This is >= the timeout the
#                        engine's cleanup thread uses, so the public product
#                        never drops a row the engine still considers active.
#
# Do not add a second definition of this filter. If something needs the
# operational view (every active row, test and press-release rows included) it
# should call active_incidents() below instead.
ACTIVE_INCIDENT_POPULATION_SQL = (
    "status = 'active' "
    "AND (is_test IS NULL OR is_test = 0) "
    "AND (description IS NULL OR description NOT LIKE '%[APD Press Release]%') "
    f"AND ts_updated >= (? - {ACTIVE_INCIDENT_TIMEOUT_S_SQL})"
)


def active_incidents() -> list:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    cutoff = time.time() - 30 * 60
    rows = conn.execute(
        "SELECT * FROM incidents WHERE status='active' AND ts_updated > ? ORDER BY ts_updated DESC",
        (cutoff,)
    ).fetchall()
    conn.close()
    return [_fill_incident_coords(dict(r)) for r in rows]


def public_active_incidents() -> list:
    """The active incidents the public live map is served, and counted from.

    Same rows the exported active gauges measure: see
    ACTIVE_INCIDENT_POPULATION_SQL. The single placeholder in that fragment is
    the current time; the per-row timeout is derived inside SQL from the row
    itype. Rows come back with the agency-HQ fallback coordinates applied,
    exactly as before, so the page keeps the ``_coords_approx`` stamp that
    keeps a fallback centroid off the map.
    """
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM incidents WHERE {ACTIVE_INCIDENT_POPULATION_SQL} "
        "ORDER BY ts_updated DESC",
        (time.time(),),
    ).fetchall()
    conn.close()
    return [_fill_incident_coords(dict(r)) for r in rows]


def get_all_incidents(limit=50) -> list:
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM incidents ORDER BY ts_start DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [_fill_incident_coords(dict(r)) for r in rows]
