# Nexora V2 — Advanced Features: Architecture, Verification and Limitations

Last updated: 2026-11-12. Every number in this document was measured by running the code
(`pytest`, `python -m evaluation.run`) on the development laptop. Nothing is estimated.

---

## 1. Audit summary (before this work)

| Area | What existed | What was wrong / missing |
|---|---|---|
| Mission Control (`mission_control.py`) | Task queue, dependency waves, retries, SQLite persistence | **Executor was a stub** — every step returned `{'stub': True}`; nothing ran. Two separate engine instances (voice vs. server), so the UI never saw voice missions. Dependents of failed steps stayed `pending` forever. No cycle check, no verification, no shared context, no parallelism, pause had no resume. |
| Event automation (`event_engine.py`) | Rules table, scheduler thread, watchdog watcher, history | `run_tool` / `run_mission` actions **never executed** (history said "fired"). No approval step. File watcher only read rules at start-up. `daily_at` compared UTC vs local. `on_event` used `asyncio.create_task` from a worker thread, so notifications never reached the UI. |
| Knowledge graph (`knowledge_graph.py`) | NetworkX graph, regex entity extraction | **Two instances** (memory.py + server.py) overwrote each other's JSON. Not connected to RAG. No source references (only the word "memory"). Relation patterns required both sides capitalised, so they almost never matched. No path/relationship search. |
| Diagnostics (`diagnostics.py`) | Internet / key / OmniRoute / DB / module / log checks | Checked two modules that did not exist (`predictor`, `digital_twin`) so it always reported failures. "Gemini configured" only checked that the env var existed. No bottleneck detection. Looked for logs in the wrong folder. |
| Voice tools | `manage_events`, `knowledge_graph`, `run_diagnostics` declared to Gemini in `tools.py` | **Never dispatched** in `ada.py` — calling them produced no response. |
| Cross-app, evaluation, predictive, digital twin | — | Not implemented. |
| Already fine, reused unchanged | Long-term memory (SQLite + embeddings), wake word, RAG, Training Centre, `computer_bridge`, `system_monitor` | Two small hooks were added to memory/RAG (graph source references). |

## 2. Architecture

```
                         Gemini Live (voice)                 Electron UI (App.jsx)
                                │ tool calls                         │ socket.io
                                ▼                                    ▼
   ada.py ──► assistant_tools.py  (create_mission, manage_missions, manage_events,
                │                  knowledge_graph, run_diagnostics, simulate,
                │                  get_recommendations, get_app_context)     server.py socket handlers
                ▼                                                             │
   ┌──────────── mission_control.engine (ONE shared MissionEngine) ◄──────────┤
   │                     │ executes each step through                         │
   │                     ▼                                                    │
   │              tool_runner.run_tool(name, args, source)  ◄── event_engine (scheduler + watcher threads)
   │                │      │                                        │  actions: notify / run_tool / run_mission
   │                │      └─► telemetry.record (opt-in)            │  approval gate for run_tool / run_mission
   │                ▼                                               │
   │   real tools: system_monitor, lk_weather, lk_news, memory, rag_engine,
   │   knowledge_graph, diagnostics, email_agent.read_emails, computer_bridge,
   │   app_context, file_list/copy/move (sandboxed to the user's home)
   │
   │   knowledge_graph.get_graph() (ONE instance) ◄── memory.save/delete (memory:<id>)
   │                                              ◄── rag_engine.ingest/delete (doc:<file>#<chunk>)
   │
   ├── digital_twin   (previews; runs a sandbox MissionEngine with a fake executor)
   ├── predictor      (reads telemetry; rules + backtest)
   ├── diagnostics    (system_monitor + perf_history.db + telemetry + all *.db + logs)
   └── evaluation/run (benchmarks on temp DBs / temp folders)
```

### Dependency map (module → what it uses)

