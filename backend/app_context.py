"""
Application context tracking for cross-application workflows.

- active_window(): the real foreground window (title, process name, pid) via Win32.
- AppContextTracker: polls the foreground window and keeps a short history of app
  switches, so Nexora knows which app the user (or a workflow) was just in.
- wait_for_window(): polls the window list until a window whose title contains the
  given text appears; workflows use it to VERIFY that "open X" actually worked.

Reuses computer_bridge's window enumeration (pygetwindow). Windows only; on other
platforms the functions return None / empty results instead of guessing.
"""
import sys
import threading
import time
from collections import deque


def active_window():
    """{'title', 'process', 'pid'} for the foreground window, or None."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        proc = ""
        try:
            import psutil
            proc = psutil.Process(pid.value).name()
        except Exception:
            pass
        return {"title": buf.value, "process": proc, "pid": pid.value}
    except Exception:
        return None


def list_window_titles():
    try:
        from computer_bridge import _windows
        return [w.title for w in _windows()]
    except Exception:
        return []


def wait_for_window(title_part: str, timeout_s: float = 10.0, poll_s: float = 0.5):
    """Return the first matching window title, or None after timeout. Real polling, no assumptions."""
    needle = (title_part or "").lower().strip()
    if not needle:
        return None
    deadline = time.time() + max(0.0, timeout_s)
    while True:
        for t in list_window_titles():
            if needle in t.lower():
                return t
        if time.time() >= deadline:
            return None
        time.sleep(poll_s)


class AppContextTracker:
    """Background poller: records each change of foreground app (title + process)."""

    def __init__(self, poll_s: float = 2.0, history: int = 50, probe=active_window):
        self.poll_s = poll_s
        self._probe = probe
        self._history = deque(maxlen=history)
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def sample(self):
        """Take one reading; append it if the foreground app/title changed. Returns the reading."""
        w = self._probe()
        if not w:
            return None
        with self._lock:
            last = self._history[-1] if self._history else None
            if not last or last["title"] != w["title"] or last["process"] != w["process"]:
                self._history.append({**w, "since": time.time()})
        return w

    def _loop(self):
        while self._running:
            try:
                self.sample()
            except Exception as e:
                print(f"[APPCTX] sample failed: {e}")
            time.sleep(self.poll_s)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="AppContextTracker")
        self._thread.start()

    def stop(self):
        self._running = False

    def recent(self, n: int = 10) -> list:
        with self._lock:
            return list(self._history)[-n:][::-1]

    def current(self):
        with self._lock:
            return dict(self._history[-1]) if self._history else None


tracker = AppContextTracker()
