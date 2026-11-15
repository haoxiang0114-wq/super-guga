@echo off
rem ===== Guga environment self-check =====
rem ASCII-only output; the report itself is printed by tools\verify_environment.py in UTF-8.
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title Guga Penguin - Environment Check
set "PY=%~dp0.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [info] No .venv yet. Run the one-click launcher once first.
    pause
    exit /b 1
)

echo Running environment check, please wait...
echo.
"%PY%" "%~dp0tools\verify_environment.py" %*
echo.
pause
