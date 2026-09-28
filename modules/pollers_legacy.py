import base64
import json
import os  # noqa: F401
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from modules.alerts import send_dm_alert  # noqa: F401
from modules.config import (
    DB_PATH,
    DECK_BASE,  # noqa: F401
    DECK_BOARD_ID,  # noqa: F401
    DECK_LABELS,  # noqa: F401
    DECK_STACK_NEW,  # noqa: F401
    GOOGLE_CSE_API_KEY,
    GOOGLE_CSE_ID,
    PI_FETCH_ENABLED,
    PI_FETCH_TOKEN,
    PI_FETCH_URL,
    TALK_BASE,
    TALK_ENABLED,  # noqa: F401
    TALK_PASS,
    TALK_ROOMS,
    TALK_USER,
    _room_for_call,  # noqa: F401
)
from modules.geocoding import _geocode_address  # noqa: F401
from modules.incident_engine import (
    _active_incidents,  # noqa: F401
    _atak_clear_marker,  # noqa: F401
    _atak_post_marker,  # noqa: F401
    _incident_lock,  # noqa: F401
)
from modules.pi_watchdog import (  # noqa: F401
    PI1_OP25_CMD_URL,
    PI1_SSH_HOST,
    PI1_SSH_KEY,
    PI1_SSH_USER,
    PI_ALERT_REPEAT_MINS,
    PI_ALERT_USERS,
    PI_AUTORESTART_MINS,
    PI_CALL_SILENCE_MINS,
    PI_WATCHDOG_INTERVAL,
    _pi_command_queue,
    _pi_watchdog_alert,
    pi_watchdog_thread,
)
from modules.sitrep import build_sitrep  # noqa: F401
from modules.talkgroups import (
    IGNORE_TGIDS,
    TGID_META,
    detect_air_asset,  # noqa: F401
    detect_dps_assets,  # noqa: F401
    is_capitol_area,  # noqa: F401
    mentions_dps,  # noqa: F401
)

_CDT = ZoneInfo("America/Chicago")


APD_NEWS_URL      = (
    "https://news.google.com/rss/search"
    "?q=APD+Austin+%22press+release%22+(homicide+OR+shooting+OR+stabbing)"
    "&hl=en-US&gl=US&ceid=US:en"
)
APD_NEWS_INTERVAL = 300   # poll every 5 minutes
_ARTICLE_MAX_AGE_SECS = 72 * 3600  # reject news articles older than 72h from radio-call matching

# Broader Google News search for Austin traffic fatalities — used to link
# crash articles to radio-detected incidents. No incident creation on no-match.
TRAFFIC_NEWS_URL = (
    "https://news.google.com/rss/search"
    "?q=Austin+Texas+(fatal+crash+OR+pedestrian+killed+OR+hit-and-run)"
    "&hl=en-US&gl=US&ceid=US:en"
)

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
_APD_SOURCE_RSS = {
    "kxan.com":          "https://www.kxan.com/news/local/feed/",
    "kvue.com":          "https://www.kvue.com/feeds/syndication/rss/news/local/",
    "austincurrent.org": "https://austincurrent.org/feed/",
}

# _APD_NEWS_SEEN replaced by apd_seen DB table (persistent across restarts)
_APD_NEWS_LOCK    = threading.Lock()

_APD_HEADLINE_KW  = [
    "homicide", "shooting", "shot", "stabbing", "robbery",
    "assault", "death", "body", "fatal", "critical", "officer",
    "arrest", "suspect", "murder", "aggravated",
]

# ---------------------------------------------------------------------------
# Retry helper for poller HTTP fetches
# ---------------------------------------------------------------------------

def _fetch_url_with_retry(url: str, headers: dict | None = None,
                          timeout: int = 15, max_retries: int = 3,
                          label: str = "poller") -> bytes:
    """Fetch a URL with exponential backoff retry.

    Returns the response body as bytes, or raises the last exception after
    all retries are exhausted.
    """
    last_err: Exception | None = None
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, TimeoutError) as e:
            last_err = e
            wait = 2 ** attempt
            if attempt < max_retries - 1:
                print(f"[{label}] fetch attempt {attempt + 1}/{max_retries} failed ({e}) — retrying in {wait}s", flush=True)
                time.sleep(wait)
    raise last_err  # type: ignore[misc]



