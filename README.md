# Listen — Local speech-to-text and voice typing for Windows

Listen is a Windows dictation app that turns speech into text and types it into the app you were using when you started recording. Speech recognition runs on your computer with a supported local model. You can also tidy dictation with Ollama, browse past dictations by date or destination app, and write or review prompts in Prompt Lab.

**[Download for Windows](https://github.com/Sczitzo/Listen/releases/tag/v2026.11.20)** · [Screenshots](#screenshots) · [System requirements](#system-requirements)

## What you can do

- **Dictate into other Windows apps.** Use the default **Right Ctrl** shortcut or choose a shortcut in Settings. Hold the key and release to finish, or tap to start and tap again to finish. The floating panel shows recording status and can display a live transcript.
- **Clean up text locally.** Ollama can tidy grammar and remove filler words. Listen keeps recognition and cleanup separate, so you can choose their models independently.
- **Find past dictations.** Search History or group entries by day or destination app. Recent entries show the app name and icon when Listen can identify the destination. Copy the cleaned text or the original transcript.
- **Write and review AI prompts.** Prompt Lab keeps draft revisions, shows feedback and lets you choose a local model or an optional cloud provider for each stage.

## System requirements

| Requirement | What you need |
| --- | --- |
| Windows | A 64-bit Windows PC and a microphone. The release installer is for x64. |
| Desktop runtime | Python 3.13 x64, the packages pinned in [`requirements.txt`](requirements.txt), and Microsoft Edge WebView2 Runtime. [Python for Windows](https://www.python.org/downloads/windows/) · [Microsoft Edge WebView2](https://developer.microsoft.com/en-us/microsoft-edge/webview2/) |
| Local speech recognition | The default is Parakeet Unified English 0.6B Q8_0. It needs the matching local model cache and `transcribe.cpp` v0.4.2 runtime. Settings can list supported Whisper models already cached on your computer. [Pinned Parakeet model](https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf/tree/7e948f21b7bdbac698d3318db9d350f1096f3b6c) · [Native runtime release](https://github.com/handy-computer/transcribe.cpp/releases/tag/v0.4.2) · [Cache and runtime paths](parakeet_runtime.py) |
| Optional AI features | Ollama with an installed text model for local cleanup or Prompt Lab. Cloud Prompt Lab stages require the provider’s API key and can incur charges. |

## Install Listen on Windows

1. Download **Listen-Windows-x64.msi** from the [Windows release](https://github.com/Sczitzo/Listen/releases/tag/v2026.11.20).
2. Run the MSI to install Listen for your Windows account and add its Start menu shortcut.
3. Install the desktop requirements above. Then use PowerShell to install Listen’s pinned Python packages into its own environment:

   ```powershell
   Set-Location "$env:LOCALAPPDATA\Programs\Listen"
   py -3.13 -m venv .venv
   & .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. For the default English speech model, put the downloaded model and DLLs in the locations below. An already configured Listen machine can reuse its existing speech cache.
5. Open **Listen** from the Windows Start menu. Once speech is ready, focus the app where you want your words to appear and use the dictation shortcut.

The MSI is a per-user installer for the app and shortcut. It’s unsigned. Python, WebView2, speech resources and optional AI services are separate prerequisites. The older [`setup.bat`](setup.bat) helper targets Whisper and downloads an Ollama model.

### Set up the default Parakeet speech model

- Download [`parakeet-unified-en-0.6b-Q8_0.gguf`](https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf/resolve/7e948f21b7bdbac698d3318db9d350f1096f3b6c/parakeet-unified-en-0.6b-Q8_0.gguf) and save it under `%USERPROFILE%\.cache\huggingface\hub\models--handy-computer--parakeet-unified-en-0.6b-gguf\snapshots\7e948f21b7bdbac698d3318db9d350f1096f3b6c\`. The expected SHA-256 is `4b50b6dd862bf6e346929aaf4f5eaacec003bfa3f56462d6c874b41ef2f38795`.
- Download the [Windows x64 CPU/Vulkan runtime archive](https://github.com/handy-computer/transcribe.cpp/releases/download/v0.4.2/transcribe-native-0.4.2-windows-x86_64-cpu-vulkan.tar.gz). Check SHA-256 `6094102cf57b404b050707ceb258b12ce647b92e801b5d46077214e5b030c8aa`, then extract the DLLs directly into `%USERPROFILE%\AppData\Local\Listen\runtimes\transcribe-0.4.2-cpu-vulkan\`. `transcribe.dll` and its sibling DLLs belong in the same folder.

To uninstall the app and shortcut, use **Settings → Apps → Installed apps → Listen**. Windows Installer leaves local history and settings in the Listen folder.

For source development on a configured machine, run [`run.bat`](run.bat) or open [`launch_listen.pyw`](launch_listen.pyw).

## Screenshots

These show the release interface with sample data.

### Dictate

![Listen Dictate tab for Windows, with a shortcut, recording controls, vocabulary, and recent dictations](docs/screenshots/dictate.png)

### History

![Listen dictation History grouped by destination app, with search, app icons, and copy controls](docs/screenshots/history.png)

### Prompt Lab

![Listen Prompt Lab with a draft, revision controls, and an inline prompt score](docs/screenshots/prompt-lab.png)

### Settings

![Listen Settings with a recording shortcut, microphone, transcript preview, and local model selections](docs/screenshots/settings.png)

The cleanup model scores in Settings come from a historical 39-sample cleanup test. Performance depends on the machine and task.

## Privacy and offline use

Listen processes speech on your computer. Dictation history, preferences, and working drafts are stored locally. Ollama can handle optional text cleanup and Prompt Lab stages on the same computer. If you enable paid cloud AI for Prompt Lab, the selected provider receives the input and context for the requests you choose to run. The destination app handles inserted text according to its own settings.

Use `ANTHROPIC_API_KEY` for Claude, `OPENAI_API_KEY` for OpenAI, or `GEMINI_API_KEY` or `GOOGLE_API_KEY` for Gemini. Keep API keys in your environment and out of screenshots, issue reports, and commits.

## Frequently asked questions

### Does Listen work offline?

Yes. Once the supported local speech runtime and model are installed, speech recognition can run offline. Optional cloud Prompt Lab requests require internet access. Initial downloads may also require an internet connection.

### Which language does this build recognize?

This build is configured for English dictation. The default Parakeet model is English-only. The vocabulary list can guide optional cleanup. Parakeet uses its own recognition vocabulary.

### Why does Listen say speech is unavailable?

Check **Settings → Speech to text & AI cleanup** and confirm the selected speech model and its runtime are installed. The model list shows supported local choices. Install the matching cache or choose an available model, then use **Retry startup** when Listen offers it.

### Can I use Listen without Ollama or a paid API key?

Yes. With a supported local speech model ready, basic dictation works without Ollama or a cloud account. Ollama enables local cleanup and local Prompt Lab processing. Provider keys are for optional cloud Prompt Lab requests.

## Verification

```powershell
python tests/run_tests.py --no-devices
```

The release source passed 75 device-free regression suites. They use disposable data and block real network calls, microphone access, physical input, and clipboard access. A separate Windows card harness is described in [the regression check guide](tests/README.md).

### Build the Windows installer from source

On a Windows build machine with Git, Python 3.13 and the Windows .NET Framework C# compiler, run this from the repository root:

```powershell
py -3.13 -B packaging/windows/build_installer.py --repo . --revision v2026.11.20 --output Listen-Windows-x64.msi
```

The build packages the tagged app files using Windows Installer and the Windows cabinet tool. It does not download models or copy any local app data.

## License

Created by Sczitzo and released under the [MIT License](LICENSE).
