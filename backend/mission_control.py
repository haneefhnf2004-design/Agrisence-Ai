"""
Mission Control Engine for Nexora.
Implements a real task queue with dependency ordering, retry handling,
cancellation, and progress reporting.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Callable
import asyncio, sqlite3, json, uuid, time, os
from datetime import datetime


@dataclass
class SubTask:
    id: str
    title: str
    tool_name: str
    args: dict
    status: str = 'pending'
    depends_on: List[str] = field(default_factory=list)
    result: Any = None
    error: Optional[str] = None
    retries: int = 0


@dataclass
class Mission:
    id: str
    title: str
    description: str
    status: str = 'pending'
    subtasks: List[SubTask] = field(default_factory=list)
    created_at: str = ''
    updated_at: str = ''
    result: Any = None


class MissionEngine:
    def __init__(self):
        self.db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'missions.db')
        self.on_progress: Optional[Callable] = None
        self._init_db()

    def _init_db(self):
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS missions (
                        id          TEXT PRIMARY KEY,
                        title       TEXT,
                        description TEXT,
                        status      TEXT,
                        data        JSON,
                        created_at  TEXT,
                        updated_at  TEXT
                    )
                """)
                conn.commit()
        except Exception as e:
            print(f"[MISSIONS] DB init error: {e}")

    # ---------------------------------------------------------------- persistence ----

    def _save_mission(self, mission: Mission):
        """Serialize and upsert mission into the database."""
        try:
            subtasks_data = []
            for st in mission.subtasks:
                subtasks_data.append({
                    'id': st.id,
                    'title': st.title,
                    'tool_name': st.tool_name,
                    'args': st.args,
                    'status': st.status,
                    'depends_on': st.depends_on,
                    'result': st.result,
                    'error': st.error,
                    'retries': st.retries,
                })
            data = json.dumps({
                'subtasks': subtasks_data,
                'result': mission.result,
            })
            now = datetime.now().isoformat(timespec='seconds')
            with sqlite3.connect(self.db_path) as conn:
                conn.execute(
                    """INSERT INTO missions (id, title, description, status, data, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           title=excluded.title,
                           description=excluded.description,
                           status=excluded.status,
                           data=excluded.data,
                           updated_at=excluded.updated_at""",
                    (mission.id, mission.title, mission.description, mission.status,
                     data, mission.created_at or now, now)
                )
                conn.commit()
        except Exception as e:
            print(f"[MISSIONS] _save_mission error: {e}")

    def _load_mission(self, mission_id: str) -> Optional[Mission]:
        """Load and deserialize a mission from the database."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                row = conn.execute(
                    "SELECT id, title, description, status, data, created_at, updated_at FROM missions WHERE id=?",
                    (mission_id,)
                ).fetchone()
            if not row:
                return None
            mid, title, description, status, data_json, created_at, updated_at = row
            data = json.loads(data_json or '{}')
            subtasks_data = data.get('subtasks', [])
            subtasks = []
            for st in subtasks_data:
                subtasks.append(SubTask(
                    id=st.get('id', str(uuid.uuid4())),
                    title=st.get('title', ''),
                    tool_name=st.get('tool_name', ''),
                    args=st.get('args', {}),
                    status=st.get('status', 'pending'),
                    depends_on=st.get('depends_on', []),
                    result=st.get('result'),
                    error=st.get('error'),
                    retries=st.get('retries', 0),
                ))
            return Mission(
                id=mid,
                title=title,
                description=description,
                status=status,
                subtasks=subtasks,
                created_at=created_at or '',
                updated_at=updated_at or '',
                result=data.get('result'),
            )
        except Exception as e:
            print(f"[MISSIONS] _load_mission error: {e}")
            return None

    # ---------------------------------------------------------------- public API ----

    def create_mission(self, title: str, description: str, subtasks_data: list) -> str:
        """Build a Mission from subtask dicts, persist it, and return the mission_id."""
        mission_id = str(uuid.uuid4())
        now = datetime.now().isoformat(timespec='seconds')
        subtasks = []
        for i, st_dict in enumerate(subtasks_data):
            subtask_id = st_dict.get('id') or str(uuid.uuid4())
            subtasks.append(SubTask(
                id=subtask_id,
                title=st_dict.get('title', f'Step {i + 1}'),
                tool_name=st_dict.get('tool_name', ''),
                args=st_dict.get('args', {}),
                status='pending',
                depends_on=st_dict.get('depends_on', []),
            ))
        mission = Mission(
            id=mission_id,
            title=title,
            description=description,
            status='pending',
            subtasks=subtasks,
            created_at=now,
            updated_at=now,
        )
        self._save_mission(mission)
        print(f"[MISSIONS] Created mission {mission_id}: {title!r} with {len(subtasks)} subtasks.")
        return mission_id

    async def execute_mission(self, mission_id: str):
        """Execute all subtasks in dependency order with retry logic."""
        mission = self._load_mission(mission_id)
        if not mission:
            print(f"[MISSIONS] execute_mission: mission {mission_id} not found.")
            return
        if mission.status in ('cancelled', 'completed'):
            print(f"[MISSIONS] execute_mission: mission {mission_id} is already {mission.status}.")
            return

        mission.status = 'running'
        self._save_mission(mission)
        self._emit(mission_id, 'status', {'status': 'running', 'title': mission.title})

        # Build an id/title index so depends_on can reference either.
        id_set = {st.id for st in mission.subtasks}
        title_to_id = {st.title: st.id for st in mission.subtasks}

        # Normalise depends_on: convert title references to ids.
        for st in mission.subtasks:
            normalised = []
            for dep in st.depends_on:
                if dep in id_set:
                    normalised.append(dep)
                elif dep in title_to_id:
                    normalised.append(title_to_id[dep])
                # Unknown references are silently dropped.
            st.depends_on = normalised

        # Execute in waves: tasks with all deps done go first, then next wave, etc.
        executed_ids: set = set()
        max_waves = len(mission.subtasks) + 1  # guard against circular deps

        for _wave in range(max_waves):
            # Reload mission status each wave to honour cancellation.
            fresh = self._load_mission(mission_id)
            if fresh and fresh.status == 'cancelled':
                print(f"[MISSIONS] Mission {mission_id} cancelled during execution.")
                return

            # Find runnable subtasks: pending with all deps satisfied.
            ready = [
                st for st in mission.subtasks
                if st.status == 'pending'
                and all(dep in executed_ids for dep in st.depends_on)
            ]
            if not ready:
                break  # either all done or stuck on failed deps

            for subtask in ready:
                # Check mission cancellation before each subtask.
                fresh = self._load_mission(mission_id)
                if fresh and fresh.status == 'cancelled':
                    return

                subtask.status = 'running'
                self._save_mission(mission)
                self._emit(mission_id, 'subtask_running', {
                    'subtask_id': subtask.id,
                    'title': subtask.title,
                    'tool': subtask.tool_name,
                })

                # Execute with up to 2 retries.
                success = False
                last_error = None
                for attempt in range(3):
                    try:
                        result = await self._execute_subtask(subtask)
                        subtask.status = 'done'
                        subtask.result = result
                        subtask.error = None
                        success = True
                        break
                    except Exception as exc:
                        last_error = str(exc)
                        subtask.retries = attempt + 1
                        print(f"[MISSIONS] Subtask {subtask.title!r} attempt {attempt + 1} failed: {exc}")
                        if attempt < 2:
                            await asyncio.sleep(1.0)

                if not success:
                    subtask.status = 'failed'
                    subtask.error = last_error
                    self._emit(mission_id, 'subtask_failed', {
                        'subtask_id': subtask.id,
                        'title': subtask.title,
                        'error': last_error,
                        'retries': subtask.retries,
                    })
                else:
                    self._emit(mission_id, 'subtask_done', {
                        'subtask_id': subtask.id,
                        'title': subtask.title,
                        'result': subtask.result,
                    })
                    executed_ids.add(subtask.id)

                self._save_mission(mission)

        # Determine final status.
        all_done = all(st.status == 'done' for st in mission.subtasks)
        mission.status = 'completed' if all_done else 'failed'
        self._save_mission(mission)
        self._emit(mission_id, 'status', {
            'status': mission.status,
            'title': mission.title,
            'subtasks': [{'id': st.id, 'title': st.title, 'status': st.status} for st in mission.subtasks],
        })
        print(f"[MISSIONS] Mission {mission_id} finished: {mission.status}")

    async def _execute_subtask(self, subtask: SubTask) -> dict:
        """
        Stub executor — returns a stub result.
        Real tool dispatch will be wired separately via on_execute callback.
        """
        await asyncio.sleep(0.1)  # yield to event loop
        return {'stub': True, 'tool': subtask.tool_name, 'args': subtask.args}

    def pause_mission(self, mission_id: str):
        """Pause a running mission."""
        mission = self._load_mission(mission_id)
        if mission and mission.status == 'running':
            mission.status = 'paused'
            self._save_mission(mission)
            print(f"[MISSIONS] Mission {mission_id} paused.")

    def cancel_mission(self, mission_id: str):
        """Cancel a mission that has not yet completed or failed."""
        mission = self._load_mission(mission_id)
        if mission and mission.status not in ('completed', 'failed'):
            mission.status = 'cancelled'
            self._save_mission(mission)
            print(f"[MISSIONS] Mission {mission_id} cancelled.")

    def get_mission(self, mission_id: str) -> Optional[dict]:
        """Return a mission as a plain dict, or None if not found."""
        mission = self._load_mission(mission_id)
        if not mission:
            return None
        return self._mission_to_dict(mission)

    def list_missions(self, limit: int = 20) -> list:
        """Return the most recent missions as a list of dicts."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                rows = conn.execute(
                    "SELECT id, title, description, status, data, created_at, updated_at "
                    "FROM missions ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
            results = []
            for row in rows:
                mid, title, description, status, data_json, created_at, updated_at = row
                data = json.loads(data_json or '{}')
                subtasks_data = data.get('subtasks', [])
                subtasks = [SubTask(
                    id=st.get('id', ''),
                    title=st.get('title', ''),
                    tool_name=st.get('tool_name', ''),
                    args=st.get('args', {}),
                    status=st.get('status', 'pending'),
                    depends_on=st.get('depends_on', []),
                    result=st.get('result'),
                    error=st.get('error'),
                    retries=st.get('retries', 0),
                ) for st in subtasks_data]
                mission = Mission(
                    id=mid, title=title, description=description,
                    status=status, subtasks=subtasks,
                    created_at=created_at or '', updated_at=updated_at or '',
                    result=data.get('result'),
                )
                results.append(self._mission_to_dict(mission))
            return results
        except Exception as e:
            print(f"[MISSIONS] list_missions error: {e}")
            return []

    # ---------------------------------------------------------------- helpers ----

    def _mission_to_dict(self, mission: Mission) -> dict:
        return {
            'id': mission.id,
            'title': mission.title,
            'description': mission.description,
            'status': mission.status,
            'created_at': mission.created_at,
            'updated_at': mission.updated_at,
            'result': mission.result,
            'subtasks': [
                {
                    'id': st.id,
                    'title': st.title,
                    'tool_name': st.tool_name,
                    'args': st.args,
                    'status': st.status,
                    'depends_on': st.depends_on,
                    'result': st.result,
                    'error': st.error,
                    'retries': st.retries,
                }
                for st in mission.subtasks
            ],
        }

    def _emit(self, mission_id: str, event_type: str, data: dict):
        """Emit a progress event if on_progress callback is registered."""
        if self.on_progress:
            try:
                self.on_progress(mission_id, event_type, data)
            except Exception as e:
                print(f"[MISSIONS] _emit error: {e}")
