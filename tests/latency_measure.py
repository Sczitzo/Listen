"""Explicit synthetic local/paired qualification; never imports the desktop app.

Default execution does nothing. --local admits no model/network/device work;
--real-local admits only cached Small and one fixed local chat request shape.
"""
import argparse
import ast
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import time
from types import SimpleNamespace
import zipfile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'audit/2026-10-28/latency/implementation'
sys.path.insert(0, str(ROOT))
import numpy as np
import requests
from dictation_quality import cleanup_is_safe as word_cleanup_is_safe, filter_segments

PRIVATE_NAMES = frozenset(('settings.json', 'history.jsonl', 'favorites.json',
                           'learned.json', 'workspace.json', 'costs.jsonl', 'dictation.log'))
MODEL = 'qwen2.5:7b'
URL = 'http://127.0.0.1:11434/api/chat'
OPTIONS = {'temperature': 0, 'num_ctx': 8192, 'num_predict': 4000}
FUNCTIONS = {'transcribe', '_chat', '_chat_impl', '_set_cleanup_status', 'refine', 'engineer', 'system_prompt'}
CONSTANTS = {'SAMPLE_RATE', 'SILENCE_RMS', 'OLLAMA_URL', 'OLLAMA_MODEL', 'KEEP_ALIVE',
             'NUM_CTX', 'PROMPT_BASE', 'PROMPT_STYLE', 'PROMPT_GUIDE', 'ENGINEER_PROMPT',
             'REFINE_PROMPT', 'ENGINE_MODEL', 'TRANSFORM_BOUNDARY'}


def mad(values):
    middle = statistics.median(values)
    return statistics.median(abs(value - middle) for value in values)


def local_decision(pairs, self_pairs):
    """Nanoseconds per complete invocation, including per-call block averaging."""
    validate_pairs(pairs, 31)
    validate_pairs(self_pairs, 31)
    before = [row[0] for row in pairs]
    savings = [row[0] - row[1] for row in pairs]
    noise = mad([row[0] - row[1] for row in self_pairs])
    saving = statistics.median(savings)
    threshold = max(1000, 3 * noise)
    baseline = statistics.median(before)
    return {'qualified': saving > threshold and saving >= .05 * baseline,
            'regressed': -saving > threshold, 'median_saving_ns': saving,
            'baseline_median_ns': baseline,
            'candidate_median_ns': statistics.median(row[1] for row in pairs),
            'self_difference_mad_ns': noise, 'absolute_threshold_ns': threshold,
            'relative_saving': saving / baseline if baseline else 0}


def paired_decision(pairs):
    validate_pairs(pairs, 12)
    before = [row[0] for row in pairs]
    increases = [row[1] - row[0] for row in pairs]
    noise = mad(before)
    increase = statistics.median(increases)
    outliers = [index for index, value in enumerate(increases)
                if value > max(250_000_000, 6 * noise)]
    regression = increase > 50_000_000 and increase > noise
    return {'accepted': not regression and not outliers,
            'material_regression': regression, 'outlier_indices': outliers,
            'median_increase_ns': increase, 'baseline_mad_ns': noise,
            'baseline_median_ns': statistics.median(before),
            'candidate_median_ns': statistics.median(row[1] for row in pairs)}


def validate_pairs(pairs, count):
    if len(pairs) != count or any(len(row) != 2 or any(
            type(value) not in (int, float) or not math.isfinite(value) or value < 0
            for value in row) for row in pairs):
        raise ValueError('Expected complete finite nonnegative per-fixture timing pairs')


