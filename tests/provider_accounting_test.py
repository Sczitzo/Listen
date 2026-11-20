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


def check_prompt_providers(api, join_all):
    write = 'Improve the synthetic prompt.'
    draft = 'Synthetic draft'
    score = {'score': 7, 'criteria': {key: 4 for key in d.CRITERION_HINT},
             'gaps': ['Add a verification step.'], 'improved': 'Improved synthetic draft'}
    cloud_urls = {
        'openai': 'https://api.openai.com/v1/responses',
        'gemini': 'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent',
    }
    calls = []
    failures = {}
    verdict = {
        'openai': '{"status":"completed","output":[]}',
        'gemini': '{"candidates":[]}',
    }
    env = {'ANTHROPIC_API_KEY': '', 'GEMINI_API_KEY': '',
           'OPENAI_API_KEY': 'synthetic-openai', 'GOOGLE_API_KEY': 'synthetic-google'}

    def post(url, **kwargs):
        calls.append((url, kwargs))
        payload = kwargs['json']
        if url == d.OLLAMA_URL:
            result = json.dumps(score) if payload.get('format') else 'Local rewrite'
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'message': {'content': result}})
        provider = next(route for route, endpoint in cloud_urls.items() if url == endpoint)
        if provider in failures:
            status = failures[provider]
            if isinstance(status, Exception):
                raise status
            return SimpleNamespace(status_code=status, json=lambda: json.loads(verdict[provider]))
        assessment = 'text' in payload if provider == 'openai' else 'responseJsonSchema' in payload['generationConfig']
        text = json.dumps(score) if assessment else 'Cloud rewrite'
        if provider == 'openai':
            parts = [{'type': 'reasoning'}, {'type': 'message',
                     'content': [{'type': 'output_text', 'text': text}]}]
            data = {'status': 'completed', 'output': parts}
        else:
            parts = [{'text': 'Hidden thought', 'thought': True}, {'text': text}]
            data = {'candidates': [{'finishReason': 'STOP', 'content': {'parts': parts}}]}
        return SimpleNamespace(status_code=200, json=lambda: data)

    def reject(action, words):
        try:
            action()
        except d.PromptProviderError as error:
            assert words in str(error), str(error)
            assert 'synthetic-' not in str(error), str(error)
        else:
            raise AssertionError('An unavailable or incomplete cloud request was accepted')

    with patch.dict(os.environ, env), patch.object(d, '_user_env', return_value=None), \
         patch.object(d.requests, 'post', side_effect=post), \
         patch.object(api, 'get_local_models', return_value={'models': ['cleanup', 'writer', 'checker']}) as local_models:
        options = api.get_options()
        assert set(options['engine'].values()) == set(options['judge'].values()) == {'local', 'anthropic', 'openai', 'gemini'}
        assert not options['prompt_providers']['anthropic']['configured']
        assert all(options['prompt_providers'][route]['configured'] for route in cloud_urls)
        assert options['prompt_providers']['openai']['model'] == 'gpt-5-mini'
        assert options['prompt_providers']['gemini']['key_env'] == 'GEMINI_API_KEY or GOOGLE_API_KEY'
        assert 'synthetic-' not in json.dumps(options)
        assert calls == []

        for setting, value in [('engine', 'openai'), ('judge', 'gemini'),
                               ('engine_local_model', 'writer'), ('judge_local_model', 'checker')]:
            assert api.set_setting(setting, value)
        assert not api.set_setting('engine', 'unknown account')
        assert not api.set_setting('engine_local_model', 'not installed')
        assert not api.set_setting('judge_local_model', '  ')
        assert local_models.call_count == 3
        saved = json.loads(Path(d.SETTINGS_FILE).read_text(encoding='utf-8'))
        assert (saved['engine'], saved['judge']) == ('openai', 'gemini')
        assert (saved['engine_local_model'], saved['judge_local_model']) == ('writer', 'checker')
        assert d.validated_settings(saved, d.SETTINGS) == saved
        assert 'synthetic-' not in json.dumps(saved)

        assert api.set_setting('cloud_enabled', False)
        reject(lambda: d.engineer(write), 'Turn on paid cloud AI')
        reject(lambda: d.run_judge(draft), 'Turn on paid cloud AI')
        assert calls == []
        assert api.set_setting('cloud_enabled', True)
        with patch.dict(os.environ, {'GOOGLE_API_KEY': ''}):
            reject(lambda: d.run_judge(draft), 'Gemini needs an API key')
            api.new_draft()
            api.set_text(draft)
            assert d.lab_run('engineering', lambda: d.lab_draft_pass('test-request', False), suggestion=True)
            join_all()
            result = api.get_lab()
            assert result['text'] == draft and 'Gemini needs an API key' in result['error'], result
            assert calls == [], 'An unconfigured checking route should block a multi-stage request before writing'

        before = len(d._cost_ledger.entries())
        unpriced_before = api.get_lab()['session_unpriced']
        assert d.engineer(write) == ('Cloud rewrite', 'gpt-5-mini', 0.0)
        assert d.run_judge(draft)['by'] == 'gemini-3.5-flash-lite'
        assert api.set_setting('engine', 'gemini') and api.set_setting('judge', 'openai')
        assert d.engineer(write) == ('Cloud rewrite', 'gemini-3.5-flash-lite', 0.0)
        assert d.run_judge(draft)['by'] == 'gpt-5-mini'
        assert [url for url, _ in calls] == [cloud_urls[p] for p in ('openai', 'gemini', 'gemini', 'openai')]
        assert all(not kwargs['allow_redirects'] and kwargs['timeout'] == (5, 55) for _, kwargs in calls)
        openai_write, gemini_judge, gemini_write, openai_judge = [kwargs for _, kwargs in calls]
        assert openai_write['headers'] == {'Authorization': 'Bearer synthetic-openai'}
        assert gemini_write['headers'] == {'x-goog-api-key': 'synthetic-google'}
        assert openai_write['json']['store'] is False
        assert json.loads(openai_write['json']['input']) == {'request': write}
        assert 'responseJsonSchema' not in gemini_write['json']['generationConfig']
        assert gemini_judge['json']['generationConfig']['responseJsonSchema'] == d.JUDGE_SCHEMA
        assert openai_judge['json']['text']['format']['schema'] == d.JUDGE_SCHEMA
        assert openai_judge['json']['text']['format']['strict'] is True
        entries = d._cost_ledger.entries()[before:]
        assert [(r['provider'], r['operation'], r['cost']) for r in entries] == [
            ('openai', 'rewrite', None), ('gemini', 'score', None),
            ('gemini', 'rewrite', None), ('openai', 'score', None)]
        assert api.get_lab()['session_unpriced'] == unpriced_before + 4
        saved_attempt = entries[0]
        d.record_cost(d, saved_attempt['model'], saved_attempt['operation'], None,
                      saved_attempt['id'], provider=saved_attempt['provider'])
        assert len(d._cost_ledger.entries()) == before + 4
        with patch.object(d._cost_ledger, 'append', side_effect=OSError('synthetic ledger failure')):
            d.record_cost(d, 'gpt-5-mini', 'rewrite', None, provider='openai')
        assert api.get_lab()['session_unpriced'] == unpriced_before + 4

        with patch.dict(os.environ, {'GEMINI_API_KEY': 'synthetic-gemini'}):
            assert d.engineer(write)[0] == 'Cloud rewrite'
            assert calls[-1][1]['headers'] == {'x-goog-api-key': 'synthetic-gemini'}
        for provider, outcome, action, message in [
            ('openai', 307, lambda: d.run_judge(draft), 'OpenAI could not complete'),
            ('gemini', 401, lambda: d.engineer(write), 'Gemini rejected the API key'),
            ('openai', d.requests.Timeout('synthetic-secret'), lambda: d.run_judge(draft), 'OpenAI took too long'),
            ('gemini', 200, lambda: d.engineer(write), 'Gemini did not return a complete'),
        ]:
            failures[provider] = outcome
            attempts = len(calls)
            reject(action, message)
            assert len(calls) == attempts + 1, 'An error retried or fell back'
            assert calls[-1][0] == cloud_urls[provider]
            assert d._cost_ledger.entries()[-1]['cost'] is None
            failures.clear()
        assert api.get_lab()['session_unpriced'] == unpriced_before + 9

        assert api.set_setting('engine', 'local') and api.set_setting('judge', 'local')
        assert d.engineer(write) == ('Local rewrite', 'writer', 0.0)
        assert d.run_judge(draft)['by'] == 'checker'
        assert [url for url, _ in calls[-2:]] == [d.OLLAMA_URL, d.OLLAMA_URL]
        assert [kwargs['json']['model'] for _, kwargs in calls[-2:]] == ['writer', 'checker']
        assert api.set_setting('engine_local_model', '')
        assert d.engineer(write)[1] == d.SETTINGS['local_model']
        assert calls[-1][1]['json']['model'] == d.SETTINGS['local_model']


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
        d._session_unpriced = 0
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
                check_prompt_providers(api, join_all)
                saved = Path(folder, 'costs.jsonl').read_text()
                assert 'synthetic-' not in saved and 'Synthetic draft' not in saved
            finally:
                if 'release' in locals(): release.set()
                join_all()
    print('provider_accounting_test: passed')

if __name__ == '__main__':
    run()
