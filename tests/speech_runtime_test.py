"""Pure same-snapshot CPU recovery checks; no real weights, devices or network."""
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speech_runtime import load_speech_runtime, SpeechUnavailableError


class Model:
    def __init__(self, behavior=None):
        self.behavior = behavior
        self.calls = []
        self.closed = False
    def transcribe(self, audio, **options):
        self.calls.append((audio, options))
        if self.behavior:
            return self.behavior(audio, options)
        return iter(('exact synthetic result',)), {'synthetic': True}
    def close(self):
        self.closed = True


class Partial:
    def __init__(self, error):
        self.error = error
        self.closed = False
        self.steps = 0
    def __iter__(self): return self
    def __next__(self):
        self.steps += 1
        if self.steps == 1: return 'discarded GPU prefix'
        raise self.error
    def close(self): self.closed = True


class Fixture:
    def __init__(self, directory, gpu=None, cpu=None, gpu_error=None, cpu_error=None,
                 supported=frozenset(('int8', 'float32'))):
        self.path = Path(directory)
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            (self.path / name).write_bytes(b'synthetic fixture, never loaded as weights')
        self.gpu = gpu or Model()
        self.cpu = cpu or Model()
        self.gpu_error, self.cpu_error = gpu_error, cpu_error
        self.supported = supported
        self.resolutions, self.loads, self.capabilities = [], [], []
    def resolve(self, name, **options):
        self.resolutions.append((name, options))
        assert name == 'small' and options == {'local_files_only': True}
        return str(self.path)
    def factory(self, path, **options):
        self.loads.append((path, options))
        assert path == str(self.path.resolve())
        assert options['local_files_only'] is True
        if options['device'] == 'cuda':
            assert options['compute_type'] == 'float16'
            if self.gpu_error: raise self.gpu_error
            return self.gpu
        assert options['device'] == 'cpu'
        if self.cpu_error: raise self.cpu_error
        return self.cpu
    def compute_types(self, device):
        self.capabilities.append(device)
        assert device == 'cpu'
        return self.supported
    def load(self):
        return load_speech_runtime('small', model_factory=self.factory,
            cache_resolver=self.resolve, compute_types=self.compute_types)


def unavailable(action, reason):
    try:
        action()
    except SpeechUnavailableError as error:
        assert error.reason == reason
        assert 'SECRET' not in str(error) and 'cache' not in str(error)
        return error
    raise AssertionError('Expected sanitized speech failure')


