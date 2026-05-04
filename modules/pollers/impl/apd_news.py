"""
modules/pollers/impl/apd_news.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
APD Press Release poller — polls Google News RSS for APD press releases
(homicides, shootings, stabbings) and Austin traffic fatality news.

NOTE: austintexas.gov/news is behind Incapsula CDN which hard-blocks the
Some VPS/cloud IP ranges may be blocked. Do NOT attempt to scrape austintexas.gov
directly from this server — it will always return 403. Google News RSS
aggregates APD press releases from KXAN, KVUE, AAS, etc. with no bot-detect.

Migrated from modules/pollers.py (apd_news_thread) as part of the
SOA / BasePoller refactor.

For each new article the poller:
  - Deduplicates against the ``apd_seen`` DB table (persistent across restarts).
  - Filters headlines by keyword list (_APD_HEADLINE_KW).
  - Resolves the real article URL (source RSS → Google CSE → Google /articles/).
  - Optionally fetches the article body via the Pi5 residential-IP fetch agent.
  - Extracts address from article body and geocodes it.
  - Determines the incident type (itype) from the headline.
  - Tries to match the article to a recent radio-detected incident in the DB.
  - If matched: stores the article link and posts a "press coverage" message to Talk.
  - If unmatched: creates a new incident record, posts to Talk, sends DM alerts,
    and places an ATAK marker when coordinates are available.

A secondary sub-poll fetches Austin traffic fatality news and links articles
to existing radio incidents (no new incident creation on no-match).

Circular-import safety: all imports from modules.config, modules.incident_engine,
modules.geocoding, and modules.pollers (send_dm_alert) are deferred inside
run() so this module is safely importable before application config is
initialised (e.g. during tests).
"""

from __future__ import annotations

import base64
import datetime
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from email import utils as _email_utils

from modules.pollers.base import BasePoller

logger = logging.getLogger("APDNewsPoller")

# ---------------------------------------------------------------------------
# Module-level constants — literal copies; do NOT import from modules.config
# at load time to avoid circular imports during bootstrap.
# ---------------------------------------------------------------------------

APD_NEWS_URL: str = (
    "https://news.google.com/rss/search"
    "?q=APD+Austin+%22press+release%22+(homicide+OR+shooting+OR+stabbing)"
    "&hl=en-US&gl=US&ceid=US:en"
)
APD_NEWS_INTERVAL: float = 300.0  # poll every 5 minutes

# Broader Google News search for Austin traffic fatalities
TRAFFIC_NEWS_URL: str = (
    "https://news.google.com/rss/search"
    "?q=Austin+Texas+(fatal+crash+OR+pedestrian+killed+OR+hit-and-run)"
    "&hl=en-US&gl=US&ceid=US:en"
)

_ARTICLE_MAX_AGE_SECS: float = 72 * 3600  # reject articles older than 72 h

# Maps article event type → compatible radio incident itypes for matching
_NEWS_ITYPE_COMPAT: dict[str, set] = {
    "SHOOTING":        {"SHOOTING", "OFFICER DOWN", "WEAPONS"},
    "STABBING":        {"STABBING", "WEAPONS"},
    "HOMICIDE":        {"SHOOTING", "STABBING", "OFFICER DOWN", "WEAPONS"},
    "WEAPONS":         {"WEAPONS", "SHOOTING", "STABBING"},
    "CRASH/COLLISION": {"CRASH/COLLISION", "FATAL CRASH", "PEDESTRIAN INCIDENT"},
    "FATAL CRASH":     {"CRASH/COLLISION", "FATAL CRASH", "PEDESTRIAN INCIDENT"},
    "STRUCTURE FIRE":  {"STRUCTURE FIRE", "FIRE DISPATCH"},
}

# Source site RSS feeds reachable from VPS — used to resolve real article URLs
_APD_SOURCE_RSS: dict[str, str] = {
    "kxan.com":          "https://www.kxan.com/news/local/feed/",
    "kvue.com":          "https://www.kvue.com/feeds/syndication/rss/news/local/",
    "austincurrent.org": "https://austincurrent.org/feed/",
}

_APD_HEADLINE_KW: list[str] = [
    "homicide", "shooting", "shot", "stabbing", "robbery",
    "assault", "death", "body", "fatal", "critical", "officer",
    "arrest", "suspect", "murder", "aggravated",
]

