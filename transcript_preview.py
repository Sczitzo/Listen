"""Bounded recording transcript state. No model, device or persistence calls."""
from dataclasses import dataclass
from types import MappingProxyType

from preview_state import PreviewState, Ticket, _number


@dataclass(frozen=True, slots=True)
class TranscriptTicket(Ticket):
    start_sample: int
    end_sample: int


def audio_interval(chunks, start, end):
    """Return chronological slices of [start, end), without concatenation."""
    if type(start) is not int or type(end) is not int or start < 0 or end < start:
        raise ValueError('Invalid audio interval')
    pieces, offset = [], 0
    for chunk in chunks:
        count = len(chunk)
        if offset >= end:
            break
        left, right = max(0, start - offset), min(count, end - offset)
        if right > left:
            pieces.append(chunk if left == 0 and right == count else chunk[left:right])
        offset += count
    return pieces


FINAL_NOTICE_SECONDS = 3


class TranscriptPreview(PreviewState):
    MAX_SAMPLES = 16000 * 600
    MAX_TEXT_BYTES = 2 * 1024 * 1024
    CHUNK_SAMPLES = 16000 * 8

    def __init__(self, *args, **kwargs):
        self._phase = 'idle'
        self._committed_end = self._captured = self._covered = 0
        self._committed = []
        self._tail = ''
        self._text_revision = 0
        self._draft_text = ''
        self._draft_source_text = ''
        self._draft_state = 'disabled'
        self._draft_reason = ''
        self._draft_revision = None
        self._draft_key = ''
        self._draft_enabled = False
        self._draft_pending = None
        self._final_text = self._final_outcome = ''
        self._dismiss_at = None
        super().__init__(*args, **kwargs)

    @property
    def committed_end(self):
        return self._committed_end

    def draft_source(self):
        """One revision and its exact immutable source parts, acquired together."""
        with self._lock:
            parts = tuple(self._committed) + ((self._tail,) if self._tail else ())
            if self._phase != 'recording':
                parts = (self._text,) if self._text else ()
            if ' '.join(parts) != self._text:
                raise ValueError('Draft source coverage mismatch')
            return dict(self._snapshot), parts

    def _publish(self):
        source = self._draft_source_text
        covered_chars = len(source) if (source and self._text.startswith(source)
                                         and self._text[len(source):len(source) + 1] in ('', ' ')) else 0
        self._snapshot = MappingProxyType(dict(active=self._active,
            generation=self._generation, version=self._version, state=self._state,
            text=self._text, reason=self._reason, context=self._context,
            phase=self._phase, text_revision=self._text_revision,
            covered_samples=self._covered, captured_samples=self._captured,
            draft_text=self._draft_text, draft_state=self._draft_state, draft_covered_chars=covered_chars,
            draft_reason=self._draft_reason,
            draft_source_revision=self._draft_revision, final_text=self._final_text,
            final_outcome=self._final_outcome, dismiss_at=self._dismiss_at))

    def _reset_text(self):
        self._committed_end = self._captured = self._covered = 0
        self._committed, self._tail = [], ''
        self._text_revision = 0
        self._draft_text, self._draft_revision = '', None
        self._draft_source_text = ''
        self._draft_state = 'waiting' if self._draft_enabled else 'disabled'
        self._draft_reason = ''
        self._draft_pending = None
        self._final_text = self._final_outcome = ''
        self._dismiss_at = None

    def begin(self, generation, context_tuple, *, now, enabled=True):
        # Base validation must happen before changing the visible session.
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
            self._active, self._generation, self._context = enabled, generation, context_tuple
            self._reset_text()
            self._phase = 'recording'
            self._last_samples, self._next_due = None, now
            self._state, self._text, self._reason = 'listening', '', ''
            self._publish()

    def clear_result_text(self):
        with self._lock:
            if self._phase not in ('finishing', 'final'):
                return False
            self._text = self._tail = self._final_text = ''
            self._committed.clear()
            self._draft_text = self._draft_source_text = self._draft_reason = ''
            self._draft_revision = None
            self._draft_state = 'disabled'
            self._text_revision += 1
            self._version += 1
            self._publish()
            return True

    def end(self):
        with self._lock:
            self._epoch += 1
            self._version += 1
            self._active, self._generation, self._context = False, None, ()
            self._reset_text()
            self._phase = 'idle'
            self._state, self._text, self._reason = 'idle', '', ''
            self._publish()

    def claim(self, generation, context, sample_count, *, now, interval=1.5, first_min_samples=None):
        if (type(generation) is not int or not isinstance(context, tuple)
                or type(sample_count) is not int or sample_count < 0
                or (first_min_samples is not None and (type(first_min_samples) is not int or first_min_samples <= 0))
                or not _number(now) or not _number(interval) or interval < 0):
            return None
        with self._lock:
            if (self._phase != 'recording' or generation != self._generation
                    or context != self._context):
                return None
            if sample_count < self._captured:
                return None
            if sample_count > self._captured:
                self._captured = sample_count
                self._version += 1
                self._publish()
            if sample_count > self.MAX_SAMPLES:
                self._state, self._reason = 'paused', 'capacity'
                self._version += 1
                self._publish()
                return None
            if self._ticket is not None or self._reason == 'capacity' or now < self._next_due:
                return None
            end = min(self._committed_end + self.CHUNK_SAMPLES, sample_count)
            full = end - self._committed_end == self.CHUNK_SAMPLES
            minimum = self._min_samples
            if self._covered == 0 and first_min_samples is not None:
                minimum = min(minimum, first_min_samples)
            if not full and (end - self._committed_end < minimum
                    or (self._last_samples is not None and end - self._last_samples < self._new_samples)):
                return None
            ticket = TranscriptTicket(self._epoch, generation, context, sample_count,
                                      float(interval), self._committed_end, end)
            self._ticket = ticket
            self._work.set()
            self._state, self._reason = 'updating', ''
            self._version += 1
            self._publish()
            return ticket

    def finish(self, ticket, text, *, now, elapsed, failed=False):
        with self._lock:
            if not isinstance(ticket, TranscriptTicket) or self._ticket is not ticket:
                return False
            self._ticket = None
            self._work.clear()
            if (self._phase != 'recording' or ticket.epoch != self._epoch
                    or ticket.generation != self._generation or ticket.context != self._context):
                return False
            if self._reason == 'capacity':
                return False
            invalid = not _number(now) or not _number(elapsed) or elapsed < 0 or not isinstance(text, str)
            if failed or invalid:
                self._state, self._reason = 'listening', 'unavailable'
                if _number(now):
                    self._next_due = now + ticket.interval
            else:
                full = ticket.end_sample - ticket.start_sample == self.CHUNK_SAMPLES
                early_empty = not text and ticket.end_sample < self._min_samples
                parts = self._committed + ([text] if text else [])
                combined = ' '.join(parts)
                if len(combined.encode('utf-8')) + len(self._draft_text.encode('utf-8')) > self.MAX_TEXT_BYTES:
                    self._state, self._reason = 'paused', 'capacity'
                else:
                    if full:
                        if text:
                            self._committed.append(text)
                        self._committed_end = ticket.end_sample
                        self._tail = ''
                        self._last_samples = None
                    else:
                        self._tail = text
                        self._last_samples = None if early_empty else ticket.end_sample
                    if combined != self._text:
                        self._text_revision += 1
                    self._text = combined
                    self._covered = ticket.end_sample
                    if (self._draft_state in ('ready', 'behind')
                            and self._draft_revision is not None
                            and self._draft_revision < self._text_revision):
                        self._draft_state = 'behind'
                    self._state = 'listening'
                    self._reason = 'slow' if elapsed > self._slow_seconds else ''
                    # A quick empty opening sample must not push the regular preview back.
                    if early_empty and elapsed <= self._slow_seconds:
                        self._next_due = now
                    else:
                        self._next_due = now + max(ticket.interval, elapsed * 2)
            self._version += 1
            self._publish()
            return True

    def finish_capture(self, generation, context, *, now):
        with self._lock:
            if (not _number(now) or self._phase != 'recording'
                    or generation != self._generation or context != self._context):
                return False
            self._epoch += 1
            self._active = False
            self._phase, self._state = 'finishing', 'finishing'
            self._draft_pending = None
            self._version += 1
            self._publish()
            return True

    def finalize(self, generation, context, raw, text, outcome, *, now):
        with self._lock:
            if (self._phase != 'finishing' or generation != self._generation
                    or context != self._context or not _number(now)
                    or not all(isinstance(value, str) for value in (raw, text, outcome))):
                return False
            if len(raw.encode('utf-8')) + len(text.encode('utf-8')) > self.MAX_TEXT_BYTES:
                self._reason = 'capacity'
                self._version += 1
                self._publish()
                return False
            self._draft_text, self._draft_revision = '', None
            self._draft_source_text = ''
            self._draft_state = 'disabled'
            self._draft_reason = ''
            self._phase, self._state = 'final', 'final'
            self._text, self._final_text, self._final_outcome = raw, text, outcome
            self._covered = self._captured
            self._text_revision += 1
            self._dismiss_at = now + FINAL_NOTICE_SECONDS
            self._version += 1
            self._publish()
            return True

    def set_draft_settings(self, key, enabled):
        if not isinstance(key, str) or type(enabled) is not bool:
            raise ValueError('Invalid draft settings')
        with self._lock:
            if (key, enabled) == (self._draft_key, self._draft_enabled):
                return False
            self._draft_key, self._draft_enabled = key, enabled
            self._draft_pending = None
            self._draft_text, self._draft_revision = '', None
            self._draft_source_text = ''
            self._draft_state = 'waiting' if enabled else 'disabled'
            self._draft_reason = ''
            self._version += 1
            self._publish()
            return True

    def draft_started(self, generation, context, revision, key):
        with self._lock:
            if not self._draft_valid(generation, context, revision, key):
                return False
            self._draft_pending = (generation, context, revision, key)
            self._draft_state = 'updating'
            self._draft_reason = ''
            self._version += 1
            self._publish()
            return True

    def _draft_valid(self, generation, context, revision, key):
        return (self._phase == 'recording' and self._draft_enabled
                and generation == self._generation and context == self._context
                and key == self._draft_key and type(revision) is int
                and 0 <= revision <= self._text_revision
                and (self._draft_revision is None or revision >= self._draft_revision))

    def draft_finished(self, generation, context, revision, key, text, *, source=None, state='ready', reason=''):
        with self._lock:
            identity = (generation, context, revision, key)
            if (identity != self._draft_pending
                    or not self._draft_valid(generation, context, revision, key)):
                return False
            self._draft_pending = None
            if state in ('ready', 'partial') and isinstance(text, str):
                if len(text.encode('utf-8')) + len(self._text.encode('utf-8')) <= self.MAX_TEXT_BYTES:
                    self._draft_text, self._draft_revision = text, revision
                    self._draft_source_text = (self._text if source is None and revision == self._text_revision
                                               else source or '')
                    self._draft_state = 'behind' if state == 'partial' or revision < self._text_revision else 'ready'
                    self._draft_reason = reason if (state == 'partial' or reason == 'local list') and isinstance(reason, str) else ''
                else:
                    self._draft_state = 'paused'
                    self._draft_reason = 'long transcript'
            else:
                self._draft_state = state if state in ('paused', 'unavailable', 'disabled') else 'unavailable'
                self._draft_reason = reason if isinstance(reason, str) else ''
            self._version += 1
            self._publish()
            return True
