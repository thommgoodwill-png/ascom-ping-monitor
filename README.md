# Ascom Ping Monitor

A self-contained network ping monitor for a Proxmox (Debian/Ubuntu) LXC container.
It pings multiple devices on a configurable 0.2 s – 60 s timer, logs everything to
SQLite, draws latency graphs in a browser GUI with selectable time periods, and
emails reports and failure alerts through Gmail.

- Latency over **50 ms** is flagged as a **warning (orange)**, over **100 ms** as
  **critical (red)** — thresholds adjustable globally *and per device*.
- **Jitter tracking** — ping-to-ping variation, flagged when high (VoIP killer).
- **Packet-loss alerts** — sustained partial loss on a device that is still up
  (flaky cable / duplex mismatch / saturated link) triggers its own email.
- **Auto-traceroute on failure** — captured the moment a device goes down or
  lossy, stored with the event and included in the alert email.
- **Correlation flagging** — several devices failing within 2 minutes is marked
  as a likely upstream/shared issue in events and emails.
- **Time-of-day heatmap** — hour-by-day grid per device; recurring problems
  (nightly backups, morning congestion) show up as dark columns.
- **Uptime / SLA page** — per-device uptime %, downtime, worst outages, CSV
  export for ISP/vendor evidence.
- **Wallboard mode** — full-screen auto-refreshing status tiles for a NOC/office
  screen (▦ button in the top bar, F11 for full screen).
- **Maintenance window** — a daily quiet period that suppresses all alert emails.
- **MAC address pickup** — learned automatically from the ARP table for
  same-subnet devices, shown throughout the GUI; a changed MAC is flagged as an
  event (device swap / DHCP change / IP conflict).
- Configurable **ping payload size** (large packets expose MTU/fragmentation faults).
- Email reports (rolling **6 h / 12 h / 24 h**) contain a per-device summary plus
  **only the problem pings** (failures and pings above the warning threshold) —
  never a list of good pings.
- Immediate **device-down** and **recovery** alert emails.
- Light and dark mode, Ascom branding, login-protected GUI.

## Install option 1 — one-liner from the Proxmox GUI (via GitHub)

Once this repo is on your GitHub (see **Publishing to GitHub** below), open the
Proxmox web GUI → **Datacenter → your node → Shell** and paste:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/YOUR-GITHUB-USERNAME/ascom-ping-monitor/main/proxmox-lxc.sh)"
```

It asks a few questions (container ID, storage, memory, IP — Enter accepts the
defaults), then automatically:

1. downloads the Debian 12 LXC template if needed,
2. creates and starts an unprivileged container (on-boot autostart, tagged
   `ascom;monitoring`),
3. pulls this repo from GitHub inside the container and installs the monitor
   as a systemd service,
4. prints the GUI address and login when done.

### Publishing to GitHub (one-time)

1. Create a **public** repo on github.com called `ascom-ping-monitor`
   (private repos won't work with the plain one-liner — raw downloads need a token).
2. Upload everything in this folder (web upload: **Add file → Upload files**,
   or `git init && git add -A && git commit -m init && git push`).
3. Edit **one line** in two files — `proxmox-lxc.sh` and `install.sh` — replacing
   `YOUR-GITHUB-USERNAME/ascom-ping-monitor` with your actual repo path, e.g.
   `thomm/ascom-ping-monitor`. (You can do this in the GitHub web editor.)
4. Make sure your default branch is `main` (or change `BRANCH=` in the same files).

Alternatively, skip editing the files by passing the repo on the command line:

```bash
GITHUB_REPO=thommgoodwill-png/ascom-ping-monitor bash -c "$(wget -qLO - https://raw.githubusercontent.com/thommgoodwill-png/ascom-ping-monitor/main/proxmox-lxc.sh)"
```

## Install option 2 — inside an existing container

Already have a Debian/Ubuntu LXC? Run this **inside the container**:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/thommgoodwill-png/ascom-ping-monitor/main/install.sh)"
```

`install.sh` detects it's running standalone and downloads the rest of the repo
itself.

## Install option 3 — from a copied folder (no GitHub)

Copy this folder into the container, then:

```bash
bash install.sh
```

That installs Python + iputils-ping, creates a venv under
`/opt/ascom-ping-monitor`, and starts the `ascom-ping-monitor` systemd service
on boot.

Open `http://<container-ip>:8080` and sign in:

| | |
|---|---|
| Username | `ascom` |
| Password | `ascom!12345` |

To use a different port: `PINGMON_PORT=9090 bash install.sh`.

### Proxmox container notes

- An **unprivileged** container is fine — the installer sets
  `net.ipv4.ping_group_range` so ping works without root. If pings still fail,
  run the container privileged or add `sysctl` permission in the CT options.
- Give the container network access to the subnets you want to monitor.

## Gmail setup (required for email)

Gmail no longer allows plain passwords over SMTP, so you need an **app password**:

1. Go to your Google Account → **Security**.
2. Turn on **2-Step Verification** if it isn't already.
3. Security → 2-Step Verification → **App passwords** → create one
   (app: "Mail", device: anything). Google shows a 16-character password once.
4. In the GUI: **Settings → Email**, enter your Gmail address, the app password
   and the recipient list, switch **Email enabled** on, then press
   **Send test email**.

## Every option (Settings page)

| Section | Option | Default |
|---|---|---|
| Monitoring | Monitoring enabled (master switch) | on |
| | Ping interval | 5 s (0.2–60 s, per-device override available) |
| | Ping timeout | 2 s |
| | Failures before "down" | 3 consecutive |
| | Warning threshold (orange) | 50 ms (per-device override available) |
| | Critical threshold (red) | 100 ms (per-device override available) |
| | Jitter warning | 30 ms |
| | Ping payload size | 56 bytes (16–1472) |
| | History retention | 30 days |
| Detection | Packet-loss alerts | on |
| | Loss threshold / window | 10 % over 15 min |
| | Traceroute on failure | on |
| | Correlation threshold | 3 devices within 2 min |
| Maintenance | Daily quiet window (alerts muted) | off, 01:00–03:00 |
| Email | Email enabled (master switch) | off |
| | Gmail address / app password / recipients | — |
| Reports | 6-hour report | on |
| | 12-hour report | on |
| | 24-hour report | on |
| | Skip clean reports (nothing bad = no email) | off |
| | Max problem rows per report | 200 |
| | "Send now" buttons for each report | — |
| Alerts | Device-down alerts | on |
| | Recovery alerts (with downtime duration) | on |
| | Alert cooldown between repeats | 15 min |
| Interface | Default theme (auto / light / dark) | auto |
| | Dashboard auto-refresh | 30 s |
| | Wallboard refresh | 10 s |

Each device also has its own **enable/disable** toggle and optional
**per-device ping interval** override on the Devices page.

## The dashboard

- Time-period buttons: **15 m, 1 h, 3 h, 6 h, 12 h, 24 h, 2 d, 7 d, 30 d** —
  they re-scope every graph.
- The overview graph shows all devices together; each device card shows its own
  graph where the line itself turns orange above the warning threshold and red
  above critical. Red triangles along the baseline mark failed pings.
- Hover any graph for an exact readout of every device at that moment.
- Long ranges are automatically down-sampled (bucketed averages, max preserved)
  so 30 days of 1-second pings still draws instantly.

## Branding

**The reliable way:** drop your official logo file into the **data directory**
— it beats everything bundled with the app and survives every update, rebuild
and reinstall:

- Linux/Proxmox: `/var/lib/ascom-ping-monitor/branding/Logo.png`
- Windows exe: `C:\ProgramData\AscomPingMonitor\branding\Logo.png`

Accepted names: `Logo.png`, `logo.png`, `Logo.svg`, `logo.svg`, `Logo.jpg`,
`logo.jpg` (checked in that order). No restart needed — refresh the browser.

Alternatively a logo in the repo's `static/branding/` folder (same names) is
used when no data-directory logo exists.

## Changing the login

The credentials are hard-coded (as requested) at the top of
`pingmon/settings.py` (`GUI_USERNAME` / `GUI_PASSWORD`). Edit and
`systemctl restart ascom-ping-monitor`.

## Service management

```bash
systemctl status ascom-ping-monitor    # status
journalctl -u ascom-ping-monitor -f    # live logs
systemctl restart ascom-ping-monitor   # restart
```

Data lives in `/var/lib/ascom-ping-monitor/pingmon.db` (SQLite, WAL mode).

## Uninstall

```bash
systemctl disable --now ascom-ping-monitor
rm -rf /opt/ascom-ping-monitor /etc/systemd/system/ascom-ping-monitor.service
rm -rf /var/lib/ascom-ping-monitor        # deletes history too
systemctl daemon-reload
```
