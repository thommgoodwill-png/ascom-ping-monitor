#!/usr/bin/env bash
# Ascom Network Monitor — installer for a Debian/Ubuntu Proxmox LXC container.
# Run as root inside the container:  bash install.sh
set -euo pipefail

APP_DIR="/opt/ascom-ping-monitor"
DATA_DIR="/var/lib/ascom-ping-monitor"
SERVICE="ascom-ping-monitor"
PORT="${PINGMON_PORT:-8080}"
# Used only when this script is run on its own (e.g. via wget one-liner) and
# the application files are not sitting next to it:
GITHUB_REPO="${GITHUB_REPO:-thommgoodwill-png/ascom-ping-monitor}"
BRANCH="${BRANCH:-main}"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (inside the LXC container)." >&2
  exit 1
fi

echo "==> Installing OS packages (python3, venv, iputils-ping, traceroute)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip iputils-ping traceroute \
  mtr-tiny tcpdump snmp iperf3 curl ca-certificates tar poppler-utils >/dev/null

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /tmp)"
if [[ ! -d "$SRC_DIR/pingmon" ]]; then
  # standalone mode: fetch the application from GitHub
  if [[ "$GITHUB_REPO" == YOUR-GITHUB-USERNAME/* ]]; then
    echo "ERROR: application files not found next to install.sh and GITHUB_REPO" >&2
    echo "       is still the placeholder. Edit the GITHUB_REPO line in install.sh," >&2
    echo "       or run:  GITHUB_REPO=youruser/ascom-ping-monitor bash install.sh" >&2
    exit 1
  fi
  echo "==> Downloading application from github.com/${GITHUB_REPO} (${BRANCH})…"
  SRC_DIR="/tmp/pingmon-src"
  rm -rf "$SRC_DIR" && mkdir -p "$SRC_DIR"
  curl -fsSL "https://codeload.github.com/${GITHUB_REPO}/tar.gz/refs/heads/${BRANCH}" \
    | tar xz -C "$SRC_DIR" --strip-components=1
fi

echo "==> Copying application to ${APP_DIR}…"
mkdir -p "$APP_DIR" "$DATA_DIR"
# clean out old app code so deleted/renamed files don't linger between updates
# (never touches $DATA_DIR, where settings and ping history live)
rm -rf "$APP_DIR/pingmon" "$APP_DIR/templates" "$APP_DIR/static"
cp -r "$SRC_DIR/pingmon" "$SRC_DIR/templates" "$SRC_DIR/static" \
      "$SRC_DIR/run.py" "$SRC_DIR/requirements.txt" "$APP_DIR/"

echo "==> Creating Python virtual environment…"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/python" -m pip install --quiet --upgrade pip || true
"$APP_DIR/venv/bin/python" -m pip install --quiet -r "$APP_DIR/requirements.txt"

echo "==> Allowing unprivileged ICMP ping (needed in unprivileged LXC)…"
if ! ping -c1 -W1 127.0.0.1 >/dev/null 2>&1; then
  sysctl -w net.ipv4.ping_group_range="0 2147483647" >/dev/null 2>&1 || true
  echo 'net.ipv4.ping_group_range = 0 2147483647' > /etc/sysctl.d/99-pingmon.conf 2>/dev/null || true
fi

echo "==> Installing systemd service…"
cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=Ascom Network Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PINGMON_DATA=${DATA_DIR}
Environment=PINGMON_PORT=${PORT}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python ${APP_DIR}/run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE}
systemctl restart ${SERVICE}   # starts fresh installs, restarts updates

sleep 2
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "=============================================================="
echo "  Ascom Network Monitor is installed and running."
echo
echo "  Web GUI:   http://${IP:-<container-ip>}:${PORT}"
echo "  Username:  ascom"
echo "  Password:  ascom!12345"
echo
echo "  Service:   systemctl status ${SERVICE}"
echo "  Logs:      journalctl -u ${SERVICE} -f"
echo "  Data:      ${DATA_DIR}"
echo
echo "  File servers (Tools -> File servers) are OFF by default. Switch on"
echo "  only what the site needs, and open the firewall to match:"
echo "    HTTP 8081/tcp   HTTPS 8443/tcp   FTP 21/tcp (+ 50000-50100 passive)"
echo "    TFTP 69/udp     - files are served from ${DATA_DIR}/fileserver"
echo "  The service runs as root, so ports 21 and 69 bind without extra setup."
echo "=============================================================="
