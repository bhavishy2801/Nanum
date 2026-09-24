"use strict";
/* Relay front end: Google sign-in, role onboarding, and one app shell whose tabs depend on who you are.
   Data refreshes every 3 s and is patched into the page (no flicker, animations don't replay). */

// ---------------------------------------------------------------- basics
if (!window.L) {   // Leaflet is from a CDN: with no internet, stub it so everything but the maps still works
  const noop = new Proxy(function () {}, {get: () => noop, apply: () => noop});
  window.L = noop; window.MAP_OFFLINE = true;
}
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const h = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const kgf = x => String(Math.round((+x || 0) * 10) / 10);
const sleep = ms => new Promise(r => setTimeout(r, ms));
const OPEN = ["posted", "offered"], ACTIVE = ["posted", "offered", "claimed", "picked_up"];
const HEX = {posted: "#94a3b8", offered: "#3b82f6", claimed: "#f59e0b", picked_up: "#8b5cf6", delivered: "#10b981", expired: "#ef4444", diverted: "#b45309"};
const CAT = {cooked: "Cooked meal", dairy: "Dairy / chilled", bakery: "Bakery", produce: "Fruit & veg", packaged: "Packaged", mixed: "Mixed", unknown: "Not sure"};
const STATUS = {posted: "Posted", offered: "Finding a driver", claimed: "Driver on the way", picked_up: "Picked up", delivered: "Delivered", expired: "Lost", diverted: "Diverted"};
const ROLE = {donor: ["Food donor", "🍲"], volunteer: ["Driver", "🛵"], shelter: ["Shelter", "🏠"], admin: ["Admin", "🛡️"]};
const TILE = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png";
const TILE_ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>';
const BLR = [12.9716, 77.5946];
const I = p => `<svg class="i" viewBox="0 0 24 24">${p}</svg>`;
const ICON = {
  pulse: '<path d="M3 12h4l3-8 4 16 3-8h4"/>', box: '<path d="M21 8 12 3 3 8l9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
  pin: '<path d="M12 21s-7-6-7-11a7 7 0 0 1 14 0c0 5-7 11-7 11z"/><circle cx="12" cy="10" r="2.5"/>', chart: '<path d="M4 20V10M10 20V4M16 20v-8M22 20H2"/>',
  bike: '<circle cx="6" cy="17" r="3"/><circle cx="18" cy="17" r="3"/><path d="M6 17l4-8h5l3 8M10 9 8 5H6"/>', home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/><path d="M10 20v-6h4v6"/>',
  flask: '<path d="M9 3h6M10 3v6L4 19a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2L14 9V3"/>', gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  spark: '<path d="m12 3 1.9 5.8L20 11l-6.1 2.2L12 19l-1.9-5.8L4 11l6.1-2.2z"/>', check: '<path d="M5 12l5 5L20 7"/>', x: '<path d="M6 6l12 12M18 6 6 18"/>',
  bell: '<path d="M6 8a6 6 0 1 1 12 0c0 7 3 8 3 8H3s3-1 3-8"/><path d="M10 21h4"/>',
};
const ROUTES = {
  donor: [["donate", "Donate food", "box"], ["mine", "My donations", "pin"], ["dimpact", "My impact", "chart"]],
  volunteer: [["drive", "My rescues", "bike"], ["vimpact", "My impact", "chart"]],
  shelter: [["shelter", "Tonight", "home"], ["received", "Received", "chart"]],
  admin: [["board", "Live board", "pulse"], ["thinks", "How Relay thinks", "spark", true], ["donate", "Donor view", "box"],
          ["drive", "Driver view", "bike"], ["shelter", "Shelter view", "home"], ["lab", "Simulation lab", "flask"],
          ["impact", "Impact", "chart"], ["system", "System", "gear"]],
};
const APP = {cfg: {}, me: null, route: null, S: null, base: 0, online: true, busy: false, pickVol: null, pickRec: null};
const isAdmin = () => APP.me?.role === "admin";

async function api(path, opt = {}) {
  const r = await fetch(path, {headers: {"Content-Type": "application/json"}, credentials: "same-origin", ...opt});
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && APP.me && !path.startsWith("/api/auth")) { toast("Your session ended. Please sign in again.", "info"); setTimeout(() => location.reload(), 1200); }
  if (!r.ok) {
    const d = j.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map(e => `${(e.loc || []).slice(-1)[0]}: ${e.msg}`).join("; ") : `request failed (${r.status})`);
  }
  return j;
}
const store = {get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch (e) { return d; } },
               set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} }};
const clock = t => {   // live minutes -> HH:MM (weekday if not today)
  const d = new Date((APP.base + t * 60) * 1000), hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  return d.toDateString() === new Date().toDateString() ? hm : `${d.toLocaleDateString([], {weekday: "short"})} ${hm}`;
};
const liveNow = () => (Date.now() / 1000 - APP.base) / 60;
const simClock = t => `${String(Math.floor(t / 60) % 24).padStart(2, "0")}:${String(Math.floor(t % 60)).padStart(2, "0")}`;
function dur(m) {
  if (m == null || !isFinite(m)) return "–";
  if (m <= 0) return "now";
  if (m >= 1440) return `${Math.floor(m / 1440)}d ${Math.floor((m % 1440) / 60)}h`;
  if (m >= 60) return `${Math.floor(m / 60)}h ${String(Math.floor(m % 60)).padStart(2, "0")}m`;
  return `${Math.max(1, Math.round(m))} min`;
}
const initials = n => String(n || "?").replace(/\(.*\)/, "").trim().split(/\s+/).filter(w => /\p{L}/u.test(w[0] || "")).map(w => w[0]).join("").slice(0, 2).toUpperCase() || "?";
const status = st => `<span class="status" style="--c:var(--${st})">${h(STATUS[st] || st)}</span>`;
const emptyCard = (title, text, icon = ICON.box) => `<div class="card empty"><div class="art">${I(icon)}</div><b>${h(title)}</b><span class="small">${h(text)}</span></div>`;

