{% extends "base.html" %}
{% block content %}
<h1 class="page-title">Settings</h1>

<div class="card">
  <h2>Monitoring</h2>
  <p class="card-sub">Global ping engine options. Everything can be switched on or off.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Monitoring enabled</div>
      <div class="t-desc">Master switch — turns all pinging on or off. History is kept.</div></div>
    <label class="switch"><input type="checkbox" data-key="monitoring_enabled"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">Ping interval (seconds)</label>
      <input type="number" data-key="ping_interval" min="0.2" max="60" step="0.1">
      <div class="f-help">0.2 – 60 s between pings. Individual devices can override this.</div>
    </div>
    <div>
      <label class="f-label">Ping timeout (seconds)</label>
      <input type="number" data-key="ping_timeout" min="1" max="10" step="1">
      <div class="f-help">How long to wait for a reply before counting a failure.</div>
    </div>
    <div>
      <label class="f-label">Failures before "down"</label>
      <input type="number" data-key="fail_threshold" min="1" max="20" step="1">
      <div class="f-help">Consecutive failed pings before a device is flagged down.</div>
    </div>
    <div>
      <label class="f-label">History retention (days)</label>
      <input type="number" data-key="retention_days" min="1" max="365" step="1">
      <div class="f-help">Older ping data is deleted automatically.</div>
    </div>
  </div>
  <div class="f-row">
    <div>
      <label class="f-label">Warning threshold (ms) <span style="color:var(--warn-text)">▲ orange</span></label>
      <input type="number" data-key="warn_ms" min="1" step="1">
      <div class="f-help">Pings above this are flagged as warnings. Devices can override this individually.</div>
    </div>
    <div>
      <label class="f-label">Critical threshold (ms) <span style="color:var(--crit-text)">■ red</span></label>
      <input type="number" data-key="crit_ms" min="1" step="1">
      <div class="f-help">Pings above this are flagged as critical. Devices can override this individually.</div>
    </div>
    <div>
      <label class="f-label">Jitter warning (ms)</label>
      <input type="number" data-key="jitter_warn_ms" min="1" step="1">
      <div class="f-help">Average jitter above this is flagged — high jitter hurts VoIP/real-time gear even when averages look fine.</div>
    </div>
    <div>
      <label class="f-label">Ping payload size (bytes)</label>
      <input type="number" data-key="ping_size" min="16" max="1472" step="1">
      <div class="f-help">Default 56. Set ~1400 to expose MTU/fragmentation faults that small pings sail through.</div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Problem detection</h2>
  <p class="card-sub">Catches degradation that never takes a device fully down.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Packet-loss alerts</div>
      <div class="t-desc">Email when a device that is still up loses more than the threshold below over the sliding window — the classic sign of a flaky cable, duplex mismatch or saturated link.</div></div>
    <label class="switch"><input type="checkbox" data-key="alert_loss"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Traceroute on failure</div>
      <div class="t-desc">Automatically run a traceroute the moment a device goes down or lossy, store it with the event and include it in the alert email — shows <b>where</b> the path broke.</div></div>
    <label class="switch"><input type="checkbox" data-key="traceroute_on_fail"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Service-check alerts</div>
      <div class="t-desc">Email when a device's TCP port, HTTP(S) or DNS check fails while the host still pings — a specific service is down, not the whole box.</div></div>
    <label class="switch"><input type="checkbox" data-key="alert_check"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">TLS certificate warning (days)</label>
      <input type="number" data-key="cert_warn_days" min="1" max="365" step="1">
      <div class="f-help">Flag HTTPS health checks when the certificate expires within this many days.</div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Device discovery &amp; rogue alerts</h2>
  <p class="card-sub">Periodically sweeps the subnet, remembers every MAC, and emails
    when a <b>new</b> device appears (after the first baseline scan). Only sees the
    monitor's own subnet.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Background subnet scanning</div>
      <div class="t-desc">Runs discovery on a schedule to build and maintain the known-device list.</div></div>
    <label class="switch"><input type="checkbox" data-key="rogue_scan_enabled"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">New-device (rogue) alerts</div>
      <div class="t-desc">Email when a MAC not seen before appears on the subnet.</div></div>
    <label class="switch"><input type="checkbox" data-key="alert_rogue"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">Subnet to scan</label>
      <input type="text" data-key="rogue_scan_subnet" placeholder="auto-detect">
      <div class="f-help">CIDR e.g. 192.168.0.0/24. Blank = auto-detect the monitor's own /24.</div>
    </div>
    <div>
      <label class="f-label">Scan interval (minutes)</label>
      <input type="number" data-key="rogue_scan_interval_min" min="5" max="1440" step="1">
    </div>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">Loss threshold (%)</label>
      <input type="number" data-key="loss_threshold_pct" min="1" max="100" step="1">
    </div>
    <div>
      <label class="f-label">Loss window (minutes)</label>
      <input type="number" data-key="loss_window_min" min="2" max="120" step="1">
    </div>
    <div>
      <label class="f-label">Correlation threshold (devices)</label>
      <input type="number" data-key="correlate_min_devices" min="2" max="50" step="1">
      <div class="f-help">If this many devices fail within 2 minutes, events and emails are flagged as a likely upstream/shared issue.</div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Maintenance window</h2>
  <p class="card-sub">A daily quiet period — pings keep logging, but <b>all alert emails are suppressed</b> so backups and reboots don't page anyone. The wallboard shows a 🔧 badge while active.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Maintenance window enabled</div>
      <div class="t-desc">Applies every day between the times below (may wrap past midnight, e.g. 23:00 → 02:00).</div></div>
    <label class="switch"><input type="checkbox" data-key="maint_enabled"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">Start (HH:MM)</label>
      <input type="time" data-key="maint_start">
    </div>
    <div>
      <label class="f-label">End (HH:MM)</label>
      <input type="time" data-key="maint_end">
    </div>
  </div>
