"""Fail-before-access checks, executed only inside the guarded test runner."""
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_tests import DEVICE_SUITES, SUITES, select_suites

if os.environ.get('DICTATE_TEST_NO_DEVICES') != '1':
    print('Skipped guard checks: use run_tests.py --no-devices')
    raise SystemExit(0)
assert os.environ.get('ANTHROPIC_API_KEY') == 'isolated-disabled-test-value'
assert len(sys.argv) == 1, 'Runner transport arguments leaked into the test CLI'
assert 'logic_test.py' in select_suites()
assert not DEVICE_SUITES.intersection(select_suites(True))
assert select_suites(True, ['api_test.py', 'api_test.py']) == ('api_test.py',)
for requested in (['logic_test.py'], ['../api_test.py'], ['missing_test.py']):
    try:
        select_suites(True, requested)
    except ValueError:
        pass
    else:
        raise AssertionError('Unsafe suite selection was admitted')


def rejects(action):
    try:
        action()
    except PermissionError:
        return
    raise AssertionError('Test guard did not reject access before execution')


import sounddevice
import pyperclip
from pynput import keyboard

for name in ('InputStream', 'RawInputStream', 'OutputStream', 'Stream', 'play', 'rec', 'query_devices'):
    rejects(lambda name=name: getattr(sounddevice, name)())
rejects(lambda: pyperclip.copy('synthetic only'))
rejects(pyperclip.paste)
rejects(lambda: keyboard.Controller().press('x'))
rejects(lambda: keyboard.Listener(on_press=lambda key: None).start())
rejects(lambda: sys.audit('ctypes.dlsym', None, 'SendInput'))
rejects(lambda: sys.audit('winreg.OpenKey', None, 'Environment', 0, 0))
outside = Path(os.environ['TEMP']).parent / '.guard-probe-not-created'
rejects(lambda: (outside / 'settings.json').read_text(encoding='utf-8'))
rejects(lambda: (outside / 'write.txt').write_text('synthetic', encoding='utf-8'))
assert not outside.exists()
print('Runner safety: selection and device/clipboard/native/credential/private-read/write guards passed')
