"""
Local tool-call telemetry for Nexora (opt-in).

Records one row per tool call: tool name, where it came from (voice / mission /
event / eval), success, latency and a short error. Nothing leaves this machine.

Collection is OFF until the user turns on the `usage_data_collection` setting.
Diagnostics (latency bottlenecks) and the predictor (usage patterns) read from
here and say "not enough data" when it is empty; they never invent numbers.
"""
import os
import sqlite3
import threading
import time
from statistics import median

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry.db")

_state = {"enabled": False, "db": DB_PATH}
_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(_state["db"], check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS tool_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    tool TEXT NOT NULL,
                    source TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    latency_ms REAL NOT NULL,
                    error TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS ix_tool_calls_ts ON tool_calls(ts)")
    return c


def set_enabled(on: bool):
    _state["enabled"] = bool(on)


def is_enabled() -> bool:
    return _state["enabled"]


def use_db(path: str):
    """Point telemetry at another database (tests / evaluation runs)."""
    _state["db"] = path


def record(tool: str, source: str, ok: bool, latency_ms: float, error: str = None, force: bool = False):
    """Store one call. `force` is used only by evaluation runs writing to their own DB."""
    if not (_state["enabled"] or force):
        return
    try:
        with _lock, _conn() as c:
            c.execute("INSERT INTO tool_calls (ts, tool, source, ok, latency_ms, error) VALUES (?,?,?,?,?,?)",
                      (time.time(), tool, source, 1 if ok else 0, float(latency_ms), (error or "")[:300] or None))
    except Exception as e:
        print(f"[TELEMETRY] record failed: {e}")


def calls(since_ts: float = 0, limit: int = 20000) -> list:
    try:
        with _lock, _conn() as c:
            rows = c.execute("SELECT ts, tool, source, ok, latency_ms, error FROM tool_calls WHERE ts >= ? "
                             "ORDER BY ts ASC LIMIT ?", (since_ts, limit)).fetchall()
        return [dict(zip(("ts", "tool", "source", "ok", "latency_ms", "error"), r)) for r in rows]
    except Exception as e:
        print(f"[TELEMETRY] read failed: {e}")
        return []


def latency_stats(since_ts: float = 0) -> dict:
    """Per-tool count, failure rate, median and p95 latency, computed from stored rows only."""
    by_tool = {}
    for r in calls(since_ts):
        by_tool.setdefault(r["tool"], []).append(r)
    out = {}
    for tool, rows in by_tool.items():
        lat = sorted(r["latency_ms"] for r in rows)
        p95 = lat[min(len(lat) - 1, int(round(0.95 * (len(lat) - 1))))]
        out[tool] = {"count": len(rows), "fail_rate": round(sum(1 for r in rows if not r["ok"]) / len(rows), 3),
                     "median_ms": round(median(lat), 1), "p95_ms": round(p95, 1)}
    return out


def clear():
    with _lock, _conn() as c:
        c.execute("DELETE FROM tool_calls")
