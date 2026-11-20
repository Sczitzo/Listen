param(
    [Parameter(Mandatory)] [string] $Installer,
    [Parameter(Mandatory)] [string] $Work,
    [string] $LegacyInstaller,
    [switch] $RunDesktop
)

$ErrorActionPreference = 'Stop'
$PSNativeCommandUseErrorActionPreference = $true
$installerPath = (Resolve-Path -LiteralPath $Installer).Path
$workPath = (New-Item -ItemType Directory -Path $Work).FullName
$app = Join-Path $workPath 'Installed Listen'
$menu = Join-Path $workPath 'Start menu'
$system = Join-Path $env:SystemRoot 'System32'
$env:Path = "$system;$env:SystemRoot"
$env:USERPROFILE = Join-Path $workPath 'Empty profile'
$env:LOCALAPPDATA = Join-Path $env:USERPROFILE 'AppData/Local'
$env:APPDATA = Join-Path $env:USERPROFILE 'AppData/Roaming'
$env:PYTHONHOME = Join-Path $workPath 'Unavailable global Python'
$env:PYTHONPATH = $env:PYTHONHOME
if (Test-Path 'HKCU:/Software/Listen/Installer') { throw 'Use a clean test machine without an installed Listen MSI.' }

function Invoke-Installer($mode, $path, $log) {
    $argsForMsi = @($mode, ('"{0}"' -f $path), '/qn', '/norestart', '/L*V', ('"{0}"' -f $log))
    if ($mode -eq '/i') {
        $argsForMsi += ('INSTALLDIR="{0}"' -f $app), ('LISTENMENUDIR="{0}"' -f $menu)
    }
    $process = Start-Process -FilePath (Join-Path $system 'msiexec.exe') -ArgumentList $argsForMsi -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) { throw "Windows Installer exit code: $($process.ExitCode). See $log." }
}

if ($LegacyInstaller) {
    $LegacyInstaller = (Resolve-Path -LiteralPath $LegacyInstaller).Path
    Invoke-Installer '/i' $LegacyInstaller (Join-Path $workPath 'legacy.log')
    Set-Content (Join-Path $app 'settings.json') '{"hotkey":"right_ctrl","speech_model":"parakeet-unified-en-0.6b"}'
    Set-Content (Join-Path $app 'history.jsonl') '{"text":"Sample saved dictation"}'
}