| Module | Depends on |
|---|---|
| `tool_runner` | `telemetry`, `system_monitor`, `lk_weather`, `lk_news`, `memory`, `rag_engine`, `knowledge_graph`, `diagnostics`, `computer_bridge`, `app_context` (all imported lazily) |
| `mission_control` | `tool_runner` (default executor) |
| `event_engine` | `tool_runner`, `mission_control.engine`, `system_monitor`, `psutil`, `watchdog` |
| `knowledge_graph` | `networkx` |
| `memory`, `rag_engine` | `knowledge_graph.get_graph()` (source references only) |
| `diagnostics` | `system_monitor`, `telemetry`, `requests` |
| `predictor` | `telemetry` |
| `digital_twin` | `tool_runner`, `mission_control`, `telemetry`, `event_engine` (passed in) |
| `app_context` | `computer_bridge._windows`, Win32 via `ctypes`, `psutil` |
| `assistant_tools` | all of the above |
| `evaluation/run.py` | `rag_engine`, `memory`, `knowledge_graph`, `mission_control`, `tool_runner`, `google-genai` (online suite only) |

No new pip packages were added: everything uses what was already in `requirements.txt`
(`networkx`, `watchdog`, `psutil`, `requests`, `google-genai`, `sentence-transformers`).

## 3. Features

### 3.1 Mission Control (`backend/mission_control.py`, `backend/tool_runner.py`)
- **Planning/decomposition**: the Gemini Live model breaks the goal into steps through the
  `create_mission` schema (each step = a registered tool). The engine **validates** the plan
  before anything runs: unknown tools, tools that need on-screen approval (sending email),
  missing dependencies, duplicate titles and cycles are rejected with a reason.
- **Execution**: dependency waves (Kahn sort). In a wave, read-only tools run concurrently;
  side-effect tools (mouse, keyboard, files) run one at a time behind a global lock.
- **Shared context**: `{{Step title}}` / `{{Step title.key}}` in args is replaced by that
  step's output.
- **Retries**: exponential back-off (`max_retries`, default 2). `ToolError` (bad arguments,
  unknown or blocked tool, path outside the sandbox) is not retried.
- **Verification**: per-step `verify` rules (`contains`, `not_contains`, `equals`, `field`,
  `min_items`, `path_exists`); file tools also check the destination exists after the copy/move.
- **Checkpoints**: the mission is saved after every step; resume skips completed steps.
- **Pause / resume / cancel** (cancel also cancels the running asyncio task).
- **Failure containment**: dependents of a failed step become `skipped` with the reason.
- **Final result**: counts, duration, `verified`, list of failures.

### 3.2 Cross-application intelligence (`backend/app_context.py`, tools in `tool_runner.py`)
- Real foreground-window tracking (Win32 `GetForegroundWindow` + process name), with a
  history of app switches (`AppContextTracker`, started with the server).
- Workflow tools: `app_active_window`, `app_recent`, `app_wait_window` (polls until a window
  title appears — used to **verify** that `computer_open` worked before typing into it).
- All `computer_*` actions from `computer_bridge` are available as mission steps; shared
  context passes data between apps; read steps run in parallel, actuators sequentially.
- Error recovery = mission retries + verification + skip of dependents.

### 3.3 Event-driven automation (`backend/event_engine.py`)
- Triggers: `schedule` (interval / daily_at, local time), `file_change` (folder, glob pattern,
  debounce), `app_health` (process running / not running, cooldown), `condition` (cpu/ram/disk/
  battery/process_count with `> >= < <= ==`, cooldown), `manual`.
- Actions: `notify`, `run_tool`, `run_mission` — **really executed** through `tool_runner` /
  `mission_control`.
- **User approval**: rules that run tools or missions are stored `approved = 0` and are
  `blocked` (not executed) until the user presses APPROVE in the EVENTS tab. The voice model
  can create rules but can never approve them. Rules the user builds in the UI are approved
  by their author.
- History stores the real outcome: `ok` / `blocked` / `error` + detail.
- File watcher rebuilds its watches within ~3 s when rules change (no restart needed).
- Notifications reach the UI from worker threads (`_emit_threadsafe`).

### 3.4 Knowledge graph (`backend/knowledge_graph.py`)
- NetworkX `MultiDiGraph` persisted atomically to `knowledge_graph.json` (one shared instance).
- Explainable rule-based extraction: capitalised phrases, CamelCase, acronyms; ~50 verb phrases
  mapped to relation types (`uses`, `works_at`, `located_in`, `depends_on`, `created_by`, ...).
