import sys
import asyncio

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

import socketio
import uvicorn
from fastapi import FastAPI
import os
import json

from dotenv import load_dotenv

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_DIR = os.path.dirname(_BACKEND_DIR)
load_dotenv(os.path.join(_PROJECT_DIR, '.env'))

API_KEY = os.getenv("GEMINI_API_KEY")
print(f"[SERVER] GEMINI_API_KEY {'FOUND' if API_KEY else 'MISSING — set in .env at project root'}")

sys.path.append(_BACKEND_DIR)

import ada
from email_agent import EmailAgent
from sheets_logger import SheetsLogger
from weather_service import city_weather

# Only the ADA window may talk to this backend. '*' let ANY website open in a browser
# connect to 127.0.0.1:8000, read settings and drive the mouse/keyboard tools.
ALLOWED_ORIGINS = [
    "http://localhost:5173", "http://127.0.0.1:5173",   # Vite dev server
    "file://",                                           # packaged Electron build
]
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins=ALLOWED_ORIGINS)
app = FastAPI()
app_socketio = socketio.ASGIApp(sio, app)

import signal

def signal_handler(sig, frame):
    print(f"\n[SERVER] Caught signal {sig}. Exiting gracefully...")
    if audio_loop:
        try:
            print("[SERVER] Stopping Audio Loop...")
            audio_loop.stop()
        except:
            pass
    print("[SERVER] Force exiting...")
    os._exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

audio_loop = None
loop_task = None
SETTINGS_FILE = os.path.join(_BACKEND_DIR, "settings.json")  # not CWD-relative
sheets_logger = SheetsLogger()

DEFAULT_SETTINGS = {
    "tool_permissions": {
        "read_emails": True,
        "send_email": True,
    },
    "email_config": {
        "imap_server": "imap.gmail.com",
        "smtp_server": "smtp.gmail.com",
        "email_address": "",
        "password": ""
    }
}

SETTINGS = DEFAULT_SETTINGS.copy()

def load_settings():
    global SETTINGS
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                loaded = json.load(f)
                for k, v in loaded.items():
                    if k == "tool_permissions" and isinstance(v, dict):
                        SETTINGS["tool_permissions"].update(v)
                    else:
                        SETTINGS[k] = v
            print("Loaded settings.")  # never print SETTINGS: it holds the email password
        except Exception as e:
            print(f"Error loading settings: {e}")

def save_settings():
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(SETTINGS, f, indent=4)
        print("Settings saved.")
    except Exception as e:
        print(f"Error saving settings: {e}")

load_settings()


def public_settings():
    """Settings as sent to the UI: the email password never leaves the backend."""
    out = json.loads(json.dumps(SETTINGS))
    cfg = out.get("email_config", {})
    cfg["password_saved"] = bool(cfg.get("password") or os.getenv("EMAIL_PASSWORD"))
    cfg["password"] = ""
    return out


def email_credentials():
    """settings.json first, then .env (same rule for voice, proactive and inbox panel)."""
    cfg = SETTINGS.get("email_config", {})
    address = (cfg.get("email_address", "") or os.getenv("EMAIL_ADDRESS", "")).strip()
    # Google shows app passwords as "abcd efgh ijkl mnop"; the spaces are not part of it.
    password = "".join((cfg.get("password", "") or os.getenv("EMAIL_PASSWORD", "")).split())
    return address, password


# ---------------------------------------------------------------- focus ----
import time as _time
from focus import FocusEngine, make_gemini_classifier

connected_clients = set()


def _focus_event(kind, data):
    """FocusEngine -> UI (and voice for nudges)."""
    asyncio.create_task(sio.emit(kind, data))
    if kind == "focus_state":
        _bluey_focus_watch(data)
    if kind == "focus_state" and audio_loop is not None:
        # Proactive email interruptions stay quiet while the user is focusing.
        if data.get("active") and not data.get("paused"):
            audio_loop.signals.busy_until = _time.time() + data.get("remaining", 0) / focus_engine.speed
        else:
            audio_loop.signals.busy_until = 0.0
    if kind == "focus_nudge":
        _bluey("say", text=data.get("text", ""), mood="worried", seconds=10)
        asyncio.create_task(sio.emit('widget_update', {'type': 'focus_nudge', 'data': data}))
        if audio_loop is not None:
            asyncio.create_task(audio_loop.speak_nudge(data))


import secrets as _secrets
os.environ["APA_BLUEY_TOKEN"] = _secrets.token_hex(16)   # inherited by the Bluey process
_caption_buf = {"text": "", "t": 0.0}


def _bluey_forward_widget(data):
    """Mirror Nexora's work into Bluey's background HUD."""
    if not SETTINGS.get("bluey_desktop", True):
        return
    t, d = data.get("type"), data.get("data") or {}
    if t == "task":
        args = d.get("args") or {}
        arg = next(iter(args.values()), "") if args else ""
        _bluey("task", id=d.get("id"), tool=d.get("tool"), state=d.get("state"), arg=str(arg)[:30])
    elif t == "focus_nudge":
        pass  # handled in _focus_event


def _bluey_forward_caption(data):
    if not SETTINGS.get("bluey_desktop", True) or data.get("sender") != "ADA":
        return
    import time as _t
    _caption_buf["text"] += data.get("text", "")
    if _t.time() - _caption_buf["t"] > 0.6:          # throttle file writes
        _bluey("caption", text=_caption_buf["text"])
        _caption_buf["text"], _caption_buf["t"] = "", _t.time()


