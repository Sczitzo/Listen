"""Natural lists through guarded actual cleanup, cache, history and preview."""
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
assert ROOT.name.startswith('.test-run-'), 'Use tests/run_tests.py --no-devices'
sys.path.insert(0, str(ROOT))
import dictate as d
import draft_preview as preview
from cleanup_policy import CleanupPolicy
from preview_card import card_content
from spoken_format import prepare_spoken_list, formatting_is_safe

RAW = "Okay, I need a shopping list with three items or so. It's going to be apples, bananas, oranges, actually scratch that milk."
EXPECTED = "Okay, I need a shopping list with three items or so.\nIt's going to be\n- apples\n- bananas\n- milk."
PUNCTUATED_MODEL = EXPECTED.replace(',', ';')
assert formatting_is_safe(prepare_spoken_list(RAW).source, PUNCTUATED_MODEL)
assert prepare_spoken_list(RAW).formatted == EXPECTED

d._quitting = False
d.SETTINGS.update(cleanup=True, learn=False)
original_prepare = d.prepare_spoken_list
with patch.object(d, 'system_prompt', return_value='Synthetic natural-list cleanup.'), \
     patch.object(d, 'vocab_words', return_value=[]):
    for intro in ("It's going to be", 'It is going to be', 'The items are', 'They are', 'Here are the items'):
        raw = RAW.replace("It's going to be", intro)
        prepared = prepare_spoken_list(raw)
        assert prepared is not None
        d._cleanup_policy = CleanupPolicy()
        d._llm_ok = False
        with patch.object(d, '_chat', return_value=prepared.source) as chat, \
             patch.object(d, 'prepare_spoken_list', wraps=original_prepare) as prepare, \
             patch.object(d, 'correction_source', side_effect=AssertionError('No legacy reparse')):
            assert d.refine(raw) == prepared.formatted, intro
            assert prepare.call_count == 1 and prepare.call_args.args[0] == raw
        chat.assert_not_called()

    d.SETTINGS['cleanup'] = False
    with patch.object(d, 'prepare_spoken_list', side_effect=AssertionError('Cleanup off')), \
         patch.object(d.cleanup_http, 'post_json', side_effect=AssertionError('Cleanup off')):
        assert d.refine(RAW) == RAW
    d.SETTINGS['cleanup'] = True

    # A model cannot create correction authority by adding punctuation.
    ambiguous = 'Make a shopping list with three items apples, bananas scratch that oranges, and milk'
    assert prepare_spoken_list(ambiguous) is None
    d._cleanup_policy = CleanupPolicy()
    with patch.object(d.cleanup_http, 'post_json', return_value={
            'done_reason': 'stop', 'message': {'content': ambiguous.replace('bananas scratch', 'bananas, scratch')}}):
        result = d.refine(ambiguous)
        assert result == ambiguous.replace('bananas scratch', 'bananas, scratch')
        assert 'bananas' in result

    raw_label = 'Create a list for groceries. First item is apples with the label called Number two is oranges.'
    punctuated_label = raw_label.replace('called Number', 'called. Number')
    assert prepare_spoken_list(raw_label) is None and prepare_spoken_list(punctuated_label) is not None
    with patch.object(d, '_chat', return_value=punctuated_label):
        assert d.refine(raw_label) == punctuated_label

    # Full source remains in history; one delivery uses the same structured result.
    for cached in (False, True):
        history, delivered = [], []
        d._cleanup_policy = CleanupPolicy()
        d._llm_ok = True
        d._lab_thread.preview_generation = 77
        timings = {}
        d._lab_thread.timings = timings
        try:
            with patch.object(d, 'transcribe', return_value=RAW), \
                 patch.object(d._draft, 'cached_whole', return_value=PUNCTUATED_MODEL if cached else None), \
                 patch.object(d, '_chat', return_value=prepare_spoken_list(RAW).source.replace(',', ';')) as chat, \
                 patch.object(d, '_append_history', side_effect=history.append), \
                 patch.object(d, 'paste', side_effect=lambda text, target: delivered.append(text) or {'status': 'pasted'}):
                d.process([object()], 2, time.perf_counter(), generations=(d._history_generation, d._recovery_generation))
            expected_result = PUNCTUATED_MODEL if cached else EXPECTED
            assert history[-1]['raw'] == RAW and history[-1]['text'] == expected_result
            assert delivered == [expected_result + ' ']
            assert chat.call_count == int(not cached)
            assert bool(timings.get('cleanup_cache_hit')) == cached
        finally:
            for name in ('preview_generation', 'timings'):
                if hasattr(d._lab_thread, name): delattr(d._lab_thread, name)

