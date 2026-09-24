"""Discrete-event simulator (§10). Calls the SAME planner functions as the live API.

Common random numbers: everything exogenous (arrivals, shifts, every volunteer's reaction to every
donation) is drawn from RNGs keyed by (scenario, seed[, volunteer, donation]), so every policy
faces an identical world. All results are SIMULATED; parameters are [ASM] unless noted.

    python sim.py [scenario] [seeds]      # prints the comparison table
"""
import csv
import functools
import heapq
import itertools
import json
import math
import os
import random
import statistics
import sys
import time
from collections import Counter, defaultdict

import planner
from core import (CFG, INF, KG_PER_MEAL, L_escalate, State, apply, available, km, latest_pickup,
                  pick_time, safe_until, sigmoid, travel, late)

CENTER = (28.5355, 77.3910)   # Noida (demo city); ponytail: synthetic layout, swap in OSM POIs via Overpass
START, ARRIVALS_END, END = 17 * 60, 24 * 60, 28 * 60   # 17:00 -> posts until 24:00 -> run to 04:00

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.path.join(HERE, "models")
_ML = {"act_at_L": True, "p_model": os.path.join(MODELS, "acceptance.json")}
POLICIES = {
    "B0": (planner.b0, {}),
    "B1": (planner.b1, {}),
    "Relay": (planner.plan, {"act_at_L": True}),
    "Relay -p model": (planner.plan, {"act_at_L": True, "p_const": 0.2}),
    "Relay -early esc": (planner.plan, {"act_at_L": True, "early_escalation": False}),
    # learned variants (python ml.py all): acceptance model, offline-RL wave sizing, behaviour cloning
    "Relay-ML": (planner.plan, _ML),
    "Relay-RL": (planner.plan, {**_ML, "wave": "fqi", "wave_model": os.path.join(MODELS, "fqi_lam0.02.joblib")}),
    "Relay-RL lam=0.25": (planner.plan, {**_ML, "wave": "fqi", "wave_model": os.path.join(MODELS, "fqi_lam0.25.joblib")}),
    "BC-greedy": (planner.plan, {**_ML, "wave": "bc", "wave_model": os.path.join(MODELS, "bc.joblib")}),
}
CLASSIC = ("B0", "B1", "Relay", "Relay -p model", "Relay -early esc")
LEARNED = ("B0", "B1", "Relay", "Relay-ML", "Relay-RL", "Relay-RL lam=0.25", "BC-greedy")


def ready(policy):
    """True if every model file the policy needs exists."""
    over = POLICIES[policy][1]
    return all(os.path.exists(over[k]) for k in ("p_model", "wave_model") if k in over)


def default_policies(scenario):
    return tuple(p for p in (LEARNED if scenario in DATA_SCENARIOS else CLASSIC) if ready(p))

# Calibration (§10.2): arrival rate and volunteer density tuned so B1 gives ~15-17% "negative" rescues
# (never claimed or rescued by a coordinator intervention), matching 412FR's published 16.4% [R1].
# 20 seeds of friday_night: B1 = 16.7%. This does not validate Relay; it keeps the scenario honest.
BASE = dict(rate=0.6, vol=3.0, p_mult=1.0, suburb_vol=1.0, disrupt=False, closure=False)
SCENARIOS = {   # rate / vol are multipliers on BASE
    "normal_weekday": dict(rate=0.6),
    "friday_night": dict(disrupt=True),
    "surge": dict(rate=3.0),
    "volunteer_drought": dict(vol=0.5),
    "recipient_closure": dict(closure=True),
    "rain": dict(p_mult=0.6),
    "sparse_suburb": dict(suburb_vol=0.2),
}

# Donor mix (§10.2): type, share, category, kg range [ASM]
DONORS = [("Restaurant", 0.45, "cooked", (3, 15)), ("Caterer", 0.10, "cooked", (10, 40)),
          ("Campus dining", 0.15, "cooked", (10, 25)), ("Bakery", 0.15, "bakery", (2, 8)),
          ("Grocer", 0.15, "produce", (5, 30))]
