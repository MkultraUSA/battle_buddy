#!/usr/bin/env python3
"""
Battle Buddy — SQLite Database Layer  v1.0

Schema overview
───────────────
  incidents       — one row per geocoded incident (from parser)
  transcriptions  — raw [HEARD] lines (from listener log)
  archives        — Broadcastify 30-min archive segments (premium API)
  archive_reviews — Whisper re-analysis of archive segments (enrichment)
  talkgroups      — known GATRRS talkgroup metadata (from RadioReference API)

Key relationships
─────────────────
  transcriptions.incident_id  → incidents.id   (many HEARD lines → one incident)
  archive_reviews.archive_id  → archives.id
  archive_reviews.incident_id → incidents.id   (confirm/correct via archive)
  incidents.talkgroup_id      → talkgroups.id

Usage (standalone — initialise and show stats)
──────────────────────────────────────────────
  python3 battle_buddy_db.py                    # create/migrate DB, print stats
  python3 battle_buddy_db.py --import-geojson logs/incidents.geojson
  python3 battle_buddy_db.py --stats
  python3 battle_buddy_db.py --export-geojson   # regenerate incidents.geojson from DB

Usage (as a library)
────────────────────
  from battle_buddy_db import BattleBuddyDB
  db = BattleBuddyDB()
  db.insert_incident({...})
  db.insert_heard_line({...})
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH      = "logs/battle_buddy.db"
VERSION      = "1.0"

# ---------------------------------------------------------------------------
# SCHEMA
# ---------------------------------------------------------------------------

SCHEMA = """
-- Canonical incident records (one per geocoded event)
CREATE TABLE IF NOT EXISTS incidents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),

    -- Core fields (from parser / LLM)
    timestamp       TEXT    NOT NULL,           -- YYYY-MM-DD HH:MM:SS (from radio log)
    type            TEXT    NOT NULL,           -- welfare check, collision, etc.
    address         TEXT    NOT NULL,
    severity        TEXT    NOT NULL DEFAULT 'unknown',  -- high / medium / low / unknown

    -- Geocoding
    lat             REAL,
    lon             REAL,
    geocode_source  TEXT    DEFAULT 'nominatim',

    -- Talkgroup context (FK to talkgroups table)
    talkgroup_id    INTEGER REFERENCES talkgroups(id),
    talkgroup_raw   TEXT,                       -- raw StreamTitle string at time of capture

    -- PhoneTrack
    phonetrack_device TEXT,                     -- device name used when pushed
    pushed_at       TEXT,                       -- ISO timestamp of successful push

    -- Archive enrichment (set when archive review confirms/corrects this incident)
    archive_confirmed   INTEGER DEFAULT 0,      -- 1 = confirmed by archive re-analysis
    archive_corrected   INTEGER DEFAULT 0,      -- 1 = address was corrected
    address_corrected   TEXT,                   -- corrected address (if archive_corrected=1)

    -- Stream source
    stream          TEXT    DEFAULT 'law',      -- law / fire / ems / ipn

    -- IPN (Incident Page Network) deduplication key
    ipn_id          TEXT    UNIQUE,             -- IPN incident ID (NULL for non-IPN rows)

    -- Soft delete
    deleted         INTEGER DEFAULT 0
);

-- Raw [HEARD] transcription lines (many → one incident)
CREATE TABLE IF NOT EXISTS transcriptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     INTEGER REFERENCES incidents(id),

    timestamp       TEXT    NOT NULL,
    text            TEXT    NOT NULL,
    talkgroup_raw   TEXT,
    stream          TEXT    DEFAULT 'law',
    log_file        TEXT,                       -- source log filename

    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Broadcastify 30-minute archive segments (premium API)
CREATE TABLE IF NOT EXISTS archives (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    feed_id         INTEGER NOT NULL,           -- Broadcastify feed ID (14439, 28517, etc.)
    stream          TEXT    NOT NULL,           -- law / fire / ems
    segment_start   TEXT    NOT NULL,           -- ISO timestamp of segment start
    segment_end     TEXT    NOT NULL,           -- ISO timestamp of segment end
    filename        TEXT,                       -- e.g. 202603061200-xxxx-14439.mp3
    download_url    TEXT,
    downloaded_at   TEXT,
    local_path      TEXT,                       -- path on disk once downloaded

    whisper_run     INTEGER DEFAULT 0,          -- 1 = Whisper has been run on this segment
    whisper_at      TEXT,

    UNIQUE(feed_id, segment_start)
);

