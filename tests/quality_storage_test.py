"""Deterministic checks: no app import, provider calls, or live saved data."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dictation_quality import CRITERIA, cleanup_is_safe, filter_segments, validate_score

assert not cleanup_is_safe(r'open reports\march_review.md', 'open reports/march_review.md')
assert not cleanup_is_safe('run deploy --dry-run', 'run deploy --dry run')
assert cleanup_is_safe(r'open reports\march_review.md', r'Open `reports\march_review.md`.')
assert not cleanup_is_safe('open /etc/config', 'open etc/config')
# Comma-delimited fillers may be dropped; the same word as content may not.
assert cleanup_is_safe('so it was, like, really huge', 'So it was really huge.')
assert cleanup_is_safe('so it was, like, really huge', 'So it was, like, really huge.')
assert not cleanup_is_safe('I like pizza', 'I pizza.')
assert not cleanup_is_safe('it was like huge', 'It was huge.')
# Literal uses of filler words, corrections, negation, quantities and technical text must survive.
assert not cleanup_is_safe('I like tea.', 'I tea.')
assert not cleanup_is_safe('You know the password.', 'The password.')
assert not cleanup_is_safe('Actually ship tomorrow.', 'Ship tomorrow.')
assert not cleanup_is_safe('it was, like, not ready', 'It was ready.')
assert not cleanup_is_safe('send, like, 12 files', 'Send 21 files.')
assert not cleanup_is_safe('run, you know, deploy --dry-run', 'Run deploy --dry run.')
assert not cleanup_is_safe('buy milk, I mean, eggs', 'Buy milk.')  # a correction, not a filler: eggs must survive
from spoken_format import formatting_is_safe
assert formatting_is_safe('so it was, like, really huge', 'So it was really huge.')
assert not formatting_is_safe('I like pizza', 'I pizza.')
# A capitalized opening filler is not a protected name; real names, negations and content still are.
assert cleanup_is_safe('Um, so I was thinking, you know, that we should probably, like, move the standup to ten.', 'So I was thinking that we should probably move the standup to ten.')
assert cleanup_is_safe('Uh, can we, uh, push the release to next week because, um, QA found two bugs.', 'Can we push the release to next week because QA found two bugs?')
assert cleanup_is_safe('Hmm, Okonkwo is away.', 'Okonkwo is away.')
assert not cleanup_is_safe('Um, tell Marcus about it.', 'Tell about it.')
assert not cleanup_is_safe('Um Marcus said no.', 'Said no.')
assert not cleanup_is_safe('Um, I did not approve it.', 'I approve it.')
assert formatting_is_safe('Uh, can we, uh, push the release to next week because, um, QA found two bugs.', 'Can we push the release to next week because QA found two bugs?')
assert not cleanup_is_safe('run --dry-run', 'run ---dry-run')
assert not cleanup_is_safe('--foo then --foo', '--foo then foo')
from safe_storage import (atomic_write_json, load_json, read_history, valid_setting,
                          validated_settings, validated_favorites, validated_learned)


def segment(text, confidence=0, silence=0):
    return SimpleNamespace(text=text, avg_logprob=confidence, no_speech_prob=silence)


assert filter_segments([segment('Thank you.')]) == 'Thank you.'
assert filter_segments([segment('noise', -2, .95), segment('Hello.')]) == 'Hello.'
assert filter_segments([segment('Project vocabulary.', -2)], 'Project vocabulary') == ''
assert filter_segments([segment('Project vocabulary.', -.1)], 'Project vocabulary') == 'Project vocabulary.'
assert cleanup_is_safe('um send Marcus the 12 files', 'Send Marcus the 12 files.')
assert cleanup_is_safe('hello hello world', 'Hello world.')
assert cleanup_is_safe('thank you', 'Thank you.')
assert not cleanup_is_safe('send Marcus the file', 'Send Martin the file.')
assert not cleanup_is_safe('do not send it', 'Do send it.')
assert not cleanup_is_safe("don't send it", 'Send it.')
assert not cleanup_is_safe('send 12 files', 'Send 13 files.')
assert not cleanup_is_safe('Can you help me?', 'Sure, I can help.')
assert not cleanup_is_safe('Delete the draft', 'The draft is deleted.')
assert not cleanup_is_safe('Marcus called Alex', 'Alex called Marcus')
assert not cleanup_is_safe('Alex calls Marcus then Marcus calls Alex', 'Alex calls Marcus then Alex calls Marcus')
assert not cleanup_is_safe('use Suno', 'Use Sono.', ['Suno'])
assert not cleanup_is_safe('hello', '')
score = dict(score=8, criteria={k: 4 for k in CRITERIA}, gaps=[], improved='better', by='test', cost=0)
assert validate_score(score) == score
for invalid in (dict(score,score=0),dict(score,criteria={k:0 for k in CRITERIA})):
    try:
        validate_score(invalid)
    except ValueError:
        pass
    else:
        raise AssertionError('Zero score violates the stated 1-based rubric')
for key, bad in [('score', float('nan')), ('score', True), ('cost', -1), ('gaps', 'bad'), ('criteria', {})]:
    try:
        validate_score(dict(score, **{key: bad}))
    except ValueError:
        pass
    else:
        raise AssertionError((key, bad))

with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
    path = Path(directory) / 'settings.json'
    atomic_write_json(path, {'safe': 1})
    original = path.read_bytes()
    with patch('safe_storage.os.replace', side_effect=OSError('simulated interruption')):
        try:
            atomic_write_json(path, {'unsafe': 2})
        except OSError:
            pass
        else:
            raise AssertionError('failure did not propagate')
    assert path.read_bytes() == original
    assert not list(Path(directory).glob('.save-*'))
    try:
        atomic_write_json(path, {'bad': float('nan')})
    except ValueError:
        pass
    assert path.read_bytes() == original
    path.write_text('[invalid', encoding='utf-8')
    damaged = path.read_bytes()
    os.unlink(str(path) + '.recovery')
    atomic_write_json(path, {'recovered': True})
    assert any(p.read_bytes() == damaged for p in Path(directory).glob('*.recovery'))
    later_damage = b'{another damaged draft'
    path.write_bytes(later_damage)
    atomic_write_json(path, {'recovered': 'again'})
    assert any(p.read_bytes() == later_damage for p in Path(directory).glob('*.recovery'))
    path.write_bytes(damaged)
    assert load_json(path, {}) == {} and path.read_bytes() == damaged
    path.write_text('[]', encoding='utf-8')
    assert load_json(path, {}) == {}
    history = Path(directory) / 'history.jsonl'
    row = dict(text='Hi', raw='hi', ts='2026-10-26T12:00:00', kind='dictate', secs=2, latency=float('nan'), cost=-2)
    history.write_bytes((json.dumps(row) + '\n{"broken"\n[]\n').encode() + b'\xff\n')
    untouched = history.read_bytes()
    rows, skipped = read_history(history)
    assert skipped == 3 and rows[0]['secs'] == 2 and rows[0]['latency'] == rows[0]['cost'] == 0
    assert history.read_bytes() == untouched
    assert read_history(history, 0) == ([], 3)

assert valid_setting('cleanup', True) and not valid_setting('cleanup', 1)
assert valid_setting('microphone', None) and not valid_setting('hotkey', 'unknown')
assert validated_settings({'engine': 'bad', 'cleanup': False}, {'engine': 'local', 'cleanup': True}) == {'engine': 'local', 'cleanup': False}
assert validated_favorites([dict(name='A', text='B', ts='', uses=-1), None])[0]['uses'] == 0
assert validated_favorites([dict(name='Preserve me', text='Authored text')])[0]['text'] == 'Authored text'
defaults = dict(dismissed=[], replacements={}, removed_headings={}, kept=[], criteria={})
assert validated_learned({'kept': [{'words': 12, 'xml': False}], 'criteria': {'bogus': [2, 1]}}, defaults)['kept'][0]['tags'] is False
assert defaults == dict(dismissed=[], replacements={}, removed_headings={}, kept=[], criteria={})
print('quality/storage: all checks passed')
