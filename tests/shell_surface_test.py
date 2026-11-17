"""The taskbar, its tray flyout and the desktop must never be a dictation destination.

Typing there is not text entry: the trailing space activates the focused tray icon (for example iCloud).
Fake user32 only; no real window, input or device is touched.
"""
import ctypes
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import paste_windows
import text_input_windows as tiw

CLASSES = {1: 'Chrome_WidgetWin_1', 2: 'Notepad', 3: 'Windows.UI.Core.CoreWindow',
           10: 'Shell_TrayWnd', 11: 'Shell_SecondaryTrayWnd', 12: 'NotifyIconOverflowWindow',
           13: 'TopLevelWindowForOverflowXamlIsland', 14: 'Progman', 15: 'WorkerW'}


class FakeUser:
    def __init__(self, foreground):
        self.foreground = foreground

    def GetForegroundWindow(self):
        return self.foreground

    def IsWindow(self, hwnd):
        return hwnd in CLASSES

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = CLASSES.get(hwnd, '')
        return len(buffer.value)


def text_native(foreground):
    native = tiw.WindowsNative.__new__(tiw.WindowsNative)
    native.user = FakeUser(foreground)
    native.pid_of = lambda hwnd: 4242
    native._focused_child = lambda hwnd: hwnd  # the window itself holds focus
    return native


def paste_native(foreground):
    native = paste_windows.WindowsNative.__new__(paste_windows.WindowsNative)
    native.user = FakeUser(foreground)
    return native


# Ordinary apps, and Start/Search, remain valid destinations.
for hwnd in (1, 2, 3):
    target = text_native(hwnd).capture_target()
    assert target is not None and target.hwnd == hwnd, CLASSES[hwnd]
    assert paste_native(hwnd).valid(hwnd), CLASSES[hwnd]

# Every shell surface is refused by both routes, so the text stays in recovery.
for hwnd in (10, 11, 12, 13, 14, 15):
    assert text_native(hwnd).capture_target() is None, CLASSES[hwnd]
    assert not paste_native(hwnd).valid(hwnd), CLASSES[hwnd]

# A destination captured earlier is refused if it was somehow a shell surface, and ready checks never accept one.
native = text_native(10)
shell_target = tiw.Target(10, 4242, 10)
assert not native.target_focused(shell_target) and not native.target_ready(shell_target)
good = tiw.Target(1, 4242, 1)
assert text_native(1).target_focused(good)

# Focus moving from an app to the tray before delivery is refused, as before.
assert not text_native(10).target_focused(good)

# The constant must match what the probe found on this machine (Windows 11 taskbar and hidden-icons flyout).
assert {'Shell_TrayWnd', 'TopLevelWindowForOverflowXamlIsland'} <= tiw.SHELL_SURFACE_CLASSES
assert 'Windows.UI.Core.CoreWindow' not in tiw.SHELL_SURFACE_CLASSES
print('shell surfaces: taskbar, tray flyout and desktop refused; apps, Start and Search accepted')
