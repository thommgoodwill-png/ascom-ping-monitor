@echo off
REM ============================================================
REM  Ascom Ping Monitor - run at Windows startup + open firewall
REM  Right-click this file and choose "Run as administrator".
REM  Keep it in the SAME folder as AscomPingMonitor.exe.
REM ============================================================
cd /d "%~dp0"

if not exist "%~dp0AscomPingMonitor.exe" (
    echo ERROR: AscomPingMonitor.exe not found next to this script.
    echo        Copy this .bat into the same folder as the exe.
    pause
    exit /b 1
)

net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: administrator rights required.
    echo        Right-click this file and "Run as administrator".
    pause
    exit /b 1
)

echo === Creating startup task (runs at boot, before anyone logs in)...
schtasks /create /f /tn "Ascom Ping Monitor" ^
    /tr "\"%~dp0AscomPingMonitor.exe\"" ^
    /sc onstart /ru SYSTEM

echo === Allowing port 8080 through Windows Firewall...
netsh advfirewall firewall delete rule name="Ascom Ping Monitor" >nul 2>&1
netsh advfirewall firewall add rule name="Ascom Ping Monitor" ^
    dir=in action=allow protocol=TCP localport=8080

echo === Starting it now...
schtasks /run /tn "Ascom Ping Monitor"

echo.
echo ============================================================
echo   Installed. The monitor now starts with Windows.
echo   GUI:  http://localhost:8080   (or this PC's IP from
echo   other machines)   Login: ascom / ascom!12345
echo   To remove: run uninstall-startup.bat as administrator.
echo ============================================================
pause