- **Source references instead of copies**: `memory:<id>` and `doc:<file>#<chunk>` on every
  node/edge, plus the evidence sentence. Deleting a memory/document removes its facts.
- Queries: `search_graph`, `relations(entity)`, `neighbors(entity, depth)`, `find_path(a, b)`,
  `retrieve_context(query)` (facts + sources, used by the voice tool).

### 3.5 Agent evaluation (`backend/evaluation/`)
Datasets in `evaluation/datasets/*.json`; results in `evaluation/results/`.

| Suite | Measures | How |
|---|---|---|
| `retrieval` | RAG retrieval accuracy (hit@1, hit@3, MRR), latency | 8 docs, 16 paraphrased queries, temp DB |
| `memory` | memory recall accuracy, latency | 10 memories, 12 queries, temp DB |
| `kg` | entity + relation extraction precision/recall/F1 | 15 hand-labelled sentences |
| `missions` | task completion, result correctness, execution reliability (repeat runs), error recovery, failure containment, latency | 9 cases × 3 repeats, real engine + real tools in a temp sandbox, fault-injection tools |
| `tool_selection` (online) | tool-selection accuracy, response latency | 24 utterances → Gemini with Nexora's real tool declarations and system prompt |

### 3.6 Advanced diagnostics (`backend/diagnostics.py`)
Internet, Gemini **authenticated** model-list request (key in header, latency, whether the live
model is listed), OmniRoute, `PRAGMA quick_check` on every database, module imports,
log error analysis grouped by component tag, bottleneck detection (sustained CPU/RAM from
`perf_history.db`, disk, heavy processes, slow/failing tools from telemetry, large DBs),
and a recommendation per finding.

### 3.7 Predictive intelligence (`backend/predictor.py`, `backend/telemetry.py`)
- Data collection is **off by default**. The user turns it on with "Learn from my usage" in the
  DIAG tab (`usage_data_collection` setting). Only tool name, source, success, latency and time
  are stored, in `backend/telemetry.db`, on this PC.
- Below 50 calls over 5 days, it answers "not enough data" with the counts — no guesses.
- Rules with evidence: routines (same tool, same 2-hour window, ≥4 days and ≥60% of active
  days) → suggested schedule rule; sequences (B follows A within 120 s, ≥5 times and ≥50%) →
  suggested mission.
- Backtest (time-ordered 70/30 split): hour-of-day model and next-tool model vs. a
  most-frequent-tool baseline, top-1/top-3; a model is only "useful" if it beats the baseline.

### 3.8 Digital twin (`backend/digital_twin.py`)
All results carry `mode: "SIMULATION", executed: false`; the UI labels them "NOTHING WAS EXECUTED".
- File previews: exact files, bytes, conflicts, permissions, free space at destination.
- Mission simulation: validation, waves (parallel vs sequential), side-effect steps, duration
  estimate from **measured** median latencies (or "no latency data for: …"), failure scenarios
  produced by running the real `MissionEngine` on a sandbox DB with a fake executor.
- Automation dry run: would the rule fire now, is it approved, what would it do.

## 4. Database migrations
Applied automatically at start-up, idempotent, non-destructive:
- `events.db`: `event_rules.approved INTEGER DEFAULT 0` (existing `notify` rules set to 1);
  `event_history.status`, `trigger_source`, `detail`.
- `missions.db`: schema unchanged; new subtask fields live in the JSON `data` column and old
  rows load with defaults.
- `knowledge_graph.json`: v1 files load (edge `relation_type`, `properties.source` →
  `sources`); saved back as v2.
- New: `telemetry.db` (`tool_calls`), only written when collection is on.

## 5. Verification report

### Tests
`cd tests && python -m pytest -q` → **69 passed** (2026-11-12). New files:
`tests/test_advanced_features.py` (38 tests) and `tests/test_server_wiring.py` (3 tests that
drive the real socket handlers in-process). The 28 pre-existing tests still pass.

Bugs the new tests caught during development and that are fixed:
- a checkpoint save overwrote a `paused`/`cancelled` status set from the UI;
- the frontend merged step progress into the mission (overwriting its title).

