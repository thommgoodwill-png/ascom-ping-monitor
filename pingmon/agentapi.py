"""Hub-side API that customer agents talk to. Authenticated by a per-site
bearer token (the site's API key), separate from the browser session login.

Endpoints (all under /agent/v1):
  GET  /config     -> the device list + thresholds the agent should monitor
  POST /report     -> agent pushes ping samples + events
  POST /heartbeat  -> agent liveness / version

Designed to work over HTTP (private/VPN) or HTTPS (public). Use HTTPS whenever
the hub is internet-reachable — the API key is a bearer secret.
"""
import functools
import logging
import time

from flask import Blueprint, jsonify, request

from . import database, netcheck, settings

log = logging.getLogger("pingmon.agentapi")

bp = Blueprint("agent", __name__, url_prefix="/agent/v1")


def _site_from_request():
    auth = request.headers.get("Authorization", "")
    key = ""
    if auth.startswith("Bearer "):
        key = auth[7:].strip()
    if not key:
        key = request.headers.get("X-Api-Key", "").strip()
    if not key:
        return None
    return database.get_site_by_key(key)


def site_auth(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        site = _site_from_request()
        if not site:
            return jsonify(error="invalid or missing site API key"), 401
        return f(site, *args, **kwargs)
    return wrapper


@bp.route("/config")
@site_auth
def config(site):
    """Devices this site's agent should monitor, plus global thresholds."""
    g_warn, g_crit = settings.get("warn_ms"), settings.get("crit_ms")
    devices = []
    for d in database.list_devices(site_id=site["id"]):
        devices.append({
            "id": d["id"],
            "name": d["name"],
            "host": d["host"],
            "enabled": d["enabled"],
            "interval": d.get("interval_override") or settings.get("ping_interval"),
            "warn_ms": d.get("warn_override") or g_warn,
            "crit_ms": d.get("crit_override") or g_crit,
            "tcp_ports": d.get("tcp_ports") or "",
            "check_url": d.get("check_url") or "",
        })
    database.touch_site(site["id"],
                        agent_version=request.args.get("v"),
                        agent_host=request.args.get("host"))
    return jsonify(
        site={"id": site["id"], "name": site["name"]},
        ping_timeout=settings.get("ping_timeout"),
        fail_threshold=settings.get("fail_threshold"),
        warn_ms=g_warn, crit_ms=g_crit,
        jitter_warn=settings.get("jitter_warn_ms"),
        devices=devices)


@bp.route("/devices", methods=["POST"])
@site_auth
def register_devices(site):
    """Register the devices an agent monitors locally so they appear in the
    controller under this site.

    Body: { "version": "..", "host": "..",
            "devices": [ {"local_id": 3, "name": "Gateway", "host": "192.168.0.1",
                          "enabled": true}, ... ] }

    Devices are matched to existing site devices by host (case-insensitive) so
    repeated registration is idempotent — it never creates duplicates. Returns
    { "id_map": { "<local_id>": <hub_device_id> } } so the agent can tag each
    local device and then push its ping data under the right hub id.
    """
    data = request.get_json(force=True, silent=True) or {}
    incoming = data.get("devices") or []
    # existing site devices indexed by normalised host
    existing = {}
    for d in database.list_devices(site_id=site["id"]):
        existing[(d["host"] or "").strip().lower()] = d

    id_map = {}
    for dev in incoming[:1000]:
        try:
            local_id = int(dev.get("local_id"))
        except (TypeError, ValueError):
            continue
        host = (dev.get("host") or "").strip()
        if not host:
            continue
        name = (str(dev.get("name") or host)).strip()[:120]
        enabled = 1 if dev.get("enabled", 1) else 0
        row = existing.get(host.lower())
        if row:
            hub_id = row["id"]
            database.update_device(hub_id, name=name, enabled=enabled)
        else:
            hub_id = database.add_device(name, host, enabled, site_id=site["id"])
            existing[host.lower()] = database.get_device(hub_id)
        id_map[str(local_id)] = hub_id

    database.touch_site(site["id"], agent_version=data.get("version"),
                        agent_host=data.get("host"))
    log.info("site %s: registered/updated %d devices from agent",
             site["id"], len(id_map))
    return jsonify(ok=True, id_map=id_map)


@bp.route("/report", methods=["POST"])
@site_auth
def report(site):
    """Ingest a batch of ping samples + events pushed by the agent.

    Body: {
      "version": "1.2", "host": "DESKTOP-01",
      "pings": { "<hub_device_id>": [[ts, latency_or_null, success_bool, jitter], ...] },
      "events": [ {"hub_id": <id>, "ts": .., "type": "down", "detail": ".."}, ... ],
      "macs":   { "<hub_device_id>": "aa:bb:cc:.." }
    }
    Only devices belonging to THIS site are accepted (tenant isolation).
    """
    data = request.get_json(force=True, silent=True) or {}
    # set of device ids that legitimately belong to this site
    own = {d["id"]: d for d in database.list_devices(site_id=site["id"])}

    # --- clock-skew correction -------------------------------------------
    # If a remote agent's system clock is badly wrong, every ping it stamps
    # lands hours/days off and the graphs look empty even though the devices
    # are up. Detect a gross offset and shift this batch onto the hub's clock.
    # Prefer the agent's reported "now"; otherwise infer it from the newest
    # sample in the batch. Only correct large offsets so normal small clock
    # differences (and short buffering) are left untouched.
    server_now = time.time()
    offset = 0.0
    agent_now = data.get("now")
    try:
        if agent_now:
            offset = server_now - float(agent_now)
    except (TypeError, ValueError):
        offset = 0.0
    if not offset:
        newest = 0.0
        for samples in (data.get("pings") or {}).values():
            if isinstance(samples, list):
                for s in samples:
                    if isinstance(s, list) and s and isinstance(s[0], (int, float)):
                        newest = max(newest, s[0])
        if newest:
            offset = server_now - newest
    if abs(offset) < 3600:          # < 1h: normal, don't touch timestamps
        offset = 0.0

    accepted = 0
    for did_str, samples in (data.get("pings") or {}).items():
        try:
            did = int(did_str)
        except (TypeError, ValueError):
            continue
        if did not in own or not isinstance(samples, list):
            continue
        clean = [s for s in samples if isinstance(s, list) and len(s) >= 3][:5000]
        if offset:
            clean = [[s[0] + offset] + list(s[1:]) for s in clean]
        database.record_pushed_pings(did, clean)
        accepted += len(clean)

    for ev in (data.get("events") or [])[:500]:
        try:
            did = int(ev.get("hub_id"))
        except (TypeError, ValueError):
            continue
        if did not in own:
            continue
        database.record_event(did, float(ev.get("ts", 0)) + offset,
                              str(ev.get("type", "info"))[:32],
                              str(ev.get("detail", ""))[:500])

    for did_str, mac in (data.get("macs") or {}).items():
        try:
            did = int(did_str)
        except (TypeError, ValueError):
            continue
        if did in own and mac:
            database.set_device_mac(did, str(mac)[:32], time.time())

    import json as _json
    database.touch_site(site["id"],
                        agent_version=data.get("version"),
                        agent_host=data.get("host"),
                        agent_diag=_json.dumps(data.get("diag")) if data.get("diag") else None)
    return jsonify(ok=True, accepted_pings=accepted)


@bp.route("/heartbeat", methods=["POST"])
@site_auth
def heartbeat(site):
    import json as _json
    data = request.get_json(force=True, silent=True) or {}
    database.touch_site(site["id"], agent_version=data.get("version"),
                        agent_host=data.get("host"),
                        agent_diag=_json.dumps(data.get("diag")) if data.get("diag") else None)
    return jsonify(ok=True)
