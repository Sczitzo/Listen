"""Actual backend functions with synthetic state; no app/device/provider imports."""
import ast
import copy
import datetime
import json
from pathlib import Path
import sys
import threading
import time
from types import ModuleType, SimpleNamespace


SOURCE = Path(__file__).resolve().parents[1] / 'dictate.py'
sys.path.insert(0, str(SOURCE.parent))
import experience_bridge as experience


class ConnectionError(Exception):
    pass


class Timeout(Exception):
    pass


class HTTPError(Exception):
    def __init__(self, status):
        self.response = SimpleNamespace(status_code=status)


class InlineThread:
    def __init__(self, target, **unused): self.target = target
    def start(self): self.target()


def runtime():
    module = ModuleType('_ui_repair_backend_fixture')
    sys.modules[module.__name__] = module
    calls = []
    draft = {'text': 'Retained Draft', 'kind': 'typed', 'by': None, 'cost': 0,
             'score': None, 'stale': False}
    saves, costs = [], []
    module.__dict__.update(
        sys=sys, json=json, datetime=datetime, time=time,
        requests=SimpleNamespace(ConnectionError=ConnectionError, Timeout=Timeout, HTTPError=HTTPError),
        threading=SimpleNamespace(Thread=InlineThread),
        _lab_lock=threading.RLock(), _mic_lock=threading.RLock(), _data_lock=threading.RLock(),
        _lab={'revs': [draft], 'cur': 0, 'busy': '', 'error': '', 'ver': 0},
        _lab_epoch=0, _lab_thread=SimpleNamespace(), _history_generation=0,
        _lab_draft_id=0, _lab_edit_id=-1,
        _workspace_store=SimpleNamespace(save=lambda state: saves.append(copy.deepcopy(state))),
        _cost_ledger=SimpleNamespace(append=lambda event: costs.append(copy.deepcopy(event)) or str(len(costs))),
        _pending=None, _retained=[], _workspace_id='synthetic-workspace',
        saves=saves, costs=costs, ExperienceApi=experience.ExperienceApi,
        set_candidate=experience.set_candidate,
        _lab_cost=lambda: 0, _append_history=lambda row: calls.append('history'),
        _composer='Retained Instruction', _quitting=False, _last_error='',
        _recording=threading.Event(), _pending_recordings=0, _pending_pastes=0,
        _busy='', _jobs=SimpleNamespace(unfinished_tasks=0),
        _preview=SimpleNamespace(in_flight=lambda: False, end=lambda: calls.append('preview-end')),
        _draft=SimpleNamespace(close=lambda **kw: calls.append('draft-close') or True),
        _field_access=SimpleNamespace(close=lambda **kw: calls.append('field-close') or True),
        _close_speech_runtime=lambda: calls.append('speech-close') or True,
        _restore_draft_after_quit=lambda: calls.append('restore'),
        _win=SimpleNamespace(destroy=lambda: calls.append('destroy')), _tray=None,
        clipboard_delivery=SimpleNamespace(pending_status=lambda **kw: {'pending': False}),
        persist_workspace=lambda r: calls.append('persist') or True,
        show_window=lambda: calls.append('show'), _chime=lambda *args: None,
        SETTINGS={'engine': 'local', 'judge': 'local', 'cloud_enabled': False,
                  'local_model': 'selected-local-model', 'target': 'general', 'learn': False},
        _operation_settings=lambda: module.SETTINGS,
        OLLAMA_MODEL='default-local-model', ENGINE_MODEL='cloud-model',
        ENGINEER_PROMPT={'general': 'synthetic system'}, REFINE_PROMPT='synthetic refine',
        validate_score=lambda data: data,
    )
    functions = {'engineer', 'run_judge', 'lab_run', 'quit_app', '_locked_lab',
                 '_lab_failure_message', '_quit_work_message', '_lab_snapshot',
                 'lab_submit', 'lab_improve', 'lab_rebuild', '_lab_score',
                 '_lab_valid', '_lab_cur', '_lab_rev', '_lab_add'}
    tree = ast.parse(SOURCE.read_text(encoding='utf-8'))
    nodes = [node for node in tree.body
             if isinstance(node, ast.FunctionDef) and node.name in functions]
    api = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Api')
    api.body = [node for node in api.body if isinstance(node, ast.FunctionDef)
                and node.name in {'_runtime', 'submit', 'improve', 'rebuild', 'score'}]
    nodes.append(api)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), 'exec'), module.__dict__)
    return module, calls


