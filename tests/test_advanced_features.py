"""Unit + integration tests for Mission Control, tool runner, event automation, knowledge graph,
diagnostics, predictor, digital twin, app context and the voice-tool dispatcher.
Everything runs on temporary databases/folders; no network, no mouse/keyboard."""
import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

import pytest

import knowledge_graph
import mission_control
import telemetry
import tool_runner
from event_engine import EventEngine, evaluate_condition
from mission_control import MissionEngine, check_verify, plan_waves


# ------------------------------------------------------------------ fixtures --
@pytest.fixture
def kg(tmp_path, monkeypatch):
    g = knowledge_graph.KnowledgeGraph(str(tmp_path / "kg.json"))
    monkeypatch.setattr(knowledge_graph, "_singleton", g)
    return g


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_runner, "FILE_ROOTS", [str(tmp_path)])
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    (tmp_path / "in" / "a.txt").write_text("alpha")
    return tmp_path


@pytest.fixture
def engine(tmp_path, monkeypatch):
    e = MissionEngine(db_path=str(tmp_path / "m.db"), retry_base_delay=0)
    monkeypatch.setattr(mission_control, "engine", e)
    return e


@pytest.fixture
def tele(tmp_path, monkeypatch):
    monkeypatch.setitem(telemetry._state, "db", str(tmp_path / "t.db"))
    monkeypatch.setitem(telemetry._state, "enabled", True)
    return telemetry


def run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------ mission control --
def test_plan_waves_orders_and_detects_cycles():
    subs = [{"id": "a", "title": "A"}, {"id": "b", "title": "B", "depends_on": ["A"]},
            {"id": "c", "title": "C", "depends_on": ["A"]}, {"id": "d", "title": "D", "depends_on": ["b", "c"]}]
    errors, waves = plan_waves(subs)
    assert errors == [] and waves == [["a"], ["b", "c"], ["d"]]
    errors, _ = plan_waves([{"id": "x", "title": "X", "depends_on": ["Y"]}, {"id": "y", "title": "Y", "depends_on": ["X"]}])
    assert any("cycle" in e for e in errors)


def test_validate_rejects_unknown_blocked_and_missing_deps(engine):
    plan = engine.validate_plan([{"title": "s", "tool_name": "send_email"}, {"title": "u", "tool_name": "nope"},
                                 {"title": "d", "tool_name": "get_datetime", "depends_on": ["ghost"]}])
    text = " ".join(plan["errors"])
    assert not plan["ok"] and "approval" in text and "unknown tool 'nope'" in text and "ghost" in text
    with pytest.raises(ValueError):
        engine.create_mission("bad", "", [{"title": "s", "tool_name": "send_email"}])


def test_mission_runs_real_tools_with_shared_context(engine):
    mid = engine.create_mission("t", "", [
        {"title": "now", "tool_name": "get_datetime"},
        {"title": "say", "tool_name": "notify", "args": {"message": "time {{now}}"}, "depends_on": ["now"],
         "verify": {"field": "message", "contains": "time 20"}}])
    run(engine.execute_mission(mid))
    m = engine.get_mission(mid)
    assert m["status"] == "completed" and m["result"]["verified"] is True
    now_out = m["subtasks"][0]["result"]["output"]
    assert m["subtasks"][1]["result"]["output"]["message"] == f"time {now_out}"


def test_parallel_safe_steps_overlap_and_side_effects_do_not(tmp_path):
    active, peak = {"n": 0}, {"par": 0, "seq": 0}

    async def ex(tool, args):
        active["n"] += 1
        key = "par" if tool == "read" else "seq"
        peak[key] = max(peak[key], active["n"])
        await asyncio.sleep(0.05)
        active["n"] -= 1
        return {"ok": True, "output": tool}
    e = MissionEngine(db_path=str(tmp_path / "p.db"), executor=ex, retry_base_delay=0)
    e.custom_parallel_safe = lambda n: n == "read"
    mid = e.create_mission("p", "", [{"title": f"r{i}", "tool_name": "read"} for i in range(4)])
    run(e.execute_mission(mid))
    assert peak["par"] == 4                                             # all four were in flight at once
    active["n"] = 0
    mid2 = e.create_mission("s", "", [{"title": f"w{i}", "tool_name": "write"} for i in range(3)])
    run(e.execute_mission(mid2))
    assert peak["seq"] == 1                                             # never two at once


