"""Windows registration and release events for non-legacy shortcuts."""

import ctypes
import logging
import threading
import time
from ctypes import wintypes


_WM_HOTKEY = 0x0312
_MOD_NOREPEAT = 0x4000
_MAX_ID = 0xBFFF
_POLL_SECONDS = 0.01
_COMMAND_TIMEOUT = 2
_MODIFIERS = ((1, (0x12,)), (2, (0x11,)), (4, (0x10,)), (8, (0x5B, 0x5C)))
_ESCAPE = (0, 0x1B)
_NO_PROPOSAL = object()


class _Message(ctypes.Structure):
    _fields_ = [('hwnd', wintypes.HWND), ('message', wintypes.UINT),
                ('wparam', wintypes.WPARAM), ('lparam', wintypes.LPARAM),
                ('time', wintypes.DWORD), ('point', wintypes.POINT),
                ('private', wintypes.DWORD)]


class _Win32:
    def __init__(self):
        user = ctypes.WinDLL('user32', use_last_error=True)
        self._ticks = ctypes.WinDLL('kernel32').GetTickCount
        self._ticks.argtypes = ()
        self._ticks.restype = wintypes.DWORD
        self._register = user.RegisterHotKey
        self._register.argtypes = (wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT)
        self._register.restype = wintypes.BOOL
        self._unregister = user.UnregisterHotKey
        self._unregister.argtypes = (wintypes.HWND, ctypes.c_int)
        self._unregister.restype = wintypes.BOOL
        self._peek = user.PeekMessageW
        self._peek.argtypes = (ctypes.POINTER(_Message), wintypes.HWND, wintypes.UINT,
                               wintypes.UINT, wintypes.UINT)
        self._peek.restype = wintypes.BOOL
        self._key_state = user.GetAsyncKeyState
        self._key_state.argtypes = (ctypes.c_int,)
        self._key_state.restype = ctypes.c_short
        self._message = _Message()
        self._peek(ctypes.byref(self._message), None, 0, 0, 0)

    def register(self, ident, combo):
        modifiers, key = combo
        return bool(self._register(None, ident, modifiers | _MOD_NOREPEAT, key))

    def unregister(self, ident):
        return bool(self._unregister(None, ident))

    def hotkey_message(self):
        if self._peek(ctypes.byref(self._message), None, _WM_HOTKEY, _WM_HOTKEY, 1):
            packed = self._message.lparam
            return self._message.wparam, (packed & 0xF, (packed >> 16) & 0xFFFF), self._message.time
        return None

    def down(self, key):
        return bool(self._key_state(key) & 0x8000)

    def ticks(self):
        return self._ticks()


def _valid_combo(combo):
    return combo is None or (
        isinstance(combo, tuple) and len(combo) == 2 and
        type(combo[0]) is int and 0 <= combo[0] <= 0xF and
        type(combo[1]) is int and 0 < combo[1] < 0xFF
    )


