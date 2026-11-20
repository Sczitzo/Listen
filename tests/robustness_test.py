"""Data-loss guards: a failing formatter, refine or history save must never lose dictated words."""
import contextlib
import io
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d

api = d.Api()
folder = tempfile.mkdtemp()
d.HISTORY = os.path.join(folder, 'history.jsonl')
d.SETTINGS_FILE = os.path.join(folder, 'settings.json')
d._chime = lambda *a: None
d.show_window = lambda *a, **k: None
words = 'ROBUST_MARKER Marcus needs 12 files by Friday'
segments = [type('Segment', (), {'text': words, 'avg_logprob': -.1, 'no_speech_prob': .01})()]
model = type('Model', (), {'transcribe': lambda *args, **kwargs: (segments, None)})()
chunks = [d.np.full((16000, 1), .05, dtype='float32')]
sent = lambda *a: {'status': 'sent', 'message': 'ok'}
quiet = contextlib.redirect_stdout(io.StringIO())


def run():
    d.SETTINGS['cleanup'] = True
    d.process(chunks, 1, d.time.perf_counter(), 123)


# 1. A formatter bug must fall back to the raw words, not raise.
with patch.object(d, 'model', model), patch.object(d, '_chat', lambda *a, **k: words + '.'), \
     patch.object(d, 'paste_text', sent), \
     patch.object(d, 'prepare_spoken_list', side_effect=ValueError('boom')), \
     patch.object(d, 'layout_explicit_lists', side_effect=ValueError('boom')), quiet:
    assert d.refine(words).startswith(words)
    run()
assert any(r['raw'] == words for r in api.get_history()), 'formatter failure lost the dictation'

# 2. If anything after transcription raises, the raw words are retained for recovery.
before = len(api.get_retained())
with patch.object(d, 'model', model), patch.object(d, 'refine', side_effect=RuntimeError('boom')), \
     patch.object(d, 'paste_text', sent), quiet:
    try:
        run()
    except RuntimeError:
        pass
    else:
        raise AssertionError('error was swallowed')
kept = api.get_retained()
assert len(kept) == before + 1 and kept[-1]['text'] == words, 'raw words not retained after a failure'

# 3. A history write failure after a successful paste must not raise or report a failed dictation.
pasted = []
with patch.object(d, 'model', model), patch.object(d, '_chat', lambda *a, **k: words + '.'), \
     patch.object(d, 'paste_text', lambda *a: pasted.append(a) or sent()), \
     patch.object(d, '_append_history', side_effect=OSError('disk full')), quiet:
    run()
assert len(pasted) == 1, 'input must be attempted exactly once, never replayed'
assert 'history' in d._last_error.lower() and 'Dictation failed' not in d._last_error, d._last_error

# 4. A microphone that never finishes opening must not leave the app silently "recording".
d._state = 'recording'; d._recording.set(); d._mic_ready = False
with quiet:
    d._mic_watchdog(d._gen)
assert not d._recording.is_set() and 'Microphone' in d._last_error
d._state = 'recording'; d._recording.set(); d._mic_ready = True  # an open mic is left alone
with quiet:
    d._mic_watchdog(d._gen)
assert d._recording.is_set()
d._mic_ready = False; d._recording.clear(); d._state = 'idle'

# 5. Local model calls bypass system proxies. The explicit cloud adapter has its own route checks.
src = Path(d.__file__).read_text(encoding='utf-8')
cloud_start = src.index('def _call_prompt_provider(')
cloud_end = src.index('\ndef engineer(', cloud_start)
cloud_adapter = src[cloud_start:cloud_end]
assert cloud_adapter.count('requests.post(') == 1 and 'allow_redirects=False' in cloud_adapter
local_src = src[:cloud_start] + src[cloud_end:]
sites = local_src.count('requests.post(') + local_src.count('requests.get(')
assert sites == 3 and local_src.count('proxies=_NO_PROXY') == 3, sites
os.environ['HTTP_PROXY'] = 'http://proxy.invalid:9'
session = d.requests.Session()
assert session.merge_environment_settings('http://127.0.0.1:11434/', {}, None, None, None)['proxies'].get('http')
assert not session.merge_environment_settings('http://127.0.0.1:11434/', d._NO_PROXY, None, None, None)['proxies'].get('http')
del os.environ['HTTP_PROXY']

# 6. A half-typed correction is un-counted when finished: one correction counts once, not once per autosave.
d.LEARNED_FILE = os.path.join(folder, 'learned.json')
d._learned = None
d.SETTINGS['learn'] = True
api.new_draft(); d._lab_add(d._lab_rev("Goal: fix the sono importer.", "engineer", "x", 0))
api.set_text("Goal: fix the Sun importer.")
api.set_text("Goal: fix the Suno importer.")
api.set_text("Goal: fix the Suno importer now.")
assert d.learned()["replacements"] == {"Suno": 1}, d.learned()["replacements"]

# 7. Retention must survive its own failures and respect Clear.
before = len(api.get_retained())
with patch.object(d, 'model', model), patch.object(d, 'refine', side_effect=RuntimeError('boom')),      patch.object(d, 'persist_workspace', side_effect=OSError('disk full')), patch.object(d, 'paste_text', sent), quiet:
    try:
        run()
    except RuntimeError:
        pass  # the original error still surfaces, not the OSError
    else:
        raise AssertionError('error was swallowed')
assert len(api.get_retained()) == before + 1 and 'not confirmed' in d._last_error, d._last_error


def clear_then_fail(*args, **kwargs):
    d._recovery_generation += 1  # the owner pressed Clear while this dictation was in flight
    raise RuntimeError('boom')


before = len(api.get_retained())
with patch.object(d, 'model', model), patch.object(d, 'refine', clear_then_fail), patch.object(d, 'paste_text', sent), quiet:
    try:
        run()
    except RuntimeError:
        pass
assert len(api.get_retained()) == before, 'Clear was overlapped: old text must not come back'

# Every list-formatter call goes through the two guarded wrappers (cache path, refine and its fallback).
assert src.count('prepare_spoken_list(') == 1 and src.count('layout_explicit_lists(') == 1

# 8. Tray favorite: same clipboard guard, and count/cue only on success.
events = []
fav = {'name': 'n', 'text': 'T'}
with patch.object(d.pyperclip, 'copy', lambda text: events.append(('copy', text))),      patch.object(d, 'bump_favorite', lambda name: events.append(('bump', name))),      patch.object(d, '_chime', lambda name: events.append(('chime', name))):
    with patch.object(d.clipboard_delivery, 'pending_status', lambda: {'pending': True}):
        assert d.copy_favorite(fav) is False
    assert events == [('chime', 'error')], events
    events.clear()
    with patch.object(d.clipboard_delivery, 'pending_status', lambda: {'pending': False}):
        assert d.copy_favorite(fav) is True
    assert events == [('copy', 'T'), ('bump', 'n'), ('chime', 'copied')], events

# 9. A hung microphone opener must not pile up threads: a new capture is refused while one is still opening.
from types import SimpleNamespace
d._state = 'idle'; d._recording.clear()
d._open_thread = SimpleNamespace(is_alive=lambda: True)
with quiet:
    d._start(target=123)
assert not d._recording.is_set() and 'still opening' in d._last_error, d._last_error
d._open_thread = None
print('robustness: all checks passed')
