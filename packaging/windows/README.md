# Listen Windows installer candidate

Install the MSI and open **Listen** from the Start menu. The first launch downloads the English speech model (about 730 MB). Listen also installs Microsoft WebView2 if Windows doesn't already have it. You need an internet connection for that first launch. Afterward, default English dictation runs offline.

The MSI includes a private x64 CPython 3.14 and Listen’s locked Python packages. It does not register them as system Python. The installer uses the CPU packages for the default Parakeet engine. Existing source installations can still use the NVIDIA packages in the main requirements file.

Uninstall removes the installer-owned app, Python, and speech engine. It keeps settings, History, and the separately downloaded speech model so reinstalling can reuse them. The MSI is unsigned.

## Build and check

Run on Windows from an explicit source revision. The installer build fetches and checks the pinned [Python runtime](https://www.python.org/ftp/python/index-windows.json), [Python package hashes](requirements.lock), and [transcribe.cpp archive](https://github.com/handy-computer/transcribe.cpp/releases/tag/v0.4.2). Their supplied license files travel with the runtime. The separately fetched model comes from the pinned [Parakeet model repository](https://huggingface.co/handy-computer/parakeet-unified-en-0.6b-gguf/tree/7e948f21b7bdbac698d3318db9d350f1096f3b6c) and uses the [NVIDIA model terms](https://huggingface.co/nvidia/parakeet-unified-en-0.6b).

```powershell
py -3.13 -B packaging/windows/build_installer.py --repo . --revision HEAD --output Listen-Windows-x64.msi
```

The test workflow uses a fresh GitHub-hosted Windows VM and an empty test profile. It runs the installed app with system Python removed from PATH, downloads and verifies the model, recognizes a generated audio sample, and checks uninstall and upgrade. It does not test a physical microphone.
