"""SQLite storage layer for the Ascom Network Monitor."""
import os
import secrets
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
    _ensure_column(db, "devices", "site_id", "site_id INTEGER")  # NULL = hub-local
    _ensure_column(db, "devices", "hub_id", "hub_id INTEGER")    # agent: mirrors hub device
    # agent: 1 = this device was pulled DOWN from the hub (a mirror); 0/NULL =
    # added locally on the agent and pushed UP to the hub.
    _ensure_column(db, "devices", "from_hub", "from_hub INTEGER DEFAULT 0")
    # running tally of failed ping events for this device (reset on 'clear data')
    _ensure_column(db, "devices", "fail_total", "fail_total INTEGER NOT NULL DEFAULT 0")
    # optional flag colour for the device tile (hex, e.g. #e5484d; NULL = default)
    _ensure_column(db, "devices", "tile_color", "tile_color TEXT")
    db.execute("""CREATE TABLE IF NOT EXISTS known_devices (
        mac TEXT PRIMARY KEY,
        ip TEXT, vendor TEXT, name TEXT,
        first_seen REAL NOT NULL, last_seen REAL NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0)""")
    # ---- multi-tenant: customers -> sites -> devices ----
    db.execute("""CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, notes TEXT, created_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        customer_id INTEGER NOT NULL,
        name TEXT NOT NULL, notes TEXT,
        api_key TEXT NOT NULL UNIQUE,
        created_at REAL NOT NULL,
        last_seen REAL,
        agent_version TEXT,
        agent_host TEXT)""")
    _ensure_column(db, "sites", "agent_diag", "agent_diag TEXT")  # agent self-report (JSON)
    # per-site agent defaults (NULL = inherit the controller global). Safe to
    # change live — they only affect ping cadence / display thresholds, never
    # the agent's connection.
    _ensure_column(db, "sites", "ping_interval", "ping_interval REAL")
    _ensure_column(db, "sites", "warn_ms", "warn_ms REAL")
    _ensure_column(db, "sites", "crit_ms", "crit_ms REAL")
    # secret token for the no-login Telligence duty-area wallboards
    _ensure_column(db, "sites", "wall_token", "wall_token TEXT")
    # ---- users / roles / 2FA ----
    db.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'standard',      -- 'admin' | 'standard'
        email TEXT,
        totp_secret TEXT,
        totp_enabled INTEGER NOT NULL DEFAULT 0,
        disabled INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        last_login REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS invites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        token TEXT NOT NULL UNIQUE,
        email TEXT,
        role TEXT NOT NULL DEFAULT 'standard',
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        accepted INTEGER NOT NULL DEFAULT 0,
        created_by TEXT)""")
    # ---- floor plans (heatmap overlay) ----
    db.execute("""CREATE TABLE IF NOT EXISTS floorplans (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,                 -- NULL = hub-local scope
        name TEXT NOT NULL,
        ext TEXT NOT NULL,               -- stored image extension (png/jpg/svg)
        w INTEGER, h INTEGER,
        sort INTEGER DEFAULT 0,
        created_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS floorplan_pins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        floorplan_id INTEGER NOT NULL,
        device_id INTEGER NOT NULL,
        x REAL NOT NULL,                 -- 0..1 relative to image width
        y REAL NOT NULL)""")
    # ---- IMT bridge devices + status events (read from the bridge DB + log) ----
    # The IMT integration was RabbitMQ-based in an earlier draft with no site
    # scoping. If we find that old shape (no site_id column) drop and rebuild —
    # there is no production IMT data to preserve, and the UNIQUE(ident) it used
    # can't be widened to per-site in place.
    _imt_cols = [r[1] for r in db.execute("PRAGMA table_info(imt_devices)")]
    if _imt_cols and "site_id" not in _imt_cols:
        db.execute("DROP TABLE IF EXISTS imt_devices")
        db.execute("DROP TABLE IF EXISTS imt_events")
    db.execute("""CREATE TABLE IF NOT EXISTS imt_devices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,                  -- NULL = this instance's local bridge
        ident TEXT NOT NULL,              -- stable key: LocationString / loc:<id> / ip:<addr>
        name TEXT,
        status TEXT NOT NULL DEFAULT 'unknown',   -- 'ok' | 'failed' | 'unknown'
        detail TEXT,
        location_text TEXT,               -- friendly room/device name
        location_string TEXT,             -- bus path e.g. 3-4-4-35-41
        location_id TEXT,                 -- IMT LocationId
        system_ip TEXT,                   -- Telligence system IP
        kind TEXT,                        -- DutyArea | Room | Device | System …
        raw TEXT,                         -- last raw log line / event
        first_seen REAL, last_seen REAL, last_change REAL,
        fail_count INTEGER NOT NULL DEFAULT 0)""")
    # NULL site_id (local) must still be unique per ident, so key on COALESCE.
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS imt_dev_site_ident "
               "ON imt_devices(COALESCE(site_id,0), ident)")
    db.execute("""CREATE TABLE IF NOT EXISTS imt_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,
        ident TEXT, name TEXT, ts REAL NOT NULL,
        status TEXT, detail TEXT)""")
    # ---- live nurse-call events (Patient Call, Emergency, WC, Presence …) ----
    # These are operational calls, NOT device faults. A call is raised (Set) and
    # later cleared (Clear); the pair shares an episode GUID.
    db.execute("""CREATE TABLE IF NOT EXISTS imt_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_id INTEGER,
        guid TEXT NOT NULL,               -- episode id (Set/Clear pair)
        code TEXT,                        -- numeric event code (EventString)
        event_text TEXT,                  -- 'Patient Call', 'Emergency', 'Cord Out' …
        category TEXT,                    -- emergency|wc|call|presence|staff|other
        priority TEXT,                    -- High | Low | Info … (from the bridge)
        location_string TEXT,             -- bus path
        location_text TEXT,               -- room address from the call
        location_id TEXT,
        name TEXT,                        -- friendly room name (from inventory if known)
        state TEXT NOT NULL DEFAULT 'active',   -- 'active' | 'cleared'
        raised_ts REAL, cleared_ts REAL,
        raw TEXT)""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS imt_call_site_guid "
               "ON imt_calls(COALESCE(site_id,0), guid)")
    db.commit()
    _seed_default_admin(db)


