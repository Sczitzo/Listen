"""Pure transcript state checks: no device, model, network or application calls."""
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from transcript_preview import TranscriptPreview, TranscriptTicket, audio_interval

C = (0, 0)


def take(s, samples, now=100, generation=1):
    t = s.claim(generation, C, samples, now=now, interval=0)
    assert isinstance(t, TranscriptTicket), s.snapshot()
    return t


def complete(s, t, text, now=100, elapsed=0, **kwargs):
    assert s.finish(t, text, now=now, elapsed=elapsed, **kwargs)


def transcript():
    s = TranscriptPreview()
    assert s.snapshot()['phase'] == 'idle'
    s.begin(1, C, now=0)
    assert s.claim(1, C, 31999, now=100) is None
    t = take(s, 32000)
    assert (t.start_sample, t.end_sample) == (0, 32000)
    try:
        t.end_sample = 0
        assert False
    except FrozenInstanceError:
        pass
    assert not s.finish(replace(t), 'forged', now=100, elapsed=0)
    assert s.in_flight()
    complete(s, t, 'first tail')
    assert s.snapshot()['text'] == 'first tail'
    assert s.claim(1, C, 47999, now=100) is None
    complete(s, take(s, 48000), 'replacement')
    assert s.snapshot()['text'] == 'replacement'
    complete(s, take(s, 64000), '')
    assert s.snapshot()['text'] == ''
    complete(s, take(s, 128000), 'repeat')
    assert s.committed_end == 128000
    complete(s, take(s, 160000), 'old tail')
    complete(s, take(s, 256000), 'repeat')
    assert s.snapshot()['text'] == 'repeat repeat'
    assert s.committed_end == 256000
    complete(s, take(s, 288000), 'x' * 900)
    assert len(s.snapshot()['text']) > 600
    complete(s, take(s, 384000), '')
    assert s.snapshot()['text'] == 'repeat repeat'
    assert s.committed_end == 384000


def backlog():
    s = TranscriptPreview()
    s.begin(1, C, now=0)
    for i in range(3):
        t = take(s, 384000, now=100 + i * 10)
        assert (t.start_sample, t.end_sample) == (i * 128000, (i + 1) * 128000)
        complete(s, t, str(i), now=100 + i * 10, elapsed=3)
        assert s.snapshot()['state'] == 'listening'
    assert s.snapshot()['text'] == '0 1 2'
    assert s.snapshot()['covered_samples'] == 384000
    assert s.claim(1, C, 384000, now=999) is None


def fences_and_failures():
    s = TranscriptPreview()
    s.begin(1, C, now=0)
    t = take(s, 32000)
    complete(s, t, 'no', failed=True)
    assert s.committed_end == 0 and s.snapshot()['covered_samples'] == 0
    complete(s, take(s, 32000), 'good')
    t = take(s, 48000)
    complete(s, t, 'bad', failed=True)
    assert s.snapshot()['text'] == 'good'
    assert s.snapshot()['covered_samples'] == 32000
    t = take(s, 48000)
    s.end()
    assert s.in_flight() and s.snapshot()['text'] == ''
    s.begin(2, C, now=0)
    assert s.claim(2, C, 32000, now=100) is None
    assert not s.finish(t, 'old', now=100, elapsed=0)
    assert not s.in_flight() and s.snapshot()['text'] == ''
    t = take(s, 32000, generation=2)
    assert s.finish_capture(2, C, now=100)
    assert s.snapshot()['phase'] == 'finishing'
    assert s.in_flight()
    assert not s.finish(t, 'late', now=100, elapsed=0)
    assert s.finalize(2, C, 'full raw', 'full clean', 'cleaned', now=110)
    final = s.snapshot()
    assert final['text'] == 'full raw' and final['final_text'] == 'full clean'
    assert final['dismiss_at'] == 113 and final['phase'] == 'final'
    s.begin(3, C, now=120)
    assert not s.finalize(2, C, 'old raw', 'old final', 'cleaned', now=121)
    assert s.snapshot()['text'] == ''
    assert s.finish_capture(3, C, now=122)
    s.end()
    assert not s.finalize(3, C, 'raw', 'final', 'cleaned', now=123)


def drafts():
    s = TranscriptPreview()
    s.set_draft_settings('a', True)
    s.begin(1, C, now=0)
    complete(s, take(s, 32000), 'one')
    assert s.draft_started(1, C, 1, 'a')
    complete(s, take(s, 48000), 'one two')
    assert s.draft_finished(1, C, 1, 'a', 'clean one')
    assert s.snapshot()['draft_state'] == 'behind'
    assert s.snapshot()['draft_source_revision'] == 1
    assert s.draft_started(1, C, 2, 'a')
    assert s.draft_finished(1, C, 2, 'a', '', state='failed')
    assert s.snapshot()['draft_text'] == 'clean one'
    assert s.snapshot()['draft_state'] == 'unavailable'
    assert not s.draft_started(1, C, 0, 'a')
    assert s.draft_started(1, C, 2, 'a')
    s.set_draft_settings('b', True)
    assert not s.draft_finished(1, C, 2, 'a', 'wrong settings')
    assert s.snapshot()['draft_text'] == ''
    assert s.draft_started(1, C, 2, 'b')
    s.end()
    assert not s.draft_finished(1, C, 2, 'b', 'late')
    assert s.snapshot()['draft_text'] == ''
    assert 'draft_key' not in s.snapshot()


