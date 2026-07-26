"""Telligence configuration database (Dukane ESM / SQL Server) connector.

The runtime IMT bridge database only knows a faulty device's logical *address*.
Its device *type* and *serial number* live in the Telligence SQL Server database
(``DukaneESMMessages``), which is usually on localhost but not always. This
module opens a read-only connection to it and exposes:

* connection test,
* schema introspection (list tables, describe a table, sample rows) — used to
  discover where the device inventory lives, and safe because table names are
  validated against the live catalogue and every query is TOP-limited,
* (once the schema is known) a device lookup by address / location.

Two drivers are supported, tried in order: ``pyodbc`` (best on Windows — handles
both Windows/trusted auth and SQL logins, needs an ODBC Driver for SQL Server,
which a SQL Server install provides) and ``python-tds`` (pure Python, SQL auth).
If neither is importable the feature reports that instead of crashing.
"""
import logging

from . import settings

log = logging.getLogger("pingmon.teldb")

try:
    import pyodbc
    _PYODBC_ERR = None
except Exception as e:      # pragma: no cover
    pyodbc = None
    _PYODBC_ERR = f"{type(e).__name__}: {e}"

try:
    import pytds
    _PYTDS_ERR = None
except Exception as e:      # pragma: no cover
    pytds = None
    _PYTDS_ERR = f"{type(e).__name__}: {e}"


def drivers_available():
    return {"pyodbc": pyodbc is not None, "pytds": pytds is not None}


def load_cfg():
    g = settings.get
    return {
        "enabled": bool(g("tel_db_enabled")),
        "host": (g("tel_db_host") or "localhost").strip(),
        "instance": (g("tel_db_instance") or "").strip(),
        "port": int(g("tel_db_port") or 1433),
        "name": (g("tel_db_name") or "DukaneESMMessages").strip(),
        "auth": (g("tel_db_auth") or "windows").strip().lower(),
        "user": (g("tel_db_user") or "").strip(),
        "password": g("tel_db_password") or "",
    }


def _odbc_driver():
    """Pick the best installed SQL Server ODBC driver."""
    prefer = ["ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server",
              "ODBC Driver 13 for SQL Server", "SQL Server Native Client 11.0",
              "SQL Server"]
    have = set(pyodbc.drivers())
    for d in prefer:
        if d in have:
            return d
    for d in have:                     # any SQL Server driver as a last resort
        if "SQL Server" in d:
            return d
    return None


class _Conn:
    """Tiny wrapper so callers use the same .query() over either driver."""
    def __init__(self, raw, kind):
        self.raw = raw
        self.kind = kind

    def query(self, sql, params=(), limit=None):
        cur = self.raw.cursor()
        try:
            cur.execute(sql, params) if params else cur.execute(sql)
            cols = [c[0] for c in cur.description] if cur.description else []
            rows = cur.fetchmany(limit) if limit else cur.fetchall()
            out = []
            for r in rows:
                out.append({cols[i]: _clean(r[i]) for i in range(len(cols))})
            return out
        finally:
            cur.close()

    def close(self):
        try:
            self.raw.close()
        except Exception:
            pass


def _clean(v):
    # JSON-safe scalars
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return str(v)


