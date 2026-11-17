"""Bounded, read-only field receipts in one owned process; no field text retained here."""
import atexit
import json
import math
import os
from pathlib import Path
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import uuid

from speech_process import (_read_exact, _write_frame, _WindowsJob,
                            _child_environment, MAX_JSON)

WORKER = Path(__file__).with_name('field_access_worker.py')
default_process_factory = subprocess.Popen
MAX_TOKENS = 8
CAPTURE_LIFETIME = 900.
REASONS = frozenset(('', 'unavailable', 'invalid_request', 'invalid_target',
    'field_unavailable', 'unsupported_field', 'password', 'readonly', 'single_line',
    'invalid_selection', 'field_too_large', 'token_limit', 'stale_token',
    'field_changed', 'invalid_text', 'unchanged', 'provider_failed', 'closed'))
OPS = {
    'init': {'parent_pid'}, 'capture': {'target'}, 'prepare': {'token', 'text'},
    'check': {'token'}, 'verify': {'token'}, 'release': {'token'}, 'close': set(),
}


def _worker_executable(executable=None):
    path = Path(sys.executable if executable is None else executable)
    match = re.fullmatch(r'pythonw(?P<version>\d+(?:\.\d+)*)?\.exe', path.name, re.IGNORECASE)
    if match:
        names = ['python' + (match.group('version') or '') + '.exe', 'python.exe']
        for name in dict.fromkeys(names):
            candidate = path.with_name(name)
            if candidate.is_file():
                return str(candidate)
    return str(path)


def _read_message(stream):
    """Speech's framing, restricted to JSON only: never allocate an audio body."""
    size, = struct.unpack('<I', _read_exact(stream, 4))
    if not 0 < size <= MAX_JSON:
        raise ValueError('Invalid field frame')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate field protocol key')
            result[key] = value
        return result
    def invalid_constant(value):
        raise ValueError('Invalid field protocol constant')
    value = json.loads(_read_exact(stream, size).decode('utf-8'),
                       object_pairs_hook=unique, parse_constant=invalid_constant)
    if not isinstance(value, dict) or 'audio_bytes' in value:
        raise ValueError('Invalid field frame')
    return value


def _target(target):
    try:
        values = {name: getattr(target, name, None) for name in ('hwnd', 'pid', 'focus')}
    except Exception:
        return None
    if any(type(value) is not int or value <= 0 for value in values.values()):
        return None
    return values


def _budget(timeout, clock):
    if type(timeout) not in (float, int) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('Field timeout must be positive and finite')
    return clock() + timeout


class _Owned:
    def __init__(self):
        self.nonce = uuid.uuid4().hex
        self.process = self.job = self.reader = self.drain = self.owner = None
        self.launched = threading.Event()
        self.stop = threading.Event()
        self.done = threading.Event()
        self.outgoing = queue.Queue(maxsize=1)
        self.events = queue.Queue(maxsize=1)
        self.lock = threading.Lock()
        self.expected = None
        self.received = self.fault = self.ready = self.failed = False