# ---------- IMT bridge devices ----------

def _imt_site_where(site_id):
    """(sql_fragment, params) matching a site scope, treating None as local."""
    if site_id is None:
        return "site_id IS NULL", ()
    return "site_id=?", (site_id,)


def imt_upsert_device(site_id, ident, name, status, detail, raw, ts,
                      location_text=None, location_string=None,
                      location_id=None, system_ip=None, kind=None,
                      authoritative=True):
    """Record/refresh an IMT device.

    `authoritative` True means `status` is a real fault-state signal (from the
    log's Set/Clear stream) and should drive status changes. False means this is
    an inventory refresh (from the bridge DB) that may create the device and
    keep its metadata current but must NOT override a status the log is driving.

    Returns (status_changed, previous_status).
    """
    db = get_db()
    where, wp = _imt_site_where(site_id)
    row = db.execute(f"SELECT id, status FROM imt_devices WHERE {where} AND ident=?",
                     (*wp, ident)).fetchone()
    if row is None:
        st = status if authoritative else (status if status in ("ok", "failed") else "unknown")
        db.execute(
            "INSERT INTO imt_devices(site_id, ident, name, status, detail, "
            "location_text, location_string, location_id, system_ip, kind, raw, "
            "first_seen, last_seen, last_change, fail_count) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (site_id, ident, name, st, detail, location_text, location_string,
             location_id, system_ip, kind, raw, ts, ts, ts,
             1 if st == "failed" else 0))
        db.commit()
        return (st != "unknown", None)

    prev = row["status"]
    meta = ("name=COALESCE(?,name), location_text=COALESCE(?,location_text), "
            "location_string=COALESCE(?,location_string), "
            "location_id=COALESCE(?,location_id), system_ip=COALESCE(?,system_ip), "
            "kind=COALESCE(?,kind), last_seen=?")
    mp = (name, location_text, location_string, location_id, system_ip, kind, ts)

    if not authoritative:
        # inventory refresh: metadata only, never touches status
        db.execute(f"UPDATE imt_devices SET {meta} WHERE id=?", (*mp, row["id"]))
        db.commit()
        return (False, prev)

    changed = (status != prev)
    if changed:
        db.execute(
            f"UPDATE imt_devices SET {meta}, status=?, detail=?, raw=?, "
            "last_change=?, fail_count=fail_count+? WHERE id=?",
            (*mp, status, detail, raw, ts, 1 if status == "failed" else 0, row["id"]))
    else:
        db.execute(f"UPDATE imt_devices SET {meta}, detail=?, raw=? WHERE id=?",
                   (*mp, detail, raw, row["id"]))
    db.commit()
    return (changed, prev)


def imt_list_devices(site_id=None):
    where, wp = _imt_site_where(site_id)
    return [dict(r) for r in get_db().execute(
        f"SELECT * FROM imt_devices WHERE {where} ORDER BY "
        "CASE status WHEN 'failed' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END, "
        "name, ident", wp).fetchall()]


