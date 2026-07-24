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

    accepted = 0
    for did_str, samples in (data.get("pings") or {}).items():
        try:
            did = int(did_str)
        except (TypeError, ValueError):
            continue
        if did not in own or not isinstance(samples, list):
            continue
        clean = [s for s in samples if isinstance(s, list) and len(s) >= 3][:5000]
        database.record_pushed_pings(did, clean)
        accepted += len(clean)

    for ev in (data.get("events") or [])[:500]:
        try:
            did = int(ev.get("hub_id"))
        except (TypeError, ValueError):
            continue
        if did not in own:
            continue
        database.record_event(did, float(ev.get("ts", 0)),
                              str(ev.get("type", "info"))[:32],
                              str(ev.get("detail", ""))[:500])

    for did_str, mac in (data.get("macs") or {}).items():
        try:
            did = int(did_str)
        except (TypeError, ValueError):
            continue
        if did in own and mac:
            import time
            database.set_device_mac(did, str(mac)[:32], time.time())

    database.touch_site(site["id"],
                        agent_version=data.get("version"),
                        agent_host=data.get("host"))
    return jsonify(ok=True, accepted_pings=accepted)


@bp.route("/heartbeat", methods=["POST"])
@site_auth
def heartbeat(site):
    data = request.get_json(force=True, silent=True) or {}
    database.touch_site(site["id"], agent_version=data.get("version"),
                        agent_host=data.get("host"))
    return jsonify(ok=True)
