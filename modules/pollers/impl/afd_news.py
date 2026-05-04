"""
modules/pollers/impl/afd_news.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
AFD Open Data poller — polls Austin Open Data for active Austin Fire Department
incidents and cross-references them against scanner-detected incidents.

Migrated from modules/pollers.py (afd_open_data_thread) as part of the
SOA / BasePoller refactor.

For each new active incident the poller:
  - Checks if a scanner incident is already tracking a nearby location.
  - If matched: posts a confirmation message to the fire-ems Talk room.
  - If unmatched: posts a "scanner missed" alert and places an ATAK marker.
  - Detects when an incident disappears from the feed (ARCHIVED) and clears
    the corresponding ATAK marker.

Circular-import safety: all imports from modules.config and
modules.incident_engine are deferred inside run() so this module is safely
importable before application config is initialised (e.g. during tests).
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import urllib.request

from modules.pollers.base import BasePoller

logger = logging.getLogger("AFDOpenDataPoller")

# ---------------------------------------------------------------------------
# Module-level constants (literal copies — do NOT import from modules.config
# at load time to avoid circular imports during bootstrap)
# ---------------------------------------------------------------------------

AFD_OPEN_DATA_URL: str = (
    "https://data.austintexas.gov/resource/wpu4-x69d.json"
    "?$where=traffic_report_status='ACTIVE'&$limit=50"
)
AFD_POLL_INTERVAL: float = 60.0  # seconds

# Maps leading word of issue_reported → internal itype
_AFD_ITYPE_MAP: dict[str, str] = {
    "STRUCTURE":  "STRUCTURE FIRE",
    "FIRE":       "STRUCTURE FIRE",
    "GRASS":      "GRASS FIRE",
    "WILDLAND":   "GRASS FIRE",
    "AIRCRAFT":   "AIRCRAFT EMERGENCY",
    "HANGER":     "STRUCTURE FIRE",
    "HANGAR":     "STRUCTURE FIRE",
    "EXPLOSION":  "EXPLOSION",
    "HAZMAT":     "HAZMAT",
    "ALARM":      "FIRE ALARM",
    "ALARMM":     "FIRE ALARM",
}


def _afd_issue_to_itype(issue: str) -> str:
    """Map AFD issue_reported string to a Battle Buddy itype."""
    prefix = issue.split()[0].upper().rstrip("-") if issue else ""
    return _AFD_ITYPE_MAP.get(prefix, "FIRE/EMS DISPATCH")


class AFDOpenDataPoller(BasePoller):
    """Poll Austin Open Data for active AFD incidents every 60 seconds.

    State is held entirely in instance variables — no module-level globals —
    so multiple instances can coexist safely (useful in tests).

    Instance variables
    ------------------
    _active_ids : dict[str, dict]
        Maps traffic_report_id → AFD incident dict for currently active incidents.
    _state_lock : threading.Lock
        Protects _active_ids during concurrent reads/writes.
    """

    NAME: str = "afd"
    INTERVAL: float = AFD_POLL_INTERVAL

    def __init__(self) -> None:
        super().__init__(interval=self.INTERVAL)
        self._active_ids: dict[str, dict] = {}
        self._state_lock = threading.Lock()

    # ------------------------------------------------------------------
    # BasePoller interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Fetch AFD incidents and process new/cleared entries."""
        # Lazy imports — avoid circular dependency at module load time
        from modules.config import (  # noqa: PLC0415
            TALK_BASE,
            TALK_PASS,
            TALK_ROOMS,
            TALK_USER,
        )
        from modules.incident_engine import (  # noqa: PLC0415
            _active_incidents,
            _atak_clear_marker,
            _atak_post_marker,
            _haversine_km,
            _incident_lock,
        )

        # ------------------------------------------------------------------
        # Fetch active AFD incidents from Austin Open Data
        # ------------------------------------------------------------------
        try:
            req = urllib.request.Request(
                AFD_OPEN_DATA_URL,
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                incidents = json.loads(resp.read())
        except Exception as exc:
            logger.warning("[afd] fetch error: %s", exc)
            return

        # ------------------------------------------------------------------
        # Reconcile with current active set
        # ------------------------------------------------------------------
        with self._state_lock:
            current_ids = {inc["traffic_report_id"] for inc in incidents}

            # Detect incidents that just went ARCHIVED (were active, now gone)
            cleared = set(self._active_ids.keys()) - current_ids
            for rid in cleared:
                old = self._active_ids.pop(rid)
                logger.info(
                    "[afd] CLEARED: %s @ %s",
                    old.get("issue_reported"),
                    old.get("address"),
                )
                afd_mid = old.get("atak_marker_id")
                if afd_mid is not None:
                    threading.Thread(
                        target=_atak_clear_marker,
                        args=(afd_mid,),
                        daemon=True,
                    ).start()

            # Process new active incidents
            for inc in incidents:
                rid = inc["traffic_report_id"]
                if rid in self._active_ids:
                    continue  # already processed

                self._active_ids[rid] = inc
                itype   = _afd_issue_to_itype(inc.get("issue_reported", ""))
                lat     = float(inc["latitude"])  if inc.get("latitude")  else None
                lon     = float(inc["longitude"]) if inc.get("longitude") else None
                address = inc.get("address", "")

                # Check if a scanner incident is already tracking this location
                matched_id = None
                if lat is not None and lon is not None:
                    with _incident_lock:
                        for iid, bb_inc in _active_incidents.items():
                            blat = bb_inc.get("lat")
                            blon = bb_inc.get("lon")
                            if blat is None or blon is None:
                                continue
                            if _haversine_km(lat, lon, blat, blon) < 0.5:
                                matched_id = iid
                                break

                logger.info(
                    "[afd] NEW %s: %s @ %s",
                    "(matched #%s)" % matched_id if matched_id else "(unmatched)",
                    inc.get("issue_reported"),
                    address,
                )

                threading.Thread(
                    target=self._post_to_talk,
                    args=(inc, itype, matched_id, TALK_BASE, TALK_USER, TALK_PASS, TALK_ROOMS),
                    daemon=True,
                ).start()

                # If unmatched and has coordinates, post an ATAK marker
                if matched_id is None and address and lat is not None and lon is not None:
                    # Use a negative sentinel incident_id to avoid colliding with real ones
                    afd_marker_id = hash(rid) % 100_000 * -1
                    self._active_ids[rid]["atak_marker_id"] = afd_marker_id
                    threading.Thread(
                        target=_atak_post_marker,
                        args=(afd_marker_id, lat, lon, itype, address),
                        daemon=True,
                    ).start()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _post_to_talk(
        incident: dict,
        itype: str,
        matched_bb_id: int | None,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
    ) -> None:
        """Post an AFD Open Data incident to the fire-ems Talk room."""
        address = incident.get("address", "Unknown address")
        issue   = incident.get("issue_reported", "Unknown")
        pub_dt  = incident.get("published_date", "")[:16].replace("T", " ")
        lat     = incident.get("latitude")
        lon     = incident.get("longitude")
        coords  = f" ({lat}, {lon})" if lat and lon else ""

        if matched_bb_id:
            msg = (
                f"📡 [AFD API CONFIRM] Scanner incident #{matched_bb_id} confirmed via city dispatch feed\n"
                f"📍 {address}{coords}\n"
                f"🚒 {issue} — dispatched {pub_dt}"
            )
        else:
            msg = (
                f"🚨 [AFD DISPATCH — scanner missed] {itype}\n"
                f"📍 {address}{coords}\n"
                f"🚒 {issue} — dispatched {pub_dt}"
            )

        payload = json.dumps({"message": msg}).encode()
        creds   = base64.b64encode(f"{talk_user}:{talk_pass}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "OCS-APIRequest": "true",
            "Content-Type": "application/json",
        }
        room_token = talk_rooms.get("fire-ems")
        if not talk_base or not room_token:
            logger.warning("[afd] Talk post skipped: TALK_BASE or fire-ems room token missing")
            return
        url = f"{talk_base}/chat/{room_token}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            logger.info("[afd] posted to fire-ems: %s @ %s", issue, address)
        except Exception as exc:
            logger.warning("[afd] Talk post failed: %s", exc)