// ---------------------------------------------------------------- DOM patching
function morph(a, b) {
  if (a.nodeType !== b.nodeType || a.nodeName !== b.nodeName || (a.nodeType === 1 && a.getAttribute("data-k") !== b.getAttribute("data-k"))) {
    a.replaceWith(b.cloneNode(true)); return;
  }
  if (a.nodeType !== 1) { if (a.nodeValue !== b.nodeValue) a.nodeValue = b.nodeValue; return; }
  for (const at of [...a.attributes]) if (!b.hasAttribute(at.name) && at.name !== "open") a.removeAttribute(at.name);
  for (const at of [...b.attributes]) if (a.getAttribute(at.name) !== at.value) a.setAttribute(at.name, at.value);
  if (["INPUT", "TEXTAREA", "SELECT"].includes(a.tagName)) return;   // keep what the user typed
  morphKids(a, b);
}
function morphKids(a, b) {
  const ak = [...a.childNodes], bk = [...b.childNodes];
  bk.forEach((n, i) => i < ak.length ? morph(ak[i], n) : a.appendChild(n.cloneNode(true)));
  for (let i = bk.length; i < ak.length; i++) ak[i].remove();
}
function setHTML(el, html) {
  if (!el || el._h === html) return;
  el._h = html;
  const t = document.createElement("template");
  t.innerHTML = html;
  morphKids(el, t.content);
}
function list(el, items, key, render, empty) {   // keyed list: items animate in/out, existing ones are patched
  if (!el) return;
  const seen = new Set();
  let prev = null;
  items.forEach((it, i) => {
    const k = String(key(it));
    seen.add(k);
    let node = [...el.children].find(c => c.dataset.key === k && !c.classList.contains("leaving"));
    if (!node) {
      node = document.createElement("div");
      node.className = "item enter";
      node.dataset.key = k;
      node.style.setProperty("--i", i);
      node.addEventListener("animationend", () => node.classList.remove("enter"), {once: true});
    }
    setHTML(node, render(it));
    const want = prev ? prev.nextSibling : el.firstChild;
    if (node !== want) el.insertBefore(node, want);
    prev = node;
  });
  for (const c of [...el.children]) {
    if (c.dataset.key && !seen.has(c.dataset.key) && !c.classList.contains("leaving")) {
      c.classList.add("leaving");
      c.addEventListener("animationend", () => c.remove(), {once: true});
      setTimeout(() => c.remove(), 500);
    }
    if (!c.dataset.key && items.length) c.remove();
  }
  if (!items.length && empty && !el.querySelector(".empty-wrap")) {
    const e = document.createElement("div");
    e.className = "empty-wrap enter";
    e.innerHTML = empty;
    el.appendChild(e);
  }
}
function countTo(el, to, fmt = v => Math.round(v).toLocaleString()) {
  if (!el) return;
  const from = el._v ?? 0;
  el._v = to;
  if (from === to) { el.textContent = fmt(to); return; }
  const t0 = performance.now(), D = 900;
  const step = now => {
    const p = clamp((now - t0) / D, 0, 1), e = 1 - Math.pow(1 - p, 3);
    el.textContent = fmt(from + (to - from) * e);
    if (p < 1 && el._v === to) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}
function kpis(el, items) {   // [id, label, value, fmt?, cls?]
  if (!el.children.length) el.innerHTML = items.map(([id, l, , , cls]) => `<div class="card kpi ${cls || ""}"><div class="v num" id="${id}">0</div><div class="l">${h(l)}</div></div>`).join("");
  for (const [id, , v, fmt] of items) typeof v === "string" ? ($("#" + id).textContent = v) : countTo($("#" + id), v, fmt);
}
function toast(msg, kind = "ok") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="ic">${kind === "err" ? "!" : kind === "info" ? "i" : "✓"}</span><span>${h(msg)}</span>`;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.classList.add("out"); el.addEventListener("animationend", () => el.remove(), {once: true}); }, kind === "err" ? 5200 : 3600);
}
async function busy(btn, fn) {
  if (btn) btn.setAttribute("aria-busy", "true");
  try { return await fn(); } catch (e) { toast(e.message, "err"); } finally { if (btn) btn.removeAttribute("aria-busy"); }
}
function screen(name) { for (const s of ["signin", "onboard", "app"]) $("#scr-" + s).hidden = s !== name; }

// ---------------------------------------------------------------- maps (keyed markers, no flicker)
const MAPS = {};
function mapOn(id, center = BLR, zoom = 12) {
  if (MAPS[id]) { MAPS[id].invalidateSize(); return MAPS[id]; }
  const el = $("#" + id);
  if (!el || !el.offsetWidth) return null;
  if (window.MAP_OFFLINE) el.innerHTML = '<div class="map-off">🗺️ The map needs internet.<br>Everything else works.</div>';
  const m = L.map(id, {zoomControl: true}).setView(center, zoom);
  L.tileLayer(TILE + (APP.cfg.carto_key ? "?key=" + encodeURIComponent(APP.cfg.carto_key) : ""),
              {attribution: TILE_ATTR, subdomains: "abcd", maxZoom: 19}).addTo(m);
  m._items = new Map();
  MAPS[id] = m;
  return m;
}
const houseIcon = cls => L.divIcon({className: "", html: `<span class="house ${cls}">${I(ICON.home)}</span>`, iconSize: [26, 26], iconAnchor: [13, 13]});
const pinIcon = (cls, color) => L.divIcon({className: "", html: `<span class="pin ${cls}" style="--c:${color}"></span>`, iconSize: [18, 18], iconAnchor: [9, 9]});
function sync(m, specs) {   // spec: {key, kind: pin|house|dot|line, loc|locs, cls, color, tip, z}
  if (!m || window.MAP_OFFLINE) return;
  const seen = new Set();
  for (const s of specs) {
    seen.add(s.key);
    const sig = JSON.stringify([s.cls, s.color, s.r]);
    let it = m._items.get(s.key);
    if (!it) {
      it = s.kind === "line" ? L.polyline(s.locs, {color: s.color, weight: 3, opacity: .85, dashArray: s.dash || null})
        : s.kind === "dot" ? L.circleMarker(s.loc, {radius: s.r || 4, color: "#fff", weight: 1, fillColor: s.color, fillOpacity: .95})
        : L.marker(s.loc, {icon: s.kind === "house" ? houseIcon(s.cls || "") : pinIcon(s.cls || "", s.color), zIndexOffset: s.z || 0});
      if (s.tip) it.bindTooltip(s.tip);
      it.addTo(m); it._sig = sig; m._items.set(s.key, it);
    } else {
      if (s.kind === "line") it.setLatLngs(s.locs); else it.setLatLng(s.loc);
      if (it._sig !== sig) {
        if (s.kind === "dot") it.setStyle({fillColor: s.color});
        else if (s.kind !== "line") it.setIcon(s.kind === "house" ? houseIcon(s.cls || "") : pinIcon(s.cls || "", s.color));
        it._sig = sig;
      }
      if (s.tip) it.setTooltipContent(s.tip);
    }
  }
  for (const [k, it] of m._items) if (!seen.has(k)) { it.remove(); m._items.delete(k); }
}
function fitOnce(m, locs, pad = 40) {
  if (!m || m._fitted || !locs.length || window.MAP_OFFLINE) return;
  m.invalidateSize();
  const sz = m.getSize();
  if (sz.x < 50 || sz.y < 50) return;
  if (locs.length === 1) m.setView(locs[0], 14); else m.fitBounds(locs, {padding: [pad, pad], maxZoom: 15});
  m._fitted = true;
}
function picker(id, start, onPick) {   // a small map with a draggable pin
  const m = mapOn(id, start, 13);
  if (!m || m._pin) return m;
  m._pin = L.marker(start, {draggable: true, icon: L.divIcon({className: "", html: '<span class="pin big" style="--c:var(--brand)"></span>', iconSize: [24, 24], iconAnchor: [12, 12]})}).addTo(m);
  m._pin.on("dragend", () => onPick(m._pin.getLatLng()));
  m.on("click", e => { m._pin.setLatLng(e.latlng); onPick(e.latlng); });
  onPick({lat: start[0], lng: start[1]});
  return m;
}

// ---------------------------------------------------------------- boot, sign in, onboarding
(async function boot() {
  try { APP.cfg = await api("/api/config"); } catch (e) { APP.cfg = {}; }
  try { APP.me = await api("/api/me"); } catch (e) { APP.me = null; }
  $("#boot").classList.add("gone");
  setTimeout(() => $("#boot").remove(), 450);
  if (!APP.me) return showSignin();
  if (!APP.me.role) return showOnboard();
  enterApp();
})();

function showSignin() {
  screen("signin");
  $("#demo-box").hidden = !APP.cfg.demo_login;
  if (!APP.cfg.google_client_id) { $("#g-missing").hidden = false; return; }
  const s = document.createElement("script");
  s.src = "https://accounts.google.com/gsi/client"; s.async = true;
  s.onload = () => {
    google.accounts.id.initialize({client_id: APP.cfg.google_client_id, callback: onGoogle, ux_mode: "popup", auto_select: false, cancel_on_tap_outside: true});
    google.accounts.id.renderButton($("#gbtn"), {theme: "outline", size: "large", shape: "pill", text: "continue_with", logo_alignment: "left", width: Math.min(340, $("#gbtn").clientWidth || 340)});
  };
  s.onerror = () => { $("#g-missing").hidden = false; $("#g-missing").textContent = "Couldn't reach Google (offline?). Try again, or use a demo account."; };
  document.head.appendChild(s);
}
async function onGoogle(resp) {
  try { APP.me = await api("/api/auth/google", {method: "POST", body: JSON.stringify({credential: resp.credential})}); afterLogin(); }
  catch (e) { toast(e.message, "err"); }
}
function afterLogin() {
  toast(`Welcome, ${String(APP.me.name || "").split(" ")[0] || "there"}!`);
  APP.me.role ? enterApp() : showOnboard();
}
async function logout() {
  try { await api("/api/auth/logout", {method: "POST"}); window.google?.accounts.id.disableAutoSelect(); } catch (e) {}
  location.hash = ""; location.reload();
}

const OB = {role: null, mode: "new", shelter: null, loc: null, list: []};
function showOnboard() {
  screen("onboard");
  $("#ob-step1").hidden = false; $("#ob-step2").hidden = true;
  $("#ob-d2").classList.remove("on");
  $("#ob-title").textContent = `Hi ${String(APP.me.name || "").split(" ")[0]}, how will you use Relay?`;
}
function obForm() {
  const n = h(APP.me.name || "");
  const map = `<div class="field" style="margin-top:14px"><span id="ob-map-label">Location <span class="hint">(click the map or drag the pin)</span></span><div id="map-ob" class="map sm"></div><span class="hint num" id="ob-loc"></span></div>`;
  if (OB.role === "donor") return `<h2>Tell us about your place</h2><div class="form" style="margin-top:14px">
      <label class="field"><span>Business name</span><input class="input" id="ob-org" maxlength="80" placeholder="e.g. Hotel Saffron, Koramangala"></label>
      <label class="field"><span>Type</span><select class="input" id="ob-kind">${["Restaurant", "Food chain outlet", "Kiosk / street food", "Caterer / events", "Bakery", "Grocery / supermarket", "Hotel", "Campus dining"].map(k => `<option>${k}</option>`).join("")}</select></label></div>${map}`;
  if (OB.role === "volunteer") return `<h2>About you as a driver</h2><div class="form" style="margin-top:14px">
      <label class="field"><span>Name shown to shelters</span><input class="input" id="ob-org" maxlength="80" value="${n}"></label>
      <label class="field"><span>Vehicle</span><select class="input" id="ob-veh"><option value="10">Bicycle · up to 10 kg</option><option value="20" selected>Scooter / bike · up to 20 kg</option><option value="40">Car · up to 40 kg</option><option value="150">Van · up to 150 kg</option></select></label></div>
      ${map.replace("Location", "Where you usually start from")}<p class="hint" style="margin-top:10px">Only your approximate area is used to match you with nearby pickups.</p>`;
  return `<h2>Your shelter or kitchen</h2><div class="seg" style="margin-top:14px"><button class="${OB.mode === "existing" ? "on" : ""}" data-act="ob-mode" data-mode="existing">Manage an existing site</button><button class="${OB.mode === "new" ? "on" : ""}" data-act="ob-mode" data-mode="new">Register a new site</button></div>
    ${OB.mode === "existing" ? `<div class="pick-list">${OB.list.map(s => `<button class="pick-item ${OB.shelter === s.id ? "on" : ""}" data-act="ob-pick" data-id="${h(s.id)}"><b class="small">${h(s.name)}</b><div class="tiny mute">${h(s.zone)}</div></button>`).join("") || '<p class="hint">All sites are already managed. Register a new one.</p>'}</div>`
    : `<div class="form" style="margin-top:14px">
      <label class="field"><span>Site name</span><input class="input" id="ob-org" maxlength="80" placeholder="e.g. Hope Night Shelter"></label>
      <label class="field"><span>Hot food space (kg)</span><input class="input num" id="ob-hot" type="number" min="0" max="1000" value="40"></label>
      <label class="field"><span>Chilled space (kg)</span><input class="input num" id="ob-cold" type="number" min="0" max="1000" value="20"></label>
      <label class="field"><span>Dry space (kg)</span><input class="input num" id="ob-amb" type="number" min="0" max="1000" value="40"></label>
      <label class="field"><span>You serve food</span><select class="input" id="ob-mu"><option value="0">As soon as it arrives</option><option value="30">Within 30 min</option><option value="60">Within 1 hour</option><option value="120">Within 2 hours</option><option value="480">Next morning</option></select></label></div>
      <div class="row wrap" style="gap:22px;margin-top:14px"><label class="switch"><input type="checkbox" id="ob-veg">Vegetarian food only</label><label class="switch"><input type="checkbox" id="ob-conf">Keep our address confidential</label></div>${map}`}`;
}
async function obStep2() {
  if (OB.role === "shelter" && !OB.list.length) { try { OB.list = await api("/api/shelters/unclaimed"); } catch (e) { OB.list = []; } }
  $("#ob-step1").hidden = true; $("#ob-step2").hidden = false; $("#ob-d2").classList.add("on");
  $("#ob-title").textContent = {donor: "Set up your food business", volunteer: "Set up your driver profile", shelter: "Set up your shelter"}[OB.role];
  $("#ob-form").innerHTML = obForm();
  if (MAPS["map-ob"]) { MAPS["map-ob"].remove(); delete MAPS["map-ob"]; }
  if ($("#map-ob")) setTimeout(() => picker("map-ob", OB.loc || [BLR[0] + (Math.random() - .5) * .03, BLR[1] + (Math.random() - .5) * .03], ll => {
    OB.loc = [+ll.lat.toFixed(5), +ll.lng.toFixed(5)]; $("#ob-loc").textContent = `📍 ${OB.loc[0].toFixed(4)}, ${OB.loc[1].toFixed(4)}`;
  }), 60);
}
async function obFinish(btn) {
  const body = {role: OB.role};
  if (OB.role === "shelter" && OB.mode === "existing") {
    const s = OB.list.find(x => x.id === OB.shelter);
    if (!s) return toast("Pick your site from the list", "info");
    Object.assign(body, {shelter_id: s.id, org: s.name, lat: s.loc[0], lon: s.loc[1]});
  } else {
    const org = ($("#ob-org")?.value || "").trim();
    if (org.length < 2) { toast("Please enter a name", "info"); return $("#ob-org")?.focus(); }
    if (!OB.loc) return toast("Pick your location on the map", "info");
    Object.assign(body, {org, lat: OB.loc[0], lon: OB.loc[1]});
    if (OB.role === "donor") body.kind = $("#ob-kind").value;
    if (OB.role === "volunteer") { body.cap_kg = +$("#ob-veh").value; body.kind = $("#ob-veh").selectedOptions[0].text.split(" ·")[0]; }
    if (OB.role === "shelter") Object.assign(body, {hot: +$("#ob-hot").value || 0, cold: +$("#ob-cold").value || 0, ambient: +$("#ob-amb").value || 0,
      serves_after: +$("#ob-mu").value, veg_only: $("#ob-veg").checked, confidential: $("#ob-conf").checked});
  }
  await busy(btn, async () => { APP.me = await api("/api/onboard", {method: "POST", body: JSON.stringify(body)}); toast("You're all set!"); enterApp(); });
}

// ---------------------------------------------------------------- app shell
function enterApp() {
  screen("app");
  const me = APP.me, [label, em] = ROLE[me.role];
  $("#av").innerHTML = me.picture ? `<img src="${h(me.picture)}" alt="" referrerpolicy="no-referrer">` : h(initials(me.name));
  $("#me-name").textContent = me.name || me.email;
  $("#me-email").textContent = me.email;
  $("#me-role").textContent = `${em} ${label}${me.entity_name || me.org ? " · " + (me.entity_name || me.org) : ""}`;
  $("#who-line").textContent = `${label}${me.entity_name || me.org ? " · " + (me.entity_name || me.org) : ""}`;
  $("#sim-pill").hidden = $("#policy-pill").hidden = !isAdmin();
  $("#f-donor-w").hidden = !isAdmin();
  $("#donate-title").textContent = isAdmin() ? "Post as any donor" : "Donate surplus food";
  $("#mine-title").textContent = isAdmin() ? "Posted from this browser" : "Just posted";
  $("#drive-pick-w").hidden = $("#drive-quick-w").hidden = $("#shelter-pick-w").hidden = $("#shelter-quick-w").hidden = !isAdmin();
  $("#tabs").querySelectorAll(".tab").forEach(t => t.remove());
  $("#tabs").insertAdjacentHTML("beforeend", ROUTES[me.role].map(([id, name, ic, isNew]) =>
    `<button class="tab" role="tab" data-act="tab" data-tab="${id}" id="t-${id}">${I(ICON[ic])}${name}${isNew ? ' <span class="new">NEW</span>' : ""}</button>`).join(""));
  go(location.hash.slice(1), false);
  if (!APP.loop) { APP.loop = setInterval(tick, 3000); setInterval(clockTick, 1000); }
  clockTick();
}
function clockTick() {
  const d = new Date();
  $("#clock").textContent = APP.online ? `Live · ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}` : "Offline, retrying…";
  $("#live").classList.toggle("off", !APP.online);
}
function moveInk() {
  const b = $(`#t-${APP.route}`);
  if (b) { $("#ink").style.left = b.offsetLeft + 10 + "px"; $("#ink").style.width = b.offsetWidth - 20 + "px"; }
}
window.addEventListener("resize", moveInk);
document.fonts?.ready.then(moveInk);
function go(id, push = true) {
  const routes = ROUTES[APP.me.role].map(r => r[0]);
  if (!routes.includes(id)) id = routes[0];
  if (APP.route === "thinks" && id !== "thinks") window.XP?.hide();
  APP.route = id;
  $$(".view").forEach(v => v.classList.toggle("on", v.id === "v-" + id));
  $$(".tab").forEach(t => { t.classList.toggle("on", t.dataset.tab === id); t.setAttribute("aria-selected", t.dataset.tab === id); });
  moveInk();
  $(`#t-${id}`)?.scrollIntoView({block: "nearest", inline: "nearest"});
  history.replaceState(null, "", "#" + id);
  window.scrollTo({top: 0});
  if (id === "thinks") window.XP?.show();
  setTimeout(() => { for (const m of Object.values(MAPS)) m.invalidateSize?.(); }, 80);
  tick(true);
}

