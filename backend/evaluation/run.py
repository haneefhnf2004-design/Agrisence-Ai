"""
Nexora agent evaluation runner. Every number comes from running the real code on the
datasets in evaluation/datasets/; nothing is estimated or hard-coded.

    cd backend
    python -m evaluation.run                 # offline suites: retrieval, memory, kg, missions
    python -m evaluation.run --online        # + tool_selection (calls the Gemini API, needs GEMINI_API_KEY)
    python -m evaluation.run --suites missions --repeats 5

Isolation: every suite works on temporary databases / a temporary sandbox folder, so
the user's memories, documents, graph and missions are never touched.
Results: evaluation/results/<timestamp>.json and evaluation/results/latest.json
"""
import argparse
import asyncio
import copy
import json
import os
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
DATA = os.path.join(HERE, 'datasets')
RESULTS = os.path.join(HERE, 'results')


def _load(name):
    with open(os.path.join(DATA, name), encoding='utf-8') as f:
        return json.load(f)


def _ms(t0):
    return (time.perf_counter() - t0) * 1000


def _lat(xs):
    xs = sorted(xs)
    if not xs:
        return None
    return {'median_ms': round(statistics.median(xs), 1), 'p95_ms': round(xs[min(len(xs) - 1, int(0.95 * (len(xs) - 1) + 0.5))], 1),
            'max_ms': round(xs[-1], 1)}


class _TempGraph:
    """Swap the shared knowledge graph for a temporary one while a suite runs."""
    def __enter__(self):
        import knowledge_graph
        self.kg, self.prev = knowledge_graph, knowledge_graph._singleton
        self.dir = tempfile.mkdtemp(prefix='nexora_eval_kg_')
        knowledge_graph._singleton = knowledge_graph.KnowledgeGraph(os.path.join(self.dir, 'kg.json'))
        return knowledge_graph._singleton

    def __exit__(self, *a):
        self.kg._singleton = self.prev
        shutil.rmtree(self.dir, ignore_errors=True)


def _rank_metrics(ranks, n):
    """ranks: 1-based rank of the correct item per query (None = not returned)."""
    return {'queries': n,
            'hit@1': round(sum(1 for r in ranks if r == 1) / n, 3),
            'hit@3': round(sum(1 for r in ranks if r and r <= 3) / n, 3),
            'mrr': round(sum(1 / r for r in ranks if r) / n, 3)}


# ------------------------------------------------------------------ suites --
def suite_retrieval():
    import rag_engine
    ds = _load('retrieval.json')
    d = tempfile.mkdtemp(prefix='nexora_eval_rag_')
    try:
        with _TempGraph():
            eng = rag_engine.RAGEngine.__new__(rag_engine.RAGEngine)
            eng.db_path = os.path.join(d, 'kb.db')
            eng._embedder, eng._has_embeddings = rag_engine._embedder, rag_engine._has_embeddings
            eng._init_db()
            for fname, text in ds['documents'].items():
                r = eng.ingest_file(fname, text.encode('utf-8'), 'txt')
                assert r.get('ok'), r
            ranks, lats, misses = [], [], []
            for q in ds['queries']:
                t0 = time.perf_counter()
                hits = eng.search_knowledge(q['q'], 5)
                lats.append(_ms(t0))
                order = list(dict.fromkeys(h['filename'] for h in hits))
                r = order.index(q['doc']) + 1 if q['doc'] in order else None
                ranks.append(r)
                if r != 1:
                    misses.append({'query': q['q'], 'expected': q['doc'], 'got': order[:3]})
        return {**_rank_metrics(ranks, len(ranks)), 'mode': 'semantic' if eng._has_embeddings else 'keyword',
                'latency': _lat(lats), 'misses': misses}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def suite_memory():
    import memory as memory_mod
    ds = _load('memory.json')
    d = tempfile.mkdtemp(prefix='nexora_eval_mem_')
    try:
        with _TempGraph():
            eng = copy.copy(memory_mod.memory)          # shares the loaded embedding model
            eng.db_path = os.path.join(d, 'mem.db')
            eng._init_db()
            ids = {}
            for m in ds['memories']:
                ids[eng.save_memory(m['content'], m['category'])['id']] = m['key']
            ranks, lats, misses = [], [], []
            for q in ds['queries']:
                t0 = time.perf_counter()
                hits = eng.search_memories(q['q'], 5)
                lats.append(_ms(t0))
                keys = [ids.get(h.get('id')) for h in hits]
                r = keys.index(q['key']) + 1 if q['key'] in keys else None
                ranks.append(r)
                if r != 1:
                    misses.append({'query': q['q'], 'expected': q['key'], 'got': keys[:3]})
        return {**_rank_metrics(ranks, len(ranks)), 'mode': 'semantic' if eng._has_embeddings else 'keyword',
                'latency': _lat(lats), 'misses': misses}
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _prf(tp, fp, fn):
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {'precision': round(p, 3), 'recall': round(r, 3), 'f1': round(2 * p * r / (p + r), 3) if p + r else 0.0,
            'tp': tp, 'fp': fp, 'fn': fn}


