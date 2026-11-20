"""Local-only cached Parakeet Unified adapter for Listen's decoder child.

Uses the existing audio/rolling-prefix request contract, not native streaming.
No model downloads, microphone access, cleanup calls, or persistent transcript writes.
"""
import ctypes as c
import hashlib
import os
from pathlib import Path
import threading
from types import SimpleNamespace

from speech_runtime import SpeechUnavailableError

MODEL_NAME = 'parakeet-unified-en-0.6b'
MODEL_SHA256 = '4b50b6dd862bf6e346929aaf4f5eaacec003bfa3f56462d6c874b41ef2f38795'
REVISION = '7e948f21b7bdbac698d3318db9d350f1096f3b6c'


def model_path():
    return (Path.home() / '.cache/huggingface/hub' /
            'models--handy-computer--parakeet-unified-en-0.6b-gguf' /
            'snapshots' / REVISION / 'parakeet-unified-en-0.6b-Q8_0.gguf')


def cached_model():
    path = model_path()
    try:
        with path.open('rb') as source:
            digest = hashlib.file_digest(source, 'sha256').hexdigest()
    except OSError:
        raise SpeechUnavailableError('cache_unavailable') from None
    if digest != MODEL_SHA256:
        raise SpeechUnavailableError('cache_invalid') from None
    return str(path)


class _ModelParams(c.Structure):
    _fields_ = [('struct_size', c.c_uint64), ('backend', c.c_int),
                ('device', c.c_void_p), ('no_mmap', c.c_bool)]


class _SessionParams(c.Structure):
    _fields_ = [('struct_size', c.c_uint64), ('n_threads', c.c_int),
                ('kv_type', c.c_int), ('n_ctx', c.c_int32)]


