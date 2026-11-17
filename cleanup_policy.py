"""In-memory ownership for optional cleanup prewarming and timeout backoff."""
import threading
import time


class CleanupPolicy:
    def __init__(self, *, clock=time.monotonic, cooldown=30.0):
        self._clock, self._cooldown = clock, cooldown
        self._lock = threading.Lock()
        self._models = {}
        self._warm = None
        self._serial = 0

    def _entry(self, model):
        if model not in self._models:
            if len(self._models) >= 16:
                for old in tuple(self._models):
                    if self._warm is None or old != self._warm[1]:
                        del self._models[old]
                        break
            self._models[model] = {'epoch': 0, 'until': 0., 'touched': float('-inf')}
        return self._models[model]

    def available(self, model):
        with self._lock:
            return self._clock() >= self._entry(model)['until']

    def is_warming(self, model):
        with self._lock:
            return (self._warm is not None and self._warm[1] == model
                    and self._warm[2] == self._entry(model)['epoch'])

    def timed_out(self, model):
        with self._lock:
            state = self._entry(model)
            state['epoch'] += 1
            state['until'] = self._clock() + self._cooldown

    def succeeded(self, model):
        with self._lock:
            state = self._entry(model)
            state['epoch'] += 1
            state['until'], state['touched'] = 0., self._clock()

    def claim_warm(self, model, *, interval):
        with self._lock:
            state, now = self._entry(model), self._clock()
            if self._warm is not None or now < state['until'] or now - state['touched'] < interval:
                return None
            self._serial += 1
            self._warm = (self._serial, model, state['epoch'])
            return self._warm

    def finish_warm(self, token, *, success):
        with self._lock:
            if token != self._warm:
                return False
            self._warm = None
            _, model, epoch = token
            state = self._entry(model)
            if state['epoch'] != epoch:
                return False
            if success is None:
                return False  # The scheduled recording ended before any request.
            if success:
                state['touched'] = self._clock()
            else:
                state['epoch'] += 1
                state['until'] = self._clock() + self._cooldown
            return True
