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

Seed path contract
------------------
The seed location is never hardcoded here. :func:`resolve_seed_path` defers to
``modules.config.resolve_homicide_seed_path`` (the single source of truth for
``HOMICIDE_SEED_PATH`` / ``BATTLE_BUDDY_DATA_DIR`` / ``BATTLE_BUDDY_HOME``),
and is resolved at call time so a sandbox clone with ``BATTLE_BUDDY_HOME``
redirected can never read the production tree. With no env override the
production default is unchanged: ``/opt/battlebuddy/homicides_2026.json``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import urllib.parse
from datetime import datetime
from typing import Any

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class HomicideSeedUnavailable(RuntimeError):
    """Raised when the curated homicide seed cannot be read.

    The seed is the authoritative area-wide homicide dataset, so a missing,
    corrupt, or unwritable seed is a deployment fault. The canonical read
    path (:func:`load_seed_strict`) raises this instead of reporting an empty
    seed, which would silently publish a total of zero confirmed homicides.
    """


def resolve_seed_path(path: str | None = None) -> str:
    """Return the homicide seed path to use.

    An explicit *path* (as passed by tests and one-off callers) wins; otherwise
    the shared environment/data-dir precedence from ``modules.config`` is
    applied. The import is deferred so this module stays importable before
    application config is initialised.
    """
    if path:
        return path
    from modules.config import resolve_homicide_seed_path  # noqa: PLC0415

    return resolve_homicide_seed_path()


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

def load_seed(path: str | None = None) -> list[dict]:
    """Load the static seed JSON, returning an empty list on any error.

    Tolerant variant, kept for callers that genuinely want "no curated
    history" semantics. The canonical read path (the public and premium
    homicide APIs) must use :func:`load_seed_strict` instead so a missing or
    corrupt seed fails visibly instead of reporting zero homicides.
    """
    path = resolve_seed_path(path)
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


def load_seed_strict(path: str | None = None) -> list[dict]:
    """Load the static seed JSON, raising :class:`HomicideSeedUnavailable`.

    Canonical read path for the homicide APIs: a missing, unreadable, or
    non-list seed raises so the caller can answer with an explicit error
    instead of a silent total of zero. The resolved path comes from
    :func:`resolve_seed_path`, so an env/data-dir override redirects the read
    and a sandbox clone never touches ``/opt/battlebuddy``.
    """
    path = resolve_seed_path(path)
    if not os.path.exists(path):
        raise HomicideSeedUnavailable(
            f"homicide seed not found at {path!r}; set HOMICIDE_SEED_PATH or "
            f"BATTLE_BUDDY_HOME to the deployment data directory"
        )
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise HomicideSeedUnavailable(
            f"homicide seed at {path!r} is unreadable: {exc}"
        ) from exc
    if not isinstance(data, list):
        raise HomicideSeedUnavailable(
            f"homicide seed at {path!r} must be a JSON list, got {type(data).__name__}"
        )
    return data


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


def _fetch_recent_homicide_rows(db_path: str, since: str = "2026-01-01") -> list[tuple]:
    """Return ``(ts_start, location)`` for confirmed homicides, newest first."""
    conn = sqlite3.connect(db_path, timeout=5.0)
    rows = conn.execute(
        """SELECT ts_start, location FROM incidents
           WHERE itype = 'HOMICIDE'
             AND lat IS NOT NULL AND lon IS NOT NULL
             AND ts_start > strftime('%s', ?)
             AND is_test = 0
           ORDER BY ts_start DESC""",
        (since,),
    ).fetchall()
    conn.close()
    return rows


