"""Check one-click feedback and suggestion review with an isolated, scripted judge."""
import os
import sys
import threading
import time
from pathlib import Path

assert os.environ.get('DICTATE_TEST_NO_DEVICES') == '1', 'Run through tests/run_tests.py --no-devices'
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d


api = d.Api()
d._chime = lambda *_: None
calls = []
entered = threading.Event()
release = threading.Event()
release.set()
responses = {
    'Draft': (6, 'Suggestion one'),
    'Suggestion one': (8, 'Suggestion two'),
    'Suggestion two': (9, 'Suggestion two'),
}


def judge(text):
    calls.append(text)
    entered.set()
    assert release.wait(3), 'Test did not release the delayed review'
    score, improved = responses[text]
    return dict(score=score, improved=improved, by='Simulated judge', cost=0,
                gaps=[f'Feedback for {text}'], criteria={
                    name: 4 for name in ('clarity', 'context', 'task', 'constraints',
                                         'success_criteria', 'format')})


def settled():
    end = time.monotonic() + 4
    while time.monotonic() < end:
        state = api.get_lab()
        if not state['busy']:
            return state
        time.sleep(.01)
    raise AssertionError('Review was still busy after four seconds')


d.run_judge = judge
assert api.feedback(api.get_lab()['draft_id'], '') is False
assert api.set_text('Draft')
original = api.get_lab()
assert api.feedback(original['draft_id'] + 1, 'Draft') is False
assert api.feedback(original['draft_id'], 'another draft') is False

release.clear()
request = api.feedback(original['draft_id'], 'Draft')
assert request and entered.wait(3)
assert api.feedback(original['draft_id'], 'Draft') is False
release.set()
initial = settled()
identity = initial['pending']['id']
assert initial['score']['feedback_id'] == request and initial['pending']['text'] == 'Suggestion one'
assert initial['text'] == 'Draft' and initial['revs'][0]['score'] == 6 and calls == ['Draft']
assert api.review_candidate(identity, True) is False and api.review_candidate(identity, -1) is False
assert api.apply_candidate(identity, 1) is False

entered.clear()
release.clear()
assert api.review_candidate(identity, 0) and entered.wait(3)
assert api.review_candidate(identity, 0) is False
assert api.apply_candidate(identity, 0) is False
release.set()
reviewed = settled()
versions = reviewed['pending']['versions']
assert reviewed['text'] == 'Draft' and reviewed['draft_id'] == original['draft_id']
assert [item['text'] for item in versions] == ['Suggestion one', 'Suggestion two']
assert versions[0]['score']['score'] == 8
assert versions[1]['score'] is None and versions[1]['based_on'] == 0
assert versions[1]['basis_score']['gaps'] == ['Feedback for Suggestion one']
assert calls == ['Draft', 'Suggestion one']
assert 'versions' not in d._workspace_store.load()['active']['pending']

assert api.review_candidate(identity, 1)
no_change = settled()
assert len(no_change['pending']['versions']) == 2
assert no_change['pending']['versions'][1]['score']['score'] == 9
assert calls[-1] == 'Suggestion two'

original_save = d._workspace_store.save
def save_refused(*_):
    raise OSError('Simulated recovery write failure')
d._workspace_store.save = save_refused
try:
    assert api.review_candidate(identity, 0)
    failed = settled()
    assert failed['pending']['versions'] == no_change['pending']['versions']
    assert failed['text'] == 'Draft' and 'Could not save this review' in failed['error']
finally:
    d._workspace_store.save = original_save

assert api.apply_candidate(identity, 0)
applied = api.get_lab()
assert applied['pending'] is None and applied['text'] == 'Suggestion one'
assert applied['score']['score'] == 8 and len(applied['revs']) == len(original['revs']) + 1
assert not applied['error']

assert api.set_text('Draft')
assert api.feedback(api.get_lab()['draft_id'], 'Draft')
pending = settled()['pending']
entered.clear()
release.clear()
assert api.review_candidate(pending['id'], 0) and entered.wait(3)
assert api.set_text('Edited during review')
release.set()
stale = settled()
assert stale['pending']['stale'] and len(stale['pending']['versions']) == 1
assert stale['text'] == 'Edited during review'
assert api.review_candidate(pending['id'], 0) is False and api.apply_candidate(pending['id'], 0) is False
assert api.discard_candidate(pending['id']) and api.get_lab()['text'] == 'Edited during review'