def test_retry_recovers_and_tool_error_is_not_retried(tmp_path):
    calls = {"flaky": 0, "bad": 0}

    async def ex(tool, args):
        calls[tool] += 1
        if tool == "flaky" and calls["flaky"] < 3:
            raise RuntimeError("transient")
        if tool == "bad":
            raise tool_runner.ToolError("bad args")
        return {"ok": True, "output": "fine"}
    e = MissionEngine(db_path=str(tmp_path / "r.db"), executor=ex, retry_base_delay=0)
    mid = e.create_mission("r", "", [{"title": "f", "tool_name": "flaky", "max_retries": 2},
                                     {"title": "b", "tool_name": "bad", "max_retries": 5},
                                     {"title": "after b", "tool_name": "flaky", "depends_on": ["b"]}])
    run(e.execute_mission(mid))
    m = {s["title"]: s for s in e.get_mission(mid)["subtasks"]}
    assert m["f"]["status"] == "done" and m["f"]["retries"] == 2
    assert m["b"]["status"] == "failed" and calls["bad"] == 1
    assert m["after b"]["status"] == "skipped" and "'b'" in m["after b"]["error"]
    assert e.get_mission(mid)["status"] == "failed"


def test_verification_failure_fails_the_step():
    assert check_verify({"contains": "x"}, "abc") == (False, "output does not contain 'x'")
    assert check_verify({"field": "n", "equals": 2}, {"n": 2})[0]
    assert not check_verify({"min_items": 2}, [1])[0]


def test_pause_resume_continues_from_checkpoint(tmp_path):
    counts = {}

    async def ex(tool, args):
        counts[args["k"]] = counts.get(args["k"], 0) + 1
        await asyncio.sleep(0.02)
        return {"ok": True, "output": args["k"]}
    e = MissionEngine(db_path=str(tmp_path / "pr.db"), executor=ex, retry_base_delay=0)
    mid = e.create_mission("pr", "", [{"title": "one", "tool_name": "x", "args": {"k": 1}},
                                      {"title": "two", "tool_name": "x", "args": {"k": 2}, "depends_on": ["one"]},
                                      {"title": "three", "tool_name": "x", "args": {"k": 3}, "depends_on": ["two"]}])

    async def scenario():
        t = e.start(mid)
        await asyncio.sleep(0.01)
        assert e.pause_mission(mid)
        await t
        assert e.get_mission(mid)["status"] == "paused"
        assert e.resume_mission(mid)
        await e.execute_mission(mid)
    run(scenario())
    assert e.get_mission(mid)["status"] == "completed"
    assert counts == {1: 1, 2: 1, 3: 1}          # nothing ran twice after resume


def test_cancel_stops_running_mission(tmp_path):
    async def ex(tool, args):
        await asyncio.sleep(5)
        return {"ok": True, "output": None}
    e = MissionEngine(db_path=str(tmp_path / "c.db"), executor=ex)
    mid = e.create_mission("c", "", [{"title": "slow", "tool_name": "x"}, {"title": "next", "tool_name": "x", "depends_on": ["slow"]}])

    async def scenario():
        t = e.start(mid)
        await asyncio.sleep(0.05)
        assert e.cancel_mission(mid)
        await asyncio.wait_for(t, 2)
    run(scenario())
    m = e.get_mission(mid)
    assert m["status"] == "cancelled" and all(s["status"] != "done" for s in m["subtasks"])


