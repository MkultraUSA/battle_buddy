-- Migration 001: initial schema baseline (2026-04-30)
-- This is the baseline extracted from production calls.db.
-- Future schema changes get their own numbered migration file.

-- Battle Buddy database schema
-- Generated from calls.db on 2026-04-30
-- Apply with: sqlite3 calls.db < schema.sql
--
-- This file is the authoritative reference for the Battle Buddy database schema.
-- All CREATE statements use IF NOT EXISTS so this script is idempotent and safe
-- to apply against an existing database.

-- Radio calls: every transcribed P25 transmission captured from the trunked
-- system, with timestamp, talkgroup, transcript, and optional geocoded location.
CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    tgid          INTEGER,
    tag           TEXT,
    category      TEXT,
    node          TEXT,
    duration      REAL,
    transcript    TEXT,
    lat           REAL,
    lon           REAL,
    location      TEXT,
    coords_approx INTEGER DEFAULT 0
);

-- Incidents: clustered events synthesized from one or more related calls,
-- representing a real-world situation (fire, pursuit, shooting, etc.).
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
    is_test     INTEGER NOT NULL DEFAULT 0,
    flagged     INTEGER NOT NULL DEFAULT 0,
    article_url TEXT
);

-- Talk room subscriptions: maps a Nextcloud user to the beats (sectors,
-- categories) they want to be notified about.
CREATE TABLE IF NOT EXISTS subscriptions (
    username TEXT NOT NULL,
    beat     TEXT NOT NULL DEFAULT 'all',
    PRIMARY KEY (username, beat)
);

-- Join table: which calls belong to which incident cluster.
CREATE TABLE IF NOT EXISTS incident_calls (
    incident_id INTEGER NOT NULL,
    call_id     INTEGER NOT NULL,
    PRIMARY KEY (incident_id, call_id)
);

-- Incident escalation log: timestamped stage transitions for an incident
-- (e.g. dispatched -> on-scene -> cleared) with optional descriptions.
CREATE TABLE IF NOT EXISTS incident_escalations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL,
    ts          REAL NOT NULL,
    stage       TEXT NOT NULL,
    description TEXT
);

-- LLM-generated guesses about what an unknown talkgroup ID represents,
-- with confidence and reasoning, used to crowdsource talkgroup identification.
CREATE TABLE IF NOT EXISTS tgid_guesses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tgid       INTEGER NOT NULL,
    ts         REAL NOT NULL,
    guess      TEXT NOT NULL,
    category   TEXT,
    confidence TEXT,
    reasoning  TEXT,
    transcript TEXT,
    confirmed  INTEGER DEFAULT 0
);

-- Booking records scraped from Travis County / APD jail feeds, deduplicated
-- by (source, case_number).
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    first_seen    REAL NOT NULL,
    source        TEXT NOT NULL,
    occurred_date TEXT,
    case_number   TEXT,
    charges       TEXT,
    arrest_type   TEXT,
    sector        TEXT,
    agency        TEXT,
    name          TEXT,
    booking_time  TEXT,
    raw_json      TEXT,
    UNIQUE(source, case_number)
);
CREATE INDEX IF NOT EXISTS bookings_occurred   ON bookings(occurred_date);
CREATE INDEX IF NOT EXISTS bookings_first_seen ON bookings(first_seen);
CREATE INDEX IF NOT EXISTS bookings_sector     ON bookings(sector);

-- Drone Remote-ID sightings ingested from the local RID receiver.
CREATE TABLE IF NOT EXISTS drone_sightings (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    serial   TEXT NOT NULL,
    ua_type  INTEGER DEFAULT 0,
    lat      REAL NOT NULL,
    lon      REAL NOT NULL,
    alt_geo  REAL,
    alt_agl  REAL,
    speed_ms REAL,
    heading  INTEGER
);

-- Crowd-sourced tips submitted by users (web form or Talk room) with optional
-- photo and reviewer workflow status.
CREATE TABLE IF NOT EXISTS tips (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    location_text TEXT,
    lat           REAL,
    lon           REAL,
    description   TEXT,
    photo_path    TEXT,
    status        TEXT DEFAULT 'pending',
    source        TEXT DEFAULT 'web',
    incident_id   INTEGER,
    reviewer_note TEXT
);

-- Premium intel-search query log for quota enforcement and audit.
CREATE TABLE IF NOT EXISTS intel_queries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT NOT NULL,
    ts         REAL NOT NULL,
    query      TEXT NOT NULL,
    result     TEXT,
    tgids_hit  TEXT,
    calls_hit  INTEGER DEFAULT 0
);

