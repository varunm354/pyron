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
  const { timeoutMs = FETCH_TIMEOUT_MS, ...fetchOptions } = options;
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(url, {
      ...fetchOptions,
      cache: "no-store",
      signal: controller.signal,
      headers: { Accept: "application/json", ...(fetchOptions.headers || {}) },
    });
    if (!resp.ok) {
      let detail = "";
      try {
        const body = await resp.json();
        if (typeof body.error === "string") detail = body.error;
        else detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
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

/* ------------------------------------------------------------------ */
/* NASA BASS-II explorer (static dataset, fetched once per page load)  */
/* ------------------------------------------------------------------ */

const NASA = {
  pageSize: 12,
  x: "numeric__Initial CO2 % by vol",
  y: "numeric__Final CO2 % by vol",
  originalColumns: [
    "PI", "Test #", "Date", "GMT", "Sample #", "Fuel Sample Material", "Flow restrictor",
    "Fan display", "Air display", "Radiometer gain (1000 250 50 10)", "Total Frames Shot",
    "Calibrated initial O2 % by vol", "Calibrated final O2 % by vol",
    "Initial CO2 % by vol", "Final CO2 % by vol", "Initial CO (ppm)", "Final CO (ppm)",
  ],
};

const nasa = { items: [], byId: new Map(), filtered: [], groups: new Map(), page: 0, selectedKey: null, domain: null };

const isNum = (v) => typeof v === "number" && Number.isFinite(v);

function describeFlag(flag) {
  const i = flag.indexOf(":");
  const kind = i < 0 ? flag : flag.slice(0, i);
  const column = i < 0 ? "" : flag.slice(i + 1);
  if (kind === "negative_value") return `Negative value — ${column}`;
  if (kind === "review_oxygen_above_30_percent") return `O₂ above 30% — ${column}`;
  if (kind === "review_date_year_2018") return "Date year 2018";
  return flag;
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("data-")) node.setAttribute(k, v);
    else node[k] = v;
  }
  for (const c of children) if (c != null) node.append(c);
  return node;
}

function safeHttpsUrl(url) {
  try {
    const u = new URL(String(url));
    return u.protocol === "https:" ? u.href : null;
  } catch (_) {
    return null;
  }
}

function flagChips(flags) {
  if (!flags.length) return el("span", { class: "muted", text: "No flags" });
  const wrap = el("span", { class: "chips" });
  for (const f of flags) wrap.append(el("span", { class: "chip chip-review", text: describeFlag(f), title: `Source-review flag: ${f}` }));
  return wrap;
}

function niceStep(span) {
  for (const s of [0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 5, 10, 20, 50, 100]) if (span / s <= 8) return s;
  return Math.pow(10, Math.ceil(Math.log10(span / 8)));
}

function computeDomain(items) {
  const vals = [];
  for (const it of items) if (isNum(it[NASA.x]) && isNum(it[NASA.y])) vals.push(it[NASA.x], it[NASA.y]);
  if (!vals.length) return { lo: 0, hi: 1, step: 0.2 };
  const rawLo = Math.min(0, ...vals);
  const rawHi = Math.max(...vals);
  const span = (rawHi - rawLo) * 1.05 || 1;
  const step = niceStep(span);
  return { lo: Math.floor(rawLo / step) * step, hi: Math.ceil((rawLo + span) / step) * step, step };
}

