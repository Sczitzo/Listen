"""Deadline-bounded JSON requests to the existing local cleanup endpoint."""
import http.client
import json
import math
import socket
import time
import threading


HOST = "127.0.0.1"
PORT = 11434
PATH = "/api/chat"
WARM_PATH = "/api/generate"
MAX_JSON_BYTES = 4 * 1024 * 1024
NUM_BATCH = 128  # Keep prompt-batch memory bounded without shrinking the context window.


class CleanupHTTPError(RuntimeError):
    """Content-free transport/protocol failure; never includes supplied text."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__("Local cleanup request failed: " + reason)


class CleanupTimeoutError(TimeoutError):
    def __init__(self):
        super().__init__("Local cleanup exceeded its time budget.")


class _Budget:
    def __init__(self, timeout, clock):
        if type(timeout) not in (int, float):
            raise ValueError("Cleanup timeout must be finite and positive.")
        try:
            valid = math.isfinite(timeout) and timeout > 0
        except OverflowError:
            valid = False
        if not valid:
            raise ValueError("Cleanup timeout must be finite and positive.")
        self.clock = clock
        self.deadline = clock() + timeout

    def remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise CleanupTimeoutError()
        return remaining


class _DeadlineSocket(socket.socket):
    """Keep socket.makefile's native lifetime rules and bound every raw read."""

    def __init__(self, budget):
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self._budget = budget

    def recv_into(self, buffer, nbytes=0, flags=0):
        self.settimeout(self._budget.remaining())
        count = super().recv_into(buffer, nbytes, flags)
        self._budget.remaining()
        return count

    def sendall(self, data, flags=0):
        self.settimeout(self._budget.remaining())
        super().sendall(data, flags)
        self._budget.remaining()


class _DeadlineConnection(http.client.HTTPConnection):
    def __init__(self, port, budget):
        super().__init__(HOST, port, timeout=budget.remaining())
        self._budget = budget

    def connect(self):
        # A numeric IPv4 socket avoids both DNS and environment proxy routing.
        native = _DeadlineSocket(self._budget)
        self.sock = native
        try:
            native.settimeout(self._budget.remaining())
            native.connect((HOST, self.port))
            self._budget.remaining()
            native.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except BaseException:
            native.close()
            self.sock = None
            raise


def _reject_constant(value):
    raise ValueError("Non-finite JSON value")


