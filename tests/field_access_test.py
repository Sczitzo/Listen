"""Pure field ownership + real framed scripted child; never native UIA or field reads."""
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import field_access as f
from field_access_worker import (FieldRegistry, Refused, Snapshot, MAX_UNITS,
                                 MAX_READ, UIAProvider, _intended)
from speech_process import _write_frame


def refused(reason, function, *args):
    try:
        function(*args)
    except Refused as error:
        assert error.reason == reason, (reason, error.reason)
    else:
        raise AssertionError('Expected conservative field refusal: ' + reason)


class Provider:
    def __init__(self):
        self.identity = 'field-a'
        self.value = Snapshot.create('before selected after', 'before ', 'selected', ' after')
        self.reads = self.captures = 0
        self.refusal = None

    def capture(self, target):
        self.captures += 1
        if self.refusal:
            raise Refused(self.refusal)
        return {'runtime': self.identity, 'target': dict(target)}

    def read(self, identity):
        self.reads += 1
        if self.identity != identity['runtime']:
            raise Refused('field_changed', 'stale')
        if self.refusal:
            raise Refused(self.refusal)
        return self.value


def registry_checks():
    clock = [100.]
    provider = Provider()
    registry = FieldRegistry(provider, 'a' * 32, clock=lambda: clock[0])
    target = dict(hwnd=10, pid=20, focus=11)
    capture = registry.capture(target)['token']
    assert provider.reads == 0, 'Capture read private field content unnecessarily'
    assert capture.startswith('a' * 32 + '.c.')
    token = registry.prepare(capture, 'line one\r\nline two \U0001f600')['token']
    assert token != capture and '.t.' in token
    assert registry.check(token) == {'value': True}
    registry.release(capture)
    assert registry.check(token) == {'value': True}, 'Capture release invalidated independent transaction'
    provider.value = Snapshot.create('before line one\nline two \U0001f600 after',
                                     'before line one\nline two \U0001f600', '', ' after')
    assert registry.verify(token) == {'value': True}
    reads = provider.reads
    provider.refusal = 'field_unavailable'
    assert registry.verify(token) == {'value': True} and provider.reads == reads
    registry.release(token)
    refused('stale_token', registry.verify, token)
    assert registry.release(token) == {}

    provider = Provider()
    registry = FieldRegistry(provider, 'b' * 32, clock=lambda: clock[0])
    capture = registry.capture(target)['token']
    # Owner edits made while recording belong in the final preflight snapshot.
    provider.value = Snapshot.create('owner changed caret', 'owner changed ', '', 'caret')
    token = registry.prepare(capture, 'inserted ')['token']
    assert registry.tokens[token]['expected'] == 'owner changed inserted caret'
    provider.value = Snapshot.create('owner changed caret', 'owner ', 'changed ', 'caret')
    assert registry.check(token) == {'value': False}, 'Selection change was ignored'
    provider.value = Snapshot.create('new owner edit caret', 'new owner edit ', '', 'caret')
    assert registry.check(token) == {'value': False}, 'Owner edit was ignored'
    assert registry.verify(token) == {'value': False}
    provider.identity = 'field-b'
    refused('field_changed', registry.check, token)
    provider.identity = 'field-a'
    assert registry.check(token) == {'value': False}
    refused('stale_token', registry.prepare, token, 'wrong token kind')
    refused('stale_token', registry.check, capture)
    refused('stale_token', registry.prepare, 'c' * 32 + capture[32:], 'different session')

    registry = FieldRegistry(Provider(), 'c' * 32, clock=lambda: clock[0])
    cap = registry.capture(target)['token']
    refused('unchanged', registry.prepare, cap, 'selected')
    clock[0] += 900
    refused('stale_token', registry.prepare, cap, 'expired')
    for _ in range(8):
        registry.capture(target)
    count = registry.provider.captures
    refused('token_limit', registry.capture, target)
    assert registry.provider.captures == count, 'Overflow read a new field'
    for invalid in ({}, dict(target, pid=True), dict(target, focus=0), None):
        invalid_registry = FieldRegistry(Provider(), 'd' * 32)
        refused('invalid_target', invalid_registry.capture, invalid)
    for reason in ('password', 'readonly', 'single_line', 'unsupported_field', 'invalid_selection'):
        provider = Provider();provider.refusal = reason
        refused(reason, FieldRegistry(provider, 'e' * 32).capture, target)
        assert provider.reads == 0
    # Newline normalization and supplementary Unicode never use Python caret offsets.
    snapshot = Snapshot.create('A\r\n\U0001f600\rB', 'A\r\n', '\U0001f600', '\rB')
    assert snapshot.document == 'A\n\U0001f600\nB'
    assert snapshot.prefix + snapshot.selected + snapshot.suffix == snapshot.document
    refused('invalid_selection', Snapshot.create, 'abc', 'a', 'b', 'wrong suffix')
    refused('field_too_large', Snapshot.create, 'a' * (MAX_UNITS + 1), '', '', '')
    refused('field_too_large', Snapshot.create, '\U0001f600' * (MAX_UNITS // 2 + 1), '', '', '')
    for bad in ('', 'a\rb', 'a\tb', 'a\0b', '\ud800', 'a' * 100001):
        refused('invalid_text', _intended, bad)
    assert _intended('a\r\nb\U0001f600') == 'a\nb\U0001f600'
    print('PASS RAM registry: exact ranges/Unicode, changed fields/selections, token ownership/expiry/limits, sticky proof and refusals')


def range_checks():
    calls = []
    class Range:
        def __init__(self, document, start, end):
            self.document, self.start, self.end = document, start, end
        def Clone(self):
            return Range(self.document, self.start, self.end)
        def CompareEndpoints(self, endpoint, other, other_endpoint):
            return getattr(self, endpoint) - getattr(other, other_endpoint)
        def MoveEndpointByRange(self, endpoint, other, other_endpoint):
            setattr(self, endpoint, getattr(other, other_endpoint))
        def GetText(self, limit):
            calls.append(limit)
            assert limit == MAX_READ, 'Provider attempted unbounded field read'
            return self.document[self.start:self.end]
    provider = UIAProvider.__new__(UIAProvider)
    provider.Endpoint = SimpleNamespace(Start='start', End='end')
    provider._native = lambda target: None
    element = SimpleNamespace(GetRuntimeId=lambda: [1, 2, 3])
    document = 'abc selected suffix'
    pattern = SimpleNamespace(DocumentRange=Range(document, 0, len(document)))
    selected = [Range(document, 4, 12)]
    provider._focused = lambda target: (element, pattern, selected[0])
    identity = dict(target={}, runtime=(1, 2, 3))
    snapshot = provider.read(identity)
    assert snapshot.prefix == 'abc ' and snapshot.selected == 'selected' and snapshot.suffix == ' suffix'
    assert calls == [MAX_READ] * 4
    for start, end in ((-1, 1), (1, len(document) + 1), (9, 3)):
        selected[0] = Range(document, start, end)
        before = len(calls)
        refused('invalid_selection', provider.read, identity)
        assert len(calls) == before, 'Out-of-document selection caused field text read'
    selected[0] = Range(document, 4, 4)
    caret = provider.read(identity)
    assert caret.selected == '' and caret.prefix + caret.suffix == document
    identity['runtime'] = (4, 5)
    before = len(calls)
    refused('field_changed', provider.read, identity)
    assert len(calls) == before
    print('PASS actual UIA read algorithm through fake ranges: all endpoints checked before bounded reads, caret and identity fences')


CHILD = r'''
import sys,time,uuid,json,struct
from field_access import _read_message
from speech_process import _write_frame
mode=MODE
while True:
    try:r=_read_message(sys.stdin.buffer)
    except Exception:break
    op=r['op'];session=r['session'];request_id=r['request_id']
    if op=='init':data={'ready':True}
    elif op=='capture':data={'token':session+'.c.'+uuid.uuid4().hex}
    elif op=='prepare':data={'token':session+'.t.'+uuid.uuid4().hex}
    elif op in ('check','verify'):data={'value':True}
    else:data={}
    response=dict(kind='response',session=session,request_id=request_id,status='ok',reason='',data=data)
    if op=='init' and mode=='slow_init':time.sleep(.45)
    if op=='capture' and mode=='disconnect':break
    if op=='capture' and mode=='hang':time.sleep(10)
    if op=='capture' and mode=='stderr':sys.stderr.write('discarded synthetic canary'*10000);sys.stderr.flush()
    if op=='capture' and mode=='wrong_session':response['session']='0'*32
    if op=='capture' and mode=='wrong_id':response['request_id']+=1
    if op=='capture' and mode=='extra_key':response['private_field']='synthetic canary'
    if op=='capture' and mode=='bad_reason':response.update(status='unsupported',reason='arbitrary provider text')
    if op=='capture' and mode=='bad_token':response['data']['token']='not-a-session-token'
    if op=='capture' and mode=='audio':response['audio_bytes']=0
    if op=='capture' and mode=='large':
        sys.stdout.buffer.write(struct.pack('<I',4*1024*1024+1));sys.stdout.buffer.flush();time.sleep(10)
    elif op=='capture' and mode=='malformed':
        sys.stdout.buffer.write(struct.pack('<I',3)+b'bad');sys.stdout.buffer.flush();time.sleep(10)
    else:
        _write_frame(sys.stdout.buffer,response)
        if op=='capture' and mode=='duplicate':_write_frame(sys.stdout.buffer,response)
    if op=='close':break
'''


class Job:
    def __init__(self, process):
        self.process, self.closed = process, False
    def close(self):
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()


def runtime(mode='good', *, clock=time.monotonic, warmup_allowed=None):
    processes, jobs, launches = [], [], []
    def factory(command, **kwargs):
        launches.append(kwargs['env'])
        process = subprocess.Popen([sys.executable, '-u', '-c', CHILD.replace('MODE', repr(mode))], **kwargs)
        processes.append(process)
        return process
    def job_factory(process):
        job = Job(process);jobs.append(job);return job
    access = f.FieldAccess(process_factory=factory, job_factory=job_factory, clock=clock,
                           warmup_allowed=warmup_allowed)
    return access, processes, jobs, launches


def wait(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Owned helper did not exit'
        time.sleep(.005)


def protocol_checks():
    target = SimpleNamespace(hwnd=10, pid=20, focus=11)
    access, processes, jobs, launches = runtime('stderr')
    try:
        assert access.start() and access.ready
        cap = access.capture(target)
        assert isinstance(cap, str)
        token = access.prepare(cap, 'synthetic first\nsecond \U0001f600')
        assert token and token != cap
        access.release(cap)
        assert access.check(token) and access.verify(token)
        # Parent proof is sticky even when the child is killed after insertion.
        access._stop(access._owned)
        wait(lambda: not access.alive)
        assert access.verify(token)
        access.release(token)
        assert not access.verify(token)
        assert not access.prepare(cap, 'not allowed')
        assert not access.start(), 'Failure backoff restarted immediately'
        assert len(processes) == 1
        assert all(not key.endswith('API_KEY') for key in launches[0])
    finally:
        assert access.close() and access.close()
    assert processes[0].poll() is not None and jobs[0].closed
    print('PASS scripted pipes: session tokens, one owned lifecycle/writer, stderr drain, sticky parent proof, credentials and backoff')

    access, processes, _, _ = runtime()
    try:
        assert access.start()
        cap = access.capture(target)
        old_session = access._owned.nonce
        assert access.close()
        # Aborted Quit can lazily warm after a confirmed normal close. Neither
        # capture nor transaction tokens migrate to that replacement session.
        assert access.capture(target) is None
        wait(lambda: access.ready)
        cap2 = access.capture(target)
        assert cap2 and cap2 != cap and access._owned.nonce != old_session
        assert access.prepare(cap, 'old session') is None
        assert len(processes) == 2
        metadata = access.metadata()
        assert metadata['state'] == 'ready' and metadata['capture_tokens'] == 1
        assert cap2 not in str(metadata) and 'old session' not in str(metadata)
    finally:
        assert access.close()
    assert all(process.poll() is not None for process in processes)

    # Failure backoff expires once, then an explicit start may replace only the
    # fully reaped process; old unverified tokens cannot migrate.
    offset = [0.]
    access, processes, _, _ = runtime(clock=lambda: time.monotonic() + offset[0])
    try:
        assert access.start()
        cap = access.capture(target)
        access._stop(access._owned)
        wait(lambda: not access.alive)
        assert not access.start()
        offset[0] += 31
        assert access.start() and len(processes) == 2
        assert access.prepare(cap, 'stale capture') is None
        assert access.capture(target)
    finally:
        assert access.close()

    # Invalid native targets never start or contact the helper.
    access, processes, _, _ = runtime()
    try:
        for target_bad in (None, SimpleNamespace(hwnd=1, pid=2, focus=0), SimpleNamespace(hwnd=True, pid=2, focus=3)):
            assert access.capture(target_bad) is None
        assert not processes and not access.check('unknown') and not access.verify('unknown')
        assert access.prepare('unknown', 'text') is None and not processes
    finally:
        assert access.close()

    for mode in ('wrong_session', 'wrong_id', 'extra_key', 'bad_reason', 'bad_token', 'audio', 'large', 'malformed', 'hang', 'duplicate', 'disconnect'):
        access, processes, _, _ = runtime(mode)
        try:
            assert access.start()
            started = time.monotonic()
            cap = access.capture(target, timeout=.08)
            assert time.monotonic() - started < .3, (mode, 'RPC ignored shared deadline')
            if mode != 'duplicate':
                assert cap is None, mode
            wait(lambda: not access.alive)
            assert not access.ready and not access.start(), mode
            expected_stage = 'rpc_timeout' if mode == 'hang' else 'disconnected' if mode == 'disconnect' else 'protocol'
            assert access.metadata()['last_failure_stage'] == expected_stage, (mode, access.metadata())
            assert access.metadata()['failure_count'] == 1
            assert len(processes) == 1 and processes[0].poll() is not None
            assert access.prepare(cap, 'no restart') is None
        finally:
            assert access.close()
    print('PASS malformed/oversize/audio/duplicate/stale responses and provider deadlines kill and reap without callback restart')

    # Writer/lifecycle launch ownership persists past an intentionally blocked
    # process factory; close reports unfinished instead of duplicating a helper.
    entered, unblock = threading.Event(), threading.Event()
    launches = []
    def delayed(command, **kwargs):
        entered.set();assert unblock.wait(2)
        process = subprocess.Popen([sys.executable, '-u', '-c', CHILD.replace('MODE', repr('good'))], **kwargs)
        launches.append(process);return process
    access = f.FieldAccess(process_factory=delayed, job_factory=Job)
    try:
        started=time.monotonic()
        assert not access.start(timeout=.04) and entered.is_set()
        assert time.monotonic()-started < .2
        assert not access.close(timeout=.02)
        assert not access.start(timeout=.02)
        unblock.set()
        wait(lambda:not access.alive)
        assert access.close() and len(launches)==1 and launches[0].poll() is not None
    finally:
        unblock.set();assert access.close()

    # Lock contention consumes the same budget; no child launch behind callers.
    access, processes, _, _ = runtime()
    access._operation.acquire()
    try:
        started=time.monotonic()
        assert not access.start(timeout=.02)
        assert time.monotonic()-started < .15 and not processes
    finally:
        access._operation.release();assert access.close()
    print('PASS bounded launch/close/lock contention; no late orphan or replacement before confirmed exit')


def warmup_checks():
    target = SimpleNamespace(hwnd=10, pid=20, focus=11)
    # The cold startup is longer than capture's entire budget. A cold capture
    # returns None promptly and warms only; it never captures that old intent
    # later, even once the helper becomes ready.
    access, processes, _, _ = runtime('slow_init')
    try:
        with patch.object(access, '_call', wraps=access._call) as calls:
            started = time.monotonic()
            assert access.capture(target) is None
            assert time.monotonic() - started < .15, 'Cold capture spent its 250ms budget on startup'
            for _ in range(30):
                assert access.request_warmup()
            assert access.metadata()['warming']
            assert len([t for t in threading.enumerate() if t.name == 'field-warmup']) == 1
            wait(lambda: access.ready)
            assert len(processes) == 1 and not access.metadata()['backoff']
            assert not [call for call in calls.call_args_list if call.args[1] == 'capture'], 'Warmup captured a later/retargeted field'
            cap = access.capture(target)
            assert cap and len([call for call in calls.call_args_list if call.args[1] == 'capture']) == 1
            assert access.metadata()['failure_count'] == 0
    finally:
        assert access.close()
    assert all(process.poll() is not None for process in processes)

    # A genuine 250ms cold start still fails; this reproduces why tying warmup
    # to that old budget cannot recover a cold CLR provider.
    access, processes, _, _ = runtime('slow_init')
    try:
        assert not access.start(timeout=.25)
        wait(lambda: not access.alive)
        assert access.metadata()['last_failure_stage'] == 'startup_timeout'
        assert access.metadata()['failure_count'] == 1 and access.metadata()['backoff']
        assert not access.request_warmup() and access.capture(target) is None
        assert not access.prepare('missing', 'text') and not access.check('missing') and not access.verify('missing')
        access.release('missing')
        assert len(processes) == 1
    finally:
        assert access.close()

    allowed = [False]
    access, processes, _, _ = runtime(warmup_allowed=lambda: allowed[0])
    try:
        assert not access.request_warmup() and not access.start() and access.capture(target) is None
        assert not processes and not access.alive
        allowed[0] = True
        assert access.request_warmup()
        wait(lambda: access.ready)
        assert len(processes) == 1
    finally:
        assert access.close()

    # Close while a warmer is waiting for the operation lock must cancel that
    # tracked thread without allowing a delayed launch after close returns.
    access, processes, _, _ = runtime()
    access._operation.acquire()
    try:
        assert access.request_warmup()
        started = time.monotonic()
        assert access.close(timeout=.3)
        assert time.monotonic() - started < .3
        assert not processes and not access.alive
    finally:
        access._operation.release()
        assert access.close()

    # Force close after the warm request but before entering real start. The
    # epoch check must live inside start's lock boundary, not only the caller.
    access, processes, _, _ = runtime()
    entered, resume, closed = threading.Event(), threading.Event(), threading.Event()
    errors = []
    real_start = access.start
    def gated_start(*args, **kwargs):
        entered.set()
        assert resume.wait(2)
        return real_start(*args, **kwargs)
    def close():
        try:
            assert access.close(timeout=1)
        except BaseException as error:
            errors.append(error)
        finally:
            closed.set()
    with patch.object(access, 'start', gated_start):
        assert access.request_warmup() and entered.wait(1)
        closer = threading.Thread(target=close, name='test-field-close')
        closer.start()
        wait(lambda: access._closing > 0)
        resume.set()
        assert closed.wait(1)
        closer.join(1)
        assert not errors and not processes and not access.alive
    assert access.close()

    # If launch itself is already in progress, close owns both warmer and
    # lifecycle on one shared deadline. It cannot claim exit or replace that
    # child before the delayed factory returns and the owner reaps it.
    entered, resume = threading.Event(), threading.Event()
    processes = []
    def delayed_factory(command, **kwargs):
        entered.set()
        assert resume.wait(2)
        process = subprocess.Popen([sys.executable, '-u', '-c', CHILD.replace('MODE', repr('good'))], **kwargs)
        processes.append(process)
        return process
    access = f.FieldAccess(process_factory=delayed_factory, job_factory=Job)
    try:
        assert access.request_warmup() and entered.wait(1)
        started = time.monotonic()
        assert not access.close(timeout=.04)
        assert time.monotonic() - started < .15, 'Close gave warmer and child separate budgets'
        assert not access.request_warmup() and not access.start(timeout=.02)
        resume.set()
        wait(lambda: not access.alive)
        assert access.close() and len(processes) == 1 and processes[0].poll() is not None
    finally:
        resume.set()
        assert access.close()

    # A pending clipboard recovery that appears after enqueue but before start
    # admission blocks the launch too.
    allowed = [True]
    access, processes, _, _ = runtime(warmup_allowed=lambda: allowed[0])
    access._operation.acquire()
    try:
        assert access.request_warmup()
        allowed[0] = False
    finally:
        access._operation.release()
    wait(lambda: not access.alive)
    assert not processes and access.close()

    # Thread creation failures must back off; repeated idle ticks cannot create
    # an unbounded series of failed warmers.
    access, processes, _, _ = runtime()
    try:
        with patch.object(f.threading.Thread, 'start', side_effect=RuntimeError('synthetic thread unavailable')) as start:
            assert not access.request_warmup()
            assert not access.request_warmup() and access.capture(target) is None
            assert start.call_count == 1
        assert not processes and not access.alive
        assert access.metadata()['last_failure_stage'] == 'thread_start'
        assert access.metadata()['failure_count'] == 1 and access.metadata()['backoff']
        assert not any('synthetic' in str(value) for value in access.metadata().values())
    finally:
        assert access.close()
    print('PASS asynchronous warmup: cold >250ms recovers under 2s, one warmer, no late capture, recovery gate, close epochs and failure backoff')


def framing_checks():
    for windowed, existing, expected in (
        ('pythonw.exe', {'python.exe'}, 'python.exe'),
        ('pythonw3.13.exe', {'python3.13.exe'}, 'python3.13.exe'),
        ('pythonw3.13.exe', {'python.exe'}, 'python.exe'),
        ('pythonw3.13.exe', set(), 'pythonw3.13.exe'),
        ('python3.13.exe', {'python.exe'}, 'python3.13.exe'),
    ):
        with patch.object(f.Path, 'is_file', lambda path: path.name in existing):
            assert Path(f._worker_executable(Path('/installed') / windowed)).name == expected
    value = {'op': 'close', 'session': 'a' * 32, 'request_id': 1}
    stream = io.BytesIO();_write_frame(stream, value);stream.seek(0)
    assert f._read_message(stream) == value
    malformed = [b'[]', b'{"audio_bytes":0}', b'{"kind":1,"kind":2}', b'{"value":NaN}']
    for raw in [struct.pack('<I', 0), struct.pack('<I', 4*1024*1024+1)] + [
            struct.pack('<I', len(payload)) + payload for payload in malformed]:
        try:
            f._read_message(io.BytesIO(raw))
        except (EOFError, ValueError):
            pass
        else:
            raise AssertionError('Malformed field frame admitted')
    print('PASS bounded JSON-only framing; no audio payload or arbitrary response content')


try:
    registry_checks()
    range_checks()
    framing_checks()
    protocol_checks()
    warmup_checks()
finally:
    assert not [thread.name for thread in threading.enumerate() if thread.name.startswith('field-')], 'Field helper threads leaked'
