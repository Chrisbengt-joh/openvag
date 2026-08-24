@echo off
title VAGDIAG
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python was not found on this computer.
    echo.
    echo   Install it from https://www.python.org/downloads/
    echo   and tick "Add python.exe to PATH" in the installer.
    echo.
    pause
    exit /b 1
)

python -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo Installing the serial port library, one moment...
    python -m pip install --quiet pyserial
)

start "" pythonw -m vagdiag --gui
if errorlevel 1 (
    python -m vagdiag --gui
    pause
)
