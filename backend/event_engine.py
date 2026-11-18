"""
Event-Driven Automation for Nexora.

Triggers
  schedule     {"interval_minutes": 30} or {"daily_at": "08:00"} (local time)
  file_change  {"folder_path": "...", "pattern": "*.pdf", "debounce_s": 3}
  app_health   {"process_name": "chrome", "condition": "running" | "not_running", "cooldown_minutes": 5}
  condition    {"metric": "cpu_percent|ram_percent|disk_percent|battery_percent|process_count",
                "op": ">", "value": 90, "cooldown_minutes": 10}
  manual       fired from the UI / voice only

Actions
  notify       {"message": "..."}                      runs without approval
  run_tool     {"tool_name": "...", "args": {...}}     needs the user's approval
  run_mission  {"title": "...", "subtasks": [...]}     needs the user's approval

A rule whose action does something (run_tool / run_mission) is stored with
approved = 0 and is NOT executed when it fires until the user approves it. Every
firing is written to event_history with the real outcome (ok / blocked / error).
"""
from dataclasses import dataclass
from typing import Optional, Callable
import asyncio, sqlite3, json, uuid, time, os, threading, fnmatch, operator
from datetime import datetime

TRIGGERS = ('schedule', 'file_change', 'app_health', 'condition', 'manual')
ACTIONS = ('notify', 'run_tool', 'run_mission')
NEEDS_APPROVAL = ('run_tool', 'run_mission')
METRICS = ('cpu_percent', 'ram_percent', 'disk_percent', 'battery_percent', 'process_count')
_OPS = {'>': operator.gt, '>=': operator.ge, '<': operator.lt, '<=': operator.le, '==': operator.eq}


@dataclass
class EventRule:
    id: str
    name: str
    trigger_type: str
    trigger_config: dict
    action_type: str
    action_config: dict
    enabled: bool = True
    last_fired: Optional[str] = None
    fire_count: int = 0
    approved: bool = False


def read_metric(metric: str, snap: dict):
    if metric == 'cpu_percent':
        return snap['cpu']['percent']
    if metric == 'ram_percent':
        return snap['ram']['percent']
    if metric == 'disk_percent':
        return snap['disk']['percent']
    if metric == 'battery_percent':
        return snap['battery']['percent'] if snap.get('battery') else None
    if metric == 'process_count':
        return snap['process_count']
    raise ValueError(f"unknown metric {metric}")


def evaluate_condition(cfg: dict, snap: dict) -> (bool, str):
    """Pure check of a condition trigger against a system snapshot. Returns (met, explanation)."""
    metric, op, value = cfg.get('metric'), cfg.get('op', '>'), cfg.get('value')
    if metric not in METRICS or op not in _OPS or value is None:
        return False, "invalid condition config"
    cur = read_metric(metric, snap)
    if cur is None:
        return False, f"{metric} not available on this machine"
    met = _OPS[op](float(cur), float(value))
    return met, f"{metric}={cur} {op} {value} -> {met}"


