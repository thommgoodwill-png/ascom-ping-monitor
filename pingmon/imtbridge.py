"""IMT bridge monitor — reads the Ascom/Telligence IMT bridge directly.

The IMT bridge connects IP room-bus devices (nurse-call stations, room
controllers and the hundreds of peripheral devices hanging off them). It keeps
its live state in a small SQLite database (``ImtBridgeDb.db3``) and streams every
supervision / failure event to a log file (``ImtBridge.log4net``). We read both:

* the **database** gives the device / location inventory and names, so every
  known room-bus location shows up even when it is healthy, and gives us a
  LocationId → LocationString map used to name faults consistently;
* the **log** carries the authoritative ``State: Set`` (failed) → ``State: Clear``
  (recovered) transitions, which drive each device's up/down status, the events
  list and the failure/recovery webhooks.

No RabbitMQ, no proprietary protocol, no licensed API — just the bridge's own
files. The reader must run where those files are reachable: on the Telligence
server itself, on a share, or (for a customer site) via the Windows agent, which
reads them locally and pushes the results up to the controller under its site.

How the log is decoded
----------------------
Each fault surfaces on two adjacent log lines with a shared UafId (``Id``):

* an **EventData** line — carries ``EventString`` + ``EventText`` (Supervision /
  Failure), the ``State`` (Set/Clear) and, crucially, the per-episode ``EventId``
  GUID that ties a Set to its later Clear. It often has *no* location.
* a **FullEvent** line — carries ``EventCategory`` and, when the fault is tied to
  a configured location, the ``LocationId`` + ``LocationText``.

We pair them by UafId to recover a fault's location, remember each episode's
GUID → device identity so the Clear resolves to the same device, and translate
LocationId → LocationString (via the DB inventory) so a device keys the same way
whether it was named on the EventData line, the FullEvent line, or the DB.

Everything recorded here is scoped to ``site_id=None`` (this instance's local
bridge). Per-site data on the controller arrives through the agent push path.
"""
import collections
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time

from . import database, proc, settings

log = logging.getLogger("pingmon.imt")

SITE_ID = None   # this reader always represents the LOCAL bridge for this instance
READER_VERSION = "2.28"  # bump on reader changes so the running build is identifiable
STATE_PATH = os.path.join(database.DATA_DIR, "imt_state.json")

# A log line starts with "2026-02-11 15:08:31,039 ".
_LINE_START = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}\b")
# The payload after "Received event:" is a run of "Key: Value" pairs.
_KV = re.compile(r"([A-Za-z]+):\s*(.*?)(?=,\s*[A-Za-z]+:\s|$)")
# The low-level NurseCall line, e.g.:
#   EventType = "NcSupervision", EventId = "1" with State = "Set" for
#   DutyAreaId = "4" from remote IpAddress = "192.168.1.100"
_UAF = re.compile(
    r'EventType = "(Nc\w+)", EventId = "([^"]*)" with State = "(\w+)" '
    r'for DutyAreaId = "([^"]*)" from remote IpAddress = "([^"]*)"')

# LocationType (int in the DB) -> friendly kind label
_KIND = {0: "System", 1: "Duty Area", 2: "Zone", 3: "Bay", 4: "Corridor",
         5: "Group", 6: "Station", 7: "Device"}
_NO_LOC = ("", "-1", "0", None)

# Event texts that mean a DEVICE FAULT (drive up/down + fault alerts). Everything
# else the bridge emits is treated as an operational nurse CALL.
FAULT_TEXTS = {"supervision", "failure"}


def classify_call(event_text, priority):
    """Bucket a call into a category used for colour/grouping. Generic — an
    unseen text falls back sensibly on its wording or the bridge's priority."""
    t = (event_text or "").lower()
    p = (priority or "").lower()
    if any(k in t for k in ("wc", "bath", "toilet", "shower", "washroom")):
        return "wc"
    if any(k in t for k in ("emergency", "cardiac", "code blue", "resus", "crash")):
        return "emergency"
    if "presence" in t or "attend" in t:
        return "presence"
    if "staff" in t:
        return "staff"
    if "cord" in t or "call" in t or "assist" in t:
        return "call"
    if p in ("high", "alarm"):
        return "emergency"
    return "other"


def load_cfg():
    g = settings.get
    return {
        "enabled": bool(g("imt_enabled")),
        "db_path": (g("imt_db_path") or "").strip(),
        "log_path": (g("imt_log_path") or "").strip(),
        "config_db_path": (g("imt_config_db_path") or "").strip(),
        "poll": int(g("imt_poll_secs") or 15),
        "alert": bool(g("imt_alert")),
    }


# --------------------------------------------------------------------------
# parsing helpers (pure — unit-testable)
# --------------------------------------------------------------------------

def parse_time_occurred(s):
    """'2026:02:11:23:10:22' -> epoch seconds (local). None on failure."""
    if not s:
        return None
    try:
        parts = [int(p) for p in str(s).strip().split(":")]
        if len(parts) != 6:
            return None
        y, mo, d, h, mi, se = parts
        return time.mktime((y, mo, d, h, mi, se, 0, 0, -1))
    except (ValueError, OverflowError):
        return None


def parse_line(line):
    """Parse one log line into a normalised record, or None.

    kind == 'eventdata'  -> a fault/call event (has EventString + State Set/Clear)
    kind == 'fullevent'  -> the paired record that may carry a LocationId
    kind == 'uaf'        -> the low-level NurseCall event: carries the device's
                            IpAddress + DutyAreaId. The device supervision/failure
                            EventData that follows has NO location, so this is the
                            only line that says WHICH device faulted.
    """
    if "Received EventData:" in line and "EventType =" in line:
        m = _UAF.search(line)
        if m:
            return {
                "kind": "uaf",
                "ncevent": m.group(1),          # NcSupervision | NcFailure | NcCall …
                "guid": m.group(2),
                "state": m.group(3).lower(),     # set | clear
                "duty_id": m.group(4),
                "ip": m.group(5),
                "ts": None,
            }
    if "Received event:" not in line:
        return None
    payload = line.split("Received event:", 1)[1]
    f = {}
    for m in _KV.finditer(payload):
        f[m.group(1).lower()] = m.group(2).strip()

    if "eventstring" in f:                          # EventData (fault) variant
        state = (f.get("state") or "").lower()
        et = f.get("eventtext") or ""
        if not et or state not in ("set", "clear"):
            return None
        return {
            "kind": "eventdata",
            "uafid": f.get("id") or "",
            "guid": f.get("eventid") or "",
            "code": f.get("eventstring") or "",
            "state": state,
            "event_text": et,
            "type": f.get("type") or "",
            "priority": f.get("priority") or "",
            "loc_string": f.get("locationstring") or "",
            "loc_text": f.get("locationtext") or "",
            "loc_id": f.get("locationid") or "",
            "ts": parse_time_occurred(f.get("timeoccurred")),
            "log_ts": parse_log_ts(line),
            "raw": line.strip()[:2000],
        }
    if "eventcategory" in f:                         # FullEvent (location) variant
        return {
            "kind": "fullevent",
            "uafid": f.get("id") or "",
            "state": (f.get("state") or "").lower(),
            "loc_id": f.get("locationid") or "",
            "loc_text": f.get("locationtext") or "",
            "event_text": f.get("eventtext") or "",
            "ts": parse_time_occurred(f.get("timeoccurred")),
        }
    return None


