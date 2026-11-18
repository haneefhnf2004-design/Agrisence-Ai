"""
Shared tool runner: the real executor behind Mission Control and Event automation.

Before this module, MissionEngine._execute_subtask returned {'stub': True} and
event rules with run_tool never ran anything. Now every subtask / rule action is
dispatched to an actual implementation registered here.

Each tool is registered with:
  side_effect   - changes something outside Nexora (mouse/keyboard, files). These
                  are serialised through one lock so two steps never fight over
                  the mouse, and they are refused for unapproved event rules.
  parallel_safe - may run at the same time as other parallel_safe steps.

Never allowed in automated runs: sending email (it keeps its on-screen approval).

run_tool() raises ToolError (do not retry: bad args, unknown tool, blocked) or any
other exception (may retry). On success it returns {"ok": True, "output": ...}.
"""
import asyncio
import os
import shutil
import time
import fnmatch
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

import telemetry


class ToolError(Exception):
    """A failure that retrying will not fix (unknown tool, bad arguments, blocked)."""


@dataclass
class ToolSpec:
    name: str
    fn: Callable[[dict], Awaitable[Any]]
    side_effect: bool = False
    parallel_safe: bool = True
    description: str = ""


REGISTRY: Dict[str, ToolSpec] = {}
BLOCKED_IN_AUTOMATION = {"send_email", "reply_to_email", "send_reply_drafts"}

# Runtime hooks set by server.py / ada.py.
_ctx: Dict[str, Any] = {"email_agent": None, "notifier": None}
_actuator_lock: Optional[asyncio.Lock] = None

# File tools may only touch paths under these roots (tests override this).
FILE_ROOTS = [os.path.expanduser("~")]


def set_email_agent(agent):
    _ctx["email_agent"] = agent


def set_notifier(fn: Callable[[dict], None]):
    """fn(payload) is called by the `notify` tool, e.g. to emit a socket event."""
    _ctx["notifier"] = fn


def register(name, side_effect=False, parallel_safe=None, description=""):
    def deco(fn):
        REGISTRY[name] = ToolSpec(name, fn, side_effect,
                                  (not side_effect) if parallel_safe is None else parallel_safe, description)
        return fn
    return deco


def spec(name) -> Optional[ToolSpec]:
    return REGISTRY.get(name)


def describe() -> list:
    return [{"name": s.name, "side_effect": s.side_effect, "parallel_safe": s.parallel_safe,
             "description": s.description} for s in sorted(REGISTRY.values(), key=lambda s: s.name)]


async def run_tool(name: str, args: dict = None, source: str = "mission") -> dict:
    global _actuator_lock
    args = dict(args or {})
    if name in BLOCKED_IN_AUTOMATION:
        raise ToolError(f"'{name}' needs the user's on-screen approval and cannot run inside a mission or automation.")
    s = REGISTRY.get(name)
    if not s:
        raise ToolError(f"Unknown tool '{name}'. Available: {', '.join(sorted(REGISTRY))}")
    t0 = time.perf_counter()
    try:
        if s.side_effect:
            if _actuator_lock is None:
                _actuator_lock = asyncio.Lock()
            async with _actuator_lock:
                out = await s.fn(args)
        else:
            out = await s.fn(args)
        telemetry.record(name, source, True, (time.perf_counter() - t0) * 1000)
        return {"ok": True, "output": out}
    except Exception as e:
        telemetry.record(name, source, False, (time.perf_counter() - t0) * 1000, f"{type(e).__name__}: {e}")
        raise


# ------------------------------------------------------------- helpers ----
def _need(args, key):
    v = args.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        raise ToolError(f"Missing required argument '{key}'.")
    return v


