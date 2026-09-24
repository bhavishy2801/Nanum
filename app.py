"""Relay API: FastAPI + append-only SQLite event log. State is a projection of the log.

    python app.py            # http://127.0.0.1:8000

ponytail: no auth (magic links / OTP per role, §8.4) and no chat channel. Add them before a real pilot.
"""
import asyncio
import csv
import io
import json
import math
import os
import secrets
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

import core
import intake
import planner
import sim
from core import INF, KG_PER_MEAL, L_escalate, available, latest_pickup, pick_time, storage, why_not

HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.exists(os.path.join(HERE, ".env")):   # KEY=value lines; real environment variables win
    with open(os.path.join(HERE, ".env")) as f:
        for line in f:
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                os.environ.setdefault(k.strip(), v.strip())
CARTO_KEY = os.environ.get("RELAY_CARTO_KEY", "")   # CARTO basemap key, served to the page by /api/config
DB = os.environ.get("RELAY_DB", os.path.join(HERE, "relay.db"))
CO2E_PER_KG = os.environ.get("RELAY_CO2E_PER_KG")   # set with its source (ReFED [R9] or EPA [R10]); unset = not shown
# Live dispatch policy: any sim.POLICIES name. Defaults to offline-RL wave sizing; falls back if models are missing.
POLICY = os.environ.get("RELAY_POLICY", "Relay-RL")
if not sim.ready(POLICY):
    POLICY = "Relay-ML" if sim.ready("Relay-ML") else "Relay"
PLAN, _over = sim.POLICIES[POLICY]
CFG = {**core.CFG, **sim.DATA_CFG, **_over}   # Bengaluru travel/handling measured from relay_data
if isinstance(CFG.get("p_model"), str):
    CFG["p_model"] = core.load_p_model(CFG["p_model"])

db = sqlite3.connect(DB, check_same_thread=False)
db.execute("pragma journal_mode=wal")
db.execute("create table if not exists events (seq integer primary key autoincrement, t real, type text, payload text)")
S = core.State()
BASE = 0.0          # epoch seconds of day-0 midnight; live times are minutes since then


def now():
    return (time.time() - BASE) / 60


def emit(e):
    e.setdefault("t", now())
    core.apply(S, e)     # raises on a bad event, so nothing invalid reaches the log
    db.execute("insert into events (t, type, payload) values (?, ?, ?)", (e["t"], e["type"], json.dumps(e)))


def boot():
    """Rebuild state by replaying the log; seed a demo city on first run."""
    global BASE
    rows = db.execute("select payload from events order by seq").fetchall()
    if rows:
        BASE = json.loads(rows[0][0])["base"]
        for (p,) in rows:
            core.apply(S, json.loads(p))
        return
    lt = time.localtime()
    BASE = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    emit({"type": "Genesis", "base": BASE})
    for e in sim.DataWorld("bengaluru", 0).setup:   # the real relay_data recipients and volunteers
        if e["type"] == "VolunteerAdded":
            e = {**e, "v": {**e["v"], "start": 0, "end": INF}}   # live demo: everyone on shift
        emit(dict(e))
    db.commit()


async def changed():
    """Re-plan on every change and on the 60 s tick. Consoles poll /api/state.
    ponytail: polling every 3 s; switch to a WebSocket push (pip install websockets) if consoles multiply."""
    for e in PLAN(S, now(), CFG):
        emit(e)
    db.commit()


async def ticker():
    while True:
        await asyncio.sleep(60)
        await changed()


@asynccontextmanager
async def lifespan(_):
    boot()
    if CFG.get("wave_model"):
        planner._model(CFG["wave_model"])   # load scikit-learn + the RL model now, not on the first donor's post
    await changed()
    task = asyncio.create_task(ticker())
    yield
    task.cancel()


app = FastAPI(title="Relay", lifespan=lifespan)


# ---------------------------------------------------------------- views

def donation_view(d, t):
    x = asdict(d)
    r = S.recipients.get(d.recipient)
    x["recipient_name"] = r.name if r else None
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
    return {"now": t, "base": BASE,
            "donations": [donation_view(d, t) for d in S.donations.values()],
            "recipients": [asdict(r) for r in S.recipients.values()],
            "volunteers": [asdict(v) for v in S.volunteers.values()],
            "offers": [asdict(o) for o in S.offers.values() if o.status in ("sent", "accepted")],
            "backup": S.backup}