_ARTICLE_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "in", "on", "at", "of", "to", "is", "was",
    "are", "were", "for", "with", "that", "this", "from", "by", "has", "have",
    "had", "been", "will", "be", "it", "its", "as", "up", "out", "after",
    "police", "apd", "austin", "texas", "tx", "officer", "officers",
    "department", "says", "said", "according", "report", "reported",
    "investigation", "man", "woman", "near", "over", "into", "between",
    "one", "two", "three", "new", "s", "no", "not", "they", "he", "she",
    "his", "her",
})


# ---------------------------------------------------------------------------
# Module-level lock (guards DB dedup operations shared across both sub-polls)
# ---------------------------------------------------------------------------
_APD_NEWS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Pure helper functions — no module-level config imports
# ---------------------------------------------------------------------------

_HOMICIDE_JSON_PATH = "/opt/battlebuddy/homicides_2026.json"
_HOMICIDE_JSON_LOCK = threading.Lock()


def _append_homicide_json(
    *,
    inc_id: int,
    date: str,
    address: str,
    victim: str,
    summary: str,
    url: str,
    lat: float | None,
    lon: float | None,
) -> None:
    """Append a new confirmed homicide to homicides_2026.json (thread-safe)."""
    if not url:
        return
    with _HOMICIDE_JSON_LOCK:
        try:
            with open(_HOMICIDE_JSON_PATH) as f:
                data = json.load(f)
        except Exception:
            data = []
        # Deduplicate by URL
        if any(h.get("url") == url for h in data):
            return
        n = max((h.get("n", 0) for h in data), default=0) + 1
        entry = {"n": n, "date": date, "address": address,
                 "victim": victim, "summary": summary, "url": url}
        if lat is not None:
            entry["lat"] = lat
        if lon is not None:
            entry["lon"] = lon
        data.append(entry)
        tmp = _HOMICIDE_JSON_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=4)
        os.replace(tmp, _HOMICIDE_JSON_PATH)
        logger.info("[apd-news] homicide #%d appended to JSON: %s", n, summary[:80])


