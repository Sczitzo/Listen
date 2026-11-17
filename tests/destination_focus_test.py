"""Native destination focus must stay bound before Unicode entry.

Uses a fake Windows API only; no windows, clipboard, or actual input.
"""
import ctypes
from ctypes import wintypes
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from text_input_windows import WindowsNative, send_text


class GuiInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('active', wintypes.HWND), ('focus', wintypes.HWND),
                ('capture', wintypes.HWND), ('menu_owner', wintypes.HWND),
                ('move_size', wintypes.HWND), ('caret', wintypes.HWND),
                ('caret_rect', wintypes.RECT)]


class User:
    def __init__(self):
        self.foreground = 10
        self.pid = 42
        self.focus = 100
        self.flags = 0
        self.gui_available = True
        self.calls = []

    def GetForegroundWindow(self):
        return self.foreground

    def IsWindow(self, hwnd):
        return hwnd in (10, 100, 101)

    def GetWindowThreadProcessId(self, hwnd, output):
        output._obj.value = self.pid
        return 5

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = 'Notepad'
        return 7

    def GetGUIThreadInfo(self, thread, output):
        assert thread == 5
        if not self.gui_available:
            return False
        info = ctypes.cast(output, ctypes.POINTER(GuiInfo)).contents
        info.flags, info.active, info.focus = self.flags, 10, self.focus
        return True

    def IsChild(self, parent, child):
        return parent == 10 and child in (100, 101)

    def GetAsyncKeyState(self, key):
        return 0

    def SendInput(self, count, events, size):
        self.calls.append(count)
        return count


def setup():
    native = WindowsNative.__new__(WindowsNative)
    native.user = User()
    target = native.capture_target()
    assert target is not None and native.target_ready(target)
    return native, target


# Opening a native menu must not let Unicode input activate or disappear in it.
for menu_flag in (0x0004, 0x0008, 0x0010):
    native, target = setup()
    native.user.flags = menu_flag
    result = send_text('latest exact dictation', target, native)
    assert result['status'] == 'retained', 'Native menu accepted automatic text entry'
    assert not native.user.calls, 'Native menu must receive no input batch'

# A different child editor in the same top-level HWND/PID is a new destination.
native, target = setup()
native.user.focus = 101
assert not native.target_ready(target), 'Same-window focus drift kept the old target valid'
assert send_text('latest exact dictation', target, native)['status'] == 'retained'
assert not native.user.calls

# Loss of focus information is not evidence that the old editor is ready.
for failure in ('missing_focus', 'gui_unavailable'):
    native, target = setup()
    if failure == 'missing_focus':
        native.user.focus = 0
    else:
        native.user.gui_available = False
    assert not native.target_ready(target), 'Missing native focus must block automatic entry'
    assert not native.user.calls

native, target = setup()
assert send_text('two independent words', target, native)['status'] == 'sent'
assert len(native.user.calls) == 1
print('Destination focus: menu modes, same-window child changes and missing focus '
      'block entry; unchanged native destination accepts one batch')
