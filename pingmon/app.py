"""Flask application: authenticated GUI + JSON API."""
import functools
import logging
import os
import secrets
import sys
import time

from flask import (Flask, jsonify, redirect, render_template, request,
                   send_from_directory, session, url_for)

from . import database, settings
from .emailer import Emailer, REPORT_KINDS
from .monitor import Monitor

log = logging.getLogger("pingmon.app")

emailer = Emailer()
monitor = Monitor(emailer)


def _secret_key():
    path = os.path.join(database.DATA_DIR, "secret_key")
    try:
        with open(path) as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    os.makedirs(database.DATA_DIR, exist_ok=True)
    key = secrets.token_hex(32)
    with open(path, "w") as f:
        f.write(key)
    os.chmod(path, 0o600)
    return key


_LOGO_NAMES = ("Logo.png", "logo.png", "Logo.svg", "logo.svg",
               "Logo.jpg", "logo.jpg")


def _find_logo(folder):
    for name in _LOGO_NAMES:
        if os.path.exists(os.path.join(folder, name)):
            return name
    return None


def _base_dir():
    """Project root — or the PyInstaller extraction dir when frozen as an exe."""
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app():
    base = _base_dir()
    app = Flask(__name__,
                template_folder=os.path.join(base, "templates"),
                static_folder=os.path.join(base, "static"))
    database.init_db()
    app.secret_key = _secret_key()
    app.config["PERMANENT_SESSION_LIFETIME"] = 86400 * 7

    # ---- branding ----
    # The OFFICIAL logo lives in the DATA directory and beats everything:
    # drop your file (e.g. Logo.png) into <data dir>/branding/ and it survives
    # every update, rebuild and reinstall. Falls back to the bundled logo in
    # static/branding otherwise.
    user_logo_dir = os.path.join(database.DATA_DIR, "branding")
    try:
        os.makedirs(user_logo_dir, exist_ok=True)
    except OSError:
        pass
    static_logo = _find_logo(os.path.join(app.static_folder, "branding")) or "logo.svg"

    @app.route("/userlogo")
    def userlogo():
        name = _find_logo(user_logo_dir)   # checked per-request: no restart needed
        if not name:
            return redirect(url_for("static", filename="branding/" + static_logo))
        return send_from_directory(user_logo_dir, name)

    _FAV_NAMES = ("favicon.svg", "favicon.ico", "favicon.png",
                  "Favicon.svg", "Favicon.ico", "Favicon.png")

    def _find_favicon():
        for name in _FAV_NAMES:
            if os.path.exists(os.path.join(user_logo_dir, name)):
                return name
        return None

    @app.route("/userfavicon")
    def userfavicon():
        name = _find_favicon()
        if not name:
            return redirect(url_for("static", filename="branding/favicon.svg"))
        return send_from_directory(user_logo_dir, name)

    @app.context_processor
    def inject_branding():
        ctx = {}
        ctx["logo_url"] = (url_for("userlogo") if _find_logo(user_logo_dir)
                           else url_for("static", filename="branding/" + static_logo))
        ctx["favicon_url"] = (url_for("userfavicon") if _find_favicon()
                              else url_for("static", filename="branding/favicon.svg"))
        return ctx

    monitor.start()
    emailer.start()
    register_routes(app)
    return app