def suite_kg():
    from knowledge_graph import extract
    ds = _load('kg_extraction.json')
    e_tp = e_fp = e_fn = t_tp = t_fp = t_fn = 0
    errors = []
    for c in ds['cases']:
        out = extract(c['text'])
        ents, gold_e = set(out['entities']), set(c['entities'])
        trip = {(s, r, o) for s, r, o, _ in out['relations']}
        gold_t = {tuple(t) for t in c['triples']}
        e_tp += len(ents & gold_e); e_fp += len(ents - gold_e); e_fn += len(gold_e - ents)
        t_tp += len(trip & gold_t); t_fp += len(trip - gold_t); t_fn += len(gold_t - trip)
        if ents != gold_e or trip != gold_t:
            errors.append({'text': c['text'], 'entities_extra': sorted(ents - gold_e), 'entities_missed': sorted(gold_e - ents),
                           'triples_extra': sorted(trip - gold_t), 'triples_missed': sorted(gold_t - trip)})
    return {'cases': len(ds['cases']), 'entities': _prf(e_tp, e_fp, e_fn), 'relations': _prf(t_tp, t_fp, t_fn),
            'errors': errors}


# --- missions ----------------------------------------------------------------
def _register_eval_tools():
    import tool_runner
    counters = {}

    async def flaky(args):
        k = args.get('key', 'default')
        counters[k] = counters.get(k, 0) + 1
        if counters[k] <= int(args.get('fail_times', 1)):
            raise RuntimeError(f'injected failure {counters[k]}')
        return {'attempts': counters[k]}

    async def echo(args):
        return str(args.get('text', ''))
    tool_runner.register('eval_flaky', description='evaluation fault injection')(flaky)
    tool_runner.register('eval_echo', description='evaluation echo')(echo)
    return counters


def _sub(obj, sandbox):
    if isinstance(obj, str):
        return obj.replace('{SANDBOX}', sandbox)
    if isinstance(obj, list):
        return [_sub(x, sandbox) for x in obj]
    if isinstance(obj, dict):
        return {k: _sub(v, sandbox) for k, v in obj.items()}
    return obj


def _check(chk, mission):
    steps = {s['title']: s for s in mission['subtasks']}
    t = chk['type']
    if t == 'file_equals':
        if not (os.path.exists(chk['a']) and os.path.exists(chk['b'])):
            return False
        with open(chk['a'], 'rb') as a, open(chk['b'], 'rb') as b:
            return a.read() == b.read()
    if t == 'not_exists':
        return not os.path.exists(chk['path'])
    if t == 'file_text':
        with open(chk['path'], encoding='utf-8') as f:
            return f.read() == chk['equals']
    if t == 'retries':
        return steps[chk['step']]['retries'] == chk['equals']
    raise ValueError(f'unknown check {t}')


