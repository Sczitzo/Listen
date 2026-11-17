"""Real child-process regressions with fake local speech packages; no weights/input."""
from contextlib import contextmanager
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import io
import json
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if importlib.util.find_spec('numpy') is None:
    store = Path.home() / 'AppData/Local/Packages'
    for path in store.glob('PythonSoftwareFoundation.Python.3.13_*/LocalCache/local-packages/Python313/site-packages'):
        sys.path.append(str(path))
import numpy as np

spec = importlib.util.find_spec('speech_process')
assert spec is not None, 'Native speech still runs in the UI process; bounded speech isolation is missing'
import speech_process as speech
from speech_runtime import SpeechUnavailableError


FAKE_MODEL = '''
import os, socket, time
from pathlib import Path
from types import SimpleNamespace
MODE = {mode!r}
class WhisperModel:
    def __init__(self, path, device, compute_type, local_files_only):
        assert local_files_only and Path(path).resolve() == (Path(__file__).parent/'weights').resolve()
        assert (device,compute_type) in [('cuda','float16'),('cpu','int8')]
        self.device=device
        if device=='cuda' and MODE=='cuda_start_hang':
            while True: time.sleep(1)
        if device=='cuda' and MODE in ('cuda_fail','all_fail'):
            raise RuntimeError('synthetic CUDA unavailable')
        if device=='cpu' and MODE=='all_fail': raise RuntimeError('synthetic CPU unavailable')
    def transcribe(self,audio,**options):
        marker=int(audio[0])
        assert options.get('language')=='en'
        assert 'preview' not in options and 'allow_fallback' not in options
        def segments():
            if marker==7 and self.device=='cuda':
                while True: time.sleep(1)
            if marker==8:
                while True: time.sleep(1)
            if marker==9: os._exit(55)
            if marker==10 and self.device=='cuda':
                yield SimpleNamespace(text='DISCARD THIS GPU PREFIX',avg_logprob=-.1,no_speech_prob=.01)
                raise RuntimeError('synthetic CUDA inference failure')
            if marker==11: raise ValueError('synthetic invalid input')
            if marker==12: print('native-looking noise must not corrupt the wire',flush=True)
            if marker==13:
                assert 'ANTHROPIC_API_KEY' not in os.environ
                assert 'OPENAI_API_KEY' not in os.environ
                try: socket.getaddrinfo('example.com',443)
                except PermissionError: pass
                else: raise AssertionError('Network was not blocked')
                try: (Path(__file__).parent/'forbidden-write').write_text('must never persist')
                except PermissionError: pass
                else: raise AssertionError('A child Python write escaped')
            if marker==14:
                if MODE=='slow_cpu_final': time.sleep(.35)
                assert self.device=='cpu' and len(audio)==48000
                assert float(audio[24000])==.625 and float(audio[-1])==.375
                assert options==dict(language='en',initial_prompt='Exact vocabulary')
                yield SimpleNamespace(text='Complete final including last words.',avg_logprob=-.2,
                                      no_speech_prob=.02,compression_ratio=1.1)
                return
            text='First isolated sentence.' if marker==1 else 'Second isolated sentence.'
            yield SimpleNamespace(text=text,avg_logprob=-.2,no_speech_prob=.02,compression_ratio=1.1)
        return segments(),None
    def close(self): pass
'''


@contextmanager
def fixture(mode='normal'):
    with tempfile.TemporaryDirectory(prefix='.speech-child-', dir=ROOT) as folder:
        base = Path(folder)
        package = base / 'faster_whisper'
        package.mkdir()
        (package / '__init__.py').write_text(FAKE_MODEL.format(mode=mode), encoding='utf-8')
        (package / 'utils.py').write_text(
            "from pathlib import Path\nimport time\n"
            "def download_model(name,local_files_only):\n"
            " assert name=='small' and local_files_only is True\n"
            + (" while True: time.sleep(1)\n" if mode == 'cache_hang' else '')
            + " return str(Path(__file__).parent/'weights')\n", encoding='utf-8')
        (base / 'ctranslate2.py').write_text("def get_supported_compute_types(device):\n assert device=='cpu'\n return {'int8','float32'}\n", encoding='utf-8')
        weights = package / 'weights'
        weights.mkdir()
        for name in ('model.bin', 'config.json', 'tokenizer.json'):
            (weights / name).write_text('synthetic fixture, never loaded as weights', encoding='utf-8')
        sys.path.insert(0, folder)
        try:
            yield base
        finally:
            sys.path.remove(folder)


