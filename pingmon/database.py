"""SQLite storage layer for the Ascom Network Monitor."""
import os
import sqlite3
import threading
import time

_local = threading.local()


def _default_data_dir():
    if os.name == "nt":     # Windows: keep data in ProgramData
        return os.path.join(os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
                            "AscomPingMonitor")
    return "/var/lib/ascom-ping-monitor"


DATA_DIR = os.environ.get("PINGMON_DATA") or _default_data_dir()
DB_PATH = os.path.join(DATA_DIR, "pingmon.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_override REAL,           -- seconds; NULL = use global interval
    sort INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS pings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    ts REAL NOT NULL,                 -- unix epoch (UTC)
    latency REAL,                     -- ms; NULL when failed
    success INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pings_dev_ts ON pings(device_id, ts);
CREATE INDEX IF NOT EXISTS idx_pings_ts ON pings(ts);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL,
    ts REAL NOT NULL,
    type TEXT NOT NULL,               -- 'down' | 'up'
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS report_state (
    kind TEXT PRIMARY KEY,            -- '6' | '12' | '24'
    last_sent REAL NOT NULL
);
"""


def get_db():
    """One connection per thread, WAL mode."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(DATA_DIR, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def _ensure_column(db, table, col, ddl):
    cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    db = get_db()
    db.executescript(SCHEMA)
    # migrations for databases created by older versions
    _ensure_column(db, "pings", "jitter", "jitter REAL")
    _ensure_column(db, "events", "trace", "trace TEXT")
    _ensure_column(db, "devices", "warn_override", "warn_override REAL")
    _ensure_column(db, "devices", "crit_override", "crit_override REAL")
    _ensure_column(db, "devices", "mac", "mac TEXT")
    _ensure_column(db, "devices", "mac_ts", "mac_ts REAL")
    _ensure_column(db, "devices", "tcp_ports", "tcp_ports TEXT")
    _ensure_column(db, "devices", "check_url", "check_url TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS known_devices (
        mac TEXT PRIMARY KEY,
        ip TEXT, vendor TEXT, name TEXT,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0)""")
    db.commit()


# ---------- devices ----------

def list_devices(enabled_only=False):
    q = "SELECT * FROM devices"
    if enabled_only:
        q += " WHERE enabled=1"
    q += " ORDER BY sort, id"
    return [dict(r) for r in get_db().execute(q).fetchall()]


def get_device(dev_id):
    r = get_db().execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    return dict(r) if r else None


def add_device(name, host, enabled=1, interval_override=None):
    db = get_db()
    cur = db.execute(
        "INSERT INTO devices(name, host, enabled, interval_override, sort, created_at)"
        " VALUES(?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM devices),?)",
        (name, host, int(enabled), interval_override, time.time()))
    db.commit()
    return cur.lastrowid


def update_device(dev_id, **fields):
    allowed = {"name", "host", "enabled", "interval_override", "sort",
               "warn_override", "crit_override", "tcp_ports", "check_url"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(dev_id)
    db = get_db()
    db.execute(f"UPDATE devices SET {', '.join(sets)} WHERE id=?", vals)
    db.commit()


def set_device_mac(dev_id, mac, ts):
    db = get_db()
    db.execute("UPDATE devices SET mac=?, mac_ts=? WHERE id=?", (mac, ts, dev_id))
    db.commit()


def delete_device(dev_id):
    db = get_db()
    db.execute("DELETE FROM devices WHERE id=?", (dev_id,))
    db.execute("DELETE FROM pings WHERE device_id=?", (dev_id,))
    db.execute("DELETE FROM events WHERE device_id=?", (dev_id,))
    db.commit()


# ---------- pings ----------

def record_ping(device_id, ts, latency, success, jitter=None):
    db = get_db()
    db.execute("INSERT INTO pings(device_id, ts, latency, success, jitter)"
               " VALUES(?,?,?,?,?)",
               (device_id, ts, latency, 1 if success else 0, jitter))
    db.commit()


def history(start, end, max_points=500):
    """Bucketed history for all enabled devices between start/end epochs.

    Returns {device_id: [[bucket_ts, avg, max, fails, count], ...]}
    """
    span = max(end - start, 1)
    bucket = max(1, int(span / max_points))
    db = get_db()
    rows = db.execute(
        """SELECT device_id,
                  CAST((ts - ?) / ? AS INTEGER) AS b,
                  AVG(latency) AS avg_l, MAX(latency) AS max_l,
                  SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fails,
                  COUNT(*) AS n,
                  AVG(jitter) AS avg_j
           FROM pings WHERE ts >= ? AND ts <= ?
           GROUP BY device_id, b ORDER BY b""",
        (start, bucket, start, end)).fetchall()
    out = {}
    for r in rows:
        ts = start + r["b"] * bucket + bucket / 2
        out.setdefault(r["device_id"], []).append([
            round(ts, 1),
            round(r["avg_l"], 2) if r["avg_l"] is not None else None,
            round(r["max_l"], 2) if r["max_l"] is not None else None,
            r["fails"], r["n"],
            round(r["avg_j"], 2) if r["avg_j"] is not None else None])
    return out, bucket


def device_stats(device_id, start, end, warn_ms, crit_ms):
    r = get_db().execute(
        """SELECT COUNT(*) AS sent,
                  SUM(success) AS ok,
                  AVG(latency) AS avg_l, MIN(latency) AS min_l, MAX(latency) AS max_l,
                  AVG(jitter) AS avg_j,
                  SUM(CASE WHEN success=1 AND latency > ? AND latency <= ? THEN 1 ELSE 0 END) AS warns,
                  SUM(CASE WHEN success=1 AND latency > ? THEN 1 ELSE 0 END) AS crits
           FROM pings WHERE device_id=? AND ts >= ? AND ts <= ?""",
        (warn_ms, crit_ms, crit_ms, device_id, start, end)).fetchone()
    return dict(r)


def bad_pings(start, end, warn_ms, crit_ms, limit):
    """Failed pings and pings above each device's effective warning threshold.

    warn_ms/crit_ms are the global defaults; per-device overrides are applied.
    """
    rows = get_db().execute(
        """SELECT p.ts, p.latency, p.success, d.name, d.host,
                  COALESCE(d.warn_override, ?) AS eff_warn,
                  COALESCE(d.crit_override, ?) AS eff_crit
           FROM pings p JOIN devices d ON d.id = p.device_id
           WHERE p.ts >= ? AND p.ts <= ?
             AND (p.success = 0 OR p.latency > COALESCE(d.warn_override, ?))
           ORDER BY p.ts LIMIT ?""",
        (warn_ms, crit_ms, start, end, warn_ms, limit + 1)).fetchall()
    truncated = len(rows) > limit
    return [dict(r) for r in rows[:limit]], truncated


def loss_stats(device_id, start, end):
    """Sent/failed counts for the packet-loss checker."""
    r = get_db().execute(
        """SELECT COUNT(*) AS sent,
                  SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fails
           FROM pings WHERE device_id=? AND ts >= ? AND ts <= ?""",
        (device_id, start, end)).fetchone()
    return r["sent"] or 0, r["fails"] or 0


def heatmap(device_id, start, end):
    """Hour-of-day grid: rows keyed by local date, columns 0-23.

    Returns [[date, hour, avg, max, loss_pct, jitter, count], ...]
    """
    rows = get_db().execute(
        """SELECT date(ts,'unixepoch','localtime') AS d,
                  CAST(strftime('%H', ts,'unixepoch','localtime') AS INTEGER) AS h,
                  AVG(latency) AS avg_l, MAX(latency) AS max_l,
                  SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS loss,
                  AVG(jitter) AS avg_j,
                  COUNT(*) AS n
           FROM pings WHERE device_id=? AND ts >= ? AND ts <= ?
           GROUP BY d, h ORDER BY d, h""",
        (device_id, start, end)).fetchall()
    return [[r["d"], r["h"],
             round(r["avg_l"], 1) if r["avg_l"] is not None else None,
             round(r["max_l"], 1) if r["max_l"] is not None else None,
             round(r["loss"], 2),
             round(r["avg_j"], 1) if r["avg_j"] is not None else None,
             r["n"]] for r in rows]


def sla_report(start, end, default_warn, default_crit):
    """Per-device uptime, latency and outage summary for the SLA page."""
    out = []
    span = max(end - start, 1)
    for d in list_devices():
        warn = d.get("warn_override") or default_warn
        crit = d.get("crit_override") or default_crit
        s = device_stats(d["id"], start, end, warn, crit)
        # reconstruct outages from down/up events
        evs = get_db().execute(
            """SELECT ts, type FROM events
               WHERE device_id=? AND ts >= ? AND ts <= ? AND type IN ('down','up')
               ORDER BY ts""", (d["id"], start, end)).fetchall()
        outages, down_at = [], None
        for e in evs:
            if e["type"] == "down" and down_at is None:
                down_at = e["ts"]
            elif e["type"] == "up" and down_at is not None:
                outages.append((down_at, e["ts"], e["ts"] - down_at))
                down_at = None
        if down_at is not None:                      # still down at window end
            outages.append((down_at, None, end - down_at))
        downtime = sum(o[2] for o in outages)
        sent = s["sent"] or 0
        ok = s["ok"] or 0
        out.append({
            "id": d["id"], "name": d["name"], "host": d["host"],
            "enabled": d["enabled"],
            "uptime_pct": round(100.0 * (1 - downtime / span), 3),
            "downtime_s": round(downtime),
            "outage_count": len(outages),
            "outages": sorted(outages, key=lambda o: -o[2])[:5],
            "sent": sent,
            "loss_pct": round((sent - ok) / sent * 100, 2) if sent else None,
            "avg_ms": round(s["avg_l"], 1) if s["avg_l"] is not None else None,
            "max_ms": round(s["max_l"], 1) if s["max_l"] is not None else None,
            "jitter_ms": round(s["avg_j"], 1) if s["avg_j"] is not None else None,
            "warns": s["warns"] or 0, "crits": s["crits"] or 0,
        })
    return out


def recent_problem_devices(since_ts):
    """Distinct devices with a down/loss event since since_ts (for correlation)."""
    rows = get_db().execute(
        """SELECT DISTINCT device_id FROM events
           WHERE ts >= ? AND type IN ('down','loss')""", (since_ts,)).fetchall()
    return [r["device_id"] for r in rows]


def seen_device(mac, ip, vendor, ts):
    """Upsert a device seen on the LAN. Returns True if it's brand new."""
    db = get_db()
    row = db.execute("SELECT mac FROM known_devices WHERE mac=?", (mac,)).fetchone()
    if row:
        db.execute("UPDATE known_devices SET ip=?, last_seen=?, "
                   "vendor=COALESCE(vendor,?) WHERE mac=?", (ip, ts, vendor, mac))
        db.commit()
        return False
    db.execute("INSERT INTO known_devices(mac, ip, vendor, first_seen, last_seen) "
               "VALUES(?,?,?,?,?)", (mac, ip, vendor, ts, ts))
    db.commit()
    return True


def list_known_devices():
    return [dict(r) for r in get_db().execute(
        "SELECT * FROM known_devices ORDER BY last_seen DESC").fetchall()]


def acknowledge_device(mac):
    db = get_db()
    db.execute("UPDATE known_devices SET acknowledged=1 WHERE mac=?", (mac,))
    db.commit()


def known_device_count():
    r = get_db().execute("SELECT COUNT(*) AS n FROM known_devices").fetchone()
    return r["n"]


def set_event_trace(event_id, trace):
    db = get_db()
    db.execute("UPDATE events SET trace=? WHERE id=?", (trace, event_id))
    db.commit()


def append_event_detail(event_id, extra):
    db = get_db()
    db.execute("UPDATE events SET detail = COALESCE(detail,'') || ? WHERE id=?",
               (extra, event_id))
    db.commit()


def last_ping(device_id):
    r = get_db().execute(
        "SELECT ts, latency, success FROM pings WHERE device_id=? ORDER BY ts DESC LIMIT 1",
        (device_id,)).fetchone()
    return dict(r) if r else None


def purge_old(retention_days):
    cutoff = time.time() - retention_days * 86400
    db = get_db()
    db.execute("DELETE FROM pings WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
    db.commit()


# ---------- events ----------

def record_event(device_id, ts, etype, detail=""):
    db = get_db()
    cur = db.execute(
        "INSERT INTO events(device_id, ts, type, detail) VALUES(?,?,?,?)",
        (device_id, ts, etype, detail))
    db.commit()
    return cur.lastrowid


def list_events(limit=200, start=None, end=None):
    q = ("SELECT e.*, d.name, d.host FROM events e "
         "JOIN devices d ON d.id = e.device_id")
    cond, vals = [], []
    if start is not None:
        cond.append("e.ts >= ?"); vals.append(start)
    if end is not None:
        cond.append("e.ts <= ?"); vals.append(end)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY e.ts DESC LIMIT ?"
    vals.append(limit)
    return [dict(r) for r in get_db().execute(q, vals).fetchall()]


# ---------- settings / report state ----------

def get_setting_raw(key):
    r = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else None


def set_setting_raw(key, value):
    db = get_db()
    db.execute("INSERT INTO settings(key, value) VALUES(?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    db.commit()


def get_report_state(kind):
    r = get_db().execute("SELECT last_sent FROM report_state WHERE kind=?", (kind,)).fetchone()
    return r["last_sent"] if r else None


def set_report_state(kind, ts):
    db = get_db()
    db.execute("INSERT INTO report_state(kind, last_sent) VALUES(?,?) "
               "ON CONFLICT(kind) DO UPDATE SET last_sent=excluded.last_sent", (kind, ts))
    db.commit()
