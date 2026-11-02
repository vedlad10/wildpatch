// Wildpatch client: pick a patch, stream a planning run, and show what each Otari call did.

const $ = (sel) => document.querySelector(sel);
const h = (tag, attrs = {}, ...kids) => {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : String(kid));
  return el;
};
const icon = (name) => {
  const s = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  const u = document.createElementNS("http://www.w3.org/2000/svg", "use");
  u.setAttribute("href", `#i-${name}`);
  s.append(u);
  return s;
};
const cents = (x) => `$${Number(x || 0).toFixed(2)}`;
const usd = (x) => (x == null ? "–" : `$${Number(x).toFixed(Number(x) < 0.01 ? 4 : 3)}`);
const secs = (ms) => (ms == null ? "" : `${(ms / 1000).toFixed(1)} s`);
const short = (model) => (model || "").split(":").pop().split("/").pop();

const PRESETS = [
  { label: "Barcelona schoolyard", lat: 41.40216, lon: 2.15646, area: 350, name: "Escola corner, Gràcia",
    goals: "Our primary school wants to turn a sunny 350 m² corner of the playground into a wildflower meadow with a low hedge, so pupils can watch bees and butterflies. Small budget; parents can help at weekends." },
  { label: "IIT Bombay lakeside", lat: 19.12993, lon: 72.91524, area: 800, name: "Powai lake edge, IIT Bombay",
    goals: "The student nature club wants to restore a degraded 800 m² strip beside Powai Lake with native trees and shrubs for birds and butterflies. Monsoon waterlogging, hot dry spring, some foot traffic." },
  { label: "London verge", lat: 51.54497, lon: -0.05525, area: 120, name: "Mown verge, Hackney",
    goals: "Residents want to turn a mown 120 m² grass verge into a pollinator strip. Dog walkers use it and the council wants it to look tidy." },
];

const PROSE_STAGES = new Set(["survey", "analysis"]);  // others answer in JSON, summarised from data events
const STAGE_ICON = { intake: "shield", vision: "eye", survey: "db", scout: "globe", analysis: "code", plan: "route", structure: "palette" };
const FEATURES = [
  ["open_weight", "Open-weight models", "layers"], ["multi_model", "Models in distinct roles", "layers"],
  ["routing", "Routing + fallbacks", "route"], ["mcp", "MCP", "db"], ["web", "Web search/fetch", "globe"],
  ["code", "Code execution", "code"], ["guardrails", "Guardrails", "shield"], ["budgets", "Budgets", "wallet"],
  ["per_user", "Per-user budgets", "user"],
];

const state = { config: null, volunteer: "ana", lat: PRESETS[0].lat, lon: PRESETS[0].lon, radius: 2, photo: null,
  stages: {}, tools: {}, receipts: [], served: new Set(), planText: "", data: {}, running: false };

// ------------------------------------------------------------------ map

const map = L.map("map", { zoomControl: true, attributionControl: true }).setView([state.lat, state.lon], 15);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
  maxZoom: 19, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
}).addTo(map);
const pinIcon = L.divIcon({ className: "", html: '<div style="width:18px;height:18px;border-radius:50%;background:#c0612f;border:3px solid #fff;box-shadow:0 2px 6px rgba(0,0,0,.35)"></div>', iconSize: [18, 18], iconAnchor: [9, 9] });
const marker = L.marker([state.lat, state.lon], { icon: pinIcon }).addTo(map);
const circle = L.circle([state.lat, state.lon], { radius: 2000, color: "#4f7a3a", weight: 1.5, fillOpacity: 0.07 }).addTo(map);

