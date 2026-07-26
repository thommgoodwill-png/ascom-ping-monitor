"""Flask application: authenticated GUI + JSON API."""
import functools
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time

from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, send_from_directory, session, url_for)

from . import capture, database, floorplans, netcheck, netdiag, oui, settings
from .emailer import Emailer, REPORT_KINDS
from .monitor import Monitor
from .webhooks import Webhooks

log = logging.getLogger("pingmon.app")

emailer = Emailer()
webhooks = Webhooks()
monitor = Monitor(emailer, webhooks)
from .agent import Agent  # noqa: E402
agent = Agent(monitor)
from .feed import CallFeed  # noqa: E402
feed = CallFeed()
from .imtbridge import ImtBridge  # noqa: E402
imt = ImtBridge(webhooks, feed)
from . import teldb  # noqa: E402


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

    is_desktop = getattr(sys, "frozen", False) or os.name == "nt"

    @app.context_processor
    def inject_branding():
        ctx = {"is_desktop": is_desktop}
        ctx["logo_url"] = (url_for("userlogo") if _find_logo(user_logo_dir)
                           else url_for("static", filename="branding/" + static_logo))
        ctx["favicon_url"] = (url_for("userfavicon") if _find_favicon()
                              else url_for("static", filename="branding/favicon.svg"))
        ctx["current_user"] = session.get("username")
        ctx["current_role"] = session.get("role", "standard")
        ctx["is_admin"] = session.get("role") == "admin"
        return ctx

    from .agentapi import bp as agent_bp
    app.register_blueprint(agent_bp)

    monitor.start()
    emailer.start()
    webhooks.start()
    agent.start()
    feed.start()
    imt.start()
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


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("authed"):
            if request.path.startswith("/api/"):
                return jsonify(error="not authenticated"), 401
            return redirect(url_for("login", next=request.path))
        if session.get("role") != "admin":
            if request.path.startswith("/api/"):
                return jsonify(error="administrator access required"), 403
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return wrapper