</div>

<div class="card">
  <h2>Email — Gmail</h2>
  <p class="card-sub">Uses your Gmail account over SMTP. You need a Google <b>app password</b>
    (Google Account → Security → 2-Step Verification → App passwords) — your normal password will not work.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Email enabled</div>
      <div class="t-desc">Master switch for ALL outgoing email (reports and alerts).</div></div>
    <label class="switch"><input type="checkbox" data-key="email_enabled"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">Gmail address</label>
      <input type="email" data-key="gmail_user" placeholder="you@gmail.com">
    </div>
    <div>
      <label class="f-label">Gmail app password</label>
      <input type="password" data-key="gmail_app_password" placeholder="16-character app password">
    </div>
    <div>
      <label class="f-label">Recipients</label>
      <input type="text" data-key="email_recipients" placeholder="noc@site.com, you@site.com">
      <div class="f-help">Comma-separated list.</div>
    </div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;">
    <button class="btn" id="test-email-btn">Send test email</button>
    <span class="muted" id="email-status" style="align-self:center;"></span>
  </div>
</div>

<div class="card">
  <h2>Central hub — agent mode</h2>
  <p class="card-sub">Turn this installation into a <b>remote agent</b> for a central hub.
    It keeps monitoring locally (everything on this dashboard still works) and also
    reports its ping data to a site on the hub. Get the hub URL and the site's API key
    from the hub's Customers → site page.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Report to central hub</div>
      <div class="t-desc">Pull this site's device list from the hub and push results back.</div></div>
    <label class="switch"><input type="checkbox" id="agent-enabled"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div style="grid-column:span 2;">
      <label class="f-label">Hub URL</label>
      <input type="text" id="agent-hub" placeholder="https://hub.example.com:8080  (or http://192.168.x.x:8080)">
      <div class="f-help">The address of your central Network Monitor. Use HTTPS if it's internet-facing.</div>
    </div>
    <div>
      <label class="f-label">Site API key</label>
      <input type="password" id="agent-key" placeholder="ste_…">
      <div class="f-help">From the site page on the hub. Stored securely (shown masked).</div>
    </div>
    <div>
      <label class="f-label">Push interval (seconds)</label>
      <input type="number" id="agent-interval" min="10" max="600" step="5" value="30">
    </div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;">
    <button class="btn" id="agent-test">Test connection</button>
    <span class="muted" id="agent-status" style="align-self:center;"></span>
  </div>