async def _run_case(case, sandbox_root, ds):
    import tool_runner
    from mission_control import MissionEngine
    sandbox = tempfile.mkdtemp(prefix='case_', dir=sandbox_root)
    for rel, text in ds['setup_files'].items():
        p = os.path.join(sandbox, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w', encoding='utf-8') as f:
            f.write(text)
    os.makedirs(os.path.join(sandbox, 'archive'), exist_ok=True)
    tool_runner.FILE_ROOTS = [sandbox]
    eng = MissionEngine(db_path=os.path.join(sandbox_root, f'm_{os.path.basename(sandbox)}.db'), retry_base_delay=0.01)
    subtasks = _sub(case['subtasks'], sandbox.replace('\\', '/'))
    t0 = time.perf_counter()
    mid = eng.create_mission(case['id'], 'eval', subtasks)
    await eng.execute_mission(mid)
    dur = _ms(t0)
    m = eng.get_mission(mid)
    exp = case['expect']
    got_steps = {s['title']: s['status'] for s in m['subtasks']}
    checks = [_check(_sub(c, sandbox.replace('\\', '/')), m) for c in case.get('checks', [])]
    correct = m['status'] == exp['status'] and got_steps == exp['steps'] and all(checks)
    return {'status': m['status'], 'steps': got_steps, 'checks': checks, 'correct': correct, 'ms': dur}


def suite_missions(repeats=3):
    import tool_runner
    import knowledge_graph
    ds = _load('missions.json')
    roots_before = list(tool_runner.FILE_ROOTS)
    counters = _register_eval_tools()
    root = tempfile.mkdtemp(prefix='nexora_eval_missions_')
    per_case = {}
    try:
        with _TempGraph():
            for case in ds['cases']:
                runs = []
                for _ in range(repeats):
                    counters.clear()
                    runs.append(asyncio.run(_run_case(case, root, ds)))
                per_case[case['id']] = {'tags': case['tags'], 'expected': case['expect']['status'], 'runs': runs,
                                        'all_correct': all(r['correct'] for r in runs),
                                        'consistent': len({json.dumps([r['status'], r['steps']]) for r in runs}) == 1}
    finally:
        tool_runner.FILE_ROOTS = roots_before
        tool_runner.REGISTRY.pop('eval_flaky', None)
        tool_runner.REGISTRY.pop('eval_echo', None)
        shutil.rmtree(root, ignore_errors=True)

    def rate(pred, ids):
        ids = list(ids)
        return round(sum(1 for i in ids if pred(per_case[i])) / len(ids), 3) if ids else None
    all_runs = [r for c in per_case.values() for r in c['runs']]
    completion_cases = [i for i, c in per_case.items() if 'completion' in c['tags']]
    return {
        'cases': len(per_case), 'repeats': repeats, 'runs': len(all_runs),
        'task_completion_rate': round(sum(1 for i in completion_cases for r in per_case[i]['runs'] if r['status'] == 'completed')
                                      / (len(completion_cases) * repeats), 3),
        'result_correctness': round(sum(1 for r in all_runs if r['correct']) / len(all_runs), 3),
        'execution_reliability': rate(lambda c: c['all_correct'] and c['consistent'], per_case),
        'error_recovery_rate': rate(lambda c: c['all_correct'], [i for i, c in per_case.items() if 'recovery' in c['tags']]),
        'failure_containment_rate': rate(lambda c: c['all_correct'], [i for i, c in per_case.items() if 'failure_containment' in c['tags']]),
        'latency': _lat([r['ms'] for r in all_runs]),
        'per_case': {i: {'correct_runs': sum(r['correct'] for r in c['runs']), 'expected': c['expected'],
                         'last_status': c['runs'][-1]['status'], 'last_steps': c['runs'][-1]['steps']}
                     for i, c in per_case.items()},
    }


# --- tool selection (online) -------------------------------------------------
def _system_prompt():
    import ast
    src = open(os.path.join(BACKEND, 'ada.py'), encoding='utf-8').read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.keyword) and node.arg == 'system_instruction':
            try:
                return ast.literal_eval(node.value)
            except Exception:
                return None
    return None