function setSite(lat, lon, zoom) {
  state.lat = +(+lat).toFixed(5);
  state.lon = +(+lon).toFixed(5);
  marker.setLatLng([state.lat, state.lon]);
  circle.setLatLng([state.lat, state.lon]);
  $("#coords").textContent = `${state.lat.toFixed(5)}, ${state.lon.toFixed(5)}`;
  if (zoom) map.setView([state.lat, state.lon], zoom);
}
map.on("click", (e) => setSite(e.latlng.lat, e.latlng.lng));
$("#radius").addEventListener("input", (e) => {
  state.radius = +e.target.value;
  $("#radius-v").textContent = `${state.radius} km`;
  circle.setRadius(state.radius * 1000);
  map.fitBounds(circle.getBounds(), { maxZoom: 16 });
});
$("#search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const q = $("#search").value.trim();
  if (!q) return;
  const r = await fetch(`/api/geocode?q=${encodeURIComponent(q)}`);
  if (!r.ok) { $("#search").setCustomValidity("Place not found"); $("#search").reportValidity(); return; }
  const place = await r.json();
  setSite(place.lat, place.lon, 16);
  if (!$("#site-name").value) $("#site-name").value = place.name.split(",").slice(0, 2).join(",");
});
$("#search").addEventListener("input", (e) => e.target.setCustomValidity(""));

for (const p of PRESETS) {
  $("#presets").append(h("button", { class: "chip", type: "button", onclick: () => {
    setSite(p.lat, p.lon, 16);
    $("#site-name").value = p.name;
    $("#area").value = p.area;
    $("#goals").value = p.goals;
  } }, p.label));
}

// ------------------------------------------------------------------ photo

async function loadPhoto(file) {
  if (!file || !file.type.startsWith("image/")) return;
  const img = await createImageBitmap(file);
  const scale = Math.min(1, 1024 / Math.max(img.width, img.height));
  const canvas = Object.assign(document.createElement("canvas"), { width: Math.round(img.width * scale), height: Math.round(img.height * scale) });
  canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
  state.photo = canvas.toDataURL("image/jpeg", 0.85);
  $("#photo-preview").src = state.photo;
  $("#photo-preview").hidden = false;
  $("#drop-text").innerHTML = `${file.name} <small>will be read by the vision model</small>`;
}
$("#photo").addEventListener("change", (e) => loadPhoto(e.target.files[0]));
const drop = $("#drop");
drop.addEventListener("dragover", (e) => { e.preventDefault(); drop.classList.add("over"); });
drop.addEventListener("dragleave", () => drop.classList.remove("over"));
drop.addEventListener("drop", (e) => { e.preventDefault(); drop.classList.remove("over"); loadPhoto(e.dataTransfer.files[0]); });

// ------------------------------------------------------------------ volunteers & allowance

function renderVolunteers() {
  const box = $("#volunteers");
  box.replaceChildren();
  for (const v of state.config.volunteers) {
    box.append(h("button", { class: "vol", type: "button", role: "radio", "data-id": v.id, title: `${v.role} · Otari end user ${v.end_user}`,
      "aria-checked": String(v.id === state.volunteer), onclick: () => { state.volunteer = v.id; renderVolunteers(); refreshAllowance(v.id); } },
      h("span", { class: "av" }, v.name[0]),
      h("span", { class: "meta" }, h("b", {}, v.name), h("span", { class: "meter" }, h("i", { id: `meter-${v.id}` })),
        h("small", { id: `spend-${v.id}` }, `– / ${cents(v.cap_usd)}`))));
  }
  for (const v of state.config.volunteers) paintAllowance(v.id, state.allowance?.[v.id]);
}
function paintAllowance(id, status) {
  const v = state.config.volunteers.find((x) => x.id === id);
  const bar = document.getElementById(`meter-${id}`);
  if (!v || !bar) return;
  const spend = status ? Number(status.spend || 0) + Number(status.reserved || 0) : 0;
  const pct = Math.min(100, (100 * spend) / (v.cap_usd || 1));
  bar.style.width = `${pct}%`;
  bar.className = pct >= 100 ? "out" : pct >= 80 ? "warn" : "";
  document.getElementById(`spend-${id}`).textContent = `${cents(spend)} / ${cents(v.cap_usd)}`;
}
async function refreshAllowance(id) {
  try {
    const r = await (await fetch(`/api/allowance/${id}`)).json();
    state.allowance = { ...(state.allowance || {}), [id]: r.status };
    paintAllowance(id, r.status);
  } catch { /* the meter just stays as it was */ }
}

