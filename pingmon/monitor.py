"""Ping engine: one worker thread per device, plus a manager that reconciles
the worker pool with the device table, runs the packet-loss checker and
handles retention purging. Failures trigger traceroute capture and
cross-device correlation flagging."""
import logging
import platform
import re
import socket
import subprocess
import threading
import time

from . import database, netcheck, oui, settings

log = logging.getLogger("pingmon.monitor")

IS_WINDOWS = platform.system() == "Windows"
# stop child processes flashing console windows when running as a windowed exe
_NO_WINDOW = {"creationflags": 0x08000000} if IS_WINDOWS else {}

_TIME_RE = re.compile(r"time[=<]([\d.]+)\s*ms", re.IGNORECASE)
_TIME_RE_ANY = re.compile(r"[=<](\d+(?:\.\d+)?)\s*ms", re.IGNORECASE)  # localized Windows


def ping_once(host, timeout_s, size=56):
    """Send a single ICMP echo. Returns latency in ms, or None on failure."""
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_s * 1000)),
               "-l", str(int(size)), host]
    else:
        cmd = ["ping", "-n", "-c", "1", "-W", str(int(max(1, timeout_s))),
               "-s", str(int(size)), host]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_s + 3, **_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    # a time= match is required: on Windows "Destination host unreachable"
    # replies still exit 0 but carry no time value
    m = _TIME_RE.search(proc.stdout) or _TIME_RE_ANY.search(proc.stdout)
    if m:
        return float(m.group(1))
    return None


def run_traceroute(host):
    """Best-effort traceroute for failure diagnosis. Returns text or None."""
    if IS_WINDOWS:
        cmd = ["tracert", "-d", "-w", "1000", "-h", "15", host]
    else:
        cmd = ["traceroute", "-n", "-w", "1", "-q", "1", "-m", "15", host]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=90 if IS_WINDOWS else 30, **_NO_WINDOW)
        out = (proc.stdout or "").strip()
        return out[:4000] if out else None
    except (subprocess.TimeoutExpired, OSError):
        return None


