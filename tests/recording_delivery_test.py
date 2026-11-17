"""Deterministic recording delivery ordering and visible failure checks."""
import ast
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dictate as d

if os.environ.get('DELIVERY_BASELINE'):
    tree = ast.parse(Path(os.environ['DELIVERY_BASELINE']).read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_stop')
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<baseline _stop>', 'exec'), d.__dict__)


class DeferredThread:
    pending = []
    def __init__(self, target, args=(), **kwargs):
        self.target, self.args = target, args
    def start(self):
        self.pending.append(self)
    def run(self):
        self.target(*self.args)


d._quitting = False
d._chime = lambda *a: None
d._jobs = queue.Queue()
d._announced = False
d._pending_recordings = 0
d._record_generations = (0, 0)
closed = []
with patch.object(d.threading, 'Thread', DeferredThread), patch.object(d, '_close_mic', lambda gen: closed.append(gen)):
    for gen in (1, 2):
        d._gen = gen
        d._buf = [gen]
        d._record_target = gen + 100
        d._rec_t = d.time.perf_counter()
        d._recording.set()
        d._stop()
    assert d._jobs.qsize() == 2, 'Jobs must reserve stop order before either asynchronous close finishes'
    # Finish B first. It must not jump ahead of A in the processing queue.
    DeferredThread.pending[1].run()
    first = d._jobs.get_nowait()
    second = d._jobs.get_nowait()
    assert [first[0], second[0]] == [[1], [2]], 'Earlier dictation was overtaken'
    assert not first[5].is_set() and second[5].is_set()
    DeferredThread.pending[0].run()
    assert first[5].is_set() and d._pending_recordings == 0
    d._jobs.task_done(); d._jobs.task_done()

# Failed delivery creates durable recovery and an audible/visible attention path,
# without focusing another window or claiming successful insertion.
folder = tempfile.mkdtemp()
d.APP_DIR = folder
d._workspace_store = d._cost_ledger = None
del d._workspace_store; del d._cost_ledger
notifications, sounds = [], []
class Tray:
    def notify(self, message, title):
        notifications.append((message, title))
d._tray = Tray()
d._state = 'idle'; d._busy = ''
with patch.object(d, 'paste_text', lambda *a: {'status': 'retained', 'message': 'Destination changed; text retained.'}), \
     patch.object(d, '_chime', lambda name: sounds.append(name)):
    result = d.paste('LATEST synthetic dictation', 123)
assert result['status'] == 'retained'
assert d._retained[-1]['text'] == 'LATEST synthetic dictation'
assert not notifications and sounds == ['error'] and d.ui_state() == 'error'

# A delayed frontend submission cannot be admitted to the replacement draft.
api = d.Api()
api.new_draft(); context = api.get_lab()['draft_id']
api.new_draft()
with patch.object(d, 'lab_run', side_effect=AssertionError('Stale submission reached operation admission')):
    assert not api.submit('old draft instruction', context)
print('recording delivery: ordering, recovery attention and stale admission passed')

# A failed native hide cannot strand the manual-paste reservation or authored text.
d._window_target = 123
d._pending_pastes = 0
class BrokenWindow:
    def hide(self): raise OSError('synthetic window failure')
with patch.object(d, '_win', BrokenWindow()), patch.object(d, 'bump_favorite', lambda *a: None), patch.object(api, 'lab_kept', lambda: True):
    assert not api.paste_draft('MANUAL synthetic text')
assert d._pending_pastes == 0
assert d._retained[-1]['text'] == 'MANUAL synthetic text'
print('manual delivery hide failure: no leaked reservation, text retained')