print('PASS: one request, exact version, no-op, rollback, manual Apply and late-result protection')

def pass_state(text, expected_score):
    state = settled()
    if state['prepared_pass']:
        state = api.finish_draft_pass(state['draft_id'], state['prepared_pass'])
    assert state['text'] == text and state['score']['score'] == expected_score
    assert state['draft_pass']['draft_id'] == state['draft_id']
    return state


release.set()
assert api.new_draft() and api.set_text('Rough intention')
responses.update({'Engineered text': (7, ''), 'Better text': (8, ''), 'Lower-scored text': (7, '')})
written = iter(('Engineered text', 'Better text', 'Lower-scored text'))
d.engineer = lambda raw, draft=None: (next(written), 'Simulated writer', 0)
assert api.set_text('Rough idea\r\nAnother detail')
start = api.get_lab()
assert not api.draft_pass(start['draft_id'], 'delayed older draft', False)
assert not api.draft_pass(start['draft_id'], 'Rough idea\nAnother detail', True)
first = api.draft_pass(start['draft_id'], 'Rough idea\nAnother detail', False)
ready = settled()
assert ready['prepared_pass'] == first and ready['text'] == start['text'] and ready['score'] is None
assert api.draft_pass(start['draft_id'], 'Rough idea\nAnother detail', False) is False
assert api.finish_draft_pass(start['draft_id'] + 1, first)['text'] == start['text']
engineered = pass_state('Engineered text', 7)
assert engineered['draft_pass']['id'] == first and calls[-1] == 'Engineered text'
assert engineered['draft_pass']['source_id'] == start['draft_id'] != engineered['draft_id']
assert api.draft_pass(engineered['draft_id'], engineered['text'], True)
refined = pass_state('Better text', 8)
assert calls[-1] == 'Better text'
assert api.draft_pass(refined['draft_id'], refined['text'], True)
held = pass_state('Better text', 8)
assert calls[-1] == 'Lower-scored text' and 'No meaningful improvement' in held['draft_pass']['message']
assert len(held['revs']) == len(refined['revs'])
d.engineer = lambda *_: ('Unscored proposal', 'Simulated writer', 0)
d.run_judge = lambda _: None
assert api.draft_pass(held['draft_id'], held['text'], True)
ungraded = pass_state('Better text', 8)
assert ungraded['draft_pass'] == held['draft_pass'] and ungraded['error']
d.run_judge = judge
responses['Unpublished improvement'] = (9, '')
d.engineer = lambda *_: ('Unpublished improvement', 'Simulated writer', 0)
stale_pass = api.draft_pass(held['draft_id'], held['text'], True)
assert settled()['prepared_pass'] == stale_pass
assert api.set_text('Edited after grading') and api.get_lab()['stale']
assert api.finish_draft_pass(held['draft_id'], stale_pass)['text'] == 'Edited after grading'
assert not api.get_lab()['prepared_pass']

responses['Candidate after failure'] = (9, '')
d.engineer = lambda *_: ('Candidate after failure', 'Simulated writer', 0)
d._workspace_store.save = save_refused
try:
    state = api.get_lab()
    assert api.draft_pass(state['draft_id'], state['text'], False)
    prepared = settled()['prepared_pass']
    assert prepared
    failed = api.finish_draft_pass(state['draft_id'], prepared)
    assert failed['text'] == 'Edited after grading' and failed['stale'] and failed['error']
finally:
    d._workspace_store.save = original_save

entered.clear()
release.clear()
def delayed_writer(raw, draft=None):
    entered.set()
    assert release.wait(3)
    return 'Candidate after switch', 'Simulated writer', 0
d.engineer = delayed_writer
state = api.get_lab()
before_calls = len(calls)
assert api.draft_pass(state['draft_id'], state['text'], False) and entered.wait(3)
assert api.new_draft() and api.set_text('New draft stays put')
release.set()
late = settled()
assert late['text'] == 'New draft stays put' and late['score'] is None and len(calls) == before_calls
print('PASS: Engineer and Refine grade exact proposed text, keep no-gain and failed passes out, and fence late work')
