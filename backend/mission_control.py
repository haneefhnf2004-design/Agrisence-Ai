"""
Mission Control Engine for Nexora.

A mission is a list of subtasks (each one a real tool call) with dependencies.
Decomposition of the user's goal into subtasks is done by the Gemini Live model
through the `create_mission` tool schema; this engine VALIDATES that plan and runs it.

What it does (all persisted to missions.db after every step = checkpoints):
  - validation: unknown tools, blocked tools, missing dependencies and cycles are
    rejected before anything runs
  - execution in dependency waves; within a wave, read-only (parallel_safe) steps run
    concurrently and side-effect steps run one at a time
  - shared context: a step's args may contain {{Step title}} or {{Step title.key}}
    which is replaced by that earlier step's output
  - retry with exponential back-off; ToolError (bad args / unknown / blocked) is not retried
  - per-step verification rules (contains, equals, min_items, path_exists, ...)
  - dependents of a failed step are marked 'skipped' with the reason
  - pause / resume (resumes from the last checkpoint, done steps are not re-run)
  - cancellation (also cancels the running asyncio task)
  - background execution + progress events through on_progress
  - final result with counts and whether every step was verified
"""
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any, Callable, Awaitable
import asyncio, sqlite3, json, uuid, time, os, re
from datetime import datetime

_TEMPLATE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
FINAL = ("completed", "failed", "cancelled")


@dataclass
class SubTask:
    id: str
    title: str
    tool_name: str
    args: dict
    status: str = 'pending'          # pending | running | done | failed | skipped
    depends_on: List[str] = field(default_factory=list)
    result: Any = None
    error: Optional[str] = None
    retries: int = 0
    max_retries: int = 2
    verify: Optional[dict] = None
    verified: Optional[bool] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    latency_ms: Optional[float] = None


@dataclass
class Mission:
    id: str
    title: str
    description: str
    status: str = 'pending'          # pending | running | paused | completed | failed | cancelled
    subtasks: List[SubTask] = field(default_factory=list)
    created_at: str = ''
    updated_at: str = ''
    result: Any = None


def _now():
    return datetime.now().isoformat(timespec='seconds')


def check_verify(rule: Optional[dict], output: Any) -> (bool, str):
    """Apply a verification rule to a step output. Returns (passed, reason)."""
    if not rule:
        return True, "no rule"
    text = output if isinstance(output, str) else json.dumps(output, default=str)
    val = output
    if rule.get("field") is not None:
        if not isinstance(output, dict) or rule["field"] not in output:
            return False, f"field '{rule['field']}' missing from output"
        val = output[rule["field"]]
        text = val if isinstance(val, str) else json.dumps(val, default=str)
    if "contains" in rule and str(rule["contains"]).lower() not in text.lower():
        return False, f"output does not contain '{rule['contains']}'"
    if "not_contains" in rule and str(rule["not_contains"]).lower() in text.lower():
        return False, f"output contains forbidden '{rule['not_contains']}'"
    if "equals" in rule and val != rule["equals"]:
        return False, f"expected {rule['equals']!r}, got {val!r}"
    if "min_items" in rule:
        n = len(val) if isinstance(val, (list, dict, str)) else 0
        if n < int(rule["min_items"]):
            return False, f"expected at least {rule['min_items']} items, got {n}"
    if "path_exists" in rule and not os.path.exists(str(rule["path_exists"])):
        return False, f"path {rule['path_exists']} does not exist"
    return True, "passed"


def plan_waves(subtasks: List[dict]) -> (List[str], List[List[str]]):
    """Kahn topological sort. Returns (errors, waves of subtask ids)."""
    ids = [s["id"] for s in subtasks]
    by_title = {s["title"]: s["id"] for s in subtasks}
    errors, deps = [], {}
    for s in subtasks:
        d = []
        for ref in s.get("depends_on") or []:
            rid = ref if ref in ids else by_title.get(ref)
            if rid is None:
                errors.append(f"'{s['title']}' depends on unknown step '{ref}'")
            elif rid == s["id"]:
                errors.append(f"'{s['title']}' depends on itself")
            else:
                d.append(rid)
        deps[s["id"]] = set(d)
    waves, done, left = [], set(), set(ids)
    while left:
        ready = sorted((i for i in left if deps[i] <= done), key=ids.index)
        if not ready:
            names = [s["title"] for s in subtasks if s["id"] in left]
            errors.append(f"dependency cycle between: {', '.join(names)}")
            break
        waves.append(ready)
        done |= set(ready)
        left -= set(ready)
    return errors, waves


