"""Synthetic transactional workspace regressions; no app/device/provider startup."""
import ast
import copy
import threading
from types import SimpleNamespace
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experience_bridge import ExperienceApi, ensure, persist_workspace, restore_workspace, archive_workspace, set_candidate


def runtime(folder):
    r = SimpleNamespace(APP_DIR=folder, _lab_lock=threading.RLock(), _data_lock=threading.RLock(),
        _lab={'revs': [], 'cur': -1, 'ver': 0}, _lab_epoch=0, _lab_draft_id=0,
        _lab_edit_id=-1, _retained=[])
    r._lab_cur = lambda: r._lab['revs'][r._lab['cur']] if r._lab['cur'] >= 0 else None
    r._lab_rev = lambda text, kind, by=None, cost=0: dict(text=text, kind=kind, by=by, cost=cost, score=None, stale=False)
    def add(rev):
        r._lab['revs'].append(rev)
        r._lab['cur'] = len(r._lab['revs']) - 1
        r._lab['ver'] += 1
        r._lab_draft_id += 1
        r._lab_edit_id = -1
    r._lab_add = add
    r.load_favorites = lambda: copy.deepcopy(r.favorites)
    r.save_favorites = lambda rows: setattr(r, 'favorites', rows)
    return r


class Api(ExperienceApi):
    def __init__(self, r): self.r = r
    def _runtime(self): return self.r

FIELDS = ('_lab', '_workspace_id', '_composer', '_pending', '_archived', '_retained',
          '_lab_epoch', '_lab_draft_id', '_lab_edit_id', '_composer_edit_id',
          '_recovery_generation', '_retained_ver')

def state(r):
    return {key: copy.deepcopy(getattr(r, key)) for key in FIELDS}


def delayed_judge_after_failed_clear():
    """Execute the real score function with a delayed synthetic judge."""
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / 'dictate.py').read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_lab_score')
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
        r = runtime(folder); ensure(r); api = Api(r)
        r._lab_add(r._lab_rev('Authored work being scored', 'raw'))
        revision = r._lab_cur()
        started, release = threading.Event(), threading.Event()
        def judge(text):
            started.set()
            assert release.wait(5), 'judge not released'
            return {'score': 8}
        namespace = dict(_lab_lock=r._lab_lock, _lab_thread=threading.local(),
            _lab_epoch=r._lab_epoch, _lab=r._lab,
            _lab_valid=lambda epoch: epoch == r._lab_epoch,
            run_judge=judge, validate_score=lambda value: value)
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'dictate.py', 'exec'), namespace)
        outcomes = []
        worker = threading.Thread(target=lambda: outcomes.append(namespace['_lab_score'](revision)))
        worker.start()
        try:
            assert started.wait(5), 'judge did not start'
            with patch.object(r._workspace_store, 'save', side_effect=OSError('synthetic disk full')):
                assert not api.clear_private_data('workspace', 'DELETE')['ok']
        finally:
            release.set()
            worker.join(5)
        assert not worker.is_alive()
        assert outcomes == [True], 'valid delayed judge was discarded after failed Clear'
        assert r._lab_cur() is revision and revision['score'] == {'score': 8}

def run():
    for operation in ('delete', 'clear', 'restore', 'apply', 'discard', 'favorite'):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
            r = runtime(folder); ensure(r); api = Api(r)
            r._lab_add(r._lab_rev('Archived authored work', 'raw'))
            archive_workspace(r)
            archived_id = r._archived[0]['id']
            r._lab.update(revs=[], cur=-1, fav='favorite', error='authored context')
            r._lab_add(r._lab_rev('Current authored work', 'raw'))
            r._composer = 'Unsent authored instruction'
            r._composer_edit_id = 5
            r._recovery_generation = 7
            r._retained_ver = 9
            r._retained = [dict(id=9, text='Retained authored work', message='test')]
            candidate = set_candidate(r, 'Proposed work', 'rewrite', 'fixture', 0, r._lab_epoch, 'Current authored work')
            r.favorites = [dict(name='one', text='Original favorite')]
            assert persist_workspace(r)
            before = state(r)
            original_lab = r._lab
            original_revisions = r._lab['revs']
            original_revision = r._lab_cur()
            disk = Path(folder, 'workspace.json').read_bytes()
            def action():
                if operation == 'delete': return api.delete_draft(archived_id, 'DELETE')
                if operation == 'clear': return api.clear_private_data('workspace', 'DELETE')['ok']
                if operation == 'restore': return api.restore_draft(archived_id)
                if operation == 'apply': return api.apply_candidate(candidate['id'])
                if operation == 'discard': return api.discard_candidate(candidate['id'])
                return api.save_favorite_checked('one', 'Replacement', True)['status'] == 'saved'
            with patch.object(r._workspace_store, 'save', side_effect=OSError('synthetic disk full')):
                assert action() is False, operation
            assert state(r) == before, operation + ': failed action mutated memory'
            assert r._lab is original_lab, operation
            assert r._lab['revs'] is original_revisions, operation
            assert r._lab_cur() is original_revision, operation
            # Match the actual delayed judge's epoch + revision identity guard.
            assert r._lab_epoch == before['_lab_epoch']
            assert any(rev is original_revision for rev in r._lab['revs'])
            original_revision['score'] = {'score': 8}
            assert r._lab_cur()['score'] == {'score': 8}
            original_revision['score'] = before['_lab']['revs'][-1]['score']
            assert Path(folder, 'workspace.json').read_bytes() == disk
            # An unrelated later successful write must never commit the failed action.
            assert api.set_composer('Later unrelated edit', r._lab_draft_id, 6)
            recovered = runtime(folder); restore_workspace(recovered)
            assert recovered._lab_cur()['text'] == 'Current authored work', operation
            assert recovered._pending['text'] == 'Proposed work', operation
            assert recovered._archived == before['_archived'], operation
            assert recovered._retained == before['_retained'], operation
            assert recovered._composer == 'Later unrelated edit'
            # Explicit retry performs the intended action once, preserving success semantics.
            assert action() is True, operation
            if operation == 'clear':
                assert not r._lab['revs'] and not r._retained and not r._archived
                assert r._recovery_generation == 8 and r._lab_epoch == before['_lab_epoch'] + 1
                assert set_candidate(r, 'Late work', 'rewrite', '', 0, before['_lab_epoch'], 'Current authored work') is None
            elif operation == 'delete': assert not r._archived
            elif operation == 'restore': assert r._lab_cur()['text'] == 'Archived authored work'
            elif operation == 'apply': assert r._lab_cur()['text'] == 'Proposed work' and r._pending is None
            elif operation == 'discard': assert r._pending is None
            else: assert r.favorites[0]['text'] == 'Replacement'
    delayed_judge_after_failed_clear()
    print('workspace_transactions_test: passed (6 failed-save/reload/retry scenarios + delayed judge)')

if __name__ == '__main__': run()