def _apd_parse_rss(xml_text: str) -> list[dict]:
    """Parse Google News RSS feed; return list of {title, link}."""
    import xml.etree.ElementTree as ET
    items = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        print(f"[apd-news] RSS parse error: {e}", flush=True)
        return []
    channel = root.find("channel")
    if channel is None:
        return []
    seen = set()
    for item in channel.findall("item"):
        title_el = item.find("title")
        link_el  = item.find("link")
        if title_el is None or link_el is None:
            continue
        title      = (title_el.text or "").strip()
        link       = (link_el.text or "").strip()
        source_el  = item.find("source")
        source_url = source_el.get("url", "") if source_el is not None else ""
        pub_ts     = None
        pub_el     = item.find("pubDate")
        if pub_el is not None and pub_el.text:
            try:
                from email import utils as _eu
                _parsed = _eu.parsedate_tz(pub_el.text.strip())
                if _parsed:
                    pub_ts = float(_eu.mktime_tz(_parsed))
            except Exception:
                pass
        if title and link and link not in seen:
            seen.add(link)
            items.append({"title": title, "link": link,
                          "source_url": source_url, "pub_ts": pub_ts})
    return items

def _resolve_article_url(source_url: str, title: str, gnews_link: str) -> str:
    """
    Try to resolve the real article URL via the source site's RSS feed.
    Falls back to a browser-accessible Google News /articles/ URL.
    """
    import xml.etree.ElementTree as _ET
    from urllib.parse import urlparse as _urlparse
    # Strip "- Publisher Name" suffix that Google News appends to titles
    clean  = title.rsplit(" - ", 1)[0].lower().strip()
    domain = re.sub(r"^www\.", "", _urlparse(source_url).netloc)
    rss_url = _APD_SOURCE_RSS.get(domain)
    if rss_url and len(clean) > 20:
        try:
            req = urllib.request.Request(rss_url, headers={"User-Agent": "BattleBuddy/2.0"})
            xml_text = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", errors="replace")
            root = _ET.fromstring(xml_text)
            ch = root.find("channel")
            if ch is not None:
                for item in ch.findall("item"):
                    t_el = item.find("title")
                    l_el = item.find("link")
                    if t_el is None or l_el is None:
                        continue
                    if clean[:40] in (t_el.text or "").lower():
                        real_url = (l_el.text or "").strip()
                        if real_url:
                            print(f"[apd-news] resolved via source RSS: {real_url}", flush=True)
                            return real_url
        except Exception as e:
            print(f"[apd-news] source RSS lookup failed ({domain}): {e}", flush=True)
    # Tier 2: Google Custom Search API — works for any source
    if GOOGLE_CSE_API_KEY and GOOGLE_CSE_ID:
        query_title = title.rsplit(" - ", 1)[0]  # strip publisher suffix
        from urllib.parse import urlparse as _up2
        src_domain = re.sub(r"^www\.", "", _up2(source_url).netloc) if source_url else ""
        site_filter = f"site:{src_domain} " if src_domain else ""
        import json as _json
        cse_params = urllib.parse.urlencode({
            "key": GOOGLE_CSE_API_KEY,
            "cx":  GOOGLE_CSE_ID,
            "q":   f'{site_filter}"{query_title[:80]}"',
            "num": "1",
        })
        cse_url = f"https://www.googleapis.com/customsearch/v1?{cse_params}"
        try:
            cse_req  = urllib.request.Request(cse_url, headers={"User-Agent": "BattleBuddy/2.0"})
            cse_resp = urllib.request.urlopen(cse_req, timeout=10).read().decode("utf-8")
            items    = _json.loads(cse_resp).get("items", [])
            if items:
                cse_link = items[0].get("link", "")
                if cse_link.startswith("http"):
                    print(f"[apd-news] resolved via Google CSE: {cse_link}", flush=True)
                    return cse_link
        except Exception as e:
            print(f"[apd-news] Google CSE lookup failed: {e}", flush=True)
    # Fallback: /rss/articles/ is RSS-only; /articles/ is browser-accessible
    return re.sub(r"[?&]oc=\d+", "", gnews_link.replace("/rss/articles/", "/articles/")).rstrip("?&")


