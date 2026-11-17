"""Pure synthetic draft scheduling and admission checks; no models or sockets."""
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from draft_preview import Cancellation, DraftCoordinator, DraftJob, LocalModelGate


def wait(predicate, timeout=2):
    end = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < end, 'Synthetic worker did not settle'
        time.sleep(.005)


def job(revision=1, text='hello world', system='Clean this.'):
    return DraftJob(1, 'dictate', revision, 'settings-1', system, text, 'qwen2.5:7b', ())


gate = LocalModelGate()
token = Cancellation()
lease = gate.try_draft(token)
assert lease and gate.try_auxiliary() is None
authority = gate.reserve_authoritative()
assert token.cancelled and not gate.wait_draft(.01)
assert gate.try_draft(Cancellation()) is None
lease.release(); lease.release()
assert gate.wait_draft(.01) and gate.try_auxiliary() is None
authority.release(); authority.release()
aux = gate.try_auxiliary()
assert aux and gate.try_auxiliary() is None and gate.try_draft(Cancellation()) is None
aux.release()
warm = gate.try_cleanup_warm()
assert warm and gate.try_cleanup_warm() is None and gate.try_draft(Cancellation()) is None
aux = gate.try_auxiliary()
assert aux, 'Speech preview can run during cleanup warm-up'
aux.release()
assert gate.try_draft(Cancellation()) is None
warm.release(); warm.release()
lease = gate.try_draft(Cancellation())
assert lease
lease.release()

started, unblock = threading.Event(), threading.Event()
requests, events = [], []
def request(payload, *, timeout, cancel):
    requests.append(payload)
    if len(requests) == 1:
        started.set()
        assert unblock.wait(2)
        assert not cancel.cancelled, 'A newer revision cancelled the running request'
    return {'done': True, 'done_reason': 'stop',
            'message': {'content': '"Hello world."'}}

coordinator = DraftCoordinator(lambda *args: events.append(args), lambda j: True,
                               request=request, interval=.01)
assert coordinator.offer(job()) and started.wait(1)
assert requests[0]['options']['num_batch'] == 128
for revision in range(2, 20):
    coordinator.offer(job(revision))
unblock.set()
wait(lambda: len([e for e in events if e[2] == 'ready']) == 2)
assert [e[0].revision for e in events if e[2] == 'ready'] == [1, 19]
assert len(requests) == 2 and requests[0]['options'] == {
    'num_ctx': 8192, 'num_predict': 4000, 'temperature': 0, 'num_batch': 128}
assert coordinator.close() and not coordinator.alive
assert not coordinator.offer(job())

# The byte limit covers the full supplied prompt, including token-dense Unicode.
events, calls = [], []
coordinator = DraftCoordinator(lambda *args: events.append(args), lambda j: True,
    request=lambda *a, **k: calls.append(a), interval=0)
coordinator.offer(job(text='界' * 1, system='x' * 3000))
wait(lambda: bool(events))
assert events[-1][2:] == ('partial', 'length') and not calls
assert coordinator.close()

# A reserved authoritative operation prevents even a synthetic draft request.
gate = LocalModelGate()
authority = gate.reserve_authoritative()
events, calls = [], []
coordinator = DraftCoordinator(lambda *a: events.append(a), lambda j: True, gate=gate,
    request=lambda *a, **k: calls.append(a), interval=0)
coordinator.offer(job()); wait(lambda: bool(events))
assert events[-1][2:] == ('paused', 'busy') and not calls
assert coordinator.close()
authority.release()

# Settings/session validation is checked again after a delayed model response.
started, unblock = threading.Event(), threading.Event()
valid, events = [True], []
def stale_request(*args, **kwargs):
    started.set()
    assert unblock.wait(2)
    return {'done': True, 'done_reason': 'stop', 'message': {'content': 'Hello world.'}}
