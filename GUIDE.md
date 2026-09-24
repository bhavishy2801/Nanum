# Relay: ML and RL guide

This guide covers how the dataset in `relay_data/` is turned into models, how those models plug into dispatch, and how to run and check everything.

All results below are **simulated**. The evaluation replays held-out episodes that no model was trained on.

---

## 0. Setup (once)

```bash
pip install -r requirements.txt
```

The dataset must be in `relay_data/`. `relay_data.zip` holds the same files.

## 1. The whole pipeline in four commands

```bash
python ml.py all                  # audit + fix the data, train the acceptance model, BC and offline RL (~45 s)
python test_relay.py              # 7 property checks incl. "sim reproduces the logged p_true exactly" (~20 s)
python sim.py bengaluru 30        # evaluate every policy on 30 held-out episodes (~2 min, 8 cores)
python app.py                     # live app at http://127.0.0.1:8000, dispatching with Relay-RL
```

Each step is explained below.

---

## 2. Step 1: audit the data (`python ml.py audit`)

The dataset has problems, and training on it as-is would produce wrong models. The audit measures each issue and writes `models/data_audit.json`.

| # | Finding | Evidence | What the pipeline does |
|---|---|---|---|
| 1 | Times in `offers.csv` and `assignments.csv` are **minutes since the donation was posted**, not episode time | 96.9% of offers would come before their own donation; first offer is always at 0 | Converts them: `t_abs = post_t + offer_t` |
| 2 | `rl_transitions.parquet` was built on those relative times | Offer counts match relative-time binning 100% and true-time binning 23%; 82% of rewards land in steps 1–16 of 61 | **Not trained on.** Transitions are rebuilt from the raw logs on the true clock |
| 3 | Reward decoded | `meals claimed in next 8 min − 0.02 × offers` explains 99.9% of non-terminal steps; the terminal term is unexplained (mean −186) | Keeps the **λ = 0.02 per notification** convention |
| 4 | `bc_dataset.csv` has no labels | `n_offers_epoch` and `escalate` are 0 in all 310,978 rows | BC labels rebuilt from `offers.csv` |
| 5 | `p_hat` equals `p_true` in every row | The behaviour policies used the oracle | Neither is used as a feature; `p_true` is only the ceiling |
| 6 | The late-night flag follows relative time ≥ 240, not the clock | Only 0.07% of offers are flagged | The late-night effect can't be learned from this data, so the model uses the report's prior (−0.8) |
| 7 | The true acceptance function is recoverable | `logit p = θ_v − 0.35·km − 0.8·late − 0.3·log(1+n7d) − 0.5·kg/capacity`, R² = 1.0 | The simulator uses it as ground truth |
| 8 | Response process | Responds 75.1%; accepts with p_true; delay lognormal (median 4 min); cancel 7.9%, no-show 3.0% | Simulator parameters |
| 9 | World | Travel 3.0 min/km (+5 min prep, +15 min handling); 76% of deliveries fell outside the listed recipient hours; 921 cooked drops went to shelters serving 8 h later | Two data scenarios (below); the safety rules always apply |
| 10 | Outcomes | 11,587 delivered, 175 expired, 332 still open; 17 safety violations; **0 escalations** | Escalation can't be learned offline, so it stays rule-based (L_d) |

Episode t=0 is taken as **18:00**. That's inferred from the volunteer shifts (17–26 h) and the generator's 240-minute late flag.

## 3. Step 2: acceptance model (`python ml.py acceptance`)

The model predicts the probability that volunteer v accepts donation d within one 8-minute wave. Relay's wave sizing (noisy-OR), P(claim) and the early-escalation rule all run on this number.

- **Features** (`core.accept_x`; training and serving share it): distance in km, late night, log(1 + offers this week), kg / vehicle capacity, plus a **per-volunteer intercept** learned with L2 shrinkage. Volunteers the model has never seen fall back to their live Beta history.
- **Splits** by episode, in time order: train 0–209, validation 210–254 (to choose the shrinkage C), test 255–299.

Results on the test set (`models/acceptance_report.json`):

