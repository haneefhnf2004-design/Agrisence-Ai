"""
Windows system control: the parts computer_bridge / system_monitor did not already cover.

Every function returns a plain dict with real measured values, or raises ValueError (bad input,
refused) / RuntimeError (Windows said no). Nothing here is simulated.

Already elsewhere (not duplicated): opening apps/files (computer_open), mouse/keyboard
(computer_*), CPU/RAM/disk/GPU/top-process stats (system_monitor.snapshot).
"""
import ctypes
import os
import re
import socket
import subprocess
import tempfile
import time

import psutil

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# Processes Nexora must never close or kill.
PROTECTED = {"system", "system idle process", "registry", "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
             "services.exe", "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "memory compression",
             "sihost.exe", "ctfmon.exe", "audiodg.exe", "spoolsv.exe", "msmpeng.exe", "securityhealthservice.exe"}

SETTINGS_PAGES = {
    "display": "ms-settings:display", "sound": "ms-settings:sound", "bluetooth": "ms-settings:bluetooth",
    "wifi": "ms-settings:network-wifi", "network": "ms-settings:network-status", "battery": "ms-settings:batterysaver",
    "power": "ms-settings:powersleep", "apps": "ms-settings:appsfeatures", "privacy": "ms-settings:privacy",
    "microphone": "ms-settings:privacy-microphone", "camera": "ms-settings:privacy-webcam",
    "update": "ms-settings:windowsupdate", "storage": "ms-settings:storagesense",
    "notifications": "ms-settings:notifications", "about": "ms-settings:about", "personalization": "ms-settings:personalization",
}


def _ps(cmd, timeout=15):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=timeout, creationflags=_NO_WINDOW)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


# ------------------------------------------------------------------ volume ----
def _endpoint():
    import comtypes
    comtypes.CoInitialize()
    from pycaw.pycaw import AudioUtilities
    dev = AudioUtilities.GetSpeakers()
    ep = getattr(dev, "EndpointVolume", None)
    if ep is None:  # older pycaw
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import IAudioEndpointVolume
        ep = cast(dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None), POINTER(IAudioEndpointVolume))
    return ep


def volume_get():
    ep = _endpoint()
    return {"volume": round(ep.GetMasterVolumeLevelScalar() * 100), "muted": bool(ep.GetMute())}


def volume_set(level):
    try:
        level = float(level)
    except (TypeError, ValueError):
        raise ValueError("volume level must be a number 0-100")
    if not 0 <= level <= 100:
        raise ValueError("volume level must be between 0 and 100")
    ep = _endpoint()
    ep.SetMasterVolumeLevelScalar(level / 100.0, None)
    if level > 0 and ep.GetMute():
        ep.SetMute(0, None)
    return volume_get()


def volume_mute(mute=True):
    ep = _endpoint()
    ep.SetMute(1 if mute else 0, None)
    return volume_get()


# -------------------------------------------------------------- brightness ----
def brightness_get():
    code, out, err = _ps("(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness -ErrorAction Stop | "
                         "Select-Object -First 1).CurrentBrightness")
    if code != 0 or not out.strip().isdigit():
        raise RuntimeError("Brightness is not controllable on this display (external monitors usually aren't).")
    return {"brightness": int(out.strip())}


def brightness_set(level):
    try:
        level = int(float(level))
    except (TypeError, ValueError):
        raise ValueError("brightness must be a number 0-100")
    if not 0 <= level <= 100:
        raise ValueError("brightness must be between 0 and 100")
    code, out, err = _ps(f"(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods -ErrorAction Stop | "
                         f"Select-Object -First 1) | Invoke-CimMethod -MethodName WmiSetBrightness "
                         f"-Arguments @{{Timeout=1; Brightness={level}}} | Out-Null")
    if code != 0:
        raise RuntimeError("Brightness is not controllable on this display.")
    return brightness_get()


# ------------------------------------------------------------------- power ----
def lock():
    if not ctypes.windll.user32.LockWorkStation():
        raise RuntimeError("Windows refused to lock the workstation.")
    return {"locked": True}