NAMES = ["Ravi", "Meena", "Arjun", "Priya", "Kabir", "Anita", "Rohit", "Sana", "Vikram", "Neha", "Imran", "Divya",
         "Farhan", "Pooja", "Karan", "Isha", "Manoj", "Ritu", "Aman", "Tara", "Dev", "Kavya", "Nikhil", "Zoya"]


def point(rng, rmin, rmax):
    ang, r = rng.uniform(0, 2 * math.pi), math.sqrt(rng.uniform(rmin ** 2, rmax ** 2))
    return (round(CENTER[0] + r * math.sin(ang) / 111.0, 5),
            round(CENTER[1] + r * math.cos(ang) / (111.0 * math.cos(math.radians(CENTER[0]))), 5))


def zone(p):
    return "downtown" if km(p, CENTER) < 4 else "suburb"


def rate(t):
    """Donations/hour: evening base, close-of-business peak 21:00-23:30 [ASM]."""
    return 7.0 if 1260 <= t < 1410 else (3.0 if t < 1260 else 2.0)


class World:
    """Synthetic Noida city (the report's scenarios)."""
    cfg, cancel_p, noshow_p, start, end = {}, 0.08, 0.03, START, END   # [ASM]

    def __init__(self, scenario, seed):
        sc = SCENARIOS[scenario]
        P = {**BASE, **sc, "rate": BASE["rate"] * sc.get("rate", 1), "vol": BASE["vol"] * sc.get("vol", 1)}
        self.key, self.P = f"{scenario}:{seed}", P
        rng = random.Random(self.key)
        self.setup = [{"type": "BackupSet", "loc": CENTER}]

        for i in range(12):   # 6 day kitchens (09:00 to 21:00-22:30), 6 open late, 2 of those breakfast-only
            day = i < 6
            loc = point(rng, 0.5, 4) if i % 2 == 0 else point(rng, 4, 9)
            need = rng.uniform(60, 200)
            self.setup.append({"type": "RecipientAdded", "r": dict(
                id=f"r{i:02d}", name=f"Shelter {chr(65 + i)}", loc=loc,
                alpha=540 if day else 0, beta=rng.uniform(1260, 1350) if day else INF,
                mu=rng.choice([0, 30, 60]) if day else (480 if i >= 10 else rng.choice([0, 15, 30])),
                cap={"hot": rng.uniform(20, 60) if day else rng.uniform(60, 120),
                     "cold": rng.uniform(10, 30), "ambient": rng.uniform(20, 80)},
                veg_only=rng.random() < 0.25, confidential=(i == 3), need=need,
                received=need * rng.uniform(0.3, 0.9), zone=zone(loc))})

        self.theta = {}
        n = 0
        for i in range(round(45 * P["vol"])):
            sub = rng.random() < 0.4
            loc = point(rng, 4, 10) if sub else point(rng, 0, 4)
            theta = rng.gauss(0.2, 0.8)
            hist = rng.randint(0, 30)
            p_hist = sigmoid(theta - 1.9)   # truth at population-mean features
            if sub and rng.random() > P["suburb_vol"]:
                continue
            vid = f"v{n:02d}"
            n += 1
            self.theta[vid] = theta
            start = rng.uniform(1020, 1260)
            self.setup.append({"type": "VolunteerAdded", "v": dict(
                id=vid, name=NAMES[n % len(NAMES)] + (f" {n // len(NAMES) + 1}" if n >= len(NAMES) else ""),
                loc=loc, cap_kg=30 if rng.random() < 0.7 else 15, start=start, end=start + rng.uniform(120, 300),
                n7=rng.randint(0, 4), acc=sum(rng.random() < p_hist for _ in range(hist)), off=hist,
                zone=zone(loc))})

        self.exo = []   # (t, event)
        t, k, lam_max = START, 0, 7.0 * P["rate"]
        while True:     # non-homogeneous Poisson by thinning
            t += rng.expovariate(lam_max / 60)
            if t >= ARRIVALS_END:
                break
            if rng.random() < rate(t) * P["rate"] / lam_max:
                self.exo.append((t, self._donation(rng, t, k)))
                k += 1
        recips = [e["r"] for e in self.setup if e["type"] == "RecipientAdded"]
        if P["disrupt"]:   # the demo burst at 22:00: a shelter fills up, a caterer posts 25 kg
            late_open = [r for r in recips if r["beta"] >= INF and r["mu"] < 480]
            big = max(late_open, key=lambda r: r["cap"]["hot"])
            self.exo.append((1320.0, {"type": "CapacityUpdated", "r": big["id"], "full": ["hot"]}))
            self.exo.append((1320.0, self._donation(rng, 1320.0, k, kind=DONORS[1], kg=25.0)))
        if P["closure"]:   # the two largest late-open recipients go offline at 21:00
            for r in sorted([r for r in recips if r["beta"] >= INF], key=lambda r: -sum(r["cap"].values()))[:2]:
                self.exo.append((1260.0, {"type": "CapacityUpdated", "r": r["id"], "full": ["hot", "cold", "ambient"]}))

    def _donation(self, rng, t, k, kind=None, kg=None):
        name, _, cat, (lo, hi) = kind or rng.choices(DONORS, weights=[x[1] for x in DONORS])[0]
        loc = point(rng, 0, 4) if (name == "Restaurant" and rng.random() < 0.7) else point(rng, 0, 10)
        holding = {"bakery": "ambient", "produce": "ambient"}.get(cat, "cold" if name == "Caterer" and rng.random() < 0.5 else "hot")
        kg = kg or round(rng.uniform(lo, hi), 1)
        # donor window: PS says most surplus has a 2-6 h usable window; restaurants close 22:30-24:00 [ASM]
        close = max(rng.uniform(1350, 1440), t + 90) if name == "Restaurant" else t + rng.uniform(120, 360)
        return {"type": "DonationPosted", "d": dict(
            id=f"d{k:03d}", donor=f"{name} {k}", loc=loc, category=cat, holding=holding,
            veg=rng.random() < 0.6, kg=kg, meals=round(kg / KG_PER_MEAL, 1), a=t, b=close,
            safe_until=safe_until(cat, t), posted=t, zone=zone(loc))}

    def draw(self, v, d):
        r = random.Random(f"{self.key}:{v}:{d}")
        return dict(u=r.random(), delay=4 * math.exp(0.6 * r.gauss(0, 1)), eps=r.gauss(0, 0.5),
                    cu=r.random(), cf=r.random(), nu=r.random())

    def p_true(self, v, d, t, eps):
        z = self.theta[v.id] - 0.35 * km(v.loc, d.loc) - 0.8 * late(t) - 0.3 * math.log1p(v.n7) + eps
        return sigmoid(z) * self.P["p_mult"]