def audio(marker):
    value = np.ones(1600, dtype=np.float32)
    value[0] = marker
    return value


def unavailable(action):
    try:
        action()
    except SpeechUnavailableError as error:
        assert 'synthetic' not in str(error) and 'SECRET' not in str(error)
        return
    raise AssertionError('An unavailable isolated engine returned a result')


def finished(runtime):
    session = runtime._session
    assert runtime.close(), 'Owned model child did not exit'
    assert runtime.close(), 'Close must be idempotent'
    assert session is None or session.process.poll() is not None


def wire_fixture(base, mode):
    script = base / 'wire_child.py'
    script.write_text('''
import sys,time
from speech_process import _read_frame,_write_frame
incoming=sys.stdin.buffer;out=sys.stdout.buffer
request,_=_read_frame(incoming)
device=request['device']
status={'model':'small','device':device,'compute_type':'float16' if device=='cuda' else 'int8',
        'fallback':device=='cpu','state':'ready','reason':''}
def send(kind,request_id=None,**values):
 _write_frame(out,dict(values,kind=kind,session=request['session'],
                     request_id=request['request_id'] if request_id is None else request_id))
send('snapshot',snapshot=SNAPSHOT)
send('ready',status=status)
if MODE=='backpressure':
 while True: time.sleep(1)
request,audio=_read_frame(incoming)
if MODE=='bad_id':
 send('result',request_id=request['request_id']-1,status=status,
      segments=[{'text':'STALE SECRET TEXT','avg_logprob':0,'no_speech_prob':0,'compression_ratio':0}])
elif MODE=='partial_frame':
 out.write(b'\\x20\\x00\\x00\\x00partial');out.flush()
while True: time.sleep(1)
'''.replace('SNAPSHOT', repr(str((base/'faster_whisper/weights').resolve())))
       .replace('MODE', repr(mode)), encoding='utf-8')
    return script


