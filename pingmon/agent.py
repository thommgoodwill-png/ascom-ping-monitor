"""Agent mode: when the app is configured with a hub URL + site API key it
becomes a remote probe. It pulls its device list down from the hub, lets the
normal local monitor ping them (so the full local dashboard still works), and
pushes new ping samples + events back up to the hub's site page.

Configuration lives in <data dir>/agent.json and is edited from Settings.
"""
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.request

from . import database, settings

log = logging.getLogger("pingmon.agent")

AGENT_VERSION = "1.6"   # 1.6 = device deletions travel both ways (1.5: hub -> agent)
# When (re)connecting with a big unsent backlog, only push pings from the last
# this-many seconds; older queued pings are skipped rather than replayed.
BACKFILL_WINDOW = 900   # 15 minutes
CONF_PATH = os.path.join(database.DATA_DIR, "agent.json")
STATE_PATH = os.path.join(database.DATA_DIR, "agent_state.json")


def load_conf():
    try:
        with open(CONF_PATH) as f:
            c = json.load(f)
    except (OSError, ValueError):
        c = {}
    return {
        "enabled": bool(c.get("enabled", False)),
        "hub_url": (c.get("hub_url") or "").rstrip("/"),
        "site_key": c.get("site_key") or "",
        "interval": int(c.get("interval", 30)),   # seconds between pushes
        # allow a self-signed / untrusted controller TLS cert (opt-in, insecure)
        "insecure": bool(c.get("insecure", False)),
    }


def save_conf(hub_url=None, site_key=None, enabled=None, interval=None,
              insecure=None):
    c = load_conf()
    if hub_url is not None:
        c["hub_url"] = hub_url.rstrip("/")
    if site_key is not None:
        c["site_key"] = site_key
    if enabled is not None:
        c["enabled"] = bool(enabled)
    if interval is not None:
        c["interval"] = max(10, min(600, int(interval)))
    if insecure is not None:
        c["insecure"] = bool(insecure)
    os.makedirs(database.DATA_DIR, exist_ok=True)
    with open(CONF_PATH, "w") as f:
        json.dump(c, f)
    try:
        os.chmod(CONF_PATH, 0o600)   # contains the site secret
    except OSError:
        pass
    return c


def _load_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"ping_wm": 0, "event_wm": 0}


def _save_state(st):
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(st, f)
    except OSError:
        pass


try:
    import certifi
    _CA_FILE = certifi.where()
except Exception:                       # certifi not bundled — fall back to OS store
    _CA_FILE = None


def _ssl_ctx(url, insecure):
    """SSL context for an agent request.

    * plain HTTP  -> None (no TLS).
    * HTTPS, insecure opted in -> verification OFF (accept self-signed).
    * HTTPS, normal -> a VERIFYING context that trusts BOTH certifi's bundled CA
      roots AND the OS trust store. The certifi bundle is what makes a valid
      public cert (e.g. a controller behind Cloudflare) verify even on a Windows
      box whose root store is missing/outdated — the cause of the
      'unable to get local issuer certificate' error."""
    if not str(url).lower().startswith("https"):
        return None
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    ctx = ssl.create_default_context(cafile=_CA_FILE) if _CA_FILE \
        else ssl.create_default_context()
    try:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)   # also trust OS roots
    except Exception:
        pass
    return ctx


