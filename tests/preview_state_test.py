"""Pure preview state/audio-tail checks; no model, device, app or input calls."""
from dataclasses import FrozenInstanceError, replace
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
import sys
import threading

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preview_state import PreviewState, Ticket, recent_chunks


CONTEXT = (0, 0)


def claim(state, generation=1, samples=32000, now=2.0, **kwargs):
    ticket = state.claim(generation, CONTEXT, samples, now=now, **kwargs)
    assert isinstance(ticket, Ticket)
    return ticket


def scheduling():
    state = PreviewState()
    assert state.snapshot() == {'active': False, 'generation': None, 'version': 0,
                                'state': 'idle', 'text': '', 'reason': ''}
    assert state.claim(1, CONTEXT, 32000, now=2) is None
    state.begin(1, CONTEXT, now=0)
    assert state.claim(2, CONTEXT, 32000, now=2) is None
    assert state.claim(1, (1, 0), 32000, now=2) is None
    assert state.claim(1, CONTEXT, 31999, now=2) is None
    first = claim(state)
    assert state.in_flight() and state.snapshot()['state'] == 'updating'
    assert state.claim(1, CONTEXT, 48000, now=100) is None
    try:
        first.epoch = 999
        raise AssertionError('Ticket was mutable')
    except FrozenInstanceError:
        pass
    assert not state.finish(None, 'invalid', now=2.1, elapsed=.1)
    assert not state.finish(replace(first), 'forged', now=2.1, elapsed=.1)
    assert state.in_flight()
    assert state.finish(first, 'first provisional text', now=2.2, elapsed=.2)
    assert not state.in_flight() and state.snapshot()['text'] == 'first provisional text'
    assert state.snapshot()['state'] == 'listening'
    copy = state.snapshot()
    copy['text'] = 'caller mutation'
    assert state.snapshot()['text'] == 'first provisional text'
    assert state.claim(1, CONTEXT, 32000, now=100) is None
    assert state.claim(1, CONTEXT, 47999, now=100) is None
    assert state.claim(1, CONTEXT, 48000, now=3.69) is None
    second = claim(state, samples=48000, now=3.7, interval=.4)
    assert state.finish(second, '', now=3.9, elapsed=.9)
    assert state.snapshot()['text'] == 'first provisional text'
    assert state.claim(1, CONTEXT, 64000, now=5.69) is None
    third = claim(state, samples=64000, now=5.71)
    assert state.finish(third, 'replacement, not appended', now=5.8, elapsed=.1)
    assert state.snapshot()['text'] == 'replacement, not appended'


def fences():
    for boundary in ('stop', 'cancel', 'clear', 'off-on', 'new-start'):
        state = PreviewState()
        state.begin(1, CONTEXT, now=0)
        old = claim(state)
        state.end()
        assert state.in_flight(), boundary + ' prematurely released native work'
        assert state.snapshot()['state'] == 'idle' and state.snapshot()['text'] == ''
        next_gen = 2 if boundary == 'new-start' else 1
        next_context = (1, 0) if boundary == 'clear' else CONTEXT
        state.begin(next_gen, next_context, now=3)
        assert state.claim(next_gen, next_context, 32000, now=5) is None
        before = state.snapshot()
        assert not state.finish(old, 'stale private provisional', now=5, elapsed=.2), boundary
        assert state.snapshot() == before and not state.in_flight(), boundary
        new = state.claim(next_gen, next_context, 32000, now=5)
        assert isinstance(new, Ticket)
        assert not state.finish(old, 'duplicated stale completion', now=6, elapsed=.2)
        assert state.in_flight(), 'Old completion cleared newer work'
        assert state.finish(new, 'current provisional', now=5.2, elapsed=.2)
        assert state.snapshot()['text'] == 'current provisional'
    # Begin alone is also a fence (e.g. replacing current recording ownership).
    state = PreviewState()
    state.begin(1, CONTEXT, now=0)
    old = claim(state)
    state.begin(2, CONTEXT, now=3)
    assert not state.finish(old, 'old generation', now=4, elapsed=.1)
    assert state.snapshot()['text'] == '' and not state.in_flight()


