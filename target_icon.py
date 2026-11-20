"""Read the Windows icon of a captured dictation destination."""
import base64
import ctypes
from ctypes import wintypes
from functools import lru_cache
import io
import ntpath
from pathlib import Path
import time
from xml.etree import ElementTree

from PIL import Image, ImageColor
from safe_storage import HISTORY_ICON_LIMIT, HISTORY_ICON_SIZE, SHARP_ICON_PREFIX

_HISTORY_BACKGROUND = '#2d2925'


def _api(dll, name, result, *arguments):
    function = getattr(dll, name)
    function.argtypes, function.restype = arguments, result
    return function


def history_app(hwnd, pid, *, legacy_icons=None):
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
        if legacy_icons is not None:
            saved = legacy_icons.get(app['name'].casefold())
            if not saved:
                return None
            previous = _encode_icon(window_icon(hwnd, pid, 32, _HISTORY_BACKGROUND))
            if previous not in saved:
                return None
        try:
            icon = _executable_icon(path.value, handle)
        except (OSError, AttributeError):
            icon = None
        try:
            if icon is None and legacy_icons is None:
                icon = window_icon(hwnd, pid, 32, _HISTORY_BACKGROUND)
            encoded = _encode_icon(icon)
            if encoded:
                app['icon'] = encoded
                if legacy_icons is not None:
                    app['previous_icon'] = previous
        except (OSError, ValueError):
            pass
        if owner(hwnd, ctypes.byref(process)) and process.value == pid:
            return app
        return None
    finally:
        close(handle)


def _encode_icon(icon):
    if icon is None:
        return None
    with icon, io.BytesIO() as output:
        icon.save(output, format='PNG')
        if output.tell() > HISTORY_ICON_LIMIT * 3 // 4:
            output.seek(0)
            output.truncate()
            with icon.quantize() as compact:
                compact.save(output, format='PNG')
        encoded = base64.b64encode(output.getvalue()).decode('ascii')
    return encoded if len(encoded) <= HISTORY_ICON_LIMIT else None


def _packaged_icon(path, process):
    if not process:
        return None
    kernel = ctypes.WinDLL('kernel32')
    get_name = _api(kernel, 'GetPackageFullName', ctypes.c_long, wintypes.HANDLE,
                    ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR)
    get_path = _api(kernel, 'GetPackagePathByFullName', ctypes.c_long, wintypes.LPCWSTR,
                    ctypes.POINTER(wintypes.UINT), wintypes.LPWSTR)
    name = ctypes.create_unicode_buffer(256)
    length = wintypes.UINT(len(name))
    if get_name(process, ctypes.byref(length), name):
        return None
    folder = ctypes.create_unicode_buffer(32768)
    length.value = len(folder)
    if get_path(name.value, ctypes.byref(length), folder):
        return None
    try:
        package = Path(folder.value).resolve()
        executable = Path(path).resolve().relative_to(package)
        with (package / 'AppxManifest.xml').open('rb') as stream:
            manifest = stream.read(1024 * 1024 + 1)
        if len(manifest) > 1024 * 1024:
            return None
        for app in ElementTree.fromstring(manifest).findall('.//{*}Application'):
            if ntpath.normcase(app.get('Executable', '')) != ntpath.normcase(str(executable)):
                continue
            visual = app.find('{*}VisualElements')
            if visual is None:
                continue
            small = Path(visual.get('Square44x44Logo', ''))
            large = Path(visual.get('Square150x150Logo', ''))
            for asset in (small.with_stem(small.stem + '.targetsize-256_altform-unplated') if small.name else small, large):
                candidate = (package / asset).resolve()
                if not asset.name or not candidate.is_relative_to(package) or not candidate.is_file() or candidate.stat().st_size > 1024 * 1024:
                    continue
                with Image.open(candidate, formats=['PNG']) as image:
                    if image.width == image.height and HISTORY_ICON_SIZE <= image.width <= 1024:
                        return image.convert('RGBA').resize((HISTORY_ICON_SIZE,) * 2, Image.Resampling.LANCZOS)
    except (OSError, ValueError, ElementTree.ParseError):
        return None
    return None


def _executable_icon(path, process=None):
    drive, _ = ntpath.splitdrive(path)
    if len(drive) != 2 or drive[1] != ':':
        return None
    drive_type = _api(ctypes.WinDLL('kernel32'), 'GetDriveTypeW', wintypes.UINT, wintypes.LPCWSTR)
    if drive_type(drive + '\\') != 3:  # DRIVE_FIXED
        return None
    icon = _packaged_icon(path, process)
    if icon is not None:
        return icon
    shell = ctypes.WinDLL('shell32')
    user = ctypes.WinDLL('user32')
    handle = wintypes.HANDLE
    extract = _api(shell, 'SHDefExtractIconW', ctypes.c_long, wintypes.LPCWSTR,
                   ctypes.c_int, wintypes.UINT, ctypes.POINTER(handle), ctypes.POINTER(handle), wintypes.UINT)
    destroy = _api(user, 'DestroyIcon', wintypes.BOOL, handle)
    icon = handle()
    try:
        if extract(path, 0, 0, ctypes.byref(icon), None, HISTORY_ICON_SIZE) == 0 and icon:
            return _draw_icon(user, icon, HISTORY_ICON_SIZE, _HISTORY_BACKGROUND)
        return None
    finally:
        if icon:
            destroy(icon)


@lru_cache(maxsize=8)
def _open_app_icons(requested, _time_window):
    user = ctypes.WinDLL('user32')
    visible = _api(user, 'IsWindowVisible', wintypes.BOOL, wintypes.HWND)
    owner = _api(user, 'GetWindowThreadProcessId', wintypes.DWORD, wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    walk = _api(user, 'EnumWindows', wintypes.BOOL, callback, wintypes.LPARAM)
    icons, visited = {}, 0
    legacy_icons = {}
    for name, icon in requested:
        legacy_icons.setdefault(name, set()).add(icon)

    @callback
    def collect(hwnd, _):
        nonlocal visited
        if not visible(hwnd):
            return True
        visited += 1
        pid = wintypes.DWORD()
        if owner(hwnd, ctypes.byref(pid)):
            try:
                app = history_app(hwnd, pid.value, legacy_icons=legacy_icons)
                if app and app.get('previous_icon'):
                    key, icon = (app['name'].casefold(), app['previous_icon']), app['icon']
                    icons[key] = icon if icons.get(key, icon) == icon else None
            except Exception:
                pass  # A closed or inaccessible window cannot stop History from opening.
        return visited < 128

    walk(collect, 0)
    return {key: icon for key, icon in icons.items() if icon}


def refresh_history_icons(rows):
    legacy = [row['app'] for row in rows if row.get('app', {}).get('icon')
              and not row['app']['icon'].startswith(SHARP_ICON_PREFIX)]
    if legacy:
        keys = frozenset((app['name'].casefold(), app['icon']) for app in legacy)
        try:
            sharp = _open_app_icons(keys, int(time.monotonic() // 30))
        except (OSError, AttributeError):
            return rows
        for app in legacy:
            app['icon'] = sharp.get((app['name'].casefold(), app['icon']), app['icon'])
    return rows


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

    return _draw_icon(user, found, size, background)


def _draw_icon(user, icon, size, background):
    # Draw into a local top-down 32-bit bitmap; the caller owns the icon lifetime.
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
        if draw(dc, 0, 0, icon, size, size, 0, None, 3) and flush():
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
