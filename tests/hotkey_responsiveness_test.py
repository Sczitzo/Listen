"""Exercise actual listener callbacks without installing hooks or devices."""
import os
import ctypes
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import dictate as d


def listener():
    # The factory must be the same one used by production startup.
    factory = getattr(d, '_create_keyboard_listener', None)
    return factory() if factory else d.pk.Listener(
        on_press=d.on_press, on_release=d.on_release,
        win32_event_filter=d._event_filter)


def press(hook, down):
    # pynput's real callback dispatch, with no installed native hook.
    data = hook._KBDLLHOOKSTRUCT()
    data.vkCode, data.flags = 0xa3, 0
    converted = hook._convert(0, 0x100 if down else 0x101, ctypes.addressof(data))
    if converted is not None:
        hook._process(*converted)


def wait_for(predicate, message):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(.005)
    assert predicate(), message


d._state = 'idle'
d._quitting = False
d._recording.clear()
d._hotkey_down = None
d._skip_release = d._locked = False
def check_hotkey_picker():
    from types import SimpleNamespace

    api = d.Api()
    writes = []
    snapshot = set()
    current_registration = None
    proposed_registration = None

    class Registration:
        def __init__(self, callback, cancel_when=None, on_cancel=None):
            self.callback = callback
            self.cancel_when, self.on_cancel = cancel_when, on_cancel

        def prepare(self, proposed):
            nonlocal proposed_registration
            proposed_registration = proposed
            return True

        def finish(self, commit):
            nonlocal current_registration
            if commit:
                current_registration = proposed_registration
            return True
    def edge(vk, pressed):
        if pressed:
            snapshot.add(vk)
        else:
            snapshot.discard(vk)
        return d._queued_event_filter(0x100 if pressed else 0x101, SimpleNamespace(vkCode=vk))

    with patch.dict(d.SETTINGS, {'hotkey': 'ctrl_r'}), \
         patch.object(d, '_state', 'idle'), \
         patch.object(d, '_physical_keyboard_snapshot', side_effect=lambda: set(snapshot)), \
         patch.object(d, '_check_registered_hotkey', return_value='free'), \
         patch.object(d, '_registered_hotkey', None), \
         patch.object(d, 'RegisteredHotkey', Registration), \
         patch.object(d, 'atomic_write_json', side_effect=lambda _, values: writes.append(values['hotkey'])), \
         patch.object(d, 'capture_target', return_value=None):
        epoch = d._hotkey_epoch
        first = api.begin_hotkey_capture()
        assert first['ok'] and d._hotkey_epoch == epoch + 1
        token = first['token']
        assert not api.begin_hotkey_capture()['ok']
        d._recording.set()
        assert not api.begin_hotkey_capture()['ok']
        d._recording.clear()
        assert edge(0xa3, True) is False and d._hotkey_events.empty()
        assert not api.finish_hotkey_capture('wrong-token', 'shift_r')['ok']
        assert api.finish_hotkey_capture(token)['ok']
        edge(0xa3, True)
        edge(0xa3, False)
        assert d._hotkey_events.empty() and not writes and d.SETTINGS['hotkey'] == 'ctrl_r'
        edge(0xa3, True)
        edge(0xa3, False)
        assert [d._hotkey_events.get_nowait()[1] for _ in range(2)] == ['press', 'release']
        d._hotkey_pending -= 2  # This fixture consumed the queue without the worker.

        second = api.begin_hotkey_capture()['token']
        edge(0xa1, True)
        edge(0xa1, False)
        assert api.check_hotkey_capture(second, 'alt+f4')['level'] == 'blocked'
        assert not api.finish_hotkey_capture(second, 'alt+f4', True)['ok']
        assert api.check_hotkey_capture(second, 'ctrl+c')['level'] == 'warning'
        assert not api.finish_hotkey_capture(second, 'ctrl+c')['ok']
        assert not writes
        with patch.object(d, '_check_registered_hotkey', return_value='claimed'):
            assert not api.finish_hotkey_capture(second, 'ctrl+c', True)['ok']
        assert api.finish_hotkey_capture(second, 'ctrl+c', True) == {'ok': True, 'hotkey': 'ctrl+c'}
        assert writes == ['ctrl+c'] and d.SETTINGS['hotkey'] == 'ctrl+c'
        assert current_registration == (2, 0x43)
        assert d._hotkey_events.empty()
        reg = d._registered_hotkey
        reg.callback((4, 0x43), True, 5)
        assert d._hotkey_events.empty()
        reg.callback(current_registration, True, 5)
        reg.callback(current_registration, True, 5.01)
        reg.callback(current_registration, False, 5.1)
        assert [d._hotkey_events.get_nowait()[1] for _ in range(2)] == ['press', 'release']
        assert d._hotkey_events.empty()
        d._hotkey_pending -= 2
        assert not reg.cancel_when()
        reg.on_cancel()
        assert d._hotkey_events.empty()
        d._recording.set()
        try:
            assert reg.cancel_when()
            reg.on_cancel()
            assert d._hotkey_events.get_nowait()[1] == 'cancel'
            d._hotkey_pending -= 1
        finally:
            d._recording.clear()

        third = api.begin_hotkey_capture()['token']
        with patch.object(d, '_hotkey_capture_until', 0):
            assert not api.finish_hotkey_capture(third, 'ctrl_r')['ok']
        assert not api.set_setting('hotkey', 'ctrl+shift+f8')
        with patch.object(d, 'atomic_write_json', side_effect=OSError):
            assert not api.finish_hotkey_capture(third, 'ctrl_r')['ok']
        assert api.finish_hotkey_capture(third)['ok']
        assert d.SETTINGS['hotkey'] == 'ctrl+c'
        assert current_registration == (2, 0x43)
        assert not d._hotkey_capture_token

        fourth = api.begin_hotkey_capture()['token']
        def save_and_check_capture_ownership(*_args):
            assert not api.finish_hotkey_capture(fourth)['ok']
            assert d._hotkey_capture_token == fourth
            assert not api.begin_hotkey_capture()['ok']
        with patch.object(d, 'atomic_write_json', side_effect=save_and_check_capture_ownership):
            assert api.finish_hotkey_capture(fourth, 'ctrl_r')['ok']