def _bluey(action, **kw):
    """Tell the desktop Bluey buddy something (starts it if needed). Never raises."""
    try:
        import bluey_buddy
        if bluey_buddy.ensure_running():
            bluey_buddy.send_command(action, **kw)
    except Exception as e:
        print(f"[BLUEY] {e}")


_last_focus_active = {"v": False}


def _bluey_focus_watch(data):
    active = bool(data.get("active"))
    if active and not _last_focus_active["v"]:
        _bluey("say", text=f"Focus mode on: {data.get('task', '')}. I've got your back!", mood="focus", seconds=6)
    elif not active and _last_focus_active["v"]:
        _bluey("say", text="Session done. Nice work!", mood="happy", seconds=6)
    _last_focus_active["v"] = active


focus_engine = FocusEngine(classify=make_gemini_classifier(ada.client), on_event=_focus_event)

import system_monitor
import lk_weather
import lk_news
from memory import memory as memory_engine

try:
    from rag_engine import rag as rag_engine
    _RAG_AVAILABLE = True
except Exception as _rag_err:
    print(f"[RAG] RAG engine unavailable: {_rag_err}")
    rag_engine = None
    _RAG_AVAILABLE = False

try:
    from wake_word import wake_listener
    _WAKE_AVAILABLE = True
except Exception as _wake_err:
    print(f"[WAKE] Wake word listener unavailable: {_wake_err}")
    wake_listener = None
    _WAKE_AVAILABLE = False

try:
    from training_centre import tc as training_centre
    _TC_AVAILABLE = True
except Exception as _tc_err:
    print(f"[TC] Training centre unavailable: {_tc_err}")
    training_centre = None
    _TC_AVAILABLE = False

try:
    from mission_control import MissionEngine
    mission_engine = MissionEngine()
    def _on_mission_progress(mission_id, event_type, data):
        asyncio.create_task(sio.emit('mission_progress', {'mission_id': mission_id, 'event_type': event_type, 'data': data}))
    mission_engine.on_progress = _on_mission_progress
    _MISSIONS_AVAILABLE = True
except Exception as _me_err:
    print(f'[MISSIONS] Mission engine unavailable: {_me_err}')
    mission_engine = None
    _MISSIONS_AVAILABLE = False

try:
    from event_engine import EventEngine
    event_engine = EventEngine()
    def _on_event_fired(rule_info):
        asyncio.create_task(sio.emit('event_fired', rule_info))
    event_engine.on_event = _on_event_fired
    _EVENTS_AVAILABLE = True
except Exception as _ee_err:
    print(f'[EVENTS] Event engine unavailable: {_ee_err}')
    event_engine = None
    _EVENTS_AVAILABLE = False

try:
    from knowledge_graph import KnowledgeGraph
    kg_engine = KnowledgeGraph()
    _KG_AVAILABLE = True
except Exception as _kg_err:
    print(f'[KG] Knowledge graph unavailable: {_kg_err}')
    kg_engine = None
    _KG_AVAILABLE = False


def _on_wake_word():
    """Called by wake word listener thread → emit socket event to all clients."""
    print("[WAKE] Wake word detected!")
    asyncio.run_coroutine_threadsafe(
        sio.emit('wake_word_detected', {'phrase': 'hey nexora'}),
        asyncio.get_event_loop()
    )


async def _system_stats_loop():
    """Live hardware + process list to the UI every 2 s, only while a window is connected."""
    while True:
        if connected_clients:
            try:
                await sio.emit('system_stats', await asyncio.to_thread(system_monitor.snapshot))
            except Exception as e:
                print(f"[STATS] {e}")
        await asyncio.sleep(1)  # Task Manager refreshes every second too


async def _daily_briefing_loop():
    """Once a day from 7 AM: Sri Lanka weather on screen, and spoken if voice is on."""
    while True:
        if connected_clients and lk_weather.daily_due():
            res = await asyncio.to_thread(lk_weather.sri_lanka_weather)
            if "error" not in res:
                lk_weather.mark_daily_done()
                await sio.emit('widget_update', {'type': 'lk_weather', 'data': res})
                news = await asyncio.to_thread(lk_news.fetch, None, 24, "en", 8)
                heads = ""
                if "error" not in news and news["items"]:
                    await sio.emit('widget_update', {'type': 'lk_news', 'data': news})
                    heads = " Then the top Sri Lanka news in 3 short sentences from these headlines: " + \
                            " | ".join(x["title"] for x in news["items"][:6])
                if audio_loop is not None and audio_loop.session:
                    try:
                        await audio_loop.session.send(input=(
                            "System Event: daily morning briefing. Greet the user, say today's date (call get_datetime), "
                            f"and give a 3-sentence Sri Lanka weather update from this data: {res['summary']}.{heads}"), end_of_turn=True)
                    except Exception as e:
                        print(f"[DAILY] speak failed: {e}")
        await asyncio.sleep(60)


async def _perf_history_loop():
    while True:
        try:
            snap = await asyncio.to_thread(system_monitor.snapshot)
            await asyncio.to_thread(system_monitor.save_perf_snapshot, snap)
        except Exception as e:
            print(f'[PERF] {e}')
        await asyncio.sleep(60)


