"""Synthetic clipboard transactions, without native clipboard/fields/input."""
from pathlib import Path
import sys
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from multiline_delivery import VerifiedPasteBackend, valid_multiline
from paste_windows import PasteNotSent


class Clipboard:
    def __init__(self):
        self.data = [(13, b'original'), (50000, b'rich-original')]
        self.seq, self.fail, self.verify_fail = 1, '', False
        self.writes, self.opened, self.owner_changed = 0, False, False
    def open(self):
        assert not self.opened
        self.opened = True
        return True
    def close(self): self.opened = False
    def sequence(self): return self.seq
    def owns_clipboard(self): return not self.owner_changed
    def snapshot(self): return [] if self.verify_fail else list(self.data)
    def prepare(self, data): return list(data)
    def release(self, prepared): pass
    def payload(self, text): return [(13, text.encode('utf-16-le'))]
    def replace(self, entries):
        self.writes += 1
        self.seq += 1
        self.data = []
        if self.fail == 'empty': raise OSError('synthetic')
        self.data = list(entries[:1])
        if self.fail == 'first': raise OSError('synthetic')
        self.data = list(entries)
    def shutdown(self): return True


def fixture():
    api = Clipboard()
    clock = [0.]
    backend = VerifiedPasteBackend(api, sleep=lambda t: clock.__setitem__(0, clock[0] + t), clock=lambda: clock[0])
    return api, backend


TEXT = '- apple\n- banana 😀'
for invalid in ('one line', 'a\n\0', 'a\n\t', 'a\rb\n', 'a\n\ud800', 'a\n' + '😀' * 50000):
    assert not valid_multiline(invalid)
assert valid_multiline(TEXT) and valid_multiline('a\r\nb')

api, backend = fixture()
original = list(api.data)
completed = []
for attempt in range(2):
    checks = []
    def send(owned):
        assert owned() and api.data[0][1].decode('utf-16-le') == TEXT
        return 4
    def receipt(timeout):
        assert not api.opened and backend.pending_status()['pending']
        checks.append(1)
        assert api.data != original  # Never restore before the actual receipt.
        return len(checks) >= 3
    result = backend.paste_text(TEXT, check=lambda: True, verify=receipt, send=send,
                                complete=lambda: completed.append(1))
    assert result['status'] == 'pasted' and api.data == original and not backend.pending_status()['pending']
assert len(completed) == 2

# An original worker can exit, then a late receipt resolves without replay.
api, backend = fixture(); original = list(api.data); late = [False]; completed = []
result = backend.paste_text(TEXT, check=lambda: True, verify=lambda timeout: late[0], send=lambda owned: 4,
                           complete=lambda: completed.append(1))
assert result['status'] == 'uncertain' and api.data != original and not completed
assert not backend.shutdown()
assert backend.resolve_pending()['status'] == 'uncertain'
late[0] = True
assert backend.poll_pending()['status'] == 'restored' and api.data == original and completed == [1]

for failure in ('empty', 'first', 'verify'):
    api, backend = fixture(); original = list(api.data); completed = []
    def receipt(timeout):
        api.fail = failure if failure != 'verify' else ''
        api.verify_fail = failure == 'verify'
        return True
    result = backend.paste_text(TEXT, check=lambda: True, verify=receipt, send=lambda owned: 4,
                               complete=lambda: completed.append(1))
    assert result['status'] == 'pasted' and result['clipboard_pending'] and not completed
    api.fail, api.verify_fail = '', False
    assert backend.resolve_pending()['status'] == 'restored'
    assert api.data == original and completed == [1]

api, backend = fixture(); completed = []
def newer(timeout):
    api.replace([(13, b'newer user copy')])
    return True
result = backend.paste_text(TEXT, check=lambda: True, verify=newer, send=lambda owned: 4,
                           complete=lambda: completed.append(1))