// ------------------------------------------------------------------ run timeline

function resetRun(ev) {
  state.stages = {}; state.tools = {}; state.receipts = []; state.served = new Set(); state.planText = ""; state.data = {};
  $("#banner").hidden = true;
  $("#results").hidden = true;
  $("#run-totals").textContent = ev.drill ? "outage drill on" : "";
  const tl = $("#timeline");
  tl.replaceChildren();
  for (const s of ev.stages) {
    const sub = h("div", { class: "stage-sub" }, h("span", { class: "mono" }, short(roleModel(s.role))), h("span", {}, s.role));
    const node = h("li", { class: "stage", id: `stage-${s.id}` },
      h("div", { class: "stage-head" },
        h("div", { class: "stage-ico" }, icon(STAGE_ICON[s.id] || "leaf")),
        h("div", {}, h("div", { class: "stage-title" }, s.title), sub),
        h("div", {}, h("div", { class: "stage-state" }, "waiting"), h("div", { class: "badges" }))),
      h("div", { class: "stage-body" }));
    tl.append(node);
    state.stages[s.id] = { node, sub, body: node.querySelector(".stage-body"), badges: node.querySelector(".badges"),
      stateEl: node.querySelector(".stage-state"), stream: null, text: "" };
  }
  renderScorecard({});
}
function roleModel(role) {
  if (role === "planner" && $("#drill").checked) return state.config.drill_policy;
  return state.config.models[role] || role;
}
function setStage(id, cls, label) {
  const s = state.stages[id];
  if (!s) return;
  s.node.classList.remove("running", "done", "error", "blocked");
  if (cls) s.node.classList.add(cls);
  if (label != null) s.stateEl.textContent = label;
}
function badge(id, text, cls = "") {
  const s = state.stages[id];
  if (s) s.badges.append(h("span", { class: `badge ${cls}` }, text));
}

const MCP_SUMMARY = {
  biodiversity_snapshot: (o) => `${o.total_species} species across ${Object.keys(o.species_by_group || {}).length} groups`,
  native_species: (o) => `${o.native_species_total} native ${o.group} (${o.native_to})`,
  introduced_species: (o) => `${o.introduced_species_total} introduced ${o.group}`,
  pollinators: (o) => `${o.pollinator_species_total} pollinator species`,
  sensitive_species: (o) => `${o.threatened_species_total} threatened species, locations withheld`,
  climate_profile: (o) => `${o.annual_precip_mm} mm rain/yr, ET₀ ${o.annual_et0_mm} mm, dry months ${(o.dry_months || []).join(", ") || "none"}`,
  locate_site: (o) => o.name,
};