class EventEngine:
    def __init__(self, db_path: str = None, snapshot_fn: Callable = None, process_names_fn: Callable = None):
        self._db_path = db_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'events.db')
        self._lock = threading.Lock()
        self.on_event: Optional[Callable] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._running = False
        self._threads = []
        self._last_file_fire = {}
        self._snapshot_fn = snapshot_fn
        self._process_names_fn = process_names_fn
        self._init_db()

    # ------------------------------------------------------------------ db --
    def _get_conn(self):
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS event_rules (
                id TEXT PRIMARY KEY, name TEXT, trigger_type TEXT, trigger_config JSON,
                action_type TEXT, action_config JSON, enabled INTEGER, last_fired TEXT, fire_count INTEGER)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS event_history (
                id TEXT PRIMARY KEY, rule_id TEXT, rule_name TEXT, fired_at TEXT,
                action_type TEXT, action_config JSON, outcome TEXT)""")
            # --- migrations (v2): approval flag, structured outcome ---
            rule_cols = {r[1] for r in conn.execute("PRAGMA table_info(event_rules)")}
            if 'approved' not in rule_cols:
                conn.execute("ALTER TABLE event_rules ADD COLUMN approved INTEGER DEFAULT 0")
                conn.execute("UPDATE event_rules SET approved = 1 WHERE action_type = 'notify'")
            hist_cols = {r[1] for r in conn.execute("PRAGMA table_info(event_history)")}
            for col in ('status', 'trigger_source', 'detail'):
                if col not in hist_cols:
                    conn.execute(f"ALTER TABLE event_history ADD COLUMN {col} TEXT")

    def _save_rule(self, rule: EventRule):
        with self._lock, self._get_conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO event_rules
                (id, name, trigger_type, trigger_config, action_type, action_config, enabled, last_fired, fire_count, approved)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (rule.id, rule.name, rule.trigger_type, json.dumps(rule.trigger_config), rule.action_type,
                 json.dumps(rule.action_config), int(rule.enabled), rule.last_fired, rule.fire_count, int(rule.approved)))

    @staticmethod
    def _row_to_rule(row) -> EventRule:
        return EventRule(id=row['id'], name=row['name'], trigger_type=row['trigger_type'],
                         trigger_config=json.loads(row['trigger_config'] or '{}'), action_type=row['action_type'],
                         action_config=json.loads(row['action_config'] or '{}'), enabled=bool(row['enabled']),
                         last_fired=row['last_fired'], fire_count=row['fire_count'] or 0,
                         approved=bool(row['approved']))

    def _load_rule(self, rule_id: str) -> Optional[EventRule]:
        with self._lock, self._get_conn() as conn:
            row = conn.execute("SELECT * FROM event_rules WHERE id = ?", (rule_id,)).fetchone()
        return self._row_to_rule(row) if row else None

    # --------------------------------------------------------------- CRUD --
    def validate_rule(self, rule_data: dict) -> list:
        errs = []
        tt = rule_data.get('trigger_type', 'manual')
        at = rule_data.get('action_type', 'notify')
        tc = self._parse_json_field(rule_data.get('trigger_config', {}))
        ac = self._parse_json_field(rule_data.get('action_config', {}))
        if tt not in TRIGGERS:
            errs.append(f"trigger_type must be one of {TRIGGERS}")
        if at not in ACTIONS:
            errs.append(f"action_type must be one of {ACTIONS}")
        if tt == 'schedule' and not (tc.get('interval_minutes') or tc.get('daily_at')):
            errs.append("schedule needs interval_minutes or daily_at (HH:MM)")
        if tt == 'schedule' and tc.get('daily_at'):
            try:
                datetime.strptime(tc['daily_at'], '%H:%M')
            except (ValueError, TypeError):
                errs.append("daily_at must be HH:MM")
        if tt == 'file_change' and not os.path.isdir(str(tc.get('folder_path', ''))):
            errs.append(f"folder_path '{tc.get('folder_path')}' is not an existing folder")
        if tt == 'app_health' and not tc.get('process_name'):
            errs.append("app_health needs process_name")
        if tt == 'condition':
            if tc.get('metric') not in METRICS:
                errs.append(f"condition metric must be one of {METRICS}")
            if tc.get('op', '>') not in _OPS:
                errs.append(f"condition op must be one of {tuple(_OPS)}")
            try:
                float(tc.get('value'))
            except (TypeError, ValueError):
                errs.append("condition needs a numeric value")
        if at == 'notify' and not ac.get('message'):
            errs.append("notify needs action_config.message")
        if at == 'run_tool':
            import tool_runner
            name = ac.get('tool_name')
            if name in tool_runner.BLOCKED_IN_AUTOMATION:
                errs.append(f"{name} needs on-screen approval and cannot be automated")
            elif name not in tool_runner.REGISTRY:
                errs.append(f"unknown tool '{name}'")
        if at == 'run_mission':
            from mission_control import engine
            plan = engine.validate_plan(ac.get('subtasks') or [])
            errs += [f"mission: {e}" for e in plan['errors']]
        return errs

    def add_rule(self, rule_data: dict, approved: bool = False) -> str:
        """Validate and store a rule. Raises ValueError with all problems if invalid.
        notify rules are approved automatically; others only when approved=True (the user's own UI)."""
        errs = self.validate_rule(rule_data)
        if errs:
            raise ValueError("; ".join(errs))
        at = rule_data.get('action_type', 'notify')
        rule = EventRule(id=str(uuid.uuid4()),
                         name=rule_data.get('rule_name') or rule_data.get('name', 'Unnamed Rule'),
                         trigger_type=rule_data.get('trigger_type', 'manual'),
                         trigger_config=self._parse_json_field(rule_data.get('trigger_config', {})),
                         action_type=at, action_config=self._parse_json_field(rule_data.get('action_config', {})),
                         enabled=bool(rule_data.get('enabled', True)),
                         approved=(at not in NEEDS_APPROVAL) or bool(approved))
        self._save_rule(rule)
        return rule.id

    def remove_rule(self, rule_id: str):
        if not rule_id:
            return
        with self._lock, self._get_conn() as conn:
            conn.execute("DELETE FROM event_rules WHERE id = ?", (rule_id,))

    def _set_flag(self, rule_id, **kw):
        rule = self._load_rule(rule_id)
        if rule:
            for k, v in kw.items():
                setattr(rule, k, v)
            self._save_rule(rule)
        return rule is not None

    def enable_rule(self, rule_id):
        return self._set_flag(rule_id, enabled=True)

    def disable_rule(self, rule_id):
        return self._set_flag(rule_id, enabled=False)

    def approve_rule(self, rule_id):
        return self._set_flag(rule_id, approved=True)

    def revoke_rule(self, rule_id):
        return self._set_flag(rule_id, approved=False)

    def list_rules(self) -> list:
        with self._lock, self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM event_rules ORDER BY rowid ASC").fetchall()
        out = []
        for r in rows:
            rule = self._row_to_rule(r)
            d = rule.__dict__.copy()
            d['needs_approval'] = rule.action_type in NEEDS_APPROVAL and not rule.approved
            out.append(d)
        return out

    def get_history(self, limit: int = 20) -> list:
        with self._lock, self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM event_history ORDER BY fired_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ firing --
    def fire_rule(self, rule_id: str, trigger_source: str = 'manual') -> Optional[dict]:
        rule = self._load_rule(rule_id)
        return self._do_fire(rule, trigger_source) if rule else None

    def _run_coro(self, coro, timeout: float):
        """Run an async action from a worker thread on the server loop (or a private loop in tests)."""
        loop = self._loop
        if loop and loop.is_running():
            try:
                if asyncio.get_running_loop() is loop:
                    raise RuntimeError("_do_fire must not be called on the event loop thread; use asyncio.to_thread")
            except RuntimeError as e:
                if "must not" in str(e):
                    raise
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
        return asyncio.run(asyncio.wait_for(coro, timeout))

    def _execute_action(self, rule: EventRule) -> (str, str):
        """Returns (status, detail). status: ok | blocked | error."""
        if rule.action_type in NEEDS_APPROVAL and not rule.approved:
            return 'blocked', 'rule is waiting for user approval; action not executed'
        try:
            if rule.action_type == 'notify':
                import tool_runner
                res = self._run_coro(tool_runner.run_tool('notify', {'title': rule.name, 'message': rule.action_config.get('message', rule.name)}, source='event'), 15)
                return 'ok', json.dumps(res['output'], default=str)[:400]
            if rule.action_type == 'run_tool':
                import tool_runner
                res = self._run_coro(tool_runner.run_tool(rule.action_config.get('tool_name'), rule.action_config.get('args') or {}, source='event'), 120)
                return 'ok', json.dumps(res['output'], default=str)[:400]
            if rule.action_type == 'run_mission':
                from mission_control import engine
                mid = engine.create_mission(rule.action_config.get('title') or rule.name,
                                            f"Started by automation rule '{rule.name}'",
                                            rule.action_config.get('subtasks') or [])
                if self._loop and self._loop.is_running():
                    async def _start():
                        engine.start(mid)          # runs in the background on the server loop
                    self._run_coro(_start(), 10)
                    return 'ok', f'mission {mid} started'
                self._run_coro(engine.execute_mission(mid), 300)   # no server loop: run to completion
                return 'ok', f"mission {mid} finished: {(engine.get_mission(mid) or {}).get('status')}"
            return 'error', f'unknown action {rule.action_type}'
        except Exception as e:
            return 'error', f'{type(e).__name__}: {e}'[:400]

    def _do_fire(self, rule: EventRule, trigger_source: str, detail_prefix: str = '') -> dict:
        now_str = datetime.now().isoformat(timespec='seconds')
        rule.last_fired = now_str
        rule.fire_count += 1
        self._save_rule(rule)
        status, detail = self._execute_action(rule)
        if detail_prefix:
            detail = f"{detail_prefix} | {detail}"
        info = {'id': str(uuid.uuid4()), 'rule_id': rule.id, 'rule_name': rule.name, 'fired_at': now_str,
                'action_type': rule.action_type, 'action_config': rule.action_config,
                'outcome': f'{status}: {detail}'[:500], 'status': status, 'trigger_source': trigger_source,
                'detail': detail, 'trigger_type': rule.trigger_type}
        with self._lock, self._get_conn() as conn:
            conn.execute("""INSERT INTO event_history (id, rule_id, rule_name, fired_at, action_type, action_config,
                            outcome, status, trigger_source, detail) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                         (info['id'], rule.id, rule.name, now_str, rule.action_type, json.dumps(rule.action_config),
                          info['outcome'], status, trigger_source, detail))
        print(f"[EVENTS] '{rule.name}' fired by {trigger_source}: {status}")
        if self.on_event:
            try:
                self.on_event(info)
            except Exception as e:
                print(f"[EVENTS] on_event callback error: {e}")
        return info

    # ------------------------------------------------- trigger evaluation --
    @staticmethod
    def _since_last(rule: dict, now: datetime) -> Optional[float]:
        if not rule['last_fired']:
            return None
        try:
            return (now - datetime.fromisoformat(rule['last_fired'])).total_seconds()
        except ValueError:
            return None

    def due_rules(self, now: datetime = None, snapshot: dict = None, process_names: list = None) -> list:
        """Which enabled rules should fire right now, with the reason. Pure: does not fire anything.
        Used by the scheduler and by the Digital Twin's automation dry run."""
        now = now or datetime.now()
        due = []
        for rd in self.list_rules():
            if not rd['enabled']:
                continue
            cfg, tt = rd['trigger_config'], rd['trigger_type']
            since = self._since_last(rd, now)
            if tt == 'schedule':
                iv = cfg.get('interval_minutes')
                if iv:
                    if since is None or since >= float(iv) * 60:
                        due.append((rd, f"interval {iv} min elapsed"))
                elif cfg.get('daily_at') and now.strftime('%H:%M') >= cfg['daily_at']:
                    last_day = rd['last_fired'][:10] if rd['last_fired'] else None
                    if last_day != now.strftime('%Y-%m-%d'):
                        due.append((rd, f"daily at {cfg['daily_at']}"))
            elif tt == 'app_health':
                if process_names is None:
                    process_names = self._process_names()
                name = cfg.get('process_name', '').lower()
                running = any(name in n.lower() for n in process_names)
                want = cfg.get('condition', 'running')
                cooldown = float(cfg.get('cooldown_minutes', 5)) * 60
                if ((want == 'running') == running) and (since is None or since >= cooldown):
                    due.append((rd, f"{cfg.get('process_name')} is {'running' if running else 'not running'}"))
            elif tt == 'condition':
                if snapshot is None:
                    snapshot = self._snapshot()
                met, why = evaluate_condition(cfg, snapshot)
                cooldown = float(cfg.get('cooldown_minutes', 10)) * 60
                if met and (since is None or since >= cooldown):
                    due.append((rd, why))
        return due

    def _snapshot(self):
        if self._snapshot_fn:
            return self._snapshot_fn()
        import system_monitor
        return system_monitor.snapshot()

    def _process_names(self):
        if self._process_names_fn:
            return self._process_names_fn()
        import psutil
        return [p.info['name'] for p in psutil.process_iter(['name']) if p.info['name']]

    def tick(self, now: datetime = None):
        """One scheduler pass: fire every due rule."""
        fired = []
        for rd, why in self.due_rules(now):
            rule = self._load_rule(rd['id'])
            if rule:
                fired.append(self._do_fire(rule, rd['trigger_type'], why))
        return fired

    def _run_scheduler(self):
        while self._running:
            try:
                self.tick()
            except Exception as e:
                print(f"[EVENTS] Scheduler error: {e}")
            for _ in range(30):
                if not self._running:
                    break
                time.sleep(1)

    # -------------------------------------------------------- file watcher --
    def handle_file_event(self, rule_id: str, path: str, now: float = None) -> Optional[dict]:
        """Filter + debounce one filesystem event, then fire. Returns the history entry if fired."""
        rule = self._load_rule(rule_id)
        if not rule or not rule.enabled:
            return None
        pat = rule.trigger_config.get('pattern')
        if pat and not fnmatch.fnmatch(os.path.basename(path), pat):
            return None
        now = now if now is not None else time.time()
        debounce = float(rule.trigger_config.get('debounce_s', 3))
        if now - self._last_file_fire.get(rule_id, 0) < debounce:
            return None
        self._last_file_fire[rule_id] = now
        return self._do_fire(rule, 'file_change', f"changed: {path}")

    def _watch_signature(self):
        return tuple(sorted((r['id'], r['trigger_config'].get('folder_path', ''), r['enabled'])
                            for r in self.list_rules() if r['trigger_type'] == 'file_change'))

    def _run_file_watcher(self):
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            print("[EVENTS] watchdog not installed; file_change triggers disabled.")
            return
        engine = self

        class _Handler(FileSystemEventHandler):
            def __init__(self, rule_id):
                super().__init__()
                self.rule_id = rule_id

            def on_any_event(self, event):
                if event.is_directory:
                    return
                try:
                    engine.handle_file_event(self.rule_id, event.src_path)
                except Exception as e:
                    print(f"[EVENTS] File watcher fire error: {e}")

        observer, sig = None, None
        while self._running:
            try:
                new_sig = self._watch_signature()
                if new_sig != sig:            # rules changed: rebuild the watches
                    if observer:
                        observer.stop()
                        observer.join(5)
                    observer, sig = Observer(), new_sig
                    for rid, folder, enabled in new_sig:
                        if enabled and folder and os.path.isdir(folder):
                            observer.schedule(_Handler(rid), os.path.abspath(folder), recursive=True)
                            print(f"[EVENTS] Watching {folder}")
                    observer.start()
            except Exception as e:
                print(f"[EVENTS] File watcher error: {e}")
            time.sleep(3)
        if observer:
            observer.stop()

    # ------------------------------------------------------------ lifecycle --
    def start(self, loop: asyncio.AbstractEventLoop = None):
        self._loop = loop
        self._running = True
        for target, name in ((self._run_scheduler, 'EventScheduler'), (self._run_file_watcher, 'EventFileWatcher')):
            t = threading.Thread(target=target, daemon=True, name=name)
            t.start()
            self._threads.append(t)
        print("[EVENTS] Event engine started.")

    def stop(self):
        self._running = False

    @staticmethod
    def _parse_json_field(value) -> dict:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                v = json.loads(value) if value.strip() else {}
                return v if isinstance(v, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}
