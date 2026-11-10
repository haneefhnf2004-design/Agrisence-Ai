"""Tests for the daily-briefing trigger, the OmniRoute file guard and the hardware snapshot (no network needed)."""
from datetime import date, datetime

import lk_weather
import omniroute_client
import system_monitor


def test_daily_briefing_fires_once_per_day_after_7am(tmp_path, monkeypatch):
    monkeypatch.setattr(lk_weather, "STATE_FILE", str(tmp_path / "state.json"))
    assert not lk_weather.daily_due(datetime(2026, 11, 10, 6, 59))   # too early
    assert lk_weather.daily_due(datetime(2026, 11, 10, 7, 0))        # first run of the day
    lk_weather.mark_daily_done(date(2026, 11, 10))
    assert not lk_weather.daily_due(datetime(2026, 11, 10, 18, 0))   # already done today
    assert lk_weather.daily_due(datetime(2026, 11, 11, 7, 30))       # next day


def test_omniroute_files_cannot_escape_build_folder():
    text = ("=== FILE: app/main.py ===\nprint(1)\n=== END FILE ===\n"
            "=== FILE: ../../Windows/evil.bat ===\nx\n=== END FILE ===\n"
            "=== FILE: C:/evil.txt ===\nx\n=== END FILE ===\n"
            "=== FILE: /etc/passwd ===\nx\n=== END FILE ===\n")
    files = omniroute_client.parse_files(text)
    assert [str(p).replace("\\", "/") for p, _ in files] == ["app/main.py"]
    assert files[0][1] == "print(1)\n"


def test_system_snapshot_has_task_manager_fields():
    s = system_monitor.snapshot()
    assert 0 <= s["cpu"]["percent"] <= 100 and 0 <= s["ram"]["percent"] <= 100
    assert s["process_count"] > 0 and len(s["processes"]) <= 12
    assert all({"pid", "name", "cpu", "mem_mb"} <= set(p) for p in s["processes"])
    assert "CPU" in system_monitor.spoken_summary(s)