# ---------------------------------------------------------------- dataset world (relay_data/)

DATA = os.path.join(HERE, "relay_data")
DATA_SCENARIOS = {"bengaluru": dict(strict=False), "bengaluru_strict": dict(strict=True)}
TEST_EPISODES = range(255, 300)      # held out from every model trained by ml.py
TRAIN_EPISODES = range(0, 210)
EPISODE_START = 18 * 60              # t=0 is 18:00 [INF: volunteer shifts 17-26 h; generator's 240-min late flag]
# Measured from the logs (python ml.py audit prints and re-checks each one):
P_RESPOND, DELAY_MU, DELAY_SIGMA, CANCEL_P, NOSHOW_P = 0.751, 1.388, 0.80, 0.079, 0.030
DATA_CFG = dict(min_per_km=3.0, prep=5, sigma=15, rho_v=5)   # travel fit: pickup 3.0*km+5, drop 3.0*km+15


@functools.cache
def data_tables():
    def rd(f):
        with open(os.path.join(DATA, f), newline="") as fh:
            return list(csv.DictReader(fh))
    by_ep = defaultdict(list)
    for r in rd("donations.csv"):
        by_ep[int(r["episode_id"])].append(r)
    n7, seen, hist = {}, defaultdict(list), defaultdict(lambda: [0, 0])
    with open(os.path.join(DATA, "offers.csv"), newline="") as fh:
        for r in csv.DictReader(fh):
            ep, vid, k = int(r["episode_id"]), r["volunteer_id"], int(r["n_offers_7d"])
            n7.setdefault((ep, vid), k)
            seen[vid].append(k)
            if ep in TRAIN_EPISODES:   # Beta history for the hand-set model: training episodes only
                h = hist[vid]
                h[1] += 1
                h[0] += r["accept"] == "True" and float(r["response_delay_min"] or 99) <= 8
    med = {v: statistics.median_low(ks) for v, ks in seen.items()}
    return by_ep, rd("volunteers.csv"), rd("recipients.csv"), n7, med, dict(hist)


