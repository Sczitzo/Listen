"""Dictation deadlines/backoff must preserve exact input and unrelated workflows."""
from pathlib import Path
import json
import sys
import queue
import threading
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d
from cleanup_policy import CleanupPolicy

clock = [1000.0]
d._cleanup_policy = CleanupPolicy(clock=lambda: clock[0])
d._quitting = False
d.SETTINGS.update(cleanup=True, local_model='qwen2.5:7b', style='tighten')
raw = 'Do not send Alex the 17 files.'
calls = []

def timeout(payload, *, timeout):
    calls.append((payload, timeout))
    raise d.cleanup_http.CleanupTimeoutError()

with patch.object(d, 'system_prompt', return_value='Synthetic system.'), \
     patch.object(d, 'vocab_words', return_value=[]), \
     patch.object(d.cleanup_http, 'post_json', timeout), \
     patch.object(d.cleanup_http, 'warm_json', side_effect=AssertionError('Warm during cooldown')):
    assert d.refine(raw) == raw
    assert len(calls) == 1 and 14.9 <= calls[0][1] <= 15.0
    assert calls[0][0]['messages'][-1]['content'] == raw
    assert calls[0][0]['options'] == {'temperature': 0, 'num_ctx': 8192, 'num_predict': 4000, 'num_batch': 128}
    assert not d._llm_ok and d._last_cleanup_timing['outcome'] == 'timeout'
    assert d.refine(raw) == raw and len(calls) == 1
    assert d._last_cleanup_timing == {'outcome': 'cooldown', 'request_ms': 0.0}
    d._warm_llm()
    clock[0] += 29.9
    assert d.refine(raw) == raw and len(calls) == 1

clock[0] += .1
def success(payload, *, timeout):
    calls.append((payload, timeout))
    return {'done_reason': 'stop', 'message': {'content': raw}, 'load_duration': 250_000_000}
with patch.object(d, 'system_prompt', return_value='Synthetic system.'), \
     patch.object(d, 'vocab_words', return_value=[]), \
     patch.object(d.cleanup_http, 'post_json', success):
    assert d.refine(raw) == raw and len(calls) == 2 and d._llm_ok
    assert d._last_cleanup_timing['load_ms'] == 250.0
    assert raw not in str(d._last_cleanup_timing)
    d.SETTINGS['cleanup'] = False
    assert d.refine(raw) == raw and len(calls) == 2
    d.SETTINGS['cleanup'] = True

# Cancellation acknowledgment consumes the same final cleanup budget. Control
# the clock to detect accidentally granting a fresh 15s after waiting.
budget_clock = [500.0]
def wait_for_draft(timeout):
    assert timeout == .5
    budget_clock[0] += .4
    return True
with patch.object(d.time, 'perf_counter', lambda: budget_clock[0]), \
     patch.object(d._draft.gate, 'wait_draft', wait_for_draft), \
     patch.object(d, 'system_prompt', return_value='Synthetic system.'), \
     patch.object(d, 'vocab_words', return_value=[]), \
     patch.object(d.cleanup_http, 'post_json', success):
    before = len(calls)
    assert d.refine(raw) == raw and len(calls) == before + 1
    assert abs(calls[-1][1] - 14.6) < 1e-8

# Local engineering's unbudgeted chat retains its separate policy.
class Response:
    def raise_for_status(self): pass
    def json(self): return {'done_reason': 'stop', 'message': {'content': 'Synthetic rewrite.'}}
with patch.object(d.requests, 'post', return_value=Response()) as post, \
     patch.object(d.cleanup_http, 'post_json', side_effect=AssertionError('Wrong transport')):
    assert d._chat('System.', 'Request.') == 'Synthetic rewrite.'
    assert post.call_args.kwargs['timeout'] == 120
    assert post.call_args.kwargs['json']['options']['num_batch'] == 128

class JudgeResponse(Response):
    def json(self): return {'message': {'content': json.dumps({'score': 7, 'criteria': {}, 'gaps': []})}}
