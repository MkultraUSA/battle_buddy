"""
modules/pollers/impl/austin_events.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Austin major events weekly digest poller.

Migrated from modules/pollers_legacy.py as part of the BasePoller refactor.
The poller reads a JSON event list, tracks the last posted window, and posts
a Talk digest when the 7-day event window changes or the weekly cadence is due.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.request
from datetime import date, datetime, timedelta

from modules.pollers.base import BasePoller

logger = logging.getLogger("AustinEventsPoller")

AUSTIN_EVENTS_JSON = "/opt/battlebuddy/austin_major_events.json"
AUSTIN_EVENTS_STATE = "/opt/battlebuddy/austin_events_state.json"
AUSTIN_EVENTS_POLL: float = 6 * 3600.0
AUSTIN_EVENTS_WINDOW = 7


def _load_events(path: str = AUSTIN_EVENTS_JSON) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("[events] load failed: %s", exc)
        return {"events": []}


def _upcoming_events(doc: dict, today: date) -> list[dict]:
    horizon = today + timedelta(days=AUSTIN_EVENTS_WINDOW)
    events = []
    for event in doc.get("events", []):
        try:
            start = date.fromisoformat(event["start"])
            end = date.fromisoformat(event.get("end") or event["start"])
        except Exception:
            continue
        if start <= horizon and end >= today:
            events.append(event)
    events.sort(key=lambda item: item.get("start", ""))
    return events


def _load_state(path: str = AUSTIN_EVENTS_STATE) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"last_post_date": None, "last_event_ids": []}


def _save_state(state: dict, path: str = AUSTIN_EVENTS_STATE) -> None:
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
    except Exception as exc:
        logger.warning("[events] state save failed: %s", exc)


def _format_events(events: list[dict], today: date) -> str | None:
    if not events:
        return None
    lines = [f"This week in Austin (window: {today.isoformat()} + {AUSTIN_EVENTS_WINDOW} days):"]
    for event in events:
        start = event.get("start", "?")
        end = event.get("end") or start
        date_range = start if end == start else f"{start} -> {end}"
        extras = []
        tier = event.get("tier")
        if tier == "major":
            extras.append("MAJOR regional impact")
        elif tier == "large":
            extras.append("large impact")
        if event.get("blast_radius_mi"):
            extras.append(f"{event['blast_radius_mi']}mi radius")
        if event.get("venue"):
            extras.append(event["venue"])
        tail = f" ({', '.join(extras)})" if extras else ""
        lines.append(f"  - {date_range}  {event.get('name', '?')}{tail}")
    return "\n".join(lines)


class AustinEventsPoller(BasePoller):
    """Post a weekly Austin major-events digest when the active window changes."""

    NAME: str = "austin-events"
    INTERVAL: float = AUSTIN_EVENTS_POLL

    def __init__(
        self,
        events_path: str = AUSTIN_EVENTS_JSON,
        state_path: str = AUSTIN_EVENTS_STATE,
    ) -> None:
        super().__init__(interval=self.INTERVAL)
        self.events_path = events_path
        self.state_path = state_path

    def run(self) -> None:
        from modules.config import TALK_BASE, TALK_PASS, TALK_ROOMS, TALK_USER  # noqa: PLC0415

        today = self._today()
        doc = _load_events(self.events_path)
        events = _upcoming_events(doc, today)
        state = _load_state(self.state_path)

        current_ids = [event.get("id") for event in events]
        last_date_s = state.get("last_post_date")
        last_ids = state.get("last_event_ids", [])
        days_since = self._days_since(today, last_date_s)

        should_post = False
        reason = None
        if events and current_ids != last_ids:
            should_post = True
            reason = "event list changed"
        elif events and (days_since is None or days_since >= 7):
            should_post = True
            reason = "weekly cadence"

        if should_post:
            msg = _format_events(events, today)
            if msg:
                self._post_to_talk(msg, TALK_BASE, TALK_USER, TALK_PASS, TALK_ROOMS)
                _save_state(
                    {
                        "last_post_date": today.isoformat(),
                        "last_event_ids": current_ids,
                    },
                    self.state_path,
                )
                logger.info("[events] posted (%s): %s events", reason, len(events))
        else:
            logger.info(
                "[events] quiet: %s events in window, last posted %s (%sd ago)",
                len(events),
                last_date_s,
                days_since,
            )

    @staticmethod
    def _today() -> date:
        try:
            import zoneinfo

            return datetime.now(zoneinfo.ZoneInfo("America/Chicago")).date()
        except Exception:
            return datetime.now().date()

    @staticmethod
    def _days_since(today: date, last_date_s: str | None) -> int | None:
        if not last_date_s:
            return None
        try:
            return (today - date.fromisoformat(last_date_s)).days
        except Exception:
            return None

    @staticmethod
    def _post_to_talk(
        msg: str,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
    ) -> None:
        payload = json.dumps({"message": msg}).encode()
        creds = base64.b64encode(f"{talk_user}:{talk_pass}".encode()).decode()
        headers = {
            "Authorization": f"Basic {creds}",
            "OCS-APIRequest": "true",
            "Content-Type": "application/json",
        }
        url = f"{talk_base}/chat/{talk_rooms['incidents']}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            logger.info("[events] weekly summary posted")
        except Exception as exc:
            logger.warning("[events] Talk post failed: %s", exc)