def decision_examples():
    control = [(100_000, 100_000)] * 31
    assert local_decision([(100_000, 80_000)] * 31, control)['qualified']
    assert not local_decision(control, control)['qualified']
    noisy = [(100_000, 100_000 - ((i % 3) - 1) * 10_000) for i in range(31)]
    assert not local_decision([(100_000, 80_000)] * 31, noisy)['qualified']
    assert not local_decision([(20_000, 19_000)] * 31, control)['qualified']
    assert local_decision([(100_000, 95_000)] * 31, control)['qualified']
    assert not local_decision([(100_000, 95_001)] * 31, control)['qualified']
    assert local_decision([(100_000, 101_001)] * 31, control)['regressed']
    assert not local_decision([(100_000, 101_000)] * 31, control)['regressed']
    assert paired_decision([(1_000_000_000, 990_000_000)] * 12)['accepted']
    assert paired_decision([(1_000_000_000, 1_000_000_000)] * 12)['accepted']
    assert paired_decision([(1_000_000_000, 1_050_000_000)] * 12)['accepted']
    assert not paired_decision([(1_000_000_000, 1_050_000_001)] * 12)['accepted']
    boundary = [(1_000_000_000, 1_000_000_000)] * 12
    boundary[0] = (1_000_000_000, 1_250_000_000)
    assert paired_decision(boundary)['accepted']
    boundary[0] = (1_000_000_000, 1_250_000_001)
    assert paired_decision(boundary)['outlier_indices'] == [0]
    for invalid in ([], [(1, 1)] * 11, [(1, 1)] * 13):
        try:
            paired_decision(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError('Incomplete per-fixture dataset accepted')
    return 17


def validate_post(url, body, kwargs):
    if url != URL or set(kwargs) - {'timeout', 'allow_redirects'}:
        raise PermissionError('Only the fixed local chat request is admitted')
    if kwargs.get('allow_redirects', False) is not False:
        raise PermissionError('Redirects are forbidden')
    if set(body) != {'model', 'stream', 'keep_alive', 'options', 'messages'}:
        raise PermissionError('Unexpected chat body keys')
    if (body['model'] != MODEL or body['stream'] is not False or
            body['keep_alive'] != '5m' or body['options'] != OPTIONS):
        raise PermissionError('Model/options/retention must match the frozen protocol')
    messages = body['messages']
    if (not isinstance(messages, list) or len(messages) != 2 or
            [row.get('role') for row in messages] != ['system', 'user'] or
            any(set(row) != {'role', 'content'} or not isinstance(row['content'], str)
                for row in messages)):
        raise PermissionError('Expected two synthetic text messages')


def guard_event(event, args, *, real_local=False, writable=()):
    """Pure policy function: negatives can prove rejection before any real access."""
    if event == 'socket.connect':
        address = args[1]
        if not real_local or not isinstance(address, tuple) or address[:2] != ('127.0.0.1', 11434):
            raise PermissionError('Socket destination not admitted')
    if event == 'socket.getaddrinfo':
        if not real_local or args[:2] != ('127.0.0.1', 11434):
            raise PermissionError('DNS/port not admitted')
    if event == 'socket.sendto':
        raise PermissionError('Datagrams are forbidden')
    if event == 'winreg.OpenKey' and len(args) > 1 and str(args[1]).lower() == 'environment':
        raise PermissionError('User credential environment lookup forbidden')
    roots = tuple(Path(path).resolve() for path in writable)
    if event == 'open' and not isinstance(args[0], int):
        path = Path(args[0]).resolve()
        writing = args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        owned = any(path.is_relative_to(root) for root in roots)
        if writing and not owned:
            raise PermissionError('Write outside owned benchmark space')
        if not writing and path.name.lower() in PRIVATE_NAMES and not owned:
            raise PermissionError('Live saved data read forbidden')
    if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.rename'):
        paths = args[:2] if event == 'os.rename' else args[:1]
        if any(not any(Path(path).resolve().is_relative_to(root) for root in roots) for path in paths):
            raise PermissionError('Filesystem change outside owned benchmark space')


class GuardedChat:
    def __init__(self, transport=None):
        self.session = requests.Session() if transport is None else None
        if self.session is not None:
            self.session.trust_env = False
        self.transport = transport or self.session.post
        self.calls = []
        self.last_response = None

    def post(self, url, **kwargs):
        body = kwargs.pop('json', None)
        if not isinstance(body, dict):
            raise PermissionError('A fixed JSON body is required')
        validate_post(url, body, kwargs)
        response = self.transport(url, json=body, timeout=kwargs.get('timeout', 120), allow_redirects=False)
        if 300 <= response.status_code < 400:
            raise PermissionError('Redirect response rejected')
        self.last_response = response
        self.calls.append({'model': body['model'], 'options': dict(body['options']),
                           'request_sha256': hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()})
        return response

    def close(self):
        if self.session is not None:
            self.session.close()


def response(content='Keep exactly 17 files.', *, payload=None, status=200, raw=None):
    result = requests.Response()
    result.status_code = status
    result.url = URL
    result.encoding = 'utf-8'
    result._content = raw if raw is not None else json.dumps(
        payload if payload is not None else {'done_reason': 'stop', 'message': {'content': content}}
    ).encode('utf-8')
    return result


def candidate_source(source, audio=False, parser=False):
    if audio:
        old = 'audio = np.concatenate(chunks).flatten()'
        new = 'audio = np.concatenate(chunks).reshape(-1)'
        if old in source:
            assert source.count(old) == 1
            source = source.replace(old, new)
        elif new not in source:
            raise AssertionError('Unknown audio source; do not guess a transformation')
    if parser:
        old = '    if r.json().get("done_reason", "stop") != "stop":'
        new = '    data = r.json()\n    if data.get("done_reason", "stop") != "stop":'
        if old in source:
            assert source.count(old) == 1
            source = source.replace(old, new)
            old_content = 'out = r.json()["message"]["content"].strip()'
            assert source.count(old_content) == 1
            source = source.replace(old_content, 'out = data["message"]["content"].strip()')
        elif '    data = r.json()' not in source:
            raise AssertionError('Unknown parser source; do not guess a transformation')
    return source


def load_functions(source, *, recognizer=None, post=None, vocab=()):
    import cleanup_http
    import threading
    from cleanup_policy import CleanupPolicy
    from spoken_format import FORMAT_INSTRUCTIONS, layout_explicit_lists, prepare_spoken_list, formatting_is_safe
    from spoken_corrections import correction_source
    tree = ast.parse(source)
    structured_guard = any(isinstance(node, ast.ImportFrom) and node.module == 'spoken_format'
        and any(alias.name == 'formatting_is_safe' and alias.asname == 'cleanup_is_safe'
                for alias in node.names) for node in tree.body)
    guard = formatting_is_safe if structured_guard else word_cleanup_is_safe
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id in CONSTANTS:
                nodes.append(node)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id in CONSTANTS:
            nodes.append(node)
    settings = {'local_model': MODEL, 'style': 'tighten', 'cleanup': True,
                'engine': 'local', 'cloud_enabled': False, 'learn': False, 'target': 'general'}
    guard_results = []
    def safety(raw, cleaned, words):
        accepted = guard(raw, cleaned, words)
        guard_results.append(accepted)
        return accepted
    transport = post or (lambda *a, **k: response())
    def bounded_chat(payload, timeout):
        result = transport(URL, json=payload, timeout=timeout)
        result.raise_for_status()
        return result.json()
    ns = {'np': np, 're': re, 'json': json, 'math': math, 'time': time, 'SETTINGS': settings,
          'FORMAT_INSTRUCTIONS': FORMAT_INSTRUCTIONS, 'layout_explicit_lists': layout_explicit_lists,
          'correction_source': correction_source,
          'prepare_spoken_list': prepare_spoken_list,
          '_prepare_list': prepare_spoken_list, '_layout': layout_explicit_lists, '_NO_PROXY': {}, '_await_speech': lambda: None,
          '_draft': SimpleNamespace(gate=SimpleNamespace(
              reserve_authoritative=lambda: SimpleNamespace(release=lambda: None), wait_draft=lambda timeout: True)),
          'CLEANUP_TIMEOUT_S': 15., '_cleanup_policy': CleanupPolicy(), '_cleanup_warm_pending': None,
          'cleanup_http': SimpleNamespace(post_json=bounded_chat, NUM_BATCH=cleanup_http.NUM_BATCH),
          '_data_lock': threading.RLock(), '_quitting': False,
          '_llm_ok': False, '_operation_settings': lambda: dict(settings),
          'vocab_words': lambda: list(vocab), 'filter_segments': filter_segments,
          'cleanup_is_safe': safety, '_guard_results': guard_results, 'print': lambda *a, **k: None,
          'requests': SimpleNamespace(post=transport),
          'model': recognizer or SimpleNamespace(transcribe=lambda *a, **k: (
              [SimpleNamespace(text='Keep exactly 17 files.', avg_logprob=0., no_speech_prob=0.)], None)),
          '_claude': lambda *a, **k: (_ for _ in ()).throw(AssertionError('Cloud forbidden')),
          'learned_prefs': lambda: (_ for _ in ()).throw(AssertionError('Private preferences forbidden'))}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), '<actual-source-functions>', 'exec'), ns)
    assert FUNCTIONS - {'_chat_impl', '_set_cleanup_status'} <= ns.keys()
    assert ns['SAMPLE_RATE'] == 16000 and ns['OLLAMA_MODEL'] == MODEL
    return ns


