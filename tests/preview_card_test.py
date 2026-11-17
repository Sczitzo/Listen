"""Card view/bounds behavior without windows, models or input."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preview_card import card_bounds, card_content, fitted_height, text_pixels
from types import SimpleNamespace

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
print('PASS card bounds, retained content, status and final lifecycle')
