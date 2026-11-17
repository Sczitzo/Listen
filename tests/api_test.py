"""Bridge-level checks for the pywebview Api: state, settings, history/stats, the revision loop, favorites, paste."""
import json, os, sys, tempfile, time, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dictate as d

tmp = tempfile.mkdtemp()
d.HISTORY = os.path.join(tmp, "history.jsonl"); d.FAVORITES = os.path.join(tmp, "favorites.json")
d.SETTINGS_FILE = os.path.join(tmp, "settings.json")
d.LEARNED_FILE = os.path.join(tmp, "learned.json")
d._chime = lambda *a, **k: None
api = d.Api()

# Fresh learning state must never share nested containers with defaults or a
# previous in-memory state, including when no saved learning file exists.
empty_learning = {"dismissed": [], "replacements": {}, "removed_headings": {}, "kept": [], "criteria": {}}
first_learning = d.learned()
for value in first_learning.values():
    if isinstance(value, list):
        value.append("test-only")
    else:
        value["test-only"] = 1
d._learned = None
assert d.learned() == empty_learning, "learning reload reused previous nested state"
assert d.LEARNED_DEFAULT == empty_learning, "learning mutated default containers"
assert all(d.learned()[key] is not first_learning[key] for key in empty_learning)
assert api.reset_learned() and d.learned() == empty_learning
assert all(d.learned()[key] is not d.LEARNED_DEFAULT[key] for key in empty_learning)
# Existing saved observations still reload unchanged; only defaults are copied.
d.learned()["kept"].append({"words": 12, "xml": False})
d.save_learned()
d._learned = None
assert d.learned()["kept"] == [{"words": 12, "xml": False, "tags": False}]
assert api.reset_learned() and d.learned() == empty_learning

# state shape
s = api.get_state()
assert {"state", "busy", "lab_ver", "lab_busy", "history_len", "levels", "event"} <= set(s), s
assert "hotkey2" not in api.get_options() and "hotkey2" not in d.SETTINGS
assert api.set_setting("target", "general") and d.SETTINGS["target"] == "general"
d.SETTINGS["target"] = "claude_code"

# history + stats
now = __import__("datetime").datetime.now()
rows = [{"ts": now.isoformat(timespec="seconds"), "kind": "dictate", "raw": "um hi", "text": "Hi there friend.", "secs": 60, "latency": 0.8, "cost": 0},
        {"ts": (now - __import__("datetime").timedelta(days=10)).isoformat(timespec="seconds"), "kind": "dictate", "raw": "", "text": "old one", "secs": 30, "latency": 1.2, "cost": 0}]
with open(d.HISTORY, "w", encoding="utf-8") as f:
    for r in rows: f.write(json.dumps(r) + "\n")
st = api.get_stats()
assert st["today"] == 1 and st["week"] == 1 and st["words_week"] == 3 and st["wpm"] == 3 and st["latency"] == 0.8, st
assert api.clear_history() and api.get_history() == []

# ---- revision loop with mocked models ----
scores = iter([5, 7, 9, 9, 9])  # v1, v2, v3, re-score of edited v3, v4
def fake_judge(text):
    n = next(scores)
    return {"score": n, "criteria": {k: 4 for k in ("clarity", "context", "task", "constraints", "success_criteria", "format")},
            "gaps": ["g"], "improved": f"{text} +better", "by": "test-judge", "cost": 0.01, "note": ""}
d.run_judge = fake_judge
real_engineer = d.engineer  # the learning checks below need the real one
d.engineer = lambda raw, draft=None: ((f"REFINED[{draft}]<-{raw}" if draft else f"ENGINEERED<-{raw}"), "test-engine", 0.02)
def wait():
    for _ in range(100):
        if not d._lab["busy"]: return
        time.sleep(0.02)
    raise AssertionError("lab op did not finish")

def apply_and_score():
    before = api.get_lab()
    assert before['pending'] and before['pending']['source_text'] == before['text']
    assert api.apply_candidate(before['pending']['id'])
    assert api.get_lab()['text'] == before['pending']['text']
    assert api.score(); wait()

