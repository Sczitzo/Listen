"""Resolve saved microphone names against fresh PortAudio enumeration."""
import json


def microphone_options(audio):
    choices = {"Use Windows default microphone": None}
    hosts = audio.query_hostapis()
    counts = {}
    for device in audio.query_devices():
        if device['max_input_channels'] < 1:
            continue
        host = hosts[device['hostapi']]['name']
        base = (device['name'], host)
        occurrence = counts.get(base, 0)
        counts[base] = occurrence + 1
        key = json.dumps([*base, occurrence], ensure_ascii=False)
        label = f"{device['name']} ({host})" + (f" #{occurrence + 1}" if occurrence else "")
        choices[label] = key
    return choices


def resolve_microphone(audio, selection):
    if selection is None:
        return None
    hosts = audio.query_hostapis()
    counts = {}
    for index, device in enumerate(audio.query_devices()):
        if device['max_input_channels'] < 1:
            continue
        base = (device['name'], hosts[device['hostapi']]['name'])
        occurrence = counts.get(base, 0)
        counts[base] = occurrence + 1
        if json.dumps([*base, occurrence], ensure_ascii=False) == selection:
            return index
    raise LookupError("Selected microphone is unavailable. Reconnect it or choose Use Windows default microphone in Settings.")
