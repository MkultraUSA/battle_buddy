"""
modules/pollers/impl/reddit_intel.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Reddit citizen intel poller.

Migrated from modules/pollers_legacy.py as part of the BasePoller refactor.
The poller watches Austin-area Reddit feeds for public safety keywords,
stores matching posts, enriches tips with location and incident matches, and
alerts on high-confidence citizen reports.
"""

from __future__ import annotations

import html
import json
import logging
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import NamedTuple

from modules.pollers.base import BasePoller

logger = logging.getLogger("RedditIntelPoller")

REDDIT_INTERVAL: float = 300.0
REDDIT_FEEDS = [
    "https://www.reddit.com/r/Austin/new.rss",
    "https://www.reddit.com/r/AustinPolice/new.rss",
    "https://www.reddit.com/r/Austin_Texas/new.rss",
    "https://www.reddit.com/r/ATX/new.rss",
]
REDDIT_MEDIUM_KW = {
    "police", "apd", "afd", "crash", "accident", "fire", "smoke", "blocked",
    "road closed", "emergency", "cop", "cops", "officer", "helicopter",
    "air1", "star flight",
}

_AUSTIN_NEIGHBORHOODS = {
    "circle c": (30.1827, -97.8640),
    "mueller": (30.2932, -97.6987),
    "hyde park": (30.3091, -97.7341),
    "rundberg": (30.3614, -97.6985),
    "domain": (30.4023, -97.7230),
    "east 6th": (30.2598, -97.7200),
    "south congress": (30.2412, -97.7500),
    "decker lane": (30.2950, -97.6200),
    "cedar park": (30.5052, -97.8203),
}

_INTERSECTION_RE = re.compile(
    r"\b(?:at|near|corner of)\s+([A-Z0-9][\w\.\-]+(?:\s+[A-Z0-9][\w\.\-]+){0,3})\s+"
    r"(?:and|&|/|\\)\s+([A-Z0-9][\w\.\-]+(?:\s+[A-Z0-9][\w\.\-]+){0,3})",
    re.IGNORECASE,
)
_SLASH_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9\.\-]+(?:\s+[A-Z][A-Za-z0-9\.\-]+){0,3})\s*/\s*"
    r"([A-Z][A-Za-z0-9\.\-]+(?:\s+[A-Z][A-Za-z0-9\.\-]+){0,3})"
)
_ADDRESS_RE = re.compile(
    r"\b(\d{2,5}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+"
    r"(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Pkwy|Parkway|"
    r"Hwy|Highway|Trail|Ct|Court|Way))\b",
    re.IGNORECASE,
)


class RedditVerdict(NamedTuple):
    """Capture decision for one post.

    captured:   True only if the post is worth storing in reddit_intel.
    confidence: "high" (may alert), "medium" (stored, never alerts),
                or "none" (not stored).
    keywords:   comma-joined whole-word/phrase hits behind the decision.
    """

    captured: bool
    confidence: str
    keywords: str


# Curated concrete crime/incident phrases. Each one is sufficient, on its
# own, for a HIGH-confidence capture + alert. Matched whole-word/phrase only.
_REDDIT_STRONG_HIGH = (
    "shots fired", "shot fired", "shots heard", "heard shots", "shots rang out",
    "gunshots heard", "heard gunshots", "reports of shots",
    "gunshot", "gunshots", "gun shot", "gun shots",
    "gunfire", "gunman", "active shooter", "mass shooting", "school shooting",
    "shooting", "shootings", "shooter", "shootout", "shot dead",
    "stabbing", "stabbed", "stab wound", "stab wounds", "knifed",
    "homicide", "murder", "murdered",
    "carjacking", "carjacked", "kidnapping", "kidnapped", "abduction", "abducted",
    "robbery", "robbed", "armed robbery", "armed suspect", "armed man",
    "armed woman", "armed person", "armed with", "gun pulled", "pulled a gun",
    "pointed a gun", "brandished a gun",
    "assault", "assaulted", "sexual assault", "aggravated assault",
    "hit and run", "hit-and-run",
    "swat", "standoff", "stand-off", "hostage", "hostages",
    "barricade", "barricaded", "barricading",
    "officer down", "officer shot", "officer killed", "officer stabbed",
    "officer wounded", "deputy shot", "deputy down", "trooper shot",
    "person shot", "man shot", "woman shot", "teen shot", "child shot",
    "person stabbed", "man stabbed", "woman stabbed",
    "body found", "bodies found", "body discovered", "found dead", "dead body",
    "person down", "man down",
    "at large", "avoid the area", "shelter in place", "lockdown", "lock down",
    "evacuated", "evacuation", "evacuate",
    "explosion", "bomb threat", "bomb squad",
    "structure fire", "house fire", "apartment fire", "building fire",
    "car fire", "vehicle fire", "brush fire", "arson",
    "crime scene",
    # Property crime: the most common r/Austin community crime post is the
    # car break-in / burglary. These specific phrases are HIGH on their own.
    "broke into", "broken into", "break in", "break-in", "break ins",
    "break-ins", "burglary", "burglaries", "burglar", "burglars",
    "burglarized", "burglarised", "burglarize", "burglarise",
    "burglarizing", "burglarising",
    "home invasion", "home invasions",
    "mugged", "mugging", "muggings", "mugger",
    "pedestrian struck",
    "missing person", "missing persons",
    "package stolen", "packages stolen", "package theft", "porch pirate",
    "porch pirates",
    "catalytic converter", "catalytic converters",
    "domestic disturbance", "domestic disturbances",
    "911 call", "911 calls",
    # Restored base captures: the canonical r/Austin crime-report phrasing
    # "Police activity at ..." / "Suspect flees police ..." matched on base
    # via substring and must keep matching (whole-word now).
    "police activity", "suspect", "suspects",
)

