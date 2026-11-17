"""Non-native watchdog checks: fake process owns RAM until its safe exit."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

path = Path(__file__).with_name('multiline_delivery_windows_test.py')
spec = importlib.util.spec_from_file_location('owned_multiline_harness', path)
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class Process:
    def __init__(self):
        self.exited = threading.Event()
        self.terminate_calls = self.communicate_calls = 0
    def poll(self):
        return 0 if self.exited.is_set() else None
    def terminate(self):
        self.terminate_calls += 1
        raise AssertionError('Watchdog terminated possible original-clipboard RAM owner')
    def communicate(self, **kwargs):
        assert self.exited.is_set(), 'Parent advanced to cleanup while RAM owner remained alive'
        self.communicate_calls += 1
        return '', None


class Owner:
    def children(self, recursive):
        assert recursive
        return []


def preserves_context(initial):
    process = Process()
    entered, attention, context_closed = threading.Event(), threading.Event(), threading.Event()
    state, errors = {}, []
    fake_now = [0.]
    def clock():
        return fake_now[0]
    def sleep(delay):
        fake_now[0] += delay
        if state['destination'].exists():
            attention.set()
        time.sleep(.001)
    def parent():
        try:
            with tempfile.TemporaryDirectory(prefix='fake-owned-multiline-') as directory:
                folder = Path(directory)
                destination = folder / 'watchdog-receipt.json'
                state.update(folder=folder, destination=destination)
                (folder / 'result.json').write_text(json.dumps(initial), encoding='utf-8')
                entered.set()
                state['result'] = harness._wait_for_owned_child(process, Owner(), folder, destination,
                    timeout=.3, clock=clock, sleep=sleep)
                assert process.exited.is_set() and folder.exists()
                process.communicate(timeout=8)
            context_closed.set()
        except BaseException as error:
            errors.append(error)
            entered.set();attention.set()
    thread = threading.Thread(target=parent, name='fake-native-watchdog-parent')
    thread.start()
    try:
        assert entered.wait(2) and attention.wait(2)
        assert not errors, errors
        assert thread.is_alive() and process.poll() is None
        assert state['folder'].exists() and not context_closed.is_set()
        receipt = json.loads(state['destination'].read_text(encoding='utf-8'))
        assert receipt['status'] == 'recovery_required' and receipt['requires_recovery']
        assert receipt['helper_still_running'] and receipt['isolated_folder'] == str(state['folder'])
        # Advance through several more watchdog periods. Neither the process nor
        # its temporary browser/receipt context may be cleaned up on timeout.
        first = fake_now[0]
        end = time.monotonic() + 2
        while fake_now[0] < first + 1.2:
            assert time.monotonic() < end
            time.sleep(.002)
        assert process.terminate_calls == process.communicate_calls == 0
        assert state['folder'].exists() and thread.is_alive()
        process.exited.set()
        thread.join(2)
        assert not thread.is_alive() and not errors, errors
        assert context_closed.is_set() and not state['folder'].exists()
        assert process.terminate_calls == 0 and process.communicate_calls == 1
        assert state['result'] == ({}, True)
    finally:
        process.exited.set()
        thread.join(2)
        assert not thread.is_alive()


preserves_context({'status': 'running', 'clipboard_mutated': True})
preserves_context({'status': 'recovery_required', 'clipboard_mutated': True, 'requires_recovery': True})
# A stale pre-publication observation cannot authorize terminate: publication
# could occur between reading its receipt and issuing the termination request.
preserves_context({'status': 'running', 'clipboard_mutated': False})
with patch.object(sys, 'argv', [str(path)]):
    assert harness.main() == 0
print('PASS native harness watchdog: publication/recovery/stale-prepublication timeouts preserve process+context until safe exit; default skip remains inert')
