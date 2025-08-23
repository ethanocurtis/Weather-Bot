import os
import sqlite3
from typing import Optional, Dict, Any, List

class WxStore:
    """Tiny SQLite-backed store for weather preferences and schedules."""
    def __init__(self, db_path: str = "data/wxbot.sqlite3"):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    # ---- schema ----
    def _init_schema(self):
        cur = self.db.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weather_zips (
                user_id INTEGER PRIMARY KEY,
                zip     TEXT NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weather_subs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                zip          TEXT    NOT NULL,
                cadence      TEXT    NOT NULL, -- 'daily' | 'weekly'
                hh           INTEGER NOT NULL,
                mi           INTEGER NOT NULL,
                weekly_days  INTEGER NOT NULL DEFAULT 7,
                next_run_utc TEXT    NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                user_id INTEGER NOT NULL,
                key     TEXT    NOT NULL,
                value   TEXT,
                PRIMARY KEY (user_id, key)
            );
        """)
        self.db.commit()

    # ---- ZIP prefs ----
    def get_user_zip(self, user_id: int) -> Optional[str]:
        row = self.db.execute("SELECT zip FROM weather_zips WHERE user_id = ?", (int(user_id),)).fetchone()
        return row[0] if row else None

    def set_user_zip(self, user_id: int, zip_code: str) -> None:
        self.db.execute("INSERT INTO weather_zips(user_id, zip) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET zip = excluded.zip", (int(user_id), str(zip_code)))
        self.db.commit()

    # ---- subscriptions ----
    def add_weather_sub(self, sub: Dict[str, Any]) -> int:
        cur = self.db.cursor()
        cur.execute(
            """INSERT INTO weather_subs(user_id, zip, cadence, hh, mi, weekly_days, next_run_utc)
                  VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (int(sub["user_id"]), str(sub["zip"]), str(sub["cadence"]), int(sub["hh"]), int(sub["mi"]), int(sub.get("weekly_days", 7)), str(sub["next_run_utc"])),
        )
        self.db.commit()
        return cur.lastrowid

    def list_weather_subs(self, user_id: Optional[int]) -> List[Dict[str, Any]]:
        if user_id is None:
            rows = self.db.execute("SELECT * FROM weather_subs").fetchall()
        else:
            rows = self.db.execute("SELECT * FROM weather_subs WHERE user_id = ?", (int(user_id),)).fetchall()
        return [dict(r) for r in rows]

    def update_weather_sub(self, sub_id: int, **fields):
        if not fields:
            return
        keys = ", ".join(f"{k} = ?" for k in fields.keys())
        vals = list(fields.values()) + [int(sub_id)]
        self.db.execute(f"UPDATE weather_subs SET {keys} WHERE id = ?", vals)
        self.db.commit()

    def remove_weather_sub(self, sub_id: int, requester_id: Optional[int] = None) -> bool:
        if requester_id is not None:
            row = self.db.execute("SELECT user_id FROM weather_subs WHERE id = ?", (int(sub_id),)).fetchone()
            if not row or int(row[0]) != int(requester_id):
                return False
        cur = self.db.execute("DELETE FROM weather_subs WHERE id = ?", (int(sub_id),))
        self.db.commit()
        return cur.rowcount > 0

    # ---- arbitrary notes (key/value) ----
    def get_note(self, user_id: int, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM notes WHERE user_id = ? AND key = ?", (int(user_id), str(key))).fetchone()
        return row[0] if row else None

    def set_note(self, user_id: int, key: str, value: str) -> None:
        self.db.execute("INSERT INTO notes(user_id, key, value) VALUES(?, ?, ?) ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value", (int(user_id), str(key), str(value)))
        self.db.commit()

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass
