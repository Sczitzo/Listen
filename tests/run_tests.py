"""Run the unpaid regression suites in isolated copies, with network access blocked."""
import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEVICE_SUITES = frozenset(("logic_test.py",))
HELPERS = ("run_tests.py", "latency_measure.py", "multiline_delivery_windows_test.py")
SUITES = ("ui_render_optimization_test.py", "speech_process_test.py", "speech_lifecycle_test.py", "right_ctrl_delivery_test.py", "destination_focus_test.py", "delivery_wait_test.py", "delivery_admission_test.py", "preview_state_test.py", "live_preview_integration_test.py", "preview_ui_test.py", "speech_runtime_test.py", "cpu_fallback_integration_test.py", "cpu_fallback_ui_test.py", "text_input_windows_test.py", "direct_delivery_integration_test.py", "delivery_ui_test.py", "ux_ui_contract_test.py", "ui_defects_test.py", "recording_delivery_test.py", "workspace_transactions_test.py", "voice_engineering_test.py", "judge_model_test.py", "sound_cues_test.py", "workspace_state_test.py", "experience_bridge_test.py", "experience_test.py", "provider_accounting_test.py", "quality_storage_test.py", "paste_windows_test.py", "ownership_test.py", "audio_devices_test.py", "startup_recovery_test.py", "privacy_recovery_test.py", "logic_test.py", "api_test.py")

SUITES = ("parakeet_runtime_test.py", "parakeet_process_test.py", "model_settings_test.py", "ui_repair_test.py", "ui_repair_backend_test.py", "robustness_test.py", "speech_unload_test.py", "shell_surface_test.py", "brand_test.py", "natural_lists_test.py", "natural_lists_integration_test.py", "field_recovery_integration_test.py", "list_field_trials_integration_test.py", "list_field_trials_test.py", "multiline_native_harness_test.py", "multiline_delivery_integration_test.py", "verified_paste_test.py", "field_access_test.py", "spoken_list_integration_test.py", "spoken_list_test.py", "spoken_format_test.py", "long_draft_test.py", "long_draft_integration_test.py", "preview_card_integration_test.py", "transcript_preview_test.py", "draft_preview_test.py", "preview_card_test.py", "hotkey_responsiveness_test.py", "cleanup_integration_test.py", "cleanup_http_test.py", "cleanup_policy_test.py", "worker_recovery_latency_test.py", "test_runner_safety_test.py", "latency_contract_test.py", "tray_status_test.py") + SUITES

# Windows Store Python may be inaccessible while its installed packages remain
# usable by an existing interpreter of the same Python minor version.
packages = []
if sys.platform == "win32" and importlib.util.find_spec("numpy") is None:
    version = f"Python{sys.version_info.major}{sys.version_info.minor}"
    store = Path.home() / "AppData" / "Local" / "Packages"
    packages = sorted(store.glob(
        f"PythonSoftwareFoundation.Python.{sys.version_info.major}.{sys.version_info.minor}_*"
        f"/LocalCache/local-packages/{version}/site-packages"
    ))

CHILD = '''
import os, runpy, sys, tempfile
sys.dont_write_bytecode = True
from pathlib import Path
tempfile.tempdir = sys.argv[1]
sys.path.extend(sys.argv[3:])
sandbox = Path(sys.argv[1]).resolve()
no_devices = os.environ.get('DICTATE_TEST_NO_DEVICES') == '1'
device_guards_ready = False
os.environ['ANTHROPIC_API_KEY'] = 'isolated-disabled-test-value'
private_names = {'settings.json', 'history.jsonl', 'favorites.json', 'learned.json',
                 'workspace.json', 'costs.jsonl', 'dictation.log'}
input_symbols = {'SendInput', 'keybd_event', 'mouse_event', 'SetCursorPos',
                 'SetWindowsHookExA', 'SetWindowsHookExW', 'OpenClipboard',
                 'GetClipboardData', 'SetClipboardData', 'EmptyClipboard', 'GetClipboardSequenceNumber'}
def block_network(event, args):
    if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
        raise PermissionError('Network disabled for tests')
    if event == 'open' and not isinstance(args[0], int):
        flags = args[2]
        path = Path(args[0]).resolve()
        if path.name.lower() in private_names and not path.is_relative_to(sandbox):
            raise PermissionError('Private saved-file reads outside test copy disabled')
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            if not path.is_relative_to(sandbox):
                raise PermissionError('Writes outside temporary test copy disabled')
    if event == 'winreg.OpenKey' and len(args) > 1 and str(args[1]).lower() == 'environment':
        raise PermissionError('Registry credential lookup disabled for tests')
    if no_devices and device_guards_ready and event == 'ctypes.dlsym' and len(args) > 1 and args[1] in input_symbols:
        raise PermissionError('Native input/clipboard API disabled for device-free tests')
    if event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.rename'):
        paths = args[:2] if event == 'os.rename' else args[:1]
        if any(not Path(path).resolve().is_relative_to(sandbox) for path in paths):
            raise PermissionError('Changes outside temporary test copy disabled')
sys.addaudithook(block_network)
if no_devices:
    sys.path.insert(0, str(sandbox / 'tests'))
    from run_tests import install_device_guards
    install_device_guards(input_symbols)
    device_guards_ready = True
test_script = sys.argv[2]
sys.argv = [test_script]
runpy.run_path(test_script, run_name='__main__')
'''


