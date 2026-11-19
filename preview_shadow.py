"""A click-through native shadow, separate from the clipped recording card."""
import ctypes
from ctypes import wintypes
import os

from PIL import Image, ImageDraw, ImageFilter

PADDING = 24
SHADOW_STYLE = 0x080800A0  # NOACTIVATE | LAYERED | TOOLWINDOW | TRANSPARENT


class _BitmapInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('width', wintypes.LONG), ('height', wintypes.LONG),
                ('planes', wintypes.WORD), ('bit_count', wintypes.WORD), ('compression', wintypes.DWORD),
                ('image_size', wintypes.DWORD), ('x_pixels', wintypes.LONG), ('y_pixels', wintypes.LONG),
                ('colors', wintypes.DWORD), ('important', wintypes.DWORD)]


class _Size(ctypes.Structure):
    _fields_ = [('cx', wintypes.LONG), ('cy', wintypes.LONG)]


class _Blend(ctypes.Structure):
    _fields_ = [('operation', ctypes.c_ubyte), ('flags', ctypes.c_ubyte),
                ('alpha', ctypes.c_ubyte), ('format', ctypes.c_ubyte)]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [('size', wintypes.DWORD), ('monitor', wintypes.RECT),
                ('work', wintypes.RECT), ('flags', wintypes.DWORD)]


def _shadow_image(width, height):
    mask = Image.new('L', (width + 2 * PADDING, height + 2 * PADDING))
    ImageDraw.Draw(mask).rounded_rectangle(
        (PADDING, PADDING + 6, PADDING + width - 1, PADDING + height + 5), radius=24, fill=85)
    mask = mask.filter(ImageFilter.GaussianBlur(11))
    ImageDraw.Draw(mask).rounded_rectangle(
        (PADDING, PADDING, PADDING + width - 1, PADDING + height - 1), radius=24, fill=0)
    image = Image.new('RGBA', mask.size)
    image.putalpha(mask)
    return image