def _apd_parse_rss(xml_text: str) -> list[dict]:
    """Parse Google News RSS feed; return list of {title, link, source_url, pub_ts}."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("[apd-news] RSS parse error: %s", exc)
        return []
    channel = root.find("channel")
    if channel is None:
        return []
    seen: set[str] = set()
    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el  = item.find("link")
        if title_el is None or link_el is None:
            continue
        title      = (title_el.text or "").strip()
        link       = (link_el.text or "").strip()
        source_el  = item.find("source")
        source_url = source_el.get("url", "") if source_el is not None else ""
        pub_ts: float | None = None
        pub_el = item.find("pubDate")
        if pub_el is not None and pub_el.text:
            try:
                parsed = _email_utils.parsedate_tz(pub_el.text.strip())
                if parsed:
                    pub_ts = float(_email_utils.mktime_tz(parsed))
            except Exception:
                pass
        if title and link and link not in seen:
            seen.add(link)
            items.append({
                "title":      title,
                "link":       link,
                "source_url": source_url,
                "pub_ts":     pub_ts,
            })
    return items


def _resolve_article_url(
    source_url: str,
    title: str,
    gnews_link: str,
    google_cse_api_key: str,
    google_cse_id: str,
) -> str:
    """
    Try to resolve the real article URL via the source site's RSS feed.
    Falls back to Google Custom Search API then to a browser-accessible
    Google News /articles/ URL.
    """
    from urllib.parse import urlparse as _urlparse

    # Strip "- Publisher Name" suffix that Google News appends to titles
    clean  = title.rsplit(" - ", 1)[0].lower().strip()
    domain = re.sub(r"^www\.", "", _urlparse(source_url).netloc)
    rss_url = _APD_SOURCE_RSS.get(domain)

    # Tier 1: source site RSS feed
    if rss_url and len(clean) > 20:
        try:
            req      = urllib.request.Request(rss_url, headers={"User-Agent": "BattleBuddy/2.0"})
            xml_text = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
            root     = ET.fromstring(xml_text)
            ch       = root.find("channel")
            if ch is not None:
                for item in ch.findall("item"):
                    t_el = item.find("title")
                    l_el = item.find("link")
                    if t_el is None or l_el is None:
                        continue
                    if clean[:40] in (t_el.text or "").lower():
                        real_url = (l_el.text or "").strip()
                        if real_url:
                            logger.debug("[apd-news] resolved via source RSS: %s", real_url)
                            return real_url
        except Exception as exc:
            logger.debug("[apd-news] source RSS lookup failed (%s): %s", domain, exc)

    # Tier 2: Google Custom Search API
    if google_cse_api_key and google_cse_id:
        query_title = title.rsplit(" - ", 1)[0]
        from urllib.parse import urlparse as _up2
        src_domain  = re.sub(r"^www\.", "", _up2(source_url).netloc) if source_url else ""
        site_filter = f"site:{src_domain} " if src_domain else ""
        cse_params  = urllib.parse.urlencode({
            "key": google_cse_api_key,
            "cx":  google_cse_id,
            "q":   f'{site_filter}"{query_title[:80]}"',
            "num": "1",
        })
        cse_url = f"https://www.googleapis.com/customsearch/v1?{cse_params}"
        try:
            cse_req  = urllib.request.Request(cse_url, headers={"User-Agent": "BattleBuddy/2.0"})
            cse_resp = urllib.request.urlopen(cse_req, timeout=10).read().decode("utf-8")
            items    = json.loads(cse_resp).get("items", [])
            if items:
                cse_link = items[0].get("link", "")
                if cse_link.startswith("http"):
                    logger.debug("[apd-news] resolved via Google CSE: %s", cse_link)
                    return cse_link
        except Exception as exc:
            logger.debug("[apd-news] Google CSE lookup failed: %s", exc)

    # Fallback: /rss/articles/ → /articles/ (browser-accessible)
    return re.sub(r"[?&]oc=\d+", "", gnews_link.replace("/rss/articles/", "/articles/")).rstrip("?&")


def _pi_fetch(url: str, pi_fetch_url: str, pi_fetch_token: str, referer: str = "") -> dict:
    """Fetch a URL via the Pi5 fetch agent (residential IP, browser headers).

    Returns a dict with keys ``url``, ``address``, ``summary``, ``text`` on
    success, or ``{}`` if the agent is unavailable.
    """
    if not (pi_fetch_url and pi_fetch_token):
        return {}
    payload = json.dumps({"url": url, "referer": referer}).encode()
    req = urllib.request.Request(
        f"{pi_fetch_url}/fetch",
        data=payload,
        headers={
            "Authorization": f"Bearer {pi_fetch_token}",
            "Content-Type":  "application/json",
        },
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read().decode("utf-8"))
        if data.get("status") != 200:
            return {}
        text = data.get("text", "")
        addr_m = re.search(
            r"(\d{3,5}(?:\s+block\s+of)?\s+[A-Z][a-zA-Z0-9 ,.]+(?:Street|St|Avenue|Ave|"
            r"Drive|Dr|Road|Rd|Lane|Ln|Boulevard|Blvd|Way|Court|Ct|Circle|Cir|"
            r"Parkway|Pkwy|Highway|Hwy|Loop|Trail|Trl|Pass|Place|Pl)"
            r"(?:\s+(?:NW|NE|SW|SE|N|S|E|W))?)",
            text,
        )
        address = addr_m.group(1).strip() if addr_m else None
        return {
            "url":     data.get("final_url", url),
            "address": address,
            "summary": text[:400].strip(),
            "text":    text,
        }
    except Exception as exc:
        logger.debug("[pi-fetch] %s failed: %s", url[:60], exc)
        return {}


def _apd_fetch_article(
    url: str,
    pi_fetch_url: str,
    pi_fetch_token: str,
) -> dict:
    """Fetch a news article URL, extract address and description.

    Tries the Pi5 residential-IP fetch agent first; falls back to direct fetch.
    Returns a dict with optional keys ``url``, ``address``, ``summary``.
    """
    # Try residential Pi fetch first (bypasses datacenter IP blocks)
    pi_result = _pi_fetch(url, pi_fetch_url, pi_fetch_token)
    if pi_result:
        return pi_result

    # Fallback: direct fetch from VPS
    try:
        req  = urllib.request.Request(
            url,
            headers={"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )},
        )
        resp      = urllib.request.urlopen(req, timeout=15)
        final_url = resp.url
        html      = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        logger.warning("[apd-news] article fetch failed %s: %s", url, exc)
        return {}

    # Strip tags for text extraction
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    addr_m = re.search(
        r"(\d{3,5}(?:\s+block\s+of)?\s+[A-Z][a-zA-Z0-9 ,.]+(?:Street|St|Avenue|Ave|Drive|Dr|"
        r"Road|Rd|Lane|Ln|Boulevard|Blvd|Way|Court|Ct|Circle|Cir|Parkway|Pkwy|Highway|Hwy|"
        r"Loop|Trail|Trl|Pass|Crossing|Crossing|Place|Pl)(?:\s+(?:NW|NE|SW|SE|N|S|E|W))?)",
        text,
    )
    address = addr_m.group(1).strip() if addr_m else None

    body_m  = re.search(r"Case Number[:\s]+(.*?)(?:Tips|Contact|Crime Stoppers)", text, re.DOTALL)
    summary = body_m.group(0)[:400].strip() if body_m else text[500:900].strip()

    return {"url": final_url, "address": address, "summary": summary}


def _article_itype_from_title(title: str) -> str:
    """Determine the incident type (itype) from a news article headline."""
    t = title.lower()
    if "homicide" in t or "murder" in t:
        return "HOMICIDE"
    if "fatal" in t and any(w in t for w in ("crash", "accident", "hit", "pedestrian", "collision")):
        return "FATAL CRASH"
    if "shooting" in t or " shot" in t:
        return "SHOOTING"
    if "stab" in t:
        return "STABBING"
    if "robbery" in t or "aggravated assault" in t:
        return "WEAPONS"
    if "crash" in t or "collision" in t or "pedestrian" in t:
        return "CRASH/COLLISION"
    return "SHOOTING"


def _match_article_to_incident(
    title: str,
    article_itype: str,
    article_ts: float,
    db_path: str,
) -> tuple[int | None, float]:
    """Try to match a news article to a recent radio-detected incident.

    Searches incidents from the 48-hour window preceding the article.
    Returns ``(incident_id, score)`` or ``(None, 0)``.
    """
    compat       = _NEWS_ITYPE_COMPAT.get(article_itype, {article_itype})
    placeholders = ",".join("?" * len(compat))
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        f"SELECT id, itype, description, location, ts_start FROM incidents "
        f"WHERE ts_start >= ? AND ts_start <= ? "
        f"AND itype IN ({placeholders}) "
        f"AND description NOT LIKE '%APD Press Release%' "
        f"ORDER BY ts_start DESC LIMIT 20",
        [article_ts - 48 * 3600, article_ts + 3600, *compat],
    ).fetchall()
    conn.close()
    if not rows:
        return None, 0

    title_lower = title.lower()
    highways    = set(re.findall(r"\b(?:i-?|ih-?|hwy\s*|fm\s*|us-?|sh-?|tx-?)\d+\b", title_lower))
    streets     = set(re.findall(
        r"[a-z]+ (?:street|st|avenue|ave|drive|dr|road|rd|lane|ln|boulevard|blvd"
        r"|way|parkway|pkwy|highway|loop|trail|pass)\b",
        title_lower,
    ))
    words = {
        w for w in re.findall(r"[a-z0-9]+", title_lower)
        if len(w) > 3 and w not in _ARTICLE_STOP_WORDS
    }
    location_tokens = highways | streets
    best_id, best_score = rows[0][0], 0.5

    for inc_id, itype, desc, location, _ts_start in rows:
        score    = 0.5
        if itype == article_itype:
            score += 0.5
        combined = ((desc or "") + " " + (location or "")).lower()
        for token in location_tokens:
            if token in combined:
                score += 2.0
        for w in words:
            if re.search(r"\b" + re.escape(w) + r"\b", combined):
                score += 0.3
        if score > best_score:
            best_score = score
            best_id    = inc_id

    # Single candidate: accept it (itype already filtered)
    if len(rows) == 1:
        return rows[0][0], max(best_score, 1.0)
    return (best_id, best_score) if best_score >= 1.0 else (None, 0)


def _store_article_link(
    incident_id: int | None,
    ts: float,
    headline: str,
    url: str,
    source: str,
    snippet: str,
    score: float,
    db_path: str,
) -> None:
    """Insert a row into incident_articles and update incidents.article_url."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO incident_articles "
        "(incident_id, ts, headline, url, source, snippet, match_score) "
        "VALUES (?,?,?,?,?,?,?)",
        (incident_id, ts, headline, url, source, (snippet or "")[:300], score),
    )
    if incident_id:
        conn.execute(
            "UPDATE incidents SET article_url=? WHERE id=? AND article_url IS NULL",
            (url, incident_id),
        )
    conn.commit()
    conn.close()


