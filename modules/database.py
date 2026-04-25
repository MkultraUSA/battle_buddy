import sqlite3
import threading
import os

# Configuration — usually defined in a global settings file
DB_PATH = "/opt/data/workspace/battle_buddy/battle_buddy.db"
_incident_lock = threading.Lock()

def get_db_connection():
    return sqlite3.connect(DB_PATH, check_same_thread=False)

def log_incident_escalation(incident_id, ts, stage, description):
    """Store an escalation step."""
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO incident_escalations (incident_id, ts, stage, description) VALUES (?,?,?,?)",
            (incident_id, ts, stage, description)
        )
        conn.commit()
    finally:
        conn.close()

def get_incident_escalation_chain(incident_id):
    """Build escalation chain narrative from DB."""
    conn = get_db_connection()
    try:
        rows = conn.execute(
            "SELECT ts, stage FROM incident_escalations WHERE incident_id=? ORDER BY ts",
            (incident_id,)
        ).fetchall()
        return " → ".join(r[1].upper() for r in rows)
    finally:
        conn.close()

def log_tgid_guess(tgid, ts, guess, category, confidence, reasoning, transcript):
    """Store TGID guess."""
    conn = get_db_connection()
    try:
        conn.execute(
            "INSERT INTO tgid_guesses (tgid, ts, guess, category, confidence, reasoning, transcript) "
            "VALUES (?,?,?,?,?,?,?)",
            (tgid, ts, guess, category, confidence, reasoning, transcript)
        )
        conn.commit()
    finally:
        conn.close()

def get_tgid_guesses(tgid):
    """Fetch recent guesses for a TGID."""
    conn = get_db_connection()
    try:
        return conn.execute(
            "SELECT guess FROM tgid_guesses WHERE tgid=? AND confirmed=0 AND confidence IN ('HIGH','MED')",
            (tgid,)
        ).fetchall()
    finally:
        conn.close()

def confirm_tgid_guesses(tgid):
    """Mark TGID guesses as confirmed."""
    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE tgid_guesses SET confirmed=1 WHERE tgid=?", (tgid,)
        )
        conn.commit()
    finally:
        conn.close()
