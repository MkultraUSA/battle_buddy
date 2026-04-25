import os
import sqlite3
import threading

# Assuming these exist in modules.config or would be imports
# I need to confirm where these specific globals live.
# Given they are used in audio_receiver.py, they are likely in modules.config 
# which is imported as "from modules.config import *".
from modules.config import TGID_TSV, TGID_META, IGNORE_TGIDS, CAT_COORDS, CATEGORY_PATTERNS, IGNORE_TAGS

def _tag_is_ignored(tag: str) -> bool:
    tl = tag.lower()
    return any(p.lower() in tl for p in IGNORE_TAGS)

def _tag_to_category(tag: str) -> str:
    for cat, patterns in CATEGORY_PATTERNS:
        if any(p.lower() in tag.lower() for p in patterns):
            return cat
    return "Unknown"

def load_talkgroups(tsv_path: str = TGID_TSV):
    global TGID_META, IGNORE_TGIDS
    if not os.path.exists(tsv_path):
        print(f"[tg] TSV not found at {tsv_path} — using built-in metadata only", flush=True)
        return
    loaded = ignored = 0
    with open(tsv_path) as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            try:
                tgid = int(parts[0])
            except ValueError:
                continue
            tag = parts[1].strip()
            if _tag_is_ignored(tag):
                IGNORE_TGIDS.add(tgid)
                ignored += 1
            else:
                cat       = _tag_to_category(tag)
                lat, lon  = CAT_COORDS.get(cat, CAT_COORDS["Unknown"])
                TGID_META[tgid] = {"tag": tag, "cat": cat, "lat": lat, "lon": lon}
                loaded += 1
    print(f"[tg] {loaded} talkgroups loaded, {ignored} on ignore list", flush=True)
