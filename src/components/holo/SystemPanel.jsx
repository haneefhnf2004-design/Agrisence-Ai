import React, { useRef } from 'react';
import { Cpu, MemoryStick, HardDrive, BatteryCharging, Battery, Gauge, ArrowDown, ArrowUp } from 'lucide-react';

const tone = (p) => (p >= 90 ? 'bg-rose-400' : p >= 70 ? 'bg-amber-300' : 'bg-violet-300');
const HISTORY = 60; // one minute of 1 s samples, like Task Manager's graphs

/** Tiny line graph of the last minute. `max` fixes the scale (100 for %), else it auto-scales. */
function Spark({ data, max, color = 'rgb(196 181 253)' }) {
    if (!data || data.length < 2) return <div className="h-4 mt-1" />;
    const top = max ?? Math.max(1, ...data);
    const pts = data.map((v, i) => `${(i / (HISTORY - 1)) * 100},${18 - (Math.min(v, top) / top) * 17}`).join(' ');
    return (
        <svg viewBox="0 0 100 18" preserveAspectRatio="none" className="w-full h-4 mt-1">
            <polyline points={pts} fill="none" stroke={color} strokeWidth="1" vectorEffect="non-scaling-stroke" opacity="0.8" />
        </svg>
    );
}

function Bar({ icon: Icon, label, percent, detail, history, max = 100, color }) {
    const p = Math.max(0, Math.min(100, percent ?? 0));
    return (
        <div>
            <div className="flex items-center gap-1.5 text-[10px] tracking-[0.15em] text-slate-400">
                <Icon size={11} className="text-violet-300/80" />
                <span className="flex-1">{label}</span>
                <span className="text-slate-200 tabular-nums">{percent == null ? '--' : `${Math.round(p)}%`}</span>
            </div>
            <div className="h-1 mt-1 rounded bg-white/10 overflow-hidden">
                <div className={`h-full rounded transition-all duration-700 ${tone(p)}`} style={{ width: `${p}%` }} />
            </div>
            {history && <Spark data={history} max={max} color={color} />}
            {detail && <div className="text-[9px] text-slate-500 mt-0.5 truncate">{detail}</div>}
        </div>
    );
}

/** Keeps the last minute of each reading so the strip can draw Task Manager style graphs. */
function useHistory(stats) {
    const hist = useRef({ t: 0, cpu: [], ram: [], gpu: [], disk: [], down: [] });
    const h = hist.current;
    if (stats && stats.time !== h.t) {
        h.t = stats.time;
        const push = (k, v) => { h[k] = [...h[k], v ?? 0].slice(-HISTORY); };
        push('cpu', stats.cpu.percent);
        push('ram', stats.ram.percent);
        push('gpu', stats.gpu?.percent);
        push('disk', stats.disk.active);
        push('down', (stats.net.down_kbs || 0) + (stats.net.up_kbs || 0));
    }
    return h;
}

/** Always-visible hardware strip: CPU (with per-core), RAM, GPU, disk, battery, network — live every second. */
export function HardwareStrip({ stats }) {
    const h = useHistory(stats);
    if (!stats) return <div className="px-4 py-3 text-[10px] text-slate-500 border-b border-white/5">Reading hardware…</div>;
    const { cpu, ram, gpu, disk, battery, net } = stats;
    return (
        <div className="px-4 py-3 grid grid-cols-2 gap-x-4 gap-y-2.5 border-b border-white/5">
            <Bar icon={Cpu} label="CPU" percent={cpu.percent} history={h.cpu} detail={`${cpu.cores} cores${cpu.ghz ? ` · ${cpu.ghz} GHz` : ''}`} />
            <Bar icon={MemoryStick} label="RAM" percent={ram.percent} history={h.ram} color="rgb(240 171 252)" detail={`${ram.used_gb} / ${ram.total_gb} GB`} />
            <Bar icon={Gauge} label="GPU" percent={gpu?.percent} history={gpu ? h.gpu : null} color="rgb(110 231 183)"
                detail={gpu ? `${gpu.temp_c}°C · ${Math.round(gpu.mem_used_mb / 1024 * 10) / 10}/${Math.round(gpu.mem_total_mb / 1024)} GB` : 'no NVIDIA data'} />
            <Bar icon={HardDrive} label="DISK C" percent={disk.active} history={h.disk} color="rgb(252 211 77)"
                detail={`R ${disk.read_mbs ?? 0} · W ${disk.write_mbs ?? 0} MB/s · ${disk.free_gb} GB free`} />
            <Bar icon={battery?.plugged ? BatteryCharging : Battery} label="BATTERY" percent={battery?.percent}
                detail={!battery ? 'desktop power' : battery.plugged ? 'charging' : battery.mins_left ? `${battery.mins_left} min left` : 'on battery'} />
            <div className="text-[10px] tracking-[0.15em] text-slate-400">
                NETWORK
                <div className="text-[10px] text-slate-200 tabular-nums mt-1 flex gap-2">
                    <span className="flex items-center gap-0.5"><ArrowDown size={10} className="text-emerald-300" />{net.down_kbs} KB/s</span>
                    <span className="flex items-center gap-0.5"><ArrowUp size={10} className="text-sky-300" />{net.up_kbs} KB/s</span>
                </div>
                <Spark data={h.down} color="rgb(125 211 252)" />
            </div>
            {cpu.per_core?.length > 0 && (
                <div className="col-span-2">
                    <div className="text-[9px] tracking-[0.2em] text-slate-500 mb-1">CPU CORES</div>
                    <div className="flex items-end gap-[2px] h-5">
                        {cpu.per_core.map((c, i) => (
                            <div key={i} className="flex-1 bg-white/10 rounded-sm h-full flex items-end" title={`Core ${i}: ${Math.round(c)}%`}>
                                <div className={`w-full rounded-sm transition-all duration-700 ${tone(c)}`} style={{ height: `${Math.max(4, c)}%` }} />
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
}

/** Task Manager style live process list. */
export function ProcessList({ stats }) {
    if (!stats) return <div className="text-xs text-slate-500">Waiting for the backend…</div>;
    return (
        <div>
            <div className="flex text-[9px] tracking-[0.2em] text-slate-500 mb-1.5">
                <span className="flex-1">{stats.process_count} PROCESSES · UP {stats.uptime_h} H</span>
            </div>
            <div className="flex text-[9px] tracking-[0.2em] text-slate-500 border-b border-white/5 pb-1 mb-1">
                <span className="flex-1">NAME</span><span className="w-12 text-right">CPU</span><span className="w-16 text-right">MEMORY</span>
            </div>
            {stats.processes.map((p) => (
                <div key={p.pid} className="flex items-center text-[11px] py-1 border-b border-white/[0.03]">
                    <span className="flex-1 truncate text-slate-300" title={`PID ${p.pid}`}>{p.name}</span>
                    <span className={`w-12 text-right tabular-nums ${p.cpu >= 20 ? 'text-amber-300' : 'text-slate-400'}`}>{p.cpu}%</span>
                    <span className="w-16 text-right tabular-nums text-slate-400">{p.mem_mb >= 1024 ? `${(p.mem_mb / 1024).toFixed(1)} GB` : `${p.mem_mb} MB`}</span>
                </div>
            ))}
            <div className="mt-2 text-[9px] text-slate-600">Live: hardware every 1 s, apps every few seconds</div>
        </div>
    );
}