def register_routes(app):

    @app.before_request
    def _enforce_2fa():
        """If the admin has required 2FA, push logged-in users who haven't set
        it up to the account page until they do."""
        if not session.get("authed") or not settings.get("require_2fa"):
            return
        u = database.get_user(session.get("uid"))
        if not u or u["totp_enabled"]:
            return
        p = request.path
        allow = (p.startswith("/static/") or p.startswith("/api/profile")
                 or p in ("/account", "/logout", "/userlogo", "/userfavicon"))
        # Escape hatch: an admin must never be able to lock themselves out with
        # their own "require 2FA" setting. Always let admins reach Settings (and
        # the Users page) so they can turn the requirement back off.
        if u.get("role") == "admin":
            allow = allow or (p in ("/settings", "/users", "/api/settings")
                              or p.startswith("/api/users"))
        if allow:
            return
        if p.startswith("/api/"):
            return jsonify(error="2FA setup required", need_2fa=True), 403
        return redirect(url_for("account_page"))

    # ---------------- auth ----------------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        from . import auth
        error = None
        stage = "password"
        if request.method == "POST":
            stage = request.form.get("stage", "password")
            nxt = request.args.get("next") or url_for("dashboard")
            if not nxt.startswith("/"):
                nxt = url_for("dashboard")

            if stage == "totp":
                # second factor: user already passed the password stage
                uid = session.get("pending_uid")
                u = database.get_user(uid) if uid else None
                if u and auth.verify_totp(u.get("totp_secret"),
                                          request.form.get("code", "")):
                    session.pop("pending_uid", None)
                    _complete_login(u)
                    return redirect(nxt)
                time.sleep(1.0)
                error = "Invalid authentication code."
                return render_template("login.html", error=error, stage="totp",
                                       theme=settings.get("default_theme"))

            # first factor: username + password
            username = request.form.get("username", "").strip()
            pw = request.form.get("password", "")
            u = database.get_user_by_name(username)
            if u and not u["disabled"] and auth.verify_password(u["password_hash"], pw):
                # email allow-list gate (admin-configured)
                patterns = auth.parse_email_patterns(settings.get("allowed_emails"))
                if not auth.email_allowed(u.get("email"), patterns):
                    time.sleep(1.0)
                    error = ("Access denied: your account email is not on the "
                             "allowed list. Contact an administrator.")
                    return render_template("login.html", error=error,
                                           theme=settings.get("default_theme"))
                if u["totp_enabled"]:
                    session["pending_uid"] = u["id"]   # go to 2FA step
                    return render_template("login.html", error=None, stage="totp",
                                           theme=settings.get("default_theme"))
                _complete_login(u)
                return redirect(nxt)
            time.sleep(1.5)   # slow brute-force attempts
            error = "Invalid username or password."
        return render_template("login.html", error=error, stage=stage,
                               theme=settings.get("default_theme"))

    def _complete_login(u):
        session.permanent = True
        session["authed"] = True
        session["uid"] = u["id"]
        session["username"] = u["username"]
        session["role"] = u["role"]
        database.update_user(u["id"], last_login=time.time())

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/api/shutdown", methods=["POST"])
    @login_required
    def api_shutdown():
        """Stop the whole application. Mainly for the Windows exe so users can
        quit without Task Manager."""
        log.info("shutdown requested from the GUI")

        def _stop():
            time.sleep(0.4)
            try:
                monitor.stop()
                emailer.stop()
                webhooks.stop()
                agent.stop()
                imt.stop()
            except Exception:
                pass
            os._exit(0)   # hard exit — reliably stops waitress on every OS
        threading.Thread(target=_stop, daemon=True).start()
        return jsonify(ok=True)

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

    # Settings are split across two pages: the network-monitoring settings live
    # under Network Monitoring → Settings, and the shared/account settings stay
    # on the top-level Settings tab.
    NET_SETTINGS = ["monitoring", "problem", "discovery", "maintenance",
                    "failure_alerts", "reports", "capture"]
    GEN_SETTINGS = ["email", "access", "agent", "webhooks", "interface"]

    @app.route("/settings")
    @login_required
    def settings_page():
        return render_template("settings.html", page="settings",
                               settings_title="Settings", sections=GEN_SETTINGS,
                               theme=settings.get("default_theme"))

    @app.route("/network/settings")
    @login_required
    def network_settings_page():
        return render_template("settings.html", page="network_settings",
                               settings_title="Network monitoring settings",
                               sections=NET_SETTINGS,
                               theme=settings.get("default_theme"))

    @app.route("/users")
    @admin_required
    def users_page():
        return render_template("users.html", page="users",
                               theme=settings.get("default_theme"))

    @app.route("/account")
    @login_required
    def account_page():
        return render_template("account.html", page="account",
                               theme=settings.get("default_theme"))

    # ---------------- customers / sites (hub) ----------------

    @app.route("/customers")
    @login_required
    def customers_page():
        return render_template("customers.html", page="customers",
                               theme=settings.get("default_theme"))

    @app.route("/customers/<int:cid>")
    @login_required
    def customer_page(cid):
        if not database.get_customer(cid):
            return redirect(url_for("customers_page"))
        return render_template("customer.html", page="customers", cid=cid,
                               theme=settings.get("default_theme"))

    @app.route("/sites/<int:site_id>")
    @login_required
    def site_page(site_id):
        site = database.get_site(site_id)
        if not site:
            return redirect(url_for("customers_page"))
        return render_template("site.html", page="customers", site_id=site_id,
                               site=site, page_sub="devices",
                               theme=settings.get("default_theme"))

    @app.route("/sites/<int:site_id>/events")
    @login_required
    def site_events_page(site_id):
        site = database.get_site(site_id)
        if not site:
            return redirect(url_for("customers_page"))
        return render_template("events.html", page="customers", site_id=site_id,
                               site=site, page_sub="events",
                               theme=settings.get("default_theme"))

    @app.route("/sites/<int:site_id>/heatmap")
    @login_required
    def site_heatmap_page(site_id):
        site = database.get_site(site_id)
        if not site:
            return redirect(url_for("customers_page"))
        return render_template("heatmap.html", page="customers", site_id=site_id,
                               site=site, page_sub="heatmap",
                               theme=settings.get("default_theme"))

    @app.route("/sites/<int:site_id>/sla")
    @login_required
    def site_sla_page(site_id):
        site = database.get_site(site_id)
        if not site:
            return redirect(url_for("customers_page"))
        return render_template("sla.html", page="customers", site_id=site_id,
                               site=site, page_sub="sla",
                               theme=settings.get("default_theme"))

    # ---------------- API: customers / sites ----------------

    @app.route("/api/customers")
    @login_required
    def api_customers():
        return jsonify(customers=database.list_customers())

    @app.route("/api/customers", methods=["POST"])
    @login_required
    def api_add_customer():
        data = request.get_json(force=True) or {}
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify(error="name required"), 400
        return jsonify(id=database.add_customer(name, (data.get("notes") or "").strip()))

    @app.route("/api/customers/<int:cid>", methods=["DELETE"])
    @login_required
    def api_delete_customer(cid):
        database.delete_customer(cid)
        return jsonify(ok=True)

    @app.route("/api/customers/<int:cid>/sites")
    @login_required
    def api_customer_sites(cid):
        cust = database.get_customer(cid)
        if not cust:
            return jsonify(error="not found"), 404
        return jsonify(customer=cust, sites=database.list_sites(cid))

    @app.route("/api/customers/<int:cid>/sites", methods=["POST"])
    @login_required
    def api_add_site(cid):
        if not database.get_customer(cid):
            return jsonify(error="customer not found"), 404
        name = (request.get_json(force=True).get("name") or "").strip()
        if not name:
            return jsonify(error="name required"), 400
        key = "ste_" + secrets.token_hex(20)
        sid = database.add_site(cid, name, key)
        return jsonify(id=sid, api_key=key)

    @app.route("/api/sites/<int:site_id>", methods=["DELETE"])
    @login_required
    def api_delete_site(site_id):
        database.delete_site(site_id)
        return jsonify(ok=True)

    @app.route("/api/sites/<int:site_id>/rekey", methods=["POST"])
    @login_required
    def api_rekey_site(site_id):
        if not database.get_site(site_id):
            return jsonify(error="not found"), 404
        key = "ste_" + secrets.token_hex(20)
        database.update_site(site_id, api_key=key)
        return jsonify(api_key=key)

    @app.route("/api/sites/<int:site_id>")
    @login_required
    def api_site(site_id):
        site = database.get_site(site_id)
        if not site:
            return jsonify(error="not found"), 404
        live = monitor.status()
        warn, crit = settings.get("warn_ms"), settings.get("crit_ms")
        now = time.time()
        out = []
        for d in database.list_devices(site_id=site_id):
            st = live.get(d["id"], {})   # only populated if the hub also pings it
            eff_warn = d.get("warn_override") or warn
            eff_crit = d.get("crit_override") or crit
            stats = database.device_stats(d["id"], now - 3600, now, eff_warn, eff_crit)
            sent = stats["sent"] or 0
            ok = stats["ok"] or 0
            last = database.last_ping(d["id"])
            out.append({
                **d, "eff_warn": eff_warn, "eff_crit": eff_crit,
                "vendor": oui.vendor(d.get("mac")),
                "last_latency": last["latency"] if last else None,
                "last_ts": last["ts"] if last else None,
                "last_success": bool(last["success"]) if last else None,
                "hour_avg": round(stats["avg_l"], 1) if stats["avg_l"] is not None else None,
                "hour_max": round(stats["max_l"], 1) if stats["max_l"] is not None else None,
                "hour_jitter": round(stats["avg_j"], 1) if stats["avg_j"] is not None else None,
                "hour_loss": round((sent - ok) / sent * 100, 1) if sent else None,
            })
        # agent online if seen in the last 3 minutes
        agent_online = bool(site["last_seen"] and now - site["last_seen"] < 180)
        return jsonify(site=site, devices=out, warn_ms=warn, crit_ms=crit,
                       jitter_warn=settings.get("jitter_warn_ms"),
                       agent_online=agent_online, now=now)

    @app.route("/api/sites/<int:site_id>/history")
    @login_required
    def api_site_history(site_id):
        if not database.get_site(site_id):
            return jsonify(error="not found"), 404
        try:
            seconds = max(60, min(90 * 86400, int(request.args.get("seconds", 3600))))
        except ValueError:
            seconds = 3600
        end = time.time()
        start = end - seconds
        series, bucket = database.history(start, end)
        g_warn, g_crit = settings.get("warn_ms"), settings.get("crit_ms")
        devs = database.list_devices(enabled_only=True, site_id=site_id)
        devices = [{"id": d["id"], "name": d["name"], "host": d["host"],
                    "eff_warn": d.get("warn_override") or g_warn,
                    "eff_crit": d.get("crit_override") or g_crit} for d in devs]
        keep = {d["id"] for d in devs}
        return jsonify(start=start, end=end, bucket=bucket,
                       warn_ms=g_warn, crit_ms=g_crit, devices=devices,
                       series={str(k): v for k, v in series.items() if k in keep})

    @app.route("/api/sites/<int:site_id>/devices", methods=["POST"])
    @login_required
    def api_site_add_device(site_id):
        if not database.get_site(site_id):
            return jsonify(error="site not found"), 404
        data = request.get_json(force=True) or {}
        name = (data.get("name") or "").strip()
        host = (data.get("host") or "").strip()
        if not name or not host:
            return jsonify(error="name and host required"), 400
        did = database.add_device(name, host, 1,
                                  _parse_interval(data.get("interval_override")),
                                  site_id=site_id)
        extra = {}
        if data.get("tcp_ports"):
            extra["tcp_ports"] = ",".join(str(p) for p in
                                          netcheck.parse_ports(data["tcp_ports"]))
        if data.get("check_url"):
            extra["check_url"] = str(data["check_url"]).strip()[:300]
        for k in ("warn_override", "crit_override"):
            if data.get(k) not in (None, ""):
                extra[k] = _parse_ms(data[k])
        if extra:
            database.update_device(did, **extra)
        return jsonify(id=did)

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

    @app.route("/capture")
    @login_required
    def capture_page():
        return render_template("capture.html", page="capture",
                               theme=settings.get("default_theme"))

    @app.route("/tools")
    @login_required
    def tools_page():
        return render_template("tools.html", page="tools",
                               theme=settings.get("default_theme"))

    # ---------------- API: on-demand network tools ----------------

    @app.route("/api/tools/env")
    @login_required
    def api_tools_env():
        return jsonify(default_subnet=netdiag.local_subnet(),
                       snmp=netdiag.snmp_available(),
                       iperf=netdiag.iperf_available())

    @app.route("/api/tools/discover", methods=["POST"])
    @login_required
    def api_tools_discover():
        cidr = (request.get_json(force=True) or {}).get("subnet", "").strip()
        cidr = cidr or netdiag.local_subnet()
        try:
            found = netdiag.discover(cidr)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        # merge known-device baseline info, then record into the baseline.
        # A manual scan populates Known Devices (but never sends rogue alerts —
        # only the background scan does that). "known" reflects PRE-scan state so
        # the UI still shows which devices were new this scan.
        known = {k["mac"]: k for k in database.list_known_devices()}
        monitored = {d["host"] for d in database.list_devices()}
        now = time.time()
        for f in found:
            k = known.get(f["mac"]) if f["mac"] else None
            f["known"] = bool(k)
            f["monitored"] = f["ip"] in monitored
            if f["mac"]:
                database.seen_device(f["mac"], f["ip"], f["vendor"], now)
        return jsonify(subnet=cidr, devices=found)

    @app.route("/api/tools/path", methods=["POST"])
    @login_required
    def api_tools_path():
        d = request.get_json(force=True) or {}
        host = (d.get("host") or "").strip()
        if not host:
            return jsonify(error="host required"), 400
        cycles = max(1, min(20, int(d.get("cycles", 5))))
        return jsonify(netdiag.path_analysis(host, cycles))

    @app.route("/api/tools/snmp", methods=["POST"])
    @login_required
    def api_tools_snmp():
        d = request.get_json(force=True) or {}
        host = (d.get("host") or "").strip()
        if not host:
            return jsonify(error="host required"), 400
        return jsonify(netdiag.snmp_get(host, d.get("community", "public").strip()
                                        or "public", d.get("version", "2c")))

    @app.route("/api/tools/iperf", methods=["POST"])
    @login_required
    def api_tools_iperf():
        d = request.get_json(force=True) or {}
        host = (d.get("host") or "").strip()
        if not host:
            return jsonify(error="host required"), 400
        return jsonify(netdiag.iperf_test(host, max(1, min(30, int(d.get("seconds", 5)))),
                                          bool(d.get("reverse")),
                                          int(d.get("port", 5201))))

    @app.route("/api/tools/known")
    @login_required
    def api_tools_known():
        rows = database.list_known_devices()
        for r in rows:
            if not r.get("vendor") and r.get("mac"):
                r["vendor"] = oui.vendor(r["mac"])
        return jsonify(devices=rows)

    @app.route("/api/tools/acknowledge", methods=["POST"])
    @login_required
    def api_tools_ack():
        mac = (request.get_json(force=True) or {}).get("mac", "")
        database.acknowledge_device(mac)
        return jsonify(ok=True)

    # ---------------- API: packet capture ----------------

    @app.route("/api/capture/env")
    @login_required
    def api_capture_env():
        return jsonify(enabled=settings.get("capture_enabled"),
                       available=capture.available(),
                       interfaces=capture.list_interfaces(),
                       max_seconds=settings.get("capture_max_seconds"),
                       max_packets=settings.get("capture_max_packets"),
                       captures=capture.list_captures())

    @app.route("/api/capture/start", methods=["POST"])
    @login_required
    def api_capture_start():
        data = request.get_json(force=True) or {}
        secs = min(int(data.get("seconds", 15) or 15),
                   settings.get("capture_max_seconds"))
        pkts = min(int(data.get("packets", 1000) or 1000),
                   settings.get("capture_max_packets"))
        try:
            job = capture.start_capture(data.get("iface", "any"),
                                        data.get("bpf", ""), secs, pkts)
        except Exception as e:
            return jsonify(error=str(e)), 400
        return jsonify(id=job.id)

    @app.route("/api/capture/status/<job_id>")
    @login_required
    def api_capture_status(job_id):
        job = capture.get_job(job_id)
        if not job:
            return jsonify(error="not found"), 404
        return jsonify(job.status())

    @app.route("/api/capture/file/<path:fname>")
    @login_required
    def api_capture_file(fname):
        if "/" in fname or "\\" in fname or not fname.endswith(".pcap"):
            return jsonify(error="bad filename"), 400
        return send_from_directory(capture.CAP_DIR, fname, as_attachment=True)

    @app.route("/api/capture/delete", methods=["POST"])
    @login_required
    def api_capture_delete():
        fname = (request.get_json(force=True) or {}).get("file", "")
        try:
            capture.delete_capture(fname)
        except (ValueError, OSError) as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True)

    @app.route("/api/capture/view/<path:fname>")
    @login_required
    def api_capture_view(fname):
        if "/" in fname or "\\" in fname or not fname.endswith(".pcap"):
            return jsonify(error="bad filename"), 400
        path = os.path.join(capture.CAP_DIR, fname)
        try:
            limit = max(1, min(5000, int(request.args.get("limit", 1000))))
        except ValueError:
            limit = 1000
        try:
            rows, total = capture.summarize_file(path, limit)
        except FileNotFoundError:
            return jsonify(error="not found"), 404
        except Exception as e:
            return jsonify(error=str(e)), 400
        return jsonify(file=fname, count=total, shown=len(rows), packets=rows)

    @app.route("/api/capture/upload", methods=["POST"])
    @login_required
    def api_capture_upload():
        if not settings.get("capture_enabled"):
            return jsonify(error="packet capture is disabled in Settings"), 400
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify(error="no file"), 400
        name = os.path.basename(f.filename)
        if not name.lower().endswith((".pcap", ".pcapng", ".cap")):
            return jsonify(error="please upload a .pcap / .pcapng / .cap file"), 400
        os.makedirs(capture.CAP_DIR, exist_ok=True)
        # store under a safe, unique name
        safe = "upload-" + re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if not safe.endswith(".pcap"):
            safe += ".pcap"
        dest = os.path.join(capture.CAP_DIR, safe)
        f.save(dest)
        return jsonify(file=safe)

    # ---------------- API: devices ----------------

    @app.route("/api/devices")
    @login_required
    def api_devices():
        live = monitor.status()
        warn, crit = settings.get("warn_ms"), settings.get("crit_ms")
        now = time.time()
        out = []
        for d in database.list_devices(site_id=None):   # hub-local only
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
                "vendor": oui.vendor(d.get("mac")),
                "state": (st.get("state", "unknown") if d["enabled"] else "disabled"),
                "in_loss": st.get("in_loss", False),
                "last_latency": st.get("last_latency"),
                "last_ts": st.get("last_ts"),
                "down_since": st.get("down_since"),
                "checks": st.get("checks", {}),
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
        extra = {}
        if data.get("tcp_ports"):
            extra["tcp_ports"] = ",".join(str(p) for p in
                                          netcheck.parse_ports(data["tcp_ports"]))
        if data.get("check_url"):
            extra["check_url"] = str(data["check_url"]).strip()[:300]
        if extra:
            database.update_device(dev_id, **extra)
        return jsonify(id=dev_id)

    @app.route("/api/devices/reorder", methods=["POST"])
    @login_required
    def api_devices_reorder():
        """Persist a new device display order (drag-to-reorder on the Devices
        page). Body: {"ids": [3, 1, 2, ...]}."""
        data = request.get_json(force=True) or {}
        ids = []
        for i in (data.get("ids") or []):
            try:
                ids.append(int(i))
            except (TypeError, ValueError):
                continue
        database.reorder_devices(ids)
        return jsonify(ok=True)

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
        if "tcp_ports" in data:
            fields["tcp_ports"] = ",".join(str(p) for p in
                                           netcheck.parse_ports(data["tcp_ports"]))
        if "check_url" in data:
            fields["check_url"] = str(data["check_url"] or "").strip()[:300]
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
                   for d in database.list_devices(enabled_only=True, site_id=None)]
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

    def _view_scope():
        """Which devices a top-level view covers. No ?site → hub-local only
        (site_id=None); ?site=<id> → that site's devices."""
        sid = request.args.get("site")
        if sid:
            try:
                return int(sid)
            except (TypeError, ValueError):
                pass
        return None

    @app.route("/api/sla")
    @login_required
    def api_sla():
        try:
            days = max(1, min(365, int(request.args.get("days", 30))))
        except ValueError:
            days = 30
        end = time.time()
        start = end - days * 86400
        rows = database.sla_report(start, end, settings.get("warn_ms"),
                                   settings.get("crit_ms"), site_id=_view_scope())
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
        rows = database.sla_report(start, end, settings.get("warn_ms"),
                                   settings.get("crit_ms"), site_id=_view_scope())
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
        return jsonify(events=database.list_events(limit=limit, site_id=_view_scope()))

    # ---------------- API: floor plans ----------------

    def _fp_online(d, live):
        """Simplified status for a floor-plan pin: 'up' (green), 'down' (red,
        throbbing) or 'unknown' (grey). Works for hub-local devices (live
        monitor state) and site devices (last pushed ping)."""
        if not d["enabled"]:
            return "unknown"
        st = live.get(d["id"])
        if st and st.get("state"):
            return "down" if st["state"] == "down" else \
                   ("unknown" if st["state"] == "unknown" else "up")
        lp = database.last_ping(d["id"])
        if not lp:
            return "unknown"
        return "up" if lp["success"] else "down"

    @app.route("/api/floorplans")
    @login_required
    def api_floorplans():
        scope = _view_scope()
        plans = database.list_floorplans(site_id=scope)
        devs = {d["id"]: d for d in database.list_devices(site_id=scope)}
        out = []
        for p in plans:
            pins = []
            for pin in database.list_pins(p["id"]):
                d = devs.get(pin["device_id"])
                if not d:
                    continue
                pins.append({"pin_id": pin["id"], "device_id": pin["device_id"],
                             "x": pin["x"], "y": pin["y"],
                             "name": d["name"], "host": d["host"]})
            out.append({"id": p["id"], "name": p["name"], "ext": p["ext"],
                        "w": p["w"], "h": p["h"], "pins": pins})
        # devices available to place (in scope)
        avail = [{"id": d["id"], "name": d["name"], "host": d["host"]}
                 for d in devs.values()]
        return jsonify(floorplans=out, devices=avail)

    @app.route("/api/floorplans", methods=["POST"])
    @login_required
    def api_floorplan_upload():
        f = request.files.get("file")
        if not f or not f.filename:
            return jsonify(error="no file uploaded"), 400
        name = (request.form.get("name") or f.filename.rsplit(".", 1)[0]).strip()[:80]
        scope = request.form.get("site")
        site_id = None
        if scope:
            try:
                site_id = int(scope)
            except ValueError:
                site_id = None
        if floorplans.ext_of(f.filename) not in floorplans.ALLOWED_EXT:
            return jsonify(error="Unsupported type. Allowed: JPG, PNG, SVG, PDF."), 400
        fp_id = database.add_floorplan(name or "Floor plan", "png", site_id=site_id)
        try:
            store_ext, w, h = floorplans.save_upload(f, fp_id)
        except (ValueError, RuntimeError) as e:
            database.delete_floorplan(fp_id)
            return jsonify(error=str(e)), 400
        except Exception as e:
            database.delete_floorplan(fp_id)
            log.warning("floor plan upload failed: %s", e)
            return jsonify(error="Couldn't process that file. Try a PNG, JPG or SVG."), 400
        # persist the real stored extension + dimensions
        db = database.get_db()
        db.execute("UPDATE floorplans SET ext=?, w=?, h=? WHERE id=?",
                   (store_ext, w, h, fp_id))
        db.commit()
        return jsonify(id=fp_id, ext=store_ext, w=w, h=h)

    @app.route("/api/floorplans/<int:fp_id>", methods=["PUT"])
    @login_required
    def api_floorplan_rename(fp_id):
        if not database.get_floorplan(fp_id):
            return jsonify(error="not found"), 404
        name = (request.get_json(force=True) or {}).get("name", "").strip()[:80]
        if name:
            database.rename_floorplan(fp_id, name)
        return jsonify(ok=True)

    @app.route("/api/floorplans/<int:fp_id>", methods=["DELETE"])
    @login_required
    def api_floorplan_delete(fp_id):
        p = database.get_floorplan(fp_id)
        if not p:
            return jsonify(error="not found"), 404
        floorplans.delete_image(fp_id, p["ext"])
        database.delete_floorplan(fp_id)
        return jsonify(ok=True)

    @app.route("/floorplan/<int:fp_id>/image")
    @login_required
    def floorplan_image(fp_id):
        p = database.get_floorplan(fp_id)
        if not p:
            return "not found", 404
        path = floorplans.image_path(fp_id, p["ext"])
        if not os.path.exists(path):
            return "no image", 404
        return send_file(path, mimetype=floorplans.MIME.get(p["ext"], "image/png"))

    @app.route("/api/floorplans/<int:fp_id>/pins", methods=["POST"])
    @login_required
    def api_floorplan_add_pin(fp_id):
        if not database.get_floorplan(fp_id):
            return jsonify(error="not found"), 404
        data = request.get_json(force=True) or {}
        try:
            device_id = int(data.get("device_id"))
            x = max(0.0, min(1.0, float(data.get("x"))))
            y = max(0.0, min(1.0, float(data.get("y"))))
        except (TypeError, ValueError):
            return jsonify(error="device_id, x, y required"), 400
        if not database.get_device(device_id):
            return jsonify(error="device not found"), 404
        pin_id = database.add_pin(fp_id, device_id, x, y)
        return jsonify(pin_id=pin_id)

    @app.route("/api/floorplans/pins/<int:pin_id>", methods=["PUT"])
    @login_required
    def api_floorplan_move_pin(pin_id):
        if not database.get_pin(pin_id):
            return jsonify(error="not found"), 404
        data = request.get_json(force=True) or {}
        try:
            x = max(0.0, min(1.0, float(data.get("x"))))
            y = max(0.0, min(1.0, float(data.get("y"))))
        except (TypeError, ValueError):
            return jsonify(error="x, y required"), 400
        database.move_pin(pin_id, x, y)
        return jsonify(ok=True)

    @app.route("/api/floorplans/pins/<int:pin_id>", methods=["DELETE"])
    @login_required
    def api_floorplan_delete_pin(pin_id):
        if not database.get_pin(pin_id):
            return jsonify(error="not found"), 404
        database.delete_pin(pin_id)
        return jsonify(ok=True)

    @app.route("/api/floorplans/status")
    @login_required
    def api_floorplan_status():
        """Per-device current status + drop count over a window, for colouring
        pins and the problem-area heat. seconds=0 → just current status."""
        scope = _view_scope()
        try:
            seconds = max(0, min(90 * 86400, int(request.args.get("seconds", 0))))
        except ValueError:
            seconds = 0
        now = time.time()
        start = now - seconds if seconds else now
        live = monitor.status()
        out = {}
        for d in database.list_devices(site_id=scope):
            lp = database.last_ping(d["id"])
            out[str(d["id"])] = {
                "state": _fp_online(d, live),
                "last_latency": lp["latency"] if lp else None,
                "last_ts": lp["ts"] if lp else None,
                "drops": database.device_drop_count(d["id"], start, now) if seconds else 0,
            }
        return jsonify(now=now, seconds=seconds, devices=out)

    @app.route("/api/floorplans/timeline")
    @login_required
    def api_floorplan_timeline():
        """down/up events per device over a window so the history slider can
        reconstruct each device's state at any moment."""
        scope = _view_scope()
        try:
            seconds = max(3600, min(90 * 86400, int(request.args.get("seconds", 86400))))
        except ValueError:
            seconds = 86400
        now = time.time()
        start = now - seconds
        out = {}
        for d in database.list_devices(site_id=scope):
            evs = database.device_status_events(d["id"], start, now)
            drops = database.device_drop_count(d["id"], start, now)
            out[str(d["id"])] = {"events": [[e["ts"], e["type"]] for e in evs],
                                 "drops": drops}
        return jsonify(now=now, start=start, seconds=seconds, devices=out)

    # ---------------- IMT bridge ----------------

    @app.route("/telligence")
    @login_required
    def telligence_page():
        return render_template("imt.html", page="telligence", site_id=None,
                               site=None, theme=settings.get("default_theme"))

    @app.route("/imt")
    @login_required
    def imt_page():          # legacy path — keep old links/bookmarks working
        return redirect(url_for("telligence_page"))

    @app.route("/telligence/settings")
    @admin_required
    def telligence_settings_page():
        return render_template("imt_settings.html", page="telligence_settings",
                               theme=settings.get("default_theme"))

    @app.route("/sites/<int:site_id>/imt")
    @login_required
    def site_imt_page(site_id):
        site = database.get_site(site_id)
        if not site:
            return redirect(url_for("customers_page"))
        return render_template("imt.html", page="customers", site_id=site_id,
                               site=site, page_sub="imt",
                               theme=settings.get("default_theme"))

    IMT_KEYS = ["imt_enabled", "imt_db_path", "imt_log_path", "imt_config_db_path",
                "imt_poll_secs", "imt_alert", "imt_emergency_alert",
                "imt_service_check", "imt_service_stale_secs", "imt_service_name"]

    @app.route("/api/imt/config")
    @login_required
    def api_imt_config():
        # config is for THIS instance's local reader (a site configures its own
        # bridge on its own agent), so it's global — not site-scoped.
        return jsonify(config={k: settings.get(k) for k in IMT_KEYS})

    @app.route("/api/imt/config", methods=["POST"])
    @admin_required
    def api_imt_config_set():
        data = request.get_json(force=True) or {}
        clean = {k: v for k, v in data.items() if k in IMT_KEYS}
        applied = settings.update(clean)
        return jsonify(applied=applied)

    def _duty_for(loc_string, duties):
        """Longest duty-area whose path is a prefix of loc_string (segment-aware)."""
        if not loc_string:
            return None
        best = None
        for d in duties:
            ds = d["string"]
            if ds and (loc_string == ds or loc_string.startswith(ds + "-")):
                if best is None or len(ds) > len(best["string"]):
                    best = d
        return best

    @app.route("/api/imt/calls")
    @login_required
    def api_imt_calls():
        scope = _view_scope()
        duties = database.imt_duty_areas(scope)

        def attach(items):
            for it in items:
                it["duty"] = _duty_for(it.get("location_string"), duties)
            return items

        active = attach(database.imt_list_active_calls(scope))
        faults = attach([d for d in database.imt_list_devices(scope)
                         if d["status"] == "failed"])
        return jsonify(active=active, faults=faults,
                       recent=database.imt_list_recent_calls(scope, limit=100),
                       duty_areas=duties, counts=database.imt_call_counts(scope))

    @app.route("/api/imt/fault-devices")
    @login_required
    def api_imt_fault_devices():
        """The specific devices behind a faulted room (drill-down). Reads the
        bridge DB + Telligence config cache on the reading instance."""
        from . import imtbridge as _ib
        ident = request.args.get("ident", "")
        if _view_scope() is not None:
            return jsonify(remote=True)   # config cache lives on the site's agent
        cfg = _ib.load_cfg()
        try:
            return jsonify(_ib.read_fault_devices(
                cfg["db_path"], cfg["config_db_path"], ident))
        except Exception as e:
            return jsonify(error=f"{type(e).__name__}: {e}"), 400

    @app.route("/api/imt/status")
    @login_required
    def api_imt_status():
        scope = _view_scope()
        counts = database.imt_counts(scope)
        counts["calls"] = database.imt_call_counts(scope)
        if scope is None:
            # local reader — live status straight off the reader thread
            return jsonify(status=imt.status(), counts=counts, scope="local")
        # a site — status derived from what the site's agent has pushed up
        site = database.get_site(scope)
        evs = database.imt_list_events(scope, limit=1)
        st = {
            "enabled": True,
            "configured": counts["total"] > 0,
            "connected": bool(site and site.get("last_seen")),
            "last_error": None,
            "event_count": None,
            "last_event_ts": evs[0]["ts"] if evs else None,
            "last_poll_ts": site.get("last_seen") if site else None,
            "remote": True,
            "agent_host": site.get("agent_host") if site else None,
        }
        return jsonify(status=st, counts=counts, scope="site")

    @app.route("/api/imt/devices")
    @login_required
    def api_imt_devices():
        return jsonify(devices=database.imt_list_devices(_view_scope()))

    @app.route("/api/imt/messages")
    @login_required
    def api_imt_messages():
        # the raw log feed only exists on the instance doing the reading
        if _view_scope() is not None:
            return jsonify(messages=[], remote=True)
        return jsonify(messages=imt.recent_messages())

    @app.route("/api/imt/events")
    @login_required
    def api_imt_events():
        return jsonify(events=database.imt_list_events(_view_scope(), limit=200))

    @app.route("/api/imt/test", methods=["POST"])
    @login_required
    def api_imt_test():
        try:
            return jsonify(result=imt.test_connection())
        except Exception as e:
            return jsonify(error=f"{type(e).__name__}: {e}"), 400

    @app.route("/api/imt/clear", methods=["POST"])
    @admin_required
    def api_imt_clear():
        database.imt_clear_devices(_view_scope())
        return jsonify(ok=True)

    @app.route("/api/imt/debug")
    @admin_required
    def api_imt_debug():
        """Live, unfiltered readout of what the reader sees in the bridge DB —
        so a fault that isn't surfacing can be diagnosed without guessing."""
        from . import imtbridge as _ib
        cfg = _ib.load_cfg()
        out = {"reader_version": _ib.READER_VERSION, "db_path": cfg["db_path"],
               "db_exists": bool(cfg["db_path"]) and os.path.exists(cfg["db_path"])}
        # is there a WAL sidecar right now, and how big?
        if out["db_exists"]:
            wal = cfg["db_path"] + "-wal"
            out["wal_exists"] = os.path.exists(wal)
            out["wal_bytes"] = os.path.getsize(wal) if out["wal_exists"] else 0
            try:
                out["located_faults"] = _ib.read_db_faults(cfg["db_path"])
            except Exception as e:
                out["read_db_faults_error"] = f"{type(e).__name__}: {e}"
            # raw dumps of BOTH active-event tables (all rows, unfiltered) so a
            # fault that isn't surfacing can be located precisely
            try:
                con, tmp = _ib._snapshot_connect(cfg["db_path"])
                try:
                    out["active_event_rows"] = [dict(r) for r in con.execute(
                        "SELECT EventText, EventString, LocationString, "
                        "LocationText, TimeOccurred FROM ActiveEventData")]
                    try:
                        out["active_full_rows"] = [dict(r) for r in con.execute(
                            "SELECT EventText, LocationString, LocationText, "
                            "LocationId, EventCategory, State, Status, "
                            "TimeOccurred FROM ActiveFullEvent")]
                    except Exception as e:
                        out["active_full_error"] = f"{type(e).__name__}: {e}"
                finally:
                    _ib._snapshot_close(con, tmp)
            except Exception as e:
                out["active_event_error"] = f"{type(e).__name__}: {e}"
        # the log is the authoritative fault source — surface its recent located
        # fault transitions so a live fault can be confirmed there directly
        try:
            out["log_faults"] = _ib.tail_fault_lines(cfg["log_path"])
        except Exception as e:
            out["log_faults_error"] = f"{type(e).__name__}: {e}"
        return jsonify(out)

    # ---------------- ASCII call feed (dutyarea|position|location|callstate) ------

    FEED_KEYS = ["feed_enabled", "feed_mode", "feed_host", "feed_port",
                 "feed_eol", "feed_clear_text", "feed_heartbeat_enabled",
                 "feed_heartbeat_secs", "feed_heartbeat_text"]

    @app.route("/api/feed/config")
    @admin_required
    def api_feed_config():
        return jsonify(config={k: settings.get(k) for k in FEED_KEYS})

    @app.route("/api/feed/config", methods=["POST"])
    @admin_required
    def api_feed_config_save():
        data = request.get_json(force=True, silent=True) or {}
        settings.update({k: data[k] for k in FEED_KEYS if k in data})
        return jsonify(config={k: settings.get(k) for k in FEED_KEYS})

    @app.route("/api/feed/status")
    @admin_required
    def api_feed_status():
        return jsonify(status=feed.status())

    @app.route("/api/feed/test", methods=["POST"])
    @admin_required
    def api_feed_test():
        try:
            return jsonify(result=feed.send_test())
        except Exception as e:
            return jsonify(error=f"{type(e).__name__}: {e}"), 400

    # ---------------- Telligence config DB (device type / serial) ----------------

    TELDB_KEYS = ["tel_db_enabled", "tel_db_host", "tel_db_instance",
                  "tel_db_port", "tel_db_name", "tel_db_auth", "tel_db_user",
                  "tel_db_password"]

    @app.route("/api/teldb/config")
    @admin_required
    def api_teldb_config():
        cfg = {k: settings.get(k) for k in TELDB_KEYS}
        cfg["tel_db_password"] = "********" if cfg["tel_db_password"] else ""
        cfg["_drivers"] = teldb.drivers_available()
        return jsonify(config=cfg)

    @app.route("/api/teldb/config", methods=["POST"])
    @admin_required
    def api_teldb_config_set():
        data = request.get_json(force=True) or {}
        if data.get("tel_db_password") == "********":
            data.pop("tel_db_password", None)
        clean = {k: v for k, v in data.items() if k in TELDB_KEYS}
        return jsonify(applied=settings.update(clean))

    @app.route("/api/teldb/test", methods=["POST"])
    @admin_required
    def api_teldb_test():
        try:
            return jsonify(result=teldb.test_connection())
        except Exception as e:
            return jsonify(error=f"{type(e).__name__}: {e}"), 400

    @app.route("/api/teldb/tables")
    @admin_required
    def api_teldb_tables():
        try:
            return jsonify(tables=teldb.list_tables())
        except Exception as e:
            return jsonify(error=f"{type(e).__name__}: {e}"), 400

    @app.route("/api/teldb/columns")
    @admin_required
    def api_teldb_columns():
        try:
            return jsonify(columns=teldb.describe_table(request.args.get("table", "")))
        except Exception as e:
            return jsonify(error=f"{type(e).__name__}: {e}"), 400

    @app.route("/api/teldb/sample")
    @admin_required
    def api_teldb_sample():
        try:
            return jsonify(rows=teldb.sample_table(request.args.get("table", ""),
                                                   request.args.get("limit", 20)))
        except Exception as e:
            return jsonify(error=f"{type(e).__name__}: {e}"), 400

    @app.route("/api/teldb/search")
    @admin_required
    def api_teldb_search():
        try:
            return jsonify(hits=teldb.search_columns(request.args.get("term", "")))
        except Exception as e:
            return jsonify(error=f"{type(e).__name__}: {e}"), 400

    # ---------------- API: settings & email ----------------

    @app.route("/api/settings", methods=["GET"])
    @login_required
    def api_get_settings():
        vals = settings.all_settings()
        vals["gmail_app_password"] = "********" if vals["gmail_app_password"] else ""
        # webhook URLs embed a secret token — mask it
        vals["wh_url"] = "********" if vals["wh_url"] else ""
        vals["tel_db_password"] = "********" if vals["tel_db_password"] else ""
        return jsonify(settings=vals,
                       email_last_error=emailer.last_error,
                       email_last_sent=emailer.last_sent,
                       webhook_last_error=webhooks.last_error,
                       webhook_last_sent=webhooks.last_sent)

    ADMIN_ONLY_SETTINGS = {"allowed_emails", "require_2fa"}

    @app.route("/api/settings", methods=["POST"])
    @login_required
    def api_set_settings():
        data = request.get_json(force=True) or {}
        # don't overwrite stored secrets with the mask
        if data.get("gmail_app_password") == "********":
            data.pop("gmail_app_password")
        if data.get("wh_url") == "********":
            data.pop("wh_url")
        if data.get("tel_db_password") == "********":
            data.pop("tel_db_password")
        # admin-only settings can't be changed by standard users
        if session.get("role") != "admin":
            for k in ADMIN_ONLY_SETTINGS:
                data.pop(k, None)
        applied = settings.update(data)
        return jsonify(applied=applied)

    @app.route("/api/test-webhook", methods=["POST"])
    @login_required
    def api_test_webhook():
        try:
            webhooks.send_test()
            return jsonify(ok=True)
        except Exception as e:
            return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 400

    # ---------------- users (admin) ----------------

    @app.route("/api/users")
    @admin_required
    def api_users():
        return jsonify(users=database.list_users())

    @app.route("/api/users", methods=["POST"])
    @admin_required
    def api_add_user():
        from . import auth
        data = request.get_json(force=True) or {}
        pw = data.get("password") or ""
        role = "admin" if data.get("role") == "admin" else "standard"
        email = (data.get("email") or "").strip()
        # The email IS the username (matches the invite flow). No separate
        # username to choose.
        username = email
        if not email or not pw:
            return jsonify(error="email and password required"), 400
        if database.get_user_by_name(username):
            return jsonify(error="a user with that email already exists"), 400
        pwerr = auth.password_strength_error(pw)
        if pwerr:
            return jsonify(error=pwerr), 400
        # enforce the allowed-email list on new accounts
        patterns = auth.parse_email_patterns(settings.get("allowed_emails"))
        if email and not auth.email_allowed(email, patterns):
            return jsonify(error="that email is not on the allowed list"), 400
        uid = database.add_user(username, auth.hash_password(pw), role, email)
        return jsonify(id=uid)

    @app.route("/api/users/<int:uid>", methods=["PUT"])
    @admin_required
    def api_update_user(uid):
        from . import auth
        u = database.get_user(uid)
        if not u:
            return jsonify(error="not found"), 404
        data = request.get_json(force=True) or {}
        fields = {}
        if "role" in data:
            new_role = "admin" if data["role"] == "admin" else "standard"
            # don't allow removing the last admin
            if u["role"] == "admin" and new_role != "admin" and database.count_admins(uid) == 0:
                return jsonify(error="cannot demote the last administrator"), 400
            fields["role"] = new_role
        if "email" in data:
            fields["email"] = (data["email"] or "").strip()
        if "disabled" in data:
            dis = 1 if data["disabled"] else 0
            if dis and u["role"] == "admin" and database.count_admins(uid) == 0:
                return jsonify(error="cannot disable the last administrator"), 400
            fields["disabled"] = dis
        if data.get("password"):
            pwerr = auth.password_strength_error(data["password"])
            if pwerr:
                return jsonify(error=pwerr), 400
            fields["password_hash"] = auth.hash_password(data["password"])
        if data.get("reset_2fa"):
            fields["totp_enabled"] = 0
            fields["totp_secret"] = None
        database.update_user(uid, **fields)
        return jsonify(ok=True)

    @app.route("/api/users/<int:uid>", methods=["DELETE"])
    @admin_required
    def api_delete_user(uid):
        u = database.get_user(uid)
        if not u:
            return jsonify(error="not found"), 404
        if uid == session.get("uid"):
            return jsonify(error="you cannot delete your own account"), 400
        if u["role"] == "admin" and database.count_admins(uid) == 0:
            return jsonify(error="cannot delete the last administrator"), 400
        database.delete_user(uid)
        return jsonify(ok=True)

    # ---------------- invites (admin) ----------------

    @app.route("/api/invites")
    @admin_required
    def api_invites():
        return jsonify(invites=database.list_invites(pending_only=True))

    @app.route("/api/invites", methods=["POST"])
    @admin_required
    def api_create_invite():
        from . import auth
        data = request.get_json(force=True) or {}
        email = (data.get("email") or "").strip()
        role = "admin" if data.get("role") == "admin" else "standard"
        if not email or "@" not in email:
            return jsonify(error="a valid email is required"), 400
        # respect the allow-list
        patterns = auth.parse_email_patterns(settings.get("allowed_emails"))
        if not auth.email_allowed(email, patterns):
            return jsonify(error="that email is not on the allowed list"), 400
        try:
            days = max(1, min(30, int(data.get("expires_days", 7))))
        except (TypeError, ValueError):
            days = 7
        token = secrets.token_urlsafe(24)
        expires = time.time() + days * 86400
        database.add_invite(token, email, role, expires, session.get("username"))
        # build the link from the request host (works over http or https)
        base = request.host_url.rstrip("/")
        link = f"{base}/invite/{token}"
        emailed, email_error = False, None
        if settings.get("email_enabled"):
            try:
                emailer.send_invite(email, link, role, session.get("username"), days * 24)
                emailed = True
            except Exception as e:
                email_error = f"{type(e).__name__}: {e}"
        return jsonify(ok=True, link=link, emailed=emailed, email_error=email_error)

    @app.route("/api/invites/<int:iid>", methods=["DELETE"])
    @admin_required
    def api_delete_invite(iid):
        database.delete_invite(iid)
        return jsonify(ok=True)

    # ---------------- invite acceptance (public) ----------------

    @app.route("/invite/<token>", methods=["GET"])
    def invite_accept_page(token):
        inv = database.get_invite(token)
        valid = bool(inv and not inv["accepted"] and inv["expires_at"] > time.time())
        return render_template("invite.html", token=token, valid=valid,
                               email=(inv["email"] if inv else ""),
                               role=(inv["role"] if inv else ""),
                               theme=settings.get("default_theme"))

    @app.route("/api/invite/<token>/2fa-begin", methods=["POST"])
    def api_invite_2fa_begin(token):
        """Generate a pending TOTP secret for an invitee (mandatory 2FA)."""
        from . import auth
        inv = database.get_invite(token)
        if not inv or inv["accepted"] or inv["expires_at"] <= time.time():
            return jsonify(error="this invite is invalid or has expired"), 400
        secret = auth.new_totp_secret()
        session["invite_totp_" + token] = secret
        uri = auth.otpauth_uri(secret, inv["email"])
        return jsonify(secret=secret, uri=uri, qr=_qr_available())

    @app.route("/invite/<token>/qr.svg")
    def invite_qr(token):
        secret = session.get("invite_totp_" + token)
        inv = database.get_invite(token)
        if not secret or not inv:
            return "no pending setup", 404
        from . import auth
        svg = _make_qr_svg(auth.otpauth_uri(secret, inv["email"]))
        if svg is None:
            return "qr unavailable", 404
        return app.response_class(svg, mimetype="image/svg+xml")

    @app.route("/api/invite/<token>", methods=["POST"])
    def api_accept_invite(token):
        from . import auth
        inv = database.get_invite(token)
        if not inv or inv["accepted"] or inv["expires_at"] <= time.time():
            return jsonify(error="this invite is invalid or has expired"), 400
        data = request.get_json(force=True) or {}
        pw = data.get("password") or ""
        code = data.get("code") or ""
        pwerr = auth.password_strength_error(pw)
        if pwerr:
            return jsonify(error=pwerr), 400
        # username IS the invited email address
        username = (inv["email"] or "").strip()
        if not username:
            return jsonify(error="this invite has no email set"), 400
        if database.get_user_by_name(username):
            return jsonify(error="an account for this email already exists"), 400
        # re-check the allow-list at acceptance time
        patterns = auth.parse_email_patterns(settings.get("allowed_emails"))
        if not auth.email_allowed(inv["email"], patterns):
            return jsonify(error="the invited email is no longer permitted"), 400
        # 2FA is mandatory for invite sign-up
        secret = session.get("invite_totp_" + token)
        if not secret:
            return jsonify(error="please set up two-factor authentication first"), 400
        if not auth.verify_totp(secret, code):
            return jsonify(error="that authentication code is incorrect"), 400
        uid = database.add_user(username, auth.hash_password(pw), inv["role"], inv["email"])
        database.update_user(uid, totp_secret=secret, totp_enabled=1)
        database.accept_invite(token)
        session.pop("invite_totp_" + token, None)
        return jsonify(ok=True)

    # ---------------- own profile / 2FA (any logged-in user) ----------------

    @app.route("/api/profile")
    @login_required
    def api_profile():
        u = database.get_user(session.get("uid"))
        if not u:
            return jsonify(error="not found"), 404
        return jsonify(username=u["username"], role=u["role"], email=u["email"],
                       totp_enabled=bool(u["totp_enabled"]))

    @app.route("/api/profile/password", methods=["POST"])
    @login_required
    def api_change_password():
        from . import auth
        u = database.get_user(session.get("uid"))
        data = request.get_json(force=True) or {}
        if not u or not auth.verify_password(u["password_hash"], data.get("current", "")):
            return jsonify(error="current password is incorrect"), 400
        new = data.get("new") or ""
        pwerr = auth.password_strength_error(new)
        if pwerr:
            return jsonify(error=pwerr), 400
        database.update_user(u["id"], password_hash=auth.hash_password(new))
        return jsonify(ok=True)

    @app.route("/api/profile/2fa/begin", methods=["POST"])
    @login_required
    def api_2fa_begin():
        from . import auth
        u = database.get_user(session.get("uid"))
        secret = auth.new_totp_secret()
        session["pending_totp_secret"] = secret       # not saved until verified
        uri = auth.otpauth_uri(secret, u["username"])
        return jsonify(secret=secret, uri=uri, qr=_qr_available())

    @app.route("/api/profile/2fa/qr.svg")
    @login_required
    def api_2fa_qr():
        secret = session.get("pending_totp_secret")
        if not secret:
            return "no pending setup", 404
        u = database.get_user(session.get("uid"))
        from . import auth
        uri = auth.otpauth_uri(secret, u["username"])
        svg = _make_qr_svg(uri)
        if svg is None:
            return "qr unavailable", 404
        return app.response_class(svg, mimetype="image/svg+xml")

    @app.route("/api/profile/2fa/enable", methods=["POST"])
    @login_required
    def api_2fa_enable():
        from . import auth
        secret = session.get("pending_totp_secret")
        code = (request.get_json(force=True) or {}).get("code", "")
        if not secret:
            return jsonify(error="start 2FA setup first"), 400
        if not auth.verify_totp(secret, code):
            return jsonify(error="that code is incorrect — try again"), 400
        database.update_user(session["uid"], totp_secret=secret, totp_enabled=1)
        session.pop("pending_totp_secret", None)
        return jsonify(ok=True)

    @app.route("/api/profile/2fa/disable", methods=["POST"])
    @login_required
    def api_2fa_disable():
        from . import auth
        u = database.get_user(session.get("uid"))
        data = request.get_json(force=True) or {}
        if not auth.verify_password(u["password_hash"], data.get("password", "")):
            return jsonify(error="password incorrect"), 400
        database.update_user(u["id"], totp_enabled=0, totp_secret=None)
        return jsonify(ok=True)

    # ---------------- agent mode (customer-side probe) ----------------

    @app.route("/api/agent", methods=["GET"])
    @login_required
    def api_agent_get():
        from . import agent as agentmod
        c = agentmod.load_conf()
        c["site_key"] = "********" if c["site_key"] else ""  # mask secret
        return jsonify(config=c, status=agent.status())

    @app.route("/api/agent", methods=["POST"])
    @login_required
    def api_agent_set():
        from . import agent as agentmod
        data = request.get_json(force=True) or {}
        kwargs = {}
        if "hub_url" in data:
            kwargs["hub_url"] = str(data["hub_url"]).strip()
        if data.get("site_key") and data["site_key"] != "********":
            kwargs["site_key"] = str(data["site_key"]).strip()
        if "enabled" in data:
            kwargs["enabled"] = bool(data["enabled"])
        if "interval" in data:
            kwargs["interval"] = data["interval"]
        agentmod.save_conf(**kwargs)
        return jsonify(ok=True)

    @app.route("/api/agent/test", methods=["POST"])
    @login_required
    def api_agent_test():
        from . import agent as agentmod
        c = agentmod.load_conf()
        data = request.get_json(force=True) or {}
        hub = str(data.get("hub_url") or c["hub_url"]).strip()
        key = data.get("site_key")
        if not key or key == "********":
            key = c["site_key"]
        if not hub or not key:
            return jsonify(ok=False, error="hub URL and site key required"), 400
        try:
            r = agentmod.test_connection(hub, key)
            return jsonify(ok=True, **r)
        except Exception as e:
            return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 400

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


def _qr_available():
    try:
        import qrcode  # noqa: F401
        return True
    except Exception:
        return False


def _make_qr_svg(data):
    """Render a QR as an SVG string, or None if the qrcode lib is unavailable."""
    try:
        import qrcode
        import qrcode.image.svg
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=10, border=2)
        import io
        buf = io.BytesIO()
        img.save(buf)
        return buf.getvalue().decode("utf-8")
    except Exception:
        return None


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