def login_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            if request.path.startswith("/api/"):
                return jsonify(error="not authenticated"), 401
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def register_routes(app):

    # ---------------- auth ----------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            user = request.form.get("username", "")
            pw = request.form.get("password", "")
            if (secrets.compare_digest(user, settings.GUI_USERNAME)
                    and secrets.compare_digest(pw, settings.GUI_PASSWORD)):
                session.permanent = True
                session["authed"] = True
                target = request.args.get("next") or url_for("dashboard")
                if not target.startswith("/"):
                    target = url_for("dashboard")
                return redirect(target)
            time.sleep(1.5)   # slow brute force attempts
            error = "Invalid username or password."
        return render_template("login.html", error=error,
                               theme=settings.get("default_theme"))

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---------------- pages ----------------

    @app.route("/")
    @login_required
    def dashboard():
        return render_template("dashboard.html", page="dashboard",
                               theme=settings.get("default_theme"))

    @app.route("/devices")
    @login_required
    def devices_page():
        return render_template("devices.html", page="devices",
                               theme=settings.get("default_theme"))

    @app.route("/events")
    @login_required
    def events_page():
        return render_template("events.html", page="events",
                               theme=settings.get("default_theme"))

    @app.route("/settings")
    @login_required
    def settings_page():
        return render_template("settings.html", page="settings",
                               theme=settings.get("default_theme"))

    @app.route("/heatmap")
    @login_required
    def heatmap_page():
        return render_template("heatmap.html", page="heatmap",
                               theme=settings.get("default_theme"))

    @app.route("/sla")
    @login_required
    def sla_page():
        return render_template("sla.html", page="sla",
                               theme=settings.get("default_theme"))

    @app.route("/wallboard")
    @login_required
    def wallboard_page():
        return render_template("wallboard.html",
                               theme=settings.get("default_theme"),
                               refresh=settings.get("wallboard_refresh"))

    # ---------------- API: devices ----------------

    @app.route("/api/devices")
    @login_required
    def api_devices():
        live = monitor.status()
        warn, crit = settings.get("warn_ms"), settings.get("crit_ms")
        now = time.time()
        out = []
        for d in database.list_devices():
            st = live.get(d["id"], {})
            eff_warn = d.get("warn_override") or warn
            eff_crit = d.get("crit_override") or crit
            stats = database.device_stats(d["id"], now - 3600, now, eff_warn, eff_crit)
            sent = stats["sent"] or 0
            ok = stats["ok"] or 0
            out.append({
                **d,
                "eff_warn": eff_warn,
                "eff_crit": eff_crit,
                "state": (st.get("state", "unknown") if d["enabled"] else "disabled"),
                "in_loss": st.get("in_loss", False),
                "last_latency": st.get("last_latency"),
                "last_ts": st.get("last_ts"),
                "down_since": st.get("down_since"),
                "hour_avg": round(stats["avg_l"], 1) if stats["avg_l"] is not None else None,
                "hour_max": round(stats["max_l"], 1) if stats["max_l"] is not None else None,
                "hour_jitter": round(stats["avg_j"], 1) if stats["avg_j"] is not None else None,
                "hour_loss": round((sent - ok) / sent * 100, 1) if sent else None,
                "hour_warns": stats["warns"] or 0,
                "hour_crits": stats["crits"] or 0,
            })
        return jsonify(devices=out, warn_ms=warn, crit_ms=crit,
                       jitter_warn=settings.get("jitter_warn_ms"),
                       monitoring_enabled=settings.get("monitoring_enabled"),
                       in_maintenance=settings.in_maintenance())

    @app.route("/api/devices", methods=["POST"])
    @login_required
    def api_add_device():
        data = request.get_json(force=True)
        name = (data.get("name") or "").strip()
        host = (data.get("host") or "").strip()
        if not name or not host:
            return jsonify(error="name and host are required"), 400
        interval = _parse_interval(data.get("interval_override"))
        dev_id = database.add_device(name, host,
                                     1 if data.get("enabled", True) else 0, interval)
        for key in ("warn_override", "crit_override"):
            if data.get(key) not in (None, ""):
                database.update_device(dev_id, **{key: _parse_ms(data[key])})
        return jsonify(id=dev_id)

    @app.route("/api/devices/<int:dev_id>", methods=["PUT"])
    @login_required
    def api_update_device(dev_id):
        if not database.get_device(dev_id):
            return jsonify(error="not found"), 404
        data = request.get_json(force=True)
        fields = {}
        if "name" in data:
            fields["name"] = str(data["name"]).strip()
        if "host" in data:
            fields["host"] = str(data["host"]).strip()
        if "enabled" in data:
            fields["enabled"] = 1 if data["enabled"] else 0
        if "interval_override" in data:
            fields["interval_override"] = _parse_interval(data["interval_override"])
        if "warn_override" in data:
            fields["warn_override"] = _parse_ms(data["warn_override"])
        if "crit_override" in data:
            fields["crit_override"] = _parse_ms(data["crit_override"])
        database.update_device(dev_id, **fields)
        return jsonify(ok=True)

    @app.route("/api/devices/<int:dev_id>", methods=["DELETE"])
    @login_required
    def api_delete_device(dev_id):
        database.delete_device(dev_id)
        return jsonify(ok=True)

    # ---------------- API: history / events ----------------

    @app.route("/api/history")
    @login_required
    def api_history():
        try:
            seconds = max(60, min(90 * 86400, int(request.args.get("seconds", 3600))))
        except ValueError:
            seconds = 3600
        end = time.time()
        start = end - seconds
        series, bucket = database.history(start, end)
        g_warn, g_crit = settings.get("warn_ms"), settings.get("crit_ms")
        devices = [{"id": d["id"], "name": d["name"], "host": d["host"],
                    "eff_warn": d.get("warn_override") or g_warn,
                    "eff_crit": d.get("crit_override") or g_crit}
                   for d in database.list_devices(enabled_only=True)]
        return jsonify(start=start, end=end, bucket=bucket,
                       warn_ms=g_warn, crit_ms=g_crit,
                       devices=devices,
                       series={str(k): v for k, v in series.items()})

    @app.route("/api/heatmap")
    @login_required
    def api_heatmap():
        try:
            dev_id = int(request.args.get("device", 0))
            days = max(1, min(60, int(request.args.get("days", 7))))
        except ValueError:
            return jsonify(error="bad parameters"), 400
        if not database.get_device(dev_id):
            return jsonify(error="device not found"), 404
        end = time.time()
        start = end - days * 86400
        return jsonify(cells=database.heatmap(dev_id, start, end),
                       warn_ms=settings.get("warn_ms"),
                       crit_ms=settings.get("crit_ms"),
                       jitter_warn=settings.get("jitter_warn_ms"))

    @app.route("/api/sla")
    @login_required
    def api_sla():
        try:
            days = max(1, min(365, int(request.args.get("days", 30))))
        except ValueError:
            days = 30
        end = time.time()
        start = end - days * 86400
        rows = database.sla_report(start, end,
                                   settings.get("warn_ms"), settings.get("crit_ms"))
        return jsonify(start=start, end=end, days=days, devices=rows)

    @app.route("/api/sla.csv")
    @login_required
    def api_sla_csv():
        try:
            days = max(1, min(365, int(request.args.get("days", 30))))
        except ValueError:
            days = 30
        end = time.time()
        start = end - days * 86400
        rows = database.sla_report(start, end,
                                   settings.get("warn_ms"), settings.get("crit_ms"))
        lines = ["device,host,uptime_pct,downtime_seconds,outages,pings_sent,"
                 "loss_pct,avg_ms,max_ms,jitter_ms,warnings,criticals"]
        for r in rows:
            name = '"' + r["name"].replace('"', '""') + '"'
            lines.append(f"{name},{r['host']},{r['uptime_pct']},{r['downtime_s']},"
                         f"{r['outage_count']},{r['sent']},"
                         f"{r['loss_pct'] if r['loss_pct'] is not None else ''},"
                         f"{r['avg_ms'] if r['avg_ms'] is not None else ''},"
                         f"{r['max_ms'] if r['max_ms'] is not None else ''},"
                         f"{r['jitter_ms'] if r['jitter_ms'] is not None else ''},"
                         f"{r['warns']},{r['crits']}")
        return app.response_class("\n".join(lines) + "\n", mimetype="text/csv",
                                  headers={"Content-Disposition":
                                           f"attachment; filename=sla-{days}d.csv"})

    @app.route("/api/events")
    @login_required
    def api_events():
        try:
            limit = max(1, min(1000, int(request.args.get("limit", 200))))
        except ValueError:
            limit = 200
        return jsonify(events=database.list_events(limit=limit))

    # ---------------- API: settings & email ----------------

    @app.route("/api/settings", methods=["GET"])
    @login_required
    def api_get_settings():
        vals = settings.all_settings()
        vals["gmail_app_password"] = "********" if vals["gmail_app_password"] else ""
        return jsonify(settings=vals,
                       email_last_error=emailer.last_error,
                       email_last_sent=emailer.last_sent)

    @app.route("/api/settings", methods=["POST"])
    @login_required
    def api_set_settings():
        data = request.get_json(force=True) or {}
        # don't overwrite the stored app password with the mask
        if data.get("gmail_app_password") == "********":
            data.pop("gmail_app_password")
        applied = settings.update(data)
        return jsonify(applied=applied)

    @app.route("/api/test-email", methods=["POST"])
    @login_required
    def api_test_email():
        try:
            emailer.send_test()
            return jsonify(ok=True)
        except Exception as e:
            return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 400

    @app.route("/api/send-report", methods=["POST"])
    @login_required
    def api_send_report():
        kind = str(request.get_json(force=True).get("kind", "24"))
        if kind not in REPORT_KINDS:
            return jsonify(error="kind must be 6, 12 or 24"), 400
        end = time.time()
        start = end - REPORT_KINDS[kind] * 3600
        queued = emailer.send_report(kind, start, end, force=True)
        return jsonify(ok=True, queued=queued)


def _parse_interval(value):
    if value in (None, "", "null"):
        return None
    try:
        return max(0.2, min(60.0, float(value)))
    except (TypeError, ValueError):
        return None


def _parse_ms(value):
    if value in (None, "", "null"):
        return None
    try:
        return max(1.0, min(10000.0, float(value)))
    except (TypeError, ValueError):
        return None
