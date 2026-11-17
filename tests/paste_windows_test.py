"""Pure fake-native checks: no real clipboard, input, or windows."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paste_windows import (PasteBackend, WindowsNative, PasteNotSent,
                           WM_RENDERFORMAT, WM_RENDERALLFORMATS, WM_DESTROYCLIPBOARD)
from types import SimpleNamespace
import ctypes


class Fake:
    def __init__(self, data=None):
        self.data = list(data or [])
        self.hwnd = 10
        self.seq = 1
        self.busy = 0
        self.opens = 0
        self.closed = 0
        self.sent = 0
        self.unsupported = False
        self.fail_send = False
        self.new_copy = False
        self.change_focus = False
        self.fail_replace = False
        self.released = []
        self.consumed = True
        self.delivery_seq = None
        self.fail_restore = False

    def foreground(self): return self.hwnd
    def valid(self, hwnd): return hwnd == 10
    def open(self):
        self.opens += 1
        if self.busy:
            self.busy -= 1
            return False
        return True
    def close(self): self.closed += 1
    def sequence(self): return self.seq
    def snapshot(self):
        if self.unsupported: raise OSError('unsupported/delayed')
        return list(self.data)
    def prepare(self, data): return list(data)
    def release(self, prepared): self.released.append(prepared)
    def payload(self, text): return [(13, text.encode())]
    def replace(self, prepared):
        self.seq += 1
        self.data = list(prepared)
        if self.delivery_seq is not None:
            self.delivery_seq = self.seq
            if self.fail_restore:
                self.data = []
                raise OSError('partial restore')
        if self.fail_replace:
            self.fail_replace = False
            raise OSError('partial mutation')
        if self.change_focus:
            self.hwnd = 11
    def publish_delivery(self, entries, target):
        try:
            self.replace(entries)
        finally:
            self.delivery_seq = self.seq
    def delivery_sequence(self): return self.delivery_seq
    def delivery_consumed(self, target, timeout=1): return self.consumed
    def end_delivery(self): pass
    def materialize_delivery(self): pass
    def send_paste(self):
        self.sent += 1
        if self.new_copy:
            self.data = [(13, b'user copy')]
            self.seq += 1
        if self.fail_send: raise OSError('injection')


def backend(fake): return PasteBackend(fake, sleep=lambda _: None)


def run():
    assert (WM_RENDERFORMAT, WM_RENDERALLFORMATS, WM_DESTROYCLIPBOARD) == (0x305, 0x306, 0x307)
    for original in ([], [(13, b'unicode'), (1, b'ansi')], [(8, b'dib image')],
                     [(15, b'file drop')], [(49152, b'HTML'), (49153, b'RTF')]):
        fake = Fake(original)
        api = backend(fake)
        assert api.capture_target() == 10
        assert api.paste_text('dictation', 10)['status'] == 'pasted'
        assert fake.data == original and fake.sent == 1
        assert fake.closed == 2 and len(fake.released) == 1
    for target in (None, 11):
        fake = Fake([(13, b'original')])
        assert backend(fake).paste_text('text', target)['status'] == 'retained'
        assert fake.opens == 0 and fake.data == [(13, b'original')]
    fake = Fake(); fake.hwnd = 11
    assert backend(fake).capture_target() is None
    fake = Fake(); fake.busy = 100
    assert backend(fake).paste_text('text', 10)['status'] == 'retained'
    assert fake.opens == 5 and fake.sent == 0
    fake = Fake([(13, b'original')]); fake.unsupported = True
    assert backend(fake).paste_text('text', 10)['status'] == 'failed'
    assert fake.data == [(13, b'original')] and fake.sent == 0
    for failure in ('fail_replace',):
        fake = Fake([(8, b'image')]); setattr(fake, failure, True)
        assert backend(fake).paste_text('text', 10)['status'] == 'failed'
        assert fake.data == [(8, b'image')]
    fake = Fake([(13, b'original')]); fake.new_copy = True
    assert backend(fake).paste_text('text', 10)['status'] == 'uncertain'
    assert fake.data == [(13, b'user copy')]
    fake = Fake([(13, b'original')]); fake.change_focus = True
    assert backend(fake).paste_text('text', 10)['status'] == 'retained'
    assert fake.sent == 0 and fake.data == [(13, b'original')]
    fake = Fake()
    api = PasteBackend(fake, send_paste=lambda: setattr(fake, 'sent', 7), sleep=lambda _: None)
    assert api.paste_text('text', 10)['status'] == 'pasted' and fake.sent == 7
    for text in ('', None, 'bad\0text'):
        fake = Fake()
        assert backend(fake).paste_text(text, 10)['status'] == 'retained' and fake.opens == 0
    # A queued paste can read arbitrarily later. Never restore on elapsed time,
    # nor replace dictation A with B while A's injected keys may still run.
    fake = Fake([(13, b'private original')]); fake.consumed = False
    api = backend(fake)
    assert api.paste_text('dictation A', 10)['status'] == 'uncertain'
    first_receipt = dict(api.last_delivery_metadata)
    assert fake.data == [(13, b'dictation A')] and fake.sent == 1
    assert api.paste_text('dictation B', 10)['status'] == 'retained'
    assert api.last_delivery_metadata == first_receipt, 'Blocked attempts must preserve the unresolved delivery receipt'
    assert fake.data == [(13, b'dictation A')] and fake.sent == 1
    assert api.pending_status()['pending'] and not fake.released
    assert api.resolve_pending()['status'] == 'uncertain'
    fake.consumed = True
    assert api.resolve_pending()['status'] == 'restored'
    assert fake.data == [(13, b'private original')]
    assert not api.pending_status()['pending']
    fake = Fake([(13, b'original')]); fake.fail_send = True; fake.consumed = False
    api = backend(fake)
    assert api.paste_text('new', 10)['status'] == 'uncertain'
    assert fake.data == [(13, b'new')] and api.pending_status()['pending']
    fake.seq += 1; fake.data = [(13, b'new user copy')]
    assert api.resolve_pending(True)['status'] == 'uncertain'
    assert fake.data == [(13, b'new user copy')] and not fake.released
    assert api.abandon_pending(True)['status'] == 'uncertain'
    assert api.abandon_pending(False, True)['status'] == 'uncertain'
    assert api.abandon_pending(True, True)['status'] == 'resolved'
    assert not api.pending_status()['pending'] and fake.data == [(13, b'new user copy')]
    fake = Fake([(13, b'original')]); fake.consumed = False
    api = backend(fake)
    api.paste_text('new', 10)
    fake.consumed = True
    assert not api.pending_status()['pending']
    assert fake.data == [(13, b'original')]
    fake = Fake([(13, b'original'), (49160, b'opaque editor metadata')])
    api = backend(fake)
    fake.fail_restore = True
    assert api.paste_text('new', 10)['status'] == 'uncertain'
    assert api.pending['saved'] == [(13, b'original'), (49160, b'opaque editor metadata')]
    fake.fail_restore = False
    assert api.resolve_pending()['status'] == 'restored'
    assert fake.data == [(13, b'original'), (49160, b'opaque editor metadata')]
    fake = Fake([(13, b'original')])
    def blocked_input():
        raise PasteNotSent('no input')
    api = PasteBackend(fake, send_paste=blocked_input, sleep=lambda _: None)
    assert api.paste_text('new', 10)['status'] == 'retained'
    assert fake.data == [(13, b'original')] and api.pending is None
    for accepted in (0, 1, 2, 3, 4):
        native = WindowsNative.__new__(WindowsNative)
        releases = []
        native.user = SimpleNamespace(GetAsyncKeyState=lambda key: 0,
            SendInput=lambda count, events, size: accepted,
            keybd_event=lambda vk, scan, flags, extra: releases.append(vk))
        try:
            native.send_paste()
            assert accepted == 4
        except PasteNotSent:
            assert accepted == 0
        except OSError:
            assert accepted in (1, 2, 3)
        assert releases == ({0: [], 1: [0x11], 2: [0x56, 0x11], 3: [0x11], 4: []}[accepted])
    # Attribution rejects unknown readers and clipboard managers, including
    # a later target read after a manager has already rendered another format.
    for first in (0, 999):
        delivery = {'target_pid': 42, 'first_reader': 0, 'read': False}
        WindowsNative._record_reader(delivery, first)
        WindowsNative._record_reader(delivery, 42)
        assert not delivery['read']
    delivery = {'target_pid': 42, 'first_reader': 0, 'read': False}
    WindowsNative._record_reader(delivery, 42)
    assert delivery['read']
    native = WindowsNative.__new__(WindowsNative)
    native._delivery = dict(delivery, error=False)
    native.user = SimpleNamespace(GetOpenClipboardWindow=lambda: 12)
    assert not native.delivery_consumed(10, timeout=0)
    native.user.GetOpenClipboardWindow = lambda: 0
    assert native.delivery_consumed(10, timeout=0)
    # Registered Chromium/VSCode-style metadata is restorable when proven to be
    # bounded HGLOBAL memory; ordinary unsupported standard handles still fail.
    native = WindowsNative.__new__(WindowsNative)
    opaque = ctypes.create_string_buffer(b'opaque\0editor-data')
    def format_name(fmt, output, size):
        output.value = 'Chromium Web Custom MIME Data Format'
        return len(output.value)
    native.user = SimpleNamespace(
        EnumClipboardFormats=lambda previous: 49160 if previous == 0 else 0,
        GetClipboardFormatNameW=format_name, GetClipboardData=lambda fmt: 100)
    native.kernel = SimpleNamespace(GlobalSize=lambda handle: len(opaque),
        GlobalLock=lambda handle: ctypes.addressof(opaque), GlobalUnlock=lambda handle: True)
    assert native.snapshot() == [(49160, opaque.raw)]
    # CloseClipboard synthesizes missing text forms and advances sequence.
    # Publishing all four forms is the regression guard without OS mutation.
    native = WindowsNative.__new__(WindowsNative)
    formats = {}
    def register(name):
        return formats.setdefault(name, 49152 + len(formats))
    native.user = SimpleNamespace(RegisterClipboardFormatW=register)
    native.kernel = SimpleNamespace(GetThreadLocale=lambda: 0x409)
    unicode_sample = 'caf\u00e9 \u6f22\u5b57 \U0001f600'
    payload = dict(native.payload(unicode_sample))
    assert payload[13] == (unicode_sample + '\0').encode('utf-16-le')
    assert payload[1] == (unicode_sample + '\0').encode('mbcs', errors='replace')
    assert payload[7] == (unicode_sample + '\0').encode('oem', errors='replace')
    assert payload[16] == b'\x09\x04\0\0'
    assert len(formats) == 3 and all(payload[f] == b'\0' * 4 for f in formats.values())
    print('Paste backend checks passed (fake native API only).')


if __name__ == '__main__': run()
