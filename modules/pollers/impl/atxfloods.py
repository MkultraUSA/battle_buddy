"""
modules/pollers/impl/atxfloods.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
ATXFloods low-water crossing poller.

Migrated from modules/pollers_legacy.py as part of the BasePoller refactor.
The poller watches crossing status transitions and posts alerts/ATAK markers
for closures and caution states.

Circular-import safety: imports from modules.config and modules.incident_engine
are deferred inside run() so this module stays importable in tests and during
application bootstrap.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import urllib.request

from modules.pollers.base import BasePoller

logger = logging.getLogger("ATXFloodsPoller")

ATXFLOODS_URL = "https://api.atxfloods.com/api/crossings"
ATXFLOODS_POLL_INTERVAL: float = 300.0

_VALID_STATUSES = {"open", "closed", "caution"}


class ATXFloodsPoller(BasePoller):
    """Poll ATXFloods and alert on low-water crossing state transitions."""

    NAME: str = "atxfloods"
    INTERVAL: float = ATXFLOODS_POLL_INTERVAL

    def __init__(self) -> None:
        super().__init__(interval=self.INTERVAL)
        self._state: dict[int, dict] = {}
        self._state_lock = threading.Lock()

    def run(self) -> None:
        """Fetch crossings and process status transitions."""
        from modules.config import TALK_BASE, TALK_PASS, TALK_ROOMS, TALK_USER  # noqa: PLC0415
        from modules.incident_engine import _atak_clear_marker, _atak_post_marker  # noqa: PLC0415

        try:
            req = urllib.request.Request(
                ATXFLOODS_URL,
                headers={"Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read())
        except Exception as exc:
            logger.warning("[atxfloods] fetch error: %s", exc)
            return

        crossings = payload.get("attributes", []) if isinstance(payload, dict) else []
        if not crossings:
            logger.info("[atxfloods] empty response")
            return

        transitions = 0
        with self._state_lock:
            for crossing in crossings:
                transition = self._process_crossing(
                    crossing,
                    TALK_BASE,
                    TALK_USER,
                    TALK_PASS,
                    TALK_ROOMS,
                    _atak_clear_marker,
                    _atak_post_marker,
                )
                if transition:
                    transitions += 1

        if transitions:
            logger.info("[atxfloods] %s state transition(s) this cycle", transitions)

    def _process_crossing(
        self,
        crossing: dict,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
        atak_clear_marker,
        atak_post_marker,
    ) -> bool:
        """Process one crossing. Returns True when a transition was handled."""
        try:
            crossing_id = int(crossing["id"])
        except (KeyError, ValueError, TypeError):
            return False

        status = (crossing.get("status") or "").lower()
        if status not in _VALID_STATUSES:
            return False

        previous = self._state.get(crossing_id)
        if previous is None:
            self._state[crossing_id] = {"status": status, "marker_id": None}
            return False
        if previous["status"] == status:
            return False

        old_status = previous["status"]
        previous["status"] = status

        self._post_to_talk(crossing, status, old_status, talk_base, talk_user, talk_pass, talk_rooms)
        self._update_marker(crossing, crossing_id, status, previous, atak_clear_marker, atak_post_marker)
        return True

    @staticmethod
    def _update_marker(
        crossing: dict,
        crossing_id: int,
        status: str,
        previous: dict,
        atak_clear_marker,
        atak_post_marker,
    ) -> None:
        """Create or clear ATAK markers for changed crossing states."""
        try:
            lat = float(crossing["lat"])
            lon = float(crossing["lon"])
        except (KeyError, ValueError, TypeError):
            lat = lon = None

        if status == "open" and previous.get("marker_id") is not None:
            threading.Thread(
                target=atak_clear_marker,
                args=(previous["marker_id"],),
                daemon=True,
            ).start()
            previous["marker_id"] = None
        elif status in ("closed", "caution") and lat is not None and lon is not None:
            marker_id = -(abs(crossing_id) % 100000) - 200001
            previous["marker_id"] = marker_id
            label = f"{crossing.get('name', '')} {crossing.get('address', '')}".strip()
            threading.Thread(
                target=atak_post_marker,
                args=(marker_id, lat, lon, "FLOODING", label),
                daemon=True,
            ).start()

    @staticmethod
    def _post_to_talk(
        crossing: dict,
        new_status: str,
        old_status: str | None,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
    ) -> None:
        """Post a crossing transition to the incidents Talk room."""
        name    = crossing.get("name", "?")
        jur     = crossing.get("jurisdiction", "?")
        addr    = crossing.get("address", "")
        lat     = crossing.get("lat")
        lon     = crossing.get("lon")
        coords  = f" ({lat}, {lon})" if lat and lon else ""
        comment = (crossing.get("comment") or "").strip()
        verb    = {"closed": "CLOSED", "caution": "CAUTION", "open": "REOPENED"}.get(
            new_status,
            new_status.upper(),
        )
        lines = [f"[FLOODING {verb}] {name} ({jur})", f"{addr}{coords}"]
        if comment:
            lines.append(f"Note: {comment}")
        if old_status:
            lines.append(f"State: {old_status} -> {new_status}")
        msg = "\n".join(lines)

        payload = json.dumps({"message": msg}).encode()
        creds = base64.b64encode(f"{talk_user}:{talk_pass}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "OCS-APIRequest": "true",
            "Content-Type": "application/json",
        }
        room_token = talk_rooms["incidents"]
        url = f"{talk_base}/chat/{room_token}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            logger.info("[atxfloods] posted: %s %s", verb, name)
        except Exception as exc:
            logger.warning("[atxfloods] Talk post failed: %s", exc)