def imt_counts(site_id=None):
    where, wp = _imt_site_where(site_id)
    r = get_db().execute(
        "SELECT COUNT(*) AS total, "
        "SUM(status='failed') AS failed, SUM(status='ok') AS ok "
        f"FROM imt_devices WHERE {where}", wp).fetchone()
    return {"total": r["total"] or 0, "failed": r["failed"] or 0, "ok": r["ok"] or 0}


def imt_add_event(site_id, ident, name, status, detail, ts):
    db = get_db()
    db.execute("INSERT INTO imt_events(site_id, ident, name, ts, status, detail) "
               "VALUES(?,?,?,?,?,?)", (site_id, ident, name, ts, status, detail))
    db.commit()


def imt_list_events(site_id=None, limit=200):
    where, wp = _imt_site_where(site_id)
    return [dict(r) for r in get_db().execute(
        f"SELECT * FROM imt_events WHERE {where} ORDER BY ts DESC LIMIT ?",
        (*wp, limit)).fetchall()]


def imt_events_after(site_id, after_id, limit=500):
    """New IMT events by row id (for the agent to push up to the hub)."""
    where, wp = _imt_site_where(site_id)
    return [dict(r) for r in get_db().execute(
        f"SELECT * FROM imt_events WHERE {where} AND id>? ORDER BY id LIMIT ?",
        (*wp, after_id, limit)).fetchall()]


def imt_clear_devices(site_id=None):
    db = get_db()
    where, wp = _imt_site_where(site_id)
    db.execute(f"DELETE FROM imt_devices WHERE {where}", wp)
    db.execute(f"DELETE FROM imt_events WHERE {where}", wp)
    db.execute(f"DELETE FROM imt_calls WHERE {where}", wp)
    db.commit()


# ---------- IMT live calls ----------

_PRIO_ORDER = ("CASE priority WHEN 'High' THEN 0 WHEN 'Alarm' THEN 0 "
               "WHEN 'Low' THEN 1 WHEN 'Normal' THEN 1 ELSE 2 END")


