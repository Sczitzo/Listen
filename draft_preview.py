"""Ephemeral, latest-only cleanup drafts; no persistence or final-path policy."""
from dataclasses import dataclass
import re
import threading
import time

import cleanup_http
from spoken_format import formatting_is_safe as cleanup_is_safe, layout_explicit_lists, prepare_spoken_list

Cancellation = cleanup_http.Cancellation


class _Lease:
    def __init__(self, gate, kind):
        self._gate, self._kind, self._released = gate, kind, False

    def release(self):
        gate = self._gate
        with gate._condition:
            if self._released:
                return
            self._released = True
            if self._kind == 'authoritative':
                assert gate._authoritative > 0
                gate._authoritative -= 1
            elif self._kind == 'draft':
                assert gate._draft is not None
                gate._draft = None
            elif self._kind == 'cleanup_warm':
                assert gate._cleanup_warm
                gate._cleanup_warm = False
            else:
                assert gate._auxiliary
                gate._auxiliary = False
            gate._condition.notify_all()


class LocalModelGate:
    """Authoritative reservations prevent new optional local model requests."""
    def __init__(self):
        self._condition = threading.Condition()
        self._authoritative = 0
        self._draft = None
        self._auxiliary = False
        self._cleanup_warm = False

    def reserve_authoritative(self):
        with self._condition:
            self._authoritative += 1
            token = self._draft
            lease = _Lease(self, 'authoritative')
        if token is not None:
            token.cancel()
        return lease

    def wait_draft(self, timeout=.5):
        with self._condition:
            return self._condition.wait_for(lambda: self._draft is None, timeout)

    def try_auxiliary(self):
        with self._condition:
            if self._authoritative or self._draft is not None or self._auxiliary:
                return None
            self._auxiliary = True
            return _Lease(self, 'auxiliary')

    def try_draft(self, token):
        with self._condition:
            if (self._authoritative or self._draft is not None or self._auxiliary
                    or self._cleanup_warm or token.cancelled):
                return None
            self._draft = token
            return _Lease(self, 'draft')

    def try_cleanup_warm(self):
        # Let speech preview run while Ollama loads, but keep cleanup drafts out.
        with self._condition:
            if self._authoritative or self._draft is not None or self._auxiliary or self._cleanup_warm:
                return None
            self._cleanup_warm = True
            return _Lease(self, 'cleanup_warm')


@dataclass(frozen=True)
class DraftJob:
    generation: int
    context: object
    revision: int
    key: object
    system: str
    text: str
    model: str
    vocab: tuple = ()
    parts: tuple = ()

    def __post_init__(self):
        if not self.parts:
            object.__setattr__(self, 'parts', (self.text,))
        if ' '.join(self.parts) != self.text:
            raise ValueError('Draft parts must cover the exact source.')


def _fragments(parts, budget):
    """Lossless UTF-8 bounded pieces, including original join separators."""
    result = []
    for index, part in enumerate(parts):
        remaining = (' ' if index else '') + part
        while remaining:
            size = 0
            end = 0
            for char in remaining:
                width = len(char.encode('utf-8'))
                if size + width > budget:
                    break
                size += width
                end += 1
            if not end:
                raise ValueError('Prompt leaves no room for source characters.')
            if end < len(remaining):
                whitespace = max((i + 1 for i, char in enumerate(remaining[:end])
                                  if char.isspace()), default=0)
                if whitespace and remaining[:whitespace].strip():
                    end = whitespace
            result.append(remaining[:end])
            remaining = remaining[end:]
    return result


def _clean(text):
    lines = text.strip().splitlines()
    if len(lines) > 1 and re.match(r"(?i)^here('s| is| are)\b.*:$", lines[0].strip()):
        lines = lines[1:]
    out = '\n'.join(lines).strip()
    if len(out) > 1 and out[0] in "\"'" and out[-1] == out[0]:
        out = out[1:-1].strip()
    return out


def _assemble(entries):
    """Keep raw separators; add paragraph breaks around validated structures."""
    structure = re.compile(r'^\s*(?:[-*+]\s+|(?:\d+|[A-Za-z])[.)]\s+|\||:?-{3,}:?\s*\|)',
                           re.MULTILINE)
    result = []
    previous_structured = False
    for entry in entries:
        cleaned = entry['clean']
        text = entry['raw'] if cleaned is None else cleaned
        structured = cleaned is not None and bool(structure.search(cleaned))
        if structured:
            leading = text[:len(text) - len(text.lstrip())]
            if leading and not leading.endswith('\n'):
                # Source join spaces belong outside the list's indentation.
                text = leading + '\n' + text[len(leading):]
        if result and (previous_structured or structured):
            # Insert presentation outside source fragments: never strip a raw
            # fallback, including its leading/trailing whitespace.
            result.append('\n\n')
        result.append(text)
        previous_structured = structured
    return ''.join(result)


