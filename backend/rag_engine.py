"""
RAG (Retrieval-Augmented Generation) engine for Nexora.
Indexes uploaded documents and provides semantic/keyword search over them.
"""
import os
import json
import sqlite3
import time

# Try to reuse or create an embedder with the same fallback pattern as memory.py
try:
    from sentence_transformers import SentenceTransformer
    try:  # cached copy first, so startup doesn't stall on network retries
        _embedder = SentenceTransformer('all-MiniLM-L6-v2', local_files_only=True)
    except Exception:
        _embedder = SentenceTransformer('all-MiniLM-L6-v2')
    _has_embeddings = True
    print("[RAG] Sentence-transformers loaded — semantic search enabled.")
except Exception:
    _embedder = None
    _has_embeddings = False
    print("[RAG] sentence-transformers not available — keyword search only.")


def _cosine_sim(a, b):
    import math
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


class RAGEngine:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'nexora_knowledge.db')
        self._embedder = _embedder
        self._has_embeddings = _has_embeddings
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS knowledge_base (
                        id              INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename        TEXT,
                        chunk_index     INTEGER,
                        content         TEXT,
                        embedding_json  TEXT,
                        created_at      REAL
                    )
                """)
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS documents (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        filename    TEXT UNIQUE,
                        file_type   TEXT,
                        chunk_count INTEGER,
                        created_at  REAL
                    )
                """)
                conn.commit()
        except Exception as e:
            print(f"[RAG] DB init error: {e}")

    def _extract_text(self, content_bytes, file_type):
        """Extract plain text from various file types."""
        ft = file_type.lower().strip('.')
        if ft == 'pdf':
            try:
                import fitz  # PyMuPDF
                doc = fitz.open(stream=content_bytes, filetype="pdf")
                pages = []
                for page in doc:
                    pages.append(page.get_text())
                doc.close()
                return "\n".join(pages), None
            except ImportError:
                return None, "PyMuPDF not installed"
            except Exception as e:
                return None, f"PDF parse error: {e}"

        if ft == 'docx':
            try:
                import io
                from docx import Document
                doc = Document(io.BytesIO(content_bytes))
                text = "\n".join(para.text for para in doc.paragraphs)
                return text, None
            except ImportError:
                return None, "python-docx not installed"
            except Exception as e:
                return None, f"DOCX parse error: {e}"

        if ft in ('txt', 'md'):
            try:
                return content_bytes.decode('utf-8', errors='replace'), None
            except Exception as e:
                return None, f"Text decode error: {e}"

        # Excel, PowerPoint, CSV and code files: real extraction instead of decoding the raw bytes.
        try:
            import doc_intel
            if ft in doc_intel.SUPPORTED:
                text, _meta = doc_intel.extract_bytes(content_bytes, ft)
                return text, None
        except ImportError as e:
            return None, f"Missing library for .{ft}: {e}"
        except Exception as e:
            return None, f"{ft.upper()} parse error: {e}"

        # Fallback: try UTF-8 decode
        try:
            return content_bytes.decode('utf-8', errors='replace'), None
        except Exception as e:
            return None, f"Unknown file type and decode failed: {e}"

    def _chunk_text(self, text, length=500, step=400):
        """Sliding window chunking. length=500, overlap=100 (step=400)."""
        chunks = []
        start = 0
        while start < len(text):
            chunk = text[start:start + length]
            if chunk.strip():
                chunks.append(chunk)
            if start + length >= len(text):
                break
            start += step
        return chunks

    def ingest_file(self, filename, content_bytes, file_type):
        """Parse file, chunk text, store in DB. Returns {"ok": True, "chunks": N} or {"error": ...}."""
        try:
            text, err = self._extract_text(content_bytes, file_type)
            if err:
                return {"error": err}
            if not text or not text.strip():
                return {"error": "No text content found in file."}

            chunks = self._chunk_text(text)
            if not chunks:
                return {"error": "Could not extract any text chunks from file."}

            now = time.time()
            # Delete existing entries for this filename
            try:
                with sqlite3.connect(self.db_path) as conn:
                    conn.execute("DELETE FROM knowledge_base WHERE filename=?", (filename,))
                    conn.execute("DELETE FROM documents WHERE filename=?", (filename,))
                    conn.commit()
            except Exception as e:
                print(f"[RAG] Pre-delete error: {e}")

            # Build embeddings and insert chunks
            rows = []
            for i, chunk in enumerate(chunks):
                embedding_json = None
                if self._has_embeddings and self._embedder is not None:
                    try:
                        vec = self._embedder.encode(chunk).tolist()
                        embedding_json = json.dumps(vec)
                    except Exception as e:
                        print(f"[RAG] Embedding error chunk {i}: {e}")
                rows.append((filename, i, chunk, embedding_json, now))

            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(
                    "INSERT INTO knowledge_base (filename, chunk_index, content, embedding_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    rows
                )
                conn.execute(
                    "INSERT OR REPLACE INTO documents (filename, file_type, chunk_count, created_at) VALUES (?, ?, ?, ?)",
                    (filename, file_type.lower().strip('.'), len(chunks), now)
                )
                conn.commit()

            graph = {"entities": 0, "relations": 0}
            try:  # graph facts point back at the chunk (doc:<file>#<i>); the text stays only in this DB
                from knowledge_graph import get_graph
                g = get_graph()
                g.remove_source(f"doc:{filename}")
                for i, chunk in enumerate(chunks):
                    r = g.extract_and_add(chunk, f"doc:{filename}#{i}")
                    graph["entities"] += len(r["entities"])
                    graph["relations"] += len(r["relations"])
            except Exception as e:
                print(f"[RAG] knowledge graph update failed: {e}")

            return {"ok": True, "chunks": len(chunks), "graph": graph}
        except Exception as e:
            print(f"[RAG] ingest_file error: {e}")
            return {"error": str(e)}

    def search_knowledge(self, query, limit=5):
        """Search indexed documents. Returns list of dicts: filename, chunk_index, content, score."""
        try:
            if self._has_embeddings and self._embedder is not None:
                return self._semantic_search(query, limit)
            return self._keyword_search(query, limit)
        except Exception as e:
            print(f"[RAG] search_knowledge error: {e}")
            return []

    def _semantic_search(self, query, limit):
        try:
            query_vec = self._embedder.encode(query).tolist()
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT filename, chunk_index, content, embedding_json FROM knowledge_base WHERE embedding_json IS NOT NULL"
                ).fetchall()
            if not rows:
                return self._keyword_search(query, limit)

            scored = []
            for filename, chunk_index, content, embedding_json in rows:
                try:
                    vec = json.loads(embedding_json)
                    score = _cosine_sim(query_vec, vec)
                    scored.append({"filename": filename, "chunk_index": chunk_index, "content": content, "score": score})
                except Exception:
                    continue

            scored.sort(key=lambda x: x["score"], reverse=True)
            return scored[:limit]
        except Exception as e:
            print(f"[RAG] _semantic_search error: {e}")
            return []

    def _keyword_search(self, query, limit):
        try:
            q = query.lower()
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT filename, chunk_index, content FROM knowledge_base"
                ).fetchall()
            results = []
            for filename, chunk_index, content in rows:
                if q in (content or '').lower():
                    results.append({"filename": filename, "chunk_index": chunk_index, "content": content, "score": 1.0})
                if len(results) >= limit:
                    break
            return results
        except Exception as e:
            print(f"[RAG] _keyword_search error: {e}")
            return []

    def list_documents(self):
        """Returns list of dicts from the documents table."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, filename, file_type, chunk_count, created_at FROM documents ORDER BY created_at DESC"
                ).fetchall()
            return [
                {"id": r[0], "filename": r[1], "file_type": r[2], "chunk_count": r[3], "created_at": r[4]}
                for r in rows
            ]
        except Exception as e:
            print(f"[RAG] list_documents error: {e}")
            return []

    def delete_document(self, filename):
        """Deletes from both tables. Returns True on success."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("DELETE FROM knowledge_base WHERE filename=?", (filename,))
                cur = conn.execute("DELETE FROM documents WHERE filename=?", (filename,))
                conn.commit()
            try:
                from knowledge_graph import get_graph
                get_graph().remove_source(f"doc:{filename}")
            except Exception as e:
                print(f"[RAG] knowledge graph cleanup failed: {e}")
            return cur.rowcount > 0
        except Exception as e:
            print(f"[RAG] delete_document error: {e}")
            return False


# Singleton — import as: from rag_engine import rag
rag = RAGEngine()
