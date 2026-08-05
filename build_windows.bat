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

set VPY=build-venv\Scripts\python.exe

echo === Installing dependencies (flask, waitress, pystray, pillow, pyinstaller)...
REM pip.exe cannot overwrite itself while it is running, so Windows pip refuses to
REM self-upgrade when called as "pip". Going through the interpreter (-m pip) works.
REM Non-fatal either way: whatever pip the venv ships with installs the rest fine.
"%VPY%" -m pip install --quiet --upgrade pip >nul 2>nul
"%VPY%" -m pip install --quiet flask waitress pystray pillow pyinstaller python-tds certifi cryptography || (echo pip install failed & pause & exit /b 1)

echo === Installing pyodbc (for Windows-auth SQL Server; optional)...
"%VPY%" -m pip install --quiet pyodbc || echo    (pyodbc unavailable - SQL-login auth will still work via python-tds)

echo === Building AscomPingMonitor.exe (takes a minute)...
build-venv\Scripts\pyinstaller.exe --noconfirm --clean --onefile --noconsole ^
    --name AscomPingMonitor ^
    --icon "static\branding\favicon.ico" ^
    --add-data "templates;templates" ^
    --add-data "static;static" ^
    --add-data "pingmon\data;pingmon\data" ^
    --hidden-import waitress ^
    --hidden-import pystray._win32 ^
    --hidden-import pyodbc ^
    --hidden-import pytds ^
    --collect-submodules pytds ^
    --hidden-import certifi ^
    --collect-data certifi ^
    --hidden-import cryptography ^
    --collect-submodules cryptography ^
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