def main():
    assert callable(getattr(speech.SpeechProcessRuntime, 'transcribe_preview', None)), \
        'A provisional timeout must not spend CPU recovery on preview text'
    calls, entered, release = [], threading.Event(), threading.Event()
    class Kernel:
        def CloseHandle(self, handle):
            calls.append(handle); entered.set(); assert release.wait(3)
    job = speech._WindowsJob.__new__(speech._WindowsJob)
    job.kernel, job.handle, job._handle_lock = Kernel(), 123, threading.Lock()
    first = threading.Thread(target=job.close)
    second = threading.Thread(target=job.close)
    try:
        first.start(); assert entered.wait(1)
        second.start(); second.join(.2)
        assert calls == [123], 'An overlapping close released the OS handle twice'
    finally:
        release.set(); first.join(3); second.join(3)

    class ShortWriter:
        def __init__(self): self.data = bytearray()
        def write(self, value): self.data.extend(value[:2]); return min(2, len(value))
        def flush(self): pass
    writer = ShortWriter()
    speech._write_frame(writer, {'kind':'synthetic','audio_bytes':4}, b'1234')
    assert speech._read_frame(io.BytesIO(writer.data)) == ({'kind':'synthetic','audio_bytes':4}, b'1234'), 'A partial pipe write truncated the request'

    with fixture(), patch.dict(os.environ, ANTHROPIC_API_KEY='SECRET', OPENAI_API_KEY='SECRET'):
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=2)
        try:
            state = runtime.status()
            assert state == {'model':'small','device':'cuda','compute_type':'float16',
                             'fallback':False,'state':'ready','reason':''}
            first, info = runtime.transcribe(audio(1), language='en', beam_size=1, vad_filter=True)
            second, _ = runtime.transcribe(audio(2), language='en', initial_prompt='Fixed vocabulary')
            assert [s.text for s in first] == ['First isolated sentence.']
            assert [s.text for s in second] == ['Second isolated sentence.']
            assert first[0].avg_logprob == -.2 and first[0].no_speech_prob == .02
            assert info is None
            state['device'] = 'tampered'
            assert runtime.status()['device'] == 'cuda'
            assert 'weights' not in repr(runtime.status())
            assert runtime.transcribe(audio(12), language='en')[0][0].text == 'Second isolated sentence.'
            assert runtime.transcribe(audio(13), language='en')[0][0].text == 'Second isolated sentence.'
        finally:
            finished(runtime)

    with fixture('cuda_start_hang'):
        started = time.monotonic()
        runtime = speech.load_speech_process('small', startup_timeout=1, inference_timeout=1)
        try:
            assert time.monotonic() - started < 5
            assert runtime.status()['device'] == 'cpu' and runtime.status()['fallback']
            assert runtime.transcribe(audio(1), language='en')[0][0].text == 'First isolated sentence.'
        finally:
            finished(runtime)

    with fixture('cuda_start_hang'):
        runtime = speech.SpeechProcessRuntime('small', startup_timeout=20)
        errors = []
        def initialize():
            try: runtime.initialize()
            except SpeechUnavailableError: errors.append('closed')
        initializer = threading.Thread(target=initialize)
        initializer.start()
        deadline = time.monotonic()+3
        while runtime._snapshot is None and time.monotonic()<deadline: time.sleep(.01)
        assert runtime._snapshot is not None, 'Initialization did not reach the constructor fixture'
        finished(runtime)
        initializer.join(3)
        assert not initializer.is_alive() and errors == ['closed']
        assert not runtime._cpu_attempted, 'Close raced into an unexpected CPU respawn'

    with fixture():
        runtime = speech.SpeechProcessRuntime('small', startup_timeout=3)
        ready, accept, stopping, stop = (threading.Event() for _ in range(4))
        original_call, original_stop = runtime._call, runtime._stop
        def gate_ready(*args, **kwargs):
            result = original_call(*args, **kwargs)
            if result['kind']=='ready': ready.set(); assert accept.wait(3)
            return result
        def gate_stop(*args, **kwargs):
            stopping.set(); assert stop.wait(3)
            return original_stop(*args, **kwargs)
        results, errors = [], []
        def initialize_while_closing():
            try: results.append(runtime.initialize())
            except SpeechUnavailableError: errors.append('closed')
        initializer = threading.Thread(target=initialize_while_closing)
        closer = threading.Thread(target=runtime.close)
        try:
            with patch.object(runtime, '_call', gate_ready), patch.object(runtime, '_stop', gate_stop):
                initializer.start(); assert ready.wait(3)
                closer.start(); assert stopping.wait(1)
                accept.set(); time.sleep(.05); stop.set()
                initializer.join(3); closer.join(3)
                assert not results and errors == ['closed'], 'Startup returned ready after Close began'
        finally:
            accept.set(); stop.set(); initializer.join(3); closer.join(3); finished(runtime)

    for mode in ('cache_hang', 'all_fail'):
        with fixture(mode):
            started = time.monotonic()
            unavailable(lambda: speech.load_speech_process('small', startup_timeout=1))
            assert time.monotonic() - started < 5, 'Failed startup left a blocked caller'

    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=.4)
        try:
            result, _ = runtime.transcribe(audio(7), language='en')
            assert [s.text for s in result] == ['Second isolated sentence.']
            assert runtime.status()['device'] == 'cpu'
            unavailable(lambda: runtime.transcribe(audio(8), language='en'))
            assert runtime.status()['state'] == 'failed'
            unavailable(lambda: runtime.transcribe(audio(1), language='en'))
        finally:
            finished(runtime)

    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=2)
        try:
            result, _ = runtime.transcribe(audio(10), language='en')
            assert [s.text for s in result] == ['Second isolated sentence.'], 'A partial GPU prefix leaked'
            assert runtime.status()['device'] == 'cpu'
            unavailable(lambda: runtime.transcribe(audio(11), language='en'))
            assert runtime.status()['state'] == 'failed'
            assert runtime._session.process.poll() is not None, 'A failed CPU engine was left running'
        finally:
            finished(runtime)

    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=2)
        try:
            unavailable(lambda: runtime.transcribe(audio(9), language='en'))
            assert runtime.status()['state'] == 'failed'
        finally:
            finished(runtime)

    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=20)
        errors = []
        def work():
            try: runtime.transcribe(audio(8), language='en')
            except SpeechUnavailableError: errors.append('closed')
        caller = threading.Thread(target=work)
        caller.start()
        time.sleep(.2)
        started = time.monotonic()
        assert runtime.status()['state'] in ('ready', 'switching')
        assert time.monotonic() - started < .1, 'Status waited on the native speech lock'
        finished(runtime)
        caller.join(3)
        assert not caller.is_alive() and errors == ['closed'], 'Close did not release inference'

    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=2)
        got, release = threading.Event(), threading.Event()
        original_get = runtime._session.events.get
        def gated_get(*args, **kwargs):
            response = original_get(*args, **kwargs)
            if response[0].get('kind') == 'result':
                got.set(); assert release.wait(3)
            return response
        results, errors = [], []
        def transcribe_during_close():
            try: results.append(runtime.transcribe(audio(1), language='en'))
            except SpeechUnavailableError: errors.append('closed')
        caller = threading.Thread(target=transcribe_during_close)
        try:
            with patch.object(runtime._session.events, 'get', gated_get):
                caller.start(); assert got.wait(3)
                assert runtime.close()
                release.set(); caller.join(3)
                assert not caller.is_alive() and not results and errors == ['closed'], 'A response raced past Close'
                assert runtime.status()['state'] == 'failed', 'A late result revived a closed decoder'
        finally:
            release.set(); caller.join(3); finished(runtime)

    with fixture():
        admitted = []
        def broken_session(process, job):
            admitted.append((process, job))
            raise RuntimeError('Synthetic reader-thread startup failure')
        try:
            with patch.object(speech, '_Session', broken_session):
                unavailable(lambda: speech.load_speech_process('small', startup_timeout=3))
            assert len(admitted)==1 and admitted[0][0].poll() is not None, 'Session startup failure orphaned a child'
        finally:
            for process, job in admitted:
                if process.poll() is None: process.terminate(); process.wait(timeout=3)
                job.close()
                for stream in (process.stdin, process.stdout, process.stderr): stream.close()

    for mode in ('bad_id', 'partial_frame', 'backpressure'):
        with fixture() as base, patch.object(speech, 'WORKER', wire_fixture(base, mode)):
            runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=.3)
            try:
                started = time.monotonic()
                samples = np.ones(2_000_000, dtype=np.float32) if mode == 'backpressure' else audio(1)
                unavailable(lambda: runtime.transcribe(samples, language='en'))
                assert time.monotonic()-started < 5, 'A blocked IPC writer escaped its deadline'
                assert runtime.status()['state']=='failed'
            finally:
                finished(runtime)

    if os.name == 'nt':
        # A real parent exits without atexit/close. Its OS Job must still stop
        # the decoder, including while native work could prevent Python cleanup.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with fixture() as base:
            owner = base / 'parent_owner.py'
            owner.write_text(
                "import os,sys\nfrom speech_process import load_speech_process\n"
                "runtime=load_speech_process('small',startup_timeout=3)\n"
                "print(runtime._session.process.pid,flush=True)\n"
                "sys.stdin.buffer.readline()\nos._exit(0)\n", encoding='utf-8')
            parent = subprocess.Popen([sys.executable, str(owner)], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=speech._child_environment(), creationflags=subprocess.CREATE_NO_WINDOW)
            child_handle = None
            try:
                line = parent.stdout.readline()
                assert line.strip().isdigit(), 'The ownership fixture failed to load'
                child_handle = kernel.OpenProcess(0x100000, False, int(line))
                assert child_handle, 'Could not observe the task-owned child handle'
                parent.stdin.write(b'exit\n'); parent.stdin.flush()
                assert parent.wait(timeout=3) == 0
                assert kernel.WaitForSingleObject(child_handle, 3000) == 0, 'Parent death left a decoder orphan'
            finally:
                if parent.poll() is None:
                    parent.terminate(); parent.wait(timeout=3)
                if child_handle: kernel.CloseHandle(child_handle)
                parent.stdin.close(); parent.stdout.close(); parent.stderr.close()

    preview_cases()
    print('Speech process: exact consecutive results, bounded startup/inference, same-cache CPU fallback, best-effort previews, no partial text, guards and owned-child cleanup passed')