def test_legacy_mission_rows_still_load(tmp_path):
    db = tmp_path / "old.db"
    e = MissionEngine(db_path=str(db))
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO missions VALUES (?,?,?,?,?,?,?)", ("old", "Old", "", "completed",
                  json.dumps({"subtasks": [{"id": "1", "title": "s", "tool_name": "t", "args": {}, "status": "done",
                                            "depends_on": [], "result": {"stub": True}, "error": None, "retries": 0}]}), "", ""))
    assert e.get_mission("old")["subtasks"][0]["status"] == "done"


# ---------------------------------------------------------------- tool runner --
def test_file_tools_copy_move_and_protect(sandbox):
    a, out = str(sandbox / "in" / "a.txt"), str(sandbox / "out")
    r = run(tool_runner.run_tool("file_copy", {"src": a, "dst": out}))
    assert r["output"]["dst"].endswith("a.txt") and (sandbox / "out" / "a.txt").read_text() == "alpha"
    with pytest.raises(tool_runner.ToolError, match="already exists"):
        run(tool_runner.run_tool("file_copy", {"src": a, "dst": out}))
    run(tool_runner.run_tool("file_move", {"src": str(sandbox / "out" / "a.txt"), "dst": str(sandbox / "b.txt")}))
    assert (sandbox / "b.txt").exists() and not (sandbox / "out" / "a.txt").exists()
    with pytest.raises(tool_runner.ToolError, match="outside"):
        run(tool_runner.run_tool("file_list", {"path": str(sandbox.parent)}))


def test_blocked_and_unknown_tools(sandbox):
    with pytest.raises(tool_runner.ToolError, match="approval"):
        run(tool_runner.run_tool("send_email", {}))
    with pytest.raises(tool_runner.ToolError, match="Unknown"):
        run(tool_runner.run_tool("format_disk", {}))


def test_notify_uses_notifier_and_telemetry_records(tele, monkeypatch):
    got = []
    monkeypatch.setitem(tool_runner._ctx, "notifier", got.append)
    run(tool_runner.run_tool("notify", {"message": "hi"}, source="mission"))
    assert got == [{"title": "Nexora", "message": "hi"}]
    rows = tele.calls()
    assert rows[-1]["tool"] == "notify" and rows[-1]["ok"] == 1 and rows[-1]["source"] == "mission"
    assert tele.latency_stats()["notify"]["count"] == 1


def test_telemetry_off_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setitem(telemetry._state, "db", str(tmp_path / "off.db"))
    monkeypatch.setitem(telemetry._state, "enabled", False)
    telemetry.record("x", "voice", True, 1.0)
    assert telemetry.calls() == []


# -------------------------------------------------------------- event engine --
SNAP = {"cpu": {"percent": 95}, "ram": {"percent": 40}, "disk": {"percent": 50}, "battery": None, "process_count": 200}


def _ee(tmp_path, **kw):
    return EventEngine(db_path=str(tmp_path / "ev.db"), snapshot_fn=lambda: SNAP, process_names_fn=lambda: ["chrome.exe"], **kw)


def test_event_migration_from_old_schema(tmp_path):
    db = tmp_path / "ev.db"
    with sqlite3.connect(db) as c:
        c.execute("CREATE TABLE event_rules (id TEXT PRIMARY KEY, name TEXT, trigger_type TEXT, trigger_config JSON, action_type TEXT, action_config JSON, enabled INTEGER, last_fired TEXT, fire_count INTEGER)")
        c.execute("CREATE TABLE event_history (id TEXT PRIMARY KEY, rule_id TEXT, rule_name TEXT, fired_at TEXT, action_type TEXT, action_config JSON, outcome TEXT)")
        c.execute("INSERT INTO event_rules VALUES ('n','N','manual','{}','notify','{\"message\":\"m\"}',1,NULL,0)")
        c.execute("INSERT INTO event_rules VALUES ('t','T','manual','{}','run_tool','{}',1,NULL,0)")
    e = _ee(tmp_path)
    rules = {r["id"]: r for r in e.list_rules()}
    assert rules["n"]["approved"] is True and rules["t"]["approved"] is False and rules["t"]["needs_approval"]


