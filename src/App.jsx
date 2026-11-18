import React, { useEffect, useRef, useState, useMemo } from 'react';
import io from 'socket.io-client';
import { AnimatePresence, motion } from 'framer-motion';
import {
    Mic, MicOff, Settings, X, Minus, Square as Maximize, Power, Mail, Target, Send,
    Activity, CloudSun, MapPin, MonitorSmartphone, Check, Loader2, Ban, Sparkles, Eye, Crosshair, FolderOpen,
    Cpu, Clock, Hammer, MailCheck, CalendarDays, BookOpen, ThumbsUp, ThumbsDown, Database,
} from 'lucide-react';

import Orb3D from './components/Orb3D';
import Stage, { ConfirmCard } from './components/holo/Stage';
import { FocusHUD, FocusLauncher, FocusTimeline, FocusHistory, NudgeBubble } from './components/holo/Focus';
import EmailWindow from './components/EmailWindow';
import ComposeWindow from './components/ComposeWindow';
import SettingsWindow from './components/SettingsWindow';
import ErrorBoundary from './ErrorBoundary';
import { sfx } from './sfx';
import { OrbHud, ScreenFrame } from './components/holo/Hud';
import Boot from './components/holo/Boot';
import Bluey from './components/holo/Bluey';
import { HardwareStrip, ProcessList } from './components/holo/SystemPanel';

const socket = io('http://localhost:8000');
let ipcRenderer = null;
try {
    ipcRenderer = window.require('electron').ipcRenderer;
} catch (e) {
    console.warn('Electron IPC unavailable (running in a browser):', e.message);
}

const TOOL_META = {
    read_emails: { label: 'Scanning inbox', icon: Mail },
    show_email: { label: 'Opening email', icon: Mail },
    reply_to_email: { label: 'Replying in thread', icon: Send },
    send_email: { label: 'Sending email', icon: Send },
    get_weather: { label: 'Weather lock', icon: CloudSun },
    show_map: { label: 'Map lock', icon: MapPin },
    gods_eye: { label: "God's eye scan", icon: Eye },
    computer_point: { label: 'Pointing', icon: Crosshair },
    computer_open: { label: 'Opening', icon: FolderOpen },
    start_focus_session: { label: 'Focus session', icon: Target },
    control_focus_session: { label: 'Focus control', icon: Target },
    get_datetime: { label: 'Checking date & time', icon: Clock },
    get_system_stats: { label: 'Laptop health', icon: Cpu },
    sri_lanka_weather: { label: 'Sri Lanka weather', icon: CloudSun },
    omniroute_task: { label: 'Building with OmniRoute', icon: Hammer },
    draft_replies_for_all: { label: 'Drafting replies', icon: Mail },
    send_reply_drafts: { label: 'Sending replies', icon: MailCheck },
    show_nexora_window: { label: 'Waking up', icon: Sparkles },
};
const toolMeta = (name) => TOOL_META[name] || (name?.startsWith('computer_')
    ? { label: name.replace('computer_', '').replace(/_/g, ' '), icon: MonitorSmartphone }
    : { label: name, icon: Sparkles });

function StatusChip({ on, label, warn }) {
    return (
        <span className={`flex items-center gap-1.5 px-2 py-0.5 rounded-full border text-[10px] tracking-[0.15em] font-mono
            ${on ? 'border-emerald-400/30 text-emerald-200 bg-emerald-400/5' : warn ? 'border-amber-400/30 text-amber-200 bg-amber-400/5' : 'border-rose-400/30 text-rose-200 bg-rose-400/5'}`}>
            <span className={`w-1.5 h-1.5 rounded-full ${on ? 'bg-emerald-400' : warn ? 'bg-amber-400' : 'bg-rose-400'} ${on ? 'animate-pulse' : ''}`} />
            {label}
        </span>
    );
}

function DockButton({ icon: Icon, label, onClick, active, tone = 'cyan', disabled }) {
    const tones = {
        cyan: 'text-cyan-200 border-cyan-300/40 bg-cyan-400/10',
        emerald: 'text-emerald-200 border-emerald-300/40 bg-emerald-400/10',
        rose: 'text-rose-200 border-rose-300/40 bg-rose-400/10',
        fuchsia: 'text-fuchsia-200 border-fuchsia-300/40 bg-fuchsia-400/10',
    };
    return (
        <button onClick={onClick} disabled={disabled} title={label} aria-label={label}
            className={`group flex flex-col items-center gap-1 w-16 disabled:opacity-40`}>
            <span className={`w-12 h-12 rounded-2xl border flex items-center justify-center transition-all group-hover:scale-105
                ${active ? tones[tone] : 'text-slate-300 border-white/10 bg-white/[0.03] group-hover:bg-white/[0.07]'}`}>
                <Icon size={20} />
            </span>
            <span className="text-[10px] tracking-wider text-slate-400 group-hover:text-slate-200">{label}</span>
        </button>
    );
}

