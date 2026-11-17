import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from experience_bridge import ExperienceApi, ensure, persist_workspace, restore_workspace, set_candidate, record_cost, archive_workspace


def runtime(folder):
    r = SimpleNamespace(APP_DIR=folder, LOG=Path(folder)/'test.log', _lab_lock=threading.RLock(), _data_lock=threading.RLock(), _lab={'revs': [], 'cur': -1, 'ver': 0}, _lab_epoch=0, _lab_draft_id=0, _lab_edit_id=-1, _retained=[], _state='idle')
    r._lab_cur=lambda: r._lab['revs'][r._lab['cur']] if r._lab['cur'] >= 0 else None
    r._lab_rev=lambda text,kind,by=None,cost=0: dict(text=text,kind=kind,by=by,cost=cost,score=None,stale=False)
    def add(rev):
        r._lab['revs'].append(rev); r._lab['cur']=len(r._lab['revs'])-1; r._lab['ver']+=1; r._lab_draft_id+=1
    r._lab_add=add
    r.favorites=[]
    r.load_favorites=lambda: list(r.favorites)
    r.save_favorites=lambda rows: setattr(r,'favorites',rows)
    return r

class Api(ExperienceApi):
    def __init__(self,r): self.r=r
    def _runtime(self): return self.r
    def save_favorite(self,name,text):
        if any(x['name']==name and x['text']!=text for x in self.r.favorites): return False
        if not any(x['name']==name for x in self.r.favorites): self.r.favorites.append(dict(name=name,text=text))
        return True
    def delete_favorite(self,name): self.r.favorites=[x for x in self.r.favorites if x['name']!=name]; return True

def run():
    with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as folder:
        r=runtime(folder); ensure(r); api=Api(r)
        assert not list(Path(folder).iterdir())
        r._lab_add(r._lab_rev('original','raw'))
        assert api.set_composer('new',r._lab_draft_id,2)
        assert not api.set_composer('old',r._lab_draft_id,1)
        with patch.object(r._workspace_store, 'save', side_effect=OSError('private/path')):
            assert not api.set_composer('preserved',r._lab_draft_id,3)
            assert r._composer=='preserved'
            assert 'private/path' not in r._save_error
        assert api.set_composer('new',r._lab_draft_id,4)
        candidate=set_candidate(r,'candidate','rewrite','model',0,r._lab_epoch,'original')
        r._lab_epoch+=1
        assert not api.apply_candidate(candidate['id'])
        assert api._workspace_details()['pending']['stale']
        assert persist_workspace(r)
        restored=runtime(folder); restore_workspace(restored)
        assert restored._composer=='new' and restored._pending['stale']
        assert restored._lab_cur()['text']=='original'
        token=api.remove_favorite('missing'); assert token is None
        r.favorites=[dict(name='one',text='old')]
        token=api.remove_favorite('one'); r.favorites=[dict(name='one',text='new')]
        assert not api.undo_favorite_removal(token)
        assert api.save_favorite_checked('one','replacement')['status']=='conflict'
        assert api.save_favorite_checked('one','replacement',True)['status']=='saved'
        token2=api.remove_favorite('one')
        assert api.undo_favorite_removal(token2)
        assert r.favorites[0]['text']=='replacement'
        assert set_candidate(r,'accepted','rewrite','model',0,r._lab_epoch,'original') is None
        assert r._pending['id'] == candidate['id']
        assert api.discard_candidate(candidate['id'])
        candidate2=set_candidate(r,'accepted','rewrite','model',0,r._lab_epoch,'original')
        assert api.apply_candidate(candidate2['id']) and r._lab_cur()['text']=='accepted'
        archive_workspace(r)
        r._lab.update(revs=[],cur=-1)
        r._lab_add(r._lab_rev('second','raw'))
        archived_id=next(x['id'] for x in api.list_drafts() if x['title']=='original')
        assert api.restore_draft(archived_id)
        assert r._lab_cur()['text']=='accepted'
        assert any(x['title']=='second' for x in api.list_drafts())
        assert not api.delete_draft(api.list_drafts()[0]['id'],'cancel')
        epoch=r._lab_epoch
        before=(Path(folder)/'workspace.json').read_bytes()
        assert not api.clear_private_data('workspace','cancel')['ok']
        assert (Path(folder)/'workspace.json').read_bytes()==before
        assert api.clear_private_data('workspace','DELETE')['ok']
        assert r._lab_epoch>epoch
        assert set_candidate(r,'late','rewrite','model',0,epoch,'original') is None
        import uuid
        call_id=str(uuid.uuid4())
        record_cost(r,'model','score',.1,call_id); record_cost(r,'model','score',.1,call_id)
        assert r._session_cost==.1
        with patch.object(r._cost_ledger, 'append', side_effect=OSError('private/path')):
            record_cost(r,'model','score',.1)
            assert r._cost_error and 'private/path' not in r._cost_error
        assert not api.finish_recording() and not api.cancel_recording()
        calls=[]; r._state='recording'; r._stop=lambda:calls.append('stop'); r._cancel=lambda:calls.append('cancel')
        assert api.finish_recording() and api.cancel_recording()
        assert calls==['stop','cancel']
    print('experience_bridge_test: passed')

if __name__=='__main__': run()