def _get(table, key):
    x = table.get(key)
    if x is None:
        raise HTTPException(404, "not found")
    return x


def _area(loc):
    return [round(loc[0] / 0.005) * 0.005, round(loc[1] / 0.005) * 0.005]   # ~500 m cell until acceptance


def _clock(t):
    return time.strftime("%H:%M", time.localtime(BASE + t * 60))


# ---------------------------------------------------------------- pages + state

@app.get("/")
async def index():
    return FileResponse(os.path.join(HERE, "index.html"), headers={"Cache-Control": "no-cache"})   # always the latest UI


@app.get("/api/config")
async def config():
    """Browser-side settings. The basemap key must reach the browser: tile requests are made by the page."""
    return {"carto_key": CARTO_KEY}


@app.get("/api/state")
async def state():
    return {**view(), "policy": POLICY}


# ---------------------------------------------------------------- donor

class ParseIn(BaseModel):
    text: str = Field(max_length=2000)


@app.post("/api/intake/parse")
async def parse(x: ParseIn):
    return await run_in_threadpool(intake.parse, x.text)


class DonationIn(BaseModel):
    donor: str = Field(min_length=1, max_length=80)
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
async def post_donation(x: DonationIn):
    t = now()
    b = t + 120   # default donor window when none is given [ASM]
    if x.ready_until:
        h, m = map(int, x.ready_until.split(":"))
        b = math.floor(t / 1440) * 1440 + h * 60 + m
        if b <= t:
            b += 1440   # "01:00" posted at 23:00 means tomorrow
        if b - t > 12 * 60:
            raise HTTPException(400, "ready-until time looks like it's in the past")
    loc = (x.lat, x.lon)
    d = dict(id=f"d{len(S.donations) + 1:03d}", donor=x.donor, loc=loc, category=x.category, holding=x.holding,
             veg=x.veg, kg=x.kg, meals=float(x.plates) if x.plates else round(x.kg / KG_PER_MEAL, 1),
             a=t, b=b, safe_until=core.safe_until(x.category, t, x.label_until),   # clock starts at posting
             posted=t, zone=min(S.recipients.values(), key=lambda r: core.km(r.loc, loc)).zone, text=x.text)
    emit({"type": "DonationPosted", "d": d})
    await changed()
    return donation_view(S.donations[d["id"]], now())


# ---------------------------------------------------------------- volunteer

@app.get("/api/volunteers/{vid}")
async def volunteer(vid: str):
    v, t = _get(S.volunteers, vid), now()
    offers, job = [], None
    for o in S.offers.values():
        if o.v == vid and o.status == "sent":
            d = S.donations[o.d]
            offers.append({"offer": o.id, "area": _area(d.loc), "km": round(core.km(v.loc, d.loc), 1),
                           "category": d.category, "kg": d.kg, "veg": d.veg,
                           "use_by": _clock(d.safe_until), "respond_by": _clock(o.expires) if o.expires < INF else None})
    for d in S.donations.values():
        if d.volunteer == vid and d.status in ("claimed", "picked_up"):
            r = S.recipients[d.recipient]
            job = {"d": d.id, "status": d.status, "donor": d.donor, "pickup": d.loc, "category": d.category,
                   "kg": d.kg, "pickup_by": _clock(latest_pickup(d, r, CFG)),
                   "recipient": "Confidential site: coordinator shares the proxy handover point" if r.confidential else r.name,
                   "dropoff": None if r.confidential else r.loc}
    return {"volunteer": asdict(v), "available": available(v, t), "offers": offers, "job": job}


class RespondIn(BaseModel):
    accept: bool


@app.post("/api/offers/{oid}/respond")
async def respond(oid: str, x: RespondIn):
    o, t = _get(S.offers, oid), now()
    if o.status != "sent":
        raise HTTPException(409, "this offer is no longer open")
    d, v = S.donations[o.d], S.volunteers[o.v]
    r = S.recipients.get(d.recipient)
    eta = pick_time(d, v.loc, t, 0, CFG)
    ok = x.accept and d.status in planner.OPEN and available(v, t) and r and eta <= latest_pickup(d, r, CFG)
    if ok:
        emit({"type": "OfferAccepted", "offer": oid, "eta_pick": eta, "code": f"{secrets.randbelow(10000):04d}"})
    else:
        emit({"type": "OfferDeclined", "offer": oid})
    await changed()
    if x.accept and not ok:
        raise HTTPException(409, "thanks, but this rescue is no longer possible for you; it's been re-offered")
    return {"ok": True}