@app.on_event("startup")
async def startup_event():
    print("[SERVER] Nexora Email Assistant Backend Starting...")
    asyncio.create_task(focus_engine.run_forever())
    asyncio.create_task(_system_stats_loop())
    asyncio.create_task(_daily_briefing_loop())
    asyncio.create_task(_perf_history_loop())
    if event_engine: await asyncio.to_thread(event_engine.start)
    if SETTINGS.get("bluey_desktop", True):
        try:
            import bluey_buddy
            await asyncio.to_thread(bluey_buddy.restart)   # fresh Bluey with this run's token
        except Exception as e:
            print(f"[BLUEY] start failed: {e}")
    if wake_listener and SETTINGS.get('wake_word_enabled', True):
        wake_listener.on_wake = _on_wake_word
        wake_listener.start()
        print("[WAKE] Wake word listener started.")

@app.get("/status")
async def status():
    return {"status": "running", "service": "Nexora Email Assistant Backend"}

from fastapi import Request
from fastapi.responses import JSONResponse


@app.post("/bluey/ask")
async def bluey_ask(request: Request):
    """Bluey's text box -> Nexora. Needs this run's secret token in a custom header,
    so no web page (CORS preflight) and no stale Bluey can send commands."""
    if request.headers.get("x-bluey-token") != os.environ.get("APA_BLUEY_TOKEN"):
        return JSONResponse({"ok": False, "error": "Not authorised."}, status_code=403)
    body = await request.json()
    text = str(body.get("text", "")).strip()[:500]
    if not text:
        return {"ok": False, "error": "Empty command."}
    if not (audio_loop and audio_loop.session):
        return {"ok": False, "error": "Nexora's voice is off. Open Nexora and press Voice on."}
    await sio.emit('transcription', {"sender": "User", "text": text})
    await audio_loop.send_text(text)
    return {"ok": True}


@app.get("/proactive/stats")
async def proactive_stats():
    """Research evidence endpoint: engine stats + last decisions (for the report)."""
    log_path = ada.ProactiveEngine().evidence_file
    entries = []
    if os.path.exists(log_path):
        try:
            with open(log_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            entries = [json.loads(l) for l in lines[-50:]]
        except Exception as e:
            return {"error": str(e)}
    stats = None
    if audio_loop and audio_loop.proactive:
        stats = audio_loop.proactive.stats
    return {"stats": stats, "recent": entries}

@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}")
    connected_clients.add(sid)
    await sio.emit('focus_state', focus_engine.state(), room=sid)
    await sio.emit('status', {'msg': 'Connected to Nexora Backend'}, room=sid)

@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")
    connected_clients.discard(sid)
    if not connected_clients and focus_engine.session:
        focus_engine.end("disconnected")  # like Bluey: never keep watching with no UI open

@sio.event
async def start_audio(sid, data=None):
    global audio_loop, loop_task

    print("Starting Audio Loop...")

    device_index = None
    device_name = None
    if data:
        if 'device_index' in data:
            device_index = data['device_index']
        if 'device_name' in data:
            device_name = data['device_name']

    print(f"Using input device: Name='{device_name}', Index={device_index}")

    if audio_loop:
        if loop_task and (loop_task.done() or loop_task.cancelled()):
            print("Audio loop task appeared finished/cancelled. Clearing and restarting...")
            audio_loop = None
            loop_task = None
        else:
            print("Audio loop already running. Re-connecting client to session.")
            await sio.emit('status', {'msg': 'Nexora Already Running'})
            return

    def on_audio_data(data_bytes):
        if isinstance(data_bytes, bytes):
            asyncio.create_task(sio.emit('audio_data', {'data': list(data_bytes)}))

    def on_transcription(data):
        asyncio.create_task(sio.emit('transcription', data))
        _bluey_forward_caption(data)

    def on_tool_confirmation(data):
        print(f"Requesting confirmation for tool: {data.get('tool')}")
        asyncio.create_task(sio.emit('tool_confirmation_request', data))

    def on_error(msg):
        print(f"Sending Error to frontend: {msg}")
        asyncio.create_task(sio.emit('error', {'msg': msg}))

    def on_widget_update(data):
        asyncio.create_task(sio.emit('widget_update', data))
        _bluey_forward_widget(data)

    try:
        print(f"Initializing AudioLoop with device_index={device_index}")
        audio_loop = ada.AudioLoop(
            video_mode="none",
            on_audio_data=on_audio_data,
            on_transcription=on_transcription,
            on_tool_confirmation=on_tool_confirmation,
            on_widget_update=on_widget_update,
            on_error=on_error,
            input_device_index=device_index,
            input_device_name=device_name,
            sheets_logger=sheets_logger,
        )
        print("AudioLoop initialized successfully.")

        audio_loop.update_permissions(SETTINGS["tool_permissions"])
        audio_loop.focus = focus_engine

        if data and data.get('muted', False):
            print("Starting with Audio Paused")
            audio_loop.set_paused(True)

        print("Creating asyncio task for AudioLoop.run()")
        loop_task = asyncio.create_task(audio_loop.run())

        def handle_loop_exit(task):
            try:
                task.result()
            except asyncio.CancelledError:
                print("Audio Loop Cancelled")
            except Exception as e:
                print(f"Audio Loop Crashed: {e}")

        loop_task.add_done_callback(handle_loop_exit)

        print("Emitting 'Nexora Started'")
        if wake_listener and wake_listener.running:
            wake_listener.stop()
            print("[WAKE] Wake word listener paused (voice active).")
        await sio.emit('status', {'msg': 'Nexora Started'})

        email_config = SETTINGS.get("email_config", {})
        if email_config and audio_loop.email_agent:
            print(f"[SERVER] Loading email configuration...")
            audio_loop.email_agent.email_address, audio_loop.email_agent.password = email_credentials()
            audio_loop.email_agent.imap_server = email_config.get("imap_server", "imap.gmail.com")
            audio_loop.email_agent.smtp_server = email_config.get("smtp_server", "smtp.gmail.com")

    except Exception as e:
        print(f"CRITICAL ERROR STARTING ADA: {e}")
        import traceback
        traceback.print_exc()
        await sio.emit('error', {'msg': f"Failed to start: {str(e)}"})
        audio_loop = None

