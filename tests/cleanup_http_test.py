"""Actual HTTP parser/deadline behavior with fake sockets; no network or devices."""
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cleanup_http as client


def exercise(wire, *, timeout=1.0, read_size=7, read_delay=0.0, send_delay=0.0,
             payload=None, cap=None, path=client.PATH):
    clock = SimpleNamespace(now=10.0)
    sockets = []

    class OfflineSocket(client._DeadlineSocket):
        def __init__(self, budget):
            # No socket.socket.__init__: no native socket is opened.
            self._budget = budget
            self._io_refs = 0
            self._closed = False
            self.data = bytearray(wire)
            self.sent = bytearray()
            self.timeouts = []
            self.closed_reads = 0
            sockets.append(self)

        def settimeout(self, value):
            self.timeouts.append(value)

        def setsockopt(self, *args):
            pass

        def connect(self, address):
            assert address == (client.HOST, client.PORT)

        def _real_close(self):
            pass

    def receive(native, buffer, nbytes=0, flags=0):
        assert not native._closed or native._io_refs > 0, 'Closed response lost its file owner'
        native.closed_reads += int(native._closed)
        clock.now += read_delay
        count = min(len(buffer), nbytes or len(buffer), read_size, len(native.data))
        buffer[:count] = native.data[:count]
        del native.data[:count]
        return count

    def send(native, data, flags=0):
        clock.now += send_delay
        native.sent.extend(data)

    outcome = None
    with patch.object(client, '_DeadlineSocket', OfflineSocket), \
         patch.object(socket.socket, 'recv_into', receive), \
         patch.object(socket.socket, 'sendall', send), \
         patch.object(socket, 'getaddrinfo', side_effect=AssertionError('DNS forbidden')), \
         patch.object(client, 'MAX_JSON_BYTES', cap or client.MAX_JSON_BYTES):
        try:
            outcome = client._post_json(payload if payload is not None else {'model': 'unchanged',
                'messages': [{'role': 'user', 'content': 'Synthetic only — café'}]}, timeout,
                clock=lambda: clock.now, path=path)
        except Exception as error:
            outcome = error
    for native in sockets:
        assert native._closed and native._io_refs == 0, 'Request leaked a socket/file owner'
        assert all(0 < value <= timeout for value in native.timeouts)
    return outcome, sockets, clock


def response(body, *, version='1.1', headers=None, status='200 OK'):
    rows = [f'HTTP/{version} {status}', 'Content-Type: application/json']
    rows += headers if headers is not None else [f'Content-Length: {len(body)}']
    return ('\r\n'.join(rows) + '\r\n\r\n').encode() + body


body = json.dumps({'done_reason': 'stop', 'message': {'content': 'Exact café'}}).encode()
for version, extra in [('1.0', None), ('1.1', ['Connection: close', f'Content-Length: {len(body)}']),
                       ('1.1', None), ('1.0', [])]:
    result, natives, _ = exercise(response(body, version=version, headers=extra))
    assert result == json.loads(body)
    request = bytes(natives[0].sent)
    assert request.startswith(b'POST /api/chat HTTP/1.1\r\n')
    assert b'Accept-Encoding: identity\r\n' in request
    assert json.loads(request.split(b'\r\n\r\n', 1)[1])['model'] == 'unchanged'
    if version == '1.0' or extra and 'Connection: close' in extra:
        assert natives[0].closed_reads, 'Must cover body reads after connection releases socket'

chunked = b'5\r\n{"a":\r\n2\r\n1}\r\n0\r\n\r\n'
result, _, _ = exercise(response(chunked, headers=['Transfer-Encoding: chunked']))
assert result == {'a': 1}

for wire in (response(body), response(chunked, headers=['Transfer-Encoding: chunked'])):
    result, natives, clock = exercise(wire, timeout=.1, read_size=1, read_delay=.04)
    assert isinstance(result, client.CleanupTimeoutError), type(result)
    assert .1 <= clock.now - 10 < .15
    assert natives[0].timeouts[-1] < natives[0].timeouts[0], 'Trickle reset the total deadline'
result, _, _ = exercise(response(body), timeout=.1, send_delay=.2)
assert isinstance(result, client.CleanupTimeoutError)

for wire, reason in [
    (response(b'{}', status='302 Found'), 'http_status'),
    (response(b'{}', status='500 Error'), 'http_status'),
    (response(b'not-json'), 'invalid_response'),
    (response(b'[]'), 'invalid_response'),
    (response(b'{"x":NaN}'), 'invalid_response'),
    (response(b'\xff'), 'invalid_response'),
    (response(b'{}', headers=['Content-Length: 15']), 'incomplete_response'),
    (response(b'{}', headers=['Content-Encoding: gzip', 'Content-Length: 2']), 'unsupported_encoding'),
    (response(b'{}', headers=['Content-Length: 999999999']), 'response_too_large'),
    (response(b'4\r\n{}', headers=['Transfer-Encoding: chunked']), 'transport_failure'),
    (response(b'not-hex\r\n', headers=['Transfer-Encoding: chunked']), 'transport_failure'),
]:
    result, _, _ = exercise(wire)
    assert isinstance(result, client.CleanupHTTPError) and result.reason == reason, (type(result), result)

result, _, _ = exercise(response(b'{' + b' ' * 160 + b'}', version='1.0', headers=[]),
                         payload={}, cap=128)
assert isinstance(result, client.CleanupHTTPError) and result.reason == 'response_too_large'
result, natives, _ = exercise(b'', payload={'text': 'private sentinel ' * 100}, cap=128)
assert isinstance(result, client.CleanupHTTPError) and result.reason == 'request_too_large'
assert not natives and 'private sentinel' not in str(result)
for payload in ([], {'invalid': float('nan')}, {'invalid': '\ud800'}):
    result, natives, _ = exercise(b'', payload=payload)
    assert isinstance(result, client.CleanupHTTPError) and result.reason == 'invalid_request'
    assert not natives

for timeout in (0, -1, float('inf'), float('nan'), True, '15', 10 ** 1000):
    result, natives, _ = exercise(b'', timeout=timeout)
    assert isinstance(result, ValueError) and not natives

with patch.object(client, '_post_json', return_value={}) as post:
    assert client.post_json({'fixed': True}) == {}
    assert post.call_args.args == ({'fixed': True}, 15.0)
    assert not post.call_args.kwargs, 'Public API must not expose an alternative endpoint'
    assert client.warm_json({'warm': True}, timeout=3) == {}
    assert post.call_args.args == ({'warm': True}, 3)
    assert post.call_args.kwargs == {'path': '/api/generate'}

result, natives, _ = exercise(response(b'{}'), path=client.WARM_PATH)
assert result == {} and bytes(natives[0].sent).startswith(b'POST /api/generate HTTP/1.1\r\n')
for path in ('http://remote.invalid/api/chat', '/unexpected'):
    result, natives, _ = exercise(b'', path=path)
    assert isinstance(result, client.CleanupHTTPError) and not natives

print('Cleanup HTTP: total deadline, fixed local route, real HTTP parser, close ownership, '
      'caps, invalid responses and content-free failures passed; fake sockets only')
