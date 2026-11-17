"""Selected microphone sounds, fallback, and nonblocking dispatch; no live app."""
import ast
from pathlib import Path
import sys
import types

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
tree = ast.parse((ROOT / "dictate.py").read_text(encoding="utf-8"))
nodes = [node for node in tree.body if
         isinstance(node, ast.FunctionDef) and node.name == "_chime" or
         isinstance(node, ast.Assign) and any(
             isinstance(t, ast.Name) and t.id == "CHIMES" for t in node.targets)]
played, pending = [], []
class Thread:
    def __init__(self, target, daemon):
        assert daemon
        self.target = target
    def start(self):
        pending.append(self.target)

selected = np.full((12, 2), 0.125, dtype=np.float32)
env = dict(np=np, SAMPLE_RATE=16000, threading=types.SimpleNamespace(Thread=Thread),
           sd=types.SimpleNamespace(play=lambda samples, rate: played.append((samples, rate))),
           load_cue=lambda name: (selected, 44100) if name in ("start", "stop") else None)
exec(compile(ast.Module(body=nodes, type_ignores=[]), "dictate-chime", "exec"), env)
for name in ("start", "stop"):
    env["_chime"](name)
    assert not played, "hotkey callback must only schedule audio"
    pending.pop()()
    samples, rate = played.pop()
    assert rate == 44100, "selected microphone WAV must replace synthesized notes"
    assert np.array_equal(samples, selected), "play chosen audio without changing its samples"

for name in ("cancel", "error", "ready", "copied"):
    env["_chime"](name)
    pending.pop()()
    samples, rate = played.pop()
    assert rate == 16000 and samples.dtype == np.float32 and np.any(samples)

# Missing/unsupported assets keep the existing audible cue.
env["load_cue"] = lambda name: None
env["_chime"]("start")
pending.pop()()
assert played.pop()[1] == 16000

from sound_cues import load_cue
import sound_cues as cues
import hashlib
import tempfile
import wave
expected = {
    "start": ("mic-open.wav", "9136caeb750756176bd696d639ec74261523fa1f9d8312be6ccd673a506e0a2b"),
    "stop": ("mic-off.wav", "a005358f8c4fb840649303cce5e79c2996b6965a1c08409d0b91da2c418d4a30"),
}
assert cues.CUE_FILES == {name: item[0] for name, item in expected.items()}
for name, (filename, digest) in expected.items():
    path = ROOT / "assets" / "sounds" / filename
    if not path.exists():
        assert load_cue(name) is None, "absent licensed recording uses fallback"
        print(f"{filename}: absent local licensed asset; fallback verified")
        continue
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    samples, rate = load_cue(name)
    assert rate == 44100 and samples.ndim == 2 and samples.shape[1] == 2
    assert samples.dtype == np.float32 and np.isfinite(samples).all()
    assert 0 < np.max(np.abs(samples)) <= 1
    assert load_cue(name)[0] is samples, "decode only once"
assert load_cue("ready") is None

# Exercise signed 24-bit boundaries independently of the downloaded recording.
with tempfile.TemporaryDirectory(prefix=".test-run-sound-", dir=ROOT) as folder:
    fixture = Path(folder) / "fixture.wav"
    with wave.open(str(fixture), "wb") as out:
        out.setparams((1, 3, 44100, 0, "NONE", "not compressed"))
        out.writeframes(bytes.fromhex("000080 ffffff 000000 ffff7f"))
    samples, _ = cues._read_wav(fixture)
    assert np.allclose(samples[:, 0], [-1, -1 / 8388608, 0, 8388607 / 8388608])
    assert cues._read_wav(Path(folder) / "missing.wav") is None
    fixture.write_bytes(b"broken")
    assert cues._read_wav(fixture) is None
print("sound cues: selected mapping, async dispatch, PCM decoding and fallback passed")