</div>

<div class="card">
  <h2>Webhooks — Teams / Discord / Slack</h2>
  <p class="card-sub">Posts alerts to a chat channel. Paste an <b>Incoming Webhook</b>
    URL from Microsoft Teams, Discord, Slack, or any endpoint that accepts JSON.
    Fires the same events as email; the maintenance window mutes these too.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Webhooks enabled</div>
      <div class="t-desc">Master switch for all outgoing webhooks.</div></div>
    <label class="switch"><input type="checkbox" data-key="webhooks_enabled"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">Platform</label>
      <select data-key="wh_platform">
        <option value="teams">Microsoft Teams</option>
        <option value="discord">Discord</option>
        <option value="slack">Slack</option>
        <option value="generic">Generic JSON</option>
      </select>
      <div class="f-help">Formats the message for the chosen service.</div>
    </div>
    <div style="grid-column:span 2;">
      <label class="f-label">Webhook URL</label>
      <input type="password" data-key="wh_url" placeholder="https://…webhook…">
      <div class="f-help">The Incoming Webhook URL. Kept secret (shown masked once saved).</div>
    </div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;">
    <button class="btn" id="test-webhook-btn">Send test webhook</button>
    <span class="muted" id="webhook-status" style="align-self:center;"></span>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Device-down</div></div>
    <label class="switch"><input type="checkbox" data-key="wh_down"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Recovery</div></div>
    <label class="switch"><input type="checkbox" data-key="wh_recovery"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Packet loss</div></div>
    <label class="switch"><input type="checkbox" data-key="wh_loss"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Service-check failures</div></div>
    <label class="switch"><input type="checkbox" data-key="wh_check"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">New / rogue device</div></div>
    <label class="switch"><input type="checkbox" data-key="wh_rogue"><span class="track"></span></label>
  </div>
  <p class="f-help" style="margin-top:10px;">
    <b>Where to get the URL:</b> Teams → channel ⋯ → Connectors → Incoming Webhook.
    Discord → channel settings → Integrations → Webhooks → New. Slack → add the
    Incoming Webhooks app and pick a channel.</p>
</div>

<div class="card">
  <h2>Scheduled reports</h2>
  <p class="card-sub">Rolling reports — each covers the period since it last went out.
    Reports list <b>only problem pings</b> (failures and pings above the warning threshold), never the full log of good pings.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">6-hour report</div>
      <div class="t-desc">Summary + problem pings, every 6 hours.</div></div>
    <label class="switch"><input type="checkbox" data-key="report_6h"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">12-hour report</div>
      <div class="t-desc">Summary + problem pings, every 12 hours.</div></div>
    <label class="switch"><input type="checkbox" data-key="report_12h"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">24-hour report</div>
      <div class="t-desc">Summary + problem pings, every 24 hours.</div></div>
    <label class="switch"><input type="checkbox" data-key="report_24h"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Skip clean reports</div>
      <div class="t-desc">Don't send a report at all if there were no warnings, criticals or failures in the period.</div></div>
    <label class="switch"><input type="checkbox" data-key="report_skip_clean"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">Max problem rows per report</label>
      <input type="number" data-key="report_max_rows" min="10" max="2000" step="10">
      <div class="f-help">Caps the size of report emails on very bad days.</div>
    </div>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;">
    <button class="btn small" data-report="6">Send 6 h report now</button>
    <button class="btn small" data-report="12">Send 12 h report now</button>
    <button class="btn small" data-report="24">Send 24 h report now</button>
  </div>
</div>

