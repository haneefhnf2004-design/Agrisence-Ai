"""
Predictive Intelligence (stage 1): data collection + explainable rules + honest evaluation.

Data: telemetry.tool_calls, collected only when the user enables `usage_data_collection`.
Nothing here guesses. Below the data thresholds every function returns
status "insufficient_data" with the counts it found.

Rules (each recommendation carries its evidence):
  routine   - the same tool used in the same 2-hour window on >= MIN_ROUTINE_DAYS distinct
              days and on >= 60% of active days -> suggest a scheduled automation rule
  sequence  - tool B follows tool A within 120 s at least MIN_SEQ_SUPPORT times and in >= 50%
              of A's uses -> suggest a two-step mission

Evaluation (backtest on a time-ordered 70/30 split):
  hour model   P(tool | 2-hour bucket)   vs baseline "most frequent tool"
  next model   P(next tool | previous)   vs the same baseline
  top-1 / top-3 accuracy on the held-out 30%. A model is reported as "useful" only if it
  beats the baseline on >= MIN_TEST calls; otherwise the rules above are all we use.
"""
import time
from collections import Counter, defaultdict
from datetime import datetime

MIN_CALLS = 50
MIN_DAYS = 5
MIN_ROUTINE_DAYS = 4
MIN_SEQ_SUPPORT = 5
MIN_TEST = 30
SEQ_WINDOW_S = 120
# Tools the user triggers by talking to Nexora. Mission/event/eval calls are not user behaviour.
USER_SOURCES = ('voice',)


def _bucket(ts):
    return datetime.fromtimestamp(ts).hour // 2


def _day(ts):
    return datetime.fromtimestamp(ts).strftime('%Y-%m-%d')


def data_status(calls: list) -> dict:
    days = {_day(c['ts']) for c in calls}
    return {'calls': len(calls), 'days': len(days),
            'sufficient': len(calls) >= MIN_CALLS and len(days) >= MIN_DAYS,
            'needs': {'calls': MIN_CALLS, 'days': MIN_DAYS}}


def routine_rules(calls: list) -> list:
    days_active = {_day(c['ts']) for c in calls}
    by = defaultdict(set)  # (tool, bucket) -> days
    for c in calls:
        by[(c['tool'], _bucket(c['ts']))].add(_day(c['ts']))
    recs = []
    for (tool, b), days in by.items():
        share = len(days) / len(days_active)
        if len(days) >= MIN_ROUTINE_DAYS and share >= 0.6:
            recs.append({'type': 'routine', 'tool': tool, 'window': f'{b * 2:02d}:00-{b * 2 + 2:02d}:00',
                         'confidence': round(share, 2),
                         'evidence': f'used on {len(days)} of {len(days_active)} active days in this window',
                         'suggestion': {'action': 'create_event_rule', 'trigger_type': 'schedule',
                                        'trigger_config': {'daily_at': f'{b * 2:02d}:00'}, 'action_type': 'run_tool',
                                        'action_config': {'tool_name': tool, 'args': {}}}})
    return sorted(recs, key=lambda r: -r['confidence'])


def sequence_rules(calls: list) -> list:
    calls = sorted(calls, key=lambda c: c['ts'])
    uses, follows = Counter(c['tool'] for c in calls), Counter()
    for a, b in zip(calls, calls[1:]):
        if a['tool'] != b['tool'] and b['ts'] - a['ts'] <= SEQ_WINDOW_S:
            follows[(a['tool'], b['tool'])] += 1
    recs = []
    for (a, b), n in follows.items():
        share = n / uses[a]
        if n >= MIN_SEQ_SUPPORT and share >= 0.5:
            recs.append({'type': 'sequence', 'tools': [a, b], 'confidence': round(share, 2),
                         'evidence': f'{b} followed {a} within {SEQ_WINDOW_S}s in {n} of {uses[a]} uses',
                         'suggestion': {'action': 'create_mission', 'subtasks': [
                             {'title': a, 'tool_name': a, 'args': {}},
                             {'title': b, 'tool_name': b, 'args': {}, 'depends_on': [a]}]}})
    return sorted(recs, key=lambda r: -r['confidence'])


def _topk_acc(preds_and_truth, k):
    if not preds_and_truth:
        return None
    return round(sum(1 for ranked, t in preds_and_truth if t in ranked[:k]) / len(preds_and_truth), 3)


def backtest(calls: list) -> dict:
    calls = sorted(calls, key=lambda c: c['ts'])
    st = data_status(calls)
    if not st['sufficient']:
        return {'status': 'insufficient_data', **st}
    cut = int(len(calls) * 0.7)
    train, test = calls[:cut], calls[cut:]
    if len(test) < MIN_TEST:
        return {'status': 'insufficient_data', 'reason': f'only {len(test)} test calls', **st}
    base_rank = [t for t, _ in Counter(c['tool'] for c in train).most_common()]
    hour = defaultdict(Counter)
    nxt = defaultdict(Counter)
    for c in train:
        hour[_bucket(c['ts'])][c['tool']] += 1
    for a, b in zip(train, train[1:]):
        nxt[a['tool']][b['tool']] += 1

    def rank(counter):
        r = [t for t, _ in counter.most_common()]
        return r + [t for t in base_rank if t not in r]

    base = [(base_rank, c['tool']) for c in test]
    hr = [(rank(hour.get(_bucket(c['ts']), Counter())), c['tool']) for c in test]
    prev = [train[-1]] + test[:-1]
    nx_ = [(rank(nxt.get(p['tool'], Counter())), c['tool']) for p, c in zip(prev, test)]
    res = {'status': 'ok', **st, 'train': len(train), 'test': len(test),
           'baseline': {'top1': _topk_acc(base, 1), 'top3': _topk_acc(base, 3)},
           'hour_model': {'top1': _topk_acc(hr, 1), 'top3': _topk_acc(hr, 3)},
           'next_model': {'top1': _topk_acc(nx_, 1), 'top3': _topk_acc(nx_, 3)}}
    for m in ('hour_model', 'next_model'):
        res[m]['beats_baseline'] = res[m]['top1'] > res['baseline']['top1']
    return res


def recommendations(calls: list = None) -> dict:
    """Main entry point: rule-based recommendations, or an honest 'not enough data' answer."""
    import telemetry
    if calls is None:
        calls = [c for c in telemetry.calls(time.time() - 60 * 86400) if c['source'] in USER_SOURCES and c['ok']]
    st = data_status(calls)
    base = {'collection_enabled': telemetry.is_enabled(), **st}
    if not st['sufficient']:
        return {'status': 'insufficient_data', **base, 'recommendations': [],
                'message': (f"Not enough usage history yet ({st['calls']} calls over {st['days']} days; "
                            f"need {MIN_CALLS} calls over {MIN_DAYS} days)."
                            + ("" if telemetry.is_enabled() else " Usage data collection is off."))}
    return {'status': 'ok', **base, 'recommendations': routine_rules(calls) + sequence_rules(calls)}
