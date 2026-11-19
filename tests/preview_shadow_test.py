"""Shadow pixels, native ownership and failure cleanup without desktop access."""
from pathlib import Path
import ctypes
import os
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preview_shadow import CardShadow, _shadow_image


image = _shadow_image(120, 90)
assert image.mode == 'RGBA' and image.size == (168, 138)
assert image.getpixel((84, 69)) == (0, 0, 0, 0)
assert 0 < image.getpixel((84, 117))[3] < 255, 'No soft shadow below the card'
assert image.getextrema()[:3] == ((0, 0),) * 3, 'Black pixels must remain premultiplied'


class User:
    def __init__(self):
        self.positions, self.hidden, self.released = [], [], []
        self.created = self.uploads = 0
        self.upload_ok = self.position_ok = self.destroy_ok = True
        self.foreign = False
        self.card_x = 40
        self.card_y = 40
        self.visible = True
        self.alive = True
    def IsWindow(self, hwnd): return hwnd == 100 or self.alive
    def IsWindowVisible(self, hwnd): return self.visible
    def GetWindowThreadProcessId(self, hwnd, result): result._obj.value = os.getpid() + self.foreign
    def GetWindowRect(self, hwnd, result):
        r = result._obj
        r.left, r.top, r.right, r.bottom = self.card_x, self.card_y, self.card_x + 120, self.card_y + 90
        return 1
    def MonitorFromWindow(self, *args): return 3
    def GetMonitorInfoW(self, monitor, result):
        work = result._obj.work
        work.left, work.top, work.right, work.bottom = 0, 0, 400, 300
        return 1
    def CreateWindowExW(self, style, *args):
        assert style & 0x080800A0 == 0x080800A0
        self.created += 1
        return 200
    def GetWindowLongW(self, *args): return 0x080800A0
    def GetDC(self, *args): return 11
    def ReleaseDC(self, hwnd, dc): self.released.append(dc)
    def UpdateLayeredWindow(self, *args):
        self.uploads += 1
        return self.upload_ok
    def SetWindowPos(self, hwnd, after, x, y, width, height, flags):
        assert hwnd == 200 and after == 100 and flags == 0x50
        self.positions.append((x, y, width, height))
        return self.position_ok
    def ShowWindow(self, hwnd, command): self.hidden.append((hwnd, command))
    def DestroyWindow(self, hwnd):
        if self.destroy_ok: self.alive = False
        return self.destroy_ok


class Gdi:
    def __init__(self):
        self.deleted, self.selected, self.dcs = [], [], []
        self.fail = ''
    def CreateCompatibleDC(self, *args): return 0 if self.fail == 'memory' else 22
    def CreateDIBSection(self, dc, info, usage, bits, *args):
        if self.fail == 'bitmap': return 0
        self.buffer = ctypes.create_string_buffer(info._obj.width * -info._obj.height * 4)
        bits._obj.value = ctypes.addressof(self.buffer)
        return 33
    def SelectObject(self, dc, bitmap):
        self.selected.append(bitmap)
        return 0 if self.fail == 'select' else 44
    def DeleteObject(self, bitmap): self.deleted.append(bitmap)
    def DeleteDC(self, dc): self.dcs.append(dc)


def fixture():
    shadow = CardShadow()
    shadow._user, shadow._gdi = User(), Gdi()
    return shadow, shadow._user, shadow._gdi


shadow, user, gdi = fixture()
assert shadow.hwnd is None and shadow.show(100)
assert user.created == user.uploads == 1
assert gdi.selected == [33, 44] and gdi.deleted == [33] and gdi.dcs == [22] and user.released == [11]
assert shadow.show(100) and user.uploads == 1, 'Unchanged layout regenerated the shadow'
user.card_x = 55
assert shadow.show(100) and user.uploads == 1, 'A position-only change regenerated pixels'
user.card_x, user.card_y = 0, 0
assert shadow.show(100) and user.positions[-1] == (0, 0, 144, 114), 'Shadow exceeded monitor work area'
assert user.uploads == 2
user.foreign = True
assert not shadow.show(100) and user.hidden[-1] == (200, 0), 'Foreign card accepted'
user.foreign = False
user.visible = False
assert not shadow.show(100), 'Hidden card left a standalone shadow'
shadow.hide()
user.destroy_ok = False
shadow.destroy()
assert shadow.hwnd == 200, 'Failed destroy discarded native ownership'
user.destroy_ok = True
shadow.destroy()
shadow.destroy()
assert shadow.hwnd is None

shadow, user, gdi = fixture()
user.upload_ok = False
assert not shadow.show(100)
assert gdi.selected == [33, 44] and gdi.deleted == [33] and gdi.dcs == [22] and user.released == [11]
assert user.hidden[-1] == (200, 0)
user.upload_ok = True
assert shadow.show(100) and user.uploads == 2, 'Failed upload was cached as valid'
user.position_ok = False
assert not shadow.show(100) and user.hidden[-1] == (200, 0)
shadow.destroy()
for stage, deleted, dcs in (('memory', [], []), ('bitmap', [], [22]), ('select', [33], [22])):
    shadow, user, gdi = fixture()
    gdi.fail = stage
    assert not shadow.show(100), stage
    assert gdi.deleted == deleted and gdi.dcs == dcs and user.released == [11], stage
    assert user.hidden[-1] == (200, 0)
    shadow.destroy()
print('preview_shadow_test: passed')
