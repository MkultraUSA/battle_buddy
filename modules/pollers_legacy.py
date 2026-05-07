import base64
import json
import os
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
    DECK_BASE,
    DECK_BOARD_ID,
    DECK_LABELS,
    DECK_STACK_NEW,
    GOOGLE_CSE_API_KEY,
    GOOGLE_CSE_ID,
    PI_FETCH_ENABLED,
    PI_FETCH_TOKEN,
    PI_FETCH_URL,
    TALK_BASE,
    TALK_ENABLED,
    TALK_PASS,
    TALK_ROOMS,
    TALK_USER,
    _room_for_call,
)
from modules.geocoding import _geocode_address
from modules.incident_engine import (
    _active_incidents,
    _atak_clear_marker,
    _atak_post_marker,
    _haversine_km,
    _incident_lock,
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
    detect_air_asset,
    detect_dps_assets,
    is_capitol_area,
    mentions_dps,
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
    import urllib.error as _ure
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


def apd_news_thread():
    """Poll APD press release page for new homicide/shooting announcements."""
    global _APD_NEWS_SEEN
    print("[apd-news] APD press release poller started", flush=True)
    while True:
        time.sleep(APD_NEWS_INTERVAL)
        try:
            xml_text = _fetch_url_with_retry(
                APD_NEWS_URL,
                headers={"User-Agent": "BattleBuddy/2.0",
                         "Accept": "application/rss+xml, application/xml, text/xml"},
                timeout=15, label="apd-news",
            ).decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[apd-news] fetch error: {e}", flush=True)
            continue

        articles = _apd_parse_rss(xml_text)
        # Dedup against DB — persistent across restarts
        with _APD_NEWS_LOCK:
            conn_d = sqlite3.connect(DB_PATH)
            existing = {row[0] for row in conn_d.execute("SELECT url FROM apd_seen")}
            new_articles = [a for a in articles if a["link"] not in existing]
            if new_articles:
                conn_d.executemany(
                    "INSERT OR IGNORE INTO apd_seen (url, ts) VALUES (?,?)",
                    [(a["link"], time.time()) for a in new_articles]
                )
                conn_d.commit()
            conn_d.close()
        for article in new_articles:
            title = article["title"].lower()
            if not any(kw in title for kw in _APD_HEADLINE_KW):
                continue

            print(f"[apd-news] NEW: {article['title']}", flush=True)
            url     = _resolve_article_url(
                article.get("source_url", ""), article["title"], article["link"]
            )
            detail  = _apd_fetch_article(url)
            address = detail.get("address")
            summary = detail.get("summary", article["title"])

            lat, lon = None, None
            if address:
                coords = _geocode_address(address)
                if coords:
                    lat, lon = coords

            # Determine itype from title
            t = article["title"].lower()
            if "homicide" in t or "murder" in t:
                itype = "HOMICIDE"
            elif "fatal" in t and any(w in t for w in ("crash","accident","hit","pedestrian","collision")):
                itype = "FATAL CRASH"
            elif "shooting" in t or " shot" in t:
                itype = "SHOOTING"
            elif "stab" in t:
                itype = "STABBING"
            elif "robbery" in t or "aggravated assault" in t:
                itype = "WEAPONS"
            elif "crash" in t or "collision" in t or "pedestrian" in t:
                itype = "CRASH/COLLISION"
            else:
                itype = "SHOOTING"

            pub_ts = article.get("pub_ts")
            if not pub_ts:
                print(f"[news] SKIP apd_pr (no pub_ts): {article['title']}", flush=True)
                continue
            if time.time() - pub_ts > _ARTICLE_MAX_AGE_SECS:
                age_h = (time.time() - pub_ts) / 3600
                print(f"[news] SKIP apd_pr (stale {age_h:.1f}h): {article['title']}", flush=True)
                continue
            ts   = pub_ts
            desc = f"[APD Press Release] {article['title']}. {summary[:200]}"

            # Try to match article to an existing radio-detected incident
            matched_id, match_score = _match_article_to_incident(article["title"], itype, ts)

            if matched_id:
                # Article links to a radio incident — store the link and notify
                _store_article_link(matched_id, ts, article["title"], url, "apd_pr",
                                    summary[:300], match_score)
                print(f"[apd-news] LINKED: '{article['title']}' → incident {matched_id} "
                      f"(score={match_score:.1f})", flush=True)
                loc_str = f" @ {address}" if address else ""
                msg = (
                    f"\U0001f4f0 [PRESS COVERAGE] Radio incident #{matched_id} now in the news\n"
                    f"\U0001f4f0 {article['title']}\n"
                    f"\U0001f517 {url}\n"
                    f"\U0001f4cd{loc_str}"
                )
                payload = json.dumps({"message": msg}).encode()
                creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
                headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                           "Content-Type": "application/json"}
                for room in [TALK_ROOMS["apd"], TALK_ROOMS["incidents"]]:
                    req2 = urllib.request.Request(
                        f"{TALK_BASE}/chat/{room}",
                        data=payload, headers=headers, method="POST"
                    )
                    try:
                        urllib.request.urlopen(req2, timeout=10)
                    except Exception as e:
                        print(f"[apd-news] Talk post (match) failed: {e}", flush=True)
            else:
                # No radio match — create a new incident from the press release
                conn = sqlite3.connect(DB_PATH)
                cur  = conn.execute(
                    "INSERT INTO incidents (ts_start, ts_updated, itype, description, agencies, "
                    "tgids, location, lat, lon, article_url, status) VALUES (?,?,?,?,?,?,?,?,?,?,'active')",
                    (ts, ts, itype, desc, '["APD"]', '[]',
                     address, lat, lon, url)
                )
                inc_id = cur.lastrowid
                conn.commit()
                conn.close()
                _store_article_link(inc_id, ts, article["title"], url, "apd_pr",
                                    summary[:300], 0.0)
                loc_str  = f" @ {address}" if address else ""
                msg = (
                    f"\U0001f6a8 [APD PRESS RELEASE] {article['title']}\n"
                    f"\U0001f517 {url}\n"
                    f"\U0001f4cd{loc_str}\n"
                    f"{summary[:300]}"
                )
                payload = json.dumps({"message": msg}).encode()
                creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
                headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                           "Content-Type": "application/json"}
                for room in [TALK_ROOMS["apd"], TALK_ROOMS["incidents"]]:
                    req2 = urllib.request.Request(
                        f"{TALK_BASE}/chat/{room}",
                        data=payload, headers=headers, method="POST"
                    )
                    try:
                        urllib.request.urlopen(req2, timeout=10)
                    except Exception as e:
                        print(f"[apd-news] Talk post failed: {e}", flush=True)
                threading.Thread(
                    target=send_dm_alert,
                    args=(itype, desc, address, "APD", "APD"),
                    daemon=True
                ).start()
                if lat is not None and lon is not None:
                    threading.Thread(
                        target=_atak_post_marker,
                        args=(inc_id, lat, lon, itype, address, desc),
                        daemon=True
                    ).start()

        # --- Traffic/crash news — link to existing radio incidents only -------
        try:
            treq = urllib.request.Request(
                TRAFFIC_NEWS_URL,
                headers={"User-Agent": "BattleBuddy/2.0",
                         "Accept": "application/rss+xml, application/xml, text/xml"}
            )
            txml_text = urllib.request.urlopen(treq, timeout=15).read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"[traffic-news] fetch error: {e}", flush=True)
            txml_text = None

        if txml_text:
            tarticles = _apd_parse_rss(txml_text)
            with _APD_NEWS_LOCK:
                conn_t = sqlite3.connect(DB_PATH)
                t_existing = {row[0] for row in conn_t.execute("SELECT url FROM apd_seen")}
                t_new = [a for a in tarticles if a["link"] not in t_existing]
                if t_new:
                    conn_t.executemany(
                        "INSERT OR IGNORE INTO apd_seen (url, ts) VALUES (?,?)",
                        [(a["link"], time.time()) for a in t_new]
                    )
                    conn_t.commit()
                conn_t.close()
            for ta in t_new:
                ttitle = ta["title"].lower()
                if not any(kw in ttitle for kw in
                           ("fatal", "killed", "pedestrian", "hit-and-run", "deadly")):
                    continue
                turl = _resolve_article_url(
                    ta.get("source_url", ""), ta["title"], ta["link"])
                art_itype = "FATAL CRASH" if any(
                    w in ttitle for w in ("fatal","killed","dead","deadly")) else "CRASH/COLLISION"
                tts = ta.get("pub_ts")
                if not tts:
                    print(f"[news] SKIP traffic-news (no pub_ts): {ta['title']}", flush=True)
                    continue
                if time.time() - tts > _ARTICLE_MAX_AGE_SECS:
                    age_h = (time.time() - tts) / 3600
                    print(f"[news] SKIP traffic-news (stale {age_h:.1f}h): {ta['title']}", flush=True)
                    continue
                t_inc_id, t_score = _match_article_to_incident(ta["title"], art_itype, tts)
                if t_inc_id:
                    t_detail  = _apd_fetch_article(turl)
                    t_snippet = t_detail.get("summary", "")
                    t_address = t_detail.get("address")
                    if t_address:
                        t_coords = _geocode_address(t_address)
                        if t_coords:
                            conn_ta = sqlite3.connect(DB_PATH)
                            conn_ta.execute(
                                "UPDATE incidents SET location=?, lat=?, lon=? "
                                "WHERE id=? AND (location IS NULL OR location='')",
                                (t_address, t_coords[0], t_coords[1], t_inc_id)
                            )
                            conn_ta.commit()
                            conn_ta.close()
                    _store_article_link(t_inc_id, tts, ta["title"], turl,
                                        "traffic-news", t_snippet, t_score)
                    print(f"[traffic-news] LINKED: '{ta['title']}' → "
                          f"incident {t_inc_id} (score={t_score:.1f})"
                          f"{f' addr={t_address}' if t_address else ''}", flush=True)


