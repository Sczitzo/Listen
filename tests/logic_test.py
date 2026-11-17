"""Hotkey state machine checks: hold/release, tap-to-lock, shortcut cancel, Esc, auto-stop, mic lifecycle."""
import os, sys, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dictate as d
from pynput.keyboard import Key

class Suppressed(Exception): pass
d._listener = types.SimpleNamespace(suppress_event=lambda: (_ for _ in ()).throw(Suppressed()))
orig_chime = d._chime
d._chime = lambda *a, **k: None  # silence
d._state = "idle"
HK = Key.ctrl_r
data = lambda vk: types.SimpleNamespace(vkCode=vk, flags=0)

def reset():
    d._recording.clear(); d._close_mic(); d._state = "idle"
    d._locked = d._hotkey_down = d._skip_release = False
    while not d._jobs.empty(): d._jobs.get()

def jobs(): return d._jobs.qsize()
def settle(s=0.6): time.sleep(s)

# 0. mic lifecycle: opened on press, released on release, never held while idle
reset(); assert d._stream is None, "mic must be closed while idle"
d.on_press(HK); settle()
assert d._stream is not None and d._mic_ready, "mic should open on press"
d._press_t -= 1.0; d.on_release(HK); settle()
assert d._stream is None and not d._mic_ready, "mic must be released on stop"
assert jobs() == 1, "hold/release should queue one job"

# 1. cancel while the mic is still opening leaves no stream behind
reset(); d.on_press(HK); d.on_press(d.pk.KeyCode.from_char("c")); settle()
assert d._stream is None and not d._recording.is_set(), "cancel during open must close the mic"

# 2. key repeat while held does not restart
reset(); d.on_press(HK); d._rec_t = 123; d.on_press(HK)
assert d._rec_t == 123, "key repeat restarted recording"

# 3. tap -> locked and still recording; next press stops, its release is ignored
reset(); d.on_press(HK); d.on_release(HK)
assert d._recording.is_set() and d._locked, "tap should lock"
d.on_press(HK); settle()
assert not d._recording.is_set() and jobs() == 1, "second press should stop"
d.on_release(HK); settle(0.2)
assert not d._recording.is_set() and jobs() == 1, "release after lock-stop must be a no-op"

# 4. hotkey + another key = shortcut -> silent cancel, nothing queued
reset(); d.on_press(HK); d.on_press(d.pk.KeyCode.from_char("c"))
assert not d._recording.is_set(), "shortcut should cancel"
d.on_release(HK); settle(0.3)
assert jobs() == 0, "release after shortcut must not queue"

# 5. in hands-free mode other keys do NOT cancel (hotkey no longer held)
reset(); d.on_press(HK); d.on_release(HK); d.on_press(d.pk.KeyCode.from_char("x"))
assert d._recording.is_set(), "typing while locked must not cancel"

# 6. Esc via the filter cancels and is suppressed; Esc while idle passes through
reset(); d.on_press(HK)
try:
    d._event_filter(0x100, data(0x1B)); raise AssertionError("Esc not suppressed")
except Suppressed:
    pass
assert not d._recording.is_set(), "Esc should cancel"
try:
    d._event_filter(0x101, data(0x1B)); raise AssertionError("Esc up not paired")
except Suppressed:
    pass
d._event_filter(0x100, data(0x1B))  # idle: must not raise

# 7. auto-stop: stop() from the tick path queues the job and releases the mic
reset(); d.on_press(HK); settle(); d._rec_t = time.perf_counter() - d.MAX_REC_S - 1; d._stop(); settle()
assert jobs() == 1 and not d._recording.is_set() and d._stream is None, "auto-stop"

# 8. worker state precedence: recording beats busy
reset(); d._busy = "cleaning"; assert d.ui_state() == "cleaning"
d._state = "recording"; assert d.ui_state() == "recording"; d._busy = ""; d._state = "idle"

# 9. prompt assembly
d.SETTINGS["vocab"] = "Suno, Roblox\nClaude Code"; d.SETTINGS["style"] = "clean"
assert d.vocab_words() == ["Suno", "Roblox", "Claude Code"]
assert "Spell these names" in d.system_prompt() and "do not rephrase" in d.system_prompt()
d.SETTINGS["style"] = "tighten"; assert "tighten the wording" in d.system_prompt()

# 12. worker: dictation transcribes, cleans, pastes, and logs history
import numpy as np, os, tempfile
d.HISTORY = os.path.join(tempfile.gettempdir(), "dictate_test_history.jsonl")
pasted = []
d.model = types.SimpleNamespace(transcribe=lambda audio, **k: ([types.SimpleNamespace(text="build me a thing")], None))
d.paste = lambda t, target=None: pasted.append(t)
d._chat = lambda system, user, **kwargs: f"<{system[:12]}|{user}>"
d.SETTINGS["engine"] = "local"  # never hit the API from tests
# engineer() returns (text, by, cost) and falls back to local when the API path fails
d.SETTINGS.update(engine="anthropic", cloud_enabled=True); d._claude = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("offline"))
t3 = d.engineer("hello"); assert t3[1] == d.OLLAMA_MODEL and t3[2] == 0.0 and t3[0].startswith('<You are a pr|{"request": "hello"}'), t3
d._claude = lambda model, system, user, schema=None: ("CLOUD:" + user, 0.01)
t4 = d.engineer("hello"); assert t4 == ('CLOUD:{"request": "hello"}', d.ENGINE_MODEL, 0.01), t4
d.SETTINGS["engine"] = "local"
chunks = [np.full((4000, 1), 0.05, dtype=np.float32)] * 3
reset()
d.process(chunks, 1.0, time.perf_counter())
assert pasted and pasted[0].endswith(" ") and "build me a thing" in pasted[0], "dictate should paste"
assert os.path.exists(d.HISTORY); os.remove(d.HISTORY)

# 13. judge: anthropic path used when selected; any failure falls back to local with a note; local-only when selected
fake = lambda by: {"score": 7, "criteria": {k: 4 for k in ("clarity", "context", "task", "constraints", "success_criteria", "format")},
                   "gaps": ["g1"], "improved": "better", "by": by, "cost": 0.0}
d._judge_local = lambda p: fake("local")
d._judge_anthropic = lambda p: fake("anthropic")
d.SETTINGS["judge"] = "anthropic"
r = d.run_judge("make me a website"); assert r["by"] == "anthropic" and r["note"] == ""
def boom(p): raise ConnectionError("offline")
d._judge_anthropic = boom
r = d.run_judge("make me a website"); assert r["by"] == "local" and "ConnectionError" in r["note"], r
d.SETTINGS["judge"] = "local"; calls_a = []
d._judge_anthropic = lambda p: calls_a.append(1)
r = d.run_judge("x"); assert not calls_a and r["by"] == "local"
# judge schema is what both backends are handed: required keys match what render_card reads
assert set(d.JUDGE_SCHEMA["required"]) == {"score", "criteria", "gaps", "improved"}

# 10. every chime renders and plays without error (real audio out, brief)
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    for name in d.CHIMES:
        orig_chime(name); time.sleep(0.25)
assert "[warn] chime" not in buf.getvalue(), buf.getvalue()
reset()
print("logic: all 11 checks passed")
