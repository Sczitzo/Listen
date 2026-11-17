"""Synthetic long-recording coordinator checks; no devices, models or network."""
from dataclasses import replace
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from draft_preview import DraftCoordinator, DraftJob, _fragments


def wait(predicate):
    end = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < end, 'Draft did not settle'
        time.sleep(.002)


def make(parts, revision=1, **changes):
    return replace(DraftJob(1, 'dictate', revision, 'settings', 'Clean.',
                            ' '.join(parts), 'same-model', parts=tuple(parts)), **changes)


def echo(payload, **kwargs):
    return {'done': True, 'done_reason': 'stop',
            'message': {'content': payload['messages'][1]['content']}}


def settle(coordinator, job):
    assert coordinator.offer(job)
    wait(lambda: not coordinator.busy)


def drain(coordinator, job):
    count = 0
    while coordinator.needs_work(job):
        settle(coordinator, job)
        count += 1
        assert count < 100
    return count


# Every operation requests one oldest dirty atomic part, and unchanged revisions
# catch up. Appending beyond 10KB retains prefixes and keeps the same model/budget.
calls, events = [], []
def request(payload, **kwargs):
    calls.append(payload)
    assert kwargs['timeout'] == 5
    assert len(''.join(m['content'] for m in payload['messages']).encode()) <= 3000
    return echo(payload)
c = DraftCoordinator(lambda *a: events.append(a), lambda j: True, request=request, interval=0)
parts = ['alpha beta gamma ' * 50 for _ in range(4)]
j = make(parts)
assert len(j.text) > 3000
assert drain(c, j) == 4
assert events[-1][1:3] == (j.text, 'ready')
assert all(p['model'] == 'same-model' for p in calls)
for n in range(4, 16):
    parts.append('delta epsilon zeta ' * 50)
    j = make(parts, n)
    assert drain(c, j) == 1
    assert events[-1][1:3] == (j.text, 'ready')
assert len(j.text) > 10000
assert not c.needs_work(j)
assert c.cached_whole(1, 'dictate', 'settings', j.text) is None
assert c.metadata()['attempts'] == len(calls)
assert not any('alpha' in str(v) for v in c.metadata().values())
assert c.close()

# Oversized unbroken Unicode, tabs, line breaks and whitespace stay lossless.
for raw in [' ' + 'x' * 10001, 'x' * 10001, '\u754c' * 4000, '  a\n\t' * 1000]:
    fragments = _fragments((raw,), 2994)
    assert ''.join(fragments) == raw
    assert all(len(f.encode()) <= 2994 for f in fragments)
    c = DraftCoordinator(lambda *a: events.append(a), lambda j: True, request=echo, interval=0)
    j = make([raw])
    drain(c, j)
    assert events[-1][1] == raw
    assert c.close()

# Failed first identity is attempted once; later fragments still receive work.
calls, events = [], []
def fail_first(payload, **kwargs):
    calls.append(payload['messages'][1]['content'])
    if len(calls) == 1:
        raise TimeoutError()
    return echo(payload)
c = DraftCoordinator(lambda *a: events.append(a), lambda j: True, request=fail_first, interval=0)
j = make(['first ' * 200, 'second ' * 200, 'third ' * 200])
assert drain(c, j) == 3
assert len(calls) == 3 and events[-1][2] == 'partial'
assert events[-1][1] == j.text and not c.needs_work(j)
assert c.metadata()['timeout'] == 1
# Editing a middle part invalidates its suffix, retaining only the exact prefix.
j2 = make([j.parts[0], 'changed ' * 200, j.parts[2]], 2)
assert drain(c, j2) == 2
# Editing the tail retains earlier successes.
j3 = make([j2.parts[0], j2.parts[1], 'tail ' * 200], 3)
assert drain(c, j3) == 1
# A nonprefix edit invalidates all parts.
j4 = make(['new ' * 200, j3.parts[1], j3.parts[2]], 4)
assert drain(c, j4) == 3
assert c.close()

# Whole cache is exact, consumable and never populated by assembled parts.
c = DraftCoordinator(lambda *a: None, lambda j: True, request=echo, interval=0)
j = make(['hello world'])
settle(c, j)
assert c.cached_whole(1, 'dictate', 'settings', 'other') is None
settle(c, j)
assert c.cached_whole(1, 'dictate', 'settings', j.text) == j.text
assert c.cached_whole(1, 'dictate', 'settings', j.text) is None
settle(c, j)
c.retire(preserve_whole=True)
assert c.cached_whole(1, 'dictate', 'settings', j.text) == j.text
settle(c, j)
c.retire()
assert c.cached_whole(1, 'dictate', 'settings', j.text) is None
settle(c, j)
assert c.close()
assert c.cached_whole(1, 'dictate', 'settings', j.text) is None