async function tick(force) {
  if (!APP.me?.role || (APP.busy && !force)) return;
  APP.busy = true;
  try {
    const r = APP.route, me = APP.me;
    if (isAdmin()) {
      APP.S = await api("/api/state");
      APP.base = APP.S.base;
      $("#policy").textContent = PNAME[APP.S.policy] || APP.S.policy;
      const sm = APP.S.sim;
      $("#sim-pill").classList.toggle("sim-off", !sm.running);
      $("#sim-speed").textContent = sm.running ? `running ×${Math.round(sm.speed)}` : "paused";
      $("#sim-toggle").textContent = sm.running ? "Pause city" : "Resume city";
    }
    if (r === "board") renderBoard();
    if (r === "donate") await renderDonate();
    if (r === "mine" || r === "dimpact") await renderDonorHome();
    if (r === "drive" || r === "vimpact") await renderDrive();
    if (r === "shelter" || r === "received") await renderShelter();
    if (r === "impact") await renderImpact();
    if (r === "system") await renderSystem();
    if (!APP.online) toast("Reconnected");
    APP.online = true;
  } catch (e) {
    if (/fetch|network/i.test(e.message)) APP.online = false; else if (force) toast(e.message, "err");
  } finally { APP.busy = false; clockTick(); }
}

// ---------------------------------------------------------------- admin: live board
function ring(p) {
  const tone = p >= .8 ? "ok" : p >= .5 ? "warn" : "bad";
  return `<div class="ring ${tone}" title="Chance a driver claims it before a human is needed"><div class="g"><svg viewBox="0 0 44 44"><circle cx="22" cy="22" r="18" class="track"/><circle cx="22" cy="22" r="18" class="val" style="stroke-dashoffset:${(113.1 * (1 - clamp(p, 0, 1))).toFixed(1)}"/></svg><span class="num">${Math.round(p * 100)}%</span></div><small>claim odds</small></div>`;
}
const whyOpen = new Set();
const vname = id => id === "backup" ? "Backup courier" : (APP.S?.volunteers.find(v => v.id === id)?.name || id);
function rescueCard(d) {
  const S = APP.S, t = liveNow(), age = t - S.now, open = OPEN.includes(d.status), escd = d.escalated && open;
  const left = d.safe_until - t, frac = clamp(left / Math.max(1, d.safe_until - d.posted), 0, 1);
  const tone = frac > .5 ? "ok" : frac > .25 ? "warn" : "bad";
  const slack = d.slack == null ? null : d.slack - age;
  const veg = d.veg === true ? "veg" : d.veg === false ? "non-veg" : "veg unknown";
  const offered = S.offers.filter(o => o.d === d.id && o.status === "sent");
  let who = "";
  if (d.volunteer) who = `<div class="note-line row wrap">${I(ICON.bike).replace('class="i"', 'class="i" width="16" height="16"')}<span><b>${h(vname(d.volunteer))}</b> ${d.status === "picked_up" ? "has the food" : `is on the way · pickup ~${clock(d.eta_pick)}`}</span>${d.volunteer !== "backup" ? `<button class="chip" data-act="open-vol" data-v="${h(d.volunteer)}">Open their view →</button>` : ""}</div>`;
  else if (offered.length) who = `<div class="note-line"><div class="tiny mute" style="margin-bottom:6px">Asked ${offered.length} driver${offered.length > 1 ? "s" : ""} · tap to act as them</div><div class="chips">${offered.slice(0, 6).map(o => `<button class="chip" data-act="open-vol" data-v="${h(o.v)}"><span class="av">${h(initials(vname(o.v)))}</span><span class="clip">${h(vname(o.v))}</span></button>`).join("")}</div></div>`;
  const why = d.why ? `<details class="why" data-why="${h(d.id)}" ${whyOpen.has(d.id) ? "open" : ""}><summary>Why this shelter?</summary><ul>${Object.entries(d.why).map(([n, w]) =>
    `<li class="${w === "chosen" ? "good" : ""}"><b>${w === "chosen" ? "✓" : w.startsWith("feasible") ? "·" : "✕"} ${h(n)}</b><span>${h(w === "chosen" ? "chosen: best fit right now" : w)}</span></li>`).join("")}</ul></details>` : "";
  return `<article class="card rescue lift ${escd ? "is-esc" : ""}">
    <div class="row between"><div class="title"><span class="dot" style="--c:var(--${escd ? "expired" : d.status})"></span><b class="clip">${h(d.donor)}</b>${d.owner === "sim" ? '<span class="tiny mute">sim</span>' : ""}</div>${status(d.status)}</div>
    <p class="sub">${kgf(d.kg)} kg · ${h(CAT[d.category] || d.category)} · ${veg} · ~${Math.round(d.meals)} meals → <b>${h(d.recipient_name || "finding a shelter…")}</b></p>
    <div class="metrics"><div class="meter"><div class="row between small"><span class="mute">Safe to eat for</span><b class="num">${dur(left)}</b></div><div class="bar"><i class="${tone}" style="width:${(frac * 100).toFixed(1)}%"></i></div><div class="tiny mute">until ${clock(d.safe_until)}${open && d.L != null ? ` · human needed by <b>${clock(d.L)}</b>` : ""}</div></div>
      ${open && d.pclaim != null ? ring(d.pclaim) : ""}</div>
    ${open && slack != null ? `<p class="tiny mute" style="margin-top:12px">Latest pickup in <b class="num">${dur(slack)}</b> · ${d.eligible} drivers could still make it</p>` : ""}
    ${who}
    ${escd ? `<div class="alert-strip"><span>${I(ICON.bell).replace('class="i"', 'class="i" width="16" height="16"')} Needs a human: act before <b>${d.L != null ? clock(d.L) : "soon"}</b></span><button class="btn danger sm" data-act="backup" data-d="${h(d.id)}">Send backup courier</button></div>` : ""}
    ${why}</article>`;
}
function renderBoard() {
  const S = APP.S, ds = S.donations;
  const active = ds.filter(d => ACTIVE.includes(d.status)), human = active.filter(d => d.escalated && OPEN.includes(d.status));
  const done = ds.filter(d => d.status === "delivered");
  countTo($("#k-active"), active.length); countTo($("#k-human"), human.length);
  $("#k-human-card").classList.toggle("alert", human.length > 0);
  countTo($("#k-kg"), done.reduce((a, d) => a + d.kg, 0), v => kgf(v)); countTo($("#k-meals"), done.reduce((a, d) => a + d.meals, 0));
  const rank = d => (d.escalated && OPEN.includes(d.status) ? 0 : OPEN.includes(d.status) ? 1 : 2);
  active.sort((a, b) => rank(a) - rank(b) || (a.slack ?? 1e9) - (b.slack ?? 1e9) || a.safe_until - b.safe_until);
  $("#q-count").textContent = active.length ? `${active.length} active` : "";
  list($("#queue"), active, d => d.id, rescueCard, emptyCard("All quiet", "No active rescues right now. Post a test donation, or wait for the simulated city.", ICON.check));
  const closed = ds.filter(d => !ACTIVE.includes(d.status)).sort((a, b) => (b.t_drop ?? b.posted) - (a.t_drop ?? a.posted));
  $("#closed-n").textContent = closed.length ? `(${closed.length})` : "";
  list($("#closed"), closed.slice(0, 30), d => d.id, d => `<div class="closed-row"><span class="dot" style="--c:var(--${d.status})"></span><b class="clip grow">${h(d.donor)}</b><span class="num mute">${kgf(d.kg)} kg</span>${status(d.status)}</div>`,
    '<div class="tiny mute" style="padding:8px 4px">Nothing closed yet.</div>');
  const m = mapOn("map-board");
  if (!m) return;
  const rec = Object.fromEntries(S.recipients.map(r => [r.id, r])), specs = [];
  for (const r of S.recipients) specs.push({key: "r" + r.id, kind: "house", loc: r.loc, cls: r.full.length ? "full" : "", z: 200, tip: `<b>${h(r.name)}</b>${r.full.length ? "<br>full for " + h(r.full.join(", ")) : ""}`});
  for (const v of S.volunteers) specs.push({key: "v" + v.id, kind: "dot", loc: v.loc, color: v.busy ? "#f59e0b" : "#cbd5e1", tip: h(v.name)});
  for (const d of ds) {
    const escd = d.escalated && OPEN.includes(d.status), fin = !ACTIVE.includes(d.status);
    specs.push({key: "d" + d.id, kind: "pin", loc: d.loc, cls: (escd ? "esc " : "") + (fin ? "done" : ""), color: escd ? "var(--expired)" : `var(--${d.status})`, z: fin ? 0 : 500, tip: `<b>${h(d.donor)}</b><br>${kgf(d.kg)} kg · ${h(STATUS[d.status])}`});
    const r = rec[d.recipient];
    if (r && ACTIVE.includes(d.status)) specs.push({key: "l" + d.id + d.status, kind: "line", locs: [d.loc, r.loc], color: HEX[d.status], dash: OPEN.includes(d.status) ? "6 8" : null});
  }
  sync(m, specs);
  fitOnce(m, S.recipients.map(r => r.loc));
}
document.addEventListener("toggle", e => { const id = e.target.dataset?.why; if (id) e.target.open ? whyOpen.add(id) : whyOpen.delete(id); }, true);

