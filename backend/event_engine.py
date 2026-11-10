from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Callable
import asyncio, sqlite3, json, uuid, time, os, threading
from datetime import datetime


@dataclass
class EventRule:
    id: str
    name: str
    trigger_type: str          # 'file_change' | 'schedule' | 'app_health' | 'manual'
    trigger_config: dict
    action_type: str           # 'notify' | 'run_tool' | 'run_mission'
    action_config: dict
    enabled: bool = True
    last_fired: Optional[str] = None
    fire_count: int = 0


class EventEngine:
    def __init__(self):
        db_path = os.path.join(os.path.dirname(__file__), 'events.db')
        self._db_path = db_path
        self._lock = threading.Lock()
        self.on_event: Optional[Callable] = None
        self._running = False
        self._watcher_thread = None
        self._scheduler_thread = None
        self._init_db()

    # ------------------------------------------------------------------ db --

    def _get_conn(self):
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS event_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    trigger_type TEXT,
                    trigger_config JSON,
                    action_type TEXT,
                    action_config JSON,
                    enabled INTEGER,
                    last_fired TEXT,
                    fire_count INTEGER
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS event_history (
                    id TEXT PRIMARY KEY,
                    rule_id TEXT,
                    rule_name TEXT,
                    fired_at TEXT,
                    action_type TEXT,
                    action_config JSON,
                    outcome TEXT
                )
            """)
            conn.commit()

    def _save_rule(self, rule: EventRule):
        with self._lock:
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT OR REPLACE INTO event_rules
                        (id, name, trigger_type, trigger_config, action_type, action_config, enabled, last_fired, fire_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    rule.id, rule.name, rule.trigger_type,
                    json.dumps(rule.trigger_config), rule.action_type,
                    json.dumps(rule.action_config), 1 if rule.enabled else 0,
                    rule.last_fired, rule.fire_count
                ))
                conn.commit()

    def _load_rule(self, rule_id: str) -> Optional[EventRule]:
        with self._lock:
            with self._get_conn() as conn:
                row = conn.execute("SELECT * FROM event_rules WHERE id = ?", (rule_id,)).fetchone()
        if not row:
            return None
        return EventRule(
            id=row['id'], name=row['name'],
            trigger_type=row['trigger_type'],
            trigger_config=json.loads(row['trigger_config'] or '{}'),
            action_type=row['action_type'],
            action_config=json.loads(row['action_config'] or '{}'),
            enabled=bool(row['enabled']),
            last_fired=row['last_fired'],
            fire_count=row['fire_count'] or 0
        )

    # --------------------------------------------------------------- CRUD --

    def add_rule(self, rule_data: dict) -> str:
        rule_id = str(uuid.uuid4())
        rule = EventRule(
            id=rule_id,
            name=rule_data.get('rule_name') or rule_data.get('name', 'Unnamed Rule'),
            trigger_type=rule_data.get('trigger_type', 'manual'),
            trigger_config=self._parse_json_field(rule_data.get('trigger_config', '{}')),
            action_type=rule_data.get('action_type', 'notify'),
            action_config=self._parse_json_field(rule_data.get('action_config', '{}')),
            enabled=bool(rule_data.get('enabled', True)),
        )
        self._save_rule(rule)
        return rule_id

    def remove_rule(self, rule_id: str):
        if not rule_id:
            return
        with self._lock:
            with self._get_conn() as conn:
                conn.execute("DELETE FROM event_rules WHERE id = ?", (rule_id,))
                conn.commit()

    def enable_rule(self, rule_id: str):
        rule = self._load_rule(rule_id)
        if rule:
            rule.enabled = True
            self._save_rule(rule)

    def disable_rule(self, rule_id: str):
        rule = self._load_rule(rule_id)
        if rule:
            rule.enabled = False
            self._save_rule(rule)

    def list_rules(self) -> list:
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute("SELECT * FROM event_rules ORDER BY rowid ASC").fetchall()
        result = []
        for row in rows:
            result.append({
                'id': row['id'],
                'name': row['name'],
                'trigger_type': row['trigger_type'],
                'trigger_config': json.loads(row['trigger_config'] or '{}'),
                'action_type': row['action_type'],
                'action_config': json.loads(row['action_config'] or '{}'),
                'enabled': bool(row['enabled']),
                'last_fired': row['last_fired'],
                'fire_count': row['fire_count'] or 0,
            })
        return result

    def get_history(self, limit: int = 20) -> list:
        with self._lock:
            with self._get_conn() as conn:
                rows = conn.execute(
                    "SELECT * FROM event_history ORDER BY fired_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]

    def fire_rule(self, rule_id: str):
        rule = self._load_rule(rule_id)
        if rule:
            self._do_fire(rule, 'manual')

    def _do_fire(self, rule: EventRule, trigger_source: str):
        now_str = datetime.utcnow().isoformat()
        rule.last_fired = now_str
        rule.fire_count += 1
        self._save_rule(rule)

        history_id = str(uuid.uuid4())
        rule_info = {
            'id': history_id,
            'rule_id': rule.id,
            'rule_name': rule.name,
            'fired_at': now_str,
            'action_type': rule.action_type,
            'action_config': rule.action_config,
            'outcome': f'fired by {trigger_source}',
            'trigger_type': rule.trigger_type,
        }

        with self._lock:
            with self._get_conn() as conn:
                conn.execute("""
                    INSERT INTO event_history (id, rule_id, rule_name, fired_at, action_type, action_config, outcome)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    history_id, rule.id, rule.name, now_str, rule.action_type,
                    json.dumps(rule.action_config), f'fired by {trigger_source}'
                ))
                conn.commit()

        if self.on_event:
            try:
                self.on_event(rule_info)
            except Exception as e:
                print(f"[EVENTS] on_event callback error: {e}")

    # --------------------------------------------------------- scheduler --

    def _run_scheduler(self):
        """Runs in a background daemon thread; polls every 30 s."""
        while self._running:
            try:
                rules = self.list_rules()
                now = datetime.now()
                now_hhmm = now.strftime('%H:%M')
                now_date = now.strftime('%Y-%m-%d')

                for rd in rules:
                    if not rd['enabled']:
                        continue

                    # ---- app_health checks run inside the same loop ----
                    if rd['trigger_type'] == 'app_health':
                        self._check_app_health(rd)
                        continue

                    if rd['trigger_type'] != 'schedule':
                        continue

                    cfg = rd['trigger_config']
                    last_fired = rd['last_fired']

                    # interval_minutes trigger
                    interval = cfg.get('interval_minutes')
                    if interval is not None:
                        try:
                            interval = int(interval)
                        except (TypeError, ValueError):
                            interval = None
                    if interval:
                        if last_fired is None:
                            self.fire_rule(rd['id'])
                        else:
                            try:
                                last_dt = datetime.fromisoformat(last_fired)
                                elapsed = (datetime.utcnow() - last_dt).total_seconds()
                                if elapsed >= interval * 60:
                                    self.fire_rule(rd['id'])
                            except Exception:
                                pass
                        continue

                    # daily_at trigger  (e.g. "08:00")
                    daily_at = cfg.get('daily_at')
                    if daily_at:
                        if now_hhmm == daily_at:
                            already_today = False
                            if last_fired:
                                try:
                                    last_dt = datetime.fromisoformat(last_fired)
                                    already_today = last_dt.strftime('%Y-%m-%d') == now_date
                                except Exception:
                                    pass
                            if not already_today:
                                self.fire_rule(rd['id'])

            except Exception as e:
                print(f"[EVENTS] Scheduler error: {e}")

            time.sleep(30)

    def _check_app_health(self, rd: dict):
        """Check a single app_health rule. Called from scheduler thread."""
        try:
            import psutil
        except ImportError:
            return
        try:
            cfg = rd['trigger_config']
            process_name = cfg.get('process_name', '')
            condition = cfg.get('condition', 'running')  # 'running' | 'not_running'
            if not process_name:
                return

            names = [p.info['name'] for p in psutil.process_iter(['name']) if p.info['name']]
            is_running = any(process_name.lower() in n.lower() for n in names)

            condition_met = (condition == 'running' and is_running) or \
                            (condition == 'not_running' and not is_running)

            if not condition_met:
                return

            last_fired = rd['last_fired']
            if last_fired:
                try:
                    elapsed = (datetime.utcnow() - datetime.fromisoformat(last_fired)).total_seconds()
                    if elapsed < 300:
                        return
                except Exception:
                    pass

            self.fire_rule(rd['id'])
        except Exception as e:
            print(f"[EVENTS] App health check error: {e}")

    # -------------------------------------------------------- file watcher --

    def _run_file_watcher(self):
        """Starts watchdog observers for all enabled file_change rules."""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            print("[EVENTS] watchdog not installed; file_change triggers disabled.")
            return

        class _RuleHandler(FileSystemEventHandler):
            def __init__(self, engine, rule_id):
                super().__init__()
                self._engine = engine
                self._rule_id = rule_id

            def on_any_event(self, event):
                if event.is_directory:
                    return
                try:
                    rule = self._engine._load_rule(self._rule_id)
                    if rule and rule.enabled:
                        self._engine._do_fire(rule, 'file_change')
                except Exception as e:
                    print(f"[EVENTS] File watcher fire error: {e}")

        try:
            observer = Observer()
            rules = self.list_rules()
            watched = set()
            for rd in rules:
                if not rd['enabled'] or rd['trigger_type'] != 'file_change':
                    continue
                folder = rd['trigger_config'].get('folder_path', '')
                if not folder or not os.path.isdir(folder):
                    continue
                folder = os.path.abspath(folder)
                if folder not in watched:
                    # One handler per rule (same folder can have multiple rules)
                    pass
                handler = _RuleHandler(self, rd['id'])
                observer.schedule(handler, folder, recursive=True)
                watched.add(folder)
                print(f"[EVENTS] Watching folder: {folder} for rule '{rd['name']}'")

            if watched:
                observer.start()
                try:
                    while self._running:
                        time.sleep(5)
                finally:
                    observer.stop()
                    observer.join()
        except Exception as e:
            print(f"[EVENTS] File watcher error: {e}")

    # ---------------------------------------------------------------- lifecycle --

    def start(self):
        self._running = True
        self._scheduler_thread = threading.Thread(target=self._run_scheduler, daemon=True, name='EventScheduler')
        self._scheduler_thread.start()
        self._watcher_thread = threading.Thread(target=self._run_file_watcher, daemon=True, name='EventFileWatcher')
        self._watcher_thread.start()
        print("[EVENTS] Event engine started.")

    def stop(self):
        self._running = False
        print("[EVENTS] Event engine stopped.")

    # ---------------------------------------------------------------- helpers --

    @staticmethod
    def _parse_json_field(value) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                return json.loads(value)
            except Exception:
                return {}
        return {}
