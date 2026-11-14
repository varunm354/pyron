"use strict";

const POLL_MS = 1000;
const HISTORY_REFRESH_MS = 5000;
const FETCH_TIMEOUT_MS = 3000;
const CHART_MAX_POINTS = 120;
const ADC_MAX = 4095;

const STATE_LABELS = { ok: "OK", warn: "WARNING", alarm: "ALARM" };
const DAMPER_LABELS = { open: "Open", half: "Half", closed: "Closed" };
const MODE_LABELS = { nasa: "NASA-inspired response", earth: "Ventilation-on comparison" };

const $ = (id) => document.getElementById(id);
const fmtNum = (n) => Number(n).toLocaleString("en-US");

function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function setIndicator(id, tone, text, title) {
  const el = $(id);
  el.dataset.tone = tone;
  setText($(`${id}-text`), text);
  el.title = title || text;
}

function formatAge(seconds) {
  if (seconds < 1) return "just now";
  if (seconds < 90) return `${Math.round(seconds)} s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
  return `${(seconds / 3600).toFixed(1)} h ago`;
}

function formatUptime(ms) {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  return h ? `${h}h ${m}m ${sec}s` : m ? `${m}m ${sec}s` : `${sec}s`;
}

async function fetchJSON(url, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  try {
    const resp = await fetch(url, {
      ...options,
      cache: "no-store",
      signal: controller.signal,
      headers: { Accept: "application/json", ...(options.headers || {}) },
    });
    if (!resp.ok) {
      let detail = "";
      try {
        const body = await resp.json();
        detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch (_) { /* non-JSON error body */ }
      throw new Error(`HTTP ${resp.status}${detail ? ` — ${detail}` : ""}`);
    }
    return await resp.json();
  } catch (err) {
    if (err.name === "AbortError") throw new Error("request timed out");
    if (err instanceof TypeError) throw new Error("network error (server unreachable)");
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

/* ------------------------------------------------------------------ */
/* Chart                                                               */
/* ------------------------------------------------------------------ */

const SVG_NS = "http://www.w3.org/2000/svg";
const CHART = { w: 640, h: 240, left: 44, right: 78, top: 12, bottom: 24 };

function svgEl(tag, attrs, text) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}

function drawChart(svg, values, { thresholds, maxPoints, stale, emptyText }) {
  const { w, h, left, right, top, bottom } = CHART;
  const plotW = w - left - right;
  const plotH = h - top - bottom;
  const y = (v) => top + plotH - (Math.min(Math.max(v, 0), ADC_MAX) / ADC_MAX) * plotH;
  const x = (i) => left + (maxPoints <= 1 ? 0 : (i / (maxPoints - 1)) * plotW);

  const frag = document.createDocumentFragment();
  const defs = svgEl("defs", {});
  const grad = svgEl("linearGradient", { id: `areaFill`, x1: 0, y1: 0, x2: 0, y2: 1 });
  grad.append(
    svgEl("stop", { offset: "0%", "stop-color": "#ff7a1a", "stop-opacity": 0.28 }),
    svgEl("stop", { offset: "100%", "stop-color": "#ff7a1a", "stop-opacity": 0 }),
  );
  defs.append(grad);
  frag.append(defs);

  for (const v of [0, 1024, 2048, 3072, ADC_MAX]) {
    frag.append(svgEl("line", { class: "grid-line", x1: left, x2: w - right, y1: y(v), y2: y(v) }));
    frag.append(svgEl("text", { class: "axis-label", x: left - 6, y: y(v) + 4, "text-anchor": "end" }, String(v)));
  }

  const thLines = [
    ["clear", "th-clear", "#34d17c"],
    ["warn", "th-warn", "#f5c542"],
    ["alarm", "th-alarm", "#ff5252"],
  ];
  if (thresholds) {
    for (const [key, cls, color] of thLines) {
      const v = thresholds[key];
      if (typeof v !== "number") continue;
      frag.append(svgEl("line", { class: `th ${cls}`, x1: left, x2: w - right, y1: y(v), y2: y(v) }));
      frag.append(svgEl("text", { class: "th-label", x: w - right + 6, y: y(v) + 4, fill: color }, `${key} ${v}`));
    }
  }

  if (values.length) {
    const pts = values.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`);
    const lastX = x(values.length - 1).toFixed(1);
    frag.append(svgEl("path", { class: "area", d: `M${x(0)},${y(0)} L${pts.join(" L")} L${lastX},${y(0)} Z` }));
    frag.append(svgEl("polyline", { class: "series", points: pts.join(" ") }));
    frag.append(svgEl("circle", { class: "last-dot", cx: lastX, cy: y(values[values.length - 1]), r: 4 }));
  } else {
    frag.append(svgEl("text", { class: "empty-label", x: left + plotW / 2, y: top + plotH * 0.2, "text-anchor": "middle" }, emptyText));
  }

  svg.classList.toggle("is-stale", !!stale);
  svg.replaceChildren(frag);
}