def _post_json(payload, timeout, *, port=PORT, clock=time.monotonic, path=PATH):
    """Private localhost-port seam for isolated HTTP fixtures; never a URL."""
    budget = _Budget(timeout, clock)
    connection = response = None
    try:
        if path not in (PATH, WARM_PATH) or type(payload) is not dict:
            raise CleanupHTTPError("invalid_request")
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
            raise CleanupHTTPError("invalid_request") from None
        budget.remaining()
        if len(body) > MAX_JSON_BYTES:
            raise CleanupHTTPError("request_too_large")
        connection = _DeadlineConnection(port, budget)
        connection.request("POST", path, body=body, headers={
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Connection": "close",
        })
        budget.remaining()
        response = connection.getresponse()
        budget.remaining()
        if not 200 <= response.status < 300:
            raise CleanupHTTPError("http_status")
        if response.getheader("Content-Encoding", "identity").strip().lower() not in ("", "identity"):
            raise CleanupHTTPError("unsupported_encoding")
        expected = response.length
        if expected is not None and expected > MAX_JSON_BYTES:
            raise CleanupHTTPError("response_too_large")
        encoded = response.read(MAX_JSON_BYTES + 1)
        budget.remaining()
        if len(encoded) > MAX_JSON_BYTES:
            raise CleanupHTTPError("response_too_large")
        if expected is not None and len(encoded) != expected:
            raise CleanupHTTPError("incomplete_response")
        try:
            result = json.loads(encoded.decode("utf-8"), parse_constant=_reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise CleanupHTTPError("invalid_response") from None
        budget.remaining()
        if type(result) is not dict:
            raise CleanupHTTPError("invalid_response")
        return result
    except CleanupHTTPError:
        raise
    except TimeoutError:
        raise CleanupTimeoutError() from None
    except (OSError, http.client.HTTPException):
        raise CleanupHTTPError("transport_failure") from None
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            if connection is not None:
                connection.close()


def post_json(payload, timeout=15.0):
    """POST one JSON object locally; no redirects, proxy, retries or threads."""
    return _post_json(payload, timeout)


def warm_json(payload, timeout=15.0):
    """Warm the selected local model through the same bounded transport."""
    return _post_json(payload, timeout, path=WARM_PATH)


class CleanupCancelledError(CleanupHTTPError):
    def __init__(self):
        super().__init__('cancelled')


class Cancellation:
    """Own the raw socket even after HTTPConnection releases its reference."""
    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._publication_lock = threading.RLock()
        self._socket = None

    @property
    def cancelled(self):
        return self._event.is_set()

    def cancel(self):
        # Serialize the cancellation boundary with draft publication, without
        # holding the socket lock during an application callback.
        with self._publication_lock:
            self._event.set()
        with self._lock:
            native = self._socket
        if native is not None:
            try:
                native.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _attach(self, native):
        with self._lock:
            self._socket = native
        if self.cancelled:
            self.cancel()

    def _detach(self, native):
        with self._lock:
            if self._socket is native:
                self._socket = None

    def check(self):
        if self.cancelled:
            raise CleanupCancelledError()


class _CancellableSocket(_DeadlineSocket):
    def __init__(self, budget, cancel):
        super().__init__(budget)
        self._cancel = cancel

    def recv_into(self, buffer, nbytes=0, flags=0):
        # Catch polling timeouts below SocketIO: a timeout escaping into the
        # buffered HTTP reader permanently poisons that reader.
        while True:
            self._cancel.check()
            self.settimeout(min(.1, self._budget.remaining()))
            try:
                count = socket.socket.recv_into(self, buffer, nbytes, flags)
            except socket.timeout:
                continue
            self._cancel.check()
            self._budget.remaining()
            return count


class _CancellableConnection(_DeadlineConnection):
    def __init__(self, port, budget, cancel):
        super().__init__(port, budget)
        self._cancel = cancel
        self.native = None

    def connect(self):
        self._cancel.check()
        native = _CancellableSocket(self._budget, self._cancel)
        self.native = self.sock = native
        self._cancel._attach(native)
        native.settimeout(self._budget.remaining())
        native.connect((HOST, self.port))
        self._cancel.check()
        self._budget.remaining()


def _stream_json(payload, timeout=5.0, *, cancel=None, port=PORT):
    budget = _Budget(timeout, time.monotonic)
    cancel = cancel if cancel is not None else Cancellation()
    connection = response = None
    try:
        cancel.check()
        if type(payload) is not dict:
            raise CleanupHTTPError('invalid_request')
        try:
            body = json.dumps({**payload, 'stream': True}, ensure_ascii=False,
                              allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
            raise CleanupHTTPError('invalid_request') from None
        if len(body) > MAX_JSON_BYTES:
            raise CleanupHTTPError('request_too_large')
        connection = _CancellableConnection(port, budget, cancel)
        connection.request('POST', PATH, body=body, headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Accept-Encoding': 'identity', 'Connection': 'close'})
        response = connection.getresponse()
        cancel.check()
        budget.remaining()
        if not 200 <= response.status < 300:
            raise CleanupHTTPError('http_status')
        if response.getheader('Content-Encoding', 'identity').strip().lower() not in ('', 'identity'):
            raise CleanupHTTPError('unsupported_encoding')
        expected = response.length
        if expected is not None and expected > MAX_JSON_BYTES:
            raise CleanupHTTPError('response_too_large')
        total, pieces, terminal = 0, [], None
        while True:
            encoded = response.readline(MAX_JSON_BYTES - total + 1)
            cancel.check()
            budget.remaining()
            if not encoded:
                break
            total += len(encoded)
            if total > MAX_JSON_BYTES:
                raise CleanupHTTPError('response_too_large')
            try:
                row = json.loads(encoded.decode('utf-8'), parse_constant=_reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise CleanupHTTPError('invalid_response') from None
            if (type(row) is not dict or terminal is not None or
                    type(row.get('done')) is not bool or
                    type(row.get('message')) is not dict or
                    type(row['message'].get('content')) is not str or row.get('error')):
                raise CleanupHTTPError('invalid_response')
            pieces.append(row['message']['content'])
            if row['done']:
                if row.get('done_reason') != 'stop':
                    raise CleanupHTTPError('incomplete_output')
                terminal = row
        if expected is not None and total != expected:
            raise CleanupHTTPError('incomplete_response')
        if terminal is None:
            raise CleanupHTTPError('missing_terminal')
        return {**terminal, 'message': {**terminal['message'], 'content': ''.join(pieces)}}
    except CleanupHTTPError:
        raise
    except TimeoutError:
        cancel.check()
        raise CleanupTimeoutError() from None
    except (OSError, http.client.HTTPException):
        cancel.check()
        raise CleanupHTTPError('transport_failure') from None
    finally:
        try:
            if response is not None:
                response.close()
        finally:
            if connection is not None:
                connection.close()
                if connection.native is not None:
                    cancel._detach(connection.native)


def stream_json(payload, *, timeout=5.0, cancel=None):
    """Return only a complete validated stream; cancellation is client-side only."""
    return _stream_json(payload, timeout, cancel=cancel)
