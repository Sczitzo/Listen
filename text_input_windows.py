"""Bounded Windows Unicode text entry. No clipboard access or input retries.

An accepted input batch does not prove editor insertion. Real-editor Unicode,
focus, and accessibility verification is required before production use.
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import unicodedata


MAX_UTF16_UNITS = 4096
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
_MODIFIERS = (0x10, 0x11, 0x12, 0x5B, 0x5C)
# Windows shell surfaces that are never a text destination: the taskbar, its hidden-icons tray flyout and the
# desktop. Typing there is not text entry; the trailing space activates the focused tray icon (for example iCloud).
# Start and Search (Windows.UI.Core.CoreWindow) are deliberately NOT listed: they accept typing.
SHELL_SURFACE_CLASSES = frozenset(('Shell_TrayWnd', 'Shell_SecondaryTrayWnd', 'NotifyIconOverflowWindow',
                                   'TopLevelWindowForOverflowXamlIsland', 'Progman', 'WorkerW'))


class _Keyboard(ctypes.Structure):
    _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD),
                ('flags', wintypes.DWORD), ('time', wintypes.DWORD),
                ('extra', ctypes.c_size_t)]


class _Mouse(ctypes.Structure):
    _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG),
                ('data', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]


class _Hardware(ctypes.Structure):
    _fields_ = [('message', wintypes.DWORD), ('low', wintypes.WORD),
                ('high', wintypes.WORD)]


class _Data(ctypes.Union):
    _fields_ = [('keyboard', _Keyboard), ('mouse', _Mouse), ('hardware', _Hardware)]


class _Input(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('data', _Data)]


class _GuiThreadInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('flags', wintypes.DWORD),
                ('active', wintypes.HWND), ('focus', wintypes.HWND),
                ('capture', wintypes.HWND), ('menu', wintypes.HWND),
                ('move_size', wintypes.HWND), ('caret', wintypes.HWND),
                ('caret_rect', wintypes.RECT)]


@dataclass(frozen=True)
class Target:
    hwnd: int
    pid: int
    focus: int = 0
    field_token: str | None = field(default=None, compare=False, repr=False)


class WindowsNative:
    def __init__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        for name, args, result in (
            ('GetForegroundWindow', [], wintypes.HWND),
            ('IsWindow', [wintypes.HWND], wintypes.BOOL),
            ('GetWindowThreadProcessId', [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            ('GetGUIThreadInfo', [wintypes.DWORD, ctypes.POINTER(_GuiThreadInfo)], wintypes.BOOL),
            ('IsChild', [wintypes.HWND, wintypes.HWND], wintypes.BOOL),
            ('GetAsyncKeyState', [ctypes.c_int], ctypes.c_short),
            ('GetClipboardSequenceNumber', [], wintypes.DWORD),
            ('GetClassNameW', [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ('SendInput', [wintypes.UINT, ctypes.POINTER(_Input), ctypes.c_int], wintypes.UINT),
        ):
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result

    def pid_of(self, hwnd):
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def clipboard_sequence(self):
        return self.user.GetClipboardSequenceNumber()

    def shell_surface(self, hwnd):
        buffer = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, buffer, 256)
        return buffer.value in SHELL_SURFACE_CLASSES

    def capture_target(self):
        hwnd = self.user.GetForegroundWindow()
        if hwnd and self.shell_surface(hwnd):
            return None  # taskbar, tray flyout or desktop: keep the text in recovery instead
        pid = self.pid_of(hwnd) if hwnd and self.user.IsWindow(hwnd) else 0
        focus = self._focused_child(hwnd) if hwnd and pid else 0
        return Target(hwnd, pid, focus) if hwnd and pid and focus else None

    def _focused_child(self, hwnd):
        pid = wintypes.DWORD()
        thread = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        info = _GuiThreadInfo()
        info.size = ctypes.sizeof(info)
        if not thread or not self.user.GetGUIThreadInfo(thread, ctypes.byref(info)):
            return 0
        # Menus can consume Unicode without inserting it into the editor.
        if info.flags & (0x0004 | 0x0008 | 0x0010):
            return 0
        focus = int(info.focus or 0)
        return focus if focus and (focus == hwnd or self.user.IsChild(hwnd, focus)) else 0

    def target_focused(self, target):
        if (not self.user.IsWindow(target.hwnd)
                or self.user.GetForegroundWindow() != target.hwnd
                or self.pid_of(target.hwnd) != target.pid
                or self.shell_surface(target.hwnd)):
            return False
        focus = self._focused_child(target.hwnd)
        return bool(focus and (not target.focus or focus == target.focus))

    def target_ready(self, target):
        return bool(self.target_focused(target)
                    and not any(self.user.GetAsyncKeyState(key) & 0x8000
                                for key in _MODIFIERS))

    def send_units(self, units, target):
        events = (_Input * (2 * len(units)))()
        for index, unit in enumerate(units):
            for offset, flags in ((0, KEYEVENTF_UNICODE),
                                  (1, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)):
                event = events[2 * index + offset]
                event.type = 1
                event.data.keyboard = _Keyboard(0, unit, flags, 0, 0)
        # Check again after allocation. Never focus another window or split the
        # immutable batch; Windows can still change foreground after this check.
        if not self.target_ready(target):
            return 0
        return self.user.SendInput(len(events), events, ctypes.sizeof(_Input))


def send_text(text, target, native=None, wait_ready=None):
    """Send one bounded, single-line Unicode batch; never replay any outcome."""
    result = {'status': 'retained', 'reason': 'invalid_input',
              'accepted_count': 0, 'expected_count': 0}
    if not isinstance(text, str) or not text:
        return result
    if len(text) > MAX_UTF16_UNITS:
        result['reason'] = 'input_too_large'
        return result
    # No Enter/Tab virtual keys, control characters, or Unicode line breaks.
    if any(unicodedata.category(character) == 'Cc' or character in '\u2028\u2029'
           for character in text):
        result['reason'] = 'control_or_multiline_input'
        return result
    try:
        encoded = text.encode('utf-16-le')
    except UnicodeEncodeError:
        return result
    if len(encoded) // 2 > MAX_UTF16_UNITS:
        result['reason'] = 'input_too_large'
        return result
    if (not isinstance(target, Target) or type(target.hwnd) is not int
            or type(target.pid) is not int or target.hwnd <= 0 or target.pid <= 0):
        result['reason'] = 'invalid_target'
        return result
    units = tuple(int.from_bytes(encoded[index:index + 2], 'little')
                  for index in range(0, len(encoded), 2))
    result['expected_count'] = 2 * len(units)
    attempted = False
    try:
        api = native if native is not None else WindowsNative()
        ready = wait_ready(api, target) if wait_ready is not None else api.target_ready(target)
        if not ready:
            result['reason'] = 'target_or_modifiers_unavailable'
            return result
        attempted = True
        accepted = api.send_units(units, target)
        result['accepted_count'] = accepted
        if accepted == result['expected_count']:
            result.update(status='sent', reason='input_accepted_insertion_unverified')
        elif accepted == 0:
            result['reason'] = 'input_not_sent'
        else:
            result.update(status='uncertain', reason='partial_input_do_not_retry')
    except Exception:
        result.update(status='uncertain' if attempted else 'retained',
                      reason='input_outcome_unknown' if attempted else 'native_unavailable',
                      accepted_count=None if attempted else 0)
    return result