# Extra medium context words beyond REDDIT_MEDIUM_KW (still whole-word only,
# still never sufficient alone).
_REDDIT_MEDIUM_EXTRA = {
    "ems", "deputy", "deputies", "trooper", "troopers", "dps",
    "sirens", "chopper", "collision", "wreck", "closure", "closures", "closed",
}

# Generic theft words: too ambiguous for HIGH on their own ("stolen valor",
# "theft-proof"), but a post pairing one with concrete detail ("car stolen
# overnight on Lakeline") is stored as medium intel, never an alert.
_REDDIT_PROPERTY_MEDIUM = (
    "stolen", "theft", "thefts", "thief", "thieves",
)

# Subjects that are clearly not community crime reports. Reject when no
# strong signal is present (a strong signal always wins: for a public-safety
# tool a false negative is worse than a false positive).
_REDDIT_TOPIC_EXCLUDE = (
    "protest", "protests", "protester", "protesters", "protesting",
    "rally", "rallies", "vigil", "vigils",
    "press conference", "news conference", "presser",
    "immigration", "deportation", "policy", "policies", "ordinance",
    "city council", "council member",
    "court", "courts", "courthouse", "judge", "judges", "jury",
    "trial", "trials", "sentencing", "lawsuit", "hearing",
    "hiring", "hire", "job", "jobs", "career", "careers",
    "recruit", "recruiting", "recruitment",
    "equipment", "bodycam", "body cam", "body camera", "budget", "funding",
    "photo", "photos", "picture", "pictures", "pic", "pics", "selfie",
    "sunset", "sunsets", "sunrise", "skyline",
    "pet", "pets", "dog", "dogs", "puppy", "cat", "cats", "kitten",
    "food", "foods", "restaurant", "restaurants", "taco", "tacos",
    "bbq", "barbecue", "coffee", "brunch", "dinner", "lunch", "breakfast",
    "event", "events", "festival", "concert", "parade", "marathon",
    "party", "celebration", "sxsw",
    "celebrity", "celebrities", "actor", "actress", "famous", "singer",
    "band", "movie", "movies", "film", "filming", "tv", "television",
    "netflix", "spotted", "sighting", "sightings", "autograph",
    "seeking", "looking for",
    "opinion", "rant",
    "for sale", "for rent", "roommate", "garage sale",
)

_PURSUIT_WORDS = (
    "pursuit", "pursuing", "chase", "chased", "chasing",
    "flee", "flees", "fled", "fleeing",
)
_AGENCY_WORDS = (
    "police", "apd", "officer", "officers", "deputy", "deputies",
    "trooper", "troopers", "dps",
)


def _phrase_re(phrase: str) -> re.Pattern:
    # Separator-aware boundaries: \b treats "-" as a boundary, so \bcop\b
    # matches inside "heli-cop-ter" and \bfire\b matches "fire-works".
    # Excluding "-" (and "_", "/") from the boundary keeps hyphen-fragment
    # evasions such as "heli-cop-ter", "fire-works", "un-armed" from matching.
    return re.compile(
        r"(?<![\w\-/])" + re.escape(phrase) + r"(?![\w\-/])", re.IGNORECASE
    )


