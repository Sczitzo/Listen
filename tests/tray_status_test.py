"""Pure tray refresh contracts; opt-in real owned Win32 popup verification."""
import argparse
import ast
import ctypes
import hashlib
from ctypes import wintypes
import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'audit/2026-10-28/latency/implementation/tray-repair'


def builder(backend, state, image=None):
    source = (ROOT / 'dictate.py').read_text(encoding='utf-8-sig')
    node = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == 'build_tray')
    scope = {'pystray': backend, 'ICONS': {'loading': image}, 'Api': lambda: SimpleNamespace(get_favorites=lambda: []),
             'ui_state': lambda: state['status'], 'show_window': lambda *args: state['opened'].append(args),
             'request_quit': lambda: state.update(quit_requested=True), 'bump_favorite': lambda *a: None,
             '_chime': lambda *a: None, 'pyperclip': SimpleNamespace(copy=lambda *a: (_ for _ in ()).throw(AssertionError('Clipboard forbidden')))}
    exec(compile(ast.Module(body=[node], type_ignores=[]), '<actual-tray-builder>', 'exec'), scope)
    return scope['build_tray']()


def pure():
    state = {'status': 'loading', 'opened': []}
    class Menu:
        def __init__(self, *items): self.items = items
    class Item:
        def __init__(self, text, action, **kwargs): self.text, self.action = text, action
    class Icon:
        def __init__(self, name, image, title, menu):
            self.menu, self.events, self.fail, self.nested = menu, [], False, False
            self.handler = self._on_notify  # Match Win32's bound callback setup.
        def run_detached(self): self.update_menu()
        def update_menu(self):
            self.events.append(('refresh', state['status']))
            if self.fail: raise RuntimeError('Synthetic menu construction failure')
            self.caption = self.menu.items[3].text(None)
        def _on_notify(self, wparam, lparam):
            self.events.append(('show' if lparam == 0x205 else 'other', self.caption))
            if self.nested:
                before = len(self.events)
                self.handler(0, 0x205); self.handler(0, 0x202)
                assert len(self.events) == before, 'Reentrant menu notification mutated active popup'
            return 17
    tray = builder(SimpleNamespace(Icon=Icon, Menu=Menu, MenuItem=Item), state)
    assert tray.caption == 'Status: loading'
    for status in ('idle', 'recording', 'transcribing', 'error', 'idle'):
        state['status'] = status
        before = len(tray.events)
        tray.nested = True
        assert tray.handler(0, 0x205) == 17
        assert tray.events[before:] == [('refresh', status), ('show', 'Status: ' + status)]
        assert tray._dictation_menu_active is False
    tray.nested = False
    before = len(tray.events)
    assert tray.handler(7, 0x202) == 17
    assert tray.events[before:][0][0] == 'other'
    tray.fail = True
    before = len(tray.events)
    assert tray.handler(0, 0x205) is None
    assert tray.events[before:] == [('refresh', 'idle')], 'Unsafe old menu was shown after refresh failure'
    assert tray._dictation_menu_active is False
    tray.fail = False
    assert tray.handler(0, 0x205) == 17
    try:
        builder(SimpleNamespace(Icon=type('Unsupported', (), {})), state)
    except RuntimeError as error:
        assert str(error) == 'Unsupported Windows tray backend'
    else:
        raise AssertionError('Unverified backend hook was accepted')
    print('Tray status: fresh popup, ordering, reentrancy, failure/retry and backend contracts passed')