| Model | Log loss | Brier | AUC | ECE |
|---|---|---|---|---|
| Base rate | 0.3141 | 0.0860 | 0.500 | 0.003 |
| Hand-set model Relay shipped with | 0.2729 | 0.0786 | 0.773 | 0.012 |
| Logistic, features only | 0.2849 | 0.0813 | 0.727 | 0.004 |
| **Logistic + volunteer effect (exported)** | **0.2665** | **0.0772** | **0.786** | 0.005 |
| Gradient boosting (not exported) | 0.2688 | 0.0777 | 0.781 | 0.003 |
| Oracle `p_true` (ceiling) | 0.2660 | 0.0771 | 0.787 | 0.003 |

The exported model is effectively at the ceiling, and gradient boosting doesn't beat it. So the model ships as plain JSON (`models/acceptance.json`): no scikit-learn is needed to serve it, and the planner stays fast. `ml.py` asserts that `core.p_accept` reproduces scikit-learn's predictions to 1e-6. The calibration table is inside the JSON.

## 4. Step 3: behaviour cloning and offline RL (`python ml.py rl`)

**Decision dataset** (`models/decisions.parquet`, 36,812 rows): one row per donation for every 8 minutes it stays open, built on the true clock.
- **State** (`planner.rl_state`, shared with serving): time since posting, slack, kg, meals, category, late night, offers already sent, their claim mass, and the pool of volunteers who could still reach the donor in time (count and top-1/3/8 p).
- **Action:** how many volunteers to offer this step, plus the claim mass of those offers.
- **Reward:** meals if the final claim lands in this step and succeeds under the safety rules, minus λ × offers.

**Behaviour cloning** (`models/bc.joblib`) clones the logged greedy policy's wave-size choice. Test accuracy is 0.70 against a 0.43 majority baseline (macro-F1 0.47).

**Fitted Q iteration** (`models/fqi_lam0.02.joblib`, `models/fqi_lam0.25.joblib`):
- **Model:** gradient-boosted Q(s, a), γ = 0.97, 20 iterations. The Bellman residual drops from 5.0 to about 0.22 and then plateaus, which is normal refit noise for tree-based FQI.
- **Action set:** k ∈ {0, 1, 2, 3, 5, 8}. The logs never send more than 8 per step, and offline RL must not choose actions it has no data for (the support constraint).
- **Two reward settings:** λ = 0.02 is the dataset's own reward. λ = 0.25 (4 notifications cost one meal) is a burnout-aware assumption.
- **Scope of the RL:** it only sizes waves. Safety, recipient choice and the escalation deadline L_d stay deterministic rules, and the logs contain no escalations to learn from anyway.

The Q-function's in-sample estimates are diagnostics, not results. The real test is the next step.

## 5. Step 4: evaluate in simulation (`python sim.py bengaluru 30`)

The `bengaluru` world uses the real 12 recipients and 150 volunteers. Seed i replays test episode 255+i's real donation stream, and volunteers respond according to the recovered generator.
- `bengaluru` receives at any hour, as the logs did.
- `bengaluru_strict` enforces the recipients' listed hours.
- In both, the 4-hour clock and the use-lead check always apply. Results go to `results_bengaluru.json`, which the Simulation lab tab reads.

**30 held-out episodes, mean ± 95% CI:**

| | B0 broadcast | B1 static412 | Relay | Relay-ML | **Relay-RL** | Relay-RL λ=.25 | BC-greedy |
|---|---|---|---|---|---|---|---|
| Rescue rate (kg) | 0.766 | **0.935** | 0.888 | 0.889 | 0.896 | 0.891 | 0.896 |
| Notifications per rescue | 145 | 66 | 25.0 | 24.9 | 12.7 | 10.7 | **8.1** |
| p95 offers per volunteer per night | 38.6 | 31.4 | 9.7 | 9.7 | 8.5 | 8.2 | 7.7 |
| Backup courier uses | 0 | 13.5 | 19.7 | 19.7 | 17.1 | 17.3 | 18.0 |
| Expired kg | 117 | 32 | 51 | 52 | 48 | 51 | 48 |
| Safety violations | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Planner p95 latency (ms) | 1.5 | 1.1 | 2.2 | 2.5 | 14.6 | 16.2 | 159 |

