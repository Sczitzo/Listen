"""Read the Windows icon of a captured dictation destination."""
import base64
import ctypes
from ctypes import wintypes
import io
import ntpath

from PIL import Image, ImageColor


def _api(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.argtypes, function.restype = arguments, result
    return function


def history_app(hwnd, pid):
    if not hwnd or not pid:
        return None
    kernel = ctypes.WinDLL('kernel32')
    user = ctypes.WinDLL('user32')
    owner = _api(user, 'GetWindowThreadProcessId', wintypes.DWORD, wintypes.HWND,
                 ctypes.POINTER(wintypes.DWORD))
    process = wintypes.DWORD()
    if not owner(hwnd, ctypes.byref(process)) or process.value != pid:
        return None

    open_process = _api(kernel, 'OpenProcess', wintypes.HANDLE,
                        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    close = _api(kernel, 'CloseHandle', wintypes.BOOL, wintypes.HANDLE)
    query = _api(kernel, 'QueryFullProcessImageNameW', wintypes.BOOL, wintypes.HANDLE,
                 wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
    handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        path = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(path))
        if not query(handle, 0, path, ctypes.byref(length)):
            return None
        name = ntpath.splitext(ntpath.basename(path.value))[0].strip()
        if not name or len(name) > 80 or not name.isprintable():
            return None
        labels = {'code': 'VS Code', 'notepad': 'Notepad', 'explorer': 'File Explorer',
                  'chrome': 'Google Chrome', 'msedge': 'Microsoft Edge', 'firefox': 'Firefox'}
        app = {'name': labels.get(name.casefold(), name)}
        try:
            icon = window_icon(hwnd, pid, 32, '#2d2925')
            if icon is not None:
                with icon, io.BytesIO() as output:
                    icon.save(output, format='PNG')
                    encoded = base64.b64encode(output.getvalue()).decode('ascii')
                if len(encoded) <= 4608:
                    app['icon'] = encoded
        except (OSError, ValueError):
            pass
        if owner(hwnd, ctypes.byref(process)) and process.value == pid:
            return app
        return None
    finally:
        close(handle)


def window_icon(hwnd, pid, size, background):
    if not hwnd or not pid:
        return None
    user = ctypes.WinDLL('user32')
    owner = _api(user, 'GetWindowThreadProcessId', wintypes.DWORD, wintypes.HWND,
                 ctypes.POINTER(wintypes.DWORD))
    process = wintypes.DWORD()
    if not owner(hwnd, ctypes.byref(process)) or process.value != pid:
        return None

    send = _api(user, 'SendMessageTimeoutW', ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT,
                ctypes.POINTER(ctypes.c_size_t))
    icon = ctypes.c_size_t()
    # WM_GETICON (small), abort on a hung or closing destination.
    found = send(hwnd, 0x7f, 2, 0, 0x22, 25, ctypes.byref(icon)) and icon.value
    if not found:
        class_icon = _api(user, 'GetClassLongPtrW', ctypes.c_size_t, wintypes.HWND, ctypes.c_int)
        found = class_icon(hwnd, -34) or class_icon(hwnd, -14)
    if not found or not owner(hwnd, ctypes.byref(process)) or process.value != pid:
        return None

    # Windows owns this icon handle. Draw it into a local top-down 32-bit bitmap.
    gdi = ctypes.WinDLL('gdi32')
    handle, pointer = wintypes.HANDLE, ctypes.c_void_p
    create_dc = _api(gdi, 'CreateCompatibleDC', handle, handle)
    delete_dc = _api(gdi, 'DeleteDC', wintypes.BOOL, handle)
    dib = _api(gdi, 'CreateDIBSection', handle, handle, pointer, wintypes.UINT,
               ctypes.POINTER(pointer), handle, wintypes.DWORD)
    select = _api(gdi, 'SelectObject', handle, handle, handle)
    delete = _api(gdi, 'DeleteObject', wintypes.BOOL, handle)
    flush = _api(gdi, 'GdiFlush', wintypes.BOOL)
    draw = _api(user, 'DrawIconEx', wintypes.BOOL, handle, ctypes.c_int, ctypes.c_int,
                handle, ctypes.c_int, ctypes.c_int, wintypes.UINT, handle, wintypes.UINT)
    dc = create_dc(None)
    if not dc:
        return None
    bitmap = previous = None
    try:
        header = (ctypes.c_int32 * 10)(40, size, -size, 1 | (32 << 16))
        bits = pointer()
        bitmap = dib(dc, header, 0, ctypes.byref(bits), None, 0)
        if not bitmap or not bits.value:
            return None
        previous = select(dc, bitmap)
        if not previous:
            return None
        red, green, blue = ImageColor.getrgb(background)
        pixels = bytes((blue, green, red, 0)) * (size * size)
        ctypes.memmove(bits, pixels, len(pixels))
        if draw(dc, 0, 0, found, size, size, 0, None, 3) and flush():
            rendered = ctypes.string_at(bits, len(pixels))
            if rendered != pixels:
                return Image.frombytes('RGB', (size, size), rendered, 'raw', 'BGRX')
        return None
    finally:
        if previous:
            select(dc, previous)
        if bitmap:
            delete(bitmap)
        delete_dc(dc)