# ---------------------------------------------------------------------------
# ADS-B air asset tracker — adsb.lol (no rate limit, 30s poll)
# Detects helicopters and law enforcement aircraft over Austin
# Stores 30-min position trails in aircraft_positions table
# ---------------------------------------------------------------------------

# adsb.lol /v2/lat/{lat}/lon/{lon}/dist/{nm} — aircraft within dist nautical miles
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
# Reddit citizen intel poller
# ─────────────────────────────────────────────────────────────────────────────
_REDDIT_INTERVAL = 300   # 5 minutes
_REDDIT_FEEDS = [
    "https://www.reddit.com/r/Austin/new.rss",
    "https://www.reddit.com/r/AustinPolice/new.rss",
    "https://www.reddit.com/r/Austin_Texas/new.rss",
    "https://www.reddit.com/r/ATX/new.rss",
]
_REDDIT_HIGH_KW = {
    "standoff", "barricade", "swat", "shooter", "shooting", "shots fired",
    "shots", "hostage", "suspect", "armed", "pursuit", "chase", "evacuate",
    "lockdown", "explosion", "stabbing", "homicide", "murder",
    "police activity", "crime scene", "avoid the area",
}
_REDDIT_MEDIUM_KW = {
    "police", "apd", "afd", "crash", "accident", "fire", "smoke", "blocked",
    "road closed", "emergency", "cop", "cops", "officer", "helicopter",
    "air1", "star flight",
}

