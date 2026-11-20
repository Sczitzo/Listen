"""Read-only controller hotkeys for Xbox-compatible digital buttons."""

import ctypes
import logging
import sys
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes


_BUTTONS = {
    "controller_dpad_up": (0x0001, "D-pad Up"),
    "controller_dpad_down": (0x0002, "D-pad Down"),
    "controller_dpad_left": (0x0004, "D-pad Left"),
    "controller_dpad_right": (0x0008, "D-pad Right"),
    "controller_start": (0x0010, "Menu"),
    "controller_back": (0x0020, "View"),
    "controller_left_stick": (0x0040, "Left Stick Press"),
    "controller_right_stick": (0x0080, "Right Stick Press"),
    "controller_left_bumper": (0x0100, "Left Bumper"),
    "controller_right_bumper": (0x0200, "Right Bumper"),
    "controller_a": (0x1000, "A"),
    "controller_b": (0x2000, "B"),
    "controller_x": (0x4000, "X"),
    "controller_y": (0x8000, "Y"),
}
_BINDINGS_BY_BIT = {bit: binding for binding, (bit, _) in _BUTTONS.items()}
_BUTTON_MASK = sum(_BINDINGS_BY_BIT)
_SLOTS = range(4)


def controller_binding(value):
    return isinstance(value, str) and value in _BUTTONS


def controller_label(value):
    return "Controller " + _BUTTONS[value][1] if controller_binding(value) else None


class _Gamepad(ctypes.Structure):
    _fields_ = [
        ("buttons", wintypes.WORD),
        ("left_trigger", wintypes.BYTE),
        ("right_trigger", wintypes.BYTE),
        ("left_x", wintypes.SHORT),
        ("left_y", wintypes.SHORT),
        ("right_x", wintypes.SHORT),
        ("right_y", wintypes.SHORT),
    ]


class _State(ctypes.Structure):
    _fields_ = [("packet", wintypes.DWORD), ("gamepad", _Gamepad)]


@contextmanager
def _xinput_reader():
    if sys.platform != "win32":
        raise OSError("Controller shortcuts require Windows.")

    ole = ctypes.WinDLL("ole32.dll", winmode=0x800)
    ole.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
    ole.CoInitializeEx.restype = ctypes.c_long
    ole.CoUninitialize.argtypes = ()
    ole.CoUninitialize.restype = None
    result = ole.CoInitializeEx(None, 0)
    if result < 0:
        raise OSError("Windows could not initialize the controller listener.")

    try:
        get_state = ctypes.WinDLL("xinput1_4.dll", winmode=0x800).XInputGetState
        get_state.argtypes = (wintypes.DWORD, ctypes.POINTER(_State))
        get_state.restype = wintypes.DWORD
        state = _State()

        def read(slot):
            result = get_state(slot, ctypes.byref(state))
            if result == 1167:  # ERROR_DEVICE_NOT_CONNECTED
                return None
            if result:
                raise OSError("Windows could not read controller state.")
            return state.gamepad.buttons & _BUTTON_MASK

        yield read
    finally:
        ole.CoUninitialize()


class _Gesture:
    def __init__(self):
        self.slot = None
        self.armed = False
        self.active = None

    def reset(self, capture):
        cancel = (self.active, "cancel") if self.active and not capture else None
        self.slot = self.active = None
        self.armed = False
        return cancel

    def step(self, states, binding, capture):
        connected = [(slot, mask) for slot, mask in enumerate(states) if mask is not None]
        if len(connected) != 1:
            return self.reset(capture)
        slot, mask = connected[0]
        if slot != self.slot:
            cancel = self.reset(capture)
            self.slot, self.armed = slot, mask == 0
            return cancel

        if mask == 0:
            active, self.active = self.active, None
            self.armed = True
            return (active, "picked" if capture else "release") if active else None
        button = _BINDINGS_BY_BIT.get(mask)
        if self.active:
            if self.active == button:
                return None
            active, self.active = self.active, None
            return (active, "cancel") if not capture else None
        if not self.armed:
            return None
        self.armed = False
        if button and (capture or binding == button):
            self.active = button
            if not capture:
                return button, "press"
        return None


class ControllerHotkey:
    """Own a single polling thread and report edges only while enabled."""

    def __init__(self, on_edge, *, reader_factory=_xinput_reader):
        self._on_edge = on_edge
        self._reader_factory = reader_factory
        self._condition = threading.Condition()
        self._gesture = _Gesture()
        self._thread = None
        self._binding = None
        self._binding_context = self._capture_context = None
        self._capture = self._picked = self._closed = False
        self._generation = 0

    def _enabled(self):
        return not self._closed and (not self._picked if self._capture else self._binding is not None)

    def _changed(self):
        self._generation += 1
        self._gesture = _Gesture()
        if self._enabled() and (self._thread is None or not self._thread.is_alive()):
            self._thread = threading.Thread(target=self._run, name="controller-hotkey", daemon=True)
            self._thread.start()
        self._condition.notify_all()

    def begin_capture(self, context):
        with self._condition:
            if self._closed or self._gesture.active or (self._capture and not self._picked):
                return False
            self._capture, self._picked = True, False
            self._capture_context = context
            self._changed()
            return True

    def end_capture(self):
        with self._condition:
            if self._capture:
                self._capture = self._picked = False
                self._capture_context = None
                self._changed()

    def set_binding(self, binding_or_none, context=None):
        if binding_or_none is not None and not controller_binding(binding_or_none):
            return False
        with self._condition:
            if self._closed:
                return False
            if self._binding == binding_or_none and self._binding_context is context:
                return True
            if self._gesture.active:
                return False
            self._binding = binding_or_none
            self._binding_context = context
            self._changed()
            return True

    def close(self):
        with self._condition:
            self._closed = True
            self._changed()
            thread = self._thread
        if thread is threading.current_thread():
            return False
        if thread is not None:
            thread.join(2)
        return thread is None or not thread.is_alive()

    def _emit(self, event, at, context):
        if event is None:
            return
        try:
            self._on_edge(*event, at, context)
        except Exception:
            logging.getLogger(__name__).error("Controller shortcut event handler failed")

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or self._enabled())
                if self._closed:
                    return
                generation = self._generation
                context = self._capture_context if self._capture else self._binding_context

            try:
                with self._reader_factory() as read:
                    self._poll(read, generation, context)
            except OSError:
                with self._condition:
                    current = generation == self._generation
                    cancel = self._gesture.reset(self._capture) if current else None
                self._emit(cancel, time.perf_counter(), context)
                with self._condition:
                    self._condition.wait_for(lambda: generation != self._generation, timeout=1.0)

    def _poll(self, read, generation, context):
        states = [None] * len(_SLOTS)
        next_check = [0.0] * len(_SLOTS)
        while True:
            with self._condition:
                if generation != self._generation or not self._enabled():
                    return
            at = time.perf_counter()
            for slot in _SLOTS:
                if at >= next_check[slot]:
                    states[slot] = read(slot)
                    next_check[slot] = at + 1.0 if states[slot] is None else at
            with self._condition:
                if generation != self._generation or not self._enabled():
                    return
                event = self._gesture.step(states, self._binding, self._capture)
                if event and event[1] == "picked":
                    self._picked = True
            # The app rejects this generation's context if its picker or binding has changed.
            self._emit(event, time.perf_counter(), context)
            with self._condition:
                # shortcut: a complete press between samples can be missed, upgrade to event delivery if needed.
                self._condition.wait_for(
                    lambda: generation != self._generation or not self._enabled(), timeout=0.02
                )