def fail(error):
    raise error


def model_failure(error, expected):
    r, calls = runtime()
    r._chat = lambda *a: fail(error)
    before = copy.deepcopy(r._lab['revs'])
    assert r.lab_run('engineering', lambda: r.engineer(r._composer, r._lab_cur()['text'])) is True
    message = r._lab['error']
    assert all(word in message.lower() for word in expected), message
    assert 'Draft' in message and 'Instruction' in message and 'open' in message, message
    assert type(error).__name__ not in message and 'RuntimeError' not in message, message
    assert 'private/path' not in message and 'saved' not in message.lower(), message
    assert r._lab['revs'] == before and r._composer == 'Retained Instruction'
    assert r._lab['busy'] == '' and calls == ['persist'], calls


def judge_fallback():
    r, unused = runtime()
    r.SETTINGS.update(judge='anthropic', cloud_enabled=True)
    r._judge_anthropic = lambda text: fail(RuntimeError('private/path'))
    r._judge_local = lambda text: {'score': 7, 'by': 'selected-local-model', 'cost': 0, 'gaps': []}
    result = r.run_judge('Retained Draft')
    assert result['by'] in result['note'] and 'locally' in result['note'], result
    assert 'RuntimeError' not in result['note'] and 'private/path' not in result['note'], result


def assessment_failure():
    r, calls = runtime()
    assert r.lab_run('scoring', lambda: fail(RuntimeError('private/path')))
    assert 'Prompt assessment' in r._lab['error'] and 'cause is unclear' in r._lab['error']
    assert 'RuntimeError' not in r._lab['error'] and 'private/path' not in r._lab['error']
    r, calls = runtime()
    def stale_failure():
        r._lab_epoch += 1
        fail(RuntimeError('private/path'))
    assert r.lab_run('engineering', stale_failure)
    assert r._lab['error'] == '' and not r._lab['busy'], r._lab


def quit_busy(kind, expected):
    r, calls = runtime()
    if kind == 'recording': r._recording.set()
    elif kind == 'lab': r._lab['busy'] = 'engineering'
    elif kind == 'score': r._lab['busy'] = 'scoring'
    elif kind == 'preview': r._preview.in_flight = lambda: True
    elif kind == 'queue': r._jobs.unfinished_tasks = 1
    elif kind == 'recording-stop': r._pending_recordings = 1
    elif kind == 'paste': r._pending_pastes = 1
    else: r._busy = kind
    assert r.quit_app() is False and r._quitting is False
    assert all(word in r._last_error.lower() for word in expected), r._last_error
    if kind != 'recording': assert 'cancel' not in r._last_error.lower(), r._last_error
    assert calls == ['show'], calls


def quit_lifecycle():
    r, calls = runtime()
    def save_with_new_work(unused):
        calls.append('persist')
        r._lab['busy'] = 'engineering'
        return True
    r.persist_workspace = save_with_new_work
    assert r.quit_app() is False and not r._quitting
    assert 'Prompt Lab' in r._last_error and 'cancel' not in r._last_error.lower()
    assert calls == ['persist'], calls
    for blocker, expected in (('persist', 'saved'), ('draft', 'preview'),
                              ('speech', 'speech'), ('field', 'text-field')):
        r, calls = runtime()
        if blocker == 'persist': r.persist_workspace = lambda unused: False
        elif blocker == 'draft': r._draft.close = lambda **kw: False
        elif blocker == 'speech': r._close_speech_runtime = lambda: False
        else: r._field_access.close = lambda **kw: False
        assert r.quit_app() is False and not r._quitting
        assert expected in r._last_error.lower() and 'cancel' not in r._last_error.lower()
        assert 'destroy' not in calls
    r, calls = runtime()
    r._state = 'loading'  # Existing startup shutdown remains permitted when cleanup succeeds.
    assert r.quit_app() is True and r._quitting and calls[-1] == 'destroy'
    assert r.quit_app() is True  # Existing repeated-Quit contract.
    r, calls = runtime()
    r.clipboard_delivery.pending_status = lambda **kw: {'pending': True}
    assert r.quit_app() is False and 'clipboard recovery' in r._last_error.lower()
    assert not calls and not r._quitting