# Safe punctuation changes in model output must not govern raw list ownership.
events = []
c = d.DraftCoordinator(lambda *args: events.append(args), lambda job: True,
    request=lambda payload, **kwargs: {'done': True, 'done_reason': 'stop',
        'message': {'content': payload['messages'][-1]['content'].replace(',', ';')}}, interval=0)
try:
    job = d.DraftJob(1, 'synthetic', 1, 'fixed', 'Clean.', RAW, 'same-model')
    with patch.object(preview, 'prepare_spoken_list', wraps=prepare_spoken_list) as prepare:
        assert c.offer(job)
        until = time.monotonic() + 3
        while c.busy:
            assert time.monotonic() < until
            time.sleep(.002)
        assert events[-1][1] == PUNCTUATED_MODEL
        assert prepare.call_count == 1 and prepare.call_args.args[0] == RAW
    assert c.cached_whole(1, 'synthetic', 'fixed', RAW) == PUNCTUATED_MODEL
finally:
    assert c.close() and not c.alive

grocery_heading = 'Create a list for groceries.'
grocery_stages = (
    f'{grocery_heading} First item is apples.',
    f'{grocery_heading} First item is apples. Number two is oranges.',
    f'{grocery_heading} First item is apples. Number two is oranges. Number three is grapes.',
)
grocery_rows = ('1. apples', '2. oranges', '3. grapes')
context = (d._history_generation, d._recovery_generation)
generation = d._gen + 1
d._gen, d._record_generations, d._state = generation, context, 'recording'
d._preview.begin(generation, context, now=0)
d._recording.set()
d.SETTINGS['live_preview'] = True
d._llm_ok = False
warm = d._draft.gate.try_cleanup_warm()
assert warm is not None
clock = 0.
heard = iter(grocery_stages)
backend = SimpleNamespace(transcribe_preview=lambda *args, **kwargs: (
    [SimpleNamespace(text=next(heard), avg_logprob=0, no_speech_prob=0, compression_ratio=1)], None))
try:
    with patch.object(d, 'model', backend), \
         patch.object(d, '_speech_status', return_value={'state': 'ready', 'device': 'cuda'}), \
         patch.object(d, 'vocab_words', return_value=[]), \
         patch.object(d.time, 'perf_counter', side_effect=lambda: clock), \
         patch.object(d._draft, 'offer', side_effect=AssertionError('Local list requested AI cleanup')), \
         patch.object(d.cleanup_http, 'post_json', side_effect=AssertionError('Local list used HTTP')):
        for size in range(1, 4):
            clock += 3
            d._audio_samples = size * 3 * d.SAMPLE_RATE
            d._buf = [d.np.zeros((d._audio_samples, 1), dtype=d.np.float32)]
            d._maybe_preview()
            visible, live_text, status = card_content(d._preview.snapshot(), now=clock)
            expected = '\n'.join((grocery_heading, *grocery_rows[:size]))
            assert visible and live_text == expected and status == 'Listening · Preview cleaned', (size, live_text)
            version = d._preview.snapshot()['version']
            d._offer_draft()
            assert d._preview.snapshot()['version'] == version

        d._recording.clear()
        d._state = 'idle'
        assert d._preview.finish_capture(generation, context, now=clock + 1)
        delivered, history = [], []
        d._lab_thread.preview_generation = generation
        with patch.object(d, 'transcribe', return_value=grocery_stages[-1]), \
             patch.object(d, '_chat', side_effect=AssertionError('Local list used final AI cleanup')), \
             patch.object(d, '_append_history', side_effect=history.append), \
             patch.object(d, 'paste', side_effect=lambda text, target: delivered.append(text) or {'status': 'pasted'}):
            d.process([object()], 9, clock, generations=context)
            assert d.refine('We need to buy apples, oranges, grapes, and pears.') == (
                'We need to buy\n- apples\n- oranges\n- grapes\n- and pears.')
        assert delivered == [expected + ' '] and history[-1]['raw'] == grocery_stages[-1]
        final = d._preview.snapshot()
        assert final['phase'] == 'final' and final['final_outcome'] == 'pasted'
        assert final['final_text'] == expected and history[-1]['text'] == expected
        assert card_content(final, now=clock) == (False, '', '')  # Successful Finish stays quiet.
finally:
    warm.release()
    d._recording.clear()
    d._preview.end()
    if hasattr(d._lab_thread, 'preview_generation'):
        del d._lab_thread.preview_generation
    assert d._draft.close() and not d._draft.alive
print('Natural-list integration: local final formatting, five intros, ambiguity, cleanup-off, history, cache and preview authority passed')
