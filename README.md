# Listen

Windows dictation with local speech recognition, optional Ollama text cleanup,
a floating recording panel, and a writing workspace.

Created by Sczitzo. Free to use under the MIT License: keep the copyright
notice and credit when you reuse it.

## Development snapshot — November 18, 2026 (build 20261119-listen19)

The Warm charcoal / Humanist interface follows the approved Quiet Instrument and
Writer's Desk composition. The recording card uses a titled continuous border,
a larger destination icon, a quiet Cancel action and optional gentle animation.
Finish remains available through the dictation key and main window.

Speech recognition has a persisted dropdown of supported installed caches,
with Parakeet Unified English 0.6B Q8_0 as the default. Switching is refused
during active work. Parakeet is English-only and does not use Whisper vocabulary
hints. Recognition and AI cleanup use separate models.

The Ollama menu reflects the installed inventory and preserves the saved choice.
Its scores and recommendation come from a historical November 9 cleanup test:
39 synthetic samples per tested model, scored by app acceptance and expected-term
checks. They are not a current formatter comparison or Prompt Lab quality scores.
Untested models are identified. Refresh does not download or run a model.

Local AI cleanup can supply validated lists and section layout. Unsafe changes
fall back conservatively. A saved text-model change starts a background readiness
check; a newer selection follows any older check already in progress. Model loading has a separate 60-second background
budget; final cleanup keeps its 15-second limit and 8192-token context. A model
inventory response alone is not readiness. This is source, not an installer or
a claim of natural-speech or real destination-delivery acceptance.

## Running on an already configured Windows machine

Use Python 3.13 with the packages in requirements.txt. Ollama must be available
locally for cleanup. Optional Claude operations use ANTHROPIC_API_KEY from the
environment and may incur charges; no credentials are included.

Parakeet requires the validated local model cache and transcribe 0.4.2 native
runtime. They are not included or downloaded here. parakeet_runtime.py specifies
the expected locations, revision and integrity hash. Cached Whisper alternatives
use the existing faster-whisper runtime. The dropdown does not install models.

setup.bat is the existing Whisper-oriented helper: it installs packages and
downloads Qwen2.5:7b but does not provision Parakeet. Review those effects before
using it. This update did not install or download anything. On a configured
machine, use run.bat or launch_listen.pyw; the latter opens the main window and
reuses an existing instance.

## Verification

```powershell
python tests/run_tests.py --no-devices
```

The 72 selected suites use disposable app data and block real network, microphone,
physical input and clipboard access. They do not establish live recognition or
delivery quality. The separate Windows card harness is opt-in and uses an owned
sample editor. See tests/README.md.

## Publication contents

Application source, UI/icons, brand source, configured regression tests,
launch/setup scripts and one code-only baseline archive required by latency tests.
Local development history, recordings, model/native binaries, drafts, settings,
credentials, owner notes, screenshots and runtime logs are excluded.
Licensed Mixkit WAV files are omitted; synthesized fallback sounds remain.

## License

MIT. See LICENSE.

The Lantern recording card now follows microphone volume with five bars and a gently wobbling circle. A soft click-through shadow adds depth while text and typing focus stay steady.

The live preview uses normal-size reading text, a smaller Listening label and tighter margins.