_MAC_RE = re.compile(r"((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})")
_BAD_MACS = {"00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}


def _normalize_mac(raw):
    mac = raw.lower().replace("-", ":")
    return None if mac in _BAD_MACS else mac


def lookup_mac(ip):
    """Read the device's MAC from the ARP/neighbour table after a ping.

    Only works for devices on the SAME subnet as the monitor — L2 addresses
    don't cross routers. Returns 'aa:bb:cc:dd:ee:ff' or None.
    """
    try:
        if IS_WINDOWS:
            proc = subprocess.run(["arp", "-a", ip], capture_output=True,
                                  text=True, timeout=5, **_NO_WINDOW)
            for line in proc.stdout.splitlines():
                if ip in line:
                    m = _MAC_RE.search(line)
                    if m:
                        return _normalize_mac(m.group(1))
            return None
        # Linux: prefer 'ip neigh', fall back to /proc/net/arp
        try:
            proc = subprocess.run(["ip", "neigh", "show", "to", ip],
                                  capture_output=True, text=True, timeout=5)
            m = _MAC_RE.search(proc.stdout)
            if m:
                return _normalize_mac(m.group(1))
        except OSError:
            pass
        with open("/proc/net/arp") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if parts and parts[0] == ip and len(parts) >= 4:
                    return _normalize_mac(parts[3])
    except Exception:
        pass
    return None


def correlation_note(window_s=120):
    """If several devices have down/loss events in the last window, flag it."""
    affected = database.recent_problem_devices(time.time() - window_s)
    need = settings.get("correlate_min_devices")
    if len(affected) >= need:
        return len(affected)
    return None


class DeviceWorker(threading.Thread):
    def __init__(self, device, monitor):
        super().__init__(daemon=True, name=f"ping-{device['id']}")
        self.device = device
        self.monitor = monitor
        self.stop_event = threading.Event()
        self.consecutive_fails = 0
        self.state = "unknown"          # unknown | up | down
        self.last_latency = None
        self.prev_latency = None        # for jitter
        self.last_jitter = None
        self.last_ts = None
        self.last_down_alert = 0.0
        self.down_since = None
        self._mac_next = 0.0        # when to next refresh the MAC
        self._resolved_ip = None
        self._checks_next = 0.0     # when to next run TCP/HTTP/DNS checks
        self.checks = {}            # kind -> {ok, ms, detail, ts}
        self._check_state = {}      # kind -> last ok (for change events)

    def interval(self):
        ov = self.device.get("interval_override")
        base = ov if ov else settings.get("ping_interval")
        return max(0.2, min(60.0, float(base)))

    def run(self):
        while not self.stop_event.is_set():
            if not settings.get("monitoring_enabled"):
                self.stop_event.wait(2)
                continue
            started = time.time()
            latency = ping_once(self.device["host"], settings.get("ping_timeout"),
                                settings.get("ping_size"))
            ts = time.time()
            success = latency is not None
            jitter = None
            if success and self.prev_latency is not None:
                jitter = round(abs(latency - self.prev_latency), 3)
            if success:
                self.prev_latency = latency
            try:
                database.record_ping(self.device["id"], ts, latency, success, jitter)
            except Exception:
                log.exception("failed to record ping for %s", self.device["host"])
            self.last_latency = latency
            self.last_jitter = jitter
            self.last_ts = ts
            if success and ts >= self._mac_next:
                self._refresh_mac(ts)
            if success and ts >= self._checks_next:
                self._checks_next = ts + 30
                threading.Thread(target=self._run_checks, args=(ts,),
                                 daemon=True, name=f"checks-{self.device['id']}"
                                 ).start()
            self._update_state(ts, success)
            # sleep the remainder of the interval
            elapsed = time.time() - started
            self.stop_event.wait(max(0.05, self.interval() - elapsed))

    def _refresh_mac(self, ts):
        """Pick up the device's MAC from the neighbour table (same-subnet only).

        Unknown MACs are retried every 5 min; known ones re-checked hourly.
        A change is recorded as an event — it usually means a swapped device,
        a DHCP re-assignment or an IP conflict.
        """
        known = self.device.get("mac")
        self._mac_next = ts + (3600 if known else 300)
        try:
            host = self.device["host"]
            if self._resolved_ip is None:
                self._resolved_ip = socket.gethostbyname(host)
            mac = lookup_mac(self._resolved_ip)
        except (socket.gaierror, OSError):
            return
        if not mac or mac == known:
            return
        database.set_device_mac(self.device["id"], mac, ts)
        self.device["mac"] = mac
        if known:
            detail = (f"MAC address changed {known} → {mac} — possible device "
                      f"swap, DHCP re-assignment or IP conflict")
            database.record_event(self.device["id"], ts, "mac-change", detail)
            log.warning("%s %s", self.device["name"], detail)
        else:
            log.info("%s MAC learned: %s", self.device["name"], mac)

    def _run_checks(self, ts):
        """TCP port / HTTP / DNS checks; record events on ok->fail transitions."""
        host = self.device["host"]
        new = {}
        # DNS timing (only meaningful for hostnames)
        try:
            socket.inet_aton(host)
        except OSError:
            ok, ms, detail = netcheck.dns_check(host)
            new["dns"] = {"ok": ok, "ms": ms, "detail": detail, "ts": ts}
        # TCP ports
        for port in netcheck.parse_ports(self.device.get("tcp_ports")):
            ok, ms, detail = netcheck.tcp_check(host, port)
            new[f"tcp:{port}"] = {"ok": ok, "ms": ms, "detail": detail, "ts": ts}
        # HTTP(S)
        url = (self.device.get("check_url") or "").strip()
        if url:
            ok, ms, code, cert_days, detail = netcheck.http_check(url)
            new["http"] = {"ok": ok, "ms": ms, "detail": detail,
                           "code": code, "cert_days": cert_days, "ts": ts}
            if cert_days is not None and cert_days <= settings.get("cert_warn_days"):
                new["http"]["cert_warn"] = True
        # emit change events
        for kind, res in new.items():
            prev = self._check_state.get(kind)
            if prev is not None and prev != res["ok"]:
                label = {"dns": "DNS", "http": "HTTP"}.get(kind, kind.upper())
                if res["ok"]:
                    database.record_event(self.device["id"], ts, "check-up",
                                          f"{label} recovered ({res['detail']})")
                else:
                    eid = database.record_event(self.device["id"], ts, "check-down",
                                                f"{label} check failed: {res['detail']}")
                    if settings.get("alert_check"):
                        self.monitor.emailer.check_failed(
                            dict(self.device), ts, label, res["detail"])
                    if self.monitor.webhooks:
                        self.monitor.webhooks.check_failed(
                            dict(self.device), ts, label, res["detail"])
            self._check_state[kind] = res["ok"]
        self.checks = new

    def _update_state(self, ts, success):
        if success:
            self.consecutive_fails = 0
            if self.state == "down":
                downtime = ts - self.down_since if self.down_since else 0
                detail = f"recovered after {_fmt_duration(downtime)} down"
                database.record_event(self.device["id"], ts, "up", detail)
                log.info("%s UP (%s)", self.device["name"], detail)
                self.monitor.emailer.device_recovered(self.device, ts, downtime)
                if self.monitor.webhooks:
                    self.monitor.webhooks.device_recovered(self.device, ts, downtime)
            self.state = "up"
            self.down_since = None
        else:
            self.consecutive_fails += 1
            if (self.state != "down"
                    and self.consecutive_fails >= settings.get("fail_threshold")):
                self.state = "down"
                self.down_since = ts
                detail = f"{self.consecutive_fails} consecutive failed pings"
                event_id = database.record_event(self.device["id"], ts, "down", detail)
                log.warning("%s DOWN (%s)", self.device["name"], detail)
                send_alert = False
                cooldown = settings.get("alert_cooldown_min") * 60
                if ts - self.last_down_alert >= cooldown:
                    self.last_down_alert = ts
                    send_alert = True
                # traceroute + correlation happen off-thread so pinging never stalls
                threading.Thread(
                    target=self._down_followup,
                    args=(dict(self.device), ts, detail, event_id, send_alert),
                    daemon=True, name=f"followup-{self.device['id']}").start()

    def _down_followup(self, device, ts, detail, event_id, send_alert):
        trace = None
        if settings.get("traceroute_on_fail"):
            trace = run_traceroute(device["host"])
            if trace:
                try:
                    database.set_event_trace(event_id, trace)
                except Exception:
                    log.exception("failed to store traceroute")
        corr = correlation_note()
        if corr:
            try:
                database.append_event_detail(
                    event_id, f" · {corr} devices affected within 2 min — "
                              f"possible upstream/shared issue")
            except Exception:
                log.exception("failed to append correlation note")
        if send_alert:
            self.monitor.emailer.device_down(device, ts, detail,
                                             trace=trace, correlated=corr)
            if self.monitor.webhooks:
                self.monitor.webhooks.device_down(device, ts, detail, correlated=corr)

    def stop(self):
        self.stop_event.set()


class Monitor:
    def __init__(self, emailer, webhooks=None):
        self.emailer = emailer
        self.webhooks = webhooks
        self.workers = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._manage, daemon=True,
                                        name="ping-manager")
        self._in_loss = set()           # device ids currently in a loss episode
        self._last_loss_alert = {}      # device id -> ts of last loss email

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        for w in self.workers.values():
            w.stop()

    def _manage(self):
        last_purge = 0.0
        last_loss_check = 0.0
        last_rogue = 0.0
        while not self._stop.is_set():
            try:
                self._reconcile()
            except Exception:
                log.exception("reconcile failed")
            if time.time() - last_loss_check > 60:
                last_loss_check = time.time()
                try:
                    self._check_loss()
                except Exception:
                    log.exception("loss check failed")
            if (settings.get("rogue_scan_enabled")
                    and time.time() - last_rogue > settings.get("rogue_scan_interval_min") * 60):
                last_rogue = time.time()
                threading.Thread(target=self._rogue_scan, daemon=True,
                                 name="rogue-scan").start()
            if time.time() - last_purge > 3600:
                last_purge = time.time()
                try:
                    database.purge_old(settings.get("retention_days"))
                except Exception:
                    log.exception("purge failed")
                try:
                    from . import capture
                    capture.purge_old_captures()
                except Exception:
                    log.exception("capture purge failed")
            self._stop.wait(3)

    def _reconcile(self):
        devices = {d["id"]: d for d in database.list_devices(enabled_only=True)}
        # stop workers for removed/disabled devices
        for dev_id in list(self.workers):
            if dev_id not in devices:
                self.workers.pop(dev_id).stop()
                self._in_loss.discard(dev_id)
        # start / refresh workers
        for dev_id, dev in devices.items():
            w = self.workers.get(dev_id)
            if w is None or not w.is_alive():
                w = DeviceWorker(dev, self)
                self.workers[dev_id] = w
                w.start()
            else:
                if w.device.get("host") != dev.get("host"):
                    w._resolved_ip = None   # host edited: re-resolve + re-learn MAC
                    w._mac_next = 0.0
                w.device = dev   # pick up host/interval edits live

    def _check_loss(self):
        """Flag sustained partial packet loss on devices that are still 'up'."""
        if not settings.get("monitoring_enabled"):
            return
        threshold = settings.get("loss_threshold_pct")
        window = settings.get("loss_window_min") * 60
        now = time.time()
        for dev_id, w in list(self.workers.items()):
            if w.state == "down":
                self._in_loss.discard(dev_id)
                continue
            sent, fails = database.loss_stats(dev_id, now - window, now)
            if sent < 10:          # not enough samples to judge
                continue
            loss = fails * 100.0 / sent
            if loss >= threshold and dev_id not in self._in_loss:
                self._in_loss.add(dev_id)
                detail = (f"{loss:.1f}% packet loss over the last "
                          f"{settings.get('loss_window_min')} min "
                          f"({fails}/{sent} pings lost)")
                event_id = database.record_event(dev_id, now, "loss", detail)
                log.warning("%s LOSS (%s)", w.device["name"], detail)
                corr = correlation_note()
                if corr:
                    database.append_event_detail(
                        event_id, f" · {corr} devices affected within 2 min — "
                                  f"possible upstream/shared issue")
                cooldown = settings.get("alert_cooldown_min") * 60
                if (settings.get("alert_loss")
                        and now - self._last_loss_alert.get(dev_id, 0) >= cooldown):
                    self._last_loss_alert[dev_id] = now
                    trace = None
                    if settings.get("traceroute_on_fail"):
                        trace = run_traceroute(w.device["host"])
                        if trace:
                            database.set_event_trace(event_id, trace)
                    self.emailer.device_loss(dict(w.device), now, loss,
                                             settings.get("loss_window_min"),
                                             trace=trace, correlated=corr)
                    if self.webhooks:
                        self.webhooks.device_loss(dict(w.device), now, loss,
                                                  settings.get("loss_window_min"),
                                                  correlated=corr)
            elif dev_id in self._in_loss and loss < threshold / 2:
                self._in_loss.discard(dev_id)
                database.record_event(
                    dev_id, now, "loss-clear",
                    f"packet loss back to {loss:.1f}% — episode over")
                log.info("%s loss cleared (%.1f%%)", w.device["name"], loss)

    def _rogue_scan(self):
        """Sweep the subnet; email when a new MAC appears (after baseline)."""
        from . import netdiag
        cidr = settings.get("rogue_scan_subnet").strip() or netdiag.local_subnet()
        try:
            found = netdiag.discover(cidr)
        except ValueError as e:
            log.warning("rogue scan: %s", e)
            return
        ts = time.time()
        had_baseline = database.known_device_count() > 0
        for d in found:
            if not d["mac"]:
                continue
            is_new = database.seen_device(d["mac"], d["ip"], d["vendor"], ts)
            if is_new and had_baseline:
                log.warning("ROGUE new device %s (%s, %s)",
                            d["ip"], d["mac"], d["vendor"] or "unknown vendor")
                if settings.get("alert_rogue"):
                    self.emailer.rogue_device(d, ts)
                if self.webhooks:
                    self.webhooks.rogue_device(d, ts)
        log.info("rogue scan of %s complete: %d hosts, %d known",
                 cidr, len(found), database.known_device_count())

    def status(self):
        """Live status per device id."""
        out = {}
        for dev_id, w in self.workers.items():
            out[dev_id] = {
                "state": w.state,
                "last_latency": w.last_latency,
                "last_jitter": w.last_jitter,
                "last_ts": w.last_ts,
                "consecutive_fails": w.consecutive_fails,
                "down_since": w.down_since,
                "in_loss": dev_id in self._in_loss,
                "checks": w.checks,
            }
        return out


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