def native(receipt):
    import pystray
    from PIL import Image
    state = {'status': 'loading', 'opened': []}
    class ProbeIcon(pystray.Icon):
        def __init__(self, name, image, title, menu):
            super().__init__('whisper-owned-tray-check-' + str(os.getpid()), image,
                             'Whisper tray check (closes automatically)', menu)
    backend = SimpleNamespace(Icon=ProbeIcon, Menu=pystray.Menu, MenuItem=pystray.MenuItem)
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user.GetMenuItemCount.argtypes = [wintypes.HMENU]
    user.GetMenuStringW.argtypes = [wintypes.HMENU, wintypes.UINT, wintypes.LPWSTR, ctypes.c_int, wintypes.UINT]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user.IsWindow.argtypes = [wintypes.HWND]
    enum_callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [enum_callback, wintypes.LPARAM]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    report = {'status': 'failed', 'global_input_calls': 0, 'models_used': False,
              'microphone_used': False, 'clipboard_used': False, 'checks': []}
    report['source_sha256'] = hashlib.sha256((ROOT / 'dictate.py').read_bytes()).hexdigest()
    report['test_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    icon = None
    def wait(check, text):
        end = time.monotonic() + 4
        while time.monotonic() < end:
            if check(): return
            time.sleep(.025)
        raise AssertionError(text)
    def owner(hwnd):
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        assert pid.value == os.getpid(), 'Native message target is not owned'
    def rows():
        menu = icon._menu_handle[0]
        result = []
        for i in range(user.GetMenuItemCount(menu)):
            text = ctypes.create_unicode_buffer(128)
            user.GetMenuStringW(menu, i, text, 128, 0x400)
            result.append(text.value)
        return result
    def popup_visible():
        found = []
        def inspect(hwnd, value):
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == os.getpid():
                text = ctypes.create_unicode_buffer(32)
                user.GetClassNameW(hwnd, text, 32)
                if text.value == '#32768': found.append(hwnd)
            return True
        user.EnumWindows(enum_callback(inspect), 0)
        return bool(found)
    def popup():
        owner(icon._hwnd)
        user.PostMessageW(icon._hwnd, 0x400 + 11, 0, 0x205)
        wait(lambda: getattr(icon, '_dictation_menu_active', False), 'Owned popup did not start')
        wait(popup_visible, 'Owned native menu was not displayed')
    def dismiss():
        owner(icon._menu_hwnd)
        user.PostMessageW(icon._menu_hwnd, 0x1F, 0, 0)
        wait(lambda: not getattr(icon, '_dictation_menu_active', False), 'Owned popup did not close')
        wait(lambda: not popup_visible(), 'Owned menu window remained visible')
    try:
        icon = builder(backend, state, Image.new('RGBA', (32, 32), '#31a775'))
        wait(lambda: icon.visible and icon._menu_handle, 'Owned test icon not ready')
        assert 'Status: loading' in rows()
        state['status'] = 'idle'; popup()
        assert 'Status: idle' in rows()
        handle = icon._menu_handle
        state['status'] = 'recording'
        owner(icon._hwnd)
        user.PostMessageW(icon._hwnd, 0x400 + 11, 0, 0x205)
        time.sleep(.15)
        assert icon._menu_handle is handle and 'Status: idle' in rows(), 'Active native handle was rebuilt'
        report['checks'].append('held popup stable across external state change and nested right click')
        dismiss(); popup()
        assert 'Status: recording' in rows() and 'Quit' in rows() and 'Open Listen' in rows()
        report['checks'].append('close/reopen displays current status and preserved actions')
        dismiss()
        user.PostMessageW(icon._hwnd, 0x400 + 11, 0, 0x202)
        wait(lambda: len(state['opened']) == 1, 'Default open callback did not run')
        report['checks'].append('default Open callback works')
        report['status'] = 'passed'
    except Exception as error:
        report['error'] = type(error).__name__ + ': ' + str(error)
    finally:
        if icon:
            if getattr(icon, '_dictation_menu_active', False):
                user.PostMessageW(icon._menu_hwnd, 0x1F, 0, 0)
            icon.stop()
            thread = getattr(icon, '_thread', None)
            if thread is not None and thread is not threading.current_thread():
                thread.join(4)
            end = time.monotonic() + 4
            while user.IsWindow(icon._hwnd) and time.monotonic() < end:
                time.sleep(.025)
            report['tray_loop_stopped'] = not icon._running
            report['tray_window_destroyed'] = not user.IsWindow(icon._hwnd)
            report['tray_thread_exited'] = thread is None or not thread.is_alive()
            if not report['tray_loop_stopped'] or not report['tray_window_destroyed'] or not report['tray_thread_exited']:
                report['status'] = 'failed'
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / receipt).write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real-native', action='store_true')
    parser.add_argument('--receipt', default='native-tray.json')
    args = parser.parse_args()
    if Path(args.receipt).name != args.receipt or not args.receipt.endswith('.json'):
        parser.error('Receipt must be a plain JSON filename')
    if args.real_native:
        if (OUT / args.receipt).exists(): parser.error('Preserve existing receipt')
        raise SystemExit(native(args.receipt))
    pure()