_STRONG_RES = tuple((kw, _phrase_re(kw)) for kw in _REDDIT_STRONG_HIGH)
_PROPERTY_RES = tuple((kw, _phrase_re(kw)) for kw in _REDDIT_PROPERTY_MEDIUM)
_MEDIUM_RES = tuple(
    (kw, _phrase_re(kw))
    for kw in sorted(set(REDDIT_MEDIUM_KW) | _REDDIT_MEDIUM_EXTRA)
)
_TOPIC_RES = tuple((kw, _phrase_re(kw)) for kw in _REDDIT_TOPIC_EXCLUDE)
_PURSUIT_RES = tuple((kw, _phrase_re(kw)) for kw in _PURSUIT_WORDS)
_AGENCY_RES = tuple((kw, _phrase_re(kw)) for kw in _AGENCY_WORDS)

# Concrete detail, split in two tiers. _DETAIL_SPECIFIC_RE is the load-bearing
# one: street/highway tokens, suffix-less Austin corridors (Ben White has no
# street suffix), numbers, "X and Y" intersection phrasing (only when
# introduced by at/on/near/around/corner of, so "hit and run" is not
# misread as an intersection), and time words. A bare medium word needs one
# of these to be stored. _DETAIL_PLACE_RE holds city/neighbourhood names
# that appear in nearly every r/Austin title and therefore prove nothing on
# their own ("Helicopter over Austin" must not capture). _DETAIL_RE is the
# union, kept for backwards compatibility.
_DETAIL_SPECIFIC_RE = re.compile(
    r"\d"
    r"|\b(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr"
    r"|parkway|pkwy|highway|hwy|freeway|interstate|mopac|loop|terrace|trail"
    r"|circle|court|plaza|exit|ramp|frontage|block)\b"
    r"|\bi-?\d{1,3}\b|\bfm\s*\d+|\brr\s*\d+"
    r"|block of|corner of|intersection"
    r"|\b(?:at|on|near|around|corner of)\s+[a-z0-9][a-z0-9.'\-]*"
    r"(?:\s+[a-z0-9][a-z0-9.'\-]*){0,3}\s+(?:and|&|/)\s+[a-z0-9][a-z0-9.'\-]*"
    r"(?:\s+[a-z0-9][a-z0-9.'\-]*){0,3}"
    r"|\btoday\b|\btonight\b|\bovernight\b|\byesterday\b|\bmorning\b|\bevening\b"
    r"|\bafternoon\b|right now|just now|minutes? ago|hours? ago|o'clock|[ap]\.m\."
    r"|\blamar\b|\bburnet\b|ben white|\briverside\b|\bcongress\b|cesar chavez"
    r"|\bguadalupe\b|barton springs|\blakeline\b|\bparmer\b|\bslaughter\b"
    r"|\boltorf\b|south congress|east 6th|dirty 6th",
    re.IGNORECASE,
)
_DETAIL_PLACE_RE = re.compile(
    r"\bdowntown\b|\baustin\b|\batx\b|\bcampus\b|\buniversity\b"
    r"|\bdomain\b|\bmueller\b|\brundberg\b|hyde park|\bzilker\b"
    r"|\bsoco\b|cedar park",
    re.IGNORECASE,
)
_DETAIL_RE = re.compile(
    _DETAIL_SPECIFIC_RE.pattern + r"|" + _DETAIL_PLACE_RE.pattern,
    re.IGNORECASE,
)

# A title that is a bare question with no concrete detail.
_CHATTER_TITLE_RE = re.compile(
    r"\?\s*$"
    r"|^\s*(?:does anyone|did anyone|has anyone|anyone(?: know| have| hear| see| else)?"
    r"|is there|are there|what|where|when|why|how|who|which)\b",
    re.IGNORECASE,
)

# Road-closure intel: a closure word plus road/traffic context.
_CLOSURE_RE = re.compile(r"\b(?:closed|closure|closures|shut\s*down)\b", re.IGNORECASE)
_ROAD_CONTEXT_RE = re.compile(
    r"\broad\b|\blane\b|\blanes\b|\bhighway\b|\bfreeway\b|\binterstate\b"
    r"|\bi-?\d{1,3}\b|\bmopac\b|\bparkway\b|\bpkwy\b|\bavenue\b|\bave\b"
    r"|\bstreet\b|\brd\b|\bblvd\b|\bboulevard\b|\btraffic\b|\bexit\b|\bramp\b"
    r"|\bfrontage\b|\bloop\b",
    re.IGNORECASE,
)