function onToolCall(ev) {
  const s = state.stages[ev.stage];
  if (!s) return;
  const res = h("span", { class: "tool-res" }, "running…");
  const box = h("div", { class: "tool" },
    h("div", { class: "tool-line" }, h("span", { class: `tool-kind ${ev.kind}` }, ev.kind === "mcp" ? "MCP" : ev.kind),
      h("span", { class: "tool-name" }, toolLabel(ev)), res));
  if (ev.kind === "code" && ev.input?.code) {
    box.append(h("details", {}, h("summary", {}, "code the model wrote"), h("pre", {}, ev.input.code)));
  }
  s.body.append(box);
  state.tools[ev.id] = { box, res, ev };
}
function toolLabel(ev) {
  const a = ev.input || {};
  if (ev.kind === "mcp") return `${ev.name}(${a.group ? `"${a.group}"` : ""}${a.radius_km ? `${a.group ? ", " : ""}${a.radius_km} km` : ""})`;
  if (ev.kind === "search") return `“${a.query || ""}”`;
  if (ev.kind === "fetch") return a.url || "";
  return ev.name || ev.kind;
}
function onToolResult(ev) {
  if (!state.tools[ev.id]) onToolCall({ ...ev, input: {} });
  const t = state.tools[ev.id];
  const out = ev.output;
  t.res.classList.toggle("err", !!ev.is_error);
  if (ev.kind === "mcp") {
    const fn = MCP_SUMMARY[ev.name];
    t.res.textContent = out?.error ? `error: ${out.error}` : fn && typeof out === "object" ? `→ ${fn(out)}` : "→ ok";
    t.box.append(h("details", {}, h("summary", {}, "result from the Wildpatch MCP server"), h("pre", {}, JSON.stringify(out, null, 1))));
  } else if (ev.kind === "code") {
    t.res.textContent = `→ exit ${out?.return_code ?? "?"}`;
    t.box.append(h("pre", {}, (out?.stdout || "") + (out?.stderr ? `\n[stderr]\n${out.stderr}` : "")));
  } else if (ev.kind === "search") {
    const list = Array.isArray(out) ? out : [];
    t.res.textContent = ev.is_error ? `→ ${out?.error}` : `→ ${list.length} sources`;
    t.box.append(h("div", { class: "sources" }, list.slice(0, 6).map((x) => h("a", { href: x.url, target: "_blank", rel: "noopener" }, x.title || x.url))));
  } else if (ev.kind === "fetch") {
    t.res.textContent = ev.is_error ? `→ ${out?.error}` : `→ ${out?.title || "page read"}`;
  }
  lightFeature({ mcp: "mcp", code: "code", search: "web", fetch: "web" }[ev.kind]);
}

function onText(ev) {
  if (ev.stage === "plan") {
    state.planText += ev.text;
    $("#results").hidden = false;
    $("#plan").innerHTML = DOMPurify.sanitize(marked.parse(state.planText)) + '<span class="cursor"></span>';
    return;
  }
  const s = state.stages[ev.stage];
  if (!s) return;
  s.text += ev.text;
  if (!s.stream) { s.stream = h("div", { class: "stream" }); s.body.append(s.stream); }
  s.stream.textContent = PROSE_STAGES.has(ev.stage) ? s.text.slice(-420) : `writing… ${s.text.length} characters`;
}

function onReceipt(ev) {
  const r = ev.receipt;
  state.receipts.push(r);
  if (r.served_model) state.served.add(r.served_model);
  const s = state.stages[ev.stage];
  if (s) {
    s.sub.replaceChildren(
      h("span", { class: "mono", title: `requested ${r.requested_model}` }, short(r.served_model || r.requested_model)),
      r.provider && h("span", {}, `via ${r.provider}`),
      h("span", { class: "mono" }, usd(r.cost_usd)),
      h("span", { class: "mono" }, `${r.input_tokens}→${r.output_tokens} tok`),
      r.request_id && h("span", { class: "mono", title: "Otari-Request-ID" }, r.request_id.slice(0, 8)));
    if (r.requested_model !== r.served_model && !r.requested_model.includes("/")) badge(ev.stage, `policy ${r.requested_model}`);
    if (r.fallback) badge(ev.stage, `fallback · ${r.attempts} attempts`, "fb");
    for (const g of r.guardrails || []) badge(ev.stage, `${g.profile} ${g.valid === false ? "flagged" : g.valid ? "passed" : "?"}`, g.valid === false ? "red" : "ok");
  }
  if ((r.guardrails || []).length) lightFeature("guardrails");
  lightFeature("budgets"); lightFeature("per_user");
  if (state.served.size >= 2) { lightFeature("open_weight"); }
  if (new Set(state.receipts.map((x) => x.role)).size >= 3) lightFeature("multi_model");
  const total = state.receipts.reduce((a, x) => a + (Number(x.cost_usd) || 0), 0);
  $("#run-totals").innerHTML = `<b>${state.receipts.length}</b> Otari calls · <b>${usd(total)}</b> · <b>${state.served.size}</b> open-weight models`;
}

