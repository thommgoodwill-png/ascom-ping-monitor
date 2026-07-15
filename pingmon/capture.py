"""On-demand packet capture via tcpdump.

Runs a bounded capture (duration + packet cap), writes a real .pcap to the
data directory (downloadable, opens in Wireshark), and parses a summary table
for the GUI. Disabled by default — must be switched on in Settings.

Reality check: a capture only sees traffic that reaches this host — its own
traffic plus broadcast/multicast. To see other devices talking to each other
you need a switch SPAN/mirror port feeding the monitor's interface.
"""
import os
import platform
import re
import shutil
import subprocess
import threading
import time

from . import database, settings

IS_WINDOWS = platform.system() == "Windows"
CAP_DIR = os.path.join(database.DATA_DIR, "captures")

# hard safety bounds regardless of what the UI sends
MAX_SECONDS = 300
MAX_PACKETS = 100000

_line_re = re.compile(r"^(\d\d:\d\d:\d\d\.\d+)\s+(.*)$")


def tcpdump_path():
    return shutil.which("tcpdump") or shutil.which("windump")


def available():
    return tcpdump_path() is not None


def list_interfaces():
    """Return capture-capable interface names."""
    exe = tcpdump_path()
    if not exe:
        return []
    try:
        out = subprocess.run([exe, "-D"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    names = []
    for line in out.splitlines():
        # format: "1.eth0 [Up, Running]"
        m = re.match(r"\s*\d+\.([^\s]+)", line)
        if m:
            name = m.group(1)
            if name not in ("any", "nflog", "nfqueue", "lo") or name == "any":
                names.append(name)
    return names


class CaptureJob:
    def __init__(self, iface, bpf, seconds, packets):
        self.iface = iface or "any"
        self.bpf = bpf.strip()
        self.seconds = max(1, min(MAX_SECONDS, int(seconds)))
        self.packets = max(1, min(MAX_PACKETS, int(packets)))
        self.id = time.strftime("%Y%m%d-%H%M%S")
        self.started = time.time()
        self.state = "running"       # running | done | error
        self.error = None
        self.count = 0
        self.proc = None
        self.pcap = os.path.join(CAP_DIR, f"capture-{self.id}.pcap")
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        os.makedirs(CAP_DIR, exist_ok=True)
        self._thread.start()

    def _run(self):
        exe = tcpdump_path()
        if not exe:
            self.state = "error"
            self.error = "tcpdump is not installed"
            return
        cmd = [exe, "-i", self.iface, "-w", self.pcap,
               "-c", str(self.packets), "-U"]
        if not IS_WINDOWS:
            cmd += ["-Z", "root"]        # don't drop privs mid-capture
        if self.bpf:
            cmd += self.bpf.split()
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, text=True)
        except OSError as e:
            self.state = "error"
            self.error = str(e)
            return
        # stop at the time limit even if the packet cap isn't reached
        try:
            self.proc.wait(timeout=self.seconds)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        rc = self.proc.returncode
        stderr = ""
        if self.proc.stderr:
            stderr = self.proc.stderr.read() or ""
        if not os.path.exists(self.pcap):
            self.state = "error"
            self.error = _friendly_error(stderr)
            return
        self.state = "done"
        self.summary = self._summarize()

    def _summarize(self, limit=500):
        rows, total = summarize_file(self.pcap, limit)
        self.count = total
        return rows

    def status(self):
        d = {"id": self.id, "state": self.state, "iface": self.iface,
             "bpf": self.bpf, "seconds": self.seconds, "packets": self.packets,
             "count": self.count, "error": self.error,
             "elapsed": round(time.time() - self.started, 1),
             "size": os.path.getsize(self.pcap) if os.path.exists(self.pcap) else 0}
        if self.state == "done":
            d["summary"] = getattr(self, "summary", [])
        return d


def summarize_file(path, limit=1000):
    """Parse any .pcap into (rows, total_count). Re-reads via tcpdump -r, so no
    binary decoding here. total_count is the real packet count; rows is capped."""
    exe = tcpdump_path()
    if not exe:
        raise RuntimeError("tcpdump is not installed — cannot read pcap files")
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    out = subprocess.run([exe, "-nn", "-tttt", "-r", path],
                         capture_output=True, text=True, timeout=120).stdout
    lines = [l for l in out.splitlines() if l.strip()]
    rows = [_parse_line(l) for l in lines[:limit]]
    return rows, len(lines)


def _parse_line(line):
    """Turn a tcpdump text line into {time, src, dst, proto, info}."""
    # 2026-07-14 14:00:00.123456 IP 10.0.0.1.443 > 10.0.0.2.51000: tcp 0
    m = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+(.*)$", line)
    ts, rest = (m.group(1), m.group(2)) if m else ("", line)
    proto = "?"
    if rest.startswith("IP6"):
        proto = "IPv6"
    elif rest.startswith("IP"):
        proto = "IPv4"
    elif rest.startswith("ARP"):
        proto = "ARP"
    src = dst = ""
    mm = re.search(r"IP6?\s+([^\s]+)\s+>\s+([^\s:]+)", rest)
    if mm:
        src, dst = mm.group(1), mm.group(2)
    low = rest.lower()
    for name in ("tcp", "udp", "icmp", "icmp6", "igmp"):
        if f" {name}" in low or low.endswith(name):
            proto = name.upper()
            break
    if "ARP" in rest:
        proto = "ARP"
    return {"time": ts.split(" ")[-1] if ts else "", "src": src, "dst": dst,
            "proto": proto, "info": rest[:200]}


def _friendly_error(stderr):
    s = (stderr or "").strip()
    if "permission denied" in s.lower() or "operation not permitted" in s.lower():
        return ("permission denied — the service needs CAP_NET_RAW. Run the "
                "container privileged, or the exe as Administrator.")
    if "no such device" in s.lower():
        return "no such interface — pick another from the list."
    return s or "capture failed (no packets and no pcap produced)"


# ---- registry of jobs (in-memory) + on-disk pcap listing ----
_jobs = {}
_lock = threading.Lock()


def start_capture(iface, bpf, seconds, packets):
    if not settings.get("capture_enabled"):
        raise RuntimeError("packet capture is disabled in Settings")
    if not available():
        raise RuntimeError("tcpdump is not installed on this host")
    job = CaptureJob(iface, bpf, seconds, packets)
    with _lock:
        # only one running capture at a time
        for j in _jobs.values():
            if j.state == "running":
                raise RuntimeError("a capture is already running")
        _jobs[job.id] = job
    job.start()
    return job


def get_job(job_id):
    with _lock:
        return _jobs.get(job_id)


def list_captures():
    """On-disk pcap files, newest first."""
    out = []
    if os.path.isdir(CAP_DIR):
        for fn in sorted(os.listdir(CAP_DIR), reverse=True):
            if fn.endswith(".pcap"):
                p = os.path.join(CAP_DIR, fn)
                out.append({"file": fn, "size": os.path.getsize(p),
                            "mtime": os.path.getmtime(p)})
    return out


def delete_capture(fname):
    if "/" in fname or "\\" in fname or not fname.endswith(".pcap"):
        raise ValueError("bad filename")
    p = os.path.join(CAP_DIR, fname)
    if os.path.exists(p):
        os.remove(p)


def purge_old_captures(keep=20, max_age_days=7):
    caps = list_captures()
    cutoff = time.time() - max_age_days * 86400
    for i, c in enumerate(caps):
        if i >= keep or c["mtime"] < cutoff:
            try:
                delete_capture(c["file"])
            except OSError:
                pass