def main():
    assert callable(getattr(__import__('speech_runtime').SpeechRuntime, 'transcribe_preview', None)), \
        'Provisional recognition needs a path that cannot load a CPU model'
    with tempfile.TemporaryDirectory(prefix='.speech-runtime-', dir=Path(__file__).resolve().parents[1]) as folder:
        f = Fixture(folder)
        runtime = f.load()
        assert runtime.status() == {'model': 'small', 'device': 'cuda',
            'compute_type': 'float16', 'fallback': False, 'state': 'ready', 'reason': ''}
        audio, prompt = object(), ['fixed vocabulary']
        result, info = runtime.transcribe(audio, language='en', initial_prompt=prompt)
        assert result == ['exact synthetic result'] and info == {'synthetic': True}
        assert f.gpu.calls == [(audio, {'language': 'en', 'initial_prompt': prompt})]
        assert not f.cpu.calls and not f.capabilities and len(f.loads) == len(f.resolutions) == 1
        snapshot = runtime.status()
        snapshot['device'] = 'tampered'
        assert runtime.status()['device'] == 'cuda'
        assert folder not in repr(runtime.status())

        # Preview errors must not load CPU, discard the live engine, or forward
        # internal scheduling controls to Faster Whisper. Final keeps its
        # existing recovery policy and always receives its original options.
        for device in ('cuda', 'cpu'):
            for failure in (ValueError('synthetic decoder error'),
                            RuntimeError('SECRET CUDA execution failed')):
                partial = Partial(failure)
                model = Model(lambda audio, options: (partial, None))
                f = Fixture(folder, gpu=model if device == 'cuda' else None,
                            cpu=model if device == 'cpu' else None,
                            gpu_error=RuntimeError('CUDA startup unavailable') if device == 'cpu' else None)
                runtime = f.load()
                loads_before = list(f.loads)
                reason = ('cuda_inference_unavailable' if device == 'cuda' and
                          isinstance(failure, RuntimeError) else 'transcription_failed')
                unavailable(lambda: runtime.transcribe_preview(audio, language='en', max_new_tokens=64), reason)
                assert partial.closed and not model.closed
                assert runtime.status()['state'] == 'ready' and f.loads == loads_before
                assert model.calls[-1][1] == {'language': 'en', 'max_new_tokens': 64}
                # A generic decoder problem can recover in place on the next
                # final call; a known GPU failure is retired by the parent.
                if reason == 'transcription_failed':
                    model.behavior = None
                    assert runtime.transcribe(audio, language='en')[0] == ['exact synthetic result']
                    assert f.loads == loads_before
        f = Fixture(folder)
        runtime = f.load()
        for action in (runtime.transcribe, runtime.transcribe_preview):
            for reserved in ('preview', 'allow_fallback'):
                try:
                    action(audio, **{reserved: False})
                except ValueError:
                    pass
                else:
                    raise AssertionError('Internal controls leaked into model options')
        assert not f.gpu.calls

        # Startup fallback uses the identical resolved path and a supported CPU
        # precision. Resolving by model name again could select another snapshot.
        for supported, precision in (({'int8', 'float32'}, 'int8'), ({'float32'}, 'float32')):
            f = Fixture(folder, gpu_error=RuntimeError('SECRET CUDA driver failure'), supported=supported)
            runtime = f.load()
            assert runtime.status()['device'] == 'cpu'
            assert runtime.status()['compute_type'] == precision
            assert runtime.status()['reason'] == 'cuda_startup_unavailable'
            assert runtime.status()['fallback'] and runtime.status()['state'] == 'ready'
            assert len(f.resolutions) == 1 and f.capabilities == ['cpu']
            assert [options['device'] for _, options in f.loads] == ['cuda', 'cpu']
            assert f.loads[0][0] == f.loads[1][0]
            runtime.transcribe(audio)
            runtime.transcribe(audio)
            assert len(f.loads) == 2 and len(f.cpu.calls) == 2 and not f.gpu.calls
        f = Fixture(folder, gpu_error=RuntimeError('SECRET GPU failure'), cpu_error=RuntimeError('SECRET CPU failure'))
        captured = io.StringIO()
        with redirect_stdout(captured), redirect_stderr(captured):
            unavailable(f.load, 'cpu_load_failed')
        assert captured.getvalue() == '' and len(f.loads) == 2
        f = Fixture(folder, gpu_error=RuntimeError('GPU failure'), supported=set())
        unavailable(f.load, 'cpu_load_failed')
        assert len(f.loads) == 1

        # Incomplete or unreadable local files fail before any constructor can
        # invoke Faster Whisper's otherwise independent tokenizer download path.
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            for damage in ('missing', 'empty', 'directory'):
                f = Fixture(folder)
                asset = Path(folder) / name
                asset.unlink()
                if damage == 'empty': asset.write_bytes(b'')
                if damage == 'directory': asset.mkdir()
                unavailable(f.load, 'cache_invalid')
                assert not f.loads
                if damage == 'directory': asset.rmdir()
        f = Fixture(folder)
        def missing(name, **options):
            assert options == {'local_files_only': True}
            raise OSError('SECRET local cache unavailable')
        unavailable(lambda: load_speech_runtime('small', model_factory=f.factory,
            cache_resolver=missing, compute_types=f.compute_types), 'cache_unavailable')
        assert not f.loads
        f = Fixture(folder)
        original_open = Path.open
        def unreadable(path, *args, **kwargs):
            if path.name == 'tokenizer.json':
                raise PermissionError('SECRET tokenizer cannot be read')
            return original_open(path, *args, **kwargs)
        with patch.object(Path, 'open', unreadable):
            unavailable(f.load, 'cache_invalid')
        assert not f.loads
        unavailable(lambda: load_speech_runtime('C:/SECRET/cache', model_factory=f.factory,
            cache_resolver=f.resolve, compute_types=f.compute_types), 'invalid_model_name')

        # Revalidate before each constructor without resolving again. GPU load
        # failure or later cache damage must not enable a tokenizer download.
        f = Fixture(folder, gpu_error=RuntimeError('CUDA unavailable'))
        factory = f.factory
        def remove_tokenizer(path, **options):
            if options['device'] == 'cuda':
                (Path(path) / 'tokenizer.json').unlink()
            return factory(path, **options)
        unavailable(lambda: load_speech_runtime('small', model_factory=remove_tokenizer,
            cache_resolver=f.resolve, compute_types=f.compute_types), 'cache_invalid')
        assert len(f.resolutions) == len(f.loads) == 1

        # A lazy GPU iterator can fail after yielding. Its prefix is closed and
        # discarded; CPU sees the same audio/options once and supplies all text.
        partial = Partial(RuntimeError('SECRET cublas64_12.dll CUDA execution failed'))
        f = Fixture(folder, gpu=Model(lambda audio, options: (partial, 'GPU info')))
        runtime = f.load()
        result, info = runtime.transcribe(audio, language='en', initial_prompt=prompt, beam_size=1)
        assert result == ['exact synthetic result'] and info == {'synthetic': True}
        assert partial.closed and f.gpu.closed
        assert f.gpu.calls[0][0] is f.cpu.calls[0][0] is audio
        assert f.gpu.calls[0][1] == f.cpu.calls[0][1]
        assert f.cpu.calls[0][1]['initial_prompt'] is prompt
        assert runtime.status()['reason'] == 'cuda_inference_unavailable'
        runtime.transcribe(audio, language='en')
        assert len(f.gpu.calls) == 1 and len(f.cpu.calls) == 2 and len(f.loads) == 2

        # Constructor success does not imply eager transcribe success either.
        def cuda_error(audio, options): raise OSError('CUDA driver unavailable')
        f = Fixture(folder, gpu=Model(cuda_error))
        assert f.load().transcribe(audio)[0] == ['exact synthetic result']
        assert len(f.loads) == 2
        f = Fixture(folder, gpu=Model(cuda_error), cpu_error=OSError('SECRET CPU construction failed'))
        runtime = f.load()
        unavailable(lambda: runtime.transcribe(audio), 'cpu_load_failed')
        unavailable(lambda: runtime.transcribe(audio), 'cpu_load_failed')
        assert runtime.status()['state'] == 'failed' and len(f.loads) == 2
        f = Fixture(folder, gpu=Model(cuda_error))
        runtime = f.load()
        (f.path / 'tokenizer.json').unlink()
        unavailable(lambda: runtime.transcribe(audio), 'cache_invalid')
        unavailable(lambda: runtime.transcribe(audio), 'cache_invalid')
        assert runtime.status()['state'] == 'failed' and runtime.status()['reason'] == 'cache_invalid'
        assert len(f.loads) == len(f.resolutions) == 1 and f.gpu.closed
        f = Fixture(folder, gpu=Model(cuda_error))
        unloaded = []
        f.gpu.model = SimpleNamespace(unload_model=lambda: unloaded.append(True))
        runtime = f.load()
        assert runtime.transcribe(audio)[0] == ['exact synthetic result']
        assert unloaded == [True] and not f.gpu.closed

        # Wrong input, schema/filter-like errors, and nonnative exceptions are
        # not evidence of accelerator failure, even if a message mentions CUDA.
        for error in (ValueError('CUDA data parse error'), RuntimeError('invalid audio shape'),
                      OSError('SECRET file permission denied')):
            partial = Partial(error)
            f = Fixture(folder, gpu=Model(lambda audio, options: (partial, None)))
            runtime = f.load()
            unavailable(lambda: runtime.transcribe(audio), 'transcription_failed')
            assert partial.closed and len(f.loads) == 1 and not f.cpu.calls
            assert runtime.status()['device'] == 'cuda' and runtime.status()['state'] == 'ready'

        # CPU failure is terminal for this runtime; a new recording cannot
        # reload models, retry the failed audio, or produce partial results.
        def cpu_error(audio, options): raise RuntimeError('SECRET CPU inference failed')
        for gpu_error in (True, False):
            gpu = Model(cuda_error) if not gpu_error else None
            f = Fixture(folder, gpu=gpu, gpu_error=RuntimeError('CUDA unavailable') if gpu_error else None,
                        cpu=Model(cpu_error))
            runtime = f.load()
            unavailable(lambda: runtime.transcribe(audio), 'cpu_inference_failed')
            unavailable(lambda: runtime.transcribe(audio), 'cpu_inference_failed')
            assert runtime.status()['state'] == 'failed' and len(f.loads) == 2
            assert len(f.cpu.calls) == 1

        # Status must remain available during inference and switching, while
        # model work is serialized without any app/workspace locks or callbacks.
        entered, release, second_attempted, second_entered = (threading.Event() for _ in range(4))
        active, results, errors = [], [], []
        def blocked(audio, options):
            active.append(audio)
            if len(active) == 1:
                entered.set()
                assert release.wait(3)
            else: second_entered.set()
            return iter(('serialized',)), None
        f = Fixture(folder, gpu=Model(blocked))
        runtime = f.load()
        def infer(second=False):
            try:
                if second: second_attempted.set()
                results.append(runtime.transcribe(audio))
            except Exception as error: errors.append(error)
        first = threading.Thread(target=infer)
        second = threading.Thread(target=lambda: infer(True))
        first.start()
        assert entered.wait(3)
        second.start()
        assert second_attempted.wait(3)
        status_done = threading.Event()
        reader = threading.Thread(target=lambda: (runtime.status(), status_done.set()))
        reader.start()
        try:
            assert status_done.wait(.5), 'Status waited for the model lock'
            assert not second_entered.wait(.1), 'Concurrent model inference escaped serialization'
        finally:
            release.set()
            first.join(3); second.join(3); reader.join(3)
        assert not errors and len(results) == 2 and all(r[0] == ['serialized'] for r in results)
        assert all(not thread.is_alive() for thread in (first, second, reader))

        entered, release = threading.Event(), threading.Event()
        f = Fixture(folder, gpu=Model(cuda_error))
        factory = f.factory
        def blocking_cpu(path, **options):
            if options['device'] == 'cpu':
                entered.set()
                assert release.wait(3)
            return factory(path, **options)
        runtime = load_speech_runtime('small', model_factory=blocking_cpu,
            cache_resolver=f.resolve, compute_types=f.compute_types)
        results, errors = [], []
        worker = threading.Thread(target=infer)
        worker.start()
        assert entered.wait(3)
        try:
            status = runtime.status()
            assert status['state'] == 'switching' and status['device'] == 'cpu'
            assert status['reason'] == 'cuda_inference_unavailable'
        finally:
            release.set()
            worker.join(3)
        assert not worker.is_alive() and not errors and runtime.status()['state'] == 'ready'
    print('speech runtime: same cached model, bounded CPU recovery, exact lazy retry, sanitized failures and nonblocking serialized status passed')


if __name__ == '__main__':
    main()
