# Relay: deadline-aware food rescue dispatch

AmiHacks Track A, PS-1. Built from `report_ps1.pdf`. Section numbers (§) below refer to that report.

```
pip install -r requirements.txt
python ml.py all                     # audit relay_data/, train acceptance model + BC + offline RL -> models/
python test_relay.py                 # property checks (§17.2) + data-world checks
python sim.py bengaluru 30           # evaluate all policies on 30 held-out episodes of relay_data
python app.py                        # http://127.0.0.1:8000 (Bengaluru, dispatching with Relay-RL)
```

**Testing each PS feature and presenting: [DEMO.md](DEMO.md)** (`python ps_check.py` against the running app gives 16 PASS/FAIL checks).
**The ML/RL guide is in [GUIDE.md](GUIDE.md)**: the data audit, model results, how to run, retrain and tune.

Delete `relay.db` to reset the live demo. It holds the event log, and state is rebuilt by replaying it.

## Files

| File | What it is |
|---|---|
| `core.py` | Config (Appendix B), safety clock (Appendix A), feasibility incl. use-lead μ_r (§9.2), L_d (§9.4), acceptance model (§6.3), event projector |
| `planner.py` | `plan(state, now, cfg) -> events`. Relay stages 0–2, plus baselines B0 (broadcast) and B1 (412FR static tiered) |
| `sim.py` | Discrete-event simulator with common random numbers. Synthetic Noida scenarios, plus `bengaluru` / `bengaluru_strict` built from relay_data. §11.2 metrics, paired comparison |
| `intake.py` | Free text -> draft card. Regex parser always runs; Claude extraction is optional (3 s timeout, cross-checked, conservative merge) |
| `app.py` | FastAPI + append-only SQLite event log. Re-plans on every change and every 60 s |
| `ml.py` | Data audit and fixes, acceptance model, decision dataset, behaviour cloning, fitted Q iteration |
| `models/` | Trained models and reports written by `ml.py` |
| `index.html` | Coordinator console (map, risk queue by slack, P(claim), L_d, backup button, "why this recipient"), Donor, Volunteer, Recipient, Impact, Simulation lab |

## Results on the synthetic city (simulated, friday_night, 30 seeds, mean ± 95% CI)

For the dataset results (Bengaluru), see GUIDE.md §5.

These come from simulation only. The only calibration is B1 matched to the one published anchor (16.4% negative rescues [R1]). They are not real-world results.

| | B1 (412FR rule) | Relay | Relay − B1 (paired) |
|---|---|---|---|
| rescue rate (kg) | 0.877 | 0.906 | +0.029 ± 0.026 |
| notifications / rescue | 50.9 | 9.2 | −41.7 ± 2.9 |
| late escalations | 1.03 | 0.17 | −0.87 ± 0.43 |
| expired kg | 30.1 | 21.9 | −8.2 ± 5.2 |
| backup courier uses | 1.7 | 2.5 | **+0.8 ± 0.5 (cost)** |
| median time to claim | 2.3 min | 16.4 min | **+14 min (slower)** |
| safety violations | 0 | 0 | 0 |

The ablations are in the Simulation lab table. Swapping the p model for a constant p costs 7 more notifications per rescue and adds 2 late escalations. The "−early esc" ablation rescues the same food, because the simulated coordinator acts at L_d either way. It only loses escalation lead time.

## Deliberately not built (ponytail)

- **Stage 3 insertion / OR-Tools PDPTW and B2.** Add them when multi-stop matters.
- **OSRM.** Uses haversine × 1.35 instead.
- **Real OSM layout.** The synthetic scenarios use a made-up city around Noida; the `bengaluru` scenarios use relay_data's coordinates.
- **Telegram/WhatsApp.** A web chat box stands in.
- **Auth.** Add magic links or OTP before any real pilot.
- **WebSocket.** The UI polls every 3 s.
- **Bootstrap CIs.** Uses a normal approximation.

## Knobs

- `RELAY_CO2E_PER_KG` shows CO2e only when set, and you should cite the factor's source.
- `RELAY_LLM_MODEL` defaults to `claude-opus-5`. LLM intake needs `pip install anthropic` and credentials.
- `RELAY_DB` sets the event-log path.
- `RELAY_POLICY` sets the live dispatch policy (default `Relay-RL`; see GUIDE.md §6).