def _snapshot_connect(db_path):
    """Open a *current* read of a live bridge DB.

    The bridge keeps the DB in WAL mode: freshly-written fault rows sit in the
    ``-wal`` sidecar until a checkpoint folds them into the main file. Opening the
    file with SQLite's ``immutable`` flag ignores that WAL and reads a stale state
    (faults missing); opening it writable to read the WAL needs write access the
    monitor doesn't have under Program Files. So we copy the DB *and its WAL/SHM*
    to a temp file and open the copy — SQLite applies the WAL on open, giving the
    live committed state, and read access to the source is all that's required.
    Returns (connection, tempdir). Raises RuntimeError on failure."""
    try:
        tmp = tempfile.mkdtemp(prefix="imtdb_")
        snap = os.path.join(tmp, "snap.db3")
        shutil.copyfile(db_path, snap)
        for ext in ("-wal", "-shm"):
            src = db_path + ext
            if os.path.exists(src):
                try:
                    shutil.copyfile(src, snap + ext)
                except OSError:
                    pass
        con = sqlite3.connect(snap, timeout=5)
        con.row_factory = sqlite3.Row
        return con, tmp
    except (OSError, sqlite3.Error) as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")


def _snapshot_close(con, tmp):
    try:
        con.close()
    except Exception:
        pass
    shutil.rmtree(tmp, ignore_errors=True)


def read_inventory(db_path):
    """Read the location/device inventory from the bridge DB (read-only).
    Returns list of dicts, newest name per LocationString."""
    if not db_path or not os.path.exists(db_path):
        return []
    out = {}
    con, tmp = _snapshot_connect(db_path)
    try:
        rows = con.execute(
            "SELECT LocConfigId, LocationId, LocationText, LocationString, "
            "LocationType, TelligenceSystemIp FROM ActiveLocConfig "
            "ORDER BY LocConfigId").fetchall()
    except sqlite3.Error as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")
    finally:
        _snapshot_close(con, tmp)
    for r in rows:
        ls = (r["LocationString"] or "").strip()
        if not ls:
            continue
        out[ls] = {                                 # later rows (newer) win
            "ident": ls,
            "location_string": ls,
            "location_id": str(r["LocationId"]) if r["LocationId"] is not None else None,
            "location_text": (r["LocationText"] or "").strip() or ls,
            "system_ip": (r["TelligenceSystemIp"] or "").strip() or None,
            "kind": _KIND.get(r["LocationType"], str(r["LocationType"])),
        }
    return list(out.values())


def parse_log_ts(line):
    """The real local time from a log line's leading '2026-07-26 16:12:33,506'
    prefix. Preferred over the event's TimeOccurred, which is in UTC and so runs
    an hour behind during BST."""
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


def read_db_faults(db_path):
    """Current LOCATED device faults from the bridge DB's active-event tables.

    Rows live in ActiveEventData while a fault is active and are deleted when it
    clears, so this is the authoritative live fault state — the same thing the
    wall panels show, and it captures room-bus/peripheral disconnects that never
    reach the log stream. Returns {location_string: {...}}. Unlocated/system
    supervisions (LocationId -1, no real LocationString) are treated as noise and
    skipped."""
    if not db_path or not os.path.exists(db_path):
        return {}
    faults = {}
    con, tmp = _snapshot_connect(db_path)
    try:
        rows = []
        # Read BOTH active-event tables — a fault can appear in ActiveFullEvent
        # (the held-alarm table) without a matching ActiveEventData row.
        for tbl in ("ActiveEventData", "ActiveFullEvent"):
            try:
                rows += con.execute(
                    "SELECT EventGuid, EventText, LocationString, LocationText, "
                    f"TelligenceSystemIp FROM {tbl}").fetchall()
            except sqlite3.Error:
                pass                          # table absent/renamed — skip it
    finally:
        _snapshot_close(con, tmp)
    seen = set()
    for r in rows:
        guid = r["EventGuid"]                 # dedupe the same fault across tables
        if guid in seen:
            continue
        seen.add(guid)
        # only real device faults — never mistake a transient call row for one
        if (r["EventText"] or "").strip().lower() not in FAULT_TEXTS:
            continue
        ls = (r["LocationString"] or "").strip()
        if "-" not in ls:                    # unlocated / system-level → noise
            continue
        f = faults.setdefault(ls, {
            "location_string": ls,
            "location_text": (r["LocationText"] or "").strip() or ls,
            "count": 0,
            "event_text": r["EventText"] or "Fault",
            "system_ip": (r["TelligenceSystemIp"] or "").strip() or None,
        })
        f["count"] += 1
    return faults


