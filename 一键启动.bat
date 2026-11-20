@echo off
rem ===== Guga one-click launcher =====
rem Output stays ASCII on purpose: some Windows code pages break on Chinese echo text.
rem Chinese manual: 使用说明.md
chcp 65001 >nul
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Guga Penguin - One Click Launch

set "ROOT=%~dp0"
set "VENV=%ROOT%.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"

rem ---------- 1. base interpreter (3.10+, real python only) ----------
if not exist "%PY%" call :make_venv
if not exist "%PY%" goto :fatal

rem ---------- 2. dependency check ----------
"%PY%" -c "import PyQt5, av, PIL, numpy, requests, psutil, sounddevice, vosk, clr, edge_tts, win32api, playwright, onnxruntime" >nul 2>&1
if errorlevel 1 call :install_deps

rem ---------- 3. config file ----------
if not exist "%ROOT%ai_config.json" (
    copy /Y "%ROOT%ai_config.example.json" "%ROOT%ai_config.json" >nul 2>&1
    echo [config] ai_config.json created.
    echo          Fill in your API keys to enable chat, vision and realtime voice.
    echo.
)

rem ---------- 4. launch ----------
if /i "%~1"=="--check" (
    echo [check] Environment ready. --check given, pet not started.
    exit /b 0
)

echo [start] Opening Guga desktop pet...
if exist "%PYW%" (
    start "" "%PYW%" "%ROOT%launcher.py"
) else (
    start "" "%PY%" "%ROOT%launcher.py"
)

rem ---------- 5. verify it survived the first seconds ----------
ping -n 5 127.0.0.1 >nul 2>&1
tasklist /fi "imagename eq pythonw.exe" 2>nul | find /i "pythonw.exe" >nul
if errorlevel 1 (
    echo.
    echo [warn] Pet process not found, startup probably failed.
    if exist "%ROOT%startup.log" (
        echo ----- startup.log last 30 lines -----
        powershell -NoProfile -Command "Get-Content -Path '%ROOT%startup.log' -Tail 30 -Encoding UTF8"
        echo -------------------------------------
    )
    echo Run the environment self-check shortcut to see what is missing.
    pause
)
exit /b 0

rem ---------- create virtual env ----------
:make_venv
echo [env] First run: preparing an isolated Python environment, once only...
set "BASEPY="
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
) do (
    if not defined BASEPY if exist %%P %%P -c "import sys" >nul 2>&1 && set "BASEPY=%%~P"
)
if not defined BASEPY (
    for %%C in (py python python3) do (
        if not defined BASEPY (
            %%C -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1 && set "BASEPY=%%C"
        )
    )
)
if not defined BASEPY (
    echo [error] Python 3.10+ not found.
    echo         Install from https://www.python.org/downloads/ and tick "Add Python to PATH".
    exit /b 1
)
echo [env] Interpreter: !BASEPY!
!BASEPY! -m venv "%VENV%"
if errorlevel 1 (
    echo [error] venv creation failed. Check free disk space and antivirus blocking.
    exit /b 1
)
call :install_deps
exit /b 0

rem ---------- install dependencies ----------
:install_deps
echo [deps] Installing dependencies, first run takes a few minutes...
"%PY%" -m pip install --disable-pip-version-check -r "%ROOT%requirements.txt"
if errorlevel 1 (
    echo [deps] Default index failed, retrying with Tsinghua mirror...
    "%PY%" -m pip install --disable-pip-version-check -r "%ROOT%requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple
)
"%PY%" -c "import yaml" >nul 2>&1
if errorlevel 1 "%PY%" -m pip install --disable-pip-version-check PyYAML >nul 2>&1
exit /b 0

:fatal
echo [error] Environment setup failed, see messages above.
pause
exit /b 1
