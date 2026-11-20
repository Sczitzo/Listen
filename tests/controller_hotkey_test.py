"""Controller gestures and poller lifecycle with device-free input."""

import queue
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller_hotkey import ControllerHotkey, _Gesture, controller_binding, controller_label


A, B = 0x1000, 0x2000
gesture = _Gesture()


def frame(mask, capture=False, second=None):
    return gesture.step((mask, second, None, None), "controller_a", capture)


assert controller_binding("controller_a")
assert not controller_binding("Controller A") and not controller_binding([])
assert controller_label("controller_a") == "Controller A"
assert controller_label("ctrl_r") is None
assert frame(A) is None  # Already held on connect.
assert frame(0) is None
assert frame(A) == ("controller_a", "press")
assert frame(A) is None
assert frame(0) == ("controller_a", "release")
assert frame(A) == ("controller_a", "press")
assert frame(None) == ("controller_a", "cancel")
assert frame(A) is None  # Still down on reconnect.
assert frame(0) is None
assert frame(A) == ("controller_a", "press")
assert frame(A | B) == ("controller_a", "cancel")
assert frame(A) is None
assert frame(0) is None
assert frame(B) is None  # Choosing an unrelated button blocks admission until neutral.
assert frame(A) is None
assert frame(0) is None
assert frame(A) == ("controller_a", "press")
assert frame(A, second=0) == ("controller_a", "cancel")
assert frame(A) is None
assert frame(0) is None
assert frame(B, capture=True) is None
assert frame(0, capture=True) == ("controller_b", "picked")
assert frame(A, capture=True) is None
assert frame(None, capture=True) is None  # Unplug isn't a pick or a release.

events = queue.Queue()
reads = []
connections = []
states = [A, None, None, None]


@contextmanager
def fake_input():
    connections.append("open")

    def read(slot):
        reads.append(slot)
        return states[slot]

    try:
        yield read
    finally:
        connections.append("close")


def wait_for(condition):
    end = time.monotonic() + 2
    while not condition():
        assert time.monotonic() < end, "Timed out waiting for the fake controller"
        time.sleep(0.005)


def expect(binding, action, context):
    actual_binding, actual_action, timestamp, actual_context = events.get(timeout=2)
    assert actual_context is context
    assert (actual_binding, actual_action) == (binding, action)
    assert isinstance(timestamp, float)


picker_a, picker_b, configured_a, configured_b = (object() for _ in range(4))
poller = ControllerHotkey(lambda *event: events.put(event), reader_factory=fake_input)
try:
    assert not reads and not connections
    assert not poller.set_binding("unknown")
    assert poller.set_binding(None)
    assert not reads and not connections
    assert poller.begin_capture(picker_a)
    wait_for(lambda: reads.count(0) >= 2)
    assert events.empty()  # No picking a button that was held when the picker opened.
    states[0] = 0
    wait_for(lambda: poller._gesture.armed)
    states[0] = B
    wait_for(lambda: poller._gesture.active == "controller_b")
    states[0] = 0
    expect("controller_b", "picked", picker_a)
    wait_for(lambda: connections[-1] == "close")
    count = len(reads)
    assert poller.set_binding("controller_a", configured_a)
    assert len(reads) == count  # Do not poll while the picker waits for confirmation.
    poller.end_capture()
    wait_for(lambda: poller._gesture.armed)
    states[0] = A
    expect("controller_a", "press", configured_a)
    assert not poller.begin_capture(picker_b)
    assert not poller.set_binding("controller_b")
    states[0] = None
    expect("controller_a", "cancel", configured_a)
    assert events.empty()
    assert poller.set_binding("controller_a", configured_b)
    states[0] = 0
    wait_for(lambda: poller._gesture.armed)
    states[0] = A
    expect("controller_a", "press", configured_b)
    states[0] = 0
    expect("controller_a", "release", configured_b)
    assert poller.set_binding(None)
    wait_for(lambda: connections[-1] == "close")
finally:
    assert poller.close()
assert not poller.begin_capture(picker_b) and not poller.set_binding("controller_a")
states[0] = 0
paused = threading.Event()
resume = threading.Event()
poller = ControllerHotkey(lambda *event: events.put(event), reader_factory=fake_input)
emit = poller._emit


def pause_old_pick(event, at, context):
    if event == ("controller_a", "picked"):
        paused.set()
        assert resume.wait(2), "Old pick was not resumed"
    emit(event, at, context)


poller._emit = pause_old_pick
try:
    assert poller.begin_capture(picker_a)
    wait_for(lambda: poller._gesture.armed)
    states[0] = A
    wait_for(lambda: poller._gesture.active == "controller_a")
    states[0] = 0
    assert paused.wait(2), "The old pick never reached dispatch"
    poller.end_capture()
    assert poller.begin_capture(picker_b)
    resume.set()
    expect("controller_a", "picked", picker_a)
    wait_for(lambda: poller._gesture.armed)
    states[0] = B
    wait_for(lambda: poller._gesture.active == "controller_b")
    states[0] = 0
    expect("controller_b", "picked", picker_b)
finally:
    resume.set()
    assert poller.close()
print("Controller gesture, source generation and worker lifecycle checks passed.")
