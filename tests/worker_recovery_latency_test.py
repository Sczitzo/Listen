"""A failed job must not delay queued work merely to display an error."""
import ast
from pathlib import Path
import queue
import threading
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from draft_preview import LocalModelGate


source = (Path(__file__).resolve().parents[1] / 'dictate.py').read_text(encoding='utf-8')
tree = ast.parse(source)
nodes = [next(node for node in tree.body if isinstance(node, ast.FunctionDef)
              and node.name == name) for name in ('worker', 'ui_state')]


def exercise(failure, *, attention_until=0.0, quit_after_failure=False):
    clock = SimpleNamespace(now=100.0, sleeps=[])

    def sleep(seconds):
        clock.sleeps.append(seconds)
        clock.now += seconds

    jobs = queue.Queue()
    releases = []
    for value in ((1,) if quit_after_failure else (1, 2)):
        releases.append(SimpleNamespace(wait=lambda: None))
        jobs.put(([value], 1.0, 99.0, None, (0, 0), releases[-1]))
    scope = {'queue': queue, 'SpeechUnavailableError': LookupError,
             '_quitting': False, '_jobs': jobs, '_busy': '', '_last_error': '',
             '_state': 'idle', '_delivery_attention_until': attention_until,
             '_recording': threading.Event(), 'IDLE_WORKER_POLL_S': .25,
             '_preview': SimpleNamespace(in_flight=lambda: False, snapshot=lambda: {'text': ''}),
             '_draft': SimpleNamespace(gate=LocalModelGate()),
             '_mic_lock': threading.RLock(), '_preview_final_jobs': {},
             '_finish_preview': lambda *args: None,
             '_lab_thread': SimpleNamespace(),
             '_speech_status': lambda: {'state': 'failed'},
             '_maybe_preview': lambda: None,
             'time': SimpleNamespace(monotonic=lambda: clock.now, perf_counter=lambda: clock.now, sleep=sleep),
             'print': lambda *args: None}
    calls, error_states = [], []

    def process(chunks, *args):
        calls.append((chunks[0], clock.now))
        if chunks[0] == 1 and failure:
            if quit_after_failure:
                scope['_quitting'] = True
            raise failure
        if chunks[0] == 2:
            # The prior error stays available without blocking new work.
            assert scope['_busy'] == 'transcribing'
            assert scope['ui_state']() == 'transcribing'
            scope['_quitting'] = True

    scope['process'] = process
    scope['_chime'] = lambda name: error_states.append(name)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<actual-worker>', 'exec'), scope)
    scope['worker']()
    assert len(calls) == (1 if quit_after_failure else 2) and jobs.unfinished_tasks == 0
    if not quit_after_failure:
        assert calls[1][1] == calls[0][1], 'Queued dictation delayed by error-display sleep'
    assert not clock.sleeps, 'Worker must not sleep to display an error'
    assert scope['_busy'] == ''
    assert set(scope['_last_dictation_timing']) == {'queue_ms', 'microphone_wait_ms', 'outcome', 'worker_total_ms',
                                                 'draft_cancel_ms', 'draft_client_released'}
    assert scope['_last_dictation_timing']['draft_client_released'] is True
    assert not hasattr(scope['_lab_thread'], 'timings'), 'Timing context leaked into next work'
    assert not hasattr(scope['_lab_thread'], 'preview_generation'), 'Recording identity leaked into next work'
    lease = scope['_draft'].gate.try_auxiliary()
    assert lease is not None, 'Final worker leaked its authoritative reservation'
    lease.release()
    if failure:
        assert error_states == ['error'] and scope['_last_error']
        assert scope['ui_state']() == 'error', 'Idle error indicator disappeared'
        clock.now = scope['_delivery_attention_until'] + .001
        assert scope['ui_state']() == 'idle', 'Transient indicator never expired'
        scope['_state'] = 'recording'
        assert scope['ui_state']() == 'recording'
    else:
        assert not error_states and scope['ui_state']() == 'idle'
    return scope


exercise(RuntimeError('synthetic failure'))
speech = exercise(LookupError('synthetic speech failure'))
assert 'Retry startup' in speech['_last_error']
retained = exercise(RuntimeError('synthetic failure'), attention_until=108.0)
assert retained['_delivery_attention_until'] == 108.0, 'Existing attention window shortened'
exercise(None)
exercise(RuntimeError('synthetic failure'), quit_after_failure=True)
print('Worker recovery: queued work starts without display delay; errors, speech failure, '
      'priority, expiry and task accounting preserved')
