"""Exercise the actual Parakeet adapter using fake native functions only."""
import ctypes as c
import hashlib
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import parakeet_runtime as adapter
from speech_runtime import SpeechUnavailableError


class Function:
    def __init__(self, call):
        self.call = call

    def __call__(self, *args):
        return self.call(*args)


class Native:
    def __init__(self, *, version=b'0.4.2', model=11, session=22,
                 model_result=0, session_result=0, bad_size=None,
                 texts=(b'first request', b'second request'), run_results=(), init_result=0):
        self.version, self.model, self.session = version, model, session
        self.model_result, self.session_result = model_result, session_result
        self.bad_size, self.texts, self.run_results = bad_size, texts, run_results
        self.init_result = init_result
        self.runs, self.freed, self.initialized = [], [], []
        self.params, self.text_calls, self.cookie_closes = {}, 0, 0
        self.on_run = None
        for name in ('version', 'init_backends', 'model_load_params_init',
                     'session_params_init', 'model_load_file', 'session_init',
                     'run', 'full_text', 'session_free', 'model_free'):
            call = (lambda value=version: value) if name == 'version' else getattr(self, name)
            setattr(self, 'transcribe_' + name, Function(call))

    def init_backends(self, path):
        self.initialized.append(path)
        return self.init_result

    def model_load_params_init(self, pointer):
        pointer._obj.struct_size = c.sizeof(adapter._ModelParams) + (self.bad_size == 'model')

    def session_params_init(self, pointer):
        pointer._obj.struct_size = c.sizeof(adapter._SessionParams) + (self.bad_size == 'session')

    def model_load_file(self, path, params, output):
        self.params.update(path=path, backend=params._obj.backend)
        output._obj.value = self.model
        return self.model_result

    def session_init(self, model, params, output):
        assert model.value == self.model
        self.params['threads'] = params._obj.n_threads
        output._obj.value = self.session
        return self.session_result

    def run(self, session, pointer, length, options):
        assert session.value == self.session and options is None
        self.runs.append(np.ctypeslib.as_array(pointer, shape=(length,)).copy())
        if self.on_run:
            self.on_run()
        index = len(self.runs) - 1
        return self.run_results[index] if index < len(self.run_results) else 0

    def full_text(self, session):
        assert session.value == self.session
        self.text_calls += 1
        return self.texts[min(len(self.runs) - 1, len(self.texts) - 1)]

    def session_free(self, handle):
        self.freed.append(('session', handle.value))

    def model_free(self, handle):
        self.freed.append(('model', handle.value))

    def close(self):
        self.cookie_closes += 1


def load(native, snapshot='pinned-model', *, cache_error=None, dll_present=True):
    with patch.object(adapter, 'cached_model', side_effect=cache_error,
                      return_value='pinned-model'), \
            patch.object(adapter.Path, 'is_file', return_value=dll_present), \
            patch.object(adapter.os, 'add_dll_directory', return_value=native), \
            patch.object(adapter.c, 'CDLL', return_value=native):
        return adapter.ParakeetRuntime(snapshot)


def unavailable(action, reason):
    try:
        action()
    except SpeechUnavailableError as error:
        assert error.reason == reason, (error.reason, reason)
        assert 'SECRET' not in str(error) and 'pinned-model' not in str(error)
    else:
        raise AssertionError('Expected sanitized failure: ' + reason)


def invalid(action):
    try:
        action()
    except ValueError:
        pass
    else:
        raise AssertionError('Expected rejected audio/options')


def test_cache():
    assert adapter.MODEL_NAME == 'parakeet-unified-en-0.6b'
    assert adapter.REVISION == '7e948f21b7bdbac698d3318db9d350f1096f3b6c'
    assert adapter.MODEL_SHA256 == '4b50b6dd862bf6e346929aaf4f5eaacec003bfa3f56462d6c874b41ef2f38795'
    with tempfile.TemporaryDirectory(prefix='.parakeet-cache-', dir=Path(__file__).resolve().parents[1]) as directory:
        home = Path(directory)
        path = (home / '.cache/huggingface/hub' /
                'models--handy-computer--parakeet-unified-en-0.6b-gguf' /
                'snapshots' / adapter.REVISION / 'parakeet-unified-en-0.6b-Q8_0.gguf')
        with patch.object(adapter.Path, 'home', return_value=home):
            unavailable(adapter.cached_model, 'cache_unavailable')
            path.parent.mkdir(parents=True)
            path.write_bytes(b'synthetic weights, never native-loaded')
            unavailable(adapter.cached_model, 'cache_invalid')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with patch.object(adapter, 'MODEL_SHA256', digest):
                assert adapter.cached_model() == str(path)
            with patch.object(adapter.Path, 'open', side_effect=PermissionError('SECRET')):
                unavailable(adapter.cached_model, 'cache_unavailable')


