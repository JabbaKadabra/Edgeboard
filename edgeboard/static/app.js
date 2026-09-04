/* edgeboard front-end: renders the snapshot pushed over Server-Sent Events. */
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
  // arms up: shown while a session card is alerting (see renderSessions)
  const ALERT = [
    "..X.....X..",
    "...X...X...",
    "..XXXXXXX..",
    "XXX.XXX.XXX",
    "X.XXXXXXX.X",
    "..XXXXXXX..",
    "..X.....X..",
    "...XX.XX...",
  ];
  const COFFEE = [
    "....s.s....",
    "...s.s.....",
    "...........",
    ".XXXXXXX...",
    ".XXXXXXXXX.",
    ".XXXXXXXX.X",
    "..XXXXXXXX.",
    "...XXXXX...",
  ];

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
  function setClass(el, value) { if (el.className !== value) el.className = value; }
  function text(id, value) { setText($(id), value); }
  function heat(pct) { return pct >= 90 ? "hot" : pct >= 70 ? "warm" : ""; }

  // ---------- mascot ----------
  function pixel(svg, x, y, cls) {
    const r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    r.setAttribute("x", x); r.setAttribute("y", y); r.setAttribute("width", 1); r.setAttribute("height", 1);
    if (cls) r.setAttribute("class", cls);
    svg.appendChild(r);
  }
  // grid: "X" = body pixel, "s" = steam pixel; eyes are drawn on top in the background colour
  function drawMascot(svg, grid, eyes) {
    svg.innerHTML = "";
    grid.forEach((row, y) => {
      [...row].forEach((ch, x) => {
        if (ch === "X") pixel(svg, x, y);
        else if (ch === "s") pixel(svg, x, y, "steam");
      });
    });
    (eyes || []).forEach(([y, x]) => pixel(svg, x, y, "eye"));
  }
  // Sessions alerting right now (id -> status at the alert); the mascot raises
  // its arms while there are any. Declared here so the pomodoro can redraw it.
  const alerted = new Map();
  let mascotAlerting = null;  // redraw only on change, or the blink animation restarts every second
  function drawClaude(force) {
    const svg = $("mascot");
    const alerting = alerted.size > 0;
    if (!force && alerting === mascotAlerting) return;
    mascotAlerting = alerting;
    drawMascot(svg, alerting ? ALERT : MASCOT, EYES);
    svg.classList.toggle("attention", alerting);
  }
  drawClaude(true);

  // ---------- clock ----------
  function tickClock() {
    const d = new Date();
    const hm = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }).replace(/\s?[AP]M$/i, "");
    text("clock-hm", hm);
    text("clock-s", String(d.getSeconds()).padStart(2, "0"));
    text("clock-date", d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + (/[AP]M/i.test(d.toLocaleTimeString()) ? (d.getHours() < 12 ? " AM" : " PM") : ""));
  }

  // ---------- pomodoro ----------
  // Tapping the mascot runs one loop: off -> focus (25 min) -> break (5 min, coffee cup) -> off.
  // A tap advances early; reaching zero advances automatically. End times are wall-clock so a stalled tab stays right.
  const POMO_FOCUS_MS = 25 * 60 * 1000;
  const POMO_BREAK_MS = 5 * 60 * 1000;
  const pomo = { phase: "off", endsAt: 0 };
  function flashMascot() {
    const svg = $("mascot");
    svg.classList.remove("flash");
    void svg.getBoundingClientRect();  // restart the animation if it is still running
    svg.classList.add("flash");
  }
  // Synthesized WebAudio chimes, one per phase entered: a blip to start, two rising notes for the
  // break, three falling notes when the loop is done. The context is created on the first tap (a
  // user gesture) so the automatic transitions at zero can sound; kiosk.sh also lifts the autoplay policy.
  const CHIMES = {
    focus: [[880, 0, 0.1]],
    break: [[660, 0, 0.14], [990, 0.16, 0.24]],
    off: [[990, 0, 0.14], [784, 0.16, 0.14], [523, 0.32, 0.36]],
    alert: [[1046, 0, 0.12], [1318, 0.15, 0.28]],  // a session needs you (EDGEBOARD_ALERT_SOUND)
  };
  let audio = null;
  function unlockAudio() {
    if (audio) return;
    try { audio = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { audio = null; }
  }
  function chime(phase) {
    if (!audio) return;
    const play = () => {
      const t0 = audio.currentTime + 0.02;
      (CHIMES[phase] || []).forEach(([freq, at, dur]) => {
        const osc = audio.createOscillator(), gain = audio.createGain();
        osc.type = "triangle"; osc.frequency.value = freq;
        gain.gain.setValueAtTime(0.0001, t0 + at);
        gain.gain.linearRampToValueAtTime(0.25, t0 + at + 0.01);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + dur);
        osc.connect(gain).connect(audio.destination);
        osc.start(t0 + at); osc.stop(t0 + at + dur + 0.05);
      });
    };
    if (audio.state === "running") play();
    else audio.resume().then(play).catch(() => {});
  }
  function advancePomo() {
    const from = pomo.phase;
    if (from === "off") { pomo.phase = "focus"; pomo.endsAt = Date.now() + POMO_FOCUS_MS; }
    else if (from === "focus") { pomo.phase = "break"; pomo.endsAt = Date.now() + POMO_BREAK_MS; }
    else { pomo.phase = "off"; pomo.endsAt = 0; }
    if (pomo.phase === "break") { $("mascot").classList.remove("attention"); drawMascot($("mascot"), COFFEE); }
    else if (from === "break") drawClaude(true);
    if (from !== "off") flashMascot();
    chime(pomo.phase);
    tickPomo();
  }
  function tickPomo() {
    const el = $("pomo");
    if (pomo.phase === "off") { el.hidden = true; return; }
    const remaining = pomo.endsAt - Date.now();
    if (remaining <= 0) { advancePomo(); return; }
    text("pomo-label", pomo.phase);
    text("pomo-time", fmtClockSecs(Math.ceil(remaining / 1000)));
    el.classList.toggle("break", pomo.phase === "break");
    el.hidden = false;
  }
  $("mascot-wrap").addEventListener("click", () => { unlockAudio(); advancePomo(); });
  $("mascot").addEventListener("animationend", (ev) => { if (ev.animationName === "flash") ev.target.classList.remove("flash"); });

  tickClock();
  tickPomo();
  setInterval(() => { tickClock(); tickPomo(); }, 1000);

  // ---------- usage ----------
  // One .limit node per window key, updated in place (like the session cards)
  // so the bar's width transition plays and nothing is re-created every second.
  const LIMIT_HTML = `
        <div class="limit-label"></div>
        <div class="limit-pct"></div>
        <div class="bar"><div class="bar-fill"></div></div>
        <div class="limit-reset"></div>
        <div class="limit-pace" hidden></div>`;
  const limitNodes = new Map();
  function renderLimits(windows, errors) {
    const limits = $("limits");
    if (!windows.length) {
      limitNodes.clear();
      const msg = errors.usage ? "usage unavailable" : "waiting for usage data…";
      if (limits.dataset.empty !== msg) { limits.dataset.empty = msg; limits.innerHTML = `<div class="muted">${msg}</div>`; }
      return;
    }
    if (limits.dataset.empty) { delete limits.dataset.empty; limits.textContent = ""; }
    const seen = new Set();
    let prev = null;
    windows.forEach((w) => {
      seen.add(w.key);
      let el = limitNodes.get(w.key);
      if (!el) {
        el = document.createElement("div");
        el.className = "limit";
        el.innerHTML = LIMIT_HTML;
        limitNodes.set(w.key, el);
      }
      updateLimit(el, w);
      const want = prev ? prev.nextElementSibling : limits.firstElementChild;
      if (want !== el) limits.insertBefore(el, want);
      prev = el;
    });
    for (const [key, el] of limitNodes) {
      if (!seen.has(key)) { el.remove(); limitNodes.delete(key); }
    }
  }
  function updateLimit(el, w) {
    const pct = w.utilization;
    const pctClass = pct == null ? "na" : pct >= 90 ? "high" : pct >= 70 ? "mid" : "";
    const pctText = pct == null ? (w.tokens != null ? fmtTokens(w.tokens) + " tok" : "n/a") : Math.round(pct) + "%";
    setText(el.querySelector(".limit-label"), w.label);
    const pctEl = el.querySelector(".limit-pct");
    setText(pctEl, pctText);
    setClass(pctEl, "limit-pct" + (pctClass ? " " + pctClass : ""));
    const fill = el.querySelector(".bar-fill");
    setClass(fill, "bar-fill" + (heat(pct || 0) ? " " + heat(pct || 0) : ""));
    fill.style.width = Math.max(0, Math.min(100, pct || 0)) + "%";
    setText(el.querySelector(".limit-reset"), w.seconds_to_reset != null ? `resets in ${fmtDuration(w.seconds_to_reset)} · ${fmtResetAt(w.resets_at)}` : "no activity in window");
    const pace = paceLine(w), paceEl = el.querySelector(".limit-pace");
    paceEl.hidden = !pace;
    if (pace) { setText(paceEl, pace.text); setClass(paceEl, "limit-pace" + (pace.warn ? " warn" : "")); }
  }
  function renderUsage(usage, errors) {
    const windows = usage.windows || [];
    const source = usage.source;
    text("usage-source", source === "api" ? (usage.stale ? "stale" : "by plan") : source === "local" ? "estimated" : source === "demo" ? "demo" : "");

    renderLimits(windows, errors);

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

  // Server-side pace projection: warn when the window fills before it resets,
  // reassure when it does not, say nothing while the pace is ~0.
  function paceLine(w) {
    if (!w.rate_per_hour || !w.projected_full_at) return null;
    const full = new Date(w.projected_full_at).getTime();
    const reset = w.resets_at ? new Date(w.resets_at).getTime() : Infinity;
    if (full < reset) return { warn: true, text: `at this pace 100% at ${fmtResetAt(w.projected_full_at)}` };
    return { warn: false, text: "safe until reset" };
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
  // The body between the project line and the detail line: the task list's
  // progress (from ~/.claude/tasks) and Claude's last reply, so a card says what
  // is going on without opening the overlay. The foot's ctx tag is a gauge
  // against the model's window with the compaction count.
  const CARD_HTML = `
      <div class="card-top"><span class="pill"></span><span class="muted card-ago"></span></div>
      <div class="card-title"></div>
      <div class="card-proj"><b></b><span class="card-branch"></span></div>
      <div class="card-body">
        <div class="card-tasks" hidden><span class="bar bar-mini"><span class="bar-fill"></span></span><span class="card-tasks-text"></span></div>
        <div class="card-reply" hidden></div>
      </div>
      <div class="card-detail"></div>
      <div class="card-actions" hidden></div>
      <div class="card-foot"><span class="tag model" hidden></span><span class="tag card-ctx"><span class="card-ctx-text"></span><span class="bar bar-mini"><span class="bar-fill"></span></span><span class="card-ctx-pct"></span><span class="card-compact" hidden></span></span><span class="tag card-agents" hidden></span><span class="tag card-msgs"></span></div>`;
  const cardNodes = new Map();
  // Action row of a card (and the preset row of the overlay). Rebuilt only when
  // its key changes so a tap never lands on a freshly re-created button.
  //   attention + answerable question: the options of a single-choice question (or one
  //   "answer…" button that opens the overlay for multi-choice / several questions) + "terminal";
  //   a question only read from the transcript (no hook waiting) gets no buttons
  //   idle + can_send: the first ``limit`` presets
  function actionButtons(s, presets, limit) {
    const buttons = [];
    if (s.status === "attention" && s.question && s.question.answerable) {
      const qs = s.question.questions || [];
      if (qs.length === 1 && !qs[0].multi && qs[0].options.length) {
        qs[0].options.forEach((label) => buttons.push({ text: label, act: "answer", q: qs[0].question, label }));
      } else {
        buttons.push({ text: "answer…", act: "open" });
      }
      buttons.push({ text: "terminal", act: "pass", cls: "terminal" });
    } else if (s.status === "idle" && s.can_send) {
      presets.slice(0, limit).forEach((p) => buttons.push({ text: p.label, act: "send", value: p.text }));
    }
    return buttons;
  }
  function updateActions(box, s, presets, limit) {
    const buttons = actionButtons(s, presets, limit);
    const key = JSON.stringify(buttons);
    if (box.dataset.key !== key) {
      box.dataset.key = key;
      box.innerHTML = "";
      buttons.forEach((b) => {
        const btn = document.createElement("button");
        btn.textContent = b.text;
        btn.className = "act" + (b.cls ? " " + b.cls : "");
        btn.dataset.act = b.act;
        if (b.q != null) btn.dataset.q = b.q;
        if (b.label != null) btn.dataset.label = b.label;
        if (b.value != null) btn.dataset.value = b.value;
        box.appendChild(btn);
      });
    }
    box.hidden = buttons.length === 0;
  }
  function updateCard(el, s, now, alert, presets) {
    const cls = "card " + s.status + (alert ? " alert" : "");
    if (el.className !== cls) el.className = cls;
    setText(el.querySelector(".pill"), s.status);
    setText(el.querySelector(".card-ago"), fmtAgo(s.last_activity, now));
    setText(el.querySelector(".card-title"), s.name);
    setText(el.querySelector(".card-proj b"), s.project);
    setText(el.querySelector(".card-branch"), s.branch ? "@" + s.branch : "");
    // a pending question replaces the detail line with what Claude is asking
    const q = s.question && s.question.questions && s.question.questions[0];
    setText(el.querySelector(".card-detail"), q ? q.question : s.detail);
    updateActions(el.querySelector(".card-actions"), s, presets, 4);
    const tasks = el.querySelector(".card-tasks");
    tasks.hidden = !s.tasks;
    if (s.tasks) {
      tasks.querySelector(".bar-fill").style.width = (s.tasks.total ? (100 * s.tasks.done) / s.tasks.total : 0) + "%";
      setText(tasks.querySelector(".card-tasks-text"), tasksLabel(s.tasks));
    }
    // the reply gives way to the question (already on the detail line)
    const reply = el.querySelector(".card-reply");
    reply.hidden = !!q || !s.last_reply;
    setText(reply, reply.hidden ? "" : s.last_reply);
    const model = el.querySelector(".tag.model");
    setText(model, s.model || "");
    model.hidden = !s.model;
    const ctx = el.querySelector(".card-ctx"), pct = s.context_pct || 0;
    setText(ctx.querySelector(".card-ctx-text"), "ctx " + fmtTokens(s.context_tokens));
    const fill = ctx.querySelector(".bar-fill");
    setClass(fill, "bar-fill" + (ctxHeat(pct) ? " " + ctxHeat(pct) : ""));
    fill.style.width = Math.min(100, pct) + "%";
    setText(ctx.querySelector(".card-ctx-pct"), pct + "%");
    const compact = ctx.querySelector(".card-compact");
    compact.hidden = !s.compactions;
    setText(compact, s.compactions ? `⟲${s.compactions}` : "");
    compact.title = s.compactions ? compactLabel(s) : "";
    setText(el.querySelector(".card-msgs"), `${s.messages} msgs`);
    const agents = el.querySelector(".card-agents");
    agents.hidden = !s.agents;
    setText(agents, agentsLabel(s));
    agents.classList.toggle("active", (s.active_agents || 0) > 0);
  }
  // "3/7 tasks · reviewing the access rules"; "5/5 tasks" once everything is done
  function tasksLabel(t) {
    return `${t.done}/${t.total} tasks` + (t.current ? ` · ${t.current}` : "");
  }
  // context gauge colours: amber from settings.context_warn - 10, red from context_warn
  let contextWarn = 80;
  function ctxHeat(pct) { return pct >= contextWarn ? "hot" : pct >= contextWarn - 10 ? "warm" : ""; }
  function compactLabel(s) {
    const when = s.last_compact_at ? `, last ${fmtTime(s.last_compact_at)}` : "";
    return `compacted ${s.compactions}×${s.last_compact_trigger ? ` (${s.last_compact_trigger})` : ""}${when}`;
  }
  // "2/3 agents" while some subagents are still writing, "3 agents" once they are quiet
  function agentsLabel(s) {
    if (!s.agents) return "";
    return (s.active_agents ? `${s.active_agents}/${s.agents}` : `${s.agents}`) + (s.agents === 1 ? " agent" : " agents");
  }
  // Attention alerts: a card that goes working -> idle (Claude finished) or -> attention
  // (permission prompt, question) flashes and stays highlighted until its status changes
  // again. Detected against the previous snapshot per session id; a first sighting never alerts.
  let prevStatus = new Map();
  function needsYou(before, status) {
    if (before == null || before === status) return false;
    return status === "attention" || (status === "idle" && before === "working");
  }
  let lastSessions = [], lastPresets = [];
  function renderSessions(sessions, summary, now, settings) {
    const box = $("sessions");
    lastSessions = sessions;
    lastPresets = Array.isArray(settings.presets) ? settings.presets : [];
    if (typeof settings.context_warn === "number") contextWarn = settings.context_warn;
    let fresh = 0;
    sessions.forEach((s) => {
      if (needsYou(prevStatus.get(s.id), s.status)) { alerted.set(s.id, s.status); fresh++; }
      else if (alerted.get(s.id) !== s.status) alerted.delete(s.id);
    });
    prevStatus = new Map(sessions.map((s) => [s.id, s.status]));
    for (const id of [...alerted.keys()]) { if (!prevStatus.has(id)) alerted.delete(id); }
    if (pomo.phase !== "break") drawClaude();
    if (fresh && settings.alert_sound) { unlockAudio(); chime("alert"); }
    const parts = [`${summary.today || 0} today`, `${summary.done || 0} done`, `${summary.working || 0} working`];
    if (summary.attention) parts.push(`${summary.attention} need you`);
    text("sessions-summary", parts.join(" · "));
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
      updateCard(el, s, now, alerted.has(s.id), lastPresets);
      const want = prev ? prev.nextElementSibling : box.firstElementChild;
      if (want !== el) box.insertBefore(el, want);  // only moves nodes that are out of order
      prev = el;
    });
    for (const [id, el] of cardNodes) {
      if (!seen.has(id)) { el.remove(); cardNodes.delete(id); }
    }
    renderOverlay(sessions, now);
  }

  // Tapping a card opens a full-height overlay with everything the card truncates.
  // It stays live (refilled from each snapshot) and closes on a backdrop tap,
  // after 20 s, or when the session leaves the snapshot.
  const OVERLAY_MS = 20 * 1000;
  let overlayId = null, overlayTimer = 0;
  function openOverlay(id) {
    overlayId = id;
    clearTimeout(overlayTimer);
    overlayTimer = setTimeout(closeOverlay, OVERLAY_MS);
    renderOverlay(lastSessions, Date.now());
  }
  function closeOverlay() {
    overlayId = null;
    clearTimeout(overlayTimer);
    $("overlay").hidden = true;
  }
  function fmtTime(iso) {
    return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  }
  function renderOverlay(sessions, now) {
    if (overlayId == null) return;
    const s = sessions.find((x) => x.id === overlayId);
    if (!s) { closeOverlay(); return; }
    const card = $("overlay-card");
    const cls = "overlay-card " + s.status;
    if (card.className !== cls) card.className = cls;
    text("ov-pill", s.status);
    text("ov-detail", s.detail || "");
    text("ov-ago", fmtAgo(s.last_activity, now) + " ago");
    text("ov-title", s.name || "");
    text("ov-cwd", s.cwd || "");
    text("ov-branch", s.branch || "–");
    text("ov-model", s.model || "–");
    const started = s.started_at ? new Date(s.started_at).getTime() : 0;
    text("ov-started", started ? `${fmtTime(s.started_at)} · running ${fmtDuration((now - started) / 1000)}` : "–");
    text("ov-activity", s.last_activity ? `${fmtTime(s.last_activity)} · ${fmtAgo(s.last_activity, now)} ago` : "–");
    text("ov-msgs", s.messages || 0);
    text("ov-ctx", `${fmtTokens(s.context_tokens)} / ${fmtTokens(s.context_window)} · ${s.context_pct || 0}%` + (s.compactions ? ` · ${compactLabel(s)}` : ""));
    text("ov-tasks", s.tasks ? `${s.tasks.done}/${s.tasks.total}` + (s.tasks.current ? ` · ${s.tasks.current}` : " · all done") : "none");
    text("ov-agents", agentsLabel(s) || "none");
    text("ov-mode", s.permission_mode || "–");
    text("ov-waiting", s.waiting_since ? `${fmtAgo(s.waiting_since, now)} · since ${fmtTime(s.waiting_since)}` : "–");
    text("ov-prompt", s.last_prompt || "–");
    text("ov-reply", s.last_reply || "–");
    renderQuestions(s);
    const actions = $("ov-actions");
    actions.hidden = !s.can_send;
    if (s.can_send) updateActions($("ov-presets"), { status: "idle", can_send: true }, lastPresets, 99);
    $("overlay").hidden = false;
  }
  // The full question set: every question with its options as toggles (multi-choice
  // allowed), sent together. Rebuilt when the question changes, so selections survive snapshots.
  // A question nothing waits for (read from the transcript, or the hook gave up) is
  // listed read-only with a note to answer it in the terminal.
  function renderQuestions(s) {
    const box = $("ov-questions");
    const q = s.status === "attention" ? s.question : null;
    const key = q ? `${q.tool_use_id}:${q.answerable ? 1 : 0}` : "";
    box.hidden = !q;
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.innerHTML = "";
    if (!q) return;
    (q.questions || []).forEach((item, i) => {
      const wrap = document.createElement("div");
      wrap.className = "ov-q";
      wrap.dataset.index = i;
      wrap.dataset.multi = item.multi ? "1" : "";
      wrap.innerHTML = `<div class="ov-q-text">${item.header ? `<b>${escapeHtml(item.header)}</b>` : ""}${escapeHtml(item.question)}</div><div class="ov-q-options"></div>`;
      const opts = wrap.querySelector(".ov-q-options");
      item.options.forEach((label) => {
        const opt = document.createElement(q.answerable ? "button" : "span");
        opt.className = q.answerable ? "act" : "ov-q-option";
        opt.textContent = label;
        if (q.answerable) { opt.dataset.act = "toggle"; opt.dataset.label = label; }
        opts.appendChild(opt);
      });
      box.appendChild(wrap);
    });
    const submit = document.createElement("div");
    submit.className = "ov-q-submit";
    submit.innerHTML = q.answerable
      ? `<button class="act" data-act="answer-all">send answers</button><button class="act terminal" data-act="pass">answer in the terminal</button>`
      : `<span class="muted">the panel cannot answer this one: answer it in the terminal (install the hooks, see the README)</span>`;
    box.appendChild(submit);
  }
  // Answers from the overlay: the selected labels per question (comma-joined for
  // multi-choice); the typed text fills every question without a selection.
  function collectAnswers(s) {
    const answers = {};
    const typed = $("ov-input").value.trim();
    for (const wrap of $("ov-questions").querySelectorAll(".ov-q")) {
      const item = s.question.questions[Number(wrap.dataset.index)];
      const picked = [...wrap.querySelectorAll("button.sel")].map((b) => b.dataset.label);
      if (picked.length) answers[item.question] = picked.join(", ");
      else if (typed) answers[item.question] = typed;
      else return null;
    }
    return answers;
  }

  // Everything a button on a card or in the overlay can do. Buttons stay busy
  // until the server answers, then show "sent" briefly; failures go to the red line.
  let localError = null, localErrorUntil = 0;
  function showError(message) {
    localError = message; localErrorUntil = Date.now() + 8000;
    renderErrors({});
  }
  async function postSession(id, action, body) {
    const r = await fetch(`/api/sessions/${encodeURIComponent(id)}/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) {
      let detail = `HTTP ${r.status}`;
      try { const data = await r.json(); if (data.detail) detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail); } catch (e) { /* keep the status */ }
      throw new Error(detail);
    }
    return r.json();
  }
  async function runAction(btn, s) {
    const act = btn.dataset.act;
    if (act === "open") { openOverlay(s.id); return; }
    if (act === "toggle") {
      const wrap = btn.closest(".ov-q");
      if (!wrap.dataset.multi) wrap.querySelectorAll("button.sel").forEach((b) => { if (b !== btn) b.classList.remove("sel"); });
      btn.classList.toggle("sel");
      return;
    }
    let action, body;
    if (act === "answer") { action = "answer"; body = { tool_use_id: s.question.tool_use_id, answers: { [btn.dataset.q]: btn.dataset.label } }; }
    else if (act === "answer-all") {
      const answers = collectAnswers(s);
      if (!answers) { showError("pick an option (or type an answer) for every question"); return; }
      action = "answer"; body = { tool_use_id: s.question.tool_use_id, answers };
    }
    else if (act === "pass") { action = "answer"; body = { tool_use_id: s.question.tool_use_id, pass: true }; }
    else if (act === "send") { action = "send"; body = { text: btn.dataset.value }; }
    else if (act === "compose") {
      const typed = $("ov-input").value.trim();
      if (!typed) return;
      if (s.status === "attention" && s.question && s.question.answerable) { action = "answer"; body = { tool_use_id: s.question.tool_use_id, answers: collectAnswers(s) }; }
      else { action = "send"; body = { text: typed }; }
    }
    else return;
    btn.classList.add("busy");
    try {
      await postSession(s.id, action, body);
      btn.classList.remove("busy"); btn.classList.add("sent");
      if (act === "compose") $("ov-input").value = "";
      setTimeout(() => btn.classList.remove("sent"), 3000);
    } catch (e) {
      btn.classList.remove("busy");
      showError(`${s.name || s.id}: ${e.message}`);
    }
  }
  function sessionOf(el) {
    const id = el.closest("[data-id]") ? el.closest("[data-id]").dataset.id : overlayId;
    return lastSessions.find((x) => x.id === id);
  }
  $("sessions").addEventListener("click", (ev) => {
    const btn = ev.target.closest("button");
    if (btn) {  // an action button: never open the overlay for it
      ev.stopPropagation();
      const s = sessionOf(btn);
      if (s) runAction(btn, s);
      return;
    }
    const card = ev.target.closest(".card");
    if (card) openOverlay(card.dataset.id);
  });
  $("overlay").addEventListener("click", (ev) => {
    if (ev.target === ev.currentTarget) { closeOverlay(); return; }
    // any tap inside keeps the overlay open for another 20 s
    clearTimeout(overlayTimer);
    overlayTimer = setTimeout(closeOverlay, OVERLAY_MS);
    const btn = ev.target.closest("button");
    const s = lastSessions.find((x) => x.id === overlayId);
    if (!btn || !s) return;
    if (btn.id === "ov-send") { btn.dataset.act = "compose"; }
    runAction(btn, s);
  });
  $("ov-input").addEventListener("keydown", (ev) => { if (ev.key === "Enter") $("ov-send").click(); });
  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // ---------- spotify ----------
  let np = { running: false, position_s: 0, length_s: 0, status: "", updatedAt: 0 };
  function renderSpotify(sp, errors) {
    const panel = $("spotify");
    np = Object.assign({}, sp, { updatedAt: Date.now() });
    panel.classList.toggle("offline", !sp.running);
    panel.classList.toggle("paused", sp.status !== "Playing");
    text("np-state", sp.running ? (sp.status || "").toLowerCase() + (sp.shuffle ? " · shuffle" : "") : "offline");
    if (!sp.running) {
      text("np-title", sp.available === false ? "playerctl missing" : "Spotify not running");
      text("np-artist", ""); text("np-album", "");
      $("art").hidden = true; $("art-placeholder").hidden = false;
      $("np-bar").style.width = "0%";
      text("np-pos", "0:00"); text("np-len", "0:00");
      text("btn-play", "▶");
      return;
    }
    setVolumeSlider(sp.volume);
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
  // Up next: from the Spotify Web API (MPRIS has no queue). Rows are rebuilt
  // only when the list actually changes so the panel does not flicker.
  const QUEUE_HINT = 'no queue: run <code>scripts/spotify_auth.py</code> once to show upcoming tracks';
  let queueKey = "";
  function renderQueue(q, sp, errors) {
    const tracks = (q && q.tracks) || [];
    const box = $("queue"), empty = $("queue-empty");
    text("queue-count", tracks.length ? `${tracks.length} · ${fmtDuration(tracks.reduce((a, t) => a + (t.length_s || 0), 0))}` : "");
    const key = JSON.stringify(tracks.map((t) => [t.title, t.artist, t.length_s]));
    if (key !== queueKey) {
      queueKey = key;
      box.innerHTML = tracks.map((t, i) => `<li data-index="${i}">
        <span class="q-n">${i + 1}</span>
        <span class="q-main"><span class="q-title">${escapeHtml(t.title)}</span><br><span class="q-artist">${escapeHtml(t.artist)}${t.album ? " · " + escapeHtml(t.album) : ""}</span></span>
        <span class="q-len">${t.length_s ? fmtClockSecs(t.length_s) : ""}</span></li>`).join("");
    }
    if (!sp.running) { empty.hidden = true; return; }
    empty.hidden = tracks.length > 0;
    if (tracks.length) return;
    const msg = !q || !q.configured ? QUEUE_HINT : errors.spotify ? escapeHtml(errors.spotify) : "queue is empty";
    if (empty.innerHTML !== msg) empty.innerHTML = msg;
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
  async function postSpotify(path, body) {
    try {
      const r = await fetch(`/api/spotify/${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const data = await r.json();
      if (data.spotify) renderSpotify(data.spotify, {});
      return data;
    } catch (e) { return null; /* the next snapshot will correct the view */ }
  }

  // Both bars share one press/drag/release handler: the fraction under the
  // finger is reported while pressed and committed on release.
  function dragBar(bar, { onMove, onEnd, enabled }) {
    let fraction = null;
    const at = (ev) => {
      const r = bar.getBoundingClientRect();
      return r.width ? Math.min(1, Math.max(0, (ev.clientX - r.left) / r.width)) : 0;
    };
    bar.addEventListener("pointerdown", (ev) => {
      if (!enabled()) return;
      bar.setPointerCapture(ev.pointerId);
      bar.classList.add("seeking");
      onMove(fraction = at(ev));
    });
    bar.addEventListener("pointermove", (ev) => { if (fraction !== null) onMove(fraction = at(ev)); });
    const finish = (ev, commit) => {
      if (fraction === null) return;
      const f = commit ? at(ev) : fraction;
      fraction = null;
      bar.classList.remove("seeking");
      onEnd(f, commit);
    };
    bar.addEventListener("pointerup", (ev) => finish(ev, true));
    bar.addEventListener("pointercancel", (ev) => finish(ev, false));
    return () => fraction !== null;  // "is the finger down?"
  }

  // Seek: the target time rides along with the finger; the fill moves on release.
  const seekLabel = $("np-seek");
  dragBar($("np-progress"), {
    enabled: () => np.running && np.length_s > 0,
    onMove: (f) => {
      seekLabel.hidden = false;
      seekLabel.style.left = Math.min(Math.max(f * 100, 6), 94) + "%";
      seekLabel.textContent = fmtClockSecs(f * (np.length_s || 0));
    },
    onEnd: (f, commit) => {
      seekLabel.hidden = true;
      if (!commit) return;
      np.position_s = f * (np.length_s || 0); np.updatedAt = Date.now();
      tickProgress();
      postSpotify("seek", { fraction: f });
    },
  });

  // Volume: the bar is the source of truth while the finger is on it, so
  // snapshots do not yank the fill back mid-drag.
  function paintVolume(v) {
    $("np-volume-fill").style.width = v * 100 + "%";
    text("np-volume-pct", Math.round(v * 100) + "%");
  }
  const volumeDragging = dragBar($("np-volume"), {
    enabled: () => np.running,
    onMove: paintVolume,
    onEnd: (f, commit) => { if (commit) { paintVolume(f); postSpotify("volume", { volume: f }); } },
  });
  function setVolumeSlider(v) {
    if (!volumeDragging() && typeof v === "number") paintVolume(v);
  }

  // Tap a queued track to skip to it: MPRIS has no "jump", so the server
  // presses next index+1 times. The tapped row and everything above it go
  // away at once; the reply carries the trimmed queue so the page and the
  // next snapshots agree.
  $("queue").addEventListener("click", async (ev) => {
    const li = ev.target.closest("li[data-index]");
    if (!li || !np.running) return;
    const index = Number(li.dataset.index);
    for (const row of [...li.parentElement.children]) { if (Number(row.dataset.index) <= index) row.remove(); }
    queueKey = "";  // whatever arrives next rebuilds the list, even if the request fails
    const data = await postSpotify("skip", { index });
    if (data && data.spotify_queue) renderQueue(data.spotify_queue, data.spotify || np, {});
  });

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
    if (!sys) return;
    const cpu = sys.cpu || {}, mem = sys.mem || {}, gpu = sys.gpu, disks = sys.disks || [], net = sys.net || {};
    text("sys-uptime", `up ${fmtDuration(sys.uptime_s)}`);
    text("sys-load", `load ${(sys.load || []).map((x) => x.toFixed(1)).join(" ")}`);
    drawSpark(sys.history || {});
    // Current values live in the one-line summary; the trace below is history only.
    const root = disks[0];
    $("sys-line").innerHTML = [
      `CPU <b>${Math.round(cpu.percent || 0)}%</b>${cpu.temp != null ? ` <b>${Math.round(cpu.temp)}°C</b>` : ""}`,
      gpu ? `GPU <b>${Math.round(gpu.percent || 0)}%</b>${gpu.temp != null ? ` <b>${Math.round(gpu.temp)}°C</b>` : ""}` : "",
      `MEM <b>${Math.round(mem.percent || 0)}%</b>`,
      root ? `DISK <b>${Math.round(root.percent)}%</b>` : "",
      `↓ <b>${fmtBytes(net.rx_bps)}/s</b>`,
      `↑ <b>${fmtBytes(net.tx_bps)}/s</b>`,
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
  }

  // ---------- render root ----------
  function renderErrors(errors) {
    if (localError && Date.now() < localErrorUntil) errors = { ...errors, panel: localError };
    const msgs = Object.entries(errors).filter(([, v]) => v).map(([k, v]) => `${k}: ${String(v).slice(0, 90)}`);
    const el = $("err-line");
    el.hidden = msgs.length === 0;
    setText(el, msgs.join("  ·  "));
  }
  // The server's build id (version + hash of the page files). A change means a
  // deploy happened under a page that may run for weeks: reload rather than
  // render new snapshots with old code.
  let build = null;
  function render(snap) {
    if (snap.version) {
      if (build !== null && snap.version !== build) { location.reload(); return; }
      build = snap.version;
    }
    const now = Date.now();
    const errors = snap.errors || {};
    try { renderErrors(errors); } catch (e) { console.error("errors", e); }
    try { renderUsage(snap.usage || {}, errors); } catch (e) { console.error("usage", e); }
    try { renderSessions(snap.sessions || [], snap.sessions_summary || {}, now, snap.settings || {}); } catch (e) { console.error("sessions", e); }
    try { renderSpotify(snap.spotify || {}, errors); } catch (e) { console.error("spotify", e); }
    try { renderQueue(snap.spotify_queue, snap.spotify || {}, errors); } catch (e) { console.error("queue", e); }
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

  const params = new URLSearchParams(location.search);
  if (params.has("kiosk")) document.body.classList.add("kiosk");
  if (params.has("debug")) document.body.classList.add("debug");
})();
