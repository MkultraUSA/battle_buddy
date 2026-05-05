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

from modules.pollers.base import BasePoller

logger = logging.getLogger("RedditIntelPoller")

REDDIT_INTERVAL: float = 300.0
REDDIT_FEEDS = [
    "https://www.reddit.com/r/Austin/new.rss",
    "https://www.reddit.com/r/AustinPolice/new.rss",
    "https://www.reddit.com/r/Austin_Texas/new.rss",
    "https://www.reddit.com/r/ATX/new.rss",
]
REDDIT_HIGH_KW = {
    "standoff", "barricade", "swat", "shooter", "shooting", "shots fired",
    "shots", "hostage", "suspect", "armed", "pursuit", "chase", "evacuate",
    "lockdown", "explosion", "stabbing", "homicide", "murder",
    "police activity", "crime scene", "avoid the area",
}
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


def reddit_matches(title: str, body: str | None) -> tuple[bool, bool, str]:
    text = (title + " " + (body or "")).lower()
    hi = [kw for kw in REDDIT_HIGH_KW if kw in text]
    med = [kw for kw in REDDIT_MEDIUM_KW if kw in text]
    all_kw = hi + [m for m in med if m not in hi]
    return bool(hi), bool(all_kw), ",".join(all_kw)


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

        for feed_url in self.feeds:
            try:
                root = self.fetch_feed(feed_url)
            except Exception as exc:
                logger.warning("[reddit] fetch error %s: %s", feed_url, exc)
                continue
            self.process_feed(root, feed_url, DB_PATH, send_dm_alert)

        try:
            self.tip_recheck(DB_PATH, _haversine_km)
        except Exception as exc:
            logger.warning("[reddit] tip_recheck loop error: %s", exc)

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
        hi, matched, keywords = reddit_matches(post["title"], post["body"])
        if not matched:
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
                keywords,
                now_ts,
            ),
        )
        conn.commit()
        conn.close()
        logger.info("[reddit] NEW %s: %s", "HI" if hi else "med", post["title"][:80])

        self.enrich_tip_location(post, db_path)
        self.enrich_incident_match(post, db_path)
        if hi:
            self.send_high_confidence_alert(post, keywords, db_path, send_alert)
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
