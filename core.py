"""Relay core: config, models, safety clock, feasibility (§9.2) and the event projector.

All times are minutes since midnight of day 0 (23:30 = 1410, 01:00 next day = 1500).
State is only ever changed by apply(state, event), so replaying the log rebuilds it exactly.
"""
import copy
import math
from dataclasses import dataclass, field
from functools import lru_cache

INF = 1e9

# Appendix B planner defaults (tune in simulation) + a few physical constants [ASM].
CFG = dict(
    tau_star=0.95, tau_min=0.80, wave_timeout=8, k_max=6, K=10, gamma=0.15, budget_window=7 * 1440,
    eps=20, eps_esc=10, alert_lead=10, rho_B=20,
    lam_F=0.5, lam_T=0.02, lam_S=0.05, S_min=60,
    rho_v=5,            # expected volunteer response delay (min)
    sigma=5,            # handling time at pickup (min)
    prep=0,             # volunteer prep before driving to the pickup (min)
    min_per_km=3.24,    # haversine x 1.35 detour at 25 km/h; the data world measures 3.0
    # Acceptance model (§6.3): logistic slopes on (dist km, late night, log(1+offers_7d)),
    # centred at population-mean features; per-volunteer intercept from Beta shrinkage.
    beta=(-0.35, -0.8, -0.3), feat_mean=(3.0, 0.5, 1.5), prior=(1.0, 4.0),
    p_model=None,             # learned acceptance model (models/acceptance.json via load_p_model); overrides beta
    p_const=None,             # ablation: constant p instead of the model
    early_escalation=True,    # ablation: P^max < tau_min rule
    act_at_L=False,           # sim coordinator: dispatch backup at L_d (Relay) vs at escalation (baselines)
)

# Appendix A: conservative defaults, must be validated by the local food-safety authority [R5, ASM].
HORIZON = {"cooked": 240, "dairy": 240, "mixed": 240, "unknown": 240, "bakery": 1440, "produce": 2880}
CATEGORIES = ["cooked", "dairy", "bakery", "produce", "packaged", "mixed", "unknown"]
HOLDINGS = ["hot", "cold", "ambient", "unknown"]
KG_PER_MEAL = 0.544  # 1.2 lb per meal convention [R9, R32]


def safe_until(category, t_off, label_until=None):
    """Hard safety deadline s_d. Never set by ML. Packaged food without a label date gets the 4 h default."""
    if category == "packaged":
        return label_until if label_until is not None else t_off + 240
    return t_off + HORIZON.get(category, 240)


def storage_class(category, holding):
    if category == "dairy":
        return "cold"
    if category in ("bakery", "produce", "packaged"):
        return "ambient"
    return "cold" if holding == "cold" else "hot"


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


@lru_cache(maxsize=None)
def _km(a, b):
    return km(a, b)


def travel(a, b, cfg=CFG):
    """Minutes by road. ponytail: haversine x a per-city min/km factor; swap in a cached OSRM matrix for real ETAs."""
    return _km(a, b) * cfg["min_per_km"]


def late(t):
    h = t % 1440
    return 1.0 if h >= 1320 or h < 300 else 0.0


def sigmoid(z):
    return 1 / (1 + math.exp(-z))


def logit(p):
    return math.log(p / (1 - p))


# ---------------------------------------------------------------- models

@dataclass
class Donation:
    id: str
    donor: str
    loc: tuple
    category: str
    holding: str
    veg: bool | None
    kg: float
    meals: float
    a: float                 # donor ready from
    b: float                 # donor ready until (closes)
    safe_until: float        # s_d, hard
    posted: float
    zone: str = "downtown"
    text: str = ""
    status: str = "posted"   # posted|offered|claimed|picked_up|delivered|expired|diverted
    recipient: str | None = None
    volunteer: str | None = None
    escalated: bool = False
    esc_t: float | None = None
    esc_L: float | None = None
    t_claim: float | None = None
    eta_pick: float | None = None
    t_pick: float | None = None
    t_drop: float | None = None
    code: str | None = None
    end_reason: str | None = None
    owner: str | None = None     # account that posted it (email), "sim" for the city simulation


@dataclass
class Recipient:
    id: str
    name: str
    loc: tuple
    alpha: float             # receiving window opens
    beta: float              # receiving window closes
    mu: float                # use lead: receipt -> service
    cap: dict                # free kg per storage class, after holds
    veg_only: bool = False
    accepts: list = field(default_factory=lambda: list(CATEGORIES))
    full: list = field(default_factory=list)
    confidential: bool = False
    need: float = 100.0      # meals over rolling 7 days
    received: float = 0.0
    zone: str = "downtown"


@dataclass
class Volunteer:
    id: str
    name: str
    loc: tuple
    cap_kg: float
    start: float = 0
    end: float = INF
    n7: int = 0              # offers in the last 7 days (fatigue + budget K) = len(sent)
    acc: int = 0             # accepted offers (history, for Beta shrinkage)
    off: int = 0             # resolved offers (history)
    busy: str | None = None
    zone: str = "downtown"
    home: tuple | None = None   # if set, the volunteer goes home after each delivery (data world)
    sent: list = field(default_factory=list)   # times of the offers counted in n7 (rolling window)


