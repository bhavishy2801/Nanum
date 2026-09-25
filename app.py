"""Relay API: FastAPI + append-only event log (state is a projection of the log) + Google sign-in + email.

    python app.py            # http://127.0.0.1:8000

Roles: donor (restaurant / kiosk / food chain / caterer ...), volunteer (driver), shelter, admin (RELAY_ADMINS in .env).
Everyone who isn't a signed-in person is simulated ("living city"), so each role can be demoed on its own.
Storage: SQLite (default) or MongoDB (MONGODB_URI), see store.py. Notifications: in-app + SMTP email, see notify.py.
"""
import asyncio
import csv
import hashlib
import io
import json
import math
import os
import random
import secrets
import time
import urllib.parse
import urllib.request
from collections import Counter, deque
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import core
import intake
import notify
import planner
import sim
import store as storage_db
from core import INF, KG_PER_MEAL, L_escalate, available, latest_pickup, pick_time, storage, why_not

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")
if os.path.exists(os.path.join(HERE, ".env")):   # KEY=value lines; real environment variables win
    with open(os.path.join(HERE, ".env")) as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip())
env = os.environ.get
CARTO_KEY = env("RELAY_CARTO_KEY", "")            # CARTO basemap key, served to the page by /api/config
GOOGLE_CLIENT_ID = env("GOOGLE_CLIENT_ID", "")    # OAuth 2.0 Web client ID from Google Cloud Console
ADMINS = {e.strip().lower() for e in env("RELAY_ADMINS", "").split(",") if e.strip()}
DEMO_LOGIN = env("RELAY_DEMO_LOGIN", "1") == "1"  # one-click demo accounts; set 0 for a real deployment
CO2E_PER_KG = env("RELAY_CO2E_PER_KG")            # set with its source (ReFED [R9] or EPA [R10]); unset = not shown
SESSION_DAYS = 7

POLICY = env("RELAY_POLICY", "Relay-RL")           # any sim.POLICIES name; falls back if its models are missing
if not sim.ready(POLICY):
    POLICY = "Relay-ML" if sim.ready("Relay-ML") else "Relay"
PLAN, _over = sim.POLICIES[POLICY]
CFG = {**core.CFG, **sim.DATA_CFG, **_over}         # Bengaluru travel/handling measured from relay_data
if isinstance(CFG.get("p_model"), str):
    CFG["p_model"] = core.load_p_model(CFG["p_model"])
EXPLAIN_MODEL = sim.POLICIES["Relay-RL"][1]["wave_model"]   # the Q-function the admin explainer shows

STORE = storage_db.Store(storage_db.backend_from_env(env))   # SQLite file, or MongoDB when MONGODB_URI is set
MAILER = notify.Mailer(STORE, env)                             # SMTP_* in .env; without it emails are previews
PUBLIC_URL = (env("RELAY_PUBLIC_URL", "") or env("RENDER_EXTERNAL_URL", "")      # links inside emails (Render sets the latter)
              or f"http://localhost:{env('PORT', '8000')}")
CAT = {"cooked": "Cooked food", "dairy": "Dairy", "bakery": "Bakery", "produce": "Fruit & veg", "packaged": "Packaged food",
       "mixed": "Mixed food", "unknown": "Food"}
RECENT = deque(maxlen=120)   # latest events, for the admin activity stream
QUIET = ("OfferExpired", "OffersAged", "BudgetRecount")   # bookkeeping, not shown in the activity stream
S = core.State()
BASE = 0.0          # epoch seconds of day-0 midnight; live times are minutes since then
WORLD = None        # the relay_data world: real volunteers' behaviour for the living-city simulation
STATS = {"plan_ms": deque(maxlen=200), "last_plan": 0.0, "booted": time.time()}


def now():
    return (time.time() - BASE) / 60


def emit(e):
    e.setdefault("t", now())
    core.apply(S, e)     # raises on a bad event, so nothing invalid reaches the log
    STORE.event(e)
    if e["type"] not in QUIET:
        RECENT.append(e)
    try:
        NOTIFY.on_event(e)
    except Exception as ex:   # a notification problem must never block a rescue; shown on System
        STATS["notify_error"] = f"{e['type']}: {type(ex).__name__}: {ex}"


def boot():
    """Rebuild state by replaying the log; seed the Bengaluru city from relay_data on first run."""
    global BASE, WORLD
    WORLD = sim.DataWorld("bengaluru", 0)
    events, STORE.events = STORE.events, []   # replayed once; after that the log lives only in the database
    if events:
        BASE = events[0]["base"]
        for e in events:
            core.apply(S, e)
        RECENT.extend(e for e in events[-400:] if e["type"] not in QUIET)
        _recount_budgets(events)
        return
    lt = time.localtime()
    BASE = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    emit({"type": "Genesis", "base": BASE})
    for e in WORLD.setup:
        if e["type"] == "VolunteerAdded":
            e = {**e, "v": {**e["v"], "start": 0, "end": INF}}   # everyone on shift for the live demo
        emit(dict(e))


def _recount_budgets(events):
    """Weekly offer counts: only real food offered to a real person counts (older logs counted every offer, which
    locked the whole city out once the simulation had sent 10 offers to everyone). Emits one correction if needed."""
    real, t, since = user_entities(), now(), now() - CFG["budget_window"]
    joined = {e["v"]["id"]: (e["t"], e["v"].get("n7", 0)) for e in events if e["type"] == "VolunteerAdded"}
    want = {}
    for v in S.volunteers.values():
        t0, n = joined.get(v.id, (0.0, 0))
        times = [t0] * n + sorted(o.t for o in S.offers.values() if o.v == v.id and v.id in real
                                  and S.donations[o.d].owner not in (None, "sim"))
        times = [x for x in times if x >= since]
        if times != v.sent:
            want[v.id] = times
    if want:
        emit({"type": "BudgetRecount", "sent": want, "t": t})


def _refresh_shelters():
    """Simulated shelters serve what they receive, so their space frees up again: reset each one to its normal
    capacity minus food still held for it. Real shelters manage their own space."""
    seed = {e["r"]["id"]: e["r"]["cap"] for e in WORLD.setup if e["type"] == "RecipientAdded"}
    real, held = user_entities(), {}
    for rid, g, kg in S.holds.values():
        held[(rid, g)] = held.get((rid, g), 0) + kg
    for r in S.recipients.values():
        if r.id in seed and r.id not in real:
            cap = {g: max(0.0, c - held.get((r.id, g), 0)) for g, c in seed[r.id].items()}
            if any(abs(cap[g] - r.cap.get(g, 0)) > 0.5 for g in cap):
                emit({"type": "CapacityUpdated", "r": r.id, "cap": cap, "by": "sim"})


async def changed():
    """Re-plan on every change, on the 60 s tick and after simulated actions. Pages poll for updates."""
    t0 = time.perf_counter()
    real = user_entities()   # signed-in people come before the simulated city's stand-ins
    cfg = {**CFG, "real_v": real & S.volunteers.keys(), "real_r": real & S.recipients.keys(), "live": True}
    for e in PLAN(S, now(), cfg):
        emit(e)
    STATS["plan_ms"].append((time.perf_counter() - t0) * 1000)
    STATS["last_plan"] = time.time()