@sio.event
async def stop_audio(sid, data=None):
    global audio_loop, loop_task
    if audio_loop:
        print("Stopping Audio Loop...")
        audio_loop.stop()
        if loop_task:
            loop_task.cancel()
        audio_loop = None
        loop_task = None
        if wake_listener and SETTINGS.get('wake_word_enabled', True) and not wake_listener.running:
            wake_listener.on_wake = _on_wake_word
            wake_listener.start()
            print("[WAKE] Wake word listener resumed.")
        await sio.emit('status', {'msg': 'Nexora Stopped'})

@sio.event
async def set_wake_word(sid, data=None):
    enabled = bool((data or {}).get('enabled', True))
    SETTINGS['wake_word_enabled'] = enabled
    save_settings()
    if enabled and wake_listener and not wake_listener.running:
        wake_listener.on_wake = _on_wake_word
        wake_listener.start()
    elif not enabled and wake_listener and wake_listener.running:
        wake_listener.stop()
    await sio.emit('settings_updated', {'settings': public_settings()}, room=sid)

@sio.event
async def toggle_mute(sid, data=None):
    if audio_loop:
        muted = data.get('muted', False) if data else False
        audio_loop.set_paused(muted)
        print(f"Mute toggled: {muted}")
        await sio.emit('status', {'msg': f"{'Muted' if muted else 'Unmuted'}"})

# Frontend emits these names — previously unhandled, so mute was a placebo.
@sio.event
async def pause_audio(sid, data=None):
    if audio_loop:
        audio_loop.set_paused(True)
        print("Audio paused (mic hot-cut).")
        await sio.emit('status', {'msg': 'Muted'})

@sio.event
async def resume_audio(sid, data=None):
    if audio_loop:
        audio_loop.set_paused(False)
        print("Audio resumed.")
        await sio.emit('status', {'msg': 'Unmuted'})

@sio.event
async def user_input(sid, data):
    if audio_loop and audio_loop.session:
        text = data.get('text', '') if data else ''
        if text:
            print(f"[SERVER] User text input: {text}")
            audio_loop.signals.text_active = True
            audio_loop.signals.last_text_activity = __import__('time').time()
            await audio_loop.send_text(text)

@sio.event
async def resolve_confirmation(sid, data):
    if audio_loop:
        audio_loop.resolve_tool_confirmation(data.get('id'), data.get('confirmed', False))

@sio.event
async def send_message(sid, data):
    """Receives a text message from frontend and sends it to the model."""
    if audio_loop and audio_loop.session:
        message = data.get('message', '')
        if message:
            print(f"[CHAT] Message from frontend: {message[:80]}...")
            audio_loop.signals.text_active = True
            audio_loop.signals.last_text_activity = __import__('time').time()
            try:
                await audio_loop.session.send(input=message, end_of_turn=True)
            except Exception as e:
                print(f"[CHAT] Error sending message: {e}")
                await sio.emit('error', {'msg': f"Failed to send message: {e}"})
    else:
        print("[CHAT] Cannot send message: Audio loop not running or no session.")
        await sio.emit('error', {'msg': "Nexora is not running."})

@sio.event
async def fetch_today_emails(sid, data=None):
    """Fetches today's emails and sends them to the frontend."""
    print("[SERVER] Fetching today's emails...")
    email_config = SETTINGS.get("email_config", {})
    agent = EmailAgent(
        email_address=email_credentials()[0],
        password=email_credentials()[1]
    )
    try:
        result = await agent.fetch_today_emails(limit=20)
        if "error" in result:
            await sio.emit('email_error', {'error': result["error"]})
        else:
            if sheets_logger:
                try:
                    sheets_logger.log_batch(result.get("emails", []))
                except Exception as e:
                    print(f"[SERVER] Sheets log failed: {e}")
            await sio.emit('today_emails', result)
    except Exception as e:
        print(f"[SERVER] fetch_today_emails error: {e}")
        await sio.emit('email_error', {'error': str(e)})

@sio.event
async def summon_demo(sid, data=None):
    """On-demand cinematic summon: fetches + analyzes inbox, pushes globe overlay."""
    print("[SERVER] Summon demo requested...")
    email_config = SETTINGS.get("email_config", {})
    agent = EmailAgent(
        email_address=email_credentials()[0],
        password=email_credentials()[1]
    )
    try:
        result = await agent.read_emails(limit=8)
        if "error" in result:
            await sio.emit('widget_update', {'type': 'error', 'data': {
                'title': 'Summon Failed', 'message': result["error"],
                'detail': 'Check email credentials in .env.'}})
            return
        emails_data = [{
            "from": e.get("from", "Unknown"), "subject": e.get("subject", "No subject"),
            "category": e.get("category", ""), "priority": e.get("priority", "Medium"),
            "sentiment": e.get("sentiment", "Neutral"), "intensity": e.get("intensity", "Mild"),
            "confidence": e.get("confidence", 0.5), "is_escalated": e.get("is_escalated", False),
            "summary": e.get("summary", ""), "draft_reply": e.get("draft_reply", "")
        } for e in result.get("emails", [])]
        await sio.emit('widget_update', {'type': 'email_summary', 'data': {
            "count": result.get("count", 0), "emails": emails_data,
            "escalation_count": result.get("escalation_count", 0),
            "low_confidence_count": result.get("low_confidence_count", 0)
        }})
        print(f"[SERVER] Summon pushed: {result.get('count', 0)} emails.")
    except Exception as e:
        print(f"[SERVER] summon_demo error: {e}")
        await sio.emit('widget_update', {'type': 'error', 'data': {
            'title': 'Summon Failed', 'message': str(e)}}, room=sid)

