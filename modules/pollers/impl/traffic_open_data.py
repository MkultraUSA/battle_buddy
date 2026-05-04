"""
modules/pollers/impl/traffic_open_data.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Austin Open Data traffic incident poller.

Migrated from modules/pollers_legacy.py as part of the BasePoller refactor.
The poller tracks active traffic incidents, cross-references nearby scanner
incidents, posts significant dispatches to Talk, and manages ATAK markers for
unmatched traffic events.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import urllib.request

from modules.pollers.base import BasePoller

logger = logging.getLogger("TrafficOpenDataPoller")

TRAFFIC_OPEN_DATA_URL = (
    "https://data.austintexas.gov/resource/dx9v-zd7x.json"
    "?$where=traffic_report_status='ACTIVE'&$limit=100"
)
TRAFFIC_POLL_INTERVAL: float = 60.0

_TRAFFIC_ITYPE_MAP = {
    "CRASH": "CRASH/COLLISION",
    "COLLISION": "CRASH/COLLISION",
    "VEHICLE": "CRASH/COLLISION",
    "MOTORCYCLE": "CRASH/COLLISION",
    "BICYCLE": "CRASH/COLLISION",
    "PEDESTRIAN": "PEDESTRIAN INCIDENT",
    "STALLED": "STALLED VEHICLE",
    "ABANDONED": "ABANDONED VEHICLE",
    "ROAD": "ROAD HAZARD",
    "DEBRIS": "ROAD HAZARD",
    "FLOODING": "FLOODING",
    "FLOODED": "FLOODING",
    "SIGNAL": "TRAFFIC SIGNAL ISSUE",
    "FIRE": "VEHICLE FIRE",
    "HAZMAT": "HAZMAT",
    "SPILL": "HAZMAT",
    "BRIDGE": "ROAD HAZARD",
    "ANIMAL": "ROAD HAZARD",
}

_TRAFFIC_TALK_ITYPES = {
    "CRASH/COLLISION",
    "PEDESTRIAN INCIDENT",
    "FLOODING",
    "VEHICLE FIRE",
    "HAZMAT",
    "ROAD HAZARD",
}


def _traffic_issue_to_itype(issue: str) -> str:
    """Map traffic issue_reported string to a Battle Buddy itype."""
    prefix = issue.split()[0].upper().rstrip("-") if issue else ""
    return _TRAFFIC_ITYPE_MAP.get(prefix, "TRAFFIC INCIDENT")


class TrafficOpenDataPoller(BasePoller):
    """Poll Austin Open Data for active traffic incidents every 60 seconds."""

    NAME: str = "traffic-open-data"
    INTERVAL: float = TRAFFIC_POLL_INTERVAL

    def __init__(self) -> None:
        super().__init__(interval=self.INTERVAL)
        self._active_ids: dict[str, dict] = {}
        self._state_lock = threading.Lock()

    def run(self) -> None:
        from modules.config import TALK_BASE, TALK_PASS, TALK_ROOMS, TALK_USER  # noqa: PLC0415
        from modules.incident_engine import (  # noqa: PLC0415
            _active_incidents,
            _atak_clear_marker,
            _atak_post_marker,
            _haversine_km,
            _incident_lock,
        )

        try:
            req = urllib.request.Request(
                TRAFFIC_OPEN_DATA_URL,
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                incidents = json.loads(resp.read())
        except Exception as exc:
            logger.warning("[traffic] fetch error: %s", exc)
            return

        with self._state_lock:
            current_ids = {inc["traffic_report_id"] for inc in incidents}

            cleared = set(self._active_ids.keys()) - current_ids
            for report_id in cleared:
                old = self._active_ids.pop(report_id)
                logger.info(
                    "[traffic] CLEARED: %s @ %s",
                    old.get("issue_reported"),
                    old.get("address"),
                )
                marker_id = old.get("atak_marker_id")
                if marker_id is not None:
                    threading.Thread(
                        target=_atak_clear_marker,
                        args=(marker_id,),
                        daemon=True,
                    ).start()

            for incident in incidents:
                self._process_incident(
                    incident,
                    TALK_BASE,
                    TALK_USER,
                    TALK_PASS,
                    TALK_ROOMS,
                    _active_incidents,
                    _incident_lock,
                    _haversine_km,
                    _atak_post_marker,
                )

    def _process_incident(
        self,
        incident: dict,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
        active_incidents: dict,
        incident_lock,
        haversine_km,
        atak_post_marker,
    ) -> int | None:
        """Process a new traffic incident. Returns matched incident ID if any."""
        report_id = incident["traffic_report_id"]
        if report_id in self._active_ids:
            return None

        self._active_ids[report_id] = incident
        itype = _traffic_issue_to_itype(incident.get("issue_reported", ""))
        lat = float(incident["latitude"]) if incident.get("latitude") else None
        lon = float(incident["longitude"]) if incident.get("longitude") else None
        address = incident.get("address", "")

        if lat is None or lon is None:
            logger.info(
                "[traffic] skipping (no coords): %s @ %s",
                incident.get("issue_reported"),
                address,
            )
            return None

        matched_id = None
        with incident_lock:
            for incident_id, bb_incident in active_incidents.items():
                bb_lat = bb_incident.get("lat")
                bb_lon = bb_incident.get("lon")
                if bb_lat is None or bb_lon is None:
                    continue
                if haversine_km(lat, lon, bb_lat, bb_lon) < 0.5:
                    matched_id = incident_id
                    break

        logger.info(
            "[traffic] NEW %s: %s @ %s",
            "(matched #%s)" % matched_id if matched_id else "(unmatched)",
            incident.get("issue_reported"),
            address,
        )

        if itype in _TRAFFIC_TALK_ITYPES or matched_id is not None:
            threading.Thread(
                target=self._post_to_talk,
                args=(incident, itype, matched_id, talk_base, talk_user, talk_pass, talk_rooms),
                daemon=True,
            ).start()

        if matched_id is None:
            marker_id = -(abs(hash(report_id)) % 100000) - 100001
            self._active_ids[report_id]["atak_marker_id"] = marker_id
            threading.Thread(
                target=atak_post_marker,
                args=(marker_id, lat, lon, itype, address),
                daemon=True,
            ).start()

        return matched_id

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
        address = incident.get("address", "Unknown address")
        issue = incident.get("issue_reported", "Unknown")
        pub_dt = incident.get("published_date", "")[:16].replace("T", " ")
        agency = incident.get("agency", "").strip()
        lat = incident.get("latitude")
        lon = incident.get("longitude")
        coords = f" ({lat}, {lon})" if lat and lon else ""

        if matched_bb_id:
            msg = (
                f"[TRAFFIC API CONFIRM] Scanner incident #{matched_bb_id} confirmed via city feed\n"
                f"Address: {address}{coords}\n"
                f"Type: {issue} ({agency}) - dispatched {pub_dt}"
            )
        else:
            msg = (
                f"[TRAFFIC DISPATCH] {itype}\n"
                f"Address: {address}{coords}\n"
                f"Type: {issue} ({agency}) - dispatched {pub_dt}"
            )

        payload = json.dumps({"message": msg}).encode()
        creds = base64.b64encode(f"{talk_user}:{talk_pass}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "OCS-APIRequest": "true",
            "Content-Type": "application/json",
        }
        room_token = talk_rooms.get("incidents")
        if not talk_base or not room_token:
            logger.warning("[traffic] Talk post skipped: TALK_BASE or incidents room token missing")
            return
        url = f"{talk_base}/chat/{room_token}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            logger.info("[traffic] posted to incidents: %s @ %s", issue, address)
        except Exception as exc:
            logger.warning("[traffic] Talk post failed: %s", exc)
