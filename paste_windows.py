"""Conservative Windows clipboard transaction; never changes foreground focus."""
import ctypes
from ctypes import wintypes
import threading
import time

from text_input_windows import SHELL_SURFACE_CLASSES

_LOCK = threading.Lock()
_SAFE = {1, 7, 8, 13, 15, 16, 17}
WM_RENDERFORMAT = 0x0305
WM_RENDERALLFORMATS = 0x0306
WM_DESTROYCLIPBOARD = 0x0307


class PasteNotSent(OSError):
    """Native preflight/SendInput proves no input was injected."""


class WindowsNative:
    def __init__(self):
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'GetForegroundWindow': ([], wintypes.HWND),
            'IsWindow': ([wintypes.HWND], wintypes.BOOL),
            'OpenClipboard': ([wintypes.HWND], wintypes.BOOL),
            'CloseClipboard': ([], wintypes.BOOL),
            'EmptyClipboard': ([], wintypes.BOOL),
            'EnumClipboardFormats': ([wintypes.UINT], wintypes.UINT),
            'GetClipboardData': ([wintypes.UINT], wintypes.HANDLE),
            'SetClipboardData': ([wintypes.UINT, wintypes.HANDLE], wintypes.HANDLE),
            'GetClipboardSequenceNumber': ([], wintypes.DWORD),
            'GetClipboardOwner': ([], wintypes.HWND),
            'RegisterClipboardFormatW': ([wintypes.LPCWSTR], wintypes.UINT),
            'GetClipboardFormatNameW': ([wintypes.UINT, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'CreateWindowExW': ([wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
                wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
            'DestroyWindow': ([wintypes.HWND], wintypes.BOOL),
            'GetAsyncKeyState': ([ctypes.c_int], ctypes.c_short),
            'GetClassNameW': ([wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            'keybd_event': ([wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t], None),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result
        for name, args, result in [
            ('GlobalAlloc', [wintypes.UINT, ctypes.c_size_t], wintypes.HANDLE),
            ('GlobalLock', [wintypes.HANDLE], ctypes.c_void_p),
            ('GlobalUnlock', [wintypes.HANDLE], wintypes.BOOL),
            ('GlobalSize', [wintypes.HANDLE], ctypes.c_size_t),
            ('GlobalFree', [wintypes.HANDLE], wintypes.HANDLE),
        ]:
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        self.owner = None
        self._owner_ready = threading.Event()
        self._owner_thread = None
        self._delivery = None

    def foreground(self):
        return self.user.GetForegroundWindow()

    def shell_surface(self, hwnd):
        buffer = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(hwnd, buffer, 256)
        return buffer.value in SHELL_SURFACE_CLASSES

    def valid(self, hwnd):
        # The taskbar, tray flyout and desktop are never a paste destination (see text_input_windows).
        return bool(hwnd and self.user.IsWindow(hwnd) and not self.shell_surface(hwnd))

    def _ensure_owner(self):
        if self.owner:
            return
        if self._owner_thread is None:
            self._owner_thread = threading.Thread(target=self._owner_loop, daemon=True,
                                                  name='Whisper clipboard owner')
            self._owner_thread.start()
        if not self._owner_ready.wait(2) or not self.owner:
            raise OSError('Clipboard owner unavailable')

    def _owner_loop(self):
        # This thread owns the hidden window and pumps delayed-render requests.
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                          wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
        class WindowClass(ctypes.Structure):
            _fields_ = [('style', wintypes.UINT), ('proc', callback_type),
                        ('cls_extra', ctypes.c_int), ('wnd_extra', ctypes.c_int),
                        ('instance', wintypes.HINSTANCE), ('icon', wintypes.HANDLE),
                        ('cursor', wintypes.HANDLE), ('background', wintypes.HANDLE),
                        ('menu', wintypes.LPCWSTR), ('name', wintypes.LPCWSTR)]
        self.user.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                             wintypes.WPARAM, wintypes.LPARAM]
        self.user.DefWindowProcW.restype = ctypes.c_ssize_t
        self.user.GetOpenClipboardWindow.restype = wintypes.HWND
        self.user.GetClipboardOwner.restype = wintypes.HWND
        self.user.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                          wintypes.UINT, wintypes.UINT]
        self.user.GetMessageW.restype = wintypes.BOOL
        self.user.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        self.user.DispatchMessageW.restype = ctypes.c_ssize_t
        self.user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM]
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                       ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        def window_proc(hwnd, message, wparam, lparam):
            try:
                if message == WM_RENDERFORMAT:  # Requester holds clipboard.
                    delivery = self._delivery
                    if delivery and wparam in delivery['formats']:
                        reader = self.user.GetOpenClipboardWindow()
                        pid = wintypes.DWORD()
                        if reader:
                            self.user.GetWindowThreadProcessId(reader, ctypes.byref(pid))
                        prepared = self.prepare([(wparam, delivery['formats'][wparam])])
                        try:
                            item = prepared[0]
                            if not self.user.SetClipboardData(item[0], item[1]):
                                raise OSError('Cannot render clipboard')
                            item[1] = None
                            delivery['sequence'] = self.sequence()
                            self._record_reader(delivery, pid.value)
                        finally:
                            self.release(prepared)
                    return 0
                if message == WM_RENDERALLFORMATS:  # Before owner destruction.
                    delivery = self._delivery
                    if delivery and self.user.OpenClipboard(hwnd):
                        try:
                            if self.user.GetClipboardOwner() == hwnd:
                                for fmt, data in delivery['formats'].items():
                                    prepared = self.prepare([(fmt, data)])
                                    try:
                                        item = prepared[0]
                                        if self.user.SetClipboardData(fmt, item[1]):
                                            item[1] = None
                                    finally:
                                        self.release(prepared)
                        finally:
                            self.user.CloseClipboard()
                    return 0
                if message == 2:  # WM_DESTROY
                    self.user.PostQuitMessage(0)
                    return 0
            except Exception:
                if self._delivery:
                    self._delivery['error'] = True
                return 0
            return self.user.DefWindowProcW(hwnd, message, wparam, lparam)
        self._window_proc = callback_type(window_proc)
        name = 'WhisperClipboardOwner_' + str(id(self))
        wc = WindowClass()
        wc.proc, wc.name = self._window_proc, name
        self.user.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
        self.user.RegisterClassW.restype = wintypes.ATOM
        if self.user.RegisterClassW(ctypes.byref(wc)):
            self.owner = self.user.CreateWindowExW(0, name, '', 0, 0, 0, 0, 0,
                                                   wintypes.HWND(-3), None, None, None)
        self._owner_ready.set()
        if self.owner:
            msg = wintypes.MSG()
            while self.user.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                self.user.TranslateMessage(ctypes.byref(msg))
                self.user.DispatchMessageW(ctypes.byref(msg))
            self.owner = None

    @staticmethod
    def _record_reader(delivery, pid):
        if not delivery['first_reader']:
            delivery['first_reader'] = pid or -1
        if pid and pid == delivery['target_pid'] and delivery['first_reader'] == pid:
            delivery['read'] = True
            delivery['read_at'] = time.monotonic()

    def open(self):
        self._ensure_owner()
        return bool(self.user.OpenClipboard(self.owner))

    def close(self):
        self.user.CloseClipboard()

    def publish_delivery(self, entries, target):
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(target, ctypes.byref(pid))
        formats = dict(entries)
        self._delivery = {'formats': {fmt: data for fmt, data in entries if fmt in (1, 7, 13)},
                          'target_pid': pid.value, 'first_reader': 0, 'read': False,
                          'error': False, 'sequence': None, 'started_at': time.monotonic()}
        self.last_send_count = None
        metadata = self.prepare([(fmt, data) for fmt, data in entries if fmt not in (1, 7, 13)])
        try:
            self.replace(metadata)
            for fmt in self._delivery['formats']:
                ctypes.set_last_error(0)
                self.user.SetClipboardData(fmt, None)
                if ctypes.get_last_error():
                    raise OSError('Cannot publish delayed clipboard')
            self._delivery['sequence'] = self.sequence()
        finally:
            self.release(metadata)

    def delivery_consumed(self, target, timeout=1.0):
        deadline = time.monotonic() + timeout
        while True:
            delivery = self._delivery
            if not delivery or delivery['error']:
                return False
            if delivery['read'] and not self.user.GetOpenClipboardWindow():
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(.01)

    def delivery_sequence(self):
        return self._delivery['sequence'] if self._delivery else None

    def delivery_metadata(self):
        delivery = self._delivery or {}
        read_at = delivery.get('read_at')
        return {'target_pid': delivery.get('target_pid'),
                'first_reader_pid': delivery.get('first_reader'),
                'target_read': delivery.get('read', False),
                'sent_count': getattr(self, 'last_send_count', None),
                'receipt_latency_ms': round((read_at - delivery['started_at']) * 1000, 1)
                if read_at is not None and 'started_at' in delivery else None}

    def end_delivery(self):
        self._delivery = None

    def materialize_delivery(self):
        # Caller holds clipboard. Before dropping the callback's RAM text,
        # transfer every remaining delayed format to Windows without clearing.
        if self._delivery and self.user.GetClipboardOwner() == self.owner:
            prepared = self.prepare(list(self._delivery['formats'].items()))
            try:
                for item in prepared:
                    if not self.user.SetClipboardData(item[0], item[1]):
                        raise OSError('Cannot preserve delayed clipboard')
                    item[1] = None
            finally:
                self.release(prepared)

    def shutdown(self):
        if self._delivery:
            return False
        if self.owner:
            self.user.PostMessageW(self.owner, 0x10, 0, 0)  # WM_CLOSE
            self._owner_thread.join(2)
            return not self._owner_thread.is_alive()
        return True

    def sequence(self):
        return self.user.GetClipboardSequenceNumber()

    def owns_clipboard(self):
        return bool(self.owner and self.user.GetClipboardOwner() == self.owner)

    def snapshot(self):
        result = []
        formats = []
        fmt = 0
        while True:
            ctypes.set_last_error(0)
            fmt = self.user.EnumClipboardFormats(fmt)
            if not fmt:
                if ctypes.get_last_error():
                    raise OSError('Cannot enumerate clipboard')
                break
            formats.append(fmt)
        for fmt in formats:
            # Windows synthesizes CF_BITMAP from DIB; bitmap handles are not HGLOBAL.
            if fmt == 2 and (8 in formats or 17 in formats):
                continue
            if fmt not in _SAFE:
                name = ctypes.create_unicode_buffer(256)
                if fmt < 0xC000 or not self.user.GetClipboardFormatNameW(fmt, name, 256):
                    raise OSError('Unsupported clipboard format')
            handle = self.user.GetClipboardData(fmt)
            size = self.kernel.GlobalSize(handle) if handle else 0
            if not size or size > 128 * 1024 * 1024:
                raise OSError('Clipboard data unavailable')
            pointer = self.kernel.GlobalLock(handle)
            if not pointer:
                raise OSError('Cannot read clipboard')
            try:
                result.append((fmt, ctypes.string_at(pointer, size)))
                if sum(len(data) for _, data in result) > 128 * 1024 * 1024:
                    raise OSError('Clipboard snapshot exceeds memory limit')
            finally:
                self.kernel.GlobalUnlock(handle)
        return result

    def prepare(self, entries):
        allocated = []
        try:
            for fmt, data in entries:
                handle = self.kernel.GlobalAlloc(0x0002, len(data))
                if not handle:
                    raise OSError('Cannot allocate clipboard')
                allocated.append([fmt, handle])
                pointer = self.kernel.GlobalLock(handle)
                if not pointer:
                    raise OSError('Cannot lock clipboard')
                try:
                    ctypes.memmove(pointer, data, len(data))
                finally:
                    self.kernel.GlobalUnlock(handle)
            return allocated
        except Exception:
            self.release(allocated)
            raise

    def release(self, prepared):
        for _, handle in prepared:
            if handle:
                self.kernel.GlobalFree(handle)

    def replace(self, prepared):
        if not self.user.EmptyClipboard():
            raise OSError('Cannot clear clipboard')
        try:
            for item in prepared:
                if not self.user.SetClipboardData(item[0], item[1]):
                    raise OSError('Cannot set clipboard format %s (Windows error %s)' %
                                  (item[0], ctypes.get_last_error()))
                item[1] = None  # Ownership transferred to Windows.
        finally:
            if self._delivery:
                self._delivery['sequence'] = self.sequence()

    def payload(self, text):
        # CloseClipboard otherwise adds ANSI/OEM/locale formats and advances the
        # sequence after we capture it. Publish all text forms explicitly.
        self.kernel.GetThreadLocale.argtypes = []
        self.kernel.GetThreadLocale.restype = wintypes.DWORD
        entries = [(13, (text + '\0').encode('utf-16-le')),
                   (1, (text + '\0').encode('mbcs', errors='replace')),
                   (7, (text + '\0').encode('oem', errors='replace')),
                   (16, int(self.kernel.GetThreadLocale()).to_bytes(4, 'little'))]
        for name in ('CanIncludeInClipboardHistory', 'CanUploadToCloudClipboard',
                     'ExcludeClipboardContentFromMonitorProcessing'):
            fmt = self.user.RegisterClipboardFormatW(name)
            if not fmt:
                raise OSError('Cannot register privacy format')
            entries.append((fmt, b'\0\0\0\0'))
        return entries

    def send_paste(self, ready=None):
        # Do not release or combine with keys held by the user.
        if any(self.user.GetAsyncKeyState(key) & 0x8000
               for key in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0x56)):
            raise PasteNotSent('Modifier or paste key is held')
        class Keyboard(ctypes.Structure):
            _fields_ = [('vk', wintypes.WORD), ('scan', wintypes.WORD),
                        ('flags', wintypes.DWORD), ('time', wintypes.DWORD),
                        ('extra', ctypes.c_size_t)]
        class Mouse(ctypes.Structure):
            _fields_ = [('x', wintypes.LONG), ('y', wintypes.LONG),
                        ('data', wintypes.DWORD), ('flags', wintypes.DWORD),
                        ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]
        class Hardware(ctypes.Structure):
            _fields_ = [('message', wintypes.DWORD), ('low', wintypes.WORD), ('high', wintypes.WORD)]
        class Data(ctypes.Union):
            _fields_ = [('keyboard', Keyboard), ('mouse', Mouse), ('hardware', Hardware)]
        class Input(ctypes.Structure):
            _fields_ = [('type', wintypes.DWORD), ('data', Data)]
        self.user.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(Input), ctypes.c_int]
        self.user.SendInput.restype = wintypes.UINT
        events = (Input * 4)()
        for event, (vk, flags) in zip(events, ((0x11, 0), (0x56, 0), (0x56, 2), (0x11, 2))):
            event.type = 1
            event.data.keyboard = Keyboard(vk, 0, flags, 0, 0)
        if ready is not None and not ready():
            raise PasteNotSent('Original field is no longer ready')
        count = self.user.SendInput(4, events, ctypes.sizeof(Input))
        self.last_send_count = count
        if count == 0:
            raise PasteNotSent('Windows refused paste input')
        if count != 4:
            # Release only keys potentially injected down; never replay paste.
            if count == 2:
                self.user.keybd_event(0x56, 0, 2, 0)
            self.user.keybd_event(0x11, 0, 2, 0)
            raise OSError('Windows accepted only part of paste input')
        return count

    def wait_keys_released(self, target):
        deadline = time.monotonic() + .15
        while True:
            if self.foreground() != target or not self.valid(target):
                raise PasteNotSent('Original target is no longer focused')
            if not any(self.user.GetAsyncKeyState(key) & 0x8000
                       for key in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0x56)):
                return
            if time.monotonic() >= deadline:
                raise PasteNotSent('Modifier or paste key is held')
            time.sleep(.01)


