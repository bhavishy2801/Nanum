"""Policies: plan(state, now, cfg) -> list of events. Pure: reads state, never mutates it.

The same functions run in the live API and inside the simulator (§5.5).
  plan  - Relay: stage 0 safety, stage 1 recipient selection, stage 2 deadline-aware waves + escalation.
  b0    - WhatsApp-style broadcast baseline.
  b1    - 412FR-style static tiered baseline [R1].
ponytail: stage 3 (pooling insertion) and B2 are skipped; add when multi-stop matters (see ablation plan §11.5).
"""
import math

from core import CFG, INF, L_escalate, available, km, late, latest_pickup, p_accept, pick_time, storage, travel, why_not

OPEN = ("posted", "offered")


def ev(type, **kw):
    return {"type": type, **kw}


def _stage0(s, now, cfg, out):
    """Expire timed-out offers and unclaimed food that can no longer be used safely. Returns handled ids."""
    for o in s.offers.values():
        if o.status == "sent" and now >= o.expires:
            out.append(ev("OfferExpired", offer=o.id))
    gone = set()
    for d in s.donations.values():
        if d.status in OPEN and (now >= d.safe_until - cfg["eps"] or now > d.b):
            reason = "safety deadline" if now >= d.safe_until - cfg["eps"] else "donor closed, food binned"
            out.append(ev("Expired", d=d.id, reason=reason))
            gone.add(d.id)
    return gone


def pickups(s, d, now, cfg):
    """Sorted earliest pickup times over available volunteers who can carry d."""
    return sorted(pick_time(d, v.loc, now, cfg["rho_v"], cfg)
                  for v in s.volunteers.values() if available(v, now) and v.cap_kg >= d.kg)


def pickup_bounds(s, d, now, cfg):
    """(t0, t3): earliest pickup by anyone incl. the backup, and the robust 3rd-nearest volunteer ETA (§9.3)."""
    if d.status == "claimed":
        return d.eta_pick, d.eta_pick
    p, bk = pickups(s, d, now, cfg), pick_time(d, s.backup, now, cfg["rho_B"], cfg)
    return (min(p[0], bk) if p else bk), (p[min(2, len(p) - 1)] if p else bk)


def _score(d, r, t3, cfg):
    """Stage-1 score, meal-normalised (§9.3): fill-rate equity - travel - robust-slack shortfall."""
    F = min(1.0, r.received / r.need) if r.need else 1.0
    slack_hat = latest_pickup(d, r, cfg) - t3
    return 1 + cfg["lam_F"] * (1 - F) - cfg["lam_T"] * travel(d.loc, r.loc, cfg) - cfg["lam_S"] * max(0.0, cfg["S_min"] - slack_hat)


def _stage1(s, now, cfg, out, gone, relay, repick):
    """Pick a recipient per open or claimed donation, most urgent first. Returns {donation id: recipient}."""
    free = {r.id: dict(r.cap) for r in s.recipients.values()}
    todo = []
    for d in s.donations.values():
        if d.id in gone or d.status not in ("posted", "offered", "claimed"):
            continue
        t0, t3 = pickup_bounds(s, d, now, cfg)
        urgency = max((latest_pickup(d, r, cfg) for r in s.recipients.values()), default=-INF) - t3
        todo.append((urgency, d, t0, t3))
    todo.sort(key=lambda x: x[0])

    chosen = {}
    for _, d, t0, t3 in todo:
        g, cur = storage(d), s.recipients.get(d.recipient)
        if cur and (not repick or why_not(d, cur, free[cur.id][g], True, t0, cfg) is None):
            chosen[d.id] = cur   # hysteresis: keep a working recipient; B0 never re-plans
            continue
        cands = [r for r in s.recipients.values()
                 if r is not cur and why_not(d, r, free[r.id][g], False, t0, cfg) is None]
        if not cands:
            if d.status == "claimed":
                out.append(ev("Expired", d=d.id, reason="no recipient reachable in time"))
                gone.add(d.id)
            elif relay and not d.escalated:
                out.append(ev("EscalationRaised", d=d.id, reason="no feasible recipient", L=None))
            continue
        if relay:
            best = max(cands, key=lambda r: _score(d, r, t3, cfg))
        else:
            best = min(cands, key=lambda r: travel(d.loc, r.loc, cfg))
        free[best.id][g] -= d.kg
        if d.id in s.holds:
            free[s.holds[d.id][0]][g] += d.kg
        out.append(ev("RecipientChosen", d=d.id, r=best.id))
        chosen[d.id] = best
    return chosen


