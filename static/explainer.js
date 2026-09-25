"use strict";
/* "How Relay thinks" (admin): one real decision explained in 8 animated steps, plus a Q-function playground.
   Everything shown comes from the server: the live rule checks, the acceptance model's probabilities, the trained
   Q-function's values and the recorded training trace. Only step 6's replies are simulated (drawn from each p). */
(() => {
  const NS = "http://www.w3.org/2000/svg", W = 1000, H = 620, STOP = Symbol("stop");
  const STEPS = [
    ["Food is posted", "Input", "#64748b"], ["Rules pick a safe shelter", "Rules", "#0ea5e9"],
    ["ML predicts who will say yes", "Machine learning", "#6366f1"], ["RL decides how many to ask", "Reinforcement learning", "#0f9d74"],
    ["Offers go out", "Action", "#f59e0b"], ["What happens, and the reward", "Reward", "#10b981"],
    ["How the agent learned", "Training", "#8b5cf6"], ["Did it work?", "Evidence", "#ef4444"]];
  const HOLD = [5200, 7500, 7000, 9000, 6500, 8500, 4000, 9000];
  const X = {D: null, T: null, R: null, i: 0, gen: 0, playing: false, live: true, show: 0, loaded: false, E: {}};
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const svg = () => $("#xp-svg");

  // ---------------------------------------------------------------- tiny SVG + animation kit
  function el(tag, attrs = {}, parent) {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
    if (parent) parent.appendChild(e);
    return e;
  }
  function anim(e, kf, c, o = {}) {
    if (!e) return Promise.resolve();
    const a = e.animate(kf, {duration: c.instant || reduce ? 0 : (o.d ?? 600), delay: c.instant || reduce ? 0 : (o.delay || 0),
      easing: o.ease || "cubic-bezier(.2,.8,.2,1)", fill: "forwards"});
    return c.instant || reduce ? Promise.resolve() : a.finished.catch(() => {});
  }
  const pop = (e, c, o) => anim(e, [{opacity: 0, transform: "scale(.2)"}, {opacity: 1, transform: "scale(1.12)", offset: .7}, {opacity: 1, transform: "scale(1)"}], c, {d: 520, ...o});
  const fade = (e, to, c, o) => anim(e, [{opacity: to ? 0 : 1}, {opacity: to}], c, {d: 400, ...o});
  function draw(path, c, o) { const L = path.getTotalLength(); path.style.strokeDasharray = `${L}`; return anim(path, [{strokeDashoffset: L}, {strokeDashoffset: 0}], c, {d: 700, ...o}); }
  function move(g, from, to, c, o) { return anim(g, [{transform: `translate(${from[0]}px,${from[1]}px)`}, {transform: `translate(${to[0]}px,${to[1]}px)`}], c, {d: 1100, ease: "cubic-bezier(.45,0,.2,1)", ...o}); }
  function rng(seed) { let s = 0; for (const ch of String(seed)) s = (s * 31 + ch.charCodeAt(0)) >>> 0; return () => ((s = (s * 1664525 + 1013904223) >>> 0) / 4294967296); }
  // camera: the SVG viewBox glides to whatever the step is about (zoom + pan), eased; replays jump straight there
  const CAM = {x: 0, y: 0, w: W, h: H, tok: 0};
  function setVB(b) { Object.assign(CAM, b); svg().setAttribute("viewBox", `${b.x.toFixed(1)} ${b.y.toFixed(1)} ${b.w.toFixed(1)} ${b.h.toFixed(1)}`); }
  function camera(pts, c, o = {}) {
    let b = {x: 0, y: 0, w: W, h: H};
    if (pts && pts.length) {
      const xs = pts.map(q => q[0]), ys = pts.map(q => q[1]), pad = o.pad ?? 110, mn = o.min ?? 520;
      let w = Math.max(Math.max(...xs) - Math.min(...xs) + 2 * pad, mn), hh = Math.max(Math.max(...ys) - Math.min(...ys) + 2 * pad, mn * H / W);
      if (w / hh > W / H) hh = w * H / W; else w = hh * W / H;
      w = Math.min(w, W); hh = Math.min(hh, H);
      const cx = (Math.max(...xs) + Math.min(...xs)) / 2, cy = (Math.max(...ys) + Math.min(...ys)) / 2 + (o.dy ?? 24);
      b = {x: clamp(cx - w / 2, 0, W - w), y: clamp(cy - hh / 2, 0, H - hh), w, h: hh};
    }
    const tok = ++CAM.tok;
    if (c.instant || reduce) { setVB(b); return Promise.resolve(); }
    const from = {x: CAM.x, y: CAM.y, w: CAM.w, h: CAM.h}, t0 = performance.now(), D = o.d ?? 1300;
    return new Promise(res => {
      const f = now => {
        if (tok !== CAM.tok) return res();
        const p = clamp((now - t0) / D, 0, 1), e = p < .5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2;
        setVB({x: from.x + (b.x - from.x) * e, y: from.y + (b.y - from.y) * e, w: from.w + (b.w - from.w) * e, h: from.h + (b.h - from.h) * e});
        p < 1 ? requestAnimationFrame(f) : res();
      };
      requestAnimationFrame(f);
    });
  }
  // narration: one plain sentence per step, typed out like a presenter talking
  const NARR = {tok: 0};
  function narrate(text) {
    const box = $("#xp-narr");
    if (!box) return;
    const tok = ++NARR.tok;
    box.classList.toggle("empty", !text);
    if (reduce || !text) { box.textContent = text || ""; return; }
    let i = 0;
    box.classList.add("typing");
    const f = () => {
      if (tok !== NARR.tok) return;
      i = Math.min(text.length, i + 2);
      box.textContent = text.slice(0, i);
      if (i < text.length) setTimeout(f, 24); else box.classList.remove("typing");
    };
    f();
  }
  const short = n => String(n).replace(/^Shelter\s+/, "").replace(/\s*\(.*\)$/, "");
  const pct = p => `${Math.round(p * 100)}%`;

  // ---------------------------------------------------------------- stage
  function layout(D) {
    /* Schematic map: real compass direction from the donor, distance square-root-compressed so nearby
       drivers don't pile up, then a few repulsion passes so no two nodes overlap. */
    const [la0, lo0] = D.donation.loc, cos = Math.cos(la0 * Math.PI / 180), cx = W / 2, cy = 280, rx = 400, ry = 215;
    const nodes = [{key: "d", x: cx, y: cy, fixed: true}];
    const far = Math.max(.5, ...D.shelters.map(s => s.km), ...D.volunteers.map(v => v.km));
    const place = (key, loc, km, min) => {
      const dx = (loc[1] - lo0) * cos, dy = loc[0] - la0, th = Math.atan2(dx, dy), f = Math.sqrt(Math.max(km, .05) / far);
      nodes.push({key, x: cx + rx * f * Math.sin(th), y: cy - ry * f * Math.cos(th), min});
    };
    for (const s of D.shelters) place("s" + s.id, s.loc, s.km, 58);
    for (const v of D.volunteers) place("v" + v.id, v.loc, v.km, 46);
    for (let it = 0; it < 80; it++) {
      for (let i = 0; i < nodes.length; i++) for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j], need = Math.max(a.min || 70, b.min || 70);
        let dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy);
        if (d >= need) continue;
        if (d < .01) { dx = Math.cos(i + j); dy = Math.sin(i + j); d = 1; }
        const push = (need - d) / 2, ux = dx / d, uy = dy / d;
        if (!a.fixed) { a.x -= ux * push * (b.fixed ? 2 : 1); a.y -= uy * push * (b.fixed ? 2 : 1); }
        if (!b.fixed) { b.x += ux * push * (a.fixed ? 2 : 1); b.y += uy * push * (a.fixed ? 2 : 1); }
      }
      for (const n of nodes) { n.x = clamp(n.x, 46, W - 46); n.y = clamp(n.y, 50, H - 110); }
    }
    return Object.fromEntries(nodes.map(n => [n.key, [n.x, n.y]]));
  }
  function build() {
    const s = svg(), D = X.D;
    s.innerHTML = "";
    CAM.tok++;
    setVB({x: 0, y: 0, w: W, h: H});
    const defs = el("defs", {}, s);
    defs.innerHTML = `<linearGradient id="xg" x1="0" x2="1"><stop offset="0" stop-color="#0f9d74"/><stop offset="1" stop-color="#0ea5e9"/></linearGradient>
      <radialGradient id="xglow"><stop offset="0" stop-color="#10b981" stop-opacity=".35"/><stop offset="1" stop-color="#10b981" stop-opacity="0"/></radialGradient>
      <pattern id="xgrid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" class="xs-grid"/></pattern>
      <filter id="xsh" x="-50%" y="-50%" width="200%" height="200%"><feDropShadow dx="0" dy="3" stdDeviation="4" flood-color="#0f172a" flood-opacity=".18"/></filter>`;
    el("rect", {x: 0, y: 0, width: W, height: H, fill: "url(#xgrid)"}, s);
    const r = rng("roads");
    const bg = el("g", {}, s);
    for (let i = 0; i < 7; i++) {
      const y = 60 + r() * 500, x = 40 + r() * 900;
      el("path", {d: `M-20 ${y} C ${250 + r() * 200} ${y - 120 + r() * 240}, ${550 + r() * 200} ${y - 120 + r() * 240}, 1020 ${y + r() * 80 - 40}`, class: "xs-road"}, bg);
      el("path", {d: `M${x} -20 C ${x - 80 + r() * 160} 200, ${x - 80 + r() * 160} 420, ${x + r() * 60 - 30} 640`, class: "xs-road"}, bg);
    }
    const city = el("g", {id: "xs-city"}, s);
    X.E = {city, lines: el("g", {}, city), shel: el("g", {}, city), vol: el("g", {}, city), fx: el("g", {}, city), top: el("g", {}, city),
           train: el("g", {id: "xs-train", opacity: 0}, s), res: el("g", {id: "xs-res", opacity: 0}, s)};
    if (!D) return;
    const P = layout(D), E = X.E;
    const dp = P.d;
    // shelters
    E.shelters = {};
    for (const sh of D.shelters) {
      const [x, y] = P["s" + sh.id], g = el("g", {transform: `translate(${x} ${y})`}, E.shel);
      const inner = el("g", {class: "xs-pop", style: "opacity:0"}, g);
      el("rect", {x: -17, y: -17, width: 34, height: 34, rx: 10, fill: "#fff", stroke: "#dbe2ec", "stroke-width": 1.5, filter: "url(#xsh)"}, inner);
      el("text", {x: 0, y: 6, "text-anchor": "middle", "font-size": 17}, inner).textContent = "🏠";
      el("text", {x: 0, y: 32, "text-anchor": "middle", class: "xs-small"}, inner).textContent = short(sh.name);
      const badge = el("g", {class: "xs-pop", style: "opacity:0"}, el("g", {transform: "translate(15 -15)"}, g));
      el("circle", {r: 9, fill: sh.ok ? "#10b981" : "#ef4444", stroke: "#fff", "stroke-width": 2}, badge);
      el("text", {x: 0, y: 4, "text-anchor": "middle", "font-size": 11, "font-weight": 800, fill: "#fff"}, badge).textContent = sh.ok ? "✓" : "✕";
      const line = el("path", {d: `M${dp[0]} ${dp[1]} L${x} ${y}`, fill: "none", stroke: sh.ok ? "#10b981" : "#ef4444", "stroke-width": 2, opacity: 0, "stroke-linecap": "round"}, E.lines);
      E.shelters[sh.id] = {g, inner, badge, line, xy: [x, y]};
    }
    // volunteers
    E.vols = {};
    D.volunteers.forEach((v, i) => {
      const [x, y] = P["v" + v.id], g = el("g", {transform: `translate(${x} ${y})`}, E.vol);
      const inner = el("g", {class: "xs-pop", style: "opacity:0"}, g);
      const col = v.eligible ? `hsl(${215 - 60 * Math.min(1, (v.p || 0) / .5)} 80% ${58 - 18 * Math.min(1, (v.p || 0) / .5)}%)` : "#cbd5e1";
      el("circle", {r: 15, fill: col, stroke: "#fff", "stroke-width": 3, filter: "url(#xsh)"}, inner);
      el("text", {x: 0, y: 4, "text-anchor": "middle", "font-size": 10.5, "font-weight": 800, fill: "#fff"}, inner).textContent = initials(v.name);
      const tag = el("g", {class: "xs-pop", style: "opacity:0"}, el("g", {transform: "translate(0 -30)"}, g));
      const txt = v.eligible ? pct(v.p) : "✕";
      el("rect", {x: -22, y: -11, width: 44, height: 21, rx: 10, fill: v.eligible ? "#0f172a" : "#fee2e2"}, tag);
      el("text", {x: 0, y: 4, "text-anchor": "middle", class: "xs-p", fill: v.eligible ? "#fff" : "#b91c1c", style: `fill:${v.eligible ? "#fff" : "#b91c1c"}`}, tag).textContent = txt;
      const ring = el("circle", {r: 15, fill: "none", stroke: "#f59e0b", "stroke-width": 3, opacity: 0, class: "xs-pop"}, g);
      E.vols[v.id] = {g, inner, tag, ring, xy: [x, y], v, rank: i};
    });
    // donor on top
    const dg = el("g", {transform: `translate(${dp[0]} ${dp[1]})`}, E.top);
    E.ripple = el("circle", {r: 30, fill: "none", stroke: "#0f9d74", "stroke-width": 2, opacity: 0, class: "xs-pop"}, dg);
    E.glow = el("circle", {r: 70, fill: "url(#xglow)", opacity: 0}, dg);
    const dinner = el("g", {class: "xs-pop", style: "opacity:0"}, dg);
    el("circle", {r: 38, fill: "none", stroke: "#e2e8f0", "stroke-width": 6}, dinner);
    E.clock = el("circle", {r: 38, fill: "none", stroke: "url(#xg)", "stroke-width": 6, "stroke-linecap": "round", transform: "rotate(-90)", "stroke-dasharray": 238.8, "stroke-dashoffset": 238.8}, dinner);
    el("circle", {r: 29, fill: "#fff", filter: "url(#xsh)"}, dinner);
    el("text", {x: 0, y: 10, "text-anchor": "middle", "font-size": 28}, dinner).textContent = "🍲";
    const lbl = el("g", {transform: "translate(0 58)"}, dinner);
    el("rect", {x: -70, y: -13, width: 140, height: 24, rx: 12, fill: "#0f172a"}, lbl);
    el("text", {x: 0, y: 4, "text-anchor": "middle", style: "font:700 11.5px Inter,system-ui,sans-serif;fill:#fff"}, lbl).textContent = `${kgf(D.donation.kg)} kg · ~${Math.round(D.donation.meals)} meals`;
    E.donor = {g: dg, inner: dinner, xy: dp};
  }
  function caption(items) { setHTML($("#xp-caption"), items.map(t => `<span>${t}</span>`).join("")); }
  function panel(i, title, body, widget = "") {
    const [, kind, color] = STEPS[i];
    $("#xp-panel").innerHTML = `<div class="xp-kicker">Step ${i + 1} of ${STEPS.length}<span class="kind" style="--c:${color}">${kind}</span></div>
      <h2>${title}</h2><p>${body}</p><div class="xp-widget" id="xp-w">${widget}</div>`;
  }
  function qBars(box, ks, qs, best) {
    if (!box) return;
    if (box.children.length !== ks.length) box.innerHTML = ks.map(k => `<div class="qbar"><span class="qv"></span><div class="col"></div><span class="k">ask ${k}</span></div>`).join("");
    const vals = qs.filter(v => v != null), mx = Math.max(...vals), mn = Math.min(...vals), lo = mn - Math.max(.15 * (mx - mn), .05);
    [...box.children].forEach((b, i) => {
      const q = qs[i];
      b.classList.toggle("na", q == null); b.classList.toggle("best", ks[i] === best); b.classList.add("show");
      b.querySelector(".qv").textContent = q == null ? "n/a" : q.toFixed(2);
      b.querySelector(".col").style.height = q == null ? "" : `${(12 + 88 * (q - lo) / (mx - lo || 1)).toFixed(1)}%`;
      b.title = q == null ? "not enough drivers for this option" : `Q = ${q.toFixed(3)}: expected meals rescued minus notification cost`;
    });
  }

  // ---------------------------------------------------------------- the 8 scenes
  const SC = [];
  SC[0] = async c => {   // food is posted
    const D = X.D, E = X.E, dn = D.donation;
    await fade(X.E.city, 1, {instant: true});
    camera([E.donor.xy], c, {min: 460, d: 1600});
    narrate(`${dn.donor} just posted ${kgf(dn.kg)} kg of food. A safety clock starts now: ${dur(dn.safe_min)} until it must be eaten.`);
    await pop(E.donor.inner, c);
    const frac = clamp(dn.safe_min / 240, 0, 1);
    anim(E.clock, [{strokeDashoffset: 238.8}, {strokeDashoffset: 238.8 * (1 - frac)}], c, {d: 1400});
    for (let k = 0; k < 2 && !c.instant; k++) { anim(E.ripple, [{opacity: .8, transform: "scale(1)"}, {opacity: 0, transform: "scale(3)"}], c, {d: 1300}); await c.wait(700); }
    panel(0, "A restaurant just posted food", `<b>${h(dn.donor)}</b> has <b>${kgf(dn.kg)} kg</b> of ${h((CAT[dn.category] || dn.category).toLowerCase())} (about <b>${Math.round(dn.meals)} meals</b>). A safety clock starts right now: it must be eaten by <b>${h(dn.safe_until)}</b>, which is ${dur(dn.safe_min)} away. The ring around the pot is that clock.`,
      `<div class="gauges"><div class="gauge"><small>Food</small><b>${kgf(dn.kg)} kg</b></div><div class="gauge" style="animation-delay:.08s"><small>Meals</small><b>~${Math.round(dn.meals)}</b></div>
       <div class="gauge" style="animation-delay:.16s"><small>Safe to eat for</small><b>${dur(dn.safe_min)}</b></div><div class="gauge" style="animation-delay:.24s"><small>Donor available</small><b>${dur(dn.window_min)}</b></div></div>
       <div class="xp-note">${D.real ? "🔴 This is a <b>live donation</b> in the city right now." : "📦 This is an <b>example</b> taken from the relay_data test nights; it isn't added to the city."} The safety clock is a fixed rule (4 hours for cooked food, longer for bakery or produce), never a prediction.</div>`);
    caption(["🍲 donor", "ring = safety clock", "schematic map: real directions, distances squeezed"]);
    await c.wait(600);
  };
  SC[1] = async c => {   // rules pick a shelter
    const D = X.D, E = X.E, dp = E.donor.xy;
    const order = [...D.shelters].sort((a, b) => a.km - b.km);
    camera([dp, ...order.map(s => E.shelters[s.id].xy)], c, {pad: 80});
    const nOk = D.shelters.filter(s => s.ok).length, ch = D.shelters.find(s => s.id === D.chosen);
    narrate(ch ? `Hard safety rules test all ${D.shelters.length} shelters. ${nOk} pass, and ${short(ch.name)} is the best fit.`
               : `Hard safety rules test all ${D.shelters.length} shelters. None can serve it safely, so a human is alerted.`);
    await Promise.all(order.map((s, i) => pop(E.shelters[s.id].inner, c, {delay: i * 60})));
    const ok = order.filter(s => s.ok), sc = ok.map(s => s.score), mx = Math.max(...sc, 1e-9), mn = Math.min(...sc, 0);
    panel(1, "Rules pick a shelter that can use it safely", `No AI here. For every shelter Relay checks hard rules: <b>will the food still be safe when they serve it?</b> Do they have space, are they open, do they accept this food? Among shelters that pass, it prefers ones that received the least recently (fairness) and are close.`,
      `<div id="xp-shel" class="stack" style="max-height:300px;overflow:auto;padding-right:4px"></div>`);
    const box = $("#xp-shel");
    for (const s of order) {
      const o = E.shelters[s.id];
      anim(o.line, [{opacity: 0}, {opacity: .8}], c, {d: 200});
      await draw(o.line, c, {d: 280});
      await pop(o.badge, c, {d: 320});
      box.insertAdjacentHTML("beforeend", s.ok
        ? `<div class="xp-row good" data-id="${h(s.id)}"><span>✓</span><span class="nm">${h(s.name)}</span><span class="xp-mini"><i style="width:${(100 * (s.score - mn) / (mx - mn || 1) * .85 + 15).toFixed(0)}%"></i></span></div>`
        : `<div class="xp-row bad"><span>✕</span><span class="nm">${h(s.name)}</span><span class="why">${h(s.why)}</span></div>`);
      box.scrollTop = box.scrollHeight;
      if (!s.ok) anim(o.line, [{opacity: .8}, {opacity: .12}], c, {d: 500, delay: 250});
      await c.wait(140);
    }
    if (!D.chosen) {
      box.insertAdjacentHTML("afterbegin", `<div class="verdict">No shelter can take this food safely right now, so a coordinator is alerted immediately. No driver is asked.</div>`);
      return c.wait(400);
    }
    const w = E.shelters[D.chosen];
    for (const s of ok) if (s.id !== D.chosen) anim(E.shelters[s.id].line, [{opacity: .8}, {opacity: .12}], c, {d: 500});
    w.line.setAttribute("stroke", "url(#xg)");
    anim(w.line, [{strokeWidth: 2}, {strokeWidth: 6}], c, {d: 600});
    const halo = el("circle", {r: 30, fill: "none", stroke: "#10b981", "stroke-width": 3, opacity: 0}, w.g);
    anim(halo, [{opacity: 0, transform: "scale(.6)"}, {opacity: 1, transform: "scale(1)"}], c, {d: 600});
    const chosen = D.shelters.find(s => s.id === D.chosen);
    [...box.children].forEach(r => r.classList.toggle("win", r.dataset.id === D.chosen));
    box.insertAdjacentHTML("afterbegin", `<div class="verdict">Chosen: <b>${h(chosen.name)}</b> · ${chosen.km} km away · ${chosen.free} kg space free</div>`);
    box.scrollTop = 0;
    caption(["🏠 shelters", "✓ passes the rules", "✕ fails a rule (reason on the right)"]);
    await c.wait(400);
  };
  SC[2] = async c => {   // ML predicts
    const D = X.D, E = X.E;
    const elig = D.volunteers.filter(v => v.eligible), inel = D.volunteers.filter(v => !v.eligible);
    camera([E.donor.xy, ...D.volunteers.map(v => E.vols[v.id].xy)], c, {pad: 70});
    narrate(elig.length ? `A machine-learning model predicts each driver's chance of saying yes. The most likely: ${elig[0].name.split(" ")[0]}, at ${pct(elig[0].p)}.`
                        : "No driver can reach the food in time, so nobody is asked.");
    await Promise.all(D.volunteers.map((v, i) => pop(E.vols[v.id].inner, c, {delay: i * 45})));
    if (!D.rl) { panel(2, "ML predicts who will say yes", "There is no safe shelter for this food, so no driver is asked. A human decides instead."); return; }
    for (const v of elig.slice(0, 8)) pop(E.vols[v.id].tag, c, {delay: 80 * elig.indexOf(v)});
    for (const v of inel) pop(E.vols[v.id].tag, c, {delay: 300});
    const reasons = {};
    for (const v of inel) reasons[v.why] = (reasons[v.why] || 0) + 1;
    panel(2, "A machine-learning model predicts who will say yes", `For each driver who could reach the food in time, a model trained on <b>147,444 past offers</b> estimates the chance they accept within 8 minutes. It weighs distance, how full their vehicle would be, how many requests they've had this week and their personal track record. It's calibrated: when it says 30%, about 30% say yes.`,
      `<div class="stack" id="xp-rank">${elig.slice(0, 8).map((v, i) => `<div class="xp-row" style="animation-delay:${i * 70}ms"><span class="num" style="width:18px;color:var(--mute)">${i + 1}</span><span class="nm">${h(v.name)}</span><span class="tiny mute">${v.km} km</span><span class="xp-mini"><i data-w="${Math.min(100, v.p / Math.max(elig[0].p, .01) * 100).toFixed(0)}"></i></span><b class="num" style="width:38px;text-align:right">${pct(v.p)}</b></div>`).join("")}</div>
       ${inel.length ? `<div class="chips">${Object.entries(reasons).map(([r, n]) => `<span class="chip static">✕ ${n} ${h(r)}</span>`).join("")}</div>` : ""}
       <div class="xp-note">${D.rl.eligible} drivers can make it in time. Grey drivers nearby can't be asked right now.</div>`);
    await c.wait(c.instant ? 0 : 60);
    $$("#xp-rank .xp-mini i").forEach(i => { i.style.width = i.dataset.w + "%"; });
    caption(["circle colour = chance of yes (bluer = lower, greener = higher)", "label = predicted chance"]);
    await c.wait(900);
  };
  SC[3] = async c => {   // RL decides
    const D = X.D, E = X.E;
    if (!D.rl) { panel(3, "RL decides how many to ask", "Skipped: nothing to decide without a safe shelter."); return; }
    const f = D.rl.features;
    camera([E.donor.xy, ...D.volunteers.slice(0, 8).map(v => E.vols[v.id].xy)], c, {pad: 90});
    narrate(`Now the reinforcement-learning agent scores six options, from asking nobody to asking eight drivers. It picks: ${D.rl.best === 1 ? "ask the single most likely driver" : D.rl.best ? `ask the top ${D.rl.best}` : "wait for now"}.`);
    for (const v of D.volunteers.slice(8)) anim(E.vols[v.id].g, [{opacity: 1}, {opacity: .35}], c, {d: 400});
    anim(E.glow, [{opacity: 0}, {opacity: 1}, {opacity: .4}], c, {d: 1400});
    panel(3, "A reinforcement-learning agent decides how many to ask", `Ask too few and the food may not be claimed in time; ask too many and you pester volunteers until they quit. The agent looks at the situation and scores each option: ask <b>0, 1, 2, 3, 5 or 8</b> of the top drivers. Each score (a <b>Q-value</b>) is its estimate of the meals this rescue will save, minus a small cost for every ping.`,
      `<div class="gauges">
        <div class="gauge"><small>Latest safe pickup in</small><b>${dur(f.slack)}</b></div>
        <div class="gauge" style="animation-delay:.07s"><small>Drivers who can make it</small><b>${Math.round(f.n_pool)}</b></div>
        <div class="gauge" style="animation-delay:.14s"><small>Best driver's chance</small><b>${pct(f.top1)}</b></div>
        <div class="gauge" style="animation-delay:.21s"><small>Expected yeses from top 3</small><b>${f.top3.toFixed(2)}</b></div>
        <div class="gauge" style="animation-delay:.28s"><small>Already asked</small><b>${Math.round(f.n_offered)}</b></div>
        <div class="gauge" style="animation-delay:.35s"><small>Meals at stake</small><b>~${Math.round(f.meals)}</b></div></div>
       <div class="qbars" id="xp-q"></div><div id="xp-qv"></div>`);
    const ks = D.rl.k, qs = D.rl.q, box = $("#xp-q");
    box.innerHTML = ks.map((k, i) => `<div class="qbar"><span class="qv" style="transition-delay:${c.instant ? 0 : .5 + i * .22}s"></span><div class="col" style="transition-delay:${c.instant ? 0 : i * .22}s"></div><span class="k">ask ${k}</span></div>`).join("");
    await c.wait(c.instant ? 0 : 400);
    void box.offsetHeight;
    qBars(box, ks, qs, null);
    await c.wait(ks.length * 220 + 900);
    qBars(box, ks, qs, D.rl.best);
    const bi = ks.indexOf(D.rl.best), others = qs.map((q, i) => i === bi || q == null ? -Infinity : q), si = others.indexOf(Math.max(...others));
    const gap = qs[bi] - qs[si], what = k => k === 1 ? "ask the most likely driver" : k ? `ask the top ${k}` : "wait (ask nobody yet)";
    $("#xp-qv").innerHTML = `<div class="verdict">The agent's choice: <b>${what(D.rl.best)}</b>. ${gap < .01
      ? `It's practically tied with “${what(ks[si])}” (difference under 0.01): with ${dur(f.slack)} left and ${Math.round(f.n_pool)} drivers able to help, the timing barely matters yet.`
      : `Its score ${qs[bi].toFixed(2)} beats the next best option (“${what(ks[si])}”) by ${gap.toFixed(2)}.`}${D.rl.best ? "" : " It will look again in 8 minutes, with the clock further along."}</div>`;
    caption(["glow = the agent weighing its options", "the agent's options are on the right"]);
    await c.wait(500);
  };
  SC[4] = async c => {   // offers go out
    const D = X.D, E = X.E, dp = E.donor.xy;
    if (!D.rl) { panel(4, "Offers go out", "Skipped."); return; }
    const asked = D.volunteers.filter(v => v.asked);
    camera(asked.length ? [dp, ...asked.map(v => E.vols[v.id].xy)] : [dp], c, {pad: 100});
    narrate(asked.length ? `Offers go to ${asked.length} driver${asked.length === 1 ? "" : "s"}. The chance at least one says yes: ${pct(D.rl.pclaim)}.`
                         : "This time the agent waits. Nobody is pinged; it will look again in 8 minutes.");
    if (!asked.length) {
      panel(4, "This time, the agent waits", `No offers go out yet: nobody is pinged, so no volunteer is bothered. In 8 minutes the agent looks again with less time left, and asking becomes more attractive. If time runs short, a human is alerted before <b>${h(D.L)}</b>.`,
        `<div class="row" style="gap:16px">${ring(0)}<div class="xp-note">Chance of a claim this round: 0%, by choice. The rules still protect the food: the escalation deadline doesn't depend on the agent.</div></div>`);
      anim(E.glow, [{opacity: .4}, {opacity: 0}], c, {d: 800});
      caption(["⏳ waiting is also a decision"]);
      return c.wait(600);
    }
    panel(4, asked.length === 1 ? "The offer goes to the most likely driver" : `Offers go to the top ${asked.length} drivers`, `Each gets a message with the food, the distance and the approximate area (the exact address comes after they accept). The chance that <b>at least one</b> says yes is 1 − (1 − p₁)(1 − p₂)… If nobody answers within 8 minutes, the agent looks again with fresh information. If time runs short, a human is alerted before <b>${h(D.L)}</b>.`,
      `<div class="row" style="gap:16px">${ring(D.rl.pclaim)}<div class="formula">P(at least one yes) = 1 − ${asked.slice(0, 4).map(v => `(1 − <b>${v.p.toFixed(2)}</b>)`).join("")}${asked.length > 4 ? "…" : ""} = <em>${pct(D.rl.pclaim)}</em></div></div>
       <div class="stack">${asked.map((v, i) => `<div class="xp-row" style="animation-delay:${i * 90}ms"><span>📨</span><span class="nm">${h(v.name)}</span><span class="tiny mute">${v.km} km</span><b class="num">${pct(v.p)}</b></div>`).join("")}</div>`);
    await Promise.all(asked.map(async (v, i) => {
      const o = E.vols[v.id], path = el("path", {d: `M${dp[0]} ${dp[1]} L${o.xy[0]} ${o.xy[1]}`, fill: "none", stroke: "#f59e0b", "stroke-width": 2.5, "stroke-dasharray": "6 7", opacity: .9}, E.lines);
      await c.wait(i * 160);
      const pk = el("g", {}, E.fx);
      el("circle", {r: 7, fill: "#f59e0b", stroke: "#fff", "stroke-width": 2}, pk);
      await move(pk, dp, o.xy, c, {d: 900});
      pk.remove();
      anim(o.ring, [{opacity: 1, transform: "scale(1)"}, {opacity: 0, transform: "scale(2.3)"}], c, {d: 900});
      anim(o.inner, [{transform: "scale(1)"}, {transform: "scale(1.25)"}, {transform: "scale(1)"}], c, {d: 500});
      path.setAttribute("stroke-dasharray", "6 7");
    }));
    caption(["📨 offer sent", "dashed = waiting for a reply"]);
    await c.wait(400);
  };
  SC[5] = async c => {   // outcome + reward
    const D = X.D, E = X.E, dp = E.donor.xy;
    if (!D.rl) { panel(5, "What happens, and the reward", "Skipped."); return; }
    if (!D.volunteers.some(v => v.asked)) {
      panel(5, "No reward yet: the rescue isn't over", `The reward only arrives when food is claimed (+ meals) or when a ping is sent (− ${D.lam} each). By waiting, the agent spent nothing this round and kept every option open. The agent was trained to maximise the total reward over the <b>whole</b> rescue, not just this moment.`,
        `<div class="ledger"><div class="ln"><span>Pings this round</span><b>0</b></div><div class="ln total"><span>Reward this round</span><b class="num">0.00</b></div></div>
         <div class="xp-note">Tip: switch to <b>Example</b> or press <b>New decision</b> to see a case where the agent sends offers.</div>`);
      return c.wait(400);
    }
    const r = rng(D.donation.id + X.show), asked = D.volunteers.filter(v => v.asked).map(v => ({v, yes: r() < v.p, t: r()})).sort((a, b) => a.t - b.t);
    const winner = asked.find(a => a.yes), k = asked.length, meals = Math.round(D.donation.meals), cost = +(D.lam * k).toFixed(2);
    camera([dp, E.shelters[D.chosen].xy, ...asked.map(a => E.vols[a.v.id].xy)], c, {pad: 90});
    narrate(winner ? `${winner.v.name.split(" ")[0]} said yes and delivers the food. Reward: ${meals} meals saved minus ${cost} for ${k} ping${k === 1 ? "" : "s"}.`
                   : `Nobody answered this round. The agent pays ${cost} for ${k === 1 ? "the ping" : "the pings"} and decides again; a human is alerted in time.`);
    panel(5, winner ? "Someone said yes. Here's the reward." : "Nobody answered this time", `Below is one possible outcome, drawn from each driver's probability. ${winner ? `<b>${h(winner.v.name)}</b> accepts, picks the food up and delivers it.` : "That happens: it's why the agent weighs the odds, and why a human is alerted in time."} The <b>reward</b> is what the agent was trained to maximise over a whole rescue: meals rescued, minus ${D.lam} for every ping.`,
      `<div class="ledger" id="xp-led"></div><div class="xp-note">The live agent doesn't change while running; everything it knows came from training on past nights (next step). Offers and outcomes like this one become new training data when you re-run <code>python ml.py rl</code>.</div>`);
    const led = $("#xp-led");
    for (const a of asked) {
      const o = E.vols[a.v.id], b = el("g", {transform: `translate(${o.xy[0]} ${o.xy[1] - 34})`}, E.fx);
      const inner = el("g", {class: "xs-pop", style: "opacity:0"}, b);
      el("rect", {x: -34, y: -12, width: 68, height: 23, rx: 11, fill: a.yes ? "#10b981" : "#e2e8f0"}, inner);
      el("text", {x: 0, y: 4, "text-anchor": "middle", style: `font:800 11px Inter,system-ui,sans-serif;fill:${a.yes ? "#fff" : "#64748b"}`}, inner).textContent = a.yes ? "✓ Yes!" : "… no reply";
      await pop(inner, c, {d: 420});
      await c.wait(380);
      if (a === winner) break;
    }
    led.innerHTML = `<div class="ln minus"><span>${k} ping${k === 1 ? "" : "s"} × ${D.lam}</span><b>−${cost}</b></div>`;
    if (winner) {
      const o = E.vols[winner.v.id], car = el("g", {}, E.fx), sh = E.shelters[D.chosen];
      el("circle", {r: 18, fill: "#fff", filter: "url(#xsh)"}, car);
      el("text", {x: 0, y: 7, "text-anchor": "middle", "font-size": 20}, car).textContent = "🛵";
      await move(car, o.xy, dp, c, {d: 1100});
      anim(E.donor.inner, [{transform: "scale(1)"}, {transform: "scale(.92)"}, {transform: "scale(1)"}], c, {d: 400});
      await move(car, dp, sh.xy, c, {d: 1300});
      for (let j = 0; j < 14 && !c.instant; j++) {
        const p = el("circle", {r: 3 + Math.random() * 3, fill: ["#10b981", "#0ea5e9", "#f59e0b", "#6366f1"][j % 4]}, E.fx);
        const ang = Math.random() * Math.PI * 2, d = 30 + Math.random() * 40;
        anim(p, [{transform: `translate(${sh.xy[0]}px,${sh.xy[1]}px)`, opacity: 1}, {transform: `translate(${sh.xy[0] + Math.cos(ang) * d}px,${sh.xy[1] + Math.sin(ang) * d}px)`, opacity: 0}], c, {d: 900}).then(() => p.remove());
      }
      led.insertAdjacentHTML("afterbegin", `<div class="ln plus"><span>Meals rescued</span><b>+${meals}</b></div>`);
      led.insertAdjacentHTML("beforeend", `<div class="ln total"><span>Reward for this rescue</span><b class="num" id="xp-rew">0.00</b></div>`);
      countTo($("#xp-rew"), meals - cost, v => v.toFixed(2));
      const plus = el("text", {x: sh.xy[0], y: sh.xy[1] - 30, "text-anchor": "middle", style: "font:800 20px Inter,system-ui,sans-serif;fill:#10b981", opacity: 0}, E.fx);
      plus.textContent = `+${meals} meals`;
      anim(plus, [{opacity: 0, transform: "translateY(10px)"}, {opacity: 1, transform: "translateY(-6px)", offset: .3}, {opacity: 0, transform: "translateY(-40px)"}], c, {d: 2200});
    } else {
      led.insertAdjacentHTML("beforeend", `<div class="ln total"><span>Reward so far</span><b class="num">${(-cost).toFixed(2)}</b></div><div class="ln"><span class="tiny mute">Next: the agent decides again; a human is alerted before ${h(D.L)}.</span></div>`);
    }
    caption(winner ? ["✓ accepted", "🛵 pickup → delivery"] : ["no reply in 8 min → decide again"]);
    await c.wait(500);
  };
  SC[6] = async c => {   // how it learned
    const T = X.T, E = X.E;
    camera(null, c, {d: 900});
    narrate(`It learned offline from ${T.rows.toLocaleString()} past decisions: each round, the reward it saw updates what it expects, until its choices stop changing.`);
    anim(E.city, [{opacity: 1}, {opacity: 0}], c, {d: 400});
    anim(E.train, [{opacity: 0}, {opacity: 1}], c, {d: 500, delay: 200});
    E.train.innerHTML = "";
    const g = E.train;
    el("text", {x: 60, y: 70, style: "font:800 20px Inter,system-ui,sans-serif;fill:#0f172a"}, g).textContent = `${T.rows.toLocaleString()} past decisions`;
    el("text", {x: 60, y: 94, class: "xs-small"}, g).textContent = `from 255 logged nights · each dot ≈ ${Math.round(T.rows / 240 / 10) * 10} decisions`;
    const dots = [];
    for (let r = 0; r < 12; r++) for (let q = 0; q < 20; q++) dots.push(el("circle", {cx: 70 + q * 19, cy: 130 + r * 22, r: 5, fill: "#c7d2fe", class: "xs-pop"}, g));
    const bx = el("g", {transform: "translate(560 120)"}, g);
    el("rect", {width: 380, height: 150, rx: 18, fill: "#fff", stroke: "#e2e8f0", filter: "url(#xsh)"}, bx);
    el("text", {x: 24, y: 38, style: "font:800 15px Inter,system-ui,sans-serif;fill:#0f172a"}, bx).textContent = "Q-function";
    el("text", {x: 24, y: 58, class: "xs-small"}, bx).textContent = "gradient-boosted trees: situation + option → score";
    const round = el("text", {x: 24, y: 118, style: "font:800 40px Inter,system-ui,sans-serif;fill:#8b5cf6"}, bx);
    el("text", {x: 200, y: 118, class: "xs-small"}, bx).textContent = `rounds of ${T.iterations}`;
    const fx = el("g", {transform: "translate(560 300)"}, g);
    el("rect", {width: 380, height: 58, rx: 14, fill: "#0f172a"}, fx);
    el("text", {x: 20, y: 35, style: "font:600 14px ui-monospace,Consolas,monospace;fill:#e2e8f0"}, fx).textContent = `target = reward + ${T.gamma} × best Q(next)`;
    const ch = el("g", {transform: "translate(560 400)"}, g);
    el("text", {x: 0, y: 0, class: "xs-small"}, ch).textContent = "How much the targets moved each round (settles = learned)";
    const cw = 380, chh = 120, mxB = Math.max(...T.bellman);
    el("line", {x1: 0, y1: chh + 14, x2: cw, y2: chh + 14, stroke: "#e2e8f0"}, ch);
    const poly = el("polyline", {points: "", fill: "none", stroke: "#8b5cf6", "stroke-width": 3, "stroke-linejoin": "round", "stroke-linecap": "round"}, ch);
    const dotEnd = el("circle", {r: 5, fill: "#8b5cf6", cx: 0, cy: chh + 14}, ch);
    const st = T.states[Math.min(X.show, T.states.length - 1)];
    panel(6, "How the agent learned (offline, from past nights)", `The agent never practised on real food. It studied <b>${T.rows.toLocaleString()} real decisions</b> from 255 logged nights. Each round, every past decision gets a target: <b>reward now + ${T.gamma} × the best score of what happened next</b>. A model is fitted to those targets, and the process repeats ${T.iterations} times. Watch its opinion about one situation change round by round.`,
      `<div class="seg" id="xp-show">${T.states.map((s, i) => `<button class="${i === Math.min(X.show, T.states.length - 1) ? "on" : ""}" data-xp-show="${i}">${h(s.name)}</button>`).join("")}</div>
       <div class="qbars" id="xp-tq"></div><div id="xp-tv"></div>`);
    const box = $("#xp-tq"), ks = T.k, its = st.q.length;
    for (let it = 0; it < its; it++) {
      const qs = st.q[it], vals = qs.map(q => q ?? -Infinity), best = ks[vals.indexOf(Math.max(...vals))];
      qBars(box, ks, qs, best);
      round.textContent = it < T.iterations ? String(it + 1) : "✓";
      const n = Math.min(it + 1, T.bellman.length);
      poly.setAttribute("points", T.bellman.slice(0, n).map((b, j) => `${(j / (T.bellman.length - 1)) * cw},${14 + chh - (b / mxB) * chh}`).join(" "));
      dotEnd.setAttribute("cx", ((n - 1) / (T.bellman.length - 1)) * cw); dotEnd.setAttribute("cy", 14 + chh - (T.bellman[n - 1] / mxB) * chh);
      $("#xp-tv").innerHTML = `<div class="verdict" style="animation:none">${it < T.iterations ? `Round ${it + 1}` : "Final model"}: in “${h(st.name)}”, the agent would <b>ask ${best}</b>.</div>`;
      if (!c.instant) {
        for (let j = 0; j < 26; j++) { const d = dots[(it * 37 + j * 11) % dots.length]; anim(d, [{fill: "#c7d2fe", transform: "scale(1)"}, {fill: "#8b5cf6", transform: "scale(1.45)"}, {fill: "#c7d2fe", transform: "scale(1)"}], c, {d: 500, delay: j * 10}); }
        for (let j = 0; j < 6; j++) {   // a few decisions (with their rewards) flow into the model: that's the update
          const d = dots[(it * 53 + j * 29) % dots.length], pk = el("g", {transform: `translate(${d.getAttribute("cx")} ${d.getAttribute("cy")})`}, g);
          el("circle", {r: 4.5, fill: "#8b5cf6", stroke: "#fff", "stroke-width": 1.5}, pk);
          move(pk, [+d.getAttribute("cx"), +d.getAttribute("cy")], [640 + j * 40, 195], c, {d: 650, delay: j * 45}).then(() => pk.remove());
        }
        anim(fx, [{transform: "translate(560px,300px) scale(1)"}, {transform: "translate(560px,300px) scale(1.035)"}, {transform: "translate(560px,300px) scale(1)"}], c, {d: 420, delay: 380});
      }
      await c.wait(it < 3 ? 700 : 380);
    }
    caption([]);
  };
  SC[7] = async c => {   // results
    const R = X.R, E = X.E, rr0 = R?.table["Relay-RL"];
    camera(null, c, {d: 700});
    narrate(rr0 ? `On 30 nights it never saw, Relay + RL rescued ${(rr0.rescue_rate[0] * 100).toFixed(1)}% of the food with just ${rr0.notif_per_rescue[0].toFixed(1)} pings per rescue.` : "");
    anim(E.city, [{opacity: 1}, {opacity: 0}], c, {d: 300});
    anim(E.train, [{opacity: 1}, {opacity: 0}], c, {d: 300});
    anim(E.res, [{opacity: 0}, {opacity: 1}], c, {d: 500, delay: 200});
    E.res.innerHTML = "";
    const pols = [["B0", "WhatsApp broadcast", "#94a3b8"], ["B1", "412FR static rule", "#64748b"], ["Relay", "Relay (rules)", "#0ea5e9"], ["Relay-RL", "Relay + RL", "#0f9d74"]].filter(p => R?.table[p[0]]);
    if (!pols.length) {
      panel(7, "Did it work?", "Results aren't available yet. Run <code>python sim.py bengaluru 30</code> once, then reopen this step.");
      return;
    }
    const charts = [["rescue_rate", "Food rescued", v => `${(v * 100).toFixed(1)}%`, 1, "higher is better"], ["notif_per_rescue", "Pings per rescue", v => v.toFixed(1), null, "lower is better"]];
    charts.forEach(([key, title, fmt, fixedMax, sub], ci) => {
      const g = el("g", {transform: `translate(${70 + ci * 470} 70)`}, E.res), cw = 400, ch = 380;
      el("text", {x: 0, y: 0, style: "font:800 18px Inter,system-ui,sans-serif;fill:#0f172a"}, g).textContent = title;
      el("text", {x: 0, y: 22, class: "xs-small"}, g).textContent = sub;
      const mx = fixedMax || Math.max(...pols.map(p => R.table[p[0]][key][0])) * 1.1;
      el("line", {x1: 0, y1: 60 + ch, x2: cw, y2: 60 + ch, stroke: "#cbd5e1"}, g);
      pols.forEach(([id, name, col], i) => {
        const v = R.table[id][key][0], bh = (v / mx) * ch, x = i * (cw / pols.length) + 12, bw = cw / pols.length - 24;
        const bar = el("rect", {x, y: 60 + ch - bh, width: bw, height: bh, rx: 10, fill: col}, g);
        bar.style.transformBox = "fill-box"; bar.style.transformOrigin = "bottom";
        anim(bar, [{transform: "scaleY(0)"}, {transform: "scaleY(1)"}], c, {d: 900, delay: 150 * i + ci * 250});
        const val = el("text", {x: x + bw / 2, y: 60 + ch - bh - 8, "text-anchor": "middle", style: "font:800 13px Inter,system-ui,sans-serif;fill:#0f172a", opacity: 0}, g);
        val.textContent = fmt(v);
        anim(val, [{opacity: 0}, {opacity: 1}], c, {d: 400, delay: 800 + 150 * i + ci * 250});
        const lab = el("text", {x: x + bw / 2, y: 60 + ch + 20, "text-anchor": "middle", class: "xs-small"}, g);
        lab.textContent = name.length > 14 ? name.slice(0, 13) + "…" : name;
      });
    });
    const rr = R?.table["Relay-RL"], b0 = R?.table.B0, b1 = R?.table.B1;
    panel(7, "Did it work? (30 nights no model ever saw)", rr ? `On 30 held-out nights, replaying the real food and real drivers, <b>Relay + RL</b> rescued <b>${(rr.rescue_rate[0] * 100).toFixed(1)}%</b> of the food with <b>${rr.notif_per_rescue[0].toFixed(1)} pings per rescue</b>, versus ${b0.notif_per_rescue[0].toFixed(0)} for a WhatsApp broadcast (which rescued ${(b0.rescue_rate[0] * 100).toFixed(1)}%). The 412FR-style rule rescues a little more (${(b1.rescue_rate[0] * 100).toFixed(1)}%) but pings ${(b1.notif_per_rescue[0] / rr.notif_per_rescue[0]).toFixed(0)}× as much. That's the burnout trade-off, and it's a setting.` : "Results not available yet: run python sim.py bengaluru 30.",
      `<div class="xp-note">Simulated, not a field result: the simulator replays the dataset's real donation streams and its measured driver behaviour. Zero food-safety violations in every policy.</div>`);
    caption(["simulated · 30 held-out nights · mean"]);
    await c.wait(1200);
  };

  // ---------------------------------------------------------------- engine
  function stepsUI() {
    setHTML($("#xp-steps"), STEPS.map(([t], i) => `<button class="xp-step ${i === X.i ? "on" : i < X.i ? "done" : ""}" data-xp-step="${i}"><i>${i < X.i ? "✓" : i + 1}</i>${h(t)}</button>`).join(""));
    $("#xp-prog").style.width = `${((X.i + 1) / STEPS.length) * 100}%`;
    $("#xp-count").textContent = `Step ${X.i + 1} of ${STEPS.length}`;
    $("#xp-play").textContent = X.playing ? "⏸ Pause" : "▶ Play tour";
    $(`[data-xp-step="${X.i}"]`)?.scrollIntoView({block: "nearest", inline: "nearest", behavior: "smooth"});
  }
  async function goto(i) {
    i = clamp(i, 0, STEPS.length - 1);
    const my = ++X.gen;
    X.i = i;
    stepsUI();
    build();
    const ctx = inst => ({instant: inst, wait: async ms => { if (inst) return; await sleep(reduce ? 0 : ms); if (my !== X.gen) throw STOP; }});
    try {
      for (let j = 0; j < i; j++) { if (j >= 6 && i > j) continue; await SC[j](ctx(true)); }
      if (my !== X.gen) return;
      await SC[i](ctx(false));
      if (X.playing) {
        await ctx(false).wait(HOLD[i]);
        if (i < STEPS.length - 1) goto(i + 1); else { X.playing = false; stepsUI(); }
      }
    } catch (e) { if (e !== STOP) { console.error(e); toast("The animation hit a problem: " + e.message, "err"); } }
  }
  async function load() {
    const [D, T, R] = await Promise.all([api(`/api/admin/rl/explain?live=${X.live}`), X.T ? X.T : api("/api/admin/rl/trace?lam=0.02"),
      X.R ? X.R : api("/api/sim/compare?scenario=bengaluru&seeds=30").catch(() => null)]);
    X.D = D; X.T = T; X.R = R; X.loaded = true;
    if (X.live && !D.real) toast("No donation is waiting right now, so this uses an example from the dataset.", "info");
  }
  async function show() {
    if (!X.loaded) {
      $("#xp-panel").innerHTML = '<div class="skel" style="height:22px;width:50%"></div><div class="skel" style="height:120px"></div><div class="skel" style="height:180px"></div>';
      try { await load(); } catch (e) { return toast(e.message, "err"); }
      goto(0);
      initPlayground();
    } else stepsUI();
  }
  function hide() { X.playing = false; X.gen++; }
  async function fresh(btn) {
    await busy(btn, async () => { X.playing = false; X.gen++; X.show = 0; await load(); goto(0); });
  }

  // ---------------------------------------------------------------- playground
  const PG = {lam: "0.02", t: null, v: {slack: 90, pool: 20, top_p: .25, meals: 25, asked: 0, late: 0}};
  const SL = [["slack", "Minutes until the latest safe pickup", 5, 240, 5, v => `${v} min`], ["pool", "Drivers who can still make it", 0, 40, 1, v => v],
    ["top_p", "Best driver's chance of saying yes", .05, .9, .01, v => `${Math.round(v * 100)}%`], ["meals", "Meals at stake", 5, 80, 1, v => v],
    ["asked", "Drivers already asked", 0, 10, 1, v => v], ["late", "Late night (after 22:00)", 0, 1, 1, v => v ? "yes" : "no"]];
  function initPlayground() {
    if ($("#pg-sliders").children.length) return;
    $("#pg-sliders").innerHTML = SL.map(([k, label, mn, mx, st, f]) => `<div class="slider"><label for="pg-${k}">${label}</label><input type="range" id="pg-${k}" min="${mn}" max="${mx}" step="${st}" value="${PG.v[k]}"><output id="pg-${k}-o">${f(PG.v[k])}</output></div>`).join("");
    askQ();
  }
  function askQ() {
    clearTimeout(PG.t);
    PG.t = setTimeout(async () => {
      try {
        const v = PG.v, x = await api("/api/admin/rl/q", {method: "POST", body: JSON.stringify({slack: +v.slack, pool: +v.pool, top_p: +v.top_p, meals: +v.meals, asked: +v.asked, late: !!+v.late, lam: PG.lam})});
        qBars($("#pg-bars"), x.k, x.q, x.best);
        const bi = x.k.indexOf(x.best);
        setHTML($("#pg-verdict"), `<div class="verdict" data-k="${x.best}">The agent would <b>${x.best ? `ask the top ${x.best}` : "wait"}</b> (Q = ${x.q[bi].toFixed(2)}). ${
          x.k.length < 6 ? `Only ${Math.max(0, +v.pool)} drivers can make it, so bigger waves aren't possible. ` : ""}${PG.lam === "0.25" ? "Pings are costlier here, so it tends to ask fewer." : "Pings are cheap in the dataset's reward, so it leans towards asking more when time is short."}</div>`);
      } catch (e) { toast(e.message, "err"); }
    }, 140);
  }

  // ---------------------------------------------------------------- wiring
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-act],[data-xp-step],[data-xp-show]");
    if (!b) return;
    if (b.dataset.xpStep) { X.playing = false; return goto(+b.dataset.xpStep); }
    if (b.dataset.xpShow) { X.show = +b.dataset.xpShow; return goto(6); }
    const a = b.dataset.act;
    if (a === "xp-next") { X.playing = false; return goto(X.i + 1); }
    if (a === "xp-prev") { X.playing = false; return goto(X.i - 1); }
    if (a === "xp-play") { X.playing = !X.playing; if (X.playing) goto(X.i >= STEPS.length - 1 ? 0 : X.i); else { X.gen++; stepsUI(); } return; }
    if (a === "xp-new") return fresh(b);
    if (a === "xp-full") return present();
    if (a === "xp-src") { X.live = b.dataset.live === "1"; $$("#xp-src button").forEach(x => x.classList.toggle("on", x === b)); return fresh(null); }
    if (a === "pg-lam") { PG.lam = b.dataset.lam; $$("#pg-lam button").forEach(x => x.classList.toggle("on", x === b)); return askQ(); }
  });
  document.addEventListener("input", e => {
    const m = e.target.id?.match(/^pg-(\w+)$/);
    if (!m) return;
    const sl = SL.find(s => s[0] === m[1]);
    PG.v[m[1]] = +e.target.value;
    $(`#pg-${m[1]}-o`).textContent = sl[5](+e.target.value);
    askQ();
  });
  function present() {
    const card = $(".card.xp");
    if (document.fullscreenElement) return document.exitFullscreen();
    card.requestFullscreen?.().catch(() => toast("Full screen isn't available here. Use F11 instead.", "info"));
  }
  document.addEventListener("fullscreenchange", () => {
    const on = document.fullscreenElement === $(".card.xp");
    $("#xp-full").textContent = on ? "✕ Exit presenter" : "⛶ Present";
  });
  document.addEventListener("keydown", e => {   // presenter keys, only while this tab is open and not typing
    if (!$("#v-thinks")?.classList.contains("on") || e.target.closest?.("input,textarea,select") || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === "ArrowRight" || e.key === "PageDown") { e.preventDefault(); X.playing = false; goto(X.i + 1); }
    else if (e.key === "ArrowLeft" || e.key === "PageUp") { e.preventDefault(); X.playing = false; goto(X.i - 1); }
    else if (e.key === " ") { e.preventDefault(); $("#xp-play").click(); }
    else if (e.key === "f" || e.key === "F") { e.preventDefault(); present(); }
    else if (/^[1-8]$/.test(e.key)) { X.playing = false; goto(+e.key - 1); }
  });
  window.XP = {show, hide};
})();
