"""
Advanced Diagnostics for Nexora. Reuses system_monitor (live snapshot + perf_history.db)
and telemetry (tool latencies, only if the user enabled collection).

Every number in the report is measured on this machine when the report runs or
read from stored history; a check that cannot run says so ("skipped"/"unknown")
instead of reporting a value.

Checks: API reachability, Gemini model connectivity (real authenticated request),
OmniRoute, every SQLite database (integrity_check), backend modules import,
log error analysis (grouped by component tag), performance bottlenecks.
"""
import os, sys, re, time, sqlite3
from datetime import datetime
from typing import Dict, List
import requests

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.dirname(_BACKEND)

MODULES = ['mission_control', 'tool_runner', 'event_engine', 'knowledge_graph', 'predictor', 'digital_twin',
           'app_context', 'telemetry', 'memory', 'rag_engine', 'system_monitor', 'focus', 'computer_bridge',
           'training_centre', 'email_agent', 'wake_word']

THRESH = {'cpu_sustained': 85.0, 'ram_sustained': 90.0, 'disk_full': 90.0, 'tool_p95_ms': 5000.0,
          'tool_fail_rate': 0.2, 'db_mb': 100.0, 'proc_cpu': 50.0, 'proc_mem_mb': 2048.0}


def _timed(fn):
    t0 = time.perf_counter()
    try:
        out = fn()
    except Exception as e:
        out = {'error': f'{type(e).__name__}: {e}'}
    if isinstance(out, dict):
        out['_ms'] = round((time.perf_counter() - t0) * 1000, 1)
    return out


