# Ascom Ping Monitor — Windows exe

The same monitor, built as a single portable `AscomPingMonitor.exe`. No Docker,
no services to configure — double-click and your browser opens on the dashboard.

## Building the exe (one time, ~2 minutes)

The exe has to be built **on a Windows PC** (any Windows 10/11 machine —
it doesn't have to be the one that will run the monitor):

1. Install Python 3.9+ from https://python.org — during setup tick
   **"Add python.exe to PATH"**.
2. Download this repository (green **Code → Download ZIP** button on GitHub)
   and unzip it.
3. Double-click **`build_windows.bat`**.

That's it — the finished exe appears at **`dist\AscomPingMonitor.exe`**.
Copy that one file anywhere (plus the two startup .bat files if you want
autostart). It contains everything: Python, the web GUI, the branding.

## Running

- Double-click `AscomPingMonitor.exe`. After a second your browser opens
  `http://localhost:8080` — login **ascom / ascom!12345**.
- Windows' built-in `ping` and `tracert` are used automatically — no admin
  rights needed for monitoring.
- Data, settings and logs live in **`C:\ProgramData\AscomPingMonitor`**
  (survives exe upgrades — just replace the exe with a newly built one).
- To view from other machines, allow TCP 8080 through Windows Firewall
  (install-startup.bat below does this for you).

## Start with Windows (optional)

Copy `install-startup.bat` and `uninstall-startup.bat` into the same folder
as the exe, right-click `install-startup.bat` → **Run as administrator**. It:

- creates a scheduled task that starts the monitor at boot (before login,
  as SYSTEM — a true set-and-forget monitor box),
- opens TCP 8080 in Windows Firewall,
- starts it immediately.

`uninstall-startup.bat` (as administrator) reverses both.

## Environment overrides (optional)

Set before launching, or in the scheduled task:

| Variable | Effect | Default |
|---|---|---|
| `PINGMON_PORT` | web GUI port | 8080 |
| `PINGMON_HOST` | bind address | 0.0.0.0 |
| `PINGMON_DATA` | data folder | C:\ProgramData\AscomPingMonitor |
| `PINGMON_NO_BROWSER` | `1` = don't auto-open the browser | — |

## Windows-specific notes

- **Timeouts on Windows ping are milliseconds internally** — the GUI setting
  is still in seconds; the app converts.
- The traceroute captured on failures uses Windows `tracert` (it's slower than
  Linux traceroute — up to ~60 s — so it runs in the background and attaches
  to the event/email when done).
- Everything else — all features, reports, Gmail, heatmap, wallboard, SLA —
  is identical to the Linux/Proxmox version.
