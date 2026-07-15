@echo off
REM ============================================================
REM  Ascom Ping Monitor - Windows exe builder
REM
REM  Requirements: Python 3.9+ from python.org installed with
REM  "Add python.exe to PATH" ticked. Then just double-click me.
REM  Result: dist\AscomPingMonitor.exe (single file, no install)
REM ============================================================
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERROR: Python not found. Install it from https://python.org
        echo        and tick "Add python.exe to PATH" during setup.
        pause
        exit /b 1
    )
    set PY=python
) else (
    set PY=py -3
)

echo === Creating build environment...
%PY% -m venv build-venv || (echo venv creation failed & pause & exit /b 1)
call build-venv\Scripts\activate.bat

echo === Installing dependencies (flask, waitress, pystray, pillow, pyinstaller)...
pip install --quiet --upgrade pip
pip install --quiet flask waitress pystray pillow pyinstaller || (echo pip install failed & pause & exit /b 1)

echo === Building AscomPingMonitor.exe (takes a minute)...
pyinstaller --noconfirm --clean --onefile --noconsole ^
    --name AscomPingMonitor ^
    --icon "static\branding\favicon.ico" ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --add-data "pingmon\data;pingmon\data" ^
    --hidden-import waitress ^
    --hidden-import pystray._win32 ^
    run.py
if errorlevel 1 (echo BUILD FAILED & pause & exit /b 1)

echo.
echo ============================================================
echo   Done!  Your exe is:  dist\AscomPingMonitor.exe
echo.
echo   - Double-click it to run. Your browser opens automatically
echo     at http://localhost:8080  (login: ascom / ascom!12345)
echo   - Data + logs live in C:\ProgramData\AscomPingMonitor
echo   - To start it with Windows and open the firewall for
echo     other devices, right-click install-startup.bat and
echo     "Run as administrator" (copy it next to the exe first).
echo ============================================================
pause
