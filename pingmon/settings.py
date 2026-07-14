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