/* ------------------------------------------------------------------ */
/* Hardware view                                                       */
/* ------------------------------------------------------------------ */

const hw = {
  running: false,
  generation: 0,
  timer: null,
  backendOk: null,
  backendError: "",
  latest: null,
  latestFetchedAt: 0,
  config: null,
  configSeq: 0,
  history: [],
  lastHistoryFetch: -Infinity,
  pending: { mode: false, simulate: false },
  status: { tone: "", text: "" },
};

function startHardware() {
  if (hw.running) return;
  hw.running = true;
  hw.lastHistoryFetch = -Infinity;
  const gen = ++hw.generation;
  renderHardware();
  hwTick(gen);
}

function stopHardware() {
  hw.running = false;
  hw.generation++;
  clearTimeout(hw.timer);
  hw.timer = null;
}

function appendHistory(record) {
  const last = hw.history[hw.history.length - 1];
  if (last && last.received_at === record.received_at) return;
  hw.history.push(record);
  if (hw.history.length > CHART_MAX_POINTS) hw.history.splice(0, hw.history.length - CHART_MAX_POINTS);
}

// One request cycle at a time: the next tick is scheduled only after this one settles.
async function hwTick(gen) {
  const started = performance.now();
  const seq = hw.configSeq;
  const wantHistory = started - hw.lastHistoryFetch >= HISTORY_REFRESH_MS;
  try {
    const [latest, config, history] = await Promise.all([
      fetchJSON("/telemetry/latest"),
      fetchJSON("/config"),
      wantHistory ? fetchJSON("/telemetry/history") : Promise.resolve(null),
    ]);
    if (gen !== hw.generation) return;
    hw.backendOk = true;
    hw.backendError = "";
    hw.latest = latest;
    hw.latestFetchedAt = performance.now();
    if (seq === hw.configSeq) hw.config = config;
    if (history) {
      hw.history = history.items.slice(-CHART_MAX_POINTS);
      hw.lastHistoryFetch = started;
    }
    if (latest.telemetry) appendHistory(latest.telemetry);
  } catch (err) {
    if (gen !== hw.generation) return;
    hw.backendOk = false;
    hw.backendError = err.message;
  }
  renderHardware();
  if (gen === hw.generation && hw.running) {
    hw.timer = setTimeout(() => hwTick(gen), Math.max(0, POLL_MS - (performance.now() - started)));
  }
}

