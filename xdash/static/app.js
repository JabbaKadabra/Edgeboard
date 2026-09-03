/* xdash front-end: renders the snapshot pushed over Server-Sent Events. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const MASCOT = [
    "..X.....X..",
    "...X...X...",
    "..XXXXXXX..",
    ".XX.XXX.XX.",
    "XXXXXXXXXXX",
    "X.XXXXXXX.X",
    "X.X.....X.X",
    "...XX.XX...",
  ];
  const EYES = [[3, 3], [3, 7]];

  // ---------- helpers ----------
  function fmtTokens(n) {
    n = Number(n) || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(1) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
    if (n >= 1e3) return Math.round(n / 1e3) + "k";
    return String(n);
  }
  function fmtBytes(n) {
    n = Number(n) || 0;
    const u = ["B", "K", "M", "G", "T"];
    let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + u[i];
  }
  function fmtDuration(secs) {
    secs = Math.max(0, Math.round(Number(secs) || 0));
    const d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
    if (d > 0) return `${d}d ${h}h`;
    if (h > 0) return `${h}h ${String(m).padStart(2, "0")}m`;
    if (m > 0) return `${m}m`;
    return `${secs}s`;
  }
  function fmtClockSecs(s) {
    s = Math.max(0, Math.floor(s || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
  function fmtAgo(iso, now) {
    if (!iso) return "";
    const secs = (now - new Date(iso).getTime()) / 1000;
    if (secs < 60) return `${Math.max(0, Math.floor(secs))}s`;
    return fmtDuration(secs).replace(/ 0+m$/, "");
  }
  function fmtResetAt(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    if (sameDay) return time;
    return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + " " + time;
  }
  function setText(el, value) { if (el && el.textContent !== String(value)) el.textContent = String(value); }
  function text(id, value) { setText($(id), value); }
  function heat(pct) { return pct >= 90 ? "hot" : pct >= 70 ? "warm" : ""; }
  function setBar(id, pct) {
    const el = $(id);
    if (!el) return;
    const p = Math.max(0, Math.min(100, Number(pct) || 0));
    el.style.width = p + "%";
    el.className = "bar-fill " + heat(p);
  }

  // ---------- mascot ----------
  function drawPixels(svg, withEyes) {
    svg.innerHTML = "";
    MASCOT.forEach((row, y) => {
      [...row].forEach((ch, x) => {
        if (ch !== "X") return;
        const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        r.setAttribute("x", x); r.setAttribute("y", y); r.setAttribute("width", 1); r.setAttribute("height", 1);
        svg.appendChild(r);
      });
    });
    if (withEyes) {
      EYES.forEach(([y, x]) => {
        const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
        r.setAttribute("x", x); r.setAttribute("y", y); r.setAttribute("width", 1); r.setAttribute("height", 1);
        r.setAttribute("class", "eye");
        svg.appendChild(r);
      });
    }
  }
  drawPixels(document.querySelector(".brand-icon"), false);
  drawPixels($("mascot"), true);

  // ---------- clock ----------
  function tickClock() {
    const d = new Date();
    const hm = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).replace(/\s?[AP]M$/i, "");
    text("clock-hm", hm);
    text("clock-s", String(d.getSeconds()).padStart(2, "0"));
    text("clock-date", d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + (/[AP]M/i.test(d.toLocaleTimeString()) ? (d.getHours() < 12 ? " AM" : " PM") : ""));
  }
  tickClock();
  setInterval(tickClock, 1000);

  // ---------- usage ----------
  function renderUsage(usage, errors) {
    const limits = $("limits");
    const windows = usage.windows || [];
    const source = usage.source;
    text("usage-source", source === "api" ? (usage.stale ? "stale" : "by plan") : source === "local" ? "estimated" : source === "demo" ? "demo" : "");
    const chip = $("chip-claude");
    chip.className = "chip " + ((source === "api" && !usage.stale) || source === "demo" ? "ok" : source === "local" || usage.stale ? "warn" : errors.usage ? "bad" : "");
    chip.title = errors.usage || "";

    limits.innerHTML = "";
    if (!windows.length) {
      limits.innerHTML = `<div class="muted">${errors.usage ? "usage unavailable" : "waiting for usage data…"}</div>`;
    }
    windows.forEach((w) => {
      const div = document.createElement("div");
      div.className = "limit";
      const pct = w.utilization;
      const pctClass = pct == null ? "na" : pct >= 90 ? "high" : pct >= 70 ? "mid" : "";
      const pctText = pct == null ? (w.tokens != null ? fmtTokens(w.tokens) + " tok" : "n/a") : Math.round(pct) + "%";
      const reset = w.seconds_to_reset != null ? `resets in ${fmtDuration(w.seconds_to_reset)} · ${fmtResetAt(w.resets_at)}` : "no activity in window";
      div.innerHTML = `
        <div class="limit-label">${escapeHtml(w.label)}</div>
        <div class="limit-pct ${pctClass}">${pctText}</div>
        <div class="bar"><div class="bar-fill ${heat(pct || 0)}" style="width:${Math.max(0, Math.min(100, pct || 0))}%"></div></div>
        <div class="limit-reset">${reset}</div>`;
      limits.appendChild(div);
    });

    const t = usage.today || {};
    text("t-out", fmtTokens(t.output));
    text("t-in", fmtTokens(t.input));
    text("t-cache", fmtTokens(t.cache_read));
    text("t-write", fmtTokens(t.cache_write));
    text("today-msgs", `${t.messages || 0} msgs`);

    const tl = usage.timeline || [];
    const peak = Math.max(1, usage.peak || 0);
    const box = $("timeline");
    if (box.childElementCount !== tl.length) {
      box.innerHTML = tl.map(() => '<div class="tb"></div>').join("");
    }
    tl.forEach((b, i) => {
      const el = box.children[i];
      const h = Math.max(2, Math.round((b.tokens / peak) * 100));
      el.style.height = h + "%";
      el.className = "tb" + (i === tl.length - 1 ? " now" : "") + (b.tokens === 0 ? " empty" : "");
      el.dataset.label = `${new Date(b.hour_start).toLocaleTimeString([], { hour: "2-digit" })} · ${fmtTokens(b.tokens)}`;
      el.title = el.dataset.label;
    });
    peakLabel = tl.length ? `peak ${fmtTokens(usage.peak)}` : "";
    if (Date.now() > tapUntil) text("timeline-peak", peakLabel);
    const labels = $("timeline-labels");
    if (tl.length && labels.childElementCount === 0) {
      labels.innerHTML = [0, 6, 12, 18, 23].map((i) => `<span>${new Date(tl[i].hour_start).toLocaleTimeString([], { hour: "2-digit" })}</span>`).join("");
    } else if (tl.length) {
      [0, 6, 12, 18, 23].forEach((i, k) => { labels.children[k].textContent = new Date(tl[i].hour_start).toLocaleTimeString([], { hour: "2-digit" }); });
    }
  }

  // Touch panels have no hover: tapping a bar shows its label for a few seconds.
  let peakLabel = "", tapUntil = 0;
  $("timeline").addEventListener("click", (ev) => {
    const bar = ev.target.closest(".tb");
    if (!bar) return;
    tapUntil = Date.now() + 4000;
    text("timeline-peak", bar.dataset.label || "");
    setTimeout(() => { if (Date.now() >= tapUntil) text("timeline-peak", peakLabel); }, 4100);
  });

  // ---------- sessions ----------
  const CARD_HTML = `
      <div class="card-top"><span class="pill"></span><span class="muted card-ago"></span></div>
      <div class="card-title"></div>
      <div class="card-proj"><b></b><span class="card-branch"></span></div>
      <div class="card-detail"></div>
      <div class="card-foot"><span class="tag model" hidden></span><span class="tag card-ctx"></span><span class="tag card-msgs"></span></div>`;
  const cardNodes = new Map();
  function updateCard(el, s, now) {
    const cls = "card " + s.status;
    if (el.className !== cls) el.className = cls;
    setText(el.querySelector(".pill"), s.status);
    setText(el.querySelector(".card-ago"), fmtAgo(s.last_activity, now));
    setText(el.querySelector(".card-title"), s.name);
    setText(el.querySelector(".card-proj b"), s.project);
    setText(el.querySelector(".card-branch"), s.branch ? "@" + s.branch : "");
    setText(el.querySelector(".card-detail"), s.detail);
    const model = el.querySelector(".tag.model");
    setText(model, s.model || "");
    model.hidden = !s.model;
    setText(el.querySelector(".card-ctx"), "ctx " + fmtTokens(s.context_tokens));
    setText(el.querySelector(".card-msgs"), `${s.messages} msgs`);
  }
  function renderSessions(sessions, summary, now) {
    const box = $("sessions");
    text("sessions-summary", `${summary.today || 0} today · ${summary.done || 0} done · ${summary.working || 0} working`);
    $("sessions-empty").hidden = sessions.length > 0;
    const seen = new Set();
    let prev = null;
    sessions.forEach((s) => {
      seen.add(s.id);
      let el = cardNodes.get(s.id);
      if (!el) {
        el = document.createElement("div");
        el.dataset.id = s.id;
        el.innerHTML = CARD_HTML;
        cardNodes.set(s.id, el);
      }
      updateCard(el, s, now);
      const want = prev ? prev.nextElementSibling : box.firstElementChild;
      if (want !== el) box.insertBefore(el, want);  // only moves nodes that are out of order
      prev = el;
    });
    for (const [id, el] of cardNodes) {
      if (!seen.has(id)) { el.remove(); cardNodes.delete(id); }
    }
    $("mascot").classList.toggle("working", (summary.working || 0) > 0);
  }
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---------- spotify ----------
  let np = { running: false, position_s: 0, length_s: 0, status: "", updatedAt: 0 };
  function renderSpotify(sp, errors) {
    const panel = $("spotify");
    const chip = $("chip-spotify");
    np = Object.assign({}, sp, { updatedAt: Date.now() });
    panel.classList.toggle("offline", !sp.running);
    panel.classList.toggle("paused", sp.status !== "Playing");
    chip.className = "chip " + (sp.running ? (sp.status === "Playing" ? "ok" : "warn") : sp.available === false ? "bad" : "");
    chip.title = sp.available === false ? "playerctl not installed" : errors.spotify || "";
    if (!sp.running) {
      text("np-title", sp.available === false ? "playerctl missing" : "Spotify not running");
      text("np-artist", ""); text("np-album", "");
      $("art").hidden = true; $("art-placeholder").hidden = false;
      $("np-bar").style.width = "0%";
      text("np-pos", "0:00"); text("np-len", "0:00");
      text("btn-play", "▶");
      return;
    }
    text("np-title", sp.title || "—");
    text("np-artist", sp.artist || "");
    text("np-album", sp.album || "");
    text("btn-play", sp.status === "Playing" ? "⏸" : "▶");
    const art = $("art");
    if (sp.art_url && art.dataset.src !== sp.art_url) {
      art.dataset.src = sp.art_url;
      art.onload = () => { art.hidden = false; $("art-placeholder").hidden = true; };
      art.onerror = () => { art.hidden = true; $("art-placeholder").hidden = false; };
      art.src = sp.art_url;
    } else if (!sp.art_url) {
      art.hidden = true; $("art-placeholder").hidden = false; art.dataset.src = "";
    }
    tickProgress();
  }
  function tickProgress() {
    if (!np.running) return;
    let pos = np.position_s || 0;
    if (np.status === "Playing") pos += (Date.now() - np.updatedAt) / 1000;
    const len = np.length_s || 0;
    pos = len ? Math.min(pos, len) : pos;
    $("np-bar").style.width = (len ? (pos / len) * 100 : 0) + "%";
    text("np-pos", fmtClockSecs(pos));
    text("np-len", fmtClockSecs(len));
  }
  setInterval(tickProgress, 1000);
  document.querySelectorAll(".ctl").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        const r = await fetch(`/api/spotify/${btn.dataset.action}`, { method: "POST" });
        const data = await r.json();
        if (data.spotify) renderSpotify(data.spotify, {});
      } catch (e) { /* the next snapshot will correct the view */ }
      btn.disabled = false;
    });
  });

  // ---------- system ----------
  function renderSystem(sys, errors) {
    const chip = $("chip-sys");
    chip.className = "chip " + (sys ? "ok" : errors.system ? "bad" : "");
    chip.title = errors.system || "";
    if (!sys) return;
    const cpu = sys.cpu || {}, mem = sys.mem || {}, gpu = sys.gpu, disks = sys.disks || [], net = sys.net || {};
    setBar("m-cpu", cpu.percent); text("v-cpu", `${Math.round(cpu.percent || 0)}%`);
    text("v-cpu-temp", cpu.temp != null ? `${Math.round(cpu.temp)}°C` : (cpu.freq_mhz ? `${(cpu.freq_mhz / 1000).toFixed(1)}GHz` : ""));
    if (gpu) {
      setBar("m-gpu", gpu.percent); text("v-gpu", gpu.percent != null ? `${Math.round(gpu.percent)}%` : "–");
      text("v-gpu-temp", gpu.temp != null ? `${Math.round(gpu.temp)}°C` : (gpu.mem_total ? fmtBytes(gpu.mem_used) : ""));
    } else { setBar("m-gpu", 0); text("v-gpu", "–"); text("v-gpu-temp", "n/a"); }
    setBar("m-mem", mem.percent); text("v-mem", `${Math.round(mem.percent || 0)}%`);
    text("v-mem-extra", mem.total ? `${fmtBytes(mem.used)}/${fmtBytes(mem.total)}` : "");
    const root = disks[0];
    if (root) { setBar("m-disk", root.percent); text("v-disk", `${Math.round(root.percent)}%`); text("v-disk-extra", `${fmtBytes(root.total - root.used)} free`); }
    $("v-net").innerHTML = `↓ <b>${fmtBytes(net.rx_bps)}/s</b> &nbsp; ↑ <b>${fmtBytes(net.tx_bps)}/s</b>`;
    text("sys-uptime", `up ${fmtDuration(sys.uptime_s)}`);
    text("sys-load", `load ${(sys.load || []).map((x) => x.toFixed(1)).join(" ")}`);
    drawSpark(sys.history || {});
    $("sys-line").innerHTML = [
      `CPU <b>${Math.round(cpu.percent || 0)}%</b>${cpu.temp != null ? ` <b>${Math.round(cpu.temp)}°C</b>` : ""}`,
      gpu ? `GPU <b>${Math.round(gpu.percent || 0)}%</b>${gpu.temp != null ? ` <b>${Math.round(gpu.temp)}°C</b>` : ""}` : "",
      `MEM <b>${Math.round(mem.percent || 0)}%</b>`,
      `↓ <b>${fmtBytes(net.rx_bps)}/s</b>`,
    ].filter(Boolean).map((s) => `<span>${s}</span>`).join("");
  }
  const SPARK_W = 120, SPARK_H = 40;
  function sparkPoints(arr, max) {
    const n = arr.length;
    return arr.map((v, i) => `${(i / (n - 1)) * SPARK_W},${SPARK_H - (Math.max(0, Math.min(max, v)) / max) * SPARK_H}`).join(" ");
  }
  function sparkLine(arr, cls, max) {
    return arr && arr.length > 1 ? `<polyline class="${cls}" points="${sparkPoints(arr, max)}"/>` : "";
  }
  function sparkArea(arr, cls, max) {
    return arr && arr.length > 1 ? `<polygon class="${cls}" points="0,${SPARK_H} ${sparkPoints(arr, max)} ${SPARK_W},${SPARK_H}"/>` : "";
  }
  function drawSpark(hist) {
    $("spark-cpu").innerHTML = sparkArea(hist.cpu, "fill", 100) + sparkLine(hist.gpu, "gpu", 100) + sparkLine(hist.cpu, "cpu", 100);
    const rx = hist.rx || [], tx = hist.tx || [];
    let max = 1;
    for (const v of rx) if (v > max) max = v;
    for (const v of tx) if (v > max) max = v;
    $("spark-net").innerHTML = sparkArea(rx, "fill-rx", max) + sparkLine(rx, "rx", max) + sparkLine(tx, "tx", max);
    text("net-peak", rx.length ? `peak ${fmtBytes(max)}/s` : "");
  }

  // ---------- render root ----------
  function renderErrors(errors) {
    const msgs = Object.entries(errors).filter(([, v]) => v).map(([k, v]) => `${k}: ${String(v).slice(0, 90)}`);
    const el = $("err-line");
    el.hidden = msgs.length === 0;
    setText(el, msgs.join("  ·  "));
  }
  function render(snap) {
    const now = Date.now();
    const errors = snap.errors || {};
    try { renderErrors(errors); } catch (e) { console.error("errors", e); }
    try { renderUsage(snap.usage || {}, errors); } catch (e) { console.error("usage", e); }
    try { renderSessions(snap.sessions || [], snap.sessions_summary || {}, now); } catch (e) { console.error("sessions", e); }
    try { renderSpotify(snap.spotify || {}, errors); } catch (e) { console.error("spotify", e); }
    try { renderSystem(snap.system, errors); } catch (e) { console.error("system", e); }
  }

  // ---------- transport ----------
  let lastEvent = 0;
  function connect() {
    const es = new EventSource("/api/events");
    es.addEventListener("state", (ev) => {
      lastEvent = Date.now();
      $("disconnected").hidden = true;
      render(JSON.parse(ev.data));
    });
    es.onerror = () => { es.close(); setTimeout(connect, 2000); };
  }
  setInterval(() => { if (Date.now() - lastEvent > 5000) $("disconnected").hidden = false; }, 1000);
  fetch("/api/state").then((r) => r.json()).then(render).catch(() => {});
  connect();

  if (location.search.includes("debug")) document.body.classList.add("debug");
})();
