"""Training Centre – feedback collection and dataset management for Nexora.

Exposes a singleton `tc` so callers can do:
    from training_centre import tc
"""

import os
import sqlite3
import time

_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nexora_training.db')


class TrainingCentre:
    def __init__(self, db_path: str = _DB_PATH):
        self._db = db_path
        self._init_db()

    # ------------------------------------------------------------------ setup
    def _init_db(self):
        try:
            with self._conn() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS feedback (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_message TEXT,
                        ai_response  TEXT,
                        rating       INTEGER,
                        note         TEXT,
                        created_at   REAL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS datasets (
                        id           INTEGER PRIMARY KEY AUTOINCREMENT,
                        name         TEXT,
                        format       TEXT,
                        size         INTEGER,
                        record_count INTEGER,
                        created_at   REAL
                    )
                """)
                conn.commit()
        except Exception as e:
            print(f"[TC] DB init error: {e}")

    def _conn(self):
        return sqlite3.connect(self._db)

    # --------------------------------------------------------------- feedback
    def submit_feedback(self, user_message: str, ai_response: str, rating: int, note: str = '') -> dict:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO feedback (user_message, ai_response, rating, note, created_at) VALUES (?,?,?,?,?)",
                    (user_message, ai_response, rating, note, time.time())
                )
                conn.commit()
                return {"ok": True, "id": cur.lastrowid}
        except Exception as e:
            print(f"[TC] submit_feedback error: {e}")
            return {"ok": False, "id": None}

    def get_feedback(self, limit: int = 50) -> list:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, user_message, ai_response, rating, note, created_at "
                    "FROM feedback ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
                return [
                    {"id": r[0], "user_message": r[1], "ai_response": r[2],
                     "rating": r[3], "note": r[4], "created_at": r[5]}
                    for r in rows
                ]
        except Exception as e:
            print(f"[TC] get_feedback error: {e}")
            return []

    def get_feedback_stats(self) -> dict:
        try:
            with self._conn() as conn:
                row = conn.execute(
                    "SELECT COUNT(*), "
                    "SUM(CASE WHEN rating > 0 THEN 1 ELSE 0 END), "
                    "SUM(CASE WHEN rating <= 0 THEN 1 ELSE 0 END) "
                    "FROM feedback"
                ).fetchone()
                total    = row[0] or 0
                positive = row[1] or 0
                negative = row[2] or 0
                ratio    = round(positive / total, 3) if total else 0.0
                return {"total": total, "positive": positive, "negative": negative, "ratio": ratio}
        except Exception as e:
            print(f"[TC] get_feedback_stats error: {e}")
            return {"total": 0, "positive": 0, "negative": 0, "ratio": 0.0}

    # --------------------------------------------------------------- datasets
    def upload_dataset(self, name: str, format_type: str, size_bytes: int, record_count: int) -> dict:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO datasets (name, format, size, record_count, created_at) VALUES (?,?,?,?,?)",
                    (name, format_type, size_bytes, record_count, time.time())
                )
                conn.commit()
                return {"ok": True, "id": cur.lastrowid}
        except Exception as e:
            print(f"[TC] upload_dataset error: {e}")
            return {"ok": False, "id": None}

    def list_datasets(self) -> list:
        try:
            with self._conn() as conn:
                rows = conn.execute(
                    "SELECT id, name, format, size, record_count, created_at "
                    "FROM datasets ORDER BY created_at DESC"
                ).fetchall()
                return [
                    {"id": r[0], "name": r[1], "format": r[2],
                     "size": r[3], "record_count": r[4], "created_at": r[5]}
                    for r in rows
                ]
        except Exception as e:
            print(f"[TC] list_datasets error: {e}")
            return []

    def delete_dataset(self, dataset_id: int) -> bool:
        try:
            with self._conn() as conn:
                conn.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))
                conn.commit()
                return True
        except Exception as e:
            print(f"[TC] delete_dataset error: {e}")
            return False


# Singleton
tc = TrainingCentre()