def candidate(r, stale=False):
    pending = r.set_candidate(r, 'Waiting suggestion', 'refine', 'fake', 0, r._lab_epoch, r._lab_cur()['text'])
    assert pending
    r._pending['stale'] = stale
    return copy.deepcopy(r._pending)


def suggestion_refusal(action, stale=False):
    r, calls = runtime()
    waiting = candidate(r, stale)
    before = (copy.deepcopy(r._lab['revs']), r._composer, len(r.saves), r._lab['ver'])
    r._lab_cost = lambda: calls.append('cost') or 0
    r.engineer = lambda *args: calls.append('model') or ('Replacement', 'fake', 0)
    r.run_judge = lambda text: calls.append('model') or dict(score=7, improved='Replacement', by='fake', cost=0)
    r.threading.Thread = lambda **kw: calls.append('thread') or InlineThread(**kw)
    api = r.Api()
    accepted = api.submit(r._composer) if action == 'submit' else getattr(api, action)()
    assert accepted is False, 'A waiting suggestion admitted ' + action
    assert not calls and not r.costs, calls
    assert r._pending == waiting and r._composer == before[1]
    assert r._lab['revs'] == before[0] and len(r.saves) == before[2]
    assert r._lab['ver'] == before[3] + 1, 'Refusal did not publish a fresh lab version'
    assert not r._lab['busy'] and 'review or discard' in r._lab['error'].lower(), r._lab


def publication_refusal(stale=False):
    r, calls = runtime()
    waiting = candidate(r, stale)
    before = (r._lab['ver'], len(r.saves), r._composer)
    assert r.set_candidate(r, 'Late replacement', 'rebuild', 'fake', .1, r._lab_epoch, r._lab_cur()['text']) is None
    assert r._pending == waiting
    assert (r._lab['ver'], len(r.saves), r._composer) == before and not r.costs


def score_with_pending():
    r, calls = runtime()
    waiting = candidate(r, stale=True)
    def judge(text):
        calls.append('judge')
        experience.record_cost(r, 'fake', 'score', .03)
        return dict(score=7, improved='Another assessment rewrite', by='fake', cost=.03)
    r.run_judge = judge
    assert r.Api().score() is True
    assert calls.count('judge') == 1 and len(r.costs) == 1 and r._session_cost == .03
    assert r._pending == waiting and r._lab_cur()['score']['score'] == 7
    assert r._composer == 'Retained Instruction' and not r._lab['busy']


def resolve_then_generate(resolution):
    r, calls = runtime()
    waiting = candidate(r)
    api = r.Api()
    assert getattr(api, resolution + '_candidate')(waiting['id']) is True
    expected_draft = 'Waiting suggestion' if resolution == 'apply' else 'Retained Draft'
    assert r._pending is None and r._lab_cur()['text'] == expected_draft
    def engineer(*args):
        experience.record_cost(r, 'fake', 'rewrite', .02)
        return 'Next suggestion', 'fake', .02
    r.engineer = engineer
    assert api.submit(r._composer) is True
    assert r._pending['text'] == 'Next suggestion' and r._pending['id'] != waiting['id']
    assert r._lab_cur()['text'] == expected_draft and r._composer == ''
    assert len(r.costs) == 1 and r._session_cost == .02


def resolution_feedback(resolution, condition):
    r, calls = runtime()
    waiting = candidate(r)
    api = r.Api()
    assert api.submit(r._composer) is False
    guard_message = r._lab['error']
    assert 'review or discard' in guard_message.lower()
    if condition == 'unrelated':
        r._lab['error'] = 'A separate model failure needs attention.'
    expected_error = r._lab['error'] if condition == 'unrelated' else ''
    if condition == 'failed-save':
        before = (copy.deepcopy(r._lab), copy.deepcopy(r._pending), r._composer)
        save = r._workspace_store.save
        r._workspace_store.save = lambda state: fail(OSError('synthetic disk full'))
        assert getattr(api, resolution + '_candidate')(waiting['id']) is False
        assert (r._lab, r._pending, r._composer) == before, 'Failed resolution lost feedback or work'
        r._workspace_store.save = save
    assert getattr(api, resolution + '_candidate')(waiting['id']) is True
    assert r._pending is None and r._composer == 'Retained Instruction'
    assert r._lab['error'] == expected_error, 'Resolution left incorrect feedback: ' + r._lab['error']
    assert not calls and not r.costs, 'Feedback cleanup started model or accounting work'