<div class="card">
  <h2>Failure alerts</h2>
  <p class="card-sub">Immediate emails when a device changes state.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Device-down alerts</div>
      <div class="t-desc">Email the moment a device is flagged down.</div></div>
    <label class="switch"><input type="checkbox" data-key="alert_down"><span class="track"></span></label>
  </div>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Recovery alerts</div>
      <div class="t-desc">Email when a down device starts answering again, including how long it was down.</div></div>
    <label class="switch"><input type="checkbox" data-key="alert_recovery"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">Alert cooldown (minutes)</label>
      <input type="number" data-key="alert_cooldown_min" min="0" max="1440" step="1">
      <div class="f-help">Minimum gap between repeated down-alerts for the same device (0 = alert every time).</div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Packet capture</h2>
  <p class="card-sub">Runs <code>tcpdump</code> on demand and saves a Wireshark-compatible
    <code>.pcap</code>. Off by default — it's a privileged, sensitive operation.
    Only sees traffic reaching the monitor unless a switch SPAN/mirror port feeds it.</p>
  <div class="toggle-line">
    <div class="t-text"><div class="t-title">Packet capture enabled</div>
      <div class="t-desc">Turns on the Capture page and the tcpdump engine.</div></div>
    <label class="switch"><input type="checkbox" data-key="capture_enabled"><span class="track"></span></label>
  </div>
  <div class="f-row" style="margin-top:14px;">
    <div>
      <label class="f-label">Max capture length (seconds)</label>
      <input type="number" data-key="capture_max_seconds" min="1" max="300" step="1">
      <div class="f-help">Upper bound users can request (hard cap 300 s).</div>
    </div>
    <div>
      <label class="f-label">Max packets per capture</label>
      <input type="number" data-key="capture_max_packets" min="10" max="100000" step="10">
    </div>
  </div>
</div>

<div class="card">
  <h2>Interface</h2>
  <div class="f-row">
    <div>
      <label class="f-label">Default theme</label>
      <select data-key="default_theme">
        <option value="auto">Auto (follow system)</option>
        <option value="light">Light</option>
        <option value="dark">Dark</option>
      </select>
      <div class="f-help">Users can still toggle per-browser with the ☾ button.</div>
    </div>
    <div>
      <label class="f-label">Dashboard auto-refresh (seconds)</label>
      <input type="number" data-key="refresh_seconds" min="0" max="3600" step="5">
      <div class="f-help">0 turns auto-refresh off.</div>
    </div>
    <div>
      <label class="f-label">Wallboard refresh (seconds)</label>
      <input type="number" data-key="wallboard_refresh" min="2" max="300" step="1">
      <div class="f-help">Refresh rate of the full-screen ▦ wallboard view.</div>
    </div>
  </div>
</div>

<div style="display:flex;gap:10px;align-items:center;position:sticky;bottom:14px;background:var(--surface);border:1px solid var(--border);border-radius:10px;box-shadow:var(--shadow);padding:10px 14px;width:fit-content;">
  <button class="btn primary" id="save-btn">Save settings</button>
  <span class="muted" id="save-note"></span>
</div>
{% endblock %}