def eligible(s, d, r, now, cfg):
    """E_d: available, under budget K, can carry, not offered d before, and can reach d in time."""
    lp = latest_pickup(d, r, cfg)
    return [v for v in s.volunteers.values()
            if available(v, now) and v.n7 < cfg["K"] and v.cap_kg >= d.kg
            and f"{d.id}:{v.id}" not in s.offers and pick_time(d, v.loc, now, cfg["rho_v"], cfg) <= lp]


def claim_odds(s, d, r, now, cfg, skip=()):
    """(outstanding offers O, eligible E, p for E, P^max = 1 - prod(1 - p) over E u O)."""
    O = [o for o in s.offers.values() if o.d == d.id and o.status == "sent" and o.id not in skip]
    E = eligible(s, d, r, now, cfg)
    p = {v.id: p_accept(v, d, now, cfg) for v in E}
    miss = math.prod(1 - (o.p or 0) for o in O) * math.prod(1 - x for x in p.values())
    return O, E, p, 1 - miss


def wave_target(W, cfg):
    """tau_w = 1 - (1 - tau*)^(1/W): per-wave target so W waves reach tau* by L_d."""
    return 1 - (1 - cfg["tau_star"]) ** (1 / W)


# ---------------------------------------------------------------- learned wave sizing (ml.py trains these)

RL_K = (0, 1, 2, 3, 5, 8)   # candidate wave sizes (top-k by p); the logs never send more per 8-min step, and
                              # offline RL must not pick actions it has no data for
RL_FEATURES = ["since_post", "slack", "kg", "meals", "cat_cooked", "cat_bakery", "cat_produce", "late",
               "n_offered", "mass_offered", "n_pool", "top1", "top3", "top8"]
_models = {}


def rl_state(since, slack, kg, meals, category, late_, n_offered, mass_offered, pool):
    """RL/BC state. `pool` = p of volunteers who could still reach the donor in time, sorted descending.
    ml.py builds the same vector from the logs; test_relay.py checks the two agree."""
    return [since, slack, kg, meals, float(category == "cooked"), float(category == "bakery"),
            float(category == "produce"), late_, n_offered, mass_offered, len(pool),
            sum(pool[:1]), sum(pool[:3]), sum(pool[:8])]


def rl_candidates(pool):
    """Feasible actions as (k, expected-claim mass of the top-k)."""
    return [(k, sum(pool[:k])) for k in RL_K if k <= len(pool)]


def _model(path):
    if path not in _models:
        import joblib   # only the learned policies need scikit-learn at runtime
        _models[path] = joblib.load(path)
    return _models[path]


def _learned_k(s, d, now, cfg):
    """Wave size from the FQI Q-function (argmax over candidates) or the behaviour-cloning classifier."""
    pool = sorted((p_accept(v, d, now, cfg) for v in s.volunteers.values()
                   if available(v, now) and v.cap_kg >= d.kg and f"{d.id}:{v.id}" not in s.offers
                   and pick_time(d, v.loc, now, 0, cfg) <= d.b), reverse=True)
    mine = [o for o in s.offers.values() if o.d == d.id]
    x = rl_state(now - d.posted, min(d.b, d.safe_until - cfg["eps"]) - now, d.kg, d.meals, d.category,
                 late(now), len(mine), sum(o.p or 0 for o in mine), pool)
    m = _model(cfg["wave_model"])
    if cfg["wave"] == "bc":
        return RL_K[int(m.predict([x])[0])]
    cands = rl_candidates(pool)
    q = m.predict([x + [k, mass] for k, mass in cands])
    return cands[int(q.argmax())][0]