function onBlocked(ev) {
  setStage(ev.stage, "blocked", "refused");
  const e = ev.error || {};
  const banner = $("#banner");
  banner.hidden = false;
  if (ev.reason === "guardrail") {
    banner.className = "banner guard";
    banner.replaceChildren(icon("shield"), h("div", {},
      h("b", {}, "Stopped before any model saw it"),
      "Wildpatch's wildlife-protection guardrail flagged this request, so Otari refused it at the gateway. ",
      "We never help find, collect or harm sensitive wildlife.",
      h("div", { class: "small" }, h("code", {}, `${e.status} ${e.code || ""}`), " ", e.detail || "")));
    badge(ev.stage, "guardrail blocked", "red");
    lightFeature("guardrails");
  } else {
    banner.className = "banner budget";
    const hrs = e.retry_after ? `${Math.floor(e.retry_after / 3600)} h ${Math.round((e.retry_after % 3600) / 60)} min` : "the next period";
    const who = state.config.volunteers.find((v) => v.id === state.volunteer)?.name || "This volunteer";
    banner.replaceChildren(icon("wallet"), h("div", {},
      h("b", {}, `${who}'s allowance is used up`),
      `Otari enforced this ${e.budget_scope === "user" ? "per-volunteer" : e.budget_scope || ""} budget before calling any provider. It resets in ${hrs}.`,
      h("div", { class: "small" }, h("code", {}, `${e.status} ${e.code || ""}`), " ", e.detail || "")));
    badge(ev.stage, "budget refused", "fb");
    lightFeature("per_user", "warn"); lightFeature("budgets", "warn");
  }
}

function onRouting(ev) {
  const parts = [];
  if (ev.fallback) parts.push(["FALLBACK", "fb"]);
  if (ev.tier_down) parts.push(["BUDGET TIER-DOWN", "fb"]);
  for (const [t, c] of parts) badge("plan", t, c);
  lightFeature("routing", ev.fallback || ev.tier_down ? "warn" : "");
  $("#plan-meta").textContent = `written by ${short(ev.served)} via ${ev.provider || "?"} · policy ${ev.policy}` +
    (ev.fallback ? ` · fell back after ${ev.attempts - 1} failed attempt${ev.attempts > 2 ? "s" : ""}` : "");
}

// ------------------------------------------------------------------ scorecard

function renderScorecard(features) {
  const box = $("#scorecard");
  box.replaceChildren(...FEATURES.map(([key, label, ic]) => {
    const f = features[key] || {};
    return h("span", { class: `feat${f.lit ? " lit" : ""}`, id: `feat-${key}`, title: detailText(f.detail) }, icon(ic), label);
  }));
}
function detailText(d) {
  if (d == null) return "";
  if (Array.isArray(d)) return d.map((x) => (typeof x === "object" ? JSON.stringify(x) : x)).join("\n");
  return String(d);
}
function lightFeature(key, extra = "") {
  const el = document.getElementById(`feat-${key}`);
  if (el) { el.classList.add("lit"); if (extra) el.classList.add(extra); }
}

// ------------------------------------------------------------------ results

function summarise(stage, lines) {
  const s = state.stages[stage];
  if (!s) return;
  s.stream?.remove(); s.stream = null;
  s.body.append(h("div", { class: "summary" }, lines.filter(Boolean)));
}

