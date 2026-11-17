"""Field-trial grammar/corrections through actual shared cleanup and preview."""
from pathlib import Path
import sys
import time
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d
from cleanup_policy import CleanupPolicy

CASES = (
    'I need a shopping list with five items Apples, bananas, a bag of chips, milk, and eggs.',
    'I need a shopping list with three items Bread, Milk, and Eggs Oh, and some ice cream',
    'I need a shopping list with 4 items, 2 apples, 3 bananas, 1 carton of milk, and 6 eggs',
    'Make a number list First, wash the vegetables Second, chop them Third, put them in the refrigerator',
    'Make a letter list of three options. Take the bus, ride a bicycle, or walk.',
    'I need a shopping list with three items, apples, bananas, scratch that oranges, and milk',
    'I need a shopping list with 4 items Apples, Bananas, Milk, and Eggs',
    'I need a shopping list with three items or so apples, bananas, actually scratch that oranges and milk',
    'I need a shopping list with three items or so Apples, Bananas Actually scratch that, oranges and milk',
    "Okay, I need a shopping list with three items or so. It's going to be apples, bananas, oranges, actually scratch that milk.",
)
d._quitting = False
d.SETTINGS.update(cleanup=True, learn=False)
with patch.object(d, 'system_prompt', return_value='Synthetic field-trial cleanup.'), \
     patch.object(d, 'vocab_words', return_value=[]):
    for raw in CASES:
        source = d.correction_source(raw)
        expected = d.layout_explicit_lists(source)
        assert expected != raw
        with patch.object(d.cleanup_http, 'post_json') as request:
            assert d.refine(raw) == expected, raw
        request.assert_not_called()
        assert d._last_cleanup_timing == {'outcome': 'local_list', 'request_ms': 0.0}

    correction = CASES[5]
    d.SETTINGS['cleanup'] = False
    with patch.object(d.cleanup_http, 'post_json', side_effect=AssertionError('Cleanup off')):
        assert d.refine(correction) == correction
    d.SETTINGS['cleanup'] = True

    # A model cannot delete an out-of-scope item merely because another list exists.
    out_of_scope = (
        'I saw apples, bananas, scratch that oranges. Make a shopping list with three items, milk, eggs, bread.',
        'Make a shopping list with three items, milk, eggs, bread. Later I saw apples, bananas, scratch that oranges.',
    )
    for raw in out_of_scope:
        d._cleanup_policy = CleanupPolicy()
        with patch.object(d.cleanup_http, 'post_json', return_value={
            'done_reason': 'stop', 'message': {'content': raw.replace('bananas, scratch that ', '')}}):
            assert d.refine(raw) == raw

    # Full recording/history keeps original source; only final text is corrected.
    d._cleanup_policy = CleanupPolicy()
    history, delivered = [], []
    with patch.object(d, 'transcribe', return_value=correction), \
         patch.object(d.cleanup_http, 'post_json') as request, \
         patch.object(d, '_append_history', side_effect=history.append), \
         patch.object(d, 'paste', side_effect=lambda text, target: delivered.append(text) or {'status': 'pasted'}):
        d.process([object()], 2, time.perf_counter(), generations=(d._history_generation, d._recovery_generation))
    request.assert_not_called()
    assert history[-1]['raw'] == correction
    assert history[-1]['text'] == d.layout_explicit_lists(d.correction_source(correction))
    assert delivered == [history[-1]['text'] + ' ']

# Preview uses the same approved local correction without weakening model safety/cache.
events = []
c = d.DraftCoordinator(lambda *args: events.append(args), lambda job: True,
    request=lambda payload, **kwargs: {'done': True, 'done_reason': 'stop',
                                      'message': {'content': payload['messages'][-1]['content']}}, interval=0)
try:
    job = d.DraftJob(1, 'synthetic', 1, 'fixed', 'Clean.', correction, 'same-model')
    assert c.offer(job)
    until = time.monotonic() + 3
    while c.busy:
        assert time.monotonic() < until
        time.sleep(.002)
    assert events[-1][1] == d.layout_explicit_lists(d.correction_source(correction))
    assert c.cached_whole(1, 'synthetic', 'fixed', correction) == correction
finally:
    assert c.close() and not c.alive
print('Field-trial integration: local final lists, preview, cleanup-off, scoped corrections and original raw history passed')
