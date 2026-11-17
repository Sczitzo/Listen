"""Actual draft/final integration in the guarded isolated app copy only."""
import json
from pathlib import Path
import queue
import sys
import time
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
assert ROOT.name.startswith('.test-run-'), 'Use tests/run_tests.py --no-devices'
sys.path.insert(0, str(ROOT))
import dictate as d

TOKEN = 'SyntheticPrivacyCanary'
BASE_SETTINGS = dict(d.SETTINGS)
requests = []
prompt = ['Clean synthetic words.']


def wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < deadline, 'Integration draft did not settle'
        time.sleep(.002)


def fake_http(payload, **kwargs):
    requests.append(payload)
    assert kwargs['timeout'] == 5
    assert len(''.join(m['content'] for m in payload['messages']).encode()) <= 3000
    return {'done': True, 'done_reason': 'stop',
            'message': {'content': payload['messages'][1]['content']}}


def reset(parts, recording_id=7):
    assert d._draft.close(timeout=2) and not d._draft.alive
    requests.clear()
    d.SETTINGS.clear()
    d.SETTINGS.update(BASE_SETTINGS, cleanup=True, live_preview=True, learn=False)
    prompt[0] = 'Clean synthetic words.'
    d._draft = d.DraftCoordinator(d._publish_draft, d._valid_draft,
                                  request=fake_http, interval=0)
    d._draft_key, d._draft_offered = '', None
    d._preview = d.TranscriptPreview(d.SAMPLE_RATE)
    d._quitting = False
    d._history_generation = d._recovery_generation = 0
    d._jobs = queue.Queue()
    d._recording.set()
    d._state, d._busy, d._last_error = 'recording', '', ''
    d._announced = False
    d._lab['busy'] = False
    context = (0, 0)
    d._preview.begin(recording_id, context, now=0)
    for index, text in enumerate(parts):
        now = 3 * (index + 1)
        ticket = d._preview.claim(recording_id, context,
            d.TranscriptPreview.CHUNK_SAMPLES * (index + 1), now=now)
        assert ticket is not None
        assert d._preview.finish(ticket, text, now=now + .1, elapsed=.1)
    return d._draft_input()


def offer_once():
    d._offer_draft()
    wait(lambda: not d._draft.busy)


def final(raw, *, recording_id=7, context=None):
    context = context or (d._history_generation, d._recovery_generation)
    snapshot = d._preview.snapshot()
    d._recording.clear()
    d._preview.finish_capture(snapshot['generation'], snapshot['context'], now=100)
    d._draft.retire(preserve_whole=True)  # actual Stop's cache-retirement boundary
    d._lab_thread.preview_generation = recording_id
    d._lab_thread.timings = {}
    refine, paste, history = Mock(return_value=raw), Mock(return_value={'status': 'sent'}), Mock()
    try:
        with patch.object(d, 'transcribe', return_value=raw), \
             patch.object(d, 'refine', refine), patch.object(d, 'paste', paste), \
             patch.object(d, '_append_history', history):
            d._process_recording([], 1, time.perf_counter(), generations=context)
        assert paste.call_count == 1 and paste.call_args.args[0] == raw + ' '
        assert history.call_count == 1
        return refine, dict(d._lab_thread.timings)
    finally:
        del d._lab_thread.preview_generation
        del d._lab_thread.timings


try:
    with patch.object(d, 'system_prompt', side_effect=lambda: prompt[0]), \
         patch.object(d, 'vocab_words', return_value=[]):
        # Real _offer_draft must revisit a revision until every part catches up.
        parts = [TOKEN + ' alpha beta gamma ' * 65 for _ in range(4)]
        job = reset(parts)
        assert len(job.text.encode()) > 3000
        revision = job.revision
        for index in range(4):
            offer_once()
            snapshot = d._preview.snapshot()
            assert snapshot['text_revision'] == revision
            assert len(requests) == index + 1
            assert snapshot['draft_state'] == ('ready' if index == 3 else 'behind')
            assert snapshot['draft_text'] == job.text
        offer_once()
        assert len(requests) == 4, 'Settled unchanged revision triggered duplicate work'
        assert d._draft.cached_whole(job.generation, job.context, job.key, job.text) is None
        refine, timings = final(job.text)
        assert refine.call_count == 1 and not timings.get('cleanup_cache_hit')
        print('PASS actual unchanged-revision long catchup; assemblies do not bypass final cleanup')

        # Whole-cache reuse is exact and skips only the duplicate refine request.
        for mismatch in ('none', 'raw', 'session', 'context', 'settings', 'model', 'disabled'):
            job = reset([TOKEN + ' keep all twelve files'])
            offer_once()
            assert d._preview.snapshot()['draft_state'] == 'ready'
            raw, recording_id, context = job.text, 7, (0, 0)
            if mismatch == 'raw':
                raw += ' today'
            elif mismatch == 'session':
                recording_id = 8
            elif mismatch == 'context':
                d._history_generation = 1
                context = (1, 0)
            elif mismatch == 'settings':
                prompt[0] += ' Use another style.'
            elif mismatch == 'model':
                d.SETTINGS['local_model'] = 'synthetic-alternate-model'
            elif mismatch == 'disabled':
                d.SETTINGS['cleanup'] = False
            refine, timings = final(raw, recording_id=recording_id, context=context)
            assert refine.call_count == (0 if mismatch == 'none' else 1), mismatch
            assert bool(timings.get('cleanup_cache_hit')) == (mismatch == 'none'), mismatch
        print('PASS actual final exact cache hit plus raw/session/context/settings misses')

        # Settings API invalidation and actual Cancel both destroy reusable text.
        job = reset([TOKEN + ' keep all twelve files'])
        offer_once()
        with patch.object(d, 'atomic_write_json'):
            assert d.Api().set_setting('cleanup', False)
        assert d._draft.cached_whole(job.generation, job.context, job.key, job.text) is None
        job = reset([TOKEN + ' keep all twelve files'])
        offer_once()
        class ImmediateThread:
            def __init__(self, target, args=(), **kwargs):
                self.target, self.args = target, args
            def start(self):
                self.target(*self.args)
        with patch.object(d.threading, 'Thread', ImmediateThread), \
             patch.object(d, '_close_mic') as close_mic:
            d._cancel()
        assert close_mic.call_count == 1 and not d._recording.is_set()
        assert d._preview.snapshot()['phase'] == 'idle'
        assert d._draft.cached_whole(job.generation, job.context, job.key, job.text) is None
        print('PASS actual settings invalidation and Cancel clear final reuse')

        # Diagnostics inspect live synthetic draft state without exposing text,
        # prompts, settings hashes or source fragments.
        job = reset([TOKEN + ' keep all twelve files'])
        offer_once()
        with patch.object(d, '_speech_status', return_value={'state': 'ready', 'reason': '', 'device': 'cuda', 'compute_type': 'float16'}):
            diagnostics = d.Api().diagnostics()
        serialized = json.dumps(diagnostics)
        assert TOKEN not in serialized and job.text not in serialized
        assert job.system not in serialized and job.key not in serialized
        metadata = json.loads(diagnostics['draft cleanup'])
        assert metadata['attempts'] == 1 and metadata['success'] == 1
        assert metadata['mode'] == 'whole' and metadata['pending'] is False
        print('PASS actual diagnostics omit synthetic text, prompt and settings key')
finally:
    d._recording.clear()
    assert d._draft.close(timeout=2) and not d._draft.alive
