"""Complete first-run setup before starting the installed edition of Listen."""

import hashlib
import json
import ctypes
import os
from pathlib import Path
import queue
import runpy
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import winreg

from parakeet_runtime import MODEL_NAME, MODEL_SHA256, REVISION, model_path
from speech_runtime import SPEECH_MODEL_IDS


ROOT = Path(__file__).resolve().parent
MODEL_SIZE = 731_357_568
MODEL_URL = (
    'https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf/'
    f'resolve/{REVISION}/parakeet-unified-en-0.6b-Q8_0.gguf?download=true'
)
WEBVIEW_URL = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
WEBVIEW_KEY = r'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
VC_RUNTIME_URL = 'https://aka.ms/vc14/vc_redist.x64.exe'


class SetupError(RuntimeError):
    pass


def vc_runtime_ready():
    system = Path(os.environ['SystemRoot']) / 'System32'
    return all((system / name).is_file() for name in ('msvcp140.dll', 'msvcp140_1.dll'))


def webview_ready():
    for hive, flags in ((winreg.HKEY_CURRENT_USER, winreg.KEY_READ),
                        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_READ | winreg.KEY_WOW64_32KEY)):
        try:
            with winreg.OpenKey(hive, WEBVIEW_KEY, 0, flags) as key:
                version, _ = winreg.QueryValueEx(key, 'pv')
                if any(int(part) > 0 for part in version.split('.')):
                    return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def download(url, output, limit, on_progress, cancelled):
    digest = hashlib.sha256()
    size = 0
    deadline = time.monotonic() + 1800
    with urllib.request.urlopen(url, timeout=30) as response, output.open('xb') as target:
        while chunk := response.read(512 * 1024):
            size += len(chunk)
            if cancelled.is_set() or time.monotonic() > deadline or size > limit:
                raise OSError('Download stopped')
            target.write(chunk)
            digest.update(chunk)
            on_progress(size)
    if cancelled.is_set():
        raise OSError('Download stopped')
    return size, digest.hexdigest()


def model_selected():
    try:
        data = json.loads((ROOT / 'settings.json').read_text(encoding='utf-8'))
        selected = data.get('speech_model', MODEL_NAME)
    except (OSError, ValueError, AttributeError):
        selected = MODEL_NAME
    # Existing conversions and invalid values default to the same model as Listen.
    return selected == MODEL_NAME or not isinstance(selected, str) or selected not in SPEECH_MODEL_IDS


def speech_model_ready():
    try:
        model = model_path()
        state = model.stat()
        stored = model.with_suffix('.verified').read_text(encoding='ascii')
        return state.st_size == MODEL_SIZE and stored == f'{MODEL_SHA256}:{state.st_mtime_ns}'
    except (OSError, UnicodeError):
        return False