# ---------------------------------------------------------------- accounts

USER_KEYS = ("email", "name", "picture", "role", "entity", "org", "kind", "lat", "lon", "created")
PREFS = {"email": True, "offers": True}   # email me at each moment / also for every new driver offer


def _user(email):
    """A copy of the account. Change it only through _save_user, so memory and the database stay in step."""
    u = STORE.users.get(email)
    return {**dict.fromkeys(USER_KEYS), **u, "prefs": {**PREFS, **(u.get("prefs") or {})}} if u else None


def _save_user(email, **fields):
    u = {**(_user(email) or {"email": email, "created": time.time()}), **fields}
    STORE.put_user(u)
    return _user(email)


def _welcome(email):
    """Welcome note + email, once per account, as soon as it has a role."""
    u = _user(email)
    if u and u["role"] and not u.get("welcomed"):
        NOTIFY.welcome(_save_user(email, welcomed=time.time()))


def _upsert_user(email, name, picture):
    u = _save_user(email, name=name, picture=picture, last_seen=time.time(), **({"role": "admin"} if email in ADMINS else {}))
    _welcome(email)
    return u


def _hash(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


def _start_session(resp, request, email):
    tok = secrets.token_urlsafe(32)
    for h in [h for h, (_, x) in STORE.sessions.items() if x < time.time()]:
        STORE.del_session(h)
    STORE.put_session(_hash(tok), email, time.time() + SESSION_DAYS * 86400)
    resp.set_cookie("relay_session", tok, max_age=SESSION_DAYS * 86400, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")


def user_opt(request: Request):
    tok = request.cookies.get("relay_session")
    if not tok:
        return None
    r = STORE.sessions.get(_hash(tok))
    return _user(r[0]) if r and r[1] > time.time() else None


def need(*roles):
    async def dep(request: Request):   # async: DB access stays on the event-loop thread
        u = user_opt(request)
        if not u:
            raise HTTPException(401, "please sign in")
        if roles and u["role"] not in roles:
            raise HTTPException(403, "this page is for another kind of account")
        return u
    return dep


def owns(u, entity):
    if u["role"] != "admin" and u["entity"] != entity:
        raise HTTPException(403, "that isn't yours")


def user_entities():
    return {u["entity"] for u in STORE.users.values() if u.get("entity")}


def _nearest_zone(loc):
    return min(S.recipients.values(), key=lambda r: core.km(r.loc, loc)).zone


# ---------------------------------------------------------------- shared actions (used by people AND the simulation)

def _try_accept(o, t, by=None):
    d, v = S.donations[o.d], S.volunteers[o.v]
    r = S.recipients.get(d.recipient)
    eta = pick_time(d, v.loc, t, 0, CFG)
    if o.status == "sent" and d.status in planner.OPEN and available(v, t) and r and eta <= latest_pickup(d, r, CFG):
        emit({"type": "OfferAccepted", "offer": o.id, "eta_pick": eta, "code": f"{secrets.randbelow(10000):04d}",
              **({"by": by} if by else {})})
        return True
    return False


def _finish_delivery(d, t, by=None):
    """Delivered if it can still be served before its safety deadline, else diverted (never served)."""
    if t + S.recipients[d.recipient].mu > d.safe_until:
        emit({"type": "Diverted", "d": d.id, "reason": "arrived too late to be served before the safety deadline",
              **({"by": by} if by else {})})
        return False
    emit({"type": "Delivered", "d": d.id, **({"by": by} if by else {})})
    return True


# ---------------------------------------------------------------- the living city (everyone who isn't signed in)

SIM = {"running": env("RELAY_SIM", "1") == "1", "speed": 10.0, "every": 45.0, "next_post": 0.0, "next_refresh": 0.0}
SIM_COUNT = Counter()
_rng = random.Random()
_decided, _due, _scheduled = set(), {}, set()


def _sim_post(t):
    by_ep = sim.data_tables()[0]
    r = _rng.choice(by_ep[_rng.choice(list(by_ep))])
    cat, loc = r["category"], (float(r["pickup_lat"]), float(r["pickup_lon"]))
    window = float(r["ready_until_t"]) - float(r["post_t"])
    d = dict(id=f"d{len(S.donations) + 1:03d}", donor=f"{r['donor_type'].title()} {r['donation_id'][-4:]}", loc=loc,
             category=cat, holding=r["holding"], veg=True if cat in ("bakery", "produce") else None,
             kg=round(float(r["weight_kg"]), 1), meals=float(r["meals"]), a=t, b=t + window,
             safe_until=core.safe_until(cat, t), posted=t, zone=r["zone"], owner="sim")
    emit({"type": "DonationPosted", "d": d, "by": "sim"})
    SIM_COUNT["posted"] += 1


def _sim_step():
    """Simulated restaurants post food; simulated drivers answer offers (the recovered data behaviour), pick up and
    deliver. Signed-in people's drivers/shelters are never auto-driven. Travel and replies run SIM['speed']x faster."""
    wall, t, sp, acted = time.time(), now(), SIM["speed"], False
    real = user_entities()
    if wall >= SIM["next_refresh"]:   # simulated shelters serve their food: space frees up hourly
        SIM["next_refresh"] = wall + 3600
        _refresh_shelters()
    if wall >= SIM["next_post"]:
        if SIM["next_post"]:
            _sim_post(t)
            acted = True
        SIM["next_post"] = wall + SIM["every"] * _rng.uniform(.6, 1.4)
    for o in list(S.offers.values()):
        if o.status != "sent" or o.id in _decided or o.v in real or o.v not in WORLD.theta:
            continue
        _decided.add(o.id)
        if _rng.random() < WORLD.p_true(S.volunteers[o.v], S.donations[o.d], t, 0):
            delay = min(60.0, math.exp(sim.DELAY_MU + sim.DELAY_SIGMA * _rng.gauss(0, 1)))
            if t + delay < o.expires:
                _due[("accept", o.id)] = wall + delay * 60 / sp
    for d in S.donations.values():
        if not d.volunteer or d.volunteer in real:
            continue
        if d.status == "claimed" and ("pick", d.id, d.t_claim) not in _scheduled:
            _scheduled.add(("pick", d.id, d.t_claim))
            _due[("pick", d.id, d.volunteer)] = wall + max(.3, d.eta_pick - t) * 60 / sp
        elif d.status == "picked_up" and ("drop", d.id) not in _scheduled:
            _scheduled.add(("drop", d.id))
            r = S.recipients[d.recipient]
            _due[("drop", d.id)] = wall + (CFG["sigma"] + core.travel(d.loc, r.loc, CFG)) * 60 / sp
    for key, due in sorted(_due.items(), key=lambda kv: kv[1]):
        if due > wall:
            continue
        del _due[key]
        if key[0] == "accept":
            o = S.offers.get(key[1])
            if o and _try_accept(o, now(), by="sim"):
                SIM_COUNT["accepted"] += 1
                acted = True
        elif key[0] == "pick":
            d = S.donations[key[1]]
            if d.status == "claimed" and d.volunteer == key[2]:
                emit({"type": "PickedUp", "d": d.id, "by": "sim"})
                SIM_COUNT["picked_up"] += 1
                acted = True
        elif key[0] == "drop":
            d = S.donations[key[1]]
            if d.status == "picked_up":
                SIM_COUNT["delivered" if _finish_delivery(d, now(), by="sim") else "diverted"] += 1
                acted = True
    return acted


async def ticker():
    n = 0
    while True:
        await asyncio.sleep(1.5)
        n += 1
        try:
            acted = SIM["running"] and _sim_step()
            if acted or n % 40 == 0:   # re-plan after simulated actions, and at least once a minute
                await changed()
        except Exception as ex:        # the city must keep running; the error is shown on the admin System page
            STATS["sim_error"] = f"{type(ex).__name__}: {ex}"


@asynccontextmanager
async def lifespan(_):
    boot()
    print(f"Relay storage: {STORE.backend.name} ({STORE.backend.where}), email: {MAILER.status()['mode']}", flush=True)
    planner._model(EXPLAIN_MODEL)   # load scikit-learn + the RL model now, not on the first donor's post
    if CFG.get("wave_model"):
        planner._model(CFG["wave_model"])
    await changed()
    task = asyncio.create_task(ticker())
    yield
    task.cancel()
    STORE.flush()   # write everything still queued before the process exits


app = FastAPI(title="Relay", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- views

def donation_view(d, t):
    x = asdict(d)
    r = S.recipients.get(d.recipient)
    v = S.volunteers.get(d.volunteer)
    x.update(recipient_name=r.name if r else None, recipient_loc=r.loc if r else None,
             volunteer_name="Backup courier" if d.volunteer == "backup" else (v.name if v else None),
             volunteer_loc=v.loc if v else None)
    if d.status in planner.OPEN:
        t0, _ = planner.pickup_bounds(S, d, t, CFG)
        g = storage(d)
        x["why"] = {q.name: ("chosen" if q is r else why_not(d, q, q.cap.get(g, 0), False, t0, CFG) or "feasible, lower score")
                    for q in S.recipients.values()}
        if r:
            _, E, _, pmax = planner.claim_odds(S, d, r, t, CFG)
            x.update(slack=latest_pickup(d, r, CFG) - t, L=L_escalate(S, d, r, CFG), pclaim=pmax, eligible=len(E))
    return x


def view():
    t = now()
    return {"now": t, "base": BASE, "policy": POLICY,
            "donations": [donation_view(d, t) for d in S.donations.values()],
            "recipients": [asdict(r) for r in S.recipients.values()],
            "volunteers": [asdict(v) for v in S.volunteers.values()],
            "offers": [asdict(o) for o in S.offers.values() if o.status in ("sent", "accepted")],
            "backup": S.backup, "sim": {**SIM, **SIM_COUNT}}


def _get(table, key):
    x = table.get(key)
    if x is None:
        raise HTTPException(404, "not found")
    return x


def _area(loc):
    return [round(loc[0] / 0.005) * 0.005, round(loc[1] / 0.005) * 0.005]   # ~500 m cell until acceptance


def _clock(t):
    lt = time.localtime(BASE + t * 60)   # weekday prefix when it isn't today (bakery/produce last 1-2 days)
    return time.strftime("%H:%M" if lt.tm_yday == time.localtime().tm_yday else "%a %H:%M", lt)


NOTIFY = notify.Notifier(STORE, MAILER, S, lambda t: _clock(t), PUBLIC_URL, CAT, CFG)


def _me(u):
    out = {k: u.get(k) for k in ("email", "name", "picture", "role", "entity", "org", "kind", "lat", "lon", "prefs")}
    if u["role"] == "volunteer" and u["entity"] in S.volunteers:
        out["entity_name"] = S.volunteers[u["entity"]].name
    if u["role"] == "shelter" and u["entity"] in S.recipients:
        out["entity_name"] = S.recipients[u["entity"]].name
    return out


# ---------------------------------------------------------------- page, config, auth

@app.get("/")
async def index():
    v = str(int(max(os.path.getmtime(os.path.join(STATIC, f)) for f in os.listdir(STATIC))))
    with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__V__", v)   # cache-bust the css/js whenever they change
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/api/config")
async def config():
    """Browser-side settings. The basemap key must reach the browser: tile requests are made by the page."""
    return {"carto_key": CARTO_KEY, "google_client_id": GOOGLE_CLIENT_ID, "demo_login": DEMO_LOGIN}


class GoogleIn(BaseModel):
    credential: str = Field(min_length=20, max_length=4096)


def _verify_google(credential):
    """Verify a Google ID token with Google (signature, expiry) and check it was issued for THIS app."""
    url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(credential)
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            info = json.load(r)
    except Exception:
        raise HTTPException(401, "Google couldn't verify that sign-in. Please try again.")
    if info.get("aud") != GOOGLE_CLIENT_ID:
        raise HTTPException(401, "this Google sign-in was issued for a different app")
    if info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise HTTPException(401, "not a Google sign-in")
    if str(info.get("email_verified")).lower() != "true":
        raise HTTPException(401, "please verify your Google email first")
    if int(info.get("exp", 0)) < time.time():
        raise HTTPException(401, "sign-in expired, please try again")
    return info["email"].lower(), info.get("name") or info["email"], info.get("picture", "")


@app.post("/api/auth/google")
async def auth_google(x: GoogleIn, request: Request, response: Response):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(503, "Google sign-in isn't configured (GOOGLE_CLIENT_ID in .env)")
    email, name, picture = await run_in_threadpool(_verify_google, x.credential)
    u = _upsert_user(email, name, picture)
    _start_session(response, request, email)
    return _me(u)


DEMO = {"admin": ("demo-admin@relay.demo", "Asha · Admin"), "donor": ("demo-kitchen@relay.demo", "Hotel Saffron"),
        "volunteer": ("demo-driver@relay.demo", "Demo Driver"), "shelter": ("demo-shelter@relay.demo", "Demo Shelter")}


class DemoIn(BaseModel):
    role: Literal["admin", "donor", "volunteer", "shelter"]


@app.post("/api/auth/demo")
async def auth_demo(x: DemoIn, request: Request, response: Response):
    """One-click demo accounts (RELAY_DEMO_LOGIN=1). Pre-onboarded, linked to real city entities."""
    if not DEMO_LOGIN:
        raise HTTPException(403, "demo accounts are switched off")
    email, name = DEMO[x.role]
    u = _upsert_user(email, name, "")
    if not u["role"]:
        taken = user_entities()
        if x.role == "admin":
            _save_user(email, role="admin")
        elif x.role == "donor":
            _save_user(email, role="donor", org="Hotel Saffron · Koramangala", kind="restaurant", lat=12.9352, lon=77.6245)
        elif x.role == "volunteer":   # the most reliable driver near the centre, so offers come quickly
            pm, c = CFG.get("p_model") or {"volunteer": {}}, S.backup
            vs = [v for v in S.volunteers.values() if v.id not in taken and core.km(v.loc, c) < 5 and v.cap_kg >= 20]
            v = max(vs, key=lambda v: pm["volunteer"].get(v.id, 0))
            _save_user(email, role="volunteer", entity=v.id, org=v.name)
        else:                         # a shelter that can take cooked food tonight
            rs = [r for r in S.recipients.values() if r.id not in taken and not r.veg_only and r.mu <= 60]
            r = max(rs, key=lambda r: r.cap["hot"])
            _save_user(email, role="shelter", entity=r.id, org=r.name)
        _welcome(email)
    _start_session(response, request, email)
    return _me(_user(email))


@app.post("/api/auth/logout")
async def logout(request: Request, response: Response):
    tok = request.cookies.get("relay_session")
    if tok:
        STORE.del_session(_hash(tok))
    response.delete_cookie("relay_session", path="/")
    return {"ok": True}


@app.get("/api/me")
async def me(request: Request):
    """The signed-in person, or null (not an error) when nobody is signed in."""
    u = user_opt(request)
    return _me(u) if u else None


# ---------------------------------------------------------------- notifications, email settings, receipts

@app.get("/api/notes")
async def notes(u=Depends(need())):
    mine = STORE.notes.get(u["email"], [])
    return {"unread": sum(not n["read"] for n in mine), "notes": mine[::-1][:40]}


class ReadIn(BaseModel):
    ids: list[str] | None = None   # None = mark everything read


@app.post("/api/notes/read")
async def notes_read(x: ReadIn, u=Depends(need())):
    for n in list(STORE.notes.get(u["email"], [])):
        if not n["read"] and (x.ids is None or n["id"] in x.ids):
            STORE.put_note({**n, "read": True})
    return {"ok": True}


class PrefsIn(BaseModel):
    email: bool
    offers: bool = True


@app.put("/api/me/prefs")
async def set_prefs(x: PrefsIn, u=Depends(need())):
    return _me(_save_user(u["email"], prefs=x.model_dump()))


@app.post("/api/me/test-email")
async def my_test_email(u=Depends(need())):
    doc = NOTIFY.test(u)
    return {"status": doc["status"], "to": doc["to"]}


@app.get("/receipt/{did}", response_class=HTMLResponse)
async def receipt(did: str, request: Request):
    """A printable donation receipt (for the donor's records / CSR report). Owner or admin only."""
    u = user_opt(request)
    if not u:
        return RedirectResponse("/")
    d = _get(S.donations, did)
    if u["role"] != "admin" and d.owner != u["email"]:
        raise HTTPException(403, "that isn't yours")
    if d.status != "delivered":
        raise HTTPException(409, "a receipt is available once the food is delivered")
    r = S.recipients[d.recipient]
    rows = [("Receipt no.", notify.E(d.id.upper())), ("Donor", notify.E(d.donor)),
            ("Food", notify.E(f"{d.kg:g} kg {CAT.get(d.category, d.category).lower()}, kept {d.holding}")),
            ("Meals (approx.)", f"{d.meals:.0f}"), ("Posted", notify.E(_clock(d.posted))), ("Picked up", notify.E(_clock(d.t_pick or d.t_claim))),
            ("Delivered", notify.E(_clock(d.t_drop))), ("Delivered to", "A confidential shelter" if r.confidential else notify.E(r.name)),
            ("Safe to eat until", notify.E(_clock(d.safe_until))), ("Date", time.strftime("%d %b %Y", time.localtime(BASE + d.t_drop * 60)))]
    if CO2E_PER_KG:
        rows.append(("CO₂e avoided (est.)", f"{d.kg * float(CO2E_PER_KG):.1f} kg"))
    body = ("<p>This confirms that the surplus food below was rescued through Relay and delivered safely, within its "
            "food-safety window.</p>" + notify.facts(rows) +
            '<p style="font-size:12px;color:#64748b">Meals use the 0.544 kg-per-meal convention. Generated by Relay.</p>'
            '<p class="noprint"><a href="javascript:print()" style="color:#0f9d74;font-weight:700">Print or save as PDF</a> · '
            '<a href="/#mine" style="color:#0f9d74">Back to Relay</a></p>')
    page = notify.layout(f"Donation receipt · {d.id.upper()}", "Relay donation receipt", body)
    return page.replace("</head>", "<style>@media print{.noprint{display:none}body{background:#fff!important}}</style></head>")


# ---------------------------------------------------------------- admin: mailbox

@app.get("/api/admin/outbox")
async def outbox(u=Depends(need("admin")), kind: str | None = None):
    with STORE.lock:
        docs = sorted(STORE.outbox.values(), key=lambda d: -d["created"])
    docs = [{k: d.get(k) for k in ("id", "created", "to", "subject", "kind", "status", "attempts", "error", "sent_at")}
            for d in docs if not kind or d["kind"] == kind][:150]
    return {"email": MAILER.status(), "storage": STORE.status(), "messages": docs}


@app.get("/api/admin/outbox/{mid}")
async def outbox_one(mid: str, u=Depends(need("admin"))):
    with STORE.lock:
        return dict(_get(STORE.outbox, mid))


@app.post("/api/admin/outbox/{mid}/resend")
async def outbox_resend(mid: str, u=Depends(need("admin"))):
    _get(STORE.outbox, mid)
    return {"status": MAILER.resend(mid)["status"]}


class TestMailIn(BaseModel):
    to: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=200)


@app.post("/api/admin/email/test")
async def admin_test_email(x: TestMailIn, u=Depends(need("admin"))):
    doc = NOTIFY.test({**u, "email": x.to.strip()})
    return {"id": doc["id"], "status": doc["status"], "to": doc["to"]}


@app.get("/api/shelters/unclaimed")
async def unclaimed(u=Depends(need())):
    taken = user_entities()
    return [{"id": r.id, "name": r.name, "zone": r.zone, "loc": r.loc} for r in S.recipients.values() if r.id not in taken]


class OnboardIn(BaseModel):
    role: Literal["donor", "volunteer", "shelter"]
    org: str = Field(min_length=2, max_length=80)
    kind: str | None = Field(default=None, max_length=40)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    cap_kg: float | None = Field(default=None, ge=5, le=300)
    shelter_id: str | None = None
    hot: float = Field(default=40, ge=0, le=1000)
    cold: float = Field(default=20, ge=0, le=1000)
    ambient: float = Field(default=40, ge=0, le=1000)
    veg_only: bool = False
    serves_after: float = Field(default=0, ge=0, le=720)
    confidential: bool = False


@app.post("/api/onboard")
async def onboard(x: OnboardIn, u=Depends(need())):
    if u["role"]:
        raise HTTPException(409, "your account already has a role")
    loc = (x.lat, x.lon)
    if x.role == "donor":
        _save_user(u["email"], role="donor", org=x.org, kind=x.kind or "restaurant", lat=x.lat, lon=x.lon)
    elif x.role == "volunteer":
        vid = f"U{sum(1 for v in S.volunteers if v.startswith('U')) + 1:04d}"
        emit({"type": "VolunteerAdded", "v": dict(id=vid, name=x.org, loc=loc, home=loc, cap_kg=x.cap_kg or 20,
                                                  start=0, end=INF, zone=_nearest_zone(loc))})
        _save_user(u["email"], role="volunteer", entity=vid, org=x.org, kind=x.kind, lat=x.lat, lon=x.lon)
    else:
        if x.shelter_id:
            if x.shelter_id not in S.recipients or x.shelter_id in user_entities():
                raise HTTPException(409, "that site is already managed by someone else")
            rid = x.shelter_id
        else:
            rid = f"S{sum(1 for r in S.recipients if r.startswith('S')) + 1:03d}"
            emit({"type": "RecipientAdded", "r": dict(
                id=rid, name=x.org, loc=loc, alpha=0, beta=INF, mu=x.serves_after,
                cap={"hot": x.hot, "cold": x.cold, "ambient": x.ambient}, veg_only=x.veg_only,
                confidential=x.confidential, need=(x.hot + x.ambient) / KG_PER_MEAL, zone=_nearest_zone(loc))})
        _save_user(u["email"], role="shelter", entity=rid, org=S.recipients[rid].name, lat=x.lat, lon=x.lon)
    _welcome(u["email"])
    await changed()
    return _me(_user(u["email"]))


# ---------------------------------------------------------------- coordinator state (admin)

@app.get("/api/state")
async def state(u=Depends(need("admin"))):
    return view()


# ---------------------------------------------------------------- donor

class ParseIn(BaseModel):
    text: str = Field(max_length=2000)


@app.post("/api/intake/parse")
async def parse(x: ParseIn, u=Depends(need("donor", "admin"))):
    return await run_in_threadpool(intake.parse, x.text)


class DonationIn(BaseModel):
    donor: str | None = Field(default=None, max_length=80)
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    category: Literal["cooked", "dairy", "bakery", "produce", "packaged", "mixed", "unknown"]
    holding: Literal["hot", "cold", "ambient", "unknown"]
    veg: bool | None = None
    kg: float = Field(gt=0, le=200)          # bigger drops go to the coordinator for review (§11.6)
    plates: int | None = Field(default=None, ge=1, le=2000)
    ready_until: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    label_until: float | None = None          # packaged use-by, live minutes
    text: str = Field(default="", max_length=2000)


@app.post("/api/donations")
async def post_donation(x: DonationIn, u=Depends(need("donor", "admin"))):
    t = now()
    b = t + 120   # default donor window when none is given [ASM]
    if x.ready_until:
        h, m = map(int, x.ready_until.split(":"))
        b = math.floor(t / 1440) * 1440 + h * 60 + m
        if b <= t:
            b += 1440   # "01:00" posted at 23:00 means tomorrow
        if b - t > 12 * 60:
            raise HTTPException(400, "ready-until time looks like it's in the past")
    donor = u["org"] if u["role"] == "donor" else (x.donor or "Coordinator post")
    loc = (x.lat, x.lon)
    d = dict(id=f"d{len(S.donations) + 1:03d}", donor=donor, loc=loc, category=x.category, holding=x.holding,
             veg=x.veg, kg=x.kg, meals=float(x.plates) if x.plates else round(x.kg / KG_PER_MEAL, 1),
             a=t, b=b, safe_until=core.safe_until(x.category, t, x.label_until),   # clock starts at posting
             posted=t, zone=_nearest_zone(loc), text=x.text, owner=u["email"])
    emit({"type": "DonationPosted", "d": d})
    await changed()
    out = donation_view(S.donations[d["id"]], now())
    out.pop("code", None)
    return out


@app.get("/api/donor/home")
async def donor_home(u=Depends(need("donor"))):
    t = now()
    mine = [d for d in S.donations.values() if d.owner == u["email"]]
    views = []
    for d in sorted(mine, key=lambda d: -d.posted)[:40]:
        x = donation_view(d, t)
        x.pop("code", None)            # the handover code is between driver and shelter
        if x.get("volunteer_name") and d.volunteer != "backup":
            x["volunteer_name"] = x["volunteer_name"].split(" ")[0]   # first name only
        views.append(x)
    done = [d for d in mine if d.status == "delivered"]
    return {"profile": _me(u), "now": t, "base": BASE, "donations": views,
            "impact": {"kg": sum(d.kg for d in done), "meals": sum(d.meals for d in done), "deliveries": len(done),
                       "posted": len(mine), "lost": sum(1 for d in mine if d.status in ("expired", "diverted"))}}


# ---------------------------------------------------------------- volunteer (driver)

@app.get("/api/volunteers/{vid}")
async def volunteer(vid: str, u=Depends(need("volunteer", "admin"))):
    owns(u, vid)
    v, t = _get(S.volunteers, vid), now()
    offers, job = [], None
    for o in S.offers.values():
        if o.v == vid and o.status == "sent":
            d = S.donations[o.d]
            offers.append({"offer": o.id, "area": _area(d.loc), "km": round(core.km(v.loc, d.loc), 1),
                           "category": d.category, "kg": d.kg, "veg": d.veg, "meals": d.meals,
                           "use_by": _clock(d.safe_until), "respond_by": _clock(o.expires) if o.expires < INF else None,
                           "p": o.p})
    for d in S.donations.values():
        if d.volunteer == vid and d.status in ("claimed", "picked_up"):
            r = S.recipients[d.recipient]
            job = {"d": d.id, "status": d.status, "donor": d.donor, "pickup": d.loc, "category": d.category,
                   "kg": d.kg, "meals": d.meals, "pickup_by": _clock(latest_pickup(d, r, CFG)),
                   "recipient": "Confidential site: coordinator shares the proxy handover point" if r.confidential else r.name,
                   "dropoff": None if r.confidential else r.loc}
    mine = sorted((d for d in S.donations.values() if d.volunteer == vid and d.status == "delivered"), key=lambda d: -d.t_drop)
    return {"volunteer": asdict(v), "available": available(v, t), "online": v.end > t, "offers": offers, "job": job,
            "stats": {"deliveries": len(mine), "kg": sum(d.kg for d in mine), "meals": sum(d.meals for d in mine)},
            "history": [{"d": d.id, "donor": d.donor, "kg": d.kg, "meals": d.meals, "at": _clock(d.t_drop),
                         "shelter": S.recipients[d.recipient].name} for d in mine[:20]]}


class AvailIn(BaseModel):
    on: bool
    v: str | None = None


@app.post("/api/volunteer/availability")
async def availability(x: AvailIn, u=Depends(need("volunteer", "admin"))):
    vid = x.v if u["role"] == "admin" and x.v else u["entity"]
    owns(u, vid)
    _get(S.volunteers, vid)
    emit({"type": "AvailabilitySet", "v": vid, "on": x.on})
    await changed()
    return {"ok": True}


class RespondIn(BaseModel):
    accept: bool


@app.post("/api/offers/{oid}/respond")
async def respond(oid: str, x: RespondIn, u=Depends(need("volunteer", "admin"))):
    o, t = _get(S.offers, oid), now()
    owns(u, o.v)
    if o.status != "sent":
        raise HTTPException(409, "this offer is no longer open")
    ok = x.accept and _try_accept(o, t)
    if not ok:
        emit({"type": "OfferDeclined", "offer": oid})
    await changed()
    if x.accept and not ok:
        raise HTTPException(409, "thanks, but this rescue is no longer possible for you; it's been re-offered")
    return {"ok": True}


def _my_job(did, u):
    d = _get(S.donations, did)
    owns(u, d.volunteer)
    if d.status not in ("claimed", "picked_up"):
        raise HTTPException(409, f"donation is {d.status}")
    return d


@app.post("/api/donations/{did}/cancel")
async def cancel(did: str, u=Depends(need("volunteer", "admin"))):
    d = _my_job(did, u)
    if d.status != "claimed" or d.volunteer == "backup":
        raise HTTPException(409, "only a volunteer claim that isn't picked up yet can be cancelled")
    emit({"type": "ClaimCancelled", "d": did})
    await changed()
    return {"ok": True}


class PickupIn(BaseModel):
    temp_ok: bool
    packaging_ok: bool


@app.post("/api/donations/{did}/pickup")
async def pickup(did: str, x: PickupIn, u=Depends(need("volunteer", "admin"))):
    d = _my_job(did, u)
    if d.status != "claimed":
        raise HTTPException(409, "already picked up")
    if x.temp_ok and x.packaging_ok:
        emit({"type": "PickedUp", "d": did})
    else:
        emit({"type": "Diverted", "d": did, "reason": "failed condition check at pickup (not for human consumption)"})
    await changed()
    return {"ok": True, "status": d.status}


class DeliverIn(BaseModel):
    code: str = Field(pattern=r"^\d{4}$")


@app.post("/api/donations/{did}/deliver")
async def deliver(did: str, x: DeliverIn, u=Depends(need("volunteer", "admin"))):
    d, t = _my_job(did, u), now()
    if d.status != "picked_up":
        raise HTTPException(409, "pick the food up first")
    if not secrets.compare_digest(x.code, d.code or ""):
        raise HTTPException(400, "wrong handover code")
    if t - d.t_claim > 120:
        raise HTTPException(400, "handover code expired (2 h); ask the coordinator to confirm")
    ok = _finish_delivery(d, t)
    await changed()
    if not ok:
        raise HTTPException(409, "too late to be served safely: divert (not for human consumption)")
    return {"ok": True, "receipt": f"Delivered to {S.recipients[d.recipient].name} at {_clock(t)} · {round(d.kg, 1):g} kg · ~{d.meals:.0f} meals"}


# ---------------------------------------------------------------- shelter

@app.get("/api/recipients/{rid}")
async def recipient(rid: str, u=Depends(need("shelter", "admin"))):
    owns(u, rid)
    r = _get(S.recipients, rid)
    arriving = [{"d": d.id, "donor": d.donor, "kg": d.kg, "category": d.category, "status": d.status, "code": d.code,
                 "volunteer": "Backup courier" if d.volunteer == "backup" else S.volunteers[d.volunteer].name,
                 "eta_pickup": _clock(d.eta_pick), "meals": d.meals}
                for d in S.donations.values() if d.recipient == rid and d.status in ("claimed", "picked_up")]
    held = [{"d": d.id, "kg": d.kg, "category": d.category}
            for d in S.donations.values() if d.recipient == rid and d.status in planner.OPEN]
    got = sorted((d for d in S.donations.values() if d.recipient == rid and d.status == "delivered"), key=lambda d: -d.t_drop)
    received = [{"d": d.id, "donor": d.donor, "kg": d.kg, "meals": d.meals, "category": d.category, "at": _clock(d.t_drop)}
                for d in got[:20]]
    return {"recipient": asdict(r), "arriving": arriving, "held": held, "received": received,
            "stats": {"deliveries": len(got), "kg": sum(d.kg for d in got), "meals": sum(d.meals for d in got)}}


class CapacityIn(BaseModel):
    cap: dict[Literal["hot", "cold", "ambient"], float] | None = None
    full: list[Literal["hot", "cold", "ambient"]] | None = None


@app.patch("/api/recipients/{rid}/capacity")
async def capacity(rid: str, x: CapacityIn, u=Depends(need("shelter", "admin"))):
    owns(u, rid)
    _get(S.recipients, rid)
    if x.cap and any(not 0 <= v <= 1000 for v in x.cap.values()):
        raise HTTPException(400, "capacity must be 0-1000 kg")
    e = {"type": "CapacityUpdated", "r": rid}
    if x.cap is not None:
        e["cap"] = x.cap
    if x.full is not None:
        e["full"] = x.full
    emit(e)
    await changed()
    return {"ok": True}


# ---------------------------------------------------------------- coordinator actions (admin)

@app.post("/api/donations/{did}/backup")
async def backup(did: str, u=Depends(need("admin"))):
    d, t = _get(S.donations, did), now()
    r = S.recipients.get(d.recipient)
    if d.status not in planner.OPEN or not r:
        raise HTTPException(409, "nothing to dispatch: already claimed, closed, or no recipient")
    eta = pick_time(d, S.backup, t, CFG["rho_B"], CFG)
    if eta > latest_pickup(d, r, CFG):
        raise HTTPException(409, "the backup can't reach it in time; divert or let it expire")
    emit({"type": "BackupDispatched", "d": did, "eta_pick": eta, "code": f"{secrets.randbelow(10000):04d}"})
    await changed()
    return {"ok": True}


def _impact():
    done = [d for d in S.donations.values() if d.status == "delivered"]
    kg = sum(d.kg for d in done)
    by_r = {}
    for d in done:
        x = by_r.setdefault(S.recipients[d.recipient].name, {"kg": 0.0, "meals": 0.0, "deliveries": 0})
        x["kg"] += d.kg
        x["meals"] += d.meals
        x["deliveries"] += 1
    zones = {}
    for d in S.donations.values():
        z = zones.setdefault(d.zone, {"posted_kg": 0.0, "delivered_kg": 0.0})
        z["posted_kg"] += d.kg
        z["delivered_kg"] += d.kg if d.status == "delivered" else 0
    return {"kg": kg, "meals": sum(d.meals for d in done), "deliveries": len(done),
            "expired_kg": sum(d.kg for d in S.donations.values() if d.status == "expired"),
            "co2e_kg": kg * float(CO2E_PER_KG) if CO2E_PER_KG else None,
            "by_recipient": by_r, "by_zone": zones,
            "method": ("Counted only on Delivered (handover code confirmed). Meals = plate count if given, else kg / 0.544 "
                       "(1.2 lb/meal, Feeding America / ReFED [R9, R32]). CO2e shown only when RELAY_CO2E_PER_KG is set "
                       "with a cited factor.")}


@app.get("/api/impact")
async def impact(u=Depends(need("admin"))):
    return _impact()


@app.get("/api/impact.csv")
async def impact_csv(u=Depends(need("admin"))):
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["id", "donor", "category", "kg", "meals", "recipient", "posted", "delivered", "status"])
    for d in S.donations.values():
        w.writerow([d.id, d.donor, d.category, d.kg, d.meals, S.recipients[d.recipient].name if d.recipient else "",
                    _clock(d.posted), _clock(d.t_drop) if d.t_drop else "", d.status])
    return PlainTextResponse(f.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=relay_impact.csv"})


# ---------------------------------------------------------------- admin: system health, users, living city

def _describe(e):
    """One human-readable line per event for the admin activity stream."""
    k, by = e["type"], " (sim)" if e.get("by") == "sim" else ""
    raw_d, raw_v, raw_r = e.get("d"), e.get("v"), e.get("r")
    d = S.donations.get(raw_d) if isinstance(raw_d, str) else None
    who = S.volunteers[raw_v].name if isinstance(raw_v, str) and raw_v in S.volunteers else ""
    if isinstance(e.get("offer"), str) and e["offer"] in S.offers:
        o = S.offers[e["offer"]]
        d, who = S.donations.get(o.d), S.volunteers[o.v].name if o.v in S.volunteers else o.v
    name = d.donor if d else ""
    r = S.recipients.get(raw_r) if isinstance(raw_r, str) else None
    if k == "DonationPosted":
        text = f"{raw_d['donor']} posted {raw_d['kg']:g} kg{by}"
    elif k == "RecipientChosen":
        text = f"{name} → {r.name if r else ''}"
    elif k == "OfferSent":
        text = f"Asked {who} about {name}"
    elif k == "OfferAccepted":
        text = f"{who} accepted {name}{by}"
    elif k == "OfferDeclined":
        text = f"{who} declined {name}"
    elif k == "EscalationRaised":
        text = f"{name} needs a human: {e.get('reason', '')}"
    elif k == "BackupDispatched":
        text = f"Backup courier sent for {name}"
    elif k == "ClaimCancelled":
        text = f"Driver cancelled {name}"
    elif k == "PickedUp":
        text = f"{name} picked up{by}"
    elif k == "Delivered":
        text = f"{name} delivered{by}"
    elif k in ("Expired", "Diverted"):
        text = f"{name} {'lost' if k == 'Expired' else 'diverted'}: {e.get('reason', '')}"
    elif k == "CapacityUpdated":
        text = f"{r.name if r else 'Shelter'} updated its space"
    elif k == "AvailabilitySet":
        text = f"{who or 'Driver'} went {'online' if e.get('on') else 'offline'}"
    elif k == "VolunteerAdded" and isinstance(raw_v, dict):
        text = f"New driver joined: {raw_v['name']}"
    elif k == "RecipientAdded" and isinstance(raw_r, dict):
        text = f"New shelter joined: {raw_r['name']}"
    else:
        text = k
    return {"t": _clock(e["t"]), "type": k, "text": text, "sim": e.get("by") == "sim"}


@app.get("/api/admin/overview")
async def overview(u=Depends(need("admin"))):
    t = now()
    delivered = [d for d in S.donations.values() if d.status == "delivered"]
    unsafe = [d.id for d in delivered if d.t_drop + S.recipients[d.recipient].mu > d.safe_until + 1e-6]
    lat = sorted(STATS["plan_ms"])
    p95 = lat[int(.95 * (len(lat) - 1))] if lat else 0
    st, ms = STORE.status(), MAILER.status()
    health = [
        {"name": f"Database ({st['backend']})", "ok": not st["error"], "warn": st["pending"] > 50,
         "detail": st["error"] or f"{st['events']:,} events · {st['pending']} waiting to be written"},
        {"name": "Email", "ok": not ms["last_error"], "warn": ms["mode"] == "preview",
         "detail": ms["last_error"] or (f"{ms['mode'].upper()} {ms['host']}:{ms['port']} · {ms['counts'].get('sent', 0)} sent" if ms["mode"] != "preview"
                                        else "preview only: set BREVO_API_KEY or SMTP_HOST to send real email")},
        {"name": "Notifications", "ok": "notify_error" not in STATS, "detail": STATS.get("notify_error", "working")},
        {"name": "Dispatch policy", "ok": POLICY == env("RELAY_POLICY", "Relay-RL"), "detail": POLICY},
        {"name": "Acceptance model", "ok": bool(CFG.get("p_model")), "detail": "models/acceptance.json" if CFG.get("p_model") else "missing: run python ml.py all"},
        {"name": "Planner speed", "ok": p95 < 500, "detail": f"p95 {p95:.0f} ms · last run {time.time() - STATS['last_plan']:.0f} s ago"},
        {"name": "Food safety", "ok": not unsafe, "detail": f"{len(delivered)} deliveries, {len(unsafe)} past their safety deadline"},
        {"name": "Living city simulation", "ok": SIM["running"] and "sim_error" not in STATS,
         "detail": STATS.get("sim_error") or ("running" if SIM["running"] else "paused")},
        {"name": "Google sign-in", "ok": bool(GOOGLE_CLIENT_ID), "detail": "configured" if GOOGLE_CLIENT_ID else "set GOOGLE_CLIENT_ID in .env"},
        {"name": "Map tiles (CARTO)", "ok": bool(CARTO_KEY), "detail": "key set" if CARTO_KEY else "set RELAY_CARTO_KEY in .env"},
        {"name": "Demo accounts", "ok": not DEMO_LOGIN, "warn": DEMO_LOGIN, "detail": "ON: turn off (RELAY_DEMO_LOGIN=0) before a real launch" if DEMO_LOGIN else "off"},
    ]
    users = sorted(({k: x.get(k) for k in ("email", "name", "picture", "role", "entity", "org", "created")} for x in STORE.users.values()),
                   key=lambda x: -(x["created"] or 0))
    events = [_describe(e) for e in list(RECENT)[::-1][:80]]
    counts = Counter(d.status for d in S.donations.values())
    return {"health": health, "users": users, "events": events, "sim": {**SIM, **SIM_COUNT, "due": len(_due)},
            "counts": counts, "uptime_min": (time.time() - STATS["booted"]) / 60, "now": _clock(t)}


class SimIn(BaseModel):
    running: bool | None = None
    speed: float | None = Field(default=None, ge=1, le=60)
    every: float | None = Field(default=None, ge=10, le=600)
    post_now: bool = False


@app.post("/api/admin/sim")
async def sim_control(x: SimIn, u=Depends(need("admin"))):
    for k in ("running", "speed", "every"):
        if getattr(x, k) is not None:
            SIM[k] = getattr(x, k)
    if x.post_now:
        _sim_post(now())
        await changed()
    STATS.pop("sim_error", None)
    return {**SIM, **SIM_COUNT}


@app.post("/api/admin/users/{email}/reset")
async def reset_user(email: str, u=Depends(need("admin"))):
    """Clear a person's role so they choose again on next sign-in (their driver/shelter stays in the city)."""
    if email.lower() in ADMINS or email == u["email"]:
        raise HTTPException(409, "admins are set in .env (RELAY_ADMINS)")
    if email.lower() not in STORE.users:
        raise HTTPException(404, "no such person")
    _save_user(email.lower(), role=None, entity=None, welcomed=None)
    return {"ok": True}


# ---------------------------------------------------------------- admin: "How Relay thinks" (explainer)

def _example_donation(t):
    """A realistic donation from relay_data, used only for explanation (never added to the city)."""
    by_ep = sim.data_tables()[0]
    rs = [r for ep in sim.TEST_EPISODES for r in by_ep[ep] if r["category"] == "cooked"]
    r = random.choice(rs)
    return core.Donation("example", f"{r['donor_type'].title()} (example)", (float(r["pickup_lat"]), float(r["pickup_lon"])),
                         "cooked", r["holding"], True, round(float(r["weight_kg"]), 1), float(r["meals"]), t,
                         t + float(r["ready_until_t"]) - float(r["post_t"]), core.safe_until("cooked", t), t, zone=r["zone"])


@app.get("/api/admin/rl/explain")
async def rl_explain(u=Depends(need("admin")), live: bool = True):
    """One real decision, step by step: safety rules -> shelter, acceptance model -> driver ranking, RL -> how many."""
    t = now()
    d = next((d for d in sorted(S.donations.values(), key=lambda d: d.safe_until) if d.status in planner.OPEN), None) if live else None
    real = d is not None
    d = d or _example_donation(t)
    g = storage(d)
    t0, t3 = planner.pickup_bounds(S, d, t, CFG)
    shelters = []
    for r in S.recipients.values():
        why = why_not(d, r, r.cap.get(g, 0), r.id == d.recipient, t0, CFG)
        shelters.append({"id": r.id, "name": r.name, "loc": r.loc, "ok": why is None, "why": why or "",
                         "score": planner._score(d, r, t3, CFG) if why is None else None,
                         "km": round(core.km(d.loc, r.loc), 1), "fill": min(1.0, r.received / r.need) if r.need else 1.0,
                         "mu": r.mu, "free": round(r.cap.get(g, 0))})
    ok = [s for s in shelters if s["ok"]]
    chosen = d.recipient if d.recipient in S.recipients else (max(ok, key=lambda s: s["score"])["id"] if ok else None)
    out = {"real": real, "now": _clock(t), "donation": {"id": d.id, "donor": d.donor, "loc": d.loc, "kg": d.kg, "meals": d.meals,
           "category": d.category, "safe_until": _clock(d.safe_until), "safe_min": d.safe_until - t, "window_min": d.b - t},
           "shelters": shelters, "chosen": chosen, "volunteers": [], "rl": None, "policy": POLICY,
           "lam": 0.02, "gamma": 0.97}
    if not chosen:
        return out
    r = S.recipients[chosen]
    E = planner.eligible(S, d, r, t, CFG)
    p = {v.id: core.p_accept(v, d, t, CFG) for v in E}
    elig = sorted(E, key=lambda v: -p[v.id])
    x, cands = planner.rl_inputs(S, d, t, CFG)
    q = planner.q_values(EXPLAIN_MODEL, x, cands)
    best = max(range(len(q)), key=q.__getitem__)
    k = cands[best][0]
    wave = elig[:k]
    miss = math.prod(1 - p[v.id] for v in wave)
    vols = [{"id": v.id, "name": v.name, "loc": v.loc, "p": p[v.id], "km": round(core.km(v.loc, d.loc), 1),
             "eligible": True, "asked": v in wave} for v in elig[:14]]
    others = []
    for v in sorted(S.volunteers.values(), key=lambda v: core.km(v.loc, d.loc)):
        if v in E or len(others) >= 6:
            continue
        reason = ("on another rescue" if v.busy else "offline" if not available(v, t) else
                  "car too small" if v.cap_kg < d.kg else "weekly limit reached" if v.n7 >= CFG["K"] else
                  "already asked" if f"{d.id}:{v.id}" in S.offers else "can't get there in time")
        others.append({"id": v.id, "name": v.name, "loc": v.loc, "p": None, "km": round(core.km(v.loc, d.loc), 1),
                       "eligible": False, "asked": False, "why": reason})
    out.update(volunteers=vols + others, L=_clock(L_escalate(S, d, r, CFG)),
               rl={"features": dict(zip(planner.RL_FEATURES, x)), "k": [c[0] for c in cands],
                   "mass": [c[1] for c in cands], "q": q, "best": k, "pclaim": 1 - miss, "eligible": len(E)})
    return out


@app.get("/api/admin/rl/trace")
async def rl_trace(u=Depends(need("admin")), lam: Literal["0.02", "0.25"] = "0.02"):
    with open(os.path.join(sim.MODELS, f"rl_trace_lam{lam}.json")) as f:
        return json.load(f)


class QIn(BaseModel):
    slack: float = Field(ge=0, le=600)
    pool: int = Field(ge=0, le=60)
    top_p: float = Field(ge=0.01, le=0.95)
    meals: float = Field(ge=1, le=200)
    asked: int = Field(ge=0, le=30)
    late: bool = False
    lam: Literal["0.02", "0.25"] = "0.02"


@app.post("/api/admin/rl/q")
async def rl_q(x: QIn, u=Depends(need("admin"))):
    """Playground: the real Q-function on a hand-built situation."""
    pool = [x.top_p * 0.88 ** i for i in range(x.pool)]
    st = planner.rl_state(min(x.asked * 8.0, 120.0), x.slack, x.meals * KG_PER_MEAL, x.meals, "cooked", float(x.late),
                          x.asked, x.asked * x.top_p * 0.5, pool)
    cands = planner.rl_candidates(pool)
    q = planner.q_values(sim.POLICIES["Relay-RL" if x.lam == "0.02" else "Relay-RL lam=0.25"][1]["wave_model"], st, cands)
    best = max(range(len(q)), key=q.__getitem__)
    return {"k": [c[0] for c in cands], "mass": [c[1] for c in cands], "q": q, "best": cands[best][0]}


# ---------------------------------------------------------------- simulation lab (admin)

def _scenario(name):
    names = list(sim.DATA_SCENARIOS) + list(sim.SCENARIOS)
    if name not in names:
        raise HTTPException(400, f"scenario must be one of {names}")
    return name


@app.get("/api/sim/demo")
async def sim_demo(scenario: str = "bengaluru", seed: int = Query(0, ge=0, le=999), u=Depends(need("admin"))):
    return await run_in_threadpool(sim.demo, _scenario(scenario), seed)


@app.get("/api/sim/compare")
async def sim_compare(scenario: str = "bengaluru", seeds: int = Query(30, ge=2, le=50), u=Depends(need("admin"))):
    path = os.path.join(HERE, f"results_{_scenario(scenario)}.json")
    if os.path.exists(path):
        with open(path) as f:
            res = json.load(f)
        if res["seeds"] == seeds:
            return res
    res = await run_in_threadpool(sim.compare, scenario, seeds, None, 1)
    with open(path, "w") as f:
        json.dump(res, f)
    return res


if __name__ == "__main__":
    import uvicorn
    # HOST=0.0.0.0 in a container / cloud host. Behind a proxy, set FORWARDED_ALLOW_IPS=* so https is detected (secure cookies).
    uvicorn.run(app, host=env("HOST", "127.0.0.1"), port=int(env("PORT", "8000")))
