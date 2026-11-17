"""One owned local speech process, bounded independently of native Python calls.

Only the child imports the native speech libraries. Audio and results travel in
memory over anonymous pipes; this module never writes recordings or transcripts.
"""
import atexit
import ctypes
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
from types import MappingProxyType, SimpleNamespace
import uuid

from speech_runtime import SpeechUnavailableError

WORKER = Path(__file__).with_name('speech_process_worker.py')
MAX_JSON = 4 * 1024 * 1024
MAX_AUDIO = 40 * 1024 * 1024


def _read_exact(stream, size):
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.read(size - len(parts))
        if not chunk:
            raise EOFError('Speech process ended')
        parts.extend(chunk)
    return bytes(parts)


def _read_frame(stream):
    size, = struct.unpack('<I', _read_exact(stream, 4))
    if not 0 < size <= MAX_JSON:
        raise ValueError('Invalid speech protocol size')
    message = json.loads(_read_exact(stream, size).decode('utf-8'))
    if not isinstance(message, dict):
        raise ValueError('Invalid speech protocol message')
    audio_size = message.get('audio_bytes', 0)
    if type(audio_size) is not int or not 0 <= audio_size <= MAX_AUDIO:
        raise ValueError('Invalid speech audio size')
    return message, _read_exact(stream, audio_size) if audio_size else b''


def _write_frame(stream, message, audio=b''):
    data = json.dumps(message, allow_nan=False, separators=(',', ':')).encode('utf-8')
    if not 0 < len(data) <= MAX_JSON or len(audio) > MAX_AUDIO:
        raise ValueError('Speech protocol input too large')
    for part in (struct.pack('<I', len(data)), data, audio):
        pending = memoryview(part)
        while pending:
            count = stream.write(pending)
            if not isinstance(count, int) or count <= 0:
                raise BrokenPipeError('Speech protocol write failed')
            pending = pending[count:]
    stream.flush()


class _Deadline(Exception):
    pass


class _Closed(Exception):
    pass


class _WindowsJob:
    """A kill-on-handle-close job contains only this runtime's own child."""
    def __init__(self, process):
        self.handle = None
        self._handle_lock = threading.Lock()
        if os.name != 'nt':
            return
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('minimum', ctypes.c_size_t),
                        ('maximum', ctypes.c_size_t), ('active', wintypes.DWORD),
                        ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD),
                        ('scheduling', wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_count', 'write_count', 'other_count', 'read_bytes',
                         'write_bytes', 'other_bytes')]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', Io), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                   ctypes.c_void_p, wintypes.DWORD]
        kernel.SetInformationJobObject.restype = wintypes.BOOL
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateJobObjectW(None, None)
        if not handle:
            raise OSError('Speech child ownership unavailable')
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info))
                or not kernel.AssignProcessToJobObject(handle, int(process._handle))):
            kernel.CloseHandle(handle)
            raise OSError('Speech child ownership unavailable')
        self.kernel, self.handle = kernel, handle

    def close(self):
        with self._handle_lock:
            handle, self.handle = self.handle, None
        if handle is not None:
            self.kernel.CloseHandle(handle)


def _child_environment():
    # Never copy provider credentials into the local decoder process.
    permitted = {'SYSTEMROOT', 'WINDIR', 'PATH', 'TEMP', 'TMP', 'USERPROFILE',
                 'LOCALAPPDATA', 'APPDATA', 'PROGRAMFILES', 'PROGRAMFILES(X86)',
                 'PROGRAMDATA', 'HOMEDRIVE', 'HOMEPATH', 'HF_HOME', 'HF_HUB_CACHE',
                 'XDG_CACHE_HOME', 'CUDA_PATH', 'CUDA_MODULE_LOADING',
                 'CUDA_VISIBLE_DEVICES', 'CUDA_DEVICE_ORDER'}
    env = {key: value for key, value in os.environ.items()
           if key.upper() in permitted or re.fullmatch(r'CUDA_PATH_V\d+_\d+', key.upper())}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8',
               PYTHONUNBUFFERED='1', HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               HF_HUB_DISABLE_TELEMETRY='1', DO_NOT_TRACK='1')
    # Preserve the same already installed packages, including Store Python paths
    # supplied by the guarded test runner. No package lookup or installation.
    env['PYTHONPATH'] = os.pathsep.join(str(path) for path in sys.path if path)
    return env


def _python_executable():
    path = Path(sys.executable)
    if path.name.lower() == 'pythonw.exe':
        candidate = path.with_name('python.exe')
        if candidate.is_file():
            return str(candidate)
    return str(path)