async function loadNasa() {
  try {
    const [data, summary] = await Promise.all([
      fetchJSON("/experiments", { timeoutMs: 15000 }),
      fetchJSON("/experiments/summary", { timeoutMs: 15000 }),
    ]);
    if (!Array.isArray(data.items)) throw new Error("unexpected /experiments response");
    nasa.items = data.items;
    nasa.byId = new Map(data.items.map((it) => [it.record_id, it]));
    nasa.domain = computeDomain(data.items);

    setText($("nasa-count"), String(summary.record_count));
    setText($("nasa-pis"), String(summary.distinct_pi_count));
    setText($("nasa-flagged"), String(summary.review_flagged_count));
    setText($("nasa-completeness"), String(data.completeness_note));
    const href = safeHttpsUrl(data.source_url);
    if (href) $("nasa-source").href = href;
    else $("nasa-source").removeAttribute("href");

    const select = $("nasa-pi");
    const pis = [...new Set(data.items.map((it) => it.PI))].sort((a, b) => a.localeCompare(b));
    for (const pi of pis) select.append(el("option", { value: pi, text: pi }));

    $("nasa-loading").hidden = true;
    $("nasa-body").hidden = false;
    applyNasaFilters();
  } catch (err) {
    $("nasa-loading").hidden = true;
    $("nasa-unavailable").hidden = false;
    setText($("nasa-unavailable-text"),
      `${err.message}. The NASA explorer cannot be shown; the hardware dashboard and demo preview are unaffected.`);
  }
}

function applyNasaFilters() {
  const q = $("nasa-search").value.trim().toLowerCase();
  const pi = $("nasa-pi").value;
  nasa.filtered = nasa.items.filter((it) => {
    if (pi && it.PI !== pi) return false;
    if (!q) return true;
    return [it.PI, it["Test #"], it["Fuel Sample Material"]].some((v) => String(v).toLowerCase().includes(q));
  });
  nasa.page = 0;

  nasa.groups = new Map();
  for (const it of nasa.filtered) {
    if (!isNum(it[NASA.x]) || !isNum(it[NASA.y])) continue;
    const key = `${it[NASA.x]}|${it[NASA.y]}`;
    if (!nasa.groups.has(key)) nasa.groups.set(key, { key, x: it[NASA.x], y: it[NASA.y], items: [] });
    nasa.groups.get(key).items.push(it);
  }
  if (!nasa.groups.has(nasa.selectedKey)) nasa.selectedKey = null;

  const total = nasa.items.length;
  setText($("nasa-match-count"), nasa.filtered.length === total
    ? `${total} of ${total} records`
    : `${nasa.filtered.length} of ${total} records match`);

  renderNasaTable();
  renderScatter();
  renderSelection();
}

function renderNasaTable() {
  const tbody = $("nasa-tbody");
  const rows = [];
  const pages = Math.max(1, Math.ceil(nasa.filtered.length / NASA.pageSize));
  nasa.page = Math.min(nasa.page, pages - 1);
  const start = nasa.page * NASA.pageSize;
  const slice = nasa.filtered.slice(start, start + NASA.pageSize);

  if (!slice.length) {
    rows.push(el("tr", { class: "empty-row" }, el("td", { colSpan: 6, text: "No records match the current search or PI filter." })));
  }
  for (const it of slice) {
    const tr = el("tr", { class: "nasa-row", tabIndex: 0, "data-id": it.record_id, title: `Show details for ${it.record_id}` },
      el("td", { text: it.PI }),
      el("td", { class: "mono", text: it["Test #"] }),
      el("td", { text: it["Fuel Sample Material"] }),
      el("td", { class: "num mono", text: it["Initial CO2 % by vol"] || "—" }),
      el("td", { class: "num mono", text: it["Final CO2 % by vol"] || "—" }),
      el("td", {}, flagChips(it.review_flags || [])),
    );
    if (it.review_flags && it.review_flags.length) tr.classList.add("is-flagged");
    rows.push(tr);
  }
  tbody.replaceChildren(...rows);

  const end = Math.min(start + NASA.pageSize, nasa.filtered.length);
  setText($("nasa-page"), nasa.filtered.length
    ? `Page ${nasa.page + 1} of ${pages} · ${start + 1}–${end} of ${nasa.filtered.length}`
    : "No matching records");
  $("nasa-prev").disabled = nasa.page === 0;
  $("nasa-next").disabled = nasa.page >= pages - 1;
}

