#!/usr/bin/env bash
# =============================================================================
#  Ascom Network Monitor — Proxmox one-liner installer
#
#  Run this in the PROXMOX HOST shell (Datacenter → your node → Shell):
#
#    bash -c "$(wget -qLO - https://raw.githubusercontent.com/YOUR-GITHUB-USERNAME/ascom-ping-monitor/main/proxmox-lxc.sh)"
#
#  It creates a small Debian 12 LXC container, downloads this repository from
#  GitHub inside it, and installs the monitor as a systemd service.
#
#  ▶ EDIT THE NEXT LINE once, after you upload this repo to your GitHub:
GITHUB_REPO="${GITHUB_REPO:-YOUR-GITHUB-USERNAME/ascom-ping-monitor}"
BRANCH="${BRANCH:-main}"
# =============================================================================
set -euo pipefail

# ---- sanity checks ----------------------------------------------------------
if ! command -v pct >/dev/null 2>&1 || ! command -v pveam >/dev/null 2>&1; then
  echo "ERROR: this script must run on a Proxmox VE host (pct/pveam not found)." >&2
  echo "       Open Datacenter → <your node> → Shell in the Proxmox GUI and run it there." >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  echo "ERROR: please run as root on the Proxmox host." >&2
  exit 1
fi
if [[ "$GITHUB_REPO" == YOUR-GITHUB-USERNAME/* ]]; then
  echo "ERROR: GITHUB_REPO is still the placeholder." >&2
  echo "       Edit the GITHUB_REPO line at the top of proxmox-lxc.sh in your repo," >&2
  echo "       or run:  GITHUB_REPO=youruser/ascom-ping-monitor bash proxmox-lxc.sh" >&2
  exit 1
fi

echo "==============================================="
echo "  Ascom Network Monitor — LXC container setup"
echo "  Source: github.com/${GITHUB_REPO} (${BRANCH})"
echo "==============================================="

# ---- gather options (Enter accepts the default) -----------------------------
DEFAULT_ID=$(pvesh get /cluster/nextid)
read -rp "Container ID            [${DEFAULT_ID}]: " CTID
CTID="${CTID:-$DEFAULT_ID}"

read -rp "Hostname                [ascom-ping-monitor]: " CTHOST
CTHOST="${CTHOST:-ascom-ping-monitor}"

DEFAULT_STORAGE=$(pvesm status --content rootdir 2>/dev/null | awk 'NR==2{print $1}')
DEFAULT_STORAGE="${DEFAULT_STORAGE:-local-lvm}"
read -rp "Rootfs storage          [${DEFAULT_STORAGE}]: " STORAGE
STORAGE="${STORAGE:-$DEFAULT_STORAGE}"

read -rp "Disk size in GB         [4]: " DISK
DISK="${DISK:-4}"

read -rp "Memory in MB            [512]: " RAM
RAM="${RAM:-512}"

read -rp "CPU cores               [1]: " CORES
CORES="${CORES:-1}"

read -rp "Network bridge          [vmbr0]: " BRIDGE
BRIDGE="${BRIDGE:-vmbr0}"

read -rp "IP address (CIDR, e.g. 192.168.1.50/24, or 'dhcp') [dhcp]: " IPADDR
IPADDR="${IPADDR:-dhcp}"
NETCONF="name=eth0,bridge=${BRIDGE},ip=${IPADDR}"
if [[ "$IPADDR" != "dhcp" ]]; then
  read -rp "Gateway                 [none]: " GW
  [[ -n "${GW:-}" ]] && NETCONF="${NETCONF},gw=${GW}"
fi

read -rp "Web GUI port            [8080]: " PORT
PORT="${PORT:-8080}"

# ---- debian template ---------------------------------------------------------
echo "==> Finding Debian 12 container template…"
pveam update >/dev/null
TEMPLATE=$(pveam available --section system | awk '/debian-12-standard/{print $2}' | sort -V | tail -1)
if [[ -z "$TEMPLATE" ]]; then
  echo "ERROR: could not find a debian-12-standard template via pveam." >&2
  exit 1
fi
TEMPLATE_STORAGE=$(pvesm status --content vztmpl 2>/dev/null | awk 'NR==2{print $1}')
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
if ! pveam list "$TEMPLATE_STORAGE" 2>/dev/null | grep -q "$TEMPLATE"; then
  echo "==> Downloading ${TEMPLATE} to ${TEMPLATE_STORAGE}…"
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

# ---- create + start container -------------------------------------------------
echo "==> Creating container ${CTID} (${CTHOST})…"
pct create "$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}" \
  --hostname "$CTHOST" \
  --cores "$CORES" --memory "$RAM" --swap 256 \
  --rootfs "${STORAGE}:${DISK}" \
  --net0 "$NETCONF" \
  --unprivileged 1 --features nesting=1 \
  --onboot 1 \
  --tags "ascom;monitoring"

echo "==> Starting container…"
pct start "$CTID"

echo "==> Waiting for network inside the container…"
for i in $(seq 1 60); do
  if pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1; then
    break
  fi
  sleep 2
  if [[ $i -eq 60 ]]; then
    echo "ERROR: container has no network/DNS after 2 minutes." >&2
    echo "       Check the bridge/IP settings, then re-run the app install with:" >&2
    echo "       pct exec $CTID -- bash -c 'bash <(wget -qO- https://raw.githubusercontent.com/${GITHUB_REPO}/${BRANCH}/install.sh)'" >&2
    exit 1
  fi
done

# ---- install the app inside the container -------------------------------------
echo "==> Downloading ${GITHUB_REPO} and installing (this takes a minute)…"
pct exec "$CTID" -- bash -c "
  set -e
  export DEBIAN_FRONTEND=noninteractive PINGMON_PORT=${PORT}
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates tar >/dev/null
  rm -rf /tmp/pingmon-src && mkdir -p /tmp/pingmon-src
  curl -fsSL 'https://codeload.github.com/${GITHUB_REPO}/tar.gz/refs/heads/${BRANCH}' \
    | tar xz -C /tmp/pingmon-src --strip-components=1
  bash /tmp/pingmon-src/install.sh
  rm -rf /tmp/pingmon-src
"

IP=$(pct exec "$CTID" -- hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "=============================================================="
echo "  ✔ Ascom Network Monitor container is ready."
echo
echo "  Container:  ${CTID} (${CTHOST})"
echo "  Web GUI:    http://${IP:-<container-ip>}:${PORT}"
echo "  Username:   ascom"
echo "  Password:   ascom!12345"
echo
echo "  Console:    pct enter ${CTID}"
echo "  Logs:       pct exec ${CTID} -- journalctl -u ascom-ping-monitor -f"
echo "=============================================================="