def _live_model_name():
    try:
        with open(os.path.join(_BACKEND, 'ada.py'), encoding='utf-8') as f:
            m = re.search(r'^MODEL\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else None
    except OSError:
        return None


class DiagnosticsEngine:
    def __init__(self, backend_dir: str = None, http=requests):
        self._backend_dir = backend_dir or _BACKEND
        self._http = http

    # ---------------------------------------------------------------- APIs --
    def check_api_health(self) -> dict:
        res = {}
        t0 = time.perf_counter()
        try:
            self._http.get('https://www.google.com/generate_204', timeout=5)
            res['internet'] = True
            res['internet_ms'] = round((time.perf_counter() - t0) * 1000, 1)
        except Exception as e:
            res['internet'] = False
            res['internet_error'] = type(e).__name__
        res['gemini_configured'] = bool(os.environ.get('GEMINI_API_KEY'))
        res.update(self.check_omniroute())
        return res

    def check_omniroute(self) -> dict:
        url = os.getenv("OMNIROUTE_URL", "http://localhost:20128/v1").rstrip("/")
        t0 = time.perf_counter()
        try:
            r = self._http.get(f'{url}/models', timeout=3)
            data = r.json() if r.status_code == 200 else {}
            models = data.get('data', data) if isinstance(data, dict) else data
            return {'omniroute': r.status_code == 200, 'omniroute_models': len(models) if isinstance(models, list) else 0,
                    'omniroute_ms': round((time.perf_counter() - t0) * 1000, 1)}
        except Exception:
            return {'omniroute': False, 'omniroute_models': 0}

    def check_model_connectivity(self) -> dict:
        """Authenticated request to the Gemini API: proves the key works and the live model exists."""
        key = os.environ.get('GEMINI_API_KEY')
        model = _live_model_name()
        if not key:
            return {'status': 'skipped', 'reason': 'GEMINI_API_KEY not set', 'model': model}
        t0 = time.perf_counter()
        try:
            r = self._http.get('https://generativelanguage.googleapis.com/v1beta/models',
                               headers={'x-goog-api-key': key}, params={'pageSize': 1000}, timeout=10)
            ms = round((time.perf_counter() - t0) * 1000, 1)
            if r.status_code != 200:
                return {'status': 'error', 'http_status': r.status_code, 'latency_ms': ms, 'model': model,
                        'reason': 'key rejected' if r.status_code in (400, 401, 403) else 'API error'}
            names = {m.get('name') for m in r.json().get('models', [])}
            return {'status': 'ok', 'latency_ms': ms, 'model': model, 'models_visible': len(names),
                    'live_model_listed': (model in names) if model else None}
        except Exception as e:
            return {'status': 'error', 'reason': f'{type(e).__name__}', 'model': model}

    # ----------------------------------------------------------- databases --
    def check_database_health(self) -> dict:
        result = {}
        for fname in sorted(os.listdir(self._backend_dir)):
            if not fname.endswith('.db'):
                continue
            fpath = os.path.join(self._backend_dir, fname)
            info = {'size_kb': round(os.path.getsize(fpath) / 1024, 1), 'tables': 0, 'ok': False}
            t0 = time.perf_counter()
            try:
                with sqlite3.connect(f'file:{fpath}?mode=ro', uri=True, timeout=3) as con:
                    info['tables'] = con.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
                    info['integrity'] = con.execute('PRAGMA quick_check').fetchone()[0]
                    info['ok'] = info['integrity'] == 'ok'
            except Exception as e:
                info['error'] = str(e)
            info['check_ms'] = round((time.perf_counter() - t0) * 1000, 1)
            result[fname] = info
        return result

    def check_backend_health(self) -> dict:
        if self._backend_dir not in sys.path:
            sys.path.insert(0, self._backend_dir)
        result = {}
        for mod in MODULES:
            t0 = time.perf_counter()
            try:
                __import__(mod)
                result[mod] = {'ok': True, 'error': None}
            except Exception as e:
                result[mod] = {'ok': False, 'error': f'{type(e).__name__}: {e}'}
            result[mod]['import_ms'] = round((time.perf_counter() - t0) * 1000, 1)
        return result

    # ---------------------------------------------------------------- logs --
    def _log_files(self) -> List[str]:
        out = []
        for d in (self._backend_dir, _PROJECT):
            for f in ('apa_launch.log', 'backend.log', 'app.log'):
                p = os.path.join(d, f)
                if os.path.exists(p) and p not in out:
                    out.append(p)
        return out

    def analyze_errors(self, log_path: str = None, tail: int = 3000) -> dict:
        files = [log_path] if log_path and os.path.exists(log_path) else self._log_files()
        if not files:
            return {'log_files': [], 'log_file': None, 'total_errors': 0, 'top_errors': [], 'by_component': {},
                    'recent_errors': []}
        err_kw = ('ERROR', 'CRITICAL', 'Traceback', '[ERR]', 'Exception', 'failed', 'error:')
        names, comps, recent, total = {}, {}, [], 0
        for path in files:
            try:
                with open(path, 'r', encoding='utf-8', errors='replace') as f:
                    lines = f.readlines()[-tail:]
            except OSError:
                continue
            for line in lines:
                if not any(k in line for k in err_kw):
                    continue
                total += 1
                line = line.rstrip()
                for n in re.findall(r'\b(\w+(?:Error|Exception))\b', line):
                    names[n] = names.get(n, 0) + 1
                tag = re.search(r'\[([A-Z][A-Z_ ]{1,15})\]', line.replace('[Python]', '').replace('[ADA DEBUG]', ''))
                comp = tag.group(1).strip() if tag else 'other'
                comps[comp] = comps.get(comp, 0) + 1
                recent.append(line[-300:])
        return {'log_files': files, 'log_file': files[0], 'total_errors': total,
                'top_errors': sorted(names.items(), key=lambda x: -x[1])[:5],
                'by_component': dict(sorted(comps.items(), key=lambda x: -x[1])[:8]),
                'recent_errors': recent[-5:]}

    # ---------------------------------------------------------- bottlenecks --
    def detect_bottlenecks(self, history: list = None, snapshot: dict = None, tool_stats: dict = None,
                           db_health: dict = None) -> dict:
        """Find sustained resource pressure and slow/failing tools from measured data."""
        import system_monitor, telemetry
        findings, basis = [], {}
        if history is None:
            history = system_monitor.get_perf_history(1)
        recent = history[-30:]
        basis['perf_samples'] = len(recent)
        if len(recent) >= 5:
            cpu = [h['cpu'] or 0 for h in recent]
            ram = [h['ram'] or 0 for h in recent]
            high_cpu = sum(1 for c in cpu if c >= THRESH['cpu_sustained']) / len(cpu)
            high_ram = sum(1 for r in ram if r >= THRESH['ram_sustained']) / len(ram)
            basis.update(cpu_avg=round(sum(cpu) / len(cpu), 1), ram_avg=round(sum(ram) / len(ram), 1))
            if high_cpu >= 0.6:
                findings.append({'severity': 'high', 'area': 'cpu',
                                 'finding': f'CPU >= {THRESH["cpu_sustained"]:.0f}% in {high_cpu:.0%} of the last {len(cpu)} minutes'})
            if high_ram >= 0.6:
                findings.append({'severity': 'high', 'area': 'ram',
                                 'finding': f'RAM >= {THRESH["ram_sustained"]:.0f}% in {high_ram:.0%} of the last {len(ram)} minutes'})
        if snapshot is None:
            snapshot = system_monitor.snapshot()
        if snapshot['disk']['percent'] >= THRESH['disk_full']:
            findings.append({'severity': 'medium', 'area': 'disk',
                             'finding': f"Disk C is {snapshot['disk']['percent']:.0f}% full ({snapshot['disk']['free_gb']} GB free)"})
        for p in snapshot.get('processes', [])[:10]:
            if p.get('cpu', 0) >= THRESH['proc_cpu'] or p.get('mem_mb', 0) >= THRESH['proc_mem_mb']:
                findings.append({'severity': 'medium', 'area': 'process',
                                 'finding': f"{p['name']} (pid {p['pid']}) uses {p['cpu']}% CPU / {p['mem_mb']} MB"})
        if tool_stats is None:
            tool_stats = telemetry.latency_stats(time.time() - 7 * 86400)
        basis['telemetry_enabled'] = telemetry.is_enabled()
        basis['tools_measured'] = len(tool_stats)
        for tool, st in tool_stats.items():
            if st['count'] >= 3 and st['p95_ms'] >= THRESH['tool_p95_ms']:
                findings.append({'severity': 'medium', 'area': 'tool_latency',
                                 'finding': f"{tool}: p95 {st['p95_ms']:.0f} ms over {st['count']} calls"})
            if st['count'] >= 5 and st['fail_rate'] >= THRESH['tool_fail_rate']:
                findings.append({'severity': 'high', 'area': 'tool_errors',
                                 'finding': f"{tool}: {st['fail_rate']:.0%} of {st['count']} calls failed"})
        for fname, info in (db_health or {}).items():
            if info.get('size_kb', 0) / 1024 >= THRESH['db_mb']:
                findings.append({'severity': 'low', 'area': 'database', 'finding': f"{fname} is {info['size_kb'] / 1024:.0f} MB"})
        return {'findings': findings, 'basis': basis, 'thresholds': THRESH}

    # -------------------------------------------------------------- report --
    def generate_report(self) -> dict:
        api = _timed(self.check_api_health)
        model = _timed(self.check_model_connectivity)
        db = self.check_database_health()
        backend = self.check_backend_health()
        errors = _timed(self.analyze_errors)
        bottlenecks = _timed(lambda: self.detect_bottlenecks(db_health=db))
        return {'timestamp': datetime.now().isoformat(timespec='seconds'), 'api_health': api,
                'model_connectivity': model, 'db_health': db, 'backend_health': backend, 'error_analysis': errors,
                'bottlenecks': bottlenecks, 'recommendations': self.recommend(api, model, db, backend, errors, bottlenecks)}

    @staticmethod
    def recommend(api, model, db, backend, errors, bottlenecks) -> list:
        rec = []
        if not api.get('internet'):
            rec.append('Internet is unreachable: check the network (voice, weather, news and email all need it).')
        if not api.get('gemini_configured'):
            rec.append('Set GEMINI_API_KEY in .env at the project root to enable the voice assistant.')
        if model.get('status') == 'error':
            rec.append(f"Gemini API check failed ({model.get('reason') or model.get('http_status')}): verify the API key and quota.")
        elif model.get('live_model_listed') is False:
            rec.append(f"The live model {model.get('model')} is not in this key's model list: update MODEL in ada.py.")
        if not api.get('omniroute'):
            rec.append('OmniRoute is not running on port 20128: start it with the `omniroute` command if you need build tasks.')
        for f, info in db.items():
            if not info.get('ok'):
                rec.append(f'Database {f} failed its integrity check ({info.get("integrity") or info.get("error")}): back it up, then delete it so it is recreated.')
        for mod, info in backend.items():
            if not info.get('ok'):
                err = info.get('error') or ''
                m = re.search(r"No module named '([^']+)'", err)
                rec.append(f"pip install {m.group(1).split('.')[0]}  (needed by {mod})" if m else f"Module {mod} failed to load: {err[:100]}")
        if errors.get('total_errors', 0) > 20:
            comp = next(iter(errors.get('by_component') or {}), 'other')
            rec.append(f"{errors['total_errors']} error lines in recent logs, mostly from [{comp}]: review {os.path.basename(errors.get('log_file') or '')}.")
        for f in bottlenecks.get('findings', []):
            area = f['area']
            tip = {'cpu': 'close heavy apps or pause God\'s Eye / focus screen checks',
                   'ram': 'close unused apps or browser tabs',
                   'disk': 'free disk space (Downloads, temp files, old logs)',
                   'process': 'check whether this process should be running',
                   'tool_latency': 'check the network or the service this tool calls',
                   'tool_errors': 'look at the error log for this tool',
                   'database': 'archive old rows (e.g. telemetry / perf history)'}.get(area, 'investigate')
            rec.append(f"{f['finding']}: {tip}.")
        return rec

    def get_performance_history(self, hours: int = 24) -> list:
        import system_monitor
        return system_monitor.get_perf_history(hours)
