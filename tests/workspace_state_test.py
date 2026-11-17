import copy
import json
import sys
import tempfile
import datetime
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from workspace_state import WorkspaceStore, CostLedger


def run():
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as directory:
        store = WorkspaceStore(directory)
        assert store.load()['active']['composer'] == ''
        assert store.load_status == 'missing'
        state = {'version': 1, 'active': {'id': 'draft-1', 'revisions': [{'text': 'authored', 'kind': 'raw', 'by': 'me', 'cost': 0, 'score': {'html': '<bad>'}}], 'cur': 0, 'composer': 'unfinished', 'pending': {'operation': 'rewrite', 'text': 'pending text'}}, 'archived': [], 'retained': [{'id': 'r1', 'text': 'keep', 'message': 'note'}]}
        state['archived'] = [dict(state['active'], id='old')]
        store.save(state)
        assert store.load_status == 'missing'
        original_bytes = (Path(directory) / 'workspace.json').read_bytes()
        for invalid in ({'version': 2, 'active': state['active']},
                        {'version': 1, 'active': None},
                        {'version': 1, 'active': {'id': 'x', 'revisions': 'bad'}}):
            try:
                store.save(invalid)
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid save accepted')
            assert (Path(directory) / 'workspace.json').read_bytes() == original_bytes
        invalids = []
        for field, value in [('text', []), ('cost', -1)]:
            bad = copy.deepcopy(state)
            bad['active']['revisions'][0][field] = value
            invalids.append(bad)
        for key, value in [('pending', {'bad': object()}), ('pending', {'deep': [[[[[[[[[[[[[[[[[[[[[[0]]]]]]]]]]]]]]]]]]]]]]})]:
            bad = copy.deepcopy(state)
            bad['active'][key] = value
            invalids.append(bad)
        for key, value in [('archived', [None]), ('retained', [{'id': [], 'text': 'x', 'message': 'y'}])]:
            bad = copy.deepcopy(state)
            bad[key] = value
            invalids.append(bad)
        for bad in invalids:
            try:
                store.save(bad)
            except ValueError:
                pass
            else:
                raise AssertionError('Malformed nested save accepted')
            assert (Path(directory) / 'workspace.json').read_bytes() == original_bytes
        state['retained'].append({'id': 3, 'text': 'integer id', 'message': 'keep'})
        store.save(state)
        restored_store = WorkspaceStore(directory)
        restored = restored_store.load()
        assert restored_store.load_status == 'ok'
        assert restored['retained'][1]['id'] == 3
        assert restored['active']['composer'] == 'unfinished'
        assert restored['active']['pending']['text'] == 'pending text'
        assert restored['archived'][0]['revisions'][0]['text'] == 'authored'
        assert restored['active']['revisions'][0]['score'] is None
        assert restored['active']['revisions'][0]['stale'] is True
        path = Path(directory) / 'workspace.json'
        before_read_error = path.read_bytes()
        with patch.object(Path, 'open', side_effect=PermissionError('denied')):
            assert store.load()['active']['composer'] == ''
            assert store.load_status == 'read-error'
            try:
                store.save(state)
            except PermissionError:
                pass
            else:
                raise AssertionError('Unreadable original overwritten')
        assert path.read_bytes() == before_read_error
        path.write_text(json.dumps({'version': 2}))
        store.load()
        assert store.load_status == 'unsupported'
        assert json.loads(path.read_text()) == {'version': 2}
        broken = '{broken'
        path.write_text(broken)
        assert store.load()['active']['revisions'] == []
        assert store.load_status == 'corrupt'
        store.clear()
        assert any(p.read_text() == broken for p in Path(directory).glob('*.recovery'))
        path.write_text(json.dumps({'version': 1, 'active': {'id': [], 'revisions': 'bad'}, 'archived': [1], 'retained': [1]}))
        assert store.load()['archived'] == []
        assert store.load()['retained'] == []
        ledger = CostLedger(directory)
        event = {'provider': 'local', 'model': 'test', 'operation': 'score', 'cost': .25, 'text': 'secret'}
        first = ledger.append(event)
        assert ledger.append(dict(event, id=first)) == first
        try:
            ledger.append(dict(event, id=first, cost=2))
        except ValueError:
            pass
        else:
            raise AssertionError('Changed event ID reuse accepted')
        assert len(ledger.entries()) == 1
        ledger.append(event)
        ledger.append(dict(event, cost=None))
        ledger.append(dict(event, cost=0))
        assert 'secret' not in (Path(directory) / 'costs.jsonl').read_text()
        for invalid_id in (3, 'not-a-uuid'):
            try:
                ledger.append(dict(event, id=invalid_id))
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid event ID accepted')
        for cost in (float('nan'), -1, True):
            try:
                ledger.append(dict(event, cost=cost))
            except ValueError:
                pass
            else:
                raise AssertionError('Invalid cost accepted')
        month = datetime.datetime.fromisoformat(ledger.entries()[0]['ts']).astimezone().strftime('%Y-%m')
        assert ledger.summary(month) == {'amount': .5, 'operations': 4, 'unknown': 1, 'unreadable': 0}
        with (Path(directory) / 'costs.jsonl').open('a') as stream:
            stream.write('{partial')
        ledger.append(event)
        assert len(ledger.entries()) == 5
        assert ledger.last_skipped == 1
        assert ledger.summary(month)['unreadable'] == 1
        assert ledger.entries()[0]['id'] == first
        boundary = {'id': first, 'ts': '2026-11-01T00:30:00+00:00',
                    'provider': 'local', 'model': 'test', 'operation': 'score', 'cost': 1}
        class LocalBoundary(datetime.datetime):
            def astimezone(self, tz=None):
                return super().astimezone(tz or datetime.timezone(datetime.timedelta(hours=-7)))
        with patch('workspace_state.datetime.datetime', LocalBoundary), patch.object(ledger, 'entries', return_value=[boundary]):
            assert ledger.summary('2026-10')['amount'] == 1
            assert ledger.summary('2026-11')['operations'] == 0
        store.clear()
        assert len(ledger.entries()) == 5
        assert store.load()['active']['composer'] == ''
    print('workspace_state_test: passed')

if __name__ == '__main__':
    run()