Invoke-Installer '/i' $installerPath (Join-Path $workPath 'install.log')
$launcher = Join-Path $app 'Listen.exe'
$python = Join-Path $app 'python/python.exe'
$setup = Join-Path $app 'packaged_setup.py'
$history = Join-Path $app 'history.jsonl'
$settings = Join-Path $app 'settings.json'
$shortcut = Join-Path $menu 'Listen.lnk'
$appProcess = $null
try {
    if (-not (Test-Path -LiteralPath $shortcut)) { throw 'Start menu shortcut missing.' }
    $shell = New-Object -ComObject WScript.Shell
    if ($shell.CreateShortcut($shortcut).TargetPath -ne $launcher) { throw 'Start menu shortcut points elsewhere.' }
    if ($LegacyInstaller) {
        if ((Get-Content -LiteralPath $history -Raw).Trim() -ne '{"text":"Sample saved dictation"}' -or
            (Get-Content -LiteralPath $settings -Raw).Trim() -ne '{"hotkey":"right_ctrl","speech_model":"parakeet-unified-en-0.6b"}') {
            throw 'Upgrade changed user files.'
        }
        $legacyCode = [guid]::Parse('1e06cba0-0bb3-59f9-adc5-fe0d9ce9347e')
        $windowsInstaller = New-Object -ComObject WindowsInstaller.Installer
        if ($windowsInstaller.ProductState($legacyCode.ToString('B').ToUpperInvariant()) -eq 5) {
            throw 'Previous Listen installer remained registered.'
        }
    }

    $check = Start-Process -FilePath $launcher -ArgumentList '--check' -Wait -PassThru -WindowStyle Hidden
    if ($check.ExitCode -ne 0) { throw 'Bundled Python or runtime could not load.' }
    Write-Output 'Bundled Python and Tk passed, with system Python excluded from PATH.'
    & $python -I -B (Join-Path $PSScriptRoot 'verify_setup.py')
    if (-not $LegacyInstaller) {
    $model = & $python -I -B -c 'from parakeet_runtime import model_path; print(model_path())'
    if (Test-Path -LiteralPath $model) { throw 'Speech model was already installed in the test profile.' }

    if ($RunDesktop) {
        if (-not $env:GITHUB_ACTIONS) { throw 'The full desktop smoke requires an isolated Windows runner.' }
        $pythonw = Join-Path $app 'python/pythonw.exe'
        $launch = Start-Process -FilePath $launcher -PassThru
        if (-not $launch.WaitForExit(10000) -or $launch.ExitCode -ne 0) { throw 'The installed launcher failed.' }
        $deadline = (Get-Date).AddMinutes(15)
        $desktopLog = Join-Path $app 'dictation.log'
        $sawSetup = $false
        do {
            Start-Sleep -Milliseconds 500
            $appProcess = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
                Where-Object { $_.ExecutablePath -eq $pythonw } | Select-Object -First 1
            if ($appProcess -and -not $sawSetup) {
                $sawSetup = (Get-Process -Id $appProcess.ProcessId -ErrorAction SilentlyContinue).MainWindowTitle -eq 'Set up Listen'
            }
            $bridgeReady = (Test-Path -LiteralPath $desktopLog) -and
                ((Get-Content -LiteralPath $desktopLog -Raw) -match '\[page\].*"bridge"\s*:\s*true')
        } until ($bridgeReady -or (Get-Date) -gt $deadline)
        if (-not $appProcess -or -not $sawSetup -or -not $bridgeReady) {
            throw 'The installed launcher did not show first-run setup and reach desktop readiness.'
        }
        Write-Output 'Start menu launcher opened first-run setup and then reached desktop and speech readiness.'
    } else {
        & $python -I -B $setup --prepare
    }
    $modelHash = (Get-FileHash -LiteralPath $model -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($modelHash -ne '4b50b6dd862bf6e346929aaf4f5eaacec003bfa3f56462d6c874b41ef2f38795') {
        throw 'First-run model does not match the pinned file.'
    }
    & $python -I -B $setup --prepare
    Write-Output 'First run downloaded the speech model and reused it on the next run.'

    Add-Type -AssemblyName System.Speech
    $wav = Join-Path $workPath 'synthetic.wav'
    $voice = [System.Speech.Synthesis.SpeechSynthesizer]::new()
    try {
        $format = [System.Speech.AudioFormat.SpeechAudioFormatInfo]::new(16000,
            [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,
            [System.Speech.AudioFormat.AudioChannel]::Mono)
        $voice.SetOutputToWaveFile($wav, $format)
        $voice.Speak('The quick brown fox jumps over the lazy dog.')
    } finally { $voice.Dispose() }
    @'
import sys
import wave
import numpy as np
from parakeet_runtime import MODEL_NAME
from speech_process import load_speech_process

with wave.open(sys.argv[1], 'rb') as source:
    assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
    audio = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32) / 32768
decoder = load_speech_process(MODEL_NAME, preferred_device='cpu', startup_timeout=120)
try:
    segments, _ = decoder.transcribe(audio)
    text = ' '.join(segment.text for segment in segments)
    assert 'quick brown fox' in text.lower() and 'lazy dog' in text.lower(), text
    print('Synthetic speech recognized:', text)
finally:
    decoder.close()
'@ | & $python -I -B - $wav

    }
    Set-Content -LiteralPath $history -Value '{"text":"Sample saved dictation"}'
} finally {
    if ($appProcess) {
        Stop-Process -Id $appProcess.ProcessId
        Wait-Process -Id $appProcess.ProcessId -Timeout 20 -ErrorAction SilentlyContinue
    }
    Invoke-Installer '/x' $installerPath (Join-Path $workPath 'uninstall.log')
    if ((Test-Path -LiteralPath $shortcut) -or (Test-Path -LiteralPath $launcher) -or
        (Test-Path -LiteralPath $python) -or (Test-Path -LiteralPath (Join-Path $app 'speech-runtime/transcribe.dll'))) {
        throw 'Uninstall left installer-owned files behind.'
    }
}
if ((Get-Content -LiteralPath $history -Raw).Trim() -ne '{"text":"Sample saved dictation"}') {
    throw 'Uninstall did not preserve sample History.'
}
Write-Output 'Uninstall removed installed files and preserved sample History.'
