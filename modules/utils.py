import sqlite3
import time
from modules.config import DB_PATH

def get_calls_since(since_ts: float) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM calls WHERE ts > ? ORDER BY ts DESC", (since_ts,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def detect_dps_assets(transcript: str) -> list[str]:
    # Placeholder for logic imported into utils
    return []

def apply_locution_corrections(transcript: str) -> str:
    # Placeholder for logic
    return transcript