class DataWorld:
    """Bengaluru from relay_data/: the real recipients and volunteers, and a held-out episode's real donation
    stream. Volunteer behaviour follows the generator's recovered ground truth (see ml.py audit).
    Default ("bengaluru") receives at any hour, as the logs did (76% of logged deliveries fell outside the listed
    hours); "bengaluru_strict" enforces the listed hours. The safety rules (4 h clock, use-lead) always apply."""
    cfg, cancel_p, noshow_p, start, end = DATA_CFG, CANCEL_P, NOSHOW_P, EPISODE_START, EPISODE_START + 480

    def __init__(self, scenario, seed):
        strict = DATA_SCENARIOS[scenario]["strict"]
        ep = TEST_EPISODES[seed % len(TEST_EPISODES)]
        by_ep, vols, recs, n7, med, hist = data_tables()
        self.key, self.episode = f"{scenario}:{ep}", ep
        center = (statistics.fmean(float(r["lat"]) for r in recs), statistics.fmean(float(r["lon"]) for r in recs))
        self.setup = [{"type": "BackupSet", "loc": center}]
        for r in recs:
            cooked, amb = float(r["capacity_cooked_kg"]), float(r["capacity_ambient_kg"])
            self.setup.append({"type": "RecipientAdded", "r": dict(
                id=r["recipient_id"], name=f"Shelter {r['recipient_id']}", loc=(float(r["lat"]), float(r["lon"])),
                alpha=60 * int(r["open_hour"]) if strict else 0, beta=60 * int(r["close_hour"]) if strict else INF,
                mu=float(r["use_lead_min"]), cap={"hot": cooked, "cold": cooked, "ambient": amb},
                veg_only=r["veg_only"] == "True", confidential=r["confidential"] == "True",
                need=(cooked + amb) / KG_PER_MEAL, received=0.0, zone=r["zone"])})
        self.theta = {}
        for i, v in enumerate(vols):
            vid, home = v["volunteer_id"], (float(v["home_lat"]), float(v["home_lon"]))
            self.theta[vid] = float(v["theta_true"])   # simulator ground truth only; never a model feature
            acc, off = hist.get(vid, (0, 0))
            self.setup.append({"type": "VolunteerAdded", "v": dict(
                id=vid, name=f"{NAMES[i % len(NAMES)]} ({vid})", loc=home, home=home,
                cap_kg=float(v["capacity_kg"]), start=60 * float(v["shift_start_h"]), end=60 * float(v["shift_end_h"]),
                n7=n7.get((ep, vid), med.get(vid, 0)), acc=int(acc), off=int(off), zone=v["zone"])})
        self.exo = []
        for r in sorted(by_ep[ep], key=lambda r: float(r["post_t"])):
            t, cat = self.start + float(r["post_t"]), r["category"]
            self.exo.append((t, {"type": "DonationPosted", "d": dict(
                id=r["donation_id"], donor=f"{r['donor_type'].title()} {r['donation_id'][-4:]}",
                loc=(float(r["pickup_lat"]), float(r["pickup_lon"])), category=cat, holding=r["holding"],
                veg=True if cat in ("bakery", "produce") else None,   # the data has no veg flag: unknown
                kg=float(r["weight_kg"]), meals=float(r["meals"]), a=t, b=self.start + float(r["ready_until_t"]),
                safe_until=min(safe_until(cat, t), self.start + float(r["safety_deadline_t"])),  # stricter of the two
                posted=t, zone=r["zone"])}))

    def draw(self, v, d):
        r = random.Random(f"{self.key}:{v}:{d}")
        return dict(u=r.random(), delay=min(60.0, math.exp(DELAY_MU + DELAY_SIGMA * r.gauss(0, 1))), eps=0.0,
                    cu=r.random(), cf=r.random(), nu=r.random())

    def p_true(self, v, d, t, eps):
        """Recovered exactly from offers.p_true (R^2 = 1) with late night on the real clock, times P(respond)."""
        z = self.theta[v.id] - 0.35 * km(v.loc, d.loc) - 0.8 * late(t) - 0.3 * math.log1p(v.n7) - 0.5 * d.kg / v.cap_kg
        return P_RESPOND * sigmoid(z)


