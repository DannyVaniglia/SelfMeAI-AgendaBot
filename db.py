import sqlite3
from datetime import datetime
from typing import List, Optional, Tuple

DB_PATH = "/var/data/data.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  start_ts INTEGER NOT NULL, -- epoch seconds (UTC)
  created_ts INTEGER NOT NULL,
  updated_ts INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user_start ON events(user_id, start_ts);
"""

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    with get_conn() as c:
        for stmt in SCHEMA.strip().split(";"):
            s = stmt.strip()
            if s:
                c.execute(s)

def add_event(user_id: int, chat_id: int, title: str, start_ts: int) -> int:
    now = int(datetime.utcnow().timestamp())
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO events (user_id, chat_id, title, start_ts, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, chat_id, title.strip(), start_ts, now, now),
        )
        return cur.lastrowid

def list_all_future(user_id: int, now_ts: int) -> List[Tuple]:
    with get_conn() as c:
        cur = c.execute(
            "SELECT id, title, start_ts FROM events WHERE user_id=? AND start_ts>=? ORDER BY start_ts ASC",
            (user_id, now_ts),
        )
        return cur.fetchall()

def update_event_time(event_id: int, new_start_ts: int) -> None:
    now = int(datetime.utcnow().timestamp())
    with get_conn() as c:
        c.execute("UPDATE events SET start_ts=?, updated_ts=? WHERE id=?", (new_start_ts, now, event_id))

def update_event_title(event_id: int, new_title: str) -> None:
    now = int(datetime.utcnow().timestamp())
    with get_conn() as c:
        c.execute("UPDATE events SET title=?, updated_ts=? WHERE id=?", (new_title.strip(), now, event_id))

def remove_event(event_id: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM events WHERE id=?", (event_id,))

def find_candidates_by_title(user_id: int, title_query: str, now_ts: int):
    like = f"%{title_query.strip()}%"
    with get_conn() as c:
        cur = c.execute(
            "SELECT id, title, start_ts FROM events WHERE user_id=? AND start_ts>=? AND title LIKE ? ORDER BY start_ts ASC",
            (user_id, now_ts, like),
        )
        return cur.fetchall()