def test_rule_validation(tmp_path):
    e = _ee(tmp_path)
    with pytest.raises(ValueError) as ex:
        e.add_rule({"trigger_type": "schedule", "trigger_config": {}, "action_type": "run_tool",
                    "action_config": {"tool_name": "send_email"}})
    assert "interval_minutes" in str(ex.value) and "cannot be automated" in str(ex.value)
    with pytest.raises(ValueError, match="folder_path"):
        e.add_rule({"trigger_type": "file_change", "trigger_config": {"folder_path": str(tmp_path / "nope")},
                    "action_type": "notify", "action_config": {"message": "x"}})


def test_unapproved_rule_does_not_execute_until_approved(tmp_path, sandbox):
    e = _ee(tmp_path)
    rid = e.add_rule({"name": "copy", "trigger_type": "manual", "action_type": "run_tool",
                      "action_config": {"tool_name": "file_copy", "args": {"src": str(sandbox / "in" / "a.txt"), "dst": str(sandbox / "out")}}})
    info = e.fire_rule(rid)
    assert info["status"] == "blocked" and not (sandbox / "out" / "a.txt").exists()
    e.approve_rule(rid)
    info = e.fire_rule(rid)
    assert info["status"] == "ok" and (sandbox / "out" / "a.txt").read_text() == "alpha"
    hist = e.get_history()
    assert [h["status"] for h in hist][:2] == ["ok", "blocked"] or {h["status"] for h in hist} == {"ok", "blocked"}


def test_condition_schedule_and_app_health_triggers(tmp_path):
    e = _ee(tmp_path)
    assert evaluate_condition({"metric": "cpu_percent", "op": ">", "value": 90}, SNAP)[0]
    assert not evaluate_condition({"metric": "battery_percent", "op": "<", "value": 20}, SNAP)[0]
    c = e.add_rule({"name": "hot", "trigger_type": "condition", "trigger_config": {"metric": "cpu_percent", "op": ">", "value": 90},
                    "action_type": "notify", "action_config": {"message": "CPU hot"}})
    s = e.add_rule({"name": "every5", "trigger_type": "schedule", "trigger_config": {"interval_minutes": 5},
                    "action_type": "notify", "action_config": {"message": "tick"}})
    d = e.add_rule({"name": "daily", "trigger_type": "schedule", "trigger_config": {"daily_at": "08:00"},
                    "action_type": "notify", "action_config": {"message": "morning"}})
    a = e.add_rule({"name": "chrome gone", "trigger_type": "app_health", "trigger_config": {"process_name": "chrome", "condition": "not_running"},
                    "action_type": "notify", "action_config": {"message": "x"}})
    due = {rd["id"] for rd, _ in e.due_rules(datetime(2026, 1, 1, 9, 0))}
    assert due == {c, s, d}                       # chrome IS running, so `a` is not due
    e.tick(datetime(2026, 1, 1, 9, 0))
    later = {rd["id"] for rd, _ in e.due_rules(datetime.now() + timedelta(minutes=1))}
    assert c not in later and s not in later      # cooldown / interval respected


def test_file_event_pattern_and_debounce(tmp_path):
    e = _ee(tmp_path)
    rid = e.add_rule({"name": "pdfs", "trigger_type": "file_change", "trigger_config": {"folder_path": str(tmp_path), "pattern": "*.pdf", "debounce_s": 3},
                      "action_type": "notify", "action_config": {"message": "new pdf"}})
    assert e.handle_file_event(rid, str(tmp_path / "a.txt"), now=100) is None
    assert e.handle_file_event(rid, str(tmp_path / "a.pdf"), now=100)["status"] == "ok"
    assert e.handle_file_event(rid, str(tmp_path / "b.pdf"), now=101) is None     # debounced
    assert e.handle_file_event(rid, str(tmp_path / "b.pdf"), now=104) is not None