def _safe_path(p: str) -> str:
    if not p:
        raise ToolError("Empty path.")
    try:
        from computer_bridge import _resolve_path
        resolved = _resolve_path(p) if not os.path.isabs(p) else None
    except Exception:
        resolved = None
    full = os.path.realpath(resolved or os.path.expanduser(p))
    for root in FILE_ROOTS:
        r = os.path.realpath(root)
        try:
            if os.path.commonpath([full, r]) == r:
                return full
        except ValueError:  # different drives
            continue
    raise ToolError(f"Path '{p}' is outside the allowed folders ({', '.join(FILE_ROOTS)}).")


# ------------------------------------------------------------- tools ------
@register("get_datetime", description="Current local date and time.")
async def _get_datetime(args):
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


@register("get_system_stats", description="CPU, RAM, disk, battery and top processes.")
async def _get_system_stats(args):
    import system_monitor
    s = await asyncio.to_thread(system_monitor.snapshot)
    return {"summary": system_monitor.spoken_summary(s), "cpu_percent": s["cpu"]["percent"],
            "ram_percent": s["ram"]["percent"], "process_count": s["process_count"],
            "top_processes": [p["name"] for p in s["processes"][:5]]}


@register("sri_lanka_weather", description="Live Sri Lanka weather by district (network).")
async def _lk_weather(args):
    import lk_weather
    res = await asyncio.wait_for(asyncio.to_thread(lk_weather.sri_lanka_weather), timeout=40)
    if "error" in res:
        raise RuntimeError(res["error"])
    return {"summary": res["summary"]}


@register("sri_lanka_news", description="Latest Sri Lanka headlines (network).")
async def _lk_news(args):
    import lk_news
    res = await asyncio.wait_for(asyncio.to_thread(lk_news.fetch, args.get("topic"), 24, "en", 8), timeout=40)
    if "error" in res:
        raise RuntimeError(res["error"])
    return {"headlines": [i["title"] for i in res["items"]]}


@register("save_memory", side_effect=False, description="Store a long-term memory.")
async def _save_memory(args):
    from memory import memory
    res = await asyncio.to_thread(memory.save_memory, str(_need(args, "content")), args.get("category", "fact"),
                                  [t.strip() for t in str(args.get("tags", "")).split(",") if t.strip()])
    if res.get("error"):
        raise RuntimeError(res["error"])
    return {"memory_id": res["id"]}


@register("recall_memory", description="Semantic search over long-term memory.")
async def _recall_memory(args):
    from memory import memory
    rows = await asyncio.to_thread(memory.search_memories, str(_need(args, "query")), int(args.get("limit", 5)))
    return [{"id": r.get("id"), "content": r.get("content")} for r in rows]


@register("search_knowledge", description="RAG search over uploaded documents.")
async def _search_knowledge(args):
    from rag_engine import rag
    rows = await asyncio.to_thread(rag.search_knowledge, str(_need(args, "query")), int(args.get("limit", 5)))
    return [{"filename": r.get("filename"), "content": (r.get("content") or "")[:400]} for r in rows]


@register("knowledge_graph_query", description="Facts and relationships about an entity from the knowledge graph.")
async def _kg_query(args):
    from knowledge_graph import get_graph
    return await asyncio.to_thread(get_graph().retrieve_context, str(_need(args, "query")))


@register("run_diagnostics", description="Full system diagnostic report.")
async def _diagnostics(args):
    from diagnostics import DiagnosticsEngine
    rep = await asyncio.to_thread(DiagnosticsEngine().generate_report)
    return {"recommendations": rep["recommendations"], "timestamp": rep["timestamp"]}


@register("read_emails", description="Read recent emails (needs the email account connected in this session).")
async def _read_emails(args):
    agent = _ctx["email_agent"]
    if not agent:
        raise ToolError("Email is not connected in this session.")
    res = await asyncio.wait_for(agent.read_emails(limit=int(args.get("limit", 5))), timeout=60)
    if "error" in res:
        raise RuntimeError(res["error"])
    keys = ("from", "subject", "category", "priority", "summary")
    return [{k: e.get(k) for k in keys} for e in res.get("emails", [])]