def make_world(scenario, seed):
    return DataWorld(scenario, seed) if scenario in DATA_SCENARIOS else World(scenario, seed)


@functools.cache
def _json(path):
    with open(path) as f:
        return json.load(f)


def coordinator(s, now, cfg):
    """Simulated coordinator: dispatches the backup for escalated, unclaimed food (Relay at L_d, baselines at once)."""
    out = []
    for d in s.donations.values():
        if d.status in planner.OPEN and d.escalated and d.recipient:
            r = s.recipients[d.recipient]
            act = L_escalate(s, d, r, cfg) if cfg["act_at_L"] else d.esc_t
            eta = pick_time(d, s.backup, now, cfg["rho_B"], cfg)
            if now >= act and eta <= latest_pickup(d, r, cfg):
                out.append(planner.ev("BackupDispatched", d=d.id, eta_pick=eta))
    return out


def run(scenario, policy, seed, record=False):
    fn, over = POLICIES[policy]
    w = make_world(scenario, seed)
    cfg = {**CFG, **w.cfg, **over}
    if isinstance(cfg.get("p_model"), str):
        cfg["p_model"] = _json(cfg["p_model"])
    s, log, lat, frames = State(), [], [], []
    START, END = w.start, w.end
    heap, seq = [], itertools.count()

    def push(t, item):
        heapq.heappush(heap, (t, next(seq), item))

    def emit(e, t):
        e["t"] = t
        apply(s, e)
        log.append(e)
        k = e["type"]
        if k == "OfferSent":
            v, d, x = s.volunteers[e["v"]], s.donations[e["d"]], w.draw(e["v"], e["d"])
            if x["u"] < w.p_true(v, d, t, x["eps"]) and t + x["delay"] < e["expires"]:
                push(t + x["delay"], ("accept", e["id"]))
        elif k in ("OfferAccepted", "BackupDispatched"):
            d = s.donations[e["d"]] if k == "BackupDispatched" else s.donations[s.offers[e["offer"]].d]
            x = w.draw(d.volunteer, d.id) if d.volunteer != "backup" else dict(cu=1, nu=1)
            if x["cu"] < w.cancel_p:      # cancel after accept
                push(t + x["cf"] * (d.eta_pick - t), ("cancel", d.id, d.volunteer))
            elif x["nu"] < w.noshow_p:    # no-show, noticed 15 min after the expected pickup
                push(d.eta_pick + 15, ("cancel", d.id, d.volunteer))
            else:
                push(d.eta_pick, ("pickup", d.id, d.volunteer))
        elif k == "PickedUp":
            d = s.donations[e["d"]]
            r = s.recipients[d.recipient]
            push(max(t + cfg["sigma"] + travel(d.loc, r.loc, cfg), r.alpha), ("deliver", d.id))

    for e in w.setup:
        emit(dict(e), START)
    for t, e in w.exo:
        push(t, ("exo", e))
    for m in range(int(START), int(END) + 1):
        push(float(m), ("tick",))

    while heap:
        t, _, item = heapq.heappop(heap)
        if t > END:
            break
        k = item[0]
        if k == "exo":
            emit(dict(item[1]), t)
        elif k == "accept":
            o = s.offers[item[1]]
            d, v = s.donations[o.d], s.volunteers[o.v]
            r = s.recipients.get(d.recipient)
            eta = pick_time(d, v.loc, t, 0, cfg)
            if o.status == "sent" and d.status in planner.OPEN and available(v, t) and r and eta <= latest_pickup(d, r, cfg):
                emit({"type": "OfferAccepted", "offer": o.id, "eta_pick": eta}, t)
            elif o.status == "sent":
                emit({"type": "OfferDeclined", "offer": o.id}, t)   # "no longer needed" / can't make it
        elif k == "cancel":
            d = s.donations[item[1]]
            if d.status == "claimed" and d.volunteer == item[2]:
                emit({"type": "ClaimCancelled", "d": d.id}, t)
        elif k == "pickup":
            d = s.donations[item[1]]
            if d.status == "claimed" and d.volunteer == item[2]:
                emit({"type": "PickedUp", "d": d.id}, t)
        elif k == "deliver":
            d = s.donations[item[1]]
            if d.status == "picked_up":
                ok = t <= s.recipients[d.recipient].beta
                emit({"type": "Delivered" if ok else "Expired", "d": d.id, "reason": "recipient closed"}, t)
        if heap and heap[0][0] == t:
            continue   # batch simultaneous events, then decide once
        for e in coordinator(s, t, cfg):
            emit(e, t)
        t0 = time.perf_counter()
        acts = fn(s, t, cfg)
        lat.append((time.perf_counter() - t0) * 1000)
        for e in acts:
            emit(e, t)
        if record and k == "tick":
            frames.append(_frame(s, t, log))

    out = {"metrics": metrics(s, log, lat, cfg)}
    if record:
        out.update(frames=frames, state=s, log=log)
    return out


