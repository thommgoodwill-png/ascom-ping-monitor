/* Lightweight latency chart engine — no external dependencies.
 * Line charts with threshold coloring, crosshair + tooltip, light/dark aware.
 */
(function () {
  "use strict";

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  const SERIES_VARS = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];

  function seriesColor(i) {
    return cssVar(SERIES_VARS[i % SERIES_VARS.length]);
  }

  function niceTicks(maxVal, count) {
    if (maxVal <= 0) maxVal = 1;
    const rough = maxVal / count;
    const mag = Math.pow(10, Math.floor(Math.log10(rough)));
    let step = mag;
    for (const m of [1, 2, 2.5, 5, 10]) {
      if (rough <= m * mag) { step = m * mag; break; }
    }
    const ticks = [];
    for (let v = 0; v <= maxVal + 1e-9; v += step) ticks.push(Math.round(v * 100) / 100);
    return ticks;
  }

  function fmtTime(ts, spanSec) {
    const d = new Date(ts * 1000);
    if (spanSec > 3 * 86400) {
      return d.toLocaleDateString([], { day: "numeric", month: "short" });
    }
    if (spanSec > 86400) {
      return d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " +
             d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function fmtTipTime(ts, bucket) {
    const d = new Date(ts * 1000);
    const opts = { hour: "2-digit", minute: "2-digit" };
    if (bucket < 60) opts.second = "2-digit";
    return d.toLocaleDateString([], { day: "numeric", month: "short" }) + " " +
           d.toLocaleTimeString([], opts);
  }

  /* opts: { series: [{name, data:[[ts,avg,max,fails,count],...], colorIndex|color}],
   *         start, end, bucket, warn, crit, height, thresholdColoring } */
  function LatencyChart(container, opts) {
    this.container = container;
    this.opts = opts;
    this.canvas = document.createElement("canvas");
    this.tip = document.createElement("div");
    this.tip.className = "chart-tip";
    container.classList.add("chart-box");
    container.appendChild(this.canvas);
    container.appendChild(this.tip);
    this.height = opts.height || 220;
    this.hoverX = null;

    this._onMove = this._onMove.bind(this);
    this._onLeave = this._onLeave.bind(this);
    this.canvas.addEventListener("pointermove", this._onMove);
    this.canvas.addEventListener("pointerleave", this._onLeave);

    this._ro = new ResizeObserver(() => this.draw());
    this._ro.observe(container);
    document.addEventListener("themechange", () => this.draw());
    this.draw();
  }

  LatencyChart.prototype.update = function (opts) {
    Object.assign(this.opts, opts);
    this.draw();
  };

  LatencyChart.prototype.destroy = function () {
    this._ro.disconnect();
    this.container.innerHTML = "";
  };

  LatencyChart.prototype._layout = function () {
    const w = this.container.clientWidth || 600;
    const h = this.height;
    return { w, h, left: 46, right: 12, top: 12, bottom: 24 };
  };

  LatencyChart.prototype.draw = function () {
    const o = this.opts;
    const L = this._layout();
    const dpr = window.devicePixelRatio || 1;
    const cv = this.canvas;
    cv.width = L.w * dpr;
    cv.height = L.h * dpr;
    cv.style.height = L.h + "px";
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, L.w, L.h);

    const ink2 = cssVar("--muted");
    const grid = cssVar("--grid");
    const baseline = cssVar("--baseline");
    const warnC = cssVar("--warn");
    const critC = cssVar("--crit");
    const surface = cssVar("--surface");

    const plotW = L.w - L.left - L.right;
    const plotH = L.h - L.top - L.bottom;
    if (plotW < 30 || plotH < 30) return;

    // y scale: at least a bit above crit so threshold lines always visible
    let dataMax = 0;
    for (const s of o.series) {
      for (const p of s.data) if (p[2] != null && p[2] > dataMax) dataMax = p[2];
    }
    const yMax = Math.max(dataMax * 1.15, o.crit * 1.25, 10);
    const ticks = niceTicks(yMax, 4);
    const yTop = ticks[ticks.length - 1] * 1.02;
    const X = ts => L.left + ((ts - o.start) / (o.end - o.start)) * plotW;
    const Y = v => L.top + plotH - (v / yTop) * plotH;

    ctx.font = "11px system-ui, sans-serif";

    // gridlines + y labels
    ctx.strokeStyle = grid;
    ctx.fillStyle = ink2;
    ctx.lineWidth = 1;
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";
    for (const t of ticks) {
      const y = Math.round(Y(t)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(L.left, y);
      ctx.lineTo(L.w - L.right, y);
      ctx.stroke();
      ctx.fillText(String(t), L.left - 6, y);
    }
    // baseline
    ctx.strokeStyle = baseline;
    ctx.beginPath();
    const by = Math.round(Y(0)) + 0.5;
    ctx.moveTo(L.left, by);
    ctx.lineTo(L.w - L.right, by);
    ctx.stroke();

    // x ticks
    const span = o.end - o.start;
    const nx = Math.max(2, Math.floor(plotW / 110));
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.fillStyle = ink2;
    for (let i = 0; i <= nx; i++) {
      const ts = o.start + (span * i) / nx;
      const x = Math.round(X(ts)) + 0.5;
      ctx.fillText(fmtTime(ts, span), Math.min(Math.max(x, L.left + 24), L.w - 30), L.top + plotH + 7);
    }

    // threshold guides (status colors, hairline + right-side label)
    for (const [val, col, lab] of [[o.warn, warnC, o.warn + " ms"], [o.crit, critC, o.crit + " ms"]]) {
      if (val >= yTop) continue;
      const y = Math.round(Y(val)) + 0.5;
      ctx.strokeStyle = col;
      ctx.globalAlpha = 0.55;
      ctx.beginPath();
      ctx.moveTo(L.left, y);
      ctx.lineTo(L.w - L.right, y);
      ctx.stroke();
      ctx.globalAlpha = 1;
      ctx.fillStyle = col;
      ctx.textAlign = "left";
      ctx.textBaseline = "bottom";
      ctx.fillText(lab, L.left + 4, y - 1);
    }

    // series lines
    const thresholdColor = v =>
      v > o.crit ? critC : v > o.warn ? warnC : null;

    o.series.forEach((s, si) => {
      const col = s.color || seriesColor(s.colorIndex != null ? s.colorIndex : si);
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      const maxGap = (o.bucket || 60) * 3; // break line across missing data
      let prev = null;
      for (const p of s.data) {
        if (p[1] == null) { prev = null; continue; }
        if (prev && p[0] - prev[0] <= maxGap) {
          let segCol = col;
          if (o.thresholdColoring) {
            segCol = thresholdColor(Math.max(prev[1], p[1])) || col;
          }
          ctx.strokeStyle = segCol;
          ctx.beginPath();
          ctx.moveTo(X(prev[0]), Y(prev[1]));
          ctx.lineTo(X(p[0]), Y(p[1]));
          ctx.stroke();
        }
        prev = p;
      }
      // breach markers on top (>=8px with 2px surface ring)
      for (const p of s.data) {
        if (p[1] == null) continue;
        const tc = thresholdColor(p[1]);
        if (tc) {
          ctx.beginPath();
          ctx.arc(X(p[0]), Y(p[1]), 4, 0, Math.PI * 2);
          ctx.fillStyle = tc;
          ctx.strokeStyle = surface;
          ctx.lineWidth = 2;
          ctx.fill();
          ctx.stroke();
        }
      }
      // failure markers: triangles at the baseline
      ctx.fillStyle = critC;
      for (const p of s.data) {
        if (p[3] > 0) {
          const x = X(p[0]);
          const y = L.top + plotH;
          ctx.beginPath();
          ctx.moveTo(x, y - 7);
          ctx.lineTo(x - 4.5, y);
          ctx.lineTo(x + 4.5, y);
          ctx.closePath();
          ctx.fill();
        }
      }
    });

    // crosshair
    if (this.hoverX != null) {
      const x = Math.round(this.hoverX) + 0.5;
      ctx.strokeStyle = baseline;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, L.top);
      ctx.lineTo(x, L.top + plotH);
      ctx.stroke();
    }
    this._L = L;
    this._X = X;
  };

  LatencyChart.prototype._nearestTs = function (px) {
    const o = this.opts;
    const L = this._L;
    const frac = (px - L.left) / (L.w - L.left - L.right);
    const target = o.start + frac * (o.end - o.start);
    // snap to nearest bucket present in any series
    let best = null, bestD = Infinity;
    for (const s of o.series) {
      for (const p of s.data) {
        const d = Math.abs(p[0] - target);
        if (d < bestD) { bestD = d; best = p[0]; }
      }
    }
    return best;
  };

  LatencyChart.prototype._onMove = function (ev) {
    const rect = this.canvas.getBoundingClientRect();
    const px = ev.clientX - rect.left;
    const o = this.opts;
    if (!this._L || !o.series.length) return;
    const ts = this._nearestTs(px);
    if (ts == null) { this._onLeave(); return; }
    this.hoverX = this._X(ts);
    this.draw();

    // build tooltip (textContent only — labels are untrusted)
    const tip = this.tip;
    tip.innerHTML = "";
    const t = document.createElement("div");
    t.className = "tip-time";
    t.textContent = fmtTipTime(ts, o.bucket || 60);
    tip.appendChild(t);
    const half = (o.bucket || 60) / 2 + 0.5;
    o.series.forEach((s, si) => {
      let pt = null;
      for (const p of s.data) if (Math.abs(p[0] - ts) <= half) { pt = p; break; }
      const row = document.createElement("div");
      row.className = "tip-row";
      const key = document.createElement("span");
      key.className = "key";
      key.style.borderTopColor = s.color || seriesColor(s.colorIndex != null ? s.colorIndex : si);
      row.appendChild(key);
      const val = document.createElement("span");
      val.className = "tip-val";
      if (!pt || pt[1] == null) {
        val.textContent = pt && pt[3] > 0 ? "✖ timeout" : "—";
        if (pt && pt[3] > 0) val.style.color = cssVar("--crit-text");
      } else {
        val.textContent = pt[1].toFixed(1) + " ms";
        if (pt[1] > o.crit) { val.textContent += " ■ crit"; val.style.color = cssVar("--crit-text"); }
        else if (pt[1] > o.warn) { val.textContent += " ▲ warn"; val.style.color = cssVar("--warn-text"); }
      }
      row.appendChild(val);
      const name = document.createElement("span");
      name.className = "tip-name";
      name.textContent = s.name;
      row.appendChild(name);
      if (pt && pt[3] > 0 && pt[1] != null) {
        const f = document.createElement("span");
        f.className = "tip-name";
        f.style.color = cssVar("--crit-text");
        f.textContent = `(${pt[3]} failed)`;
        row.appendChild(f);
      }
      if (o.series.length === 1 && pt && pt[5] != null) {
        const j = document.createElement("span");
        j.className = "tip-name";
        j.textContent = `· jitter ${pt[5].toFixed(1)} ms`;
        row.appendChild(j);
      }
      tip.appendChild(row);
    });
    tip.style.display = "block";
    const cw = this.container.clientWidth;
    const tw = tip.offsetWidth;
    let lx = this.hoverX + 14;
    if (lx + tw > cw - 4) lx = this.hoverX - tw - 14;
    tip.style.left = Math.max(4, lx) + "px";
    tip.style.top = "8px";
  };

  LatencyChart.prototype._onLeave = function () {
    this.hoverX = null;
    this.tip.style.display = "none";
    this.draw();
  };

  window.LatencyChart = LatencyChart;
  window.chartSeriesColor = seriesColor;
})();
