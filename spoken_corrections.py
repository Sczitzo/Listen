"""Compatibility source view of the shared original-span list preparation."""
from spoken_format import prepare_spoken_list


def correction_source(text):
    prepared = prepare_spoken_list(text)
    return prepared.source if prepared is not None else text
