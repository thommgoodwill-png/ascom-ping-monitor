"""Outbound webhook alerts for Microsoft Teams, Discord, Slack, or a generic
JSON endpoint. Fires the same events as the email engine (device down/up,
packet loss, service-check failures, new/rogue devices). Sent on a background
thread so monitoring never blocks."""
import json
import logging
import queue
import threading
import time
import urllib.request

from . import settings

log = logging.getLogger("pingmon.webhook")

# severity -> colour (hex int for Discord, hex str for Teams/Slack)
SEV = {
    "critical": (0xD03B3B, "#D03B3B", "danger"),
    "warning":  (0xEC835A, "#EC835A", "warning"),
    "good":     (0x0CA30C, "#0CA30C", "good"),
    "info":     (0x2A78D6, "#2A78D6", "#2A78D6"),
}


class Webhooks:
    def __init__(self):
        self._q = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._loop, daemon=True,
                                        name="webhook-sender")
        self.last_error = None
        self.last_sent = None

    def start(self):
        self._worker.start()

    def stop(self):
        self._stop.set()

    # ---- event entry points (mirror the emailer) ----

    def device_down(self, device, ts, detail, correlated=None):
        if settings.get("wh_down"):
            self._event("critical", "✖ DEVICE DOWN", device["name"], device["host"],
                        detail, correlated)

    def device_recovered(self, device, ts, downtime):
        if settings.get("wh_recovery"):
            from .monitor import _fmt_duration
            self._event("good", "✔ RECOVERED", device["name"], device["host"],
                        f"was down for {_fmt_duration(downtime)}")

    def device_loss(self, device, ts, loss_pct, window_min, correlated=None):
        if settings.get("wh_loss"):
            self._event("warning", "▲ PACKET LOSS", device["name"], device["host"],
                        f"{loss_pct:.1f}% loss over {window_min} min (still up)",
                        correlated)

    def check_failed(self, device, ts, label, detail):
        if settings.get("wh_check"):
            self._event("warning", f"▲ {label} CHECK FAILED", device["name"],
                        device["host"], detail)

    def rogue_device(self, dev, ts):
        if settings.get("wh_rogue"):
            v = f" ({dev['vendor']})" if dev.get("vendor") else ""
            self._event("warning", "⚠ NEW DEVICE ON NETWORK",
                        dev.get("ip", "?"), dev.get("mac", "?"),
                        f"new MAC seen{v} — acknowledge on the Tools page if expected")

    def send_test(self):
        """Synchronous test send so the GUI can report the real result."""
        url = settings.get("wh_url").strip()
        if not url:
            raise RuntimeError("no webhook URL configured")
        payload = _format(settings.get("wh_platform"), "info",
                          "Ascom Network Monitor", "test",
                          "This is a test webhook. If you can read this, it works.",
                          None)
        _post(url, payload)

    # ---- internals ----

    def _event(self, sev, title, name, host, detail, correlated=None):
        if not settings.get("webhooks_enabled"):
            return
        if settings.in_maintenance(time.time()):
            log.info("maintenance window: suppressed webhook '%s'", title)
            return
        url = settings.get("wh_url").strip()
        if not url:
            return
        self._q.put((settings.get("wh_platform"), url, sev, title, name, host,
                     detail, correlated))

    def _loop(self):
        while not self._stop.is_set():
            try:
                platform, url, sev, title, name, host, detail, corr = \
                    self._q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                payload = _format(platform, sev, title, name, detail, host, corr)
                _post(url, payload)
                self.last_error = None
                self.last_sent = time.time()
                log.info("webhook sent: %s %s", title, name)
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {e}"
                log.error("webhook failed (%s): %s", title, e)


def _fields(name, host, detail, correlated):
    lines = [f"**Device:** {name}", f"**Host:** {host}", f"**Detail:** {detail}"]
    if correlated:
        lines.append(f"**⚠ {correlated} devices affected within 2 min — "
                     f"likely upstream/shared issue**")
    return lines


def _format(platform, sev, title, name, detail, host=None, correlated=None):
    disc_col, hex_col, slack_col = SEV.get(sev, SEV["info"])
    text_lines = _fields(name, host, detail, correlated) if host is not None else [detail]
    body_md = "\n".join(text_lines)
    plain = f"{title} — {name}" + (f" ({host})" if host else "") + f"\n{detail}"

    if platform == "discord":
        return {
            "username": "Ascom Network Monitor",
            "embeds": [{
                "title": title,
                "description": body_md,
                "color": disc_col,
                "footer": {"text": "Ascom Network Monitor"},
            }],
        }

    if platform == "slack":
        return {
            "attachments": [{
                "color": hex_col,
                "title": title,
                "text": body_md.replace("**", "*"),   # slack uses single *
                "footer": "Ascom Network Monitor",
            }],
        }

    if platform == "teams":
        # MessageCard (works with the classic Incoming Webhook connector)
        facts = []
        if host is not None:
            facts = [{"name": "Device", "value": name},
                     {"name": "Host", "value": host},
                     {"name": "Detail", "value": detail}]
            if correlated:
                facts.append({"name": "Correlation",
                              "value": f"{correlated} devices affected within 2 min"})
        return {
            "@type": "MessageCard",
            "@context": "http://schema.org/extensions",
            "themeColor": hex_col.lstrip("#"),
            "summary": plain,
            "title": title,
            "sections": [{
                "activityTitle": name if host is not None else "",
                "facts": facts,
                "text": "" if host is not None else detail,
            }],
        }

    # generic: plain, predictable JSON for anything else (n8n, Zapier, custom)
    return {
        "source": "ascom-network-monitor",
        "severity": sev,
        "title": title,
        "device": name,
        "host": host,
        "detail": detail,
        "correlated": correlated,
        "text": plain,
        "ts": int(time.time()),
    }


def _post(url, payload, timeout=15):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "AscomNetworkMonitor"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        code = resp.getcode()
        # Slack/Discord/Teams return 200/204 on success
        if code >= 300:
            raise RuntimeError(f"HTTP {code}")
        return code