# Model/settings/context/session changes invalidate sticky mode and cached parts.
for changes in [dict(model='other-model'), dict(key='other-settings'),
                dict(context='engineer'), dict(generation=2)]:
    calls = []
    def record(payload, **kwargs):
        calls.append(payload)
        return echo(payload)
    c = DraftCoordinator(lambda *a: None, lambda j: True, request=record, interval=0)
    long = make(['alpha ' * 200, 'beta ' * 200, 'gamma ' * 200])
    drain(c, long)
    newer = make(['hello world'], 2, **changes)
    assert drain(c, newer) == 1
    assert c.metadata()['mode'] == 'whole'
    assert c.close()

# Global preservation rejection publishes the exact raw assembly, never blank.
events = []
c = DraftCoordinator(lambda *a: events.append(a), lambda j: True, request=echo, interval=0)
j = make(['alpha ' * 300, 'beta ' * 300])
def safe(raw, cleaned, vocab):
    return len(raw) < 3000
with patch('draft_preview.cleanup_is_safe', safe):
    drain(c, j)
assert events[-1][1:4] == (j.text, 'partial', 'unsafe')
assert c.close()

# Prompt alone over capacity makes no HTTP request and remains visible.
events, calls = [], []
c = DraftCoordinator(lambda *a: events.append(a), lambda j: True,
                     request=lambda *a, **k: calls.append(a), interval=0)
settle(c, make(['hello'], system='x' * 3000))
assert not calls and events[-1][2:] == ('partial', 'length')
assert not c.needs_work(make(['hello'], system='x' * 3000))
assert c.close()

# Independently structured fragments need paragraph boundaries. Otherwise the
# next bullet/number becomes part of the previous item and global validation
# discards valid formatting. Raw fallback still retains every original byte.
from spoken_format import formatting_is_safe
for markers in [('-', '-'), ('1.', '2.'), ('a.', 'b.'), None]:
    events, calls = [], []
    def structured_request(payload, **kwargs):
        calls.append(payload)
        words = payload['messages'][1]['content'].split()
        middle = len(words) // 2
        if markers is None:
            text = 'Item | Detail\n--- | ---\n'
            text += ' '.join(words[:middle]) + ' | ' + ' '.join(words[middle:])
        else:
            text = markers[0] + ' ' + ' '.join(words[:middle]) + '\n'
            text += markers[1] + ' ' + ' '.join(words[middle:])
        return {'done': True, 'done_reason': 'stop', 'message': {'content': text}}
    c = DraftCoordinator(lambda *a: events.append(a), lambda j: True,
                         request=structured_request, interval=0)
    j = make(['alpha beta gamma ' * 100, 'delta epsilon zeta ' * 100])
    assert len(j.text.encode()) > 3000
    settle(c, j)
    partial = events[-1]
    assert partial[2] == 'partial' and partial[1] != j.text
    assert '\n\n' in partial[1]
    assert partial[1].endswith(' ' + j.parts[1])
    assert formatting_is_safe(j.text, partial[1])
    assert drain(c, j) == 1
    ready = events[-1]
    assert ready[2] == 'ready' and ready[1] != j.text
    assert len(calls) == 2 and '\n\n' in ready[1]
    assert formatting_is_safe(j.text, ready[1])
    assert c.cached_whole(1, 'dictate', 'settings', j.text) is None
    assert c.close()

# Cancellation during validation cannot populate the final-reuse cache.
started, release = threading.Event(), threading.Event()
c = DraftCoordinator(lambda *a: None, lambda j: True, request=echo, interval=0)
def blocked(*args):
    started.set()
    assert release.wait(2)
    return True
with patch('draft_preview.cleanup_is_safe', blocked):
    c.offer(make(['hello world']))
    assert started.wait(1)
    c.retire(preserve_whole=True)
    release.set()
    wait(lambda: not c.busy)
assert c.cached_whole(1, 'dictate', 'settings', 'hello world') is None
assert c.close()
print('Long drafts: sustained catchup, bounded lossless fragments, failure progress, invalidation, final cache and cancellation passed')
