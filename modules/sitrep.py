"""
modules/sitrep.py
~~~~~~~~~~~~~~~~~
Situation report formatters.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - local Python 3.8 fallback
    ZoneInfo = None

from modules.database import active_incidents, calls_for_sitrep

_CDT = ZoneInfo("America/Chicago") if ZoneInfo else timezone(timedelta(hours=-5), "CDT")

_SITREP_HIGH_KW = [
    "officer down",
    "shots fired",
    "shooting",
    "stabbing",
    "structure fire",
    "mass casualty",
    "hostage",
    "barricade",
    "10-99",
    "homicide",
    "body found",
    "found dead",
    "death investigation",
    "medical examiner",
]
_SITREP_MED_KW = ["crash", "collision", "hazmat", "fire", "rollover", "working fire"]


def build_sitrep(
    minutes: int = 60,
    calls_provider=calls_for_sitrep,
    incidents_provider=active_incidents,
    now_func=None,
    time_func=None,
) -> str:
    now_func = now_func or (lambda: datetime.now(_CDT))
    time_func = time_func or time.time
    calls = calls_provider(minutes)
    incidents = [i for i in incidents_provider() if not i.get("is_test")]

    lines = [
        f"SITUATION REPORT — last {minutes} min — {now_func().strftime('%Y-%m-%d %H:%M %Z')}",
        f"Total calls: {len(calls)}",
    ]

    if incidents:
        lines.append("")
        lines.append("*** ACTIVE INCIDENTS ***")
        for inc in incidents:
            age = int((time_func() - inc["ts_start"]) / 60)
            updated = int((time_func() - inc["ts_updated"]) / 60)
            agencies = ", ".join(json.loads(inc["agencies"] or "[]"))
            loc = f" @ {inc['location']}" if inc.get("location") else ""
            lines.append(
                f"  [{inc['itype']}]{loc} — started {age}m ago, "
                f"last activity {updated}m ago — agencies: {agencies}"
            )
            lines.append(f"  {inc['description']}")
        lines.append("*** END ACTIVE INCIDENTS ***")
    else:
        lines.append("  No active incidents.")

    if not calls:
        lines.append(f"\nNo calls in the last {minutes} minutes.")
        return "\n".join(lines)

    high_calls = [
        c
        for c in calls
        if (c.get("llm") or {}).get("priority") == "HIGH"
        or any(k in (c.get("transcript") or "").lower() for k in _SITREP_HIGH_KW)
    ]
    if high_calls:
        lines.append("")
        lines.append("*** HIGH PRIORITY ***")
        for c in high_calls[:10]:
            ts = datetime.fromtimestamp(c["ts"]).strftime("%H:%M")
            loc = f" @ {c['location']}" if c.get("location") else ""
            txt = (c.get("transcript") or "(no transcript)")[:150]
            llm_desc = (c.get("llm") or {}).get("description", "")
            lines.append(f"  🔴 {ts} {c['tag'] or c['tgid']}{loc}: {txt}")
            if llm_desc:
                lines.append(f"     → {llm_desc}")
        lines.append("")

    med_calls = [
        c
        for c in calls
        if c not in high_calls
        and (
            (c.get("llm") or {}).get("priority") == "MED"
            or any(k in (c.get("transcript") or "").lower() for k in _SITREP_MED_KW)
        )
    ]
    if med_calls:
        lines.append("*** NOTABLE ***")
        for c in med_calls[:10]:
            ts = datetime.fromtimestamp(c["ts"]).strftime("%H:%M")
            loc = f" @ {c['location']}" if c.get("location") else ""
            txt = (c.get("transcript") or "(no transcript)")[:120]
            lines.append(f"  🟡 {ts} {c['tag'] or c['tgid']}{loc}: {txt}")
        lines.append("")

    by_cat: dict[str, int] = {}
    for c in calls:
        cat = c["category"] or "Unknown"
        by_cat[cat] = by_cat.get(cat, 0) + 1

    lines.append("*** CALL VOLUME ***")
    for cat, count in sorted(by_cat.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count}")

    return "\n".join(lines)


def build_voice_sitrep(
    minutes: int = 60,
    calls_provider=calls_for_sitrep,
    incidents_provider=active_incidents,
    now_func=None,
    time_func=None,
) -> str:
    now_func = now_func or (lambda: datetime.now(_CDT))
    time_func = time_func or time.time
    calls = calls_provider(minutes)
    incidents = [i for i in incidents_provider() if not i.get("is_test")]

    now = now_func().strftime("%I:%M %p %Z").lstrip("0")
    parts = [f"Battle Buddy. Austin Metro situation report as of {now}."]

    if incidents:
        count = len(incidents)
        parts.append(f"{count} active {'incident' if count == 1 else 'incidents'}.")
        for inc in incidents:
            age = int((time_func() - inc["ts_start"]) / 60)
            loc = f" at {inc['location']}" if inc.get("location") else ""
            agencies = json.loads(inc.get("agencies") or "[]")
            agency_str = ", ".join(agencies[:3]) if agencies else "unknown agencies"
            age_str = f"{age} minutes ago" if age < 60 else f"{age // 60} hours ago"
            parts.append(
                f"{inc['itype'].replace('/', ' or ')}{loc}, "
                f"detected {age_str}, {agency_str} responding."
            )
    else:
        parts.append("No active incidents at this time.")

    if calls:
        by_cat: dict[str, int] = {}
        for c in calls:
            cat = c.get("category") or "Unknown"
            by_cat[cat] = by_cat.get(cat, 0) + 1
        top = sorted(by_cat.items(), key=lambda x: -x[1])[:4]
        summary = ", ".join(f"{cat} {n}" for cat, n in top)
        parts.append(f"{len(calls)} calls monitored in the past {minutes} minutes across {summary}.")
    else:
        parts.append(f"No calls received in the past {minutes} minutes.")

    return " ".join(parts)