# Benign-context guard for the highest-false-positive strong words. Each pair
# is (strong-phrase family, benign context that excuses it). A guarded hit is
# dropped UNLESS corroborated by a second, distinct strong phrase -- so
# "Sunset shooting spots downtown?" is dropped while "Shots fired downtown"
# (unambiguous phrase, no benign context) still alerts HIGH.
_BENIGN_GUARDS: tuple[tuple[frozenset, re.Pattern], ...] = (
    (
        frozenset({"shooting", "shootings", "shooter", "shooters", "shootout"}),
        re.compile(
            r"\b(?:range|ranges|spot|spots|photo|photos|photography|stars?|"
            r"scene|scenes|sunset|sunrise|film|filming|movie|movies)\b",
            re.IGNORECASE,
        ),
    ),
    (
        frozenset({"murder", "murdered"}),
        re.compile(r"\bmyster(?:y|ies)\b", re.IGNORECASE),
    ),
    (
        frozenset({"stabbing", "stabbed", "stab wound", "stab wounds", "knifed"}),
        re.compile(r"\bpains?\b|\bpainful\b", re.IGNORECASE),
    ),
    (
        frozenset({"swat"}),
        re.compile(r"\bcostumes?\b|\bcosplay\b", re.IGNORECASE),
    ),
    (
        frozenset({"robbery", "robbed", "armed robbery"}),
        re.compile(
            r"\btheme[ds]?\b|\bthemed\b|\bmovie\b|\bfilm\b|\bgame\b|\bbook\b",
            re.IGNORECASE,
        ),
    ),
    (
        frozenset({"assault", "assaulted", "sexual assault", "aggravated assault"}),
        re.compile(
            r"\btaste\b|\bbuds?\b|\bflavou?rs?\b|\bmenu\b", re.IGNORECASE
        ),
    ),
    (
        frozenset({"hit and run", "hit-and-run"}),
        re.compile(
            r"\bmovie\b|\bfilm\b|\bshow\b|\bepisode\b|\bsong\b|\bbook\b"
            r"|\bnovel\b|\bgame\b|\btv\b|\bnetflix\b",
            re.IGNORECASE,
        ),
    ),
)

# Strong phrases so ambiguous they need a corroborating signal whenever ANY
# topic-filter word is present (e.g. "Assault on my taste buds at this taco
# truck": assault + taco). Unambiguous phrases (shots fired, body found,
# suspect at large, ...) never take this path.
_AMBIGUOUS_STRONG = frozenset({
    "shooting", "shootings", "shooter", "shooters", "shootout",
    "stabbing", "stabbed", "knifed",
    "murder", "murdered", "homicide",
    "swat", "assault", "assaulted",
    "robbery", "robbed", "armed robbery",
    "hit and run", "hit-and-run",
})


def _benign_blocked(strong: list[str], topic: list[str], text: str) -> bool:
    """True when a strong hit is excused by benign context.

    Corroboration (two or more distinct strong phrases) always overrides the
    guard: a real report naming two independent signals still alerts.
    """
    if len(set(strong)) >= 2:
        return False
    hit = set(strong)
    for family, benign_rx in _BENIGN_GUARDS:
        if hit & family and benign_rx.search(text):
            return True
    if topic and hit and hit <= _AMBIGUOUS_STRONG:
        return True
    return False


def _matched(keywords_and_res: tuple, text: str) -> list[str]:
    return sorted({kw for kw, rx in keywords_and_res if rx.search(text)})


