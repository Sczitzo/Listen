"""Actual list fallback integration; guarded runner, synthetic transport only."""
from pathlib import Path
import queue
import sys
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
assert ROOT.name.startswith('.test-run-'), 'Use tests/run_tests.py --no-devices'
sys.path.insert(0, str(ROOT))
import dictate as d
from cleanup_policy import CleanupPolicy
from spoken_format import formatting_is_safe, layout_explicit_lists

RAW = 'I need to make a shopping list with five items. One is apple, I need a banana, bag of chips, milk and eggs.'
UNSAFE = 'I need to make a shopping list with five items: apple, banana, bag of chips, milk, and eggs.'
LAYOUT = layout_explicit_lists(RAW)
assert LAYOUT != RAW and LAYOUT.count('\n- ') == 5
assert formatting_is_safe(RAW, LAYOUT)


def wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, 'Synthetic coordinator did not settle'
        time.sleep(.002)


def reset():
    assert d._draft.close(timeout=2) and not d._draft.alive
    d._draft = d.DraftCoordinator(d._publish_draft, d._valid_draft, interval=0)
    d._cleanup_policy = CleanupPolicy()
    d.SETTINGS.update(cleanup=True, live_preview=True, learn=False)
    d._llm_ok = False
    d._last_cleanup_timing = {}
    d._quitting = False
    d._history_generation = d._recovery_generation = 0
    d._jobs = queue.Queue()
    d._recording.clear()
    for name in ('settings', 'history_generation', 'recovery_generation', 'preview_generation', 'timings'):
        if hasattr(d._lab_thread, name):
            delattr(d._lab_thread, name)


try:
    with patch.object(d, 'system_prompt', return_value='Synthetic list cleanup.'), \
         patch.object(d, 'vocab_words', return_value=[]):
        for online in (False, True):
            reset()
            d._llm_ok = online
            with patch.object(d.cleanup_http, 'post_json') as request:
                assert d.refine(RAW) == LAYOUT
            request.assert_not_called()
            assert d._llm_ok is online
            assert d._last_cleanup_timing == {'outcome': 'local_list', 'request_ms': 0.0}
        print('PASS actual refine formats validated lists without probing or changing AI availability')

        reset()
        d.SETTINGS['cleanup'] = False
        d._last_cleanup_timing = {'outcome': 'prior_synthetic_timing', 'request_ms': 42}
        with patch.object(d.cleanup_http, 'post_json', side_effect=AssertionError('Cleanup-off model call')):
            assert d.refine(RAW) == RAW
        assert d._llm_ok is False
        assert d._last_cleanup_timing == {'outcome': 'prior_synthetic_timing', 'request_ms': 42}
        print('PASS cleanup-off returns exact raw and does not fabricate timing or availability')

        # Rejected/failed model work must not allow a suffix of an ambiguous
        # count phrase, or a later request, to override the spoken constraints.
        refused = [
            'Make a shopping list with twenty five items. Apples, bananas, pears, milk, eggs.',
            'Make a shopping list with 2.5 items. Apples, bananas, milk, pears, eggs.',
            'Make a shopping list with 2 to 3 items. Apples, bananas, milk.',
            'Make a shopping list with fewer than three items. Apples, bananas, milk.',
            'Do not turn this into a list. I need a shopping list with three items. apple, banana, eggs.',
        ]
        for source in refused:
            reset()
            with patch.object(d.cleanup_http, 'post_json', side_effect=d.cleanup_http.CleanupTimeoutError()) as transport:
                assert d.refine(source) == source, ('Ambiguous-count/refusal fallback changed source', source)
            assert transport.call_count == 1
            assert d._llm_ok is False and d._last_cleanup_timing['outcome'] == 'timeout'
        print('PASS actual timeout fallback preserves ambiguous counts and earlier explicit formatting refusal')

        # Shared coordinator shapes presentation after judging the actual model
        # response. Unsafe-model fallback never becomes a successful whole cache.
        for valid in (False, True):
            reset()
            events, requests = [], []
            def stream(payload, *, timeout, cancel):
                requests.append(payload)
                return {'done': True, 'done_reason': 'stop',
                        'message': {'content': RAW if valid else UNSAFE}}
            d._draft = d.DraftCoordinator(lambda *a: events.append(a), lambda j: True,
                                          request=stream, interval=0)
            job = d.DraftJob(7, (0, 0), 1, 'synthetic-key', 'Synthetic list cleanup.', RAW,
                             d.SETTINGS.get('local_model', d.OLLAMA_MODEL))
            assert d._draft.offer(job)
            wait(lambda: not d._draft.busy)
            assert events[-1][1] == LAYOUT and formatting_is_safe(RAW, events[-1][1])
            assert events[-1][2:] == (('ready', '') if valid else ('partial', 'unsafe'))
            assert len(requests) == 1
            metadata = d._draft.metadata()
            assert metadata['attempts'] == 1 and metadata['success'] == int(valid)
            cached = d._draft.cached_whole(7, (0, 0), 'synthetic-key', RAW)
            assert cached == (RAW if valid else None)
            assert d._draft.close(timeout=2)
        print('PASS coordinator emits structured unsafe partial/valid ready while preserving whole-cache provenance')

        # Validated lists publish locally and the final pass uses the same
        # formatter. This mock exercises the retained delivery boundary.
        reset()
        d._draft_key, d._draft_offered = '', None
        d._preview = d.TranscriptPreview(d.SAMPLE_RATE)
        d._preview.begin(7, (0, 0), now=0)
        ticket = d._preview.claim(7, (0, 0), 32000, now=2)
        assert ticket and d._preview.finish(ticket, RAW, now=2.1, elapsed=.1)
        d._recording.set()
        d._draft._request = Mock(side_effect=AssertionError('Local list used model cleanup for preview'))
        d._offer_draft()
        wait(lambda: not d._draft.busy)
        assert d._preview.snapshot()['draft_text'] == LAYOUT
        assert d._preview.snapshot()['draft_reason'] == 'local list'
        d._draft._request.assert_not_called()
        d._recording.clear()
        assert d._preview.finish_capture(7, (0, 0), now=3)
        d._draft.retire(preserve_whole=True)
        d._lab_thread.preview_generation = 7
        d._lab_thread.timings = {}
        delivery = Mock(return_value={'status': 'retained', 'message': 'Synthetic retained boundary'})
        with patch.object(d, 'transcribe', return_value=RAW), \
             patch.object(d, '_chat', side_effect=AssertionError('Local list used model cleanup for Finish')) as chat, \
             patch.object(d, 'paste', delivery), patch.object(d, '_append_history'):
            d._process_recording([], 1, time.perf_counter(), generations=(0, 0))
        chat.assert_not_called()
        assert not d._lab_thread.timings.get('cleanup_cache_hit')
        assert delivery.call_count == 1 and delivery.call_args.args[0] == LAYOUT + ' '
        assert d._preview.snapshot()['final_text'] == LAYOUT
        assert d._preview.snapshot()['final_outcome'] == 'retained'
        print('PASS same local list in preview and final; zero model calls and mocked retained delivery')
finally:
    d._recording.clear()
    assert d._draft.close(timeout=2) and not d._draft.alive