def connect(cfg, timeout=6):
    """Open a read-only connection. Raises RuntimeError with a clear message."""
    if pyodbc is not None:
        drv = _odbc_driver()
        if not drv:
            if pytds is None:
                raise RuntimeError("no SQL Server ODBC driver found on this host "
                                   "(install 'ODBC Driver 18 for SQL Server')")
        else:
            inst = cfg["instance"]
            if inst.upper() in ("MSSQLSERVER", "DEFAULT"):
                inst = ""                    # the default instance is addressed by host alone
            server = cfg["host"]
            if inst:
                server += "\\" + inst        # named instance → SQL Browser resolves it
            elif cfg["port"] and int(cfg["port"]) != 1433:
                server += "," + str(cfg["port"])   # only force a port if it's non-default
            # (a bare 'localhost' lets the driver use shared memory / named pipes,
            #  so it still connects even when TCP/IP is disabled on SQL Server)
            parts = [f"DRIVER={{{drv}}}", f"SERVER={server}",
                     f"DATABASE={cfg['name']}", "Encrypt=no",
                     "TrustServerCertificate=yes", f"Connection Timeout={timeout}"]
            if cfg["auth"] == "sql":
                parts += [f"UID={cfg['user']}", f"PWD={cfg['password']}"]
            else:
                parts.append("Trusted_Connection=yes")
            return _Conn(pyodbc.connect(";".join(parts), timeout=timeout), "pyodbc")
    if pytds is not None:
        if cfg["auth"] != "sql":
            raise RuntimeError("Windows authentication needs the pyodbc driver; "
                               "with python-tds only, use SQL authentication")
        kw = dict(dsn=cfg["host"], database=cfg["name"], user=cfg["user"],
                  password=cfg["password"], login_timeout=timeout, timeout=timeout)
        if cfg["instance"]:
            kw["instance"] = cfg["instance"]     # SQL Browser resolves the port
        else:
            kw["port"] = cfg["port"]
        return _Conn(pytds.connect(**kw), "pytds")
    raise RuntimeError("no SQL driver installed — add 'pyodbc' or 'python-tds' "
                       f"(pyodbc: {_PYODBC_ERR}; pytds: {_PYTDS_ERR})")


# ---- introspection (read-only, safe) ----

def test_connection():
    cfg = load_cfg()
    c = connect(cfg)
    try:
        ver = c.query("SELECT @@VERSION AS v")[0]["v"]
        n = c.query("SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.TABLES")[0]["n"]
        return {"ok": True, "driver": c.kind, "database": cfg["name"],
                "tables": n, "server_version": (ver or "").splitlines()[0]}
    finally:
        c.close()


def list_tables():
    c = connect(load_cfg())
    try:
        return c.query(
            "SELECT TABLE_SCHEMA AS [schema], TABLE_NAME AS name "
            "FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE' "
            "ORDER BY TABLE_NAME")
    finally:
        c.close()


def _valid_table(c, table):
    """Return the exact (schema, name) if `table` exists, else None. Prevents
    injection — only names actually present in the catalogue are ever used."""
    for t in c.query("SELECT TABLE_SCHEMA AS s, TABLE_NAME AS n "
                     "FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE'"):
        if t["n"].lower() == table.lower():
            return t["s"], t["n"]
    return None


def describe_table(table):
    c = connect(load_cfg())
    try:
        tv = _valid_table(c, table)
        if not tv:
            raise RuntimeError("no such table: " + table)
        return c.query(
            "SELECT COLUMN_NAME AS name, DATA_TYPE AS type, "
            "CHARACTER_MAXIMUM_LENGTH AS len, IS_NULLABLE AS nullable "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME=? "
            "ORDER BY ORDINAL_POSITION", (tv[1],))
    finally:
        c.close()


def sample_table(table, limit=20):
    limit = max(1, min(200, int(limit)))
    c = connect(load_cfg())
    try:
        tv = _valid_table(c, table)
        if not tv:
            raise RuntimeError("no such table: " + table)
        # table/schema names come from the live catalogue (whitelisted above)
        return c.query(f"SELECT TOP {limit} * FROM [{tv[0]}].[{tv[1]}]")
    finally:
        c.close()


def search_columns(term):
    """Find columns whose name contains `term` (e.g. 'serial', 'type') — a fast
    way to locate the device inventory."""
    c = connect(load_cfg())
    try:
        return c.query(
            "SELECT TABLE_NAME AS [table], COLUMN_NAME AS column, DATA_TYPE AS type "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE COLUMN_NAME LIKE ? "
            "ORDER BY TABLE_NAME, COLUMN_NAME", ("%" + term + "%",))
    finally:
        c.close()