def test_run_mission_action(tmp_path, engine):
    e = _ee(tmp_path)
    rid = e.add_rule({"name": "m", "trigger_type": "manual", "action_type": "run_mission",
                      "action_config": {"title": "auto", "subtasks": [{"title": "t", "tool_name": "get_datetime"}]}}, approved=True)
    info = e.fire_rule(rid)
    assert info["status"] == "ok" and "completed" in info["detail"]


def test_live_file_watcher_fires(tmp_path):
    pytest.importorskip("watchdog")
    e = _ee(tmp_path)
    fired = []
    e.on_event = fired.append
    watch = tmp_path / "watch"
    watch.mkdir()
    e._running = True
    import threading
    t = threading.Thread(target=e._run_file_watcher, daemon=True)
    t.start()
    try:
        e.add_rule({"name": "w", "trigger_type": "file_change", "trigger_config": {"folder_path": str(watch), "debounce_s": 0},
                    "action_type": "notify", "action_config": {"message": "changed"}})
        time.sleep(4)                                # watcher picks up the new rule without restart
        (watch / "new.txt").write_text("x")
        deadline = time.time() + 5
        while not fired and time.time() < deadline:
            time.sleep(0.2)
    finally:
        e._running = False
    assert fired and fired[0]["trigger_source"] == "file_change"


# ----------------------------------------------------------- knowledge graph --
def test_extraction_and_sources(kg):
    r = kg.extract_and_add("Nexora uses SQLite. Ahamed works at ICST.", "memory:1")
    assert ("Nexora", "uses", "SQLite") in r["relations"]
    facts = kg.relations("Nexora")
    assert facts[0]["sources"] == ["memory:1"] and "Nexora uses SQLite" in facts[0]["evidence"]
    kg.extract_and_add("Nexora uses SQLite.", "memory:12")
    assert kg.relations("nexora")[0]["sources"] == ["memory:1", "memory:12"]   # merged, not duplicated
    assert kg.stats()["node_count"] == 4


def test_remove_source_exact_and_prefix(kg):
    kg.extract_and_add("Nexora uses SQLite.", "memory:1")
    kg.extract_and_add("Nexora uses SQLite.", "memory:12")
    kg.remove_source("memory:1")
    assert kg.relations("Nexora")[0]["sources"] == ["memory:12"]   # memory:12 is not a child of memory:1
    kg.extract_and_add("FastAPI depends on Starlette.", "doc:a.pdf#0")
    kg.remove_source("doc:a.pdf")
    assert kg.relations("FastAPI") == []
    kg.remove_source("memory:", prefix=True)
    assert kg.stats()["node_count"] == 0


def test_path_neighbors_context_and_persistence(kg, tmp_path):
    kg.extract_and_add("Ahamed works at ICST. ICST is located in Colombo. Colombo is located in Sri Lanka.", "doc:x#0")
    p = kg.find_path("Ahamed", "Sri Lanka")
    assert p["found"] and p["path"] == ["Ahamed", "ICST", "Colombo", "Sri Lanka"]
    assert [n["name"] for n in kg.neighbors("ICST")] and "Ahamed works_at ICST" not in ""
    ctx = kg.retrieve_context("ICST")
    assert "Ahamed --works_at--> ICST (source: doc:x#0)" in ctx["text"]
    g2 = knowledge_graph.KnowledgeGraph(str(tmp_path / "kg.json"))
    assert g2.find_path("Ahamed", "Sri Lanka")["found"]


def test_legacy_graph_file_loads(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"nodes": [{"id": "a", "name": "A", "type": "concept", "properties": {"source": "memory"}},
                                       {"id": "b", "name": "B", "type": "concept", "properties": {}}],
                             "edges": [{"from": "a", "to": "b", "relation_type": "uses", "properties": {}}]}))
    g = knowledge_graph.KnowledgeGraph(str(p))
    assert g.relations("A")[0]["relation"] == "uses" and g.get_entity("a")["sources"] == ["memory"]