def tail_fault_lines(log_path, max_bytes=200000, limit=40):
    """Diagnostic: the most recent located fault transitions in the log.

    Reads the tail of the file and returns the parsed ``Received event:``
    Supervision / Failure lines that carry a real LocationString, newest first.
    Lets the debug page show whether a live fault is actually in the log even
    when the bridge DB doesn't hold it."""
    if not log_path or not os.path.exists(log_path):
        return {"exists": False, "lines": []}
    size = os.path.getsize(log_path)
    start = max(0, size - max_bytes)
    out = []
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            if start:
                f.readline()                 # drop the partial first line
            for line in f:
                if "Received event:" not in line:
                    continue
                rec = parse_line(line)
                if not rec or rec["kind"] != "eventdata":
                    continue
                if (rec["event_text"] or "").strip().lower() not in FAULT_TEXTS:
                    continue
                ls = (rec["loc_string"] or "").strip()
                out.append({
                    "ts": line[:23],
                    "state": rec["state"],
                    "event_text": rec["event_text"],
                    "location_string": ls,
                    "location_text": rec["loc_text"],
                    "located": "-" in ls,
                    "guid": rec["guid"],
                })
    except OSError as e:
        return {"exists": True, "error": f"{type(e).__name__}: {e}", "lines": []}
    out.reverse()
    return {"exists": True, "size": size, "lines": out[:limit]}


def _ipfmt(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if not n:
        return ""
    return "%d.%d.%d.%d" % ((n >> 24) & 255, (n >> 16) & 255, (n >> 8) & 255, n & 255)


def _macfmt(n):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if not n:
        return ""
    h = format(n, "012x")[-12:]
    return ":".join(h[i:i + 2] for i in range(0, 12, 2)).upper()


def _parse_pd_name(name):
    """'NUEPS-HK Emergency Pull Switch Module G3168261' -> model / desc / serial."""
    parts = (name or "").split()
    if not parts:
        return {"model": "", "description": "", "serial": ""}
    serial = ""
    if len(parts) > 1 and re.match(r"^[A-Za-z]?\d{4,}$", parts[-1]):
        serial = parts[-1]
        parts = parts[:-1]
    return {"model": parts[0], "description": " ".join(parts[1:]), "serial": serial}


def read_fault_devices(db_path, config_db_path, location_string):
    """Drill-down for a faulted room. Returns:
      { room, reported_to:[covering devices + addresses], peripherals:[the real
        installed room devices with model + serial], gateway }.
    The covering device is who the fault is *reported to* (a console); the
    peripherals are the actual room-bus hardware that failed."""
    if not db_path or not os.path.exists(db_path) or not location_string:
        return {}
    con, tmp = _snapshot_connect(db_path)
    try:
        ed = con.execute(
            "SELECT e.EventId AS addr, e.EventText AS fault, e.LocationText AS ltext, "
            "cd.IpDeviceId AS ipid, cd.IpDeviceArchitectName AS covname "
            "FROM ActiveEventData e LEFT JOIN CoveringDeviceInfo cd "
            "ON cd.EventDataId = e.EventDataId WHERE e.LocationString = ?",
            (location_string,)).fetchall()
        try:
            fe = con.execute("SELECT EventId AS addr, EventText AS fault, "
                             "LocationText AS ltext FROM ActiveFullEvent "
                             "WHERE LocationString = ?", (location_string,)).fetchall()
        except sqlite3.Error:
            fe = []
    except sqlite3.Error as e:
        raise RuntimeError(f"{type(e).__name__}: {e}")
    finally:
        _snapshot_close(con, tmp)

    room = ""
    for r in list(ed) + list(fe):
        if (r["ltext"] or "").strip():
            room = r["ltext"].strip()
            break

    # covering ("reported to") devices, grouped, + config lookup
    types, devinfo = {}, {}
    peripherals, gateway = [], None
    if config_db_path and os.path.exists(config_db_path):
        try:
            ccon, ctmp = _snapshot_connect(config_db_path)
            try:
                for t in ccon.execute("SELECT IpDeviceType, ArchitectName "
                                      "FROM Dv1IpDevice WHERE IpDeviceId < 100"):
                    types[t["IpDeviceType"]] = t["ArchitectName"]
                for d in ccon.execute(
                        "SELECT IpDeviceId, IpDeviceType, IpAddress, MacAddress, "
                        "ArchitectName, HardwareVersion FROM Dv1IpDevice "
                        "WHERE IpDeviceId >= 100"):
                    devinfo[d["IpDeviceId"]] = dict(d)
                # bed / bathroom sub-locations within the room, so a faulted
                # room's peripherals group under "Bed area" vs "Ensuite /
                # bathroom" (Dcs1Beds = bed areas, Dcs1BedsBathroom = ensuites).
                beds = {}          # BedId -> {"label": str, "area": bed|bathroom}
                for bt, area in (("Dcs1Beds", "bed"), ("Dcs1BedsBathroom", "bathroom")):
                    try:
                        for b in ccon.execute("SELECT BedId, BedNumber, "
                                              "OverrideBedNumber, BedAlias "
                                              f"FROM {bt}"):
                            lbl = ((b["OverrideBedNumber"] or "").strip()
                                   or (b["BedAlias"] or "").strip()
                                   or (b["BedNumber"] or "").strip())
                            beds[b["BedId"]] = {"label": lbl, "area": area}
                    except sqlite3.Error:
                        pass
                # the room's actual installed peripherals: match room name -> the
                # verified virtual station(s) -> their peripheral devices
                if room:
                    vs = ccon.execute("SELECT VStationId, IpDeviceId FROM "
                                      "Dv1VirtualStation WHERE ArchitectName = ? "
                                      "COLLATE NOCASE", (room,)).fetchall()
                    vsids = [v["VStationId"] for v in vs]
                    gwids = [v["IpDeviceId"] for v in vs if v["IpDeviceId"]]
                    if vsids:
                        ph = ",".join("?" * len(vsids))
                        for p in ccon.execute(
                                "SELECT ArchitectName, PdType, HwVersion, SwVersion, "
                                f"BedId1 FROM Dv1PeripheralDevice WHERE VStationId IN ({ph})",
                                vsids):
                            nm = (p["ArchitectName"] or "").strip()
                            if not nm:
                                continue
                            pn = _parse_pd_name(nm)
                            bed = beds.get(p["BedId1"]) if p["BedId1"] else None
                            peripherals.append({
                                "name": nm, "model": pn["model"],
                                "description": pn["description"], "serial": pn["serial"],
                                "hw": p["HwVersion"], "sw": p["SwVersion"],
                                "area": bed["area"] if bed else "room",
                                "area_label": bed["label"] if bed else ""})
                    if gwids:
                        gi = devinfo.get(gwids[0])
                        if gi:
                            gateway = {"name": gi["ArchitectName"],
                                       "type": types.get(gi["IpDeviceType"], ""),
                                       "ip": _ipfmt(gi["IpAddress"]),
                                       "mac": _macfmt(gi["MacAddress"])}
            finally:
                _snapshot_close(ccon, ctmp)
        except (RuntimeError, sqlite3.Error):
            pass

    reported = {}
    for r in ed:
        ipid = r["ipid"]
        key = ipid if ipid is not None else ("cov:" + (r["covname"] or "?"))
        g = reported.get(key)
        if g is None:
            di = devinfo.get(ipid)
            g = {"name": (di["ArchitectName"] if di else None) or r["covname"]
                         or (f"IP device {ipid}" if ipid else "Console"),
                 "type": types.get(di["IpDeviceType"], "") if di else "",
                 "ip": _ipfmt(di["IpAddress"]) if di else "",
                 "mac": _macfmt(di["MacAddress"]) if di else "",
                 "addresses": []}
            reported[key] = g
        g["addresses"].append({"address": r["addr"], "fault": r["fault"]})

    # group peripherals by their bed / bathroom sub-location for the drill-down:
    # bed areas first, then ensuite / bathroom, then any room-level devices.
    _AREA_ORDER = {"bed": 0, "bathroom": 1, "room": 2}
    _AREA_TITLE = {"bed": "Bed area", "bathroom": "Ensuite / bathroom", "room": "Room"}
    grouped = {}
    for p in peripherals:
        title = p["area_label"] or _AREA_TITLE.get(p["area"], "Room")
        g = grouped.setdefault((p["area"], title), {
            "area": p["area"], "title": title, "devices": []})
        g["devices"].append(p)
    peripheral_groups = sorted(
        grouped.values(),
        key=lambda g: (_AREA_ORDER.get(g["area"], 9), g["title"]))

    return {"room": room, "reported_to": list(reported.values()),
            "peripherals": peripherals, "peripheral_groups": peripheral_groups,
            "gateway": gateway}


def read_bed_areas(config_db_path):
    """Map every BedId to its human sub-location: {BedId: {"area", "label"}}.
    ``area`` is ``Bed`` (Dcs1Beds) or ``Bathroom`` (Dcs1BedsBathroom); ``label``
    is the configured friendly name (e.g. ``Bedroom 1`` / ``Bedroom 1 Ensuite``).
    Used to turn a call's trailing LocationString segment into a friendly Bed /
    Bathroom position."""
    out = {}
    if not config_db_path or not os.path.exists(config_db_path):
        return out
    con, tmp = _snapshot_connect(config_db_path)
    try:
        for tbl, area in (("Dcs1Beds", "Bed"), ("Dcs1BedsBathroom", "Bathroom")):
            try:
                for b in con.execute("SELECT BedId, BedNumber, OverrideBedNumber, "
                                     f"BedAlias FROM {tbl}"):
                    lbl = ((b["OverrideBedNumber"] or "").strip()
                           or (b["BedAlias"] or "").strip()
                           or (b["BedNumber"] or "").strip())
                    out[str(b["BedId"])] = {"area": area, "label": lbl}
            except sqlite3.Error:
                pass
    finally:
        _snapshot_close(con, tmp)
    return out


_SERVICE_STATES = {1: "STOPPED", 2: "START_PENDING", 3: "STOP_PENDING",
                   4: "RUNNING", 5: "CONTINUE_PENDING", 6: "PAUSE_PENDING",
                   7: "PAUSED"}


def _query_service_api(name):
    """Service state straight from the Windows service control manager.

    Used in preference to shelling out to ``sc``: launching a console program
    from a windowed (``--noconsole``) build makes Windows allocate a console for
    it, which flashes a cmd box on screen every time the health check runs. This
    is an in-process API call — no child process, nothing to see, and faster."""
    import ctypes
    from ctypes import wintypes

    class SERVICE_STATUS(ctypes.Structure):
        _fields_ = [("dwServiceType", wintypes.DWORD),
                    ("dwCurrentState", wintypes.DWORD),
                    ("dwControlsAccepted", wintypes.DWORD),
                    ("dwWin32ExitCode", wintypes.DWORD),
                    ("dwServiceSpecificExitCode", wintypes.DWORD),
                    ("dwCheckPoint", wintypes.DWORD),
                    ("dwWaitHint", wintypes.DWORD)]

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    adv.OpenSCManagerW.restype = wintypes.HANDLE
    adv.OpenSCManagerW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR,
                                   wintypes.DWORD]
    adv.OpenServiceW.restype = wintypes.HANDLE
    adv.OpenServiceW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR,
                                 wintypes.DWORD]
    adv.QueryServiceStatus.argtypes = [wintypes.HANDLE,
                                       ctypes.POINTER(SERVICE_STATUS)]
    adv.CloseServiceHandle.argtypes = [wintypes.HANDLE]

    scm = adv.OpenSCManagerW(None, None, 0x0001)      # SC_MANAGER_CONNECT
    if not scm:
        return None
    try:
        svc = adv.OpenServiceW(scm, name, 0x0004)     # SERVICE_QUERY_STATUS
        if not svc:
            return None                               # no such service
        try:
            st = SERVICE_STATUS()
            if not adv.QueryServiceStatus(svc, ctypes.byref(st)):
                return None
            return _SERVICE_STATES.get(st.dwCurrentState, "UNKNOWN")
        finally:
            adv.CloseServiceHandle(svc)
    finally:
        adv.CloseServiceHandle(scm)


