{% extends "base.html" %}
{% block content %}
<div style="margin-bottom:8px;"><a href="/customers" class="muted">← Customers</a></div>
<h1 class="page-title" id="c-title">Customer</h1>

<div class="card">
  <h2>Add a site</h2>
  <p class="card-sub">A site is a monitored location. Adding one generates a unique
    <b>API key</b> — install the agent on a machine at that site and paste the key
    (and this hub's URL) into its Settings → Central hub section.</p>
  <div class="f-row">
    <div>
      <label class="f-label" for="s-name">Site name</label>
      <input type="text" id="s-name" placeholder="Head Office / Site A">
    </div>
    <div style="display:flex;align-items:flex-end;">
      <button class="btn primary" id="s-add">+ Add site</button>
    </div>
  </div>
  <div id="s-newkey" style="display:none;" class="card" style="background:var(--surface-2);">
    <div class="t-title">Site created — here is its API key (shown once):</div>
    <div class="mono" id="s-key" style="font-size:13px;margin:8px 0;padding:8px;background:var(--surface);border-radius:6px;word-break:break-all;"></div>
    <div class="f-help">Copy it now. You can regenerate it later from the site page if lost.</div>
  </div>
</div>

<div class="card">
  <h2>Sites</h2>
  <table class="list" id="s-table">
    <thead><tr><th>Site</th><th>Agent</th><th class="num">Devices</th><th>Last seen</th><th class="right">Actions</th></tr></thead>
    <tbody></tbody>
  </table>
  <p class="muted" id="s-empty" style="display:none;">No sites yet — add one above.</p>
</div>
{% endblock %}

{% block scripts %}
<script>
(function () {
  "use strict";
  const CID = {{ cid }};

  function agentPill(site) {
    const online = site.last_seen && (Date.now()/1000 - site.last_seen) < 180;
    const span = document.createElement("span");
    span.className = "pill " + (online ? "up" : "disabled");
    span.textContent = online ? "● Online" : (site.last_seen ? "○ Offline" : "… No agent yet");
    return span;
  }

  async function load() {
    const d = await api("/api/customers/" + CID + "/sites");
    document.getElementById("c-title").textContent = d.customer.name;
    const tb = document.querySelector("#s-table tbody");
    tb.innerHTML = "";
    document.getElementById("s-empty").style.display = d.sites.length ? "none" : "block";
    for (const s of d.sites) {
      const tr = document.createElement("tr");
      const n = document.createElement("td");
      const a = document.createElement("a");
      a.href = "/sites/" + s.id; a.textContent = s.name;
      a.style.cssText = "font-weight:600;color:var(--ascom);text-decoration:none;";
      n.appendChild(a); tr.appendChild(n);
      const ag = document.createElement("td"); ag.appendChild(agentPill(s));
      if (s.agent_version) { const v=document.createElement("span"); v.className="muted"; v.style.marginLeft="6px"; v.textContent="v"+s.agent_version; ag.appendChild(v);} tr.appendChild(ag);
      const dc = document.createElement("td"); dc.className="num mono"; dc.textContent=s.device_count; tr.appendChild(dc);
      const ls = document.createElement("td"); ls.className="muted";
      ls.textContent = s.last_seen ? new Date(s.last_seen*1000).toLocaleString() : "never"; tr.appendChild(ls);
      const ac = document.createElement("td"); ac.className="right";
      const open = document.createElement("a"); open.className="btn small"; open.textContent="Open"; open.href="/sites/"+s.id;
      const del = document.createElement("button"); del.className="btn small danger"; del.style.marginLeft="6px"; del.textContent="Delete";
      del.addEventListener("click", async () => {
        if (!confirm('Delete site "'+s.name+'" and its devices/history?')) return;
        try { await api("/api/sites/"+s.id, {method:"DELETE"}); load(); } catch(e){ toast(e.message,true); }
      });
      ac.append(open, del); tr.appendChild(ac);
      tb.appendChild(tr);
    }
  }

  document.getElementById("s-add").addEventListener("click", async () => {
    const name = document.getElementById("s-name").value.trim();
    if (!name) { toast("Name required", true); return; }
    try {
      const r = await api("/api/customers/" + CID + "/sites", { method:"POST",
        body: JSON.stringify({ name: name }) });
      document.getElementById("s-name").value = "";
      document.getElementById("s-key").textContent = r.api_key;
      document.getElementById("s-newkey").style.display = "block";
      load();
    } catch (e) { toast(e.message, true); }
  });
  load();
})();
</script>
{% endblock %}