function onData(ev) {
  state.data[ev.key] = ev.value;
  const v = ev.value || {};
  if (ev.key === "brief") summarise("intake", [h("b", {}, v.summary || ""),
    h("div", { class: "pills" }, [v.site_type, v.area_m2 && `${v.area_m2} m²`, ...(v.goals || []), ...(v.constraints || [])]
      .filter(Boolean).map((t) => h("span", { class: "pill" }, t)))]);
  if (ev.key === "vision") summarise("vision", [h("span", {}, [v.surface, v.light, v.soil_hints].filter(Boolean).join(" · ")),
    h("div", { class: "pills" }, (v.existing_plants || []).map((p) => h("span", { class: "pill" }, `${p.name} (${p.confidence})`)))]);
  if (ev.key === "scout") summarise("scout", [h("div", { class: "pills" }, (v.resources || []).map((r) =>
    h("a", { class: `pill${r.verified ? " ok" : ""}`, href: r.url, target: "_blank", rel: "noopener" }, `${r.kind}: ${r.name}`)))]);
  if (ev.key === "analysis") renderAnalysis(ev.value);
  if (ev.key === "survey" && ev.value.missing?.length) badge("survey", `missing ${ev.value.missing.length}`, "fb");
  if (ev.key === "scout") {
    const n = (ev.value.resources || []).filter((r) => r.verified).length;
    badge("scout", `${n} links verified`, n ? "ok" : "");
  }
}

let waterChart, groupChart;
function renderAnalysis(a) {
  const x = a.sandbox || a.local;
  if (!x) return;
  $("#results").hidden = false;  // Chart.js measures its container, so it must be visible first
  const v = a.verification;
  const ok = v && v.mismatches.length === 0;
  badge("analysis", a.sandbox ? (ok ? "sandbox = local ✓" : `${v.mismatches.length} mismatches`) : "local fallback", a.sandbox && ok ? "ok" : "fb");
  $("#verify-note").textContent = a.sandbox
    ? (ok ? `Computed by the model's own Python in Otari's code sandbox, and re-checked here: ${v.checked} metrics match.`
          : `Sandbox numbers differ from the local check on ${v.mismatches.map((m) => m.metric).join(", ")}.`)
    : "The sandbox did not return numbers, so these come from Wildpatch's local check.";
  const tick = a.sandbox && ok ? h("span", { class: "v" }, icon("check"), "verified") : null;
  const kpi = (val, label) => h("div", { class: "kpi" }, h("b", {}, val), h("span", {}, label), tick?.cloneNode(true));
  const d = x.diversity || {};
  $("#kpis").replaceChildren(
    kpi(`${d.plants?.richness ?? "–"}`, `native plant species · H′ ${d.plants?.shannon ?? "–"}`),
    kpi(`${d.pollinators?.richness ?? "–"}`, `pollinator species · H′ ${d.pollinators?.shannon ?? "–"}`),
    kpi(`${x.native_share_pct != null ? Math.round(x.native_share_pct) : "–"}%`, "of recorded plants are native"),
    kpi(`${Math.round(x.water?.establishment_year_litres || 0).toLocaleString()} L`, "water in the first year"),
    kpi(`${x.planting?.meadow_plugs ?? "–"}`, `meadow plugs on ${Math.round(x.planting?.meadow_m2 || 0)} m²`),
    kpi(`${x.planting?.shrubs ?? "–"} + ${x.planting?.trees ?? "–"}`, "shrubs + trees"));
  const cm = a.dataset?.climate_monthly || {};
  const months = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"];
  waterChart?.destroy();
  waterChart = new Chart($("#chart-water"), {
    data: { labels: months, datasets: [
      { type: "bar", label: "Rain (mm)", data: cm.precip_mm, backgroundColor: "#9cc3d5", yAxisID: "mm", order: 2 },
      { type: "bar", label: "Plants need (mm)", data: (cm.et0_mm || []).map((e) => +(0.5 * e).toFixed(1)), backgroundColor: "#e6c27f", yAxisID: "mm", order: 3 },
      { type: "line", label: "Watering (L)", data: x.water?.litres_by_month, borderColor: "#1f4a33", backgroundColor: "#1f4a33", yAxisID: "l", tension: 0.35, pointRadius: 2, order: 1 },
    ] },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { boxWidth: 10, font: { size: 10 } } } },
      scales: { mm: { position: "left", grid: { color: "#eee8da" }, ticks: { font: { size: 10 } } },
        l: { position: "right", grid: { display: false }, ticks: { font: { size: 10 } } }, x: { grid: { display: false } } } },
  });
  const groups = a.dataset?.richness_by_group || {};
  const entries = Object.entries(groups).sort((p, q) => q[1] - p[1]).slice(0, 8);
  groupChart?.destroy();
  groupChart = new Chart($("#chart-groups"), {
    type: "bar",
    data: { labels: entries.map((e) => e[0]), datasets: [{ data: entries.map((e) => e[1]), backgroundColor: "#4f7a3a", borderRadius: 4 }] },
    options: { responsive: true, maintainAspectRatio: false, indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { grid: { color: "#eee8da" } }, y: { grid: { display: false } } } },
  });
}