-- Whisper re-analysis results for archive segments
CREATE TABLE IF NOT EXISTS archive_reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id      INTEGER NOT NULL REFERENCES archives(id),
    incident_id     INTEGER REFERENCES incidents(id),   -- NULL if new incident found in archive

    timestamp       TEXT,
    type            TEXT,
    address         TEXT,
    severity        TEXT,
    transcript_text TEXT,                       -- raw Whisper output for this segment
    match_type      TEXT,                       -- 'confirm' / 'correct' / 'new' / 'no_match'
    notes           TEXT,

    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- GATRRS talkgroup metadata (populated from RadioReference API when key arrives)
CREATE TABLE IF NOT EXISTS talkgroups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    talkgroup_id    INTEGER UNIQUE,             -- decimal talkgroup number
    alpha_tag       TEXT,                       -- short tag e.g. "TCSO ADAM-WEST"
    description     TEXT,
    mode            TEXT,                       -- D = digital, A = analog
    tag             TEXT,                       -- Law Dispatch / Fire-Tac / EMS-Tac etc.
    category        TEXT,
    system          TEXT    DEFAULT 'GATRRS',
    coverage_area   TEXT,                       -- freetext: "western Travis County"
    lat_center      REAL,                       -- approximate geographic centre
    lon_center      REAL,

    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- Seed known talkgroups (populated now — RadioReference API will fill the rest)
INSERT OR IGNORE INTO talkgroups
    (talkgroup_id, alpha_tag, description, tag, coverage_area, lat_center, lon_center)
VALUES
    (2403, 'TCSO BAKER-EAST', 'Travis Co SO Baker East',  'Law Dispatch', 'Eastern Travis County',  30.35, -97.55),
    (2405, 'TCSO ADAM-WEST',  'Travis Co SO Adam West',   'Law Dispatch', 'Western Travis County',  30.40, -97.85),
    (2551, 'TC CONSTABLE',    'Travis Co Constable',       'Law Dispatch', 'Travis County',          30.39, -97.72),
    (3522, 'DPS ATC 1',       'DPS Austin Area',           'Law Dispatch', 'State highways - Austin', 30.30, -97.75);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_incidents_timestamp   ON incidents(timestamp);
