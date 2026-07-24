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
import threading
import time
import urllib.request

from . import database, settings

log = logging.getLogger("pingmon.agent")

AGENT_VERSION = "1.2"   # 1.2 = watermark self-heal + self-report diagnostics
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
    }


def save_conf(hub_url=None, site_key=None, enabled=None, interval=None):
    c = load_conf()
    if hub_url is not None:
        c["hub_url"] = hub_url.rstrip("/")
    if site_key is not None:
        c["site_key"] = site_key
    if enabled is not None:
        c["enabled"] = bool(enabled)
    if interval is not None:
        c["interval"] = max(10, min(600, int(interval)))
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


def _post(url, key, payload, timeout=20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "User-Agent": "AscomNetworkMonitor-Agent/" + AGENT_VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get(url, key, timeout=20):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "User-Agent": "AscomNetworkMonitor-Agent/" + AGENT_VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_connection(hub_url, site_key):
    """Synchronous config fetch so the GUI can confirm the hub + key work."""
    hub_url = hub_url.rstrip("/")
    cfg = _get(f"{hub_url}/agent/v1/config?v={AGENT_VERSION}", site_key, timeout=15)
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
        old link went stale, the hub id is simply refreshed to the live one."""
        owned = [d for d in database.list_devices() if not d.get("from_hub")]
        if not owned:
            return
        payload = {
            "version": AGENT_VERSION, "host": self._hostname(),
            "devices": [{"local_id": d["id"], "name": d["name"],
                         "host": d["host"], "enabled": d["enabled"]}
                        for d in owned if d.get("host")],
        }
        if not payload["devices"]:
            return
        resp = _post(f"{c['hub_url']}/agent/v1/devices", c["site_key"], payload)
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
        log.info("agent asserted %d local device(s) to the hub", tagged)

    def _sync_config(self, c):
        """Pull any hub-defined devices for this site and mirror them into the
        local DB so the normal monitor pings them too. Non-destructive: devices
        added on this agent (and pushed up via _register_local) are never
        removed here — only pure hub-side mirrors are updated."""
        url = f"{c['hub_url']}/agent/v1/config?v={AGENT_VERSION}&host={self._hostname()}"
        cfg = _get(url, c["site_key"])
        wanted = {int(d["id"]): d for d in cfg.get("devices", [])}
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
            }
        except Exception as e:
            return {"diag_error": f"{type(e).__name__}: {e}"}

    def _push(self, c):
        st = _load_state()
        # map local device id -> hub_id for agent-managed devices
        id_map = {d["id"]: d["hub_id"] for d in database.list_devices()
                  if d.get("hub_id")}
        diag = self._build_diag(st)
        if not id_map:
            # nothing registered yet — heartbeat so the hub still sees us + diag
            _post(f"{c['hub_url']}/agent/v1/heartbeat", c["site_key"],
                  {"version": AGENT_VERSION, "host": self._hostname(), "diag": diag})
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
                  {"version": AGENT_VERSION, "host": self._hostname(), "diag": diag})
            return

        payload = {"version": AGENT_VERSION, "host": self._hostname(),
                   "now": time.time(),
                   "pings": by_hub, "events": ev_out, "macs": macs, "diag": diag}
        resp = _post(f"{c['hub_url']}/agent/v1/report", c["site_key"], payload)
        st["ping_wm"] = max_pid
        st["event_wm"] = max_eid
        _save_state(st)
        self.last_push = time.time()
        self.last_pushed_count = resp.get("accepted_pings", 0)
        log.info("agent pushed %d pings, %d events to hub",
                 self.last_pushed_count, len(ev_out))