def baseline_text(path):
    path = Path(path).resolve()
    with zipfile.ZipFile(path) as archive:
        if archive.namelist().count('dictate.py') != 1:
            raise ValueError('Expected one allowlisted dictate.py archive member')
        return archive.read('dictate.py').decode('utf-8-sig')


def time_blocks(first, second, repeats):
    def timed(call):
        started = time.perf_counter_ns()
        for _ in range(repeats):
            call()
        return (time.perf_counter_ns() - started) / repeats
    for index in range(5):
        if index % 2:
            timed(second); timed(first)
        else:
            timed(first); timed(second)
    rows = []
    for index in range(31):
        if index % 2:
            after = timed(second); before = timed(first)
        else:
            before = timed(first); after = timed(second)
        rows.append((before, after))
    return rows


def local_measure(source, current):
    report = {'protocol': '31 alternating blocks after five paired warmup blocks; ns per complete call',
              'decision_examples': decision_examples(), 'audio_cases': [], 'json_cases': []}
    audio = candidate_source(source, audio=True)
    parser = candidate_source(source, parser=True)
    def function_tree(text, name):
        return ast.dump(next(node for node in ast.parse(text).body
                             if isinstance(node, ast.FunctionDef) and node.name == name))
    audio_from_current = function_tree(current, 'transcribe') == function_tree(audio, 'transcribe')
    parser_from_current = function_tree(current, '_chat') == function_tree(parser, '_chat')
    for name in ('refine', 'engineer', 'system_prompt'):
        assert function_tree(source, name) == function_tree(current, name), 'Unreviewed caller change'
    base, changed = load_functions(source), load_functions(current if audio_from_current else audio)
    current_ns = load_functions(current)
    for seconds in (1, 10, 60, 600):
        chunks = [np.full((1600, 1), .02, dtype=np.float32) for _ in range(seconds * 10)]
        repeats = 3 if seconds == 600 else 10 if seconds == 60 else 100
        a = lambda: base['transcribe'](chunks, seconds)
        b = lambda: changed['transcribe'](chunks, seconds)
        self_pairs = time_blocks(a, a, repeats)
        pairs = time_blocks(a, b, repeats)
        assert a() == b() == current_ns['transcribe'](chunks, seconds)
        report['audio_cases'].append({'seconds': seconds, 'chunks': len(chunks),
            'candidate_origin': 'actual_current' if audio_from_current else 'source_candidate_in_memory',
            'repeats_per_block': repeats, 'pairs_ns': pairs, 'self_pairs_ns': self_pairs,
            'decision': local_decision(pairs, self_pairs), 'avoided_full_copy_bytes': seconds * 16000 * 4})
    for name, content in (('short_sentence', 'Keep exactly 17 files.'),
                          ('paragraph_4096', ('Keep the blue lantern beside the green river. ' * 100)[:4096])):
        item = response(content)
        first = load_functions(source, post=lambda *a, **k: item)
        second = load_functions(current if parser_from_current else parser, post=lambda *a, **k: item)
        a = lambda: first['_chat']('Synthetic system.', 'Synthetic request.')
        b = lambda: second['_chat']('Synthetic system.', 'Synthetic request.')
        self_pairs = time_blocks(a, a, 500)
        pairs = time_blocks(a, b, 500)
        assert a() == b() == content
        report['json_cases'].append({'case': name, 'content_characters': len(content),
            'candidate_origin': 'actual_current' if parser_from_current else 'source_candidate_in_memory',
            'repeats_per_block': 500, 'pairs_ns': pairs, 'self_pairs_ns': self_pairs,
            'decision': local_decision(pairs, self_pairs)})
    for name in ('audio', 'json'):
        rows = report[name + '_cases']
        report[name + '_qualified'] = (any(row['decision']['qualified'] for row in rows) and
                                      not any(row['decision']['regressed'] for row in rows))
    report['source_sha256'] = hashlib.sha256(source.encode()).hexdigest()
    report['current_source_sha256'] = hashlib.sha256(current.encode()).hexdigest()
    report['status'] = 'passed'
    report['limits'] = 'Local work and allocation only; not a perceptible Finish-to-text speedup claim.'
    return report