def _reddit_matches(title, body):
    text = (title + " " + (body or "")).lower()
    hi   = [kw for kw in _REDDIT_HIGH_KW   if kw in text]
    med  = [kw for kw in _REDDIT_MEDIUM_KW if kw in text]
    all_kw = hi + [m for m in med if m not in hi]
    return bool(hi), bool(all_kw), ",".join(all_kw)


def _reddit_match_incident(title, body, ts):
    """Score a reddit post against incidents within ±4h. Returns (incident_id, score) or (None, 0)."""
    text = (title + " " + (body or "")).lower()
    window = 4 * 3600
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT id, ts_start, itype, description, location FROM incidents "
        "WHERE ts_start BETWEEN ? AND ? AND is_test=0",
        (ts - window, ts + window)
    ).fetchall()
    conn.close()

    _TYPE_KW = {
        "SHOOTING":        ["shooting","shot","shots","fired","gun","gunshot","bullet","gunfire"],
        "STABBING":        ["stabbing","stabbed","knife","stab"],
        "CRASH/COLLISION": ["crash","accident","collision","wreck"],
        "STRUCTURE FIRE":  ["fire","smoke","burning","flames","blaze"],
        "HOMICIDE":        ["murder","homicide","killed","dead","body found"],
        "AIR ASSET ACTIVE":["helicopter","air1","star flight","chopper","aircraft"],
        "PURSUIT":         ["pursuit","chase","fleeing","high speed"],
        "OFFICER DOWN":    ["officer down","officer shot","cop shot"],
    }

    best_score, best_id = 0.0, None
    for inc_id, ts_start, itype, description, location in rows:
        score = 0.0
        for kw in _TYPE_KW.get(itype, []):
            if kw in text:
                score += 4
                break
        if location:
            for lw in (w.lower().strip(".,") for w in location.split() if len(w) > 4):
                if lw in text:
                    score += 6
        if description:
            dw = {w.lower().strip(".,") for w in description.split() if len(w) > 5}
            score += min(len(dw & set(text.split())) * 1.5, 6)
        diff = abs(ts - ts_start) / 3600
        score += 5 if diff < 0.5 else (3 if diff < 1 else (1 if diff < 2 else 0))
        if score > best_score:
            best_score, best_id = score, inc_id

    return (best_id, round(best_score, 1)) if best_score >= 8 else (None, 0.0)

_AUSTIN_NEIGHBORHOODS = {
    "circle c":      (30.1827, -97.8640),
    "mueller":       (30.2932, -97.6987),
    "hyde park":     (30.3091, -97.7341),
    "rundberg":      (30.3614, -97.6985),
    "domain":        (30.4023, -97.7230),
    "east 6th":      (30.2598, -97.7200),
    "south congress":(30.2412, -97.7500),
    "decker lane":   (30.2950, -97.6200),
    "cedar park":    (30.5052, -97.8203),
}

_INTERSECTION_RE = re.compile(
    r"\b(?:at|near|corner of)\s+([A-Z0-9][\w\.\-]+(?:\s+[A-Z0-9][\w\.\-]+){0,3})\s+(?:and|&|/|\\)\s+([A-Z0-9][\w\.\-]+(?:\s+[A-Z0-9][\w\.\-]+){0,3})",
    re.IGNORECASE,
)
_SLASH_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9\.\-]+(?:\s+[A-Z][A-Za-z0-9\.\-]+){0,3})\s*/\s*([A-Z][A-Za-z0-9\.\-]+(?:\s+[A-Z][A-Za-z0-9\.\-]+){0,3})"
)
_ADDRESS_RE = re.compile(
    r"\b(\d{2,5}\s+(?:[NSEW]\.?\s+)?[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){0,3}\s+(?:St|Street|Ave|Avenue|Blvd|Boulevard|Rd|Road|Dr|Drive|Ln|Lane|Pkwy|Parkway|Hwy|Highway|Trail|Ct|Court|Way))\b",
    re.IGNORECASE,
)


