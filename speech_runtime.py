"""Serialized same-snapshot speech inference with bounded CPU recovery.

Dependencies are injected: this module never imports providers or downloads a
model. Status publication is independent of the model/inference lock.
"""
from pathlib import Path
import re
import threading
from types import MappingProxyType


class SpeechUnavailableError(RuntimeError):
    """Sanitized speech failure; reason is a fixed, content-free code."""
    _REASONS = frozenset(('invalid_model_name', 'cache_unavailable', 'cache_invalid',
                          'cpu_load_failed', 'cpu_inference_failed',
                          'transcription_failed', 'speech_unavailable',
                          'cuda_inference_unavailable', 'preview_recovery_pending'))

    def __init__(self, reason='speech_unavailable'):
        self.reason = reason if reason in self._REASONS else 'speech_unavailable'
        super().__init__('Speech recognition is unavailable. Retry speech startup.')


def _cached_snapshot(model_name, cache_resolver):
    if not isinstance(model_name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', model_name):
        raise SpeechUnavailableError('invalid_model_name')
    try:
        snapshot = Path(cache_resolver(model_name, local_files_only=True)).resolve(strict=True)
    except Exception:
        raise SpeechUnavailableError('cache_unavailable') from None
    _validate_snapshot(snapshot)
    return str(snapshot)


def _validate_snapshot(snapshot):
    try:
        snapshot = Path(snapshot)
        if not snapshot.is_dir():
            raise ValueError('Not a directory')
        # Faster Whisper can download a missing tokenizer even when its model
        # lookup is local-only. Verify every mandatory local asset first.
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            asset = snapshot / name
            if not asset.is_file():
                raise ValueError('Incomplete snapshot')
            with asset.open('rb') as source:
                if not source.read(1):
                    raise ValueError('Empty snapshot asset')
    except Exception:
        raise SpeechUnavailableError('cache_invalid') from None


def _cuda_failure(error):
    if not isinstance(error, (RuntimeError, OSError)):
        return False
    message = str(error).lower()
    # These are native accelerator errors, not arbitrary input/JSON errors.
    return any(marker in message for marker in
               ('cuda', 'cudnn', 'cublas', 'nvcuda.dll', 'nvidia driver',
                'gpu driver', 'out of memory', 'out-of-memory'))


def _close_iterator(iterator):
    close = getattr(iterator, 'close', None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class SpeechRuntime:
    def __init__(self, model_name, snapshot, model_factory, compute_types):
        self._name = model_name
        self._snapshot = snapshot
        self._factory = model_factory
        self._compute_types = compute_types
        self._lock = threading.RLock()
        self._model = None
        self._cpu_attempted = False
        self._status = MappingProxyType({})
        self._publish('cuda', 'float16', False, 'switching', '')

    def _publish(self, device, precision, fallback, state, reason):
        self._status = MappingProxyType({'model': self._name, 'device': device,
            'compute_type': precision, 'fallback': fallback, 'state': state, 'reason': reason})

    def status(self):
        """A copy of one immutable publication; never waits for inference."""
        return dict(self._status)

    def _construct(self, device, precision):
        # The cache may change after initial resolution or a GPU load failure.
        # Validate again immediately before every constructor; never re-resolve.
        _validate_snapshot(self._snapshot)
        model = self._factory(self._snapshot, device=device, compute_type=precision,
                              local_files_only=True)
        if not callable(getattr(model, 'transcribe', None)):
            raise TypeError('Speech backend unavailable')
        return model

    def _release_failed_gpu(self):
        previous, self._model = self._model, None
        # Real Faster Whisper exposes CTranslate2's unload through .model.
        # Optional fake/alternate wrappers can instead expose their own close.
        unload = getattr(getattr(previous, 'model', None), 'unload_model', None)
        close = unload if callable(unload) else getattr(previous, 'close', None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _load_cpu(self, reason):
        self._cpu_attempted = True
        self._publish('cpu', '', True, 'switching', reason)
        precision = ''
        try:
            supported = self._compute_types('cpu')
            precision = 'int8' if 'int8' in supported else 'float32'
            if precision not in supported:
                raise ValueError('CPU precision unavailable')
            candidate = self._construct('cpu', precision)
        except Exception as error:
            self._model = None
            failure = 'cache_invalid' if isinstance(error, SpeechUnavailableError) and error.reason == 'cache_invalid' else 'cpu_load_failed'
            self._publish('cpu', precision, True, 'failed', failure)
            raise SpeechUnavailableError(failure) from None
        self._model = candidate
        self._publish('cpu', precision, True, 'ready', reason)

    def _initialize(self):
        with self._lock:
            try:
                self._model = self._construct('cuda', 'float16')
            except Exception as error:
                if isinstance(error, SpeechUnavailableError) and error.reason == 'cache_invalid':
                    self._publish('cuda', 'float16', False, 'failed', 'cache_invalid')
                    raise SpeechUnavailableError('cache_invalid') from None
                self._load_cpu('cuda_startup_unavailable')
            else:
                self._publish('cuda', 'float16', False, 'ready', '')

    @staticmethod
    def _consume(model, audio, options):
        segments, info = model.transcribe(audio, **options)
        iterator = iter(segments)
        try:
            return list(iterator), info
        finally:
            _close_iterator(iterator)

    def transcribe(self, audio, **kwargs):
        return self._transcribe(audio, kwargs, allow_fallback=True)

    def transcribe_preview(self, audio, **kwargs):
        """Provisional work never loads another model or poisons a ready CPU."""
        return self._transcribe(audio, kwargs, allow_fallback=False)

    def _transcribe(self, audio, options, *, allow_fallback):
        if any(key in options for key in ('preview', 'allow_fallback')):
            raise ValueError('Internal speech controls are not model options')
        with self._lock:
            current = self._status
            if current['state'] != 'ready' or self._model is None:
                raise SpeechUnavailableError(current.get('reason'))
            try:
                return self._consume(self._model, audio, options)
            except Exception as error:
                if not allow_fallback:
                    # A known GPU failure asks the process owner to retire the
                    # child. Do no recovery or native unload inside preview.
                    reason = ('cuda_inference_unavailable' if current['device'] == 'cuda'
                              and _cuda_failure(error) else 'transcription_failed')
                    raise SpeechUnavailableError(reason) from None
                if current['device'] == 'cuda' and not self._cpu_attempted and _cuda_failure(error):
                    self._release_failed_gpu()
                    self._load_cpu('cuda_inference_unavailable')
                    try:
                        return self._consume(self._model, audio, options)
                    except Exception:
                        self._model = None
                        self._publish('cpu', self._status['compute_type'], True,
                                      'failed', 'cpu_inference_failed')
                        raise SpeechUnavailableError('cpu_inference_failed') from None
                if current['device'] == 'cpu':
                    self._model = None
                    self._publish('cpu', current['compute_type'], True,
                                  'failed', 'cpu_inference_failed')
                    raise SpeechUnavailableError('cpu_inference_failed') from None
                raise SpeechUnavailableError('transcription_failed') from None


def load_speech_runtime(model_name, *, model_factory, cache_resolver, compute_types):
    """Load the requested already-cached snapshot; attempt GPU then CPU once."""
    snapshot = _cached_snapshot(model_name, cache_resolver)
    runtime = SpeechRuntime(model_name, snapshot, model_factory, compute_types)
    runtime._initialize()
    return runtime