def _apd_fetch_article(url: str) -> dict:
    """Fetch a news article URL (follows redirects), extract address and description.
    Tries the Pi5 residential-IP fetch agent first; falls back to direct fetch.
    """
    import re
    # Try residential Pi fetch first (bypasses datacenter IP blocks)
    pi_result = _pi_fetch(url)
    if pi_result:
        return pi_result
    # Fallback: direct fetch from VPS
    try:
        req  = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                                   "Chrome/120.0.0.0 Safari/537.36"}
        )
        resp      = urllib.request.urlopen(req, timeout=15)
        final_url = resp.url
        html      = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[apd-news] article fetch failed {url}: {e}", flush=True)
        return {}

    # Strip tags for text extraction
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)

    # Look for address patterns: "1234 Some Street" or "1234 block of Some Street"
    addr_m = re.search(
        r"(\d{3,5}(?:\s+block\s+of)?\s+[A-Z][a-zA-Z0-9 ,.]+(?:Street|St|Avenue|Ave|Drive|Dr|"
        r"Road|Rd|Lane|Ln|Boulevard|Blvd|Way|Court|Ct|Circle|Cir|Parkway|Pkwy|Highway|Hwy|"
        r"Loop|Trail|Trl|Pass|Crossing|Crossing|Place|Pl)(?:\s+(?:NW|NE|SW|SE|N|S|E|W))?)",
        text
    )
    address = addr_m.group(1).strip() if addr_m else None

    # Pull first 400 chars of body text after stripping nav/header noise
    body_m = re.search(r"Case Number[:\s]+(.*?)(?:Tips|Contact|Crime Stoppers)", text, re.DOTALL)
    summary = body_m.group(0)[:400].strip() if body_m else text[500:900].strip()

    return {"url": final_url, "address": address, "summary": summary}


def _pi_fetch(url: str, referer: str = "") -> dict:
    """Fetch a URL via the Pi5 fetch agent (residential IP, browser headers).
    Returns the same dict shape as _apd_fetch_article on success.
    Returns {} if Pi is unavailable — caller falls back to direct fetch.
    """
    if not PI_FETCH_ENABLED:
        return {}
    import json as _json
    payload = _json.dumps({"url": url, "referer": referer}).encode()
    req = urllib.request.Request(
        f"{PI_FETCH_URL}/fetch",
        data=payload,
        headers={
            "Authorization": f"Bearer {PI_FETCH_TOKEN}",
            "Content-Type":  "application/json",
        },
        method="POST"
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = _json.loads(resp.read().decode("utf-8"))
        if data.get("status") != 200:
            return {}
        text = data.get("text", "")
        html = data.get("html", "")  # noqa: F841
        # Extract address from text (reuse same regex as _apd_fetch_article)
        addr_m = re.search(
            r"(\d{3,5}(?:\s+block\s+of)?\s+[A-Z][a-zA-Z0-9 ,.]+(?:Street|St|Avenue|Ave|"
            r"Drive|Dr|Road|Rd|Lane|Ln|Boulevard|Blvd|Way|Court|Ct|Circle|Cir|"
            r"Parkway|Pkwy|Highway|Hwy|Loop|Trail|Trl|Pass|Place|Pl)(?:\s+(?:NW|NE|SW|SE|N|S|E|W))?)",
            text
        )
        address = addr_m.group(1).strip() if addr_m else None
        summary = text[:400].strip()
        return {
            "url":     data.get("final_url", url),
            "address": address,
            "summary": summary,
            "text":    text,
        }
    except Exception as e:
        print(f"[pi-fetch] {url[:60]} failed: {e}", flush=True)
        return {}


_ARTICLE_STOP_WORDS = {
    "a","an","the","and","or","in","on","at","of","to","is","was","are","were",
    "for","with","that","this","from","by","has","have","had","been","will","be",
    "it","its","as","up","out","after","police","apd","austin","texas","tx",
    "officer","officers","department","says","said","according","report",
    "reported","investigation","man","woman","near","over","into","between",
    "one","two","three","new","s","no","not","they","he","she","his","her",
}


def _match_article_to_incident(title: str, article_itype: str, article_ts: float) -> tuple:
    """Try to match a news article to a recent radio-detected incident.
    Returns (incident_id, score) or (None, 0).
    Searches incidents from the 48h window preceding the article.
    """
    compat = _NEWS_ITYPE_COMPAT.get(article_itype, {article_itype})
    placeholders = ",".join("?" * len(compat))
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        f"SELECT id, itype, description, location, ts_start FROM incidents "
        f"WHERE ts_start >= ? AND ts_start <= ? "
        f"AND itype IN ({placeholders}) "
        f"AND description NOT LIKE '%APD Press Release%' "
        f"ORDER BY ts_start DESC LIMIT 20",
        [article_ts - 48*3600, article_ts + 3600] + list(compat)
    ).fetchall()
    conn.close()
    if not rows:
        return None, 0
    # Extract location tokens from article title for scoring
    title_lower = title.lower()
    highways = set(re.findall(
        r"\b(?:i-?|ih-?|hwy\s*|fm\s*|us-?|sh-?|tx-?)\d+\b", title_lower))
    streets  = set(re.findall(
        r"[a-z]+ (?:street|st|avenue|ave|drive|dr|road|rd|lane|ln|boulevard|blvd"
        r"|way|parkway|pkwy|highway|loop|trail|pass)\b", title_lower))
    words    = {w for w in re.findall(r"[a-z0-9]+", title_lower)
                if len(w) > 3 and w not in _ARTICLE_STOP_WORDS}
    location_tokens = highways | streets
    best_id, best_score = rows[0][0], 0.5
    for inc_id, itype, desc, location, ts_start in rows:
        score = 0.5
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
            best_id = inc_id
    # Single candidate: accept it (itype already filtered)
    if len(rows) == 1:
        return rows[0][0], max(best_score, 1.0)
    return (best_id, best_score) if best_score >= 1.0 else (None, 0)


