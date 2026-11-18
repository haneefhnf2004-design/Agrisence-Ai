"""
Voice-assistant handlers for the advanced tools (missions, events, knowledge graph,
diagnostics, digital twin, recommendations, app context).

ada.py calls handle(name, args, push_widget) for every name in HANDLED and sends the
returned string back to Gemini as the function response. Kept separate from ada.py
so it can be tested without the audio stack.
"""
import asyncio
import json

import tool_runner

HANDLED = ("create_mission", "manage_missions", "manage_events", "knowledge_graph", "run_diagnostics",
           "simulate", "get_recommendations", "get_app_context")


def _j(v, default):
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            raise ValueError(f"not valid JSON: {v[:80]}")
    return default


def _short(obj, n=1800):
    s = obj if isinstance(obj, str) else json.dumps(obj, default=str)
    return s if len(s) <= n else s[:n] + " ...[truncated]"


async def handle(name: str, args: dict, push_widget=lambda t, d: None) -> str:
    try:
        return await _handle(name, dict(args or {}), push_widget)
    except ValueError as e:
        return f"That didn't work: {e}"


async def _handle(name, args, push):
    if name == "create_mission":
        from mission_control import engine
        subtasks = args.get("subtasks") or []
        if args.get("dry_run"):
            import digital_twin
            res = await digital_twin.simulate_mission(subtasks)
            push("simulation", res)
            return "SIMULATION ONLY, nothing was executed: " + _short(res)
        try:
            mid = await asyncio.to_thread(engine.create_mission, args.get("title", "Mission"), args.get("description", ""), subtasks)
        except ValueError as e:
            return f"I could not start that mission: {e}. Fix the plan and try again."
        engine.start(mid)
        return (f"Mission '{args.get('title')}' started in the background (id {mid}, {len(subtasks)} steps). "
                "Progress shows in the MISSIONS tab; use manage_missions to check on it.")

    if name == "manage_missions":
        from mission_control import engine
        act, mid = args.get("action", "list"), args.get("mission_id")
        if act == "list":
            rows = await asyncio.to_thread(engine.list_missions, 8)
            return _short([{"id": m["id"], "title": m["title"], "status": m["status"], "result": m["result"]} for m in rows]) if rows else "No missions yet."
        if not mid:
            return "Which mission? I need its id (use action list)."
        if act == "status":
            m = await asyncio.to_thread(engine.get_mission, mid)
            return _short({"title": m["title"], "status": m["status"], "result": m["result"],
                           "steps": [{"title": s["title"], "status": s["status"], "error": s["error"]} for s in m["subtasks"]]}) if m else "No such mission."
        if act == "pause":
            return "Paused (takes effect after the current step)." if engine.pause_mission(mid) else "That mission is not running."
        if act == "resume":
            if engine.resume_mission(mid):
                engine.start(mid)
                return "Resumed from the last checkpoint."
            return "That mission can't be resumed (finished, cancelled or already running)."
        if act == "cancel":
            return "Cancelled." if engine.cancel_mission(mid) else "That mission already finished."
        return f"Unknown action {act}."

    if name == "manage_events":
        ee = tool_runner._ctx.get("event_engine")
        if not ee:
            return "Automation engine is not available."
        act = args.get("action", "list")
        if act == "list":
            rules = await asyncio.to_thread(ee.list_rules)
            return _short([{"id": r["id"], "name": r["name"], "trigger": r["trigger_type"], "action": r["action_type"],
                            "enabled": r["enabled"], "waiting_for_approval": r["needs_approval"], "fired": r["fire_count"]}
                           for r in rules]) if rules else "No automation rules yet."
        if act == "history":
            return _short(await asyncio.to_thread(ee.get_history, 10)) or "No rule has fired yet."
        if act == "add":
            data = {"rule_name": args.get("rule_name"), "trigger_type": args.get("trigger_type", "manual"),
                    "trigger_config": _j(args.get("trigger_config"), {}), "action_type": args.get("action_type", "notify"),
                    "action_config": _j(args.get("action_config"), {})}
            try:
                rid = await asyncio.to_thread(ee.add_rule, data, False)   # the model can never approve its own rule
            except ValueError as e:
                return f"Rule not created: {e}"
            push("events_changed", {})
            pending = data["action_type"] != "notify"
            return (f"Rule '{data['rule_name']}' created (id {rid})."
                    + (" It runs a tool, so it will only act after the user approves it in the EVENTS tab." if pending else ""))
        rid = args.get("rule_id")
        if not rid:
            return "I need the rule id (use action list)."
        if act == "remove":
            await asyncio.to_thread(ee.remove_rule, rid)
            push("events_changed", {})
            return "Rule removed."
        if act == "fire":
            info = await asyncio.to_thread(ee.fire_rule, rid, "voice")
            return _short(info) if info else "No such rule."
        if act == "dry_run":
            import digital_twin
            return "SIMULATION ONLY: " + _short(await digital_twin.dry_run_rule(ee, rid))
        return f"Unknown action {act}."

    if name == "knowledge_graph":
        from knowledge_graph import get_graph
        g, act, q = get_graph(), args.get("action", "context"), (args.get("query") or "").strip()
        if act == "context":
            return (await asyncio.to_thread(g.retrieve_context, q))["text"]
        if act == "search":
            return _short([{k: r[k] for k in ("name", "type", "sources", "neighbors")} for r in await asyncio.to_thread(g.search_graph, q)][:10]) or "Nothing found."
        if act == "relations":
            return _short(await asyncio.to_thread(g.relations, q, args.get("relation") or None)) or f"No facts about {q}."
        if act == "path":
            return _short(await asyncio.to_thread(g.find_path, q, args.get("entity_b", "")))
        if act == "add_entity":
            nid = await asyncio.to_thread(g.add_entity, args.get("entity_name") or q, args.get("entity_type", "concept"), _j(args.get("properties"), {}))
            return f"Entity stored (id {nid})."
        if act == "add_fact":
            a = await asyncio.to_thread(g.add_entity, q, "concept", None, "voice")
            b = await asyncio.to_thread(g.add_entity, args.get("entity_b", ""), "concept", None, "voice")
            rel = (args.get("relation") or "related_to").strip().replace(" ", "_")
            await asyncio.to_thread(g.add_relationship, a, b, rel, None, "voice", f"told by the user: {q} {rel} {args.get('entity_b')}")
            return f"Stored: {q} --{rel}--> {args.get('entity_b')}."
        if act == "get_all":
            return _short([{k: e[k] for k in ("name", "type", "degree")} for e in await asyncio.to_thread(g.get_all_entities, 30)])
        return f"Unknown action {act}."

    if name == "run_diagnostics":
        from diagnostics import DiagnosticsEngine
        rep = await asyncio.to_thread(DiagnosticsEngine().generate_report)
        push("diagnostics", rep)
        broken = [m for m, i in rep["backend_health"].items() if not i["ok"]]
        bad_db = [f for f, i in rep["db_health"].items() if not i["ok"]]
        mc = rep["model_connectivity"]
        return _short({"internet": rep["api_health"].get("internet"), "gemini": mc.get("status"),
                       "gemini_latency_ms": mc.get("latency_ms"), "omniroute": rep["api_health"].get("omniroute"),
                       "broken_modules": broken, "bad_databases": bad_db,
                       "log_errors": rep["error_analysis"].get("total_errors"),
                       "bottlenecks": [f["finding"] for f in rep["bottlenecks"].get("findings", [])],
                       "recommendations": rep["recommendations"]})

    if name == "simulate":
        import digital_twin
        target = args.get("target")
        if target == "file_operation":
            res = await asyncio.to_thread(digital_twin.preview_file_operation, args.get("op"), args.get("src"),
                                          args.get("dst"), args.get("pattern"), bool(args.get("overwrite")))
        elif target == "mission":
            res = await digital_twin.simulate_mission(args.get("subtasks") or [], args.get("fail_steps") or None)
        elif target == "event_rule":
            ee = tool_runner._ctx.get("event_engine")
            res = await digital_twin.dry_run_rule(ee, args.get("rule_id")) if ee else {"error": "automation engine unavailable"}
        else:
            return "target must be file_operation, mission or event_rule."
        push("simulation", res)
        return "SIMULATION ONLY, nothing was executed. Say so to the user. " + _short(res)

    if name == "get_recommendations":
        import predictor
        res = await asyncio.to_thread(predictor.recommendations)
        if res["status"] != "ok":
            return res["message"]
        return _short(res["recommendations"][:5]) if res["recommendations"] else "Enough data, but no clear pattern worth automating yet."

    if name == "get_app_context":
        import app_context
        now = await asyncio.to_thread(app_context.active_window)
        return _short({"active": now, "recent": app_context.tracker.recent(8)})

    return f"Unknown tool {name}."