def reddit_matches(title: str, body: str | None) -> RedditVerdict:
    """Confidence-based capture decision (strict).

    Returns a RedditVerdict(captured, confidence, keywords) where confidence
    is "high" (store + may alert), "medium" (store, never alerts), or "none"
    (do not store). All matching is whole-word / whole-phrase via compiled
    regex with separator-aware boundaries (``(?<![\\w\\-/])``): ``fire``
    never matches ``fireworks``/``fire-works``, ``cop`` never matches
    ``copy``/``copper``/``helicopter``/``heli-cop-ter``, and ``armed`` never
    matches ``un-armed``.

    Decision order: benign-context guard (an ambiguous strong word excused
    by e.g. "range"/"mystery"/"pain"/"costume" is dropped unless a second
    distinct strong phrase corroborates it) -> HIGH on any surviving strong
    phrase (property-crime phrases such as "broke into" alert; generic
    "stolen"/"theft"/"thief" only store as medium with concrete detail) ->
    topic filter -> chatter guard (bare question with no SPECIFIC detail;
    a city/neighbourhood name alone does not count) -> closure / pursuit /
    medium+detail (medium, never alerts).

    NOTE: the return contract changed from ``(hi, matched, keywords)`` to a
    ``RedditVerdict`` named tuple; the sole caller (process_post) was updated
    in the same commit.
    """
    text = (title + " " + (body or ""))
    low = text.lower()

    strong = _matched(_STRONG_RES, low)
    medium = _matched(_MEDIUM_RES, low)
    prop = _matched(_PROPERTY_RES, low)
    topic = _matched(_TOPIC_RES, low)

    def _kw(*groups: list[str]) -> str:
        return ",".join(sorted({kw for g in groups for kw in g}))

    # A0. benign-context guard: an ambiguous strong word excused by benign
    # context is dropped unless corroborated (this check MUST run before the
    # strong branch below, otherwise the topic filter is bypassed whenever a
    # strong word is present).
    if strong and _benign_blocked(strong, topic, low):
        return RedditVerdict(False, "none", _kw(strong, medium, topic))

    # A. strong signal required for HIGH; a surviving strong signal overrides
    # topic/chatter guards (a false negative is worse than a false positive).
    if strong:
        return RedditVerdict(True, "high", _kw(strong, medium))

    # C. topic filter: everyday subjects are not crime reports.
    if topic:
        return RedditVerdict(False, "none", _kw(topic, medium))

    # D. chatter guard: bare question with no SPECIFIC detail. A bare
    # city/neighbourhood name ("Austin", "Rundberg") is not specific enough.
    if _CHATTER_TITLE_RE.search(title or "") and not _DETAIL_SPECIFIC_RE.search(low):
        return RedditVerdict(False, "none", _kw(medium, prop) or "chatter")

    # B. traffic closures with road context are stored as medium intel.
    if _CLOSURE_RE.search(low) and _ROAD_CONTEXT_RE.search(low):
        return RedditVerdict(True, "medium", _kw(medium, prop) or "closure")

    # B. an active pursuit naming an agency + concrete detail pages as high.
    if (
        any(rx.search(low) for _, rx in _PURSUIT_RES)
        and any(rx.search(low) for _, rx in _AGENCY_RES)
        and _DETAIL_SPECIFIC_RE.search(low)
    ):
        return RedditVerdict(True, "high", _kw(medium, ["pursuit"]))

    # B. bare medium words alone are never enough: require SPECIFIC detail
    # (street, highway, corridor, number, intersection, or time) -- a bare
    # city/neighbourhood name does not count. Generic theft words ("stolen",
    # "theft", "thief") clear the same bar and are stored as medium.
    if (medium or prop) and _DETAIL_SPECIFIC_RE.search(low):
        return RedditVerdict(True, "medium", _kw(medium, prop))

    return RedditVerdict(False, "none", _kw(medium, prop))


def nominatim_geocode(query: str) -> tuple[float | None, float | None]:
    """Geocode a free-form Austin string. Returns (lat, lon) or (None, None)."""
    try:
        q = urllib.parse.quote_plus(f"{query} Austin TX")
        url = f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1"
        req = urllib.request.Request(url, headers={"User-Agent": "BattleBuddy/2.0"})
        data = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8"))
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as exc:
        logger.warning("[reddit] nominatim error for %r: %s", query, exc)
    return None, None


def extract_tip_location(title: str | None, body: str | None) -> tuple[str | None, float | None, float | None]:
    """Extract a location from a Reddit post. Returns (location, lat, lon)."""
    text = f"{title or ''} {body or ''}".strip()
    if not text:
        return None, None, None
    low = text.lower()

    for name, (lat, lon) in _AUSTIN_NEIGHBORHOODS.items():
        if name in low:
            return name.title(), lat, lon

    for pattern in (_INTERSECTION_RE, _SLASH_RE, _ADDRESS_RE):
        match = pattern.search(text)
        if not match:
            continue
        loc = match.group(1).strip()
        if pattern is not _ADDRESS_RE:
            loc = f"{loc} & {match.group(2).strip()}"
        lat, lon = nominatim_geocode(loc)
        if lat is not None:
            return loc, lat, lon

    return None, None, None