def _nominatim_geocode(query: str):
    """Geocode a free-form Austin string. Returns (lat, lon) or (None, None)."""
    try:
        q = urllib.parse.quote_plus(f"{query} Austin TX")
        url = f"https://nominatim.openstreetmap.org/search?q={q}&format=json&limit=1"
        data = json.loads(_fetch_url_with_retry(
            url,
            headers={"User-Agent": "BattleBuddy/2.0"},
            timeout=10, label="nominatim",
        ).decode("utf-8"))
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"[reddit] nominatim error for {query!r}: {e}", flush=True)
    return None, None


def _extract_tip_location(title, body):
    """Extract a location from a Reddit post. Returns (location_str, lat, lon) or (None, None, None)."""
    text = f"{title or ''} {body or ''}".strip()
    if not text:
        return (None, None, None)
    low = text.lower()

    # 1. Known neighborhoods/corridors — skip Nominatim
    for name, (lat, lon) in _AUSTIN_NEIGHBORHOODS.items():
        if name in low:
            return (name.title(), lat, lon)

    # 2. Intersection ("at X and Y", "corner of X and Y")
    m = _INTERSECTION_RE.search(text)
    if m:
        loc = f"{m.group(1).strip()} & {m.group(2).strip()}"
        lat, lon = _nominatim_geocode(loc)
        if lat is not None:
            return (loc, lat, lon)

    # 3. Slash form ("Lamar / 38th")
    m = _SLASH_RE.search(text)
    if m:
        loc = f"{m.group(1).strip()} & {m.group(2).strip()}"
        lat, lon = _nominatim_geocode(loc)
        if lat is not None:
            return (loc, lat, lon)

    # 4. Street address
    m = _ADDRESS_RE.search(text)
    if m:
        loc = m.group(1).strip()
        lat, lon = _nominatim_geocode(loc)
        if lat is not None:
            return (loc, lat, lon)

    return (None, None, None)


def _reddit_tip_recheck(db_path):
    """Re-check investigating tips against radio calls and incidents. Marks matched / no_data."""
    now = time.time()
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT post_id, title, body, tip_lat, tip_lon, tip_location, tip_ts_start "
            "FROM reddit_intel WHERE tip_status='investigating'"
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"[reddit] tip_recheck load error: {e}", flush=True)
        return

    for post_id, title, body, tip_lat, tip_lon, tip_location, tip_ts_start in rows:
        if not tip_ts_start:
            continue

        # Timeout — 2 hours, no match
        if now - tip_ts_start > 7200:
            try:
                c = sqlite3.connect(db_path)
                c.execute(
                    "UPDATE reddit_intel SET tip_status='no_data', tip_ts_cleared=?, "
                    "tip_summary=? WHERE post_id=?",
                    (now, "Monitored for 2 hours — nothing detected on radio", post_id),
                )
                c.commit(); c.close()  # noqa: E702
                print(f"[reddit] tip {post_id} -> no_data (timeout)", flush=True)
            except Exception as e:
                print(f"[reddit] tip_recheck timeout error: {e}", flush=True)
            continue

        # Geographic call match (within 0.8 km, last 2 hours)
        nearby_calls = []
        if tip_lat is not None and tip_lon is not None:
            try:
                c = sqlite3.connect(db_path)
                call_rows = c.execute(
                    "SELECT id, ts, tag, category, transcript, lat, lon, location FROM calls "
                    "WHERE ts >= ? AND lat IS NOT NULL AND lon IS NOT NULL",
                    (tip_ts_start - 7200,),
                ).fetchall()
                c.close()
                for cr in call_rows:
                    cid, cts, ctag, ccat, ctranscript, clat, clon, cloc = cr
                    try:
                        d = _haversine_km(tip_lat, tip_lon, clat, clon)
                    except Exception:
                        continue
                    if d <= 0.8:
                        nearby_calls.append((cid, cts, ctag, ccat, ctranscript, cloc))
            except Exception as e:
                print(f"[reddit] tip_recheck calls error: {e}", flush=True)

        # Text-based incident match
        inc_id, inc_score = _reddit_match_incident(title or "", body or "", tip_ts_start)

        if nearby_calls or inc_id:
            # Build summary
            parts = []
            if inc_id:
                try:
                    c = sqlite3.connect(db_path)
                    irow = c.execute(
                        "SELECT itype, location FROM incidents WHERE id=?", (inc_id,)
                    ).fetchone()
                    c.close()
                    if irow:
                        itype, iloc = irow
                        parts.append(
                            f"{itype} detected on radio"
                            + (f" near {iloc}" if iloc else "")
                        )
                except Exception:
                    pass
            if nearby_calls:
                parts.append(f"{len(nearby_calls)} related radio call(s) within 0.5 mi")
            elif not parts:
                parts.append("Possible radio match")
            summary = " — ".join(parts) + "."

            try:
                c = sqlite3.connect(db_path)
                if inc_id:
                    c.execute(
                        "UPDATE reddit_intel SET tip_status='matched', tip_ts_cleared=?, "
                        "tip_summary=?, incident_id=?, match_score=? WHERE post_id=?",
                        (now, summary, inc_id, inc_score, post_id),
                    )
                else:
                    c.execute(
                        "UPDATE reddit_intel SET tip_status='matched', tip_ts_cleared=?, "
                        "tip_summary=? WHERE post_id=?",
                        (now, summary, post_id),
                    )
                c.commit(); c.close()  # noqa: E702
                print(f"[reddit] tip {post_id} -> matched: {summary}", flush=True)
            except Exception as e:
                print(f"[reddit] tip_recheck update error: {e}", flush=True)



