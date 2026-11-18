"""
Digital Twin = concrete, testable previews of what Nexora WOULD do. Nothing here changes
the user's files, apps or data. Every result carries mode="SIMULATION", executed=False.

  preview_file_operation   copy / move / rename / delete: exact files affected, bytes,
                           conflicts, permission problems, free disk space at the target
  simulate_mission         validates the plan, shows execution waves (what runs in parallel),
                           side effects, a duration estimate from MEASURED tool latencies
                           (telemetry; 'unknown' when there is no data), and failure scenarios:
                           the real MissionEngine is run against a sandbox database with a
                           fake executor, so skip/retry behaviour is the engine's actual behaviour
  dry_run_rule             would an automation rule fire right now, and what would it do
"""
import os
import shutil
import tempfile
import fnmatch
import time

SIM = {'mode': 'SIMULATION', 'executed': False}


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def preview_file_operation(op: str, src: str, dst: str = None, pattern: str = None, overwrite: bool = False) -> dict:
    import tool_runner
    op = (op or '').lower()
    issues, ops = [], []
    if op not in ('copy', 'move', 'rename', 'delete'):
        return {**SIM, 'would_succeed': False, 'issues': [f"unknown operation '{op}' (copy, move, rename, delete)"]}
    try:
        src_full = tool_runner._safe_path(src)
    except tool_runner.ToolError as e:
        return {**SIM, 'would_succeed': False, 'issues': [str(e)]}
    if not os.path.exists(src_full):
        return {**SIM, 'would_succeed': False, 'issues': [f'source does not exist: {src_full}']}
    if pattern and os.path.isdir(src_full):
        sources = [os.path.join(src_full, n) for n in sorted(os.listdir(src_full)) if fnmatch.fnmatch(n, pattern)]
    else:
        sources = [src_full]
    dst_full = None
    if op != 'delete':
        if not dst:
            return {**SIM, 'would_succeed': False, 'issues': ['destination is required']}
        try:
            dst_full = tool_runner._safe_path(dst)
        except tool_runner.ToolError as e:
            return {**SIM, 'would_succeed': False, 'issues': [str(e)]}
        many = len(sources) > 1 or pattern
        dest_dir = dst_full if (os.path.isdir(dst_full) or many) else os.path.dirname(dst_full)
        if not os.path.isdir(dest_dir):
            issues.append(f'destination folder does not exist: {dest_dir}')
        elif not os.access(dest_dir, os.W_OK):
            issues.append(f'no write permission for {dest_dir}')
    total = 0
    for s in sources:
        size = _size(s)
        total += size
        entry = {'src': s, 'bytes': size, 'is_dir': os.path.isdir(s)}
        if not os.access(s, os.R_OK):
            entry['issue'] = 'not readable'
        if op in ('move', 'rename', 'delete') and not os.access(os.path.dirname(s), os.W_OK):
            entry['issue'] = 'source folder not writable'
        if dst_full:
            target = os.path.join(dst_full, os.path.basename(s)) if (os.path.isdir(dst_full) or len(sources) > 1 or pattern) else dst_full
            entry['dst'] = target
            if os.path.exists(target):
                entry['conflict'] = 'overwrite' if overwrite else 'destination exists (operation would be refused)'
        ops.append(entry)
    for e in ops:
        if e.get('issue'):
            issues.append(f"{e['src']}: {e['issue']}")
        if e.get('conflict') and not overwrite:
            issues.append(f"{e.get('dst')}: {e['conflict']}")
    disk = None
    if dst_full and op in ('copy', 'move', 'rename'):
        probe = dst_full if os.path.exists(dst_full) else os.path.dirname(dst_full)
        same_device = os.path.splitdrive(src_full)[0].lower() == os.path.splitdrive(probe)[0].lower()
        needs = total if (op == 'copy' or not same_device) else 0
        try:
            free = shutil.disk_usage(probe).free
            disk = {'free_bytes': free, 'needed_bytes': needs, 'enough': free > needs, 'same_device': same_device}
            if free <= needs:
                issues.append(f'not enough free space: need {needs} bytes, {free} free')
        except OSError as e:
            issues.append(f'could not read free space: {e}')
    executable = None
    if op in ('copy', 'move', 'rename') and len(ops) == 1:
        executable = {'tool_name': 'file_copy' if op == 'copy' else 'file_move',
                      'args': {'src': ops[0]['src'], 'dst': ops[0].get('dst'), 'overwrite': overwrite}}
    return {**SIM, 'operation': op, 'files': len(ops), 'total_bytes': total, 'operations': ops[:200],
            'disk': disk, 'issues': issues, 'would_succeed': not issues, 'executable_as': executable,
            'note': 'Nexora has no delete tool; delete is preview-only.' if op == 'delete' else None}