def reddit_match_incident(title: str, body: str | None, ts: float, db_path: str) -> tuple[int | None, float]:
    """Score a Reddit post against incidents within +/-4h."""
    text = (title + " " + (body or "")).lower()
    window = 4 * 3600
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, ts_start, itype, description, location FROM incidents "
        "WHERE ts_start BETWEEN ? AND ? AND is_test=0",
        (ts - window, ts + window),
    ).fetchall()
    conn.close()

    type_kw = {
        "SHOOTING": ["shooting", "shot", "shots", "fired", "gun", "gunshot", "bullet", "gunfire"],
        "STABBING": ["stabbing", "stabbed", "knife", "stab"],
        "CRASH/COLLISION": ["crash", "accident", "collision", "wreck"],
        "STRUCTURE FIRE": ["fire", "smoke", "burning", "flames", "blaze"],
        "HOMICIDE": ["murder", "homicide", "killed", "dead", "body found"],
        "AIR ASSET ACTIVE": ["helicopter", "air1", "star flight", "chopper", "aircraft"],
        "PURSUIT": ["pursuit", "chase", "fleeing", "high speed"],
        "OFFICER DOWN": ["officer down", "officer shot", "cop shot"],
    }

    best_score, best_id = 0.0, None
    for inc_id, ts_start, itype, description, location in rows:
        score = 0.0
        for kw in type_kw.get(itype, []):
            if kw in text:
                score += 4
                break
        if location:
            for lw in (w.lower().strip(".,") for w in location.split() if len(w) > 4):
                if lw in text:
                    score += 6
        if description:
            words = {w.lower().strip(".,") for w in description.split() if len(w) > 5}
            score += min(len(words & set(text.split())) * 1.5, 6)
        diff = abs(ts - ts_start) / 3600
        score += 5 if diff < 0.5 else (3 if diff < 1 else (1 if diff < 2 else 0))
        if score > best_score:
            best_score, best_id = score, inc_id

    return (best_id, round(best_score, 1)) if best_score >= 8 else (None, 0.0)


