<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}Ascom Network Monitor{% endblock %}</title>
<link rel="icon" href="{{ favicon_url }}">
<link rel="stylesheet" href="{{ url_for('static', filename='css/style.css') }}">
<script>
  // apply saved theme before first paint to avoid flash
  (function () {
    var t = localStorage.getItem("pingmon-theme") || "{{ theme }}";
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  })();
</script>
</head>
<body>
<header class="topbar">
  <a class="brand" href="/">
    <img src="{{ logo_url }}" alt="ascom">
    <span class="sub">NETWORK MONITOR</span>
  </a>
  <nav class="nav">
    <a href="/" class="{{ 'active' if page == 'dashboard' }}">Dashboard</a>
    <a href="/customers" class="{{ 'active' if page == 'customers' }}">Customers</a>
    <a href="/devices" class="{{ 'active' if page == 'devices' }}">Devices</a>
    <a href="/events" class="{{ 'active' if page == 'events' }}">Events</a>
    <a href="/heatmap" class="{{ 'active' if page == 'heatmap' }}">Heatmap</a>
    <a href="/sla" class="{{ 'active' if page == 'sla' }}">SLA</a>
    <a href="/capture" class="{{ 'active' if page == 'capture' }}">Capture</a>
    <a href="/tools" class="{{ 'active' if page == 'tools' }}">Tools</a>
    <a href="/settings" class="{{ 'active' if page == 'settings' }}">Settings</a>
  </nav>
  <span class="spacer"></span>
  <div class="top-actions">
    <span class="mon-state" id="mon-state" title="Monitoring status">
      <span class="dot"></span><span id="mon-state-text">…</span>
    </span>
    <a class="icon-btn" href="/wallboard" title="Wallboard mode (full-screen status)">▦</a>
    <button class="icon-btn" id="theme-btn" title="Toggle light / dark mode">☾</button>
    <a class="btn small" href="/logout">Log out</a>
    {% if is_desktop %}
    <button class="btn small danger" id="quit-btn" title="Stop the Network Monitor app">⏻ Quit</button>
    {% endif %}
  </div>
</header>
<main class="wrap">
  {% block content %}{% endblock %}
</main>
<script src="{{ url_for('static', filename='js/app.js') }}"></script>
<script src="{{ url_for('static', filename='js/charts.js') }}"></script>
<script>initTheme("{{ theme }}");</script>
<script>
  (function () {
    var q = document.getElementById("quit-btn");
    if (!q) return;
    q.addEventListener("click", async function () {
      if (!confirm("Stop the Ascom Network Monitor?\n\nMonitoring and the web interface will shut down until you start the app again.")) return;
      q.disabled = true; q.textContent = "Stopping…";
      try {
        await fetch("/api/shutdown", { method: "POST", credentials: "same-origin" });
      } catch (e) { /* the server exits mid-request — expected */ }
      document.body.innerHTML =
        '<div style="max-width:460px;margin:16vh auto;text-align:center;' +
        'font-family:system-ui,sans-serif;color:#52514e;">' +
        '<div style="font-size:26px;font-weight:800;color:#DA291C;">ascom</div>' +
        '<h2 style="margin:18px 0 8px;">Network Monitor stopped</h2>' +
        '<p>You can close this browser tab. To start it again, launch the app ' +
        '(or, on the container, <code>systemctl start ascom-ping-monitor</code>).</p>' +
        '</div>';
    });
  })();
</script>
{% block scripts %}{% endblock %}
</body>
</html>
