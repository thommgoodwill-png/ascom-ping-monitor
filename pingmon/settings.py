"""Application settings with defaults. Everything toggleable lives here."""
import json
import re
import threading
import time

from . import database

# ---- hard-coded GUI credentials (as requested) ----
GUI_USERNAME = "ascom"
GUI_PASSWORD = "ascom!12345"

DEFAULTS = {
    # -------- monitoring --------
    "monitoring_enabled": True,     # master on/off for all pinging
    "ping_interval": 5.0,           # seconds between pings (1 - 60)
    "ping_timeout": 2,              # seconds to wait for a reply (1 - 10)
    "fail_threshold": 3,            # consecutive failures before device is DOWN
    "warn_ms": 50.0,                # latency above this = warning (orange)
    "crit_ms": 100.0,               # latency above this = critical (red)
    "retention_days": 30,           # how long to keep ping history
    "ping_size": 56,                # ICMP payload bytes (large sizes expose MTU issues)
    "jitter_warn_ms": 30.0,         # avg jitter above this is flagged

    # -------- problem detection --------
    "alert_loss": True,             # email on sustained packet loss (device still up)
    "loss_threshold_pct": 10.0,     # % loss over the window that triggers the alert
    "loss_window_min": 15,          # sliding window for loss detection
    "traceroute_on_fail": True,     # run traceroute when a device goes down/lossy
    "correlate_min_devices": 3,     # >= this many devices failing together = upstream flag
    "alert_check": True,            # email when a TCP/HTTP/DNS check fails
    "cert_warn_days": 21,           # warn when a TLS cert expires within N days

    # -------- discovery / rogue devices --------
    "rogue_scan_enabled": False,    # periodically sweep the subnet for new devices
    "rogue_scan_subnet": "",        # CIDR, or blank = auto-detect the local /24
    "rogue_scan_interval_min": 30,  # minutes between sweeps
    "alert_rogue": True,            # email when a new MAC appears (after baseline)

    # -------- maintenance window (daily) --------
    "maint_enabled": False,         # suppress ALL alert emails during the window
    "maint_start": "01:00",         # HH:MM local time
    "maint_end": "03:00",           # HH:MM local time (may wrap past midnight)

    # -------- email (Gmail) --------
    "email_enabled": False,         # master on/off for ALL email
    "gmail_user": "",               # full gmail address
    "gmail_app_password": "",       # 16-char Google app password
    "email_recipients": "",         # comma-separated list
    "report_6h": True,              # rolling 6-hour report on/off
    "report_12h": True,             # rolling 12-hour report on/off
    "report_24h": True,             # rolling 24-hour report on/off
    "report_skip_clean": False,     # skip a report entirely if there were no issues
    "report_max_rows": 200,         # cap on bad-ping rows per report email
    "alert_down": True,             # email when a device goes down
    "alert_recovery": True,         # email when a device recovers
    "alert_cooldown_min": 15,       # min minutes between repeat down-alerts per device

    # -------- webhooks (Teams / Discord / Slack / generic) --------
    "webhooks_enabled": False,      # master on/off for outbound webhooks
    "wh_url": "",                   # the webhook URL
    "wh_platform": "teams",         # teams | discord | slack | generic
    "wh_down": True,                # post on device down
    "wh_recovery": True,            # post on recovery
    "wh_loss": True,                # post on packet loss
    "wh_check": True,               # post on service-check failure
    "wh_rogue": True,               # post on new/rogue device

    # -------- IMT bridge (reads the bridge's own DB + log on the server) --------
    # The Ascom/Telligence IMT bridge keeps its live state in a small SQLite
    # database and streams device supervision/failure events to a log file.
    # We read both directly — no RabbitMQ, no proprietary protocol. The reader
    # must run where those files are reachable (the Telligence server itself, or
    # a share). On a customer site the Windows agent reads them locally and
    # pushes the results up to the controller under that site.
    "imt_enabled": False,           # master on/off for the IMT bridge monitor
    # Default to the standard Ascom install location; override per-server if the
    # bridge lives elsewhere. Reader stays off until imt_enabled is switched on.
    "imt_db_path": r"C:\Program Files (x86)\Ascom\IMT Bridge\Db\ImtBridgeDb.db3",
    "imt_log_path": r"C:\Program Files (x86)\Ascom\IMT Bridge\ImtBridge.log4net",
    # Telligence config cache (SQLite) — device type / IP / MAC for the fault
    # drill-down. Read-only, on demand (not polled).
    "imt_config_db_path": r"C:\Program Files (x86)\Ascom\IMT Bridge\Db\ConfigDb3.db3",
    "imt_poll_secs": 8,             # how often to re-read the DB and tail the log
                                    # (log tail is incremental + cheap; faults come
                                    # from the log so this mainly bounds call latency)
    "imt_alert": True,              # fire a webhook when an IMT device fails / recovers
    "imt_emergency_alert": False,   # fire a webhook when an Emergency / WC call is raised
                                    # (all other calls are display-only on the board)
    # -------- IMT bridge SERVICE health --------
    # Watch that the bridge service itself is running. Primary signal is
    # write-freshness (the bridge writes its log/DB continuously while up); set a
    # Windows service name for an authoritative `sc query` check instead.
    "imt_service_check": True,       # monitor the bridge service up/down
    "imt_service_stale_secs": 180,   # no log/DB write for this long = service down
    "imt_service_name": "",          # optional Windows service name (authoritative)

    # -------- Telligence config database (Dukane ESM / SQL Server) --------
    # The runtime bridge DB only knows a faulty device's address; its type and
    # serial live in the Telligence SQL Server database (DukaneESMMessages).
    # Often on localhost, but not always — hence a full connection config.
    "tel_db_enabled": False,        # master on/off for the Telligence DB lookup
    "tel_db_host": "localhost",     # SQL Server host / IP
    "tel_db_instance": "",          # named instance (e.g. TELLIGENCE); blank = default
    "tel_db_port": 1433,            # TCP port (ignored when an instance is named)
    "tel_db_name": "DukaneESMMessages",
    "tel_db_auth": "windows",       # windows (trusted) | sql
    "tel_db_user": "",              # SQL login (sql auth only)
    "tel_db_password": "",          # SQL password (masked in the API)

    # -------- local Telligence wallboards (standalone install, no controller) --
    # Secret token for the no-login duty-area wallboards when this instance reads
    # its own bridge directly (site_id=None). Sites use sites.wall_token instead.
    "local_wall_token": "",

    # -------- ASCII call feed (dutyarea|position|location|callstate over IP) --------
    # Streams one delimited line per call transition to a third-party receiver
    # (display board / paging gateway / logger). Purely outbound — a Reset line
    # only reflects that the nurse-call system cleared the call.
    "feed_enabled": False,          # master on/off for the call feed
    "feed_mode": "tcp_client",      # tcp_client (connect out) | tcp_server (listen) | udp
    "feed_host": "",                # destination host / IP (tcp_client + udp)
    "feed_port": 0,                 # destination / listen port
    "feed_eol": "cr",               # line ending: cr (\r) | crlf | lf
    "feed_clear_text": "Reset",     # callstate word sent when a call clears
    "feed_heartbeat_enabled": False,  # master on/off for the keepalive line
    "feed_heartbeat_secs": 30,      # keepalive line every N seconds
    "feed_heartbeat_text": "HEARTBEAT",   # the keepalive line content

    # -------- packet capture --------
    "capture_enabled": False,       # master on/off for tcpdump packet capture
    "capture_max_seconds": 60,      # UI upper bound for a single capture
    "capture_max_packets": 5000,    # UI upper bound for a single capture

    # -------- access control (admin) --------
    "allowed_emails": "",           # comma/newline list of allowed email patterns
                                    # (wildcards, e.g. *@ascom.com). Blank = allow all.
    "require_2fa": False,           # force every user to set up 2FA before access

    # -------- interface --------
    "default_theme": "auto",        # auto | light | dark
    "refresh_seconds": 30,          # dashboard auto-refresh (0 = off)
    "wallboard_refresh": 10,        # wallboard auto-refresh seconds
}