const SC = { w: 440, h: 400, left: 62, top: 14, size: 326 };

function renderScatter() {
  const svg = $("nasa-scatter");
  const { lo, hi, step } = nasa.domain || { lo: 0, hi: 1, step: 0.2 };
  const { left, top, size } = SC;
  const sx = (v) => left + ((v - lo) / (hi - lo)) * size;
  const sy = (v) => top + size - ((v - lo) / (hi - lo)) * size;
  const frag = document.createDocumentFragment();

  for (let v = lo, i = 0; v <= hi + step / 2 && i < 50; v += step, i++) {
    const label = String(Math.round(v * 100) / 100);
    frag.append(svgEl("line", { class: "grid-line", x1: sx(v), x2: sx(v), y1: top, y2: top + size }));
    frag.append(svgEl("line", { class: "grid-line", x1: left, x2: left + size, y1: sy(v), y2: sy(v) }));
    frag.append(svgEl("text", { class: "axis-label", x: sx(v), y: top + size + 16, "text-anchor": "middle" }, label));
    frag.append(svgEl("text", { class: "axis-label", x: left - 8, y: sy(v) + 4, "text-anchor": "end" }, label));
  }
  frag.append(svgEl("rect", { class: "plot-frame", x: left, y: top, width: size, height: size }));
  frag.append(svgEl("text", { class: "axis-title", x: left + size / 2, y: top + size + 40, "text-anchor": "middle" }, "Initial CO2 (% by volume)"));
  frag.append(svgEl("text", {
    class: "axis-title", x: 0, y: 0, "text-anchor": "middle",
    transform: `translate(${left - 44} ${top + size / 2}) rotate(-90)`,
  }, "Final CO2 (% by volume)"));

  frag.append(svgEl("line", { class: "ref-line", x1: sx(lo), y1: sy(lo), x2: sx(hi), y2: sy(hi) }));
  frag.append(svgEl("text", { class: "ref-label", x: sx(hi) - 4, y: sy(hi) + 34, "text-anchor": "end" }, "y = x"));

  const groups = [...nasa.groups.values()].sort((a, b) => a.x - b.x || a.y - b.y);
  // One Tab stop for the chart; arrow keys move between points in x order.
  const focusKey = nasa.groups.has(nasa.selectedKey) ? nasa.selectedKey : groups.length ? groups[0].key : null;
  for (const g of groups) {
    const n = g.items.length;
    const flagged = g.items.some((it) => it.review_flags && it.review_flags.length);
    const ids = g.items.map((it) => it.record_id).join(", ");
    const c = svgEl("circle", {
      class: `pt${flagged ? " pt-flagged" : ""}${g.key === nasa.selectedKey ? " pt-selected" : ""}`,
      cx: sx(g.x), cy: sy(g.y), r: n > 1 ? 6.5 : 4.5,
      tabindex: g.key === focusKey ? 0 : -1, role: "button", "data-key": g.key,
      "aria-label": `${ids}: initial ${g.x}%, final ${g.y}%${n > 1 ? ` (${n} records)` : ""}`,
    });
    frag.append(c);
    if (n > 1) frag.append(svgEl("text", { class: "pt-count", x: sx(g.x) + 9, y: sy(g.y) - 7 }, `×${n}`));
  }

  if (!nasa.filtered.length) {
    frag.append(svgEl("text", { class: "empty-label", x: left + size / 2, y: top + size / 2, "text-anchor": "middle" }, "No records match the current filter"));
  } else if (!groups.length) {
    frag.append(svgEl("text", { class: "empty-label", x: left + size / 2, y: top + size / 2, "text-anchor": "middle" }, "No plottable records in this selection"));
  }
  svg.replaceChildren(frag);
  hideTip();

  const plotted = groups.reduce((sum, g) => sum + g.items.length, 0);
  const excluded = nasa.filtered.length - plotted;
  const shared = groups.filter((g) => g.items.length > 1).reduce((sum, g) => sum + g.items.length, 0);
  let text = nasa.filtered.length
    ? `Plotted ${plotted} of ${nasa.filtered.length} matching records · ${excluded} excluded (initial or final CO₂ missing or nonnumeric)`
    : "0 matching records — nothing to plot";
  if (shared) text += ` · ${shared} records share identical values (larger points, labeled ×n)`;
  setText($("nasa-plot-counts"), text);
}