def imt_call_set(site_id, guid, code, event_text, category, priority,
                 location_string, location_text, location_id, name, ts, raw):
    """Raise (or refresh) an active call. Returns True if it's newly active."""
    db = get_db()
    where, wp = _imt_site_where(site_id)
    row = db.execute(f"SELECT id, state FROM imt_calls WHERE {where} AND guid=?",
                     (*wp, guid)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO imt_calls(site_id, guid, code, event_text, category, "
            "priority, location_string, location_text, location_id, name, state, "
            "raised_ts, cleared_ts, raw) VALUES(?,?,?,?,?,?,?,?,?,?, 'active', ?, NULL, ?)",
            (site_id, guid, code, event_text, category, priority, location_string,
             location_text, location_id, name, ts, raw))
        db.commit()
        return True
    db.execute(
        "UPDATE imt_calls SET state='active', code=?, event_text=?, category=?, "
        "priority=?, location_string=COALESCE(?,location_string), "
        "location_text=COALESCE(?,location_text), location_id=COALESCE(?,location_id), "
        "name=COALESCE(?,name), raised_ts=?, cleared_ts=NULL, raw=? WHERE id=?",
        (code, event_text, category, priority, location_string, location_text,
         location_id, name, ts, raw, row["id"]))
    db.commit()
    return row["state"] != "active"


def imt_call_clear(site_id, guid, ts):
    """Clear an active call. Returns True if one was actually active."""
    db = get_db()
    where, wp = _imt_site_where(site_id)
    row = db.execute(f"SELECT id FROM imt_calls WHERE {where} AND guid=? "
                     "AND state='active'", (*wp, guid)).fetchone()
    if not row:
        return False
    db.execute("UPDATE imt_calls SET state='cleared', cleared_ts=? WHERE id=?",
               (ts, row["id"]))
    db.commit()
    return True


def imt_list_active_calls(site_id=None):
    where, wp = _imt_site_where(site_id)
    return [dict(r) for r in get_db().execute(
        f"SELECT * FROM imt_calls WHERE {where} AND state='active' "
        f"ORDER BY {_PRIO_ORDER}, raised_ts", wp).fetchall()]


def imt_list_recent_calls(site_id=None, limit=100):
    where, wp = _imt_site_where(site_id)
    return [dict(r) for r in get_db().execute(
        f"SELECT * FROM imt_calls WHERE {where} "
        "ORDER BY COALESCE(cleared_ts, raised_ts) DESC LIMIT ?",
        (*wp, limit)).fetchall()]


def imt_call_counts(site_id=None):
    where, wp = _imt_site_where(site_id)
    r = get_db().execute(
        "SELECT COUNT(*) AS active, "
        "SUM(priority IN ('High','Alarm')) AS emergency "
        f"FROM imt_calls WHERE {where} AND state='active'", wp).fetchone()
    return {"active": r["active"] or 0, "emergency": r["emergency"] or 0}


def imt_duty_areas(site_id=None):
    """The duty areas known for this scope (from the location inventory), each
    {string, name}. Used to split calls/faults per duty area."""
    where, wp = _imt_site_where(site_id)
    rows = get_db().execute(
        "SELECT location_string AS string, MAX(name) AS name FROM imt_devices "
        f"WHERE {where} AND kind='Duty Area' AND location_string IS NOT NULL "
        "GROUP BY location_string ORDER BY name", wp).fetchall()
    return [{"string": r["string"], "name": r["name"] or r["string"]} for r in rows]


def imt_calls_prune(site_id=None, keep_cleared=500):
    """Keep all active calls but bound the cleared history."""
    db = get_db()
    where, wp = _imt_site_where(site_id)
    ids = [row["id"] for row in db.execute(
        f"SELECT id FROM imt_calls WHERE {where} AND state='cleared' "
        "ORDER BY cleared_ts DESC LIMIT -1 OFFSET ?", (*wp, keep_cleared)).fetchall()]
    if ids:
        db.executemany("DELETE FROM imt_calls WHERE id=?", [(i,) for i in ids])
        db.commit()


def imt_clear_calls(site_id=None):
    db = get_db()
    where, wp = _imt_site_where(site_id)
    db.execute(f"DELETE FROM imt_calls WHERE {where}", wp)
    db.commit()


def imt_ingest_calls_from_agent(site_id, active, history):
    """Hub-side: replace a site's active calls with the agent's snapshot and
    fold in any freshly-cleared calls for the history."""
    db = get_db()
    where, wp = _imt_site_where(site_id)
    db.execute(f"DELETE FROM imt_calls WHERE {where} AND state='active'", wp)
    def _ins(c, state):
        db.execute(
            "INSERT INTO imt_calls(site_id, guid, code, event_text, category, "
            "priority, location_string, location_text, location_id, name, state, "
            "raised_ts, cleared_ts, raw) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (site_id, c.get("guid"), c.get("code"), c.get("event_text"),
             c.get("category"), c.get("priority"), c.get("location_string"),
             c.get("location_text"), c.get("location_id"), c.get("name"), state,
             c.get("raised_ts"), c.get("cleared_ts"), c.get("raw")))
    n = 0
    for c in (active or [])[:2000]:
        if not c.get("guid"):
            continue
        _ins(c, "active")
        n += 1
    for c in (history or [])[:2000]:
        g = c.get("guid")
        if not g:
            continue
        ex = db.execute(f"SELECT id FROM imt_calls WHERE {where} AND guid=?",
                        (*wp, g)).fetchone()
        if ex:
            db.execute("UPDATE imt_calls SET state='cleared', cleared_ts=? WHERE id=?",
                       (c.get("cleared_ts"), ex["id"]))
        else:
            _ins(c, "cleared")
    db.commit()
    imt_calls_prune(site_id)
    return {"active": n}


def imt_ingest_from_agent(site_id, devices, events):
    """Hub-side: absorb a batch of IMT devices + events pushed by a site agent.
    Device rows carry the agent's current status snapshot (authoritative), so
    the hub mirrors them under the given site."""
    n_dev = n_ev = 0
    for d in (devices or [])[:5000]:
        ident = (d.get("ident") or "").strip()
        if not ident:
            continue
        imt_upsert_device(
            site_id, ident, d.get("name"), d.get("status") or "unknown",
            d.get("detail"), d.get("raw"), float(d.get("last_change") or time.time()),
            location_text=d.get("location_text"),
            location_string=d.get("location_string"),
            location_id=d.get("location_id"), system_ip=d.get("system_ip"),
            kind=d.get("kind"), authoritative=True)
        n_dev += 1
    for e in (events or [])[:2000]:
        ident = (e.get("ident") or "").strip()
        if not ident:
            continue
        imt_add_event(site_id, ident, e.get("name") or ident,
                      e.get("status"), e.get("detail"),
                      float(e.get("ts") or time.time()))
        n_ev += 1
    return {"devices": n_dev, "events": n_ev}


# ---------- floor plans ----------