function renderHardware() {
  if (!hw.running) return;
  const latest = hw.latest;
  const t = latest && latest.telemetry;
  const cfg = hw.config;

  let age = null;
  if (latest && latest.age_seconds != null) {
    age = latest.age_seconds + (performance.now() - hw.latestFetchedAt) / 1000;
  }
  const online = hw.backendOk === true && !!latest && latest.online === true;
  const stale = !!t && !online;

  // Header indicators
  if (hw.backendOk === null) setIndicator("ind-backend", "neutral", "Connecting…");
  else if (hw.backendOk) setIndicator("ind-backend", "ok", "Connected");
  else setIndicator("ind-backend", "alarm", "Unreachable", `Backend unreachable: ${hw.backendError}`);

  if (hw.backendOk === false) {
    setIndicator("ind-device", "neutral", "Unknown (backend unreachable)");
  } else if (!latest) {
    setIndicator("ind-device", "neutral", "Checking…");
  } else if (!t) {
    setIndicator("ind-device", "warn", "Waiting for ESP32");
  } else if (online) {
    setIndicator("ind-device", "ok", "Online");
  } else {
    setIndicator("ind-device", "alarm", `Offline · last seen ${formatAge(age)}`);
  }

  $("hw-backend-banner").hidden = hw.backendOk !== false;
  setText($("hw-backend-banner-text"),
    `The dashboard cannot reach the Pyron server (${hw.backendError}). ESP32 status is unknown; values below are the last data received.`);

  // State
  const stateCard = $("hw-state-card");
  stateCard.dataset.state = t ? t.state : "waiting";
  stateCard.classList.toggle("is-stale", stale);
  $("hw-readings-card").classList.toggle("is-stale", stale);
  setText($("hw-state"), t ? STATE_LABELS[t.state] || String(t.state).toUpperCase() : "WAITING");

  const badge = $("hw-stale-badge");
  badge.hidden = !stale;
  if (stale) setText(badge, age != null ? `STALE · ${formatAge(age)}` : "STALE");

  let sub;
  if (!t) sub = hw.backendOk === false ? "No telemetry received (backend unreachable)" : "Waiting for ESP32";
  else if (hw.backendOk === false) sub = "Last reading before the backend became unreachable";
  else if (!online) sub = "ESP32 offline — showing last reported reading";
  else sub = "Live, as reported by the ESP32";
  setText($("hw-state-sub"), sub);

  setText($("hw-device"), t ? String(t.device) : "—");
  setText($("hw-received"), t ? new Date(t.received_at).toLocaleTimeString() + (age != null ? ` (${formatAge(age)})` : "") : "—");
  setText($("hw-uptime"), t ? formatUptime(t.uptime_ms) : "—");

  // Readings
  setText($("hw-mq2"), t ? fmtNum(t.mq2_raw) : "—");
  setText($("hw-damper"), t ? DAMPER_LABELS[t.damper] || String(t.damper) : "—");
  setText($("hw-damper-deg"), t ? `${t.damper_deg}°` : "—");

  const air = $("hw-airflow");
  if (!t) {
    air.replaceChildren("—");
    setText($("hw-airflow-note"), "provisional estimate, not measured");
  } else if (t.airflow_cm_s == null) {
    const span = document.createElement("span");
    span.className = "unavailable";
    span.textContent = "Unavailable";
    air.replaceChildren(span);
    setText($("hw-airflow-note"), `no estimate for device config v${t.config_version} in this server session`);
  } else {
    air.replaceChildren(`~${t.airflow_cm_s} cm/s`);
    setText($("hw-airflow-note"), "provisional estimate, not measured");
  }

  // Configuration
  setText($("hw-cfg-version"), cfg ? `v${cfg.version}` : "—");
  setText($("hw-dev-version"), t ? `v${t.config_version}` : "—");
  setText($("hw-cfg-mode"), cfg ? MODE_LABELS[cfg.mode] || cfg.mode : "—");
  setText($("hw-cfg-sim"), cfg ? (cfg.simulate ? "ON — ESP32 told to report alarm" : "Off") : "—");

  const confirm = $("hw-confirm");
  let tone = "neutral", title = "—", detail = "";
  if (!cfg) {
    title = hw.backendOk === false ? "Configuration unavailable" : "Loading configuration…";
  } else if (!t) {
    title = "No device telemetry yet";
    detail = `Backend is serving config v${cfg.version}. Confirmation appears once the ESP32 reports it.`;
  } else if (t.config_version !== cfg.version) {
    tone = "warn";
    title = "Pending device confirmation";
    detail = `ESP32 last reported config v${t.config_version}; backend is serving v${cfg.version}. ` +
      "The hardware has not confirmed the new configuration, and the dampers may not have changed yet.";
  } else if (stale) {
    title = `Last confirmed v${cfg.version}`;
    detail = "The ESP32 reported this version before going offline.";
  } else {
    tone = "ok";
    title = "Device reports current configuration";
    detail = `ESP32 is reporting config v${cfg.version}. Damper values above are what it reports commanding.`;
  }
  confirm.dataset.tone = tone;
  setText($("hw-confirm-title"), title);
  setText($("hw-confirm-detail"), detail);

  // Controls
  const canControl = hw.backendOk === true && !!cfg;
  document.querySelectorAll(".mode-btn").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(!!cfg && cfg.mode === btn.dataset.mode));
    btn.disabled = !canControl || hw.pending.mode;
  });
  const sim = $("hw-sim-toggle");
  sim.checked = !!cfg && cfg.simulate;
  sim.disabled = !canControl || hw.pending.simulate;

  setText($("hw-alarm-response"), cfg
    ? `Configured alarm response for the current mode: dampers ${cfg.response.alarm} (${cfg.damper_deg[cfg.response.alarm]}° command). This is configuration, not confirmed hardware state.`
    : "");

  if (hw.status.awaitVersion != null && t && t.config_version === hw.status.awaitVersion && online) {
    hw.status = {
      tone: "info",
      text: `ESP32 confirmed config v${hw.status.awaitVersion}: it now reports ${STATE_LABELS[t.state] || t.state} with dampers ${t.damper}.`,
    };
  }
  const status = $("hw-control-status");
  status.dataset.tone = hw.status.tone;
  setText(status, hw.status.text);

  // Chart
  drawChart($("hw-chart"), hw.history.map((r) => r.mq2_raw), {
    thresholds: cfg ? { clear: cfg.clear_adc, warn: cfg.warn_adc, alarm: cfg.alarm_adc } : null,
    maxPoints: CHART_MAX_POINTS,
    stale,
    emptyText: hw.backendOk === false ? "Backend unreachable" : "Waiting for ESP32 telemetry",
  });
  setText($("hw-chart-caption"), hw.history.length
    ? `Last ${hw.history.length} samples · server receipt order${cfg ? ` · thresholds from config v${cfg.version}` : ""}`
    : "ADC counts · server receipt order");
}

