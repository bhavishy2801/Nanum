"""Relay ML pipeline on relay_data/: audit + fix the logs, train the acceptance model, behaviour cloning and
offline RL (fitted Q iteration) for wave sizing.

    python ml.py audit        # checks the dataset, writes models/data_audit.json
    python ml.py acceptance   # P(volunteer accepts within 8 min) -> models/acceptance.json (+ report)
    python ml.py rl           # corrected decision dataset -> models/bc.joblib, models/fqi_lam*.joblib (+ report)
    python ml.py all          # all three, in order

Splits are by episode and chronological: train 0-209, valid 210-254, test 255-299. The test episodes are also
the ones `python sim.py bengaluru 30` replays, so every reported number is on data no model was fitted on.
"""
import json
import math
import os
import sys
import time

import numpy as np
import pandas as pd
from scipy import sparse
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, brier_score_loss, f1_score, log_loss, roc_auc_score

import core
import planner
import sim

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "relay_data")
MODELS = os.path.join(HERE, "models")
TRAIN, VALID, TEST = range(0, 210), range(210, 255), range(255, 300)
EPISODE_START = 18 * 60    # t=0 is 18:00 (see audit: shifts 17-26 h, late flag at t=240 -> 22:00)
DELTA = core.CFG["wave_timeout"]   # the acceptance model predicts "accepts within one wave"
LATE_PRIOR = core.CFG["beta"][1]   # -0.8: the logs carry almost no late-night offers (audit #6), so not learnable
LAMBDAS = (0.02, 0.25)             # notification cost in meals: the dataset's own reward (0.02) and a burnout-aware one
GAMMA, FQI_ITERS = 0.97, 20


def _save(name, obj):
    os.makedirs(MODELS, exist_ok=True)
    with open(os.path.join(MODELS, name), "w") as f:
        json.dump(obj, f, indent=1, default=float)


def _hav(lat1, lon1, lat2, lon2):
    la1, lo1, la2, lo2 = map(np.radians, (lat1, lon1, lat2, lon2))
    h = np.sin((la2 - la1) / 2) ** 2 + np.cos(la1) * np.cos(la2) * np.sin((lo2 - lo1) / 2) ** 2
    return 12742 * np.arcsin(np.sqrt(h))


def load():
    """Read relay_data/ and FIX the clock: offer and assignment times are minutes since the donation was posted."""
    rd = lambda f: pd.read_csv(os.path.join(DATA, f))
    D = {k: rd(f"{k}.csv") for k in ("donations", "offers", "assignments", "volunteers", "recipients", "episodes_meta")}
    post = D["donations"].set_index("donation_id").post_t
    D["offers"]["t_abs"] = D["offers"].donation_id.map(post) + D["offers"].offer_t
    for c in ("accepted_at", "pickup_time_min", "delivered_time_min"):
        D["assignments"][c + "_abs"] = D["assignments"].donation_id.map(post) + D["assignments"][c]
    D["offers"]["y"] = D["offers"].accept & (D["offers"].response_delay_min <= DELTA)
    return D


# ---------------------------------------------------------------- 1. audit