# typing/pasting creates v1; Improve scores v1, applies the rewrite as v2, scores v2
api.new_draft(); api.set_text("make me a website")
L = api.get_lab(); assert len(L["revs"]) == 1 and L["revs"][0]["kind"] == "typed" and L["score"] is None
assert api.improve(); wait(); apply_and_score(); L = api.get_lab()
assert [r["score"] for r in L["revs"]] == [5, 7] and L["cur"] == 1 and L["text"] == "make me a website +better", L
assert L["prev_score"] == 5 and L["score"]["score"] == 7, L
# second round uses the existing score (no re-score): v3 = v2's improved, scored 9
assert api.improve(); wait(); apply_and_score(); L = api.get_lab()
assert [r["score"] for r in L["revs"]] == [5, 7, 9] and L["text"].endswith("+better +better"), L
# hand edit marks the score stale; Improve then re-scores first
api.set_text(L["text"] + " edited"); L = api.get_lab(); assert L["stale"] and L["score"]["score"] == 9
assert api.improve(); wait(); apply_and_score(); L = api.get_lab()
assert L["revs"][2]["score"] == 9 and not L["revs"][2]["stale"] and len(L["revs"]) == 4, L  # re-scored (9), then v4 from its rewrite
# composer: with a draft present it's a refine instruction; select an old rev and refine from there
scores = iter([6])
assert api.select_rev(0) and api.get_lab()["text"] == "make me a website"
assert api.submit("add a test command"); wait(); apply_and_score(); L = api.get_lab()
assert L["revs"][-1]["kind"] == "refine" and L["text"] == "REFINED[make me a website]<-add a test command" and L["score"]["score"] == 6, L
# composer on an empty lab engineers from the description
scores = iter([8]); api.new_draft()
assert api.submit("build me a thing"); wait(); apply_and_score(); L = api.get_lab()
assert L["revs"][0]["kind"] == "engineer" and L["text"] == "ENGINEERED<-build me a thing" and L["score"]["score"] == 8, L
# rebuild
scores = iter([8]); assert api.rebuild(); wait(); apply_and_score(); L = api.get_lab(); assert L["revs"][-1]["kind"] == "rebuild" and L["text"].startswith("ENGINEERED<-ENGINEERED"), L
# busy guard: two ops can't overlap
d._lab["busy"] = "scoring"; assert not api.improve(); d._lab["busy"] = ""
# judge returns no change -> friendly error, no new revision
scores = iter([9]); d.run_judge = lambda t: {"score": 9, "criteria": {k: 5 for k in d.CRITERION_HINT}, "gaps": [], "improved": t, "by": "j", "cost": 0, "note": ""}
n = len(d._lab["revs"]); api.set_text("final prompt"); assert api.improve(); wait(); L = api.get_lab()
assert len(L["revs"]) == n and "already as strong" in L["error"], L

# favorites
assert api.save_favorite("Thing", "Goal: build me a thing") and api.get_favorites()[0]["name"] == "Thing"
assert api.get_lab()["fav"] == "Thing"
api.load_favorite("Thing"); L = api.get_lab(); assert L["revs"] and L["revs"][0]["kind"] == "favorite" and L["text"] == "Goal: build me a thing"
d.bump_favorite("Thing"); assert api.get_favorites()[0]["uses"] == 1
assert api.delete_favorite("Thing") and api.get_favorites() == []

# paste_draft hides the window, then pastes on a thread; events are one-shot
calls = []
d._win = types.SimpleNamespace(hide=lambda: calls.append("hide"), show=lambda: calls.append("show"), restore=lambda: None)
d._window_target = 123
d.paste = lambda t, target=None: (calls.append(("paste", t)) or {"status": "pasted"})
assert api.paste_draft("  Goal: go  "); time.sleep(0.6)
assert calls == ["hide", ("paste", "Goal: go")], calls
assert api.get_state()["event"] == "pasted" and api.get_state()["event"] == ""
d.capture_target = lambda: None
d.show_window("lab"); assert api.get_state()["event"] == "show_lab" and "show" in calls
assert "prompt lab" in api.diagnostics()
print("api: all checks passed")

