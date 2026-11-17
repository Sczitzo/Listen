"""Synthetic actual-source contracts; no app import, models, sockets or devices."""
import gc
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import latency_measure as m

# Freeze/validate acceptance rules before any candidate benchmark can run.
assert m.decision_examples() == 17
ledger = []
def first_half():
    assert ledger[-1]['status'] == 'running'
    return {'raw': 'First synthetic result.', 'final': 'First synthetic result.'}
def fail_second_half():
    assert ledger[-1]['status'] == 'running'
    raise RuntimeError('Deliberate second-invocation failure')
m.invoke_recorded(ledger, 'synthetic_fixture', 3, 'measured', 'baseline', first_half)
try:
    m.invoke_recorded(ledger, 'synthetic_fixture', 3, 'measured', 'candidate', fail_second_half)
except RuntimeError:
    pass
else:
    raise AssertionError('Deliberate failure was swallowed')
assert ledger == [
    {'fixture': 'synthetic_fixture', 'pair_index': 3, 'phase': 'measured', 'variant': 'baseline',
     'status': 'completed', 'result': {'raw': 'First synthetic result.', 'final': 'First synthetic result.'}},
    {'fixture': 'synthetic_fixture', 'pair_index': 3, 'phase': 'measured', 'variant': 'candidate',
     'status': 'failed', 'error_type': 'RuntimeError'}]
source = m.baseline_text(m.OUTPUT / 'baseline.zip')
candidate = (m.ROOT / 'dictate.py').read_text(encoding='utf-8-sig')
np = m.np
observed = []


class Recognizer:
    def transcribe(self, audio, **options):
        # Keep the exact object the model received. Copying here would make
        # mutation/GC lifetime checks pass even if app preparation aliased input.
        observed.append((audio, dict(options)))
        return [SimpleNamespace(text='Keep exactly 17 files.', avg_logprob=0., no_speech_prob=0.)], None


for ndim in (1, 2):
    for samples in (16000, 960000, 9600000):
        observed.clear()
        audio = np.full(samples, .02, np.float32)
        chunks = [audio[i:i + 1600].reshape((-1, 1) if ndim == 2 else (-1,)) for i in range(0, samples, 1600)]
        outputs = []
        for text in (source, candidate):
            ns = m.load_functions(text, recognizer=Recognizer())
            outputs.append(ns['transcribe'](chunks, samples / 16000))
        assert outputs == ['Keep exactly 17 files.'] * 2
        assert np.array_equal(observed[-2][0], observed[-1][0])
        assert observed[-2][0].dtype == observed[-1][0].dtype == np.float32
        assert observed[-2][1] == observed[-1][1] == {
            'language': 'en', 'beam_size': 1, 'vad_filter': True, 'initial_prompt': None}
        model_inputs = [observed[-2][0], observed[-1][0]]
        fingerprints = [hashlib.sha256(value.tobytes()).digest() for value in model_inputs]
        assert all(not np.shares_memory(value, chunk) for value in model_inputs for chunk in chunks)
        # Mutate and release every original callback buffer, then force GC.
        # The exact baseline and current model-input objects must survive.
        for chunk in chunks:
            chunk[:] = .5
        assert all(np.all(value == np.float32(.02)) for value in model_inputs)
        del chunk, chunks, audio
        gc.collect()
        assert [hashlib.sha256(value.tobytes()).digest() for value in model_inputs] == fingerprints

for text in (source, candidate):
    ns = m.load_functions(text, recognizer=Recognizer())
    count = len(observed)
    assert ns['transcribe']([], .1) == ''
    try:
        ns['transcribe']([], 1.)
    except RuntimeError:
        pass
    else:
        raise AssertionError('Missing microphone audio was not rejected')
    assert ns['transcribe']([np.full((1600, 1), .02, np.float32)], .1) == ''
    assert ns['transcribe']([np.zeros((16000, 1), np.float32)], 1.) == ''
    assert len(observed) == count
    long_gap = [np.full((1600, 1), .02, np.float32), np.zeros((360 * 16000, 1), np.float32),
                np.full((1600, 1), .03, np.float32)]
    assert ns['transcribe'](long_gap, 360.2) == 'Keep exactly 17 files.'
    assert len(observed[-1][0]) == 360 * 16000 + 3200
    assert np.all(observed[-1][0][-1600:] == np.float32(.03))

# Observe the actual extra array operation: this check rejects the original
# preparation when the optimized operation contract is applied to it.
def extra_copy_count(text):
    counts = {'flatten': 0, 'reshape': 0}
    class Tracked(np.ndarray):
        def flatten(self, *args, **kwargs):
            counts['flatten'] += 1
            return super().flatten(*args, **kwargs)
        def reshape(self, *args, **kwargs):
            counts['reshape'] += 1
            return super().reshape(*args, **kwargs)
    def concatenate(chunks):
        return np.concatenate(chunks).view(Tracked)
    ns = m.load_functions(text)
    ns['np'] = SimpleNamespace(concatenate=concatenate, sqrt=np.sqrt, mean=np.mean)
    ns['transcribe']([np.full((16000, 1), .02, np.float32)], 1.)
    return counts
assert extra_copy_count(candidate) == {'flatten': 0, 'reshape': 1}
assert extra_copy_count(source) == {'flatten': 1, 'reshape': 0}, 'Frozen baseline counterexample no longer detects extra copy'

