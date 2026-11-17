"""One verified final clipboard paste; never restores on a timer or replays input."""
import threading
import time
import unicodedata
import uuid

import paste_windows


def valid_multiline(text):
    if not isinstance(text, str) or not text or '\n' not in text:
        return False
    if len(text) > 100_000:
        return False
    try:
        if len(text.encode('utf-16-le')) // 2 > 100_000:
            return False
    except UnicodeEncodeError:
        return False
    cleaned = text.replace('\r\n', '\n')
    return not any((unicodedata.category(c) == 'Cc' and c != '\n') or c in '\u2028\u2029'
                   for c in cleaned)


class VerifiedPasteBackend:
    def __init__(self, native=None, *, sleep=time.sleep, clock=time.monotonic):
        self.native, self.sleep, self.clock = native, sleep, clock
        self._lock = threading.Lock()
        self._active = self._pending = self._manual = None
        self.last_delivery_metadata = {}

    def _api(self):
        if self.native is None:
            self.native = paste_windows.WindowsNative()
        return self.native

    def pending_status(self, include_manual=False):
        # Called under application locks by Quit. No UIA, native I/O or waiting.
        with self._lock:
            pending, active = self._pending, self._active
            if include_manual and not (pending or active) and self._manual:
                return {'pending': True, 'manual_id': self._manual['id'],
                        'message': 'Your previous clipboard item is available to restore.'}
            return {'pending': pending is not None or active is not None,
                    'message': ('Formatted paste is in progress.' if active else
                                'Previous clipboard is held safely in memory.' if pending else '')}

    def _open(self, api):
        for _ in range(5):
            if api.open():
                return True
            self.sleep(.02)
        return False

    def clipboard_owned(self, transaction):
        sequence = transaction['sequence']
        return type(sequence) is int and sequence > 0 and self._api().sequence() == sequence

    def _publish(self, tx, text, *, expected=None, commit_lock=None, check=None):
        api = self._api()
        with paste_windows._LOCK:
            if not self._open(api):
                raise OSError('clipboard_busy')
            prepared = rollback = None
            try:
                if expected is not None and api.sequence() != expected:
                    return
                tx['saved'] = api.snapshot()
                tx['before'] = api.sequence()
                with self._lock:
                    tx['replaces_manual'] = bool(self._manual and self._manual['sequence'] == tx['before']
                                                 and api.owns_clipboard())
                prepared = api.prepare(api.payload(text))
                if expected is not None:
                    rollback = api.prepare(tx['saved'])
                if commit_lock is not None and not commit_lock.acquire(blocking=False):
                    return  # A workspace action may already be waiting to copy.
                try:
                    if expected is not None and (api.sequence() != expected or not check()):
                        return
                    tx['changed'] = True
                    try:
                        api.replace(prepared)
                    except Exception:
                        if rollback is not None:
                            try:
                                api.replace(rollback)
                                tx['changed'] = dict(api.snapshot()) != dict(tx['saved'])
                            except Exception:
                                pass  # Keep the original snapshot for recovery.
                        raise
                    finally:
                        # Record ownership while the clipboard is still open.
                        tx['sequence'] = api.sequence()
                finally:
                    if commit_lock is not None:
                        commit_lock.release()
            finally:
                if prepared is not None:
                    api.release(prepared)
                if rollback is not None:
                    api.release(rollback)
                api.close()

    def _restore(self, tx, *, manual=False):
        api = self._api()
        with paste_windows._LOCK:
            if not self._open(api):
                return False
            prepared = None
            try:
                owned = self.clipboard_owned(tx) and (not manual or api.owns_clipboard())
                if not owned and not (manual and api.sequence() and not api.snapshot()):
                    return False  # Preserve a newer user copy.
                prepared = api.prepare(tx['saved'])
                try:
                    api.replace(prepared)
                finally:
                    tx['sequence'] = api.sequence()
                restored = dict(api.snapshot()) == dict(tx['saved'])
                if restored and not manual and tx.get('replaces_manual') and tx['sequence']:
                    with self._lock:
                        if self._manual and self._manual['sequence'] == tx.get('before'):
                            self._manual['sequence'] = tx['sequence']
                return restored
            except Exception:
                return False
            finally:
                if prepared is not None:
                    api.release(prepared)
                api.close()

    @staticmethod
    def _result(status, reason, tx, pending=False):
        return {'status': status, 'reason': reason, 'accepted_count': tx.get('count', 0),
                'expected_count': 4, 'clipboard_pending': pending,
                'message': {
                    'paste_verified': 'Formatted text was pasted into the original field.',
                    'paste_verified_clipboard_pending': 'Text was pasted. Previous clipboard recovery is pending; do not paste this result again.',
                    'paste_unverified': 'Paste was attempted once but could not be verified. Check the original field before copying; clipboard recovery is available.',
                    'paste_not_sent': 'Paste was not sent. Your text is retained in Whisper.',
                    'clipboard_restore_pending': 'Paste was not sent. Previous clipboard recovery is pending.',
                    'prior_delivery_pending': 'Earlier clipboard recovery is pending. Your new text is retained.',
                    'invalid_multiline': 'This result cannot be pasted automatically. Your exact text is retained.',
                }.get(reason, 'Automatic paste is unavailable; your text is retained.')}

    def _finish(self, tx):
        try:
            restored = not tx['changed'] or self._restore(tx)
        except Exception:
            restored = False
        pending = tx['changed'] and (not restored)
        with self._lock:
            if self._active is not tx:
                raise RuntimeError('Transaction ownership lost')
            self._active = None
            self._pending = tx if pending else None
        if not pending:
            try:
                tx['complete']()
            except Exception:
                pass  # Token cleanup never changes a verified delivery outcome.
        return pending

    def copy_unsent(self, text, sequence, *, commit_lock, check):
        if not text or len(text) > 100_000 or '\0' in text or type(sequence) is not int or sequence <= 0:
            return False
        tx = dict(id=uuid.uuid4().hex, changed=False, potential=False, verified=False,
                  sequence=None, complete=lambda: None, verify=lambda _: False)
        with self._lock:
            if self._active or self._pending or self._manual:
                return False
            self._active = tx
        try:
            self._publish(tx, text, expected=sequence, commit_lock=commit_lock, check=check)
            if not tx['changed'] or not tx['sequence']:
                self._finish(tx)
                return False
            owned = self.clipboard_owned(tx) and self._api().owns_clipboard()
        except Exception:
            self._finish(tx)
            return False
        with self._lock:
            self._manual = tx if tx['saved'] else None
            self._active = None
        return owned

    def resolve_manual(self, receipt, *, keep_current=False):
        with self._lock:
            tx = self._manual
            if self._active or self._pending or not tx or tx['id'] != receipt:
                return {'status': 'uncertain', 'message': 'Clipboard recovery changed. Review it again.'}
            if keep_current:
                self._manual = None
                return {'status': 'resolved', 'message': 'Current clipboard kept.'}
            self._active = tx
        try:
            restored = self._restore(tx, manual=True)
        except Exception:
            restored = False
        with self._lock:
            if restored:
                self._manual = None
            self._active = None
        return {'status': 'restored' if restored else 'uncertain', 'message': (
            'Previous clipboard restored.' if restored else
            'Your previous item is still saved. If you copied something else, save it elsewhere and clear the clipboard before restoring.')}

    def paste_text(self, text, target=None, *, check, verify, send, complete):
        empty = {'count': 0}
        if not valid_multiline(text):
            complete()
            return self._result('retained', 'invalid_multiline', empty)
        tx = dict(id=uuid.uuid4().hex, saved=None, sequence=None, changed=False,
                  potential=False, verified=False, count=0, verify=verify, complete=complete)
        with self._lock:
            if self._active is not None or self._pending is not None:
                blocked = True
            else:
                self._active, blocked = tx, False
        if blocked:
            complete()
            return self._result('retained', 'prior_delivery_pending', empty, True)
        try:
            if check():
                self._publish(tx, text)
                if check() and self.clipboard_owned(tx):
                    # send() performs only final native/context/sequence checks
                    # after allocating its four events, never UIA under locks.
                    tx['potential'] = True
                    try:
                        tx['count'] = send(lambda: self.clipboard_owned(tx))
                        if tx['count'] == 0:
                            tx['potential'] = False
                    except paste_windows.PasteNotSent:
                        tx['potential'], tx['count'] = False, 0
                    except Exception:
                        tx['count'] = getattr(self._api(), 'last_send_count', None)
                    if tx['potential']:
                        until = self.clock() + 1.5
                        while True:
                            remaining = until - self.clock()
                            if remaining <= 0:
                                break
                            try:
                                tx['verified'] = bool(verify(min(.2, remaining)))
                            except Exception:
                                tx['verified'] = False
                            if tx['verified'] or self.clock() >= until:
                                break
                            self.sleep(min(.025, max(0., until - self.clock())))
        except Exception:
            pass  # No exception text or field/clipboard content is logged.
        if tx['potential'] and not tx['verified']:
            with self._lock:
                self._active, self._pending = None, tx
            pending = True  # A queued paste must never see the previous clipboard.
        else:
            pending = self._finish(tx)
        if tx['verified']:
            result = self._result('pasted', 'paste_verified_clipboard_pending' if pending else 'paste_verified', tx, pending)
        elif tx['potential']:
            result = self._result('uncertain', 'paste_unverified', tx, pending)
        else:
            result = self._result('retained', 'clipboard_restore_pending' if pending else 'paste_not_sent', tx, pending)
            if tx['changed'] and not pending:
                result.update(clipboard_sequence_before=tx['before'], clipboard_sequence_after=tx['sequence'])
        self.last_delivery_metadata = {key: result[key] for key in
                                      ('status', 'reason', 'accepted_count', 'expected_count', 'clipboard_pending')}
        return result

    def poll_pending(self):
        # Explicit worker-side polling, never called by metadata-only status.
        with self._lock:
            tx = self._pending
        if tx is not None and not tx['verified']:
            try:
                verified = bool(tx['verify'](.2))
            except Exception:
                verified = False
            with self._lock:
                if self._pending is tx:
                    tx['verified'] = tx['verified'] or verified
        if tx is not None and tx['verified']:
            return self.resolve_pending()
        return None

    def resolve_pending(self, confirm_no_pending_paste=False):
        with self._lock:
            tx = self._pending
            if self._active is not None:
                return {'status': 'uncertain', 'message': 'Formatted paste is still in progress.'}
            if tx is None:
                return {'status': 'retained', 'message': 'No clipboard recovery is pending.'}
            if tx['potential'] and not tx['verified'] and not confirm_no_pending_paste:
                return {'status': 'uncertain', 'message': 'Paste has not been verified; clipboard recovery remains pending.'}
            self._pending, self._active = None, tx
        pending = self._finish(tx)
        return {'status': 'uncertain' if pending else 'restored',
                'message': 'Previous clipboard remains in memory; a newer copy or restore failure was preserved.' if pending else 'Previous clipboard restored.'}

    def abandon_pending(self, confirm_no_pending_paste=False, confirm_discard_original=False):
        if not confirm_no_pending_paste or not confirm_discard_original:
            return {'status': 'uncertain', 'message': 'Confirm the paste has finished and discarding the saved original clipboard first.'}
        with self._lock:
            if self._active is not None:
                return {'status': 'uncertain', 'message': 'Formatted paste is still in progress.'}
            tx, self._pending = self._pending, None
        if tx is not None:
            try:
                tx['complete']()
            except Exception:
                pass
        return {'status': 'resolved', 'message': 'Current clipboard preserved; saved original clipboard discarded.'}

    def shutdown(self):
        if self.pending_status(include_manual=True)['pending']:
            return False
        return self.native.shutdown() if self.native is not None else True


