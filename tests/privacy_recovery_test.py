"""Integration checks with real app functions and fake model/clipboard boundaries."""
import contextlib
import datetime
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d
import target_icon

api = d.Api()
folder = tempfile.mkdtemp()
d.HISTORY = os.path.join(folder, 'history.jsonl')
d.SETTINGS_FILE = os.path.join(folder, 'settings.json')
d._chime = lambda *a: None
private = 'PRIVATE_MARKER_847921 do not send Marcus 12 files'
segments = [type('Segment', (), {'text': private, 'avg_logprob': -.1, 'no_speech_prob': .01})()]
model = type('Model', (), {'transcribe': lambda *args, **kwargs: (segments, None)})()
output = io.StringIO()
target = SimpleNamespace(hwnd=27, pid=123, field_token='PRIVATE_FIELD_TOKEN')
executable = r'C:\Private_User_Profile\Programs\Code.exe'
closed = []
owners = iter((target.pid, target.pid, target.pid, target.pid, target.pid, target.pid + 1))

def window_owner(hwnd, process):
    assert hwnd == target.hwnd
    process._obj.value = next(owners)
    return 1

def process_name(handle, flags, path, length):
    assert (handle, flags) == (55, 0) and length._obj.value >= len(executable)
    path.value = executable
    return True

with patch.object(target_icon.ctypes, 'WinDLL', lambda name: name), \
     patch.object(target_icon, '_api', side_effect=lambda dll, symbol, *args: {
         'GetWindowThreadProcessId': window_owner,
         'OpenProcess': lambda access, inherit, pid: 55 if (access, inherit, pid) == (0x1000, False, target.pid) else None,
         'QueryFullProcessImageNameW': process_name,
         'CloseHandle': closed.append,
     }[symbol]), patch.object(target_icon, 'window_icon', side_effect=lambda *args: Image.new('RGB', (32, 32), 'red')) as icon:
    app = target_icon.history_app(target.hwnd, target.pid)
    icon.assert_called_once_with(target.hwnd, target.pid, 32, '#2d2925')
    icon.side_effect = OSError('synthetic icon failure')
    assert target_icon.history_app(target.hwnd, target.pid) == {'name': 'VS Code'}
    assert target_icon.history_app(target.hwnd, target.pid) is None
assert closed == [55, 55, 55] and app['name'] == 'VS Code' and app['icon'].startswith('iVBORw0KGgo')

with patch.object(d, 'model', model), patch.object(d, '_chat', lambda *a, **k: private + '.'), \
     patch.object(d, 'paste_text', lambda *a: {'status': 'retained', 'message': 'Destination changed; text retained.'}), \
     patch.object(d, 'history_app', return_value=app) as metadata, \
     contextlib.redirect_stdout(output):
    d.SETTINGS['cleanup'] = True
    chunks = [d.np.full((16000, 1), .05, dtype='float32')]
    d.process(chunks, 1, d.time.perf_counter(), target)
metadata.assert_called_once_with(target.hwnd, target.pid)
assert private not in output.getvalue(), 'Routine log exposed transcript'
assert api.get_retained()[-1]['text'].strip() == private + '.'
saved = api.get_history()[0]
assert saved['raw'] == private and saved['app'] == app and saved['delivery_status'] == 'retained'
assert set(saved['app']) == {'name', 'icon'}
assert 'Private_User_Profile' not in Path(d.HISTORY).read_text() and target.field_token not in Path(d.HISTORY).read_text()

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
assert 'app' not in api.get_history()[0]

d._append_history({**saved, 'text': 'damaged icon', 'app': {'name': 'VS Code', 'icon': 'iVBORw0KGgo='}})
d._append_history({**saved, 'text': 'damaged name', 'app': {'name': executable, 'icon': app['icon']}})
bad_name, bad_icon = api.get_history()[:2]
assert bad_name['text'] == 'damaged name' and 'app' not in bad_name
assert bad_icon['text'] == 'damaged icon' and bad_icon['app'] == {'name': 'VS Code'}
with patch('safe_storage.Image.open') as decode_icon:
    api.get_stats()
    decode_icon.assert_not_called()

with patch.object(d, 'transcribe', return_value='Metadata failure stays recoverable'), \
     patch.object(d, 'refine', side_effect=lambda raw: raw), \
     patch.object(d, 'paste', return_value={'status': 'sent'}) as paste, \
     patch.object(d, 'history_app', side_effect=OSError('synthetic native failure')):
    d.process(chunks, 1, d.time.perf_counter(), target)
assert paste.call_count == 1
saved_without_metadata = api.get_history()[0]
assert saved_without_metadata['text'] == 'Metadata failure stays recoverable'
assert saved_without_metadata['delivery_status'] == 'sent' and 'app' not in saved_without_metadata

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