check_hotkey_picker()
entered, release, returned = threading.Event(), threading.Event(), threading.Event()
failures = []
original_start = d._start
original_chime = d._chime


def delayed_start(*args, **kwargs):
    entered.set()
    assert release.wait(3)
    return original_start(*args, **kwargs)


try:
    with patch.dict(d.SETTINGS, {'hotkey': 'ctrl_r', 'cleanup': False, 'live_preview': False}), \
         patch.object(d, 'capture_target', return_value=None), \
         patch.object(d, '_text_native', return_value=None), \
         patch.object(d, '_open_mic', lambda *args: None), \
         patch.object(d, '_close_mic', lambda *args: None), \
         patch.object(d, '_chime', lambda *args: None), \
         patch.object(d, '_start', delayed_start):
        hook = listener()
        def send():
            try:
                press(hook, True)
            except BaseException as error:
                failures.append(error)
            finally:
                returned.set()
        ingress = threading.Thread(target=send)
        ingress.start()
        assert entered.wait(2)
        try:
            assert returned.wait(.15), 'Recording startup blocked the Windows keyboard listener'
            press(hook, False)  # short tap arrives while start is delayed
            time.sleep(d.LOCK_TAP_S + .05)
        finally:
            release.set()
            ingress.join(3)
        deadline = time.monotonic() + 2
        while not d._locked and time.monotonic() < deadline:
            time.sleep(.005)
        assert not failures and d._recording.is_set() and d._locked, 'Delayed tap lost hands-free timing'
        press(hook, True)
        press(hook, False)
        deadline = time.monotonic() + 2
        while d._recording.is_set() and time.monotonic() < deadline:
            time.sleep(.005)
        assert not d._recording.is_set(), 'Second tap did not finish capture'
        wait_for(lambda: d._hotkey_down is None, 'Stop release did not settle')

        # A thrown action must not stop pynput or the persistent dispatcher.
        failed = threading.Event()
        def broken_start(*args, **kwargs):
            d._recording.set()
            d._state = 'recording'
            failed.set()
            raise RuntimeError('synthetic microphone thread startup failure')
        with patch.object(d, '_start', broken_start):
            press(hook, True)
            assert failed.wait(2)
            wait_for(lambda: not d._recording.is_set(), 'Action failure left microphone active')
        press(hook, False)
        press(hook, True)
        press(hook, False)
        wait_for(lambda: d._recording.is_set() and d._locked, 'Input did not recover after an action exception')

        # GUI Finish invalidates old pending taps, without restarting capture.
        with d._mic_lock:
            press(hook, True)
            press(hook, False)
            d._stop()
        time.sleep(.05)
        assert not d._recording.is_set() and d._hotkey_down is None

        # A blocked start followed by Escape must retain physical event order.
        entered.clear()
        release.clear()
        press(hook, True)
        assert entered.wait(2)
        press(hook, False)
        class Suppressed(Exception):
            pass
        def suppress():
            raise Suppressed()
        with patch.object(d, '_listener', type('Listener', (), {'suppress_event': staticmethod(suppress)})()):
            for message in (0x100, 0x100, 0x101):
                data = type('Edge', (), {'vkCode': 0x1b, 'flags': 0})()
                try:
                    d._event_filter(message, data)
                except Suppressed:
                    pass
                else:
                    raise AssertionError('Escape pair leaked during delayed startup')
        cancelled = threading.Event()
        real_cancel = d._cancel
        def cancel():
            real_cancel()
            cancelled.set()
        with patch.object(d, '_cancel', cancel):
            release.set()
            assert cancelled.wait(2)
        assert not d._recording.is_set() and d._jobs.qsize() == 2, 'Escape delivered or restarted cancelled audio'

        # Fail real thread-creation boundaries, rather than only an action stub.
        real_thread_start = threading.Thread.start
        def reject_mic(thread):
            if thread._target is d._open_mic:
                raise RuntimeError('synthetic thread creation refusal')
            return real_thread_start(thread)
        with patch.object(threading.Thread, 'start', reject_mic):
            previous_epoch = d._hotkey_epoch
            press(hook, True)
            wait_for(lambda: d._hotkey_epoch > previous_epoch, 'Microphone thread failure escaped recovery')
            assert not d._recording.is_set()
        press(hook, False)

        # A real chime startup failure and close-thread startup failure cannot
        # strand the queued final job. Closing must allow audio callbacks to lock.
        press(hook, True)
        press(hook, False)
        wait_for(lambda: d._recording.is_set() and d._locked, 'Recovery capture did not lock')
        d._announced = True
        close_checked = threading.Event()
        close_errors = []
        def close_with_callback(*args):
            callback_done = threading.Event()
            def callback():
                with d._mic_lock:
                    callback_done.set()
            callback_thread = threading.Thread(target=callback)
            callback_thread.start()
            if not callback_done.wait(1):
                close_errors.append('microphone close held callback lock')
            callback_thread.join(2)
            close_checked.set()
        def reject_auxiliary(thread):
            if getattr(thread._target, '__name__', '') in ('play', 'finish'):
                raise RuntimeError('synthetic helper thread creation refusal')
            return real_thread_start(thread)
        with patch.object(d, '_close_mic', close_with_callback), \
             patch.object(d, '_chime', original_chime), \
             patch.object(threading.Thread, 'start', reject_auxiliary):
            press(hook, True)
            press(hook, False)
            assert close_checked.wait(2), 'Failed helper startup stranded microphone release'
            wait_for(lambda: d._pending_recordings == 0, 'Pending recording never settled')
        assert not close_errors and not d._recording.is_set()
        jobs = []
        while not d._jobs.empty():
            job = d._jobs.get_nowait()
            assert job[-1].is_set(), 'Queued final is still waiting for microphone release'
            d._jobs.task_done()
        assert not hook.is_alive(), 'No native keyboard hook may be installed by this test'
finally:
    release.set()
    stop = getattr(d, '_close_hotkey_dispatch', None)
    if stop:
        assert stop(), 'Task-owned hotkey worker did not exit'
print('hotkey responsiveness: delayed start, tap timing, Finish, worker exit passed')