def _wave(s, d, E, p, L, now, cfg):
    if cfg.get("wave", "noisy_or") != "noisy_or":
        k = _learned_k(s, d, now, cfg)
        return sorted(E, key=lambda v: p[v.id], reverse=True)[:k]
    W = math.floor((L - now) / cfg["wave_timeout"])
    if W < 1:
        return E   # out of time: blast what's left
    tau_w, wave, miss = wave_target(W, cfg), [], 1.0
    for v in sorted(E, key=lambda v: p[v.id] / (1 + cfg["gamma"] * v.n7), reverse=True):
        if 1 - miss >= tau_w or len(wave) >= cfg["k_max"]:
            break
        wave.append(v)
        miss *= 1 - p[v.id]
    return wave


def plan(s, now, cfg=CFG):
    """Relay. Wave sizing is noisy-OR by default, or learned (cfg["wave"] = "fqi" | "bc"); safety,
    recipient choice and the escalation deadline L_d stay rule-based whatever sizes the waves."""
    out = []
    gone = _stage0(s, now, cfg, out)
    chosen = _stage1(s, now, cfg, out, gone, relay=True, repick=True)
    expiring = {e["offer"] for e in out if e["type"] == "OfferExpired"}
    sent = {}   # offers made earlier in this same pass count against the budget K too
    for d in s.donations.values():
        if d.id in gone or d.status not in OPEN or d.id not in chosen:
            continue
        r = chosen[d.id]
        L = L_escalate(s, d, r, cfg)
        O, E, p, pmax = claim_odds(s, d, r, now, cfg, expiring)
        if not d.escalated and (now >= L - cfg["alert_lead"] or (cfg["early_escalation"] and pmax < cfg["tau_min"])):
            reason = "escalation deadline near" if now >= L - cfg["alert_lead"] else f"low claim odds ({pmax:.2f})"
            out.append(ev("EscalationRaised", d=d.id, reason=reason, L=L, pmax=round(pmax, 3)))
        if O or not E:
            continue   # wait for the current wave to time out
        for v in _wave(s, d, E, p, L, now, cfg):
            if v.n7 + sent.get(v.id, 0) >= cfg["K"]:
                continue
            sent[v.id] = sent.get(v.id, 0) + 1
            out.append(ev("OfferSent", id=f"{d.id}:{v.id}", d=d.id, v=v.id,
                          expires=now + cfg["wave_timeout"], p=round(p[v.id], 4)))
    return out


def _baseline(s, now, cfg, repick, radius, trigger, reason):
    out = []
    gone = _stage0(s, now, cfg, out)
    chosen = _stage1(s, now, cfg, out, gone, relay=False, repick=repick)
    for d in s.donations.values():
        if d.id in gone or d.status not in OPEN or d.id not in chosen:
            continue
        rad = radius(d)
        for v in s.volunteers.values():
            if available(v, now) and f"{d.id}:{v.id}" not in s.offers and km(v.loc, d.loc) <= rad:
                out.append(ev("OfferSent", id=f"{d.id}:{v.id}", d=d.id, v=v.id, expires=INF, p=None))
        if not d.escalated and now >= trigger(d):
            out.append(ev("EscalationRaised", d=d.id, reason=reason, L=L_escalate(s, d, chosen[d.id], cfg)))
    return out


def b0(s, now, cfg=CFG):
    """Broadcast to everyone at once; nearest feasible recipient; no re-planning; coordinator 30 min before expiry."""
    return _baseline(s, now, cfg, False, lambda d: INF, lambda d: d.safe_until - 30, "30 min to expiry")


def b1(s, now, cfg=CFG):
    """412FR 2018-19 scheme [R1]: 8 km (~5 mi) radius, everyone after 15 min, coordinator at 60 min of window left."""
    return _baseline(s, now, cfg, True, lambda d: 8.0 if now < d.posted + 15 else INF,
                     lambda d: d.b - 60, "60 min of pickup window left")