// ---------------------------------------------------------------- donor
$("#f-category").innerHTML = Object.entries(CAT).map(([k, v]) => `<option value="${k}">${h(v)}</option>`).join("");
$("#f-category").addEventListener("change", () => { $("#f-label-w").hidden = $("#f-category").value !== "packaged"; });
let pickLoc = null, posted = store.get("relay_posted", []);
function ensurePick() {
  const start = APP.me.lat != null ? [APP.me.lat, APP.me.lon] : [BLR[0] + (Math.random() - .5) * .03, BLR[1] + (Math.random() - .5) * .03];
  picker("map-pick", start, ll => { pickLoc = [+ll.lat.toFixed(5), +ll.lng.toFixed(5)]; $("#f-loc").textContent = `📍 ${pickLoc[0].toFixed(4)}, ${pickLoc[1].toFixed(4)}`; });
  if (window.MAP_OFFLINE && !pickLoc) { pickLoc = start; $("#f-loc").textContent = `📍 ${start[0].toFixed(4)}, ${start[1].toFixed(4)}`; }
}
async function parseText(btn) {
  const text = $("#d-text").value.trim();
  if (!text) { toast("Type or paste what you have first", "info"); return $("#d-text").focus(); }
  await busy(btn, async () => {
    const x = await api("/api/intake/parse", {method: "POST", body: JSON.stringify({text})});
    $("#f-category").value = x.category; $("#f-holding").value = x.holding; $("#f-veg").value = x.veg == null ? "" : String(x.veg);
    let until = x.ready_until ?? "";
    if (until) {   // "we close 11" said after 23:00 has already passed: use the default 2-hour window instead
      const [hh, mm] = until.split(":").map(Number), d = new Date(), ahead = (hh * 60 + mm - (d.getHours() * 60 + d.getMinutes()) + 1440) % 1440;
      if (ahead > 720) { toast(`${until} has already passed, so we'll assume the food is available for the next 2 hours.`, "info"); until = ""; }
    }
    $("#f-kg").value = x.kg ?? ""; $("#f-plates").value = x.plates ?? ""; $("#f-until").value = until;
    $("#f-label-w").hidden = x.category !== "packaged";
    for (const id of ["#f-category", "#f-holding", "#f-veg", "#f-kg", "#f-plates", "#f-until"]) { const el = $(id); el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
    $("#d-parsed").innerHTML = `<span class="parsed">✓ Understood (${x.source === "regex" ? "rules" : "AI + rules"}). Please check below.</span>`;
    if (x.warnings?.length) toast(x.warnings.join("; "), "info");
    if (!x.kg) toast("Couldn't find a weight. Please add kg.", "info");
  });
}
async function postDonation(btn) {
  const kg = +$("#f-kg").value;
  if (!(kg > 0)) { toast("Please enter the weight in kg", "info"); return $("#f-kg").focus(); }
  if (!pickLoc) return toast("Pick the pickup location on the map", "info");
  const veg = $("#f-veg").value, lab = $("#f-label").value, cat = $("#f-category").value;
  const body = {donor: $("#f-donor").value.trim() || null, category: cat, holding: $("#f-holding").value, veg: veg === "" ? null : veg === "true", kg,
    plates: $("#f-plates").value ? +$("#f-plates").value : null, ready_until: $("#f-until").value || null, lat: pickLoc[0], lon: pickLoc[1],
    text: $("#d-text").value, label_until: cat === "packaged" && lab ? (new Date(lab).getTime() / 1000 - APP.base) / 60 : null};
  await busy(btn, async () => {
    const d = await api("/api/donations", {method: "POST", body: JSON.stringify(body)});
    posted = [{id: d.id, posted: d.posted}, ...posted.filter(m => m.id !== d.id)].slice(0, 20);
    store.set("relay_posted", posted);
    toast(d.recipient_name ? `Posted! Heading to ${d.recipient_name}; asking drivers now.` : "Posted! Finding a shelter…");
    $("#d-text").value = ""; $("#d-parsed").innerHTML = "";
    await tick(true);
  });
}
function donationCard(d) {
  const failed = d.status === "expired" || d.status === "diverted";
  const steps = ["Posted", "Matched", "Claimed", "Picked up", "Delivered"];
  const r = failed ? (d.t_pick ? 3 : d.volunteer ? 2 : d.recipient ? 1 : 0) : {posted: d.recipient ? 1 : 0, offered: 1, claimed: 2, picked_up: 3, delivered: 4}[d.status];
  const cls = i => i < r || (i === r && (failed || d.status === "delivered")) ? "done" : failed && i === r + 1 ? "fail" : i === r ? "cur" : "";
  let msg;
  if (d.status === "delivered") msg = `✅ Delivered to <b>${h(d.recipient_name)}</b> at ${clock(d.t_drop)} · ${kgf(d.kg)} kg · ~${Math.round(d.meals)} meals. Thank you!`;
  else if (failed) msg = `We couldn't rescue this one (${h(d.end_reason || d.status)}). Please dispose of it safely. We're sorry.`;
  else if (d.volunteer) msg = d.status === "picked_up" ? `<b>${h(d.volunteer_name)}</b> has it, on the way to ${h(d.recipient_name)}.` : `<b>${h(d.volunteer_name)}</b> is coming. Pickup around <b>${clock(d.eta_pick)}</b>.`;
  else if (d.status === "posted" && d.recipient_name) msg = `Matched to <b>${h(d.recipient_name)}</b>. There's time, so Relay is choosing the best moment to ask drivers.`;
  else if (d.pclaim != null) msg = d.pclaim >= .8 ? `Asking drivers now. Very likely to be claimed (${Math.round(d.pclaim * 100)}%).` : `Drivers are scarce right now (${Math.round(d.pclaim * 100)}%). Our coordinator has been alerted.`;
  else msg = "Finding the right shelter…";
  return `<article class="card lift"><div class="row between" style="align-items:flex-start"><b class="grow" style="overflow-wrap:anywhere">${h(d.donor)} · ${kgf(d.kg)} kg ${h(CAT[d.category] || d.category)}</b>${status(d.status)}</div>
    <div class="steps">${steps.map((s, i) => `<div class="s ${cls(i)}"><i>${cls(i) === "done" ? I(ICON.check) : cls(i) === "fail" ? I(ICON.x) : ""}</i><span>${s}</span></div>`).join("")}</div>
    <p class="small" style="margin-top:10px">${msg}</p><p class="tiny mute" style="margin-top:6px">Safe to eat until ${clock(d.safe_until)}${d.recipient_name ? ` · going to ${h(d.recipient_name)}` : ""}</p></article>`;
}
async function renderDonate() {
  ensurePick();
  let ds;
  if (isAdmin()) ds = posted.map(m => APP.S.donations.find(d => d.id === m.id && Math.abs(d.posted - m.posted) < .01)).filter(Boolean);
  else { const x = await api("/api/donor/home"); APP.base = x.base; APP.home = x; ds = x.donations.slice(0, 5); }
  list($("#mine"), ds, d => d.id, donationCard, emptyCard("Nothing posted yet", "Your donations and their live progress appear here."));
}
async function renderDonorHome() {
  const x = await api("/api/donor/home");
  APP.base = x.base; APP.home = x;
  if (APP.route === "mine") {
    list($("#mine-all"), x.donations, d => d.id, donationCard, emptyCard("No donations yet", "Post from the Donate food tab. You'll follow it here in real time."));
    const m = mapOn("map-mine", [x.profile.lat || BLR[0], x.profile.lon || BLR[1]], 13);
    if (!m) return;
    const specs = [], locs = [];
    for (const d of x.donations.filter(d => ACTIVE.includes(d.status) || d.status === "delivered").slice(0, 12)) {
      const done = d.status === "delivered";
      specs.push({key: "d" + d.id, kind: "pin", loc: d.loc, color: `var(--${d.status})`, cls: done ? "done" : "", z: 300, tip: `<b>${kgf(d.kg)} kg ${h(CAT[d.category])}</b><br>${h(STATUS[d.status])}`});
      locs.push(d.loc);
      if (d.recipient_loc) { specs.push({key: "r" + d.id, kind: "house", loc: d.recipient_loc, tip: h(d.recipient_name)}); locs.push(d.recipient_loc);
        if (!done) specs.push({key: "l" + d.id + d.status, kind: "line", locs: [d.loc, d.recipient_loc], color: HEX[d.status], dash: OPEN.includes(d.status) ? "6 8" : null}); }
      if (d.volunteer_loc && d.status === "claimed") specs.push({key: "v" + d.id, kind: "dot", loc: d.volunteer_loc, color: "#f59e0b", r: 6, tip: `${h(d.volunteer_name)} (driver)`});
    }
    sync(m, specs);
    fitOnce(m, locs.length ? locs : [[x.profile.lat || BLR[0], x.profile.lon || BLR[1]]]);
  } else {
    const i = x.impact;
    kpis($("#dimp-kpis"), [["di-kg", "kg rescued", i.kg, v => kgf(v), "good"], ["di-meals", "meals served", i.meals, undefined, "good"], ["di-del", "deliveries", i.deliveries], ["di-post", "posted", i.posted]]);
    const done = x.donations.filter(d => d.status === "delivered");
    setHTML($("#dimp-list"), done.length ? done.map(d => `<div class="hist-row"><span class="dot" style="--c:var(--delivered)"></span><b class="grow clip">${kgf(d.kg)} kg ${h(CAT[d.category])} → ${h(d.recipient_name)}</b><span class="num mute">~${Math.round(d.meals)} meals · ${clock(d.t_drop)}</span></div>`).join("")
      : '<div class="empty small">Your first delivery will show up here.</div>');
  }
}

// ---------------------------------------------------------------- driver
function driveTarget() {
  if (!isAdmin()) return APP.me.entity;
  const S = APP.S, sel = $("#drive-pick");
  if (sel.options.length !== S.volunteers.length) sel.innerHTML = [...S.volunteers].sort((a, b) => a.name.localeCompare(b.name)).map(v => `<option value="${h(v.id)}">${h(v.name)} · ${h(v.zone)}</option>`).join("");
  const ids = [...new Set([...S.donations.filter(d => ["claimed", "picked_up"].includes(d.status) && d.volunteer !== "backup").map(d => d.volunteer), ...S.offers.filter(o => o.status === "sent").map(o => o.v)])].slice(0, 8);
  if (!APP.pickVol || !S.volunteers.some(v => v.id === APP.pickVol)) APP.pickVol = ids[0] || S.volunteers[0]?.id;
  sel.value = APP.pickVol;
  setHTML($("#drive-quick"), ids.length ? ids.map(id => `<button class="chip" data-act="open-vol" data-v="${h(id)}"><span class="av">${h(initials(vname(id)))}</span><span class="clip">${h(vname(id))}</span></button>`).join("") : '<span class="tiny mute">Nobody has an offer right now.</span>');
  return APP.pickVol;
}
async function renderDrive() {
  const vid = driveTarget();
  if (!vid) return;
  const x = await api(`/api/volunteers/${encodeURIComponent(vid)}`), v = x.volunteer;
  if (APP.route === "vimpact") {
    kpis($("#vimp-kpis"), [["vi-del", "deliveries", x.stats.deliveries, undefined, "good"], ["vi-kg", "kg carried", x.stats.kg, v => kgf(v), "good"], ["vi-meals", "meals served", x.stats.meals], ["vi-week", "offers this week", v.n7]]);
    setHTML($("#vimp-list"), x.history.length ? x.history.map(d => `<div class="hist-row"><span class="dot" style="--c:var(--delivered)"></span><b class="grow clip">${h(d.donor)} → ${h(d.shelter)}</b><span class="num mute">${kgf(d.kg)} kg · ${h(d.at)}</span></div>`).join("")
      : '<div class="empty small">Your deliveries will show up here.</div>');
    return;
  }
  $("#drive-title").textContent = isAdmin() ? `Driver view · ${v.name}` : "My rescues";
  const on = $("#online"); if (document.activeElement !== on) on.checked = x.online;
  $("#online-state").textContent = x.online ? (x.available ? "You're online" : "On a rescue") : "You're offline";
  $("#drive-info").textContent = `${kgf(v.cap_kg)} kg vehicle · ${v.n7} offers this week · ${v.zone}`;
  setHTML($("#drive-stats"), [["deliveries", x.stats.deliveries], ["kg", kgf(x.stats.kg)], ["meals", Math.round(x.stats.meals)]].map(([l, n]) => `<span class="chip static"><b class="num">${n}</b>&nbsp;${l}</span>`).join(""));
  const j = x.job;
  setHTML($("#drive-job"), j ? `<article class="card" data-k="job-${h(j.d)}" style="border-color:color-mix(in srgb,var(--brand) 45%,var(--line));margin-bottom:16px;animation:up .5s var(--ease)">
    <div class="row between"><h2>Your rescue: ${h(j.donor)}</h2>${status(j.status)}</div>
    <div class="steps"><div class="s done"><i>${I(ICON.check)}</i><span>Accepted</span></div><div class="s ${j.status === "picked_up" ? "done" : "cur"}"><i>${j.status === "picked_up" ? I(ICON.check) : ""}</i><span>Picked up</span></div><div class="s ${j.status === "picked_up" ? "cur" : ""}"><i></i><span>Delivered</span></div></div>
    <div class="grid-2e" style="margin-top:14px;gap:12px">
      <div class="card" style="background:var(--surface-2);box-shadow:none;padding:12px"><div class="tiny mute">Pick up</div><b class="small">${kgf(j.kg)} kg ${h(CAT[j.category] || j.category)} by ${h(j.pickup_by)}</b><div class="tiny mute num">${j.pickup.map(a => a.toFixed(4)).join(", ")}</div></div>
      <div class="card" style="background:var(--surface-2);box-shadow:none;padding:12px"><div class="tiny mute">Drop off</div><b class="small">${h(j.recipient)}</b><div class="tiny mute num">${j.dropoff ? j.dropoff.map(a => a.toFixed(4)).join(", ") : "address shared by the coordinator"}</div></div></div>
    ${j.status === "claimed" ? `<div class="row wrap" style="margin-top:16px;gap:18px"><label class="switch"><input type="checkbox" id="c-temp">Still hot/cold as stated</label><label class="switch"><input type="checkbox" id="c-pack">Packed and covered</label></div>
      <div class="row wrap" style="margin-top:14px"><button class="btn" data-act="pickup" data-d="${h(j.d)}">Picked up</button><button class="btn ghost" data-act="cancel" data-d="${h(j.d)}">I can't make it</button></div>
      <p class="hint" style="margin-top:8px">Untick a box if the food isn't right. It gets diverted, never delivered.</p>`
    : `<div class="row wrap" style="margin-top:16px"><input class="input code-in num" id="c-code" inputmode="numeric" maxlength="4" placeholder="0000" aria-label="Handover code"><button class="btn" data-act="deliver" data-d="${h(j.d)}">Confirm delivery</button></div>
      <p class="hint" style="margin-top:8px">Ask the shelter for their 4-digit code (it's on their Tonight screen).</p>`}</article>` : "");
  list($("#drive-offers"), x.offers, o => o.offer, o => `<article class="card lift"><div class="row between"><b>${kgf(o.kg)} kg ${h(CAT[o.category] || o.category)}${o.veg === true ? " · veg" : ""}</b><span class="status" style="--c:var(--offered)">${o.km} km away</span></div>
    <p class="small mute" style="margin-top:6px">~${Math.round(o.meals)} meals · around ${o.area.map(a => a.toFixed(3)).join(", ")} (exact address after you accept) · eat by <b>${h(o.use_by)}</b>${o.respond_by ? ` · reply by <b>${h(o.respond_by)}</b>` : ""}</p>
    <div class="row" style="margin-top:12px"><button class="btn" data-act="accept" data-o="${h(o.offer)}">Accept</button><button class="btn ghost" data-act="decline" data-o="${h(o.offer)}">Not this time</button></div></article>`,
    emptyCard(x.online ? "No offers right now" : "You're offline", x.online ? "Relay only pings you when you're a good fit. Keep this open." : "Go online to receive offers.", ICON.bike));
  const m = mapOn("map-drive", v.loc, 13);
  if (!m) return;
  const specs = [{key: "me", kind: "pin", loc: v.loc, color: "var(--brand)", cls: "big", z: 900, tip: "You"}], locs = [v.loc];
  for (const o of x.offers) { specs.push({key: "o" + o.offer, kind: "pin", loc: o.area, color: "var(--offered)", tip: `${kgf(o.kg)} kg · ${o.km} km (approximate)`}); locs.push(o.area); }
  if (j) { specs.push({key: "jp" + j.d, kind: "pin", loc: j.pickup, color: "var(--claimed)", cls: "big", z: 800, tip: "Pickup"}); locs.push(j.pickup);
    if (j.dropoff) { specs.push({key: "jd" + j.d, kind: "house", loc: j.dropoff, cls: "me", tip: h(j.recipient)}, {key: "jl" + j.d + j.status, kind: "line", locs: [j.pickup, j.dropoff], color: HEX[j.status]}); locs.push(j.dropoff); } }
  sync(m, specs);
  const sig = locs.map(l => l.join()).join("|");
  if (m._sig !== sig) { m._fitted = false; m._sig = sig; }
  fitOnce(m, locs, 50);
}

// ---------------------------------------------------------------- shelter
let capFor = null;
function shelterTarget() {
  if (!isAdmin()) return APP.me.entity;
  const S = APP.S, sel = $("#shelter-pick");
  if (sel.options.length !== S.recipients.length) sel.innerHTML = S.recipients.map(r => `<option value="${h(r.id)}">${h(r.name)} · ${h(r.zone)}</option>`).join("");
  const ids = [...new Set(S.donations.filter(d => ACTIVE.includes(d.status) && d.recipient).map(d => d.recipient))].slice(0, 8);
  if (!APP.pickRec || !S.recipients.some(r => r.id === APP.pickRec)) APP.pickRec = S.donations.find(d => ["claimed", "picked_up"].includes(d.status))?.recipient || ids[0] || S.recipients[0]?.id;
  sel.value = APP.pickRec;
  setHTML($("#shelter-quick"), ids.length ? ids.map(id => `<button class="chip" data-act="open-rec" data-r="${h(id)}">${I(ICON.home).replace('class="i"', 'class="i" width="14" height="14"')}${h(S.recipients.find(r => r.id === id)?.name || id)}</button>`).join("") : '<span class="tiny mute">No food heading to any shelter yet.</span>');
  return APP.pickRec;
}
function capForm(r) {
  return [["hot", "Hot food"], ["cold", "Chilled"], ["ambient", "Dry / room temp"]].map(([g, n]) => `<div class="row wrap between" style="padding:10px 0;border-bottom:1px solid var(--line);gap:14px">
      <div style="min-width:120px"><b class="small">${n}</b><div class="tiny mute">kg free tonight</div></div>
      <input class="input num" style="max-width:120px" id="cap-${g}" type="number" min="0" max="1000" step="1" value="${Math.max(0, Math.round(r.cap[g] ?? 0))}">
      <label class="switch red"><input type="checkbox" id="full-${g}" ${r.full.includes(g) ? "checked" : ""}>Full</label></div>`).join("") +
    `<div class="row" style="margin-top:14px"><button class="btn" data-act="save-cap">Save</button><span class="hint">Relay re-plans right away.</span></div>`;
}
async function renderShelter(forceForm) {
  const rid = shelterTarget();
  if (!rid) return;
  const x = await api(`/api/recipients/${encodeURIComponent(rid)}`), r = x.recipient;
  if (APP.route === "received") {
    kpis($("#rec-kpis"), [["ri-del", "handovers", x.stats.deliveries, undefined, "good"], ["ri-kg", "kg received", x.stats.kg, v => kgf(v), "good"], ["ri-meals", "meals", x.stats.meals], ["ri-now", "on the way now", x.arriving.length]]);
    setHTML($("#rec-list"), x.received.length ? x.received.map(d => `<div class="hist-row"><span class="dot" style="--c:var(--delivered)"></span><b class="grow clip">${h(d.donor)}</b><span class="num mute">${kgf(d.kg)} kg · ~${Math.round(d.meals)} meals · ${h(d.at)}</span></div>`).join("")
      : '<div class="empty small">Handovers will show up here.</div>');
    return;
  }
  $("#shelter-title").textContent = isAdmin() ? `Shelter view · ${r.name}` : `Tonight at ${r.name}`;
  if (forceForm || capFor !== rid) { $("#r-cap").innerHTML = capForm(r); capFor = rid; }
  setHTML($("#r-tags"), [r.veg_only ? "🥗 Veg only" : "🍽️ Veg & non-veg", r.mu ? `⏱️ Serves ${r.mu >= 60 ? r.mu / 60 + " h" : r.mu + " min"} after food arrives` : "⏱️ Serves on arrival",
    r.confidential ? "🔒 Confidential address" : "📍 Public address", `Area: ${r.zone}`].map(t => `<span class="chip static">${h(t)}</span>`).join(""));
  list($("#r-arr"), x.arriving, a => a.d, a => `<article class="card lift"><div class="row between wrap"><div class="grow"><b class="clip">${h(a.donor)}</b><div class="small mute">${kgf(a.kg)} kg ${h(CAT[a.category] || a.category)} · ~${Math.round(a.meals)} meals · ${h(a.volunteer)} · ${a.status === "picked_up" ? "on the way now" : "pickup ~" + h(a.eta_pickup)}</div></div>
    <div style="text-align:center"><div class="tiny mute">Handover code</div><div class="big-num num" style="letter-spacing:.12em;color:var(--brand)">${h(a.code || "—")}</div></div></div></article>`,
    emptyCard("Nothing on the way", "When a driver claims food for you, it shows here with a handover code.", ICON.home));
  setHTML($("#r-held"), x.held.length ? `<p class="small mute">Space held for you while we find a driver: ${x.held.map(hd => `${kgf(hd.kg)} kg ${h(CAT[hd.category] || hd.category)}`).join(", ")}</p>` : "");
}

// ---------------------------------------------------------------- admin: impact, system
async function renderImpact() {
  const x = await api("/api/impact");
  kpis($("#i-kpis"), [["i-kg", "kg rescued", x.kg, v => kgf(v), "good"], ["i-meals", "meals served", x.meals, undefined, "good"], ["i-del", "deliveries", x.deliveries], ["i-co2", "kg CO₂e avoided", x.co2e_kg == null ? "not set" : x.co2e_kg]]);
  const recs = Object.entries(x.by_recipient).sort((a, b) => b[1].kg - a[1].kg), maxKg = Math.max(1, ...recs.map(r => r[1].kg));
  setHTML($("#i-rec"), recs.length ? recs.map(([n, r]) => `<div class="hbar"><span class="clip">${h(n)}</span><span class="track"><i style="width:${(100 * r.kg / maxKg).toFixed(1)}%"></i></span><span class="val num">${kgf(r.kg)} kg</span></div>`).join("") : '<div class="empty small">No deliveries yet.</div>');
  const zs = Object.entries(x.by_zone).sort((a, b) => a[0].localeCompare(b[0]));
  setHTML($("#i-zone"), zs.length ? zs.map(([n, z]) => { const p = z.posted_kg ? z.delivered_kg / z.posted_kg : 0;
    return `<div class="hbar"><span class="clip">${h(n)}</span><span class="track"><i style="width:${(100 * p).toFixed(1)}%"></i></span><span class="val num">${Math.round(100 * p)}%</span></div>`; }).join("") : '<div class="empty small">No donations yet.</div>');
  setHTML($("#i-method"), `<b>How we count.</b> ${h(x.method)}`);
}
const ROLE_C = {admin: "var(--brand-3)", donor: "#ea580c", volunteer: "var(--claimed)", shelter: "var(--offered)"};
async function renderSystem() {
  const x = await api("/api/admin/overview");
  $("#sys-up").textContent = `up ${Math.round(x.uptime_min)} min · ${x.now}`;
  list($("#health"), x.health, c => c.name, c => `<div class="hcheck ${c.ok ? "" : c.warn ? "warn" : "bad"}" style="animation:none"><span class="ic">${c.ok ? "✓" : c.warn ? "!" : "✕"}</span><div><b>${h(c.name)}</b><span>${h(c.detail)}</span></div></div>`);
  const sm = x.sim;
  if (document.activeElement?.id !== "sys-speed") { $("#sys-speed").value = sm.speed; $("#sys-speed-v").textContent = `×${Math.round(sm.speed)}`; }
  if (document.activeElement?.id !== "sys-every") { $("#sys-every").value = sm.every; $("#sys-every-v").textContent = `${Math.round(sm.every)} s`; }
  $("#sys-run").checked = sm.running;
  kpis($("#sys-sim"), [["ss-post", "sim donations", sm.posted || 0], ["ss-acc", "sim accepts", sm.accepted || 0], ["ss-pick", "sim pickups", sm.picked_up || 0], ["ss-del", "sim deliveries", sm.delivered || 0, undefined, "good"]]);
  list($("#users"), x.users, u => u.email, u => `<div class="urow"><div class="row"><span class="avatar" style="width:28px;height:28px;font-size:11px">${u.picture ? `<img src="${h(u.picture)}" alt="" referrerpolicy="no-referrer">` : h(initials(u.name))}</span><div class="grow"><b class="small clip" style="display:block">${h(u.name)}</b><span class="tiny mute clip" style="display:block">${h(u.email)}</span></div></div>
      <span><span class="role-badge" style="--c:${ROLE_C[u.role] || "var(--mute)"}">${h(u.role ? ROLE[u.role][0] : "choosing…")}</span></span><span class="small clip">${h(u.org || u.entity || "—")}</span>
      <span>${u.role && u.role !== "admin" ? `<button class="btn ghost sm" data-act="reset-user" data-email="${h(u.email)}">Reset</button>` : ""}</span></div>`,
    '<div class="tiny mute" style="padding:10px 0">No one has signed in yet.</div>');
  list($("#events"), x.events.slice(0, 60).reverse().map((e, i, a) => ({...e, k: `${e.t}|${e.text}|${a.slice(0, i).filter(z => z.t === e.t && z.text === e.text).length}`})).reverse(), e => e.k,
    e => `<div class="ev"><time>${h(e.t)}</time><span class="grow">${h(e.text)}</span>${e.sim ? '<span class="tag sim">sim</span>' : ""}</div>`);
}

// ---------------------------------------------------------------- admin: simulation lab
const SCEN = [["bengaluru", "Bengaluru · real data (recommended)"], ["bengaluru_strict", "Bengaluru · shelters' listed hours"], ["friday_night", "Synthetic Friday night"],
  ["normal_weekday", "Synthetic quiet weekday"], ["surge", "Synthetic surge (3× food)"], ["volunteer_drought", "Synthetic driver drought"],
  ["recipient_closure", "Synthetic shelter closure"], ["rain", "Synthetic rainy night"], ["sparse_suburb", "Synthetic thin suburbs"]];
$("#scen").innerHTML = SCEN.map(([k, n]) => `<option value="${k}">${h(n)}</option>`).join("");
const PNAME = {B0: "WhatsApp broadcast", B1: "412FR static rule", Relay: "Relay (rules)", "Relay-ML": "Relay + ML", "Relay-RL": "Relay + RL",
  "Relay-RL lam=0.25": "Relay + RL (burnout-aware)", "BC-greedy": "Imitation (BC)", "Relay -p model": "Relay, no p model", "Relay -early esc": "Relay, no early alert"};
let demo = null, frame = 0, timer = null;
async function runDemo(btn) {
  await busy(btn, async () => {
    $("#lab-player").innerHTML = '<div class="grid-2e"><div class="skel" style="height:360px"></div><div class="skel" style="height:360px"></div></div>';
    demo = await api(`/api/sim/demo?scenario=${$("#scen").value}&seed=${clamp(+$("#seed").value || 0, 0, 999)}`);
    const fr = demo[demo.policies[1]].frames, n = fr.length - 1;
    frame = Math.max(0, fr.findIndex(f => Object.keys(f.d).length > 0) - 2);
    $("#lab-player").innerHTML = `<div class="card"><div class="player"><button class="btn ghost sm" data-act="lab-play" id="play">Pause</button><span class="sim-clock num" id="sim-clock">--:--</span>
        <input type="range" id="scrub" min="0" max="${n}" value="${frame}" aria-label="Time">
        <label class="row small mute">Speed <select class="input" id="speed" style="width:auto;padding:6px 10px"><option value="2">2 min/s</option><option value="5" selected>5 min/s</option><option value="10">10 min/s</option></select></label></div>
      <div class="grid-2e">${demo.policies.map((p, i) => `<div class="card" style="box-shadow:none"><div class="row between"><h2>${h(PNAME[p] || p)}</h2><span class="status" style="--c:${i ? "var(--brand)" : "var(--posted)"}">${i ? "challenger" : "baseline"}</span></div>
        <div class="pane-stats"><div><b class="num" id="p${i}-kg">0</b><small>kg rescued</small></div><div><b class="num" id="p${i}-exp" style="color:var(--bad)">0</b><small>kg lost</small></div><div><b class="num" id="p${i}-n">0</b><small>driver pings</small></div></div>
        <div class="tiles" id="p${i}-tiles"></div><div class="feed" id="p${i}-feed"></div></div>`).join("")}</div>
      <div class="map-foot" style="border:0;padding:12px 0 0">${["offered", "claimed", "picked_up", "delivered", "expired"].map(s => `<span class="legend"><i style="--c:var(--${s})"></i>${STATUS[s]}</span>`).join("")}<span class="legend"><i style="--c:#fff;box-shadow:0 0 0 2px var(--bad)"></i>Escalated</span><span class="legend"><i style="--c:#eef2f7"></i>Not posted yet</span></div></div>`;
    $("#scrub").addEventListener("input", e => { frame = +e.target.value; drawFrame(); });
    play(true); drawFrame();
  });
}
function play(on) {
  clearInterval(timer); timer = null;
  if (on) timer = setInterval(() => { const n = demo[demo.policies[1]].frames.length - 1; frame = Math.min(frame + +$("#speed").value, n); drawFrame(); if (frame >= n) play(false); }, 1000);
  const b = $("#play"); if (b) b.textContent = timer ? "Pause" : "Play";
}
function drawFrame() {
  if (!demo || !$("#scrub")) return;
  $("#scrub").value = frame;
  const info = Object.fromEntries(demo.donations.map(d => [d.id, d]));
  demo.policies.forEach((p, i) => {
    const run = demo[p], f = run.frames[Math.min(frame, run.frames.length - 1)];
    $("#sim-clock").textContent = simClock(f.t);
    countTo($(`#p${i}-kg`), f.kg); countTo($(`#p${i}-exp`), f.exp); countTo($(`#p${i}-n`), f.n);
    setHTML($(`#p${i}-tiles`), demo.donations.map(dn => { const s = f.d[dn.id];
      return s ? `<span class="tile ${s[1] && OPEN.includes(s[0]) ? "x" : ""}" style="--c:var(--${s[0]})" title="${h(dn.donor)} · ${kgf(dn.kg)} kg · ${h(STATUS[s[0]] || s[0])}">${Math.round(dn.kg)}</span>` : `<span class="tile none"></span>`; }).join(""));
    const ev = run.feed.filter(e => e.t <= f.t && e.type !== "RecipientChosen").slice(-30).reverse();
    list($(`#p${i}-feed`), ev, e => `${e.t}:${e.type}:${e.d}`, e => {
      const d = info[e.d], who = d ? `${h(d.donor)} (${kgf(d.kg)} kg)` : h(demo.recipients[e.r]?.name || "");
      const txt = {EscalationRaised: `⚠️ needs a human: ${h(e.reason)}`, BackupDispatched: "🚐 backup courier sent", ClaimCancelled: "↩️ driver cancelled",
        Delivered: "✅ delivered", Expired: `❌ lost: ${h(e.reason)}`, CapacityUpdated: "🏠 shelter marked full"}[e.type] || h(e.type);
      return `<time>${simClock(e.t)}</time><span><b>${who}</b> ${txt}</span>`;
    });
  });
}
const KEYS = [["rescue_rate", "Food rescued", 3, 1, v => Math.round(v * 1000) / 10 + "%"], ["meals", "Meals rescued", 0, 1], ["expired_kg", "Food lost (kg)", 1, -1],
  ["safety_violations", "Safety violations", 0, -1], ["notif_per_rescue", "Pings per rescue", 1, -1], ["p95_offers_per_vol", "Pings to busiest drivers", 1, -1],
  ["claim_p50", "Median time to claim (min)", 1, -1], ["late_escalations", "Late alerts (<10 min)", 1, -1], ["backups", "Backup courier trips", 1, -1],
  ["zone_gap", "Gap between areas", 3, -1], ["latency_p95_ms", "Planner speed p95 (ms)", 1, -1]];
