import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


class CacheDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    def _conn(self):
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA busy_timeout=30000;")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kuj_cache (
                kuj TEXT NOT NULL,
                adattipus TEXT NOT NULL,
                status TEXT,
                fetched_at TEXT,
                raw_json TEXT,
                error_message TEXT,
                method TEXT,
                PRIMARY KEY (kuj, adattipus)
            )
        """)
        conn.commit()
        conn.close()

    def get(self, kuj: str, adattipus: str) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        row = conn.execute(
            "SELECT kuj, adattipus, status, fetched_at, raw_json, error_message, method "
            "FROM kuj_cache WHERE kuj=? AND adattipus=?",
            (kuj, adattipus),
        ).fetchone()
        if not row:
            return None
        return {
            "kuj": row[0],
            "adattipus": row[1],
            "status": row[2],
            "fetched_at": row[3],
            "raw_json": row[4],
            "error_message": row[5],
            "method": row[6],
        }

    def save(self, kuj: str, adattipus: str, status: str,
             raw_json: str = "", error_message: str = "", method: str = "requests"):
        conn = self._conn()
        conn.execute("""
            INSERT INTO kuj_cache (kuj, adattipus, status, fetched_at, raw_json, error_message, method)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(kuj, adattipus) DO UPDATE SET
                status=excluded.status,
                fetched_at=excluded.fetched_at,
                raw_json=excluded.raw_json,
                error_message=excluded.error_message,
                method=excluded.method
        """, (kuj, adattipus, status, datetime.now().isoformat(), raw_json, error_message, method))
        conn.commit()

    def clear_all(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("DELETE FROM kuj_cache")
        conn.commit()
        conn.close()
        self._local.conn = None