def _frame(s, t, log):
    ok = sum(d.kg for d in s.donations.values() if d.status == "delivered")
    exp = sum(d.kg for d in s.donations.values() if d.status == "expired")
    return {"t": t, "kg": round(ok, 1), "exp": round(exp, 1),
            "n": sum(1 for e in log if e["type"] == "OfferSent"),
            "d": {d.id: [d.status, int(d.escalated), d.recipient, d.volunteer] for d in s.donations.values()}}


def pct(xs, q):
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(q * len(xs)))]


def metrics(s, log, lat, cfg):
    """§11.2 metrics for one run."""
    ds = list(s.donations.values())
    posted = sum(d.kg for d in ds) or 1.0
    delivered = [d for d in ds if d.status == "delivered"]
    safe = [d for d in delivered if d.t_drop + s.recipients[d.recipient].mu <= d.safe_until]
    offers = [e for e in log if e["type"] == "OfferSent"]
    first_claim = {}
    for e in log:
        if e["type"] in ("OfferAccepted", "BackupDispatched"):
            did = e["d"] if "d" in e else s.offers[e["offer"]].d
            first_claim.setdefault(did, e["t"])
    claim = [first_claim[d.id] - d.posted for d in ds if d.id in first_claim]
    esc = [e for e in log if e["type"] == "EscalationRaised"]
    leads = [e["L"] - e["t"] for e in esc if e.get("L") is not None]
    backups = {e["d"] for e in log if e["type"] == "BackupDispatched"}
    safe_ids, zones = {d.id for d in safe}, {}
    for d in ds:
        z = zones.setdefault(d.zone, [0.0, 0.0])
        z[0] += d.kg
        z[1] += d.kg if d.id in safe_ids else 0
    rates = [z[1] / z[0] for z in zones.values() if z[0]]
    rs = s.recipients.values()
    return dict(
        donations=len(ds),
        rescue_rate=sum(d.kg for d in safe) / posted,
        meals=sum(d.meals for d in safe),
        expired_kg=sum(d.kg for d in ds if d.status == "expired"),
        safety_violations=len(delivered) - len(safe),
        notifications=len(offers),
        notif_per_rescue=len(offers) / max(1, len(safe)),
        p95_offers_per_vol=pct(list(Counter(e["v"] for e in offers).values()), 0.95),
        max_n7=max((v.n7 for v in s.volunteers.values()), default=0),
        claim_p50=pct(claim, 0.5), claim_p90=pct(claim, 0.9),
        escalations=len(esc),
        late_escalations=sum(1 for x in leads if x < 10),
        esc_lead_p50=pct(leads, 0.5),
        backups=len(backups),
        negative_pct=100 * sum(1 for d in ds if d.status == "expired" or d.id in backups) / max(1, len(ds)),
        zone_gap=(max(rates) - min(rates)) if rates else 0.0,
        min_fill=min((r.received / r.need for r in rs), default=0.0),
        latency_p50_ms=pct(lat, 0.5), latency_p95_ms=pct(lat, 0.95),
    )


def _one(args):
    return run(*args)["metrics"]