@dataclass
class Offer:
    id: str
    d: str
    v: str
    t: float
    expires: float
    p: float | None
    status: str = "sent"     # sent|accepted|declined|expired|withdrawn|cancelled


@dataclass
class State:
    donations: dict = field(default_factory=dict)
    recipients: dict = field(default_factory=dict)
    volunteers: dict = field(default_factory=dict)
    offers: dict = field(default_factory=dict)
    holds: dict = field(default_factory=dict)   # donation -> [recipient, storage class, kg]
    backup: tuple = (0.0, 0.0)


# ---------------------------------------------------------------- feasibility (§9.2)

def storage(d):
    return storage_class(d.category, d.holding)


def available(v, now):
    return v.busy is None and v.start <= now < v.end


def pick_time(d, loc, now, delay, cfg=CFG):
    return max(now + delay + cfg["prep"] + travel(loc, d.loc, cfg), d.a)


def use_by(d, r, cfg=CFG):
    """Latest drop time at r that still gets d *used* (not just delivered) before s_d - eps."""
    return min(r.beta, d.safe_until - cfg["eps"] - r.mu)


def latest_pickup(d, r, cfg=CFG):
    """Latest pickup time for which (d -> r) meets every constraint; -INF if none does."""
    u = use_by(d, r, cfg)
    if r.alpha > u:
        return -INF
    return min(d.b, u - cfg["sigma"] - travel(d.loc, r.loc, cfg))


def L_escalate(s, d, r, cfg=CFG):
    """L_d (§9.4): last moment handing to the backup still gets the food used safely. Rules + travel only."""
    return latest_pickup(d, r, cfg) - cfg["rho_B"] - cfg["prep"] - travel(s.backup, d.loc, cfg) - cfg["eps_esc"]


def why_not(d, r, free_kg, own_hold, t_pick=None, cfg=CFG):
    """None if r can take d, else a short human-readable reason (shown on the console)."""
    if d.category not in r.accepts:
        return f"doesn't accept {d.category}"
    if r.veg_only and d.veg is not True:
        return "veg-only kitchen"
    g = storage(d)
    if g in r.full:
        return f"marked full for {g}"
    if not own_hold and free_kg < d.kg:
        return f"only {max(free_kg, 0):.0f} kg {g} space"
    if t_pick is not None and t_pick > latest_pickup(d, r, cfg):
        if t_pick > d.b:
            return "donor closes before anyone can get there"
        drop = max(t_pick + cfg["sigma"] + travel(d.loc, r.loc, cfg), r.alpha)
        if drop > r.beta:
            return "closed before food could arrive"
        if r.mu:
            return f"next service is {r.mu / 60:.1f} h after arrival, past the safety deadline"
        return "can't get it there before the safety deadline"
    return None


ACCEPT_FEATURES = ["distance_km", "late_night", "log1p_offers_7d", "weight_ratio"]


def accept_x(distance_km, late_night, offers_7d, weight_ratio):
    """Acceptance-model features. ml.py trains on exactly this function, so training and serving can't drift."""
    return [distance_km, float(late_night), math.log1p(offers_7d), weight_ratio]


def p_accept(v, d, now, cfg=CFG):
    """P(v accepts d within the wave timeout).

    Learned model (cfg["p_model"]): logistic on accept_x + a per-volunteer intercept learned with L2 shrinkage;
    volunteers the model never saw fall back to their Beta-shrunk live history.
    Hand-set model otherwise (§6.3): logistic slopes + Beta-shrunk per-volunteer intercept."""
    if cfg["p_const"] is not None:
        return cfg["p_const"]
    a0, b0 = cfg["prior"]
    rate = (v.acc + a0) / (v.off + a0 + b0)
    m = cfg.get("p_model")
    if m:
        x = accept_x(km(v.loc, d.loc), late(now), v.n7, d.kg / v.cap_kg)
        theta = m["volunteer"].get(v.id)
        if theta is None:
            theta = logit(rate) - logit(a0 / (a0 + b0))
        return sigmoid(m["intercept"] + sum(w * xi for w, xi in zip(m["coef"], x)) + theta)
    (b1, b2, b3), (m1, m2, m3) = cfg["beta"], cfg["feat_mean"]
    z = logit(rate) + b1 * (km(v.loc, d.loc) - m1) + b2 * (late(now) - m2) + b3 * (math.log1p(v.n7) - m3)
    return sigmoid(z)


def load_p_model(path):
    """Load models/acceptance.json (written by ml.py). Returns None if it doesn't exist."""
    import json
    import os
    if not os.path.exists(path):
        return None
    with open(path) as f:
        m = json.load(f)
    assert m["features"] == ACCEPT_FEATURES, "acceptance model was trained on different features; retrain"
    return m


