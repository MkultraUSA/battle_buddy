import sqlite3




def test_issue_session_preserves_canonical_premium_username(tmp_path, monkeypatch):
    from modules import premium
    db = tmp_path / "calls.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE premium_users (username TEXT PRIMARY KEY, status TEXT)"
    )
    conn.execute(
        "CREATE TABLE sessions (token TEXT PRIMARY KEY, username TEXT, created_ts REAL, expires_ts REAL, is_admin INTEGER, is_premium INTEGER)"
    )
    conn.execute("INSERT INTO premium_users (username, status) VALUES (?, ?)", ("Paul", "active"))
    conn.commit()
    conn.close()

    monkeypatch.setattr(premium, "DB_PATH", str(db))
    token = premium._issue_session("paul")

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT username, is_premium FROM sessions WHERE token=?", (token,)).fetchone()
    conn.close()

    assert row == ("Paul", 1)