class ParakeetRuntime:
    def __init__(self, snapshot):
        import numpy as np
        self._np = np
        self._lock = threading.RLock()
        self._closing = threading.Event()
        self._model, self._session = c.c_void_p(), c.c_void_p()
        self._publish('switching')
        self._dll_cookie = None
        self._lib = None
        try:
            if snapshot != cached_model():
                raise SpeechUnavailableError('cache_invalid')
            base = Path(__file__).with_name('speech-runtime')
            if not base.is_dir():
                base = Path.home() / 'AppData/Local/Listen/runtimes/transcribe-0.4.2-cpu-vulkan'
            if not (base / 'transcribe.dll').is_file():
                raise SpeechUnavailableError('cache_unavailable')
            self._dll_cookie = os.add_dll_directory(str(base))
            self._lib = c.CDLL(str(base / 'transcribe.dll'))
            version = self._api('version', [], c.c_char_p)()
            if version != b'0.4.2':
                raise SpeechUnavailableError('cache_invalid')
            self._check(self._api('init_backends', [c.c_char_p])(os.fsencode(base)), 'cpu_load_failed')
            mp, sp = _ModelParams(), _SessionParams()
            self._api('model_load_params_init', [c.POINTER(_ModelParams)], None)(c.byref(mp))
            self._api('session_params_init', [c.POINTER(_SessionParams)], None)(c.byref(sp))
            if mp.struct_size != c.sizeof(mp) or sp.struct_size != c.sizeof(sp):
                raise SpeechUnavailableError('cache_invalid')
            self._api('session_free', [c.c_void_p], None)
            self._api('model_free', [c.c_void_p], None)
            mp.backend = 1  # Explicit CPU: matches the tested trial, never competes for GPU VRAM.
            sp.n_threads = 4
            self._check(self._api('model_load_file',
                [c.c_char_p, c.POINTER(_ModelParams), c.POINTER(c.c_void_p)])(
                    os.fsencode(snapshot), c.byref(mp), c.byref(self._model)), 'cpu_load_failed')
            if not self._model.value:
                raise SpeechUnavailableError('cpu_load_failed')
            self._check(self._api('session_init',
                [c.c_void_p, c.POINTER(_SessionParams), c.POINTER(c.c_void_p)])(
                    self._model, c.byref(sp), c.byref(self._session)), 'cpu_load_failed')
            if not self._session.value:
                raise SpeechUnavailableError('cpu_load_failed')
            self._run = self._api('run', [c.c_void_p, c.POINTER(c.c_float), c.c_int, c.c_void_p])
            self._text = self._api('full_text', [c.c_void_p], c.c_char_p)
            self._publish('ready')
        except Exception as error:
            reason = error.reason if isinstance(error, SpeechUnavailableError) else 'cpu_load_failed'
            try:
                self.close()
            except Exception:
                pass  # Preserve the sanitized startup failure after attempting all cleanup.
            self._publish('failed', reason)
            raise SpeechUnavailableError(reason) from None

    def _api(self, name, args, result=c.c_int):
        function = getattr(self._lib, 'transcribe_' + name)
        function.argtypes, function.restype = args, result
        return function

    @staticmethod
    def _check(result, reason='cpu_inference_failed'):
        if result:
            raise SpeechUnavailableError(reason)

    def _publish(self, state, reason=''):
        self._status = dict(model=MODEL_NAME, device='cpu', compute_type='Q8_0',
                            fallback=False, state=state, reason=reason)

    def status(self):
        return dict(self._status)

    def close(self):
        self._closing.set()
        with self._lock:
            self._publish('failed', 'speech_unavailable')
            session, model, cookie = self._session, self._model, self._dll_cookie
            self._session, self._model, self._dll_cookie = c.c_void_p(), c.c_void_p(), None
            try:
                if session.value:
                    self._api('session_free', [c.c_void_p], None)(session)
            finally:
                try:
                    if model.value:
                        self._api('model_free', [c.c_void_p], None)(model)
                finally:
                    if cookie is not None:
                        cookie.close()

    def transcribe(self, audio, **options):
        np = self._np
        with self._lock:
            if self._closing.is_set() or self._status['state'] != 'ready':
                raise SpeechUnavailableError(self._status['reason'] or 'speech_unavailable')
            if options.get('language', 'en') != 'en':
                raise ValueError('Parakeet Unified is English-only')
            # Existing Whisper tuning/hints have no equivalent in this native decoder.
            if options.keys() - {'language', 'beam_size', 'vad_filter', 'initial_prompt'}:
                raise ValueError('Unsupported Parakeet option')
            if np.iscomplexobj(audio):
                raise ValueError('Expected finite mono PCM')
            with np.errstate(over='ignore', invalid='ignore'):
                samples = np.asarray(audio, dtype=np.float32)
            if samples.ndim != 1 or not np.isfinite(samples).all():
                raise ValueError('Expected finite mono PCM')
            # Preserve long dictations without unbounded full-attention allocation.
            # Cut at the quietest 100ms window near each 20s boundary, without overlap.
            # shortcut: cuts can split uninterrupted words; qualify long speech before activation.
            start, texts = 0, []
            while start < len(samples):
                if self._closing.is_set():
                    raise SpeechUnavailableError('speech_unavailable')
                end = min(start + 20 * 16000, len(samples))
                if end < len(samples):
                    cuts = range(max(start + 15 * 16000, end - 3 * 16000), end, 1600)
                    end = min(cuts, key=lambda at: float(np.mean(
                        np.square(samples[at:at + 1600], dtype=np.float64)))) + 800
                chunk = np.ascontiguousarray(samples[start:end])
                start = end
                if float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64)))) < .0001:
                    continue
                if self._closing.is_set():
                    raise SpeechUnavailableError('speech_unavailable')
                try:
                    self._check(self._run(self._session, chunk.ctypes.data_as(c.POINTER(c.c_float)),
                                          len(chunk), None))
                    encoded = self._text(self._session)
                    if encoded is None:
                        raise SpeechUnavailableError('cpu_inference_failed')
                    text = encoded.decode('utf-8').strip()
                except Exception:
                    self._publish('failed', 'cpu_inference_failed')
                    raise SpeechUnavailableError('cpu_inference_failed') from None
                if text:
                    texts.append(text)
            if self._closing.is_set():
                raise SpeechUnavailableError('speech_unavailable')
            # These fields mean unavailable metrics, not measured model confidence.
            # The wire protocol requires finite values; retain Listen's neutral defaults.
            return [SimpleNamespace(text=text, avg_logprob=0.0, no_speech_prob=0.0,
                                    compression_ratio=0.0) for text in texts], None

    def transcribe_preview(self, audio, **options):
        return self.transcribe(audio, **options)

