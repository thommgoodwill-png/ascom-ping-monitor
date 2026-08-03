/* Shared helpers: theme, fetch, toast */
(function () {
  "use strict";

  // ---- theme ----
  function applyTheme(mode) {
    if (mode === "light" || mode === "dark") {
      document.documentElement.setAttribute("data-theme", mode);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    document.dispatchEvent(new Event("themechange"));
    const btn = document.getElementById("theme-btn");
    if (btn) btn.textContent = currentIsDark() ? "☀" : "☾";
  }

  function currentIsDark() {
    const attr = document.documentElement.getAttribute("data-theme");
    if (attr) return attr === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  window.initTheme = function (serverDefault) {
    const saved = localStorage.getItem("pingmon-theme");
    applyTheme(saved || serverDefault || "auto");
    const btn = document.getElementById("theme-btn");
    if (btn) {
      btn.addEventListener("click", () => {
        const next = currentIsDark() ? "light" : "dark";
        localStorage.setItem("pingmon-theme", next);
        applyTheme(next);
      });
    }
    window.matchMedia("(prefers-color-scheme: dark)")
      .addEventListener("change", () => applyTheme(
        document.documentElement.getAttribute("data-theme") || "auto"));
  };

  // ---- api ----
  window.api = async function (url, opts) {
    const res = await fetch(url, Object.assign({
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
    }, opts));
    if (res.status === 401) { location.href = "/login"; throw new Error("auth"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  };

  // ---- toast ----
  let toastTimer = null;
  window.toast = function (msg, isErr) {
    let el = document.getElementById("toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.className = isErr ? "err" : "";
    el.style.display = "block";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.style.display = "none"; }, 3500);
  };

  window.fmtAgo = function (ts) {
    if (!ts) return "—";
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 60) return Math.round(s) + "s ago";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    return Math.round(s / 3600) + "h ago";
  };

  // populate the top-bar monitoring indicator on every page
  document.addEventListener("DOMContentLoaded", function () {
    const st = document.getElementById("mon-state");
    if (!st || !window.api) return;
    api("/api/settings").then(d => {
      const on = d.settings.monitoring_enabled;
      st.classList.toggle("off", !on);
      document.getElementById("mon-state-text").textContent = on ? "Monitoring" : "Paused";
    }).catch(() => {});
  });

  window.latClass = function (v, warn, crit) {
    if (v == null) return "";
    if (v > crit) return "v-crit";
    if (v > warn) return "v-warn";
    return "v-good";
  };

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }
  function fmtDur(s) {
    s = Math.round(s || 0);
    if (s < 60) return s + "s";
    if (s < 3600) return Math.floor(s / 60) + "m " + (s % 60) + "s";
    if (s < 86400) return Math.floor(s / 3600) + "h " + Math.floor((s % 3600) / 60) + "m";
    return Math.floor(s / 86400) + "d " + Math.floor((s % 86400) / 3600) + "h";
  }
  function fmtWhen(ts) {
    try { return new Date(ts * 1000).toLocaleString(); } catch (_) { return "—"; }
  }

  // ---- device flag colour (tile background) ----
  // preset flags shown in the picker; value stored is the hex string
  window.TILE_COLORS = [
    { name: "None", hex: "" },
    { name: "Red", hex: "#e5484d" },
    { name: "Orange", hex: "#f5a524" },
    { name: "Yellow", hex: "#e2c50a" },
    { name: "Green", hex: "#30a46c" },
    { name: "Blue", hex: "#3b82f6" },
    { name: "Purple", hex: "#8b5cf6" },
    { name: "Pink", hex: "#e93d82" },
    { name: "Grey", hex: "#8b8f98" },
  ];
  window.hexToRgba = function (hex, a) {
    hex = (hex || "").replace("#", "");
    if (hex.length === 3) hex = hex.split("").map(c => c + c).join("");
    if (hex.length !== 6) return null;
    const n = parseInt(hex, 16);
    if (isNaN(n)) return null;
    return "rgba(" + ((n >> 16) & 255) + "," + ((n >> 8) & 255) + "," + (n & 255) + "," + a + ")";
  };
  // apply a flag colour to a card as a subtle tint + left accent (theme-safe:
  // the tint overlays the existing surface, so text stays readable in dark mode)
  window.applyTileColor = function (card, hex) {
    if (!card) return;
    const bg = hexToRgba(hex, 0.14), bd = hexToRgba(hex, 0.85);
    if (bg) {
      card.style.backgroundImage = "linear-gradient(" + bg + "," + bg + ")";
      card.style.borderLeft = "4px solid " + bd;
    } else {
      card.style.backgroundImage = "";
      card.style.borderLeft = "";
    }
  };

  // ---- reusable modal ----
  let _onClose = null;
  function _modalKey(e) { if (e.key === "Escape") window.closeModal(); }
  window.closeModal = function () {
    const o = document.getElementById("modal-overlay");
    if (o) o.remove();
    document.removeEventListener("keydown", _modalKey);
    if (_onClose) { const f = _onClose; _onClose = null; try { f(); } catch (_) {} }
  };
  window.openModal = function (content, opts) {
    opts = opts || {};
    window.closeModal();
    _onClose = opts.onClose || null;
    const ov = document.createElement("div");
    ov.id = "modal-overlay"; ov.className = "modal-overlay";
    const box = document.createElement("div");
    box.className = "modal-box" + (opts.wide ? " wide" : "");
    const x = document.createElement("button");
    x.className = "modal-close"; x.innerHTML = "✕"; x.title = "Close (Esc)";
    x.addEventListener("click", window.closeModal);
    box.appendChild(x);
    const body = document.createElement("div"); body.className = "modal-body";
    if (typeof content === "string") body.innerHTML = content;
    else body.appendChild(content);
    box.appendChild(body);
    ov.appendChild(box);
    ov.addEventListener("mousedown", e => { if (e.target === ov) window.closeModal(); });
    document.body.appendChild(ov);
    document.addEventListener("keydown", _modalKey);
    return body;
  };

  // ---- shared device EDIT modal (works for hub + site devices; saves via PUT
  //      which the site agent mirrors down on its next config sync) ----
  window.deviceEditModal = function (dev, onSaved) {
    const body = document.createElement("div");
    body.innerHTML =
      '<h2 style="margin-top:0;">Edit device</h2>' +
      '<div class="f-row"><div style="flex:2;"><label class="f-label">Name</label>' +
        '<input id="ed-name"></div><div style="flex:2;"><label class="f-label">IP / hostname</label>' +
        '<input id="ed-host"></div></div>' +
      '<div class="f-row"><div><label class="f-label">Ping interval override (s)</label>' +
        '<input id="ed-iv" type="number" min="0.2" max="3600" step="0.1" placeholder="use global"></div>' +
        '<div><label class="f-label">Warn override (ms)</label><input id="ed-warn" type="number" placeholder="global"></div>' +
        '<div><label class="f-label">Crit override (ms)</label><input id="ed-crit" type="number" placeholder="global"></div></div>' +
      '<div class="f-row"><div><label class="f-label">TCP ports</label>' +
        '<input id="ed-ports" placeholder="443, 22"></div>' +
        '<div style="flex:2;"><label class="f-label">HTTP(S) URL</label>' +
        '<input id="ed-url" placeholder="https://host/health"></div>' +
        '<div style="display:flex;align-items:flex-end;"><label style="display:inline-flex;gap:8px;align-items:center;">' +
        '<span class="switch"><input type="checkbox" id="ed-en"><span class="track"></span></span>Enabled</label></div></div>' +
      '<div class="f-row"><div style="flex:1;"><label class="f-label">Flag colour ' +
        '<span class="muted">(tile background — for easy identification)</span></label>' +
        '<div id="ed-colors" class="color-swatches"></div></div></div>' +
      '<div class="f-help">Interval / warn / crit blank = use the global default. Changes to a ' +
        'customer-site device are pushed to its agent on the next sync.</div>' +
      '<div style="display:flex;gap:8px;margin-top:14px;"><button class="btn primary" id="ed-save">Save</button>' +
        '<button class="btn" id="ed-cancel">Cancel</button></div>' +
      '<div id="ed-msg" class="muted" style="margin-top:8px;font-size:13px;"></div>';
    window.openModal(body);
    const g = s => body.querySelector(s);
    g("#ed-name").value = dev.name || ""; g("#ed-host").value = dev.host || "";
    g("#ed-iv").value = dev.interval_override || ""; g("#ed-warn").value = dev.warn_override || "";
    g("#ed-crit").value = dev.crit_override || ""; g("#ed-ports").value = dev.tcp_ports || "";
    g("#ed-url").value = dev.check_url || ""; g("#ed-en").checked = !!dev.enabled;
    // flag colour picker
    let chosen = dev.tile_color || "";
    const cbox = g("#ed-colors");
    function renderSwatches() {
      cbox.innerHTML = "";
      for (const c of window.TILE_COLORS) {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "swatch" + (chosen === c.hex ? " sel" : "") + (c.hex ? "" : " none");
        b.title = c.name;
        if (c.hex) b.style.background = c.hex;
        b.addEventListener("click", () => { chosen = c.hex; renderSwatches(); });
        cbox.appendChild(b);
      }
    }
    renderSwatches();
    g("#ed-cancel").addEventListener("click", window.closeModal);
    g("#ed-save").addEventListener("click", async () => {
      const b = {
        name: g("#ed-name").value.trim(), host: g("#ed-host").value.trim(),
        enabled: g("#ed-en").checked,
        interval_override: g("#ed-iv").value.trim() || null,
        warn_override: g("#ed-warn").value.trim() || null,
        crit_override: g("#ed-crit").value.trim() || null,
        tcp_ports: g("#ed-ports").value.trim(),
        check_url: g("#ed-url").value.trim(),
        tile_color: chosen,
      };
      if (!b.name || !b.host) {
        g("#ed-msg").innerHTML = '<span class="v-crit">Name and host are required</span>'; return;
      }
      try {
        await api("/api/devices/" + dev.id, { method: "PUT", body: JSON.stringify(b) });
        window.closeModal(); toast("Saved"); if (onSaved) onSaved();
      } catch (e) { g("#ed-msg").innerHTML = '<span class="v-crit">' + esc(e.message) + '</span>'; }
    });
  };

  // ---- shared device DETAIL modal (in-depth behaviour view) ----
  window.deviceDetailModal = function (devId) {
    const body = document.createElement("div");
    body.innerHTML =
      '<div id="dd-head" style="margin-bottom:6px;"></div>' +
      '<div id="dd-range" style="margin:8px 0;"></div>' +
      '<div id="dd-chart" style="min-height:220px;"></div>' +
      '<div id="dd-stats" class="modal-stats"></div>' +
      '<h3 style="margin:16px 0 6px;">Recent events</h3><div id="dd-events"></div>';
    let chart = null;
    window.openModal(body, { wide: true, onClose: () => { if (chart) chart.destroy(); } });
    const ranges = [["15 m", 900], ["1 h", 3600], ["6 h", 21600],
                    ["24 h", 86400], ["7 d", 604800], ["30 d", 2592000]];
    let sec = 3600;
    const rr = body.querySelector("#dd-range");
    rr.innerHTML = '<div class="range-group">' + ranges.map(r =>
      '<button class="range-btn' + (r[1] === sec ? " active" : "") + '" data-s="' + r[1] + '">' +
      r[0] + '</button>').join("") + '</div>';
    rr.addEventListener("click", e => {
      const b = e.target.closest(".range-btn"); if (!b) return;
      rr.querySelectorAll(".range-btn").forEach(x => x.classList.remove("active"));
      b.classList.add("active"); sec = parseInt(b.dataset.s, 10); load();
    });
    const stat = (l, v) => '<div class="stat"><div class="label">' + l +
      '</div><div class="value small">' + v + '</div></div>';
    const msv = (v, w, c) => v == null ? '<span class="muted">—</span>' :
      '<span class="' + latClass(v, w, c) + '">' + v + '<span class="unit"> ms</span></span>';
    const evPill = t => '<span class="pill ' + (t === "up" ? "up" : t === "loss" ? "warn" : "down") +
      '">' + (t === "up" ? "● up" : t === "loss" ? "▲ loss" : "✖ down") + '</span>';
    async function load() {
      let d;
      try { d = await api("/api/devices/" + devId + "/detail?seconds=" + sec); }
      catch (e) { body.querySelector("#dd-head").innerHTML = '<span class="v-crit">' + esc(e.message) + '</span>'; return; }
      const dev = d.device, s = d.stats;
      const [pc, pl] = pillForState(dev);
      body.querySelector("#dd-head").innerHTML =
        '<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;">' +
        '<span class="pill ' + pc + '">' + pl + '</span>' +
        '<h2 style="margin:0;">' + esc(dev.name) + '</h2>' +
        '<span class="muted">' + esc(dev.host) + (dev.mac ? " · " + esc(dev.mac) : "") +
        (dev.vendor ? " · " + esc(dev.vendor) : "") + '</span>' +
        '<button class="btn small" id="dd-edit" style="margin-left:auto;">Edit device</button></div>';
      body.querySelector("#dd-edit").addEventListener("click", () =>
        window.deviceEditModal(dev, () => window.deviceDetailModal(devId)));
      body.querySelector("#dd-stats").innerHTML =
        stat("Uptime", s.uptime_pct == null ? "—" : s.uptime_pct + '<span class="unit"> %</span>') +
        stat("Avg", msv(s.avg, d.warn_ms, d.crit_ms)) +
        stat("Max", msv(s.max, d.warn_ms, d.crit_ms)) +
        stat("Min", msv(s.min, d.warn_ms, d.crit_ms)) +
        stat("Jitter", s.jitter == null ? '<span class="muted">—</span>' : s.jitter + '<span class="unit"> ms</span>') +
        stat("Loss", s.loss == null ? '<span class="muted">—</span>' :
          '<span class="' + (s.loss > 0 ? "v-crit" : "v-good") + '">' + s.loss + '<span class="unit"> %</span></span>') +
        stat("Failed pings", '<span class="' + ((s.failed || 0) > 0 ? "v-crit" : "v-good") + '">' + (s.failed || 0) + '</span>') +
        stat("Outages", s.outage_count) +
        stat("Downtime", fmtDur(s.downtime_s)) +
        stat("Samples", s.sent);
      const o = { series: [{ name: dev.name, colorIndex: 0, data: d.series || [] }],
        start: d.start, end: d.end, bucket: d.bucket, warn: d.warn_ms, crit: d.crit_ms,
        height: 220, thresholdColoring: true };
      const box = body.querySelector("#dd-chart");
      if (!chart) chart = new LatencyChart(box, o); else chart.update(o);
      const ev = d.events || [];
      body.querySelector("#dd-events").innerHTML = ev.length ?
        '<table class="list"><thead><tr><th>When</th><th>Event</th><th>Detail</th></tr></thead><tbody>' +
        ev.map(e => '<tr><td class="mono" style="font-size:12px;white-space:nowrap;">' + fmtWhen(e.ts) +
          '</td><td>' + evPill(e.type) + '</td><td class="muted">' + esc(e.detail || "") + '</td></tr>').join("") +
        '</tbody></table>' : '<span class="muted">No events recorded in this range.</span>';
    }
    function pillForState(dev) {
      if (!dev.enabled) return ["disabled", "○ Disabled"];
      if (dev.state === "down" || dev.last_success === false) return ["down", "✖ Down"];
      if (dev.last_latency != null && dev.last_latency > dev.eff_crit) return ["crit", "■ Slow"];
      if (dev.last_latency != null && dev.last_latency > dev.eff_warn) return ["warn", "▲ Warn"];
      if (dev.last_latency != null || dev.state === "up") return ["up", "● Up"];
      return ["unknown", "… Waiting"];
    }
    load();
  };
})();