class _Registration:
    def __init__(self, native, on_edge, cancel_when=None, on_cancel=None):
        self.native = native
        self.on_edge = on_edge
        self.cancel_when = cancel_when
        self.on_cancel = on_cancel
        self.current = None
        self.proposed = _NO_PROPOSAL
        self.held = False
        self.trigger_seen_down = False
        self.trigger_released_at = None
        self.next_id = 1
        self.active_since = None
        self.cancel_id = None
        self.cancel_attempted = False
        self.cancel_fired = False

    def _allocate_id(self):
        reserved = {value[0] for value in (self.current, self.proposed)
                    if value is not None and value is not _NO_PROPOSAL}
        reserved.add(self.cancel_id)
        while self.next_id in reserved:
            self.next_id = self.next_id % _MAX_ID + 1
        ident = self.next_id
        self.next_id = ident % _MAX_ID + 1
        return ident

    def prepare(self, combo):
        if self.proposed is not _NO_PROPOSAL or self.held:
            return False
        if self.current is not None and self.current[1] == combo:
            self.proposed = self.current
            return True
        if combo is None:
            self.proposed = None
            return True
        ident = self._allocate_id()
        if not self.native.register(ident, combo):
            return False
        self.proposed = (ident, combo)
        return True

    def finish(self, commit):
        if self.proposed is _NO_PROPOSAL:
            return False
        discard = self.current if commit else self.proposed
        if discard and self.current != self.proposed and not self.native.unregister(discard[0]):
            return False
        if commit:
            self.current = self.proposed
        self.proposed = _NO_PROPOSAL
        self.active_since = self.native.ticks() if self.current else None
        return True

    def replace(self, combo):
        if not self.prepare(combo):
            return False
        if self.finish(True):
            return True
        self.finish(False)
        return False

    def _emit(self, combo, pressed, at):
        try:
            self.on_edge(combo, pressed, at)
        except Exception:
            logging.getLogger(__name__).error('Windows shortcut event handler failed')

    def press(self, message, at):
        ident, combo, ticks = message
        if ident == self.cancel_id and combo == _ESCAPE:
            if not self.cancel_fired and self._can_cancel():
                self.cancel_fired = True
                try:
                    self.on_cancel()
                except Exception:
                    logging.getLogger(__name__).error('Windows shortcut cancel handler failed')
            return
        if not self.held and self.proposed is _NO_PROPOSAL and self.current == (ident, combo):
            if self.active_since is not None:
                age = (ticks - self.active_since) & 0xFFFFFFFF
                if not 0 < age < 0x80000000:
                    return
            self.held = True
            self.trigger_seen_down = self.native.down(combo[1])
            self.trigger_released_at = None
            self._emit(combo, True, at)

    def _can_cancel(self):
        return (self.current is not None and self.cancel_when is not None
                and self.on_cancel is not None and self.cancel_when())

    def sync_cancel(self):
        if self._can_cancel():
            if not self.cancel_attempted:
                self.cancel_attempted = True
                ident = self._allocate_id()
                if self.native.register(ident, _ESCAPE):
                    self.cancel_id = ident
        else:
            if self.cancel_id is not None:
                # Exit this worker if Windows leaves Escape claimed outside recording.
                if not self.native.unregister(self.cancel_id):
                    raise OSError('Could not release Escape registration')
                self.cancel_id = None
            self.cancel_attempted = self.cancel_fired = False

    def poll_release(self, at):
        if not self.held:
            return
        modifiers, key = self.current[1]
        # shortcut: releases are sampled from Windows; Raw Input would be needed to resolve release-and-repress between polls.
        if self.native.down(key):
            self.trigger_seen_down = True
            self.trigger_released_at = None
            return
        if self.trigger_seen_down and self.trigger_released_at is None:
            self.trigger_released_at = at
        if any(
            self.native.down(code) for flag, codes in _MODIFIERS if modifiers & flag for code in codes
        ):
            return
        self.held = False
        released_at = self.trigger_released_at if self.trigger_released_at is not None else at
        self._emit(self.current[1], False, released_at)

    def close(self):
        registrations = {value[0] for value in (self.current, self.proposed)
                         if value is not None and value is not _NO_PROPOSAL}
        if self.cancel_id is not None:
            registrations.add(self.cancel_id)
        results = [self.native.unregister(ident) for ident in registrations]
        return all(results)


class _Command:
    def __init__(self, method, value):
        self.method = method
        self.value = value
        self.finished = threading.Event()
        self.result = False


class RegisteredHotkey:
    """Own a single Windows shortcut, with transactional replacement."""

    def __init__(self, on_edge, cancel_when=None, on_cancel=None):
        self._on_edge = on_edge
        self._cancel_when = cancel_when
        self._on_cancel = on_cancel
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._pending = None
        self._stopping = False
        self._cleanup_ok = True
        self._thread = threading.Thread(target=self._run, name='windows-hotkey', daemon=True)
        self._thread.start()

    def _call(self, method, value):
        command = _Command(method, value)
        if threading.current_thread() is self._thread:
            return False
        with self._lock:
            if self._stopping or self._pending is not None:
                return False
            self._pending = command
            self._wake.set()
        if not command.finished.wait(_COMMAND_TIMEOUT):
            # The same lock covers the short Win32 reservation calls and cancellation.
            with self._lock:
                if self._pending is command:
                    self._pending = None
        return command.result

    def prepare(self, combo):
        return _valid_combo(combo) and self._call('prepare', combo)

    def finish(self, commit):
        return type(commit) is bool and self._call('finish', commit)

    def replace(self, combo):
        return _valid_combo(combo) and self._call('replace', combo)

    def close(self):
        if threading.current_thread() is self._thread:
            return False
        with self._lock:
            self._stopping = True
            self._wake.set()
        self._thread.join(_COMMAND_TIMEOUT)
        return not self._thread.is_alive() and self._cleanup_ok

    def _run(self):
        state = None
        try:
            state = _Registration(_Win32(), self._on_edge, self._cancel_when, self._on_cancel)
            while True:
                with self._lock:
                    self._wake.clear()
                    if self._stopping:
                        break
                    command, self._pending = self._pending, None
                    if command is not None:
                        try:
                            command.result = getattr(state, command.method)(command.value)
                        finally:
                            command.finished.set()
                state.sync_cancel()
                watching = state.current is not None or state.proposed is not _NO_PROPOSAL
                if watching:
                    for _ in range(32):
                        message = state.native.hotkey_message()
                        if message is None:
                            if state.active_since is not None and state.native.ticks() != state.active_since:
                                state.active_since = None
                            break
                        state.press(message, time.perf_counter())
                    state.poll_release(time.perf_counter())
                self._wake.wait(_POLL_SECONDS if watching else None)
        except (OSError, AttributeError):
            logging.getLogger(__name__).error('Windows shortcut registration is unavailable')
        finally:
            try:
                if state is not None:
                    self._cleanup_ok = False
                    self._cleanup_ok = state.close()
            finally:
                with self._lock:
                    self._stopping = True
                    if self._pending is not None:
                        self._pending.finished.set()
                        self._pending = None