def pauses():
    state = PreviewState(max_chars=600)
    state.begin(1, CONTEXT, now=0)
    first = claim(state)
    assert state.finish(first, '😀' * 700, now=4.1, elapsed=2.1)
    snapshot = state.snapshot()
    assert snapshot['text'] == '😀' * 600
    assert snapshot['state'] == 'paused' and snapshot['reason'] == 'slow'
    assert state.claim(1, CONTEXT, 9999999, now=999999) is None
    state.begin(2, CONTEXT, now=5)
    assert state.snapshot()['state'] == 'listening' and state.snapshot()['text'] == ''
    first = claim(state, generation=2, now=7)
    assert state.finish(first, 'last good text', now=7.2, elapsed=.2)
    second = claim(state, generation=2, samples=48000, now=9)
    assert state.finish(second, 'bad result must not replace good text', now=9.1, elapsed=.1, failed=True)
    assert state.snapshot()['text'] == 'last good text'
    assert state.snapshot()['state'] == 'paused' and state.snapshot()['reason'] == 'unavailable'
    assert not state.in_flight() and state.claim(2, CONTEXT, 9999999, now=999999) is None
    state.end()
    assert state.snapshot()['text'] == '' and state.snapshot()['reason'] == ''
    for now, elapsed, text in ((float('nan'), .1, 'x'), (1, float('inf'), 'x'), (1, -1, 'x'), (1, .1, None)):
        state.begin(3, CONTEXT, now=0)
        ticket = claim(state, generation=3)
        assert state.finish(ticket, text, now=now, elapsed=elapsed)
        assert not state.in_flight() and state.snapshot()['reason'] == 'unavailable'
    for params in ({'sample_rate': 0}, {'new_seconds': float('nan')},
                   {'slow_seconds': -1}, {'max_chars': 0}):
        try:
            PreviewState(**params)
            raise AssertionError('Invalid preview configuration accepted')
        except ValueError:
            pass


def audio_tails():
    chunks = [np.arange(10, dtype=np.float32), np.arange(10, 20, dtype=np.float32),
              np.arange(20, 30, dtype=np.float32)]
    originals = [chunk.copy() for chunk in chunks]
    selected = recent_chunks(chunks, 14)
    assert sum(map(len, selected)) == 14 and len(selected) == 2
    assert np.array_equal(np.concatenate(selected), np.arange(16, 30))
    assert np.shares_memory(selected[0], chunks[1]) and selected[1] is chunks[2]
    assert all(np.array_equal(before, after) for before, after in zip(originals, chunks))
    assert recent_chunks([], 100) == [] and recent_chunks(chunks, 0) == []
    assert sum(map(len, recent_chunks([np.array([]), *chunks], 999))) == 30
    # One oversized chunk is sliced before callers concatenate, not afterward.
    huge = np.zeros(16000 * 600, dtype=np.float32)
    huge[-1] = 9
    selected = recent_chunks([huge], 16000 * 6)
    assert len(selected) == 1 and len(selected[0]) == 16000 * 6
    assert selected[0][-1] == 9 and np.shares_memory(selected[0], huge)
    assert huge[0] == 0 and huge[-1] == 9
    # Logical ten-minute buffer: only six seconds of references/views survive.
    block = np.ones((1024, 1), dtype=np.float32)
    ten_minutes = [block] * ((16000 * 600) // 1024)
    selected = recent_chunks(ten_minutes, 16000 * 6)
    assert sum(map(len, selected)) == 16000 * 6 and len(selected) <= 95
    assert all(np.shares_memory(chunk, block) for chunk in selected)
    assert len(ten_minutes) == (16000 * 600) // 1024 and np.all(block == 1)


def concurrency():
    state, finished, errors = PreviewState(), threading.Event(), []
    def writer():
        try:
            for generation in range(200):
                state.begin(generation, CONTEXT, now=0)
                ticket = claim(state, generation=generation)
                state.finish(ticket, 'g' + str(generation) + ':preview', now=2.2, elapsed=.2)
                state.end()
        except Exception as error:
            errors.append(error)
        finally:
            finished.set()
    def reader():
        previous = -1
        try:
            while not finished.is_set():
                value = state.snapshot()
                assert value['version'] >= previous
                previous = value['version']
                if not value['active']:
                    assert value['generation'] is None and value['state'] == 'idle' and value['text'] == ''
                elif value['text']:
                    assert value['text'].startswith('g' + str(value['generation']) + ':')
        except Exception as error:
            errors.append(error)
    readers = [threading.Thread(target=reader) for _ in range(2)]
    worker = threading.Thread(target=writer)
    for thread in readers: thread.start()
    worker.start()
    for thread in [worker, *readers]:
        thread.join(3)
        assert not thread.is_alive()
    assert not errors, errors
    # Whichever side wins the publication fence, End is the final visible state.
    for _ in range(30):
        state.begin(1, CONTEXT, now=0)
        ticket = claim(state)
        go = threading.Event()
        ender = threading.Thread(target=lambda: (go.wait(), state.end()))
        finisher = threading.Thread(target=lambda: (go.wait(), state.finish(ticket, 'racing', now=2.2, elapsed=.2)))
        ender.start(); finisher.start(); go.set()
        ender.join(3); finisher.join(3)
        assert not ender.is_alive() and not finisher.is_alive()
        assert state.snapshot()['state'] == 'idle' and state.snapshot()['text'] == '' and not state.in_flight()


def main():
    captured = io.StringIO()
    with redirect_stdout(captured), redirect_stderr(captured):
        scheduling()
        fences()
        pauses()
        audio_tails()
        concurrency()
    assert captured.getvalue() == '', 'Preview helper logged provisional text'
    print('preview state: scheduling, bounded tails, stale fences, pause policy and thread-safe publication passed; no inference or input')


if __name__ == '__main__':
    main()