@register("notify", description="Show a notification in the Nexora window.")
async def _notify(args):
    msg = str(_need(args, "message"))
    fn = _ctx["notifier"]
    if fn:
        fn({"title": args.get("title", "Nexora"), "message": msg})
    return {"message": msg, "delivered": bool(fn)}


@register("wait", description="Pause the workflow for up to 60 seconds.")
async def _wait(args):
    secs = float(args.get("seconds", 1))
    if not 0 <= secs <= 60:
        raise ToolError("seconds must be between 0 and 60.")
    await asyncio.sleep(secs)
    return {"waited_s": secs}


# --- cross-application: context + verification -----------------------------
@register("app_active_window", description="Which window/app is in the foreground right now.")
async def _active(args):
    import app_context
    w = await asyncio.to_thread(app_context.active_window)
    if not w:
        raise RuntimeError("Could not read the foreground window.")
    return w


@register("app_recent", description="Recent foreground app switches.")
async def _recent(args):
    import app_context
    return app_context.tracker.recent(int(args.get("limit", 10)))


@register("app_wait_window", description="Wait until a window whose title contains `title` exists (verification).")
async def _wait_window(args):
    import app_context
    title = str(_need(args, "title"))
    found = await asyncio.to_thread(app_context.wait_for_window, title, float(args.get("timeout_s", 10)))
    if not found:
        raise RuntimeError(f"No window containing '{title}' appeared within {args.get('timeout_s', 10)} s.")
    return {"window": found}


def _computer(name, side_effect):
    async def fn(args):
        from computer_bridge import call_computer_tool
        res = await call_computer_tool(name, args)
        text = " ".join(res.get("texts") or [])
        if res.get("is_error"):
            raise RuntimeError(text or f"{name} failed")
        return {"text": text[:1500]}
    register(name, side_effect=side_effect, description=f"Computer control: {name}")(fn)


for _n in ("computer_list_windows", "computer_find_text"):
    _computer(_n, False)
for _n in ("computer_open", "computer_focus_window", "computer_click_text", "computer_type",
           "computer_key", "computer_hotkey", "computer_mouse_click", "computer_mouse_move"):
    _computer(_n, True)


# --- file operations (inside FILE_ROOTS only) ------------------------------
@register("file_list", description="List files in a folder, optional glob pattern.")
async def _file_list(args):
    folder = _safe_path(str(_need(args, "path")))
    if not os.path.isdir(folder):
        raise ToolError(f"Not a folder: {folder}")
    pat = args.get("pattern") or "*"
    names = sorted(n for n in os.listdir(folder) if fnmatch.fnmatch(n, pat))
    return {"folder": folder, "files": names[:500], "count": len(names)}


def _transfer(op):
    async def fn(args):
        src = _safe_path(str(_need(args, "src")))
        dst = _safe_path(str(_need(args, "dst")))
        if not os.path.exists(src):
            raise ToolError(f"Source does not exist: {src}")
        if os.path.isdir(dst):
            dst = os.path.join(dst, os.path.basename(src))
        if os.path.exists(dst) and not args.get("overwrite"):
            raise ToolError(f"Destination already exists: {dst} (pass overwrite=true to replace).")
        def work():
            if op == "copy":
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=bool(args.get("overwrite")))
                else:
                    shutil.copy2(src, dst)
            else:
                shutil.move(src, dst)
        await asyncio.to_thread(work)
        if not os.path.exists(dst):  # verify the effect, not just the absence of an exception
            raise RuntimeError(f"{op} reported success but {dst} does not exist.")
        return {"src": src, "dst": dst, "op": op}
    return fn


register("file_copy", side_effect=True, description="Copy a file/folder (no overwrite unless asked).")(_transfer("copy"))
register("file_move", side_effect=True, description="Move/rename a file/folder (no overwrite unless asked).")(_transfer("move"))