class MissionEngine:
    def __init__(self, db_path: str = None, executor: Callable[[str, dict], Awaitable[dict]] = None,
                 retry_base_delay: float = 0.5):
        self.db_path = db_path or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'missions.db')
        self.on_progress: Optional[Callable] = None
        self._custom_executor = executor
        self.retry_base_delay = retry_base_delay
        self._tasks: Dict[str, asyncio.Task] = {}
        self._loops: Dict[str, asyncio.AbstractEventLoop] = {}
        self._init_db()

    # ------------------------------------------------------------ executor ----
    async def _exec(self, tool_name: str, args: dict) -> dict:
        if self._custom_executor:
            return await self._custom_executor(tool_name, args)
        import tool_runner
        return await tool_runner.run_tool(tool_name, args, source="mission")

    def _known_tools(self):
        if self._custom_executor:
            return None
        import tool_runner
        return tool_runner

    # ------------------------------------------------------------ persistence --
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS missions (
                id TEXT PRIMARY KEY, title TEXT, description TEXT, status TEXT,
                data JSON, created_at TEXT, updated_at TEXT)""")

    def _save_mission(self, m: Mission):
        data = json.dumps({'subtasks': [asdict(s) for s in m.subtasks], 'result': m.result}, default=str)
        now = _now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""INSERT INTO missions (id, title, description, status, data, created_at, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description,
                              status=excluded.status, data=excluded.data, updated_at=excluded.updated_at""",
                         (m.id, m.title, m.description, m.status, data, m.created_at or now, now))

    @staticmethod
    def _row_to_mission(row) -> Mission:
        mid, title, description, status, data_json, created_at, updated_at = row
        data = json.loads(data_json or '{}')
        known = SubTask.__dataclass_fields__.keys()
        subs = []
        for st in data.get('subtasks', []):
            st = {k: v for k, v in st.items() if k in known}
            st.setdefault('id', str(uuid.uuid4()))
            st.setdefault('title', '')
            st.setdefault('tool_name', '')
            st.setdefault('args', {})
            subs.append(SubTask(**st))
        return Mission(mid, title, description, status, subs, created_at or '', updated_at or '', data.get('result'))

    def _load_mission(self, mission_id: str) -> Optional[Mission]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT id, title, description, status, data, created_at, updated_at "
                               "FROM missions WHERE id=?", (mission_id,)).fetchone()
        return self._row_to_mission(row) if row else None

    def _checkpoint(self, m: Mission):
        """Save progress without clobbering a pause/cancel that arrived from another caller."""
        st = self._db_status(m.id)
        if st in ('paused', 'cancelled') and m.status == 'running':
            m.status = st
        self._save_mission(m)

    def _db_status(self, mission_id: str) -> Optional[str]:
        with sqlite3.connect(self.db_path) as conn:
            row = conn.execute("SELECT status FROM missions WHERE id=?", (mission_id,)).fetchone()
        return row[0] if row else None

    def _set_status(self, mission_id: str, status: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE missions SET status=?, updated_at=? WHERE id=?", (status, _now(), mission_id))

    # ------------------------------------------------------------ planning ----
    def normalise_subtasks(self, subtasks_data: list) -> List[dict]:
        out = []
        for i, st in enumerate(subtasks_data or []):
            if not isinstance(st, dict):
                raise ValueError(f"Subtask {i + 1} is not an object.")
            args = st.get('args') or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    raise ValueError(f"Subtask {i + 1} args is not valid JSON.")
            verify = st.get('verify')
            if isinstance(verify, str):
                verify = json.loads(verify) if verify.strip() else None
            deps = st.get('depends_on') or []
            if isinstance(deps, str):
                deps = [deps]
            out.append({'id': st.get('id') or str(uuid.uuid4()), 'title': st.get('title') or f'Step {i + 1}',
                        'tool_name': st.get('tool_name') or st.get('tool') or '', 'args': dict(args),
                        'depends_on': list(deps), 'verify': verify,
                        'max_retries': int(st.get('max_retries', 2))})
        return out

    def validate_plan(self, subtasks_data: list) -> dict:
        """Check a plan without running it. Returns {'ok', 'errors', 'waves', 'subtasks'}."""
        try:
            subs = self.normalise_subtasks(subtasks_data)
        except (ValueError, json.JSONDecodeError) as e:
            return {'ok': False, 'errors': [str(e)], 'waves': [], 'subtasks': []}
        errors = []
        if not subs:
            errors.append("A mission needs at least one subtask.")
        titles = [s['title'] for s in subs]
        for t in {t for t in titles if titles.count(t) > 1}:
            errors.append(f"Duplicate step title '{t}' (titles must be unique so steps can reference each other).")
        tr = self._known_tools()
        for s in subs:
            if not s['tool_name']:
                errors.append(f"'{s['title']}' has no tool_name.")
            elif tr is not None:
                if s['tool_name'] in tr.BLOCKED_IN_AUTOMATION:
                    errors.append(f"'{s['title']}' uses {s['tool_name']}, which needs on-screen approval and can't run in a mission.")
                elif s['tool_name'] not in tr.REGISTRY:
                    errors.append(f"'{s['title']}' uses unknown tool '{s['tool_name']}'.")
        cyc_errors, waves = plan_waves(subs)
        errors += cyc_errors
        return {'ok': not errors, 'errors': errors, 'waves': waves, 'subtasks': subs}

    # ------------------------------------------------------------ public API --
    def create_mission(self, title: str, description: str, subtasks_data: list) -> str:
        """Validate, persist and return the mission id. Raises ValueError for an invalid plan."""
        plan = self.validate_plan(subtasks_data)
        if not plan['ok']:
            raise ValueError("Invalid mission plan: " + "; ".join(plan['errors']))
        now = _now()
        m = Mission(id=str(uuid.uuid4()), title=title or 'Mission', description=description or '',
                    subtasks=[SubTask(**s) for s in plan['subtasks']], created_at=now, updated_at=now)
        self._save_mission(m)
        print(f"[MISSIONS] Created mission {m.id}: {m.title!r} with {len(m.subtasks)} subtasks.")
        return m.id

    def start(self, mission_id: str) -> asyncio.Task:
        """Run a mission in the background on the current event loop."""
        return asyncio.create_task(self.execute_mission(mission_id))

    async def execute_mission(self, mission_id: str):
        running = self._tasks.get(mission_id)
        if running and not running.done() and running is not asyncio.current_task():
            return  # already running
        self._tasks[mission_id] = asyncio.current_task()
        self._loops[mission_id] = asyncio.get_running_loop()
        try:
            await self._run(mission_id)
        except asyncio.CancelledError:
            m = self._load_mission(mission_id)
            if m:
                for st in m.subtasks:
                    if st.status == 'running':
                        st.status, st.error = 'pending', 'interrupted by cancellation'
                m.status = 'cancelled'
                self._save_mission(m)
                self._emit(mission_id, 'status', {'status': 'cancelled', 'title': m.title})
        finally:
            self._tasks.pop(mission_id, None)
            self._loops.pop(mission_id, None)

    async def _run(self, mission_id: str):
        m = self._load_mission(mission_id)
        if not m:
            print(f"[MISSIONS] mission {mission_id} not found.")
            return
        if m.status in FINAL:
            return
        t_start = time.perf_counter()
        ids = {s.id for s in m.subtasks}
        by_title = {s.title: s.id for s in m.subtasks}
        for s in m.subtasks:
            s.depends_on = [d if d in ids else by_title[d] for d in s.depends_on if d in ids or d in by_title]
            if s.status == 'running':        # resumed after pause / crash: re-run that step
                s.status = 'pending'
        m.status = 'running'
        self._save_mission(m)
        self._emit(mission_id, 'status', {'status': 'running', 'title': m.title})
        by_id = {s.id: s for s in m.subtasks}

        while True:
            st_db = self._db_status(mission_id)
            if st_db in ('cancelled', 'paused'):
                self._emit(mission_id, 'status', {'status': st_db, 'title': m.title})
                return
            # Dependents of failed/skipped steps can never run.
            changed = True
            while changed:
                changed = False
                for s in m.subtasks:
                    if s.status != 'pending':
                        continue
                    bad = [by_id[d] for d in s.depends_on if by_id[d].status in ('failed', 'skipped')]
                    if bad:
                        s.status, s.error, changed = 'skipped', f"dependency '{bad[0].title}' did not succeed", True
                        self._emit(mission_id, 'subtask_skipped', {'subtask_id': s.id, 'title': s.title, 'reason': s.error})
            ready = [s for s in m.subtasks if s.status == 'pending'
                     and all(by_id[d].status == 'done' for d in s.depends_on)]
            if not ready:
                break
            parallel = [s for s in ready if self._parallel_safe(s.tool_name)]
            sequential = [s for s in ready if s not in parallel]
            if parallel:
                await asyncio.gather(*(self._run_subtask(mission_id, m, s, by_id) for s in parallel))
                self._checkpoint(m)
            for s in sequential:
                if self._db_status(mission_id) in ('cancelled', 'paused'):
                    break
                await self._run_subtask(mission_id, m, s, by_id)
                self._checkpoint(m)

        if self._db_status(mission_id) in ('cancelled', 'paused'):
            m.status = self._db_status(mission_id)
            self._save_mission(m)
            self._emit(mission_id, 'status', {'status': m.status, 'title': m.title})
            return
        counts = {k: sum(1 for s in m.subtasks if s.status == k) for k in ('done', 'failed', 'skipped', 'pending')}
        all_done = counts['done'] == len(m.subtasks)
        m.status = 'completed' if all_done else 'failed'
        m.result = {**counts, 'total': len(m.subtasks),
                    'verified': all_done and all(s.verified is not False for s in m.subtasks),
                    'duration_s': round(time.perf_counter() - t_start, 3),
                    'failures': [{'title': s.title, 'error': s.error} for s in m.subtasks if s.status in ('failed', 'skipped')]}
        self._save_mission(m)
        self._emit(mission_id, 'status', {'status': m.status, 'title': m.title, 'result': m.result,
                                          'subtasks': [{'id': s.id, 'title': s.title, 'status': s.status} for s in m.subtasks]})
        print(f"[MISSIONS] Mission {mission_id} finished: {m.status} {counts}")

    def _parallel_safe(self, tool_name: str) -> bool:
        if self._custom_executor:
            return getattr(self, 'custom_parallel_safe', lambda n: False)(tool_name)
        import tool_runner
        s = tool_runner.spec(tool_name)
        return bool(s and s.parallel_safe)

    @staticmethod
    def resolve_args(args: Any, m: Mission) -> Any:
        """Replace {{Step}} / {{Step.key}} with earlier step outputs (shared task context)."""
        outputs = {}
        for s in m.subtasks:
            if s.status == 'done':
                out = (s.result or {}).get('output') if isinstance(s.result, dict) else s.result
                outputs[s.title] = outputs[s.id] = out

        def lookup(ref):
            name, _, key = ref.partition('.')
            if name not in outputs and ref in outputs:
                return outputs[ref]
            if name not in outputs:
                raise ValueError(f"Template {{{{{ref}}}}} refers to a step that has not completed.")
            v = outputs[name]
            for k in [k for k in key.split('.') if k]:
                if isinstance(v, dict) and k in v:
                    v = v[k]
                elif isinstance(v, list) and k.isdigit() and int(k) < len(v):
                    v = v[int(k)]
                else:
                    raise ValueError(f"Template {{{{{ref}}}}}: '{k}' not found in that step's output.")
            return v

        if isinstance(args, dict):
            return {k: MissionEngine.resolve_args(v, m) for k, v in args.items()}
        if isinstance(args, list):
            return [MissionEngine.resolve_args(v, m) for v in args]
        if isinstance(args, str):
            whole = _TEMPLATE.fullmatch(args.strip())
            if whole:
                return lookup(whole.group(1))
            return _TEMPLATE.sub(lambda mt: (lambda v: v if isinstance(v, str) else json.dumps(v, default=str))(lookup(mt.group(1))), args)
        return args

    async def _run_subtask(self, mission_id: str, m: Mission, s: SubTask, by_id):
        s.status, s.started_at = 'running', _now()
        self._checkpoint(m)
        self._emit(mission_id, 'subtask_running', {'subtask_id': s.id, 'title': s.title, 'tool': s.tool_name})
        t0 = time.perf_counter()
        try:
            args = self.resolve_args(s.args, m)
        except ValueError as e:
            s.status, s.error, s.finished_at = 'failed', str(e), _now()
            self._emit(mission_id, 'subtask_failed', {'subtask_id': s.id, 'title': s.title, 'error': s.error, 'retries': 0})
            return
        last_error = None
        for attempt in range(s.max_retries + 1):
            try:
                res = await self._exec(s.tool_name, args)
                output = res.get('output') if isinstance(res, dict) else res
                ok, why = check_verify(s.verify, output)
                if not ok:
                    raise RuntimeError(f"verification failed: {why}")
                s.status, s.result, s.error, s.retries = 'done', res, None, attempt
                s.verified = True if s.verify else None
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                s.retries = attempt
                print(f"[MISSIONS] {s.title!r} attempt {attempt + 1} failed: {last_error}")
                if type(exc).__name__ == 'ToolError' or attempt >= s.max_retries:
                    break
                await asyncio.sleep(self.retry_base_delay * (2 ** attempt))
        s.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        s.finished_at = _now()
        if s.status == 'done':
            self._emit(mission_id, 'subtask_done', {'subtask_id': s.id, 'title': s.title, 'result': s.result,
                                                    'retries': s.retries})
        else:
            s.status, s.error = 'failed', last_error
            if s.verify and last_error and 'verification failed' in last_error:
                s.verified = False
            self._emit(mission_id, 'subtask_failed', {'subtask_id': s.id, 'title': s.title, 'error': last_error,
                                                      'retries': s.retries})

    def pause_mission(self, mission_id: str) -> bool:
        if self._db_status(mission_id) == 'running':
            self._set_status(mission_id, 'paused')   # takes effect between steps
            return True
        return False

    def resume_mission(self, mission_id: str) -> bool:
        """Mark a paused (or interrupted) mission pending again; call start()/execute_mission to run it."""
        if self._db_status(mission_id) in ('paused', 'pending', 'running') and mission_id not in self._tasks:
            self._set_status(mission_id, 'pending')
            return True
        return False

    def cancel_mission(self, mission_id: str) -> bool:
        st = self._db_status(mission_id)
        if st is None or st in FINAL:
            return False
        self._set_status(mission_id, 'cancelled')
        task, loop = self._tasks.get(mission_id), self._loops.get(mission_id)
        if task and loop and not task.done():
            try:
                if loop.is_running() and loop is not _running_loop():
                    loop.call_soon_threadsafe(task.cancel)
                else:
                    task.cancel()
            except RuntimeError:
                pass
        print(f"[MISSIONS] Mission {mission_id} cancelled.")
        return True

    def get_mission(self, mission_id: str) -> Optional[dict]:
        m = self._load_mission(mission_id)
        return self._mission_to_dict(m) if m else None

    def list_missions(self, limit: int = 20) -> list:
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT id, title, description, status, data, created_at, updated_at "
                                "FROM missions ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._mission_to_dict(self._row_to_mission(r)) for r in rows]

    @staticmethod
    def _mission_to_dict(m: Mission) -> dict:
        return {'id': m.id, 'title': m.title, 'description': m.description, 'status': m.status,
                'created_at': m.created_at, 'updated_at': m.updated_at, 'result': m.result,
                'subtasks': [asdict(s) for s in m.subtasks]}

    def _emit(self, mission_id: str, event_type: str, data: dict):
        if self.on_progress:
            try:
                self.on_progress(mission_id, event_type, data)
            except Exception as e:
                print(f"[MISSIONS] _emit error: {e}")


def _running_loop():
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# One shared engine for the voice assistant (ada.py) and the socket server.
engine = MissionEngine()