async function postConfig(kind, path, body, describe) {
  if (hw.pending[kind]) return;
  hw.pending[kind] = true;
  hw.configSeq++;
  hw.status = { tone: "info", text: `Sending ${describe}…` };
  renderHardware();
  try {
    const before = hw.config ? hw.config.version : null;
    const cfg = await fetchJSON(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    hw.configSeq++;
    hw.config = cfg;
    hw.status = cfg.version === before
      ? { tone: "info", text: `No change: ${describe} was already set (config v${cfg.version}).` }
      : {
          tone: "info",
          awaitVersion: cfg.version,
          text: `Backend config updated to v${cfg.version}. Pending device confirmation — the ESP32 applies it on its next config fetch.`,
        };
  } catch (err) {
    hw.status = { tone: "error", text: `Failed to update ${describe}: ${err.message}. Configuration unchanged.` };
  } finally {
    hw.pending[kind] = false;
    renderHardware();
  }
}

document.querySelectorAll(".mode-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const mode = btn.dataset.mode;
    postConfig("mode", "/config/mode", { mode }, `mode "${MODE_LABELS[mode]}"`);
  });
});

$("hw-sim-toggle").addEventListener("change", (e) => {
  const on = e.target.checked;
  e.target.checked = !!hw.config && hw.config.simulate;
  postConfig("simulate", "/config/simulate", { on }, `force alarm ${on ? "ON" : "OFF"}`);
});

/* ------------------------------------------------------------------ */
/* Demo preview (browser-only, synthetic)                              */
/* ------------------------------------------------------------------ */

const DEMO = {
  sampleMs: 500,
  samples: 48,
  seed: 20261114,
  thresholds: { warn: 1200, alarm: 2000 },
  damperDeg: { open: 90, half: 45, closed: 0 },
  airflow: { open: 15, half: 7, closed: 0 },
  responses: {
    nasa: { ok: "open", warn: "half", alarm: "closed" },
    earth: { ok: "open", warn: "open", alarm: "open" },
  },
};

const demo = { timer: null, index: 0, values: [], rng: null, done: false };

