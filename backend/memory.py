"""
Long-term memory engine for Nexora.
Uses SQLite for storage and optional sentence-transformers for semantic search.
"""
import os
import json
import sqlite3
import time

# Try to load sentence-transformers for semantic embeddings.
try:
    from sentence_transformers import SentenceTransformer as _SentenceTransformer
    import numpy as _np
    _has_embeddings = True
except Exception:
    _has_embeddings = False


def _cosine_sim(a, b):
    """Cosine similarity between two flat lists/arrays."""
    import math
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


class MemoryEngine:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nexora_memory.db')
        self._model = None
        self._has_embeddings = False

        # Load embedding model if sentence-transformers is available.
        if _has_embeddings:
            try:
                self._model = _SentenceTransformer('all-MiniLM-L6-v2')
                self._has_embeddings = True
                print("[MEMORY] Sentence-transformers loaded — semantic search enabled.")
            except Exception as e:
                print(f"[MEMORY] Could not load SentenceTransformer: {e}. Falling back to keyword search.")
                self._has_embeddings = False
        else:
            print("[MEMORY] sentence-transformers not installed — keyword search only.")

        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS memories (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        content         TEXT    NOT NULL,
                        category        TEXT    DEFAULT 'fact',
                        tags            TEXT    DEFAULT '[]',
                        embedding_json  TEXT,
                        created_at      REAL,
                        accessed_at     REAL,
                        access_count    INTEGER DEFAULT 0
                    )
                """)
                conn.commit()
        except Exception as e:
            print(f"[MEMORY] DB init error: {e}")

    def _row_to_dict(self, row, columns):
        d = dict(zip(columns, row))
        # Deserialise tags from JSON string
        try:
            d['tags'] = json.loads(d.get('tags') or '[]')
        except Exception:
            d['tags'] = []
        # Drop the raw embedding blob from returned dicts
        d.pop('embedding_json', None)
        return d

    def save_memory(self, content, category='fact', tags=None):
        """Store a memory and return a dict with its id."""
        try:
            if tags is None:
                tags = []
            tags_json = json.dumps(tags)
            now = time.time()

            embedding_json = None
            if self._has_embeddings and self._model is not None:
                try:
                    vec = self._model.encode(content).tolist()
                    embedding_json = json.dumps(vec)
                except Exception as e:
                    print(f"[MEMORY] Embedding generation failed: {e}")

            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute(
                    """INSERT INTO memories (content, category, tags, embedding_json, created_at, accessed_at, access_count)
                       VALUES (?, ?, ?, ?, ?, ?, 0)""",
                    (content, category, tags_json, embedding_json, now, now)
                )
                conn.commit()
                return {'id': cur.lastrowid, 'content': content, 'category': category, 'tags': tags}
        except Exception as e:
            print(f"[MEMORY] save_memory error: {e}")
            return {'id': None, 'error': str(e)}

    def search_memories(self, query, limit=5):
        """Search memories semantically (if embeddings available) or by keyword."""
        try:
            if self._has_embeddings and self._model is not None:
                return self._semantic_search(query, limit)
            return self._keyword_search(query, limit)
        except Exception as e:
            print(f"[MEMORY] search_memories error: {e}")
            return []

    def _semantic_search(self, query, limit):
        try:
            query_vec = self._model.encode(query).tolist()
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT * FROM memories WHERE embedding_json IS NOT NULL"
                ).fetchall()
            if not rows:
                return self._keyword_search(query, limit)

            scored = []
            columns = ['id', 'content', 'category', 'tags', 'embedding_json', 'created_at', 'accessed_at', 'access_count']
            for row in rows:
                try:
                    vec = json.loads(row['embedding_json'])
                    score = _cosine_sim(query_vec, vec)
                    scored.append((score, row))
                except Exception:
                    continue

            scored.sort(key=lambda x: x[0], reverse=True)
            top = scored[:limit]

            results = []
            ids_to_update = []
            now = time.time()
            for _, row in top:
                d = {c: row[c] for c in row.keys()}
                try:
                    d['tags'] = json.loads(d.get('tags') or '[]')
                except Exception:
                    d['tags'] = []
                d.pop('embedding_json', None)
                results.append(d)
                ids_to_update.append(d['id'])

            if ids_to_update:
                with sqlite3.connect(self.db_path) as conn:
                    conn.executemany(
                        "UPDATE memories SET accessed_at=?, access_count=access_count+1 WHERE id=?",
                        [(now, mid) for mid in ids_to_update]
                    )
                    conn.commit()
            return results
        except Exception as e:
            print(f"[MEMORY] _semantic_search error: {e}")
            return []

    def _keyword_search(self, query, limit):
        try:
            q = query.lower()
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, content, category, tags, created_at, accessed_at, access_count FROM memories ORDER BY accessed_at DESC"
                ).fetchall()
            columns = ['id', 'content', 'category', 'tags', 'created_at', 'accessed_at', 'access_count']

            results = []
            ids_to_update = []
            now = time.time()
            for row in rows:
                content_lower = (row[1] or '').lower()
                tags_lower = (row[3] or '').lower()
                if q in content_lower or q in tags_lower:
                    d = self._row_to_dict(row, columns)
                    results.append(d)
                    ids_to_update.append(d['id'])
                if len(results) >= limit:
                    break

            if ids_to_update:
                with sqlite3.connect(self.db_path) as conn:
                    conn.executemany(
                        "UPDATE memories SET accessed_at=?, access_count=access_count+1 WHERE id=?",
                        [(now, mid) for mid in ids_to_update]
                    )
                    conn.commit()
            return results
        except Exception as e:
            print(f"[MEMORY] _keyword_search error: {e}")
            return []

    def get_all_memories(self, limit=50):
        """Return all memories ordered by accessed_at DESC."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, content, category, tags, created_at, accessed_at, access_count FROM memories ORDER BY accessed_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            columns = ['id', 'content', 'category', 'tags', 'created_at', 'accessed_at', 'access_count']
            return [self._row_to_dict(row, columns) for row in rows]
        except Exception as e:
            print(f"[MEMORY] get_all_memories error: {e}")
            return []

    def delete_memory(self, memory_id):
        """Delete a memory by id. Returns True on success."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
                conn.commit()
            return cur.rowcount > 0
        except Exception as e:
            print(f"[MEMORY] delete_memory error: {e}")
            return False

    def clear_all_memories(self):
        """Delete all memories. Returns the count deleted."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.execute("DELETE FROM memories")
                conn.commit()
            return cur.rowcount
        except Exception as e:
            print(f"[MEMORY] clear_all_memories error: {e}")
            return 0


# Singleton instance — import as: from memory import memory
memory = MemoryEngine()