class _Session:
    def __init__(self, process, job):
        self.process, self.job = process, job
        self.stop_lock = threading.Lock()
        self.token = uuid.uuid4().hex
        self.events = queue.Queue(maxsize=16)
        self.reader = threading.Thread(target=self._read, daemon=True,
                                       name='speech-response-reader')
        self.error_reader = threading.Thread(target=self._discard_errors, daemon=True,
                                             name='speech-error-drain')
        self.reader.start()
        self.error_reader.start()

    def _read(self):
        try:
            while True:
                value = _read_frame(self.process.stdout)
                self.events.put_nowait(value)
        except Exception:
            try: self.events.put_nowait(({'kind': 'disconnected'}, b''))
            except queue.Full: pass

    def _discard_errors(self):
        try:
            while self.process.stderr.read(8192):
                pass
        except (OSError, ValueError):
            pass


class SpeechProcessRuntime:
    def __init__(self, model_name, *, preferred_device='cuda', startup_timeout=45,
                 inference_timeout=None):
        if not isinstance(model_name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', model_name):
            raise SpeechUnavailableError('invalid_model_name')
        if preferred_device not in ('cuda', 'cpu'):
            raise ValueError('Unsupported speech device')
        for value in (startup_timeout, inference_timeout):
            if value is not None and (type(value) not in (int, float)
                                      or not math.isfinite(value) or value <= 0):
                raise ValueError('Speech timeout must be positive and finite')
        self._name = model_name
        self._startup_timeout, self._inference_timeout = startup_timeout, inference_timeout
        self._snapshot = None
        self._cpu_attempted = preferred_device == 'cpu'
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._result_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._session = None
        self._preview_recovery = None
        self._serial = 0
        self._state = MappingProxyType({'model': model_name, 'device': preferred_device,
            'compute_type': 'float16' if preferred_device == 'cuda' else '',
            'fallback': preferred_device == 'cpu' and model_name != 'parakeet-unified-en-0.6b', 'state': 'switching', 'reason': ''})
        atexit.register(self.close)

    def status(self):
        return dict(self._state)

    def _publish(self, status):
        required = {'model', 'device', 'compute_type', 'fallback', 'state', 'reason'}
        if (not isinstance(status, dict) or set(status) != required
                or status['model'] != self._name
                or status['device'] not in ('cuda', 'cpu')
                or status['compute_type'] not in ('', 'float16', 'float32', 'int8', 'Q8_0')
                or (status['compute_type'] == 'Q8_0' and
                    (self._name != 'parakeet-unified-en-0.6b' or status['device'] != 'cpu'))
                or type(status['fallback']) is not bool
                or status['state'] not in ('switching', 'ready', 'failed')
                or status['reason'] not in ('', 'cuda_startup_unavailable', 'cuda_inference_unavailable',
                    'cache_invalid', 'cache_unavailable', 'cpu_load_failed', 'cpu_inference_failed',
                    'transcription_failed', 'speech_unavailable', 'preview_recovery_pending')):
            raise ValueError('Invalid speech status')
        self._state = MappingProxyType(dict(status))
        if status['device'] == 'cpu':
            self._cpu_attempted = True

    def _failed(self, reason='speech_unavailable'):
        self._preview_recovery = None
        state = dict(self._state)
        state.update(state='failed', reason=reason)
        self._state = MappingProxyType(state)

    def _launch(self):
        with self._process_lock:
            if self._closing.is_set():
                raise _Closed()
            if self._session is not None and self._session.process.poll() is None:
                raise RuntimeError('Previous speech child is still active')
            process = subprocess.Popen([_python_executable(), str(WORKER)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=str(WORKER.parent), env=_child_environment(), bufsize=0,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            job = None
            try:
                # The child first waits for init input. No native library loads
                # until the OS ownership job has been successfully assigned.
                job = _WindowsJob(process)
                session = _Session(process, job)
            except Exception:
                if job is not None:
                    job.close()
                try:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=3)
                finally:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        stream.close()
                raise
            self._session = session
            return session

    def _stop(self, session):
        if session is None:
            return True
        # Close and a failing in-flight request can arrive together. Pair the
        # OS job handle with exactly one stop, even under that overlap.
        if not session.stop_lock.acquire(timeout=5):
            return False
        try:
            return self._stop_owned(session)
        finally:
            session.stop_lock.release()

    def _stop_owned(self, session):
        process = session.process
        try:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return False
        finally:
            if process.poll() is not None:
                session.job.close()
        for stream in (process.stdin, process.stdout, process.stderr):
            try: stream.close()
            except (OSError, ValueError): pass
        session.reader.join(timeout=.5)
        session.error_reader.join(timeout=.5)
        return process.poll() is not None

    def close(self):
        with self._result_lock:
            self._closing.set()
        with self._process_lock:
            session = self._session
        stopped = self._stop(session)
        if stopped:
            self._failed()
            with self._process_lock:
                if self._session is session:
                    self._session = None
            atexit.unregister(self.close)
        return stopped

    def _call(self, session, op, *, timeout, audio=b'', **values):
        if self._closing.is_set():
            raise _Closed()
        self._serial += 1
        request_id = self._serial
        message = dict(values, op=op, session=session.token, request_id=request_id,
                       audio_bytes=len(audio))
        sender_error = []
        sent = threading.Event()
        def send():
            try: _write_frame(session.process.stdin, message, audio)
            except Exception: sender_error.append(True)
            finally: sent.set()
        threading.Thread(target=send, daemon=True, name='speech-request-writer').start()
        deadline = time.monotonic() + timeout
        while True:
            if self._closing.is_set():
                raise _Closed()
            if sent.is_set() and sender_error:
                raise EOFError('Speech request failed')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Deadline()
            try:
                response, blob = session.events.get(timeout=min(.05, remaining))
            except queue.Empty:
                continue
            if self._closing.is_set():
                raise _Closed()
            if (blob or response.get('kind') == 'disconnected'):
                raise EOFError('Speech process disconnected')
            if response.get('session') != session.token or response.get('request_id') != request_id:
                raise ValueError('Stale speech response')
            if 'status' in response:
                with self._result_lock:
                    if self._closing.is_set():
                        raise _Closed()
                    self._publish(response['status'])
            kind = response.get('kind')
            if kind == 'snapshot':
                snapshot = response.get('snapshot')
                if op != 'initialize' or not isinstance(snapshot, str) or not Path(snapshot).is_absolute():
                    raise ValueError('Invalid speech snapshot')
                if self._snapshot is not None and snapshot != self._snapshot:
                    raise ValueError('Speech snapshot changed')
                self._snapshot = snapshot
            elif kind == 'status':
                continue
            elif kind in ('ready', 'result', 'error'):
                return response
            else:
                raise ValueError('Invalid speech response')

    def initialize(self, preferred_device='cuda'):
        with self._lock:
            try:
                self._initialize_child(preferred_device)
                return self
            except _Deadline:
                session = self._session
                if not self._stop(session):
                    self._failed()
                    raise SpeechUnavailableError() from None
                if (preferred_device == 'cuda' and self._snapshot is not None
                        and not self._cpu_attempted and not self._closing.is_set()):
                    self._cpu_attempted = True
                    try:
                        self._initialize_child('cpu', reason='cuda_startup_unavailable')
                        return self
                    except Exception:
                        pass
                self._failed('cpu_load_failed' if self._cpu_attempted else 'speech_unavailable')
                self._stop(self._session)
                raise SpeechUnavailableError() from None
            except Exception as error:
                self._failed(error.reason if isinstance(error, SpeechUnavailableError) else 'speech_unavailable')
                self._stop(self._session)
                raise SpeechUnavailableError() from None

    def _initialize_child(self, device, reason=''):
        if device == 'cpu':
            self._cpu_attempted = True
        session = self._launch()
        response = self._call(session, 'initialize', timeout=self._startup_timeout,
            model=self._name, device=device, snapshot=self._snapshot, reason=reason,
            parent_pid=os.getpid())
        with self._result_lock:
            if self._closing.is_set():
                raise _Closed()
            if response['kind'] != 'ready' or self._state['state'] != 'ready':
                raise SpeechUnavailableError(response.get('reason'))

    def transcribe(self, audio, **options):
        return self._transcribe(audio, options, preview=False)

    def transcribe_preview(self, audio, **options):
        """Best-effort provisional work; never initialize or retry a model."""
        return self._transcribe(audio, options, preview=True)

    def _defer_preview_recovery(self):
        """Retire a failed preview child; only final recognition may recover."""
        if self._closing.is_set():
            raise SpeechUnavailableError()
        state, session = dict(self._state), self._session
        if not self._stop(session):
            self._failed()
            raise SpeechUnavailableError()
        with self._result_lock:
            if self._closing.is_set():
                raise SpeechUnavailableError()
            # Never lose ownership of a child whose exit was not established.
            with self._process_lock:
                if self._session is session:
                    self._session = None
            if (self._snapshot is None or
                    (state['device'] == 'cuda' and self._cpu_attempted)):
                self._failed()
                raise SpeechUnavailableError()
            reason = ('cuda_inference_unavailable' if state['device'] == 'cuda'
                      else state['reason'])
            self._preview_recovery = ('cpu', reason)
            state.update(state='switching', reason='preview_recovery_pending')
            self._publish(state)

    def _transcribe(self, audio, options, *, preview):
        import numpy as np
        if any(key in options for key in ('preview', 'allow_fallback')):
            raise ValueError('Internal speech controls are not model options')
        if (not isinstance(audio, np.ndarray) or audio.ndim != 1 or not audio.size
                or audio.size * 4 > MAX_AUDIO or not np.isfinite(audio).all()):
            raise ValueError('Speech requires bounded finite mono samples')
        encoded = np.asarray(audio, dtype='<f4', order='C').tobytes()
        # Validate before admission; never leave a blocked writer for bad JSON.
        json.dumps(options, allow_nan=False)
        timeout = self._inference_timeout
        if timeout is None:
            timeout = 15 if preview else min(900, 30 + 2 * audio.size / 16000)
        with self._lock:
            if self._closing.is_set():
                raise SpeechUnavailableError(self._state['reason'])
            if (not preview and self._preview_recovery is not None
                    and self._state['state'] == 'switching'
                    and self._state['reason'] == 'preview_recovery_pending'):
                device, reason = self._preview_recovery
                self._preview_recovery = None  # one attempt, not a reload loop
                try:
                    self._initialize_child(device, reason=reason)
                except Exception as error:
                    reason = error.reason if isinstance(error, SpeechUnavailableError) else 'cpu_load_failed'
                    self._failed(reason)
                    self._stop(self._session)
                    raise SpeechUnavailableError(reason) from None
            if self._closing.is_set() or self._state['state'] != 'ready':
                raise SpeechUnavailableError(self._state['reason'])
            try:
                return self._transcribe_once(encoded, options, timeout, preview=preview)
            except _Deadline:
                if preview:
                    self._defer_preview_recovery()
                    raise SpeechUnavailableError('preview_recovery_pending') from None
                if not self._stop(self._session):
                    self._failed()
                    raise SpeechUnavailableError() from None
                if (self._state['device'] == 'cuda' and not self._cpu_attempted
                        and self._snapshot is not None and not self._closing.is_set()):
                    self._cpu_attempted = True
                    try:
                        self._initialize_child('cpu', reason='cuda_inference_unavailable')
                        return self._transcribe_once(encoded, options, timeout)
                    except Exception:
                        pass
                self._failed('cpu_inference_failed' if self._cpu_attempted else 'transcription_failed')
                self._stop(self._session)
                raise SpeechUnavailableError() from None
            except SpeechUnavailableError as error:
                if preview and error.reason == 'cuda_inference_unavailable':
                    self._defer_preview_recovery()
                    raise SpeechUnavailableError('preview_recovery_pending') from None
                if self._state['state'] == 'failed':
                    self._stop(self._session)
                raise
            except Exception:
                if preview:
                    self._defer_preview_recovery()
                    raise SpeechUnavailableError('preview_recovery_pending') from None
                self._failed()
                self._stop(self._session)
                raise SpeechUnavailableError() from None

    def _transcribe_once(self, audio, options, timeout, *, preview=False):
        response = self._call(self._session, 'transcribe', timeout=timeout,
                              audio=audio, options=options, preview=preview)
        if response['kind'] == 'error':
            raise SpeechUnavailableError(response.get('reason'))
        if response['kind'] != 'result' or self._state['state'] != 'ready':
            raise ValueError('Speech result unavailable')
        rows = response.get('segments')
        if not isinstance(rows, list) or len(rows) > 10000:
            raise ValueError('Invalid speech result')
        segments = []
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'text', 'avg_logprob', 'no_speech_prob', 'compression_ratio'}
                    or not isinstance(row['text'], str) or len(row['text']) > 65536
                    or any(type(row[key]) not in (int, float) or not math.isfinite(row[key])
                           for key in ('avg_logprob', 'no_speech_prob', 'compression_ratio'))
                    or not 0 <= row['no_speech_prob'] <= 1):
                raise ValueError('Invalid speech segment')
            segments.append(SimpleNamespace(**row))
        with self._result_lock:
            if self._closing.is_set():
                raise _Closed()
            return segments, None


def load_speech_process(model_name, *, preferred_device='cuda', startup_timeout=45,
                        inference_timeout=None):
    runtime = SpeechProcessRuntime(model_name, preferred_device=preferred_device,
        startup_timeout=startup_timeout, inference_timeout=inference_timeout)
    try:
        return runtime.initialize(preferred_device)
    except Exception:
        runtime.close()
        raise