class FieldAccess:
    """Only start launches. Delivery callbacks use the already warmed session."""
    def __init__(self, *, process_factory=None, job_factory=None, worker_path=None,
                 clock=time.monotonic, warmup_allowed=None):
        self._factory = process_factory or default_process_factory
        self._job_factory = job_factory or _WindowsJob
        self._worker = Path(worker_path) if worker_path is not None else WORKER
        self._clock = clock
        self._warmup_allowed = warmup_allowed if warmup_allowed is not None else lambda: True
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._owned = None
        self._tokens = {}
        self._serial = 0
        self._backoff = 0.
        self._closing = 0
        self._epoch = 0
        self._warm_thread = None
        self._last_failure_stage = ''
        self._failure_count = 0
        atexit.register(self.close)

    @property
    def ready(self):
        with self._lock:
            return bool(not self._closing and self._owned and self._owned.ready and not self._owned.stop.is_set())

    def metadata(self):
        with self._lock:
            owned = self._owned
            live = bool(owned and owned.owner and owned.owner.is_alive())
            warming = bool((self._warm_thread and self._warm_thread.is_alive()) or
                           (live and not owned.ready and not owned.stop.is_set()))
            state = ('stopping' if self._closing or (live and owned.stop.is_set()) else
                     'ready' if self.ready else 'warming' if warming else 'offline')
            return {'state': state, 'warming': warming,
                    'capture_tokens': sum(value['kind'] == 'c' for value in self._tokens.values()),
                    'transaction_tokens': sum(value['kind'] == 't' for value in self._tokens.values()),
                    'verified_tokens': sum(value['verified'] for value in self._tokens.values()),
                    'backoff': self._clock() < self._backoff,
                    'last_failure_stage': self._last_failure_stage,
                    'failure_count': self._failure_count}

    @property
    def alive(self):
        with self._lock:
            owned = self._owned
            warmer = self._warm_thread
        return bool((owned and owned.owner and owned.owner.is_alive()) or
                    (warmer and warmer.is_alive()))

    def _allowed(self):
        try:
            return bool(self._warmup_allowed())
        except Exception:
            return False

    def _failure(self, stage, owned=None):
        # Only the first observable failure of a session is counted. Cleanup
        # errors/EOF following our own kill cannot replace its original stage.
        with self._lock:
            if owned is not None:
                if owned.failed:
                    return
                owned.failed = True
            self._failure_count += 1
            self._last_failure_stage = stage
            self._backoff = max(self._backoff, self._clock() + 30.)

    def _invalidate(self, owned):
        with self._lock:
            owned.ready = False
            self._tokens = {key: value for key, value in self._tokens.items()
                            if value['verified'] or value['session'] != owned.nonce}

    def _stop(self, owned, failure=True, stage='stopped'):
        if owned is None:
            return
        if failure:
            self._failure(stage, owned)
        owned.stop.set()
        self._invalidate(owned)
        # Closing the job/terminating is nonblocking; the one lifecycle owner
        # reaps and joins. RPC deadlines do not gain a second cleanup budget.
        try:
            if owned.job is not None:
                owned.job.close()
        except Exception:
            pass
        try:
            if owned.process is not None and owned.process.poll() is None:
                owned.process.terminate()
        except OSError:
            pass

    def _fault(self, owned, stage='protocol'):
        with owned.lock:
            owned.fault = True
        self._stop(owned, stage=stage)
        try:
            owned.events.put_nowait(None)
        except queue.Full:
            pass

    def _read(self, owned):
        try:
            while not owned.stop.is_set():
                response = _read_message(owned.process.stdout)
                with owned.lock:
                    expected = owned.expected
                    if (owned.received or expected is None or
                            response.get('request_id') != expected or
                            response.get('session') != owned.nonce):
                        raise ValueError('Unmatched field response')
                    owned.received = True
                owned.events.put_nowait(response)
        except EOFError:
            if not owned.stop.is_set():
                self._fault(owned, 'disconnected')
        except Exception:
            if not owned.stop.is_set():
                self._fault(owned)

    @staticmethod
    def _drain(owned):
        try:
            while owned.process.stderr.read(8192):
                pass
        except (OSError, ValueError):
            pass

    def _own(self, owned):
        process = None
        stage = 'launch'
        try:
            with self._lock:
                if owned.stop.is_set() or not self._allowed():
                    owned.stop.set()
                    return
            process = self._factory([_worker_executable(), str(self._worker)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(self._worker.parent), env=_child_environment(), bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            owned.process = process
            if owned.stop.is_set():
                return
            stage = 'ownership'
            owned.job = self._job_factory(process)
            if owned.stop.is_set():
                return
            owned.reader = threading.Thread(target=self._read, args=(owned,), daemon=True,
                                            name='field-response-reader')
            owned.drain = threading.Thread(target=self._drain, args=(owned,), daemon=True,
                                           name='field-error-drain')
            stage = 'thread_start'
            owned.reader.start()
            owned.drain.start()
            owned.launched.set()
            stage = 'writer'
            while not owned.stop.is_set():
                try:
                    message = owned.outgoing.get(timeout=.025)
                except queue.Empty:
                    continue
                _write_frame(process.stdin, message)
        except Exception:
            self._fault(owned, stage)
        finally:
            owned.stop.set()
            owned.launched.set()
            self._invalidate(owned)
            exited = process is None
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                    exited = process.poll() is not None
                except (OSError, subprocess.TimeoutExpired):
                    exited = False
                if exited:
                    if owned.job is not None:
                        owned.job.close()
                    for stream in (process.stdin, process.stdout, process.stderr):
                        try:
                            stream.close()
                        except (OSError, ValueError):
                            pass
            for thread in (owned.reader, owned.drain):
                if thread is not None and thread.ident is not None:
                    thread.join(.25)
            while True:
                try:
                    owned.outgoing.get_nowait()
                except queue.Empty:
                    break
            if exited and all(thread is None or not thread.is_alive()
                              for thread in (owned.reader, owned.drain)):
                owned.done.set()

    def _admitted_epoch(self, epoch):
        with self._lock:
            return not self._closing and epoch == self._epoch

    def _acquire(self, deadline, expected_epoch=None):
        if expected_epoch is None:
            return self._operation.acquire(timeout=max(0., deadline - self._clock()))
        while self._admitted_epoch(expected_epoch):
            remaining = deadline - self._clock()
            if remaining <= 0:
                return False
            if self._operation.acquire(timeout=min(.01, remaining)):
                return True
        return False

    def _call(self, owned, op, deadline, **inputs):
        if owned is None or owned.stop.is_set() or deadline <= self._clock():
            return None
        with owned.lock:
            invalid = not owned.events.empty() or owned.expected is not None
        if invalid:
            self._fault(owned)
            return None
        with owned.lock:
            self._serial += 1
            request_id = self._serial
            owned.expected, owned.received = request_id, False
        message = dict(op=op, session=owned.nonce, request_id=request_id, **inputs)
        failure_stage = 'startup_timeout' if op == 'init' else 'rpc_timeout'
        try:
            owned.outgoing.put_nowait(message)
            while not owned.stop.is_set():
                remaining = deadline - self._clock()
                if remaining <= 0:
                    break
                try:
                    response = owned.events.get(timeout=min(.01, remaining))
                except queue.Empty:
                    continue
                if response is None or owned.fault:
                    break
                failure_stage = 'protocol'
                if (set(response) != {'kind', 'session', 'request_id', 'status', 'reason', 'data'}
                        or response['kind'] != 'response' or response['session'] != owned.nonce
                        or type(response['request_id']) is not int or response['request_id'] != request_id
                        or response['status'] not in ('ok', 'unsupported', 'stale', 'error')
                        or response['reason'] not in REASONS or not isinstance(response['data'], dict)
                        or (response['status'] == 'ok') != (response['reason'] == '')):
                    raise ValueError('Invalid field response')
                status, data = response['status'], response['data']
                if status != 'ok':
                    if data or status == 'error':
                        if not data and response['reason'] in ('provider_failed', 'unavailable'):
                            failure_stage = 'provider_error'
                        raise ValueError('Field helper failure')
                    return None
                if op in ('capture', 'prepare'):
                    kind = 'c' if op == 'capture' else 't'
                    token = data.get('token')
                    if set(data) != {'token'} or not isinstance(token, str) or not re.fullmatch(
                            re.escape(owned.nonce) + r'\.' + kind + r'\.[0-9a-f]{32}', token):
                        raise ValueError('Invalid field token')
                elif op in ('check', 'verify'):
                    if set(data) != {'value'} or type(data['value']) is not bool:
                        raise ValueError('Invalid field acknowledgment')
                elif op == 'init':
                    if set(data) != {'ready'} or type(data['ready']) is not bool or not data['ready']:
                        raise ValueError('Invalid field readiness')
                elif data:
                    raise ValueError('Invalid field response data')
                return data
        except Exception:
            pass
        finally:
            with owned.lock:
                owned.expected = None
        self._stop(owned, stage=failure_stage)
        return None

    def start(self, timeout=2, *, expected_epoch=None):
        deadline = _budget(timeout, self._clock)
        with self._lock:
            if expected_epoch is None:
                expected_epoch = self._epoch
        if not self._acquire(deadline, expected_epoch):
            return False
        try:
            with self._lock:
                if self._closing or expected_epoch != self._epoch or not self._allowed():
                    return False
                owned = self._owned
                if owned and owned.ready and not owned.stop.is_set():
                    return True
                if (owned and (not owned.done.is_set() or owned.owner.is_alive())) or self._clock() < self._backoff:
                    return False
                owned = _Owned()
                self._owned = owned
                owned.owner = threading.Thread(target=self._own, args=(owned,), daemon=True,
                                               name='field-lifecycle-writer')
                try:
                    owned.owner.start()
                except Exception:
                    owned.done.set()
                    self._stop(owned, stage='thread_start')
                    return False
            while not owned.launched.is_set():
                if not self._admitted_epoch(expected_epoch) or owned.stop.is_set():
                    self._stop(owned, failure=False)
                    return False
                remaining = deadline - self._clock()
                if remaining <= 0:
                    self._stop(owned, stage='startup_timeout')
                    return False
                owned.launched.wait(min(.01, remaining))
            result = self._call(owned, 'init', deadline, parent_pid=os.getpid())
            with self._lock:
                admitted = not self._closing and expected_epoch == self._epoch
                if result is not None and admitted and not owned.stop.is_set():
                    owned.ready = True
                    return True
            self._stop(owned, failure=admitted and not owned.stop.is_set(), stage='startup_timeout')
            return False
        finally:
            self._operation.release()

    def request_warmup(self):
        """Queue one 2s warmup without capturing any field or delaying recording."""
        with self._lock:
            if self._closing or not self._allowed():
                return False
            if self.ready:
                return True
            if self._warm_thread is not None and self._warm_thread.is_alive():
                return True
            owned = self._owned
            if owned is not None and (not owned.done.is_set() or owned.owner.is_alive()):
                return bool(not owned.stop.is_set())  # Existing explicit startup.
            if self._clock() < self._backoff:
                return False
            epoch = self._epoch
            def warm():
                self.start(timeout=2, expected_epoch=epoch)
            thread = threading.Thread(target=warm, daemon=True, name='field-warmup')
            self._warm_thread = thread
            try:
                thread.start()
            except Exception:
                self._failure('thread_start')
                return False
            return True

    def _prune(self):
        now = self._clock()
        self._tokens = {token: entry for token, entry in self._tokens.items()
                        if entry['kind'] != 'c' or now < entry['expires']}

    def capture(self, target, timeout=.25):
        deadline = _budget(timeout, self._clock)
        values = _target(target)
        if values is None:
            return None
        if not self.ready:
            self.request_warmup()
            return None  # This recording never acquires a later/retargeted field.
        return self._token_call('capture', timeout, deadline=deadline, target=values)

    def prepare(self, capture_token, text, timeout=.35):
        if not isinstance(text, str):
            return None
        try:
            if len(text.encode('utf-16-le')) // 2 > 100000 or not text:
                return None
        except UnicodeError:
            return None
        return self._token_call('prepare', timeout, token=capture_token, text=text)

    def _token_call(self, op, timeout, *, deadline=None, **inputs):
        deadline = _budget(timeout, self._clock) if deadline is None else deadline
        if not self._acquire(deadline):
            return None
        try:
            with self._lock:
                self._prune()
                owned = self._owned
                if not self.ready or len(self._tokens) >= MAX_TOKENS:
                    return None
                if op == 'prepare':
                    entry = self._tokens.get(inputs['token']) if isinstance(inputs['token'], str) else None
                    if not entry or entry['kind'] != 'c' or entry['session'] != owned.nonce:
                        return None
            data = self._call(owned, op, deadline, **inputs)
            if data is None:
                return None
            token = data['token']
            with self._lock:
                if owned.stop.is_set():
                    return None
                if token in self._tokens:
                    self._stop(owned)
                    return None
                self._tokens[token] = dict(kind='c' if op == 'capture' else 't',
                    session=owned.nonce, expires=self._clock() + CAPTURE_LIFETIME, verified=False)
            return token
        finally:
            self._operation.release()

    def _boolean(self, op, token, timeout):
        deadline = _budget(timeout, self._clock)
        if not self._acquire(deadline):
            return False
        try:
            with self._lock:
                entry = self._tokens.get(token) if isinstance(token, str) else None
                if not entry or entry['kind'] != 't':
                    return False
                if op == 'verify' and entry['verified']:
                    return True
                owned = self._owned
                if not self.ready or entry['session'] != owned.nonce:
                    return False
                epoch = self._epoch
            data = self._call(owned, op, deadline, token=token)
            value = data is not None and data['value']
            if op == 'verify' and value:
                with self._lock:
                    if self._epoch != epoch:
                        return False
                    entry['verified'] = True
                    # A completed true response can race EOF/session retirement.
                    # Preserve that proof even if retirement removed this entry
                    # between receiving the response and taking this lock.
                    self._tokens[token] = entry
            elif value:
                with self._lock:
                    if self._epoch != epoch:
                        return False
            return value
        finally:
            self._operation.release()

    def check(self, transaction_token, timeout=.2):
        return self._boolean('check', transaction_token, timeout)

    def verify(self, transaction_token, timeout=.2):
        return self._boolean('verify', transaction_token, timeout)

    def release(self, token, timeout=.1):
        if not isinstance(token, str):
            return
        deadline = _budget(timeout, self._clock)
        if not self._acquire(deadline):
            # Do not drop a child token unless its session is stopped. A caller
            # unable to serialize cancellation invalidates this owned session.
            with self._lock:
                owned = self._owned
            self._stop(owned)
            with self._lock:
                self._tokens.pop(token, None)
            return
        try:
            with self._lock:
                entry = self._tokens.pop(token, None) if isinstance(token, str) else None
                owned = self._owned
                eligible = entry and self.ready and entry['session'] == owned.nonce
            if eligible:
                self._call(owned, 'release', deadline, token=token)
        finally:
            self._operation.release()

    def close(self, timeout=2):
        deadline = _budget(timeout, self._clock)
        with self._lock:
            self._closing += 1
            self._epoch += 1
            owned = self._owned
            warmer = self._warm_thread
            self._tokens.clear()
        try:
            self._stop(owned, failure=False)
            if warmer is threading.current_thread():
                return False
            if warmer is not None and warmer.ident is not None:
                warmer.join(max(0., deadline - self._clock()))
            warm_closed = warmer is None or not warmer.is_alive()
            if owned is None:
                return warm_closed
            if owned.owner is threading.current_thread():
                return False
            if owned.owner.ident is not None:
                owned.owner.join(max(0., deadline - self._clock()))
            return warm_closed and owned.done.is_set() and not owned.owner.is_alive()
        finally:
            with self._lock:
                self._closing -= 1
