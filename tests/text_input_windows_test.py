"""Pure fake-user32 checks. Never creates windows or sends actual input."""
import ctypes
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from text_input_windows import (MAX_UTF16_UNITS, Target, WindowsNative,
                                send_text, KEYEVENTF_UNICODE, KEYEVENTF_KEYUP)


class FakeUser:
    def __init__(self):
        self.foreground = 10
        self.pid = 42
        self.valid = True
        self.held = set()
        self.calls = []
        self.accepted = None
        self.failure = False
        self.foreground_reads = 0
        self.change_before_send = False

    def GetForegroundWindow(self):
        self.foreground_reads += 1
        return 11 if self.change_before_send and self.foreground_reads > 1 else self.foreground

    def IsWindow(self, hwnd):
        return self.valid and hwnd == 10

    def GetWindowThreadProcessId(self, hwnd, output):
        output._obj.value = self.pid
        return 5

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = 'Notepad'
        return 7

    def GetGUIThreadInfo(self, thread, output):
        output._obj.focus = 100
        output._obj.flags = 0
        return True

    def IsChild(self, parent, child):
        return parent == 10 and child == 100

    def GetAsyncKeyState(self, key):
        return 0x8000 if key in self.held else 0

    def SendInput(self, count, events, size):
        self.calls.append([(event.type, event.data.keyboard.vk,
                            event.data.keyboard.scan, event.data.keyboard.flags)
                           for event in events])
        if self.failure:
            raise OSError('simulated ambiguous native call')
        return count if self.accepted is None else self.accepted


def setup():
    user = FakeUser()
    native = WindowsNative.__new__(WindowsNative)
    native.user = user
    return user, native


def main():
    target = Target(10, 42, 100)
    user, native = setup()
    assert native.capture_target() == target
    result = send_text('Aé漢😀', target, native)
    units = [0x41, 0xE9, 0x6F22, 0xD83D, 0xDE00]
    assert result == {'status': 'sent', 'reason': 'input_accepted_insertion_unverified',
                      'accepted_count': 10, 'expected_count': 10}
    assert len(user.calls) == 1
    assert user.calls[0] == [event for unit in units for event in (
        (1, 0, unit, KEYEVENTF_UNICODE),
        (1, 0, unit, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP))]
    for text in (None, '', 17, '\0', 'a\nb', 'a\rb', '\t', '\x1b', '\x7f', '\x85', '\u2028', '\u2029', '\ud800'):
        user, native = setup()
        assert send_text(text, target, native)['status'] == 'retained'
        assert not user.calls
    user, native = setup()
    assert send_text('😀' * (MAX_UTF16_UNITS // 2 + 1), target, native)['reason'] == 'input_too_large'
    assert not user.calls
    assert send_text('a' * MAX_UTF16_UNITS, target, native)['status'] == 'sent'
    assert len(user.calls) == 1 and len(user.calls[0]) == 2 * MAX_UTF16_UNITS
    for invalid in (None, 10, Target(0, 42), Target(10, 0), Target(True, 42), Target(10, '42')):
        user, native = setup()
        assert send_text('text', invalid, native)['status'] == 'retained'
        assert not user.calls
    for change in ('foreground', 'pid', 'valid', 'change_before_send'):
        user, native = setup()
        setattr(user, change, {'foreground': 11, 'pid': 43, 'valid': False,
                              'change_before_send': True}[change])
        assert send_text('text', target, native)['status'] == 'retained'
        assert not user.calls
    for modifier in (0x10, 0x11, 0x12, 0x5B, 0x5C):
        user, native = setup()
        user.held.add(modifier)
        assert send_text('text', target, native)['status'] == 'retained'
        assert not user.calls
    for count in range(5):
        user, native = setup()
        user.accepted = count
        result = send_text('ab', target, native)
        assert result['accepted_count'] == count
        assert result['status'] == ('retained' if count == 0 else 'sent' if count == 4 else 'uncertain')
        assert len(user.calls) == 1  # No replay or modifier-cleanup input.
    user, native = setup()
    user.failure = True
    result = send_text('text', target, native)
    assert result['status'] == 'uncertain' and result['accepted_count'] is None
    assert len(user.calls) == 1
    unavailable = SimpleNamespace(target_ready=lambda target: (_ for _ in ()).throw(OSError()))
    assert send_text('text', target, unavailable)['status'] == 'retained'
    # FakeUser deliberately has no clipboard APIs: any accidental use fails.
    print('PASS: isolated Unicode input prototype; fake-native only, no actual input')


if __name__ == '__main__':
    main()