def _claimed_by_volunteer(did):
    d = _get(S.donations, did)
    if d.status not in ("claimed", "picked_up"):
        raise HTTPException(409, f"donation is {d.status}")
    return d


@app.post("/api/donations/{did}/cancel")
async def cancel(did: str):
    d = _claimed_by_volunteer(did)
    if d.status != "claimed" or d.volunteer == "backup":
        raise HTTPException(409, "only a volunteer claim that isn't picked up yet can be cancelled")
    emit({"type": "ClaimCancelled", "d": did})
    await changed()
    return {"ok": True}


class PickupIn(BaseModel):
    temp_ok: bool
    packaging_ok: bool


@app.post("/api/donations/{did}/pickup")
async def pickup(did: str, x: PickupIn):
    d = _claimed_by_volunteer(did)
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
async def deliver(did: str, x: DeliverIn):
    d, t = _claimed_by_volunteer(did), now()
    if d.status != "picked_up":
        raise HTTPException(409, "pick the food up first")
    if not secrets.compare_digest(x.code, d.code or ""):
        raise HTTPException(400, "wrong handover code")
    if t - d.t_claim > 120:
        raise HTTPException(400, "handover code expired (2 h); ask the coordinator to confirm")
    if t + S.recipients[d.recipient].mu > d.safe_until:
        emit({"type": "Diverted", "d": did, "reason": "arrived too late to be served before the safety deadline"})
        await changed()
        raise HTTPException(409, "too late to be served safely: divert (not for human consumption)")
    emit({"type": "Delivered", "d": did})
    await changed()
    return {"ok": True, "receipt": f"Delivered to {S.recipients[d.recipient].name} at {_clock(t)} · {round(d.kg, 1):g} kg · ~{d.meals:.0f} meals"}


# ---------------------------------------------------------------- recipient

@app.get("/api/recipients/{rid}")
async def recipient(rid: str):
    r = _get(S.recipients, rid)
    arriving = [{"d": d.id, "donor": d.donor, "kg": d.kg, "category": d.category, "status": d.status, "code": d.code,
                 "volunteer": "Backup courier" if d.volunteer == "backup" else S.volunteers[d.volunteer].name,
                 "eta_pickup": _clock(d.eta_pick)}
                for d in S.donations.values() if d.recipient == rid and d.status in ("claimed", "picked_up")]
    held = [{"d": d.id, "kg": d.kg, "category": d.category}
            for d in S.donations.values() if d.recipient == rid and d.status in planner.OPEN]
    return {"recipient": asdict(r), "arriving": arriving, "held": held}


class CapacityIn(BaseModel):
    cap: dict[Literal["hot", "cold", "ambient"], float] | None = None
    full: list[Literal["hot", "cold", "ambient"]] | None = None


@app.patch("/api/recipients/{rid}/capacity")
async def capacity(rid: str, x: CapacityIn):
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


# ---------------------------------------------------------------- coordinator

@app.post("/api/donations/{did}/backup")
async def backup(did: str):
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


# ---------------------------------------------------------------- impact

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
async def impact():
    return _impact()


@app.get("/api/impact.csv")
async def impact_csv():
    f = io.StringIO()
    w = csv.writer(f)
    w.writerow(["id", "donor", "category", "kg", "meals", "recipient", "posted", "delivered", "status"])
    for d in S.donations.values():
        w.writerow([d.id, d.donor, d.category, d.kg, d.meals, S.recipients[d.recipient].name if d.recipient else "",
                    _clock(d.posted), _clock(d.t_drop) if d.t_drop else "", d.status])
    return PlainTextResponse(f.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=relay_impact.csv"})


# ---------------------------------------------------------------- simulation lab

def _scenario(name):
    names = list(sim.DATA_SCENARIOS) + list(sim.SCENARIOS)
    if name not in names:
        raise HTTPException(400, f"scenario must be one of {names}")
    return name


@app.get("/api/sim/demo")
async def sim_demo(scenario: str = "friday_night", seed: int = Query(0, ge=0, le=999)):
    return await run_in_threadpool(sim.demo, _scenario(scenario), seed)


@app.get("/api/sim/compare")
async def sim_compare(scenario: str = "friday_night", seeds: int = Query(30, ge=2, le=50)):
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
    uvicorn.run(app, host="127.0.0.1", port=8000)