# ---- layer-1 learning: vocabulary suggestions, edit capture, kept-prompt preferences, reset ----
# This phase starts with no saved observations from favorites/paste checks.
d.LEARNED_FILE = os.path.join(tmp, "learning-phase.json"); d._learned = None
d.SETTINGS["vocab"] = "Roblox"; d.SETTINGS["learn"] = True
with open(d.HISTORY, "w", encoding="utf-8") as f:
    for t in ["Ship the Suno export today.", "Then push Suno to Roblox and tell Marcus.", "Marcus said the Expo build is fine.", "I think Expo is ready."]:
        f.write(json.dumps({"ts": now.isoformat(timespec="seconds"), "kind": "dictate", "raw": "", "text": t, "secs": 2, "latency": 0.5, "cost": 0}) + "\n")
sug = api.get_vocab_suggestions()
assert sug == ["Expo", "Suno"], sug   # recurring mid-sentence capitals; "Marcus" only counts once (its other use starts a sentence)
assert "Roblox" not in sug and "Ship" not in sug and "I" not in sug, sug
assert api.dismiss_vocab("Expo") and "Expo" not in api.get_vocab_suggestions()
assert api.add_vocab("Suno") and "Suno" in d.vocab_words() and "Suno" not in api.get_vocab_suggestions()
# hand corrections to generated text become suggestions (and only on generated revisions)
api.new_draft(); d._lab_add(d._lab_rev("Goal: fix the sono importer.\n\nAssumptions:\nnone", "engineer", "x", 0))
api.set_text("Goal: fix the Suno importer.\n\nnone")           # word fixed + "Assumptions:" heading removed
assert d.learned()["replacements"] == {"Suno": 1} and d.learned()["removed_headings"] == {"Assumptions": 1}, d.learned()
api.new_draft(); api.set_text("my own typed text"); api.set_text("my own typed txt")  # typed revs are not learned from
assert d.learned()["replacements"] == {"Suno": 1}
# kept prompts feed the preferences sentence once there are enough of them
assert api.get_learned()["lines"] == ["Corrected by hand: Suno."] and api.get_learned()["snippet"] == ""  # corrections show, but no prompt hint yet
for i in range(3):
    api.new_draft(); d._lab_add(d._lab_rev("<context>\nx\n</context>\n" + "word " * 100, "improve", "x", 0))
    d._lab_cur()["score"] = {"score": 7, "criteria": {"clarity": 5, "context": 4, "task": 4, "constraints": 4, "success_criteria": 2, "format": 5}, "gaps": [], "improved": "", "by": "j", "cost": 0}
    api.lab_kept()
d.learned()["removed_headings"]["Assumptions"] = 2
P = api.get_learned()
assert P["kept"] == 3 and any("XML-style" in l for l in P["lines"]) and any("Assumptions" in l for l in P["lines"]), P
assert "roughly 103 words" in P["snippet"] and "omit a “Assumptions” section" in P["snippet"], P["snippet"]
assert "success criteria" not in P["snippet"]  # only 3 samples; the criterion hint needs 5
for _ in range(2): api.lab_kept()
assert "check the reader can run" in api.get_learned()["snippet"], api.get_learned()
# the snippet is injected into the engineer prompt only while learning is on
seen = {}
d._claude = lambda model, system, user, schema=None: (seen.update(sys=system, user=user) or "ok", 0.0)
d.SETTINGS.update(engine="anthropic", cloud_enabled=True); real_engineer("hi")
assert "Preferences learned" not in seen["sys"] and "Preferences learned" in json.loads(seen["user"])["learned_style_preferences"]
seen.clear(); d.SETTINGS["learn"] = False; real_engineer("hi"); assert "learned_style_preferences" not in json.loads(seen["user"])
d.SETTINGS["learn"] = True; d.SETTINGS["engine"] = "local"
assert api.reset_learned() and api.get_learned()["kept"] == 0 and api.get_learned()["lines"] == []
print("learning: all checks passed")