@sio.event
async def weather_city(sid, data=None):
    """Ask a city -> geocode -> globe locks on -> live weather card. Fully coded."""
    city = ((data or {}).get("city", "") if isinstance(data, dict) else "") or "Colombo"
    print(f"[SERVER] Weather lock requested: {city}")
    try:
        import asyncio as _aio
        result = await _aio.to_thread(city_weather, city)
        if "error" in result:
            await sio.emit('widget_update', {'type': 'error', 'data': {
                'title': 'Weather Lock Failed', 'message': result["error"]}})
            return
        await sio.emit('widget_update', {'type': 'weather', 'data': result})
        print(f"[SERVER] Weather lock pushed: {result['city']} ({result['lat']:.2f}, {result['lon']:.2f}).")
    except Exception as e:
        print(f"[SERVER] weather_city error: {e}")
        await sio.emit('widget_update', {'type': 'error', 'data': {
            'title': 'Weather Lock Failed', 'message': str(e)}})

@sio.event
async def control_demo(sid, data=None):
    """Cinematic takeover demo: synthetic control events so the HUD overlay can be
    shown without a live voice session (same pattern as weather_demo/summon_demo)."""
    import asyncio as _aio
    import base64
    import io as _io
    from PIL import Image

    def _demo_feed(step):
        img = Image.new('RGB', (320, 200), (8, 12, 24))
        px = img.load()
        for y in range(200):
            for x in range(0, 320, 3):
                v = (x * y // 900 + step * 40) % 80
                px[x, y] = (v // 4 + 8, v // 2 + 20, v // 3 + 44)
        for i in range(step, 320, 4):
            for h in range(120 + (i * 7) % 60, 160):
                px[i, h] = (34, 211, 238)
        buf = _io.BytesIO()
        img.save(buf, format='PNG')
        return base64.b64encode(buf.getvalue()).decode()

    events = [
        ({'type': 'computer_control', 'data': {'state': 'active', 'tool': 'computer_list_windows'}}, 0.0),
        ({'type': 'computer_control', 'data': {'state': 'done', 'tool': 'computer_list_windows'}}, 0.7),
        ({'type': 'computer_control', 'data': {'state': 'active', 'tool': 'computer_screenshot'}}, 1.4),
        ({'type': 'computer_screenshot', 'data': {'image': _demo_feed(1), 'tool': 'computer_screenshot'}}, 2.0),
        ({'type': 'computer_control', 'data': {'state': 'active', 'tool': 'computer_type'}}, 2.7),
        ({'type': 'computer_screenshot', 'data': {'image': _demo_feed(2), 'tool': 'computer_screenshot'}}, 3.4),
        ({'type': 'computer_control', 'data': {'state': 'done', 'tool': 'computer_type', 'detail': 'typed into focused window'}}, 4.0),
    ]
    print("[SERVER] Control demo sequence starting...")
    for payload, delay in events:
        await _aio.sleep(delay)
        await sio.emit('widget_update', payload)
    await sio.emit('status', {'msg': 'Control demo sequence played'})

@sio.event
async def weather_demo(sid, data=None):
    """On-demand weather globe summon (sample data until live API wired)."""
    city = (data or {}).get("city", "Colombo") if isinstance(data, dict) else "Colombo"
    await sio.emit('widget_update', {'type': 'weather', 'data': {
        "city": city, "temp": 87, "feels": 93, "description": "partly sunny",
        "humidity": 74, "wind": "12 km/h",
        "forecast": [
            {"date": "10-14", "desc": "sunny", "lo": 78, "hi": 88},
            {"date": "10-15", "desc": "scattered showers", "lo": 77, "hi": 86},
            {"date": "10-16", "desc": "thunderstorms", "lo": 77, "hi": 85},
        ],
    }})
    print(f"[SERVER] Weather summon pushed: {city}.")

@sio.event
async def new_email(sid, data):
    """Sends a new email from the compose window."""
    if not data:
        return
    to = data.get("to", "").strip()
    subject = data.get("subject", "").strip()
    body = data.get("body", "").strip()
    priority = data.get("priority", "normal")
    if not to or not subject or not body:
        await sio.emit('email_send_result', {'error': 'To, Subject, and Body are required.'})
        return
    email_config = SETTINGS.get("email_config", {})
    agent = EmailAgent(
        email_address=email_credentials()[0],
        password=email_credentials()[1]
    )
    try:
        result = await agent.send_email(to, subject, body, priority=priority)
        if "error" in result:
            await sio.emit('email_send_result', {'error': result["error"]})
        else:
            if sheets_logger:
                try:
                    sheets_logger.log_email_sent(to, subject, priority)
                except Exception as e:
                    print(f"[SERVER] Sheets log send failed: {e}")
            await sio.emit('email_send_result', {'success': True, 'message': result.get("message", "Email sent.")})
    except Exception as e:
        print(f"[SERVER] new_email error: {e}")
        await sio.emit('email_send_result', {'error': str(e)})

@sio.event
async def emergency_stop(sid, data=None):
    """User kill-switch: disables all mouse/keyboard control instantly, no session required."""
    from computer_bridge import call_computer_tool
    try:
        result = await call_computer_tool("computer_emergency_stop", timeout=25)
        ok = not result.get("is_error")
        await sio.emit('widget_update', {'type': 'computer_control', 'data': {
            'state': 'stopped' if ok else 'failed',
            'tool': 'computer_emergency_stop'}})
        await sio.emit('status', {'msg': 'Emergency stop engaged - control disabled' if ok
                                  else 'Emergency stop failed'})
    except Exception as e:
        print(f"[SERVER] Emergency stop error: {e}")
        await sio.emit('error', {'msg': f'Emergency stop failed: {e}'})

@sio.event
async def bluey(sid, data=None):
    """Show / hide the desktop Bluey buddy, or make it say something."""
    data = data or {}
    if data.get("on") is False:
        SETTINGS["bluey_desktop"] = False
        _bluey_quit()
    elif data.get("on") is True:
        SETTINGS["bluey_desktop"] = True
        _bluey("say", text="I'm back!", mood="happy", seconds=4)
    elif data.get("say"):
        _bluey("say", text=data["say"], seconds=6)
    save_settings()
    await sio.emit('bluey_state', {'on': SETTINGS.get("bluey_desktop", True)}, room=sid)


def _bluey_quit():
    try:
        import bluey_buddy
        bluey_buddy.send_command("quit")
    except Exception:
        pass

@sio.event
async def computer_resume(sid, data=None):
    """Lift the emergency stop so Nexora may use mouse/keyboard again."""
    from computer_bridge import resume_control
    resume_control()
    await sio.emit('widget_update', {'type': 'computer_control', 'data': {
        'state': 'done', 'tool': 'computer_resume', 'detail': 'control re-enabled'}})
    await sio.emit('status', {'msg': 'Computer control resumed'})

@sio.event
async def update_settings(sid, data):
    global SETTINGS
    if data:
        for k, v in data.items():
            if k == "tool_permissions" and isinstance(v, dict):
                SETTINGS["tool_permissions"].update(v)
                if audio_loop:
                    audio_loop.update_permissions(v)
            elif k == "email_config" and isinstance(v, dict):
                v = dict(v)
                v.pop("password_saved", None)
                v["password"] = "".join(str(v.get("password") or "").split())
                if not v["password"]:              # blank = keep the saved password
                    v["password"] = SETTINGS.get("email_config", {}).get("password", "")
                elif len(v["password"]) != 16:
                    print(f"[SERVER] Warning: Gmail app passwords have 16 letters; this one has {len(v['password'])}.")
                SETTINGS["email_config"] = {**SETTINGS.get("email_config", {}), **v}
            else:
                SETTINGS[k] = v
        save_settings()
        print("Settings updated.")
        await sio.emit('settings_updated', {'settings': public_settings()})
        if "email_config" in data:
            await apply_email_settings()


async def apply_email_settings():
    """Push saved email settings into the running voice agent and reconnect.
    Without this, a voice session started before the save keeps the old password."""
    agent = audio_loop.email_agent if audio_loop else None
    if not agent:
        return
    cfg = SETTINGS.get("email_config", {})
    agent.email_address, agent.password = email_credentials()
    agent.imap_server = cfg.get("imap_server", "imap.gmail.com")
    agent.smtp_server = cfg.get("smtp_server", "smtp.gmail.com")
    agent.connected = False
    result = await asyncio.to_thread(agent.connect)
    if "error" in result:
        print(f"[SERVER] Email login failed with the new settings: {result['error']}")
        await sio.emit('widget_update', {'type': 'error', 'data': {
            'title': 'Email login failed', 'message': result['error'],
            'detail': 'Check the 16-letter app password and that IMAP is on in Gmail.'}})
    else:
        print("[SERVER] Email connected with the new settings.")

@sio.event
async def get_settings(sid, data=None):
    await sio.emit('settings_loaded', {'settings': public_settings()})


# ------------------------------------------------------- focus sessions ----
@sio.event
async def focus_start(sid, data=None):
    data = data or {}
    focus_engine.start(data.get('task', ''), data.get('minutes', 30), data.get('dev_mode'))

@sio.event
async def focus_control(sid, data=None):
    action = (data or {}).get('action')
    if action == 'pause':
        focus_engine.pause()
    elif action == 'resume':
        focus_engine.resume()
    elif action == 'end':
        focus_engine.end('ended')
    await sio.emit('focus_state', focus_engine.state(), room=sid)

@sio.event
async def focus_dev_mode(sid, data=None):
    focus_engine.set_dev_mode(bool((data or {}).get('on')))

@sio.event
async def focus_history(sid, data=None):
    await sio.emit('focus_history', {'sessions': focus_engine.history()[-20:]}, room=sid)

@sio.event
async def focus_test_nudge(sid, data=None):
    """Checks the nudge path (widget + chirp + voice) without taking a screenshot."""
    st = focus_engine.state()
    _focus_event('focus_nudge', {'t': st.get('elapsed', 0), 'task': st.get('task', 'your task'),
                                 'app': 'test', 'reason': 'test nudge', 'point_text': '',
                                 'text': 'Test nudge: this is how I will bring you back on task.'})

@sio.event
async def get_memories(sid, data=None):
    limit = (data or {}).get('limit', 50)
    mems = await asyncio.to_thread(memory_engine.get_all_memories, limit)
    await sio.emit('memories_list', {'memories': mems}, room=sid)

@sio.event
async def delete_memory(sid, data=None):
    mid = (data or {}).get('id')
    if mid is not None:
        ok = await asyncio.to_thread(memory_engine.delete_memory, mid)
        await sio.emit('memory_deleted', {'id': mid, 'ok': ok}, room=sid)

@sio.event
async def upload_document(sid, data=None):
    if not rag_engine:
        await sio.emit('rag_error', {'error': 'RAG engine unavailable'}, room=sid)
        return
    if not data:
        return
    import base64
    filename = data.get('filename', 'document.txt')
    b64 = data.get('content', '')
    try:
        content_bytes = base64.b64decode(b64)
    except Exception as e:
        await sio.emit('rag_error', {'error': f'Invalid file data: {e}'}, room=sid)
        return
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'txt'
    result = await asyncio.to_thread(rag_engine.ingest_file, filename, content_bytes, ext)
    if 'error' in result:
        await sio.emit('rag_error', {'error': result['error']}, room=sid)
    else:
        docs = await asyncio.to_thread(rag_engine.list_documents)
        await sio.emit('documents_list', {'documents': docs}, room=sid)

@sio.event
async def list_documents(sid, data=None):
    if not rag_engine:
        await sio.emit('documents_list', {'documents': []}, room=sid)
        return
    docs = await asyncio.to_thread(rag_engine.list_documents)
    await sio.emit('documents_list', {'documents': docs}, room=sid)

@sio.event
async def delete_document(sid, data=None):
    if not rag_engine:
        return
    filename = (data or {}).get('filename')
    if filename:
        ok = await asyncio.to_thread(rag_engine.delete_document, filename)
        docs = await asyncio.to_thread(rag_engine.list_documents)
        await sio.emit('documents_list', {'documents': docs}, room=sid)

@sio.event
async def submit_feedback(sid, data=None):
    if not training_centre:
        return
    data = data or {}
    result = await asyncio.to_thread(
        training_centre.submit_feedback,
        data.get('user_message', ''),
        data.get('ai_response', ''),
        data.get('rating', 1),
        data.get('note', '')
    )
    stats = await asyncio.to_thread(training_centre.get_feedback_stats)
    await sio.emit('feedback_stats', stats, room=sid)

@sio.event
async def get_feedback_stats(sid, data=None):
    if not training_centre:
        await sio.emit('feedback_stats', {'total': 0, 'positive': 0, 'negative': 0, 'ratio': 0}, room=sid)
        return
    stats = await asyncio.to_thread(training_centre.get_feedback_stats)
    fb = await asyncio.to_thread(training_centre.get_feedback, 10)
    await sio.emit('feedback_stats', stats, room=sid)
    await sio.emit('feedback_list', {'feedback': fb}, room=sid)

@sio.event
async def upload_dataset(sid, data=None):
    if not training_centre:
        return
    data = data or {}
    import base64
    name = data.get('name', 'dataset')
    fmt = data.get('format', 'json')
    b64 = data.get('content', '')
    try:
        content_bytes = base64.b64decode(b64) if b64 else b''
    except Exception:
        content_bytes = b''
    size = len(content_bytes)
    record_count = 0
    if fmt in ('json', 'jsonl') and content_bytes:
        try:
            import json as _json
            text = content_bytes.decode('utf-8', errors='replace')
            if fmt == 'jsonl':
                record_count = len([l for l in text.splitlines() if l.strip()])
            else:
                obj = _json.loads(text)
                record_count = len(obj) if isinstance(obj, list) else 1
        except Exception:
            pass
    elif fmt == 'csv' and content_bytes:
        try:
            text = content_bytes.decode('utf-8', errors='replace')
            record_count = max(0, len(text.splitlines()) - 1)
        except Exception:
            pass
    result = await asyncio.to_thread(training_centre.upload_dataset, name, fmt, size, record_count)
    datasets = await asyncio.to_thread(training_centre.list_datasets)
    await sio.emit('datasets_list', {'datasets': datasets}, room=sid)

@sio.event
async def list_datasets(sid, data=None):
    if not training_centre:
        await sio.emit('datasets_list', {'datasets': []}, room=sid)
        return
    datasets = await asyncio.to_thread(training_centre.list_datasets)
    await sio.emit('datasets_list', {'datasets': datasets}, room=sid)


@sio.event
async def create_mission(sid, data=None):
    if not mission_engine:
        await sio.emit('mission_error', {'error': 'Mission engine unavailable'}, room=sid)
        return
    data = data or {}
    mission_id = await asyncio.to_thread(mission_engine.create_mission, data.get('title', 'Untitled'), data.get('description', ''), data.get('subtasks', []))
    asyncio.create_task(mission_engine.execute_mission(mission_id))
    await sio.emit('mission_created', {'mission_id': mission_id}, room=sid)

@sio.event
async def execute_mission(sid, data=None):
    if not mission_engine: return
    mission_id = (data or {}).get('mission_id')
    if mission_id:
        asyncio.create_task(mission_engine.execute_mission(mission_id))

@sio.event
async def cancel_mission(sid, data=None):
    if not mission_engine: return
    mission_id = (data or {}).get('mission_id')
    if mission_id:
        await asyncio.to_thread(mission_engine.cancel_mission, mission_id)
        await sio.emit('mission_cancelled', {'mission_id': mission_id}, room=sid)

@sio.event
async def get_missions(sid, data=None):
    if not mission_engine:
        await sio.emit('missions_list', {'missions': []}, room=sid)
        return
    missions = await asyncio.to_thread(mission_engine.list_missions)
    await sio.emit('missions_list', {'missions': missions}, room=sid)

@sio.event
async def get_mission(sid, data=None):
    if not mission_engine: return
    mission_id = (data or {}).get('mission_id')
    if mission_id:
        m = await asyncio.to_thread(mission_engine.get_mission, mission_id)
        await sio.emit('mission_detail', {'mission': m}, room=sid)


@sio.event
async def add_event_rule(sid, data=None):
    if not event_engine: return
    rule_id = await asyncio.to_thread(event_engine.add_rule, data or {})
    rules = await asyncio.to_thread(event_engine.list_rules)
    await sio.emit('event_rules_list', {'rules': rules}, room=sid)

@sio.event
async def remove_event_rule(sid, data=None):
    if not event_engine: return
    await asyncio.to_thread(event_engine.remove_rule, (data or {}).get('rule_id'))
    rules = await asyncio.to_thread(event_engine.list_rules)
    await sio.emit('event_rules_list', {'rules': rules}, room=sid)

@sio.event
async def toggle_event_rule(sid, data=None):
    if not event_engine: return
    data = data or {}
    if data.get('enabled'):
        await asyncio.to_thread(event_engine.enable_rule, data.get('rule_id'))
    else:
        await asyncio.to_thread(event_engine.disable_rule, data.get('rule_id'))
    rules = await asyncio.to_thread(event_engine.list_rules)
    await sio.emit('event_rules_list', {'rules': rules}, room=sid)

@sio.event
async def list_event_rules(sid, data=None):
    if not event_engine:
        await sio.emit('event_rules_list', {'rules': []}, room=sid)
        return
    rules = await asyncio.to_thread(event_engine.list_rules)
    history = await asyncio.to_thread(event_engine.get_history)
    await sio.emit('event_rules_list', {'rules': rules}, room=sid)
    await sio.emit('event_history', {'history': history}, room=sid)

@sio.event
async def fire_event_rule(sid, data=None):
    if not event_engine: return
    rule_id = (data or {}).get('rule_id')
    if rule_id:
        await asyncio.to_thread(event_engine.fire_rule, rule_id)


@sio.event
async def get_knowledge_graph(sid, data=None):
    if not kg_engine:
        await sio.emit('kg_data', {'nodes': [], 'edges': [], 'stats': {'node_count': 0, 'edge_count': 0, 'available': False}}, room=sid)
        return
    entities = await asyncio.to_thread(kg_engine.get_all_entities, 100)
    stats = await asyncio.to_thread(kg_engine.stats)
    await sio.emit('kg_data', {'entities': entities, 'stats': stats}, room=sid)

@sio.event
async def add_kg_entity(sid, data=None):
    if not kg_engine: return
    data = data or {}
    node_id = await asyncio.to_thread(kg_engine.add_entity, data.get('name', 'Unknown'), data.get('entity_type', 'concept'), data.get('properties', {}))
    entities = await asyncio.to_thread(kg_engine.get_all_entities, 100)
    stats = await asyncio.to_thread(kg_engine.stats)
    await sio.emit('kg_data', {'entities': entities, 'stats': stats}, room=sid)

@sio.event
async def delete_kg_entity(sid, data=None):
    if not kg_engine: return
    node_id = (data or {}).get('node_id')
    if node_id:
        await asyncio.to_thread(kg_engine.delete_entity, node_id)
        entities = await asyncio.to_thread(kg_engine.get_all_entities, 100)
        stats = await asyncio.to_thread(kg_engine.stats)
        await sio.emit('kg_data', {'entities': entities, 'stats': stats}, room=sid)

@sio.event
async def search_kg(sid, data=None):
    if not kg_engine:
        await sio.emit('kg_search_results', {'results': []}, room=sid)
        return
    query = (data or {}).get('query', '')
    results = await asyncio.to_thread(kg_engine.search_graph, query)
    await sio.emit('kg_search_results', {'results': results}, room=sid)


try:
    from diagnostics import DiagnosticsEngine
    diag_engine = DiagnosticsEngine()
    _DIAG_AVAILABLE = True
except Exception as _diag_err:
    print(f'[DIAG] Diagnostics unavailable: {_diag_err}')
    diag_engine = None
    _DIAG_AVAILABLE = False


@sio.event
async def run_diagnostics(sid, data=None):
    if not diag_engine:
        await sio.emit('diagnostics_report', {'error': 'Diagnostics unavailable'}, room=sid)
        return
    report = await asyncio.to_thread(diag_engine.generate_report)
    await sio.emit('diagnostics_report', report, room=sid)

@sio.event
async def get_perf_history(sid, data=None):
    hours = (data or {}).get('hours', 24)
    try:
        history = await asyncio.to_thread(system_monitor.get_perf_history, hours)
        await sio.emit('perf_history', {'history': history}, room=sid)
    except Exception as e:
        await sio.emit('perf_history', {'history': [], 'error': str(e)}, room=sid)


if __name__ == "__main__":
    uvicorn.run(
        "server:app_socketio",
        host="127.0.0.1",
        port=8000,
        reload=False,
        loop="asyncio",
    )