def queued_publication(late_candidate):
    r, calls = runtime()
    queued = []
    class QueuedThread:
        def __init__(self, target, **unused): self.target = target
        def start(self): queued.append(self.target)
    r.threading.Thread = QueuedThread
    def engineer(*args):
        calls.append('engineer')
        experience.record_cost(r, 'fake', 'rewrite', .02)
        return 'Finished suggestion', 'fake', .02
    r.engineer = engineer
    api = r.Api()
    assert api.submit(r._composer) is True
    assert not api.improve() and not api.rebuild() and len(queued) == 1
    assert not calls and not r.costs
    waiting = candidate(r) if late_candidate else None
    queued.pop()()
    assert not r._lab['busy'] and calls.count('engineer') == 1
    assert len(r.costs) == 1 and r._session_cost == .02
    if late_candidate:
        assert r._pending == waiting and r._composer == 'Retained Instruction'
    else:
        assert r._pending['text'] == 'Finished suggestion' and r._composer == ''


def run():
    checks = [
        ('connection failure', lambda: model_failure(ConnectionError('private/path'), ('ollama', 'running'))),
        ('model timeout', lambda: model_failure(Timeout('private/path'), ('took too long', 'try again'))),
        ('deadline timeout', lambda: model_failure(TimeoutError('private/path'), ('took too long', 'try again'))),
        ('missing local model', lambda: model_failure(HTTPError(404), ('local model', 'settings'))),
        ('model request refusal', lambda: model_failure(HTTPError(503), ('local model', 'check ollama', 'try again'))),
        ('unusable response', lambda: model_failure(json.JSONDecodeError('private/path', '', 0), ('response', 'try again'))),
        ('unknown failure', lambda: model_failure(RuntimeError('private/path'), ('cause', 'try again'))),
        ('local judge fallback', judge_fallback),
        ('assessment and stale failures', assessment_failure),
    ]
    for kind, expected in (('recording', ('finish', 'cancel', 'recording')), ('lab', ('prompt lab', 'writing', 'wait')),
                           ('score', ('prompt lab', 'assessing', 'wait')), ('preview', ('preview', 'wait')),
                           ('queue', ('dictation', 'wait')), ('recording-stop', ('dictation', 'wait')),
                           ('paste', ('delivery', 'wait')), ('transcribing', ('speech', 'wait')),
                           ('cleaning', ('cleaning', 'wait')), ('engineering', ('prompt', 'wait'))):
        checks.append(('quit ' + kind, lambda kind=kind, expected=expected: quit_busy(kind, expected)))
    checks.append(('quit lifecycle guards', quit_lifecycle))
    for stale in (False, True):
        for action in ('submit', 'improve', 'rebuild'):
            checks.append(('waiting suggestion ' + action + (' stale' if stale else ''),
                           lambda action=action, stale=stale: suggestion_refusal(action, stale)))
        checks.append(('candidate publication' + (' stale' if stale else ''),
                       lambda stale=stale: publication_refusal(stale)))
    checks.extend((('ordinary Score with waiting suggestion', score_with_pending),
                   ('Apply then generate', lambda: resolve_then_generate('apply')),
                   ('Discard then generate', lambda: resolve_then_generate('discard')),
                   ('concurrent admission', lambda: queued_publication(False)),
                   ('late candidate publication', lambda: queued_publication(True))))
    for resolution in ('apply', 'discard'):
        for condition in ('guard', 'unrelated', 'failed-save'):
            checks.append((resolution + ' feedback ' + condition,
                           lambda resolution=resolution, condition=condition: resolution_feedback(resolution, condition)))
    failures = []
    for label, check in checks:
        try: check()
        except AssertionError as error:
            failures.append(label)
            print('FAIL ' + label + ': ' + str(error))
        else: print('PASS ' + label)
    assert not failures, 'Failed checks: ' + ', '.join(failures)
    print('ui_repair_backend_test: all ' + str(len(checks)) + ' checks passed')


if __name__ == '__main__': run()