def add_floorplan(name, ext, site_id=None, w=None, h=None):
    db = get_db()
    cur = db.execute(
        "INSERT INTO floorplans(site_id, name, ext, w, h, sort, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (site_id, name, ext, w, h, time.time(), time.time()))
    db.commit()
    return cur.lastrowid


def list_floorplans(site_id="__all__"):
    q = "SELECT * FROM floorplans"
    cond, vals = [], []
    if site_id is None:
        cond.append("site_id IS NULL")
    elif site_id != "__all__":
        cond.append("site_id=?"); vals.append(site_id)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY sort, id"
    return [dict(r) for r in get_db().execute(q, vals).fetchall()]


def get_floorplan(fp_id):
    r = get_db().execute("SELECT * FROM floorplans WHERE id=?", (fp_id,)).fetchone()
    return dict(r) if r else None


def delete_floorplan(fp_id):
    db = get_db()
    db.execute("DELETE FROM floorplan_pins WHERE floorplan_id=?", (fp_id,))
    db.execute("DELETE FROM floorplans WHERE id=?", (fp_id,))
    db.commit()


def rename_floorplan(fp_id, name):
    db = get_db()
    db.execute("UPDATE floorplans SET name=? WHERE id=?", (name, fp_id))
    db.commit()


def list_pins(fp_id):
    return [dict(r) for r in get_db().execute(
        "SELECT * FROM floorplan_pins WHERE floorplan_id=? ORDER BY id",
        (fp_id,)).fetchall()]


def add_pin(fp_id, device_id, x, y):
    db = get_db()
    cur = db.execute(
        "INSERT INTO floorplan_pins(floorplan_id, device_id, x, y) VALUES(?,?,?,?)",
        (fp_id, device_id, x, y))
    db.commit()
    return cur.lastrowid


def move_pin(pin_id, x, y):
    db = get_db()
    db.execute("UPDATE floorplan_pins SET x=?, y=? WHERE id=?", (x, y, pin_id))
    db.commit()


def delete_pin(pin_id):
    db = get_db()
    db.execute("DELETE FROM floorplan_pins WHERE id=?", (pin_id,))
    db.commit()


def get_pin(pin_id):
    r = get_db().execute("SELECT * FROM floorplan_pins WHERE id=?", (pin_id,)).fetchone()
    return dict(r) if r else None


def device_drop_count(device_id, start, end):
    """Number of 'down' events for a device within a window — used to size the
    problem-area heat on floor plans."""
    r = get_db().execute(
        "SELECT COUNT(*) AS n FROM events WHERE device_id=? AND type='down' "
        "AND ts >= ? AND ts <= ?", (device_id, start, end)).fetchone()
    return r["n"] if r else 0


def device_status_events(device_id, start, end):
    """down/up events for a device in a window, oldest first — lets the history
    slider reconstruct up/down state at any point in time."""
    return [dict(r) for r in get_db().execute(
        "SELECT ts, type FROM events WHERE device_id=? AND type IN ('down','up') "
        "AND ts >= ? AND ts <= ? ORDER BY ts", (device_id, start, end)).fetchall()]


def _seed_default_admin(db):
    """Ensure the built-in local admin (ascom) always exists, so a local
    instance is never locked out. Created only if missing; never overwrites."""
    from . import auth, settings as _settings
    row = db.execute("SELECT id FROM users WHERE username=?",
                     (_settings.GUI_USERNAME,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO users(username, password_hash, role, email, created_at) "
            "VALUES(?,?,?,?,?)",
            (_settings.GUI_USERNAME, auth.hash_password(_settings.GUI_PASSWORD),
             "admin", "", time.time()))
        db.commit()


# ---------- devices ----------

def list_devices(enabled_only=False, site_id="__all__"):
    """List devices. site_id: '__all__' = every device; None = hub-local only;
    an int = that site's devices."""
    q = "SELECT * FROM devices"
    cond, vals = [], []
    if enabled_only:
        cond.append("enabled=1")
    if site_id is None:
        cond.append("site_id IS NULL")
    elif site_id != "__all__":
        cond.append("site_id=?"); vals.append(site_id)
    if cond:
        q += " WHERE " + " AND ".join(cond)
    q += " ORDER BY sort, id"
    return [dict(r) for r in get_db().execute(q, vals).fetchall()]


def get_device(dev_id):
    r = get_db().execute("SELECT * FROM devices WHERE id=?", (dev_id,)).fetchone()
    return dict(r) if r else None


def add_device(name, host, enabled=1, interval_override=None, site_id=None):
    db = get_db()
    cur = db.execute(
        "INSERT INTO devices(name, host, enabled, interval_override, sort, created_at, site_id)"
        " VALUES(?,?,?,?,(SELECT COALESCE(MAX(sort),0)+1 FROM devices),?,?)",
        (name, host, int(enabled), interval_override, time.time(), site_id))
    db.commit()
    return cur.lastrowid


def reorder_devices(ids):
    """Set the sort order of devices to match the given id sequence."""
    db = get_db()
    for i, did in enumerate(ids):
        db.execute("UPDATE devices SET sort=? WHERE id=?", (i, did))
    db.commit()


def update_device(dev_id, **fields):
    allowed = {"name", "host", "enabled", "interval_override", "sort",
               "warn_override", "crit_override", "tcp_ports", "check_url",
               "site_id", "hub_id", "from_hub", "tile_color"}
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


def clear_site_data(site_id):
    """Wipe stored ping history, events and the failed-ping tally for every
    device at a site — keeps the devices themselves. Returns rows cleared."""
    db = get_db()
    devs = [r["id"] for r in db.execute(
        "SELECT id FROM devices WHERE site_id=?", (site_id,)).fetchall()]
    if not devs:
        return {"devices": 0, "pings": 0, "events": 0}
    ph = ",".join("?" * len(devs))
    pings = db.execute(f"SELECT COUNT(*) FROM pings WHERE device_id IN ({ph})",
                       devs).fetchone()[0]
    events = db.execute(f"SELECT COUNT(*) FROM events WHERE device_id IN ({ph})",
                        devs).fetchone()[0]
    db.execute(f"DELETE FROM pings WHERE device_id IN ({ph})", devs)
    db.execute(f"DELETE FROM events WHERE device_id IN ({ph})", devs)
    db.execute(f"UPDATE devices SET fail_total=0 WHERE id IN ({ph})", devs)
    db.commit()
    return {"devices": len(devs), "pings": pings, "events": events}


# ---------- pings ----------

def record_ping(device_id, ts, latency, success, jitter=None):
    db = get_db()
    db.execute("INSERT INTO pings(device_id, ts, latency, success, jitter)"
               " VALUES(?,?,?,?,?)",
               (device_id, ts, latency, 1 if success else 0, jitter))
    if not success:
        db.execute("UPDATE devices SET fail_total=fail_total+1 WHERE id=?",
                   (device_id,))
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


def device_history(device_id, start, end, max_points=500):
    """Bucketed latency history for a SINGLE device (for the detail modal chart).
    Returns ([[bucket_ts, avg, max, fails, count, avg_jitter], ...], bucket_secs)."""
    span = max(end - start, 1)
    bucket = max(1, int(span / max_points))
    rows = get_db().execute(
        """SELECT CAST((ts - ?) / ? AS INTEGER) AS b,
                  AVG(latency) AS avg_l, MAX(latency) AS max_l,
                  SUM(CASE WHEN success=0 THEN 1 ELSE 0 END) AS fails,
                  COUNT(*) AS n, AVG(jitter) AS avg_j
           FROM pings WHERE device_id=? AND ts >= ? AND ts <= ?
           GROUP BY b ORDER BY b""",
        (start, bucket, device_id, start, end)).fetchall()
    out = []
    for r in rows:
        ts = start + r["b"] * bucket + bucket / 2
        out.append([round(ts, 1),
                    round(r["avg_l"], 2) if r["avg_l"] is not None else None,
                    round(r["max_l"], 2) if r["max_l"] is not None else None,
                    r["fails"], r["n"],
                    round(r["avg_j"], 2) if r["avg_j"] is not None else None])
    return out, bucket


def device_events(device_id, limit=50, since=None):
    """Recent down/up/loss events for one device, newest first."""
    q = "SELECT ts, type, detail FROM events WHERE device_id=?"
    vals = [device_id]
    if since is not None:
        q += " AND ts >= ?"; vals.append(since)
    q += " ORDER BY ts DESC LIMIT ?"; vals.append(limit)
    return [dict(r) for r in get_db().execute(q, vals).fetchall()]


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


def sla_report(start, end, default_warn, default_crit, site_id="__all__"):
    """Per-device uptime, latency and outage summary for the SLA page.
    site_id: '__all__' = every device; None = hub-local only; int = that site."""
    out = []
    span = max(end - start, 1)
    for d in list_devices(site_id=site_id):
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


# ---------- users ----------

def list_users():
    rows = get_db().execute(
        "SELECT id, username, role, email, totp_enabled, disabled, created_at, "
        "last_login FROM users ORDER BY username").fetchall()
    return [dict(r) for r in rows]


def get_user(user_id):
    r = get_db().execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(r) if r else None


def get_user_by_name(username):
    r = get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(r) if r else None


def add_user(username, password_hash, role="standard", email=""):
    db = get_db()
    cur = db.execute(
        "INSERT INTO users(username, password_hash, role, email, created_at) "
        "VALUES(?,?,?,?,?)", (username, password_hash, role, email, time.time()))
    db.commit()
    return cur.lastrowid


def update_user(user_id, **fields):
    allowed = {"password_hash", "role", "email", "totp_secret", "totp_enabled",
               "disabled", "last_login", "username"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return
    vals.append(user_id)
    db = get_db()
    db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", vals)
    db.commit()


def delete_user(user_id):
    db = get_db()
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()


def count_admins(exclude_id=None):
    q = "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0"
    vals = []
    if exclude_id is not None:
        q += " AND id<>?"; vals.append(exclude_id)
    return get_db().execute(q, vals).fetchone()["n"]


# ---------- invites ----------

def add_invite(token, email, role, expires_at, created_by):
    db = get_db()
    cur = db.execute(
        "INSERT INTO invites(token, email, role, created_at, expires_at, created_by) "
        "VALUES(?,?,?,?,?,?)",
        (token, email, role, time.time(), expires_at, created_by))
    db.commit()
    return cur.lastrowid


def get_invite(token):
    r = get_db().execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    return dict(r) if r else None


def list_invites(pending_only=True):
    q = "SELECT * FROM invites"
    if pending_only:
        q += " WHERE accepted=0 AND expires_at > " + str(time.time())
    q += " ORDER BY created_at DESC"
    return [dict(r) for r in get_db().execute(q).fetchall()]


def accept_invite(token):
    db = get_db()
    db.execute("UPDATE invites SET accepted=1 WHERE token=?", (token,))
    db.commit()


def delete_invite(invite_id):
    db = get_db()
    db.execute("DELETE FROM invites WHERE id=?", (invite_id,))
    db.commit()


# ---------- customers / sites (multi-tenant) ----------

def list_customers():
    rows = get_db().execute(
        """SELECT c.*,
              (SELECT COUNT(*) FROM sites s WHERE s.customer_id=c.id) AS site_count
           FROM customers c ORDER BY c.name""").fetchall()
    return [dict(r) for r in rows]


def get_customer(cid):
    r = get_db().execute("SELECT * FROM customers WHERE id=?", (cid,)).fetchone()
    return dict(r) if r else None


def add_customer(name, notes=""):
    db = get_db()
    cur = db.execute("INSERT INTO customers(name, notes, created_at) VALUES(?,?,?)",
                     (name, notes, time.time()))
    db.commit()
    return cur.lastrowid


def update_customer(cid, name=None, notes=None):
    db = get_db()
    if name is not None:
        db.execute("UPDATE customers SET name=? WHERE id=?", (name, cid))
    if notes is not None:
        db.execute("UPDATE customers SET notes=? WHERE id=?", (notes, cid))
    db.commit()


def delete_customer(cid):
    db = get_db()
    for s in db.execute("SELECT id FROM sites WHERE customer_id=?", (cid,)).fetchall():
        delete_site(s["id"])
    db.execute("DELETE FROM customers WHERE id=?", (cid,))
    db.commit()


def list_sites(customer_id=None):
    q = ("SELECT s.*, c.name AS customer_name, "
         "(SELECT COUNT(*) FROM devices d WHERE d.site_id=s.id) AS device_count "
         "FROM sites s JOIN customers c ON c.id=s.customer_id")
    vals = []
    if customer_id is not None:
        q += " WHERE s.customer_id=?"; vals.append(customer_id)
    q += " ORDER BY s.name"
    return [dict(r) for r in get_db().execute(q, vals).fetchall()]


def get_site(site_id):
    r = get_db().execute(
        "SELECT s.*, c.name AS customer_name FROM sites s "
        "JOIN customers c ON c.id=s.customer_id WHERE s.id=?", (site_id,)).fetchone()
    return dict(r) if r else None


def get_site_by_key(api_key):
    r = get_db().execute("SELECT * FROM sites WHERE api_key=?", (api_key,)).fetchone()
    return dict(r) if r else None


def add_site(customer_id, name, api_key, notes=""):
    db = get_db()
    cur = db.execute(
        "INSERT INTO sites(customer_id, name, notes, api_key, created_at) "
        "VALUES(?,?,?,?,?)", (customer_id, name, notes, api_key, time.time()))
    db.commit()
    return cur.lastrowid


def update_site(site_id, **fields):
    allowed = {"name", "notes", "api_key", "last_seen", "agent_version",
               "agent_host", "agent_diag", "ping_interval", "warn_ms", "crit_ms"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?"); vals.append(v)
    if not sets:
        return
    vals.append(site_id)
    db = get_db()
    db.execute(f"UPDATE sites SET {', '.join(sets)} WHERE id=?", vals)
    db.commit()


def get_or_create_wall_token(site_id):
    """The site's wallboard token, generating (and storing) one if absent."""
    db = get_db()
    r = db.execute("SELECT wall_token FROM sites WHERE id=?", (site_id,)).fetchone()
    if not r:
        return None
    tok = r["wall_token"]
    if not tok:
        tok = secrets.token_urlsafe(24)
        db.execute("UPDATE sites SET wall_token=? WHERE id=?", (tok, site_id))
        db.commit()
    return tok


def regenerate_wall_token(site_id):
    """Issue a fresh wallboard token, invalidating any existing shared links."""
    tok = secrets.token_urlsafe(24)
    db = get_db()
    db.execute("UPDATE sites SET wall_token=? WHERE id=?", (tok, site_id))
    db.commit()
    return tok


def site_by_wall_token(token):
    """Resolve a wallboard token to its site (read-only kiosk access). None if
    the token is blank/unknown."""
    if not token:
        return None
    r = get_db().execute(
        "SELECT s.*, c.name AS customer_name FROM sites s "
        "JOIN customers c ON c.id=s.customer_id WHERE s.wall_token=?",
        (token,)).fetchone()
    return dict(r) if r else None


def delete_site(site_id):
    db = get_db()
    devs = db.execute("SELECT id FROM devices WHERE site_id=?", (site_id,)).fetchall()
    for d in devs:
        db.execute("DELETE FROM pings WHERE device_id=?", (d["id"],))
        db.execute("DELETE FROM events WHERE device_id=?", (d["id"],))
    db.execute("DELETE FROM devices WHERE site_id=?", (site_id,))
    db.execute("DELETE FROM sites WHERE id=?", (site_id,))
    db.commit()


def max_ping_id():
    """Highest ping row id in the local DB (0 if none). Used by the agent to
    detect a stale push watermark after the local ping table was reset."""
    r = get_db().execute("SELECT MAX(id) AS m FROM pings").fetchone()
    return (r["m"] if r and r["m"] is not None else 0)


def first_ping_id_since(ts):
    """Smallest ping id with a timestamp >= ts (None if none). Lets the agent
    skip a large stale backlog and push only recent pings after reconnecting."""
    r = get_db().execute(
        "SELECT MIN(id) AS m FROM pings WHERE ts >= ?", (ts,)).fetchone()
    return (r["m"] if r and r["m"] is not None else None)


def record_pushed_pings(device_id, samples):
    """Bulk-insert ping samples an agent pushed. samples: [[ts,lat,success,jitter],…]"""
    if not samples:
        return
    db = get_db()
    db.executemany(
        "INSERT INTO pings(device_id, ts, latency, success, jitter) VALUES(?,?,?,?,?)",
        [(device_id, s[0], s[1], 1 if s[2] else 0,
          s[3] if len(s) > 3 else None) for s in samples])
    fails = sum(1 for s in samples if not s[2])
    if fails:
        db.execute("UPDATE devices SET fail_total=fail_total+? WHERE id=?",
                   (fails, device_id))
    db.commit()


def device_by_hub_id(hub_id):
    r = get_db().execute("SELECT * FROM devices WHERE hub_id=?", (hub_id,)).fetchone()
    return dict(r) if r else None


def pings_after(last_id, limit=2000):
    """Agent-side: local pings newer than a watermark, with their ids."""
    rows = get_db().execute(
        "SELECT id, device_id, ts, latency, success, jitter FROM pings "
        "WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit)).fetchall()
    return [dict(r) for r in rows]


def events_after(last_id, limit=500):
    rows = get_db().execute(
        "SELECT id, device_id, ts, type, detail FROM events "
        "WHERE id > ? ORDER BY id LIMIT ?", (last_id, limit)).fetchall()
    return [dict(r) for r in rows]


def max_ping_id():
    r = get_db().execute("SELECT COALESCE(MAX(id),0) AS m FROM pings").fetchone()
    return r["m"]


def max_event_id():
    r = get_db().execute("SELECT COALESCE(MAX(id),0) AS m FROM events").fetchone()
    return r["m"]


def touch_site(site_id, agent_version=None, agent_host=None, agent_diag=None):
    fields = {"last_seen": time.time()}
    if agent_version:
        fields["agent_version"] = agent_version
    if agent_host:
        fields["agent_host"] = agent_host
    if agent_diag is not None:
        fields["agent_diag"] = agent_diag
    update_site(site_id, **fields)


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


def list_events(limit=200, start=None, end=None, site_id="__all__"):
    """Events, optionally scoped by site. site_id: '__all__' = every device;
    None = hub-local devices only; an int = that site's devices."""
    q = ("SELECT e.*, d.name, d.host FROM events e "
         "JOIN devices d ON d.id = e.device_id")
    cond, vals = [], []
    if start is not None:
        cond.append("e.ts >= ?"); vals.append(start)
    if end is not None:
        cond.append("e.ts <= ?"); vals.append(end)
    if site_id is None:
        cond.append("d.site_id IS NULL")
    elif site_id != "__all__":
        cond.append("d.site_id = ?"); vals.append(site_id)
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