def reddit_intel_thread():
    """Deprecated compatibility wrapper for the extracted Reddit intel poller."""
    import warnings

    from modules.pollers.impl.reddit_intel import RedditIntelPoller

    warnings.warn(
        "reddit_intel_thread() is deprecated; use RedditIntelPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    poller = RedditIntelPoller()
    poller.start()
    while not poller.stop_event.is_set():
        time.sleep(60)


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



def adsb_air_asset_thread():
    """Deprecated compatibility wrapper for the extracted ADS-B air asset poller."""
    import warnings

    from modules.pollers.impl.adsb_air_asset import ADSBAirAssetPoller

    warnings.warn(
        "adsb_air_asset_thread() is deprecated; use ADSBAirAssetPoller().start() instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    poller = ADSBAirAssetPoller()
    poller.start()
    while not poller.stop_event.is_set():
        time.sleep(60)

# ---------------------------------------------------------------------------
# AFD Open Data poller — MOVED to modules/pollers/impl/afd_news.py
# ---------------------------------------------------------------------------
# afd_open_data_thread() and all helpers (_afd_post_to_talk, _afd_issue_to_itype,
# _afd_active_ids, _afd_lock, AFD_OPEN_DATA_URL, AFD_POLL_INTERVAL, _AFD_ITYPE_MAP)
# have been refactored into the AFDOpenDataPoller BasePoller subclass.
#
# Use:  from modules.pollers.impl.afd_news import AFDOpenDataPoller
#       AFDOpenDataPoller().start()
#
# A backward-compatible afd_open_data_thread() shim is available in
# modules/pollers/__init__.py for callers that have not yet been updated.
# ---------------------------------------------------------------------------

# Austin Open Data — Real-Time Traffic Incidents poller
# ---------------------------------------------------------------------------

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


def traffic_open_data_thread():
    """Poll Austin Open Data for active traffic incidents and cross-reference with scanner."""
    print("[traffic] Traffic Open Data poller started", flush=True)
    while True:
        time.sleep(TRAFFIC_POLL_INTERVAL)
        try:
            raw = _fetch_url_with_retry(
                TRAFFIC_OPEN_DATA_URL,
                headers={"Accept": "application/json"},
                timeout=15, label="traffic",
            )
            incidents = json.loads(raw)
        except Exception as e:
            print(f"[traffic] fetch error: {e}", flush=True)
            continue

        with _traffic_lock:
            current_ids = {inc["traffic_report_id"] for inc in incidents}

            # Detect incidents that just went ARCHIVED (were active, now gone)
            cleared = set(_traffic_active_ids.keys()) - current_ids
            for rid in cleared:
                old = _traffic_active_ids.pop(rid)
                print(f"[traffic] CLEARED: {old.get('issue_reported')} @ {old.get('address')}", flush=True)
                t_mid = old.get("atak_marker_id")
                if t_mid is not None:
                    threading.Thread(target=_atak_clear_marker, args=(t_mid,), daemon=True).start()

            # Process new active incidents
            for inc in incidents:
                rid = inc["traffic_report_id"]
                if rid in _traffic_active_ids:
                    continue  # already processed

                _traffic_active_ids[rid] = inc
                itype   = _traffic_issue_to_itype(inc.get("issue_reported", ""))
                lat     = float(inc["latitude"])  if inc.get("latitude")  else None
                lon     = float(inc["longitude"]) if inc.get("longitude") else None
                address = inc.get("address", "")

                if lat is None or lon is None:
                    print(f"[traffic] skipping (no coords): {inc.get('issue_reported')} @ {address}", flush=True)
                    continue

                # Cross-reference against active scanner incidents
                matched_id = None
                with _incident_lock:
                    for iid, bb_inc in _active_incidents.items():
                        blat = bb_inc.get("lat")
                        blon = bb_inc.get("lon")
                        if blat is None or blon is None:
                            continue
                        if _haversine_km(lat, lon, blat, blon) < 0.5:
                            matched_id = iid
                            break

                print(f"[traffic] NEW {'(matched #'+str(matched_id)+')' if matched_id else '(unmatched)'}: "
                      f"{inc.get('issue_reported')} @ {address}", flush=True)

                # Post to Talk for significant types or scanner cross-references
                if itype in _TRAFFIC_TALK_ITYPES or matched_id is not None:
                    threading.Thread(
                        target=_traffic_post_to_talk,
                        args=(inc, itype, matched_id),
                        daemon=True
                    ).start()

                # ATAK marker for all unmatched incidents
                # Offset range -(100001..200000) avoids collision with AFD range -(0..99999)
                if matched_id is None:
                    t_marker_id = -(abs(hash(rid)) % 100000) - 100001
                    _traffic_active_ids[rid]["atak_marker_id"] = t_marker_id
                    threading.Thread(
                        target=_atak_post_marker,
                        args=(t_marker_id, lat, lon, itype, address),
                        daemon=True
                    ).start()



# ---------------------------------------------------------------------------
# ATXFloods — Low-water-crossing closures poller (api.atxfloods.com)
# ---------------------------------------------------------------------------

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


def atxfloods_thread():
    """Poll ATXFloods and alert on state transitions (first sighting silent)."""
    print("[atxfloods] ATXFloods poller started", flush=True)
    while True:
        try:
            raw = _fetch_url_with_retry(
                ATXFLOODS_URL,
                headers={"Accept": "application/json"},
                timeout=20, label="atxfloods",
            )
            payload = json.loads(raw)
        except Exception as e:
            print(f"[atxfloods] fetch error: {e}", flush=True)
            time.sleep(ATXFLOODS_POLL_INTERVAL)
            continue

        crossings = payload.get("attributes", []) if isinstance(payload, dict) else []
        if not crossings:
            print("[atxfloods] empty response", flush=True)
            time.sleep(ATXFLOODS_POLL_INTERVAL)
            continue

        transitions = 0
        with _atxfloods_lock:
            for c in crossings:
                try:
                    cid = int(c["id"])
                except (KeyError, ValueError, TypeError):
                    continue
                status = (c.get("status") or "").lower()
                if status not in ("open", "closed", "caution"):
                    continue

                prev = _atxfloods_state.get(cid)
                if prev is None:
                    # First sighting — seed state silently, no alert, no marker
                    _atxfloods_state[cid] = {"status": status, "marker_id": None}
                    continue
                if prev["status"] == status:
                    continue

                old_status = prev["status"]
                prev["status"] = status
                transitions += 1
                _atxfloods_post_to_talk(c, status, old_status)

                try:
                    lat = float(c["lat"]); lon = float(c["lon"])  # noqa: E702
                except (KeyError, ValueError, TypeError):
                    lat = lon = None

                if status == "open" and prev.get("marker_id") is not None:
                    threading.Thread(target=_atak_clear_marker,
                                     args=(prev["marker_id"],), daemon=True).start()
                    prev["marker_id"] = None
                elif status in ("closed", "caution") and lat is not None and lon is not None:
                    marker_id = -(abs(cid) % 100000) - 200001
                    prev["marker_id"] = marker_id
                    label = f"{c.get('name','')} {c.get('address','')}".strip()
                    threading.Thread(
                        target=_atak_post_marker,
                        args=(marker_id, lat, lon, "FLOODING", label),
                        daemon=True,
                    ).start()

        if transitions:
            print(f"[atxfloods] {transitions} state transition(s) this cycle", flush=True)
        time.sleep(ATXFLOODS_POLL_INTERVAL)




# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Austin major events — weekly "this week in Austin" digest
# ---------------------------------------------------------------------------
# Reads /opt/battlebuddy/austin_major_events.json and posts a summary to the
# incidents Talk room when events are within 7 days. File is re-read every
# poll cycle — edits take effect without a service restart.
# ---------------------------------------------------------------------------

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


def austin_events_thread():
    """Post a 'this week in Austin' digest when the 7-day window changes
    or when >=7 days have passed since the last post."""
    print("[events] Austin major events poller started", flush=True)
    try:
        import zoneinfo
        austin_tz = zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        austin_tz = None
    from datetime import date as _date
    from datetime import datetime as _dt
    while True:
        try:
            now_austin = _dt.now(austin_tz) if austin_tz else _dt.now()
            today = now_austin.date()
            doc = _austin_events_load()
            events = _austin_events_upcoming(doc, today)
            state = _austin_events_state_load()
            current_ids = [e.get("id") for e in events]
            last_date_s = state.get("last_post_date")
            last_ids    = state.get("last_event_ids", [])
            days_since = None
            if last_date_s:
                try:
                    days_since = (today - _date.fromisoformat(last_date_s)).days
                except Exception:
                    days_since = None

            should_post = False
            reason = None
            if events and current_ids != last_ids:
                should_post = True
                reason = "event list changed"
            elif events and (days_since is None or days_since >= 7):
                should_post = True
                reason = "weekly cadence"

            if should_post:
                msg = _austin_events_format(events, today)
                if msg:
                    _austin_events_post_to_talk(msg)
                    _austin_events_state_save({
                        "last_post_date": today.isoformat(),
                        "last_event_ids": current_ids,
                    })
                    print(f"[events] posted ({reason}): {len(events)} events", flush=True)
            else:
                print(f"[events] quiet: {len(events)} events in window, "
                      f"last posted {last_date_s} ({days_since}d ago)", flush=True)
        except Exception as e:
            print(f"[events] cycle error: {e}", flush=True)
        time.sleep(AUSTIN_EVENTS_POLL)


# ---------------------------------------------------------------------------
# Austin PD CAD — Retrospective enrichment poller
# ---------------------------------------------------------------------------
# Polls the APD Computer Aided Dispatch open data feed (~2 week lag) and
# cross-references it against scanner incidents to:
#   1. Enrich incidents with CAD final description, mental health flag,
#      disposition, sector, and council district
#   2. Harvest TGID→sector hints for unknown talkgroup identification
# ---------------------------------------------------------------------------

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


def apd_cad_thread():
    """Retrospective CAD enrichment — poll every 6 hours, match and harvest."""
    print("[cad] APD CAD enrichment poller started", flush=True)
    _cad_init_db()
    while True:
        _cad_fetch_and_store()
        _cad_match_and_harvest()
        time.sleep(APD_CAD_POLL_INTERVAL)




# Unit/callsign patterns common in P25 traffic
_UNIT_PATTERNS = [
    re.compile(r'\b((?:engine|truck|medic|rescue|battalion|squad|ladder|unit)\s+\d{1,3})\b', re.I),
    re.compile(r'\b([A-Z][a-z]+\s+\d{1,3})\b'),          # Adam 21, Baker 45
    re.compile(r'\b([A-Z]-?\d{2,3})\b'),                  # A-21, B45
    re.compile(r'\bunit[s]?\s+(\d{1,4})\b', re.I),
]

_HIGH_PRIORITY = {
    "OFFICER DOWN", "SHOOTING", "STABBING", "AIRCRAFT EMERGENCY",
    "MASS CASUALTY", "STRUCTURE FIRE", "HOSTAGE/BARRICADE",
}
_MED_PRIORITY = {
    "CRASH/COLLISION", "HAZMAT", "FIRE DISPATCH",
    "MULTI-AGENCY RESPONSE", "APD SURGE", "TRANSIT INCIDENT", "AIRPORT EMERGENCY",
}
_HIGH_KW = ["officer down", "shots fired", "shooting", "stabbing",
            "structure fire", "mass casualty", "hostage", "barricade", "10-99",
            "homicide", "body found", "found dead", "death investigation", "medical examiner"]
_MED_KW  = ["crash", "collision", "hazmat", "fire", "rollover", "working fire"]


def _extract_units(transcript: str) -> list[str]:
    found, seen = [], set()
    for pat in _UNIT_PATTERNS:
        for m in pat.finditer(transcript):
            unit = m.group(1).strip()
            key  = unit.lower()
            if key not in seen:
                seen.add(key)
                found.append(unit)
    return found[:6]


def post_to_talk(call: dict):
    if not TALK_ENABLED:
        return

    ts         = datetime.fromtimestamp(call["ts"]).strftime("%H:%M")
    tag        = call.get("tag") or f"TGID {call.get('tgid')}"
    cat        = call.get("category", "Unknown")
    loc        = f" @ {call['location']}" if call.get("location") else ""
    transcript = call.get("transcript") or "(no transcript)"
    tgid       = call.get("tgid")
    text_lower = transcript.lower()

    # Only post to Talk for genuinely high-danger calls — officer down, shots fired, etc.
    # Incident-level alerts are handled by send_dm_alert when an incident is created.
    # This prevents routine chatter from flooding the Talk room.
    groq_pri_early = (call.get("groq") or {}).get("priority", "NONE")
    has_high_kw = any(k in text_lower for k in _HIGH_KW)
    if groq_pri_early != "HIGH" and not has_high_kw:
        return

    # --- Incident linkage ---
    incident_line = ""
    matched_itype = None
    with _incident_lock:
        for inc in _active_incidents.values():
            if tgid in inc.get("tgids", set()) or cat in inc.get("agencies", set()):
                age = int((time.time() - inc["ts_updated"]) / 60)
                matched_itype = inc["itype"]  # noqa: F841
                incident_line = f"\n⚡ INCIDENT: {inc['itype']} — active {age}m"
                break

    # All calls reaching this point are high-priority by definition (gated above)
    priority = "🔴"

    # --- Unit extraction ---
    units = _extract_units(transcript)
    units_line = f"\nUnits: {', '.join(units)}" if units else ""

    # --- Air asset context ---
    air_line = ""
    air_context = detect_air_asset(tgid, transcript, cat)
    if air_context:
        air_line = f"\n🚁 AIR: {air_context}"

    # --- DPS asset/Capitol context ---
    dps_line = ""
    if cat == "DPS" or mentions_dps(transcript):
        assets = detect_dps_assets(transcript)
        capitol = is_capitol_area(transcript, call.get("location"))
        parts = []
        if assets:
            parts.append(", ".join(assets))
        if capitol:
            parts.append("Capitol area")
        if parts:
            dps_line = f"\n🏛 DPS: {' — '.join(parts)}"

    message = (
        f"{priority} [{ts}] {cat} — {tag}{loc}"
        f"{incident_line}"
        f"{air_line}"
        f"{dps_line}"
        f"{units_line}"
        f"\n\"{transcript}\""
    )

    payload = json.dumps({"message": message}).encode()
    creds   = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
               "Content-Type": "application/json"}

    for room_token in _room_for_call(call, priority):
        url = f"{TALK_BASE}/chat/{room_token}"
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            urllib.request.urlopen(req, timeout=10)
            print(f"[talk] posted {priority} {tag} → {room_token}: {transcript[:50]}", flush=True)
        except Exception as e:
            print(f"[talk] post failed → {room_token}: {e}", flush=True)


# ---------------------------------------------------------------------------
# Announcement banner — site-wide breaking alert for the most serious incidents
# ---------------------------------------------------------------------------

BANNER_BASE = os.environ.get("NEXTCLOUD_BANNER_BASE", "https://nextcloud.example.com/index.php/apps/announcementbanner/banners")

# Only these incident types trigger a site-wide banner
BANNER_ITYPES = {
    "OFFICER DOWN", "SHOOTING", "STABBING", "MASS CASUALTY",
    "STRUCTURE FIRE", "HOSTAGE/BARRICADE", "AIRCRAFT EMERGENCY",
    "AIR ASSET ACTIVE",
}

_active_banner_id: str | None = None
_banner_lock = threading.Lock()


def _banner_api(path: str = "", data: dict | None = None, method: str | None = None):
    if method is None:
        method = "POST" if data is not None else "GET"
    url = BANNER_BASE + (f"/{path}" if path else "")
    creds = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Authorization": f"Basic {creds}", "OCS-APIRequest": "true",
                 "Content-Type": "application/json"},
        method=method
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def post_banner(itype: str, location: str | None, agencies: str):
    """Post a site-wide breaking banner for serious incidents."""
    global _active_banner_id
    if itype not in BANNER_ITYPES:
        return
    loc_str = f" @ {location}" if location else ""
    message  = f"🔴 BREAKING: {itype}{loc_str} — {agencies} responding"
    with _banner_lock:
        try:
            # Remove previous banner if one exists
            if _active_banner_id:
                _banner_api(_active_banner_id, method="DELETE")
            result = _banner_api(data={
                "enabled": True, "message": message, "variant": "danger",
                "dismissible": False, "readMoreText": "", "readMoreUrl": "",
                "scheduleStart": "", "scheduleEnd": "",
                "audienceTarget": "all", "audienceGroups": [],
                "targetAppMode": "all", "targetApps": [],
            })
            _active_banner_id = result.get("id")
            print(f"[banner] posted: {message}", flush=True)
        except Exception as e:
            print(f"[banner] failed: {e}", flush=True)