def chunks_of(audio):
    return [audio[index:index + 1600].reshape(-1, 1) for index in range(0, len(audio), 1600)]


def hash_file(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def cleanup_model_identity():
    """Owner-confirmed existing public model manifest, not a new management call."""
    models = Path.home() / '.ollama/models'
    manifest = models / 'manifests/registry.ollama.ai/library/qwen2.5/7b'
    body = json.loads(manifest.read_text(encoding='utf-8'))
    identities = {}
    for item in [body['config'], *body['layers']]:
        digest = item['digest']
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
            raise ValueError('Unexpected cached model digest')
        blob = models / 'blobs' / digest.replace(':', '-')
        actual = hash_file(blob)
        if actual != digest.split(':')[1]:
            raise AssertionError('Cached cleanup blob does not match its declared digest')
        identities[digest] = {'bytes': blob.stat().st_size, 'verified_sha256': actual}
    return {'manifest_sha256': hash_file(manifest), 'verified_blobs': identities}


def phrase_check(text, phrases):
    normalized = re.sub(r'[.,!?]', '', text.lower())
    positions = [normalized.find(phrase) for phrase in phrases]
    return all(index >= 0 for index in positions) and positions == sorted(positions)


def invoke_recorded(ledger, fixture, index, phase, variant, call):
    """Preserve each completed half and a failing next attempt independently."""
    item = {'fixture': fixture, 'pair_index': index, 'phase': phase,
            'variant': variant, 'status': 'running'}
    ledger.append(item)
    try:
        result = call()
    except Exception as error:
        item.update(status='failed', error_type=type(error).__name__)
        raise
    item.update(status='completed', result=result)
    return result


def real_measure(source, current):
    from faster_whisper.audio import decode_audio
    from speech_process import load_speech_process
    runtime, chat = None, GuardedChat()
    report = {'status': 'failed', 'paid_calls': 0, 'microphone_calls': 0, 'native_input_calls': 0,
              'private_data_reads': 0, 'records': [], 'content_cases': [], 'attempts': [], 'model': MODEL}
    try:
        report['cleanup_model_before'] = cleanup_model_identity()
        started = time.perf_counter_ns()
        runtime = load_speech_process('small', preferred_device='cuda')
        report['decoder_startup_ns'] = time.perf_counter_ns() - started
        report['speech_runtime'] = runtime.status()
        assert report['speech_runtime']['device'] == 'cuda'
        snapshot = Path(runtime._snapshot)
        report['model_hashes_before'] = {name: hash_file(snapshot / name)
                                         for name in ('model.bin', 'config.json', 'tokenizer.json')}
        base = load_functions(source, recognizer=runtime, post=chat.post)
        changed = load_functions(current, recognizer=runtime, post=chat.post)
        folder = ROOT / 'audit/2026-10-26/live-preview'
        first = decode_audio(str(folder / 'first.wav'), sampling_rate=16000)
        second = decode_audio(str(folder / 'second.wav'), sampling_rate=16000)
        report['fixture_hashes'] = {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                     for name in ('first.wav', 'second.wav')}
        primary = [('first', first), ('second', second),
                   ('pause_60', np.concatenate((first, np.zeros(60 * 16000, np.float32), second)))]
        def run(ns, chunks, held):
            call_count = len(chat.calls)
            guard_count = len(ns['_guard_results'])
            started = time.perf_counter_ns()
            raw = ns['transcribe'](chunks, held)
            speech_end = time.perf_counter_ns()
            text = ns['refine'](raw) if raw else ''
            ended = time.perf_counter_ns()
            # Inspect timing fields only after the declared measured interval.
            try:
                data = chat.last_response.json() if raw and chat.last_response is not None else {}
            except ValueError:
                data = {}
            guarded = len(ns['_guard_results']) == guard_count + 1
            return {'raw': raw, 'final': text,
                    'cleanup_completed': not raw or (len(chat.calls) == call_count + 1 and ns['_llm_ok']),
                    'guard_exercised': not raw or guarded,
                    'safety_accepted': ns['_guard_results'][-1] if raw and guarded else None,
                    'speech_ns': speech_end - started, 'cleanup_ns': ended - speech_end,
                    'core_pipeline_to_result_ns': ended - started,
                    'request_sha256': chat.calls[-1]['request_sha256'] if raw and len(chat.calls) > call_count else None,
                    'ollama': {key: data.get(key) for key in ('model', 'done_reason', 'load_duration',
                        'prompt_eval_duration', 'eval_duration', 'prompt_eval_count', 'eval_count', 'total_duration')}}
        for name, audio in primary:
            chunks = chunks_of(audio); held = len(audio) / 16000
            fixture_record = {'fixture': name, 'audio_seconds': held,
                'prepared_audio_sha256': hashlib.sha256(audio.tobytes()).hexdigest(),
                'warmup_pairs': 1, 'warmup_records': [], 'measured_pairs': []}
            report['records'].append(fixture_record)
            report['active_phase'] = name + ':warmup'
            warm = fixture_record['warmup_records']
            warm.append(invoke_recorded(report['attempts'], name, 0, 'warmup', 'baseline',
                                        lambda: run(base, chunks, held)))
            warm.append(invoke_recorded(report['attempts'], name, 0, 'warmup', 'candidate',
                                        lambda: run(changed, chunks, held)))
            assert all(row['cleanup_completed'] and row['guard_exercised'] for row in warm)
            assert warm[0]['raw'] == warm[1]['raw'] and warm[0]['final'] == warm[1]['final']
            rows = fixture_record['measured_pairs']
            expected = (['blue lantern', 'green river', 'red apple'] if name == 'first' else
                        ['silver mountain', 'quiet forest', 'yellow chair'] if name == 'second' else
                        ['blue lantern', 'green river', 'red apple', 'silver mountain', 'quiet forest', 'yellow chair'])
            for index in range(12):
                report['active_phase'] = name + ':pair_' + str(index)
                pair_record = {'baseline': None, 'candidate': None}
                rows.append(pair_record)
                def before_call():
                    pair_record['baseline'] = invoke_recorded(report['attempts'], name, index, 'measured',
                        'baseline', lambda: run(base, chunks, held))
                    return pair_record['baseline']
                def after_call():
                    pair_record['candidate'] = invoke_recorded(report['attempts'], name, index, 'measured',
                        'candidate', lambda: run(changed, chunks, held))
                    return pair_record['candidate']
                if index % 2:
                    after = after_call(); before = before_call()
                else:
                    before = before_call(); after = after_call()
                assert before['cleanup_completed'] and after['cleanup_completed'], 'Local cleanup request did not complete'
                assert before['guard_exercised'] and after['guard_exercised'], 'Actual cleanup guard was not exercised'
                assert (before['raw'], before['final'], before['safety_accepted']) == (
                    after['raw'], after['final'], after['safety_accepted']), 'Actual paired content differed'
                assert before['request_sha256'] == after['request_sha256'], 'Paired request options/content differed'
                assert phrase_check(before['raw'], expected) and phrase_check(before['final'], expected), 'Known phrase baseline failed'
            decision = paired_decision([(row['baseline']['core_pipeline_to_result_ns'],
                                         row['candidate']['core_pipeline_to_result_ns']) for row in rows])
            fixture_record['decision'] = decision
        extras = [(f'pause_{seconds}', np.concatenate((first, np.zeros(seconds * 16000, np.float32), second)))
                  for seconds in (5, 12, 20)]
        extras.extend((('leading_silence', np.concatenate((np.zeros(5 * 16000, np.float32), first))),
                       ('trailing_silence', np.concatenate((first, np.zeros(20 * 16000, np.float32)))),
                       ('repetition', np.concatenate((first, first))),
                       ('silence', np.zeros(16000, np.float32)),
                       ('tap', np.full(1600, .02, np.float32))))
        for name, audio in extras:
            chunks = chunks_of(audio); held = len(audio) / 16000
            report['active_phase'] = name + ':content'
            content_record = {'fixture': name, 'baseline': None, 'candidate': None,
                'prepared_audio_sha256': hashlib.sha256(audio.tobytes()).hexdigest()}
            report['content_cases'].append(content_record)
            before = invoke_recorded(report['attempts'], name, 0, 'content', 'baseline',
                                     lambda: run(base, chunks, held))
            content_record['baseline'] = before
            after = invoke_recorded(report['attempts'], name, 0, 'content', 'candidate',
                                    lambda: run(changed, chunks, held))
            content_record['candidate'] = after
            assert before['cleanup_completed'] and after['cleanup_completed']
            assert before['guard_exercised'] and after['guard_exercised']
            assert (before['raw'], before['final']) == (after['raw'], after['final'])
            if name in ('silence', 'tap'):
                assert before['raw'] == before['final'] == ''
        report['status'] = 'passed' if all(row['decision']['accepted'] for row in report['records']) else 'inconclusive'
        report['local_chat_calls'] = len(chat.calls)
        report['speech_runtime_after'] = runtime.status()
        assert report['speech_runtime_after']['device'] == 'cuda'
        report['model_hashes_after'] = {name: hash_file(snapshot / name)
                                        for name in ('model.bin', 'config.json', 'tokenizer.json')}
        report['cleanup_model_after'] = cleanup_model_identity()
        assert report['model_hashes_before'] == report['model_hashes_after']
        assert report['cleanup_model_before'] == report['cleanup_model_after']
        report['source_sha256'] = hashlib.sha256(source.encode()).hexdigest()
        report['current_source_sha256'] = hashlib.sha256(current.encode()).hexdigest()
        report['limits'] = 'Synthetic core pipeline only; excludes Finish/queue/mic/editor and does not prove general accuracy.'
    except Exception as error:
        report['error_type'] = type(error).__name__
    finally:
        report['local_chat_calls'] = len(chat.calls)
        report['decoder_closed'] = runtime is None or runtime.close()
        chat.close()
        if not report['decoder_closed']:
            report['status'] = 'failed'
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--local', action='store_true')
    modes.add_argument('--real-local', action='store_true')
    parser.add_argument('--baseline', default=str(OUTPUT / 'baseline.zip'))
    parser.add_argument('--receipt', default='local-baseline.json')
    args = parser.parse_args()
    if not (args.local or args.real_local):
        print('Skipped: explicitly select --local or --real-local')
        return 0
    if Path(args.receipt).name != args.receipt or not args.receipt.endswith('.json'):
        raise ValueError('Receipt must be one JSON filename')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / args.receipt
    if destination.exists():
        raise FileExistsError('Preserve existing receipt; select a new admitted name')
    # Example-table checks run before any timing or candidate measurements.
    assert decision_examples() == 17
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['ANTHROPIC_API_KEY'] = 'isolated-disabled-test-value'
    os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
    sys.addaudithook(lambda event, values: guard_event(event, values, real_local=args.real_local,
                                                     writable=(OUTPUT,)))
    source = baseline_text(args.baseline)
    current = (ROOT / 'dictate.py').read_text(encoding='utf-8-sig')
    frozen = {'plan': hash_file(ROOT / '.ai-context/LATENCY-PLAN.md'),
              'corpus': hash_file(OUTPUT / 'corpus.json'),
              'current': hash_file(ROOT / 'dictate.py'),
              'harness': hash_file(Path(__file__))}
    report = local_measure(source, current) if args.local else real_measure(source, current)
    report['frozen_hashes_before'] = frozen
    report['frozen_hashes_after'] = {'plan': hash_file(ROOT / '.ai-context/LATENCY-PLAN.md'),
              'corpus': hash_file(OUTPUT / 'corpus.json'), 'current': hash_file(ROOT / 'dictate.py'),
              'harness': hash_file(Path(__file__))}
    report['frozen_files_unchanged'] = report['frozen_hashes_before'] == report['frozen_hashes_after']
    if not report['frozen_files_unchanged']:
        report['status'] = 'failed'
    report['current_file_sha256'] = hashlib.sha256((ROOT / 'dictate.py').read_bytes()).hexdigest()
    with zipfile.ZipFile(args.baseline) as archive:
        report['baseline_file_sha256'] = hashlib.sha256(archive.read('dictate.py')).hexdigest()
    report['plan_sha256'] = hashlib.sha256((ROOT / '.ai-context/LATENCY-PLAN.md').read_bytes()).hexdigest()
    report['corpus_sha256'] = hashlib.sha256((OUTPUT / 'corpus.json').read_bytes()).hexdigest()
    destination.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: report[key] for key in ('status', 'audio_qualified', 'json_qualified', 'decoder_closed') if key in report}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