with patch.object(d.requests, 'post', return_value=JudgeResponse()) as post:
    assert d._judge_local_impl('Synthetic request.')['score'] == 7
    assert post.call_args.kwargs['timeout'] == 180
    assert post.call_args.kwargs['json']['options']['num_batch'] == 128

# Prewarm uses the selected model, fixed retention/context and bounded route.
clock[0] += 240
warms = []
with patch.object(d.cleanup_http, 'warm_json', lambda payload, timeout: warms.append((payload, timeout))):
    d._warm_llm(interval=240)
    d._warm_llm(interval=240)
assert len(warms) == 1
assert warms[0] == ({'model': 'qwen2.5:7b', 'keep_alive': '5m', 'options': {'num_ctx': 8192, 'num_batch': 128}}, 60.0)

# A successful current-model warm must recover the displayed offline state.
d._cleanup_policy = CleanupPolicy()
d._llm_ok = False
with patch.object(d.cleanup_http, 'warm_json', return_value={}):
    d._warm_llm()
assert d._llm_ok

# An early Finish keeps its own deadline while the cold warm request is active.
d._cleanup_policy = CleanupPolicy()
d._llm_ok = False
def finish_while_warming(payload, timeout):
    assert timeout == 60
    assert d.Api().get_state()['cleanup_warming']
    with patch.object(d.cleanup_http, 'post_json', side_effect=d.cleanup_http.CleanupTimeoutError()):
        assert d.refine(raw) == raw
    assert not d.Api().get_state()['cleanup_warming'], 'Warming hid a newer cleanup timeout'
    return {}
with patch.object(d.cleanup_http, 'warm_json', finish_while_warming):
    d._warm_llm()
assert not d._llm_ok and not d.Api().get_state()['cleanup_warming']
assert d.CLEANUP_TIMEOUT_S == 15 and d.CLEANUP_WARM_TIMEOUT_S == 60

# Model changes, newer failures, newer real successes and Quit beat older warming.
for event in ('model', 'timeout', 'engineer', 'judge', 'quit'):
    d._cleanup_policy = CleanupPolicy()
    d._llm_ok = False
    d._quitting = False
    d.SETTINGS['local_model'] = 'qwen2.5:7b'
    def interrupted_warm(payload, timeout):
        if event == 'model':
            d.SETTINGS['local_model'] = 'other-model'
        elif event == 'timeout':
            d._set_cleanup_status('qwen2.5:7b', False, timed_out=True)
        elif event in ('engineer', 'judge'):
            with patch.object(d.requests, 'post', return_value=JudgeResponse() if event == 'judge' else Response()):
                if event == 'judge': d._judge_local_impl('Synthetic request.')
                else: d._chat('Synthetic system.', 'Synthetic request.')
            raise d.cleanup_http.CleanupHTTPError('synthetic_old_warm_failure')
        else:
            d._quitting = True
        return {}
    with patch.object(d.cleanup_http, 'warm_json', interrupted_warm):
        d._warm_llm()
    assert d._llm_ok == (event in ('engineer', 'judge')), event
    if event in ('engineer', 'judge'): assert d._cleanup_policy.available('qwen2.5:7b')
    lease = d._draft.gate.try_cleanup_warm()
    assert lease, 'Warm completion leaked its gate'
    lease.release()
d._quitting = False
d.SETTINGS['local_model'] = 'qwen2.5:7b'

# Selection acknowledgements invalidate readiness; old-model completions cannot restore it.
d._llm_ok = True
with patch.object(d.Api, 'get_local_models', return_value={'models': ['other-model']}):
    assert d.Api().set_setting('local_model', 'other-model')
assert not d._llm_ok
d._set_cleanup_status('qwen2.5:7b', True)
assert not d._llm_ok
d.SETTINGS['local_model'] = 'qwen2.5:7b'