function App() {
    const [socketConnected, setSocketConnected] = useState(socket.connected);
    const [isApaReady, setIsApaReady] = useState(false);
    const [isConnected, setIsConnected] = useState(true);   // user wants voice on
    const [isMuted, setIsMuted] = useState(false);
    const [messages, setMessages] = useState([]);
    const [inputValue, setInputValue] = useState('');
    const [aiAudioData, setAiAudioData] = useState([0]);
    const [micLevel, setMicLevel] = useState(0);
    const [micDevices, setMicDevices] = useState([]);
    const [speakerDevices, setSpeakerDevices] = useState([]);
    const [selectedMicId, setSelectedMicId] = useState(() => localStorage.getItem('selectedMicId') || '');
    const [selectedSpeakerId, setSelectedSpeakerId] = useState(() => localStorage.getItem('selectedSpeakerId') || '');

    const [widgets, setWidgets] = useState([]);
    const [tasks, setTasks] = useState([]);
    const [computer, setComputer] = useState({ steps: [], shot: null });
    const [confirmQueue, setConfirmQueue] = useState([]);
    const [focus, setFocus] = useState({ active: false });
    const [focusHistory, setFocusHistory] = useState([]);
    const [nudge, setNudge] = useState(null);
    const [blueySay, setBlueySay] = useState(null);
    const [blueyDesktop, setBlueyDesktop] = useState(true);
    const blueyTimer = useRef(null);
    const blueyTalk = (text, ms = 9000) => {
        setBlueySay(text);
        clearTimeout(blueyTimer.current);
        blueyTimer.current = setTimeout(() => setBlueySay(null), ms);
    };
    const [escalationCount, setEscalationCount] = useState(0);

    const [showSettings, setShowSettings] = useState(false);
    const [showInbox, setShowInbox] = useState(false);
    const [showCompose, setShowCompose] = useState(false);
    const [showFocus, setShowFocus] = useState(false);
    const [showKnowledge, setShowKnowledge] = useState(false);
    const [documents, setDocuments] = useState([]);
    const [ragError, setRagError] = useState(null);
    const [rightTab, setRightTab] = useState('log');
    const [memories, setMemories] = useState([]);
    const [feedbackStats, setFeedbackStats] = useState({ total: 0, positive: 0, negative: 0, ratio: 0 });
    const [feedbackList, setFeedbackList] = useState([]);
    const [datasets, setDatasets] = useState([]);
    const [missions, setMissions] = useState([]);
    const [missionDetail, setMissionDetail] = useState(null);
    const [eventRules, setEventRules] = useState([]);
    const [eventHistory, setEventHistory] = useState([]);
    const [kgData, setKgData] = useState({ entities: [], stats: { node_count: 0, edge_count: 0 } });
    const [kgSearch, setKgSearch] = useState('');
    const [kgSearchResults, setKgSearchResults] = useState([]);
    const [diagReport, setDiagReport] = useState(null);
    const [diagRunning, setDiagRunning] = useState(false);
    const [perfHistory, setPerfHistory] = useState([]);
    const [evalState, setEvalState] = useState({ running: false, summary: '' });
    const [recs, setRecs] = useState(null);
    const [usageCollection, setUsageCollection] = useState(false);
    const [simResult, setSimResult] = useState(null);
    const [clock, setClock] = useState(new Date());
    const [sysStats, setSysStats] = useState(null);
    const [booting, setBooting] = useState(true);
    const [mouse, setMouse] = useState({ x: 0, y: 0 });
    useEffect(() => {
        let raf = 0;
        const mv = (e) => {
            cancelAnimationFrame(raf);
            raf = requestAnimationFrame(() => setMouse({ x: e.clientX / window.innerWidth - 0.5, y: e.clientY / window.innerHeight - 0.5 }));
        };
        window.addEventListener('mousemove', mv);
        return () => window.removeEventListener('mousemove', mv);
    }, []);

    const widgetId = useRef(0);
    const chatEnd = useRef(null);
    const audioCtx = useRef(null);
    const micRaf = useRef(null);
    const micSrc = useRef(null);
    const autoStarted = useRef(false);
    const isConnectedRef = useRef(isConnected);

    const addMessage = (sender, text) => setMessages((p) => [...p, { sender, text, time: new Date().toLocaleTimeString() }]);
    const pushWidget = (w) => {
        widgetId.current += 1;
        const nw = { id: `w-${widgetId.current}`, ...w };
        setWidgets((prev) => [...prev.filter((x) => x.type !== w.type), nw]);
        return nw;
    };
    const dismissWidget = (id) => setWidgets((prev) => prev.filter((w) => w.id !== id));

    /* ------------------------------------------------------ socket ---- */
    useEffect(() => {
        const t = setInterval(() => setClock(new Date()), 1000);
        socket.on('connect', () => { setSocketConnected(true); socket.emit('get_settings'); socket.emit('focus_history'); setTimeout(() => blueyTalk("Hi! I'm Bluey, your focus buddy. Tap me to start focusing!", 7000), 8200); });
        socket.on('disconnect', () => { setSocketConnected(false); setIsApaReady(false); });
        socket.on('status', (d) => {
            if (d.msg === 'Nexora Started' || d.msg === 'Nexora Already Running') setIsApaReady(true);
            else if (d.msg === 'Nexora Stopped') setIsApaReady(false);
            if (!['Connected to Nexora Backend'].includes(d.msg)) addMessage('System', d.msg);
        });
        socket.on('audio_data', (d) => setAiAudioData(d.data || [0]));
        socket.on('error', (d) => pushWidget({ type: 'error', data: { title: 'System fault', message: d.msg } }));
        socket.on('transcription', (d) => {
            setMessages((prev) => {
                const last = prev[prev.length - 1];
                if (last && last.sender === d.sender) return [...prev.slice(0, -1), { ...last, text: last.text + d.text }];
                return [...prev, { sender: d.sender, text: d.text, time: new Date().toLocaleTimeString() }];
            });
        });
        // Real approval gate (the old UI auto-approved every request).
        socket.on('tool_confirmation_request', (d) => { if (d?.id) { sfx.alert(); setConfirmQueue((q) => [...q, d]); } });
        socket.on('widget_update', (d) => {
            if (!d?.type) return;
            if (d.type === 'task') {
                setTasks((prev) => {
                    const i = prev.findIndex((t) => t.id === d.data.id);
                    const entry = { ...(i >= 0 ? prev[i] : { started: Date.now() }), ...d.data, updated: Date.now() };
                    if (i >= 0) { const n = [...prev]; n[i] = entry; return n; }
                    sfx.blip();
                    return [...prev, entry].slice(-60);
                });
                return;
            }
            if (d.type === 'computer_control') {
                setComputer((c) => {
                    const steps = [...c.steps];
                    const last = steps[steps.length - 1];
                    if (last && last.tool === d.data.tool && last.state === 'active') steps[steps.length - 1] = d.data;
                    else steps.push(d.data);
                    return { ...c, steps: steps.slice(-30) };
                });
                setWidgets((prev) => (prev.some((w) => w.type === 'computer') ? prev : [...prev, { id: `w-c${Date.now()}`, type: 'computer' }]));
                return;
            }
            // area details arrive a few seconds after the map; fill them into the open map card
            if (d.type === 'map_details') {
                setWidgets((prev) => prev.map((w) => (w.type === 'map' && w.data?.lat === d.data.lat && w.data?.lon === d.data.lon
                    ? { ...w, data: { ...w.data, details: d.data.details } } : w)));
                return;
            }
            if (d.type === 'show_window') { ipcRenderer?.send('window-show'); return; }
            if (d.type === 'system_focus') { setRightTab('system'); return; }
            if (d.type === 'computer_screenshot') { setComputer((c) => ({ ...c, shot: d.data.image })); return; }
            if (d.type === 'focus_nudge') { sfx.chirp(); setNudge({ id: Date.now(), ...d.data }); blueyTalk(d.data.text + (d.data.point_text ? ` Look at: "${d.data.point_text}"` : ''), 12000); return; }
            const w = pushWidget(d);
            if (d.type === 'error') setTimeout(() => dismissWidget(w.id), 15000);
            else sfx.summon();
            if (d.type === 'email_summary') setEscalationCount(d.data?.escalation_count || 0);
        });
        socket.on('focus_state', (d) => setFocus((prev) => {
            if (d.active && !prev.active) blueyTalk(`Focus mode on: ${d.task}. I've got your back!`, 6000);
            if (!d.active && prev.active) blueyTalk('Session done. Nice work!', 6000);
            return d;
        }));
        socket.on('bluey_state', (d) => setBlueyDesktop(!!d.on));
        socket.on('system_stats', setSysStats);
        socket.on('focus_history', (d) => setFocusHistory(d.sessions || []));
        socket.on('memories_list', (d) => setMemories(d.memories || []));
        socket.on('memory_deleted', (d) => { if (d.ok) setMemories(prev => prev.filter(m => m.id !== d.id)); });
        socket.on('documents_list', (d) => setDocuments(d.documents || []));
        socket.on('rag_error', (d) => setRagError(d.error));
        socket.on('feedback_stats', setFeedbackStats);
        socket.on('feedback_list', (d) => setFeedbackList(d.feedback || []));
        socket.on('datasets_list', (d) => setDatasets(d.datasets || []));
        socket.on('missions_list', (d) => setMissions(d.missions || []));
        socket.on('mission_progress', () => socket.emit('get_missions'));
        socket.on('mission_error', (d) => addMessage('System', `Mission not started: ${d.error}`));
        socket.on('event_rule_error', (d) => addMessage('System', `Rule not created: ${d.error}`));
        socket.on('nexora_notification', (d) => addMessage('Nexora', `${d.title}: ${d.message}`));
        socket.on('simulation_result', setSimResult);
        socket.on('eval_results', (d) => setEvalState({ running: !!d.running, summary: d.summary || '' }));
        socket.on('recommendations', setRecs);
        socket.on('settings_loaded', (d) => setUsageCollection(!!d.settings?.usage_data_collection));
        socket.on('settings_updated', (d) => setUsageCollection(!!d.settings?.usage_data_collection));
        socket.on('mission_created', () => socket.emit('get_missions'));
        socket.on('mission_cancelled', () => socket.emit('get_missions'));
        socket.on('mission_detail', (d) => setMissionDetail(d.mission));
        socket.on('event_rules_list', (d) => setEventRules(d.rules || []));
        socket.on('event_history', (d) => setEventHistory(d.history || []));
        socket.on('event_fired', (d) => {
            setEventHistory(prev => [d, ...prev].slice(0, 20));
        });
        socket.on('kg_data', setKgData);
        socket.on('kg_search_results', (d) => setKgSearchResults(d.results || []));
        socket.on('diagnostics_report', (d) => { setDiagReport(d); setDiagRunning(false); });
        socket.on('perf_history', (d) => setPerfHistory(d.history || []));
        socket.on('focus_ended', (d) => {
            addMessage('System', `Focus session ended (${d.reason}).`);
            socket.emit('focus_history');
        });
        socket.on('wake_word_detected', () => {
            // Show window via IPC
            ipcRenderer?.send('window-show');
            // Auto-start voice if not already running
            if (!isConnectedRef.current) {
                const idx = micDevices.findIndex((d) => d.deviceId === selectedMicId);
                socket.emit('start_audio', { device_index: idx >= 0 ? idx : null });
                setIsConnected(true);
                setIsMuted(false);
            }
        });

        navigator.mediaDevices?.getUserMedia({ audio: true })
            .then((s) => { s.getTracks().forEach((t) => t.stop()); return navigator.mediaDevices.enumerateDevices(); })
            .catch(() => navigator.mediaDevices?.enumerateDevices())
            .then((devs = []) => {
                const ins = devs.filter((d) => d.kind === 'audioinput');
                const outs = devs.filter((d) => d.kind === 'audiooutput');
                setMicDevices(ins);
                setSpeakerDevices(outs);
                const savedMic = localStorage.getItem('selectedMicId');
                if (!(savedMic && ins.some((d) => d.deviceId === savedMic)) && ins.length) setSelectedMicId(ins[0].deviceId);
                const savedSpk = localStorage.getItem('selectedSpeakerId');
                if (!(savedSpk && outs.some((d) => d.deviceId === savedSpk)) && outs.length) setSelectedSpeakerId(outs[0].deviceId);
            });

        return () => {
            clearInterval(t);
            ['connect', 'disconnect', 'status', 'audio_data', 'error', 'transcription', 'tool_confirmation_request',
                'widget_update', 'focus_state', 'focus_history', 'focus_ended', 'bluey_state', 'system_stats',
                'memories_list', 'memory_deleted', 'wake_word_detected', 'documents_list', 'rag_error',
                'feedback_stats', 'feedback_list', 'datasets_list',
                'missions_list', 'mission_progress', 'mission_created', 'mission_cancelled', 'mission_detail',
                'event_rules_list', 'event_history', 'event_fired',
                'kg_data', 'kg_search_results',
                'diagnostics_report', 'perf_history', 'mission_error', 'event_rule_error', 'nexora_notification',
                'simulation_result', 'eval_results', 'recommendations', 'settings_loaded', 'settings_updated'].forEach((e) => socket.off(e));
        };
    }, []);

    // Auto-start voice once backend + mic are ready.
    useEffect(() => {
        if (isConnected && socketConnected && micDevices.length > 0 && !autoStarted.current) {
            autoStarted.current = true;
            const idx = micDevices.findIndex((d) => d.deviceId === selectedMicId);
            const dev = micDevices[idx];
            socket.emit('start_audio', { device_index: idx >= 0 ? idx : null, device_name: dev ? dev.label : null, muted: isMuted });
        }
        if (!socketConnected) autoStarted.current = false;
    }, [isConnected, socketConnected, micDevices, selectedMicId]);

    useEffect(() => { if (selectedMicId) localStorage.setItem('selectedMicId', selectedMicId); }, [selectedMicId]);
    useEffect(() => { if (selectedSpeakerId) localStorage.setItem('selectedSpeakerId', selectedSpeakerId); }, [selectedSpeakerId]);
    useEffect(() => { isConnectedRef.current = isConnected; }, [isConnected]);
    useEffect(() => { chatEnd.current?.scrollIntoView({ behavior: 'smooth' }); }, [messages]);

    // Mic level for the orb.
    useEffect(() => {
        if (!selectedMicId || !navigator.mediaDevices) return;
        let stopped = false;
        navigator.mediaDevices.getUserMedia({ audio: { deviceId: { exact: selectedMicId } } }).then((stream) => {
            if (stopped) return;
            audioCtx.current = new (window.AudioContext || window.webkitAudioContext)();
            const an = audioCtx.current.createAnalyser();
            an.fftSize = 64;
            micSrc.current = audioCtx.current.createMediaStreamSource(stream);
            micSrc.current.connect(an);
            const buf = new Uint8Array(an.frequencyBinCount);
            let last = 0;
            const loop = (ts) => {
                an.getByteFrequencyData(buf);
                if (ts - last > 50) { last = ts; setMicLevel(buf.reduce((a, b) => a + b, 0) / buf.length / 255); }
                micRaf.current = requestAnimationFrame(loop);
            };
            micRaf.current = requestAnimationFrame(loop);
        }).catch(() => {});
        return () => {
            stopped = true;
            if (micRaf.current) cancelAnimationFrame(micRaf.current);
            try { micSrc.current?.disconnect(); audioCtx.current?.close(); } catch (e) { /* ignore */ }
        };
    }, [selectedMicId]);

    /* ----------------------------------------------------- actions ---- */
    const togglePower = () => {
        if (isConnected) { socket.emit('stop_audio'); setIsConnected(false); setIsMuted(false); }
        else {
            const idx = micDevices.findIndex((d) => d.deviceId === selectedMicId);
            socket.emit('start_audio', { device_index: idx >= 0 ? idx : null });
            setIsConnected(true); setIsMuted(false);
        }
    };
    const toggleMute = () => {
        if (!isConnected) return;
        socket.emit(isMuted ? 'resume_audio' : 'pause_audio');
        setIsMuted(!isMuted);
    };
    const sendText = (text) => {
        if (!text.trim()) return;
        socket.emit('user_input', { text });
        addMessage('User', text);
    };
    const resolveConfirm = (ok) => {
        const req = confirmQueue[0];
        if (!req) return;
        socket.emit('resolve_confirmation', { id: req.id, confirmed: ok });
        setConfirmQueue((q) => q.slice(1));
    };
    const focusControl = (action) => socket.emit('focus_control', { action });
    const openInbox = () => { setShowInbox(true); socket.emit('fetch_today_emails'); };

    const handleClose = () => {
        const close = () => ipcRenderer?.send('window-close');
        if (socket.connected) { socket.emit('shutdown', {}, close); setTimeout(close, 500); } else close();
    };

    /* ------------------------------------------------------- state ---- */
    const audioAmp = aiAudioData.reduce((a, b) => a + b, 0) / Math.max(1, aiAudioData.length) / 255;
    const mainSummon = widgets.some((w) => ['weather', 'map', 'email_summary', 'email_view', 'computer', 'gods_eye', 'lk_weather', 'lk_news'].includes(w.type));
    // Layout follows the summon with a lag on recall: panels fly back into the docked orb first,
    // then the orb glides home and the sidebars reopen (otherwise panels chase an orb that has left).
    const [summonLayout, setSummonLayout] = useState(false);
    useEffect(() => {
        if (mainSummon) { setSummonLayout(true); return undefined; }
        const t = setTimeout(() => setSummonLayout(false), 520);
        return () => clearTimeout(t);
    }, [mainSummon]);
    const running = tasks.some((t) => t.state === 'running');
    const lastMsg = messages[messages.length - 1];

    let orbState = 'idle';
    if (!socketConnected) orbState = 'offline';
    else if (confirmQueue.length) orbState = 'alert';
    else if (mainSummon) orbState = 'summoning';
    else if (isApaReady && audioAmp > 0.02) orbState = 'speaking';
    else if (running || (lastMsg && lastMsg.sender === 'User')) orbState = 'thinking';
    else if (focus.active && !focus.paused) orbState = 'focus';
    else if (isApaReady && isConnected && !isMuted) orbState = 'listening';

    const orbLevel = orbState === 'speaking' ? Math.min(1, audioAmp * 3) : orbState === 'listening' ? Math.min(1, micLevel * 1.6) : 0;
    const STATE_TEXT = {
        offline: 'BACKEND OFFLINE', idle: isConnected ? 'STANDING BY' : 'VOICE OFF', listening: 'LISTENING',
        thinking: 'PROCESSING', speaking: 'SPEAKING', summoning: 'DISPLAYING', focus: 'GUARDING YOUR FOCUS', alert: 'AWAITING AUTHORISATION',
    };
    const caption = useMemo(() => {
        const m = [...messages].reverse().find((x) => x.sender === 'ADA');
        return m ? m.text.slice(-160) : '';
    }, [messages]);

    /* ------------------------------------------------------ render ---- */
    return (
        <ErrorBoundary>
            <div className="h-screen w-screen overflow-hidden app-bg text-slate-200 font-mono select-none flex flex-col">
                <div className="cinema-fx" aria-hidden="true" />
                {/* ---------- top bar ---------- */}
                <header className="h-12 shrink-0 flex items-center gap-4 px-4 border-b border-white/5 bg-black/30 backdrop-blur-md" style={{ WebkitAppRegion: 'drag' }}>
                    <span className="text-base font-bold tracking-[0.35em] bg-gradient-to-r from-cyan-200 to-fuchsia-200 bg-clip-text text-transparent">NEXORA</span>
                    <span className="text-[9px] px-1.5 py-0.5 rounded border border-white/10 text-slate-500">V2.1</span>
                    <div className="flex gap-2 ml-2">
                        <StatusChip on={socketConnected} label={socketConnected ? 'CORE' : 'CORE OFFLINE'} />
                        <StatusChip on={isApaReady} warn={socketConnected && !isApaReady} label={isApaReady ? (isMuted ? 'VOICE · MUTED' : 'VOICE') : 'VOICE OFF'} />
                        {focus.active && <StatusChip on={!focus.paused} warn={focus.paused} label={focus.paused ? 'FOCUS · PAUSED' : 'FOCUS'} />}
                        {escalationCount > 0 && <StatusChip warn label={`${escalationCount} ESCALATED`} />}
                    </div>
                    <div className="flex-1" />
                    <span className="flex items-center gap-2 text-[11px] text-slate-400 tabular-nums">
                        <CalendarDays size={12} className="text-violet-300/70" />
                        {clock.toLocaleDateString(undefined, { weekday: 'short', day: '2-digit', month: 'short', year: 'numeric' })}
                        <span className="text-violet-200">{clock.toLocaleTimeString()}</span>
                    </span>
                    <div className="flex gap-1" style={{ WebkitAppRegion: 'no-drag' }}>
                        <button aria-label="Minimise" title="Minimise" onClick={() => ipcRenderer?.send('window-minimize')} className="p-1.5 rounded hover:bg-white/10 text-slate-300"><Minus size={16} /></button>
                        <button aria-label="Maximise" title="Maximise" onClick={() => ipcRenderer?.send('window-maximize')} className="p-1.5 rounded hover:bg-white/10 text-slate-300"><Maximize size={14} /></button>
                        <button aria-label="Close" title="Close" onClick={handleClose} className="p-1.5 rounded hover:bg-rose-500/20 text-rose-300"><X size={16} /></button>
                    </div>
                </header>

                <div className="flex-1 min-h-0 flex relative">
                    {/* ---------- left: conversation ---------- */}
                    <motion.aside animate={{ width: summonLayout ? 0 : 340, opacity: summonLayout ? 0 : 1, x: summonLayout ? -60 : 0 }} transition={{ type: 'spring', stiffness: 90, damping: 20 }} className="shrink-0 flex flex-col border-r border-white/5 bg-black/20 backdrop-blur-sm overflow-hidden"><div className="w-[340px] h-full flex flex-col">
                        <div className="px-4 py-3 text-[10px] tracking-[0.3em] text-slate-500 border-b border-white/5">CONVERSATION</div>
                        <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-3 space-y-3">
                            {messages.length === 0 && (
                                <div className="text-xs text-slate-500 leading-relaxed mt-6">
                                    <div className="text-slate-300 mb-2">Try saying or typing:</div>
                                    {['“Check my emails”', '“Scan flights over Colombo”', '“Any earthquakes today?”', '“Where is the ISS right now?”', '“Open my assingment folder on the Desktop”', '“Point at the Start button”', '“Start Bluey focus mode: writing my report for 45 minutes”'].map((s) => (
                                        <button key={s} onClick={() => sendText(s.replace(/[“”]/g, ''))} className="block w-full text-left px-3 py-2 mb-1.5 rounded-lg border border-white/5 hover:border-cyan-300/30 hover:text-cyan-100 transition-colors">{s}</button>
                                    ))}
                                </div>
                            )}
                            {messages.map((m, i) => (
                                <div key={i} className={m.sender === 'User' ? 'text-right' : ''}>
                                    <div className={`text-[9px] tracking-[0.2em] mb-0.5 ${m.sender === 'ADA' ? 'text-cyan-400/70' : m.sender === 'User' ? 'text-fuchsia-300/70' : 'text-slate-600'}`}>
                                        {m.sender === 'ADA' ? 'NEXORA' : m.sender === 'User' ? 'YOU' : 'SYSTEM'} · {m.time}
                                    </div>
                                    <div className={`inline-block text-[13px] leading-relaxed px-3 py-2 rounded-xl max-w-full text-left font-sans
                                        ${m.sender === 'ADA' ? 'bg-cyan-400/[0.06] border border-cyan-300/10 text-slate-100' : m.sender === 'User' ? 'bg-fuchsia-400/[0.08] border border-fuchsia-300/10 text-slate-100' : 'text-slate-500 text-xs'}`}>
                                        {m.text}
                                    </div>
                                </div>
                            ))}
                            <div ref={chatEnd} />
                        </div>
                        <div className="p-3 border-t border-white/5 flex gap-2">
                            <input value={inputValue} onChange={(e) => setInputValue(e.target.value)}
                                onKeyDown={(e) => { if (e.key === 'Enter') { sendText(inputValue); setInputValue(''); } }}
                                placeholder={isApaReady ? 'Type a command…' : 'Start voice to chat…'} disabled={!isApaReady}
                                className="holo-input flex-1 disabled:opacity-50" />
                            <button aria-label="Send" title="Send" onClick={() => { sendText(inputValue); setInputValue(''); }} disabled={!isApaReady || !inputValue.trim()}
                                className="px-3 rounded-lg border border-cyan-300/30 text-cyan-200 hover:bg-cyan-400/10 disabled:opacity-30"><Send size={15} /></button>
                        </div>
                    </div></motion.aside>

                    {/* ---------- centre: the orb ---------- */}
                    <main className="flex-1 min-w-0 relative flex flex-col items-center">
                        <ScreenFrame state={orbState} />
                        <motion.div className="flex-1 w-full relative" animate={{ x: summonLayout ? -window.innerWidth * 0.28 : 0 }} transition={{ type: 'spring', stiffness: 60, damping: 16 }}>
                            <motion.div className="absolute inset-0 flex items-center justify-center pointer-events-none"
                                animate={{ x: mouse.x * 26, y: mouse.y * 18, scale: summonLayout ? 0.72 : 1 }} transition={{ type: 'spring', stiffness: 50, damping: 18 }}>
                                <OrbHud state={orbState} size={Math.round(window.innerHeight * 0.74)} />
                            </motion.div>
                            <motion.div className="absolute inset-0 overflow-hidden pointer-events-none" animate={{ x: mouse.x * -10, y: mouse.y * -8 }} transition={{ type: 'spring', stiffness: 50, damping: 18 }}>
                                {/* fixed-size canvas, centred: it never resizes while the sidebars animate, so the orb glides instead of snapping */}
                                <Orb3D state={orbState} level={orbLevel} compact={summonLayout} className="absolute top-0 bottom-0 left-1/2 -translate-x-1/2 orb-fx"
                                    style={{ width: Math.round(window.innerHeight * 1.6) }} />
                            </motion.div>
                            <AnimatePresence><FocusHUD focus={focus} onControl={focusControl} /></AnimatePresence>
                        </motion.div>
                        <motion.div className="shrink-0 text-center -mt-6 mb-3 px-6 z-10" animate={{ x: summonLayout ? -window.innerWidth * 0.28 : 0, opacity: summonLayout ? 0.85 : 1 }} transition={{ type: 'spring', stiffness: 60, damping: 16 }}>
                            <div className={`text-xs tracking-[0.5em] ${orbState === 'offline' ? 'text-rose-300' : orbState === 'alert' ? 'text-amber-300' : 'text-cyan-200'}`}>{STATE_TEXT[orbState]}</div>
                            <div className="h-10 mt-2 text-sm text-slate-300 font-sans max-w-[640px] mx-auto line-clamp-2">
                                {!socketConnected ? 'Start the Python backend (backend/server.py) to bring Nexora online.' : caption}
                            </div>
                        </motion.div>
                        <motion.div className="absolute left-12 bottom-24 z-[45]" initial={{ opacity: 0, scale: 0.4 }}
                            animate={{ opacity: booting ? 0 : 1, scale: booting ? 0.4 : 1, transition: { delay: booting ? 0 : 0.6, type: 'spring', stiffness: 160, damping: 12 } }}>
                            <Bluey size={110} say={blueySay}
                                mood={nudge && Date.now() - nudge.id < 12000 ? 'worried' : focus.active && !focus.paused ? 'focus' : !socketConnected || !isConnected ? 'sleepy' : 'happy'}
                                onClick={() => (focus.active ? blueyTalk(`${Math.ceil((focus.remaining || 0) / 60)} min left on "${focus.task}". You got this!`, 5000) : setShowFocus(true))} />
                        </motion.div>
                        <nav className="shrink-0 mb-5 z-10 flex gap-2 px-4 py-3 rounded-3xl border border-white/10 bg-black/40 backdrop-blur-md">
                            <DockButton icon={Power} label={isConnected ? 'Voice on' : 'Voice off'} onClick={togglePower} active={isConnected} tone="emerald" disabled={!socketConnected} />
                            <DockButton icon={isMuted ? MicOff : Mic} label={isMuted ? 'Muted' : 'Mic'} onClick={toggleMute} active={isConnected && !isMuted} tone={isMuted ? 'rose' : 'cyan'} disabled={!isConnected || !socketConnected} />
                            <DockButton icon={Target} label="Focus" onClick={() => (focus.active ? setRightTab('focus') : setShowFocus(true))} active={focus.active} tone="emerald" disabled={!socketConnected} />
                            <DockButton icon={Mail} label="Inbox" onClick={openInbox} active={showInbox} tone="fuchsia" disabled={!socketConnected} />
                            <DockButton icon={BookOpen} label="Knowledge" onClick={() => { setShowKnowledge(true); socket.emit('list_documents'); }} active={showKnowledge} tone="fuchsia" disabled={!socketConnected} />
                            <DockButton icon={Sparkles} label={blueyDesktop ? 'Bluey on' : 'Bluey off'} active={blueyDesktop} tone="fuchsia" disabled={!socketConnected}
                                onClick={() => { socket.emit('bluey', { on: !blueyDesktop }); setBlueyDesktop(!blueyDesktop); }} />
                            <DockButton icon={Settings} label="Settings" onClick={() => setShowSettings(true)} active={showSettings} />
                        </nav>
                    </main>

                    {/* ---------- right: activity ---------- */}
                    <motion.aside animate={{ width: summonLayout ? 0 : 320, opacity: summonLayout ? 0 : 1, x: summonLayout ? 60 : 0 }} transition={{ type: 'spring', stiffness: 90, damping: 20 }} className="shrink-0 flex flex-col border-l border-white/5 bg-black/20 backdrop-blur-sm overflow-hidden"><div className="w-[320px] h-full flex flex-col">
                        <HardwareStrip stats={sysStats} />
                        <div className="flex border-b border-white/5">
                            {[['log', 'LOG', Activity], ['system', 'TASKS', Cpu], ['focus', 'FOCUS', Target], ['memory', 'MEMORY', Sparkles], ['train', 'TRAIN', Database], ['missions', 'MISSIONS', Hammer], ['events', 'EVENTS', Activity], ['diag', 'DIAG', Activity]].map(([k, l, I]) => (
                                <button key={k} onClick={() => { setRightTab(k); if (k === 'train') { socket.emit('get_feedback_stats'); socket.emit('list_datasets'); } else if (k === 'memory') { socket.emit('get_memories'); socket.emit('get_knowledge_graph'); } else if (k === 'missions') socket.emit('get_missions'); else if (k === 'events') socket.emit('list_event_rules'); else if (k === 'diag') { socket.emit('get_perf_history'); socket.emit('get_eval_results'); socket.emit('get_settings'); } }}
                                    className={`flex-1 py-3 text-[10px] tracking-[0.25em] flex items-center justify-center gap-1.5 ${rightTab === k ? 'text-cyan-200 border-b border-cyan-300' : 'text-slate-500 hover:text-slate-300'}`}>
                                    <I size={12} /> {l}
                                </button>
                            ))}
                        </div>
                        <div className="flex-1 overflow-y-auto custom-scrollbar p-4">
                            {rightTab === 'system' ? <ProcessList stats={sysStats} /> : rightTab === 'log' ? (
                                tasks.length === 0 ? <div className="text-xs text-slate-500">Every task Nexora runs shows up here, live.</div> : (
                                    <div className="space-y-2">
                                        {[...tasks].reverse().map((t) => {
                                            const meta = toolMeta(t.tool);
                                            const I = meta.icon;
                                            return (
                                                <motion.div key={t.id} initial={{ opacity: 0, x: 20 }} animate={{ opacity: 1, x: 0 }}
                                                    className="rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2">
                                                    <div className="flex items-center gap-2 text-xs">
                                                        <I size={13} className="text-cyan-300/80" />
                                                        <span className="flex-1 capitalize text-slate-200 truncate">{meta.label}</span>
                                                        {t.state === 'running' && <Loader2 size={13} className="animate-spin text-amber-300" />}
                                                        {t.state === 'done' && <Check size={13} className="text-emerald-400" />}
                                                        {t.state === 'denied' && <Ban size={13} className="text-rose-400" />}
                                                    </div>
                                                    <div className="text-[10px] text-slate-500 mt-0.5 truncate">
                                                        {new Date(t.started).toLocaleTimeString()} · {t.detail || Object.values(t.args || {}).join(' · ') || t.tool}
                                                    </div>
                                                </motion.div>
                                            );
                                        })}
                                    </div>
                                )
                            ) : rightTab === 'memory' ? (
                                <div className="space-y-3">
                                    <div className="flex items-center justify-between">
                                        <div className="text-[10px] tracking-[0.25em] text-slate-500">LONG-TERM MEMORY</div>
                                        <button
                                            onClick={() => { socket.emit('get_memories'); }}
                                            className="text-[9px] text-cyan-400 hover:text-cyan-200 tracking-wider"
                                        >REFRESH</button>
                                    </div>
                                    {memories.length === 0 ? (
                                        <div className="text-xs text-slate-500">No memories yet. Tell Nexora to remember something.</div>
                                    ) : (
                                        memories.map(mem => (
                                            <div key={mem.id} className="rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2">
                                                <div className="flex items-start justify-between gap-2">
                                                    <div className="flex-1">
                                                        <div className="text-[9px] tracking-[0.2em] text-fuchsia-300/70 mb-0.5">{(mem.category || 'fact').toUpperCase()}</div>
                                                        <div className="text-xs text-slate-200 leading-relaxed">{mem.content}</div>
                                                    </div>
                                                    <button
                                                        onClick={() => socket.emit('delete_memory', { id: mem.id })}
                                                        className="shrink-0 p-1 rounded hover:bg-rose-400/20 text-rose-400/60 hover:text-rose-300 transition-colors"
                                                        title="Delete memory"
                                                    ><X size={11} /></button>
                                                </div>
                                            </div>
                                        ))
                                    )}
                                    {/* Knowledge Graph section */}
                                    <div className="mt-4 pt-4 border-t border-white/5">
                                        <div className="flex items-center justify-between mb-2">
                                            <div className="text-[10px] tracking-[0.25em] text-slate-500">KNOWLEDGE GRAPH</div>
                                            <button onClick={() => socket.emit('get_knowledge_graph')} className="text-[9px] text-cyan-400 hover:text-cyan-200 tracking-wider">REFRESH</button>
                                        </div>
                                        <div className="flex gap-2 mb-3">
                                            <div className="flex-1 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 text-center">
                                                <div className="text-lg text-fuchsia-400 font-mono">{kgData.stats?.node_count || 0}</div>
                                                <div className="text-[9px] text-slate-500">ENTITIES</div>
                                            </div>
                                            <div className="flex-1 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 text-center">
                                                <div className="text-lg text-cyan-400 font-mono">{kgData.stats?.edge_count || 0}</div>
                                                <div className="text-[9px] text-slate-500">RELATIONS</div>
                                            </div>
                                        </div>
                                        <div className="flex gap-2 mb-2">
                                            <input
                                                value={kgSearch}
                                                onChange={e => setKgSearch(e.target.value)}
                                                onKeyDown={e => { if (e.key === 'Enter' && kgSearch.trim()) socket.emit('search_kg', { query: kgSearch }); }}
                                                placeholder="Search graph…" className="holo-input flex-1 text-xs" />
                                            <button onClick={() => kgSearch.trim() && socket.emit('search_kg', { query: kgSearch })} className="px-2 rounded-lg border border-cyan-300/30 text-cyan-200 hover:bg-cyan-400/10 text-xs">GO</button>
                                        </div>
                                        {kgSearchResults.length > 0 && (
                                            <div className="space-y-1 mb-2">
                                                {kgSearchResults.map((e, i) => (
                                                    <div key={i} className="text-[10px] px-2 py-1 rounded border border-fuchsia-300/10 bg-fuchsia-400/5">
                                                        <span className="text-fuchsia-300">{e.name}</span>
                                                        <span className="text-slate-500 ml-1">· {e.type}</span>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                        {(kgData.entities || []).length > 0 && kgSearchResults.length === 0 && (
                                            <div className="space-y-1">
                                                {(kgData.entities || []).slice(0, 8).map((e, i) => (
                                                    <div key={i} className="flex items-center justify-between text-[10px] px-2 py-1 rounded border border-white/5 bg-white/[0.02]">
                                                        <div>
                                                            <span className="text-slate-200">{e.name}</span>
                                                            <span className="text-slate-500 ml-1">· {e.type}</span>
                                                        </div>
                                                        <div className="flex items-center gap-2">
                                                            <span className="text-slate-600">{e.degree} links</span>
                                                            <button onClick={() => socket.emit('delete_kg_entity', { node_id: e.id })} className="text-rose-400/60 hover:text-rose-300"><X size={9} /></button>
                                                        </div>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                </div>
                            ) : rightTab === 'train' ? (
                                <div className="space-y-4">
                                    {/* Dataset Upload */}
                                    <div>
                                        <div className="text-[10px] tracking-[0.25em] text-slate-500 mb-2">DATASET UPLOAD</div>
                                        <label className="flex items-center justify-center gap-2 w-full py-5 rounded-xl border-2 border-dashed border-white/10 hover:border-fuchsia-300/30 cursor-pointer transition-colors text-slate-400 hover:text-fuchsia-300 text-xs">
                                            <Database size={14} />
                                            <span>Upload JSON / JSONL / CSV / TXT</span>
                                            <input type="file" accept=".json,.jsonl,.csv,.txt" className="hidden" onChange={(e) => {
                                                const file = e.target.files?.[0];
                                                if (!file) return;
                                                const reader = new FileReader();
                                                reader.onload = (ev) => {
                                                    const b64 = btoa(String.fromCharCode(...new Uint8Array(ev.target.result)));
                                                    socket.emit('upload_dataset', { name: file.name, format: file.name.split('.').pop()?.toLowerCase() || 'txt', content: b64 });
                                                };
                                                reader.readAsArrayBuffer(file);
                                                e.target.value = '';
                                            }} />
                                        </label>
                                        {datasets.length > 0 && (
                                            <div className="mt-2 space-y-1">
                                                {datasets.map((ds, i) => (
                                                    <div key={i} className="flex items-center justify-between px-3 py-1.5 rounded-lg border border-white/5 bg-white/[0.02]">
                                                        <div>
                                                            <div className="text-[11px] text-slate-200 truncate max-w-[180px]">{ds.name}</div>
                                                            <div className="text-[9px] text-slate-500">{ds.record_count} records · {ds.format?.toUpperCase()}</div>
                                                        </div>
                                                        <span className="text-[9px] text-emerald-400">indexed</span>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                    {/* Feedback */}
                                    <div>
                                        <div className="text-[10px] tracking-[0.25em] text-slate-500 mb-2">AI RESPONSE FEEDBACK</div>
                                        <div className="flex gap-2 mb-2">
                                            <div className="flex-1 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 text-center">
                                                <div className="text-lg text-emerald-400 font-mono">{feedbackStats.positive}</div>
                                                <div className="text-[9px] text-slate-500">POSITIVE</div>
                                            </div>
                                            <div className="flex-1 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 text-center">
                                                <div className="text-lg text-rose-400 font-mono">{feedbackStats.negative}</div>
                                                <div className="text-[9px] text-slate-500">NEGATIVE</div>
                                            </div>
                                            <div className="flex-1 rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2 text-center">
                                                <div className="text-lg text-cyan-400 font-mono">{feedbackStats.total}</div>
                                                <div className="text-[9px] text-slate-500">TOTAL</div>
                                            </div>
                                        </div>
                                        {/* Rate last response */}
                                        {messages.filter(m => m.sender === 'ADA').length > 0 && (() => {
                                            const lastAI = [...messages].reverse().find(m => m.sender === 'ADA');
                                            const lastUser = [...messages].reverse().find(m => m.sender === 'User');
                                            return (
                                                <div className="rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2">
                                                    <div className="text-[9px] text-slate-500 mb-1">LAST RESPONSE</div>
                                                    <div className="text-[11px] text-slate-300 line-clamp-2 mb-2">{lastAI?.text?.slice(0, 120)}</div>
                                                    <div className="flex gap-2">
                                                        <button
                                                            onClick={() => { socket.emit('submit_feedback', { user_message: lastUser?.text || '', ai_response: lastAI?.text || '', rating: 1 }); }}
                                                            className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-emerald-300/20 text-emerald-300 hover:bg-emerald-400/10 text-xs transition-colors"
                                                        ><ThumbsUp size={11} /> Good</button>
                                                        <button
                                                            onClick={() => { socket.emit('submit_feedback', { user_message: lastUser?.text || '', ai_response: lastAI?.text || '', rating: -1 }); }}
                                                            className="flex items-center gap-1 px-3 py-1.5 rounded-lg border border-rose-300/20 text-rose-300 hover:bg-rose-400/10 text-xs transition-colors"
                                                        ><ThumbsDown size={11} /> Bad</button>
                                                    </div>
                                                </div>
                                            );
                                        })()}
                                    </div>
                                    {/* Knowledge Base Status */}
                                    <div>
                                        <div className="text-[10px] tracking-[0.25em] text-slate-500 mb-2">KNOWLEDGE BASE STATUS</div>
                                        <div className="rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2">
                                            <div className="text-xs text-slate-300">{documents.length} documents indexed</div>
                                            <div className="text-[9px] text-slate-500 mt-0.5">
                                                {documents.reduce((acc, d) => acc + (d.chunk_count || 0), 0)} total chunks
                                            </div>
                                            <button
                                                onClick={() => { setShowKnowledge(true); socket.emit('list_documents'); }}
                                                className="mt-2 text-[10px] text-cyan-400 hover:text-cyan-200 tracking-wider"
                                            >OPEN KNOWLEDGE BASE →</button>
                                        </div>
                                    </div>
                                </div>
                            ) : rightTab === 'missions' ? (
                                <div className="space-y-3">
                                    <div className="flex items-center justify-between">
                                        <div className="text-[10px] tracking-[0.25em] text-slate-500">MISSION CONTROL</div>
                                        <button onClick={() => socket.emit('get_missions')} className="text-[9px] text-cyan-400 hover:text-cyan-200 tracking-wider">REFRESH</button>
                                    </div>
                                    {missions.length === 0 ? (
                                        <div className="text-xs text-slate-500">No missions yet. Ask Nexora to run a multi-step task.</div>
                                    ) : (
                                        missions.map(m => (
                                            <div key={m.id} className="rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2">
                                                <div className="flex items-center justify-between gap-2 mb-1">
                                                    <div className="text-xs text-slate-200 truncate flex-1">{m.title}</div>
                                                    <span className={`text-[9px] px-1.5 py-0.5 rounded-full border ${
                                                        m.status === 'completed' ? 'border-emerald-400/30 text-emerald-300 bg-emerald-400/5' :
                                                        m.status === 'running' ? 'border-amber-400/30 text-amber-300 bg-amber-400/5' :
                                                        m.status === 'failed' ? 'border-rose-400/30 text-rose-300 bg-rose-400/5' :
                                                        m.status === 'cancelled' ? 'border-slate-400/30 text-slate-400 bg-slate-400/5' :
                                                        'border-cyan-400/30 text-cyan-300 bg-cyan-400/5'
                                                    }`}>{m.status?.toUpperCase()}</span>
                                                </div>
                                                <div className="text-[10px] text-slate-500 truncate mb-2">{m.description}</div>
                                                {(m.subtasks || []).map((st, i) => (
                                                    <div key={i} className="flex items-center gap-2 text-[10px] py-0.5">
                                                        <span className={`w-1.5 h-1.5 rounded-full ${
                                                            st.status === 'done' ? 'bg-emerald-400' :
                                                            st.status === 'running' ? 'bg-amber-400 animate-pulse' :
                                                            st.status === 'failed' ? 'bg-rose-400' :
                                                            st.status === 'skipped' ? 'bg-slate-700' : 'bg-slate-500'
                                                        }`} />
                                                        <span className="text-slate-400 flex-1 truncate" title={st.error || ''}>{st.title}{st.retries ? ` (retried ${st.retries})` : ''}</span>
                                                        <span className="text-slate-600">{st.tool_name}</span>
                                                    </div>
                                                ))}
                                                {m.result && (
                                                    <div className="text-[9px] text-slate-600 mt-1">
                                                        {m.result.done}/{m.result.total} done · {m.result.duration_s}s{m.result.verified ? ' · verified' : ''}
                                                        {(m.result.failures || []).slice(0, 2).map((f, i) => <div key={i} className="text-rose-300/70 truncate">{f.title}: {f.error}</div>)}
                                                    </div>
                                                )}
                                                <div className="flex gap-3">
                                                    {m.status === 'running' && (
                                                        <button onClick={() => socket.emit('pause_mission', { mission_id: m.id })} className="mt-2 text-[9px] text-amber-300 hover:text-amber-100 tracking-wider">PAUSE</button>
                                                    )}
                                                    {m.status === 'paused' && (
                                                        <button onClick={() => socket.emit('resume_mission', { mission_id: m.id })} className="mt-2 text-[9px] text-emerald-300 hover:text-emerald-100 tracking-wider">RESUME</button>
                                                    )}
                                                    {['pending', 'running', 'paused'].includes(m.status) && (
                                                        <button onClick={() => socket.emit('cancel_mission', { mission_id: m.id })} className="mt-2 text-[9px] text-rose-400 hover:text-rose-200 tracking-wider">CANCEL</button>
                                                    )}
                                                </div>
                                            </div>
                                        ))
                                    )}
                                </div>
                            ) : rightTab === 'events' ? (
                                <div className="space-y-3">
                                    <div className="flex items-center justify-between">
                                        <div className="text-[10px] tracking-[0.25em] text-slate-500">EVENT RULES</div>
                                        <button onClick={() => socket.emit('list_event_rules')} className="text-[9px] text-cyan-400 hover:text-cyan-200 tracking-wider">REFRESH</button>
                                    </div>
                                    {eventRules.length === 0 ? (
                                        <div className="text-xs text-slate-500">No rules yet. Ask Nexora to watch a folder or schedule a task.</div>
                                    ) : (
                                        eventRules.map(r => (
                                            <div key={r.id} className="rounded-lg border border-white/5 bg-white/[0.02] px-3 py-2">
                                                <div className="flex items-center justify-between gap-2">
                                                    <div className="text-xs text-slate-200 truncate flex-1">{r.name}</div>
                                                    <div className="flex items-center gap-2">
                                                        <button
                                                            onClick={() => socket.emit('toggle_event_rule', { rule_id: r.id, enabled: !r.enabled })}
                                                            className={`text-[9px] px-1.5 py-0.5 rounded-full border ${
                                                                r.enabled ? 'border-emerald-400/30 text-emerald-300' : 'border-slate-400/30 text-slate-500'
                                                            }`}>{r.enabled ? 'ON' : 'OFF'}</button>
                                                        {r.needs_approval && (
                                                            <button
                                                                onClick={() => socket.emit('approve_event_rule', { rule_id: r.id })}
                                                                className="text-[9px] px-1.5 py-0.5 rounded-full border border-amber-400/40 text-amber-300 hover:text-amber-100">APPROVE</button>
                                                        )}
                                                        <button
                                                            onClick={() => socket.emit('dry_run_event_rule', { rule_id: r.id })}
                                                            className="text-[9px] text-slate-400 hover:text-slate-200">DRY RUN</button>
                                                        <button
                                                            onClick={() => socket.emit('fire_event_rule', { rule_id: r.id })}
                                                            className="text-[9px] text-cyan-400 hover:text-cyan-200">FIRE</button>
                                                    </div>
                                                </div>
                                                <div className="text-[10px] text-slate-500 mt-0.5">{r.trigger_type} → {r.action_type}{r.needs_approval ? ' · waiting for your approval' : ''}</div>
                                                {r.last_fired && <div className="text-[9px] text-slate-600 mt-0.5">Last: {new Date(r.last_fired).toLocaleString()}</div>}
                                            </div>
                                        ))
                                    )}
                                    {eventHistory.length > 0 && (
                                        <div>
                                            <div className="text-[10px] tracking-[0.25em] text-slate-500 mb-2 mt-4">RECENT FIRES</div>
                                            {eventHistory.slice(0, 5).map((h, i) => (
                                                <div key={i} className="text-[10px] text-slate-500 py-0.5 truncate">
                                                    <span className="text-cyan-400/70">{h.rule_name}</span> · {new Date(h.fired_at).toLocaleTimeString()} · {h.action_type}
                                                    {h.status && <span className={h.status === 'ok' ? 'text-emerald-400/70' : h.status === 'blocked' ? 'text-amber-300/70' : 'text-rose-400/70'}> · {h.status}</span>}
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                    {simResult && (
                                        <div className="rounded-lg border border-amber-400/20 bg-amber-400/[0.03] px-3 py-2">
                                            <div className="flex justify-between text-[9px] tracking-wider text-amber-300 mb-1">
                                                <span>SIMULATION · NOTHING WAS EXECUTED</span>
                                                <button onClick={() => setSimResult(null)} className="text-slate-500 hover:text-slate-300">CLOSE</button>
                                            </div>
                                            <pre className="text-[9px] text-slate-400 whitespace-pre-wrap max-h-48 overflow-auto">{JSON.stringify(simResult, null, 1)}</pre>
                                        </div>
                                    )}
                                </div>
                            ) : rightTab === 'diag' ? (
                                <div className="space-y-3">
                                    <div className="flex items-center justify-between">
                                        <div className="text-[10px] tracking-[0.25em] text-slate-500">SYSTEM DIAGNOSTICS</div>
                                        <button
                                            onClick={() => { setDiagRunning(true); socket.emit('run_diagnostics'); }}
                                            disabled={diagRunning}
                                            className="text-[9px] text-cyan-400 hover:text-cyan-200 tracking-wider disabled:opacity-50">
                                            {diagRunning ? 'RUNNING…' : 'RUN'}
                                        </button>
                                    </div>
                                    {diagReport && (
                                        <div className="space-y-3">
                                            {/* API Health */}
                                            <div>
                                                <div className="text-[9px] text-slate-600 mb-1">API HEALTH</div>
                                                {Object.entries(diagReport.api_health || {}).filter(([, v]) => typeof v === 'boolean').map(([k, v]) => (
                                                    <div key={k} className="flex items-center justify-between text-[10px] py-0.5">
                                                        <span className="text-slate-400">{k.replace(/_/g,' ')}</span>
                                                        <span className={v ? 'text-emerald-400' : 'text-rose-400'}>{v ? 'OK' : 'FAIL'}</span>
                                                    </div>
                                                ))}
                                            </div>
                                            {diagReport.model_connectivity && (
                                                <div className="flex items-center justify-between text-[10px] py-0.5">
                                                    <span className="text-slate-400">gemini model</span>
                                                    <span className={diagReport.model_connectivity.status === 'ok' ? 'text-emerald-400' : 'text-rose-400'}>
                                                        {diagReport.model_connectivity.status?.toUpperCase()}{diagReport.model_connectivity.latency_ms ? ` · ${Math.round(diagReport.model_connectivity.latency_ms)} ms` : ''}
                                                    </span>
                                                </div>
                                            )}
                                            {(diagReport.bottlenecks?.findings || []).length > 0 && (
                                                <div>
                                                    <div className="text-[9px] text-slate-600 mb-1">BOTTLENECKS</div>
                                                    {diagReport.bottlenecks.findings.map((f, i) => (
                                                        <div key={i} className={`text-[10px] py-0.5 ${f.severity === 'high' ? 'text-rose-300' : 'text-amber-300/80'}`}>· {f.finding}</div>
                                                    ))}
                                                </div>
                                            )}
                                            {/* Backend Health */}
                                            <div>
                                                <div className="text-[9px] text-slate-600 mb-1">MODULES</div>
                                                {Object.entries(diagReport.backend_health || {}).map(([k, v]) => (
                                                    <div key={k} className="flex items-center justify-between text-[10px] py-0.5">
                                                        <span className="text-slate-400 truncate flex-1">{k}</span>
                                                        <span className={v?.ok ? 'text-emerald-400' : 'text-rose-400'}>{v?.ok ? 'OK' : 'ERR'}</span>
                                                    </div>
                                                ))}
                                            </div>
                                            {/* Recommendations */}
                                            {(diagReport.recommendations || []).length > 0 && (
                                                <div>
                                                    <div className="text-[9px] text-slate-600 mb-1">RECOMMENDATIONS</div>
                                                    {diagReport.recommendations.map((r, i) => (
                                                        <div key={i} className="text-[10px] text-amber-300/80 py-0.5">· {r}</div>
                                                    ))}
                                                </div>
                                            )}
                                        </div>
                                    )}
                                    {!diagReport && !diagRunning && (
                                        <div className="text-xs text-slate-500">Click RUN to check all systems.</div>
                                    )}
                                    <div className="mt-3 pt-3 border-t border-white/5">
                                        <div className="flex items-center justify-between">
                                            <div className="text-[10px] tracking-[0.25em] text-slate-500">AGENT EVALUATION</div>
                                            <button onClick={() => socket.emit('run_evaluation')} disabled={evalState.running}
                                                className="text-[9px] text-cyan-400 hover:text-cyan-200 tracking-wider disabled:opacity-50">
                                                {evalState.running ? 'RUNNING…' : 'RUN'}</button>
                                        </div>
                                        <pre className="text-[9px] text-slate-400 whitespace-pre-wrap mt-1">{evalState.summary || 'No results yet. RUN runs the offline benchmark suites on temporary data.'}</pre>
                                    </div>
                                    <div className="mt-3 pt-3 border-t border-white/5">
                                        <div className="flex items-center justify-between">
                                            <div className="text-[10px] tracking-[0.25em] text-slate-500">SUGGESTIONS</div>
                                            <button onClick={() => socket.emit('get_recommendations')} className="text-[9px] text-cyan-400 hover:text-cyan-200 tracking-wider">CHECK</button>
                                        </div>
                                        <label className="flex items-center gap-2 text-[10px] text-slate-400 mt-1">
                                            <input type="checkbox" checked={usageCollection}
                                                onChange={(e) => socket.emit('update_settings', { usage_data_collection: e.target.checked })} />
                                            Learn from my usage (tool names + times, stored only on this PC)
                                        </label>
                                        {recs && (recs.status !== 'ok' ? (
                                            <div className="text-[10px] text-slate-500 mt-1">{recs.message}</div>
                                        ) : recs.recommendations.length === 0 ? (
                                            <div className="text-[10px] text-slate-500 mt-1">No clear pattern yet.</div>
                                        ) : recs.recommendations.slice(0, 5).map((r, i) => (
                                            <div key={i} className="text-[10px] text-emerald-300/80 py-0.5">· {r.type === 'routine' ? `${r.tool} around ${r.window}` : r.tools.join(' → ')} <span className="text-slate-500">({r.evidence})</span></div>
                                        )))}
                                    </div>
                                    {/* Performance sparkline (numbers) */}
                                    {perfHistory.length > 0 && (
                                        <div className="mt-3">
                                            <div className="text-[9px] text-slate-600 mb-1">PERFORMANCE (LAST {perfHistory.length} MIN)</div>
                                            <div className="flex gap-2 text-[10px]">
                                                <span className="text-slate-500">CPU:</span>
                                                <span className="text-cyan-300">{Math.round(perfHistory[perfHistory.length-1]?.cpu || 0)}%</span>
                                                <span className="text-slate-500">RAM:</span>
                                                <span className="text-fuchsia-300">{Math.round(perfHistory[perfHistory.length-1]?.ram || 0)}%</span>
                                                <span className="text-slate-500">avg CPU:</span>
                                                <span className="text-cyan-300">{Math.round(perfHistory.reduce((a,b) => a + (b.cpu||0), 0) / perfHistory.length)}%</span>
                                            </div>
                                        </div>
                                    )}
                                </div>
                            ) : (
                                <div className="space-y-5">
                                    {focus.active ? (                                        <div>
                                            <div className="text-[10px] tracking-[0.25em] text-emerald-300 mb-2">CURRENT · {focus.task}</div>
                                            <FocusTimeline checks={focus.checks} total={focus.planned_minutes * 60} elapsed={focus.elapsed} />
                                            <div className="flex gap-3 mt-2 text-[10px] text-slate-500">
                                                <span><span className="text-emerald-400">■</span> on</span><span><span className="text-rose-400">■</span> off</span><span><span className="text-slate-500">■</span> unsure</span>
                                            </div>
                                        </div>
                                    ) : (
                                        <button onClick={() => setShowFocus(true)} disabled={!socketConnected} className="holo-btn w-full justify-center disabled:opacity-40"><Target size={14} /> Start a focus session</button>
                                    )}
                                    <div>
                                        <div className="text-[10px] tracking-[0.25em] text-slate-500 mb-2">HISTORY</div>
                                        <FocusHistory sessions={focusHistory} />
                                    </div>
                                </div>
                            )}
                        </div>
                    </div></motion.aside>

                    {/* ---------- cinematic stage ---------- */}
                    <Stage widgets={widgets} computer={computer} onDismiss={dismissWidget}
                        onOpenInbox={openInbox} onAsk={sendText} onStop={() => socket.emit('emergency_stop')} onResume={() => socket.emit('computer_resume')} />
                </div>

                <div className="fixed inset-0 pointer-events-none z-[150] fx-scanlines" />
                {booting && <Boot coreOnline={socketConnected} onDone={() => setBooting(false)} />}
                <AnimatePresence>{confirmQueue[0] && <ConfirmCard request={confirmQueue[0]} onResolve={resolveConfirm} />}</AnimatePresence>
                <AnimatePresence>
                    {showFocus && <FocusLauncher open focus={focus} onClose={() => setShowFocus(false)}
                        onDevMode={(on) => socket.emit('focus_dev_mode', { on })}
                        onTestNudge={() => socket.emit('focus_test_nudge')}
                        onStart={(task, minutes) => { socket.emit('focus_start', { task, minutes }); setShowFocus(false); setRightTab('focus'); }} />}
                </AnimatePresence>

                {showSettings && (
                    <SettingsWindow socket={socket} micDevices={micDevices} speakerDevices={speakerDevices}
                        selectedMicId={selectedMicId} setSelectedMicId={setSelectedMicId}
                        selectedSpeakerId={selectedSpeakerId} setSelectedSpeakerId={setSelectedSpeakerId}
                        handleFileUpload={() => {}} onClose={() => setShowSettings(false)} />
                )}
                {showInbox && (
                    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60" onClick={() => setShowInbox(false)}>
                        <div onClick={(e) => e.stopPropagation()} className="w-[620px] h-[520px] relative">
                            <EmailWindow socket={socket} onClose={() => setShowInbox(false)} onCompose={() => setShowCompose(true)} />
                        </div>
                    </div>
                )}
                {showCompose && (
                    <div className="fixed inset-0 z-[95] flex items-center justify-center bg-black/60">
                        <ComposeWindow socket={socket} onClose={() => setShowCompose(false)} />
                    </div>
                )}
                {showKnowledge && (
                    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/60" onClick={() => setShowKnowledge(false)}>
                        <div onClick={(e) => e.stopPropagation()} className="w-[560px] max-h-[520px] flex flex-col rounded-2xl border border-white/10 bg-gradient-to-b from-slate-900/90 to-black/90 backdrop-blur-2xl shadow-2xl overflow-hidden">
                            <div className="flex items-center justify-between px-6 py-4 border-b border-white/10">
                                <span className="text-sm font-bold tracking-widest text-cyan-300">KNOWLEDGE BASE</span>
                                <button onClick={() => setShowKnowledge(false)} className="p-1.5 hover:bg-white/10 rounded-lg text-slate-300"><X size={18} /></button>
                            </div>
                            <div className="flex-1 overflow-y-auto custom-scrollbar p-6 space-y-4">
                                {ragError && (
                                    <div className="px-3 py-2 rounded-lg bg-rose-400/10 border border-rose-400/20 text-rose-300 text-xs">{ragError}</div>
                                )}
                                <div>
                                    <label className="block text-[10px] tracking-[0.25em] text-slate-500 mb-2">UPLOAD DOCUMENT</label>
                                    <label className="flex items-center justify-center gap-2 w-full py-8 rounded-xl border-2 border-dashed border-white/10 hover:border-cyan-300/30 cursor-pointer transition-colors text-slate-400 hover:text-cyan-300 text-xs">
                                        <BookOpen size={16} />
                                        <span>Click to upload PDF, DOCX, TXT, or MD</span>
                                        <input type="file" accept=".pdf,.docx,.txt,.md" className="hidden" onChange={(e) => {
                                            const file = e.target.files?.[0];
                                            if (!file) return;
                                            setRagError(null);
                                            const reader = new FileReader();
                                            reader.onload = (ev) => {
                                                const b64 = btoa(String.fromCharCode(...new Uint8Array(ev.target.result)));
                                                socket.emit('upload_document', { filename: file.name, content: b64 });
                                            };
                                            reader.readAsArrayBuffer(file);
                                            e.target.value = '';
                                        }} />
                                    </label>
                                </div>
                                <div>
                                    <div className="text-[10px] tracking-[0.25em] text-slate-500 mb-2">INDEXED DOCUMENTS ({documents.length})</div>
                                    {documents.length === 0 ? (
                                        <div className="text-xs text-slate-500">No documents indexed yet. Upload a file above.</div>
                                    ) : (
                                        <div className="space-y-2">
                                            {documents.map((doc, i) => (
                                                <div key={i} className="flex items-center justify-between px-3 py-2 rounded-lg border border-white/5 bg-white/[0.02]">
                                                    <div>
                                                        <div className="text-xs text-slate-200 truncate max-w-[300px]">{doc.filename}</div>
                                                        <div className="text-[9px] text-slate-500 mt-0.5">{doc.chunk_count} chunks · {doc.file_type?.toUpperCase()}</div>
                                                    </div>
                                                    <button
                                                        onClick={() => socket.emit('delete_document', { filename: doc.filename })}
                                                        className="p-1 rounded hover:bg-rose-400/20 text-rose-400/60 hover:text-rose-300 transition-colors"
                                                        title="Remove document"
                                                    ><X size={12} /></button>
                                                </div>
                                            ))}
                                        </div>
                                    )}
                                </div>
                            </div>
                        </div>
                    </div>
                )}
            </div>
        </ErrorBoundary>
    );
}

export default App;
