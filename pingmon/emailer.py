"""Gmail email engine: rolling 6/12/24h reports and device down/up alerts.

Reports include ONLY problem pings (failures and pings above the warning
threshold) plus per-device summary statistics - never a full list of good pings.
"""
import html
import logging
import queue
import smtplib
import threading
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import database, settings

log = logging.getLogger("pingmon.email")

ASCOM_RED = "#DA291C"
WARN_BG = "#fdeadd"
CRIT_BG = "#f8dcdc"
WARN_FG = "#b4501f"
CRIT_FG = "#a12626"

REPORT_KINDS = {"6": 6, "12": 12, "24": 24}


def _corr_banner(count):
    if not count:
        return ""
    return (f'<div style="margin-top:10px;background:#fff3cd;border:1px solid #e5c66a;'
            f'border-radius:4px;padding:10px 12px;color:#7a5d00;font-weight:600;">'
            f'&#9888; {count} devices reported problems within 2 minutes &mdash; '
            f'this looks like a shared/upstream issue (switch, router or link), '
            f'not a single-device fault.</div>')


def _trace_block(trace):
    if not trace:
        return ""
    return (f'<div style="margin-top:10px;"><div style="font-size:12px;color:#666;'
            f'font-weight:600;margin-bottom:4px;">Traceroute at time of failure</div>'
            f'<pre style="background:#f4f4f2;border:1px solid #e0e0e0;border-radius:4px;'
            f'padding:10px;font-size:11px;overflow-x:auto;margin:0;">'
            f'{html.escape(trace)}</pre></div>')


