"""Idle readiness gates without UIA, clipboard, threads or model calls."""
from pathlib import Path
import queue
import sys
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d

calls = []
fields = SimpleNamespace(ready=False, request_warmup=lambda: calls.append('warm'))
pending = [False]
route = SimpleNamespace(backend=SimpleNamespace(pending_status=lambda: {'pending': pending[0]}))
with patch.object(d, '_field_access', fields), patch.object(d, '_multiline', route), \
     patch.object(d.clipboard_delivery._backend, 'pending', None), patch.object(d, '_quitting', False):
    assert d._field_warmup_allowed()
    d._maybe_warm_field_access()
    assert calls == ['warm']
    fields.ready = True
    d._maybe_warm_field_access()
    assert calls == ['warm']
    fields.ready = False
    pending[0] = True
    assert not d._field_warmup_allowed()
    d._maybe_warm_field_access()
    assert calls == ['warm']
    pending[0] = False
    with patch.object(d.clipboard_delivery._backend, 'pending', {'opaque': 'legacy'}):
        assert not d._field_warmup_allowed()
        d._maybe_warm_field_access()
    assert calls == ['warm']
    with patch.object(d, '_quitting', True):
        d._maybe_warm_field_access()
    assert calls == ['warm']

    # Actual idle worker requests readiness, then exits. No field token is
    # manufactured and no capture/insert path is invoked by the warm tick.
    class Jobs:
        unfinished_tasks = 0
        def get(self, timeout): raise queue.Empty()
    with patch.object(d, '_jobs', Jobs()), patch.object(d, '_recording', SimpleNamespace(is_set=lambda: False)), \
         patch.object(d, '_preview', SimpleNamespace(in_flight=lambda: False)), \
         patch.object(d, '_maybe_preview', side_effect=lambda: setattr(d, '_quitting', True)):
        d.worker()
    assert calls == ['warm', 'warm']

print('Field recovery integration: idle warm, ready/closing/pending gates and no retroactive target/input passed')
