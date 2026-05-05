"""
modules/pollers/impl/adsb_air_asset.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
ADS-B air asset poller.

Migrated from modules/pollers_legacy.py as part of the BasePoller refactor.
The poller tracks low-altitude helicopters and known Austin-area public safety
air assets, writes aircraft trails, posts ATAK markers, and alerts on LEO
aircraft and orbiting behavior.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import sqlite3
import threading
import time
import urllib.request

from modules.pollers.base import BasePoller

logger = logging.getLogger("ADSBAirAssetPoller")

ADSB_LOL_URL = "https://api.adsb.lol/v2/lat/30.2672/lon/-97.7431/dist/52"
ADSB_INTERVAL: float = 30.0
ADSB_MAX_ALT_FT = 5000
ADSB_TRAIL_SECS = 1800
ADSB_REFRACTORY = 1800

KNOWN_AIR_ASSETS = {
    "a820f8": ("APD Air1 (N6227)", True),
    "a064fb": ("APD Air Support (N1240W)", True),
    "a33eb6": ("STAR Flight 2 (N308TC)", False),
    "a3426d": ("STAR Flight 3 (N309TC)", False),
}


class ADSBAirAssetPoller(BasePoller):
    """Poll adsb.lol for low-altitude helicopters over Austin."""

    NAME: str = "adsb-air-asset"
    INTERVAL: float = ADSB_INTERVAL

    def __init__(self) -> None:
        super().__init__(interval=self.INTERVAL)
        self._seen: dict[str, float] = {}
        self._orbit_seen: dict[str, float] = {}
        self._lock = threading.Lock()
        self._schema_ready = False

    def run(self) -> None:
        from modules.config import (  # noqa: PLC0415
            DB_PATH,
            TALK_BASE,
            TALK_PASS,
            TALK_ROOMS,
            TALK_USER,
        )
        from modules.incident_engine import _atak_post_marker  # noqa: PLC0415
        from modules.pollers_legacy import send_dm_alert  # noqa: PLC0415

        if not self._schema_ready:
            self.ensure_schema(DB_PATH)
            self._schema_ready = True

        try:
            data = self.fetch_aircraft()
        except Exception as exc:
            logger.warning("[adsb] fetch error: %s", exc)
            return

        now = time.time()
        self.prune_positions(DB_PATH, now)
        for aircraft in data.get("ac") or []:
            self.process_aircraft(
                aircraft,
                now,
                DB_PATH,
                TALK_BASE,
                TALK_USER,
                TALK_PASS,
                TALK_ROOMS,
                _atak_post_marker,
            )

        self.detect_orbits(
            DB_PATH,
            time.time(),
            TALK_BASE,
            TALK_USER,
            TALK_PASS,
            TALK_ROOMS,
            _atak_post_marker,
            send_dm_alert,
        )

    @staticmethod
    def ensure_schema(db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS aircraft_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            icao24 TEXT NOT NULL,
            callsign TEXT,
            lat REAL NOT NULL,
            lon REAL NOT NULL,
            alt_ft INTEGER,
            heading REAL,
            speed_kts REAL,
            is_leo INTEGER DEFAULT 0,
            label TEXT
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_ts ON aircraft_positions(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_aircraft_icao ON aircraft_positions(icao24, ts)")
        conn.commit()
        conn.close()

    @staticmethod
    def fetch_aircraft() -> dict:
        req = urllib.request.Request(
            ADSB_LOL_URL,
            headers={"User-Agent": "BattleBuddy/2.0", "Accept": "application/json"},
        )
        return json.loads(urllib.request.urlopen(req, timeout=20).read())

    @staticmethod
    def prune_positions(db_path: str, now: float) -> None:
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("DELETE FROM aircraft_positions WHERE ts < ?", (now - ADSB_TRAIL_SECS,))
            conn.commit()
            conn.close()
        except Exception:
            pass

    def process_aircraft(
        self,
        aircraft: dict,
        now: float,
        db_path: str,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
        atak_post_marker,
    ) -> str | None:
        normalized = self.normalize_aircraft(aircraft)
        if normalized is None:
            return None

        self.store_position(db_path, now, normalized)

        icao24 = normalized["icao24"]
        with self._lock:
            last_alert = self._seen.get(icao24, 0)
            if now - last_alert < ADSB_REFRACTORY:
                return "refractory"
            self._seen[icao24] = now

        if normalized["is_leo"]:
            self.alert_leo_aircraft(
                db_path,
                now,
                normalized,
                talk_base,
                talk_user,
                talk_pass,
                talk_rooms,
                atak_post_marker,
            )
            return "leo"

        self.post_non_leo_marker(now, normalized, atak_post_marker)
        return "non-leo"

    @staticmethod
    def normalize_aircraft(aircraft: dict) -> dict | None:
        icao24 = (aircraft.get("hex") or "").strip().lower()
        callsign = (aircraft.get("flight") or aircraft.get("r") or "").strip()
        lat = aircraft.get("lat")
        lon = aircraft.get("lon")
        alt_ft = aircraft.get("alt_baro")
        heading = aircraft.get("track")
        speed_kts = aircraft.get("gs")
        on_ground = aircraft.get("alt_baro") == "ground" or aircraft.get("on_ground") == 1

        if not icao24 or lat is None or lon is None or on_ground:
            return None
        if isinstance(alt_ft, str):
            return None
        if alt_ft is None or alt_ft > ADSB_MAX_ALT_FT:
            return None

        category = (aircraft.get("category") or "").upper()
        is_helo = category.startswith("A7") or category.startswith("B")
        label, is_leo = KNOWN_AIR_ASSETS.get(icao24, (None, False))
        if label is None and not is_helo:
            return None

        return {
            "icao24": icao24,
            "callsign": callsign,
            "lat": lat,
            "lon": lon,
            "alt_ft": alt_ft,
            "heading": heading,
            "speed_kts": speed_kts,
            "label": label or (callsign or f"ICAO {icao24}"),
            "display_label": label or "Unknown Helicopter",
            "is_leo": is_leo,
        }

    @staticmethod
    def store_position(db_path: str, now: float, aircraft: dict) -> None:
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO aircraft_positions "
                "(ts,icao24,callsign,lat,lon,alt_ft,heading,speed_kts,is_leo,label) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    now,
                    aircraft["icao24"],
                    aircraft["callsign"] or None,
                    aircraft["lat"],
                    aircraft["lon"],
                    int(aircraft["alt_ft"]) if aircraft["alt_ft"] else None,
                    aircraft["heading"],
                    aircraft["speed_kts"],
                    1 if aircraft["is_leo"] else 0,
                    aircraft["label"],
                ),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            logger.warning("[adsb] db write error: %s", exc)

    @staticmethod
    def post_non_leo_marker(now: float, aircraft: dict, atak_post_marker) -> None:
        callsign = f" ({aircraft['callsign']})" if aircraft["callsign"] else ""
        desc = (
            f"[ADS-B] {aircraft['display_label']}{callsign} airborne over Austin - "
            f"{int(aircraft['alt_ft'])}ft AGL, ICAO {aircraft['icao24']}"
        )
        synthetic_id = -int(now * 1000) % 2147483647
        threading.Thread(
            target=atak_post_marker,
            args=(
                synthetic_id,
                aircraft["lat"],
                aircraft["lon"],
                "AIR ASSET EMS",
                aircraft["display_label"] + callsign,
                desc,
            ),
            daemon=True,
        ).start()

    @staticmethod
    def alert_leo_aircraft(
        db_path: str,
        now: float,
        aircraft: dict,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
        atak_post_marker,
    ) -> int:
        callsign = f" ({aircraft['callsign']})" if aircraft["callsign"] else ""
        desc = (
            f"[ADS-B] {aircraft['display_label']}{callsign} detected over Austin - "
            f"{int(aircraft['alt_ft'])}ft AGL, ICAO {aircraft['icao24']}"
        )
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, "
            "tgids, location, lat, lon, status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
            (now, now, "AIR ASSET ACTIVE", desc, '["APD"]', '[]', "Austin airspace", aircraft["lat"], aircraft["lon"]),
        )
        inc_id = cur.lastrowid
        conn.commit()
        conn.close()

        msg = (
            f"LEO AIR ASSET: {aircraft['display_label']}{callsign}\n"
            f"Altitude: {int(aircraft['alt_ft'])}ft | ICAO: {aircraft['icao24']}\n"
            f"Position: {aircraft['lat']:.4f}, {aircraft['lon']:.4f}"
        )
        ADSBAirAssetPoller.post_to_talk(talk_base, talk_user, talk_pass, talk_rooms.get("apd"), msg)
        threading.Thread(
            target=atak_post_marker,
            args=(inc_id, aircraft["lat"], aircraft["lon"], "AIR ASSET ACTIVE", "Austin airspace", desc),
            daemon=True,
        ).start()
        return inc_id

    def detect_orbits(
        self,
        db_path: str,
        now: float,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
        atak_post_marker,
        send_alert,
    ) -> None:
        try:
            conn = sqlite3.connect(db_path)
            leo_icaos = conn.execute(
                "SELECT DISTINCT icao24 FROM aircraft_positions WHERE is_leo=1 AND ts >= ?",
                (now - 300,),
            ).fetchall()
            conn.close()
        except Exception:
            leo_icaos = []

        for (icao24,) in leo_icaos:
            with self._lock:
                if now - self._orbit_seen.get(icao24, 0) < ADSB_REFRACTORY:
                    continue
            if not check_orbit(db_path, icao24, now):
                continue
            with self._lock:
                self._orbit_seen[icao24] = now
            self.alert_orbit(
                db_path,
                now,
                icao24,
                talk_base,
                talk_user,
                talk_pass,
                talk_rooms,
                atak_post_marker,
                send_alert,
            )

    @staticmethod
    def alert_orbit(
        db_path: str,
        now: float,
        icao24: str,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
        atak_post_marker,
        send_alert,
    ) -> int | None:
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT lat, lon, alt_ft, label, callsign FROM aircraft_positions "
                "WHERE icao24=? ORDER BY ts DESC LIMIT 1",
                (icao24,),
            ).fetchone()
            conn.close()
        except Exception:
            return None
        if not row:
            return None

        lat, lon, alt_ft, label, callsign = row
        label = label or f"ICAO {icao24}"
        callsign_text = f" ({callsign})" if callsign else ""
        desc = (
            f"[ADS-B ORBIT] {label}{callsign_text} circling over Austin - "
            f"possible ground surveillance. {int(alt_ft or 0)}ft AGL, ICAO {icao24}"
        )
        conn = sqlite3.connect(db_path)
        cur = conn.execute(
            "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, "
            "tgids, location, lat, lon, status) VALUES (?,?,?,?,?,?,?,?,?,'active')",
            (now, now, "AIR ASSET ORBIT", desc, '["APD"]', '[]', "Austin airspace", lat, lon),
        )
        inc_id = cur.lastrowid
        conn.commit()
        conn.close()

        msg = (
            f"LEO HELICOPTER ORBITING: {label}{callsign_text}\n"
            f"Altitude: {int(alt_ft or 0)}ft | ICAO: {icao24}\n"
            f"Position: {lat:.4f}, {lon:.4f}\n"
            "Aircraft circling - likely observing ground target."
        )
        for room in (talk_rooms.get("apd"), talk_rooms.get("incidents")):
            ADSBAirAssetPoller.post_to_talk(talk_base, talk_user, talk_pass, room, msg)

        threading.Thread(
            target=send_alert,
            args=("AIR ASSET ORBIT", desc, "Austin airspace", "APD", "APD"),
            daemon=True,
        ).start()
        threading.Thread(
            target=atak_post_marker,
            args=(inc_id, lat, lon, "AIR ASSET ORBIT", "Austin airspace", desc),
            daemon=True,
        ).start()
        return inc_id

    @staticmethod
    def post_to_talk(talk_base: str, talk_user: str, talk_pass: str, room: str | None, msg: str) -> None:
        if not talk_base or not room:
            return
        payload = json.dumps({"message": msg}).encode()
        creds = base64.b64encode(f"{talk_user}:{talk_pass}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "OCS-APIRequest": "true",
            "Content-Type": "application/json",
        }
        req = urllib.request.Request(
            f"{talk_base}/chat/{room}",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            logger.warning("[adsb] Talk post failed: %s", exc)


def check_orbit(db_path: str, icao24: str, now: float) -> bool:
    """Return True if icao24 has been orbiting in the last 5 minutes."""
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT lat, lon, heading FROM aircraft_positions "
            "WHERE icao24=? AND ts >= ? ORDER BY ts",
            (icao24, now - 300),
        ).fetchall()
        conn.close()
    except Exception:
        return False

    if len(rows) < 6:
        return False

    lats = [row[0] for row in rows]
    lons = [row[1] for row in rows]
    headings = [row[2] for row in rows if row[2] is not None]
    center_lat = sum(lats) / len(lats)
    center_lon = sum(lons) / len(lons)

    max_dist = max(_km(center_lat, center_lon, lat, lon) for lat, lon in zip(lats, lons))
    if max_dist > 1.2:
        return False

    if len(headings) < 5:
        return False
    return max(headings) - min(headings) >= 180


def _km(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    radius = 6371.0
    dlat = math.radians(lat_b - lat_a)
    dlon = math.radians(lon_b - lon_a)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat_a)) * math.cos(math.radians(lat_b)) * math.sin(dlon / 2) ** 2
    )
    return radius * 2 * math.asin(math.sqrt(a))