def estimate_duration(subtasks: list, waves: list) -> dict:
    """Sum of wave times; a wave of parallel steps costs its slowest step. Uses median measured latency."""
    import telemetry, tool_runner
    stats = telemetry.latency_stats(time.time() - 30 * 86400)
    by_id = {s['id']: s for s in subtasks}
    total, unknown, per_step = 0.0, [], {}
    for wave in waves:
        par, seq = [], []
        for sid in wave:
            s = by_id[sid]
            st = stats.get(s['tool_name'])
            if s['tool_name'] == 'wait':
                ms = float(s['args'].get('seconds', 1)) * 1000
            elif st:
                ms = st['median_ms']
            else:
                unknown.append(s['title'])
                continue
            per_step[s['title']] = ms
            spec = tool_runner.spec(s['tool_name'])
            (par if spec and spec.parallel_safe else seq).append(ms)
        total += (max(par) if par else 0) + sum(seq)
    return {'estimated_ms': round(total, 1) if not unknown else None,
            'known_part_ms': round(total, 1), 'per_step_ms': per_step,
            'no_latency_data_for': unknown,
            'basis': 'median of measured telemetry latencies' if per_step else 'no measured latencies'}


async def simulate_mission(subtasks: list, fail_steps: list = None) -> dict:
    import tool_runner
    from mission_control import MissionEngine, engine as live
    plan = live.validate_plan(subtasks)
    if not plan['ok']:
        return {**SIM, 'valid': False, 'errors': plan['errors']}
    subs, waves = plan['subtasks'], plan['waves']
    title = {s['id']: s['title'] for s in subs}
    wave_view = []
    for w in waves:
        steps = [title[i] for i in w]
        par = [title[i] for i in w if (tool_runner.spec(next(s for s in subs if s['id'] == i)['tool_name']) or tool_runner.ToolSpec('', None)).parallel_safe]
        wave_view.append({'steps': steps, 'parallel': par if len(par) > 1 else [], 'sequential': [s for s in steps if s not in par or len(par) == 1]})
    side_effects = [s['title'] for s in subs if (tool_runner.spec(s['tool_name']) and tool_runner.spec(s['tool_name']).side_effect)]

    async def run_sandbox(fail: set):
        d = tempfile.mkdtemp(prefix='nexora_twin_')
        try:
            async def executor(tool, args):
                if tool == '__fail__':
                    raise RuntimeError('simulated failure')
                return {'ok': True, 'output': {'simulated': True, 'tool': tool}}
            eng = MissionEngine(db_path=os.path.join(d, 'twin.db'), executor=executor, retry_base_delay=0)
            eng.custom_parallel_safe = lambda n: bool(tool_runner.spec(n) and tool_runner.spec(n).parallel_safe)
            eng.resolve_args = lambda a, m: a     # outputs are fake, so templates are not resolved
            sim_subs = [{**s, 'verify': None, 'max_retries': 0,
                         'tool_name': '__fail__' if (s['title'] in fail or s['id'] in fail) else s['tool_name']}
                        for s in subs]
            mid = eng.create_mission('twin', 'simulation', sim_subs)
            await eng.execute_mission(mid)
            r = eng.get_mission(mid)
            return {'status': r['status'], 'steps': {s['title']: s['status'] for s in r['subtasks']},
                    'skipped': [s['title'] for s in r['subtasks'] if s['status'] == 'skipped']}
        finally:
            shutil.rmtree(d, ignore_errors=True)

    scenarios = []
    targets = fail_steps or [s['title'] for s in subs]
    for t in targets:
        out = await run_sandbox({t})
        scenarios.append({'if_fails': t, 'mission_status': out['status'], 'would_skip': out['skipped']})
    return {**SIM, 'valid': True, 'steps': len(subs), 'waves': wave_view, 'side_effect_steps': side_effects,
            'estimate': estimate_duration(subs, waves), 'failure_scenarios': scenarios,
            'happy_path': await run_sandbox(set())}


async def dry_run_rule(event_engine, rule_id: str) -> dict:
    rules = {r['id']: r for r in event_engine.list_rules()}
    r = rules.get(rule_id)
    if not r:
        return {**SIM, 'error': f'no rule {rule_id}'}
    import asyncio
    due = await asyncio.to_thread(event_engine.due_rules)
    hit = next((why for rd, why in due if rd['id'] == rule_id), None)
    out = {**SIM, 'rule': r['name'], 'enabled': r['enabled'], 'trigger_type': r['trigger_type'],
           'would_fire_now': hit is not None if r['trigger_type'] not in ('file_change', 'manual') else None,
           'reason': hit or ('fires on file events only' if r['trigger_type'] == 'file_change'
                             else 'fires only when triggered by hand' if r['trigger_type'] == 'manual' else 'condition not met'),
           'action_type': r['action_type'], 'approved': r['approved'],
           'action_would_execute': r['approved'] or r['action_type'] == 'notify'}
    if r['action_type'] == 'run_tool':
        import tool_runner
        s = tool_runner.spec(r['action_config'].get('tool_name'))
        out['tool'] = {'name': r['action_config'].get('tool_name'), 'exists': bool(s),
                       'side_effect': bool(s and s.side_effect), 'args': r['action_config'].get('args', {})}
    elif r['action_type'] == 'run_mission':
        out['mission'] = await simulate_mission(r['action_config'].get('subtasks') or [])
    return out