def test_rag_ingest_feeds_graph_with_chunk_sources(kg, tmp_path):
    import rag_engine
    eng = rag_engine.RAGEngine.__new__(rag_engine.RAGEngine)
    eng.db_path, eng._embedder, eng._has_embeddings = str(tmp_path / "kb.db"), None, False
    eng._init_db()
    res = eng.ingest_file("notes.txt", b"Nexora uses SQLite for memory.", "txt")
    assert res["ok"] and res["graph"]["relations"] == 1
    assert kg.relations("Nexora")[0]["sources"] == ["doc:notes.txt#0"]
    eng.delete_document("notes.txt")
    assert kg.relations("Nexora") == []


def test_memory_feeds_graph_with_memory_ids(kg, tmp_path):
    import copy
    import memory
    eng = copy.copy(memory.memory)
    eng.db_path = str(tmp_path / "mem.db")
    eng._init_db()
    mid = eng.save_memory("Ahamed works at ICST.")["id"]
    assert kg.relations("Ahamed")[0]["sources"] == [f"memory:{mid}"]
    eng.delete_memory(mid)
    assert kg.relations("Ahamed") == []


# ---------------------------------------------------------------- diagnostics --
class _Resp:
    def __init__(self, code, data):
        self.status_code, self._d = code, data

    def json(self):
        return self._d


class _Http:
    def __init__(self, models):
        self.models = models

    def get(self, url, **kw):
        if "generativelanguage" in url:
            assert kw["headers"]["x-goog-api-key"] == "k" and "key=" not in url   # key never in the URL
            return _Resp(200, {"models": [{"name": m} for m in self.models]})
        raise ConnectionError("offline")


def test_model_connectivity(monkeypatch):
    from diagnostics import DiagnosticsEngine, _live_model_name
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert DiagnosticsEngine().check_model_connectivity()["status"] == "skipped"
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    ok = DiagnosticsEngine(http=_Http([_live_model_name()])).check_model_connectivity()
    assert ok["status"] == "ok" and ok["live_model_listed"] is True
    assert DiagnosticsEngine(http=_Http(["models/other"])).check_model_connectivity()["live_model_listed"] is False


def test_bottlenecks_and_recommendations():
    from diagnostics import DiagnosticsEngine
    d = DiagnosticsEngine()
    hist = [{"cpu": 97, "ram": 50} for _ in range(10)]
    snap = {"disk": {"percent": 95, "free_gb": 10}, "processes": [{"name": "hog.exe", "pid": 1, "cpu": 80, "mem_mb": 100}]}
    tools = {"sri_lanka_news": {"count": 10, "fail_rate": 0.5, "median_ms": 9000, "p95_ms": 12000}}
    res = d.detect_bottlenecks(hist, snap, tools, {"big.db": {"size_kb": 200 * 1024}})
    areas = {f["area"] for f in res["findings"]}
    assert areas == {"cpu", "disk", "process", "tool_latency", "tool_errors", "database"}
    quiet = d.detect_bottlenecks([{"cpu": 5, "ram": 30}] * 10, {"disk": {"percent": 40, "free_gb": 200}, "processes": []}, {}, {})
    assert quiet["findings"] == []
    rec = DiagnosticsEngine.recommend({"internet": True, "gemini_configured": True, "omniroute": True}, {"status": "ok"},
                                      {}, {}, {"total_errors": 0}, res)
    assert any("hog.exe" in r for r in rec)