def test_constructor_and_status():
    native = Native()
    runtime = load(native)
    assert runtime.status() == dict(model=adapter.MODEL_NAME, device='cpu',
        compute_type='Q8_0', fallback=False, state='ready', reason='')
    assert native.params == dict(path=b'pinned-model', backend=1, threads=4)
    expected = Path.home() / 'AppData/Local/Listen/runtimes/transcribe-0.4.2-cpu-vulkan'
    assert native.initialized == [adapter.os.fsencode(expected)]
    status = runtime.status()
    status['state'] = 'tampered'
    assert runtime.status()['state'] == 'ready'
    runtime.close()
    assert runtime.status()['state'] == 'failed'
    assert runtime.status()['reason'] == 'speech_unavailable'
    runtime.close()
    assert native.freed == [('session', 22), ('model', 11)]
    assert native.cookie_closes == 1


def test_constructor_failures():
    for reason in ('cache_invalid', 'cache_unavailable'):
        native = Native()
        unavailable(lambda: load(native, cache_error=SpeechUnavailableError(reason)), reason)
        assert native.freed == [] and native.cookie_closes == 0
    for native, options, reason, freed, cookies in (
            (Native(), {'snapshot': 'wrong-model'}, 'cache_invalid', [], 0),
            (Native(), {'dll_present': False}, 'cache_unavailable', [], 0),
            (Native(version=b'wrong'), {}, 'cache_invalid', [], 1),
            (Native(version=None), {}, 'cache_invalid', [], 1),
            (Native(bad_size='model'), {}, 'cache_invalid', [], 1),
            (Native(bad_size='session'), {}, 'cache_invalid', [], 1),
            (Native(init_result=1), {}, 'cpu_load_failed', [], 1),
            (Native(model=None), {}, 'cpu_load_failed', [], 1),
            (Native(session=None), {}, 'cpu_load_failed', [('model', 11)], 1),
            (Native(model_result=1), {}, 'cpu_load_failed', [('model', 11)], 1),
            (Native(session_result=1), {}, 'cpu_load_failed', [('session', 22), ('model', 11)], 1)):
        unavailable(lambda: load(native, **options), reason)
        assert native.freed == freed, (reason, native.freed)
        assert native.cookie_closes == cookies
    for missing, freed in (('session_free', []), ('model_free', []),
                            ('run', [('session', 22), ('model', 11)])):
        native = Native()
        delattr(native, 'transcribe_' + missing)
        unavailable(lambda: load(native), 'cpu_load_failed')
        assert native.freed == freed and native.cookie_closes == 1
    native = Native()
    with patch.object(adapter.c, 'CDLL', side_effect=OSError('SECRET')):
        with patch.object(adapter, 'cached_model', return_value='pinned-model'), \
                patch.object(adapter.Path, 'is_file', return_value=True), \
                patch.object(adapter.os, 'add_dll_directory', return_value=native):
            unavailable(lambda: adapter.ParakeetRuntime('pinned-model'), 'cpu_load_failed')
    assert native.cookie_closes == 1


def test_consecutive_and_options():
    native = Native(texts=(b'  first request  ', b'second request'))
    runtime = load(native)
    audio = np.full(1600, .1, dtype=np.float32)
    first, info = runtime.transcribe_preview(audio, language='en', beam_size=1,
        initial_prompt='Whisper-only vocabulary', vad_filter=True)
    second, _ = runtime.transcribe(audio * 2, language='en', beam_size=5)
    assert [row.text for row in first] == ['first request'] and info is None
    assert [row.text for row in second] == ['second request']
    assert first[0].avg_logprob == first[0].no_speech_prob == first[0].compression_ratio == 0.0
    assert len(native.runs) == native.text_calls == 2
    assert np.array_equal(native.runs[0], audio)
    assert np.array_equal(native.runs[1], audio * 2)
    runtime.close()


def test_silence_and_invalid_inputs():
    native = Native()
    runtime = load(native)
    for audio in ([], np.zeros(16000), np.zeros(25 * 16000), np.full(16000, .00001)):
        assert runtime.transcribe(audio) == ([], None)
    for audio in (np.ones((2, 2)), [float('nan')], [float('inf')], [float('-inf')],
                  [complex(1, 2)], [1e300], ['bad']):
        invalid(lambda: runtime.transcribe(audio))
    invalid(lambda: runtime.transcribe([.1], language='fr'))
    invalid(lambda: runtime.transcribe([.1], preview=True))
    invalid(lambda: runtime.transcribe([.1], allow_fallback=True))
    assert not native.runs and not native.text_calls
    assert runtime.status()['state'] == 'ready'
    runtime.close()


def test_native_text_and_inference_failure():
    for text in (None, b'\xff'):
        native = Native(texts=(text,))
        runtime = load(native)
        unavailable(lambda: runtime.transcribe([.1]), 'cpu_inference_failed')
        assert runtime.status()['state'] == 'failed'
        unavailable(lambda: runtime.transcribe([.1]), 'cpu_inference_failed')
        assert len(native.runs) == 1
        runtime.close()
    native = Native(texts=(b'', b'new words'))
    runtime = load(native)
    assert runtime.transcribe([.1]) == ([], None)
    result, _ = runtime.transcribe([.2])
    assert [row.text for row in result] == ['new words']
    runtime.close()


