"""Opt-in owned WebView multiline paste qualification; no model/mic/global hooks."""
import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = Path.home() / 'AppData/Local/Packages/PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0/LocalCache/local-packages/Python313/site-packages'
sys.path.append(str(PACKAGES))
sys.dont_write_bytecode = True
SOURCES = ('field_access.py', 'field_access_worker.py', 'speech_process.py',
           'speech_process_worker.py', 'speech_runtime.py', 'text_input_windows.py',
           'paste_windows.py', 'multiline_delivery.py', 'spoken_format.py',
           'spoken_corrections.py', 'spoken_lists.py', 'dictation_quality.py')
PAYLOAD = '- Apples \U0001f34e\n- Caf\u00e9 and milk\n- \u6f22\u5b57 eggs'
BASE = 'Synthetic prefix SELECT synthetic suffix.'
START, END = BASE.index('SELECT'), BASE.index('SELECT') + len('SELECT')
HTML = '''<!doctype html><meta charset="utf-8"><body style="font:18px Segoe UI;padding:24px">
<h2>Owned multiline paste qualification</h2><p>Synthetic fields only. No microphone or models.</p>
<form id="form"><textarea id="plain" aria-label="Synthetic plain" rows="5" style="width:95%"></textarea>
<div id="rich" aria-label="Synthetic rich" role="textbox" contenteditable="true" style="white-space:pre-wrap;border:1px solid;padding:12px;min-height:130px"></div></form>
<script>
window.trace={pastes:0,enters:0,tabs:0,submits:0,ctrlV:0,byField:{}};
document.addEventListener('paste',e=>{trace.pastes++;trace.byField[e.target.id]=(trace.byField[e.target.id]||0)+1});
document.addEventListener('keydown',e=>{if(e.key==='Enter')trace.enters++;if(e.key==='Tab')trace.tabs++;if(e.ctrlKey&&e.key.toLowerCase()==='v')trace.ctrlV++});
document.getElementById('form').onsubmit=e=>{e.preventDefault();trace.submits++};
window.configure=(id,text,start,end)=>{let e=document.getElementById(id);e.focus();
if(e instanceof HTMLTextAreaElement){e.value=text;e.setSelectionRange(start,end)}else{e.textContent=text;let r=document.createRange();r.setStart(e.firstChild,start);r.setEnd(e.firstChild,end);let s=getSelection();s.removeAllRanges();s.addRange(r)}
return document.activeElement===e&&document.hasFocus()};
window.read=(id)=>{let e=document.getElementById(id);return e instanceof HTMLTextAreaElement?e.value:e.innerText};
</script></body>'''