How to read it:
- **Learned wave sizing is the real gain.** RL and BC halve to third Relay's notifications per rescue (25 → 8–13) and cut backup calls. They also rescue slightly more food.
- **B1 still rescues the most food in this world** (paired difference vs Relay-RL: +3.9 ± 2.1 points). The cost is 5× the notifications per rescue and about 31 pings per volunteer per night. This data has short donor windows (30–120 min), so blasting wins on speed.
- **The main lever is Relay's weekly offer budget K = 10.** These volunteers arrive with up to 9 offers already that week. On 12 seeds:

  | K | Relay rescue | Relay notifications per rescue | Relay-RL rescue | Relay-RL notifications per rescue |
  |---|---|---|---|---|
  | 10 | 0.885 | 24 | 0.897 | 12 |
  | 20 | 0.909 | 39 | 0.906 | 11 |
  | ∞ | 0.914 | 41 | 0.908 | 11 |

  Raising K trades volunteer burnout for food; that's a policy choice, not a bug. Change it in `core.CFG["K"]`.
- **The learned acceptance model barely changes outcomes here.** The hand-set model was already decent on this data. Its value is calibrated P(claim) numbers on the console, and a model that retrains when new logs arrive.

To evaluate a subset of policies:

```bash
python sim.py bengaluru_strict 30 "B1,Relay-ML,Relay-RL"
```

The original synthetic scenarios still work:

```bash
python sim.py friday_night 30
```

## 6. Step 5: run the live app

```bash
python app.py
```

The first run seeds the Bengaluru recipients and volunteers from `relay_data/`. The dispatch policy defaults to `Relay-RL` and falls back to `Relay-ML`, then `Relay`, if model files are missing. You can pick a policy yourself:

```bash
RELAY_POLICY="Relay-RL lam=0.25" python app.py
```

Any name in `sim.POLICIES` works: `B1`, `Relay`, `Relay-ML`, `Relay-RL`, `Relay-RL lam=0.25`, `BC-greedy`. The console header shows the active policy.

On PowerShell, set the variable first:

```bash
$env:RELAY_POLICY="Relay-ML"; python app.py
```

Delete `relay.db` to reset the live state; it's rebuilt by replaying the event log.

## 7. Step 6: tests

```bash
python test_relay.py
```

The tests check:
- zero safety violations for every policy and scenario, including the learned ones;
- claims never reassigned without a cancellation;
- the budget K is respected;
- replaying the log gives the same state;
- the planner is deterministic;
- the wave maths;
- the simulator reproduces the logged `p_true` to 1e-9;
- the learned models load and give volunteer-specific probabilities.

## 8. Retraining on new data

Replace the CSVs in `relay_data/` with the same columns, then run:

```bash
python ml.py all
python test_relay.py
python sim.py bengaluru 30
```

If a new dataset fixes the relative-time bug (absolute offer times), change `load()` in `ml.py` so it doesn't add `post_t` again. The audit's first finding will then read about 0%, which is your signal.

Real field logs are what would validate everything. Swap the simulator's ground truth for them by running Relay in shadow mode first, as the report's pilot plan (§16.5) describes.

## 9. Knobs

| Where | What |
|---|---|
| `core.CFG` | K (weekly offer budget), τ\*, wave timeout, safety buffers, backup response time |
| `ml.py` | `LAMBDAS` (notification cost), `GAMMA`, `FQI_ITERS`, splits, `LATE_PRIOR` |
| `planner.RL_K` | Candidate wave sizes. Only extend this if the logs contain those sizes |
| `sim.DATA_CFG`, `sim.P_RESPOND` etc. | Data-world physics, each measured by the audit |

## 10. Honest limits

- The data comes from a generator, not the field. The simulator reuses that generator's recovered behaviour, so "the model is near the oracle" means near *this generator's* oracle.
- Late-night and escalation behaviour aren't in the logs (findings 6 and 10). Those parts come from rules and priors, not learning.
- FQI evaluation relies on the simulator. There's no off-policy estimate that can be trusted with this much distribution shift.
- BC runs at about 159 ms per decision (a 6-class tree ensemble). That's fine for dispatch but not free.
