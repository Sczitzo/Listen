"""Private read-only UIA child. Field values and carets never leave its RAM."""
from dataclasses import dataclass
import copy
import os
from pathlib import Path
import re
import sys
import time
import unicodedata
import uuid

sys.dont_write_bytecode = True

MAX_UNITS = 524288
MAX_READ = MAX_UNITS + 1
MAX_TOKENS = 8


class Refused(Exception):
    def __init__(self, reason, status='unsupported'):
        self.reason, self.status = reason, status
        super().__init__(reason)


def _units(text):
    if not isinstance(text, str):
        raise Refused('field_unavailable')
    try:
        return len(text.encode('utf-16-le')) // 2
    except UnicodeError:
        raise Refused('field_unavailable') from None


def _normalize(text):
    if _units(text) > MAX_UNITS:
        raise Refused('field_too_large')
    return text.replace('\r\n', '\n').replace('\r', '\n')


def _intended(text):
    if not isinstance(text, str) or not text:
        raise Refused('invalid_text')
    try:
        if _units(text) > 100000:
            raise Refused('invalid_text')
        if '\r' in text.replace('\r\n', ''):
            raise Refused('invalid_text')
        normalized = text.replace('\r\n', '\n')
        if any(unicodedata.category(char) == 'Cc' and char != '\n' for char in normalized):
            raise Refused('invalid_text')
        return normalized
    except Refused:
        raise Refused('invalid_text') from None


@dataclass(frozen=True)
class Snapshot:
    document: str
    prefix: str
    selected: str
    suffix: str

    @classmethod
    def create(cls, document, prefix, selected, suffix):
        # Bound both the provider's original UTF16 reads and their normalized
        # reconstruction; never calculate caret offsets from Python indexes.
        if sum(_units(value) for value in (prefix, selected, suffix)) > MAX_UNITS:
            raise Refused('field_too_large')
        values = tuple(_normalize(value) for value in (document, prefix, selected, suffix))
        if values[0] != ''.join(values[1:]):
            raise Refused('invalid_selection')
        return cls(*values)


