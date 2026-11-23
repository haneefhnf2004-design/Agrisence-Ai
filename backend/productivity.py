"""
Personal productivity: tasks (priority, project, due date), notes, reminders that really fire,
and a daily briefing built from real data. Stored locally in nexora_productivity.db.

Reminders: a daemon thread checks every 15 s and calls the notifier set by server.py
(socket event + desktop toast in the UI). Secrets are refused everywhere.
"""
import datetime as dt
import os
import re
import sqlite3
import threading
import time

import security

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nexora_productivity.db")
PRIORITIES = ("low", "normal", "high")


class Productivity:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._notifier = None
        self._thread = None
        self._stop = threading.Event()
        with self._db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, notes TEXT DEFAULT '',
                project TEXT DEFAULT '', priority TEXT DEFAULT 'normal', due TEXT, status TEXT DEFAULT 'open',
                created TEXT, completed TEXT);
            CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY, title TEXT, body TEXT NOT NULL, tags TEXT DEFAULT '',
                created TEXT, updated TEXT);
            CREATE TABLE IF NOT EXISTS reminders (id INTEGER PRIMARY KEY, text TEXT NOT NULL, at TEXT NOT NULL,
                fired INTEGER DEFAULT 0, cancelled INTEGER DEFAULT 0, created TEXT);
            """)

    def _db(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    # ------------------------------------------------------------ helpers --
    @staticmethod
    def _now():
        return dt.datetime.now().replace(microsecond=0)

    @staticmethod
    def parse_when(s, now=None):
        """ISO 'YYYY-MM-DD[ HH:MM]', 'HH:MM', 'today 17:00', 'tomorrow 9am', 'in 30 minutes', 'in 2 hours/days'."""
        if s is None or str(s).strip() == "":
            return None
        now = now or Productivity._now()
        t = str(s).strip().lower()
        m = re.fullmatch(r"in\s+(\d+)\s*(min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)", t)
        if m:
            n, u = int(m.group(1)), m.group(2)
            delta = dt.timedelta(minutes=n) if u.startswith("m") else dt.timedelta(hours=n) if u.startswith("h") \
                else dt.timedelta(days=n)
            return (now + delta).replace(second=0)
        def _clock(txt):
            mm = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", txt.strip())
            if not mm:
                return None
            h, mi, ap = int(mm.group(1)), int(mm.group(2) or 0), mm.group(3)
            if ap == "pm" and h < 12:
                h += 12
            if ap == "am" and h == 12:
                h = 0
            if h > 23 or mi > 59:
                return None
            return h, mi
        for word, days in (("today", 0), ("tonight", 0), ("tomorrow", 1)):
            if t.startswith(word):
                rest = t[len(word):].strip().removeprefix("at").strip()
                hm = _clock(rest) if rest else ((20, 0) if word == "tonight" else (9, 0))
                if hm is None:
                    raise ValueError(f"Could not understand the time in '{s}'.")
                return (now + dt.timedelta(days=days)).replace(hour=hm[0], minute=hm[1], second=0)
        hm = _clock(t)
        if hm:
            cand = now.replace(hour=hm[0], minute=hm[1], second=0)
            return cand if cand > now else cand + dt.timedelta(days=1)
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return dt.datetime.strptime(str(s).strip(), fmt)
            except ValueError:
                continue
        raise ValueError(f"Could not understand the date/time '{s}'. Use e.g. '2026-11-20 14:30', 'tomorrow 9am' or 'in 30 minutes'.")

    @staticmethod
    def _clean(text, field, maxlen=2000, required=True):
        if text is None or str(text).strip() == "":
            if required:
                raise ValueError(f"'{field}' is required.")
            return ""
        text = str(text).strip()[:maxlen]
        if security.contains_secret(text):
            raise ValueError("That looks like a password or key; Nexora does not store secrets.")
        return text

    # -------------------------------------------------------------- tasks --
    def add_task(self, title, due=None, priority="normal", project="", notes=""):
        title = self._clean(title, "title", 300)
        priority = (priority or "normal").lower()
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {PRIORITIES}")
        d = self.parse_when(due)
        with self._db() as c:
            cur = c.execute("INSERT INTO tasks (title, notes, project, priority, due, created) VALUES (?,?,?,?,?,?)",
                            (title, self._clean(notes, "notes", required=False), self._clean(project, "project", 100, False),
                             priority, d.isoformat(sep=" ") if d else None, self._now().isoformat(sep=" ")))
        return self.get_task(cur.lastrowid)

    def get_task(self, tid):
        with self._db() as c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (int(tid),)).fetchone()
        if not r:
            raise ValueError(f"No task with id {tid}.")
        return dict(r)

    def list_tasks(self, status="open", project=None, limit=50):
        q, p = "SELECT * FROM tasks WHERE 1=1", []
        if status in ("open", "done"):
            q += " AND status=?"
            p.append(status)
        if project:
            q += " AND lower(project)=lower(?)"
            p.append(project)
        q += (" ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, "
              "CASE WHEN due IS NULL THEN 1 ELSE 0 END, due, id LIMIT ?")
        p.append(max(1, min(int(limit or 50), 500)))
        with self._db() as c:
            rows = [dict(r) for r in c.execute(q, p)]
        now = self._now().isoformat(sep=" ")
        for r in rows:
            r["overdue"] = bool(r["due"] and r["status"] == "open" and r["due"] < now)
        return rows

    def complete_task(self, tid):
        self.get_task(tid)
        with self._db() as c:
            c.execute("UPDATE tasks SET status='done', completed=? WHERE id=?", (self._now().isoformat(sep=" "), int(tid)))
        return self.get_task(tid)

    def update_task(self, tid, **fields):
        self.get_task(tid)
        sets, vals = [], []
        for k in ("title", "notes", "project", "priority", "due"):
            if fields.get(k) is not None:
                v = fields[k]
                if k == "due":
                    d = self.parse_when(v)
                    v = d.isoformat(sep=" ") if d else None
                elif k == "priority":
                    if v not in PRIORITIES:
                        raise ValueError(f"priority must be one of {PRIORITIES}")
                else:
                    v = self._clean(v, k, required=(k == "title"))
                sets.append(f"{k}=?")
                vals.append(v)
        if not sets:
            raise ValueError("Nothing to update.")
        with self._db() as c:
            c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", (*vals, int(tid)))
        return self.get_task(tid)

    def delete_task(self, tid):
        t = self.get_task(tid)
        with self._db() as c:
            c.execute("DELETE FROM tasks WHERE id=?", (int(tid),))
        return {"deleted": t["title"]}

    # -------------------------------------------------------------- notes --
    def add_note(self, body, title="", tags=""):
        body = self._clean(body, "body", 20000)
        now = self._now().isoformat(sep=" ")
        with self._db() as c:
            cur = c.execute("INSERT INTO notes (title, body, tags, created, updated) VALUES (?,?,?,?,?)",
                            (self._clean(title, "title", 200, False), body, self._clean(tags, "tags", 200, False), now, now))
        return {"id": cur.lastrowid, "title": title, "created": now}

    def list_notes(self, query=None, limit=20):
        limit = max(1, min(int(limit or 20), 200))
        with self._db() as c:
            if query:
                like = f"%{query}%"
                rows = c.execute("SELECT * FROM notes WHERE title LIKE ? OR body LIKE ? OR tags LIKE ? ORDER BY updated DESC LIMIT ?",
                                 (like, like, like, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM notes ORDER BY updated DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def delete_note(self, nid):
        with self._db() as c:
            cur = c.execute("DELETE FROM notes WHERE id=?", (int(nid),))
        if not cur.rowcount:
            raise ValueError(f"No note with id {nid}.")
        return {"deleted": int(nid)}

    # ---------------------------------------------------------- reminders --
    def add_reminder(self, text, at):
        text = self._clean(text, "text", 500)
        when = self.parse_when(at)
        if when is None:
            raise ValueError("'at' (when to remind) is required.")
        if when < self._now() - dt.timedelta(minutes=1):
            raise ValueError(f"{when:%Y-%m-%d %H:%M} is in the past.")
        with self._db() as c:
            cur = c.execute("INSERT INTO reminders (text, at, created) VALUES (?,?,?)",
                            (text, when.isoformat(sep=" "), self._now().isoformat(sep=" ")))
        return {"id": cur.lastrowid, "text": text, "at": when.strftime("%Y-%m-%d %H:%M")}

    def list_reminders(self, include_done=False):
        q = "SELECT * FROM reminders" + ("" if include_done else " WHERE fired=0 AND cancelled=0") + " ORDER BY at LIMIT 100"
        with self._db() as c:
            return [dict(r) for r in c.execute(q)]

    def cancel_reminder(self, rid):
        with self._db() as c:
            cur = c.execute("UPDATE reminders SET cancelled=1 WHERE id=? AND fired=0", (int(rid),))
        if not cur.rowcount:
            raise ValueError(f"No pending reminder with id {rid}.")
        return {"cancelled": int(rid)}

    def due_reminders(self):
        """Mark and return reminders whose time has come (each fires once)."""
        now = self._now().isoformat(sep=" ")
        with self._db() as c:
            rows = [dict(r) for r in c.execute("SELECT * FROM reminders WHERE fired=0 AND cancelled=0 AND at<=?", (now,))]
            for r in rows:
                c.execute("UPDATE reminders SET fired=1 WHERE id=?", (r["id"],))
        return rows

    def start(self, notifier):
        """notifier(payload: dict) is called for each due reminder."""
        self._notifier = notifier
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(15):
                try:
                    for r in self.due_reminders():
                        if self._notifier:
                            self._notifier({"type": "reminder", "title": "Reminder", "message": r["text"],
                                            "id": r["id"], "at": r["at"]})
                except Exception as e:
                    print(f"[PRODUCTIVITY] reminder loop error: {e}")

        self._thread = threading.Thread(target=loop, daemon=True, name="Reminders")
        self._thread.start()

    def stop(self):
        self._stop.set()

    # ----------------------------------------------------------- briefing --
    def daily_briefing(self):
        now = self._now()
        today_end = now.replace(hour=23, minute=59, second=59).isoformat(sep=" ")
        open_tasks = self.list_tasks("open", limit=500)
        overdue = [t for t in open_tasks if t["overdue"]]
        due_today = [t for t in open_tasks if t["due"] and not t["overdue"] and t["due"] <= today_end]
        high = [t for t in open_tasks if t["priority"] == "high" and t not in overdue and t not in due_today]
        reminders_today = [r for r in self.list_reminders() if r["at"] <= today_end]
        with self._db() as c:
            done_yesterday = c.execute("SELECT COUNT(*) FROM tasks WHERE status='done' AND completed>=?",
                                       ((now - dt.timedelta(days=1)).isoformat(sep=" "),)).fetchone()[0]
        brief = {"date": now.strftime("%A %d %B %Y, %H:%M"),
                 "overdue": [{"id": t["id"], "title": t["title"], "due": t["due"]} for t in overdue],
                 "due_today": [{"id": t["id"], "title": t["title"], "due": t["due"]} for t in due_today],
                 "high_priority": [{"id": t["id"], "title": t["title"]} for t in high[:5]],
                 "reminders_today": [{"text": r["text"], "at": r["at"][11:16]} for r in reminders_today],
                 "open_tasks": len(open_tasks), "completed_last_24h": done_yesterday}
        alerts = []
        try:
            import psutil
            b = psutil.sensors_battery()
            if b and b.percent < 25 and not b.power_plugged:
                alerts.append(f"Battery is at {round(b.percent)}% and not charging.")
            for part in psutil.disk_partitions(all=False):
                try:
                    u = psutil.disk_usage(part.mountpoint)
                    if u.percent >= 90:
                        alerts.append(f"Drive {part.device} is {u.percent}% full.")
                except OSError:
                    continue
        except Exception:
            pass
        brief["system_alerts"] = alerts
        return brief


_instance = None


def get():
    global _instance
    if _instance is None:
        _instance = Productivity()
    return _instance
