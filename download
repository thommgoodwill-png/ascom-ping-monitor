{% extends "base.html" %}
{% block content %}
<h1 class="page-title">Network tools</h1>

<div class="card">
  <h2>Subnet discovery</h2>
  <p class="card-sub">Ping-sweeps a subnet and lists everything alive, with MAC and
    vendor. Every device found is added to <b>Known devices</b> below (building the
    baseline); a manual scan never emails — only background scanning (Settings) sends
    rogue-device alerts.</p>
  <div class="filter-row" style="margin-bottom:12px;">
    <input type="text" id="disc-subnet" style="width:220px;" placeholder="192.168.0.0/24">
    <button class="btn primary" id="disc-btn">Scan</button>
    <span class="muted" id="disc-status"></span>
  </div>
  <table class="list" id="disc-table" style="display:none;">
    <thead><tr><th>IP</th><th>MAC</th><th>Vendor</th><th>State</th><th class="right">Action</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div class="card">
  <h2>Path analysis (MTR-style)</h2>
  <p class="card-sub">Repeated hop-by-hop probing — shows <b>which hop</b> is losing
    packets or adding latency. Turns "the internet is slow" into a specific hop.</p>
  <div class="filter-row" style="margin-bottom:12px;">
    <input type="text" id="path-host" style="width:220px;" placeholder="8.8.8.8 or host">
    <select id="path-cycles" style="width:auto;">
      <option value="3">3 cycles</option>
      <option value="5" selected>5 cycles</option>
      <option value="10">10 cycles</option>
    </select>
    <button class="btn primary" id="path-btn">Trace</button>
    <span class="muted" id="path-status"></span>
  </div>
  <table class="list" id="path-table" style="display:none;">
    <thead><tr><th class="num">Hop</th><th>Host</th><th class="num">Loss</th>
      <th class="num">Sent</th><th class="num">Avg</th><th class="num">Best</th><th class="num">Worst</th></tr></thead>
    <tbody></tbody>
  </table>
</div>

<div class="card">
  <h2>SNMP query <span class="muted" id="snmp-avail"></span></h2>
  <p class="card-sub">Reads standard system OIDs from managed switches/APs/UPS.
    The device must have SNMP enabled and the right community string.</p>
  <div class="filter-row" style="margin-bottom:12px;">
    <input type="text" id="snmp-host" style="width:180px;" placeholder="10.0.0.1">
    <input type="text" id="snmp-comm" style="width:140px;" placeholder="community (public)">
    <select id="snmp-ver" style="width:auto;">
      <option value="2c" selected>v2c</option>
      <option value="1">v1</option>
    </select>
    <button class="btn primary" id="snmp-btn">Query</button>
    <span class="muted" id="snmp-status"></span>
  </div>
  <table class="list" id="snmp-table" style="display:none;"><tbody></tbody></table>
</div>

<div class="card">
  <h2>Throughput test (iperf3) <span class="muted" id="iperf-avail"></span></h2>
  <p class="card-sub">Measures real bandwidth to a host running <code>iperf3 -s</code>.
    Proves or disproves "the link is slow".</p>
  <div class="filter-row" style="margin-bottom:12px;">
    <input type="text" id="iperf-host" style="width:180px;" placeholder="iperf3 server IP">
    <select id="iperf-secs" style="width:auto;">
      <option value="5" selected>5 s</option><option value="10">10 s</option>
    </select>
    <label style="display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--ink-2);">
      <input type="checkbox" id="iperf-rev"> reverse (download)
    </label>
    <button class="btn primary" id="iperf-btn">Run</button>
    <span class="muted" id="iperf-status"></span>
  </div>
  <div id="iperf-result" style="display:none;" class="stats"></div>
</div>

<div class="card">
  <h2>Known devices</h2>
  <p class="card-sub">Everything ever seen on the subnet. Acknowledge expected devices
    so only genuinely new ones trigger rogue alerts.</p>
  <table class="list" id="known-table">
    <thead><tr><th>IP</th><th>MAC</th><th>Vendor</th><th>First seen</th><th>Last seen</th><th class="right">Status</th></tr></thead>
    <tbody></tbody>
  </table>
  <p class="muted" id="known-empty" style="display:none;">Nothing yet — run a scan above or enable rogue scanning in Settings.</p>