class PasteBackend:
    def __init__(self, native=None, send_paste=None, sleep=time.sleep):
        self.native = native
        self.send_paste = send_paste
        self.sleep = sleep
        self.pending = None
        self.last_delivery_metadata = {}

    def _api(self):
        if self.native is None:
            self.native = WindowsNative()
        return self.native

    def capture_target(self):
        try:
            api = self._api()
            hwnd = api.foreground()
            return hwnd if api.valid(hwnd) else None
        except Exception:
            return None

    def _open(self, api):
        for _ in range(5):
            if api.open():
                return True
            self.sleep(0.02)
        return False

    def pending_status(self):
        if self.pending:
            self.resolve_pending()
        return {'pending': self.pending is not None,
                'message': 'Previous clipboard is held safely in memory.' if self.pending else ''}

    @staticmethod
    def _restore(api, snapshot):
        prepared = api.prepare(snapshot)
        try:
            api.replace(prepared)
            actual = dict(api.snapshot())
            if actual != dict(snapshot):
                api.last_restore_shape = [(fmt, len(data), len(actual.get(fmt, b'')),
                                           actual.get(fmt, b'').startswith(data),
                                           not any(actual.get(fmt, b'')[len(data):]))
                                          for fmt, data in snapshot]
                raise OSError('Clipboard restoration verification failed')
        finally:
            api.release(prepared)

    def shutdown(self):
        # The caller must refuse application exit while recovery is pending;
        # process termination would otherwise lose the RAM-only original.
        with _LOCK:
            return False if self.pending else (self.native.shutdown() if self.native else True)

    def resolve_pending(self, confirm_no_pending_paste=False):
        # Explicit recovery is allowed only after the caller has confirmed no
        # queued paste remains (or the consumer supplied an attributable receipt).
        with _LOCK:
            if not self.pending:
                return {'status': 'retained', 'message': 'No clipboard recovery is pending.'}
            api = self._api()
            pending = self.pending
            consumed = api.delivery_consumed(pending['target'], timeout=0)
            if not self._open(api):
                return {'status': 'uncertain', 'message': 'Clipboard is busy; recovery remains available.'}
            try:
                current = api.sequence()
                owned = api.delivery_sequence()
                if current != owned:
                    # A newer copy must not be overwritten. Keep the original
                    # RAM snapshot for an explicit future recovery decision.
                    return {'status': 'uncertain', 'message': 'A newer clipboard copy is preserved; previous clipboard remains in memory.'}
                if not confirm_no_pending_paste and not consumed:
                    return {'status': 'uncertain', 'message': 'Paste has not been confirmed; clipboard recovery remains pending.'}
                self._restore(api, pending['saved'])
                api.end_delivery()
                self.pending = None
                return {'status': 'restored', 'message': 'Previous clipboard restored.'}
            except Exception:
                return {'status': 'uncertain', 'message': 'Clipboard recovery failed; previous clipboard remains in memory.'}
            finally:
                api.close()

    def abandon_pending(self, confirm_no_pending_paste=False, confirm_discard_original=False):
        with _LOCK:
            if not self.pending:
                return {'status': 'retained', 'message': 'No clipboard recovery is pending.'}
            if not confirm_no_pending_paste or not confirm_discard_original:
                return {'status': 'uncertain', 'message': 'Finish or cancel the paste and explicitly confirm discarding the saved original clipboard first.'}
            api = self._api()
            if not self._open(api):
                return {'status': 'uncertain', 'message': 'Clipboard is busy; recovery remains available.'}
            try:
                api.materialize_delivery()
                api.end_delivery()
                self.pending = None
                return {'status': 'resolved', 'message': 'Current clipboard preserved; saved original clipboard discarded.'}
            except Exception:
                return {'status': 'uncertain', 'message': 'Clipboard preservation failed; saved original remains in memory.'}
            finally:
                api.close()

    def paste_text(self, text, target=None):
        if not isinstance(text, str) or not text or '\0' in text:
            return {'status': 'retained', 'message': 'Text was retained; paste input was invalid.'}
        with _LOCK:
            if self.pending:
                return {'status': 'retained', 'message': 'Earlier paste is unconfirmed. New text is retained in the app; resolve clipboard recovery first.'}
            self.last_delivery_metadata = {'status': 'retained', 'reason': 'invalid_input',
                                           'sent_count': 0}
            saved = temporary = None
            opened = changed = sent = False
            sequence = None
            result = {'status': 'retained', 'message': 'Text is retained in the app. Paste it manually.'}
            try:
                api = self._api()
                if not target or not api.valid(target) or api.foreground() != target:
                    return result
                if not self._open(api):
                    return result
                opened = True
                # Keep immutable bytes, not transferred HGLOBAL handles: a
                # partially failed restore must remain retryable without loss.
                saved = api.snapshot()
                entries = api.payload(text)
                if not api.valid(target) or api.foreground() != target:
                    return result
                changed = True
                api.publish_delivery(entries, target)
                sequence = api.delivery_sequence()
                api.close()
                opened = False
                if api.valid(target) and api.foreground() == target:
                    # Injection exceptions can happen after key-down. Treat them
                    # as potentially queued, never replay and never expose old data.
                    if hasattr(api, 'wait_keys_released'):
                        api.wait_keys_released(target)
                    sent = True
                    (self.send_paste or api.send_paste)()
                    if api.delivery_consumed(target):
                        result = {'status': 'pasted', 'message': 'The original application read the paste; insertion is not independently verified.'}
                    else:
                        result = {'status': 'uncertain', 'message': 'Paste was sent but not confirmed. Text is retained; previous clipboard is held for recovery.'}
            except PasteNotSent:
                sent = False
                result = {'status': 'retained', 'message': 'Paste was not sent because the target or keyboard was unavailable. Text is retained in the app.'}
            except Exception:
                result = {'status': 'uncertain' if sent else 'failed',
                          'message': 'Automatic paste was not confirmed. Your text is retained in the app.'}
            finally:
                metadata = api.delivery_metadata() if changed and hasattr(api, 'delivery_metadata') else {}
                if changed:
                    restore = not sent or result['status'] == 'pasted'
                    if restore:
                        try:
                            if not opened:
                                opened = self._open(api)
                            if not opened:
                                raise OSError('Clipboard busy during restore')
                            expected = api.delivery_sequence() or sequence
                            if api.sequence() == expected:
                                self._restore(api, saved)
                                api.end_delivery()
                            else:
                                raise OSError('Newer clipboard must be preserved')
                        except Exception as exc:
                            self.last_restore_error = type(exc).__name__ + ': ' + str(exc)
                            restore = False
                            result = {'status': 'uncertain', 'message': 'Previous clipboard remains in memory for recovery; text is retained.'}
                    if not restore:
                        self.pending = {'saved': saved, 'target': target}
                        saved = None
                if opened:
                    api.close()
                self.last_delivery_metadata = dict(metadata, status=result['status'],
                                                   reason=result['message'])
            return result