-- Premium subscriber records: Stripe linkage, intel quota, optional commute
-- watch configuration.
CREATE TABLE IF NOT EXISTS premium_users (
    username               TEXT PRIMARY KEY,
    email                  TEXT,
    stripe_customer_id     TEXT,
    stripe_subscription_id TEXT,
    status                 TEXT NOT NULL DEFAULT 'active',
    created_ts             REAL NOT NULL,
    intel_queries_used     INTEGER DEFAULT 0,
    intel_quota            INTEGER DEFAULT 5,
    commute_origin         TEXT,
    commute_origin_lat     REAL,
    commute_origin_lon     REAL,
    commute_dest           TEXT,
    commute_dest_lat       REAL,
    commute_dest_lon       REAL,
    commute_baseline_mins  INTEGER,
    commute_share_token    TEXT,
    setup_token            TEXT,
    setup_token_expires    INTEGER
);

-- Web session tokens for authenticated UI access.
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    created_ts REAL NOT NULL,
    expires_ts REAL NOT NULL,
    is_admin   INTEGER DEFAULT 0,
    is_premium INTEGER DEFAULT 0
);

-- Dedup set for APD press-release URLs already ingested by the news poller.
CREATE TABLE IF NOT EXISTS apd_seen (
    url TEXT PRIMARY KEY,
    ts  REAL NOT NULL
);

-- Reddit posts pulled from local-interest subreddits; may be promoted to
-- tips or matched to incidents.
CREATE TABLE IF NOT EXISTS reddit_intel (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL NOT NULL,
    post_id         TEXT UNIQUE,
    subreddit       TEXT,
    title           TEXT,
    url             TEXT,
    author          TEXT,
    body            TEXT,
    keywords        TEXT,
    notified        INTEGER DEFAULT 0,
    incident_id     INTEGER,
    match_score     REAL DEFAULT 0,
    tip_lat         REAL,
    tip_lon         REAL,
    tip_location    TEXT,
    tip_status      TEXT DEFAULT 'new',
    tip_ts_start    REAL,
    tip_ts_cleared  REAL,
    tip_summary     TEXT
);

-- APD CAD (Computer-Aided Dispatch) records from the public Socrata feed,
-- matched back to internal incidents when possible.
CREATE TABLE IF NOT EXISTS apd_cad (
    incident_number      TEXT PRIMARY KEY,
    response_ts          REAL,
    call_closed_ts       REAL,
    sector               TEXT,
    council_district     TEXT,
    priority_level       TEXT,
    initial_description  TEXT,
    initial_category     TEXT,
    final_description    TEXT,
    final_category       TEXT,
    mental_health_flag   TEXT,
    disposition          TEXT,
    geoid                TEXT,
    matched_incident_id  INTEGER,
    match_confidence     TEXT,
    fetched_ts           REAL
);
CREATE INDEX IF NOT EXISTS idx_apd_cad_response_ts ON apd_cad(response_ts);
CREATE INDEX IF NOT EXISTS idx_apd_cad_unmatched   ON apd_cad(matched_incident_id) WHERE matched_incident_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_apd_cad_incident_once ON apd_cad(matched_incident_id) WHERE matched_incident_id IS NOT NULL;

-- Per-talkgroup sector hint counts: which APD sector a given tgid is most
-- often associated with, used to geo-bias unknown-tgid calls.
CREATE TABLE IF NOT EXISTS tgid_sector_hints (
    tgid      INTEGER,
    sector    TEXT,
    hit_count INTEGER DEFAULT 1,
    last_seen REAL,
    PRIMARY KEY (tgid, sector)
);

-- News articles linked to incidents for press-release / corroboration display.
CREATE TABLE IF NOT EXISTS incident_articles (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER,
    ts          REAL NOT NULL,
    headline    TEXT NOT NULL,
    url         TEXT NOT NULL,
    source      TEXT,
    snippet     TEXT,
    match_score REAL DEFAULT 0
);

-- Aircraft position samples from ADS-B / dump1090, with LEO (law-enforcement
-- orbit) flag for surveillance pattern detection.
CREATE TABLE IF NOT EXISTS aircraft_positions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        REAL NOT NULL,
    icao24    TEXT NOT NULL,
    callsign  TEXT,
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    alt_ft    INTEGER,
    heading   REAL,
    speed_kts REAL,
    is_leo    INTEGER DEFAULT 0,
    label     TEXT
);
CREATE INDEX IF NOT EXISTS idx_aircraft_ts   ON aircraft_positions(ts);
CREATE INDEX IF NOT EXISTS idx_aircraft_icao ON aircraft_positions(icao24, ts);

-- Cached geocoder results to minimize external geocoding API calls.
CREATE TABLE IF NOT EXISTS geocode_cache (
    address_key TEXT PRIMARY KEY,
    lat         REAL,
    lon         REAL,
    ts_cached   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_geocode_key ON geocode_cache(address_key);
