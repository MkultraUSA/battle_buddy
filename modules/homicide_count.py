"""
modules/homicide_count.py
~~~~~~~~~~~~~~~~~~~~~~~~~
Canonical area-wide homicide counting policy.

Rules
-----
1. Every counted homicide **must** have a valid ``source_url``.
   Incidents without a URL are excluded from the area total — they may be
   scanner-detected signals that have not been confirmed by an official source.

2. Deduplication across agencies / feeds:
   - The static seed file (``homicides_2026.json``) is the **authoritative
     canonical list**.  Each entry is uniquely identified by its ``url`` field.
   - Live DB incidents (``itype = 'HOMICIDE'``) are merged into the canonical
     set, but only when they carry a non-empty ``article_url`` (the resolved
     press-release link).
   - If a live DB incident shares the same URL (or a URL that normalises to the
     same path) as a seed entry, the seed entry wins (it was manually curated).
   - If two live DB incidents share the same URL only one is counted.

3. Agency breakdown + area total are computed **after** deduplication so the
   sum of per-agency counts equals the area total.

4. The public API (``/api/homicides``) exposes::

       {
         "homicides":        [ ... ],   # canonical deduped list (seed + live)
         "live":             [ ... ],   # raw live DB entries (unchanged),
         "total_area_homicides":  N,
         "homicides_by_agency":  { "APD": N, "TCSO": N, ... }
       }

Source_url validation
---------------------
A URL is considered valid when::

  - it is a non-empty string, AND
  - it starts with ``http://`` or ``https://``

Any entry that fails this check is silently excluded from the canonical count
but is preserved in the raw ``homicides`` / ``live`` arrays so the frontend
can display a "source missing" indicator if desired.
"""

from __future__ import annotations

import json
import os
import sqlite3
import urllib.parse
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SEED_PATH = "/opt/battlebuddy/homicides_2026.json"


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _is_valid_url(url: Any) -> bool:
    """Return True when *url* is a non-empty http(s) URL string."""
    if not isinstance(url, str):
        return False
    url = url.strip()
    if not url:
        return False
    return url.startswith("http://") or url.startswith("https://")


def _normalise_url(url: str) -> str:
    """Strip tracking query parameters and trailing slashes for dedup comparison."""
    url = url.strip()
    try:
        parsed = urllib.parse.urlparse(url)
        # Keep only scheme, netloc, path — drop query/fragment
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    except Exception:
        return url


# ---------------------------------------------------------------------------
# Seed loader
# ---------------------------------------------------------------------------

def load_seed(path: str = SEED_PATH) -> list[dict]:
    """Load the static seed JSON, returning an empty list on any error."""
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return data
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Live DB fetcher
# ---------------------------------------------------------------------------

def fetch_live_homicides(
    db_path: str,
    *,
    since: str = "2026-01-01",
) -> list[dict]:
    """Return confirmed homicides from the incidents table.

    Only rows with ``itype = 'HOMICIDE'``, non-null lat/lon, and
    ``is_test = 0`` are returned.  The result is a list of plain dicts
    suitable for merging into the canonical set.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, ts_start, itype, description, location, lat, lon,
                  article_url, agencies
           FROM incidents
           WHERE itype = 'HOMICIDE'
             AND lat IS NOT NULL AND lon IS NOT NULL
             AND ts_start > strftime('%s', ?)
             AND is_test = 0""",
        (since,),
    ).fetchall()
    conn.close()

    results: list[dict] = []
    for r in rows:
        results.append({
            "source": "scanner",
            "date": datetime.fromtimestamp(r["ts_start"]).strftime("%Y-%m-%d"),
            "itype": r["itype"],
            "summary": (r["description"] or "")[:120],
            "address": r["location"] or "",
            "lat": r["lat"],
            "lon": r["lon"],
            "url": r["article_url"] or "",
            "agencies": r["agencies"] or "[]",
            "_db_id": r["id"],
        })
    return results


# ---------------------------------------------------------------------------
# Canonical merge + dedup
# ---------------------------------------------------------------------------

def _agency_from_entry(entry: dict) -> str:
    """Extract the primary agency label from a homicide entry."""
    # Seed entries may carry a "source" field (e.g. "FOX 7 Austin / Pflugerville PD").
    # Live entries have an "agencies" JSON string.
    src = entry.get("source", "")
    if src and src != "scanner":
        # Try to extract a known agency from the free-text source field
        for known in ("APD", "TCSO", "UTPD", "DPS", "AFD", "Pflugerville PD"):
            if known in src:
                return known
        return src  # return the full source string as-is

    agencies_str = entry.get("agencies", "[]")
    try:
        agencies = json.loads(agencies_str) if isinstance(agencies_str, str) else agencies_str
        if isinstance(agencies, list) and agencies:
            return agencies[0]
    except Exception:
        pass
    return "Unknown"


def canonical_homicides(
    seed: list[dict],
    live: list[dict],
) -> tuple[list[dict], int, dict[str, int]]:
    """Merge seed + live lists into a deduped canonical list.

    Returns ``(canonical, total_area, by_agency)`` where:

    - ``canonical`` — the deduped list (seed entries take priority).
    - ``total_area`` — count of canonical entries that have a valid source_url.
    - ``by_agency`` — ``{ agency: count }`` over the same validated entries.

    Deduplication rules
    -------------------
    - Entries are keyed by ``_normalise_url(url)``.
    - Seed entries are inserted first; live entries fill in only new URLs.
    - Entries whose URL fails ``_is_valid_url()`` are **kept** in the
      canonical array (so the frontend can show them) but are **excluded**
      from ``total_area`` and ``by_agency``.
    """
    seen_urls: dict[str, int] = {}  # normalised_url → index in canonical
    canonical: list[dict] = []

    def _add(entry: dict) -> None:
        raw_url = entry.get("url", "")
        norm = _normalise_url(raw_url) if raw_url else ""
        if norm and norm in seen_urls:
            return  # already have this URL
        if norm:
            seen_urls[norm] = len(canonical)
        canonical.append(entry)

    # Seed entries first (authoritative)
    for entry in seed:
        _add(entry)

    # Live entries fill gaps
    for entry in live:
        _add(entry)

    # Compute validated totals
    total_area = 0
    by_agency: dict[str, int] = {}
    for entry in canonical:
        raw_url = entry.get("url", "")
        if not _is_valid_url(raw_url):
            continue  # skip from totals — no valid source
        total_area += 1
        agency = _agency_from_entry(entry)
        by_agency[agency] = by_agency.get(agency, 0) + 1

    return canonical, total_area, by_agency