function onResult(ev) {
  $("#results").hidden = false;
  $("#plan").innerHTML = DOMPurify.sanitize(marked.parse(ev.plan || state.planText || ""));
  for (const a of $("#plan").querySelectorAll("a")) { a.target = "_blank"; a.rel = "noopener"; }
  const palette = ev.palette?.palette || [];
  $("#palette").replaceChildren(...palette.map((p) => h("div", { class: "plant" },
    p.photo ? h("img", { src: p.photo.replace("square", "medium"), alt: p.scientific_name, loading: "lazy" }) : h("div", { class: "ph" }, icon("leaf")),
    h("div", { class: "plant-body" },
      h("b", {}, p.scientific_name), p.common_name && h("span", {}, p.common_name),
      h("span", { class: "muted" }, p.reason || ""),
      h("div", { class: "plant-tags" },
        h("span", { class: "badge" }, p.layer || "plant"),
        p.quantity ? h("span", { class: "badge" }, `× ${p.quantity}`) : null,
        p.verified_native ? h("a", { class: "badge ok", href: p.inat_url, target: "_blank", rel: "noopener", title: "Observed natively within the survey radius" }, `native · ${p.observations} obs`)
                          : h("span", { class: "badge fb", title: "Not in the native list from the survey" }, "unverified"))))));
  $("#wins").replaceChildren(...(ev.palette?.quick_wins || []).map((w) => h("li", {}, w)));
  $("#resources").replaceChildren(...(ev.scout?.resources || []).map((r) => h("li", {},
    h("span", { class: "kind" }, `${r.kind || "link"}${r.verified ? " · verified link" : ""}`),
    h("a", { href: r.url, target: "_blank", rel: "noopener" }, r.name || r.url), h("span", { class: "muted small" }, r.why || ""))));
  if (!ev.scout?.resources?.length) $("#resources").append(h("li", { class: "muted" }, "No local resources found this time."));
  $("#results").scrollIntoView({ behavior: "smooth", block: "start" });
}

async function refreshInbox() {
  try {
    const box = await (await fetch("/api/inbox")).json();
    const time = (t) => new Date(t * 1000).toLocaleTimeString();
    $("#inbox-mcp").replaceChildren(...box.mcp.slice().reverse().map((c) => h("li", {},
      `${time(c.at)} ${c.tool}(${Object.entries(c.args).map(([k, v]) => `${k}=${v}`).join(", ")}) · ${c.ms} ms `,
      h("span", { class: c.ok ? "pass" : "flag" }, c.ok ? "ok" : c.error))));
    $("#inbox-guard").replaceChildren(...box.guard.slice().reverse().map((g) => h("li", {},
      `${time(g.at)} ${g.profile} · ${g.ms} ms${g.cached ? " (cached)" : ""} `,
      h("span", { class: g.valid === false ? "flag" : "pass" }, g.valid === false ? "FLAGGED" : g.valid ? "passed" : "inconclusive"),
      ` ${g.explanation || ""}`)));
  } catch { /* optional panel */ }
}