async function runCompare(btn) {
  await busy(btn, async () => {
    $("#lab-table").innerHTML = `<div class="card"><div class="skel" style="height:22px;width:40%"></div><div class="skel" style="height:260px;margin-top:14px"></div><p class="hint" style="margin-top:10px">First run of a scenario can take a few minutes; results are cached afterwards.</p></div>`;
    const x = await api(`/api/sim/compare?scenario=${$("#scen").value}&seeds=30`);
    const pols = Object.keys(x.table), ref = pols.includes("Relay-RL") ? "Relay-RL" : "Relay";
    const fmt = (m, dp, f) => m[0] == null ? "–" : `${f ? f(m[0]) : m[0].toFixed(dp)} <small>±${f && dp === 3 ? (m[1] * 100).toFixed(1) + "pt" : m[1].toFixed(dp)}</small>`;
    $("#lab-table").innerHTML = `<div class="card enter"><div class="row between wrap"><h2>${h(SCEN.find(s => s[0] === x.scenario)?.[1] || x.scenario)} · ${x.seeds} nights</h2><span class="status" style="--c:var(--warn)">simulated · mean ± 95% CI</span></div>
      <div class="table-wrap"><table><thead><tr><th>Metric</th>${pols.map(p => `<th>${h(PNAME[p] || p)}</th>`).join("")}<th>${h(PNAME[ref] || ref)} vs 412FR</th></tr></thead><tbody>
      ${KEYS.map(([k, n, dp, dir, f]) => { const vals = pols.map(p => x.table[p][k]?.[0]).filter(v => v != null), best = dir > 0 ? Math.max(...vals) : Math.min(...vals), pr = x.paired_vs_B1[ref]?.[k];
        return `<tr><td>${n}</td>${pols.map(p => { const m = x.table[p][k] || [null]; return `<td class="${m[0] != null && Math.abs(m[0] - best) < 1e-9 && vals.length > 1 ? "best" : ""}">${fmt(m, dp, f)}</td>`; }).join("")}
          <td>${pr && pr[0] != null ? `${pr[0] > 0 ? "+" : ""}${f ? (pr[0] * 100).toFixed(1) + "pt" : pr[0].toFixed(dp)} <small>±${f ? (pr[1] * 100).toFixed(1) : pr[1].toFixed(dp)}</small>` : "–"}</td></tr>`; }).join("")}
      </tbody></table></div><p class="hint" style="margin-top:10px">${h(x.label)}. Green = best in row.</p></div>`;
  });
}