CREATE INDEX IF NOT EXISTS idx_incidents_type        ON incidents(type);
CREATE INDEX IF NOT EXISTS idx_incidents_severity    ON incidents(severity);
CREATE INDEX IF NOT EXISTS idx_incidents_stream      ON incidents(stream);
CREATE INDEX IF NOT EXISTS idx_incidents_deleted     ON incidents(deleted);
CREATE INDEX IF NOT EXISTS idx_transcriptions_ts     ON transcriptions(timestamp);
CREATE INDEX IF NOT EXISTS idx_transcriptions_inc    ON transcriptions(incident_id);
CREATE INDEX IF NOT EXISTS idx_archives_feed         ON archives(feed_id, segment_start);
CREATE INDEX IF NOT EXISTS idx_archive_reviews_inc   ON archive_reviews(incident_id);
"""

# ---------------------------------------------------------------------------
# DB CLASS
# ---------------------------------------------------------------------------

class BattleBuddyDB:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self):
        """Create tables and seed data (idempotent)."""
        self.conn.executescript(SCHEMA)
        # Add columns introduced after initial schema (ALTER TABLE is not idempotent)
        for sql in [
            "ALTER TABLE incidents ADD COLUMN ipn_id TEXT UNIQUE",
        ]:
            try:
                self.conn.execute(sql)
            except Exception:
                pass  # column already exists
        self.conn.commit()

    def close(self):
        self.conn.close()

    # ── INCIDENTS ──────────────────────────────────────────────────────────

    def insert_incident(self, inc: dict) -> int:
        """
        Insert one incident dict. Returns new row id.
        Expected keys: timestamp, type, address, severity, lat, lon,
                       talkgroup_raw (optional), phonetrack_device (optional),
                       stream (optional, default 'law')
        """
        cur = self.conn.execute(
            """
            INSERT INTO incidents
                (timestamp, type, address, severity, lat, lon,
                 talkgroup_raw, phonetrack_device, stream)
            VALUES
                (:timestamp, :type, :address, :severity, :lat, :lon,
                 :talkgroup_raw, :phonetrack_device, :stream)
            """,
            {
                "timestamp":        inc.get("timestamp", ""),
                "type":             inc.get("type", "unknown"),
                "address":          inc.get("address", ""),
                "severity":         inc.get("severity", "unknown"),
                "lat":              inc.get("lat"),
                "lon":              inc.get("lon"),
                "talkgroup_raw":    inc.get("talkgroup_raw"),
                "phonetrack_device":inc.get("phonetrack_device"),
                "stream":           inc.get("stream", "law"),
            },
        )
        self.conn.commit()
        return cur.lastrowid

    def mark_pushed(self, incident_id: int, device: str):
        """Record that an incident was successfully pushed to PhoneTrack."""
        self.conn.execute(
            "UPDATE incidents SET phonetrack_device=?, pushed_at=? WHERE id=?",
            (device, datetime.now(timezone.utc).isoformat(), incident_id),
        )
        self.conn.commit()

    def get_unpushed(self, stream: str = None) -> list[sqlite3.Row]:
        """Return geocoded incidents not yet pushed to PhoneTrack."""
        q = "SELECT * FROM incidents WHERE pushed_at IS NULL AND lat IS NOT NULL AND deleted=0"
        params = []
        if stream:
            q += " AND stream=?"
            params.append(stream)
        return self.conn.execute(q, params).fetchall()

    def get_incidents(
        self,
        since: str = None,
        inc_type: str = None,
        severity: str = None,
        stream: str = None,
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        """Flexible incident query. All filters optional."""
        clauses = ["deleted=0", "lat IS NOT NULL"]
        params  = []
        if since:
            clauses.append("timestamp >= ?")
            params.append(since)
        if inc_type:
            clauses.append("type LIKE ?")
            params.append(f"%{inc_type}%")
        if severity:
            clauses.append("severity=?")
            params.append(severity)
        if stream:
            clauses.append("stream=?")
            params.append(stream)
        q = f"SELECT * FROM incidents WHERE {' AND '.join(clauses)} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return self.conn.execute(q, params).fetchall()

    # ── TRANSCRIPTIONS ─────────────────────────────────────────────────────

    def insert_heard_line(self, line: dict, incident_id: int = None) -> int:
        """
        Insert one [HEARD] line.
        Expected keys: timestamp, text, talkgroup_raw (opt), stream (opt), log_file (opt)
        """
        cur = self.conn.execute(
            """
            INSERT INTO transcriptions
                (incident_id, timestamp, text, talkgroup_raw, stream, log_file)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                incident_id,
                line.get("timestamp", ""),
                line.get("text", ""),
                line.get("talkgroup_raw"),
                line.get("stream", "law"),
                line.get("log_file"),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    # ── ARCHIVES ───────────────────────────────────────────────────────────

    def upsert_archive(self, arc: dict) -> int:
        """
        Insert or update an archive segment record.
        Expected keys: feed_id, stream, segment_start, segment_end, filename,
                       download_url (opt), local_path (opt)
        """
        cur = self.conn.execute(
            """
            INSERT INTO archives (feed_id, stream, segment_start, segment_end,
                                  filename, download_url, local_path)
            VALUES (:feed_id, :stream, :segment_start, :segment_end,
                    :filename, :download_url, :local_path)
            ON CONFLICT(feed_id, segment_start) DO UPDATE SET
                download_url = excluded.download_url,
                local_path   = COALESCE(excluded.local_path, local_path)
            """,
            {
                "feed_id":       arc["feed_id"],
                "stream":        arc.get("stream", "law"),
                "segment_start": arc["segment_start"],
                "segment_end":   arc["segment_end"],
                "filename":      arc.get("filename"),
                "download_url":  arc.get("download_url"),
                "local_path":    arc.get("local_path"),
            },
        )
        self.conn.commit()
        return cur.lastrowid

    def get_unwhispered_archives(self, stream: str = None) -> list[sqlite3.Row]:
        """Return downloaded archives not yet re-analysed by Whisper."""
        q = "SELECT * FROM archives WHERE local_path IS NOT NULL AND whisper_run=0"
        params = []
        if stream:
            q += " AND stream=?"
            params.append(stream)
        return self.conn.execute(q, params).fetchall()

    # ── ARCHIVE REVIEWS ────────────────────────────────────────────────────

    def insert_archive_review(self, review: dict) -> int:
        """
        Link an archive segment analysis result to an incident.
        Expected keys: archive_id, incident_id (opt), timestamp (opt), type (opt),
                       address (opt), match_type, transcript_text (opt), notes (opt)
        """
        cur = self.conn.execute(
            """
            INSERT INTO archive_reviews
                (archive_id, incident_id, timestamp, type, address,
                 severity, transcript_text, match_type, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review["archive_id"],
                review.get("incident_id"),
                review.get("timestamp"),
                review.get("type"),
                review.get("address"),
                review.get("severity"),
                review.get("transcript_text"),
                review.get("match_type", "no_match"),
                review.get("notes"),
            ),
        )
        # Update the parent incident if we have a confirmation/correction
        if review.get("incident_id"):
            if review.get("match_type") == "confirm":
                self.conn.execute(
                    "UPDATE incidents SET archive_confirmed=1 WHERE id=?",
                    (review["incident_id"],),
                )
            elif review.get("match_type") == "correct" and review.get("address"):
                self.conn.execute(
                    """UPDATE incidents
                       SET archive_confirmed=1, archive_corrected=1,
                           address_corrected=?
                       WHERE id=?""",
                    (review["address"], review["incident_id"]),
                )
        self.conn.commit()
        return cur.lastrowid

    # ── TALKGROUPS ─────────────────────────────────────────────────────────

    def upsert_talkgroup(self, tg: dict) -> int:
        """Insert or update a talkgroup from RadioReference API data."""
        cur = self.conn.execute(
            """
            INSERT INTO talkgroups
                (talkgroup_id, alpha_tag, description, mode, tag,
                 category, coverage_area, lat_center, lon_center, updated_at)
            VALUES
                (:talkgroup_id, :alpha_tag, :description, :mode, :tag,
                 :category, :coverage_area, :lat_center, :lon_center,
                 strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            ON CONFLICT(talkgroup_id) DO UPDATE SET
                alpha_tag     = excluded.alpha_tag,
                description   = excluded.description,
                mode          = excluded.mode,
                tag           = excluded.tag,
                category      = excluded.category,
                coverage_area = COALESCE(excluded.coverage_area, coverage_area),
                lat_center    = COALESCE(excluded.lat_center, lat_center),
                lon_center    = COALESCE(excluded.lon_center, lon_center),
                updated_at    = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            {
                "talkgroup_id":  tg.get("talkgroup_id") or tg.get("decimal"),
                "alpha_tag":     tg.get("alpha_tag") or tg.get("alphaTag"),
                "description":   tg.get("description"),
                "mode":          tg.get("mode"),
                "tag":           tg.get("tag"),
                "category":      tg.get("category"),
                "coverage_area": tg.get("coverage_area"),
                "lat_center":    tg.get("lat_center"),
                "lon_center":    tg.get("lon_center"),
            },
        )
        self.conn.commit()
        return cur.lastrowid

    def lookup_talkgroup(self, raw_title: str) -> sqlite3.Row | None:
        """Try to match a StreamTitle string to a known talkgroup."""
        if not raw_title:
            return None
        upper = raw_title.upper().strip()
        return self.conn.execute(
            "SELECT * FROM talkgroups WHERE UPPER(alpha_tag)=? OR UPPER(description) LIKE ?",
            (upper, f"%{upper}%"),
        ).fetchone()

    # ── STATS ──────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        """Return a summary dict for display."""
        def scalar(q, *a):
            return self.conn.execute(q, a).fetchone()[0] or 0

        return {
            "incidents":           scalar("SELECT COUNT(*) FROM incidents WHERE deleted=0"),
            "geocoded":            scalar("SELECT COUNT(*) FROM incidents WHERE lat IS NOT NULL AND deleted=0"),
            "pushed":              scalar("SELECT COUNT(*) FROM incidents WHERE pushed_at IS NOT NULL AND deleted=0"),
            "archive_confirmed":   scalar("SELECT COUNT(*) FROM incidents WHERE archive_confirmed=1"),
            "archive_corrected":   scalar("SELECT COUNT(*) FROM incidents WHERE archive_corrected=1"),
            "transcriptions":      scalar("SELECT COUNT(*) FROM transcriptions"),
            "archives_known":      scalar("SELECT COUNT(*) FROM archives"),
            "archives_downloaded": scalar("SELECT COUNT(*) FROM archives WHERE local_path IS NOT NULL"),
            "archives_whispered":  scalar("SELECT COUNT(*) FROM archives WHERE whisper_run=1"),
            "talkgroups":          scalar("SELECT COUNT(*) FROM talkgroups"),
            "by_type": {
                row[0]: row[1] for row in self.conn.execute(
                    "SELECT type, COUNT(*) FROM incidents WHERE deleted=0 GROUP BY type ORDER BY 2 DESC"
                ).fetchall()
            },
            "by_severity": {
                row[0]: row[1] for row in self.conn.execute(
                    "SELECT severity, COUNT(*) FROM incidents WHERE deleted=0 GROUP BY severity"
                ).fetchall()
            },
            "by_stream": {
                row[0]: row[1] for row in self.conn.execute(
                    "SELECT stream, COUNT(*) FROM incidents WHERE deleted=0 GROUP BY stream"
                ).fetchall()
            },
        }

    # ── GEOJSON EXPORT ─────────────────────────────────────────────────────

    def export_geojson(self, out_path: str = "logs/incidents.geojson", **filters):
        """Export all geocoded incidents to GeoJSON (re-generates from DB)."""
        SEVERITY_COLORS = {
            "high": "#e63946", "medium": "#f4a261",
            "low": "#2a9d8f", "unknown": "#adb5bd",
        }
        INCIDENT_ICONS = {
            "welfare check": "🏥", "collision": "🚗", "accident": "🚗",
            "suspicious": "🚔", "arrest": "🚔", "warrant": "🚔",
            "disturbance": "⚠️", "fight": "⚠️", "fire": "🔥",
            "medical": "🏥", "mental health": "🏥", "theft": "🚔",
            "burglary": "🚔", "trespass": "🚔", "panic alarm": "🚨",
            "pursuit": "🚔",
        }

        incidents = self.get_incidents(**filters)
        features = []
        for inc in incidents:
            inc = dict(inc)
            sev   = (inc.get("severity") or "unknown").lower()
            itype = (inc.get("type") or "").lower()
            color = SEVERITY_COLORS.get(sev, SEVERITY_COLORS["unknown"])
            icon  = next((v for k, v in INCIDENT_ICONS.items() if k in itype), "⚠️")
            addr  = inc.get("address_corrected") or inc.get("address", "")

            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [inc["lon"], inc["lat"]]},
                "properties": {
                    "title":        f"{icon} {inc['type']} — {sev}",
                    "description": (
                        f"<b>Address:</b> {addr}<br>"
                        f"<b>Time:</b> {inc['timestamp']}<br>"
                        f"<b>Severity:</b> {sev}<br>"
                        f"<b>Stream:</b> {inc.get('stream','?')}<br>"
                        + (f"<b>Talkgroup:</b> {inc['talkgroup_raw']}<br>" if inc.get("talkgroup_raw") else "")
                        + ("✅ Archive confirmed<br>" if inc.get("archive_confirmed") else "")
                        + ("📝 Address corrected by archive<br>" if inc.get("archive_corrected") else "")
                    ),
                    "timestamp":         inc["timestamp"],
                    "type":              inc["type"],
                    "severity":          sev,
                    "address":           addr,
                    "stream":            inc.get("stream"),
                    "talkgroup":         inc.get("talkgroup_raw"),
                    "archive_confirmed": bool(inc.get("archive_confirmed")),
                    "marker-color":      color,
                    "marker-size":       "large" if sev == "high" else "medium",
                },
            })

        geojson = {
            "type": "FeatureCollection",
            "name": f"Battle Buddy DB Export — {datetime.now(timezone.utc).isoformat()}",
            "features": features,
        }
        Path(out_path).write_text(json.dumps(geojson, indent=2, ensure_ascii=False))
        print(f"[db→geojson] {len(features)} incidents → {out_path}")

    # ── GEOJSON IMPORT ─────────────────────────────────────────────────────

    def import_geojson(self, path: str) -> int:
        """Bulk-import incidents from an existing GeoJSON file. Returns count inserted."""
        data = json.loads(Path(path).read_text())
        count = 0
        for feat in data.get("features", []):
            props = feat.get("properties", {})
            coords = feat.get("geometry", {}).get("coordinates", [None, None])
            inc = {
                "timestamp": props.get("timestamp", ""),
                "type":      props.get("type", "unknown"),
                "address":   props.get("address", ""),
                "severity":  props.get("severity", "unknown"),
                "lon":       coords[0],
                "lat":       coords[1],
                "stream":    props.get("stream", "law"),
                "talkgroup_raw": props.get("talkgroup"),
            }
            try:
                self.insert_incident(inc)
                count += 1
            except sqlite3.IntegrityError:
                pass   # skip duplicates
        print(f"[import] {count} incidents imported from {path}")
        return count


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def print_stats(db: BattleBuddyDB):
    s = db.stats()
    print(f"\n{'─'*50}")
    print(f"  Battle Buddy DB  v{VERSION}  →  {db.db_path}")
    print(f"{'─'*50}")
    print(f"  Incidents total  : {s['incidents']}")
    print(f"    geocoded       : {s['geocoded']}")
    print(f"    pushed         : {s['pushed']}")
    print(f"    archive ✓      : {s['archive_confirmed']}")
    print(f"    addr corrected : {s['archive_corrected']}")
    print(f"  Transcriptions   : {s['transcriptions']}")
    print(f"  Archives known   : {s['archives_known']}")
    print(f"    downloaded     : {s['archives_downloaded']}")
    print(f"    re-whispered   : {s['archives_whispered']}")
    print(f"  Talkgroups       : {s['talkgroups']}")

    if s["by_type"]:
        print(f"\n  By type:")
        for t, n in s["by_type"].items():
            print(f"    {t:28s} {n}")

    if s["by_severity"]:
        print(f"\n  By severity:")
        for sev, n in s["by_severity"].items():
            print(f"    {sev:10s} {n}")

    if s["by_stream"]:
        print(f"\n  By stream:")
        for st, n in s["by_stream"].items():
            print(f"    {st:10s} {n}")

    print(f"{'─'*50}\n")


def main():
    p = argparse.ArgumentParser(
        description=f"Battle Buddy DB v{VERSION} — SQLite database manager"
    )
    p.add_argument("--db",             default=DB_PATH,                  help="Database path")
    p.add_argument("--stats",          action="store_true",              help="Print database statistics")
    p.add_argument("--import-geojson", metavar="FILE",                   help="Import incidents from GeoJSON file")
    p.add_argument("--export-geojson", metavar="FILE", nargs="?",
                   const="logs/incidents.geojson",                       help="Export incidents to GeoJSON")
    p.add_argument("--since",          metavar="YYYY-MM-DD",             help="Filter export to this date or later")
    args = p.parse_args()

    db = BattleBuddyDB(args.db)

    if args.import_geojson:
        db.import_geojson(args.import_geojson)

    if args.export_geojson:
        filters = {}
        if args.since:
            filters["since"] = args.since
        db.export_geojson(args.export_geojson, **filters)

    print_stats(db)
    db.close()


if __name__ == "__main__":
    main()
