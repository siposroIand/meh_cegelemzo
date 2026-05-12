import json
import pickle
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path
from typing import List, Tuple, Optional, Dict, Any

import pandas as pd


class PersistenceService:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    def _init_db(self):
        conn = self._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                source_file TEXT,
                note TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_source_rows (
                snapshot_id INTEGER NOT NULL,
                data_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_raw_rows (
                snapshot_id INTEGER NOT NULL,
                data_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshot_missing_rows (
                snapshot_id INTEGER NOT NULL,
                data_json TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ktj_meta_cache (
                ktj TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------
    def save_snapshot(
        self,
        source_df: pd.DataFrame,
        raw_df: pd.DataFrame,
        missing_df: pd.DataFrame,
        source_file: str = "",
        note: str = "",
    ) -> int:
        conn = self._conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO snapshots (created_at, source_file, note) VALUES (?, ?, ?)",
            (datetime.now().isoformat(), source_file, note),
        )
        snapshot_id = cur.lastrowid

        for _, row in source_df.fillna("").iterrows():
            cur.execute(
                "INSERT INTO snapshot_source_rows (snapshot_id, data_json) VALUES (?, ?)",
                (snapshot_id, json.dumps(row.to_dict(), ensure_ascii=False)),
            )

        for _, row in raw_df.fillna("").iterrows():
            cur.execute(
                "INSERT INTO snapshot_raw_rows (snapshot_id, data_json) VALUES (?, ?)",
                (snapshot_id, json.dumps(row.to_dict(), ensure_ascii=False)),
            )

        for _, row in missing_df.fillna("").iterrows():
            cur.execute(
                "INSERT INTO snapshot_missing_rows (snapshot_id, data_json) VALUES (?, ?)",
                (snapshot_id, json.dumps(row.to_dict(), ensure_ascii=False)),
            )

        conn.commit()
        conn.close()
        return snapshot_id

    def list_snapshots(self) -> List[Tuple[int, str, str, str]]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, created_at, source_file, note FROM snapshots ORDER BY id DESC"
        ).fetchall()
        conn.close()
        return rows

    def _load_rows(self, conn, table_name: str, snapshot_id: int):
        rows = conn.execute(
            f"SELECT data_json FROM {table_name} WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchall()
        if not rows:
            return pd.DataFrame()
        parsed = [json.loads(r[0]) for r in rows]
        return pd.DataFrame(parsed)

    def load_snapshot(self, snapshot_id: int):
        conn = self._conn()
        source_df = self._load_rows(conn, "snapshot_source_rows", snapshot_id)
        raw_df = self._load_rows(conn, "snapshot_raw_rows", snapshot_id)
        missing_df = self._load_rows(conn, "snapshot_missing_rows", snapshot_id)
        conn.close()
        return source_df, raw_df, missing_df

    # ------------------------------------------------------------------
    # KTJ meta cache
    # ------------------------------------------------------------------
    def load_ktj_meta(self, ktj: str) -> Optional[Dict]:
        conn = self._conn()
        row = conn.execute(
            "SELECT payload_json FROM ktj_meta_cache WHERE ktj = ?",
            (str(ktj).strip(),),
        ).fetchone()
        conn.close()

        if not row:
            return None

        try:
            return json.loads(row[0]) if row[0] else None
        except Exception:
            return None

    def save_ktj_meta(self, ktj: str, payload: Dict):
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO ktj_meta_cache (ktj, payload_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(ktj) DO UPDATE SET
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at
            """,
            (
                str(ktj).strip(),
                json.dumps(payload or {}, ensure_ascii=False),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Shared package export / import
    # ------------------------------------------------------------------
    def export_shared_package(
        self,
        package_path: str,
        source_df: pd.DataFrame,
        raw_df: pd.DataFrame,
        missing_df: pd.DataFrame,
        state: Dict[str, Any],
        ktj_meta_cache: Optional[Dict[str, Any]] = None,
    ):
        package_path = str(package_path)

        with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                "state.json",
                json.dumps(state or {}, ensure_ascii=False, indent=2),
            )
            zf.writestr(
                "ktj_meta.json",
                json.dumps(ktj_meta_cache or {}, ensure_ascii=False, indent=2),
            )
            zf.writestr("source.pkl", pickle.dumps(source_df))
            zf.writestr("raw.pkl", pickle.dumps(raw_df))
            zf.writestr("missing.pkl", pickle.dumps(missing_df))

    def import_shared_package(self, package_path: str):
        package_path = str(package_path)

        with zipfile.ZipFile(package_path, "r") as zf:
            state = (
                json.loads(zf.read("state.json").decode("utf-8"))
                if "state.json" in zf.namelist()
                else {}
            )
            ktj_meta = (
                json.loads(zf.read("ktj_meta.json").decode("utf-8"))
                if "ktj_meta.json" in zf.namelist()
                else {}
            )
            source_df = (
                pickle.loads(zf.read("source.pkl"))
                if "source.pkl" in zf.namelist()
                else pd.DataFrame()
            )
            raw_df = (
                pickle.loads(zf.read("raw.pkl"))
                if "raw.pkl" in zf.namelist()
                else pd.DataFrame()
            )
            missing_df = (
                pickle.loads(zf.read("missing.pkl"))
                if "missing.pkl" in zf.namelist()
                else pd.DataFrame()
            )

        return source_df, raw_df, missing_df, state, ktj_meta