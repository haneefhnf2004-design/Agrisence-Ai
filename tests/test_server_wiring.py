"""Integration: drive the real socket handlers in server.py in-process (no network, no UI),
with temporary mission/event/graph stores, and check what they emit."""
import asyncio

import pytest


@pytest.fixture(scope="module")
def server_mod():
    import server
    return server


@pytest.fixture
def wired(server_mod, tmp_path, monkeypatch):
    import knowledge_graph, mission_control, tool_runner
    from event_engine import EventEngine
    from mission_control import MissionEngine
    emitted = []

    async def fake_emit(event, data=None, room=None, **kw):
        emitted.append((event, data))
    monkeypatch.setattr(server_mod.sio, "emit", fake_emit)
    me = MissionEngine(db_path=str(tmp_path / "m.db"), retry_base_delay=0)
    me.on_progress = server_mod._on_mission_progress
    monkeypatch.setattr(mission_control, "engine", me)
    monkeypatch.setattr(server_mod, "mission_engine", me)
    ee = EventEngine(db_path=str(tmp_path / "e.db"))
    monkeypatch.setattr(server_mod, "event_engine", ee)
    monkeypatch.setitem(tool_runner._ctx, "event_engine", ee)
    g = knowledge_graph.KnowledgeGraph(str(tmp_path / "kg.json"))
    monkeypatch.setattr(knowledge_graph, "_singleton", g)
    monkeypatch.setattr(server_mod, "kg_engine", g)
    return server_mod, emitted, me, ee, g


def _last(emitted, name):
    return [d for e, d in emitted if e == name][-1]


def test_mission_socket_flow(wired):
    server, emitted, me, _, _ = wired

    async def go():
        server.MAIN_LOOP = asyncio.get_running_loop()
        await server.create_mission("sid", {"title": "t", "subtasks": [{"title": "a", "tool_name": "get_datetime"}]})
        await asyncio.sleep(0.3)
        await server.create_mission("sid", {"title": "bad", "subtasks": [{"title": "a", "tool_name": "send_email"}]})
        await server.get_missions("sid")
    asyncio.run(go())
    assert any(e == "mission_progress" for e, _ in emitted)
    assert "approval" in _last(emitted, "mission_error")["error"]
    assert _last(emitted, "missions_list")["missions"][0]["status"] == "completed"


def test_event_socket_flow(wired, tmp_path):
    server, emitted, _, ee, _ = wired

    async def go():
        await server.add_event_rule("sid", {"name": "bad", "trigger_type": "schedule", "action_type": "notify", "action_config": {}})
        await server.add_event_rule("sid", {"name": "t", "trigger_type": "manual", "action_type": "run_tool",
                                            "action_config": {"tool_name": "get_datetime"}})
        await server.dry_run_event_rule("sid", {"rule_id": ee.list_rules()[0]["id"]})
        await server.fire_event_rule("sid", {"rule_id": ee.list_rules()[0]["id"]})
    asyncio.run(go())
    assert "interval_minutes" in _last(emitted, "event_rule_error")["error"]
    rules = _last(emitted, "event_rules_list")["rules"]
    assert rules[0]["approved"] is True                      # created by the user in the UI
    assert _last(emitted, "simulation_result")["executed"] is False
    assert _last(emitted, "event_history")["history"][0]["status"] == "ok"


def test_kg_simulate_recommend_eval_handlers(wired):
    server, emitted, _, _, g = wired
    g.extract_and_add("Ahamed works at ICST. ICST is located in Colombo.", "memory:1")

    async def go():
        await server.kg_relations("sid", {"name": "ICST"})
        await server.kg_path("sid", {"a": "Ahamed", "b": "Colombo"})
        await server.simulate("sid", {"target": "mission", "subtasks": [{"title": "a", "tool_name": "get_datetime"}]})
        await server.get_recommendations("sid")
        await server.get_eval_results("sid")
        await server.get_app_context("sid")
    asyncio.run(go())
    assert len(_last(emitted, "kg_relations")["facts"]) == 2
    assert _last(emitted, "kg_path")["path"] == ["Ahamed", "ICST", "Colombo"]
    assert _last(emitted, "simulation_result")["mode"] == "SIMULATION"
    assert _last(emitted, "recommendations")["status"] in ("ok", "insufficient_data")
    assert "results" in _last(emitted, "eval_results")
    assert "recent" in _last(emitted, "app_context")
