"""Property checks from §17.2. Run: python test_relay.py"""
import copy
import os
from dataclasses import asdict

import core
import intake
import planner
import sim


def test_safety_and_invariants():
    learned = [p for p in ("Relay-ML", "Relay-RL", "BC-greedy") if sim.ready(p)]
    for scen in ("friday_night", "surge", "recipient_closure", "rain", "bengaluru", "bengaluru_strict"):
        for pol in ("B0", "B1", "Relay") + (tuple(learned) if scen.startswith("bengaluru") else ()):
            r = sim.run(scen, pol, 3, record=True)
            s, log, m = r["state"], r["log"], r["metrics"]
            assert m["safety_violations"] == 0, (scen, pol)
            for d in s.donations.values():            # every delivery is used before its safety deadline
                if d.status == "delivered":
                    assert d.t_drop + s.recipients[d.recipient].mu <= d.safe_until - core.CFG["eps"] + 1e-6, d
            owner = {}                                 # claims are never reassigned without ClaimCancelled
            for e in log:
                if e["type"] in ("OfferAccepted", "BackupDispatched"):
                    did = e.get("d") or s.offers[e["offer"]].d
                    assert did not in owner, (pol, did)
                    owner[did] = True
                elif e["type"] in ("ClaimCancelled",):
                    owner.pop(e["d"], None)
            if pol == "Relay":
                assert m["max_n7"] <= core.CFG["K"], m["max_n7"]   # offer budget K per volunteer


def test_replay_and_determinism():
    r1 = sim.run("friday_night", "Relay", 7, record=True)
    r2 = sim.run("friday_night", "Relay", 7, record=True)
    strip = lambda m: {k: v for k, v in m.items() if not k.startswith("latency")}
    assert strip(r1["metrics"]) == strip(r2["metrics"])
    assert asdict(core.replay(copy.deepcopy(r1["log"]))) == asdict(r1["state"])   # log -> same state
    s, t = r1["state"], 1300.0
    assert planner.plan(s, t) == planner.plan(s, t)                            # plan is a pure function


def test_wave_math():
    cfg = core.CFG
    assert abs(planner.wave_target(1, cfg) - cfg["tau_star"]) < 1e-12
    W = 5
    assert abs(1 - (1 - planner.wave_target(W, cfg)) ** W - cfg["tau_star"]) < 1e-12


def test_safety_clock():
    assert core.safe_until("cooked", 1000) == 1240
    assert core.safe_until("unknown", 1000) == 1240
    assert core.safe_until("packaged", 1000) == 1240          # no label date -> conservative 4 h
    assert core.safe_until("packaged", 1000, 2000) == 2000


def test_data_world_matches_generator():
    """The simulator's volunteer model reproduces the logged p_true exactly (non-late rows)."""
    import csv, os
    w = sim.DataWorld("bengaluru", 0)
    vols = {e["v"]["id"]: e["v"] for e in w.setup if e["type"] == "VolunteerAdded"}
    dons = {r["donation_id"]: r for ep in sim.data_tables()[0].values() for r in ep}
    with open(os.path.join(sim.DATA, "offers.csv"), newline="") as f:
        for i, r in enumerate(csv.DictReader(f)):
            if i >= 300:
                break
            if r["late_night"] == "True":
                continue
            v, dr = vols[r["volunteer_id"]], dons[r["donation_id"]]
            vv = core.Volunteer(v["id"], "", tuple(v["loc"]), v["cap_kg"], n7=int(r["n_offers_7d"]))
            dd = core.Donation("d", "", (float(dr["pickup_lat"]), float(dr["pickup_lon"])), "cooked", "hot", None,
                               float(dr["weight_kg"]), 1, 0, 0, 0, 0)
            assert abs(w.p_true(vv, dd, 1080.0, 0) / sim.P_RESPOND - float(r["p_true"])) < 1e-9


def test_learned_models_load():
    m = core.load_p_model(os.path.join(sim.MODELS, "acceptance.json"))
    if m is None:
        print("  (skipped: run python ml.py all)")
        return
    w = sim.DataWorld("bengaluru", 0)
    s = core.replay([dict(e, t=0.0) for e in w.setup])
    d = core.Donation("d", "", w.exo[0][1]["d"]["loc"], "cooked", "hot", None, 10.0, 18, 0, 0, 0, 0)
    ps = [core.p_accept(v, d, 1080.0, {**core.CFG, "p_model": m}) for v in s.volunteers.values()]
    assert all(0 < p < 1 for p in ps) and len(set(round(p, 6) for p in ps)) > 100   # volunteer-specific
    assert len(planner.rl_state(0, 60, 10, 18, "cooked", 0.0, 0, 0.0, sorted(ps, reverse=True))) == len(planner.RL_FEATURES)


def test_intake():
    x = intake.regex_parse("30 plates veg biryani + 40 rotis, kept hot, we close 11")
    assert (x["category"], x["holding"], x["veg"], x["plates"], x["ready_until"]) == ("cooked", "hot", True, 30, "23:00")
    y = intake.regex_parse("10 kg chicken curry, closing at 10:30pm")
    assert (y["veg"], y["kg"], y["ready_until"], y["holding"]) == (False, 10, "22:30", "unknown")
    assert intake.regex_parse("some food, up to 5 kg")["ready_until"] is None


def test_real_people_before_simulated_stand_ins():
    """A real donor's food goes to a real signed-in driver and shelter first, even when simulated ones rank higher.
    Simulated donations are planned as before, so a real driver isn't offered every simulated post."""
    s, INF = core.State(), core.INF
    for rid, loc in (("R001", (12.93, 77.62)), ("S001", (12.95, 77.60))):   # R = simulated, S = registered by a person
        core.apply(s, {"type": "RecipientAdded", "t": 0, "r": dict(id=rid, name=rid, loc=loc, alpha=0, beta=INF, mu=0,
                                                                  cap={"hot": 100, "cold": 100, "ambient": 100})})
    for i in range(6):   # simulated drivers right next to the donor, the real one a bit further away
        core.apply(s, {"type": "VolunteerAdded", "t": 0, "v": dict(id=f"V{i}", name=f"V{i}", loc=(12.935, 77.625 + i * 1e-4), cap_kg=30)})
    core.apply(s, {"type": "VolunteerAdded", "t": 0, "v": dict(id="U0001", name="Real", loc=(12.96, 77.64), cap_kg=30)})
    cfg = {**core.CFG, "real_v": {"U0001"}, "real_r": {"S001"}}
    for did, owner in (("d1", "cook@x.com"), ("d2", "sim")):
        core.apply(s, {"type": "DonationPosted", "t": 600, "d": dict(id=did, donor="K", loc=(12.935, 77.625), category="cooked",
                                                                    holding="hot", veg=True, kg=10, meals=18, a=600, b=720,
                                                                    safe_until=840, posted=600, owner=owner)})
    plain = [e for e in planner.plan(copy.deepcopy(s), 600, core.CFG) if e.get("d") == "d2"]   # without any real people
    for e in planner.plan(s, 600, cfg):
        e.setdefault("t", 600)
        core.apply(s, e)
    to = lambda did: {o.v for o in s.offers.values() if o.d == did}
    assert to("d1") == {"U0001"}, to("d1")                      # the real driver, not the nearer simulated ones
    assert s.donations["d1"].recipient == "S001"               # the real shelter (it passes the safety rules)
    assert to("d2") == {e["v"] for e in plain if e["type"] == "OfferSent"}   # simulated food: planned exactly as before


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