def clear_banner(itype: str):
    """Remove the site-wide banner when an incident clears."""
    global _active_banner_id
    if itype not in BANNER_ITYPES:
        return
    with _banner_lock:
        if _active_banner_id:
            try:
                _banner_api(_active_banner_id, method="DELETE")
                print(f"[banner] cleared for {itype}", flush=True)
                _active_banner_id = None
            except Exception as e:
                print(f"[banner] clear failed: {e}", flush=True)



def create_deck_card(incident: dict):
    """Create a Deck card in the 🆕 New column when a new incident is detected."""
    itype    = incident.get("itype", "INCIDENT")
    desc     = incident.get("description", "")
    location = incident.get("location")
    agencies = ", ".join(json.loads(incident.get("agencies") or "[]"))
    ts       = datetime.fromtimestamp(incident.get("ts_start", time.time())).strftime("%H:%M")

    title = f"{itype}"
    if location:
        title += f" @ {location}"

    body = (
        f"**Time:** {ts}\n"
        f"**Agencies:** {agencies or 'unknown'}\n"
        f"**Details:** {desc}\n"
    )

    label_id = DECK_LABELS.get(itype, DECK_LABELS.get("SHOOTING"))  # fallback
    creds    = base64.b64encode(f"{TALK_USER}:{TALK_PASS}".encode()).decode()
    headers  = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}

    # Create the card
    card_url = f"{DECK_BASE}/boards/{DECK_BOARD_ID}/stacks/{DECK_STACK_NEW}/cards"
    card_data = json.dumps({"title": title, "type": "plain", "order": 0,
                            "description": body}).encode()
    try:
        req  = urllib.request.Request(card_url, data=card_data, headers=headers, method="POST")
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        card_id = resp.get("id")
        print(f"[deck] card created: {title} (id={card_id})", flush=True)

        # Assign label if matched
        if label_id and card_id:
            label_url  = f"{DECK_BASE}/boards/{DECK_BOARD_ID}/stacks/{DECK_STACK_NEW}/cards/{card_id}/assignLabel"
            label_data = json.dumps({"labelId": label_id}).encode()
            req = urllib.request.Request(label_url, data=label_data, headers=headers, method="PUT")
            urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[deck] card creation failed: {e}", flush=True)