class Counted(m.requests.Response):
    calls = 0
    def json(self, **kwargs):
        self.calls += 1
        return super().json(**kwargs)


def counted_response(**kwargs):
    item = m.response(**kwargs)
    item.__class__ = Counted
    item.calls = 0
    return item


normalizations = [('Keep exactly 17 files.', 'Keep exactly 17 files.'),
                  ('Here is the cleaned transcript:\nKeep exactly 17 files.', 'Keep exactly 17 files.'),
                  ('"Keep exactly 17 files."', 'Keep exactly 17 files.'),
                  ('First paragraph.\n\nSecond paragraph.', 'First paragraph.\n\nSecond paragraph.')]
for content, expected in normalizations:
    for text in (source, candidate):
        reply = counted_response(content=content)
        ns = m.load_functions(text, post=lambda *a, **k: reply)
        assert ns['_chat']('Synthetic system.', 'Synthetic request.') == expected
        assert ns['_llm_ok'] is True
        expected_calls = 2 if text == source else 1
        assert reply.calls == expected_calls

bad_replies = [dict(raw=b'not JSON'), dict(payload={'done_reason': 'length', 'message': {'content': 'Partial'}}),
               dict(payload={'done_reason': 'stop'}), dict(payload={'done_reason': 'stop', 'message': {'content': None}}),
               dict(payload=[]), dict(status=503)]
for kwargs in bad_replies:
    for text in (source, candidate):
        ns = m.load_functions(text, post=lambda *a, **k: m.response(**kwargs))
        assert ns['refine']('Keep exactly 17 files.') == 'Keep exactly 17 files.'
        assert ns['_llm_ok'] is False
        try:
            ns['engineer']('Keep exactly 17 files.')
        except RuntimeError as error:
            assert str(error) == 'Rewrite unavailable; original draft and instruction preserved'
        else:
            raise AssertionError('Local engineering failure did not preserve recovery contract')
        assert ns['_llm_ok'] is False

literal_cases = ['Do not send Alex the 12 files.', 'Alex calls Marcus then Marcus calls Alex.',
                 r'Keep C:\Test\draft.txt and --dry-run unchanged.', 'Keep user_id and 17 copies.']
for raw in literal_cases:
    for text in (source, candidate):
        ns = m.load_functions(text, post=lambda *a, **k: m.response(content=raw))
        assert ns['refine'](raw) == raw
        assert ns['engineer'](raw) == (raw, m.MODEL, 0.0)
        ns = m.load_functions(text, post=lambda *a, **k: m.response(content='Discard everything.'))
        assert ns['refine'](raw) == raw
        assert ns['_guard_results'] == [False]

# The extracted candidate must use its actual formatting-aware guard; the
# historical baseline retains its original word-only guard for honest pairing.
raw = 'Keep apples, bananas and milk.'
numbered = '1. Keep apples\n2. bananas\n3. and milk.'
for text, expected, accepted in ((source, raw, False), (candidate, numbered, True)):
    ns = m.load_functions(text, post=lambda *a, **k: m.response(content=numbered))
    assert ns['refine'](raw) == expected
    assert ns['_guard_results'] == [accepted]

# Negative admission checks use in-memory policy calls, never real requests/reads.
sent = []
chat = m.GuardedChat(transport=lambda *a, **k: sent.append((a, k)) or m.response())
valid = {'model': m.MODEL, 'stream': False, 'keep_alive': '5m', 'options': dict(m.OPTIONS),
         'messages': [{'role': 'system', 'content': 'Synthetic system.'},
                      {'role': 'user', 'content': 'Synthetic request.'}]}
for url, body, extra in [(m.URL.replace('127.0.0.1', 'example.invalid'), valid, {}),
                         (m.URL.replace('/api/chat', '/api/delete'), valid, {}),
                         (m.URL, dict(valid, keep_alive=0), {}),
                         (m.URL, valid, {'allow_redirects': True}),
                         (m.URL, dict(valid, model='unapproved'), {}),
                         (m.URL, valid, {'proxies': {}})]:
    try:
        chat.post(url, json=body, **extra)
    except PermissionError:
        pass
    else:
        raise AssertionError('Forbidden request admitted')
assert not sent
assert chat.post(m.URL, json=valid).status_code == 200 and len(sent) == 1
assert sent[0][1]['allow_redirects'] is False
redirect = m.GuardedChat(transport=lambda *a, **k: m.response(status=302))
try:
    redirect.post(m.URL, json=valid)
except PermissionError:
    pass
else:
    raise AssertionError('Redirect response admitted')
for event, args in [('socket.connect', (None, ('example.invalid', 443))),
                    ('socket.connect', (None, ('127.0.0.1', 11434))),
                    ('socket.getaddrinfo', ('example.invalid', 443)),
                    ('winreg.OpenKey', (0, 'Environment')),
                    ('open', (str(m.ROOT / 'settings.json'), 'r', os.O_RDONLY)),
                    ('open', (str(m.ROOT / 'forbidden-output.txt'), 'w', os.O_WRONLY))]:
    try:
        m.guard_event(event, args, writable=())
    except PermissionError:
        pass
    else:
        raise AssertionError('Forbidden I/O policy admitted')

print('Latency contract: decision tables, actual audio/parser/callers, long-gap samples and pre-access guards passed')
