@echo off
setlocal
title Local Dictation - Automated Setup
echo ============================================================
echo  Local Dictation Engine - Setup (RTX 4060 / CUDA 12.x)
echo ============================================================
echo.

:: ---------- 1. Verify NVIDIA GPU ----------
echo [1/4] Checking for NVIDIA GPU...
nvidia-smi >nul 2>&1
if errorlevel 1 (
    echo   ERROR: nvidia-smi not found. Install the NVIDIA driver first:
    echo   https://www.nvidia.com/Download/index.aspx
    pause & exit /b 1
)
nvidia-smi --query-gpu=name,driver_version --format=csv,noheader
echo   GPU detected OK.
echo.

:: ---------- 2. CUDA / cuDNN guidance ----------
echo [2/4] CUDA Toolkit / cuDNN check...
where nvcc >nul 2>&1
if errorlevel 1 (
    echo   CUDA Toolkit not detected on PATH. faster-whisper needs the
    echo   CUDA 12.x runtime + cuDNN 9 DLLs. Download here:
    echo     CUDA Toolkit 12.x: https://developer.nvidia.com/cuda-downloads
    echo     cuDNN 9 for CUDA 12: https://developer.nvidia.com/cudnn-downloads
    echo   NOTE: pip package below also bundles the needed CUDA DLLs, so
    echo   you can usually skip the manual install. Continuing...
) else (
    for /f "tokens=*" %%v in ('nvcc --version ^| findstr /i "release"') do echo   Found: %%v
)
echo.

:: ---------- 3. Python dependencies ----------
echo [3/4] Installing Python dependencies...
py --version >nul 2>&1
if errorlevel 1 (
    echo   ERROR: Python not found on PATH. Install from https://www.python.org/downloads/
    pause & exit /b 1
)
py -m pip install --upgrade pip
:: nvidia pip wheels supply CUDA 12 / cuDNN 9 DLLs so faster-whisper's
:: ctranslate2 backend finds them without a system-wide CUDA install.
py -m pip install -r requirements.txt
if errorlevel 1 (
    echo   ERROR: pip install failed. See output above.
    pause & exit /b 1
)
echo   Python dependencies installed OK.
echo.

:: ---------- 4. Ollama + qwen2.5:7b ----------
echo [4/4] Checking Ollama...
where ollama >nul 2>&1
if errorlevel 1 (
    echo   Ollama not installed. Download the Windows installer:
    echo     https://ollama.com/download/windows
    echo   Install it, then re-run this script to pull qwen2.5:7b.
    pause & exit /b 1
)
echo   Ollama found. Pulling qwen2.5:7b model (skips if already present)...
ollama pull qwen2.5:7b
if errorlevel 1 (
    echo   WARNING: pull failed. Make sure the Ollama service is running
    echo   (start the Ollama app or run "ollama serve"), then re-run.
    pause & exit /b 1
)
echo.
echo ============================================================
echo  Setup complete. Launch the app with:   run.bat
echo ============================================================
pause