_backend = PasteBackend()
_verified_backend = None
capture_target = _backend.capture_target
paste_text = _backend.paste_text


def configure_verified_backend(backend):
    global _verified_backend
    if _verified_backend is not None and _verified_backend is not backend and _verified_backend.pending_status()['pending']:
        raise RuntimeError('Formatted clipboard recovery still owns its transaction')
    _verified_backend = backend


def pending_status(include_manual=False):
    if _verified_backend is not None:
        status = _verified_backend.pending_status()
        if status['pending']:
            return status
    status = _backend.pending_status()
    if include_manual and not status['pending'] and _verified_backend is not None:
        return _verified_backend.pending_status(include_manual=True)
    return status


def resolve_pending(confirm_no_pending_paste=False):
    backend = _verified_backend if _verified_backend is not None and _verified_backend.pending_status()['pending'] else _backend
    return backend.resolve_pending(confirm_no_pending_paste)


def abandon_pending(confirm_no_pending_paste=False, confirm_discard_original=False):
    backend = _verified_backend if _verified_backend is not None and _verified_backend.pending_status()['pending'] else _backend
    return backend.abandon_pending(confirm_no_pending_paste, confirm_discard_original)


def shutdown():
    if _verified_backend is not None and not _verified_backend.shutdown():
        return False
    return _backend.shutdown()


def get_delivery_metadata():
    """Content-free diagnostics only; never returns clipboard or dictated text."""
    if _verified_backend is not None and _verified_backend.last_delivery_metadata:
        return dict(_verified_backend.last_delivery_metadata)
    return dict(_backend.last_delivery_metadata)