def draft_pause_preservation():
    s = TranscriptPreview()
    s.set_draft_settings('a', True)
    s.begin(1, C, now=0)
    complete(s, take(s, 32000), 'one')
    assert s.draft_started(1, C, 1, 'a')
    assert s.draft_finished(1, C, 1, 'a', 'valid draft')
    assert s.draft_started(1, C, 1, 'a')
    assert s.draft_finished(1, C, 1, 'a', 'x' * s.MAX_TEXT_BYTES)
    assert s.snapshot()['draft_state'] == 'paused'
    assert s.snapshot()['draft_reason'] == 'long transcript'
    assert s.snapshot()['draft_text'] == 'valid draft'
    complete(s, take(s, 48000), 'new raw')
    assert s.snapshot()['draft_state'] == 'paused'
    assert s.snapshot()['draft_reason'] == 'long transcript'
    assert s.snapshot()['draft_text'] == 'valid draft'
    for status in ('paused', 'unavailable', 'disabled'):
        assert s.draft_started(1, C, 2, 'a')
        assert s.draft_finished(1, C, 2, 'a', '', state=status, reason='long transcript')
        assert s.snapshot()['draft_state'] == status
        assert s.snapshot()['draft_reason'] == 'long transcript'
        assert s.snapshot()['draft_text'] == 'valid draft'
    s.set_draft_settings('b', True)
    assert s.snapshot()['draft_reason'] == ''
    assert s.finish_capture(1, C, now=101)
    assert s.finalize(1, C, 'raw', 'final', 'cleaned', now=102)
    assert s.snapshot()['draft_reason'] == ''


def exact_draft_coverage():
    s = TranscriptPreview()
    s.set_draft_settings('a', True)
    s.begin(1, C, now=0)
    source = 'Send one'
    complete(s, take(s, 32000), source)
    revision = s.snapshot()['text_revision']
    assert s.draft_started(1, C, revision, 'a')
    complete(s, take(s, 48000), 'Send one café order')
    assert s.draft_finished(1, C, revision, 'a', 'Send 1', source=source)
    assert s.snapshot()['draft_covered_chars'] == len(source)
    complete(s, take(s, 64000), 'Send oneself a reminder')
    assert s.snapshot()['draft_covered_chars'] == 0
    complete(s, take(s, 80000), 'Send one, then call')
    assert s.snapshot()['draft_covered_chars'] == 0
    complete(s, take(s, 96000), 'Send one café order and call')
    assert s.snapshot()['draft_covered_chars'] == len(source)
    assert s.set_draft_settings('b', True)
    assert s.snapshot()['draft_covered_chars'] == 0
    latest = s.snapshot()
    assert s.draft_started(1, C, latest['text_revision'], 'b')
    assert s.draft_finished(1, C, latest['text_revision'], 'b', 'Send 1 café order and call.', source=latest['text'])
    assert s.snapshot()['draft_covered_chars'] == len(latest['text'])
    assert s.finish_capture(1, C, now=102)
    assert s.snapshot()['draft_covered_chars'] == len(latest['text'])
    assert s.finalize(1, C, latest['text'], 'Final text.', 'sent', now=103)
    assert s.snapshot()['draft_covered_chars'] == 0
    s.begin(2, C, now=105)
    complete(s, take(s, 32000, now=106, generation=2), source, now=106)
    assert s.draft_started(2, C, 1, 'b')
    assert s.draft_finished(2, C, 1, 'b', 'Send 1', source=source)
    s.end()
    assert s.snapshot()['draft_covered_chars'] == 0 and not s.snapshot()['draft_text']


def capacity_and_audio():
    s = TranscriptPreview()
    s.begin(1, C, now=0)
    assert s.claim(1, C, 16000 * 600 + 1, now=100) is None
    assert s.snapshot()['reason'] == 'capacity'
    s.begin(2, C, now=0)
    outstanding = take(s, 32000, generation=2)
    assert s.claim(2, C, s.MAX_SAMPLES + 1, now=101) is None
    assert not s.finish(outstanding, 'must stay closed', now=102, elapsed=0)
    assert not s.in_flight() and s.snapshot()['text'] == ''
    s.begin(2, C, now=0)
    complete(s, take(s, 32000, generation=2), 'x' * (s.MAX_TEXT_BYTES + 1))
    assert s.snapshot()['text'] == '' and s.snapshot()['reason'] == 'capacity'
    chunks = [list(range(10)), list(range(10, 20)), list(range(20, 30))]
    assert audio_interval(chunks, 7, 24) == [list(range(7, 10)), chunks[1], list(range(20, 24))]
    assert audio_interval(chunks, 10, 20)[0] is chunks[1]
    assert audio_interval(chunks, 0, 0) == []
    assert audio_interval(chunks, 30, 40) == []
    assert audio_interval(chunks, 28, 99) == [[28, 29]]
    for a, b in ((-1, 3), (4, 2), (0, 1.0)):
        try:
            audio_interval(chunks, a, b)
            assert False
        except ValueError:
            pass


if __name__ == '__main__':
    source = TranscriptPreview()
    source.begin(1, C, now=0)
    complete(source, take(source, 32000), 'Atomic source text.')
    snapshot, parts = source.draft_source()
    assert ' '.join(parts) == snapshot['text'] == 'Atomic source text.'
    revision = snapshot['text_revision']
    complete(source, take(source, 48000), 'Atomic source text.')
    assert source.snapshot()['text_revision'] == revision, 'Unchanged text created new model work'
    source.set_draft_settings('format-key', True)
    assert source.draft_started(1, C, revision, 'format-key')
    assert source.draft_finished(1, C, revision, 'format-key', '- Atomic source text.', state='partial', reason='pending')
    assert source.snapshot()['draft_text'] == '- Atomic source text.' and source.snapshot()['draft_state'] == 'behind'
    for check in (transcript, backlog, fences_and_failures, drafts, draft_pause_preservation,
                  exact_draft_coverage, capacity_and_audio):
        check()
        print(check.__name__ + ': PASS')