class CardShadow:
    def __init__(self):
        self.hwnd = None
        self._user = self._gdi = None
        self._layout = None

    def _load(self):
        if self._user is not None:
            return
        user = ctypes.WinDLL('user32', use_last_error=True)
        gdi = ctypes.WinDLL('gdi32', use_last_error=True)
        for library, name, arguments, result in (
            (user, 'IsWindow', [wintypes.HWND], wintypes.BOOL),
            (user, 'IsWindowVisible', [wintypes.HWND], wintypes.BOOL),
            (user, 'GetWindowThreadProcessId', [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            (user, 'GetWindowRect', [wintypes.HWND, ctypes.POINTER(wintypes.RECT)], wintypes.BOOL),
            (user, 'MonitorFromWindow', [wintypes.HWND, wintypes.DWORD], wintypes.HANDLE),
            (user, 'GetMonitorInfoW', [wintypes.HANDLE, ctypes.POINTER(_MonitorInfo)], wintypes.BOOL),
            (user, 'CreateWindowExW', [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
            (user, 'GetWindowLongW', [wintypes.HWND, ctypes.c_int], wintypes.LONG),
            (user, 'GetDC', [wintypes.HWND], wintypes.HDC),
            (user, 'ReleaseDC', [wintypes.HWND, wintypes.HDC], ctypes.c_int),
            (user, 'UpdateLayeredWindow', [wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                          ctypes.POINTER(_Size), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
                                          wintypes.DWORD, ctypes.POINTER(_Blend), wintypes.DWORD], wintypes.BOOL),
            (user, 'SetWindowPos', [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.BOOL),
            (user, 'ShowWindow', [wintypes.HWND, ctypes.c_int], wintypes.BOOL),
            (user, 'DestroyWindow', [wintypes.HWND], wintypes.BOOL),
            (gdi, 'CreateCompatibleDC', [wintypes.HDC], wintypes.HDC),
            (gdi, 'CreateDIBSection', [wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
                                      ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD], wintypes.HANDLE),
            (gdi, 'SelectObject', [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE),
            (gdi, 'DeleteObject', [wintypes.HANDLE], wintypes.BOOL),
            (gdi, 'DeleteDC', [wintypes.HDC], wintypes.BOOL),
        ):
            function = getattr(library, name)
            function.argtypes, function.restype = arguments, result
        self._user, self._gdi = user, gdi

    def _upload(self, image):
        user, gdi = self._user, self._gdi
        screen = memory = bitmap = previous = None
        try:
            screen = user.GetDC(None)
            if not screen:
                raise OSError('Shadow screen surface unavailable')
            memory = gdi.CreateCompatibleDC(screen)
            if not memory:
                raise OSError('Shadow memory surface unavailable')
            info = _BitmapInfo(size=ctypes.sizeof(_BitmapInfo), width=image.width, height=-image.height,
                               planes=1, bit_count=32)
            bits = ctypes.c_void_p()
            bitmap = gdi.CreateDIBSection(screen, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise OSError('Shadow bitmap unavailable')
            previous = gdi.SelectObject(memory, bitmap)
            if not previous or previous == ctypes.c_void_p(-1).value:
                previous = None
                raise OSError('Shadow bitmap selection failed')
            # Black RGB is already premultiplied for every alpha value.
            pixels = image.tobytes('raw', 'BGRA')
            ctypes.memmove(bits, pixels, len(pixels))
            size, origin, blend = _Size(*image.size), wintypes.POINT(), _Blend(0, 0, 255, 1)
            if not user.UpdateLayeredWindow(self.hwnd, screen, None, ctypes.byref(size), memory,
                                            ctypes.byref(origin), 0, ctypes.byref(blend), 2):
                raise OSError('Shadow composition failed')
        finally:
            if previous:
                gdi.SelectObject(memory, previous)
            if bitmap:
                gdi.DeleteObject(bitmap)
            if memory:
                gdi.DeleteDC(memory)
            if screen:
                user.ReleaseDC(None, screen)

    def show(self, card_hwnd):
        try:
            self._load()
            user = self._user
            owner = wintypes.DWORD()
            if not card_hwnd or not user.IsWindow(card_hwnd) or not user.IsWindowVisible(card_hwnd):
                self.hide()
                return False
            user.GetWindowThreadProcessId(card_hwnd, ctypes.byref(owner))
            if owner.value != os.getpid():
                self.hide()
                return False
            rectangle, monitor = wintypes.RECT(), _MonitorInfo(size=ctypes.sizeof(_MonitorInfo))
            if not user.GetWindowRect(card_hwnd, ctypes.byref(rectangle)) or not user.GetMonitorInfoW(
                    user.MonitorFromWindow(card_hwnd, 2), ctypes.byref(monitor)):
                raise OSError('Shadow bounds unavailable')
            width, height = rectangle.right - rectangle.left, rectangle.bottom - rectangle.top
            if width <= 0 or height <= 0:
                raise OSError('Shadow card bounds unavailable')
            left, top = rectangle.left - PADDING, rectangle.top - PADDING
            x, y = max(left, monitor.work.left), max(top, monitor.work.top)
            right = min(rectangle.right + PADDING, monitor.work.right)
            bottom = min(rectangle.bottom + PADDING, monitor.work.bottom)
            if right <= x or bottom <= y:
                raise OSError('Shadow outside monitor work area')
            if self.hwnd is not None and not user.IsWindow(self.hwnd):
                self.hwnd, self._layout = None, None
            if self.hwnd is None:
                self.hwnd = user.CreateWindowExW(SHADOW_STYLE, 'Static', '', 0x88000000,
                                                 0, 0, 0, 0, None, None, None, None)
                if not self.hwnd:
                    raise OSError('Shadow window unavailable')
            if user.GetWindowLongW(self.hwnd, -20) & SHADOW_STYLE != SHADOW_STYLE:
                raise OSError('Shadow input protection unavailable')
            layout = width, height, x - left, y - top, right - left, bottom - top
            if layout != self._layout:
                image = _shadow_image(width, height).crop(layout[2:])
                self._upload(image)
                self._layout = layout
            if not user.SetWindowPos(self.hwnd, card_hwnd, x, y, right - x, bottom - y, 0x50):
                raise OSError('Shadow placement failed')
            return True
        except (AttributeError, OSError):
            self.hide()
            return False

    def hide(self):
        if self.hwnd is not None and self._user is not None:
            try:
                self._user.ShowWindow(self.hwnd, 0)
            except (AttributeError, OSError):
                pass

    def destroy(self):
        if self.hwnd is not None and self._user is not None:
            self.hide()
            try:
                if not self._user.IsWindow(self.hwnd) or self._user.DestroyWindow(self.hwnd):
                    self.hwnd, self._layout = None, None
            except (AttributeError, OSError):
                pass
