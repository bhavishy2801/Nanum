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


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