def _store_article_link(incident_id: int | None, ts: float, headline: str,
                        url: str, source: str, snippet: str, score: float):
    """Insert a row into incident_articles and update incidents.article_url."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO incident_articles "
        "(incident_id, ts, headline, url, source, snippet, match_score) "
        "VALUES (?,?,?,?,?,?,?)",
        (incident_id, ts, headline, url, source, snippet[:300] if snippet else "", score)
    )
    if incident_id:
        conn.execute(
            "UPDATE incidents SET article_url=? WHERE id=? AND article_url IS NULL",
            (url, incident_id)
        )
    conn.commit()
    conn.close()


_ADSB_LOL_URL    = "https://api.adsb.lol/v2/lat/30.2672/lon/-97.7431/dist/52"
ADSB_INTERVAL    = 30    # poll every 30 seconds
ADSB_MAX_ALT_FT  = 5000  # only track aircraft below 5,000 ft AGL
ADSB_TRAIL_SECS  = 1800  # 30 minutes of trail history
ADSB_REFRACTORY  = 1800  # 30 min before re-alerting same aircraft

# Known Austin-area LEO / EMS air assets (icao24 hex → (label, is_leo))
KNOWN_AIR_ASSETS = {
    "a820f8": ("APD Air1 (N6227)",         True),   # Eurocopter AS350B3 — LEO
    "a064fb": ("APD Air Support (N1240W)", True),   # Eurocopter EC120B — LEO
    "a33eb6": ("STAR Flight 2 (N308TC)",   False),  # Leonardo AW169 — EMS
    "a3426d": ("STAR Flight 3 (N309TC)",   False),  # Leonardo AW169 — EMS
}

_adsb_seen       : dict[str, float] = {}   # icao24 → last alert timestamp
_adsb_orbit_seen : dict[str, float] = {}   # icao24 → last orbit-alert timestamp

# ─────────────────────────────────────────────────────────────────────────────
# Reddit citizen intel poller -- REMOVED (dead code, do not revive here).
# The legacy loose substring matcher (`kw in text`) and _reddit_tip_recheck
# had no callers. The live implementation is RedditIntelPoller in
# modules/pollers/impl/reddit_intel.py (strict whole-word/phrase matching
# with a confidence column; high-confidence only alerts). Re-enabling a
# legacy reddit thread would resurrect the loose behaviour -- implement any
# future reddit work in the impl module instead.
# ─────────────────────────────────────────────────────────────────────────────

# (Dead legacy reddit helpers -- _reddit_match_incident, _nominatim_geocode,
# _extract_tip_location, _reddit_tip_recheck and the shared reddit regex
# tables -- were removed with the section above. The live versions live in
# modules/pollers/impl/reddit_intel.py.)



_adsb_lock       = threading.Lock()


def _adsb_check_orbit(icao24: str, now: float) -> bool:
    """Return True if icao24 has been orbiting (circling/hovering) in the last 5 minutes."""
    import math
    try:
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            "SELECT lat, lon, heading FROM aircraft_positions "
            "WHERE icao24=? AND ts >= ? ORDER BY ts",
            (icao24, now - 300)
        ).fetchall()
        conn.close()
    except Exception:
        return False

    if len(rows) < 6:
        return False

    lats = [r[0] for r in rows]
    lons = [r[1] for r in rows]
    headings = [r[2] for r in rows if r[2] is not None]

    clat = sum(lats) / len(lats)
    clon = sum(lons) / len(lons)

    def _km(la, lo, lb, lb2):
        R = 6371.0
        dlat = math.radians(lb - la)
        dlon = math.radians(lb2 - lo)
        a = math.sin(dlat/2)**2 + math.cos(math.radians(la)) * math.cos(math.radians(lb)) * math.sin(dlon/2)**2
        return R * 2 * math.asin(math.sqrt(a))

    max_dist = max(_km(clat, clon, la, lo) for la, lo in zip(lats, lons))
    if max_dist > 1.2:
        return False

    if len(headings) < 5:
        return False
    min_h = min(headings)
    max_h = max(headings)
    span = max_h - min_h
    return span >= 180



TRAFFIC_OPEN_DATA_URL = (
    "https://data.austintexas.gov/resource/dx9v-zd7x.json"
    "?$where=traffic_report_status='ACTIVE'&$limit=100"
)
TRAFFIC_POLL_INTERVAL = 60  # seconds

_TRAFFIC_ITYPE_MAP = {
    "CRASH":       "CRASH/COLLISION",
    "COLLISION":   "CRASH/COLLISION",
    "VEHICLE":     "CRASH/COLLISION",
    "MOTORCYCLE":  "CRASH/COLLISION",
    "BICYCLE":     "CRASH/COLLISION",
    "PEDESTRIAN":  "PEDESTRIAN INCIDENT",
    "STALLED":     "STALLED VEHICLE",
    "ABANDONED":   "ABANDONED VEHICLE",
    "ROAD":        "ROAD HAZARD",
    "DEBRIS":      "ROAD HAZARD",
    "FLOODING":    "FLOODING",
    "FLOODED":     "FLOODING",
    "SIGNAL":      "TRAFFIC SIGNAL ISSUE",
    "FIRE":        "VEHICLE FIRE",
    "HAZMAT":      "HAZMAT",
    "SPILL":       "HAZMAT",
    "BRIDGE":      "ROAD HAZARD",
    "ANIMAL":      "ROAD HAZARD",
}

# Types worth posting to Talk (suppress stalls/abandoned to reduce noise)
_TRAFFIC_TALK_ITYPES = {
    "CRASH/COLLISION", "PEDESTRIAN INCIDENT", "FLOODING",
    "VEHICLE FIRE", "HAZMAT", "ROAD HAZARD",
}

_traffic_active_ids: dict[str, dict] = {}
_traffic_lock = threading.Lock()


def _traffic_issue_to_itype(issue: str) -> str:
    """Map traffic issue_reported string to a BB itype."""
    prefix = issue.split()[0].upper().rstrip("-")
    return _TRAFFIC_ITYPE_MAP.get(prefix, "TRAFFIC INCIDENT")


def _traffic_post_to_talk(incident: dict, itype: str, matched_bb_id: int | None):
    """Post a traffic incident to the incidents Talk room."""
    address = incident.get("address", "Unknown address")
    issue   = incident.get("issue_reported", "Unknown")
    pub_dt  = incident.get("published_date", "")[:16].replace("T", " ")
    agency  = incident.get("agency", "").strip()
    lat     = incident.get("latitude")
    lon     = incident.get("longitude")
    coords  = f" ({lat}, {lon})" if lat and lon else ""

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
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
               "Content-Type": "application/json"}
    room_token = TALK_ROOMS["incidents"]
    url  = f"{TALK_BASE}/chat/{room_token}"
    req  = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[traffic] posted to incidents: {issue} @ {address}", flush=True)
    except Exception as e:
        print(f"[traffic] Talk post failed: {e}", flush=True)


ATXFLOODS_URL = "https://api.atxfloods.com/api/crossings"
ATXFLOODS_POLL_INTERVAL = 300  # 5 minutes

_atxfloods_state: dict[int, dict] = {}
_atxfloods_lock = threading.Lock()


def _atxfloods_post_to_talk(crossing: dict, new_status: str, old_status):
    name    = crossing.get("name", "?")
    jur     = crossing.get("jurisdiction", "?")
    addr    = crossing.get("address", "")
    lat     = crossing.get("lat")
    lon     = crossing.get("lon")
    coords  = f" ({lat}, {lon})" if lat and lon else ""
    comment = (crossing.get("comment") or "").strip()
    verb    = {"closed": "CLOSED", "caution": "CAUTION", "open": "REOPENED"}.get(
        new_status, new_status.upper()
    )
    lines = [f"[FLOODING {verb}] {name} ({jur})", f"{addr}{coords}"]
    if comment:
        lines.append(f"Note: {comment}")
    if old_status:
        lines.append(f"State: {old_status} -> {new_status}")
    msg = "\n".join(lines)

    payload = json.dumps({"message": msg}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
               "Content-Type": "application/json"}
    room_token = TALK_ROOMS["incidents"]
    url  = f"{TALK_BASE}/chat/{room_token}"
    req  = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        print(f"[atxfloods] posted: {verb} {name}", flush=True)
    except Exception as e:
        print(f"[atxfloods] Talk post failed: {e}", flush=True)


AUSTIN_EVENTS_JSON   = "/opt/battlebuddy/austin_major_events.json"
AUSTIN_EVENTS_STATE  = "/opt/battlebuddy/austin_events_state.json"
AUSTIN_EVENTS_POLL   = 6 * 3600   # 6 hours
AUSTIN_EVENTS_WINDOW = 7          # days


def _austin_events_load():
    try:
        with open(AUSTIN_EVENTS_JSON) as fh:
            return json.load(fh)
    except Exception as e:
        print(f"[events] load failed: {e}", flush=True)
        return {"events": []}


def _austin_events_upcoming(doc, today):
    from datetime import date as _date
    horizon = today + timedelta(days=AUSTIN_EVENTS_WINDOW)
    out = []
    for ev in doc.get("events", []):
        try:
            s_ = _date.fromisoformat(ev["start"])
            e_ = _date.fromisoformat(ev.get("end") or ev["start"])
        except Exception:
            continue
        if s_ <= horizon and e_ >= today:
            out.append(ev)
    out.sort(key=lambda x: x.get("start", ""))
    return out


def _austin_events_state_load():
    try:
        with open(AUSTIN_EVENTS_STATE) as fh:
            return json.load(fh)
    except Exception:
        return {"last_post_date": None, "last_event_ids": []}


def _austin_events_state_save(state):
    try:
        with open(AUSTIN_EVENTS_STATE, "w") as fh:
            json.dump(state, fh)
    except Exception as e:
        print(f"[events] state save failed: {e}", flush=True)


def _austin_events_format(events, today):
    if not events:
        return None
    lines = [f"📅 This week in Austin (window: {today.isoformat()} + {AUSTIN_EVENTS_WINDOW} days):"]
    for ev in events:
        start = ev.get("start", "?")
        end   = ev.get("end") or start
        rng   = start if end == start else f"{start} → {end}"
        extras = []
        tier = ev.get("tier")
        if tier == "major":
            extras.append("MAJOR regional impact")
        elif tier == "large":
            extras.append("large impact")
        if ev.get("blast_radius_mi"):
            extras.append(f"{ev['blast_radius_mi']}mi radius")
        if ev.get("venue"):
            extras.append(ev["venue"])
        tail = f"  ({', '.join(extras)})" if extras else ""
        lines.append(f"  • {rng}  {ev.get('name','?')}{tail}")
    return "\n".join(lines)


def _austin_events_post_to_talk(msg):
    payload = json.dumps({"message": msg}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
               "Content-Type": "application/json"}
    url = f"{TALK_BASE}/chat/{TALK_ROOMS['incidents']}"
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=10)
        print("[events] weekly summary posted", flush=True)
    except Exception as e:
        print(f"[events] Talk post failed: {e}", flush=True)


APD_CAD_URL = (
    "https://data.austintexas.gov/resource/22de-7rzg.json"
    "?$where=response_datetime>{lookback}"
    "&$order=response_datetime+DESC"
    "&$limit=5000"
)
APD_CAD_POLL_INTERVAL = 6 * 3600   # every 6 hours
APD_CAD_LOOKBACK_DAYS = 21  # dataset lags ~15 days; 21 gives comfortable headroom

# Maps CAD initial_problem_category → BB itype (for match confidence scoring)
_CAD_CATEGORY_MAP = {
    "Shoot/Stab":                  "SHOOTING",
    "Homicide":                    "SHOOTING",
    "Aggravated Assault":          "STABBING",
    "Weapons/Firearms Violations": "WEAPONS",
    "Robbery":                     "WEAPONS",
    "Bomb/Explosives":             "EXPLOSION",
    "Arson":                       "STRUCTURE FIRE",
    "Crashes":                     "CRASH/COLLISION",
    "Traffic Stop/Hazard":         "CRASH/COLLISION",
    "DUI/DWI":                     "CRASH/COLLISION",
    "Evading/Resisting Arrest":    "PURSUIT",
}

# Categories worth harvesting TGIDs for (skip noise categories)
_CAD_HARVEST_CATEGORIES = {
    "Shoot/Stab", "Homicide", "Aggravated Assault",
    "Weapons/Firearms Violations", "Robbery", "Bomb/Explosives",
    "Arson", "Crashes", "Evading/Resisting Arrest",
}


def _cad_init_db():
    """Create apd_cad and tgid_sector_hints tables if they don't exist."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS apd_cad (
            incident_number      TEXT PRIMARY KEY,
            response_ts          REAL,
            call_closed_ts       REAL,
            sector               TEXT,
            council_district     TEXT,
            priority_level       TEXT,
            initial_description  TEXT,
            initial_category     TEXT,
            final_description    TEXT,
            final_category       TEXT,
            mental_health_flag   TEXT,
            disposition          TEXT,
            geoid                TEXT,
            matched_incident_id  INTEGER,
            match_confidence     TEXT,
            fetched_ts           REAL
        );
        CREATE TABLE IF NOT EXISTS tgid_sector_hints (
            tgid        INTEGER,
            sector      TEXT,
            hit_count   INTEGER DEFAULT 1,
            last_seen   REAL,
            PRIMARY KEY (tgid, sector)
        );
        CREATE INDEX IF NOT EXISTS idx_apd_cad_response_ts
            ON apd_cad(response_ts);
        CREATE INDEX IF NOT EXISTS idx_apd_cad_unmatched
            ON apd_cad(matched_incident_id)
            WHERE matched_incident_id IS NULL;
    """)
    conn.commit()
    conn.close()
    print("[cad] DB tables ready", flush=True)


def _cad_fetch_and_store():
    """Fetch CAD records from the last 14 days and upsert into apd_cad."""
    lookback_dt = (datetime.now(timezone.utc) - timedelta(days=APD_CAD_LOOKBACK_DAYS))
    lookback_str = lookback_dt.strftime("'%Y-%m-%dT%H:%M:%S'")
    url = APD_CAD_URL.format(lookback=lookback_str)

    try:
        raw = _fetch_url_with_retry(
            url,
            headers={"Accept": "application/json"},
            timeout=30, label="cad",
        )
        records = json.loads(raw)
    except Exception as e:
        print(f"[cad] fetch error: {e}", flush=True)
        return 0

    def parse_ts(dt_str):
        if not dt_str:
            return None
        try:
            return datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=None).timestamp() - time.timezone
        except Exception:
            return None

    now = time.time()
    conn = sqlite3.connect(DB_PATH)
    upserted = 0
    for r in records:
        incident_number = r.get("incident_number")
        if not incident_number:
            continue
        conn.execute("""
            INSERT INTO apd_cad
                (incident_number, response_ts, call_closed_ts, sector,
                 council_district, priority_level, initial_description,
                 initial_category, final_description, final_category,
                 mental_health_flag, disposition, geoid, fetched_ts)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(incident_number) DO UPDATE SET
                final_description = excluded.final_description,
                final_category    = excluded.final_category,
                disposition       = excluded.disposition,
                fetched_ts        = excluded.fetched_ts
        """, (
            incident_number,
            parse_ts(r.get("response_datetime")),
            parse_ts(r.get("call_closed_datetime")),
            r.get("sector"),
            r.get("council_district"),
            r.get("priority_level"),
            r.get("initial_problem_description"),
            r.get("initial_problem_category"),
            r.get("final_problem_description"),
            r.get("final_problem_category"),
            r.get("mental_health_flag"),
            r.get("call_disposition_description"),
            r.get("geoid"),
            now,
        ))
        upserted += 1
    conn.commit()
    conn.close()
    print(f"[cad] upserted {upserted} records ({len(records)} fetched)", flush=True)
    return upserted


def _cad_match_and_harvest():
    """
    Match unmatched CAD records against scanner incidents.
    On match: enrich the incident row and harvest TGID→sector hints.
    """
    MATCH_WINDOW = 1800  # ±30 minutes in seconds
    TGID_WINDOW_PRE  = 300  # seconds before CAD response_ts to include calls
    TGID_WINDOW_POST = 120  # seconds after call_closed_ts to include calls

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Fetch unmatched CAD records that have been in the DB long enough to have
    # corresponding scanner data (response_ts < now - 2h to avoid partial incidents)
    cutoff = time.time() - 7200
    cad_rows = conn.execute("""
        SELECT * FROM apd_cad
        WHERE matched_incident_id IS NULL
          AND response_ts IS NOT NULL
          AND response_ts < ?
        ORDER BY response_ts DESC
        LIMIT 2000
    """, (cutoff,)).fetchall()

    # Pre-load scanner incidents already claimed by a prior CAD match
    # so we enforce one CAD row per scanner incident.
    claimed_ids = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT matched_incident_id FROM apd_cad "
            "WHERE matched_incident_id IS NOT NULL"
        ).fetchall()
    }

    matched = 0
    harvested_hints = 0
    # matched_cad_nums tracks CAD records successfully matched in pass 1
    # so pass 2 doesn't overwrite them (sqlite3.Row objects are stale after UPDATEs)
    matched_cad_nums = set()

    # Two-pass: pass 1 = high-confidence type matches only; pass 2 = time_only fallback
    for pass_num in (1, 2):
        for cad in cad_rows:
            # Pass 2: skip records already matched in pass 1
            if pass_num == 2 and cad["incident_number"] in matched_cad_nums:
                continue

            response_ts  = cad["response_ts"]
            sector       = cad["sector"]
            init_cat     = cad["initial_category"] or ""
            bb_itype     = _CAD_CATEGORY_MAP.get(init_cat)
            call_closed  = cad["call_closed_ts"] or (response_ts + 1800)

            # Pass 1: only typed categories (those with a known bb_itype)
            if pass_num == 1 and not bb_itype:
                continue

            # Find scanner incidents within time window
            candidates = conn.execute("""
                SELECT id, itype, agencies, ts_start FROM incidents
                WHERE ts_start BETWEEN ? AND ?
                  AND (is_test IS NULL OR is_test = 0)
                ORDER BY ABS(ts_start - ?) ASC
                LIMIT 5
            """, (response_ts - MATCH_WINDOW, response_ts + MATCH_WINDOW, response_ts)
            ).fetchall()

            best_match_id   = None
            best_confidence = None

            for inc in candidates:
                if inc["id"] in claimed_ids:
                    continue
                inc_itype = inc["itype"] or ""
                # High confidence: itype matches the CAD category mapping
                if bb_itype and inc_itype == bb_itype:
                    best_match_id   = inc["id"]
                    best_confidence = "high"
                    break
                # Time-only fallback: pass 2 only
                if pass_num == 2 and best_match_id is None:
                    best_match_id   = inc["id"]
                    best_confidence = "time_only"

            # Update CAD record with match result
            # Unique index on matched_incident_id prevents two CAD rows claiming the same incident.
            # Catch constraint violation and treat as no-match for this CAD record.
            if best_match_id:
                try:
                    conn.execute("""
                        UPDATE apd_cad
                        SET matched_incident_id = ?, match_confidence = ?
                        WHERE incident_number = ?
                    """, (best_match_id, best_confidence, cad["incident_number"]))
                    matched += 1
                    claimed_ids.add(best_match_id)
                    matched_cad_nums.add(cad["incident_number"])
                    # Enrich the scanner incident on high-confidence matches
                    if best_confidence == "high":
                        conn.execute("""
                            UPDATE incidents SET
                                description = description || ' [CAD: ' || ? || ', ' || ? || ', sector ' || ? || ']'
                            WHERE id = ? AND description NOT LIKE '%[CAD:%'
                        """, (
                            cad["final_description"] or cad["initial_description"] or "",
                            cad["disposition"] or "",
                            sector or "?",
                            best_match_id,
                        ))
                except sqlite3.IntegrityError:
                    best_match_id = None
                    conn.execute("""
                        UPDATE apd_cad SET matched_incident_id = NULL, match_confidence = NULL
                        WHERE incident_number = ?
                    """, (cad["incident_number"],))
            else:
                conn.execute("""
                    UPDATE apd_cad
                    SET matched_incident_id = NULL, match_confidence = NULL
                    WHERE incident_number = ?
                """, (cad["incident_number"],))

    # Harvest TGID hints once per CAD row (after both match passes complete)
    # to avoid double-incrementing hit_count on rows that run in pass 1 and pass 2.
    for cad in cad_rows:
        response_ts = cad["response_ts"]
        sector      = cad["sector"]
        init_cat    = cad["initial_category"] or ""
        call_closed = cad["call_closed_ts"] or (response_ts + 1800)

        if sector and init_cat in _CAD_HARVEST_CATEGORIES:
            tgid_window_start = response_ts - TGID_WINDOW_PRE
            tgid_window_end   = call_closed + TGID_WINDOW_POST
            tgid_rows = conn.execute("""
                SELECT tgid, COUNT(*) as call_count
                FROM calls
                WHERE ts BETWEEN ? AND ?
                  AND tgid IS NOT NULL
                  AND tgid > 0
                GROUP BY tgid
                HAVING call_count >= 2
            """, (tgid_window_start, tgid_window_end)).fetchall()

            for tr in tgid_rows:
                tgid = tr["tgid"]
                # Skip already-tagged/ignored TGIDs — harvest is for unknown discovery
                if tgid in TGID_META or tgid in IGNORE_TGIDS:
                    continue
                conn.execute("""
                    INSERT INTO tgid_sector_hints (tgid, sector, hit_count, last_seen)
                    VALUES (?, ?, 1, ?)
                    ON CONFLICT(tgid, sector) DO UPDATE SET
                        hit_count = hit_count + 1,
                        last_seen = excluded.last_seen
                """, (tgid, sector, response_ts))
                harvested_hints += 1

    conn.commit()
    conn.close()
    print(f"[cad] match run: {matched}/{len(cad_rows)} matched, "
          f"{harvested_hints} TGID hints harvested", flush=True)



BANNER_INCIDENT_TYPES = [
    "structure fire", "mass casualty", "hostage", "barricade", "10-99",
    "homicide", "body found", "found dead", "death investigation", "medical examiner"]



# Only these incident types trigger a site-wide banner

_active_banner_id: str | None = None

