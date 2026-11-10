"""Live laptop hardware + Task Manager style process list (psutil, plus nvidia-smi when present)."""
import shutil
import subprocess
import threading
import time

import psutil

_procs = {}          # pid -> Process, kept so cpu_percent() has a previous sample to diff against
_net = {"t": 0.0, "sent": 0, "recv": 0}
_disk = {"t": 0.0, "r": 0, "w": 0, "busy": 0}
_HAS_NVIDIA = shutil.which("nvidia-smi") is not None
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW: no console flash every 2 s


def _gpu():
    if not _HAS_NVIDIA:
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, creationflags=_NO_WINDOW).stdout.strip().splitlines()[0]
        name, util, used, total, temp = [x.strip() for x in out.split(",")]
        return {"name": name, "percent": float(util), "mem_used_mb": float(used), "mem_total_mb": float(total), "temp_c": float(temp)}
    except Exception:
        return None


def _top_processes(n=25):
    cores = psutil.cpu_count() or 1
    rows, alive = [], set()
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        alive.add(p.pid)
        proc = _procs.setdefault(p.pid, p)
        try:
            cpu = proc.cpu_percent(None) / cores   # Task Manager shows % of the whole CPU
            mem = (p.info["memory_info"].rss if p.info["memory_info"] else 0) / 1048576
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if p.pid == 0:  # "System Idle Process" is not a real load
            continue
        rows.append({"pid": p.pid, "name": p.info["name"] or "?", "cpu": round(cpu, 1), "mem_mb": round(mem)})
    for pid in list(_procs):
        if pid not in alive:
            _procs.pop(pid, None)
    rows.sort(key=lambda r: (r["cpu"], r["mem_mb"]), reverse=True)
    return rows[:n], len(alive)


# Reading ~350 processes takes ~3 s on this laptop, so a background thread keeps the list fresh
# and snapshot() (the 1 s hardware readout) never waits for it.
_proc_cache = {"rows": [], "count": 0}


def _proc_scanner():
    while True:
        try:
            rows, count = _top_processes()
            _proc_cache.update(rows=rows, count=count)
        except Exception as e:
            print(f"[STATS] process scan: {e}")
        time.sleep(1)


threading.Thread(target=_proc_scanner, daemon=True, name="proc-scanner").start()