### Benchmark results (offline suites, run 2026-11-12 01:33)
| Metric | Result | n |
|---|---|---|
| RAG retrieval hit@1 / hit@3 / MRR (semantic) | 100% / 100% / 1.00 | 16 queries |
| RAG search latency median / p95 | 41.6 ms / 61.8 ms | 16 |
| Memory recall hit@1 / hit@3 / MRR (semantic) | 92% / 100% / 0.958 | 12 queries |
| KG entities P / R / F1 | 0.971 / 1.000 / 0.985 | 15 sentences |
| KG relations P / R / F1 | 1.000 / 0.769 / 0.870 | 15 sentences |
| Mission task completion rate | 100% | 4 cases × 3 |
| Mission result correctness (status + every step + disk checks) | 100% | 27 runs |
| Execution reliability (same correct outcome on every repeat) | 100% | 9 cases |
| Error recovery (flaky tool recovered by retry) | 100% | 1 case × 3 |
| Failure containment (dependents skipped, files protected, sandbox) | 100% | 4 cases × 3 |
| Mission latency median / p95 | 71 ms / 136 ms | 27 |

What the misses were (from `evaluation/results/latest.json`):
- Memory: "when do I have to submit the report" ranked the supervisor memory first, the deadline second.
- KG: missed "was developed by", "stores … in", "is written in" (not in the verb list or not
  adjacent); extracted "Dr" as an entity. The extractor was not tuned to this test set.

How to read the mission numbers: they measure the **engine** (ordering, retries, verification,
containment) with deterministic tools. They do not measure how well Gemini plans a mission;
that is what `tool_selection` partly covers.

### Online tool-selection benchmark — NOT YET MEASURED
The suite is implemented, but no valid result exists yet:
- First run: all 24 requests failed — the eval client used API v1, where function calling is
  not enabled (fixed: v1beta), and the key started returning 429.
- Retry: the key's `gemini-2.5-flash` quota was exhausted (429 RESOURCE_EXHAUSTED);
  `gemini-2.0-flash` / `2.5-flash-lite` are no longer available to this key.
- API errors are now excluded from scoring and reported separately, so a quota problem can
  never show up as "0% accuracy". Rerun when the quota resets:
  `python -m evaluation.run --suites tool_selection` (or `--model <name>`).

## 6. Known limitations
- **Planning quality** depends on Gemini; the engine validates plans but does not invent them.
  There is no separate planner model.
- **Tool-selection eval** uses `gemini-2.5-flash` through the normal API, not the Live audio
  model the app talks to (the Live API has no text-only batch mode). Accuracy is a proxy.
  The free-tier key is rate-limited; requests that hit 429 are retried, and any that still fail
  are reported as errors, not as wrong answers.
- **KG extraction** is rule-based English; it misses relations phrased differently and does not
  resolve pronouns or co-references ("he", "the company").
- **Telemetry latency** for voice tools that ask for approval (send email) includes the time the
  user took to approve. Voice success/failure is judged from the tool's reply text.
- **Predictor** gives nothing until enough consented data exists; no machine-learning model is
  trained. Fine-tuning is not implemented (not supported for the Gemini Live model).
- **App context** is Windows-only.
- **File tools** only work under the user's home folder; there is no delete tool (delete is preview-only).
- **Email** in automation: reading works when the voice session's email account is connected;
  sending is never allowed in missions or automations.
- Missions run in the backend process; a backend restart interrupts a running mission (it can
  be resumed from its last checkpoint).
- The new UI controls were verified by `vite build` and the socket-handler tests, not by
  clicking through the running Electron app.

## 7. Installation and configuration
1. `pip install -r requirements.txt` (no new packages).
2. `.env` at the project root: `GEMINI_API_KEY=...` (optional `OMNIROUTE_URL`).
3. Start as before: `START_APA.bat` (or `npm run dev`).
4. Optional: tick **Learn from my usage** in the DIAG tab to enable predictions.
5. Evaluation:
   ```
   cd backend
   python -m evaluation.run              # offline suites
   python -m evaluation.run --online     # + tool selection (uses the Gemini API quota)
   ```
   Or press RUN under AGENT EVALUATION in the DIAG tab (offline suites only).
6. Tests: `cd tests && python -m pytest -q`.
