# Listen

Windows dictation with local speech recognition, optional Ollama cleanup,
a floating recording panel, and a writing workspace.

## Work-in-progress snapshot — November 16, 2026

This source backup includes the Warm charcoal / Humanist appearance, Parakeet
integration, installed-model menu repairs and the current cleanup repair.
It is not a release or a standalone installer.

**Cleanup remains unresolved.** A real local trial loaded Qwen2.5:7b in 43.201
seconds, but the following synthetic cleanup exceeded its 15-second limit.
The repair uses a smaller prompt batch, a separate 60-second background loading
limit and guarded readiness reporting. This does not prove a completed fix.
Recognized text falls back to its original form on failure. This final snapshot
has not been activated in the regular desktop app.

All 69 guarded device-free regression suites passed on this published source,
including the new loading/status checks. The interface script also passed
node --check. Earlier startup-test timeouts occurred during tool failures; the
passing rerun does not establish their cause. Natural-speech accuracy and
physical text delivery have not been accepted in this update.

## Running on an already configured Windows machine

Use Python 3.13 with the packages in requirements.txt. Ollama must be available
locally for cleanup. Optional Claude operations use ANTHROPIC_API_KEY from the
environment and may incur charges; no credentials are included.

The selected speech backend requires the existing validated Parakeet Unified
English 0.6B Q8_0 model cache and transcribe 0.4.2 native runtime. They are not
included or downloaded here. parakeet_runtime.py specifies their exact expected
locations, revision and integrity hash. Parakeet is English-only and does not
use Whisper recognition vocabulary hints.

setup.bat is the existing Whisper-oriented helper: it installs packages and
downloads Qwen2.5:7b but does not provision Parakeet. Review those effects before
using it. This publication did not install or download anything. On an already
configured machine, launch with run.bat.

## Verification

```powershell
python tests/run_tests.py --no-devices
```

This selects isolated, unpaid checks with network, microphone, physical input
and clipboard access blocked. It does not establish real recognition or delivery
quality. See tests/README.md.

## Publication contents

Application source, UI/icons, brand source, configured regression tests,
launch/setup scripts and one code-only baseline archive required by latency tests.
Local git history, recordings, model/native binaries, drafts, settings,
credentials, local notes, screenshots and runtime logs are excluded.
Licensed Mixkit WAV files are omitted; synthesized fallback sounds remain.
No source license is granted by this backup.
