"""Saved microphone selection survives enumeration changes without silent substitution."""
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_devices import microphone_options, resolve_microphone

devices = [{'name': 'Speakers', 'hostapi': 0, 'max_input_channels': 0},
           {'name': 'USB mic', 'hostapi': 0, 'max_input_channels': 1}]
audio = SimpleNamespace(query_devices=lambda: devices,
                        query_hostapis=lambda: [{'name': 'WASAPI'}])
choices = microphone_options(audio)
assert len(choices) == 2 and choices['Use Windows default microphone'] is None
selection = choices['USB mic (WASAPI)']
assert resolve_microphone(audio, selection) == 1
devices.reverse()
assert resolve_microphone(audio, selection) == 0
devices.clear()
try:
    resolve_microphone(audio, selection)
except LookupError as error:
    assert 'Reconnect' in str(error) and 'default' in str(error)
else:
    raise AssertionError('Missing microphone silently replaced')
assert resolve_microphone(audio, None) is None
print('audio devices: passed')
