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
})();