function recordLines(it) {
  return [
    el("div", { class: "tip-id mono", text: it.record_id }),
    el("div", { text: `PI: ${it.PI} · Test #: ${it["Test #"]}` }),
    el("div", { class: "muted", text: it["Fuel Sample Material"] }),
    el("div", { class: "mono", text: `Initial CO₂ ${it[NASA.x]}% → Final CO₂ ${it[NASA.y]}% (by vol)` }),
    (it.review_flags || []).length ? el("div", { class: "tip-flag", text: `Source-review flag: ${it.review_flags.map(describeFlag).join("; ")}` }) : null,
  ];
}

function showTip(circle) {
  const g = nasa.groups.get(circle.dataset.key);
  if (!g) return;
  const tip = $("nasa-tip");
  const blocks = g.items.slice(0, 4).map((it) => el("div", { class: "tip-record" }, ...recordLines(it)));
  if (g.items.length > 4) blocks.push(el("div", { class: "muted", text: `+${g.items.length - 4} more — click to list all` }));
  tip.replaceChildren(...blocks);
  tip.hidden = false;
  const wrap = circle.closest(".scatter-wrap").getBoundingClientRect();
  const r = circle.getBoundingClientRect();
  const x = r.left - wrap.left + r.width / 2;
  const y = r.top - wrap.top;
  const tipW = tip.offsetWidth;
  tip.style.left = `${Math.min(Math.max(x - tipW / 2, 4), wrap.width - tipW - 4)}px`;
  tip.style.top = `${y - tip.offsetHeight - 10 < 0 ? y + r.height + 10 : y - tip.offsetHeight - 10}px`;
}

function hideTip() {
  $("nasa-tip").hidden = true;
}

function selectPoint(key) {
  nasa.selectedKey = nasa.selectedKey === key ? null : key;
  document.querySelectorAll("#nasa-scatter .pt").forEach((c) => c.classList.toggle("pt-selected", c.dataset.key === nasa.selectedKey));
  renderSelection();
}

function renderSelection() {
  const panel = $("nasa-selection");
  const g = nasa.selectedKey && nasa.groups.get(nasa.selectedKey);
  if (!g) {
    panel.hidden = true;
    panel.replaceChildren();
    return;
  }
  const head = el("div", { class: "selection-head" },
    el("strong", { text: `Selected point · ${g.items.length} record${g.items.length > 1 ? "s" : ""}` }),
    el("button", { type: "button", class: "btn btn-small", text: "Clear", onclick: () => selectPoint(g.key) }),
  );
  const list = g.items.map((it) => el("div", { class: "selection-record" },
    el("div", {}, ...recordLines(it)),
    el("button", { type: "button", class: "btn btn-small", text: "View details", onclick: () => openNasaDetail(it.record_id) }),
  ));
  panel.replaceChildren(head, ...list);
  panel.hidden = false;
}