// ------------------------------------------------------------------ run

$("#plan-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.running) return;
  state.running = true;
  const go = $("#go");
  go.disabled = true; go.textContent = "Planning…";
  $("#go-hint").textContent = "";
  const body = { lat: state.lat, lon: state.lon, radius_km: state.radius, site_name: $("#site-name").value.trim(),
    area_m2: $("#area").value ? +$("#area").value : null, goals: $("#goals").value.trim(), photo: state.photo,
    volunteer: state.volunteer, drill: $("#drill").checked };
  try {
    const r = await fetch("/api/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `HTTP ${r.status}`);
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
        for (const line of chunk.split("\n")) if (line.startsWith("data: ")) handle(JSON.parse(line.slice(6)));
      }
    }
  } catch (err) {
    $("#go-hint").textContent = String(err.message || err);
  } finally {
    state.running = false;
    go.disabled = false; go.textContent = "Plan this patch";
    if (state.planText) $("#plan").querySelector(".cursor")?.remove();
  }
});

function handle(ev) {
  switch (ev.type) {
    case "run_start": resetRun(ev); break;
    case "stage_start": setStage(ev.stage, "running", "working"); break;
    case "stage_done": setStage(ev.stage, "done", secs(ev.ms)); break;
    case "stage_error": setStage(ev.stage, "error", "skipped"); badge(ev.stage, (ev.error?.code || ev.error?.detail || "error").slice(0, 40), "fb");
      state.stages[ev.stage]?.body.append(h("div", { class: "tool-res err small" }, ev.error?.detail || "")); break;
    case "text": onText(ev); break;
    case "tool_call": onToolCall(ev); break;
    case "tool_result": onToolResult(ev); break;
    case "receipt": onReceipt(ev); break;
    case "data": onData(ev); break;
    case "routing": onRouting(ev); break;
    case "blocked": onBlocked(ev); break;
    case "result": onResult(ev); break;
    case "allowance": state.allowance = { ...(state.allowance || {}), [ev.volunteer]: ev.status }; paintAllowance(ev.volunteer, ev.status); break;
    case "scorecard": renderScorecard(ev.features); break;
    case "run_error": {
      const banner = $("#banner");
      banner.hidden = false; banner.className = "banner info";
      banner.replaceChildren(icon("leaf"), h("div", {}, h("b", {}, "The run stopped early"), ev.error?.detail || "A required stage failed."));
      break;
    }
    case "run_done": {
      for (const id of Object.keys(state.stages)) if (state.stages[id].node.classList.contains("running")) setStage(id, "error", "stopped");
      const t = $("#run-totals");
      t.innerHTML = `<b>${ev.receipts.length}</b> Otari calls · <b>${usd(ev.total_cost_usd)}</b> · <b>${ev.wall_s} s</b> · <b>${state.served.size}</b> open-weight models`;
      refreshInbox();
      break;
    }
  }
}

// ------------------------------------------------------------------ boot

(async function boot() {
  const p = PRESETS[0];
  $("#site-name").value = p.name; $("#area").value = p.area; $("#goals").value = p.goals;
  setSite(p.lat, p.lon);
  state.config = await (await fetch("/api/config")).json();
  const pill = $("#otari-pill");
  if (state.config.otari_configured) {
    $("#otari-status").textContent = `Otari · ${new URL(state.config.base_url).host}`;
  } else {
    pill.classList.add("off");
    $("#otari-status").textContent = "Otari key missing";
    $("#go-hint").textContent = "Set OTARI_API_KEY on the server to run plans.";
  }
  if (!state.config.guardrails) $("#go-hint").textContent += " Guardrails need PUBLIC_URL and GUARD_TOKEN.";
  renderVolunteers();
  renderScorecard({});
  for (const v of state.config.volunteers) refreshAllowance(v.id);
  refreshInbox();
})();