def sleep():
    subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=_NO_WINDOW)
    return {"sleeping": True}


def shutdown(restart=False, delay=60):
    delay = max(10, min(int(delay or 60), 3600))
    flag = "/r" if restart else "/s"
    r = subprocess.run(["shutdown", flag, "/t", str(delay), "/c", "Nexora: scheduled by voice command. Run 'cancel shutdown' to stop."],
                       capture_output=True, text=True, creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "shutdown failed").strip())
    return {"scheduled": "restart" if restart else "shutdown", "in_seconds": delay, "cancel_with": "cancel_shutdown"}


def cancel_shutdown():
    r = subprocess.run(["shutdown", "/a"], capture_output=True, text=True, creationflags=_NO_WINDOW)
    return {"cancelled": r.returncode == 0,
            "detail": "" if r.returncode == 0 else "No shutdown or restart was scheduled."}


# --------------------------------------------------------------- processes ----
def list_processes(sort="memory", limit=15, name=None):
    limit = max(1, min(int(limit or 15), 50))
    procs = []
    for p in psutil.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
        try:
            if name and name.lower() not in (p.info["name"] or "").lower():
                continue
            procs.append({"pid": p.info["pid"], "name": p.info["name"],
                          "memory_mb": round((p.info["memory_info"].rss if p.info["memory_info"] else 0) / 1048576, 1),
                          "cpu_percent": p.info["cpu_percent"] or 0.0})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    key = "cpu_percent" if sort == "cpu" else "memory_mb"
    procs.sort(key=lambda x: x[key], reverse=True)
    return {"count": len(procs), "processes": procs[:limit]}


def _match_processes(target):
    if target is None or str(target).strip() == "":
        raise ValueError("Give a process name (e.g. 'notepad') or a PID.")
    t = str(target).strip()
    me = os.getpid()
    out = []
    if t.isdigit():
        try:
            out = [psutil.Process(int(t))]
        except psutil.NoSuchProcess:
            raise ValueError(f"No process with PID {t}.")
    else:
        want = t.lower().removesuffix(".exe")
        for p in psutil.process_iter(["name"]):
            n = (p.info["name"] or "").lower()
            if n.removesuffix(".exe") == want:
                out.append(p)
        if not out:
            raise ValueError(f"No running process called '{t}'.")
    for p in out:
        n = (p.name() or "").lower()
        if n in PROTECTED or p.pid in (0, 4, me, os.getppid()):
            raise ValueError(f"'{p.name()}' (PID {p.pid}) is protected; Nexora will not close it.")
    return out


def close_app(target):
    """Ask the app's windows to close (like clicking X), so it can save. Verifies it actually exited."""
    procs = _match_processes(target)
    pids = {p.pid for p in procs}
    user32 = ctypes.windll.user32
    WM_CLOSE = 0x0010
    hwnds = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def _cb(hwnd, _):
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.IsWindowVisible(hwnd):
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    if not hwnds:
        raise RuntimeError(f"'{target}' has no open window to close. Use kill_process to force it.")
    for h in hwnds:
        user32.PostMessageW(h, WM_CLOSE, 0, 0)
    gone, alive = psutil.wait_procs(procs, timeout=5)
    return {"closed": [p.pid for p in gone], "still_running": [p.pid for p in alive],
            "note": "Still running usually means it is asking to save changes." if alive else ""}


def kill_process(target):
    procs = _match_processes(target)
    for p in procs:
        try:
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=4)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    gone2, alive2 = psutil.wait_procs(alive, timeout=3)
    return {"killed": [p.pid for p in gone + gone2], "failed": [p.pid for p in alive2]}


# --------------------------------------------------------------- clipboard ----
def clipboard_get():
    import pyperclip
    text = pyperclip.paste() or ""
    return {"text": text[:3000], "length": len(text), "truncated": len(text) > 3000}