def snapshot():
    """One reading of everything the UI shows. Call every ~2 s."""
    vm = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage("C:\\")
    except Exception:
        disk = psutil.disk_usage("/")
    bat = psutil.sensors_battery()
    io = psutil.net_io_counters()
    now = time.time()
    dt = now - _net["t"] if _net["t"] else 0
    up = (io.bytes_sent - _net["sent"]) / dt / 1024 if dt else 0
    down = (io.bytes_recv - _net["recv"]) / dt / 1024 if dt else 0
    _net.update(t=now, sent=io.bytes_sent, recv=io.bytes_recv)
    # Disk activity like Task Manager: read/write speed and % of time the disk was busy.
    dio = psutil.disk_io_counters()
    ddt = now - _disk["t"] if _disk["t"] else 0
    disk_io = {"read_mbs": 0, "write_mbs": 0, "active": 0}
    if dio:
        busy = dio.read_time + dio.write_time  # ms
        if ddt:
            disk_io = {"read_mbs": round(max(0, dio.read_bytes - _disk["r"]) / ddt / 1048576, 1),
                       "write_mbs": round(max(0, dio.write_bytes - _disk["w"]) / ddt / 1048576, 1),
                       "active": round(min(100, max(0, (busy - _disk["busy"]) / (ddt * 10))))}
        _disk.update(t=now, r=dio.read_bytes, w=dio.write_bytes, busy=busy)
    freq = psutil.cpu_freq()
    procs, count = _proc_cache["rows"], _proc_cache["count"]
    return {
        "time": now,
        "cpu": {"percent": psutil.cpu_percent(None), "cores": psutil.cpu_count(),
                "per_core": psutil.cpu_percent(None, percpu=True), "ghz": round(freq.current / 1000, 2) if freq else None},
        "ram": {"percent": vm.percent, "used_gb": round(vm.used / 1073741824, 1), "total_gb": round(vm.total / 1073741824, 1)},
        "disk": {"percent": disk.percent, "free_gb": round(disk.free / 1073741824), "total_gb": round(disk.total / 1073741824), **disk_io},
        "battery": {"percent": round(bat.percent), "plugged": bool(bat.power_plugged),
                    "mins_left": (bat.secsleft // 60 if bat.secsleft and bat.secsleft > 0 else None)} if bat else None,
        "net": {"up_kbs": round(max(up, 0), 1), "down_kbs": round(max(down, 0), 1)},
        "gpu": _gpu(),
        "processes": procs,
        "process_count": count,
        "uptime_h": round((now - psutil.boot_time()) / 3600, 1),
    }


import sqlite3 as _sqlite3
import os as _os

_PERF_DB = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'perf_history.db')

def _init_perf_db():
    with _sqlite3.connect(_PERF_DB) as con:
        con.execute('''CREATE TABLE IF NOT EXISTS perf_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            cpu_percent REAL,
            ram_percent REAL,
            disk_percent REAL,
            battery_percent REAL
        )''')

def save_perf_snapshot(snap: dict):
    _init_perf_db()
    import datetime
    ts = datetime.datetime.now().isoformat()
    cpu = snap.get('cpu', {}).get('percent', 0) if isinstance(snap.get('cpu'), dict) else 0
    ram = snap.get('ram', {}).get('percent', 0) if isinstance(snap.get('ram'), dict) else 0
    disk = snap.get('disk', {}).get('percent', 0) if isinstance(snap.get('disk'), dict) else 0
    battery = snap.get('battery', {}).get('percent', 0) if isinstance(snap.get('battery'), dict) else 0
    with _sqlite3.connect(_PERF_DB) as con:
        con.execute('INSERT INTO perf_history (timestamp, cpu_percent, ram_percent, disk_percent, battery_percent) VALUES (?,?,?,?,?)',
                    (ts, cpu, ram, disk, battery))
        # Keep only last 1440 rows (24h at 1/min)
        con.execute('DELETE FROM perf_history WHERE id NOT IN (SELECT id FROM perf_history ORDER BY id DESC LIMIT 1440)')

def get_perf_history(hours=24):
    _init_perf_db()
    import datetime
    cutoff = (datetime.datetime.now() - datetime.timedelta(hours=hours)).isoformat()
    with _sqlite3.connect(_PERF_DB) as con:
        rows = con.execute('SELECT timestamp, cpu_percent, ram_percent, disk_percent, battery_percent FROM perf_history WHERE timestamp > ? ORDER BY timestamp ASC', (cutoff,)).fetchall()
    return [{'timestamp': r[0], 'cpu': r[1], 'ram': r[2], 'disk': r[3], 'battery': r[4]} for r in rows]


def spoken_summary(s=None):
    s = s or snapshot()
    parts = [f"CPU {s['cpu']['percent']:.0f}%", f"RAM {s['ram']['percent']:.0f}% ({s['ram']['used_gb']} of {s['ram']['total_gb']} GB)",
             f"disk C {s['disk']['percent']:.0f}% full ({s['disk']['free_gb']} GB free)"]
    if s["gpu"]:
        parts.append(f"GPU {s['gpu']['percent']:.0f}% at {s['gpu']['temp_c']:.0f}°C")
    if s["battery"]:
        b = s["battery"]
        left = f", about {b['mins_left']} min left" if b["mins_left"] else ""
        parts.append(f"battery {b['percent']}% " + ("charging" if b["plugged"] else "on battery" + left))
    top = ", ".join(f"{p['name']} {p['cpu']}%" for p in s["processes"][:3])
    return "; ".join(parts) + f". {s['process_count']} processes running. Busiest: {top}."