function openNasaDetail(id) {
  const it = nasa.byId.get(id);
  if (!it) return;
  setText($("nasa-dialog-title"), it.record_id);

  const flags = it.review_flags || [];
  const flagBox = $("nasa-dialog-flags");
  flagBox.className = flags.length ? "dialog-flags has-flags" : "dialog-flags";
  flagBox.replaceChildren(
    el("strong", { text: flags.length ? "Source-review flags" : "No source-review flags" }),
    flags.length ? el("ul", {}, ...flags.map((f) => el("li", {}, el("span", { class: "mono", text: f }), ` — ${describeFlag(f)}`))) : null,
    flags.length ? el("div", { class: "muted", text: "Flags mark values to check against the source. They are not proven errors, and flagged records stay in the table and chart." }) : null,
  );

  const rows = NASA.originalColumns.map((col) => {
    const numKey = `numeric__${col}`;
    let parsed;
    if (!(numKey in it)) parsed = el("span", { class: "muted", text: "text field (not parsed)" });
    else if (it[numKey] === null) parsed = el("span", { class: "muted", text: "null (blank or nonnumeric)" });
    else parsed = el("span", { class: "mono", text: String(it[numKey]) });
    const original = it[col] === "" ? el("span", { class: "muted", text: "(blank)" }) : el("span", { text: String(it[col]) });
    return el("tr", {}, el("th", { scope: "row", text: col }), el("td", {}, original), el("td", {}, parsed));
  });
  $("nasa-dialog-fields").replaceChildren(...rows);

  const href = safeHttpsUrl(it.source_url);
  const urlNode = href ? el("a", { href, target: "_blank", rel: "noopener noreferrer", text: it.source_url }) : el("span", { text: String(it.source_url) });
  const prov = [
    ["record_id", el("span", { class: "mono", text: it.record_id })],
    ["source_file", el("span", { text: it.source_file })],
    ["source_row", el("span", { class: "mono", text: it.source_row })],
    ["source_url", urlNode],
    ["quality_flags", el("span", { class: "mono", text: it.quality_flags || "(none)" })],
    ["dataset_note", el("span", { text: it.dataset_note })],
  ].map(([k, v]) => el("div", {}, el("dt", { text: k }), el("dd", {}, v)));
  $("nasa-dialog-provenance").replaceChildren(...prov);

  hideTip();
  $("nasa-dialog").showModal();
}

$("nasa-search").addEventListener("input", applyNasaFilters);
$("nasa-pi").addEventListener("change", applyNasaFilters);
$("nasa-prev").addEventListener("click", () => { nasa.page--; renderNasaTable(); });
$("nasa-next").addEventListener("click", () => { nasa.page++; renderNasaTable(); });

$("nasa-tbody").addEventListener("click", (e) => {
  const row = e.target.closest("tr.nasa-row");
  if (row) openNasaDetail(row.dataset.id);
});
$("nasa-tbody").addEventListener("keydown", (e) => {
  const row = e.target.closest("tr.nasa-row");
  if (row && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    openNasaDetail(row.dataset.id);
  }
});

const scatter = $("nasa-scatter");
scatter.addEventListener("pointerover", (e) => { if (e.target.classList.contains("pt")) showTip(e.target); });
scatter.addEventListener("pointerout", (e) => { if (e.target.classList.contains("pt")) hideTip(); });
scatter.addEventListener("focusin", (e) => { if (e.target.classList.contains("pt")) showTip(e.target); });
scatter.addEventListener("focusout", hideTip);
scatter.addEventListener("click", (e) => { if (e.target.classList.contains("pt")) selectPoint(e.target.dataset.key); });
scatter.addEventListener("keydown", (e) => {
  if (!e.target.classList.contains("pt")) return;
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    selectPoint(e.target.dataset.key);
    return;
  }
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
  if (!step) return;
  e.preventDefault();
  const pts = [...scatter.querySelectorAll(".pt")];
  const next = pts[Math.min(Math.max(pts.indexOf(e.target) + step, 0), pts.length - 1)];
  e.target.setAttribute("tabindex", "-1");
  next.setAttribute("tabindex", "0");
  next.focus();
});

$("nasa-dialog-close").addEventListener("click", () => $("nasa-dialog").close());
$("nasa-dialog").addEventListener("click", (e) => { if (e.target === e.currentTarget) e.currentTarget.close(); });

setView(location.hash === "#demo" ? "demo" : "hardware");
loadNasa();