def _post(url, key, payload, timeout=20, insecure=False):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "User-Agent": "AscomNetworkMonitor-Agent/" + AGENT_VERSION})
    with urllib.request.urlopen(req, timeout=timeout,
                               context=_ssl_ctx(url, insecure)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url, key, timeout=20, insecure=False):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "User-Agent": "AscomNetworkMonitor-Agent/" + AGENT_VERSION})
    with urllib.request.urlopen(req, timeout=timeout,
                               context=_ssl_ctx(url, insecure)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_connection(hub_url, site_key, insecure=False):
    """Synchronous config fetch so the GUI can confirm the hub + key work."""
    hub_url = hub_url.rstrip("/")
    cfg = _get(f"{hub_url}/agent/v1/config?v={AGENT_VERSION}", site_key,
               timeout=15, insecure=insecure)
    return {"site": cfg.get("site"), "devices": len(cfg.get("devices", []))}


class Agent:
    def __init__(self, monitor):
        self.monitor = monitor
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="agent")
        self.last_error = None
        self.last_push = None
        self.last_pushed_count = 0

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()

    def status(self):
        c = load_conf()
        return {"enabled": c["enabled"], "hub_url": c["hub_url"],
                "configured": bool(c["hub_url"] and c["site_key"]),
                "last_error": self.last_error, "last_push": self.last_push,
                "last_pushed_count": self.last_pushed_count}

    def _loop(self):
        while not self._stop.is_set():
            c = load_conf()
            if not (c["enabled"] and c["hub_url"] and c["site_key"]):
                self._stop.wait(5)
                continue
            try:
                self._register_local(c)   # push agent-added devices UP to the hub
                self._sync_config(c)      # pull any hub-defined devices DOWN
                self._push(c)             # push ping samples + events
                self.last_error = None
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.warning("agent cycle failed: %s", e)
            try:
                self._push_imt(c)         # push IMT bridge faults UP to the hub
            except Exception as e:
                log.warning("agent IMT push failed: %s", e)
            self._stop.wait(max(10, c["interval"]))

    def _hostname(self):
        try:
            return socket.gethostname()
        except OSError:
            return ""

    def _register_local(self, c):
        """Push devices added locally on this agent UP to the hub, so they show
        up in the controller under this site.

        Every agent-owned device (from_hub falsy) is re-asserted each cycle.
        The hub matches by host and returns the current hub id, so this is
        idempotent AND self-healing: if the site was recreated or a device's
        old link went stale, the hub id is simply refreshed to the live one.

        The hub also replies with 'removed' — local ids it has deleted its side
        and refuses to recreate. Those are deleted here too, otherwise deleting
        a device on the controller would be a tug of war we re-assert our way
        every cycle, and the agent would keep pinging a device nobody wants.

        created_at goes up with each device so the hub can tell a device added
        here after a deletion from the deleted one coming back — same address,
        but a deliberate re-add, which it should accept rather than refuse.

        Deletions travel UP as well as down. When a device is deleted here the
        controller still has its copy and would push it straight back, so the
        deletion is recorded locally (database.delete_device) and every one of
        those tombstones is sent with this call for the controller to apply.
        Only an explicit tombstone is ever sent — a device that is merely absent
        says nothing — so a rebuilt or restored agent cannot wipe the
        controller's device list simply by starting up with an empty database.

        An empty list is still sent: it is how the hub learns we have acted on
        a removal, and it keeps the site's last-seen time fresh on an agent that
        happens to own no devices of its own."""
        owned = [d for d in database.list_devices() if not d.get("from_hub")]
        gone = database.deleted_hub_hosts()
        payload = {
            "version": AGENT_VERSION, "host": self._hostname(),
            "devices": [{"local_id": d["id"], "name": d["name"],
                         "host": d["host"], "enabled": d["enabled"],
                         "created_at": d.get("created_at")}
                        for d in owned if d.get("host")],
            "deleted": [{"host": h, "at": ts} for h, ts in gone.items()],
        }
        resp = _post(f"{c['hub_url']}/agent/v1/devices", c["site_key"], payload,
                     insecure=c.get("insecure", False))
        id_map = resp.get("id_map") or {}
        tagged = 0
        for local_id_str, hub_id in id_map.items():
            try:
                lid, hid = int(local_id_str), int(hub_id)
            except (TypeError, ValueError):
                continue
            # only write when it actually changed, to avoid needless churn
            cur = database.get_device(lid)
            if cur and cur.get("hub_id") != hid:
                database.update_device(lid, hub_id=hid)
            tagged += 1
        # devices the operator deleted on the controller: honour it locally
        dropped = 0
        ours = {d["id"] for d in owned}
        for lid in (resp.get("removed") or []):
            try:
                lid = int(lid)
            except (TypeError, ValueError):
                continue
            # only ever delete a device we ourselves just offered, so a bad or
            # stale reply can never reach into the rest of the local config
            if lid in ours and database.get_device(lid):
                database.delete_device(lid)
                dropped += 1
        # deletions made here that the controller has now applied: drop the
        # tombstone, so the same host can be deployed again later
        done = [h for h in (resp.get("deleted_ok") or []) if isinstance(h, str)]
        if done:
            database.forget_deleted_hub_hosts([h.strip().lower() for h in done])
        log.info("agent asserted %d local device(s) to the hub%s%s", tagged,
                 ", deleted %d removed on the controller" % dropped if dropped else "",
                 ", controller applied %d local deletion(s)" % len(done) if done else "")

    def _sync_config(self, c):
        """Pull any hub-defined devices for this site and mirror them into the
        local DB so the normal monitor pings them too.

        Devices added on this agent (and pushed up via _register_local) are
        never touched here — only pure hub-side mirrors (from_hub=1). A mirror
        whose hub device has been deleted is deleted locally as well: it exists
        solely to reflect the hub, so keeping it would leave the agent pinging a
        device that no longer appears anywhere in the controller.

        A device deleted here is skipped until the controller confirms it has
        deleted its own copy. Without that, this method is exactly what made
        deleting a device on an agent appear to do nothing: the row went, and
        the very next config pull — seconds later — put it straight back."""
        url = f"{c['hub_url']}/agent/v1/config?v={AGENT_VERSION}&host={self._hostname()}"
        cfg = _get(url, c["site_key"], insecure=c.get("insecure", False))
        pending = database.deleted_hub_hosts()
        wanted = {int(d["id"]): d for d in cfg.get("devices", [])
                  if (d.get("host") or "").strip().lower() not in pending}
        # index local devices already linked to a hub device
        local = {d["hub_id"]: d for d in database.list_devices()
                 if d.get("hub_id")}
        for hub_id, d in wanted.items():
            iv = d.get("interval")
            fields = dict(name=d["name"], host=d["host"],
                          enabled=1 if d.get("enabled", 1) else 0,
                          interval_override=iv,
                          warn_override=d.get("warn_ms"),
                          crit_override=d.get("crit_ms"),
                          tcp_ports=d.get("tcp_ports") or None,
                          check_url=d.get("check_url") or None)
            if hub_id in local:
                database.update_device(local[hub_id]["id"], **fields)
            else:
                new_id = database.add_device(d["name"], d["host"], fields["enabled"], iv)
                database.update_device(new_id, hub_id=hub_id, from_hub=1, **{
                    k: v for k, v in fields.items()
                    if k in ("warn_override", "crit_override", "tcp_ports", "check_url")})
        # retire mirrors of hub devices that no longer exist
        for hub_id, d in local.items():
            if hub_id not in wanted and d.get("from_hub"):
                database.delete_device(d["id"])
                log.info("agent removed mirror of deleted hub device %s (%s)",
                         hub_id, d.get("host"))

    def _push_imt(self, c):
        """Push the IMT bridge faults this agent read locally up to the hub, so
        they appear under this site on the controller. The agent's own IMT
        reader (running against the site's local ImtBridgeDb.db3 + log) fills the
        local imt_devices/imt_events tables at site_id=None; we mirror that
        snapshot up. Sends only when there's something to send."""
        devices = database.imt_list_devices(None)
        st = _load_state()
        wm = st.get("imt_event_wm", 0)
        events = database.imt_events_after(None, wm, limit=1000)
        has_calls = database.imt_call_counts(None)["active"] > 0 or \
            bool(database.imt_list_recent_calls(None, limit=1))
        if not devices and not events and not has_calls:
            return
        dev_out = [{"ident": d["ident"], "name": d["name"], "status": d["status"],
                    "detail": d["detail"], "location_text": d["location_text"],
                    "location_string": d["location_string"],
                    "location_id": d["location_id"], "system_ip": d["system_ip"],
                    "kind": d["kind"], "last_change": d["last_change"]}
                   for d in devices]
        ev_out = [{"ident": e["ident"], "name": e["name"], "status": e["status"],
                   "detail": e["detail"], "ts": e["ts"]} for e in events]
        # live calls: send the full active set + a slice of recent history
        active = database.imt_list_active_calls(None)
        recent = database.imt_list_recent_calls(None, limit=50)
        history = [c for c in recent if c["state"] == "cleared"]
        call_cols = ("guid", "code", "event_text", "category", "priority",
                     "location_string", "location_text", "location_id", "name",
                     "raised_ts", "cleared_ts", "raw")
        ca_out = [{k: c[k] for k in call_cols} for c in active]
        ch_out = [{k: c[k] for k in call_cols} for c in history]
        _post(f"{c['hub_url']}/agent/v1/imt", c["site_key"],
              {"version": AGENT_VERSION, "host": self._hostname(),
               "devices": dev_out, "events": ev_out,
               "calls_active": ca_out, "calls_history": ch_out},
              insecure=c.get("insecure", False))
        if events:
            st = _load_state()
            st["imt_event_wm"] = max(e["id"] for e in events)
            _save_state(st)

    def _build_diag(self, st):
        """A small self-report the hub can display, so a remote agent can be
        diagnosed without opening its dashboard. Never raises."""
        try:
            devs = database.list_devices()
            reg = [d for d in devs if d.get("hub_id")]
            sample = []
            for d in reg[:12]:
                lp = database.last_ping(d["id"]) or {}
                sample.append({
                    "host": d["host"], "hub_id": d.get("hub_id"),
                    "up": bool(lp.get("success")) if lp else None,
                    "last_latency": lp.get("latency") if lp else None,
                    "age": round(time.time() - lp["ts"], 1) if lp.get("ts") else None,
                })
            return {
                "local_devices": len(devs),
                "registered": len(reg),
                "max_ping_id": database.max_ping_id(),
                "ping_wm": st.get("ping_wm", 0),
                "monitoring": bool(settings.get("monitoring_enabled")),
                "last_error": self.last_error,
                "sample": sample,
                "imt": self._imt_diag(),
            }
        except Exception as e:
            return {"diag_error": f"{type(e).__name__}: {e}"}

    def _imt_diag(self):
        """The agent's local Telligence reader state, so the controller can see
        why (say) faults arrive but calls don't. Calls only flow through the
        bridge LOG, so a reader with a DB path but no log path forwards faults
        yet never calls. Never raises."""
        try:
            from . import imtbridge as ib
            cfg = ib.load_cfg()
            calls = database.imt_call_counts(None)
            faults = sum(1 for d in database.imt_list_devices(None)
                         if d["status"] == "failed")
            return {
                "enabled": cfg["enabled"],
                "db_path_set": bool(cfg["db_path"]),
                "log_path_set": bool(cfg["log_path"]),
                "log_exists": bool(cfg["log_path"]) and os.path.exists(cfg["log_path"]),
                "active_calls": calls.get("active", 0),
                "faults": faults,
            }
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def _push(self, c):
        st = _load_state()
        # map local device id -> hub_id for agent-managed devices
        id_map = {d["id"]: d["hub_id"] for d in database.list_devices()
                  if d.get("hub_id")}
        diag = self._build_diag(st)
        if not id_map:
            # nothing registered yet — heartbeat so the hub still sees us + diag
            _post(f"{c['hub_url']}/agent/v1/heartbeat", c["site_key"],
                  {"version": AGENT_VERSION, "host": self._hostname(), "diag": diag},
                  insecure=c.get("insecure", False))
            return

        # Self-heal a stale watermark: if it's ahead of the highest local ping
        # id, the local ping table was reset behind us — rewind so we resume
        # pushing instead of silently sending nothing forever.
        wm = st.get("ping_wm", 0)
        max_id = database.max_ping_id()
        if wm > max_id:
            log.warning("agent: ping watermark %s ahead of max id %s — resetting",
                        wm, max_id)
            wm = 0

        # Skip a large stale backlog. If this agent monitored locally for a
        # long time before being connected to a hub (or was offline a while),
        # it can hold days of unsent pings. Replaying them oldest-first floods
        # the hub with ancient data and delays current readings by hours — so
        # jump the watermark forward and push only the last BACKFILL_WINDOW.
        recent_id = database.first_ping_id_since(time.time() - BACKFILL_WINDOW)
        if recent_id is not None and recent_id - 1 > wm:
            skipped = recent_id - 1 - wm
            log.warning("agent: skipping %d stale queued pings; sending only the "
                        "last %d min", skipped, BACKFILL_WINDOW // 60)
            wm = recent_id - 1

        pings = database.pings_after(wm, limit=5000)
        by_hub = {}
        max_pid = wm
        for p in pings:
            max_pid = max(max_pid, p["id"])
            hub_id = id_map.get(p["device_id"])
            if hub_id is None:
                continue
            by_hub.setdefault(str(hub_id), []).append(
                [round(p["ts"], 2), p["latency"], bool(p["success"]), p["jitter"]])

        events = database.events_after(st.get("event_wm", 0), limit=500)
        ev_out = []
        max_eid = st.get("event_wm", 0)
        for e in events:
            max_eid = max(max_eid, e["id"])
            hub_id = id_map.get(e["device_id"])
            if hub_id is None:
                continue
            ev_out.append({"hub_id": hub_id, "ts": e["ts"],
                           "type": e["type"], "detail": e["detail"]})

        macs = {str(d["hub_id"]): d["mac"] for d in database.list_devices()
                if d.get("hub_id") and d.get("mac")}

        if not by_hub and not ev_out:
            # nothing new — still heartbeat (with diag) so the hub shows us online
            _post(f"{c['hub_url']}/agent/v1/heartbeat", c["site_key"],
                  {"version": AGENT_VERSION, "host": self._hostname(), "diag": diag},
                  insecure=c.get("insecure", False))
            return

        payload = {"version": AGENT_VERSION, "host": self._hostname(),
                   "now": time.time(),
                   "pings": by_hub, "events": ev_out, "macs": macs, "diag": diag}
        resp = _post(f"{c['hub_url']}/agent/v1/report", c["site_key"], payload,
                     insecure=c.get("insecure", False))
        st["ping_wm"] = max_pid
        st["event_wm"] = max_eid
        _save_state(st)
        self.last_push = time.time()
        self.last_pushed_count = resp.get("accepted_pings", 0)
        log.info("agent pushed %d pings, %d events to hub",
                 self.last_pushed_count, len(ev_out))