assert result['status'] == 'pasted' and result['clipboard_pending']
assert backend.resolve_pending(True)['status'] == 'uncertain' and api.data == [(13, b'newer user copy')]
assert backend.abandon_pending(True, True)['status'] == 'resolved' and completed == [1]

for count in (0, 2):
    api, backend = fixture(); original = list(api.data)
    result = backend.paste_text(TEXT, check=lambda: True, verify=lambda timeout: False,
                               send=lambda owned: count, complete=lambda: None)
    assert result['status'] == ('retained' if count == 0 else 'uncertain')
    assert (api.data == original) == (count == 0)
    if count: assert backend.resolve_pending(True)['status'] == 'restored'

api, backend = fixture(); calls = [0]
def changed_field():
    calls[0] += 1
    return calls[0] == 1
result = backend.paste_text(TEXT, check=changed_field, verify=lambda timeout: False,
                           send=lambda owned: (_ for _ in ()).throw(AssertionError('input forbidden')),
                           complete=lambda: None)
assert result['status'] == 'retained' and api.data == [(13, b'original'), (50000, b'rich-original')]
api, backend = fixture()
api.sequence = lambda: 0
result = backend.paste_text(TEXT, check=lambda: True, verify=lambda timeout: True,
                           send=lambda owned: (_ for _ in ()).throw(AssertionError('unknown ownership cannot send')),
                           complete=lambda: None)
assert result['status'] == 'retained' and result['clipboard_pending'] and api.writes == 1

# Manual fallback keeps the old data, and a later verified paste does not
# prevent restoring it or overwrite it with the later paste's snapshot.
api, backend = fixture()
original = list(api.data)
options = dict(commit_lock=threading.Lock(), check=lambda: True)
assert backend.copy_unsent('Recovered dictation 😀', api.seq, **options)
assert api.data[0][1].decode('utf-16-le') == 'Recovered dictation 😀'
manual = backend.pending_status(include_manual=True)['manual_id']
assert not backend.pending_status()['pending'] and not backend.shutdown()
assert not backend.copy_unsent('Another failure', api.seq, **options)
assert backend.paste_text(TEXT, check=lambda: True, verify=lambda timeout: True,
                          send=lambda owned: 4, complete=lambda: None)['status'] == 'pasted'
assert backend.resolve_manual('stale')['status'] == 'uncertain'
assert backend.resolve_manual(manual)['status'] == 'restored' and api.data == original

api, backend = fixture()
initial = api.seq
api.replace([(13, b'newer')])
assert not backend.copy_unsent('Do not overwrite', initial, **options) and api.writes == 1
assert backend.copy_unsent('Keep this available', api.seq, **options)
manual = backend.pending_status(include_manual=True)['manual_id']
api.owner_changed = True
assert backend.resolve_manual(manual)['status'] == 'uncertain'
api.replace([(13, b'latest')])
assert backend.resolve_manual(manual)['status'] == 'uncertain' and api.data == [(13, b'latest')]
api.replace([])
assert backend.resolve_manual(manual)['status'] == 'restored' and api.data == [(13, b'newer')]

api, backend = fixture()
original_snapshot = api.snapshot
def delayed_render():
    api.seq += 1
    return original_snapshot()
api.snapshot = delayed_render
assert not backend.copy_unsent('Delayed newer copy', api.seq, **options) and api.writes == 0

api, backend = fixture()
original = list(api.data)
assert not backend.copy_unsent('Cleared dictation', api.seq, commit_lock=threading.Lock(), check=lambda: False)
assert api.writes == 0
replace = api.replace
def partial_write(entries):
    api.fail = 'first' if api.writes == 0 else ''
    replace(entries)
api.replace = partial_write
assert not backend.copy_unsent('Partial publication', api.seq, **options)
assert api.data == original and not backend.pending_status(include_manual=True)['pending']
print('Verified paste: consecutive receipts, no timer restore, delayed recovery, partial restores, newer copy, zero/partial input and changed-field refusal passed')
