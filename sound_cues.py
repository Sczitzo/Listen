"""Local microphone cues. Other notifications keep their existing tones."""
from functools import lru_cache
from pathlib import Path
import wave

import numpy as np


SOUND_DIR = Path(__file__).resolve().parent / "assets" / "sounds"
CUE_FILES = {"start": "mic-open.wav", "stop": "mic-off.wav"}


def _read_wav(path):
    try:
        with wave.open(str(path), "rb") as source:
            channels, width, rate, frames, compression, _ = source.getparams()
            if compression != "NONE" or width not in (2, 3) or channels not in (1, 2):
                return None
            if not 8000 <= rate <= 192000 or not 0 < frames <= rate * 5:
                return None
            raw = source.readframes(frames)
        if len(raw) != frames * channels * width:
            return None
        if width == 2:
            samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768
        else:
            octets = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
            signed = octets[:, 0] | (octets[:, 1] << 8) | (octets[:, 2] << 16)
            signed = (signed ^ 0x800000) - 0x800000
            samples = signed.astype(np.float32) / 8388608
        samples = samples.reshape(-1, channels)
        samples.setflags(write=False)
        return samples, rate
    except (OSError, EOFError, ValueError, wave.Error):
        return None


@lru_cache(maxsize=2)
def load_cue(name):
    filename = CUE_FILES.get(name)
    return _read_wav(SOUND_DIR / filename) if filename else None