CLAMPS = {
    "ping_interval": (0.2, 60.0),
    "ping_timeout": (1, 10),
    "fail_threshold": (1, 20),
    "warn_ms": (1.0, 10000.0),
    "crit_ms": (1.0, 10000.0),
    "retention_days": (1, 365),
    "report_max_rows": (10, 2000),
    "alert_cooldown_min": (0, 1440),
    "refresh_seconds": (0, 3600),
    "ping_size": (16, 1472),
    "jitter_warn_ms": (1.0, 1000.0),
    "loss_threshold_pct": (1.0, 100.0),
    "loss_window_min": (2, 120),
    "correlate_min_devices": (2, 50),
    "wallboard_refresh": (2, 300),
    "capture_max_seconds": (1, 300),
    "capture_max_packets": (10, 100000),
    "cert_warn_days": (1, 365),
    "rogue_scan_interval_min": (5, 1440),
    "imt_poll_secs": (1, 3600),
    "imt_service_stale_secs": (30, 3600),
    "tel_db_port": (1, 65535),
    "feed_port": (0, 65535),
    "feed_heartbeat_secs": (0, 3600),
}

_HHMM = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")

_cache = {}
_lock = threading.Lock()


def _coerce(key, value):
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.lower() in ("1", "true", "on", "yes")
        return bool(value)
    if isinstance(default, float):
        value = float(value)
    elif isinstance(default, int):
        value = int(float(value))
    else:
        value = str(value)
        if key in ("maint_start", "maint_end") and not _HHMM.match(value):
            value = default
    if key in CLAMPS:
        lo, hi = CLAMPS[key]
        value = max(lo, min(hi, value))
    return value


def get(key):
    with _lock:
        if key in _cache:
            return _cache[key]
    raw = database.get_setting_raw(key)
    if raw is None:
        value = DEFAULTS[key]
    else:
        try:
            value = _coerce(key, json.loads(raw))
        except (ValueError, TypeError):
            value = DEFAULTS[key]
    with _lock:
        _cache[key] = value
    return value


def set(key, value):
    if key not in DEFAULTS:
        raise KeyError(key)
    value = _coerce(key, value)
    database.set_setting_raw(key, json.dumps(value))
    with _lock:
        _cache[key] = value
    return value


def all_settings():
    return {k: get(k) for k in DEFAULTS}


def update(payload):
    """Apply a dict of settings; returns applied values. Keeps warn < crit sane."""
    applied = {}
    for k, v in payload.items():
        if k in DEFAULTS:
            applied[k] = set(k, v)
    # keep thresholds ordered
    if get("crit_ms") <= get("warn_ms"):
        applied["crit_ms"] = set("crit_ms", get("warn_ms") + 1)
    return applied


def in_maintenance(now=None):
    """True while the daily maintenance window is active (alerts suppressed)."""
    if not get("maint_enabled"):
        return False
    lt = time.localtime(now if now is not None else time.time())
    cur = lt.tm_hour * 60 + lt.tm_min
    sh, sm = map(int, get("maint_start").split(":"))
    eh, em = map(int, get("maint_end").split(":"))
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= cur < end
    return cur >= start or cur < end      # window wraps past midnight