def _query_service(name):
    """Windows service state (authoritative). Returns RUNNING / STOPPED /
    START_PENDING / STOP_PENDING / UNKNOWN, or None if it can't be queried
    (not Windows, no such service). Never opens a console window."""
    if os.name != "nt" or not name:
        return None
    try:
        return _query_service_api(name)
    except Exception:                       # pragma: no cover - API unavailable
        pass
    # fall back to sc, with its console window suppressed
    import subprocess
    try:
        out = proc.run(["sc", "query", name], capture_output=True,
                       text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    t = (out.stdout or "").upper()
    for s in ("START_PENDING", "STOP_PENDING", "RUNNING", "STOPPED"):
        if s in t:
            return s
    return "UNKNOWN" if out.returncode == 0 else None


# The Ascom/Telligence IMT bridge runs as a Windows service (the log shows
# ImtBridgeCore.ImtBridgeService). We try these exact names first, then fall back
# to scanning all services for one whose name/display-name looks like the bridge.
_KNOWN_SERVICES = ("ImtBridge", "ImtBridgeCore", "ImtBridgeService",
                   "Ascom IMT Bridge", "AscomImtBridge")
_SERVICE_HINTS = ("imtbridge", "imt bridge", "telligence", "dukane",
                  "ascom")
_svc_cache = {"name": None, "ts": 0.0}
_svc_state_cache = {"name": None, "state": None, "ts": 0.0}


def _scan_services_registry():
    """Find a bridge-looking service by reading the service list out of the
    registry. Every installed service has a key under
    ``HKLM\\SYSTEM\\CurrentControlSet\\Services``, so this gives the same answer
    as ``sc query`` without starting a child process."""
    try:
        import winreg
    except ImportError:
        return None
    try:
        root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                              r"SYSTEM\CurrentControlSet\Services")
    except OSError:
        return None
    hits = []
    try:
        i = 0
        while True:
            try:
                name = winreg.EnumKey(root, i)
            except OSError:
                break                        # ran off the end
            i += 1
            text = name.lower()
            try:                             # match the friendly name too
                with winreg.OpenKey(root, name) as k:
                    text += " " + str(winreg.QueryValueEx(k, "DisplayName")[0]).lower()
            except OSError:
                pass
            rank = next((n for n, h in enumerate(_SERVICE_HINTS) if h in text), None)
            if rank is not None:
                hits.append((rank, name))
    finally:
        root.Close()
    # _SERVICE_HINTS is ordered most- to least-specific, so the best match wins
    # ("...ImtBridge..." beats a service that merely has "ascom" in its name).
    return min(hits)[1] if hits else None