def preview_cases():
    # Mode, rather than an incidental model token limit, owns the preview
    # deadline. No production child/model call is needed to observe admission.
    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3)
        try:
            with patch.object(runtime, '_transcribe_once', return_value=([], None)) as once:
                runtime.transcribe_preview(audio(1), language='en', max_new_tokens=64)
                assert once.call_args.args[2] == 15 and once.call_args.kwargs == {'preview': True}
                runtime.transcribe(audio(1), language='en', max_new_tokens=64)
                assert once.call_args.args[2] == 30.2 and once.call_args.kwargs == {'preview': False}
        finally:
            finished(runtime)
    # Ordinary decoder errors leave the same child usable, including on CPU.
    for device in ('cuda', 'cpu'):
        with fixture():
            runtime = speech.load_speech_process('small', preferred_device=device,
                                                startup_timeout=3, inference_timeout=3)
            first = runtime._session
            try:
                unavailable(lambda: runtime.transcribe_preview(audio(11), language='en', max_new_tokens=64))
                assert runtime.status()['state'] == 'ready' and runtime._session is first
                assert first.process.poll() is None
                assert [s.text for s in runtime.transcribe(audio(1), language='en')[0]] == ['First isolated sentence.']
                for action in (runtime.transcribe, runtime.transcribe_preview):
                    for key in ('preview', 'allow_fallback'):
                        try: action(audio(1), language='en', **{key: False})
                        except ValueError: pass
                        else: raise AssertionError('Reserved controls were accepted as native options')
            finally:
                finished(runtime)

    # A native GPU preview error, deadline or crash retires the old child,
    # without loading/retrying CPU until the next complete final recording.
    # An already-CPU deadline gets exactly one same-CPU reload at final.
    for device, marker in (('cuda', 10), ('cuda', 7), ('cuda', 9), ('cpu', 8)):
        with fixture('slow_cpu_final'):
            runtime = speech.load_speech_process('small', preferred_device=device,
                                                startup_timeout=3, inference_timeout=.2)
            first = runtime._session
            snapshot = runtime._snapshot
            try:
                with patch.object(runtime, '_initialize_child', wraps=runtime._initialize_child) as init:
                    unavailable(lambda: runtime.transcribe_preview(audio(marker), language='en', max_new_tokens=64))
                    assert init.call_count == 0, 'Optional preview performed model recovery'
                    assert first.process.poll() is not None, 'Pending recovery retained a live old child'
                    assert runtime._session is None
                    assert runtime.status()['state'] == 'switching'
                    assert runtime.status()['reason'] == 'preview_recovery_pending'
                    unavailable(lambda: runtime.transcribe_preview(audio(1), language='en'))
                    assert init.call_count == 0, 'Pending preview restarted the model'
                    # Only the deliberately hung preview needs a .2 s test
                    # deadline. A fresh CPU child still imports numpy after
                    # announcing readiness. Give the independent successful
                    # final 3 s; its .35 s fake work proves that reusing the
                    # preview's failure budget would incorrectly reject it.
                    runtime._inference_timeout = 3
                    final_audio = np.zeros(48000, dtype=np.float32)
                    final_audio[0], final_audio[24000], final_audio[-1] = 14, .625, .375
                    call_trace = []
                    actual_call = runtime._call
                    def trace_call(session, op, **kwargs):
                        started = time.monotonic()
                        row = {'operation': op, 'timeout': kwargs.get('timeout'),
                               'preview': kwargs.get('preview'), 'outcome': 'pending'}
                        call_trace.append(row)
                        try:
                            response = actual_call(session, op, **kwargs)
                            row['outcome'] = response.get('kind')
                            row['reason'] = response.get('reason')
                            return response
                        except Exception as error:
                            row['outcome'] = type(error).__name__
                            raise
                        finally:
                            row['elapsed'] = time.monotonic() - started
                    try:
                        with patch.object(runtime, '_call', trace_call):
                            result, _ = runtime.transcribe(final_audio, language='en', initial_prompt='Exact vocabulary')
                    except Exception:
                        print('Preview recovery diagnostic: ' + json.dumps({
                            'initial_device': device, 'marker': marker, 'calls': call_trace,
                            'state': runtime.status(), 'old_child_exited': first.process.poll() is not None}))
                        raise
                    assert [s.text for s in result] == ['Complete final including last words.']
                    assert init.call_count == 1 and init.call_args.args == ('cpu',)
                    assert runtime.status()['state'] == 'ready' and runtime.status()['device'] == 'cpu'
                    assert runtime._snapshot == snapshot
                    assert runtime._session is not first
            finally:
                finished(runtime)

    # Failed final recovery is terminal, never another automatic reload loop.
    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=.2)
        try:
            unavailable(lambda: runtime.transcribe_preview(audio(7), language='en'))
            with patch.object(runtime, '_initialize_child', wraps=runtime._initialize_child) as init:
                runtime._inference_timeout = 3  # test the decoder error, not cold-child scheduling
                unavailable(lambda: runtime.transcribe(audio(11), language='en'))
                assert runtime.status()['state'] == 'failed' and init.call_count == 1
                unavailable(lambda: runtime.transcribe(audio(1), language='en'))
                assert init.call_count == 1
        finally:
            finished(runtime)

    # Cancel/Quit while pending must not itself load a model or retry audio.
    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=.2)
        try:
            unavailable(lambda: runtime.transcribe_preview(audio(7), language='en'))
            with patch.object(runtime, '_initialize_child', side_effect=AssertionError('Close reloaded speech')):
                assert runtime.close()
                unavailable(lambda: runtime.transcribe(audio(1), language='en'))
                assert runtime.status()['state'] == 'failed' and runtime._session is None
        finally:
            finished(runtime)

    # Never replace a child whose stop did not establish process exit.
    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=.2)
        first = runtime._session
        try:
            with patch.object(runtime, '_stop', return_value=False), \
                 patch.object(runtime, '_initialize_child', side_effect=AssertionError('Unclosed child replaced')):
                unavailable(lambda: runtime.transcribe_preview(audio(7), language='en'))
                assert runtime.status()['state'] == 'failed' and runtime._session is first
                assert first.process.poll() is None
                unavailable(lambda: runtime.transcribe(audio(1), language='en'))
        finally:
            finished(runtime)

    # Close can win after preview retirement but before pending publication.
    with fixture():
        runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=.2)
        stopped, release = threading.Event(), threading.Event()
        real_stop = runtime._stop
        stop_calls = []
        def gated_stop(session):
            result = real_stop(session)
            stop_calls.append(True)
            if len(stop_calls) == 1:
                stopped.set(); assert release.wait(3)
            return result
        errors = []
        def preview():
            try: runtime.transcribe_preview(audio(7), language='en')
            except SpeechUnavailableError: errors.append('closed')
        caller = threading.Thread(target=preview)
        try:
            with patch.object(runtime, '_stop', gated_stop):
                caller.start(); assert stopped.wait(3)
                assert runtime.close()
                release.set(); caller.join(3)
                assert not caller.is_alive() and errors == ['closed']
                assert runtime.status()['state'] == 'failed' and runtime._session is None
        finally:
            release.set(); caller.join(3); finished(runtime)

    # Wire mode is a strict boolean, never a model option or truthy string.
    for invalid in ('true', 1, None):
        with fixture():
            runtime = speech.load_speech_process('small', startup_timeout=3, inference_timeout=.3)
            first = runtime._session
            try:
                try:
                    runtime._call(first, 'transcribe', timeout=.5, audio=audio(1).tobytes(),
                                  options={'language': 'en'}, preview=invalid)
                except EOFError:
                    pass
                else:
                    raise AssertionError('Malformed preview mode reached native inference')
                first.process.wait(timeout=3)
                assert first.process.returncode == 2
            finally:
                finished(runtime)


if __name__ == '__main__':
    main()
