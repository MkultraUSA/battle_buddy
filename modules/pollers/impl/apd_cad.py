"""
modules/pollers/impl/apd_cad.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Austin PD CAD retrospective enrichment poller.

Migrated from modules/pollers_legacy.py as part of the BasePoller refactor.
The poller fetches lagged APD CAD records, enriches matching scanner
incidents, and harvests TGID-to-sector hints for unknown talkgroups.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from modules.pollers.base import BasePoller

logger = logging.getLogger("APDCADPoller")

APD_CAD_URL = (
    "https://data.austintexas.gov/resource/22de-7rzg.json"
    "?$where=response_datetime>{lookback}"
    "&$order=response_datetime+DESC"
    "&$limit=5000"
)
APD_CAD_POLL_INTERVAL: float = 6 * 3600
APD_CAD_LOOKBACK_DAYS = 21

_CAD_CATEGORY_MAP = {
    "Shoot/Stab": "SHOOTING",
    "Homicide": "SHOOTING",
    "Aggravated Assault": "STABBING",
    "Weapons/Firearms Violations": "WEAPONS",
    "Robbery": "WEAPONS",
    "Bomb/Explosives": "EXPLOSION",
    "Arson": "STRUCTURE FIRE",
    "Crashes": "CRASH/COLLISION",
    "Traffic Stop/Hazard": "CRASH/COLLISION",
    "DUI/DWI": "CRASH/COLLISION",
    "Evading/Resisting Arrest": "PURSUIT",
}

_CAD_HARVEST_CATEGORIES = {
    "Shoot/Stab",
    "Homicide",
    "Aggravated Assault",
    "Weapons/Firearms Violations",
    "Robbery",
    "Bomb/Explosives",
    "Arson",
    "Crashes",
    "Evading/Resisting Arrest",
}


def _parse_cad_ts(dt_str: str | None) -> float | None:
    if not dt_str:
        return None
    try:
        return (
            datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")
            .replace(tzinfo=None)
            .timestamp()
            - time.timezone
        )
    except Exception:
        return None


class APDCADPoller(BasePoller):
    """Poll APD CAD records every six hours and enrich matching incidents."""

    NAME: str = "apd-cad"
    INTERVAL: float = APD_CAD_POLL_INTERVAL

    def __init__(self, db_path: str | None = None) -> None:
        super().__init__(interval=self.INTERVAL)
        if db_path is None:
            from modules.config import DB_PATH  # noqa: PLC0415

            db_path = DB_PATH
        self.db_path = db_path
        self._db_ready = False

    def run(self) -> None:
        if not self._db_ready:
            self.init_db()
            self._db_ready = True
        self.fetch_and_store()
        self.match_and_harvest()

    def init_db(self) -> None:
        """Create APD CAD and TGID hint tables if they do not exist."""
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
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
            CREATE TABLE IF NOT EXISTS tgid_sector_hints (
                tgid        INTEGER,
                sector      TEXT,
                hit_count   INTEGER DEFAULT 1,
                last_seen   REAL,
                PRIMARY KEY (tgid, sector)
            );
            CREATE INDEX IF NOT EXISTS idx_apd_cad_response_ts
                ON apd_cad(response_ts);
            CREATE INDEX IF NOT EXISTS idx_apd_cad_unmatched
                ON apd_cad(matched_incident_id)
                WHERE matched_incident_id IS NULL;
            """
        )
        conn.commit()
        conn.close()
        logger.info("[cad] DB tables ready")

    def fetch_and_store(self) -> int:
        """Fetch CAD records from the lookback window and upsert them."""
        lookback_dt = datetime.now(timezone.utc) - timedelta(days=APD_CAD_LOOKBACK_DAYS)
        lookback_str = lookback_dt.strftime("'%Y-%m-%dT%H:%M:%S'")
        url = APD_CAD_URL.format(lookback=lookback_str)

        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                records = json.loads(resp.read())
        except Exception as exc:
            logger.warning("[cad] fetch error: %s", exc)
            return 0

        now = time.time()
        conn = sqlite3.connect(self.db_path)
        upserted = 0
        for record in records:
            incident_number = record.get("incident_number")
            if not incident_number:
                continue
            conn.execute(
                """
                INSERT INTO apd_cad
                    (incident_number, response_ts, call_closed_ts, sector,
                     council_district, priority_level, initial_description,
                     initial_category, final_description, final_category,
                     mental_health_flag, disposition, geoid, fetched_ts)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(incident_number) DO UPDATE SET
                    final_description = excluded.final_description,
                    final_category    = excluded.final_category,
                    disposition       = excluded.disposition,
                    fetched_ts        = excluded.fetched_ts
                """,
                (
                    incident_number,
                    _parse_cad_ts(record.get("response_datetime")),
                    _parse_cad_ts(record.get("call_closed_datetime")),
                    record.get("sector"),
                    record.get("council_district"),
                    record.get("priority_level"),
                    record.get("initial_problem_description"),
                    record.get("initial_problem_category"),
                    record.get("final_problem_description"),
                    record.get("final_problem_category"),
                    record.get("mental_health_flag"),
                    record.get("call_disposition_description"),
                    record.get("geoid"),
                    now,
                ),
            )
            upserted += 1
        conn.commit()
        conn.close()
        logger.info("[cad] upserted %s records (%s fetched)", upserted, len(records))
        return upserted

    def match_and_harvest(self) -> tuple[int, int]:
        """Match unmatched CAD rows to scanner incidents and harvest TGID hints."""
        from modules.talkgroups import IGNORE_TGIDS, TGID_META  # noqa: PLC0415

        match_window = 1800
        tgid_window_pre = 300
        tgid_window_post = 120

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cutoff = time.time() - 7200
        cad_rows = conn.execute(
            """
            SELECT * FROM apd_cad
            WHERE matched_incident_id IS NULL
              AND response_ts IS NOT NULL
              AND response_ts < ?
            ORDER BY response_ts DESC
            LIMIT 2000
            """,
            (cutoff,),
        ).fetchall()

        claimed_ids = {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT matched_incident_id FROM apd_cad "
                "WHERE matched_incident_id IS NOT NULL"
            ).fetchall()
        }

        matched = 0
        harvested_hints = 0
        matched_cad_nums = set()

        for pass_num in (1, 2):
            for cad in cad_rows:
                if pass_num == 2 and cad["incident_number"] in matched_cad_nums:
                    continue

                response_ts = cad["response_ts"]
                init_cat = cad["initial_category"] or ""
                bb_itype = _CAD_CATEGORY_MAP.get(init_cat)

                if pass_num == 1 and not bb_itype:
                    continue

                candidates = conn.execute(
                    """
                    SELECT id, itype, agencies, ts_start FROM incidents
                    WHERE ts_start BETWEEN ? AND ?
                      AND (is_test IS NULL OR is_test = 0)
                    ORDER BY ABS(ts_start - ?) ASC
                    LIMIT 5
                    """,
                    (response_ts - match_window, response_ts + match_window, response_ts),
                ).fetchall()

                best_match_id = None
                best_confidence = None

                for incident in candidates:
                    if incident["id"] in claimed_ids:
                        continue
                    inc_itype = incident["itype"] or ""
                    if bb_itype and inc_itype == bb_itype:
                        best_match_id = incident["id"]
                        best_confidence = "high"
                        break
                    if pass_num == 2 and best_match_id is None:
                        best_match_id = incident["id"]
                        best_confidence = "time_only"

                if best_match_id:
                    try:
                        conn.execute(
                            """
                            UPDATE apd_cad
                            SET matched_incident_id = ?, match_confidence = ?
                            WHERE incident_number = ?
                            """,
                            (best_match_id, best_confidence, cad["incident_number"]),
                        )
                        matched += 1
                        claimed_ids.add(best_match_id)
                        matched_cad_nums.add(cad["incident_number"])
                        if best_confidence == "high":
                            conn.execute(
                                """
                                UPDATE incidents SET
                                    description = description || ' [CAD: ' || ? || ', ' || ? || ', sector ' || ? || ']'
                                WHERE id = ? AND description NOT LIKE '%[CAD:%'
                                """,
                                (
                                    cad["final_description"] or cad["initial_description"] or "",
                                    cad["disposition"] or "",
                                    cad["sector"] or "?",
                                    best_match_id,
                                ),
                            )
                    except sqlite3.IntegrityError:
                        conn.execute(
                            """
                            UPDATE apd_cad SET matched_incident_id = NULL, match_confidence = NULL
                            WHERE incident_number = ?
                            """,
                            (cad["incident_number"],),
                        )
                else:
                    conn.execute(
                        """
                        UPDATE apd_cad
                        SET matched_incident_id = NULL, match_confidence = NULL
                        WHERE incident_number = ?
                        """,
                        (cad["incident_number"],),
                    )

        for cad in cad_rows:
            response_ts = cad["response_ts"]
            sector = cad["sector"]
            init_cat = cad["initial_category"] or ""
            call_closed = cad["call_closed_ts"] or (response_ts + 1800)

            if sector and init_cat in _CAD_HARVEST_CATEGORIES:
                tgid_rows = conn.execute(
                    """
                    SELECT tgid, COUNT(*) as call_count
                    FROM calls
                    WHERE ts BETWEEN ? AND ?
                      AND tgid IS NOT NULL
                      AND tgid > 0
                    GROUP BY tgid
                    HAVING call_count >= 2
                    """,
                    (response_ts - tgid_window_pre, call_closed + tgid_window_post),
                ).fetchall()

                for row in tgid_rows:
                    tgid = row["tgid"]
                    if tgid in TGID_META or tgid in IGNORE_TGIDS:
                        continue
                    conn.execute(
                        """
                        INSERT INTO tgid_sector_hints (tgid, sector, hit_count, last_seen)
                        VALUES (?, ?, 1, ?)
                        ON CONFLICT(tgid, sector) DO UPDATE SET
                            hit_count = hit_count + 1,
                            last_seen = excluded.last_seen
                        """,
                        (tgid, sector, response_ts),
                    )
                    harvested_hints += 1

        conn.commit()
        conn.close()
        logger.info(
            "[cad] match run: %s/%s matched, %s TGID hints harvested",
            matched,
            len(cad_rows),
            harvested_hints,
        )
        return matched, harvested_hints
