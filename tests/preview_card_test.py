"""Card view/bounds behavior without windows, models or input."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preview_card import PreviewCard, card_bounds, card_content, client_animation_enabled, draw_card_border, fitted_height, text_pixels
import ctypes
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import patch

class BorderCanvas:
    def __init__(self): self.lines, self.arcs, self.deleted, self.lowered = [], [], [], []
    def delete(self, tag): self.deleted.append(tag)
    def create_line(self, *points, **options): self.lines.append((points, options))
    def create_arc(self, *box, **options): self.arcs.append((box, options))
    def tag_lower(self, tag): self.lowered.append(tag)

for width, height, top in ((500, 310, 14), (268, 160, 22), (500, 90, 1), (10, 10, 30)):
    canvas = BorderCanvas()
    draw_card_border(canvas, width, height, top)
    assert len(canvas.lines) == len(canvas.arcs) == 4, 'All four straight edges and rounded corners are required'
    assert canvas.deleted == canvas.lowered == ['card-border']
    assert [options['start'] for _, options in canvas.arcs] == [90, 0, 270, 180]
    assert all(options['extent'] == 90 and options['style'] == 'arc' for _, options in canvas.arcs)
    for coordinates, options in canvas.lines + canvas.arcs:
        assert all(1 <= value <= width - 2 for value in coordinates[::2])
        assert all(1 <= value <= height - 2 for value in coordinates[1::2])
        assert options['width'] == 1, 'The outline must stay inside the native clip'
    top_line = canvas.lines[0][0]
    assert top_line[1] == top_line[3] == min(top, height - 3)
    # Each quarter-circle meets its two adjacent straight edges without a gap.
    from math import cos, sin, radians
    line_ends = [point for points, _ in canvas.lines for point in (points[:2], points[2:])]
    for box, options in canvas.arcs:
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        radius = (box[2] - box[0]) / 2
        for angle in (options['start'], options['start'] + 90):
            endpoint = (cx + radius * cos(radians(angle)), cy - radius * sin(radians(angle)))
            assert any(abs(endpoint[0] - x) < 1e-8 and abs(endpoint[1] - y) < 1e-8 for x, y in line_ends)
canvas = BorderCanvas()
draw_card_border(canvas, 1, 1)
assert not canvas.lines and not canvas.arcs

class Widget:
    def __init__(self, width=20, height=20):
        self.width, self.height, self.options, self.packed = width, height, {}, True
        self.colors, self.timers, self.cancelled = [], [], []
        self.viewable = True
    def configure(self, **options): self.options.update(options)
    def pack(self, **options): self.packed = True
    def pack_forget(self): self.packed = False
    def winfo_reqwidth(self): return self.width
    def winfo_reqheight(self): return self.height
    def winfo_viewable(self): return self.viewable
    def winfo_ismapped(self): return self.packed and getattr(self, 'parent_window', self).viewable
    def winfo_width(self): return self.width
    def cget(self, name): return 6 if name == 'padx' else ''
    def count(self, *args): return (20,)
    def index(self, *args): return '1.0'
    def yview_moveto(self, *args): pass
    def coords(self, *args): pass
    def itemconfigure(self, bar, **options): self.colors.append((bar, options))
    def after(self, interval, callback): self.timers.append((interval, callback)); return 'timer1'
    def after_cancel(self, timer): self.cancelled.append(timer)
    def attributes(self, *args): pass
    def withdraw(self): self.viewable = False
    def state(self, value): self.viewable = value == 'normal'
    def update_idletasks(self): pass
    def destroy(self): self.destroyed = True

# Tk's container request lags newly packed children until its next geometry idle.
area = (0, 0, 1920, 1040)
card = SimpleNamespace(_configure_native=lambda window: True, _protect_activation=lambda: True,
    window=Widget(), _generation=None, _area=None, _shown=True, _text='', _follow=True,
    _target_icon=lambda target: None, _on_cancel=lambda generation: True,
    heading=Widget(84, 24), meter=Widget(38, 28), legend=Widget(1, 1),
    cancel_button=Widget(70, 30), badge=Widget(32, 32), status=Widget(),
    text=Widget(450, 100), _header_height=0, _legend_height=0, _legend_width=0,
    _footer_height=28, _status_layout=None, _height=0, bars=range(5), _pending_fit=None,
    _schedule_fit=lambda: None, _animation_enabled=False, _animation_id=None, _stop_animation=lambda: None, _icon=None)
card.status.parent_window = card.window
card._place = lambda area, height, has_text: setattr(card, '_height', height)
card._replace = lambda value: setattr(card, '_text', value)
short_snapshot = dict(generation=1, phase='recording', active=True, text='A short thought.')
PreviewCard.update(card, short_snapshot, area=area)
PreviewCard._fit_text(card)
first_short_height = card._height
assert card.status.options['text'] == '' and not card.status.packed, 'Recording instructions crowded the footer'
card.legend.width, card.legend.height = 144, 28
card._height = 520
PreviewCard.update(card, dict(short_snapshot, generation=2), area=area)
PreviewCard._fit_text(card)
assert card._height == first_short_height, 'Container geometry settling changed the next recording height'
PreviewCard.update(card, dict(short_snapshot, generation=2, phase='final', final_outcome='copied', dismiss_at=103), area=area, now=100)
assert card.status.packed and card.status.options['text'] == 'Text copied · Ctrl+V to paste'
assert card._header_height == 0, 'Recovery kept recording-only chrome'
for outcome, expected in (('copied', 'Text copied · Ctrl+V to paste'),
                          ('uncertain', 'Check the original app for your text'),
                          ('retained', 'Open Listen to recover text')):
    PreviewCard.update(card, short_snapshot, area=area, hands_free_key='Right Ctrl')
    assert not card.status.winfo_ismapped(), 'Routine recording instructions are still mapped'
    PreviewCard.hide(card)
    assert not card.window.winfo_viewable()
    PreviewCard.update(card, dict(short_snapshot, phase='final', final_outcome=outcome, dismiss_at=103),
                       area=area, now=100, enabled=False, hands_free_key='Right Ctrl')
    assert card._shown and card.status.winfo_ismapped(), 'Withdrawn recording did not map its recovery message'
    assert card.status.options['text'] == expected and not card.cancel_button.packed

animation = SimpleNamespace(_animation_enabled=True, _shown=True, _recording_controls=True,
    window=Widget(), meter=Widget(), bars=range(5), _animation_id=None)
animation._animate = lambda: PreviewCard._animate(animation)
with patch('preview_card.time.monotonic', return_value=0):
    animation._animate()
first_color = animation.meter.colors[0][1]['fill']
assert len(animation.meter.colors) == 5 and animation.window.timers[0][0] == 80
PreviewCard._stop_animation(animation)
assert animation.window.cancelled == ['timer1'] and animation._animation_id is None
animation.meter.colors.clear()
with patch('preview_card.time.monotonic', return_value=1):
    animation._animate()
assert animation.meter.colors[0][1]['fill'] != first_color, 'The state cue did not breathe'
PreviewCard._stop_animation(animation)
animation._animation_enabled = False
animation.window.timers.clear()
animation._animate()
assert not animation.window.timers, 'Reduced motion still scheduled animation'
animation._animation_enabled = True
animation._shown = False
animation._animate()
assert not animation.window.timers, 'A hidden card scheduled animation'
animation._shown = True
animation._animate()
animation._text, animation._icon = '', None
animation._stop_animation = lambda: PreviewCard._stop_animation(animation)
PreviewCard.hide(animation)
assert not animation._shown and animation._animation_id is None
animation._shown = True
animation._animate()
animation._destroyed, animation._fit_id, animation._native_hook = False, None, None
PreviewCard.destroy(animation)
PreviewCard.destroy(animation)
assert animation._animation_id is None and animation.window.destroyed

class AnimationPreference:
    def __init__(self, enabled, success=True): self.enabled, self.success = enabled, success
    def __call__(self, flag, size, target, options):
        assert (flag, size, options) == (0x1042, 0, 0)
        ctypes.cast(target, ctypes.POINTER(wintypes.BOOL)).contents.value = self.enabled
        return self.success
for enabled, success in ((True, True), (False, True), (True, False)):
    with patch('preview_card.ctypes.WinDLL', return_value=SimpleNamespace(SystemParametersInfoW=AnimationPreference(enabled, success))):
        assert client_animation_enabled() == (enabled and success)
with patch('preview_card.ctypes.WinDLL', side_effect=OSError('Synthetic unavailable preference')):
    assert not client_animation_enabled()

destination = SimpleNamespace(hwnd=123, pid=456)
with patch('preview_card.window_icon', return_value=object()) as icon, \
     patch('preview_card.ImageTk.PhotoImage', return_value='synthetic image'):
    PreviewCard._target_icon(card, destination)
    icon.assert_called_once_with(123, 456, 32, '#2d2925')
    assert card.badge.options['image'] == 'synthetic image'

assert card_bounds((0, 0, 1920, 1040), width=500, height=90, centered=True) == (500, 90, 710, 934)
assert card_bounds((-1920, -200, 0, 840), width=500, height=124, centered=True) == (500, 124, -1210, 700)
assert card_bounds((40, 0, 340, 200), width=500, height=124, centered=True) == (268, 124, 56, 60)
assert fitted_height(0, (0, 0, 1920, 1040)) == 90
assert fitted_height(200, (0, 0, 1920, 1040)) == 256
assert fitted_height(5000, (0, 0, 1920, 1040)) == 520
assert fitted_height(10, (0, 0, 1920, 1040), previous=400) == 400
assert fitted_height(5000, (0, 0, 400, 250)) == 119
assert fitted_height(0, (0, 0, 400, 20)) == 1
for result in (123, (123,)):
    assert text_pixels(SimpleNamespace(count=lambda *args: result)) == 123
assert card_bounds((-1920, -200, 0, 840), width=500, height=520, centered=True)[2:] == (-1210, 304)

source = 'Um, set up café.'
tail = ' ' + ('A new sentence. ' * 120).rstrip()
full = source + tail
s = dict(phase='recording', text=full, draft_text='Set up café.', draft_state='behind',
         draft_covered_chars=len(source))
visible, text, _ = card_content(s, 100)
assert visible and text == 'Set up café.' + tail
s.update(draft_text='Please set up the café.')
assert card_content(s, 100)[1] == 'Please set up the café.' + tail
s.update(draft_covered_chars=0)
assert card_content(s, 100)[1] == full
s.update(draft_text='', draft_covered_chars=len(source), draft_state='disabled')
assert card_content(s, 100)[1] == full
s.update(draft_text='Set up café.', draft_state='ready', text=source)
assert card_content(s, 100)[1] == 'Set up café.'
assert card_content(s, 100)[2] == 'Listening · Preview cleaned'
assert card_content(dict(s, state='paused'), 100)[2] == 'Listening · Preview paused'
s.update(active=False, phase='finishing')
assert card_content(s, 100) == (False, '', '')
s.update(phase='final', final_text='Actual final result', dismiss_at=103)
for outcome in ('sent', 'pasted'):
    assert card_content(dict(s, final_outcome=outcome), 100) == (False, '', '')
assert card_content(dict(s, final_outcome='no_speech'), 100) == (True, '', 'No speech heard')
assert card_content(dict(s, final_outcome='uncertain'), 100) == (True, '', 'Check the original app for your text')
assert card_content(dict(s, final_outcome='retained'), 100) == (True, '', 'Open Listen to recover text')
assert card_content(dict(s, final_outcome='copied'), 103) == (False, '', '')
assert not card_content(dict(phase='idle'), 100)[0]
print('PASS connected rounded border geometry, card bounds, retained content, status and final lifecycle')