// ---------------------------------------------------------------- actions
document.addEventListener("click", async e => {
  const b = e.target.closest("[data-act]");
  if (!$("#me-menu").hidden && !e.target.closest(".me")) $("#me-menu").hidden = true;
  if (!b) return;
  const a = b.dataset.act;
  if (a === "demo") return busy(b, async () => { APP.me = await api("/api/auth/demo", {method: "POST", body: JSON.stringify({role: b.dataset.role})}); afterLogin(); });
  if (a === "logout") return logout();
  if (a === "menu") { $("#me-menu").hidden = !$("#me-menu").hidden; return; }
  if (a === "ob-role") { OB.role = b.dataset.role; $$(".role-card").forEach(c => c.classList.toggle("on", c === b)); $("#ob-next").disabled = false; return; }
  if (a === "ob-next") return obStep2();
  if (a === "ob-back") { $("#ob-step1").hidden = false; $("#ob-step2").hidden = true; $("#ob-d2").classList.remove("on"); return; }
  if (a === "ob-mode") { OB.mode = b.dataset.mode; return obStep2(); }
  if (a === "ob-pick") { OB.shelter = b.dataset.id; $$(".pick-item").forEach(p => p.classList.toggle("on", p === b)); return; }
  if (a === "ob-finish") return obFinish(b);
  if (a === "tab") return go(b.dataset.tab);
  if (a === "example") { $("#d-text").value = b.dataset.text; return parseText($("[data-act=parse]")); }
  if (a === "parse") return parseText(b);
  if (a === "post") return postDonation(b);
  if (a === "geo") return navigator.geolocation ? navigator.geolocation.getCurrentPosition(p => { const ll = {lat: p.coords.latitude, lng: p.coords.longitude}; MAPS["map-pick"]?._pin?.setLatLng(ll); MAPS["map-pick"]?.setView(ll, 15); pickLoc = [+ll.lat.toFixed(5), +ll.lng.toFixed(5)]; $("#f-loc").textContent = `📍 ${pickLoc[0].toFixed(4)}, ${pickLoc[1].toFixed(4)}`; }, () => toast("Location not available", "err")) : toast("Location not available", "err");
  if (a === "open-vol") { APP.pickVol = b.dataset.v; $("#drive-offers").innerHTML = ""; $("#drive-job")._h = null; return go("drive"); }
  if (a === "open-rec") { APP.pickRec = b.dataset.r; $("#r-arr").innerHTML = ""; capFor = null; return go("shelter"); }
  if (a === "backup") return busy(b, async () => { await api(`/api/donations/${b.dataset.d}/backup`, {method: "POST"}); toast("Backup courier dispatched"); await tick(true); });
  if (a === "accept" || a === "decline") return busy(b, async () => {
    await api(`/api/offers/${encodeURIComponent(b.dataset.o)}/respond`, {method: "POST", body: JSON.stringify({accept: a === "accept"})});
    toast(a === "accept" ? "Accepted! The address is now visible." : "No problem, we'll ask someone else.", a === "accept" ? "ok" : "info"); await tick(true); });
  if (a === "pickup") return busy(b, async () => {
    const r = await api(`/api/donations/${b.dataset.d}/pickup`, {method: "POST", body: JSON.stringify({temp_ok: $("#c-temp").checked, packaging_ok: $("#c-pack").checked})});
    toast(r.status === "diverted" ? "Diverted: food won't be served (safety first)" : "Picked up. Head to the shelter.", r.status === "diverted" ? "info" : "ok"); await tick(true); });
  if (a === "cancel") return busy(b, async () => { await api(`/api/donations/${b.dataset.d}/cancel`, {method: "POST"}); toast("Released. Relay is asking someone else.", "info"); await tick(true); });
  if (a === "deliver") return busy(b, async () => {
    const code = $("#c-code").value.trim();
    if (!/^\d{4}$/.test(code)) throw new Error("Enter the 4-digit code from the shelter");
    const r = await api(`/api/donations/${b.dataset.d}/deliver`, {method: "POST", body: JSON.stringify({code})}); toast(r.receipt); await tick(true); });
  if (a === "save-cap") return busy(b, async () => {
    const G = ["hot", "cold", "ambient"], rid = isAdmin() ? APP.pickRec : APP.me.entity;
    await api(`/api/recipients/${encodeURIComponent(rid)}/capacity`, {method: "PATCH", body: JSON.stringify({cap: Object.fromEntries(G.map(g => [g, clamp(+$(`#cap-${g}`).value || 0, 0, 1000)])), full: G.filter(g => $(`#full-${g}`).checked)})});
    toast("Saved. Relay re-planned around your space."); await renderShelter(true); });
  if (a === "sim-toggle") return busy(b, async () => { await api("/api/admin/sim", {method: "POST", body: JSON.stringify({running: !APP.S.sim.running})}); await tick(true); });
  if (a === "sim-post") return busy(b, async () => { await api("/api/admin/sim", {method: "POST", body: JSON.stringify({post_now: true})}); toast("A simulated restaurant just posted food"); await tick(true); });
  if (a === "reset-user") return busy(b, async () => { await api(`/api/admin/users/${encodeURIComponent(b.dataset.email)}/reset`, {method: "POST"}); toast("Role cleared. They'll choose again on next sign-in.", "info"); await tick(true); });
  if (a === "lab-demo") return runDemo(b);
  if (a === "lab-compare") return runCompare(b);
  if (a === "lab-play") return play(!timer);
});
document.addEventListener("change", async e => {
  const id = e.target.id;
  if (id === "online") return busy(null, async () => { await api("/api/volunteer/availability", {method: "POST", body: JSON.stringify({on: e.target.checked, v: isAdmin() ? APP.pickVol : null})}); toast(e.target.checked ? "You're online. Offers will come to you." : "You're offline.", "info"); await tick(true); });
  if (id === "sys-run") return busy(null, async () => { await api("/api/admin/sim", {method: "POST", body: JSON.stringify({running: e.target.checked})}); await tick(true); });
  if (id === "sys-speed" || id === "sys-every") return busy(null, async () => { await api("/api/admin/sim", {method: "POST", body: JSON.stringify({[id === "sys-speed" ? "speed" : "every"]: +e.target.value})}); e.target.blur(); await tick(true); });
  if (id === "drive-pick") { APP.pickVol = e.target.value; $("#drive-offers").innerHTML = ""; $("#drive-job")._h = null; if (MAPS["map-drive"]) MAPS["map-drive"]._fitted = false; return tick(true); }
  if (id === "shelter-pick") { APP.pickRec = e.target.value; $("#r-arr").innerHTML = ""; capFor = null; return tick(true); }
});
document.addEventListener("input", e => {
  if (e.target.id === "sys-speed") $("#sys-speed-v").textContent = `×${e.target.value}`;
  if (e.target.id === "sys-every") $("#sys-every-v").textContent = `${e.target.value} s`;
});
$("#d-text").addEventListener("keydown", e => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) parseText($("[data-act=parse]")); });