def clipboard_set(text):
    import pyperclip
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    pyperclip.copy(text)
    if pyperclip.paste() != text:
        raise RuntimeError("Clipboard did not accept the text.")
    return {"copied_chars": len(text)}


# ----------------------------------------------------------------- network ----
def internet_check():
    results = {}
    for host, port in (("1.1.1.1", 443), ("8.8.8.8", 53)):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=2.5):
                results[host] = round((time.perf_counter() - t0) * 1000)
        except OSError:
            results[host] = None
    try:
        socket.getaddrinfo("www.google.com", 443)
        dns = True
    except OSError:
        dns = False
    online = any(v is not None for v in results.values())
    lat = [v for v in results.values() if v is not None]
    return {"online": online and dns, "tcp_reachable": online, "dns_ok": dns,
            "latency_ms": min(lat) if lat else None}


def wifi_status():
    try:
        out = subprocess.run(["netsh", "wlan", "show", "interfaces"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=8, creationflags=_NO_WINDOW).stdout or ""
    except Exception as e:
        return {"available": False, "detail": str(e)}
    info = {}
    for line in out.splitlines():
        m = re.match(r"\s*([^:]+?)\s*:\s*(.+)$", line)
        if m:
            info.setdefault(m.group(1).strip().lower(), m.group(2).strip())
    if not info:
        return {"available": False, "detail": "No Wi-Fi adapter found."}
    return {"available": True, "state": info.get("state"), "ssid": info.get("ssid"),
            "signal": info.get("signal"), "radio": info.get("radio type"),
            "receive_mbps": info.get("receive rate (mbps)"), "transmit_mbps": info.get("transmit rate (mbps)")}


def network_info():
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    ifaces = []
    for name, lst in addrs.items():
        st = stats.get(name)
        if not st or not st.isup:
            continue
        ipv4 = [a.address for a in lst if a.family == socket.AF_INET and not a.address.startswith("127.")]
        if ipv4:
            ifaces.append({"name": name, "ipv4": ipv4, "speed_mbps": st.speed})
    return {"interfaces": ifaces, "wifi": wifi_status(), "internet": internet_check()}


def net_throughput(seconds=1.0):
    """Current traffic through the adapters (not an internet speed test)."""
    seconds = max(0.5, min(float(seconds or 1), 5))
    a = psutil.net_io_counters()
    time.sleep(seconds)
    b = psutil.net_io_counters()
    return {"download_kbps": round((b.bytes_recv - a.bytes_recv) / 1024 / seconds, 1),
            "upload_kbps": round((b.bytes_sent - a.bytes_sent) / 1024 / seconds, 1),
            "sample_seconds": seconds,
            "note": "Live traffic right now, not a speed test."}


# ----------------------------------------------------------------- battery ----
def battery():
    b = psutil.sensors_battery()
    if b is None:
        return {"present": False}
    res = {"present": True, "percent": round(b.percent), "plugged_in": bool(b.power_plugged),
           "minutes_left": None if b.secsleft in (psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN) or b.secsleft < 0
           else round(b.secsleft / 60)}
    res.update(battery_health())
    return res


def battery_health():
    """Design vs full-charge capacity from Windows' own battery report."""
    path = os.path.join(tempfile.gettempdir(), "nexora_battery.xml")
    try:
        subprocess.run(["powercfg", "/batteryreport", "/xml", "/output", path], capture_output=True,
                       timeout=20, creationflags=_NO_WINDOW)
        with open(path, encoding="utf-8", errors="ignore") as f:
            xml = f.read()
        design = re.search(r"<DesignCapacity>(\d+)</DesignCapacity>", xml)
        full = re.search(r"<FullChargeCapacity>(\d+)</FullChargeCapacity>", xml)
        cycles = re.search(r"<CycleCount>(\d+)</CycleCount>", xml)
        if design and full and int(design.group(1)) > 0:
            d, fc = int(design.group(1)), int(full.group(1))
            return {"design_mwh": d, "full_charge_mwh": fc, "health_percent": round(fc * 100 / d, 1),
                    "cycle_count": int(cycles.group(1)) if cycles else None}
    except Exception:
        pass
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return {"health_percent": None}


# --------------------------------------------------------------- bluetooth ----
def bluetooth_status():
    code, out, err = _ps("Get-PnpDevice -Class Bluetooth -PresentOnly -ErrorAction SilentlyContinue | "
                         "Select-Object Status,FriendlyName | ConvertTo-Json -Compress", timeout=20)
    if not out:
        return {"adapter": False, "devices": []}
    import json
    items = json.loads(out)
    items = items if isinstance(items, list) else [items]
    radios = [i for i in items if re.search(r"(?i)adapter|radio|wireless bluetooth|intel.*bluetooth|realtek.*bluetooth", i.get("FriendlyName") or "")]
    return {"adapter": bool(radios) or bool(items),
            "adapter_ok": any(i.get("Status") == "OK" for i in radios) if radios else None,
            "devices": [{"name": i.get("FriendlyName"), "status": i.get("Status")} for i in items][:25]}


# ---------------------------------------------------------------- settings ----
def open_settings(page):
    key = str(page or "").strip().lower()
    uri = SETTINGS_PAGES.get(key)
    if not uri:
        raise ValueError(f"Unknown settings page '{page}'. Options: {', '.join(sorted(SETTINGS_PAGES))}")
    os.startfile(uri)
    return {"opened": key}


def system_info():
    import platform
    vm = psutil.virtual_memory()
    boot = psutil.boot_time()
    info = {"os": f"{platform.system()} {platform.release()} ({platform.version()})",
            "machine": platform.machine(), "cpu": platform.processor(),
            "cores_physical": psutil.cpu_count(logical=False), "cores_logical": psutil.cpu_count(),
            "ram_total_gb": round(vm.total / 1073741824, 1),
            "uptime_hours": round((time.time() - boot) / 3600, 1)}
    try:
        from system_monitor import _gpu
        g = _gpu()
        if g:
            info["gpu"] = g
    except Exception:
        pass
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            u = psutil.disk_usage(part.mountpoint)
            disks.append({"drive": part.device, "total_gb": round(u.total / 1073741824, 1),
                          "free_gb": round(u.free / 1073741824, 1), "used_percent": u.percent})
        except (PermissionError, OSError):
            continue
    info["disks"] = disks
    return info


ACTIONS = {
    "volume_get": lambda a: volume_get(),
    "volume_set": lambda a: volume_set(a.get("value")),
    "mute": lambda a: volume_mute(True),
    "unmute": lambda a: volume_mute(False),
    "brightness_get": lambda a: brightness_get(),
    "brightness_set": lambda a: brightness_set(a.get("value")),
    "lock": lambda a: lock(),
    "sleep": lambda a: sleep(),
    "shutdown": lambda a: shutdown(False, a.get("value") or 60),
    "restart": lambda a: shutdown(True, a.get("value") or 60),
    "cancel_shutdown": lambda a: cancel_shutdown(),
    "list_processes": lambda a: list_processes(a.get("sort", "memory"), a.get("limit", 15), a.get("target")),
    "close_app": lambda a: close_app(a.get("target")),
    "kill_process": lambda a: kill_process(a.get("target")),
    "clipboard_get": lambda a: clipboard_get(),
    "clipboard_set": lambda a: clipboard_set(a.get("text")),
    "internet_check": lambda a: internet_check(),
    "network_info": lambda a: network_info(),
    "wifi_status": lambda a: wifi_status(),
    "net_throughput": lambda a: net_throughput(a.get("value") or 1),
    "battery": lambda a: battery(),
    "bluetooth_status": lambda a: bluetooth_status(),
    "open_settings": lambda a: open_settings(a.get("target")),
    "system_info": lambda a: system_info(),
}


def run(action, args=None):
    fn = ACTIONS.get(str(action or "").strip().lower())
    if fn is None:
        raise ValueError(f"Unknown action '{action}'. Options: {', '.join(ACTIONS)}")
    return fn(dict(args or {}))
