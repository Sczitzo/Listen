"""Pure Windows hotkey recognition with paired input suppression.

The router only decides which input edges belong to a configured binding. It
does not call application, native input, storage, microphone or model APIs.
"""
import threading


LEGACY_BINDINGS = frozenset(('ctrl_r', 'shift_r', 'alt_r', 'pause', 'scroll_lock'))
LEGACY_KEYS = {'ctrl_r': 0xA3, 'shift_r': 0xA1, 'alt_r': 0xA5,
               'pause': 0x13, 'scroll_lock': 0x91}
MOUSE_BINDINGS = frozenset(('mouse_x1', 'mouse_x2'))
MODIFIERS = ('ctrl', 'alt', 'shift')
MODIFIER_KEYS = {
    'ctrl': frozenset((0x11, 0xA2, 0xA3)),
    'alt': frozenset((0x12, 0xA4, 0xA5)),
    'shift': frozenset((0x10, 0xA0, 0xA1)),
}
ALL_MODIFIER_KEYS = frozenset().union(*MODIFIER_KEYS.values())
TRIGGERS = {'space': 0x20}
TRIGGERS.update({f'f{index}': 0x70 + index - 1 for index in range(1, 13)})
TRIGGERS.update({letter: ord(letter.upper()) for letter in 'abcdefghijklmnopqrstuvwxyz'})
TRIGGERS.update({number: ord(number) for number in '0123456789'})


def _parse(value):
    if not isinstance(value, str):
        return None
    if value in LEGACY_BINDINGS:
        return ('legacy', (), LEGACY_KEYS[value])
    if value in MOUSE_BINDINGS:
        return ('mouse', (), value[6:])
    # Require an exact canonical sequence, no duplicates and a single trigger.
    parts = value.split('+')
    if len(parts) < 2 or len(parts) > 4 or parts[-1] not in TRIGGERS:
        return None
    modifiers = parts[:-1]
    if (any(part not in MODIFIERS for part in modifiers)
            or modifiers != [part for part in MODIFIERS if part in modifiers]):
        return None
    return ('key', tuple(modifiers), TRIGGERS[parts[-1]])


def valid_binding(value):
    """Accept only existing single keys, two side buttons, or canonical chords."""
    return _parse(value) is not None


def _physical_snapshot(keys_down, mouse_down):
    try:
        if (keys_down is None or mouse_down is None
                or isinstance(keys_down, (str, bytes))
                or isinstance(mouse_down, (str, bytes))):
            raise ValueError('A complete physical snapshot is required')
        keys = set(keys_down)
        mouse = set(mouse_down)
    except (TypeError, ValueError):
        raise ValueError('A complete physical snapshot is required') from None
    if (any(type(code) is not int or not 0 <= code <= 255 for code in keys)
            or any(code not in ('x1', 'x2') for code in mouse)):
        raise ValueError('Invalid physical input snapshot')
    return keys, mouse