def audit(D):
    don, off, asg, vol, rec = D["donations"], D["offers"], D["assignments"], D["volunteers"], D["recipients"]
    post = don.set_index("donation_id").post_t
    F = {}
    rel = off.offer_t - off.donation_id.map(post)
    F["1_times_are_relative"] = {
        "offers_before_their_donation_was_posted_if_read_as_absolute": float((rel < 0).mean()),
        "first_offer_offset_median_min": float(off.groupby("donation_id").offer_t.min().median()),
        "fix": "t_abs = donations.post_t + offer_t (same for accepted_at / pickup_time_min / delivered_time_min)"}

    rl = pd.read_parquet(os.path.join(DATA, "rl_transitions.parquet"),
                         columns=["episode_id", "step", "n_offers", "reward", "done"])
    b_rel = off.assign(step=(off.offer_t // 8).astype(int)).groupby(["episode_id", "step"]).size()
    b_abs = off.assign(step=(off.t_abs // 8).astype(int)).groupby(["episode_id", "step"]).size()
    x = rl.set_index(["episode_id", "step"]).n_offers
    active = rl[rl.reward.abs() > 1e-9].step
    F["2_rl_transitions_misaligned"] = {
        "n_offers_match_if_binned_by_RELATIVE_time": float((x == b_rel.reindex(x.index, fill_value=0)).mean()),
        "n_offers_match_if_binned_by_true_time": float((x == b_abs.reindex(x.index, fill_value=0)).mean()),
        "share_of_reward_bearing_steps_in_first_16_of_61": float((active.between(1, 16)).mean()),
        "verdict": "rewards/offers are front-loaded onto the wrong steps; rebuilt from the raw logs instead"}

    claims = asg.merge(don[["donation_id", "meals"]], on="donation_id")
    claims["step"] = (claims.accepted_at // 8).astype(int) + 1      # the generator's (relative-time) credit
    c = claims.groupby(["episode_id", "step"]).meals.sum()
    r = rl.set_index(["episode_id", "step"])
    resid = r.reward - (c.reindex(r.index, fill_value=0) - 0.02 * r.n_offers)
    F["3_reward_decoded"] = {
        "formula": "meals claimed in the next 8 min - 0.02 * offers sent (+ an unexplained terminal term)",
        "non_terminal_steps_exactly_explained": float((resid[~r.done].abs() < 1e-6).mean()),
        "terminal_term_mean": float(resid[r.done].mean()),
        "notification_cost_lambda": 0.02}

    bc = pd.read_csv(os.path.join(DATA, "bc_dataset.csv"), usecols=["n_offers_epoch", "escalate"])
    F["4_bc_dataset_labels_empty"] = {"rows": len(bc), "nonzero_n_offers_epoch": int((bc.n_offers_epoch != 0).sum()),
                                     "nonzero_escalate": int((bc.escalate != 0).sum()),
                                     "fix": "BC labels rebuilt from offers.csv (offers per donation per 8-min step)"}
    F["5_p_hat_is_the_oracle"] = {"share_p_hat_equals_p_true": float((off.p_hat - off.p_true).abs().lt(1e-12).mean()),
                                  "consequence": "neither is usable as a feature; p_true is only used as the ceiling"}
    F["6_late_flag_uses_relative_time"] = {
        "matches_offer_t_rel_ge_240": float(((off.offer_t >= 240) == off.late_night).mean()),
        "matches_real_clock_ge_22h": float(((off.t_abs >= 240) == off.late_night).mean()),
        "late_share_logged": float(off.late_night.mean()),
        "consequence": f"late-night effect not learnable from these logs; the model uses the prior {LATE_PRIOR}"}

    v = vol.set_index("volunteer_id")
    lg = np.log(off.p_true.clip(1e-9, 1 - 1e-9) / (1 - off.p_true.clip(1e-9, 1 - 1e-9)))
    X = np.c_[np.ones(len(off)), off.volunteer_id.map(v.theta_true), off.distance_km, off.late_night,
              np.log1p(off.n_offers_7d), off.weight_ratio]
    coef, *_ = np.linalg.lstsq(X, lg, rcond=None)
    F["7_true_acceptance_recovered"] = {
        "logit_p_true": dict(zip(["const", "theta_v", "distance_km", "late", "log1p_n7", "weight_ratio"], coef.round(4))),
        "r2": float(1 - ((lg - X @ coef) ** 2).sum() / ((lg - lg.mean()) ** 2).sum())}

    acc = off[off.accept]
    F["8_response_process"] = {
        "p_respond": float(off.responded.mean()),
        "accept_given_respond": float(off[off.responded].accept.mean()),
        "mean_p_true_given_respond": float(off[off.responded].p_true.mean()),
        "delay_lognormal_mu_sigma": [float(np.log(off[off.responded].response_delay_min).mean()),
                                     float(np.log(off[off.responded].response_delay_min).std())],
        "accept_within_8_min": float((acc.response_delay_min <= DELTA).mean()),
        "cancel_given_accept": float(acc.cancelled.mean()), "no_show_given_accept": float(acc.no_show.mean()),
        "accepted_offers_that_became_the_assignment": float(len(asg) / len(acc))}

    a = asg.merge(don[["donation_id", "pickup_lat", "pickup_lon", "ready_until_t"]], on="donation_id") \
        .merge(vol[["volunteer_id", "home_lat", "home_lon"]], on="volunteer_id") \
        .merge(rec[["recipient_id", "lat", "lon", "open_hour", "close_hour", "use_lead_min"]], on="recipient_id")
    d1 = _hav(a.home_lat, a.home_lon, a.pickup_lat, a.pickup_lon)
    d2 = _hav(a.pickup_lat, a.pickup_lon, a.lat, a.lon)
    clock = 18 + a.delivered_time_min_abs / 60
    F["9_world"] = {
        "distance_is_haversine_from_home": float(np.allclose(off.distance_km, _hav(
            off.volunteer_id.map(v.home_lat), off.volunteer_id.map(v.home_lon),
            off.donation_id.map(don.set_index("donation_id").pickup_lat),
            off.donation_id.map(don.set_index("donation_id").pickup_lon)))),
        "pickup_leg_min_fit": list(np.polyfit(d1, a.pickup_time_min - a.accepted_at, 1).round(2)),
        "drop_leg_min_fit": list(np.polyfit(d2, a.delivered_time_min - a.pickup_time_min, 1).round(2)),
        "deliveries_outside_listed_recipient_hours": float(((clock < a.open_hour) | (clock > a.close_hour)).mean()),
        "cooked_to_recipients_serving_8h_later": int(((a.use_lead_min >= 480) &
                                                      a.donation_id.map(don.set_index("donation_id").category).eq("cooked")).sum()),
        "pickups_after_donor_ready_until": float((a.pickup_time_min_abs > a.ready_until_t).mean()),
        "detour_min_all_zero": bool((off.detour_min == 0).all())}
    F["10_outcomes"] = {"status": don.status.value_counts().to_dict(),
                        "safety_violations_logged": int(asg.safety_violation.sum()),
                        "escalations_logged": int(D["episodes_meta"].n_escalations.sum()),
                        "episodes_by_policy": D["episodes_meta"].policy.value_counts().to_dict()}
    _save("data_audit.json", F)
    for k, val in F.items():
        print(k, json.dumps(val, default=float))
    return F


# ---------------------------------------------------------------- 2. acceptance model

def _x(o):
    """Vectorised core.accept_x (without late, which is fixed to its prior)."""
    return np.c_[o.distance_km, np.log1p(o.n_offers_7d), o.weight_ratio]


def _metrics(y, p):
    p = np.clip(p, 1e-6, 1 - 1e-6)
    bins = np.minimum((p * 10).astype(int), 9)
    ece = sum(abs(p[bins == b].mean() - y[bins == b].mean()) * (bins == b).mean() for b in range(10) if (bins == b).any())
    return {"log_loss": log_loss(y, p), "brier": brier_score_loss(y, p), "auc": roc_auc_score(y, p),
            "ece": float(ece), "mean_pred": float(p.mean()), "observed": float(y.mean())}


def acceptance(D):
    off, vol = D["offers"], D["volunteers"]
    vids = list(vol.volunteer_id)
    vi = off.volunteer_id.map({v: i for i, v in enumerate(vids)}).to_numpy()
    y = off.y.to_numpy().astype(int)
    ep = off.episode_id.to_numpy()
    tr, va, te = (np.isin(ep, list(s)) for s in (TRAIN, VALID, TEST))
    late = off.late_night.to_numpy().astype(float)
    Xf = _x(off)
    Xv = sparse.hstack([sparse.csr_matrix(Xf), sparse.csr_matrix((np.ones(len(off)), (np.arange(len(off)), vi)),
                                                                 shape=(len(off), len(vids)))]).tocsr()
    R = {}
    R["base_rate"] = _metrics(y[te], np.full(te.sum(), y[tr].mean()))

    # the hand-set model the planner shipped with (core.p_accept), with Beta history from training episodes only
    a0, b0 = core.CFG["prior"]
    h = off[tr].groupby("volunteer_id").y.agg(["sum", "count"])
    rate = off.volunteer_id.map((h["sum"] + a0) / (h["count"] + a0 + b0)).fillna(a0 / (a0 + b0)).to_numpy()
    (c1, c2, c3), (m1, m2, m3) = core.CFG["beta"], core.CFG["feat_mean"]
    z = np.log(rate / (1 - rate)) + c1 * (off.distance_km - m1) + c2 * (late - m2) + c3 * (np.log1p(off.n_offers_7d) - m3)
    R["hand_set_relay_model"] = _metrics(y[te], 1 / (1 + np.exp(-z[te])))

    lr = LogisticRegression(C=1.0, max_iter=2000).fit(Xf[tr], y[tr])
    R["logistic_features_only"] = _metrics(y[te], lr.predict_proba(Xf[te])[:, 1])

    best = None
    for C in (0.01, 0.03, 0.1, 0.3, 1.0):   # L2 = shrinkage of per-volunteer intercepts toward the population
        m = LogisticRegression(C=C, max_iter=3000).fit(Xv[tr], y[tr])
        ll = log_loss(y[va], m.predict_proba(Xv[va])[:, 1])
        if best is None or ll < best[0]:
            best = (ll, C)
    C = best[1]
    trva = tr | va
    lrv = LogisticRegression(C=C, max_iter=3000).fit(Xv[trva], y[trva])
    p_te = lrv.predict_proba(Xv[te])[:, 1]
    R["logistic_plus_volunteer_effect (EXPORTED)"] = {**_metrics(y[te], p_te), "C": C}

    cats = pd.DataFrame({"vol": vi, "cat": off.donation_id.map(D["donations"].set_index("donation_id").category)
                         .astype("category").cat.codes,
                         "zone": off.donation_id.map(D["donations"].set_index("donation_id").zone).astype("category").cat.codes})
    Xh = np.c_[Xf, cats.to_numpy(), off.t_abs]
    hgb = HistGradientBoostingClassifier(max_iter=400, learning_rate=0.05, categorical_features=[3, 4, 5],
                                         early_stopping=True, random_state=0).fit(Xh[trva], y[trva])
    R["gradient_boosting (not exported)"] = _metrics(y[te], hgb.predict_proba(Xh[te])[:, 1])

    # ceiling: the generator's own probability for this target
    k = (off.responded & (off.response_delay_min <= DELTA))[tr].mean()
    R["oracle_p_true_ceiling"] = _metrics(y[te], (off.p_true * k).to_numpy()[te])

    coef = lrv.coef_[0]
    model = {"features": core.ACCEPT_FEATURES, "target": f"accepts within {DELTA} min of the offer",
             "intercept": float(lrv.intercept_[0]),
             "coef": [float(coef[0]), LATE_PRIOR, float(coef[1]), float(coef[2])],
             "late_night_coef": "fixed prior from the report (§6.3), not learned: 0.07% of logged offers were late",
             "volunteer": {v: float(w) for v, w in zip(vids, coef[3:])},
             "trained_on_episodes": [0, 254], "C": C, "test_metrics": R["logistic_plus_volunteer_effect (EXPORTED)"]}

    # serving parity: core.p_accept must reproduce the fitted model exactly (late=0 rows)
    rows = np.flatnonzero(te & (late == 0))[:200]
    don = D["donations"].set_index("donation_id")
    for i in rows:
        o = off.iloc[i]
        v = core.Volunteer(o.volunteer_id, "", (0.0, 0.0), 1.0, n7=int(o.n_offers_7d))
        d = core.Donation("d", "", (0.0, 0.0), "cooked", "hot", None, float(o.weight_ratio), 1, 0, 0, 0, 0)
        v.loc = (0.0, 0.0)
        d.loc = (0.0, math.degrees(o.distance_km / 6371))   # a point exactly distance_km east of v
        p = core.p_accept(v, d, 1080.0, {**core.CFG, "p_model": model})
        assert abs(p - lrv.predict_proba(Xv[i])[0, 1]) < 1e-6, (p, lrv.predict_proba(Xv[i])[0, 1])

    bins = np.minimum((p_te * 10).astype(int), 9)
    model["calibration_test"] = [{"bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": int((bins == b).sum()),
                                  "predicted": float(p_te[bins == b].mean()), "observed": float(y[te][bins == b].mean())}
                                 for b in range(10) if (bins == b).any()]
    _save("acceptance.json", model)
    _save("acceptance_report.json", R)
    print(pd.DataFrame(R).T[["log_loss", "brier", "auc", "ece", "mean_pred", "observed"]].round(4).to_string())
    return model


# ---------------------------------------------------------------- 3. decision dataset (corrected) for BC and RL

def decisions(D, pm):
    """One row per (donation, 8-min step since posting) while it is open, rebuilt on the TRUE clock.
    State = planner.rl_state; action = offers sent in the step (count, claim mass); gain = meals if the final
    claim lands in the step AND succeeds under the safety rules (picked up within the donor window)."""
    don, off, asg, vol = D["donations"], D["offers"].copy(), D["assignments"], D["volunteers"]
    vids = list(vol.volunteer_id)
    vix = {v: i for i, v in enumerate(vids)}
    vlat, vlon, cap = vol.home_lat.to_numpy(), vol.home_lon.to_numpy(), vol.capacity_kg.to_numpy()
    s_on = vol.shift_start_h.to_numpy() * 60 - EPISODE_START
    s_off = vol.shift_end_h.to_numpy() * 60 - EPISODE_START
    th = np.array([pm["volunteer"].get(v, 0.0) for v in vids])
    b0, (c_d, c_l, c_n, c_w) = pm["intercept"], pm["coef"]
    off["vi"] = off.volunteer_id.map(vix)
    off["p"] = 1 / (1 + np.exp(-(b0 + c_d * off.distance_km + c_l * off.late_night + c_n * np.log1p(off.n_offers_7d)
                                  + c_w * off.weight_ratio + th[off.vi])))
    n7_med = off.groupby("vi").n_offers_7d.median()
    eps, rows = core.CFG["eps"], []
    reach_prep, reach_mpk = sim.DATA_CFG["prep"], sim.DATA_CFG["min_per_km"]
    t0 = time.time()
    for ep, dg in don.groupby("episode_id"):
        oe = off[off.episode_id == ep]
        ae = asg[asg.episode_id == ep]
        n7 = np.array([n7_med.get(i, 0) for i in range(len(vids))], dtype=float)
        first = oe.drop_duplicates("vi")
        n7[first.vi.to_numpy()] = first.n_offers_7d.to_numpy()
        busy = [(vix[r.volunteer_id], r.accepted_at_abs, r.delivered_time_min_abs) for r in ae.itertuples()]
        claim = {r.donation_id: r for r in ae.itertuples()}
        by_d = {k: g.sort_values("t_abs") for k, g in oe.groupby("donation_id")}
        for d in dg.itertuples():
            s_d = min(core.safe_until(d.category, d.post_t), d.safety_deadline_t)
            limit = min(d.ready_until_t, s_d - eps, 480.0)
            c = claim.get(d.donation_id)
            claimed = c is not None and c.accepted_at_abs < limit
            end = c.accepted_at_abs if claimed else limit
            ok = claimed and bool(c.used_safely) and c.pickup_time_min_abs <= d.ready_until_t
            dist = _hav(vlat, vlon, d.pickup_lat, d.pickup_lon)
            og = by_d.get(d.donation_id)
            ot = og.t_abs.to_numpy() if og is not None else np.empty(0)
            ov = og.vi.to_numpy() if og is not None else np.empty(0, int)
            op = og.p.to_numpy() if og is not None else np.empty(0)
            t, k = d.post_t, 0
            while t < end:
                before = ot < t
                win = (ot >= t) & (ot < t + 8)
                offered = np.zeros(len(vids), bool)
                offered[ov[before]] = True
                is_busy = np.zeros(len(vids), bool)
                for i, a_, b_ in busy:
                    if a_ <= t < b_:
                        is_busy[i] = True
                lt = core.late(EPISODE_START + t)
                mask = (s_on <= t) & (t < s_off) & ~is_busy & (cap >= d.weight_kg) & ~offered \
                    & (t + reach_prep + reach_mpk * dist <= d.ready_until_t)   # == sim pick_time(.., 0) <= b
                pz = b0 + c_d * dist + c_l * lt + c_n * np.log1p(n7) + c_w * d.weight_kg / cap + th
                pool = sorted((1 / (1 + np.exp(-pz[mask]))).tolist(), reverse=True)
                x = planner.rl_state(t - d.post_t, min(d.ready_until_t, s_d - eps) - t, d.weight_kg, d.meals,
                                     d.category, lt, int(before.sum()), float(op[before].sum()), pool)
                cm = np.cumsum([0.0] + pool)
                now_claim = claimed and t <= end < t + 8
                rows.append(x + [int(win.sum()), float(op[win].sum()),
                                 float(d.meals) if (now_claim and ok) else 0.0, bool(now_claim or t + 8 >= end),
                                 ep, d.donation_id, k] + [cm[min(kk, len(pool))] if kk <= len(pool) else np.nan
                                                          for kk in planner.RL_K])
                t, k = t + 8, k + 1
    cols = planner.RL_FEATURES + ["n_act", "mass_act", "gain", "done", "episode_id", "donation_id", "step"] + \
        [f"cand_{k}" for k in planner.RL_K]
    X = pd.DataFrame(rows, columns=cols)
    X["next"] = np.where(X.done, -1, np.arange(len(X)) + 1)
    pol = D["episodes_meta"].set_index("episode_id").policy
    X["policy"] = X.episode_id.map(pol)
    print(f"decision rows: {len(X)} from {X.donation_id.nunique()} donations in {time.time() - t0:.0f}s")
    return X


# ---------------------------------------------------------------- 4. behaviour cloning + fitted Q iteration

def _bucket(n):
    return np.searchsorted(np.array(planner.RL_K), n, side="right") - 1


def bc(X):
    """Clone the logged 'greedy' behaviour: which wave-size bucket it chose, from the state."""
    g = X[X.policy == "greedy"]
    S, yb = g[planner.RL_FEATURES].to_numpy(), _bucket(g.n_act.to_numpy())
    tr = g.episode_id.isin(list(TRAIN) + list(VALID)).to_numpy()
    te = g.episode_id.isin(list(TEST)).to_numpy()
    m = HistGradientBoostingClassifier(max_iter=200, learning_rate=0.05, early_stopping=False, random_state=0)
    m.fit(S[tr], yb[tr])
    pred = m.predict(S[te])
    maj = np.bincount(yb[tr]).argmax()
    rep = {"rows_train": int(tr.sum()), "rows_test": int(te.sum()),
           "accuracy": accuracy_score(yb[te], pred), "macro_f1": f1_score(yb[te], pred, average="macro"),
           "majority_baseline_accuracy": accuracy_score(yb[te], np.full(te.sum(), maj)),
           "classes": list(planner.RL_K)}
    import joblib
    joblib.dump(m, os.path.join(MODELS, "bc.joblib"))
    print("BC (greedy clone):", {k: round(v, 3) if isinstance(v, float) else v for k, v in rep.items()})
    return rep


def fqi(X, lam):
    """Fitted Q iteration. Q(s, a) with a = (k offers, their claim mass); reward = meals claimed - lam * offers.
    Next-state value = max over feasible top-k candidates. Trained on episodes 0-254."""
    tr = X.episode_id.isin(list(TRAIN) + list(VALID)).to_numpy()
    Z = X[tr].reset_index(drop=True)
    remap = np.full(len(X), -1)
    remap[np.flatnonzero(tr)] = np.arange(tr.sum())
    nxt = np.where(Z.next.to_numpy() >= 0, remap[np.clip(X.next.to_numpy()[tr], 0, None)], -1)
    S = Z[planner.RL_FEATURES].to_numpy()
    XA = np.c_[S, Z.n_act, Z.mass_act]
    r = Z.gain.to_numpy() - lam * Z.n_act.to_numpy()
    has = nxt >= 0
    Sn = S[nxt[has]]
    cand = Z[[f"cand_{k}" for k in planner.RL_K]].to_numpy()[nxt[has]]
    y, hist = r.copy(), []
    m = None
    for it in range(FQI_ITERS):
        m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.08, max_leaf_nodes=31, min_samples_leaf=40,
                                          l2_regularization=1.0, random_state=0).fit(XA, y)
        q = np.full(cand.shape, -np.inf)
        for j, k in enumerate(planner.RL_K):
            ok = ~np.isnan(cand[:, j])
            if ok.any():
                q[ok, j] = m.predict(np.c_[Sn[ok], np.full(ok.sum(), k), cand[ok, j]])
        y_new = r.copy()
        y_new[has] += GAMMA * q.max(axis=1)
        hist.append(float(np.abs(y_new - y).mean()))
        y = y_new
    m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.08, max_leaf_nodes=31, min_samples_leaf=40,
                                      l2_regularization=1.0, random_state=0).fit(XA, y)
    import joblib
    name = f"fqi_lam{lam}.joblib"
    joblib.dump(m, os.path.join(MODELS, name))

    T = X[X.episode_id.isin(list(TEST))]   # diagnostics on held-out states
    St = T[planner.RL_FEATURES].to_numpy()
    ct = T[[f"cand_{k}" for k in planner.RL_K]].to_numpy()
    qt = np.full(ct.shape, -np.inf)
    for j, k in enumerate(planner.RL_K):
        ok = ~np.isnan(ct[:, j])
        qt[ok, j] = m.predict(np.c_[St[ok], np.full(ok.sum(), k), ct[ok, j]])
    k_star = np.array(planner.RL_K)[qt.argmax(axis=1)]
    q_log = m.predict(np.c_[St, T.n_act, T.mass_act])
    rep = {"lambda": lam, "gamma": GAMMA, "iterations": FQI_ITERS, "rows": int(tr.sum()),
           "bellman_change_per_iter": hist,
           "test_mean_k_policy": float(k_star.mean()), "test_mean_k_logged": float(T.n_act.mean()),
           "test_share_wait_k0": float((k_star == 0).mean()),
           "test_Q_policy_minus_Q_logged (model estimate, not a result)": float((qt.max(axis=1) - q_log).mean())}
    print(f"FQI lam={lam}:", {k: (round(v, 3) if isinstance(v, float) else v) for k, v in rep.items()
                                if k != "bellman_change_per_iter"}, "bellman", [round(h, 2) for h in hist[-3:]])
    return rep


def rl(D):
    pm = core.load_p_model(os.path.join(MODELS, "acceptance.json"))
    assert pm, "run `python ml.py acceptance` first"
    X = decisions(D, pm)
    X.to_parquet(os.path.join(MODELS, "decisions.parquet"))
    rep = {"decision_rows": len(X), "actions_logged": X.groupby("policy").n_act.describe().round(2).to_dict(),
           "bc": bc(X), "fqi": [fqi(X, lam) for lam in LAMBDAS]}
    _save("rl_report.json", rep)
    return rep


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "all"
    D = load()
    if what in ("audit", "all"):
        audit(D)
    if what in ("acceptance", "all"):
        acceptance(D)
    if what in ("rl", "all"):
        rl(D)