# ---------------------------------------------------------------- projector

def _release(s, did):
    h = s.holds.pop(did, None)
    if h:
        s.recipients[h[0]].cap[h[1]] += h[2]


def _withdraw(s, did):
    for o in s.offers.values():
        if o.d == did and o.status == "sent":
            o.status = "withdrawn"


def _free(s, d, loc):
    v = s.volunteers.get(d.volunteer)
    if v:
        v.busy = None
        if loc:
            v.loc = loc


def apply(s, e):
    """Fold one event into state. The only place state changes."""
    k, t = e["type"], e["t"]
    d = s.donations.get(e["d"]) if isinstance(e.get("d"), str) else None
    if k == "Genesis":
        pass
    elif k == "BackupSet":
        s.backup = tuple(e["loc"])
    elif k == "RecipientAdded":
        r = Recipient(**copy.deepcopy(e["r"]))
        r.loc = tuple(r.loc)
        s.recipients[r.id] = r
    elif k == "VolunteerAdded":
        v = Volunteer(**copy.deepcopy(e["v"]))
        v.loc = tuple(v.loc)
        v.home = tuple(v.home) if v.home else None
        v.sent = [t] * v.n7   # prior history counts from the moment the volunteer joins
        s.volunteers[v.id] = v
    elif k == "DonationPosted":
        d = Donation(**copy.deepcopy(e["d"]))
        d.loc = tuple(d.loc)
        s.donations[d.id] = d
    elif k == "RecipientChosen":
        _release(s, d.id)
        r, g = s.recipients[e["r"]], storage(d)
        r.cap[g] -= d.kg
        s.holds[d.id] = [r.id, g, d.kg]
        d.recipient = r.id
    elif k == "OfferSent":
        s.offers[e["id"]] = Offer(e["id"], e["d"], e["v"], t, e["expires"], e.get("p"))
        if not e.get("free"):   # free = the living city's stand-ins or simulated food: no one's weekly budget
            v = s.volunteers[e["v"]]
            v.sent.append(t)
            v.n7 = len(v.sent)
        if d.status == "posted":
            d.status = "offered"
    elif k in ("OfferExpired", "OfferDeclined"):
        o = s.offers[e["offer"]]
        o.status = "expired" if k == "OfferExpired" else "declined"
        s.volunteers[o.v].off += 1
    elif k in ("OfferAccepted", "BackupDispatched"):
        if k == "OfferAccepted":
            o = s.offers[e["offer"]]
            o.status = "accepted"
            v = s.volunteers[o.v]
            v.acc += 1
            v.off += 1
            v.busy = o.d
            d, who = s.donations[o.d], o.v
        else:
            who = "backup"
        _withdraw(s, d.id)
        d.status, d.volunteer, d.t_claim, d.eta_pick, d.code = "claimed", who, t, e["eta_pick"], e.get("code")
    elif k == "ClaimCancelled":
        for o in s.offers.values():
            if o.d == d.id and o.v == d.volunteer and o.status == "accepted":
                o.status = "cancelled"
        _free(s, d, None)
        d.status, d.volunteer, d.t_claim, d.eta_pick, d.code = "offered", None, None, None, None
    elif k == "EscalationRaised":
        d.escalated, d.esc_t, d.esc_L = True, t, e.get("L")
    elif k == "PickedUp":
        d.status, d.t_pick = "picked_up", t
    elif k == "Delivered":
        s.holds.pop(d.id, None)          # capacity is consumed, not returned
        r = s.recipients[d.recipient]
        r.received += d.meals
        d.status, d.t_drop = "delivered", t
        v = s.volunteers.get(d.volunteer)
        _free(s, d, v.home if v and v.home else r.loc)
    elif k in ("Expired", "Diverted"):
        _release(s, d.id)
        _withdraw(s, d.id)
        _free(s, d, d.loc if k == "Diverted" else None)
        d.status, d.end_reason = ("expired" if k == "Expired" else "diverted"), e.get("reason")
    elif k == "AvailabilitySet":     # a driver goes online / offline
        v = s.volunteers[e["v"]]
        v.start, v.end = (0, INF) if e["on"] else (0, 0)
    elif k == "OffersAged":            # offers older than the budget window stop counting
        v = s.volunteers[e["v"]]
        v.sent = [x for x in v.sent if x >= e["before"]]
        v.n7 = len(v.sent)
    elif k == "BudgetRecount":         # one-off correction of weekly counts (older logs counted every offer)
        for vid, times in e["sent"].items():
            v = s.volunteers[vid]
            v.sent = list(times)
            v.n7 = len(v.sent)
    elif k == "CapacityUpdated":
        r = s.recipients[e["r"]]
        r.cap.update(e.get("cap", {}))
        if "full" in e:
            r.full = list(e["full"])
    else:
        raise ValueError(f"unknown event {k}")


def replay(events):
    s = State()
    for e in events:
        apply(s, e)
    return s