def _fmt_ts(ts):
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_duration(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


class Emailer:
    """Queues and sends mail on a worker thread so pinging never blocks."""

    def __init__(self):
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._sender = threading.Thread(target=self._send_loop, daemon=True,
                                        name="email-sender")
        self._scheduler = threading.Thread(target=self._schedule_loop, daemon=True,
                                           name="report-scheduler")
        self.last_error = None
        self.last_sent = None

    def start(self):
        self._sender.start()
        self._scheduler.start()

    def stop(self):
        self._stop.set()

    # ---------------- queueing ----------------

    def _enqueue(self, subject, html_body):
        self._q.put((subject, html_body))

    def _send_loop(self):
        while not self._stop.is_set():
            try:
                subject, body = self._q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._smtp_send(subject, body)
                self.last_error = None
                self.last_sent = time.time()
                log.info("email sent: %s", subject)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.error("email failed (%s): %s", subject, e)

    def _smtp_send(self, subject, html_body):
        user = settings.get("gmail_user").strip()
        password = settings.get("gmail_app_password").strip()
        recipients = [r.strip() for r in settings.get("email_recipients").split(",")
                      if r.strip()]
        if not (user and password and recipients):
            raise RuntimeError("Gmail account / app password / recipients not configured")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"Ascom Ping Monitor <{user}>"
        msg["To"] = ", ".join(recipients)
        msg.attach(MIMEText(html_body, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(user, password)
            smtp.sendmail(user, recipients, msg.as_string())

    # ---------------- alerts ----------------

    def device_down(self, device, ts, detail, trace=None, correlated=None):
        if not (settings.get("email_enabled") and settings.get("alert_down")):
            return
        if settings.in_maintenance(ts):
            log.info("maintenance window: suppressed DOWN alert for %s", device["name"])
            return
        name = html.escape(device["name"])
        host = html.escape(device["host"])
        body = _shell(f"""
          <div style="background:{CRIT_BG};border-left:4px solid {CRIT_FG};
                      padding:14px 16px;border-radius:4px;">
            <div style="font-size:16px;font-weight:700;color:{CRIT_FG};">
              &#10006; DEVICE DOWN &mdash; {name}</div>
            <div style="margin-top:6px;color:#333;">Host: <b>{host}</b><br>
              Time: {_fmt_ts(ts)}<br>Reason: {html.escape(detail)}</div>
          </div>
          {_corr_banner(correlated)}
          {_trace_block(trace)}""")
        self._enqueue(f"[Ascom Ping Monitor] DOWN: {device['name']} ({device['host']})", body)

    def device_loss(self, device, ts, loss_pct, window_min, trace=None, correlated=None):
        if not (settings.get("email_enabled") and settings.get("alert_loss")):
            return
        if settings.in_maintenance(ts):
            log.info("maintenance window: suppressed LOSS alert for %s", device["name"])
            return
        name = html.escape(device["name"])
        host = html.escape(device["host"])
        body = _shell(f"""
          <div style="background:{WARN_BG};border-left:4px solid {WARN_FG};
                      padding:14px 16px;border-radius:4px;">
            <div style="font-size:16px;font-weight:700;color:{WARN_FG};">
              &#9650; PACKET LOSS &mdash; {name}</div>
            <div style="margin-top:6px;color:#333;">Host: <b>{host}</b><br>
              Time: {_fmt_ts(ts)}<br>
              Loss: <b>{loss_pct:.1f}%</b> over the last {window_min} minutes
              (device is still responding &mdash; this is degradation, not an outage)</div>
          </div>
          {_corr_banner(correlated)}
          {_trace_block(trace)}""")
        self._enqueue(f"[Ascom Ping Monitor] PACKET LOSS {loss_pct:.0f}%: "
                      f"{device['name']} ({device['host']})", body)

    def device_recovered(self, device, ts, downtime):
        if not (settings.get("email_enabled") and settings.get("alert_recovery")):
            return
        if settings.in_maintenance(ts):
            log.info("maintenance window: suppressed RECOVERED alert for %s",
                     device["name"])
            return
        name = html.escape(device["name"])
        host = html.escape(device["host"])
        body = _shell(f"""
          <div style="background:#e2f2e2;border-left:4px solid #0a7d0a;
                      padding:14px 16px;border-radius:4px;">
            <div style="font-size:16px;font-weight:700;color:#0a7d0a;">
              &#10004; RECOVERED &mdash; {name}</div>
            <div style="margin-top:6px;color:#333;">Host: <b>{host}</b><br>
              Time: {_fmt_ts(ts)}<br>Downtime: {_fmt_duration(downtime)}</div>
          </div>""")
        self._enqueue(f"[Ascom Ping Monitor] RECOVERED: {device['name']} ({device['host']})", body)

    def send_test(self):
        body = _shell("""<p>This is a test email from your Ascom Ping Monitor.
          If you are reading this, Gmail sending is configured correctly.</p>""")
        # send synchronously so the GUI can report the real result
        self._smtp_send("[Ascom Ping Monitor] Test email", body)

    # ---------------- reports ----------------

    def _schedule_loop(self):
        # anchor rolling windows at first start so we never blast on boot
        now = time.time()
        for kind in REPORT_KINDS:
            if database.get_report_state(kind) is None:
                database.set_report_state(kind, now)
        while not self._stop.is_set():
            try:
                self._check_reports()
            except Exception:
                log.exception("report scheduler error")
            self._stop.wait(30)

    def _check_reports(self):
        if not settings.get("email_enabled"):
            return
        now = time.time()
        for kind, hours in REPORT_KINDS.items():
            if not settings.get(f"report_{kind}h"):
                continue
            last = database.get_report_state(kind) or now
            if now - last >= hours * 3600:
                self.send_report(kind, last, now)
                database.set_report_state(kind, now)

    def send_report(self, kind, start, end, force=False):
        """Build and queue a report covering [start, end]."""
        subject, body, issues = build_report(kind, start, end)
        if issues == 0 and settings.get("report_skip_clean") and not force:
            log.info("skipping clean %sh report", kind)
            return False
        self._enqueue(subject, body)
        return True


# ---------------- report builder ----------------

def build_report(kind, start, end):
    warn_ms = settings.get("warn_ms")
    crit_ms = settings.get("crit_ms")
    devices = database.list_devices()
    period = f"{_fmt_ts(start)} &rarr; {_fmt_ts(end)}"

    # per-device summary (per-device threshold overrides applied)
    jitter_warn = settings.get("jitter_warn_ms")
    summary_rows = []
    total_issues = 0
    for d in devices:
        eff_warn = d.get("warn_override") or warn_ms
        eff_crit = d.get("crit_override") or crit_ms
        s = database.device_stats(d["id"], start, end, eff_warn, eff_crit)
        sent = s["sent"] or 0
        ok = s["ok"] or 0
        fails = sent - ok
        warns = s["warns"] or 0
        crits = s["crits"] or 0
        total_issues += fails + warns + crits
        loss = (fails / sent * 100) if sent else 0
        jit = s["avg_j"]
        state_html = ('<span style="color:#0a7d0a;font-weight:700;">&#10004; OK</span>'
                      if fails == 0 and crits == 0 else
                      f'<span style="color:{CRIT_FG};font-weight:700;">&#9888; ISSUES</span>')
        summary_rows.append(f"""
          <tr>
            <td style="{_TD}">{html.escape(d['name'])}<br>
                <span style="color:#777;font-size:11px;">{html.escape(d['host'])}</span></td>
            <td style="{_TD}">{state_html}</td>
            <td style="{_TDR}">{sent}</td>
            <td style="{_TDR}{_hl(loss > 0)}">{loss:.1f}%</td>
            <td style="{_TDR}">{_ms(s['avg_l'])}</td>
            <td style="{_TDR}">{_ms(s['max_l'])}</td>
            <td style="{_TDR}{_hl(jit is not None and jit > jitter_warn, WARN_BG, WARN_FG)}">{_ms(jit)}</td>
            <td style="{_TDR}{_hl(warns, WARN_BG, WARN_FG)}">{warns}</td>
            <td style="{_TDR}{_hl(crits, CRIT_BG, CRIT_FG)}">{crits}</td>
            <td style="{_TDR}{_hl(fails, CRIT_BG, CRIT_FG)}">{fails}</td>
          </tr>""")

    # down/up/loss events in window
    events = [e for e in database.list_events(limit=500, start=start, end=end)]
    events.reverse()  # chronological
    _EV = {"up": ("&#10004; UP", "#0a7d0a"),
           "down": ("&#10006; DOWN", CRIT_FG),
           "loss": ("&#9650; LOSS", WARN_FG),
           "loss-clear": ("&#10004; LOSS ENDED", "#0a7d0a"),
           "mac-change": ("&#8646; MAC CHANGE", WARN_FG)}
    event_rows = "".join(f"""
        <tr><td style="{_TD}">{_fmt_ts(e['ts'])}</td>
            <td style="{_TD}">{html.escape(e['name'])}</td>
            <td style="{_TD}"><span style="font-weight:700;color:{_EV.get(e['type'], ('', '#333'))[1]};">
              {_EV.get(e['type'], (html.escape(e['type']), ''))[0]}</span></td>
            <td style="{_TD}">{html.escape(e['detail'] or '')}</td></tr>"""
        for e in events)

    # bad pings only (per-device thresholds applied)
    max_rows = settings.get("report_max_rows")
    bad, truncated = database.bad_pings(start, end, warn_ms, crit_ms, max_rows)
    bad_rows = []
    for p in bad:
        if not p["success"]:
            level = f'<span style="color:{CRIT_FG};font-weight:700;">&#10006; TIMEOUT</span>'
            val, style = "&mdash;", _hl(True, CRIT_BG, CRIT_FG)
        elif p["latency"] > p["eff_crit"]:
            level = f'<span style="color:{CRIT_FG};font-weight:700;">&#9632; CRITICAL</span>'
            val, style = f"{p['latency']:.1f} ms", _hl(True, CRIT_BG, CRIT_FG)
        else:
            level = f'<span style="color:{WARN_FG};font-weight:700;">&#9650; WARNING</span>'
            val, style = f"{p['latency']:.1f} ms", _hl(True, WARN_BG, WARN_FG)
        bad_rows.append(f"""
          <tr><td style="{_TD}">{_fmt_ts(p['ts'])}</td>
              <td style="{_TD}">{html.escape(p['name'])}</td>
              <td style="{_TD}">{level}</td>
              <td style="{_TDR}{style}">{val}</td></tr>""")

    issues_label = (f"{total_issues} issue{'s' if total_issues != 1 else ''}"
                    if total_issues else "all clear")

    summary_body = "".join(summary_rows) or (
        f'<tr><td colspan="10" style="{_TD}">No devices configured.</td></tr>')
    if event_rows:
        events_html = (f'<table cellspacing="0" cellpadding="0" style="{_TABLE}">'
                       f'<tr><th style="{_TH}">Time</th><th style="{_TH}">Device</th>'
                       f'<th style="{_TH}">Event</th><th style="{_TH}">Detail</th></tr>'
                       f'{event_rows}</table>')
    else:
        events_html = '<p style="color:#0a7d0a;margin:0;">&#10004; No outages.</p>'
    if bad_rows:
        bad_html = (f'<table cellspacing="0" cellpadding="0" style="{_TABLE}">'
                    f'<tr><th style="{_TH}">Time</th><th style="{_TH}">Device</th>'
                    f'<th style="{_TH}">Level</th><th style="{_THR}">Latency</th></tr>'
                    f'{"".join(bad_rows)}</table>')
    else:
        bad_html = ('<p style="color:#0a7d0a;margin:0;">&#10004; '
                    'No problem pings in this period.</p>')
    truncated_html = (f'<p style="color:{CRIT_FG};font-size:11px;">List capped at '
                      f'{max_rows} rows &mdash; see the dashboard for full history.</p>'
                      if truncated else '')
    inner = f"""
      <p style="margin:0 0 4px;color:#555;">Reporting period</p>
      <p style="margin:0 0 18px;font-weight:700;">{period}</p>

      <h3 style="margin:0 0 8px;font-size:14px;">Device summary</h3>
      <table cellspacing="0" cellpadding="0" style="{_TABLE}">
        <tr>
          <th style="{_TH}">Device</th><th style="{_TH}">Status</th>
          <th style="{_THR}">Pings</th><th style="{_THR}">Loss</th>
          <th style="{_THR}">Avg</th><th style="{_THR}">Max</th>
          <th style="{_THR}">Jitter</th>
          <th style="{_THR}">&#9650; Warn</th>
          <th style="{_THR}">&#9632; Crit</th>
          <th style="{_THR}">&#10006; Failed</th>
        </tr>
        {summary_body}
      </table>

      <h3 style="margin:22px 0 8px;font-size:14px;">Outages in this period</h3>
      {events_html}

      <h3 style="margin:22px 0 8px;font-size:14px;">Problem pings
        <span style="font-weight:400;color:#777;">(&gt;{warn_ms:.0f} ms or failed &mdash; good pings are not listed)</span></h3>
      {bad_html}
      {truncated_html}
    """
    subject = f"[Ascom Ping Monitor] {kind}h report — {issues_label}"
    return subject, _shell(inner, title=f"{kind}-hour network report"), total_issues


# ---- shared inline styles ----
_TABLE = ("width:100%;border-collapse:collapse;font-size:12px;"
          "border:1px solid #e0e0e0;")
_TH = ("text-align:left;padding:7px 9px;background:#f4f4f2;border-bottom:1px solid #e0e0e0;"
       "font-size:11px;text-transform:uppercase;letter-spacing:.4px;color:#666;")
_THR = _TH + "text-align:right;"
_TD = "padding:7px 9px;border-bottom:1px solid #eee;vertical-align:top;"
_TDR = _TD + "text-align:right;white-space:nowrap;"


def _ms(v):
    return f"{v:.1f} ms" if v is not None else "&mdash;"


def _hl(cond, bg=CRIT_BG, fg=CRIT_FG):
    return f"background:{bg};color:{fg};font-weight:700;" if cond else ""


def _shell(inner, title="Ascom Ping Monitor"):
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:0;background:#f0f0ee;">
<div style="max-width:760px;margin:0 auto;padding:24px 12px;
            font-family:-apple-system,'Segoe UI',Arial,sans-serif;color:#222;">
  <div style="background:#fff;border-radius:8px;overflow:hidden;
              border:1px solid #e2e2df;">
    <div style="padding:18px 22px;border-bottom:3px solid {ASCOM_RED};">
      <span style="font-size:26px;font-weight:800;letter-spacing:-1px;
                   color:{ASCOM_RED};">ascom</span>
      <span style="font-size:13px;color:#888;margin-left:10px;">Ping Monitor</span>
      <div style="font-size:15px;font-weight:600;margin-top:6px;">{title}</div>
    </div>
    <div style="padding:20px 22px;">{inner}</div>
    <div style="padding:12px 22px;background:#fafaf8;border-top:1px solid #eee;
                font-size:11px;color:#999;">
      Generated {_fmt_ts(time.time())} &middot; Ascom Ping Monitor</div>
  </div>
</div></body></html>"""