def install_device_guards(input_symbols):
    """Bootstrap trusted libraries, then disable cached device/input entry points."""
    def forbidden_device(*args, **kwargs):
        raise PermissionError('Real device/input/clipboard access disabled for tests')
    import field_access
    field_access.default_process_factory = forbidden_device
    original_warmup = field_access.FieldAccess.request_warmup
    def guarded_warmup(instance):
        if instance._factory is forbidden_device:
            return False
        return original_warmup(instance)
    field_access.FieldAccess.request_warmup = guarded_warmup
    import sounddevice, pyperclip
    from pynput import keyboard, mouse
    for name in ('InputStream', 'RawInputStream', 'OutputStream', 'RawOutputStream',
                 'Stream', 'RawStream', 'play', 'rec', 'playrec', 'query_devices'):
        setattr(sounddevice, name, forbidden_device)
    pyperclip.copy = pyperclip.paste = forbidden_device
    keyboard.Listener.start = forbidden_device
    for name in ('press', 'release', 'type', 'tap'):
        setattr(keyboard.Controller, name, forbidden_device)
    mouse.Listener.start = forbidden_device
    for name in ('press', 'release', 'click', 'scroll', 'move'):
        setattr(mouse.Controller, name, forbidden_device)
    mouse.Controller.position = property(mouse.Controller.position.fget, forbidden_device)
    # pynput binds native pointers during its trusted import. Replace those
    # cached aliases before app/test code and reject any later native binding.
    import ctypes
    for symbol in input_symbols:
        setattr(ctypes.windll.user32, symbol, forbidden_device)
    for module_name in ('pynput._util.win32', 'pynput.keyboard._win32', 'pynput.mouse._win32'):
        module = sys.modules.get(module_name)
        if module is not None:
            for symbol in input_symbols.intersection(vars(module)):
                setattr(module, symbol, forbidden_device)


def select_suites(no_devices=False, requested=None):
    selected = tuple(dict.fromkeys(requested or SUITES))
    if any(name not in SUITES for name in selected):
        raise ValueError('Unknown suite selection')
    if no_devices and requested and any(name in DEVICE_SUITES for name in selected):
        raise ValueError('Physical-device suite is not admitted by --no-devices')
    return tuple(name for name in selected if not no_devices or name not in DEVICE_SUITES)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-devices', action='store_true', help='Exclude physical suites and block real device/input/clipboard calls')
    parser.add_argument('--suite', action='append', choices=SUITES, help='Run only this suite; repeat to select several')
    args = parser.parse_args(argv)
    try:
        selected = select_suites(args.no_devices, args.suite)
    except ValueError as error:
        parser.error(str(error))
    print(f"Interpreter: {sys.executable}", flush=True)
    print(f"Selected {len(selected)} suites; device-free={args.no_devices}; excluded={sorted(set(SUITES) - set(selected))}", flush=True)
    for path in packages:
        print(f"Existing packages: {path}", flush=True)
    failures = []
    for name in selected:
        # Use the writable checkout, avoiding inaccessible sandbox temp folders.
        with tempfile.TemporaryDirectory(prefix=".test-run-", dir=ROOT) as folder:
            target = Path(folder)
            for source in ROOT.glob("*.py"):
                shutil.copy2(source, target / source.name)
            (target / "ui").mkdir()
            for source in (ROOT / "ui").glob("*"):
                if source.is_file(): shutil.copy2(source, target / "ui" / source.name)
            sounds = target / "assets" / "sounds"
            sounds.mkdir(parents=True)
            for source in (ROOT / "assets" / "sounds").glob("*.wav"):
                shutil.copy2(source, sounds / source.name)
            (target / "tests").mkdir()
            for helper in dict.fromkeys((name,) + HELPERS):
                helper_path = ROOT / 'tests' / helper
                if helper_path.is_file():
                    shutil.copy2(helper_path, target / 'tests' / helper)
            if name == 'latency_contract_test.py':
                baseline_dir = target / 'audit/2026-10-28/latency/implementation'
                baseline_dir.mkdir(parents=True)
                shutil.copy2(ROOT / 'audit/2026-10-28/latency/implementation/baseline.zip',
                             baseline_dir / 'baseline.zip')
            env = os.environ.copy()
            env['ANTHROPIC_API_KEY'] = 'isolated-disabled-test-value'
            env['DICTATE_TEST_NO_DEVICES'] = '1' if args.no_devices else '0'
            env.update(TEMP=folder, TMP=folder, PYTHONIOENCODING="utf-8")
            try:
                result = subprocess.run(
                    [sys.executable, "-c", CHILD, folder,
                     str(target / "tests" / name), *map(str, packages)],
                    cwd=target, env=env, timeout=90,
                )
                failed = result.returncode != 0
            except subprocess.TimeoutExpired:
                print(f"{name}: timed out after 90 seconds", flush=True)
                failed = True
            print(f"{name}: {'FAIL' if failed else 'PASS'}", flush=True)
            if failed:
                failures.append(name)
    return bool(failures)


if __name__ == "__main__":
    sys.exit(main())
