"""Check the picker conflict policy without reserving a global shortcut."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hotkey_bindings import HotkeyRouter, shortcut_conflict, valid_keyboard_binding, windows_registration


class RegistrationProbe:
    def __init__(self):
        self.result = 'free'
        self.attempts = []

    def __call__(self, modifiers, key):
        self.attempts.append((modifiers, key))
        return self.result


probe = RegistrationProbe()
assert windows_registration('ctrl+page_down') == (2, 0x22)
assert windows_registration('shift+f24') == (4, 0x87)
assert windows_registration('ctrl+alt+delete') is None
assert windows_registration('ctrl_r') is None
for reserved in ('alt+f4', 'win+l', 'f12', 'ctrl+f12', 'ctrl+alt+delete'):
    assert shortcut_conflict(reserved, probe)[0] == 'blocked', reserved
    assert not valid_keyboard_binding(reserved)
assert shortcut_conflict('ctrl_r', probe)[0] == 'clear'
assert not probe.attempts

assert shortcut_conflict('ctrl+shift+f8', probe)[0] == 'clear'
assert probe.attempts[-1] == (('ctrl', 'shift'), 0x77)
assert shortcut_conflict('ctrl+c', probe)[0] == 'warning'
assert shortcut_conflict('ctrl+3', probe) == ('warning', 'Listen already uses this shortcut for History.')
assert shortcut_conflict('f8', probe)[0] == 'warning'
assert not valid_keyboard_binding('f')
probe.result = 'claimed'
assert shortcut_conflict('ctrl+shift+f8', probe)[0] == 'blocked'
probe.result = 'unknown'
assert shortcut_conflict('ctrl+shift+f8', probe)[0] == 'warning'

router = HotkeyRouter('f8')
assert router.transition('key', 0x77, True) == (True, ['press'])
assert router.transition('key', 0x77, True) == (True, [])
assert router.transition('key', 0x77, False) == (True, ['release'])
assert router.transition('key', 0x41, True) == (False, [])
print('Conflict policy and normal single-key press/repeat/release passed.')