coordinator = DraftCoordinator(lambda *a: events.append(a), lambda j: valid[0],
                               request=stale_request, interval=0)
coordinator.offer(job()); assert started.wait(1)
valid[0] = False
unblock.set(); wait(lambda: not coordinator.busy)
assert not [e for e in events if e[2] == 'ready']
assert coordinator.close()

# An uncooperative injected request remains owned until it actually returns.
started, unblock = threading.Event(), threading.Event()
def stuck(*args, **kwargs):
    started.set(); unblock.wait(2)
    return {'done': True, 'done_reason': 'stop', 'message': {'content': 'Hello world.'}}
events = []
coordinator = DraftCoordinator(lambda *a: events.append(a), lambda j: True, request=stuck)
coordinator.offer(job()); assert started.wait(1)
assert not coordinator.close(.01) and coordinator.alive and coordinator.busy
unblock.set(); assert coordinator.close(1)
assert not [e for e in events if e[2] == 'ready']

# Incomplete and changed-content replies cannot become a ready draft.
for reply, reason in [
    ({'done': True, 'done_reason': 'stop', 'message': {'content': 'Goodbye world.'}}, 'unsafe'),
    ({'done': True, 'done_reason': 'length', 'message': {'content': 'Hello world.'}}, 'unavailable')]:
    events = []
    coordinator = DraftCoordinator(lambda *a: events.append(a), lambda j: True,
                                   request=lambda *a, **k: reply, interval=0)
    coordinator.offer(job()); wait(lambda: any(e[2] == 'partial' for e in events))
    assert events[-1][2:] == ('partial', reason)
    assert coordinator.close()

# A failed whole draft switches to bounded work; failed identities do not retry.
calls, events = [], []
def failed_request(*args, **kwargs):
    calls.append(1)
    raise TimeoutError()
coordinator = DraftCoordinator(lambda *a: events.append(a), lambda j: True,
                               request=failed_request, interval=0)
coordinator.offer(job()); wait(lambda: any(e[2] == 'partial' for e in events))
coordinator.offer(job(2))
wait(lambda: not coordinator.busy)
assert len(calls) == 2 and not coordinator.needs_work(job(2))
assert coordinator.close()

print('Draft preview: latest-only, capacity, shared gate, stale settings, validation and owned shutdown passed')

# Completion may be cancelled while preservation validation is still running.
# External validity deliberately stays True to exercise the internal boundary.
for action in ('retire', 'close', 'authoritative'):
    validating, unblock = threading.Event(), threading.Event()
    events = []
    gate = LocalModelGate()
    def blocked_validation(*args):
        validating.set()
        assert unblock.wait(2)
        return True
    def complete_request(*args, **kwargs):
        return {'done': True, 'done_reason': 'stop', 'message': {'content': 'Hello world.'}}
    coordinator = DraftCoordinator(lambda *a: events.append(a), lambda j: True,
                                   gate=gate, request=complete_request, interval=0)
    authority = None
    with patch('draft_preview.cleanup_is_safe', blocked_validation):
        coordinator.offer(job())
        assert validating.wait(1)
        if action == 'close':
            assert not coordinator.close(.01)
        elif action == 'retire':
            coordinator.retire()
        else:
            authority = gate.reserve_authoritative()
        unblock.set()
        wait(lambda: not coordinator.busy)
    assert not [e for e in events if e[2] == 'ready'], action
    assert coordinator.close()
    if authority is not None:
        authority.release()

# Reentrant cancellation from a publication callback must not deadlock.
events = []
def reentrant_publish(*args):
    events.append(args)
    coordinator.retire()
coordinator = DraftCoordinator(reentrant_publish, lambda j: True,
                               request=complete_request, interval=0)
coordinator.offer(job())
wait(lambda: not coordinator.busy)
assert coordinator.close()
assert not [e for e in events if e[2] == 'ready']
print('Draft publication: retire, close, authoritative cancellation and reentrant callbacks passed')