def detect_bridge_service(ttl=300):
    """Best-effort auto-detection of the bridge's Windows service name so the
    health check works with no configuration. Cached (service names don't move).
    Returns the service name, or None (not Windows / not found)."""
    if os.name != "nt":
        return None
    now = time.time()
    if _svc_cache["name"] and now - _svc_cache["ts"] < ttl:
        return _svc_cache["name"]
    if now - _svc_cache["ts"] < ttl and _svc_cache["ts"]:
        return _svc_cache["name"]          # negative result cached too
    found = None
    for name in _KNOWN_SERVICES:           # 1) cheap: probe the likely names
        if _query_service(name) is not None:
            found = name
            break
    if not found:                          # 2) enumerate from the registry
        found = _scan_services_registry()   #    (in-process, no console window)
    if not found:                          # 3) last resort: enumerate via sc
        import subprocess
        try:
            out = proc.run(["sc", "query", "type=", "service", "state=", "all"],
                           capture_output=True, text=True, timeout=8)
            cur = None
            for line in (out.stdout or "").splitlines():
                s = line.strip()
                up = s.upper()
                if up.startswith("SERVICE_NAME:"):
                    cur = s.split(":", 1)[1].strip()
                    if any(h in cur.lower() for h in _SERVICE_HINTS):
                        found = cur
                        break
                elif up.startswith("DISPLAY_NAME:") and cur:
                    if any(h in s.split(":", 1)[1].lower() for h in _SERVICE_HINTS):
                        found = cur
                        break
        except (OSError, subprocess.SubprocessError):
            pass
    _svc_cache["name"] = found
    _svc_cache["ts"] = now
    return found


def bridge_health(cfg, stale_secs, service_name=""):
    """Is the IMT bridge service actually working right now?

    Checks the actual Windows service by default — the configured name if set,
    otherwise an auto-detected one. Only when no service can be found (e.g. the
    monitor runs on a different box from the bridge, or a non-Windows host) does
    it fall back to write-freshness: the bridge writes to its log/DB continuously
    while running, so if neither file has changed for longer than ``stale_secs``
    the service is stopped or hung."""
    now = time.time()
    name = (service_name or "").strip() or detect_bridge_service()
    if name:
        # The poll can run as often as once a second; a service starting or
        # stopping is a once-in-a-blue-moon event, so a few seconds of cache
        # costs nothing and keeps the check off the hot path.
        c = _svc_state_cache
        if c["name"] == name and now - c["ts"] < 5:
            state = c["state"]
        else:
            state = _query_service(name)
            c.update(name=name, state=state, ts=now)
        if state is not None:
            ok = state == "RUNNING"
            return {"status": "ok" if ok else "failed", "source": "service",
                    "service": name, "service_state": state,
                    "reason": f"service '{name}' is {state}",
                    "last_activity": now if ok else None,
                    "age": 0 if ok else None}
    times = []
    for p in (cfg.get("log_path"), cfg.get("db_path")):
        if p and os.path.exists(p):
            try:
                times.append(os.path.getmtime(p))
            except OSError:
                pass
    if not times:
        return {"status": "failed", "source": "files",
                "reason": "bridge log / database not found",
                "last_activity": None, "age": None}
    last = max(times)
    age = now - last
    if age > stale_secs:
        return {"status": "failed", "source": "files", "last_activity": last,
                "age": age,
                "reason": f"no bridge activity for {int(age)}s "
                          f"(log/DB not written — service likely stopped)"}
    return {"status": "ok", "source": "files", "last_activity": last, "age": age,
            "reason": f"writing (last {int(age)}s ago; no service found to query)"}


