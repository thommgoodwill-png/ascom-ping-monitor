"""On-demand network diagnostics: subnet discovery, MTR-style path analysis,
SNMP GET and iperf3 throughput. Each degrades gracefully if an optional
binary/library is missing."""
import concurrent.futures
import ipaddress
import platform
import re
import shutil
import socket
import subprocess
import threading
import time

from . import monitor, oui

IS_WINDOWS = platform.system() == "Windows"
_NO_WINDOW = {"creationflags": 0x08000000} if IS_WINDOWS else {}


# ---------------- subnet discovery ----------------

def local_subnet():
    """Best-guess local /24 the monitor sits on, e.g. '192.168.0.0/24'."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        net = ipaddress.ip_network(ip + "/24", strict=False)
        return str(net)
    except OSError:
        return "192.168.1.0/24"


def discover(cidr, timeout=1.0, workers=64):
    """Ping-sweep a subnet, then read MACs from the ARP table. Returns a list
    of {ip, mac, vendor, alive}. Capped at /22 (1022 hosts) for sanity."""
    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        raise ValueError("invalid subnet — use e.g. 192.168.0.0/24")
    hosts = list(net.hosts())
    if len(hosts) > 1022:
        raise ValueError("subnet too large — use /22 or smaller")

    alive = []
    lock = threading.Lock()

    def probe(ip):
        lat = monitor.ping_once(str(ip), timeout, 56)
        if lat is not None:
            with lock:
                alive.append(str(ip))

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        ex.map(probe, hosts)

    results = []
    for ip in sorted(alive, key=lambda x: tuple(int(o) for o in x.split("."))):
        mac = monitor.lookup_mac(ip)
        results.append({"ip": ip, "mac": mac,
                        "vendor": oui.vendor(mac) if mac else None})
    return results


# ---------------- MTR-style path analysis ----------------

def path_analysis(host, cycles=5, max_hops=20):
    """Run repeated traceroute-style probing and aggregate per-hop loss/latency.

    Uses mtr if available (best), else falls back to repeated traceroute.
    Returns {hops: [{hop, host, loss_pct, sent, avg, best, worst}], tool}.
    """
    if shutil.which("mtr"):
        return _mtr(host, cycles, max_hops)
    return _traceroute_loop(host, cycles, max_hops)


def _mtr(host, cycles, max_hops):
    try:
        out = subprocess.run(
            ["mtr", "-n", "-r", "-c", str(cycles), "-m", str(max_hops), host],
            capture_output=True, text=True, timeout=cycles * max_hops + 30).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"error": str(e), "hops": [], "tool": "mtr"}
    hops = []
    for line in out.splitlines():
        # "  1.|-- 192.168.0.1  0.0%  5  0.4  0.5  0.4  0.6  0.1"
        m = re.match(r"\s*(\d+)\.\|--\s+([^\s]+)\s+([\d.]+)%\s+(\d+)\s+"
                     r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)", line)
        if m:
            hops.append({"hop": int(m.group(1)), "host": m.group(2),
                         "loss_pct": float(m.group(3)), "sent": int(m.group(4)),
                         "avg": float(m.group(6)), "best": float(m.group(7)),
                         "worst": float(m.group(8))})
    return {"hops": hops, "tool": "mtr"}


def _traceroute_loop(host, cycles, max_hops):
    """Fallback: run traceroute a few times, aggregate loss/latency per hop."""
    tr = "tracert" if IS_WINDOWS else "traceroute"
    if not shutil.which(tr):
        return {"error": f"{tr} not installed", "hops": [], "tool": "none"}
    hop_ip = {}
    hop_lat = {}
    hop_sent = {}
    hop_recv = {}
    for _ in range(max(1, cycles)):
        if IS_WINDOWS:
            cmd = [tr, "-d", "-w", "1000", "-h", str(max_hops), host]
        else:
            cmd = [tr, "-n", "-w", "1", "-q", "1", "-m", str(max_hops), host]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=max_hops * 2 + 20, **_NO_WINDOW).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in out.splitlines():
            hm = re.match(r"\s*(\d+)\s+(.*)", line)
            if not hm:
                continue
            hop = int(hm.group(1))
            rest = hm.group(2)
            hop_sent[hop] = hop_sent.get(hop, 0) + 1
            ipm = re.search(r"(\d{1,3}(?:\.\d{1,3}){3})", rest)
            latm = re.search(r"([\d.]+)\s*ms", rest)
            if ipm:
                hop_ip[hop] = ipm.group(1)
            if latm and "*" not in rest.split("ms")[0][-3:]:
                hop_recv[hop] = hop_recv.get(hop, 0) + 1
                hop_lat.setdefault(hop, []).append(float(latm.group(1)))
            elif "*" in rest:
                pass
    hops = []
    for hop in sorted(hop_sent):
        lats = hop_lat.get(hop, [])
        sent = hop_sent.get(hop, 0)
        recv = hop_recv.get(hop, 0)
        hops.append({
            "hop": hop, "host": hop_ip.get(hop, "*"),
            "loss_pct": round((1 - recv / sent) * 100, 1) if sent else 100.0,
            "sent": sent,
            "avg": round(sum(lats) / len(lats), 1) if lats else None,
            "best": round(min(lats), 1) if lats else None,
            "worst": round(max(lats), 1) if lats else None,
        })
    return {"hops": hops, "tool": tr}


# ---------------- SNMP ----------------

# common OIDs worth reading from managed gear
SNMP_OIDS = {
    "sysDescr": "1.3.6.1.2.1.1.1.0",
    "sysName": "1.3.6.1.2.1.1.5.0",
    "sysUpTime": "1.3.6.1.2.1.1.3.0",
    "sysLocation": "1.3.6.1.2.1.1.6.0",
    "sysContact": "1.3.6.1.2.1.1.4.0",
    "ifNumber": "1.3.6.1.2.1.2.1.0",
}


def snmp_available():
    try:
        import pysnmp  # noqa: F401
        return True
    except Exception:
        return bool(shutil.which("snmpget"))


def snmp_get(host, community="public", version="2c"):
    """Read a handful of standard OIDs. Tries pysnmp, then snmpget CLI."""
    results = {}
    # 1) pysnmp (pure python, bundled via requirements)
    try:
        from pysnmp.hlapi import (SnmpEngine, CommunityData, UdpTransportTarget,
                                  ContextData, ObjectType, ObjectIdentity, getCmd)
        mp = 1 if version == "2c" else 0
        for name, oid in SNMP_OIDS.items():
            it = getCmd(SnmpEngine(), CommunityData(community, mpModel=mp),
                        UdpTransportTarget((host, 161), timeout=2, retries=1),
                        ContextData(), ObjectType(ObjectIdentity(oid)))
            errI, errS, errX, binds = next(it)
            if errI or errS:
                continue
            for _, val in binds:
                results[name] = str(val)
        if results:
            return {"ok": True, "tool": "pysnmp", "values": results}
    except Exception:
        pass
    # 2) net-snmp CLI fallback
    if shutil.which("snmpget"):
        try:
            for name, oid in SNMP_OIDS.items():
                out = subprocess.run(
                    ["snmpget", "-v", version, "-c", community, "-Ovq",
                     "-t", "2", "-r", "1", host, oid],
                    capture_output=True, text=True, timeout=8, **_NO_WINDOW)
                v = out.stdout.strip()
                if out.returncode == 0 and v:
                    results[name] = v.strip('"')
            if results:
                return {"ok": True, "tool": "snmpget", "values": results}
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {"ok": False, "tool": None, "values": {},
            "error": "no SNMP response — check the device has SNMP enabled, the "
                     "community string is correct, and pysnmp or net-snmp is installed"}


# ---------------- iperf3 throughput ----------------

def iperf_available():
    return bool(shutil.which("iperf3"))


def iperf_test(host, seconds=5, reverse=False, port=5201):
    """Run an iperf3 client against a host running `iperf3 -s`."""
    if not iperf_available():
        return {"ok": False, "error": "iperf3 is not installed on the monitor"}
    cmd = ["iperf3", "-c", host, "-t", str(int(seconds)), "-p", str(int(port)),
           "-J"]
    if reverse:
        cmd.append("-R")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=int(seconds) + 20, **_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"ok": False, "error": str(e)}
    import json
    try:
        data = json.loads(out.stdout)
    except ValueError:
        err = out.stderr.strip() or "iperf3 failed (is `iperf3 -s` running on the target?)"
        return {"ok": False, "error": err}
    if "error" in data:
        return {"ok": False, "error": data["error"]}
    end = data.get("end", {})
    recv = end.get("sum_received", {})
    sent = end.get("sum_sent", {})
    return {"ok": True,
            "sent_mbps": round(sent.get("bits_per_second", 0) / 1e6, 1),
            "recv_mbps": round(recv.get("bits_per_second", 0) / 1e6, 1),
            "retransmits": sent.get("retransmits"),
            "seconds": seconds, "reverse": reverse}