class DraftCoordinator:
    def __init__(self, publish, valid, *, gate=None, request=None, interval=2.5, timeout=5):
        self._publish, self._valid = publish, valid
        self.gate = gate if gate is not None else LocalModelGate()
        self._request = request if request is not None else cleanup_http.stream_json
        self._interval, self._timeout = interval, timeout
        self._condition = threading.Condition()
        self._pending = self._active = self._active_job = self._thread = None
        self._closed = False
        self._next = 0.
        self._epoch = 0
        self._scope = None
        self._mode = 'whole'
        self._entries = []
        self._whole = None
        self._capacity_identity = None
        self._counts = dict(attempts=0, success=0, partial=0, length=0, timeout=0, unsafe=0)
        self._last_reason = ''

    @staticmethod
    def _scope_for(job):
        return (job.generation, job.context, job.key, job.model, job.system, job.vocab)

    def _sync(self, job):
        scope = self._scope_for(job)
        if self._scope != scope:
            self._scope, self._mode, self._entries, self._whole = scope, 'whole', [], None
            self._capacity_identity = None
        budget = 3000 - len(job.system.encode('utf-8'))
        if budget <= 0:
            return []
        if len(job.text.encode('utf-8')) > budget:
            self._mode = 'parts'
        if self._mode == 'whole':
            return []
        try:
            fragments = _fragments(job.parts, budget)
        except ValueError:
            return []
        prefix = 0
        while prefix < min(len(fragments), len(self._entries)):
            if self._entries[prefix]['raw'] != fragments[prefix]:
                break
            prefix += 1
        self._entries[prefix:] = [dict(raw=raw, clean=raw if not raw.strip() else None,
                                      attempted=not raw.strip())
                                  for raw in fragments[prefix:]]
        return self._entries

    @property
    def closed(self):
        with self._condition:
            return self._closed

    @property
    def busy(self):
        with self._condition:
            return self._active is not None or self._pending is not None

    @property
    def alive(self):
        with self._condition:
            return self._thread is not None and self._thread.is_alive()

    def metadata(self):
        with self._condition:
            return dict(self._counts, last_reason=self._last_reason, mode=self._mode,
                        pending=self._pending is not None, fragments=len(self._entries))

    def needs_work(self, job):
        with self._condition:
            if self._closed or self._pending == job or self._active_job == job:
                return False
            entries = self._sync(job)
            if self._capacity_identity == (self._scope, job.text):
                return False
            if self._mode == 'whole':
                return self._whole is None or self._whole[0] != self._scope or self._whole[1] != job.text
            return any(not entry['attempted'] for entry in entries)

    def offer(self, job):
        with self._condition:
            if self._closed or self._pending == job or self._active_job == job:
                return False
            self._pending = job
            if self._thread is None:
                thread = threading.Thread(target=self._run, name='cleanup-draft', daemon=True)
                self._thread = thread
                try:
                    thread.start()
                except Exception:
                    self._thread = self._pending = None
                    raise
            self._condition.notify_all()
            return True

    def cached_whole(self, generation, context, key, raw):
        with self._condition:
            cached, self._whole = self._whole, None
            if cached is not None and cached[0][:3] == (generation, context, key) and cached[1] == raw:
                return cached[2]
            return None

    def retire(self, preserve_whole=False):
        with self._condition:
            self._epoch += 1
            self._pending = None
            self._entries = []
            self._scope = None
            self._mode = 'whole'
            self._capacity_identity = None
            if not preserve_whole:
                self._whole = None
            active = self._active
            self._condition.notify_all()
        if active is not None:
            active.cancel()

    def close(self, timeout=2):
        with self._condition:
            self._closed = True
            thread = self._thread
        self.retire()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return not self.alive

    def _emit(self, token, job, text, state, reason=''):
        if state in ('ready', 'partial') and text:
            prepared = prepare_spoken_list(job.text, job.vocab)
            text = prepared.formatted if prepared else layout_explicit_lists(text, job.vocab)
        # Cancellation and publication share this boundary, including callbacks.
        with token._publication_lock:
            if token.cancelled:
                return
            with self._condition:
                closed = self._closed
            if closed or not self._valid(job) or token.cancelled:
                return
            with self._condition:
                closed = self._closed
                self._last_reason = reason
                if state == 'partial':
                    self._counts['partial'] += 1
            if not closed:
                self._publish(job, text, state, reason)

    def _completion_current(self, token, job, epoch, scope):
        return (not token.cancelled and self._valid(job) and
                not self._closed and epoch == self._epoch and self._scope == scope)

    def _run(self):
        while True:
            with self._condition:
                while not self._closed:
                    delay = self._next - time.monotonic()
                    if self._pending is not None and delay <= 0:
                        break
                    self._condition.wait(delay if self._pending is not None and delay > 0 else None)
                if self._closed:
                    return
                job, self._pending = self._pending, None
                token = Cancellation()
                self._active, self._active_job = token, job
                epoch = self._epoch
            lease = None
            try:
                if not self._valid(job):
                    continue
                with self._condition:
                    entries = list(self._sync(job))
                    scope, mode = self._scope, self._mode
                    capacity = len(job.system.encode('utf-8')) >= 3000 or (mode == 'parts' and not entries)
                    index = next((i for i, e in enumerate(entries) if not e['attempted']), None)
                    raw = job.text if mode == 'whole' else (entries[index]['raw'] if index is not None else None)
                if capacity:
                    with self._condition:
                        self._counts['length'] += 1
                        self._capacity_identity = (scope, job.text)
                    self._emit(token, job, job.text, 'partial', 'length')
                    continue
                if raw is None:
                    continue
                lease = self.gate.try_draft(token)
                if lease is None:
                    self._emit(token, job, '', 'paused', 'busy')
                    continue
                self._emit(token, job, '', 'working')
                token.check()
                payload = {'model': job.model, 'stream': True, 'keep_alive': '5m',
                           'options': {'num_ctx': 8192, 'num_predict': 4000, 'temperature': 0,
                                       'num_batch': cleanup_http.NUM_BATCH},
                           'messages': [{'role': 'system', 'content': job.system},
                                        {'role': 'user', 'content': raw}]}
                if job.model.startswith('qwen3'):
                    payload['think'] = False
                reason, text = '', None
                with self._condition:
                    self._counts['attempts'] += 1
                try:
                    data = self._request(payload, timeout=self._timeout, cancel=token)
                    token.check()
                    if data.get('done') is not True or data.get('done_reason') != 'stop':
                        raise cleanup_http.CleanupHTTPError('incomplete_output')
                    candidate = _clean(data['message']['content'])
                    if cleanup_is_safe(raw, candidate, job.vocab):
                        # Preserve source separators when a bounded fragment is cleaned.
                        leading = raw[:len(raw) - len(raw.lstrip())]
                        trailing = raw[len(raw.rstrip()):]
                        text = candidate if mode == 'whole' else leading + candidate + trailing
                    else:
                        reason = 'unsafe'
                except cleanup_http.CleanupCancelledError:
                    raise
                except Exception as exc:
                    reason = 'timeout' if isinstance(exc, TimeoutError) else 'unavailable'
                # Cache writes share cancellation's publication boundary too.
                with token._publication_lock:
                    if not self._completion_current(token, job, epoch, scope):
                        continue
                    with self._condition:
                        if self._closed or epoch != self._epoch or self._scope != scope:
                            continue
                        if reason in self._counts:
                            self._counts[reason] += 1
                        if text is not None:
                            self._counts['success'] += 1
                        if mode == 'whole':
                            if text is not None:
                                self._whole = (scope, job.text, text)
                            else:
                                self._mode = 'parts'
                                self._sync(job)
                        else:
                            # A newer revision may have invalidated this exact suffix.
                            if index < len(self._entries) and self._entries[index] is entries[index]:
                                self._entries[index].update(clean=text, attempted=True)
                    if mode == 'whole' and text is not None:
                        self._emit(token, job, text, 'ready')
                    elif mode == 'whole':
                        self._emit(token, job, job.text, 'partial', reason)
                    else:
                        assembly = _assemble(entries)
                        complete = all(e['clean'] is not None for e in entries)
                        if not cleanup_is_safe(job.text, assembly, job.vocab):
                            assembly, complete, reason = job.text, False, 'unsafe'
                            with self._condition:
                                self._counts['unsafe'] += 1
                        self._emit(token, job, assembly, 'ready' if complete else 'partial', reason)
            except cleanup_http.CleanupCancelledError:
                pass
            except Exception:
                try:
                    self._emit(token, job, job.text, 'partial', 'unavailable')
                except Exception:
                    pass
            finally:
                if lease is not None:
                    lease.release()
                with self._condition:
                    self._active = self._active_job = None
                    self._next = time.monotonic() + self._interval
                    self._condition.notify_all()