def test_error_analysis_and_db_integrity(tmp_path):
    from diagnostics import DiagnosticsEngine
    log = tmp_path / "apa_launch.log"
    log.write_text("[1] [Python]: [EMAIL] login failed: AuthenticationError\n[1] fine\n[1] [ADA DEBUG] [ERR] Connection Error: TimeoutError\n")
    (tmp_path / "bad.db").write_bytes(b"this is not sqlite" * 100)
    with sqlite3.connect(tmp_path / "good.db") as c:
        c.execute("CREATE TABLE t (x)")
    d = DiagnosticsEngine(backend_dir=str(tmp_path))
    a = d.analyze_errors(str(log))
    assert a["total_errors"] == 2 and dict(a["top_errors"]) == {"AuthenticationError": 1, "TimeoutError": 1}
    db = d.check_database_health()
    assert db["good.db"]["ok"] and not db["bad.db"]["ok"]


# ------------------------------------------------------------------ predictor --
def _calls(days, hour, tool, minute=0, start=datetime(2026, 10, 1)):
    return [{"ts": (start + timedelta(days=i, hours=hour, minutes=minute)).timestamp(), "tool": tool, "source": "voice", "ok": 1}
            for i in range(days)]


def test_predictor_refuses_without_data(tele):
    import predictor
    res = predictor.recommendations([])
    assert res["status"] == "insufficient_data" and res["recommendations"] == [] and "need 50" in res["message"]
    assert predictor.backtest(_calls(3, 8, "x"))["status"] == "insufficient_data"


def test_predictor_finds_routine_and_sequence(tele):
    import predictor
    calls = (_calls(30, 7, "sri_lanka_weather") + _calls(30, 7, "sri_lanka_news", minute=1)
             + _calls(30, 15, "read_emails") + _calls(30, 21, "get_system_stats"))
    res = predictor.recommendations(calls)
    assert res["status"] == "ok"
    routine = [r for r in res["recommendations"] if r["type"] == "routine" and r["tool"] == "sri_lanka_weather"][0]
    assert routine["window"] == "06:00-08:00" and "30 of 30" in routine["evidence"]
    seq = [r for r in res["recommendations"] if r["type"] == "sequence"][0]
    assert seq["tools"] == ["sri_lanka_weather", "sri_lanka_news"]
    bt = predictor.backtest(calls)
    assert bt["status"] == "ok" and bt["hour_model"]["top1"] > bt["baseline"]["top1"] and bt["hour_model"]["beats_baseline"]


# ---------------------------------------------------------------- digital twin --
def test_file_preview_executes_nothing(sandbox):
    import digital_twin
    (sandbox / "out" / "a.txt").write_text("old")
    r = digital_twin.preview_file_operation("copy", str(sandbox / "in"), str(sandbox / "out"), pattern="*.txt")
    assert r["mode"] == "SIMULATION" and r["executed"] is False and r["files"] == 1 and r["total_bytes"] == 5
    assert not r["would_succeed"] and "destination exists" in r["issues"][0]
    assert (sandbox / "out" / "a.txt").read_text() == "old"
    d = digital_twin.preview_file_operation("delete", str(sandbox / "in" / "a.txt"))
    assert d["would_succeed"] and (sandbox / "in" / "a.txt").exists()
    assert not digital_twin.preview_file_operation("copy", str(sandbox / "missing"), str(sandbox))["would_succeed"]
    ok = digital_twin.preview_file_operation("move", str(sandbox / "in" / "a.txt"), str(sandbox / "moved.txt"))
    assert ok["would_succeed"] and ok["executable_as"]["tool_name"] == "file_move" and (sandbox / "in" / "a.txt").exists()


def test_simulate_mission_failure_scenarios(sandbox, tele):
    import digital_twin
    res = run(digital_twin.simulate_mission([
        {"title": "copy", "tool_name": "file_copy", "args": {"src": str(sandbox / "in" / "a.txt"), "dst": str(sandbox / "out")}},
        {"title": "list", "tool_name": "file_list", "args": {"path": str(sandbox / "out")}, "depends_on": ["copy"]},
        {"title": "time", "tool_name": "get_datetime"}]))
    assert res["valid"] and res["executed"] is False and not (sandbox / "out" / "a.txt").exists()
    scen = {s["if_fails"]: s["would_skip"] for s in res["failure_scenarios"]}
    assert scen == {"copy": ["list"], "list": [], "time": []}
    assert res["side_effect_steps"] == ["copy"] and res["happy_path"]["status"] == "completed"
    assert res["estimate"]["estimated_ms"] is None and set(res["estimate"]["no_latency_data_for"]) == {"copy", "list", "time"}