def _post_to_talk(
    message: str,
    room_tokens: list[str],
    talk_base: str,
    talk_user: str,
    talk_pass: str,
    log_tag: str = "apd-news",
) -> None:
    """Post *message* to each Talk room token in *room_tokens*."""
    payload = json.dumps({"message": message}).encode()
    creds   = base64.b64encode(f"{talk_user}:{talk_pass}".encode()).decode()
    headers = {
        "Authorization":  f"Basic {creds}",
        "OCS-APIRequest": "true",
        "Content-Type":   "application/json",
    }
    if not talk_base:
        logger.warning("[%s] Talk post skipped: TALK_BASE missing", log_tag)
        return
    for room in room_tokens:
        if not room:
            logger.warning("[%s] Talk post skipped: room token missing", log_tag)
            continue
        req = urllib.request.Request(
            f"{talk_base}/chat/{room}",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as exc:
            logger.warning("[%s] Talk post failed: %s", log_tag, exc)


# ---------------------------------------------------------------------------
# BasePoller subclass
# ---------------------------------------------------------------------------

class APDNewsPoller(BasePoller):
    """Poll Google News RSS for APD press releases every 5 minutes.

    State is held entirely in instance variables — no module-level mutable
    globals — so multiple instances can coexist safely (useful in tests).

    Instance variables
    ------------------
    _lock : threading.Lock
        Guards the ``apd_seen`` DB dedup set shared between the APD sub-poll
        and the traffic sub-poll within a single run() call.
    """

    NAME: str     = "apd_news"
    INTERVAL: float = APD_NEWS_INTERVAL

    def __init__(self) -> None:
        super().__init__(interval=int(self.INTERVAL))
        self._lock = threading.Lock()

    def diagnostics(self) -> str:
        """Return a human-readable status string for health checks and tests."""
        return f"APDNewsPoller(interval={self.interval}s)"

    # ------------------------------------------------------------------
    # BasePoller interface
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Perform one full poll cycle: APD press releases + traffic fatalities."""
        # Lazy imports — avoids circular dependency at module load time
        from modules.config import (  # noqa: PLC0415
            DB_PATH,
            GOOGLE_CSE_API_KEY,
            GOOGLE_CSE_ID,
            PI_FETCH_TOKEN,
            PI_FETCH_URL,
            TALK_BASE,
            TALK_PASS,
            TALK_ROOMS,
            TALK_USER,
        )
        from modules.geocoding import _geocode_address  # noqa: PLC0415
        from modules.incident_engine import _atak_post_marker  # noqa: PLC0415

        # ---- APD press release sub-poll ----------------------------------
        self._poll_apd_press_releases(
            db_path=DB_PATH,
            talk_base=TALK_BASE,
            talk_user=TALK_USER,
            talk_pass=TALK_PASS,
            talk_rooms=TALK_ROOMS,
            google_cse_api_key=GOOGLE_CSE_API_KEY,
            google_cse_id=GOOGLE_CSE_ID,
            pi_fetch_url=PI_FETCH_URL,
            pi_fetch_token=PI_FETCH_TOKEN,
            geocode_fn=_geocode_address,
            atak_post_fn=_atak_post_marker,
        )

        # ---- Traffic fatality news sub-poll ------------------------------
        self._poll_traffic_news(
            db_path=DB_PATH,
            google_cse_api_key=GOOGLE_CSE_API_KEY,
            google_cse_id=GOOGLE_CSE_ID,
            pi_fetch_url=PI_FETCH_URL,
            pi_fetch_token=PI_FETCH_TOKEN,
            geocode_fn=_geocode_address,
        )

    # ------------------------------------------------------------------
    # Private sub-pollers
    # ------------------------------------------------------------------

    def _poll_apd_press_releases(
        self,
        *,
        db_path: str,
        talk_base: str,
        talk_user: str,
        talk_pass: str,
        talk_rooms: dict,
        google_cse_api_key: str,
        google_cse_id: str,
        pi_fetch_url: str,
        pi_fetch_token: str,
        geocode_fn,
        atak_post_fn,
    ) -> None:
        """Fetch and process APD press release articles from Google News RSS."""
        # Lazy import — avoids circular dependency
        from modules.pollers import send_dm_alert  # noqa: PLC0415

        try:
            req = urllib.request.Request(
                APD_NEWS_URL,
                headers={
                    "User-Agent": "BattleBuddy/2.0",
                    "Accept":     "application/rss+xml, application/xml, text/xml",
                },
            )
            xml_text = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("[apd-news] fetch error: %s", exc)
            return

        articles = _apd_parse_rss(xml_text)

        # Dedup against DB — persistent across restarts
        with self._lock:
            conn_d   = sqlite3.connect(db_path)
            existing = {row[0] for row in conn_d.execute("SELECT url FROM apd_seen")}
            new_articles = [a for a in articles if a["link"] not in existing]
            if new_articles:
                conn_d.executemany(
                    "INSERT OR IGNORE INTO apd_seen (url, ts) VALUES (?,?)",
                    [(a["link"], time.time()) for a in new_articles],
                )
                conn_d.commit()
            conn_d.close()

        for article in new_articles:
            title_lower = article["title"].lower()
            if not any(kw in title_lower for kw in _APD_HEADLINE_KW):
                continue

            logger.info("[apd-news] NEW: %s", article["title"])
            url    = _resolve_article_url(
                article.get("source_url", ""), article["title"], article["link"],
                google_cse_api_key, google_cse_id,
            )
            detail  = _apd_fetch_article(url, pi_fetch_url, pi_fetch_token)
            address = detail.get("address")
            summary = detail.get("summary", article["title"])

            lat: float | None = None
            lon: float | None = None
            if address:
                coords = geocode_fn(address)
                if coords:
                    lat, lon = coords

            itype  = _article_itype_from_title(article["title"])
            pub_ts = article.get("pub_ts")

            if not pub_ts:
                logger.info("[news] SKIP apd_pr (no pub_ts): %s", article["title"])
                continue
            age = time.time() - pub_ts
            if age > _ARTICLE_MAX_AGE_SECS:
                logger.info("[news] SKIP apd_pr (stale %.1fh): %s", age / 3600, article["title"])
                continue

            ts   = pub_ts
            desc = f"[APD Press Release] {article['title']}. {summary[:200]}"

            matched_id, match_score = _match_article_to_incident(
                article["title"], itype, ts, db_path
            )

            if matched_id:
                # Article matches a radio incident — link and notify
                _store_article_link(
                    matched_id, ts, article["title"], url,
                    "apd_pr", summary[:300], match_score, db_path,
                )
                if itype == "HOMICIDE":
                    _append_homicide_json(
                        inc_id=matched_id,
                        date=datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d'),
                        address=address or "",
                        victim="",
                        summary=article["title"].rsplit(" - ", 1)[0],
                        url=url,
                        lat=lat,
                        lon=lon,
                    )
                logger.info(
                    "[apd-news] LINKED: '%s' → incident %s (score=%.1f)",
                    article["title"], matched_id, match_score,
                )
                loc_str = f" @ {address}" if address else ""
                msg = (
                    f"\U0001f4f0 [PRESS COVERAGE] Radio incident #{matched_id} now in the news\n"
                    f"\U0001f4f0 {article['title']}\n"
                    f"\U0001f517 {url}\n"
                    f"\U0001f4cd{loc_str}"
                )
                _post_to_talk(
                    msg,
                    [talk_rooms["apd"], talk_rooms["incidents"]],
                    talk_base, talk_user, talk_pass,
                    log_tag="apd-news",
                )
            else:
                # No radio match — create a new incident from the press release
                conn = sqlite3.connect(db_path)
                cur  = conn.execute(
                    "INSERT INTO incidents "
                    "(ts_start, ts_updated, itype, description, agencies, "
                    "tgids, location, lat, lon, article_url, status) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,'active')",
                    (ts, ts, itype, desc, '["APD"]', "[]", address, lat, lon, url),
                )
                inc_id = cur.lastrowid
                conn.commit()
                conn.close()

                _store_article_link(inc_id, ts, article["title"], url, "apd_pr",
                                    summary[:300], 0.0, db_path)

                if itype == "HOMICIDE":
                    _append_homicide_json(
                        inc_id=inc_id,
                        date=datetime.datetime.fromtimestamp(ts).strftime('%Y-%m-%d'),
                        address=address or "",
                        victim="",
                        summary=article["title"].rsplit(" - ", 1)[0],
                        url=url,
                        lat=lat,
                        lon=lon,
                    )

                loc_str = f" @ {address}" if address else ""
                msg = (
                    f"\U0001f6a8 [APD PRESS RELEASE] {article['title']}\n"
                    f"\U0001f517 {url}\n"
                    f"\U0001f4cd{loc_str}\n"
                    f"{summary[:300]}"
                )
                _post_to_talk(
                    msg,
                    [talk_rooms["apd"], talk_rooms["incidents"]],
                    talk_base, talk_user, talk_pass,
                    log_tag="apd-news",
                )
                threading.Thread(
                    target=send_dm_alert,
                    args=(itype, desc, address, "APD", "APD"),
                    daemon=True,
                ).start()

                if lat is not None and lon is not None:
                    threading.Thread(
                        target=atak_post_fn,
                        args=(inc_id, lat, lon, itype, address, desc),
                        daemon=True,
                    ).start()

    def _poll_traffic_news(
        self,
        *,
        db_path: str,
        google_cse_api_key: str,
        google_cse_id: str,
        pi_fetch_url: str,
        pi_fetch_token: str,
        geocode_fn,
    ) -> None:
        """Fetch Austin traffic fatality news and link to existing radio incidents."""
        try:
            treq = urllib.request.Request(
                TRAFFIC_NEWS_URL,
                headers={
                    "User-Agent": "BattleBuddy/2.0",
                    "Accept":     "application/rss+xml, application/xml, text/xml",
                },
            )
            txml_text = urllib.request.urlopen(treq, timeout=15).read().decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("[traffic-news] fetch error: %s", exc)
            return

        tarticles = _apd_parse_rss(txml_text)

        with self._lock:
            conn_t     = sqlite3.connect(db_path)
            t_existing = {row[0] for row in conn_t.execute("SELECT url FROM apd_seen")}
            t_new      = [a for a in tarticles if a["link"] not in t_existing]
            if t_new:
                conn_t.executemany(
                    "INSERT OR IGNORE INTO apd_seen (url, ts) VALUES (?,?)",
                    [(a["link"], time.time()) for a in t_new],
                )
                conn_t.commit()
            conn_t.close()

        for ta in t_new:
            ttitle = ta["title"].lower()
            if not any(kw in ttitle for kw in ("fatal", "killed", "pedestrian", "hit-and-run", "deadly")):
                continue

            turl      = _resolve_article_url(
                ta.get("source_url", ""), ta["title"], ta["link"],
                google_cse_api_key, google_cse_id,
            )
            art_itype = (
                "FATAL CRASH"
                if any(w in ttitle for w in ("fatal", "killed", "dead", "deadly"))
                else "CRASH/COLLISION"
            )
            tts = ta.get("pub_ts")
            if not tts:
                logger.info("[news] SKIP traffic-news (no pub_ts): %s", ta["title"])
                continue
            age = time.time() - tts
            if age > _ARTICLE_MAX_AGE_SECS:
                logger.info("[news] SKIP traffic-news (stale %.1fh): %s", age / 3600, ta["title"])
                continue

            t_inc_id, t_score = _match_article_to_incident(ta["title"], art_itype, tts, db_path)
            if not t_inc_id:
                continue

            t_detail  = _apd_fetch_article(turl, pi_fetch_url, pi_fetch_token)
            t_snippet = t_detail.get("summary", "")
            t_address = t_detail.get("address")

            if t_address:
                t_coords = geocode_fn(t_address)
                if t_coords:
                    conn_ta = sqlite3.connect(db_path)
                    conn_ta.execute(
                        "UPDATE incidents SET location=?, lat=?, lon=? "
                        "WHERE id=? AND (location IS NULL OR location='')",
                        (t_address, t_coords[0], t_coords[1], t_inc_id),
                    )
                    conn_ta.commit()
                    conn_ta.close()

            _store_article_link(t_inc_id, tts, ta["title"], turl,
                                "traffic-news", t_snippet, t_score, db_path)
            logger.info(
                "[traffic-news] LINKED: '%s' → incident %s (score=%.1f)%s",
                ta["title"], t_inc_id, t_score,
                f" addr={t_address}" if t_address else "",
            )
