"""Read the Windows icon of a captured dictation destination."""
import ctypes
from ctypes import wintypes

from PIL import Image, ImageColor


def _api(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.argtypes, function.restype = arguments, result
    return function


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