# A newer timeout cannot slip between warm-policy acceptance and readiness publication.
d._cleanup_policy = CleanupPolicy()
d._llm_ok = False
accepted, release_warm, timeout_started, timeout_finished = (threading.Event() for _ in range(4))
finish_warm = d._cleanup_policy.finish_warm
errors = []
def paused_finish(token, *, success):
    result = finish_warm(token, success=success)
    accepted.set()
    assert release_warm.wait(2)
    return result
def warm_thread():
    try: d._warm_llm()
    except Exception as error: errors.append(error)
def timeout_thread():
    timeout_started.set()
    d._set_cleanup_status('qwen2.5:7b', False, timed_out=True)
    timeout_finished.set()
with patch.object(d.cleanup_http, 'warm_json', return_value={}), \
     patch.object(d._cleanup_policy, 'finish_warm', paused_finish):
    warmer = threading.Thread(target=warm_thread)
    newer = threading.Thread(target=timeout_thread)
    warmer.start()
    try:
        assert accepted.wait(1)
        newer.start()
        assert timeout_started.wait(1)
        assert not timeout_finished.wait(.03), 'Readiness publication released its lock too early'
    finally:
        release_warm.set()
        warmer.join(2)
        if newer.ident is not None: newer.join(2)
assert not warmer.is_alive() and not newer.is_alive() and not errors
assert timeout_finished.is_set() and not d._llm_ok

# An older error's fallback handler must not publish offline a second time after recovery.
for operation in ('cleanup', 'engineer'):
    d._cleanup_policy = CleanupPolicy()
    d._llm_ok = False
    failures = []
    publish = d._set_cleanup_status
    def recovered_between_handlers(selected, ready, **kwargs):
        result = publish(selected, ready, **kwargs)
        if not ready:
            failures.append(selected)
            if len(failures) == 1:
                publish(selected, True)
        return result
    with patch.object(d, '_set_cleanup_status', recovered_between_handlers), \
         patch.object(d.cleanup_http, 'post_json', side_effect=d.cleanup_http.CleanupTimeoutError()), \
         patch.object(d.requests, 'post', side_effect=d.requests.ConnectionError('synthetic')):
        if operation == 'cleanup': assert d.refine(raw) == raw
        else:
            try: d.engineer('Synthetic request.')
            except RuntimeError: pass
            else: raise AssertionError('Failed rewrite was treated as success')
    assert len(failures) == 1 and d._llm_ok, operation

# Exercise the real idle worker ticks: 4-minute active refresh, no idle service.
for active, cancel_before_request in ((True, False), (False, False), (True, True)):
    now = [1000.]
    policy = CleanupPolicy(clock=lambda: now[0])
    policy.succeeded('qwen2.5:7b')
    recording = threading.Event()
    if active: recording.set()
    ticks, requests = [], []
    class EmptyQueue:
        unfinished_tasks = 0
        def get(self, timeout):
            now[0] += (239., 1., 0.)[len(ticks)]
            raise queue.Empty()
    class ImmediateThread:
        def __init__(self, target, args=(), **kwargs): self.target, self.args = target, args
        def start(self):
            if cancel_before_request: recording.clear()
            self.target(*self.args)
    def tick():
        ticks.append(now[0])
        if len(ticks) == 3: d._quitting = True
    with patch.object(d, '_cleanup_policy', policy), patch.object(d, '_recording', recording), \
         patch.object(d, '_quitting', False), patch.object(d, '_jobs', EmptyQueue()), \
         patch.object(d, '_preview', SimpleNamespace(in_flight=lambda: False)), \
         patch.object(d, '_maybe_preview', tick), patch.object(d.threading, 'Thread', ImmediateThread), \
         patch.object(d.cleanup_http, 'warm_json', lambda payload, timeout: requests.append((payload, timeout))):
        d.worker()
    assert len(requests) == (1 if active and not cancel_before_request else 0)
    assert ticks == [1239., 1240., 1240.]
    if cancel_before_request: assert policy.available('qwen2.5:7b')
print('Cleanup integration: shared 15s wait/request budget, exact raw fallback, cooldown/recovery, model/options, '
      'content-free timing, separate engineering and coalesced warm route passed')
