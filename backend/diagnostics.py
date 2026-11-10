import os, sys, json, re, asyncio, time
from datetime import datetime
from typing import Dict, Any, List
import requests


class DiagnosticsEngine:
    def __init__(self):
        self._backend_dir = os.path.dirname(os.path.abspath(__file__))

    def check_api_health(self) -> dict:
        # Internet check
        internet = False
        try:
            requests.get('https://www.google.com', timeout=5)
            internet = True
        except Exception:
            pass

        # Gemini check
        gemini_configured = False
        try:
            import google.genai  # noqa: F401
            gemini_configured = bool(os.environ.get('GEMINI_API_KEY'))
        except Exception:
            gemini_configured = bool(os.environ.get('GEMINI_API_KEY'))

        # OmniRoute check
        omniroute = False
        omniroute_models = 0
        try:
            resp = requests.get('http://localhost:20128/v1/models', timeout=3)
            if resp.status_code == 200:
                omniroute = True
                data = resp.json()
                models = data.get('data', data) if isinstance(data, dict) else data
                omniroute_models = len(models) if isinstance(models, list) else 0
        except Exception:
            pass

        return {
            'internet': internet,
            'gemini_configured': gemini_configured,
            'omniroute': omniroute,
            'omniroute_models': omniroute_models,
        }

    def check_database_health(self) -> dict:
        import sqlite3
        result = {}
        for fname in os.listdir(self._backend_dir):
            if not fname.endswith('.db'):
                continue
            fpath = os.path.join(self._backend_dir, fname)
            size_kb = round(os.path.getsize(fpath) / 1024, 1)
            tables = 0
            ok = False
            try:
                with sqlite3.connect(fpath) as con:
                    rows = con.execute('SELECT COUNT(*) FROM sqlite_master').fetchone()
                    tables = rows[0] if rows else 0
                    ok = True
            except Exception as e:
                ok = False
            result[fname] = {'size_kb': size_kb, 'tables': tables, 'ok': ok}
        return result

    def check_backend_health(self) -> dict:
        modules = [
            'mission_control', 'event_engine', 'knowledge_graph', 'predictor',
            'digital_twin', 'memory', 'rag_engine', 'system_monitor',
            'focus', 'computer_bridge',
        ]
        result = {}
        # Temporarily add backend dir to path if needed
        if self._backend_dir not in sys.path:
            sys.path.insert(0, self._backend_dir)
        for mod in modules:
            try:
                __import__(mod)
                result[mod] = {'ok': True, 'error': None}
            except Exception as e:
                result[mod] = {'ok': False, 'error': str(e)}
        return result

    def analyze_errors(self, log_path: str = None) -> dict:
        # Find a log file
        found_log = None
        if log_path and os.path.exists(log_path):
            found_log = log_path
        else:
            for candidate in ['app.log', 'backend.log']:
                p = os.path.join(self._backend_dir, candidate)
                if os.path.exists(p):
                    found_log = p
                    break
            if not found_log:
                for fname in os.listdir(self._backend_dir):
                    if fname.endswith('.log'):
                        found_log = os.path.join(self._backend_dir, fname)
                        break

        if not found_log:
            return {
                'log_file': None,
                'total_errors': 0,
                'top_errors': [],
                'recent_errors': [],
            }

        try:
            with open(found_log, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
        except Exception as e:
            return {
                'log_file': found_log,
                'total_errors': 0,
                'top_errors': [],
                'recent_errors': [],
                'read_error': str(e),
            }

        last_lines = lines[-500:]
        error_keywords = ('ERROR', 'CRITICAL', 'Exception', 'Traceback')
        error_lines = [l.rstrip() for l in last_lines if any(k in l for k in error_keywords)]
        total_errors = len(error_lines)

        # Count named error/exception patterns
        pattern_counts: Dict[str, int] = {}
        error_pattern = re.compile(r'(\w+Error|\w+Exception)')
        for l in last_lines:
            for match in error_pattern.findall(l):
                pattern_counts[match] = pattern_counts.get(match, 0) + 1
        top_errors = sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True)[:5]

        recent_errors = error_lines[-3:] if error_lines else []

        return {
            'log_file': found_log,
            'total_errors': total_errors,
            'top_errors': top_errors,
            'recent_errors': recent_errors,
        }

    def generate_report(self) -> dict:
        api_health = self.check_api_health()
        db_health = self.check_database_health()
        backend_health = self.check_backend_health()
        error_analysis = self.analyze_errors()

        recommendations = []

        if not api_health.get('internet'):
            recommendations.append('Check network connection — internet is unreachable.')
        if not api_health.get('gemini_configured'):
            recommendations.append('Set GEMINI_API_KEY in .env to enable Gemini AI.')
        if not api_health.get('omniroute'):
            recommendations.append('OmniRoute is not running on port 20128 — start the OmniRoute server.')

        for fname, info in db_health.items():
            if not info.get('ok'):
                recommendations.append(f'Database {fname} is corrupt or unreadable — consider deleting and restarting.')

        for mod, info in backend_health.items():
            if not info.get('ok'):
                err = info.get('error', '')
                if 'No module named' in err:
                    pkg = err.split("'")[1] if "'" in err else mod
                    recommendations.append(f"Install missing dependency for {mod}: pip install {pkg}")
                else:
                    recommendations.append(f"Module {mod} failed to load: {err[:80]}")

        if error_analysis.get('total_errors', 0) > 20:
            recommendations.append(f"High error count in logs ({error_analysis['total_errors']} errors) — review {error_analysis.get('log_file', 'log file')}.")

        return {
            'timestamp': datetime.now().isoformat(),
            'api_health': api_health,
            'db_health': db_health,
            'backend_health': backend_health,
            'error_analysis': error_analysis,
            'recommendations': recommendations,
        }

    def get_performance_history(self, hours: int = 24) -> list:
        import system_monitor
        return system_monitor.get_perf_history(hours)
