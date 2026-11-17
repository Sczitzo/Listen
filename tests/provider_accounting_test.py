"""Provider accounting checks; execute only through tests/run_tests.py isolation."""
import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
# Refuse accidental execution against the real checkout's saved data.
if not ROOT.name.startswith('.test-run-'):
    raise RuntimeError('Run this suite using the isolated test runner')
sys.path.insert(0, str(ROOT))
import dictate as d
from workspace_state import WorkspaceStore, CostLedger


def run():
    with tempfile.TemporaryDirectory() as folder:
        d._workspace_store = WorkspaceStore(folder)
        d._cost_ledger = CostLedger(folder)
        for attribute, name in [('HISTORY', 'history.jsonl'), ('FAVORITES', 'favorites.json'),
                                ('LEARNED_FILE', 'learned.json'), ('SETTINGS_FILE', 'settings.json')]:
            setattr(d, attribute, os.path.join(folder, name))
        d._chime = lambda *args: None
        d.ensure_experience(d)
        d._session_cost = 0
        d._session_call_ids = set()
        d._cost_error = ''
        d.SETTINGS.update(cloud_enabled=False, engine='anthropic', judge='anthropic', learn=False,
                          local_model='selected-before')
        api = d.Api()
        constructors, requests, owned = [], [], []
        response = SimpleNamespace(usage=SimpleNamespace(input_tokens=1000, output_tokens=200),
            stop_reason='end_turn', content=[SimpleNamespace(type='text', text='valid output')])
        outcome = [response]
        def create(**kwargs):
            requests.append(kwargs)
            if isinstance(outcome[0], Exception): raise outcome[0]
            return outcome[0]
        def client(**kwargs):
            constructors.append(kwargs)
            return SimpleNamespace(messages=SimpleNamespace(create=create))
        month = datetime.datetime.now().strftime('%Y-%m')
        model = 'claude-haiku-4-5'
        unit_cost = .002
        original_thread = threading.Thread
        def tracked_thread(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            owned.append(thread)
            return thread
        def join_all():
            for thread in owned:
                thread.join(5)
                assert not thread.is_alive(), 'Owned operation thread did not exit'
        with patch.dict(sys.modules, anthropic=SimpleNamespace(Anthropic=client)), \
             patch.object(d.threading, 'Thread', tracked_thread):
            try:
                try: d._claude(model, 'system', 'private input')
                except PermissionError: pass
                else: raise AssertionError('Cloud request was allowed without consent')
                assert constructors == [] and requests == []
                d.SETTINGS['cloud_enabled'] = True
                text, cost = d._claude(model, 'system', 'private input')
                assert text == 'valid output' and abs(cost-unit_cost)<1e-12
                assert constructors[-1]['max_retries'] == 0
                assert abs(d._session_cost-unit_cost)<1e-12
                assert len(d._cost_ledger.entries()) == 1
                response.stop_reason = 'max_tokens'
                try: d._claude(model, 'system', 'private input')
                except RuntimeError: pass
                else: raise AssertionError('Incomplete provider response accepted')
                assert len(d._cost_ledger.entries()) == 2
                assert abs(d._session_cost-2*unit_cost)<1e-12
                response.stop_reason = 'end_turn'
                outcome[0] = RuntimeError('fake provider failed')
                try: d._claude(model, 'system', 'private input')
                except RuntimeError: pass
                else: raise AssertionError('Provider exception did not propagate')
                summary = d._cost_ledger.summary(month)
                assert summary['unknown'] == 1 and d._cost_ledger.entries()[-1]['cost'] is None
                outcome[0] = response
                # Parsing and score validation failures occur after returned usage is charged.
                for invalid in ('not json', json.dumps({'score': 999})):
                    response.content[0].text = invalid
                    before = len(d._cost_ledger.entries())
                    try:
                        d.validate_score(d._judge_anthropic('private prompt'))
                    except (ValueError, KeyError, TypeError): pass
                    else: raise AssertionError('Invalid paid judge output accepted')
                    assert len(d._cost_ledger.entries()) == before+1
                valid_score = dict(score=7, criteria={key:4 for key in d.CRITERION_HINT}, gaps=[], improved='improved')
                response.content[0].text = json.dumps(valid_score)
                api.new_draft(); api.set_text('score this prompt')
                before = d._cost_ledger.summary(month)
                assert api.score(); join_all()
                assert api.score(); join_all()
                after = d._cost_ledger.summary(month)
                assert after['operations'] == before['operations']+2
                judge_in, judge_out = d.PRICES[d.JUDGE_MODEL]
                judge_cost = (1000*judge_in+200*judge_out)/1e6
                assert abs(after['amount']-before['amount']-2*judge_cost)<1e-12
                api.clear_history(); api.new_draft()
                assert d._cost_ledger.summary(month) == after
                # The operation owns a settings snapshot even if settings change mid-call.
                started, release = threading.Event(), threading.Event()
                seen = []
                def blocked_engineer():
                    seen.append(dict(d._operation_settings()))
                    started.set()
                    assert release.wait(5), 'Snapshot test was not released'
                    seen.append(dict(d._operation_settings()))
                assert d.lab_run('engineering', blocked_engineer)
                assert started.wait(5), 'Operation did not start'
                d.SETTINGS.update(engine='local', judge='local', local_model='selected-after')
                release.set(); join_all()
                assert seen[0] == seen[1]
                assert seen[0]['engine']=='anthropic' and seen[0]['local_model']=='selected-before'
                d.SETTINGS['cloud_enabled']=True
                with patch.object(d._cost_ledger, 'append', side_effect=OSError('simulated write failure')):
                    d._claude(model, 'system', 'private input')
                warning = d._cost_error
                assert warning
                d._claude(model, 'system', 'private input')
                assert d._cost_error == warning, 'Later success hid an earlier missing cost record'
                assert warning in api.get_stats()['spend_note']
                saved = Path(folder, 'costs.jsonl').read_text()
                assert 'private input' not in saved and 'private prompt' not in saved
            finally:
                if 'release' in locals(): release.set()
                join_all()
    print('provider_accounting_test: passed')

if __name__ == '__main__':
    run()
