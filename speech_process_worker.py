"""Private local decoder child. It never imports the app or records user data."""
import os
from pathlib import Path
import sys

sys.dont_write_bytecode = True


def _guard():
    def audit(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
            raise PermissionError('Speech is local only')
        if event == 'open' and not isinstance(args[0], int):
            if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise PermissionError('The decoder never writes files')
        if event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.rename'):
            raise PermissionError('The decoder never changes files')
    sys.addaudithook(audit)


def _parent_lifetime(parent_pid):
    if os.name != 'nt':
        # Windows is owned by the parent's kill-on-close Job before init is
        # sent. Linux test/development children get the kernel's parent signal.
        import ctypes
        import signal
        libc = ctypes.CDLL(None)
        if libc.prctl(1, signal.SIGTERM) != 0 or os.getppid() != parent_pid:
            raise RuntimeError('Speech parent ownership unavailable')


def main():
    _guard()
    from speech_process import _read_frame, _write_frame
    # Preserve a dedicated wire descriptor, then send all Python/native library
    # console output to stderr (the parent discards it, including error text).
    wire = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    incoming = sys.stdin.buffer
    init, blob = _read_frame(incoming)
    session, request_id = init.get('session'), init.get('request_id')
    if (blob or init.get('op') != 'initialize' or not isinstance(session, str)
            or len(session) != 32 or type(request_id) is not int
            or type(init.get('parent_pid')) is not int):
        return 2
    _parent_lifetime(init['parent_pid'])
    current_id = request_id
    runtime = None

    def send(kind, **values):
        _write_frame(wire, dict(values, kind=kind, session=session, request_id=current_id))

    try:
        from speech_runtime import SpeechRuntime, SpeechUnavailableError, _cached_snapshot, _validate_snapshot
        model_name, device = init.get('model'), init.get('device')
        if device not in ('cuda', 'cpu'):
            raise ValueError('Unsupported speech device')
        if model_name == 'parakeet-unified-en-0.6b':
            from parakeet_runtime import cached_model, ParakeetRuntime
            snapshot = cached_model()
            if init.get('snapshot') not in (None, snapshot):
                raise SpeechUnavailableError('cache_invalid')
            send('snapshot', snapshot=snapshot)
            runtime = ParakeetRuntime(snapshot)
        else:
            from faster_whisper import WhisperModel
            from ctranslate2 import get_supported_compute_types
            snapshot = init.get('snapshot')
            if snapshot is None:
                from faster_whisper.utils import download_model
                snapshot = _cached_snapshot(model_name, download_model)
            else:
                if not isinstance(snapshot, str) or not Path(snapshot).is_absolute():
                    raise ValueError('Invalid pinned speech snapshot')
                _validate_snapshot(snapshot)
            send('snapshot', snapshot=snapshot)

            class ReportingRuntime(SpeechRuntime):
                def _publish(self, *args):
                    super()._publish(*args)
                    send('status', status=self.status())

            runtime = ReportingRuntime(model_name, snapshot, WhisperModel,
                                       get_supported_compute_types)
            if device == 'cuda':
                runtime._initialize()
            else:
                runtime._load_cpu(init.get('reason') or 'cuda_startup_unavailable')
        import numpy as np
        send('ready', status=runtime.status())
    except Exception as error:
        reason = error.reason if isinstance(error, SpeechUnavailableError) else 'speech_unavailable'
        values = {'reason': reason}
        if runtime is not None:
            values['status'] = runtime.status()
        send('error', **values)
        return 1

    while True:
        try:
            message, audio = _read_frame(incoming)
        except EOFError:
            return 0
        if (message.get('session') != session or type(message.get('request_id')) is not int
                or message['request_id'] <= current_id or message.get('op') != 'transcribe'):
            return 2
        current_id = message['request_id']
        preview = message.get('preview', False)
        if type(preview) is not bool:
            return 2
        try:
            options = message.get('options')
            if (not isinstance(options, dict) or len(audio) % 4 or not audio
                    or any(key in options for key in ('preview', 'allow_fallback'))):
                raise ValueError('Invalid speech request')
            samples = np.frombuffer(audio, dtype='<f4')
            if not np.isfinite(samples).all():
                raise ValueError('Invalid speech samples')
            transcribe = runtime.transcribe_preview if preview else runtime.transcribe
            segments, _ = transcribe(samples, **options)
            rows = [{'text': segment.text,
                     'avg_logprob': float(getattr(segment, 'avg_logprob', 0)),
                     'no_speech_prob': float(getattr(segment, 'no_speech_prob', 0)),
                     'compression_ratio': float(getattr(segment, 'compression_ratio', 0))}
                    for segment in segments]
            send('result', status=runtime.status(), segments=rows)
        except Exception as error:
            reason = error.reason if isinstance(error, SpeechUnavailableError) else 'transcription_failed'
            send('error', status=runtime.status(), reason=reason)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (EOFError, BrokenPipeError):
        raise SystemExit(0)
