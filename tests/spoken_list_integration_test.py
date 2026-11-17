"""Actual list fallback integration; guarded runner, synthetic transport only."""
from pathlib import Path
import queue
import sys
import time
from types import SimpleNamespace
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
            with patch.object(d.cleanup_http, 'post_json', return_value={
                    'done_reason': 'stop', 'message': {'content': LAYOUT}}) as request:
                assert d.refine(RAW) == LAYOUT
            assert request.call_count == int(online), 'Ready cleanup must construct the list'
            assert d._llm_ok is online
            assert d._last_cleanup_timing['outcome'] == ('response' if online else 'local_list')
        print('PASS ready AI constructs lists; offline list fallback stays immediate')

        reset()
        d._llm_ok = True
        labeled = 'Create a list for groceries. First item is apples. Number two is oranges.'
        ai_list = 'Create a list for groceries:\n1. Apples.\n2. Oranges.'
        prepared = d.prepare_spoken_list(labeled)
        assert prepared is not None and ai_list != prepared.formatted
        with patch.object(d.cleanup_http, 'post_json', return_value={
                'done_reason': 'stop', 'message': {'content': ai_list}}) as request:
            assert d.refine(labeled) == ai_list, 'Validated AI structure was replaced'
        assert request.call_args.args[0]['messages'][-1]['content'] == prepared.formatted
        assert 0 < request.call_args.kwargs['timeout'] <= 15
        with patch.object(d.cleanup_http, 'post_json', return_value={
                'done_reason': 'stop', 'message': {'content': ai_list.replace('Oranges', 'Pears')}}):
            assert d.refine(labeled) == prepared.formatted
        print('PASS original speech owns correction authority and accepted AI list layout survives')

        reset()
        d._llm_ok = True
        enumerated = 'Today I have three tasks. First email Alex the 17 files. Second do not delete the backup. Third review the report.'
        numbered = 'Today I have three tasks:\n\n1. Email Alex the 17 files.\n2. Do not delete the backup.\n3. Review the report.'
        with patch.object(d.cleanup_http, 'post_json', return_value={
                'done_reason': 'stop', 'message': {'content': numbered}}) as request:
            assert d.refine(enumerated) == numbered
        assert request.call_args.args[0]['messages'][-1]['content'] == d.prepare_spoken_list(enumerated).formatted
        assert '17' in numbered and 'not delete' in numbered
        print('PASS clear original counted ordinals authorize AI numbering without redundant First/Second text')

        merged = ('Today I have three tasks:\n\n'
                  '- Email Alex the 17 files. Do not delete the backup.\n'
                  '- Review the report.')
        prepared_tasks = d.prepare_spoken_list(enumerated)
        assert formatting_is_safe(prepared_tasks.source, merged), 'This regression must pass the word-only guard'
        with patch.object(d.cleanup_http, 'post_json', return_value={
                'done_reason': 'stop', 'message': {'content': merged}}):
            assert d.refine(enumerated) == prepared_tasks.formatted, 'Merged AI items must use the original three-item layout'

        for candidate in (merged, numbered):
            events, requests = [], []
            def boundary_stream(payload, **kwargs):
                requests.append(payload)
                return {'done': True, 'done_reason': 'stop', 'message': {'content': candidate}}
            assert d._draft.close()
            d._draft = d.DraftCoordinator(lambda *args: events.append(args), lambda job: True,
                                          request=boundary_stream, interval=0)
            job = d.DraftJob(42, (0, 0), 1, 'item-boundaries', 'Synthetic cleanup.', enumerated,
                             d.SETTINGS.get('local_model', d.OLLAMA_MODEL))
            assert d._draft.offer(job)
            wait(lambda: not d._draft.busy)
            good = candidate == numbered
            assert events[-1][1:] == ((numbered, 'ready', '') if good else
                                       (prepared_tasks.formatted, 'partial', 'unsafe'))
            assert d._draft.cached_whole(42, (0, 0), 'item-boundaries', enumerated) == (numbered if good else None)
            assert requests[0]['messages'][-1]['content'] == prepared_tasks.formatted
            assert d._draft.close()

        d._lab_thread.preview_generation = 42
        d._lab_thread.timings = {}
        delivered = []
        with patch.object(d, 'transcribe', return_value=enumerated), \
             patch.object(d._draft, 'cached_whole', return_value=merged), \
             patch.object(d, '_chat', return_value=merged) as chat, \
             patch.object(d, 'paste', side_effect=lambda text, target: delivered.append(text) or {'status': 'retained'}), \
             patch.object(d, '_append_history'):
            d.process([], 1, time.perf_counter(), generations=(0, 0))
        assert chat.call_count == 1 and not d._lab_thread.timings.get('cleanup_cache_hit')
        assert delivered == [prepared_tasks.formatted + ' ']
        print('PASS merged AI items rejected by cleanup, preview and final-cache reuse; safe three-item fallback retained')

        reset()
        source_snapshot = {'active': True, 'text': enumerated, 'covered_samples': 2 * d.SAMPLE_RATE,
                           'generation': 42, 'context': (0, 0), 'text_revision': 1}
        fake_preview = SimpleNamespace(draft_source=lambda: (source_snapshot, ()), set_draft_settings=Mock())
        d._draft_key = ''
        with patch.object(d, '_preview', fake_preview), \
             patch.object(d, '_draft', SimpleNamespace(retire=Mock())), \
             patch.object(d, '_restore_draft_after_quit', return_value=None):
            current_job = d._draft_input()
            with patch.object(d, 'FORMAT_REVISION', 'old-interpretation'):
                old_job = d._draft_input()
        assert current_job.key != old_job.key and current_job.text == old_job.text == enumerated
        d._lab_thread.preview_generation = 42
        d._lab_thread.timings = {}
        with patch.object(d, 'transcribe', return_value=enumerated), \
             patch.object(d._draft, 'cached_whole', return_value=numbered) as cache, \
             patch.object(d, '_chat', side_effect=AssertionError('Validated cache must be reused')), \
             patch.object(d, 'paste', return_value={'status': 'retained'}), \
             patch.object(d, '_append_history'):
            d.process([], 1, time.perf_counter(), generations=(0, 0))
        assert cache.call_args.args[2] == current_job.key
        assert d._lab_thread.timings['cleanup_cache_hit']
        d._draft_key = ''
        print('PASS interpretation revision fences both draft and final cache identities')

        reset()
        d._llm_ok = True
        for source, structured in (
            ('Today email Alex the 17 files then do not delete the backup',
             'Today:\n- Email Alex the 17 files\n- then do not delete the backup'),
            ('Project status the draft is ready next steps ask Alex to review it tomorrow',
             '# Project status\nThe draft is ready.\n\n# Next steps\nAsk Alex to review it tomorrow.'),
            ('Item Quantity apples 3 oranges 2',
             '| Item | Quantity |\n| --- | --- |\n| apples | 3 |\n| oranges | 2 |'),
        ):
            with patch.object(d.cleanup_http, 'post_json', return_value={
                    'done_reason': 'stop', 'message': {'content': structured}}):
                assert d.refine(source) == structured
        print('PASS actual cleanup retains safe task lists, sections and tables without adding facts')

        reset()
        d._llm_ok = True
        events, requests = [], []
        def structured_stream(payload, **kwargs):
            requests.append(payload)
            return {'done': True, 'done_reason': 'stop', 'message': {'content': ai_list}}
        assert d._draft.close()
        d._draft = d.DraftCoordinator(lambda *a: events.append(a), lambda j: True,
                                      request=structured_stream, interval=0)
        job = d.DraftJob(42, (0, 0), 1, 'ai-structure', 'Synthetic list cleanup.', labeled,
                         d.SETTINGS.get('local_model', d.OLLAMA_MODEL))
        with patch.object(d, '_draft_input', return_value=job), \
             patch.object(d, '_valid_draft', return_value=True), \
             patch.object(d._preview, 'snapshot', return_value={'draft_source_revision': 1, 'draft_reason': 'local list'}):
            d._offer_draft()
            wait(lambda: not d._draft.busy)
        assert len(requests) == 1 and requests[0]['messages'][-1]['content'] == prepared.formatted
        assert events[-1][1:] == (ai_list, 'ready', '')
        assert d._draft.cached_whole(42, (0, 0), 'ai-structure', labeled) == ai_list
        d._lab_thread.preview_generation = 42
        d._lab_thread.timings = {}
        delivered, history = [], []
        with patch.object(d, 'transcribe', return_value=labeled), \
             patch.object(d._draft, 'cached_whole', return_value=ai_list), \
             patch.object(d, '_chat', side_effect=AssertionError('Safe whole AI result was cleaned twice')), \
             patch.object(d, 'paste', side_effect=lambda text, target: delivered.append(text) or {'status': 'retained'}), \
             patch.object(d, '_append_history', side_effect=history.append):
            d.process([], 1, time.perf_counter(), generations=(0, 0))
        assert delivered == [ai_list + ' ']
        assert history[-1]['raw'] == labeled and history[-1]['text'] == ai_list
        assert d._lab_thread.timings['cleanup_cache_hit']
        print('PASS ready AI preview, whole-cache reuse, single delivery and exact original history')

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
                        'message': {'content': LAYOUT if valid else UNSAFE}}
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
            assert cached == (LAYOUT if valid else None)
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