class UIAProvider:
    """Native/UIA reads only. Constructor is called after owned init input."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        import clr
        base = Path(os.environ.get('WINDIR', 'C:/Windows'))
        wpf = base / 'Microsoft.NET/Framework64/v4.0.30319/WPF'
        for name in ('WindowsBase', 'UIAutomationTypes', 'UIAutomationClient'):
            clr.AddReference(str(wpf / (name + '.dll')))
        from System import IntPtr
        from System.Windows.Automation import (Automation, AutomationElement, TextPattern,
                                               ValuePattern, TreeWalker)
        from System.Windows.Automation.Text import TextPatternRangeEndpoint
        self.ctypes, self.wintypes, self.IntPtr = ctypes, wintypes, IntPtr
        self.Automation, self.Element = Automation, AutomationElement
        self.Text, self.Value, self.Walker = TextPattern, ValuePattern, TreeWalker.ControlViewWalker
        self.Endpoint = TextPatternRangeEndpoint
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        class GuiInfo(ctypes.Structure):
            _fields_ = [('size', wintypes.DWORD), ('flags', wintypes.DWORD),
                        ('active', wintypes.HWND), ('focus', wintypes.HWND),
                        ('capture', wintypes.HWND), ('menu', wintypes.HWND),
                        ('move_size', wintypes.HWND), ('caret', wintypes.HWND),
                        ('caret_rect', wintypes.RECT)]
        self.GuiInfo = GuiInfo
        for name, args, result in (
            ('GetForegroundWindow', [], wintypes.HWND),
            ('IsWindow', [wintypes.HWND], wintypes.BOOL),
            ('IsChild', [wintypes.HWND, wintypes.HWND], wintypes.BOOL),
            ('GetWindowThreadProcessId', [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            ('GetGUIThreadInfo', [wintypes.DWORD, ctypes.POINTER(GuiInfo)], wintypes.BOOL),
            ('GetClassNameW', [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int], ctypes.c_int),
            ('GetWindowLongW', [wintypes.HWND, ctypes.c_int], wintypes.LONG),
        ):
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result

    def _native(self, target):
        if (not self.user.IsWindow(target['hwnd']) or
                int(self.user.GetForegroundWindow() or 0) != target['hwnd']):
            raise Refused('field_changed', 'stale')
        pid = self.wintypes.DWORD()
        thread = self.user.GetWindowThreadProcessId(target['hwnd'], self.ctypes.byref(pid))
        info = self.GuiInfo()
        info.size = self.ctypes.sizeof(info)
        if (pid.value != target['pid'] or not thread or
                not self.user.GetGUIThreadInfo(thread, self.ctypes.byref(info)) or
                info.flags & (0x0004 | 0x0008 | 0x0010) or
                int(info.focus or 0) != target['focus'] or
                not (target['focus'] == target['hwnd'] or self.user.IsChild(target['hwnd'], target['focus']))):
            raise Refused('field_changed', 'stale')

    def _focused(self, target):
        self._native(target)
        root = self.Element.FromHandle(self.IntPtr(target['hwnd']))
        element = self.Element.FocusedElement
        ancestor = element
        for _ in range(64):
            if ancestor is None:
                raise Refused('field_changed', 'stale')
            if self.Automation.Compare(ancestor, root):
                break
            ancestor = self.Walker.GetParent(ancestor)
        else:
            raise Refused('field_changed', 'stale')
        current = element.Current
        if current.IsPassword:
            raise Refused('password')
        if (not current.IsEnabled or current.IsOffscreen or
                current.ControlType.ProgrammaticName != 'ControlType.Edit'):
            raise Refused('unsupported_field')
        supported = element.GetSupportedPatterns()
        if self.Text.Pattern not in supported:
            raise Refused('unsupported_field')
        pattern = element.GetCurrentPattern(self.Text.Pattern)
        writable = False
        if self.Value.Pattern in supported:
            if element.GetCurrentPattern(self.Value.Pattern).Current.IsReadOnly:
                raise Refused('readonly')
            writable = True
        readonly = pattern.DocumentRange.GetAttributeValue(self.Text.IsReadOnlyAttribute)
        if str(readonly) == 'True':
            raise Refused('readonly')
        writable = writable or str(readonly) == 'False'
        if not writable:
            raise Refused('unsupported_field')
        # Native Edit exposes multiline/password capability without reading its
        # value. Browser renderer HWNDs do not provide native edit style bits.
        name = self.ctypes.create_unicode_buffer(128)
        self.user.GetClassNameW(target['focus'], name, len(name))
        if name.value.casefold() in ('edit', 'richedit20w', 'richedit20a', 'richedit50w', 'richedit'):
            style = self.user.GetWindowLongW(target['focus'], -16)
            if style & 0x20:
                raise Refused('password')
            if style & 0x800:
                raise Refused('readonly')
            if not style & 0x4:
                raise Refused('single_line')
        selections = pattern.GetSelection()
        if len(selections) != 1:
            raise Refused('invalid_selection')
        self._native(target)
        return element, pattern, selections[0]

    def capture(self, target):
        element, _, _ = self._focused(target)
        runtime = tuple(int(value) for value in element.GetRuntimeId())
        if not runtime:
            raise Refused('unsupported_field')
        return dict(target=dict(target), runtime=runtime)

    def read(self, identity):
        target = identity['target']
        element, pattern, selection = self._focused(target)
        if tuple(int(value) for value in element.GetRuntimeId()) != identity['runtime']:
            raise Refused('field_changed', 'stale')
        document = pattern.DocumentRange
        start, end = self.Endpoint.Start, self.Endpoint.End
        if (selection.CompareEndpoints(start, document, start) < 0 or
                selection.CompareEndpoints(end, document, end) > 0 or
                selection.CompareEndpoints(start, selection, end) > 0):
            raise Refused('invalid_selection')
        prefix, suffix = document.Clone(), document.Clone()
        prefix.MoveEndpointByRange(end, selection, start)
        suffix.MoveEndpointByRange(start, selection, end)
        result = Snapshot.create(document.GetText(MAX_READ), prefix.GetText(MAX_READ),
                                 selection.GetText(MAX_READ), suffix.GetText(MAX_READ))
        self._native(target)
        return result


class FieldRegistry:
    """Provider-independent RAM token ownership; used by pure fake-provider tests."""
    def __init__(self, provider, session, *, clock=time.monotonic):
        self.provider, self.session, self.clock = provider, session, clock
        self.tokens = {}

    def _prune(self):
        now = self.clock()
        self.tokens = {token: entry for token, entry in self.tokens.items()
                       if entry['kind'] != 'c' or now < entry['expires']}

    def _entry(self, token, kind):
        self._prune()
        if not isinstance(token, str):
            raise Refused('stale_token', 'stale')
        entry = self.tokens.get(token)
        if entry is None or entry['kind'] != kind:
            raise Refused('stale_token', 'stale')
        return entry

    def _new(self, kind, **values):
        self._prune()
        if len(self.tokens) >= MAX_TOKENS:
            raise Refused('token_limit')
        token = self.session + '.' + kind + '.' + uuid.uuid4().hex
        self.tokens[token] = dict(kind=kind, expires=self.clock() + 900., **values)
        return {'token': token}

    def capture(self, target):
        self._prune()
        if len(self.tokens) >= MAX_TOKENS:
            raise Refused('token_limit')
        if (not isinstance(target, dict) or set(target) != {'hwnd', 'pid', 'focus'} or
                any(type(value) is not int or value <= 0 for value in target.values())):
            raise Refused('invalid_target')
        return self._new('c', identity=self.provider.capture(target))

    def prepare(self, token, text):
        entry = self._entry(token, 'c')
        self._prune()
        if len(self.tokens) >= MAX_TOKENS:
            raise Refused('token_limit')
        intended = _intended(text)
        before = self.provider.read(entry['identity'])
        expected = before.prefix + intended + before.suffix
        if _units(expected) > MAX_UNITS:
            raise Refused('field_too_large')
        if expected == before.document:
            raise Refused('unchanged')
        # Independent transaction entry survives release of its capture token.
        return self._new('t', identity=copy.deepcopy(entry['identity']), before=before,
                         expected=expected, verified=False)

    def check(self, token):
        entry = self._entry(token, 't')
        return {'value': self.provider.read(entry['identity']) == entry['before']}

    def verify(self, token):
        entry = self._entry(token, 't')
        if entry['verified']:
            return {'value': True}
        current = self.provider.read(entry['identity'])
        value = current.document == entry['expected']
        if value:
            entry['verified'] = True
        return {'value': value}

    def release(self, token):
        if isinstance(token, str):
            self.tokens.pop(token, None)
        return {}


def _guard():
    def audit(event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.sendto'):
            raise PermissionError('Field helper is local only')
        if event == 'open' and not isinstance(args[0], int):
            if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                raise PermissionError('Field helper never writes files')
        if event in ('os.remove', 'os.rmdir', 'os.mkdir', 'os.rename'):
            raise PermissionError('Field helper never changes files')
    sys.addaudithook(audit)


def main():
    _guard()
    from field_access import _read_message, OPS
    from speech_process import _write_frame
    from speech_process_worker import _parent_lifetime
    wire = os.fdopen(os.dup(sys.stdout.fileno()), 'wb', buffering=0)
    # Native/library console output cannot contaminate framed stdout. No
    # exception payload or supplied field/dictation is printed to either pipe.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    incoming = sys.stdin.buffer
    session, last_id, registry = None, None, None
    while True:
        try:
            request = _read_message(incoming)
        except (EOFError, ValueError, UnicodeError):
            return 0
        op, nonce, request_id = request.get('op'), request.get('session'), request.get('request_id')
        valid = (isinstance(op, str) and op in OPS and
                 set(request) == {'op', 'session', 'request_id'} | OPS[op] and
                 isinstance(nonce, str) and re.fullmatch(r'[0-9a-f]{32}', nonce) and
                 type(request_id) is int and request_id > 0 and
                 (session is None or nonce == session) and
                 (last_id is None or request_id == last_id + 1))
        if not valid:
            return 2
        session, last_id = nonce, request_id
        response = dict(kind='response', session=session, request_id=request_id,
                        status='ok', reason='', data={})
        try:
            if registry is None:
                if op != 'init' or type(request['parent_pid']) is not int or request['parent_pid'] <= 0:
                    raise Refused('invalid_request', 'error')
                _parent_lifetime(request['parent_pid'])
                registry = FieldRegistry(UIAProvider(), session)
                response['data'] = {'ready': True}
            elif op == 'init':
                raise Refused('invalid_request', 'error')
            elif op == 'close':
                registry.tokens.clear()
            else:
                inputs = {key: request[key] for key in OPS[op]}
                response['data'] = getattr(registry, op)(**inputs)
        except Refused as error:
            response.update(status=error.status, reason=error.reason, data={})
        except Exception:
            response.update(status='error', reason='provider_failed', data={})
        try:
            _write_frame(wire, response)
        except (OSError, ValueError):
            return 2
        if op == 'close' or response['status'] == 'error':
            return 0


if __name__ == '__main__':
    raise SystemExit(main())
