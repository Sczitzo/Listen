"""Ephemeral live-preview scheduling; no model, app, storage or input calls."""
from dataclasses import dataclass
import math
import threading
from types import MappingProxyType


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True, slots=True)
class Ticket:
    epoch: int
    generation: int
    context: tuple
    sample_count: int
    interval: float


class PreviewState:
    def __init__(self, sample_rate=16000, min_seconds=2.0, new_seconds=1.0,
                 slow_seconds=2.0, max_chars=600):
        if type(sample_rate) is not int or sample_rate <= 0:
            raise ValueError('sample_rate must be a positive integer')
        if any(not _number(value) or value < 0 for value in
               (min_seconds, new_seconds, slow_seconds)):
            raise ValueError('Preview durations must be finite and nonnegative')
        if type(max_chars) is not int or max_chars <= 0:
            raise ValueError('max_chars must be a positive integer')
        self._min_samples = max(1, math.ceil(sample_rate * min_seconds))
        self._new_samples = max(1, math.ceil(sample_rate * new_seconds))
        self._slow_seconds = slow_seconds
        self._max_chars = max_chars
        self._lock = threading.Lock()
        self._work = threading.Event()
        self._ticket = None
        self._epoch = 0
        self._version = 0
        self._active = False
        self._generation = None
        self._context = ()
        self._last_samples = None
        self._next_due = 0.0
        self._state, self._text, self._reason = 'idle', '', ''
        self._publish()

    def _publish(self):
        self._snapshot = MappingProxyType({'active': self._active,
            'generation': self._generation, 'version': self._version,
            'state': self._state, 'text': self._text, 'reason': self._reason})

    def snapshot(self):
        """Copy one immutable publication without waiting for model work."""
        return dict(self._snapshot)

    def in_flight(self):
        return self._work.is_set()

    def begin(self, generation, context_tuple, *, now):
        if type(generation) is not int or generation < 0 or not _number(now):
            raise ValueError('Invalid preview generation or clock')
        if not isinstance(context_tuple, tuple):
            raise ValueError('Preview context must be an immutable tuple')
        try:
            hash(context_tuple)
        except TypeError:
            raise ValueError('Preview context must be immutable') from None
        with self._lock:
            self._epoch += 1
            self._version += 1
            self._active, self._generation, self._context = True, generation, context_tuple
            self._last_samples = None
            self._next_due = now
            self._state, self._text, self._reason = 'listening', '', ''
            # An old native inference still owns the work reservation. Its
            # visible epoch is invalid, but a second model call cannot start.
            self._publish()

    def end(self):
        with self._lock:
            self._epoch += 1
            self._version += 1
            self._active, self._generation, self._context = False, None, ()
            self._state, self._text, self._reason = 'idle', '', ''
            self._publish()

    def claim(self, generation, context, sample_count, *, now, interval=1.5):
        if (type(generation) is not int or not isinstance(context, tuple)
                or type(sample_count) is not int or sample_count < 0 or not _number(now)
                or not _number(interval) or interval < 0):
            return None
        with self._lock:
            if (not self._active or generation != self._generation or context != self._context
                    or self._ticket is not None or self._state == 'paused'
                    or sample_count < self._min_samples or now < self._next_due):
                return None
            if self._last_samples is not None and sample_count - self._last_samples < self._new_samples:
                return None
            ticket = Ticket(self._epoch, generation, context, sample_count, float(interval))
            self._ticket = ticket
            self._work.set()
            self._last_samples = sample_count
            self._state = 'updating'
            self._version += 1
            self._publish()
            return ticket

    def finish(self, ticket, text, *, now, elapsed, failed=False):
        with self._lock:
            if not isinstance(ticket, Ticket) or self._ticket is not ticket:
                return False
            self._ticket = None
            self._work.clear()
            if (not self._active or ticket.epoch != self._epoch
                    or ticket.generation != self._generation or ticket.context != self._context):
                return False
            invalid_result = not _number(now) or not _number(elapsed) or elapsed < 0 or not isinstance(text, str)
            if failed or invalid_result:
                self._state, self._reason = 'paused', 'unavailable'
            else:
                if text:
                    self._text = text[:self._max_chars]
                if elapsed > self._slow_seconds:
                    self._state, self._reason = 'paused', 'slow'
                else:
                    self._state, self._reason = 'listening', ''
                self._next_due = now + max(ticket.interval, elapsed * 2)
            self._version += 1
            self._publish()
            return True


def recent_chunks(chunks, max_samples):
    """Return only bounded chronological tails; never concatenate or mutate."""
    if type(max_samples) is not int or max_samples < 0:
        raise ValueError('max_samples must be a nonnegative integer')
    remaining, selected = max_samples, []
    for chunk in reversed(chunks):
        if remaining == 0:
            break
        count = min(len(chunk), remaining)
        if count:
            selected.append(chunk if count == len(chunk) else chunk[-count:])
            remaining -= count
    selected.reverse()
    return selected