def suite_tool_selection(model='gemini-2.5-flash', pause_s=6.5):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(BACKEND), '.env'))
    key = os.environ.get('GEMINI_API_KEY')
    if not key:
        return {'status': 'skipped', 'reason': 'GEMINI_API_KEY not set'}
    from google import genai
    from google.genai import types
    from tools import tools_list
    decls = tools_list[0]['function_declarations']
    client = genai.Client(api_key=key, http_options={'api_version': 'v1beta'})  # function calling needs v1beta
    sys_prompt = _system_prompt()
    cfg = types.GenerateContentConfig(
        system_instruction=sys_prompt, temperature=0,
        tools=[types.Tool(function_declarations=decls)],
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    ds = _load('tool_selection.json')
    rows, lats, errors = [], [], 0
    declared = {d['name'] for d in decls}
    for c in ds['cases']:
        got, api_error = None, None
        for attempt in range(4):
            t0 = time.perf_counter()
            try:
                r = client.models.generate_content(model=model, contents=c['u'], config=cfg)
                calls = [p.function_call.name for cand in (r.candidates or []) for p in ((cand.content and cand.content.parts) or [])
                         if getattr(p, 'function_call', None)]
                got, api_error = (calls[0] if calls else None), None
                lats.append(_ms(t0))
                break
            except Exception as e:
                api_error = f'{type(e).__name__}: {str(e)[:160]}'
                if '429' in api_error and attempt < 3:   # rate limited: wait and retry, never count it as a miss
                    time.sleep(20 * (attempt + 1))
                    continue
                break
        if api_error:
            errors += 1
        ok = None if api_error else (got == c['expected'] or got in c.get('acceptable', []))
        rows.append({'utterance': c['u'], 'expected': c['expected'], 'got': got, 'correct': ok, 'api_error': api_error,
                     'expected_declared': c['expected'] in declared})
        time.sleep(pause_s)
    valid = [r for r in rows if r['expected_declared'] and r['correct'] is not None]
    if not valid:
        return {'status': 'unavailable', 'model': model, 'reason': 'every request failed: ' + (rows[0]['api_error'] if rows else ''),
                'cases': len(rows), 'api_errors': errors}
    return {'status': 'ok', 'model': model, 'system_prompt': 'ada.py' if sys_prompt else 'none',
            'cases': len(rows), 'scored': len(valid), 'accuracy': round(sum(r['correct'] for r in valid) / len(valid), 3),
            'no_tool_called': sum(1 for r in rows if r['got'] is None), 'api_errors': errors,
            'undeclared_expected_tools': [r['expected'] for r in rows if not r['expected_declared']],
            'latency': _lat(lats), 'mistakes': [r for r in rows if r['correct'] is False],
            'errored': [r['utterance'] for r in rows if r['api_error']]}


SUITES = {'retrieval': suite_retrieval, 'memory': suite_memory, 'kg': suite_kg, 'missions': suite_missions,
          'tool_selection': suite_tool_selection}


def run(suites=None, online=False, repeats=3, model=None) -> dict:
    suites = suites or ['retrieval', 'memory', 'kg', 'missions'] + (['tool_selection'] if online else [])
    out = {'timestamp': datetime.now().isoformat(timespec='seconds'), 'suites': {}}
    for s in suites:
        t0 = time.perf_counter()
        try:
            res = (SUITES[s](repeats) if s == 'missions'
                   else SUITES[s](model) if s == 'tool_selection' and model else SUITES[s]())
        except Exception as e:
            res = {'status': 'error', 'error': f'{type(e).__name__}: {e}'}
        res['suite_seconds'] = round(_ms(t0) / 1000, 2)
        out['suites'][s] = res
    os.makedirs(RESULTS, exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    with open(os.path.join(RESULTS, f'{stamp}.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, indent=2, default=str)
    merged = latest() or {'suites': {}}       # latest.json keeps the newest result of EVERY suite
    for name, res in out['suites'].items():
        merged['suites'][name] = {**res, 'run_at': out['timestamp']}
    merged['timestamp'] = out['timestamp']
    with open(os.path.join(RESULTS, 'latest.json'), 'w', encoding='utf-8') as f:
        json.dump(merged, f, indent=2, default=str)
    return out


def latest() -> dict:
    p = os.path.join(RESULTS, 'latest.json')
    if not os.path.exists(p):
        return {}
    with open(p, encoding='utf-8') as f:
        return json.load(f)


def summary(res: dict) -> str:
    lines = [f"Nexora evaluation {res['timestamp']}"]
    s = res['suites']
    if 'retrieval' in s and 'hit@1' in s['retrieval']:
        r = s['retrieval']; lines.append(f"RAG retrieval ({r['mode']}): hit@1 {r['hit@1']:.0%}, hit@3 {r['hit@3']:.0%}, MRR {r['mrr']}, median {r['latency']['median_ms']} ms, n={r['queries']}")
    if 'memory' in s and 'hit@1' in s['memory']:
        r = s['memory']; lines.append(f"Memory recall ({r['mode']}): hit@1 {r['hit@1']:.0%}, hit@3 {r['hit@3']:.0%}, MRR {r['mrr']}, median {r['latency']['median_ms']} ms, n={r['queries']}")
    if 'kg' in s and 'relations' in s['kg']:
        r = s['kg']; lines.append(f"KG extraction: entities P {r['entities']['precision']} R {r['entities']['recall']} F1 {r['entities']['f1']}; relations P {r['relations']['precision']} R {r['relations']['recall']} F1 {r['relations']['f1']}, n={r['cases']}")
    if 'missions' in s and 'result_correctness' in s['missions']:
        r = s['missions']; lines.append(f"Missions: completion {r['task_completion_rate']:.0%}, correctness {r['result_correctness']:.0%}, reliability {r['execution_reliability']:.0%}, recovery {r['error_recovery_rate']:.0%}, containment {r['failure_containment_rate']:.0%}, median {r['latency']['median_ms']} ms, runs={r['runs']}")
    if 'tool_selection' in s:
        r = s['tool_selection']
        if r.get('status') == 'ok' and 'scored' in r:
            lines.append(f"Tool selection ({r.get('model')}): accuracy {r['accuracy']:.0%} on {r['scored']} scored of {r['cases']}, "
                         f"median {r['latency']['median_ms']} ms, api errors {r['api_errors']}")
        else:
            lines.append(f"Tool selection: {r.get('status')} ({r.get('reason') or r.get('error') or 'old result format, rerun'})")
    for name, r in s.items():
        if r.get('status') == 'error':
            lines.append(f"{name}: ERROR {r['error']}")
    return "\n".join(lines)


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--suites', help='comma list: ' + ','.join(SUITES))
    ap.add_argument('--online', action='store_true', help='include tool_selection (Gemini API calls)')
    ap.add_argument('--repeats', type=int, default=3)
    ap.add_argument('--model', help='Gemini model for tool_selection (default gemini-2.5-flash)')
    a = ap.parse_args()
    res = run(a.suites.split(',') if a.suites else None, a.online, a.repeats, a.model)
    print(summary(res))