class HotkeyRouter:
    """Serialize short physical transitions across keyboard and mouse hooks."""

    def __init__(self, binding):
        parsed = _parse(binding)
        if parsed is None:
            raise ValueError('Invalid hotkey binding')
        self._lock = threading.Lock()
        self._value = binding
        self._binding = parsed
        self._keys_down = set()
        self._mouse_down = set()
        self._gesture = None
        self._consumed_key = None
        self._consumed_mouse = None
        self._key_tombstones = set()
        self._mouse_tombstones = set()
        self._reservation = None
        self._last_rebind_result = 'idle'

    def _physical_idle(self):
        return not (self._keys_down or self._mouse_down or self._gesture
                    or self._consumed_key is not None or self._consumed_mouse is not None)

    def _idle(self):
        return self._physical_idle() and self._reservation is None

    def mode(self):
        """The effective mode, which remains old while a change is pending."""
        with self._lock:
            return self._binding[0]

    def binding(self):
        """The effective value, which remains old while a change is pending."""
        with self._lock:
            return self._value

    def idle(self):
        """Whether changing the binding can no longer split an input pair."""
        with self._lock:
            return self._idle()

    def rebind(self, binding):
        parsed = _parse(binding)
        if parsed is None:
            return False
        with self._lock:
            if not self._idle():
                return False
            self._binding = parsed
            self._value = binding
            self._last_rebind_result = 'applied'
            return True

    def begin_rebind(self, binding):
        """Reserve an idle binding change before a separate settings write."""
        parsed = _parse(binding)
        if parsed is None:
            return False
        with self._lock:
            if not self._idle():
                return False
            self._reservation = {'value': binding, 'binding': parsed, 'success': None}
            self._last_rebind_result = 'reserved'
            return True

    def _settle_rebind(self):
        if (self._reservation is None or self._reservation['success'] is None
                or not self._physical_idle()):
            return
        if self._reservation['success']:
            self._binding = self._reservation['binding']
            self._value = self._reservation['value']
            self._last_rebind_result = 'applied'
        else:
            self._last_rebind_result = 'unchanged'
        self._reservation = None

    def finish_rebind(self, success):
        """Complete a settings write, or defer its decision until all ups arrive."""
        if type(success) is not bool:
            raise ValueError('Rebind success must be a boolean')
        with self._lock:
            if self._reservation is None:
                return self._last_rebind_result
            prior = self._reservation['success']
            if prior is not None and prior is not success:
                raise ValueError('Rebind outcome was already decided')
            self._reservation['success'] = success
            self._last_rebind_result = 'pending'
            self._settle_rebind()
            return self._last_rebind_result

    def rebind_status(self):
        """Expose a content-free status for a reserved settings write."""
        with self._lock:
            return self._last_rebind_result

    def reconcile(self, keys_down, mouse_down):
        """Reconcile a complete, caller-sampled physical input snapshot.

        This never emits hotkey actions. A swallowed down and an active
        gesture remain protected until their actual matching release reaches
        transition(); reconciliation must not synthesize a Stop or leak an up.
        The caller must serialize sampling with its input ingress and supply a
        fresh, trustworthy complete snapshot, not an unavailable device result.
        """
        keys, mouse = _physical_snapshot(keys_down, mouse_down)
        with self._lock:
            old_keys, old_mouse = self._keys_down, self._mouse_down
            if self._consumed_key is not None:
                keys.add(self._consumed_key)
            if self._consumed_mouse is not None:
                mouse.add(self._consumed_mouse)
            if self._gesture:
                kind, required, trigger = self._gesture
                if kind == 'key':
                    # Preserve the whole in-progress chord. A missed up must
                    # never turn an observation into an implicit release.
                    protected = {trigger}
                    for modifier in required:
                        protected.update(MODIFIER_KEYS[modifier])
                    keys.update(old_keys & protected)
                elif kind == 'mouse':
                    mouse.add(trigger)
            self._keys_down, self._mouse_down = keys, mouse
            report = {'removed_keys': len(old_keys - keys),
                      'removed_mouse': len(old_mouse - mouse),
                      'added_keys': len(keys - old_keys),
                      'added_mouse': len(mouse - old_mouse),
                      'active_gesture': self._gesture is not None,
                      'protected_consumed': self._consumed_key is not None or self._consumed_mouse is not None}
            self._settle_rebind()
            return report

    def recover_cancel(self, keys_down, mouse_down):
        """Recover only after the caller explicitly cancelled the recording.

        A fresh complete snapshot must be sampled with input ingress serialized.
        Recovery refuses to change state while any active/consumed component is
        physically held. A later orphaned up for a consumed down is swallowed
        once; the next actual down clears that protection and starts normally.
        No press, release, or other action is emitted by this method.
        """
        keys, mouse = _physical_snapshot(keys_down, mouse_down)
        with self._lock:
            protected_keys = set()
            protected_mouse = set()
            if self._consumed_key is not None:
                protected_keys.add(self._consumed_key)
                kind, required, trigger = self._binding
                if kind == 'key' and trigger == self._consumed_key:
                    for modifier in required:
                        protected_keys.update(MODIFIER_KEYS[modifier])
            if self._consumed_mouse is not None:
                protected_mouse.add(self._consumed_mouse)
            if self._gesture:
                kind, required, trigger = self._gesture
                if kind == 'key':
                    protected_keys.add(trigger)
                    for modifier in required:
                        protected_keys.update(MODIFIER_KEYS[modifier])
                elif kind == 'mouse':
                    protected_mouse.add(trigger)
            # Legacy gestures are driven by the existing listener rather than
            # this router, but their configured physical key must also be up.
            if self._binding[0] == 'legacy':
                protected_keys.add(self._binding[2])
            if protected_keys & keys or protected_mouse & mouse:
                return False
            if self._consumed_key is not None:
                self._key_tombstones.add(self._consumed_key)
            if self._consumed_mouse is not None:
                self._mouse_tombstones.add(self._consumed_mouse)
            self._keys_down, self._mouse_down = keys, mouse
            self._gesture = self._consumed_key = self._consumed_mouse = None
            self._settle_rebind()
            return True

    def _modifiers_down(self):
        return {name for name, keys in MODIFIER_KEYS.items() if self._keys_down & keys}

    def _finish_key_gesture(self):
        if not self._gesture or self._gesture[0] != 'key':
            return False
        _, required, trigger = self._gesture
        if trigger in self._keys_down:
            return False
        if any(self._keys_down & MODIFIER_KEYS[name] for name in required):
            return False
        self._gesture = None
        return True

    def transition(self, device, code, down, injected=False):
        """Return (consume Windows event, actions) for one physical input edge.

        Actions are synchronous decisions only: the integrating app must execute
        their side effects separately and in order. Injected events never alter
        physical state and must continue through to the intended application.
        """
        if injected or type(down) is not bool:
            return False, []
        if device == 'key':
            if type(code) is not int or not 0 <= code <= 255:
                return False, []
            with self._lock:
                reserved = self._reservation is not None
                suppress, actions = self._key_transition(code, down)
                self._settle_rebind()
                return suppress, [] if reserved else actions
        if device == 'mouse' and code in ('x1', 'x2'):
            with self._lock:
                reserved = self._reservation is not None
                suppress, actions = self._mouse_transition(code, down)
                self._settle_rebind()
                return suppress, [] if reserved else actions
        return False, []

    def _key_transition(self, code, down):
        was_down = code in self._keys_down
        if down:
            self._key_tombstones.discard(code)
            if was_down:
                return code == self._consumed_key, []
            self._keys_down.add(code)
            if self._gesture:
                kind, required, _ = self._gesture
                if kind == 'key' and any(code in MODIFIER_KEYS[name] for name in required):
                    return False, []
                return False, ['other']
            kind, required, trigger = self._binding
            if self._reservation is not None and kind == 'legacy' and code == trigger:
                self._gesture = ('key', (), trigger)
                self._consumed_key = trigger
                return True, ['press']
            if kind != 'key' or code != trigger:
                return False, []
            modifiers = self._modifiers_down()
            if modifiers != set(required):
                return False, []
            # Windows AltGr commonly reports left Ctrl + right Alt. Refusing
            # that pair prevents text-entry combinations from starting speech.
            if 'ctrl' in required and 'alt' in required and 0xA5 in self._keys_down:
                return False, []
            if (self._keys_down - ALL_MODIFIER_KEYS) != {trigger} or self._mouse_down:
                # The configured chord is reserved even when another held input
                # makes this gesture ineligible to start a recording.
                self._consumed_key = trigger
                return True, []
            self._gesture = ('key', required, trigger)
            self._consumed_key = trigger
            return True, ['press']

        if not was_down and code in self._key_tombstones:
            self._key_tombstones.remove(code)
            return True, []
        if not was_down:
            return False, []
        self._key_tombstones.discard(code)
        self._keys_down.remove(code)
        suppress = code == self._consumed_key
        if suppress:
            self._consumed_key = None
        actions = ['release'] if self._finish_key_gesture() else []
        return suppress, actions

    def _mouse_transition(self, code, down):
        was_down = code in self._mouse_down
        if down:
            self._mouse_tombstones.discard(code)
            if was_down:
                return code == self._consumed_mouse, []
            self._mouse_down.add(code)
            kind, _, trigger = self._binding
            if kind != 'mouse' or code != trigger:
                return False, []
            # A selected side button belongs to this app even if the press is
            # ineligible to start recording; letting it through could navigate
            # a browser away from an unsaved draft.
            self._consumed_mouse = code
            if self._gesture or self._keys_down or self._mouse_down != {code}:
                return True, []
            self._gesture = ('mouse', (), code)
            return True, ['press']

        if not was_down and code in self._mouse_tombstones:
            self._mouse_tombstones.remove(code)
            return True, []
        if not was_down:
            return False, []
        self._mouse_tombstones.discard(code)
        self._mouse_down.remove(code)
        suppress = code == self._consumed_mouse
        if suppress:
            self._consumed_mouse = None
            if self._gesture and self._gesture[0] == 'mouse' and self._gesture[2] == code:
                self._gesture = None
                return True, ['release']
            return True, []
        return False, []