class MultilineDelivery:
    def __init__(self, fields, native_getter, context_valid, backend=None):
        self.fields, self.native_getter, self.context_valid = fields, native_getter, context_valid
        self.backend = backend if backend is not None else VerifiedPasteBackend()

    def paste_text(self, text, target, wait_ready):
        from text_input_windows import Target
        result = {'status': 'retained', 'reason': 'field_unavailable', 'accepted_count': 0,
                  'expected_count': 4, 'message': 'The original editable field could not be verified. Your text is saved in Whisper; copy it manually into the intended field.'}
        if not valid_multiline(text) or not isinstance(target, Target) or not target.field_token:
            return result
        self.backend.poll_pending()
        if self.backend.pending_status()['pending'] or paste_windows._backend.pending_status()['pending']:
            result.update(reason='prior_delivery_pending', message='Earlier clipboard recovery is pending. Your new text is saved in Whisper.')
            return result
        native = self.native_getter()
        if not self.context_valid() or not wait_ready(native, target):
            return result
        token = self.fields.prepare(target.field_token, text)
        if token is None:
            return result

        def check():
            return bool(self.context_valid() and native.target_ready(target) and self.fields.check(token))

        def send(owned):
            api = self.backend._api()
            api.last_send_count = 0
            api.send_paste(ready=lambda: self.context_valid() and native.target_ready(target) and owned())
            return api.last_send_count

        return self.backend.paste_text(text, target.hwnd, check=check,
            verify=lambda timeout: self.fields.verify(token, timeout=timeout), send=send,
            complete=lambda: self.fields.release(token))