def test_long_audio_coverage():
    native = Native(texts=(b'one', b'two', b'three', b'tail'))
    runtime = load(native)
    audio = np.linspace(.1, .9, 61 * 16000 + 123, dtype=np.float32)
    # The quiet boundary must be selected without dropping or repeating any sample.
    audio[18 * 16000:18 * 16000 + 1600] = .001
    rows, _ = runtime.transcribe(audio)
    assert len(native.runs) == 4
    assert len(native.runs[0]) == 18 * 16000 + 800
    assert all(17 * 16000 <= len(chunk) < 20 * 16000 for chunk in native.runs[:-1])
    assert np.array_equal(np.concatenate(native.runs), audio)
    assert len(native.runs[-1]) > 0 and native.runs[-1][-1] == audio[-1]
    assert [row.text for row in rows] == ['one', 'two', 'three', 'tail']
    runtime.close()


def test_silent_middle_and_final_tail():
    native = Native(texts=(b'before silence', b'after silence'))
    runtime = load(native)
    audio = np.zeros(60 * 16000, dtype=np.float32)
    audio[:2 * 16000] = .1
    audio[-1600:] = .2
    rows, _ = runtime.transcribe(audio)
    assert len(native.runs) == 2
    assert np.array_equal(native.runs[0], audio[:272800])
    assert np.array_equal(native.runs[1], audio[818400:])
    assert not np.any(audio[272800:818400])
    assert native.runs[0][0] == audio[0] and native.runs[-1][-1] == audio[-1]
    assert [row.text for row in rows] == ['before silence', 'after silence']
    runtime.close()


def test_later_chunk_failure_is_not_partial_success():
    for results, texts in (((0, 1), (b'partial text', b'unused')),
                           ((0, 0), (b'partial text', None))):
        native = Native(run_results=results, texts=texts)
        runtime = load(native)
        unavailable(lambda: runtime.transcribe(np.full(40 * 16000, .1)), 'cpu_inference_failed')
        assert len(native.runs) == 2 and runtime.status()['state'] == 'failed'
        runtime.close()


def test_close_during_request():
    native = Native()
    runtime = load(native)
    entered, release = threading.Event(), threading.Event()
    failures = []

    def blocked():
        entered.set()
        assert release.wait(5), 'Test did not release fake inference'

    def transcribe():
        try:
            runtime.transcribe(np.full(40 * 16000, .1))
        except SpeechUnavailableError as error:
            failures.append(error.reason)

    native.on_run = blocked
    inference = threading.Thread(target=transcribe)
    inference.start()
    assert entered.wait(5)
    closer = threading.Thread(target=runtime.close)
    closer.start()
    queued = threading.Thread(target=transcribe)
    try:
        assert runtime._closing.wait(5), 'Close must reserve cancellation before waiting'
        assert native.freed == [], 'An in-use native session must not be freed'
        queued.start()
    finally:
        release.set()
        inference.join(5)
        closer.join(5)
        if queued.ident is not None:
            queued.join(5)
    assert not inference.is_alive() and not closer.is_alive() and not queued.is_alive()
    assert failures == ['speech_unavailable', 'speech_unavailable']
    assert len(native.runs) == 1
    assert runtime.status()['state'] == 'failed'
    unavailable(lambda: runtime.transcribe([.1]), 'speech_unavailable')
    unavailable(lambda: runtime.transcribe_preview([.1]), 'speech_unavailable')
    runtime.close()
    assert native.freed == [('session', 22), ('model', 11)] and native.cookie_closes == 1


def test_close_cleanup_failure():
    native = Native()
    runtime = load(native)

    def fail_free(handle):
        native.freed.append(('session', handle.value))
        raise RuntimeError('synthetic free error')

    native.transcribe_session_free = Function(fail_free)
    try:
        runtime.close()
    except RuntimeError:
        pass
    else:
        raise AssertionError('Unexpected cleanup success')
    runtime.close()
    assert native.freed == [('session', 22), ('model', 11)] and native.cookie_closes == 1
    assert runtime.status()['state'] == 'failed'
    unavailable(lambda: runtime.transcribe([.1]), 'speech_unavailable')


def main():
    tests = (test_cache, test_constructor_and_status, test_constructor_failures,
             test_consecutive_and_options, test_silence_and_invalid_inputs,
             test_native_text_and_inference_failure, test_long_audio_coverage,
             test_silent_middle_and_final_tail, test_later_chunk_failure_is_not_partial_success,
             test_close_during_request, test_close_cleanup_failure)
    for test in tests:
        test()
        print('PASS', test.__name__)
    print(f'{len(tests)} Parakeet adapter groups passed; fake native functions only')


if __name__ == '__main__':
    main()
