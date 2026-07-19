import os
import sqlite3
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List


class WxStore:
    """SQLite store with backward-compatible migrations for Weather Bot."""

    def __init__(self, db_path: str = "data/wxbot.sqlite3"):
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def _columns(self, table: str) -> set[str]:
        return {r[1] for r in self.db.execute(f"PRAGMA table_info({table})").fetchall()}

    def _add_column(self, table: str, name: str, definition: str) -> None:
        if name not in self._columns(table):
            self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _init_schema(self):
        cur = self.db.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS weather_zips (user_id INTEGER PRIMARY KEY, zip TEXT NOT NULL)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS weather_locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT NOT NULL DEFAULT 'Default',
                query TEXT,
                display_name TEXT NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                country_code TEXT,
                admin1 TEXT,
                timezone TEXT,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_weather_locations_user ON weather_locations(user_id)")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_weather_locations_user_name ON weather_locations(user_id, name COLLATE NOCASE)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS weather_subs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                zip TEXT NOT NULL DEFAULT '',
                cadence TEXT NOT NULL,
                hh INTEGER NOT NULL,
                mi INTEGER NOT NULL,
                weekly_days INTEGER,
                tz_name TEXT,
                units TEXT,
                next_run_utc TEXT NOT NULL
            )
        """)
        # Generalized subscription fields; old rows remain valid.
        for name, definition in [
            ("location_id", "INTEGER"), ("location_name", "TEXT"), ("latitude", "REAL"),
            ("longitude", "REAL"), ("country_code", "TEXT"), ("destination_type", "TEXT NOT NULL DEFAULT 'dm'"),
            ("guild_id", "INTEGER"), ("channel_id", "INTEGER"), ("created_by", "INTEGER"),
            ("condition_metric", "TEXT"), ("condition_operator", "TEXT"), ("condition_value", "REAL"),
            ("condition_unit", "TEXT"), ("enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("failure_count", "INTEGER NOT NULL DEFAULT 0"), ("last_error", "TEXT"),
            ("last_result", "TEXT"), ("last_sent_at", "TEXT")
        ]:
            self._add_column("weather_subs", name, definition)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_weather_subs_next ON weather_subs(next_run_utc)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_weather_subs_user ON weather_subs(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_weather_subs_guild ON weather_subs(guild_id)")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                user_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                PRIMARY KEY (user_id, key)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                guild_id INTEGER,
                guild_name TEXT,
                request_type TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'submitted',
                staff_note TEXT,
                feedback_message_id INTEGER,
                feedback_channel_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                notified_at TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_user ON feedback_requests(user_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_status ON feedback_requests(status)")
        self.db.commit()

    # Legacy ZIP compatibility
    def get_user_zip(self, user_id: int) -> Optional[str]:
        row = self.db.execute("SELECT zip FROM weather_zips WHERE user_id = ?", (int(user_id),)).fetchone()
        return row["zip"] if row else None

    def set_user_zip(self, user_id: int, zip_code: str) -> None:
        self.db.execute("INSERT INTO weather_zips(user_id, zip) VALUES (?, ?) ON CONFLICT(user_id) DO UPDATE SET zip=excluded.zip", (int(user_id), str(zip_code)))
        self.db.commit()

    # Locations
    def save_location(self, user_id: int, location: Dict[str, Any], name: str = "Default", make_default: bool = True) -> int:
        now = datetime.now(timezone.utc).isoformat()
        if make_default:
            self.db.execute("UPDATE weather_locations SET is_default=0 WHERE user_id=?", (int(user_id),))
        self.db.execute("""
            INSERT INTO weather_locations(user_id,name,query,display_name,latitude,longitude,country_code,admin1,timezone,is_default,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(user_id,name) DO UPDATE SET query=excluded.query,display_name=excluded.display_name,
              latitude=excluded.latitude,longitude=excluded.longitude,country_code=excluded.country_code,
              admin1=excluded.admin1,timezone=excluded.timezone,is_default=excluded.is_default
        """, (int(user_id), name.strip() or "Default", location.get("query"), location["display_name"], float(location["latitude"]), float(location["longitude"]), location.get("country_code"), location.get("admin1"), location.get("timezone"), 1 if make_default else 0, now))
        self.db.commit()
        row = self.db.execute("SELECT id FROM weather_locations WHERE user_id=? AND name=? COLLATE NOCASE", (int(user_id), name.strip() or "Default")).fetchone()
        return int(row["id"])

    def get_default_location(self, user_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT * FROM weather_locations WHERE user_id=? ORDER BY is_default DESC,id ASC LIMIT 1", (int(user_id),)).fetchone()
        return dict(row) if row else None

    def get_location(self, location_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT * FROM weather_locations WHERE id=?", (int(location_id),)).fetchone()
        return dict(row) if row else None

    def list_locations(self, user_id: int) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM weather_locations WHERE user_id=? ORDER BY is_default DESC,name", (int(user_id),)).fetchall()]

    # Subscriptions
    def add_weather_sub(self, sub: Dict[str, Any]) -> int:
        fields = ["user_id","zip","cadence","hh","mi","weekly_days","tz_name","units","next_run_utc","location_id","location_name","latitude","longitude","country_code","destination_type","guild_id","channel_id","created_by","condition_metric","condition_operator","condition_value","condition_unit","enabled"]
        vals = [sub.get(f) for f in fields]
        vals[0] = int(vals[0]); vals[1] = str(vals[1] or "")
        cur = self.db.execute(f"INSERT INTO weather_subs({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", vals)
        self.db.commit(); return int(cur.lastrowid)

    def list_weather_subs(self, user_id: Optional[int] = None, guild_id: Optional[int] = None, enabled_only: bool = False) -> List[Dict[str, Any]]:
        where, args = [], []
        if user_id is not None: where.append("user_id=?"); args.append(int(user_id))
        if guild_id is not None: where.append("guild_id=?"); args.append(int(guild_id))
        if enabled_only: where.append("enabled=1")
        sql = "SELECT * FROM weather_subs" + ((" WHERE " + " AND ".join(where)) if where else "") + " ORDER BY next_run_utc ASC"
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def remove_weather_sub(self, sub_id: int, requester_id: int, guild_id: Optional[int] = None, allow_guild_admin: bool = False) -> bool:
        if allow_guild_admin and guild_id is not None:
            cur = self.db.execute("DELETE FROM weather_subs WHERE id=? AND (user_id=? OR guild_id=?)", (int(sub_id), int(requester_id), int(guild_id)))
        else:
            cur = self.db.execute("DELETE FROM weather_subs WHERE id=? AND user_id=?", (int(sub_id), int(requester_id)))
        self.db.commit(); return cur.rowcount > 0

    def update_weather_sub(self, sub_id: int, next_run_utc: Optional[str] = None, **updates) -> None:
        if next_run_utc is not None: updates["next_run_utc"] = str(next_run_utc)
        allowed = {"next_run_utc","enabled","failure_count","last_error","last_result","last_sent_at"}
        updates = {k:v for k,v in updates.items() if k in allowed}
        if not updates: return
        self.db.execute(f"UPDATE weather_subs SET {','.join(f'{k}=?' for k in updates)} WHERE id=?", [*updates.values(), int(sub_id)])
        self.db.commit()

    # Feedback requests
    def create_feedback_request(self, user_id: int, guild_id: Optional[int], guild_name: Optional[str], request_type: str, message: str) -> int:
        now = datetime.now(timezone.utc).isoformat()
        cur = self.db.execute("INSERT INTO feedback_requests(user_id,guild_id,guild_name,request_type,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (int(user_id), guild_id, guild_name, request_type, message, now, now))
        self.db.commit(); return int(cur.lastrowid)

    def set_feedback_message(self, request_id: int, channel_id: int, message_id: int) -> None:
        self.db.execute("UPDATE feedback_requests SET feedback_channel_id=?,feedback_message_id=? WHERE id=?", (int(channel_id), int(message_id), int(request_id))); self.db.commit()

    def get_feedback_request(self, request_id: int) -> Optional[Dict[str, Any]]:
        row = self.db.execute("SELECT * FROM feedback_requests WHERE id=?", (int(request_id),)).fetchone(); return dict(row) if row else None

    def list_feedback_requests(self, user_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM feedback_requests WHERE user_id=? ORDER BY id DESC LIMIT ?", (int(user_id), int(limit))).fetchall()]

    def update_feedback_status(self, request_id: int, status: str, staff_note: Optional[str] = None) -> Optional[Dict[str, Any]]:
        now = datetime.now(timezone.utc).isoformat(); completed = now if status in {"completed","fixed","declined","duplicate"} else None
        self.db.execute("UPDATE feedback_requests SET status=?,staff_note=COALESCE(?,staff_note),updated_at=?,completed_at=? WHERE id=?", (status, staff_note, now, completed, int(request_id))); self.db.commit()
        return self.get_feedback_request(request_id)

    def mark_feedback_notified(self, request_id: int) -> None:
        self.db.execute("UPDATE feedback_requests SET notified_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), int(request_id))); self.db.commit()

    def get_note(self, user_id: int, key: str) -> Optional[str]:
        row = self.db.execute("SELECT value FROM notes WHERE user_id=? AND key=?", (int(user_id), str(key))).fetchone(); return row["value"] if row else None

    def set_note(self, user_id: int, key: str, value: str) -> None:
        self.db.execute("INSERT INTO notes(user_id,key,value) VALUES(?,?,?) ON CONFLICT(user_id,key) DO UPDATE SET value=excluded.value", (int(user_id), str(key), str(value))); self.db.commit()

    def close(self):
        try: self.db.close()
        except Exception: pass