function mulberry32(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Piecewise profile in seconds: baseline, rise into warn, hold, rise into alarm, hold.
function demoProfile(sec) {
  const lerp = (a, b, f) => a + (b - a) * Math.min(Math.max(f, 0), 1);
  if (sec < 6) return 450;
  if (sec < 11) return lerp(450, 1600, (sec - 6) / 5);
  if (sec < 15) return 1600;
  if (sec < 20) return lerp(1600, 2800, (sec - 15) / 5);
  return 2800;
}

function demoClassify(v) {
  if (v >= DEMO.thresholds.alarm) return "alarm";
  if (v >= DEMO.thresholds.warn) return "warn";
  return "ok";
}

function demoReset() {
  clearInterval(demo.timer);
  demo.timer = null;
  demo.index = 0;
  demo.values = [];
  demo.rng = mulberry32(DEMO.seed);
  demo.done = false;
  renderDemo();
}

function demoStep() {
  const sec = (demo.index * DEMO.sampleMs) / 1000;
  const noise = (demo.rng() - 0.5) * 80;
  demo.values.push(Math.round(Math.min(Math.max(demoProfile(sec) + noise, 0), ADC_MAX)));
  demo.index++;
  if (demo.index >= DEMO.samples) {
    clearInterval(demo.timer);
    demo.timer = null;
    demo.done = true;
  }
  renderDemo();
}

function demoStart() {
  if (demo.timer) return;
  if (demo.done || demo.index === 0) demoReset();
  demoStep();
  if (!demo.done) demo.timer = setInterval(demoStep, DEMO.sampleMs);
  renderDemo();
}

function demoStop() {
  clearInterval(demo.timer);
  demo.timer = null;
}

function renderDemoPanel(panel, state) {
  const mode = panel.dataset.mode;
  const pos = state ? DEMO.responses[mode][state] : null;
  panel.dataset.pos = pos || "";
  const deg = pos ? DEMO.damperDeg[pos] : DEMO.damperDeg.open;
  const flow = pos ? DEMO.airflow[pos] : null;
  panel.querySelector(".blade-wrap").style.transform = `rotate(${deg}deg)`;
  const svg = panel.querySelector(".damper-svg");
  const frac = flow ? flow / DEMO.airflow.open : 0;
  svg.style.setProperty("--flow-opacity", String(state ? 0.12 + 0.8 * frac : 0.12));
  svg.style.setProperty("--flow-duration", `${frac ? 1.4 / frac : 1}s`);
  svg.style.setProperty("--flow-play", frac && demo.timer ? "running" : "paused");
  setText(panel.querySelector(".damper-pos"), pos ? DAMPER_LABELS[pos] : "—");
  setText(panel.querySelector(".damper-deg"), pos ? `${DEMO.damperDeg[pos]}°` : "—");
  setText(panel.querySelector(".damper-air"), pos ? `~${flow} cm/s (estimate)` : "—");
}

function renderDemo() {
  const n = demo.values.length;
  const value = n ? demo.values[n - 1] : null;
  const state = value != null ? demoClassify(value) : null;

  const card = $("demo-state-card");
  card.dataset.state = state || "idle";
  setText($("demo-state"), state ? STATE_LABELS[state] : "READY");
  setText($("demo-state-sub"),
    demo.done ? "Scenario complete. Reset to run it again."
      : demo.timer ? "Running synthetic scenario…"
        : n ? "Paused." : "Press Start scenario to run OK → WARNING → ALARM.");
  setText($("demo-mq2"), value != null ? fmtNum(value) : "—");

  const order = ["ok", "warn", "alarm"];
  const reached = state ? order.indexOf(state) : -1;
  document.querySelectorAll(".phase-track li").forEach((li, i) => {
    li.classList.toggle("active", i === reached);
    li.classList.toggle("done", i < reached);
  });

  const total = (DEMO.samples * DEMO.sampleMs) / 1000;
  const elapsed = (demo.index * DEMO.sampleMs) / 1000;
  $("demo-progress-fill").style.width = `${(demo.index / DEMO.samples) * 100}%`;
  setText($("demo-clock"), `${elapsed.toFixed(1)} s / ${total.toFixed(1)} s`);

  const startBtn = $("demo-start");
  startBtn.disabled = !!demo.timer;
  setText(startBtn, demo.done ? "Restart scenario" : "Start scenario");

  renderDemoPanel($("demo-panel-nasa"), state);
  renderDemoPanel($("demo-panel-earth"), state);

  drawChart($("demo-chart"), demo.values, {
    thresholds: DEMO.thresholds,
    maxPoints: DEMO.samples,
    stale: false,
    emptyText: "Synthetic scenario not started",
  });
}

$("demo-start").addEventListener("click", demoStart);
$("demo-reset").addEventListener("click", demoReset);

/* ------------------------------------------------------------------ */
/* View switching                                                      */
/* ------------------------------------------------------------------ */

function setView(view) {
  const isDemo = view === "demo";
  document.body.dataset.view = view;
  $("hardware-view").hidden = isDemo;
  $("demo-view").hidden = !isDemo;
  $("tab-hardware").setAttribute("aria-selected", String(!isDemo));
  $("tab-demo").setAttribute("aria-selected", String(isDemo));
  history.replaceState(null, "", isDemo ? "#demo" : location.pathname + location.search);

  if (isDemo) {
    stopHardware();
    setIndicator("ind-backend", "neutral", "Not polled in demo");
    setIndicator("ind-device", "neutral", "Not shown in demo");
    demoReset();
  } else {
    demoStop();
    demoReset();
    hw.backendOk = null;
    hw.latest = null;
    hw.status = { tone: "", text: "" };
    startHardware();
  }
}

$("tab-hardware").addEventListener("click", () => setView("hardware"));
$("tab-demo").addEventListener("click", () => setView("demo"));

setView(location.hash === "#demo" ? "demo" : "hardware");
