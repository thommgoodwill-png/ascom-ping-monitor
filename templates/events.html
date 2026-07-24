{% extends "base.html" %}
{% block content %}
<h1 class="page-title">Events</h1>
<div class="card">
  <h2>Device outages &amp; recoveries</h2>
  <p class="card-sub">A device is marked down after the configured number of consecutive failed pings (see Settings).</p>
  <table class="list" id="ev-table">
    <thead>
      <tr><th>Time</th><th>Device</th><th>Event</th><th>Detail</th></tr>
    </thead>
    <tbody></tbody>
  </table>
  <p class="muted" id="ev-empty" style="display:none;">No events recorded — everything has stayed up.</p>
</div>
{% endblock %}

{% block scripts %}
<script>
(function () {
  "use strict";
  async function load() {
    const data = await api("/api/events?limit=300");
    const tb = document.querySelector("#ev-table tbody");
    tb.innerHTML = "";
    document.getElementById("ev-empty").style.display =
      data.events.length ? "none" : "block";
    for (const e of data.events) {
      const tr = document.createElement("tr");
      const tdT = document.createElement("td");
      tdT.className = "mono";
      tdT.textContent = new Date(e.ts * 1000).toLocaleString();
      tr.appendChild(tdT);
      const tdD = document.createElement("td");
      tdD.style.fontWeight = "600";
      tdD.textContent = e.name;
      tr.appendChild(tdD);
      const tdE = document.createElement("td");
      const KINDS = {
        up: ["up", "✔ Recovered"],
        down: ["down", "✖ Down"],
        loss: ["warn", "▲ Packet loss"],
        "loss-clear": ["up", "✔ Loss ended"],
        "mac-change": ["warn", "⇄ MAC change"],
        "check-down": ["warn", "▲ Service check failed"],
        "check-up": ["up", "✔ Service recovered"],
      };
      const [cls, label] = KINDS[e.type] || ["unknown", e.type];
      const pill = document.createElement("span");
      pill.className = "pill " + cls;
      pill.textContent = label;
      tdE.appendChild(pill);
      tr.appendChild(tdE);
      const tdX = document.createElement("td");
      tdX.className = "muted";
      tdX.textContent = e.detail || "";
      if (e.trace) {
        const det = document.createElement("details");
        det.style.marginTop = "4px";
        const sum = document.createElement("summary");
        sum.textContent = "traceroute at time of failure";
        sum.style.cssText = "cursor:pointer;font-size:12px;color:var(--s1);";
        const pre = document.createElement("pre");
        pre.textContent = e.trace;
        pre.style.cssText = "font-size:11px;background:var(--surface-2);padding:8px;" +
                            "border-radius:6px;overflow-x:auto;margin:6px 0 0;";
        det.appendChild(sum);
        det.appendChild(pre);
        tdX.appendChild(det);
      }
      tr.appendChild(tdX);
      tb.appendChild(tr);
    }
  }
  load();
  setInterval(load, 15000);
})();
</script>
{% endblock %}