{% block scripts %}
<script>
(function () {
  "use strict";
  const fields = document.querySelectorAll("[data-key]");

  async function load() {
    const data = await api("/api/settings");
    const s = data.settings;
    for (const el of fields) {
      const k = el.dataset.key;
      if (el.type === "checkbox") el.checked = !!s[k];
      else el.value = s[k];
    }
    const es = document.getElementById("email-status");
    if (data.email_last_error) {
      es.textContent = "Last email error: " + data.email_last_error;
      es.style.color = "var(--crit-text)";
    } else if (data.email_last_sent) {
      es.textContent = "Last email sent " + new Date(data.email_last_sent * 1000).toLocaleString();
      es.style.color = "var(--good-text)";
    }
    // agent-mode config (separate store)
    try {
      const ag = await api("/api/agent");
      document.getElementById("agent-enabled").checked = !!ag.config.enabled;
      document.getElementById("agent-hub").value = ag.config.hub_url || "";
      document.getElementById("agent-key").value = ag.config.site_key || "";
      document.getElementById("agent-interval").value = ag.config.interval || 30;
      const as = document.getElementById("agent-status");
      if (ag.status.last_error) { as.textContent = "Last error: " + ag.status.last_error; as.style.color = "var(--crit-text)"; }
      else if (ag.status.last_push) { as.textContent = "Last push " + new Date(ag.status.last_push*1000).toLocaleString() + " (" + ag.status.last_pushed_count + " pings)"; as.style.color = "var(--good-text)"; }
    } catch (e) {}
    const ws = document.getElementById("webhook-status");
    if (ws) {
      if (data.webhook_last_error) {
        ws.textContent = "Last webhook error: " + data.webhook_last_error;
        ws.style.color = "var(--crit-text)";
      } else if (data.webhook_last_sent) {
        ws.textContent = "Last webhook sent " + new Date(data.webhook_last_sent * 1000).toLocaleString();
        ws.style.color = "var(--good-text)";
      }
    }
  }

  document.getElementById("save-btn").addEventListener("click", async () => {
    const payload = {};
    for (const el of fields) {
      const k = el.dataset.key;
      payload[k] = el.type === "checkbox" ? el.checked : el.value;
    }
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
      // save agent-mode config too
      await api("/api/agent", { method: "POST", body: JSON.stringify({
        enabled: document.getElementById("agent-enabled").checked,
        hub_url: document.getElementById("agent-hub").value.trim(),
        site_key: document.getElementById("agent-key").value.trim(),
        interval: parseInt(document.getElementById("agent-interval").value, 10) || 30,
      }) });
      toast("Settings saved");
      document.getElementById("save-note").textContent =
        "Saved " + new Date().toLocaleTimeString();
      load();
    } catch (e) { toast("Save failed: " + e.message, true); }
  });

  document.getElementById("agent-test").addEventListener("click", async ev => {
    const btn = ev.target; btn.disabled = true; btn.textContent = "Testing…";
    const as = document.getElementById("agent-status");
    try {
      const r = await api("/api/agent/test", { method: "POST", body: JSON.stringify({
        hub_url: document.getElementById("agent-hub").value.trim(),
        site_key: document.getElementById("agent-key").value.trim() }) });
      as.textContent = "✓ Connected to site “" + (r.site ? r.site.name : "?") + "” — " + r.devices + " device(s) configured";
      as.style.color = "var(--good-text)";
      toast("Hub connection OK");
    } catch (e) { as.textContent = "✗ " + e.message; as.style.color = "var(--crit-text)"; toast("Test failed: " + e.message, true); }
    btn.disabled = false; btn.textContent = "Test connection";
  });

  document.getElementById("test-email-btn").addEventListener("click", async ev => {
    const btn = ev.target;
    btn.disabled = true; btn.textContent = "Sending…";
    try {
      await api("/api/test-email", { method: "POST", body: "{}" });
      toast("Test email sent — check your inbox");
    } catch (e) { toast("Test failed: " + e.message, true); }
    btn.disabled = false; btn.textContent = "Send test email";
  });

  document.getElementById("test-webhook-btn").addEventListener("click", async ev => {
    const btn = ev.target;
    // save first so the URL/platform are current, then fire the test
    const payload = {};
    for (const el of fields) {
      const k = el.dataset.key;
      payload[k] = el.type === "checkbox" ? el.checked : el.value;
    }
    btn.disabled = true; btn.textContent = "Sending…";
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify(payload) });
      await api("/api/test-webhook", { method: "POST", body: "{}" });
      toast("Test webhook sent — check your channel");
      load();
    } catch (e) { toast("Test failed: " + e.message, true); }
    btn.disabled = false; btn.textContent = "Send test webhook";
  });

  document.querySelectorAll("[data-report]").forEach(btn => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api("/api/send-report", { method: "POST",
          body: JSON.stringify({ kind: btn.dataset.report }) });
        toast(btn.dataset.report + " h report queued — sending in the background");
      } catch (e) { toast(e.message, true); }
      btn.disabled = false;
    });
  });

  load();
})();
</script>
{% endblock %}
