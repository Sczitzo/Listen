"""Integration checks with real app functions and fake model/clipboard boundaries."""
import contextlib
import datetime
import io
import json
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
private = 'PRIVATE_MARKER_847921 do not send Marcus 12 files'
segments = [type('Segment', (), {'text': private, 'avg_logprob': -.1, 'no_speech_prob': .01})()]
model = type('Model', (), {'transcribe': lambda *args, **kwargs: (segments, None)})()
output = io.StringIO()
with patch.object(d, 'model', model), patch.object(d, '_chat', lambda *a, **k: private + '.'), \
     patch.object(d, 'paste_text', lambda *a: {'status': 'retained', 'message': 'Destination changed; text retained.'}), \
     contextlib.redirect_stdout(output):
    d.SETTINGS['cleanup'] = True
    chunks = [d.np.full((16000, 1), .05, dtype='float32')]
    d.process(chunks, 1, d.time.perf_counter(), 123)
assert private not in output.getvalue(), 'Routine log exposed transcript'
assert api.get_retained()[-1]['text'].strip() == private + '.'
assert api.get_history()[0]['raw'] == private

# Error values can include provider payloads: neither logs nor UI should echo them.
with patch.object(d, '_chat', side_effect=RuntimeError(private)), contextlib.redirect_stdout(output):
    assert d.refine(private) == private
assert private not in output.getvalue()

# Preserve a truncated tail and allow the next appended record to survive.
with open(d.HISTORY, 'ab') as stream:
    stream.write(b'{"broken":')
damaged = Path(d.HISTORY).read_bytes()
rows = api.get_history()
assert len(rows) == 1 and 'skipped' in d._last_error
assert Path(d.HISTORY).read_bytes() == damaged
d._append_history(dict(ts=datetime.datetime.now().isoformat(), kind='dictate', raw='new', text='new', secs=1, latency=0, cost=0))
assert [r['text'] for r in api.get_history()] == ['new', private + '.']

before = dict(d.SETTINGS)
with patch.object(d, 'atomic_write_json', side_effect=OSError('disk full')):
    try:
        api.set_setting('cleanup', not before['cleanup'])
    except OSError:
        pass
    else:
        raise AssertionError('Save failure suppressed')
assert d.SETTINGS == before
assert not api.set_setting([], True)
assert not api.save_favorite(None, 'text')
assert not api.copy_text(None)
print('privacy/recovery integration: passed')