def test_dry_run_rule(tmp_path):
    import digital_twin
    e = _ee(tmp_path)
    rid = e.add_rule({"name": "hot", "trigger_type": "condition", "trigger_config": {"metric": "cpu_percent", "op": ">", "value": 90},
                      "action_type": "run_tool", "action_config": {"tool_name": "get_datetime"}})
    r = run(digital_twin.dry_run_rule(e, rid))
    assert r["would_fire_now"] is True and r["approved"] is False and r["action_would_execute"] is False
    assert e.get_history() == [] and e.list_rules()[0]["fire_count"] == 0     # dry run fired nothing


# ------------------------------------------------- app context / voice tools --
def test_app_context_tracker_records_switches():
    import app_context
    seq = iter([{"title": "Inbox - Gmail", "process": "chrome.exe", "pid": 1}] * 2 + [{"title": "Doc", "process": "WINWORD.EXE", "pid": 2}])
    t = app_context.AppContextTracker(probe=lambda: next(seq))
    t.sample(); t.sample(); t.sample()
    assert [r["process"] for r in t.recent()] == ["WINWORD.EXE", "chrome.exe"]


def test_voice_tools_cannot_self_approve_and_dry_run_does_not_execute(tmp_path, engine, monkeypatch, kg):
    import assistant_tools
    e = _ee(tmp_path)
    monkeypatch.setitem(tool_runner._ctx, "event_engine", e)
    msg = run(assistant_tools.handle("manage_events", {"action": "add", "rule_name": "x", "trigger_type": "manual",
                                                       "action_type": "run_tool", "action_config": '{"tool_name": "get_datetime"}'}))
    assert "approves" in msg and e.list_rules()[0]["needs_approval"]
    out = run(assistant_tools.handle("create_mission", {"title": "t", "dry_run": True,
                                                        "subtasks": [{"title": "a", "tool_name": "get_datetime"}]}))
    assert out.startswith("SIMULATION ONLY") and engine.list_missions() == []
    bad = run(assistant_tools.handle("create_mission", {"title": "t", "subtasks": [{"title": "a", "tool_name": "send_email"}]}))
    assert "could not start" in bad
    run(assistant_tools.handle("knowledge_graph", {"action": "add_fact", "query": "Nexora", "relation": "uses", "entity_b": "Gemini"}))
    assert "Nexora --uses--> Gemini (source: voice)" in run(assistant_tools.handle("knowledge_graph", {"action": "context", "query": "Nexora"}))


def test_every_declared_tool_is_dispatched():
    """Regression: manage_events / knowledge_graph / run_diagnostics were declared to Gemini but never handled."""
    import ast
    import tools
    src = open(os.path.join(os.path.dirname(tools.__file__), "ada.py"), encoding="utf-8").read()
    import assistant_tools
    from computer_bridge import ALLOWED_COMPUTER_TOOLS
    declared = {d["name"] for d in tools.tools_list[0]["function_declarations"]}
    handled = set(assistant_tools.HANDLED) | set(ALLOWED_COMPUTER_TOOLS)
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in declared:
            handled.add(node.value)
    assert declared - handled == set()


# ----------------------------------------------------------------- evaluation --
def test_evaluation_offline_suites_run():
    from evaluation import run as ev
    kg_res = ev.suite_kg()
    assert kg_res["cases"] == 15 and 0 <= kg_res["relations"]["f1"] <= 1
    m = ev.suite_missions(repeats=1)
    assert m["runs"] == 9 and m["result_correctness"] == 1.0, m["per_case"]
    assert "eval_flaky" not in tool_runner.REGISTRY
