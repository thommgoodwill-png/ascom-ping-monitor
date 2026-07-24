@echo off
REM  Removes the Ascom Ping Monitor startup task and firewall rule.
REM  Right-click and "Run as administrator".
net session >nul 2>&1
if errorlevel 1 (
    echo ERROR: administrator rights required.
    pause
    exit /b 1
)
taskkill /im AscomPingMonitor.exe /f >nul 2>&1
schtasks /delete /f /tn "Ascom Ping Monitor"
netsh advfirewall firewall delete rule name="Ascom Ping Monitor"
echo Removed. Ping data in C:\ProgramData\AscomPingMonitor was kept -
echo delete that folder manually if you also want the history gone.
pause