def prepare_model(report, cancelled):
    destination = model_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    verified = False
    if destination.is_file() and destination.stat().st_size == MODEL_SIZE:
        report('Checking your existing speech model', None)
        with destination.open('rb') as source:
            verified = hashlib.file_digest(source, 'sha256').hexdigest() == MODEL_SHA256
    if cancelled.is_set():
        return
    if not verified:
        if shutil.disk_usage(destination.parent).free < MODEL_SIZE:
            raise OSError('Not enough disk space for the speech model')
        report('Downloading English speech model', 0)
        with tempfile.TemporaryDirectory(prefix='listen-download-', dir=destination.parent) as working:
            temporary = Path(working) / destination.name
            size, digest = download(MODEL_URL, temporary, MODEL_SIZE,
                lambda count: report('Downloading English speech model', count * 100 // MODEL_SIZE), cancelled)
            if size != MODEL_SIZE or digest != MODEL_SHA256:
                raise OSError('Speech model verification failed')
            temporary.replace(destination)
    # The speech worker independently checks the full hash before using the model.
    try:
        destination.with_suffix('.verified').write_text(
            f'{MODEL_SHA256}:{destination.stat().st_mtime_ns}', encoding='ascii')
    except OSError:
        pass


def install_microsoft(url, name, *, administrator, cancelled):
    system = Path(os.environ['SystemRoot'])
    powershell = system / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    permitted = {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'LOCALAPPDATA', 'APPDATA',
                 'USERPROFILE', 'PROGRAMDATA', 'ALLUSERSPROFILE', 'SYSTEMDRIVE',
                 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMW6432',
                 'COMMONPROGRAMFILES', 'COMMONPROGRAMFILES(X86)', 'COMMONPROGRAMW6432',
                 'PROCESSOR_ARCHITECTURE', 'USERNAME', 'USERDOMAIN', 'COMPUTERNAME'}
    environment = {key: value for key, value in os.environ.items() if key.upper() in permitted}
    environment['PATH'] = os.pathsep.join((str(system / 'System32'), str(system)))
    with tempfile.TemporaryDirectory(prefix='listen-microsoft-') as working:
        bootstrapper = Path(working) / name
        download(url, bootstrapper, 64 * 1024 * 1024, lambda count: None, cancelled)
        environment['LISTEN_MICROSOFT_SETUP'] = str(bootstrapper)
        signature = (
            '$signature=Get-AuthenticodeSignature -LiteralPath $env:LISTEN_MICROSOFT_SETUP; '
            'if ($signature.Status -ne "Valid" -or '
            '$signature.SignerCertificate.GetNameInfo("SimpleName",$false) -ne "Microsoft Corporation") '
            '{exit 1}'
        )
        subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-Command', signature],
                       env=environment, check=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
        if cancelled.is_set():
            return
        command = [str(bootstrapper), '/silent', '/install']
        if administrator:
            command = [str(powershell), '-NoProfile', '-NonInteractive', '-Command',
                       '$ErrorActionPreference="Stop"; '
                       '$process=Start-Process -FilePath $env:LISTEN_MICROSOFT_SETUP '
                       '-ArgumentList "/install","/quiet","/norestart" -Verb RunAs -PassThru -Wait; '
                       'exit $process.ExitCode']
        installed = subprocess.run(command, env=environment, timeout=600,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        if installed.returncode not in (0, 3010, 1641):
            if administrator:
                raise SetupError('Windows could not install the Visual C++ runtime. Approve the Windows permission prompt and try again.')
            raise SetupError('Windows could not install Microsoft WebView2. Try again.')


def prepare(report, cancelled):
    if not vc_runtime_ready():
        report('Installing Microsoft Visual C++. Windows may ask for permission.', None)
        install_microsoft(VC_RUNTIME_URL, 'vc_redist.x64.exe', administrator=True, cancelled=cancelled)
        if not cancelled.is_set() and not vc_runtime_ready():
            raise SetupError('Restart Windows to finish installing Visual C++, then open Listen again.')
    if not cancelled.is_set() and not webview_ready():
        report('Installing Microsoft WebView2', None)
        install_microsoft(WEBVIEW_URL, 'MicrosoftEdgeWebview2Setup.exe', administrator=False, cancelled=cancelled)
        if not cancelled.is_set() and not webview_ready():
            raise SetupError('Restart Windows to finish installing Microsoft WebView2, then open Listen again.')
    if not cancelled.is_set() and model_selected() and not speech_model_ready():
        prepare_model(report, cancelled)


def setup_window():
    import tkinter as tk
    from tkinter import ttk

    window = tk.Tk()
    window.title('Set up Listen')
    window.resizable(False, False)
    window.columnconfigure(0, weight=1)
    ttk.Label(window, text='Set up Listen', font=('Segoe UI', 20, 'bold')).grid(
        row=0, column=0, sticky='w', padx=24, pady=(20, 10))
    ttk.Label(window, text='Listen downloads its English speech model once (about 730 MB).',
              wraplength=430).grid(row=1, column=0, sticky='w', padx=24, pady=(0, 12))
    message = tk.StringVar(value='Preparing Listen…')
    ttk.Label(window, textvariable=message, wraplength=430).grid(row=2, column=0, sticky='w', padx=24)
    progress = ttk.Progressbar(window, mode='determinate', length=430)
    progress.grid(row=3, column=0, padx=24, pady=14)
    retry = ttk.Button(window, text='Try again')
    retry.grid(row=4, column=0, sticky='e', padx=24, pady=(0, 20))
    events = queue.SimpleQueue()
    cancelled = threading.Event()
    success = False
    running = False

    def report(text, percent):
        events.put((text, percent))

    def work():
        try:
            prepare(report, cancelled)
        except (OSError, ValueError, subprocess.SubprocessError, SetupError) as error:
            if not cancelled.is_set():
                try:
                    (ROOT / 'setup.log').write_text(
                        f'First-run setup needs a retry ({type(error).__name__}).\n', encoding='ascii')
                except OSError:
                    pass
            events.put(str(error) if isinstance(error, SetupError) else False)
        else:
            events.put(True)

    def start():
        nonlocal running
        try:
            (ROOT / 'setup.log').unlink(missing_ok=True)
        except OSError:
            pass
        running = True
        retry.state(['disabled'])
        threading.Thread(target=work, name='Listen setup', daemon=True).start()

    def close():
        cancelled.set()
        if running:
            message.set('Finishing the current setup step…')
        else:
            window.destroy()

    def poll():
        nonlocal success, running
        while not events.empty():
            event = events.get()
            if isinstance(event, tuple):
                if cancelled.is_set():
                    continue
                text, percent = event
                message.set(f'{text} · {percent}%' if percent is not None else text)
                progress['value'] = percent or 0
            else:
                running = False
                success = event is True and not cancelled.is_set()
                if success or cancelled.is_set():
                    window.destroy()
                    return
                message.set(event if isinstance(event, str) else
                            'Setup could not finish. Check your internet connection and available disk space, then try again.')
                retry.state(['!disabled'])
        window.after(100, poll)

    retry.configure(command=start)
    window.protocol('WM_DELETE_WINDOW', close)
    window.after(100, poll)
    window.after_idle(start)
    window.mainloop()
    return success


def check_runtime():
    import importlib
    for name in ('tkinter', 'webview', 'clr', 'numpy', 'sounddevice', 'PIL',
                 'pynput', 'pystray', 'pyperclip', 'requests', 'anthropic', 'faster_whisper', 'ctranslate2'):
        importlib.import_module(name)
    import tkinter
    tkinter.Tcl()
    return (ROOT / 'speech-runtime' / 'transcribe.dll').is_file()


def main():
    if sys.argv[1:] == ['--check']:
        raise SystemExit(0 if check_runtime() else 1)
    if sys.argv[1:] == ['--prepare']:
        prepare(lambda text, percent: print(text) if percent in (None, 0) else None, threading.Event())
        raise SystemExit(0)
    if not vc_runtime_ready() or not webview_ready() or (model_selected() and not speech_model_ready()):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        ctypes.set_last_error(0)
        lock = kernel.CreateMutexW(None, False, r'Local\Listen.FirstRunSetup')
        already_running = ctypes.get_last_error() == 183
        if not lock:
            raise OSError('Could not start Listen setup')
        try:
            if already_running:
                return
            if not setup_window():
                raise SystemExit(1)
        finally:
            kernel.CloseHandle(lock)
    runpy.run_path(str(ROOT / 'launch_listen.pyw'), run_name='__main__')


if __name__ == '__main__':
    main()