</div>
{% endblock %}

{% block scripts %}
<script>
(function () {
  "use strict";

  function busy(id, on, msg) {
    document.getElementById(id).textContent = msg || "";
  }
  function mkCell(txt, cls) {
    const td = document.createElement("td"); td.textContent = txt;
    if (cls) td.className = cls; return td;
  }
  function lossCls(p) { return p >= 20 ? "v-crit" : p > 0 ? "v-warn" : "v-good"; }

  // ---- discovery ----
  document.getElementById("disc-btn").addEventListener("click", async () => {
    const subnet = document.getElementById("disc-subnet").value.trim();
    busy("disc-status", true, "Scanning… (can take up to a minute)");
    try {
      const d = await api("/api/tools/discover", { method: "POST",
        body: JSON.stringify({ subnet }) });
      const tb = document.querySelector("#disc-table tbody");
      tb.innerHTML = "";
      for (const x of d.devices) {
        const tr = document.createElement("tr");
        tr.appendChild(mkCell(x.ip, "mono"));
        tr.appendChild(mkCell(x.mac || "—", "mono"));
        tr.appendChild(mkCell(x.vendor || "—"));
        const state = x.monitored ? "monitored" : x.known ? "known" : "NEW";
        tr.appendChild(mkCell(state, x.monitored ? "v-good" : x.known ? "" : "v-warn"));
        const td = document.createElement("td"); td.className = "right";
        if (!x.monitored) {
          const b = document.createElement("button");
          b.className = "btn small"; b.textContent = "+ Monitor";
          b.addEventListener("click", async () => {
            try {
              await api("/api/devices", { method: "POST",
                body: JSON.stringify({ name: x.vendor ? x.vendor + " " + x.ip : x.ip,
                                       host: x.ip }) });
              toast("Added " + x.ip + " to monitoring");
              b.disabled = true; b.textContent = "added";
            } catch (e) { toast(e.message, true); }
          });
          td.appendChild(b);
        }
        tr.appendChild(td);
        tb.appendChild(tr);
      }
      document.getElementById("disc-table").style.display = d.devices.length ? "" : "none";
      busy("disc-status", false, d.devices.length + " hosts alive on " + d.subnet);
      loadKnown();
    } catch (e) { busy("disc-status", false, ""); toast(e.message, true); }
  });

  // ---- path analysis ----
  document.getElementById("path-btn").addEventListener("click", async () => {
    const host = document.getElementById("path-host").value.trim();
    if (!host) { toast("Enter a host", true); return; }
    busy("path-status", true, "Tracing…");
    try {
      const d = await api("/api/tools/path", { method: "POST",
        body: JSON.stringify({ host, cycles: parseInt(document.getElementById("path-cycles").value, 10) }) });
      const tb = document.querySelector("#path-table tbody");
      tb.innerHTML = "";
      for (const h of (d.hops || [])) {
        const tr = document.createElement("tr");
        tr.appendChild(mkCell(h.hop, "num mono"));
        tr.appendChild(mkCell(h.host, "mono"));
        tr.appendChild(mkCell(h.loss_pct + "%", "num mono " + lossCls(h.loss_pct)));
        tr.appendChild(mkCell(h.sent, "num mono"));
        tr.appendChild(mkCell(h.avg == null ? "—" : h.avg + " ms", "num mono"));
        tr.appendChild(mkCell(h.best == null ? "—" : h.best + " ms", "num mono"));
        tr.appendChild(mkCell(h.worst == null ? "—" : h.worst + " ms", "num mono"));
        tb.appendChild(tr);
      }
      document.getElementById("path-table").style.display = (d.hops || []).length ? "" : "none";
      busy("path-status", false, d.error ? "Error: " + d.error : "via " + d.tool);
    } catch (e) { busy("path-status", false, ""); toast(e.message, true); }
  });

  // ---- SNMP ----
  document.getElementById("snmp-btn").addEventListener("click", async () => {
    const host = document.getElementById("snmp-host").value.trim();
    if (!host) { toast("Enter a host", true); return; }
    busy("snmp-status", true, "Querying…");
    try {
      const d = await api("/api/tools/snmp", { method: "POST",
        body: JSON.stringify({ host, community: document.getElementById("snmp-comm").value,
                               version: document.getElementById("snmp-ver").value }) });
      const tb = document.querySelector("#snmp-table tbody");
      tb.innerHTML = "";
      if (d.ok) {
        for (const [k, v] of Object.entries(d.values)) {
          const tr = document.createElement("tr");
          const c1 = mkCell(k); c1.style.fontWeight = "600"; c1.style.width = "160px";
          tr.appendChild(c1); tr.appendChild(mkCell(v, "mono"));
          tb.appendChild(tr);
        }
        document.getElementById("snmp-table").style.display = "";
        busy("snmp-status", false, "via " + d.tool);
      } else {
        document.getElementById("snmp-table").style.display = "none";
        busy("snmp-status", false, d.error || "no response");
      }
    } catch (e) { busy("snmp-status", false, ""); toast(e.message, true); }
  });

  // ---- iperf ----
  document.getElementById("iperf-btn").addEventListener("click", async () => {
    const host = document.getElementById("iperf-host").value.trim();
    if (!host) { toast("Enter a host", true); return; }
    busy("iperf-status", true, "Testing… (this takes a few seconds)");
    try {
      const d = await api("/api/tools/iperf", { method: "POST",
        body: JSON.stringify({ host, seconds: parseInt(document.getElementById("iperf-secs").value, 10),
                               reverse: document.getElementById("iperf-rev").checked }) });
      const box = document.getElementById("iperf-result");
      if (d.ok) {
        box.style.display = "flex";
        box.innerHTML =
          '<div class="stat"><div class="label">Upload</div><div class="value">' +
            d.sent_mbps + '<span class="unit"> Mbps</span></div></div>' +
          '<div class="stat"><div class="label">Download</div><div class="value">' +
            d.recv_mbps + '<span class="unit"> Mbps</span></div></div>' +
          (d.retransmits != null ? '<div class="stat"><div class="label">Retransmits</div><div class="value small ' +
            (d.retransmits > 0 ? "v-warn" : "v-good") + '">' + d.retransmits + '</div></div>' : '');
        busy("iperf-status", false, "");
      } else {
        box.style.display = "none";
        busy("iperf-status", false, d.error || "failed");
      }
    } catch (e) { busy("iperf-status", false, ""); toast(e.message, true); }
  });

  // ---- known devices ----
  async function loadKnown() {
    const d = await api("/api/tools/known");
    const tb = document.querySelector("#known-table tbody");
    tb.innerHTML = "";
    document.getElementById("known-empty").style.display = d.devices.length ? "none" : "block";
    for (const x of d.devices) {
      const tr = document.createElement("tr");
      tr.appendChild(mkCell(x.ip || "—", "mono"));
      tr.appendChild(mkCell(x.mac, "mono"));
      tr.appendChild(mkCell(x.vendor || "—"));
      tr.appendChild(mkCell(new Date(x.first_seen * 1000).toLocaleString()));
      tr.appendChild(mkCell(new Date(x.last_seen * 1000).toLocaleString()));
      const td = document.createElement("td"); td.className = "right";
      if (x.acknowledged) {
        td.appendChild(mkCell("✓ acknowledged", "v-good"));
      } else {
        const b = document.createElement("button");
        b.className = "btn small"; b.textContent = "Acknowledge";
        b.addEventListener("click", async () => {
          await api("/api/tools/acknowledge", { method: "POST",
            body: JSON.stringify({ mac: x.mac }) });
          loadKnown();
        });
        td.appendChild(b);
      }
      tr.appendChild(td);
      tb.appendChild(tr);
    }
  }

  api("/api/tools/env").then(e => {
    document.getElementById("disc-subnet").placeholder = e.default_subnet;
    document.getElementById("snmp-avail").textContent = e.snmp ? "" : "— engine not installed";
    document.getElementById("iperf-avail").textContent = e.iperf ? "" : "— iperf3 not installed";
    if (!e.snmp) document.getElementById("snmp-btn").disabled = true;
    if (!e.iperf) document.getElementById("iperf-btn").disabled = true;
  }).catch(() => {});
  loadKnown().catch(() => {});
})();
</script>
{% endblock %}