class RedditIntelPoller(BasePoller):
    """Poll Austin-area Reddit feeds for citizen intel."""

    NAME: str = "reddit-intel"
    INTERVAL: float = REDDIT_INTERVAL

    def __init__(self, feeds: list[str] | None = None) -> None:
        super().__init__(interval=self.INTERVAL)
        self.feeds = feeds or list(REDDIT_FEEDS)
        self._schema_ready = False

    def run(self) -> None:
        from modules.config import DB_PATH  # noqa: PLC0415
        from modules.incident_engine import _haversine_km  # noqa: PLC0415
        from modules.pollers_legacy import send_dm_alert  # noqa: PLC0415

        if not self._schema_ready:
            self.ensure_schema(DB_PATH)
            self._schema_ready = True

        fetch_errors = []
        for feed_url in self.feeds:
            try:
                root = self.fetch_feed(feed_url)
            except Exception as exc:
                logger.warning("[reddit] fetch error %s: %s", feed_url, exc)
                fetch_errors.append(exc)
                continue
            self.process_feed(root, feed_url, DB_PATH, send_dm_alert)

        try:
            self.tip_recheck(DB_PATH, _haversine_km)
        except Exception as exc:
            logger.warning("[reddit] tip_recheck loop error: %s", exc)

        if fetch_errors:
            raise RuntimeError(f"Reddit fetch failed for {len(fetch_errors)} feed(s)") from fetch_errors[-1]

    @staticmethod
    def ensure_schema(db_path: str) -> None:
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE IF NOT EXISTS reddit_intel (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            post_id TEXT UNIQUE,
            subreddit TEXT,
            title TEXT,
            url TEXT,
            author TEXT,
            body TEXT,
            keywords TEXT,
            notified INTEGER DEFAULT 0
        )""")
        for col_sql in [
            "ALTER TABLE reddit_intel ADD COLUMN incident_id INTEGER",
            "ALTER TABLE reddit_intel ADD COLUMN match_score REAL DEFAULT 0",
            "ALTER TABLE reddit_intel ADD COLUMN tip_lat REAL",
            "ALTER TABLE reddit_intel ADD COLUMN tip_lon REAL",
            "ALTER TABLE reddit_intel ADD COLUMN tip_location TEXT",
            "ALTER TABLE reddit_intel ADD COLUMN tip_status TEXT DEFAULT 'new'",
            "ALTER TABLE reddit_intel ADD COLUMN tip_ts_start REAL",
            "ALTER TABLE reddit_intel ADD COLUMN tip_ts_cleared REAL",
            "ALTER TABLE reddit_intel ADD COLUMN tip_summary TEXT",
            "ALTER TABLE reddit_intel ADD COLUMN confidence TEXT DEFAULT 'medium'",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass
        conn.commit()
        conn.close()

    @staticmethod
    def fetch_feed(feed_url: str):
        req = urllib.request.Request(
            feed_url,
            headers={"User-Agent": "BattleBuddy/2.0 (contact: admin@battlebuddy.news)"},
        )
        xml_bytes = urllib.request.urlopen(req, timeout=15).read()
        return ET.fromstring(xml_bytes.decode("utf-8", errors="replace"))

    def process_feed(self, root, feed_url: str, db_path: str, send_alert) -> None:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        subreddit = feed_url.split("/r/")[1].split("/")[0]

        for entry in root.findall("atom:entry", ns):
            post = self.parse_entry(entry, subreddit, ns)
            if post is None:
                continue
            self.process_post(post, db_path, send_alert)

    @staticmethod
    def parse_entry(entry, subreddit: str, ns: dict[str, str]) -> dict | None:
        post_id_raw = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip()
        post_id = post_id_raw.split("_")[-1] if "_" in post_id_raw else post_id_raw
        title = html.unescape((entry.findtext("atom:title", default="", namespaces=ns) or "").strip())
        link_el = entry.find("atom:link[@rel='alternate']", ns)
        url = link_el.attrib.get("href", "") if link_el is not None else ""
        if not url:
            any_link = entry.find("atom:link", ns)
            if any_link is not None:
                url = any_link.attrib.get("href", "") or ""
        if not url and post_id:
            url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
        author_el = entry.find("atom:author/atom:name", ns)
        author = author_el.text.strip() if author_el is not None else ""
        content_el = entry.find("atom:content", ns)
        body_html = (content_el.text or "") if content_el is not None else ""
        body = re.sub(r"<[^>]+>", " ", body_html)
        body = html.unescape(body).strip()[:800]
        if not post_id or not title:
            return None
        return {
            "post_id": post_id,
            "subreddit": subreddit,
            "title": title,
            "url": url,
            "author": author,
            "body": body,
        }

    def process_post(self, post: dict, db_path: str, send_alert) -> bool:
        verdict = reddit_matches(post["title"], post["body"])
        if not verdict.captured:
            return False

        conn = sqlite3.connect(db_path)
        existing = conn.execute(
            "SELECT notified FROM reddit_intel WHERE post_id=?",
            (post["post_id"],),
        ).fetchone()
        if existing is not None:
            conn.close()
            return False

        now_ts = time.time()
        try:
            conn.execute(
                "INSERT INTO reddit_intel "
                "(ts,post_id,subreddit,title,url,author,body,keywords,notified,"
                "tip_status,tip_ts_start,confidence) "
                "VALUES (?,?,?,?,?,?,?,?,0,'investigating',?,?)",
                (
                    now_ts,
                    post["post_id"],
                    post["subreddit"],
                    post["title"],
                    post["url"],
                    post["author"],
                    post["body"][:500],
                    verdict.keywords,
                    now_ts,
                    verdict.confidence,
                ),
            )
        except sqlite3.OperationalError:
            # Database predates the confidence column (ensure_schema not yet
            # run): fall back to the legacy column list.
            conn.execute(
                "INSERT INTO reddit_intel "
                "(ts,post_id,subreddit,title,url,author,body,keywords,notified,tip_status,tip_ts_start) "
                "VALUES (?,?,?,?,?,?,?,?,0,'investigating',?)",
                (
                    now_ts,
                    post["post_id"],
                    post["subreddit"],
                    post["title"],
                    post["url"],
                    post["author"],
                    post["body"][:500],
                    verdict.keywords,
                    now_ts,
                ),
            )
        conn.commit()
        conn.close()
        logger.info(
            "[reddit] NEW %s: %s", verdict.confidence, post["title"][:80]
        )

        self.enrich_tip_location(post, db_path)
        self.enrich_incident_match(post, db_path)
        if verdict.confidence == "high":
            self.send_high_confidence_alert(post, verdict.keywords, db_path, send_alert)
        return True

    @staticmethod
    def enrich_tip_location(post: dict, db_path: str) -> None:
        try:
            loc, lat, lon = extract_tip_location(post["title"], post["body"])
            if not loc:
                return
            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE reddit_intel SET tip_location=?, tip_lat=?, tip_lon=? WHERE post_id=?",
                (loc, lat, lon, post["post_id"]),
            )
            conn.commit()
            conn.close()
            logger.info("[reddit] tip %s geocoded -> %s (%s,%s)", post["post_id"], loc, lat, lon)
        except Exception as exc:
            logger.warning("[reddit] geocode error for %s: %s", post["post_id"], exc)

    @staticmethod
    def enrich_incident_match(post: dict, db_path: str) -> None:
        inc_id, inc_score = reddit_match_incident(post["title"], post["body"], time.time(), db_path)
        if not inc_id:
            return
        conn = sqlite3.connect(db_path)
        conn.execute(
            "UPDATE reddit_intel SET incident_id=?,match_score=? WHERE post_id=?",
            (inc_id, inc_score, post["post_id"]),
        )
        conn.commit()
        conn.close()
        logger.info("[reddit] matched post %s -> incident #%s (score %s)", post["post_id"], inc_id, inc_score)

    @staticmethod
    def send_high_confidence_alert(post: dict, keywords: str, db_path: str, send_alert) -> None:
        msg = (
            f"Reddit Citizen Report - r/{post['subreddit']}\n"
            f"{post['title']}\n"
            f"Keywords: {keywords}\n"
            f"{post['url']}"
        )
        threading.Thread(
            target=send_alert,
            args=("CITIZEN REPORT", msg, post["title"], "Reddit", "general"),
            daemon=True,
        ).start()
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE reddit_intel SET notified=1 WHERE post_id=?", (post["post_id"],))
        conn.commit()
        conn.close()

    @staticmethod
    def tip_recheck(db_path: str, haversine_km) -> None:
        """Re-check investigating tips against radio calls and incidents."""
        now = time.time()
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT post_id, title, body, tip_lat, tip_lon, tip_location, tip_ts_start "
                "FROM reddit_intel WHERE tip_status='investigating'",
            ).fetchall()
            conn.close()
        except Exception as exc:
            logger.warning("[reddit] tip_recheck load error: %s", exc)
            return

        for post_id, title, body, tip_lat, tip_lon, tip_location, tip_ts_start in rows:
            if not tip_ts_start:
                continue
            if now - tip_ts_start > 7200:
                RedditIntelPoller._mark_tip_no_data(db_path, now, post_id)
                continue
            nearby_calls = RedditIntelPoller._nearby_calls(db_path, tip_ts_start, tip_lat, tip_lon, haversine_km)
            inc_id, inc_score = reddit_match_incident(title or "", body or "", tip_ts_start, db_path)
            if nearby_calls or inc_id:
                RedditIntelPoller._mark_tip_matched(db_path, now, post_id, nearby_calls, inc_id, inc_score)

    @staticmethod
    def _nearby_calls(db_path: str, tip_ts_start: float, tip_lat, tip_lon, haversine_km) -> list:
        nearby_calls = []
        if tip_lat is None or tip_lon is None:
            return nearby_calls
        try:
            conn = sqlite3.connect(db_path)
            call_rows = conn.execute(
                "SELECT id, ts, tag, category, transcript, lat, lon, location FROM calls "
                "WHERE ts >= ? AND lat IS NOT NULL AND lon IS NOT NULL",
                (tip_ts_start - 7200,),
            ).fetchall()
            conn.close()
            for cr in call_rows:
                cid, cts, ctag, ccat, ctranscript, clat, clon, cloc = cr
                try:
                    dist = haversine_km(tip_lat, tip_lon, clat, clon)
                except Exception:
                    continue
                if dist <= 0.8:
                    nearby_calls.append((cid, cts, ctag, ccat, ctranscript, cloc))
        except Exception as exc:
            logger.warning("[reddit] tip_recheck calls error: %s", exc)
        return nearby_calls

    @staticmethod
    def _mark_tip_no_data(db_path: str, now: float, post_id: str) -> None:
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "UPDATE reddit_intel SET tip_status='no_data', tip_ts_cleared=?, "
                "tip_summary=? WHERE post_id=?",
                (now, "Monitored for 2 hours - nothing detected on radio", post_id),
            )
            conn.commit()
            conn.close()
            logger.info("[reddit] tip %s -> no_data (timeout)", post_id)
        except Exception as exc:
            logger.warning("[reddit] tip_recheck timeout error: %s", exc)

    @staticmethod
    def _mark_tip_matched(db_path: str, now: float, post_id: str, nearby_calls: list, inc_id, inc_score) -> None:
        parts = []
        if inc_id:
            try:
                conn = sqlite3.connect(db_path)
                irow = conn.execute(
                    "SELECT itype, location FROM incidents WHERE id=?",
                    (inc_id,),
                ).fetchone()
                conn.close()
                if irow:
                    itype, iloc = irow
                    parts.append(f"{itype} detected on radio" + (f" near {iloc}" if iloc else ""))
            except Exception:
                pass
        if nearby_calls:
            parts.append(f"{len(nearby_calls)} related radio call(s) within 0.5 mi")
        elif not parts:
            parts.append("Possible radio match")
        summary = " - ".join(parts) + "."

        try:
            conn = sqlite3.connect(db_path)
            if inc_id:
                conn.execute(
                    "UPDATE reddit_intel SET tip_status='matched', tip_ts_cleared=?, "
                    "tip_summary=?, incident_id=?, match_score=? WHERE post_id=?",
                    (now, summary, inc_id, inc_score, post_id),
                )
            else:
                conn.execute(
                    "UPDATE reddit_intel SET tip_status='matched', tip_ts_cleared=?, "
                    "tip_summary=? WHERE post_id=?",
                    (now, summary, post_id),
                )
            conn.commit()
            conn.close()
            logger.info("[reddit] tip %s -> matched: %s", post_id, summary)
        except Exception as exc:
            logger.warning("[reddit] tip_recheck update error: %s", exc)