def compare(scenario="friday_night", seeds=30, policies=None, workers=None):
    """Paired comparison over seeds with common random numbers. Mean and 95% CI (normal approx.).
    Data scenarios: seed i replays held-out test episode 255 + i."""
    policies = tuple(policies or default_policies(scenario))
    jobs = [(scenario, p, sd) for p in policies for sd in range(seeds)]
    if workers == 1:
        res = [_one(j) for j in jobs]
    else:
        from concurrent.futures import ProcessPoolExecutor
        with ProcessPoolExecutor(workers) as ex:
            res = list(ex.map(_one, jobs, chunksize=4))
    by = {p: res[i * seeds:(i + 1) * seeds] for i, p in enumerate(policies)}

    def ci(xs):
        xs = [x for x in xs if x is not None]   # e.g. no escalations -> no lead time
        if not xs:
            return [None, None]
        m = statistics.fmean(xs)
        return [m, 1.96 * statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else 0.0]

    keys = list(res[0])
    table = {p: {k: ci([m[k] for m in by[p]]) for k in keys} for p in policies}
    paired = {}
    if "B1" in by:
        for p in policies:
            if p != "B1":
                paired[p] = {k: ci([a[k] - b[k] for a, b in zip(by[p], by["B1"])
                                    if a[k] is not None and b[k] is not None]) for k in keys}
    label = ("Simulated on relay_data: real donation streams of held-out test episodes, real volunteers and recipients, "
             "volunteer behaviour from the recovered generator" if scenario in DATA_SCENARIOS else
             "Simulated; parameters in Appendix; calibrated to 1 published statistic (16.4% negative rescues [R1])")
    return {"scenario": scenario, "seeds": seeds, "table": table, "paired_vs_B1": paired, "label": label}


def demo(scenario="friday_night", seed=0, policies=None):
    """Same seed, two policies, minute-by-minute frames for the split-screen view."""
    if policies is None:
        best = [p for p in ("Relay-RL lam=0.25", "Relay-ML") if scenario in DATA_SCENARIOS and ready(p)]
        policies = ("B1", best[0] if best else "Relay")
    out = {"policies": list(policies)}
    for p in policies:
        r = run(scenario, p, seed, record=True)
        s = r["state"]
        feed = [{"t": e["t"], "type": e["type"], "d": e.get("d") or (s.offers[e["offer"]].d if "offer" in e else None),
                 "r": e.get("r"), "reason": e.get("reason")}
                for e in r["log"] if e["type"] in ("EscalationRaised", "BackupDispatched", "ClaimCancelled",
                                                   "Delivered", "Expired", "RecipientChosen", "CapacityUpdated")]
        out[p] = {"metrics": r["metrics"], "frames": r["frames"], "feed": feed}
    s = r["state"]
    out["donations"] = [{"id": d.id, "donor": d.donor, "kg": d.kg, "category": d.category, "loc": d.loc,
                         "posted": d.posted, "safe_until": d.safe_until, "b": d.b, "zone": d.zone}
                        for d in s.donations.values()]
    out["recipients"] = {r.id: {"name": r.name, "loc": r.loc} for r in s.recipients.values()}
    out["volunteers"] = {v.id: v.name for v in s.volunteers.values()}
    return out


if __name__ == "__main__":
    scen = sys.argv[1] if len(sys.argv) > 1 else "friday_night"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    t0 = time.time()
    res = compare(scen, n, sys.argv[3].split(",") if len(sys.argv) > 3 else None)
    print(f"{scen}, {n} seeds, {time.time() - t0:.1f}s  -- {res['label']}")
    keys = ["rescue_rate", "meals", "expired_kg", "safety_violations", "notif_per_rescue", "p95_offers_per_vol",
            "claim_p50", "escalations", "late_escalations", "backups", "negative_pct", "zone_gap", "latency_p95_ms"]
    print(f"{'metric':20}" + "".join(f"{p:>20}" for p in res["table"]))
    for k in keys:
        print(f"{k:20}" + "".join(f"{m:>12.3f} +-{c:<5.3f}" if m is not None else f"{'-':>20}"
                                  for m, c in (res["table"][p][k] for p in res["table"])))
    with open(f"results_{scen}.json", "w") as f:
        json.dump(res, f, indent=1)