def _load_state():
    import json
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_state(st):
    import json
    try:
        os.makedirs(database.DATA_DIR, exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(st, f)
    except OSError:
        pass


class ImtBridge:
    def __init__(self, webhooks, feed=None):
        self.webhooks = webhooks
        self.feed = feed            # optional ASCII call feed (dutyarea|position|…)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="imt")
        self.connected = False
        self.last_error = None
        self.event_count = 0
        self.last_event_ts = None
        self.last_poll_ts = None
        self._offset = None
        self._log_path = None
        self._locid2str = {}        # LocationId -> LocationString (from the DB)
        self._loc_names = {}        # LocationString -> friendly room name (from the DB)
        self._duty_by_seg = {}      # DutyAreaId -> duty-area LocationString
        self._duty_areas = {}       # duty-area LocationString -> friendly name
        self._bed_area = {}         # BedId -> {"area": Bed|Bathroom, "label": …}
        self._bed_area_ts = 0       # when the bed map was last loaded
        self._ip_names = {}         # device IP -> friendly name (reserved / user map)
        self._guid_ident = {}       # fault GUID -> (ident, name) learned at Set
        self._ident_guids = {}      # fault ident -> set of active episode GUIDs
        self._first_fault_poll = True   # don't fire webhooks for the seed state
        self._first_service_poll = True # don't alert on the very first health read
        self.service_health = None      # last IMT-bridge service health dict
        self.recent = collections.deque(maxlen=100)

    # ---- lifecycle ----

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def status(self):
        c = load_cfg()
        return {
            "version": READER_VERSION,
            "enabled": c["enabled"],
            "configured": bool(c["db_path"] or c["log_path"]),
            "connected": self.connected,
            "last_error": self.last_error,
            "event_count": self.event_count,
            "last_event_ts": self.last_event_ts,
            "last_poll_ts": self.last_poll_ts,
            "db_path": c["db_path"],
            "log_path": c["log_path"],
            "poll": c["poll"],
            "service_health": self.service_health,
        }

    def recent_messages(self):
        return list(self.recent)

    # ---- main loop ----

    def _loop(self):
        st = _load_state()
        self._log_path = st.get("log_path")
        self._offset = st.get("offset")
        backoff = 3
        while not self._stop.is_set():
            cfg = load_cfg()
            if not cfg["enabled"] or not (cfg["db_path"] or cfg["log_path"]):
                self.connected = False
                self._stop.wait(5)
                continue
            try:
                self._poll(cfg)
                self.connected = True
                self.last_error = None
                self.last_poll_ts = time.time()
                backoff = 3
            except Exception as e:
                self.connected = False
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("IMT poll failed: %s", e)
                self._stop.wait(backoff)
                backoff = min(60, backoff * 2)
                continue
            self._stop.wait(max(1, cfg["poll"]))

    def _poll(self, cfg):
        # 0) is the bridge service itself alive? (runs first — needs no DB read)
        self._check_service(cfg)
        # 1) refresh inventory + LocationId->LocationString map from the DB
        if cfg["db_path"]:
            inv = read_inventory(cfg["db_path"])
            self._locid2str = {i["location_id"]: i["location_string"]
                               for i in inv if i["location_id"]}
            self._loc_names = {i["location_string"]: i["location_text"]
                               for i in inv if i["location_text"]}
            # DutyAreaId (e.g. "4") -> duty-area path ("3-4-4"): match on the
            # last path segment, kept only when it's unambiguous.
            seg = {}
            self._duty_areas = {}
            for i in inv:
                if i["kind"] == "Duty Area" and i["location_string"]:
                    s = i["location_string"].split("-")[-1]
                    seg.setdefault(s, []).append(i["location_string"])
                    self._duty_areas[i["location_string"]] = i["location_text"]
            self._duty_by_seg = {s: v[0] for s, v in seg.items() if len(v) == 1}
            # bed / bathroom map for friendly call positions (config DB, cached)
            now = time.time()
            if cfg["config_db_path"] and (not self._bed_area
                                          or now - self._bed_area_ts > 300):
                try:
                    self._bed_area = read_bed_areas(cfg["config_db_path"])
                    self._bed_area_ts = now
                except Exception:
                    pass
            for i in inv:
                database.imt_upsert_device(
                    SITE_ID, i["ident"], i["location_text"], "unknown", None,
                    None, time.time(),
                    location_text=i["location_text"],
                    location_string=i["location_string"],
                    location_id=i["location_id"], system_ip=i["system_ip"],
                    kind=i["kind"], authoritative=False)
            # 2) device FAULTS come straight from the bridge DB's active-event
            #    tables (authoritative, located — catches bus/peripheral drops)
            self._reconcile_faults(read_db_faults(cfg["db_path"]),
                                   emit=not self._first_fault_poll)
            self._first_fault_poll = False
        # 3) tail the log for live CALLS (calls flow through the log, not the DB)
        if cfg["log_path"]:
            self._tail_log(cfg)

    def _check_service(self, cfg):
        """Track whether the IMT bridge service is running and alert on change.
        Surfaced as a single component 'IMT Bridge service' (ident svc:imt-bridge)
        so a stopped/hung bridge stands out on the board and raises an alert —
        even while the server itself still pings fine."""
        if not settings.get("imt_service_check"):
            return
        stale = int(settings.get("imt_service_stale_secs") or 180)
        svc = (settings.get("imt_service_name") or "").strip()
        h = bridge_health(cfg, stale, svc)
        self.service_health = h
        now = time.time()
        changed, prev = database.imt_upsert_device(
            SITE_ID, "svc:imt-bridge", "IMT Bridge service", h["status"],
            h["reason"], None, now, kind="Service", authoritative=True)
        if changed and not self._first_service_poll:
            self.event_count += 1
            self.last_event_ts = now
            database.imt_add_event(SITE_ID, "svc:imt-bridge", "IMT Bridge service",
                                   h["status"], h["reason"], now)
            self.recent.appendleft({
                "ts": now, "state": "set" if h["status"] == "failed" else "clear",
                "name": "IMT Bridge service", "ident": "svc:imt-bridge",
                "detail": h["reason"] + " (service)", "body": h["reason"]})
            if cfg_alert():
                if h["status"] == "failed":
                    self.webhooks.imt_failed("IMT Bridge service", "svc:imt-bridge",
                                             h["reason"])
                elif prev == "failed":
                    self.webhooks.imt_recovered("IMT Bridge service",
                                                "svc:imt-bridge", h["reason"])
        self._first_service_poll = False

    def _reconcile_faults(self, faults, emit):
        """Supplementary fault source from the DB's active-event tables. The log
        is authoritative (see ``_apply_log_fault``); this catches the rare case
        where a fault is in the DB but hasn't hit the log yet. A room present in
        the DB set is marked FAILED; recovery, however, requires the room to be
        absent from BOTH the DB set AND the log's active-episode set, so a DB poll
        can never clear a fault the log is still holding."""
        now = time.time()
        for ls, f in faults.items():
            name = self._loc_names.get(ls) or f["location_text"]
            detail = f["event_text"] + (f" · {f['count']} active" if f["count"] > 1 else "")
            changed, prev = database.imt_upsert_device(
                SITE_ID, ls, name, "failed", detail, None, now,
                location_text=name, location_string=ls,
                system_ip=f["system_ip"], authoritative=True)
            if changed:
                self.event_count += 1
                self.last_event_ts = now
                database.imt_add_event(SITE_ID, ls, name, "failed", detail, now)
                self.recent.appendleft({"ts": now, "state": "set", "name": name,
                                        "ident": ls, "detail": detail + " (fault)",
                                        "body": "from ActiveEventData"})
                if emit and cfg_alert():
                    self.webhooks.imt_failed(name, ls, detail)
        # recover only what is failed AND absent from the DB set AND has no
        # active log episode — otherwise a DB poll would wipe a live log fault
        for d in database.imt_list_devices(SITE_ID):
            if d["ident"].startswith("svc:"):
                continue                     # service health is driven separately
            if (d["status"] == "failed" and d["ident"] not in faults
                    and not self._ident_guids.get(d["ident"])):
                nm = d["name"] or d["ident"]
                changed, prev = database.imt_upsert_device(
                    SITE_ID, d["ident"], nm, "ok", None, None, now,
                    authoritative=True)
                if changed:
                    self.event_count += 1
                    self.last_event_ts = now
                    database.imt_add_event(SITE_ID, d["ident"], nm, "ok",
                                           "recovered", now)
                    if emit and cfg_alert():
                        self.webhooks.imt_recovered(nm, d["ident"], "recovered")

    def _tail_log(self, cfg):
        path = cfg["log_path"]
        if not os.path.exists(path):
            raise RuntimeError("log file not found: " + path)
        size = os.path.getsize(path)

        seed = False
        if self._log_path != path or self._offset is None:
            seed, start = True, 0            # first sight — seed silently
        elif size < self._offset:
            seed, start = True, 0            # rotated/truncated — reseed silently
        else:
            start = self._offset

        records = []
        emitted_to = start
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(start)
            for line in f:
                if not line.endswith("\n"):
                    break                    # partial trailing line — wait for more
                emitted_to += len(line.encode("utf-8", "replace"))
                if not _LINE_START.match(line):
                    continue
                rec = parse_line(line)
                if rec:
                    records.append(rec)

        self._process_batch(records, emit=not seed)
        try:
            database.imt_calls_prune(SITE_ID)
        except Exception:
            pass
        self._offset = emitted_to
        self._log_path = path
        _save_state({"log_path": path, "offset": self._offset})

    def _process_batch(self, records, emit):
        # index FullEvent locations by UafId (only genuinely located ones)
        full = {}
        for r in records:
            if r["kind"] == "fullevent" and r["loc_id"] not in _NO_LOC:
                full[r["uafid"]] = (r["loc_id"], r["loc_text"])
        for r in records:
            if r["kind"] != "eventdata":
                continue
            if (r["event_text"] or "").strip().lower() in FAULT_TEXTS:
                # A device FAULT (Supervision / Failure). The log is the
                # authoritative source: the Set stays in the append-only file
                # until a real Clear, whereas the bridge DB holds the row only
                # for a few seconds. We drive up/down from the LOCATED ones here
                # (a real LocationString like "3-4-4-35"); unlocated / system
                # supervisions carry no room and are noise.
                ls = (r["loc_string"] or "").strip()
                if "-" in ls:
                    self._apply_log_fault(r, emit)
                continue
            self._apply_call(r, full, emit)

    def _apply_log_fault(self, r, emit):
        """A located device fault (Supervision / Failure) from the log's
        append-only ``State: Set`` / ``State: Clear`` stream. Keyed by the room's
        LocationString so it matches the DB inventory, and ref-counted per episode
        GUID so a room with several devices down stays FAILED until the last one
        clears. This is the reliable fault source — the DB row is transient."""
        ls = r["loc_string"].strip()
        guid = r["guid"] or ("uaf:" + (r["uafid"] or "?"))
        name = self._loc_names.get(ls) or r["loc_text"] or ls
        state = r["state"]
        active = self._ident_guids.setdefault(ls, set())
        if state == "set":
            active.add(guid)
            status = "failed"
        else:
            active.discard(guid)
            status = "failed" if active else "ok"
        detail = (r["event_text"] or "Fault")
        if len(active) > 1:
            detail += f" · {len(active)} active"
        ts = r.get("log_ts") or r["ts"] or time.time()
        changed, prev = database.imt_upsert_device(
            SITE_ID, ls, name, status, detail, r["raw"], ts,
            location_text=name, location_string=ls,
            location_id=(r["loc_id"] or None), authoritative=True)
        if status == "ok" and not active:
            self._ident_guids.pop(ls, None)
        self.recent.appendleft({
            "ts": ts, "state": state, "name": name, "ident": ls,
            "detail": detail + " (fault)", "body": r["raw"]})
        if not changed:
            return
        self.event_count += 1
        self.last_event_ts = ts
        database.imt_add_event(SITE_ID, ls, name, status, detail, ts)
        if not (emit and cfg_alert()):
            return
        if status == "failed":
            self.webhooks.imt_failed(name, ls, detail or "device failed")
        elif prev == "failed":
            self.webhooks.imt_recovered(name, ls, detail or "device recovered")

    def _apply_call(self, r, full, emit):
        """A live nurse-call event — raised (Set) or cleared (Clear). Never
        touches device up/down status and never raises a device-fault alert."""
        state = r["state"]
        guid = r["guid"] or ("uaf:" + (r["uafid"] or "?"))
        ident, name, loc_string, loc_id, loc_text = self._resolve_ident(r, full)
        friendly = self._loc_names.get(loc_string) if loc_string else None
        disp = friendly or name or loc_text or loc_string or ident or "Unknown"
        category = classify_call(r["event_text"], r["priority"])
        detail = " · ".join(b for b in (r["event_text"], r["priority"]) if b)
        ts = r.get("log_ts") or r["ts"] or time.time()

        self.recent.appendleft({
            "ts": ts, "state": state, "name": disp, "ident": ident or loc_string,
            "detail": (r["event_text"] or "") + " (call)", "body": r["raw"],
        })
        self.event_count += 1
        self.last_event_ts = ts

        # human-friendly feed fields, resolved from the config/inventory
        dutyarea, position, location = self._feed_fields(
            loc_string, disp, r["event_text"])

        if state == "set":
            newly = database.imt_call_set(
                SITE_ID, guid, r.get("code") or "", r["event_text"], category,
                r["priority"] or "", loc_string or None, loc_text or None,
                loc_id or None, disp, ts, r["raw"])
            if (emit and newly and category in ("emergency", "wc")
                    and settings.get("imt_emergency_alert")):
                self.webhooks.imt_call(disp, ident or loc_string or guid, detail)
            if emit and self.feed is not None:
                self.feed.emit_call(guid, dutyarea, position, location,
                                    r["event_text"] or "Call")
        else:
            database.imt_call_clear(SITE_ID, guid, ts)
            if emit and self.feed is not None:
                self.feed.emit_reset(guid, dutyarea, position, location)

    def _feed_fields(self, loc_string, disp, event_text):
        """Resolve a call's (dutyarea, position, location) as friendly names.

        * dutyarea — the friendly name of the longest LocationString prefix that
          is a configured duty area (e.g. ``3-4-4`` -> ``Test Rig``);
        * location — the friendly room / point name (``Bedroom 1``);
        * position — ``Bed`` or ``Bathroom``, from the call's trailing bed
          segment (``…-35-41`` -> BedId 41 -> Bed), falling back to the call type
          (WC / bathroom calls -> Bathroom, otherwise Bed)."""
        dutyarea = ""
        segs = loc_string.split("-") if loc_string else []
        for i in range(len(segs), 0, -1):
            pref = "-".join(segs[:i])
            if pref in self._duty_areas:
                dutyarea = self._duty_areas[pref] or pref
                break
        location = ((self._loc_names.get(loc_string) if loc_string else None)
                    or disp or loc_string or "")
        position = ""
        if len(segs) >= 5:
            bed = self._bed_area.get(segs[-1])
            if bed:
                position = bed["area"]
        if not position:
            position = "Bathroom" if classify_call(event_text, "") == "wc" else "Bed"
        return dutyarea, position, location

    def _resolve_ident(self, r, full):
        """Work out (ident, name, meta) for a fault EventData record."""
        loc_string = r["loc_string"]
        loc_id = r["loc_id"] if r["loc_id"] not in _NO_LOC else ""
        loc_text = r["loc_text"]
        if not loc_string and not loc_id:
            hit = full.get(r["uafid"])          # borrow from the paired FullEvent
            if hit:
                loc_id, ft = hit
                loc_text = loc_text or ft
        if not loc_string and loc_id:
            loc_string = self._locid2str.get(loc_id, "")   # LocationId -> path
        if loc_string:
            ident = loc_string
        elif loc_id:
            ident = "loc:" + loc_id
        else:
            ident = ""                          # unlocated / system-level
        name = loc_text or (loc_string or None)
        return ident, name, loc_string, loc_id, loc_text

    def _apply_fault(self, r, full, emit, uaf=None):
        state = r["state"]
        guid = r["guid"] or ("uaf:" + (r["uafid"] or "?"))
        ident, name, loc_string, loc_id, loc_text = self._resolve_ident(r, full)

        # A device supervision/failure usually carries NO location on its own
        # line — the only identifier is the device IP on the preceding uaf line.
        if not ident and uaf and uaf.get("ip"):
            ip = uaf["ip"]
            ident = "ip:" + ip
            name = self._ip_names.get(ip) or ("Controller " + ip)
            loc_text = loc_text or name
            da = self._duty_by_seg.get(uaf.get("duty_id"))
            if da and not loc_string:
                loc_string = da          # so it buckets under the right duty area

        if not ident:
            # still nothing: pair Set/Clear on the episode GUID
            if state == "clear" and guid in self._guid_ident:
                ident, name = self._guid_ident[guid]
            else:
                ident = "evt:" + guid
                name = name or (r["event_text"] + (" " + guid[:8] if r["guid"] else ""))

        if state == "set":
            self._guid_ident[guid] = (ident, name)
            if len(self._guid_ident) > 8000:
                self._guid_ident.pop(next(iter(self._guid_ident)))
        else:
            rid = self._guid_ident.pop(guid, None)
            if rid:
                ident, name = rid

        # Refcount concurrent episodes per device: a controller with several
        # peripherals down stays FAILED until the last one clears.
        active = self._ident_guids.setdefault(ident, set())
        if state == "set":
            active.add(guid)
            status = "failed"
        else:
            active.discard(guid)
            status = "ok" if not active else "failed"

        detail = " · ".join(b for b in (r["event_text"], r["type"], r["priority"]) if b)
        if len(active) > 1:
            detail += f" · {len(active)} active"
        ts = r["ts"] or time.time()

        self.recent.appendleft({
            "ts": ts, "state": state, "name": name or ident,
            "ident": ident, "detail": detail, "body": r["raw"],
        })
        changed, prev = database.imt_upsert_device(
            SITE_ID, ident, name, status, detail, r["raw"], ts,
            location_text=loc_text or None, location_string=loc_string or None,
            location_id=loc_id or None, authoritative=True)
        if state == "clear" and not active:
            self._ident_guids.pop(ident, None)
        if not changed:
            return
        self.event_count += 1
        self.last_event_ts = ts
        database.imt_add_event(SITE_ID, ident, name or ident, status, detail, ts)
        if not (emit and cfg_alert()):
            return
        nm = name or ident
        if status == "failed":
            self.webhooks.imt_failed(nm, ident, detail or "device failed")
        elif prev == "failed":
            self.webhooks.imt_recovered(nm, ident, detail or "device recovered")

    # ---- setup check for the UI ----

    def test_connection(self):
        """Verify the configured files are readable and report what's in them."""
        cfg = load_cfg()
        if not cfg["db_path"] and not cfg["log_path"]:
            raise RuntimeError("set the bridge database and/or log path first")
        out = {"ok": True}
        if cfg["db_path"]:
            if not os.path.exists(cfg["db_path"]):
                raise RuntimeError("database not found: " + cfg["db_path"])
            out["db"] = cfg["db_path"]
            out["locations"] = len(read_inventory(cfg["db_path"]))
        if cfg["log_path"]:
            if not os.path.exists(cfg["log_path"]):
                raise RuntimeError("log not found: " + cfg["log_path"])
            out["log"] = cfg["log_path"]
            out["log_bytes"] = os.path.getsize(cfg["log_path"])
        return out


def cfg_alert():
    return bool(settings.get("imt_alert"))