def child(directory):
    folder = Path(directory).resolve()
    assert ROOT == folder and folder.name.startswith('.qa-multiline-')
    sys.path.insert(0, str(folder))
    import ctypes
    from ctypes import wintypes
    import psutil
    import webview
    from field_access import FieldAccess
    from text_input_windows import WindowsNative as InputNative, Target
    from paste_windows import WindowsNative as ClipboardNative
    from multiline_delivery import MultilineDelivery, VerifiedPasteBackend
    payloads = None
    if (folder / 'field-trials.flag').exists():
        from spoken_format import prepare_spoken_list
        examples = (
            'I need a shopping list with 4 items, 2 apples, 3 bananas, 1 carton of milk, and 6 eggs',
            'Make a number list First, wash the vegetables Second, chop them Third, put them in the refrigerator',
            'Make a letter list of three options. Take the bus, ride a bicycle, or walk.',
            'I need a shopping list with three items Bread, Milk, and Eggs Oh, and some ice cream',
            "Okay, I need a shopping list with three items or so. It's going to be apples, bananas, oranges, actually scratch that milk.",
            'Create a list for groceries. First item is apples. Number two is oranges. Number three is grapes.',
        )
        preparations = [prepare_spoken_list(raw) for raw in examples]
        assert all(prepared is not None for prepared in preparations)
        payloads = [prepared.formatted for prepared in preparations]
        assert all('\n' in payload for payload in payloads)
        assert 'oranges' not in payloads[4].lower() and 'bananas' in payloads[4]
        assert payloads[5] == 'Create a list for groceries.\n1. apples\n2. oranges\n3. grapes'

    report = dict(status='running', pid=os.getpid(), cases=[], input_batches=0,
                  microphone_calls=0, model_calls=0, native_clipboard_content_logged=False,
                  network_calls=0, clipboard_mutated=False, requires_recovery=False,
                  limitations=['Owned synthetic WebView only; no Codex/Melty live acceptance.',
                               'Python socket requests blocked; native browser background networking not separately measured.'])
    report['field_trial_payloads'] = payloads is not None
    result_file = folder / 'result.json'
    def save(phase):
        report['phase'] = phase
        result_file.write_text(json.dumps(report, indent=2), encoding='utf-8')
    def audit(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
            report['network_calls'] += 1
            raise PermissionError('Network prohibited in native fixture')
    sys.addaudithook(audit)
    fields = FieldAccess()
    native, api = InputNative(), ClipboardNative()
    backend = VerifiedPasteBackend(api)
    valid = [True]
    delivery = MultilineDelivery(fields, lambda: native, lambda: valid[0], backend)
    user = native.user
    for name, args, result in (
        ('FindWindowW', [wintypes.LPCWSTR, wintypes.LPCWSTR], wintypes.HWND),
        ('SetForegroundWindow', [wintypes.HWND], wintypes.BOOL),
        ('AttachThreadInput', [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL], wintypes.BOOL),
    ):
        function = getattr(user, name)
        function.argtypes, function.restype = args, result
    title = 'Owned formatted paste QA ' + str(os.getpid())
    window = webview.create_window(title, html=HTML, width=900, height=700)
    other = webview.create_window(title + ' secondary', html='<h2>Owned secondary window: no input expected.</h2>',
                                  width=500, height=250, hidden=True)
    hwnd = other_hwnd = 0
    original = None
    active_field = [None]
    raw_send = api.send_paste
    def own(handle):
        assert handle and user.IsWindow(handle) and native.pid_of(handle) == os.getpid(), 'Native owned HWND/PID guard failed'
    def foreground(handle):
        own(handle)
        assert int(user.GetForegroundWindow() or 0) == handle, 'Owned window is not foreground; no input'
    def js(code):
        foreground(hwnd)
        return window.evaluate_js(code)
    def wait(predicate, label, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.025)
        raise AssertionError(label)
    def dom_ready():
        return js('document.hasFocus() && document.activeElement.id===' + json.dumps(active_field[0]))
    def configure(field, text=BASE, start=START, end=END):
        active_field[0] = field
        assert js('window.configure(' + ','.join(json.dumps(value) for value in (field, text, start, end)) + ')')
        wait(dom_ready, 'Owned DOM editor/document readiness failed')
        # Accessibility provider focus trails DOM focus. This delay is fixture
        # readiness only; production capture keeps its strict 250ms deadline.
        time.sleep(.25)
    def target():
        foreground(hwnd)
        assert dom_ready()
        value = native.capture_target()
        assert isinstance(value, Target) and value.hwnd == hwnd and value.pid == os.getpid()
        assert native.target_ready(value)
        started = time.monotonic()
        cap = fields.capture(value, timeout=.25)
        elapsed = time.monotonic() - started
        report.setdefault('capture_ms', []).append(round(elapsed * 1000, 2))
        assert cap, 'Read-only helper could not capture owned field within 250ms'
        return replace(value, field_token=cap)
    def guarded_send(ready=None):
        foreground(hwnd)
        assert dom_ready(), 'Owned DOM focus missing at input boundary'
        actual = native.capture_target()
        assert actual and native.target_ready(actual) and actual.hwnd == hwnd
        assert not any(user.GetAsyncKeyState(key) & 0x8000 for key in (0x10,0x11,0x12,0x5B,0x5C,0x56)), 'Held key; no input sent'
        report['input_batches'] += 1
        raw_send(ready=ready)  # Actual single four-event SendInput; never replay.
    api.send_paste = guarded_send
    def trace():
        return js('window.trace')
    def current(field):
        return js('window.read(' + json.dumps(field) + ')').replace('\r\n', '\n').replace('\r', '\n')
    def clipboard_same():
        with __import__('paste_windows')._LOCK:
            assert backend._open(api), 'Clipboard unavailable for verification'
            try:
                return dict(api.snapshot()) == dict(original)
            finally:
                api.close()
    def admitted(field, captured=None, payload=PAYLOAD, selection=(START, END)):
        before_text = current(field)
        before_trace = trace()
        cap_target = captured or target()
        # The first input replaces the known synthetic selection. Subsequent
        # tests explicitly reset their own fixture selection before capture.
        expected = before_text[:selection[0]] + payload + before_text[selection[1]:]
        report['clipboard_mutated'] = True
        save('paste_' + field)
        try:
            result = delivery.paste_text(payload, cap_target, lambda api, dest: api.target_ready(dest))
            report['cases'].append(dict(field=field, kind='admitted', status=result['status'], reason=result['reason'],
                                        accepted_count=result.get('accepted_count'), clipboard_pending=result.get('clipboard_pending')))
            assert result['status'] == 'pasted' and result.get('accepted_count') == 4, 'Native multiline paste was not verified'
            assert not result.get('clipboard_pending') and not backend.pending_status()['pending']
            wait(lambda: current(field) == expected, 'Exact synthetic multiline/prefix/suffix readback failed')
            after = trace()
            assert after['pastes'] == before_trace['pastes'] + 1 and after['ctrlV'] == before_trace['ctrlV'] + 1
            assert after['enters'] == after['tabs'] == after['submits'] == 0
            assert clipboard_same(), 'Original clipboard map not restored exactly'
            report['cases'][-1].update(exact_field=True, one_paste=True, no_enter_tab_submit=True, clipboard_restored=True)
        finally:
            fields.release(cap_target.field_token)
        return expected
    def refused(kind, cap_target):
        before = trace()
        count = report['input_batches']
        sequence = api.sequence()
        result = delivery.paste_text(PAYLOAD, cap_target, lambda api, dest: api.target_ready(dest))
        assert result['status'] == 'retained' and report['input_batches'] == count
        assert trace() == before and api.sequence() == sequence, 'Refused case changed clipboard or injected input'
        report['cases'].append(dict(kind=kind, status=result['status'], no_input=True, no_clipboard_mutation=True))

    def drive():
        nonlocal hwnd, other_hwnd, original
        try:
            save('native_startup')
            wait(lambda: bool(user.FindWindowW(None, title)), 'Owned root HWND missing', timeout=12)
            hwnd = int(user.FindWindowW(None, title));own(hwnd)
            owned_thread = user.GetWindowThreadProcessId(hwnd, None)
            foreground_thread = user.GetWindowThreadProcessId(user.GetForegroundWindow(), None)
            attached = bool(owned_thread != foreground_thread and user.AttachThreadInput(owned_thread, foreground_thread, True))
            try:
                user.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    assert user.AttachThreadInput(owned_thread, foreground_thread, False)
            foreground(hwnd)
            report['owned_hwnd'] = hwnd
            assert fields.start(timeout=2), 'Read-only helper startup failed'
            report['helper_warmed'] = True
            # Snapshot every supported original format before any clipboard
            # mutation. Unsupported native handles bail out at this boundary.
            with __import__('paste_windows')._LOCK:
                assert backend._open(api), 'Cannot snapshot clipboard before mutation'
                try:
                    original = api.snapshot()
                finally:
                    api.close()
            report['original_clipboard_snapshot_complete'] = True
            save('clipboard_snapshot_ready')
            for index, field in enumerate(('plain', 'rich')):
                configure(field)
                first = admitted(field, payload=payloads[index * 2] if payloads else PAYLOAD)
                report['cases'][-1]['consecutive_index'] = 1
                # Preserve the complete first result for delivery two. The
                # synthetic caret is before its list, at an ASCII-only offset.
                configure(field, first, START, START)
                admitted(field, payload=(payloads[index * 2 + 1] if payloads else PAYLOAD) + '\n', selection=(START, START))
                report['cases'][-1].update(consecutive_index=2, first_result_preserved=True)
            configure('rich')
            cap_target = target()
            try:
                configure('plain')
                refused('same_window_other_field', cap_target)
                configure('rich')
                admitted('rich', captured=cap_target, payload=payloads[4] if payloads else PAYLOAD)
            finally:
                fields.release(cap_target.field_token)
            if payloads:
                configure('plain')
                admitted('plain', payload=payloads[5])
                report['cases'][-1]['spoken_numbered_groceries'] = True
            configure('plain')
            cap_target = target()
            try:
                valid[0] = False
                refused('cancelled_context', cap_target)
                valid[0] = True
                other.show()
                other_hwnd = int(user.FindWindowW(None, title + ' secondary'));own(other_hwnd)
                user.SetForegroundWindow(other_hwnd);foreground(other_hwnd)
                count, sequence = report['input_batches'], api.sequence()
                result = delivery.paste_text(PAYLOAD, cap_target, lambda api, dest: api.target_ready(dest))
                assert result['status'] == 'retained' and count == report['input_batches'] and sequence == api.sequence()
                report['cases'].append(dict(kind='wrong_window',status=result['status'],no_input=True,no_clipboard_mutation=True))
                # No further input/DOM operations follow. Cleanup does not need
                # to take foreground back from this owned secondary window.
            finally:
                fields.release(cap_target.field_token)
            assert clipboard_same() and report['input_batches'] == (6 if payloads else 5)
            report['status'] = 'passed'
        except Exception as error:
            report.update(status='failed', error=type(error).__name__ + ': ' + str(error), traceback=traceback.format_exc())
        finally:
            report['field_helper_closed'] = fields.close(timeout=2)
            report['field_helper_alive'] = fields.alive
            save('closing_owned_windows')
            other.destroy()
            window.destroy()

    storage = folder / 'webview-profile'
    webview.start(drive, gui='edgechromium', debug=False, private_mode=True, storage_path=str(storage))
    # Consumers are this helper's owned WebView descendants only. Their exit
    # proves a failed synthetic paste cannot remain queued when cleaning up.
    descendants = psutil.Process().children(recursive=True)
    _, remaining = psutil.wait_procs(descendants, timeout=5)
    for process in remaining:
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, remaining = psutil.wait_procs(remaining, timeout=3)
    report['consumer_processes_exited'] = not remaining
    report['owned_windows_closed'] = not user.IsWindow(hwnd) and not user.IsWindow(other_hwnd)
    if original is not None:
        if backend.pending_status()['pending'] and not remaining:
            # Controlled test consumer has exited; no paste can remain queued.
            backend.resolve_pending(confirm_no_pending_paste=True)
        same = clipboard_same()
        report['original_clipboard_restored'] = same
        if not same or backend.pending_status()['pending']:
            report.update(status='recovery_required', requires_recovery=True,
                          recovery_reason='Newer clipboard copy or restoration failure preserved; original held in helper RAM.')
            save('recovery_required')
            # Never terminate the RAM owner or silently discard its snapshot.
            # A visible recovery surface makes this exceptional failure concrete.
            import tkinter as tk
            recovery = tk.Tk();recovery.title('Owned paste test clipboard recovery')
            tk.Label(recovery, text='Test ended. Original clipboard remains in this helper RAM.\nA newer copy will not be overwritten. Coordinate recovery in Codex.', padx=20,pady=20).pack()
            recovery.protocol('WM_DELETE_WINDOW',lambda:None)
            def retry():
                backend.resolve_pending(confirm_no_pending_paste=True)
                if clipboard_same() and not backend.pending_status()['pending']:
                    report.update(original_clipboard_restored=True,requires_recovery=False,status='failed')
                    recovery.destroy()
            def discard():
                from tkinter import messagebox
                if messagebox.askyesno('Discard saved original clipboard?',
                    'Keep the current clipboard and permanently discard the previous clipboard held by this test?'):
                    backend.abandon_pending(True,True)
                    report.update(explicit_original_discarded=True,requires_recovery=False,status='failed')
                    recovery.destroy()
            tk.Button(recovery,text='Retry safe restoration',command=retry).pack(pady=8)
            tk.Button(recovery,text='Keep current clipboard; discard saved original',command=discard).pack(pady=8)
            recovery.mainloop()
    report['clipboard_owner_closed'] = backend.shutdown()
    report['field_helper_threads_closed'] = not any(thread.name.startswith('field-') for thread in threading.enumerate())
    save('complete')
    return 0 if (report['status']=='passed' and report['owned_windows_closed'] and report['clipboard_owner_closed']
                 and report['field_helper_closed'] and report['consumer_processes_exited']) else 1


def _wait_for_owned_child(process, owner, folder, destination, *, timeout=60,
                          clock=time.monotonic, sleep=time.sleep):
    """Watchdogs report and wait; they never destroy a possible RAM owner.

    Even a previously read pre-publication receipt cannot authorize terminate:
    the child could publish between that read and process termination.
    """
    owned = {}
    deadline = clock() + timeout
    attention = False
    latest = {}
    while process.poll() is None:
        try:
            for descendant in owner.children(recursive=True):
                try:
                    owned[(descendant.pid, descendant.create_time())] = descendant
                except Exception:
                    pass  # A descendant may have exited during enumeration.
        except Exception:
            pass  # Metadata failure never authorizes killing the RAM owner.
        result_file = folder / 'result.json'
        if result_file.exists():
            try:
                candidate = json.loads(result_file.read_text(encoding='utf-8'))
                if isinstance(candidate, dict):
                    latest = candidate
            except (OSError, ValueError):
                pass
        expired = clock() >= deadline
        if latest.get('requires_recovery') or expired:
            if not attention:
                interim = dict(latest, status='recovery_required', requires_recovery=True,
                    helper_still_running=True, isolated_folder=str(folder),
                    parent_watchdog_attention=expired,
                    parent_recovery_reason=('Owned helper exceeded its watchdog; process and temporary context '
                        'remain alive because it may own an unrecovered original clipboard.' if expired else
                        'Owned helper requests recovery; process and temporary context remain alive.'))
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(json.dumps(interim, indent=2), encoding='utf-8')
                except OSError:
                    # A diagnostic write failure must not unwind TemporaryDirectory
                    # while its child may still own the original clipboard.
                    print('Recovery receipt could not be saved; owned context remains at ' + str(folder), flush=True)
                print('Recovery requires coordinator attention; owned helper and temporary context are preserved.', flush=True)
                attention = True
            if expired:
                deadline = clock() + timeout
        sleep(.1)
    return owned, attention


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real-windows',action='store_true')
    parser.add_argument('--child')
    parser.add_argument('--field-trials', action='store_true')
    parser.add_argument('--receipt',default='audit/2026-10-30/multiline-paste/native-01.json')
    args=parser.parse_args()
    if args.child:return child(args.child)
    if not args.real_windows:
        print('Skipped: --real-windows required; no clipboard/input/window access')
        return 0
    if os.name!='nt':raise SystemExit('Windows required')
    import psutil
    destination=Path(args.receipt).resolve()
    assert destination.parent==ROOT/'audit/2026-10-30/multiline-paste' and destination.name.startswith('native-')
    if destination.exists():raise SystemExit('Preserve existing receipt; choose another name')
    with tempfile.TemporaryDirectory(prefix='.qa-multiline-',dir=ROOT) as directory:
        folder=Path(directory)
        for name in SOURCES:shutil.copy2(ROOT/name,folder/name)
        if args.field_trials:
            (folder/'field-trials.flag').write_text('Synthetic owner-shared list patterns', encoding='utf-8')
        (folder/'tests').mkdir()
        harness=folder/'tests'/Path(__file__).name;shutil.copy2(__file__,harness)
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1',PYTHONIOENCODING='utf-8',TEMP=directory,TMP=directory,
                 PYTHONPATH=os.pathsep.join((str(folder),str(PACKAGES))))
        for key in tuple(env):
            if key.endswith('API_KEY'):env.pop(key)
        process=subprocess.Popen([sys.executable,str(harness),'--child',directory],cwd=folder,env=env,
            stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW)
        owned, attention = _wait_for_owned_child(process, psutil.Process(process.pid), folder, destination)
        output,_=process.communicate(timeout=8)
        _,alive=psutil.wait_procs(list(owned.values()),timeout=5)
        result_file=folder/'result.json'
        report=json.loads(result_file.read_text()) if result_file.exists() else dict(status='failed',error='No child receipt')
        report.update(exit_code=process.returncode,process_exited=process.poll() is not None,
            parent_watchdog_or_recovery_attention=attention,
            owned_child_pids=[pid for pid,_ in owned],owned_children_still_running=[p.pid for p in alive],
            all_owned_processes_exited=not alive,child_output=output[-2500:],
            source_sha256={name:hashlib.sha256((folder/name).read_bytes()).hexdigest() for name in SOURCES},
            harness_sha256=hashlib.sha256(harness.read_bytes()).hexdigest())
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report))
        return 0 if report['status']=='passed' and process.returncode==0 and not alive else 1


if __name__=='__main__':raise SystemExit(main())