def _seed_victim_count(entry: Any) -> int:
    """Return how many victims one seed *entry* is worth.

    A curated entry that is not a dict (a stray string, a number, a nested list
    from a bad hand-edit) carries no count at all, so it is worth 0: it is
    skipped rather than guessed at and rather than allowed to raise, because the
    premium dashboard must not 500 over one corrupt line and counting an entry
    nobody can read would publish a number that cannot be verified. A dict with
    an unusable ``count`` still stands for the single incident it describes.
    """
    if not isinstance(entry, dict):
        return 0
    try:
        return int(entry.get("count", 1))
    except (TypeError, ValueError):
        return 1


def premium_homicide_summary(db_path: str, *, since: str = "2026-01-01") -> dict:
    """Build the premium homicide YTD summary from the same resolved seed.

    ``ytd`` is the curated seed count plus the live geocoded homicide count,
    and ``last`` is the most recent homicide (live rows win, the newest readable
    seed entry is the fallback).

    Non-dict seed entries are skipped, not fatal: they can be neither counted as
    victims nor sorted by date, and one bad line must not take the dashboard down
    (the public ``/api/homicides`` path already has its own answer for a wholly
    unreadable seed — a 503).

    Raises :class:`HomicideSeedUnavailable` when the seed cannot be read: the
    premium dashboard must not be told the area total is zero because of a
    deployment fault. The route in ``audio_receiver.py`` turns that into an
    explicit 503 instead of a fabricated number, and keeps the exception text
    (which names the absolute seed path) in the server log rather than the
    response body.
    """
    seed = load_seed_strict()
    entries = [e for e in seed if isinstance(e, dict)]
    seed_count = sum(_seed_victim_count(e) for e in entries)

    rows = _fetch_recent_homicide_rows(db_path, since=since)

    last: dict | None = None
    if rows:
        ts_start, location = rows[0]
        last = {
            "date":     datetime.fromtimestamp(ts_start).strftime("%b %d"),
            "location": location or "",
        }
    elif seed_count:
        newest = sorted(entries, key=lambda e: str(e.get("date") or ""))[-1]
        raw_date = str(newest.get("date") or "")
        try:
            pretty_date = datetime.strptime(raw_date, "%Y-%m-%d").strftime("%b %d")
        except ValueError:
            pretty_date = raw_date
        last = {
            "date":     pretty_date,
            "location": str(newest.get("address") or ""),
        }

    return {
        "ytd":   seed_count + len(rows),
        "year":  2026,
        "last":  last,
    }


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


# ---------------------------------------------------------------------------
# Means-of-death classification (server-side, tested)
# ---------------------------------------------------------------------------
# Previously this ran in inline page JavaScript inside a Python triple-quoted
# string, where \\b regex escapes were silently eaten at import time and every
# marker rendered UNKNOWN grey. Keep regexes HERE, never in page templates.

MEANS_COLORS = {
    "SHOOTING": "#ef4444",
    "STABBING": "#818cf8",
    "OTHER": "#a8a29e",
    "UNKNOWN": "#a8a29e",
}

_SHOOT_RE = re.compile(
    r"\bshot\b|\bshooting\b|\bgunshot\b|\bfired\b|\bfirearm\b|\bgunman\b"
)
_STAB_RE = re.compile(
    r"\bstabbed\b|\bstabbing\b|\bstab\b|\bknife\b|\bknifed\b|\bslashed\b"
)
_OTHER_RE = re.compile(
    r"blunt|trauma|beaten|\bbeat\b|strang|asphyx|fentanyl|overdose|toxic|bludgeon|suffocat"
)


def means_of(summary=None, victim=None) -> str:
    """Classify means of death from press-release text.

    Returns one of SHOOTING / STABBING / OTHER / UNKNOWN. Word boundaries
    matter ('constable' is not a stabbing); a shooting mention wins over a
    knife mention (e.g. victim shot by driver during knife encounter).
    """
    t = f"{summary or ''} {victim or ''}".lower()
    if _SHOOT_RE.search(t):
        return "SHOOTING"
    if _STAB_RE.search(t):
        return "STABBING"
    if _OTHER_RE.search(t):
        return "OTHER"
    return "UNKNOWN"
