# Testing and presenting Relay (PS-1: Surplus-to-Shelter)

## 1. Prove every PS feature works (2 minutes)

```bash
del relay.db            # PowerShell. On bash: rm relay.db
python app.py
```

Then, in a second terminal:

```bash
python ps_check.py
```

`ps_check.py` drives the live API through every capability the problem statement lists and prints `16/16 checks passed`. **Screenshot that output for a slide.** Afterwards, delete `relay.db` again so the demo starts clean.

The deeper checks are these (GUIDE.md explains them):

```bash
python test_relay.py
```

```bash
python sim.py bengaluru 30
```

### PS requirement → where it lives → how to show it

| PS asks for | Relay feature | Show it by | Verified by |
|---|---|---|---|
| §5.1 Fast intake (under a minute) | Paste free text → parsed card → one-tap post | Donor tab: paste, click **Read message**, click **Post** | ps_check: parse ~25 ms, post ~50 ms |
| §5.2 Real-time matching on capacity, need and distance | Stage-1 recipient scoring: fill-rate equity, travel, slack, safety | Console card → **Why this recipient?** | ps_check |
| §5.3 Dispatch / routing suggestion | Sized offer wave (RL-learned) to the best volunteers; route line on the map | Console map line; Volunteer tab offer → accept → pickup-by time and drop-off | ps_check: 2 of 150 volunteers offered, not a broadcast |
| §5.4 Capacity and preferences | Per-class capacity, "full" toggles, veg-only, use-lead | Recipient tab: tick **full for hot** → console shows the re-route | ps_check: R004 → R005 |
| §5.5 Status tracking | posted → offered → claimed → picked up → delivered, plus expired and diverted | Console badges; donor card updates live | ps_check |
| §5.6 Impact dashboard | kg and meals counted only on confirmed delivery; per recipient and per zone; CSV | Impact tab → **Download CSV** | ps_check |
| §5.7 Notifications | **In-app only**: volunteer offers, coordinator escalation alerts, donor status | Volunteer tab, red escalation cards | *SMS, email and push are not built. Say so.* |
| §5.8 Optional: safety / expiry-risk scoring | Rule-based safety clock (never ML), live P(claim), slack, escalation deadline L_d | Console card: countdown, P(claim) bar, L_d | ps_check: food past its use-by is never offered |
| §7 Safety | Deadline is when food is **used**, not delivered (use-lead) | Why-not text: "next service is 8.0 h after arrival" | test_relay: 0 violations across all policies and scenarios |
| §7 Privacy | ~500 m area until acceptance; confidential shelters hidden | Volunteer offer card | ps_check |
| §7 Reliability | Cancel → automatic re-offer; one-click backup; event-log replay | Volunteer **I can't make it**; console **Assign backup** | ps_check, test_relay |
| §7 Near-instant | Planner p95 is 2–16 ms | Simulation lab table (latency row) | sim.py |
| §6 AI/ML/RL (only where it helps) | Acceptance model at the oracle ceiling; offline RL for wave size | GUIDE.md §3–5 | ml.py reports |

**CO₂e:** the PS asks for it, and it's off by default because a number without its source is a red flag. Pick one factor, cite it, and start the app with it:

```bash
$env:RELAY_CO2E_PER_KG="<factor>"; python app.py
```

Put the factor's source on the slide.

---

## 2. Before you present (checklist)

- [ ] `del relay.db`, `python app.py`, then open http://127.0.0.1:8000. The first start takes a few seconds while the models load.
- [ ] `results_bengaluru.json` exists, so the 30-seed table loads instantly instead of recomputing.
- [ ] Internet is available for the map tiles. Without it the page says "Map offline" and **everything else still works**. Hotspot recommended.
- [ ] Browser zoom at 125% so the back row can read it.
- [ ] Record a backup video of the demo below. Use it if anything breaks.
- [ ] Rehearse the demo twice; it takes 3 minutes.

---

## 3. The 5-minute pitch

| Time | Say | Show |
|---|---|---|
| 0:00–0:25 **Hook** | "It's 10:45 on a Friday. A restaurant has 30 plates of biryani and 4 hours before it's unsafe. A shelter wants it, and a volunteer lives 2 km away. It still gets binned, not because nobody cares, but because nobody *commits* in time." | Title slide |
| 0:25–1:00 **Insight** | "We thought this was matching. It isn't: platforms report 99% match rates. In the best public data (412 Food Rescue), 1 in 6 rescues went unclaimed or needed a last-hour phone call. **You can't dispatch a volunteer. You can only ask.**" | One statistic |
| 1:00–1:15 **Product** | "Relay is a co-pilot for the rescue coordinator. For every donation it answers: will this food make it, and if not, when must a human step in?" | Console |
| 1:15–3:00 **Live demo** | Script below | App |
| 3:00–3:50 **How it works** | "Three clocks: food safety, donor and recipient. Safety is a fixed rule and never ML. An ML model predicts who will say yes; it's at the accuracy ceiling on held-out data. Offline RL learns how many people to ask. And an escalation deadline, independent of ML, alerts the coordinator while there's still time." | One architecture slide |
| 3:50–4:30 **Evidence** | "On 30 held-out episodes from the dataset, Relay-RL rescues 90% of the food with **12.7 notifications per rescue, versus 145** for a WhatsApp broadcast, with zero safety violations. The 412-style rule rescues 4 points more, but needs 5× the pings, and its busiest volunteers get about 31 per night. That's the burnout trade-off, and it's one parameter." | Results table, labelled "simulated" |
| 4:30–5:00 **Close** | "Pilot: our campus dining hall and one NGO chapter, 6 weeks in shadow mode first. Relay makes every donation someone's job, before the clock runs out." | Closing slide |

### Live demo script (1:45)

1. **Donor tab.** Paste `30 plates veg biryani + 40 rotis, kept hot, we close 11` and click **Read message**. The card fills in (cooked, hot, veg, 30 plates, until 23:00). Click **Post**. *Say:* "Under a minute, from the message they already type."
2. **Coordinator tab.** The new card shows the safety countdown, P(claim) and the escalation time. Open **Why this recipient?** and point at a shelter ruled out because its "next service is 8.0 h after arrival". *Say:* "The safety deadline is when food is eaten, not when it's delivered. Naive systems miss this."
3. **Recipient tab.** For the chosen shelter, tick **full for hot** and click **Save**. Back on the console, the donation has moved to another shelter. *Say:* "Plans change, so Relay re-plans."
4. **Volunteer tab.** Pick the volunteer who has the offer. It shows only a ~500 m area and the distance. Click **Accept**; the full address appears. Tick the checklist and click **Picked up**. Get the 4-digit code from the Recipient tab, enter it and click **Delivered**. A receipt pops up. *Say:* "Only a handful of the 150 volunteers were pinged."
5. **Impact tab.** kg and meals have updated, and the CSV is ready for CSR reporting.
6. **Simulation lab.** Choose scenario `bengaluru`, click **Same-seed replay**, and let it play for 20 s. Then click **30-seed comparison**. *Say:* "Same donations, same volunteers, different policy. Every number here is simulated, and we say so."

---

## 4. Hard questions: answer honestly

| Question | Answer |
|---|---|
| "Your results are simulated." | "Yes. The simulator replays real donation streams from held-out episodes, and its volunteer model reproduces the dataset's own probabilities exactly; there's a test for that. Field proof is the 6-week shadow pilot." |
| "B1 rescues more food than you." | "On this dataset, yes: 4 points, because donors are only available 30–120 minutes and blasting wins on speed. It costs 5× the notifications. Lifting our per-volunteer budget closes about half the gap (0.885 → 0.914) at the cost of more pings; the trade-off table is in the guide. We'd rather decide burnout policy with the NGO than hide it." |
| "Why RL?" | "Only for wave size, where it halved notifications. Safety and escalation stay rules: the logs had zero escalations to learn from, and a wrong model must never make food unsafe." |
| "Is the ML real?" | "The acceptance model scores log loss 0.2665 against a perfect-information ceiling of 0.2660 on held-out episodes, and its predictions are served exactly as trained." |
| "What was wrong with the data?" | "Five issues: relative timestamps, an RL file built on them, empty behaviour-cloning labels, the oracle probability leaked as `p_hat`, and a broken late-night flag. We measured each one and rebuilt from the raw logs." |
| "Notifications?" | "In-app today. SMS or WhatsApp is the next integration; the planner already produces the messages." |
| "What does the LLM do?" | "Extraction only, with a regex fallback and a confirm card. It never makes a safety call." |
| "Scale?" | "The planner runs in single-digit milliseconds. Rescue is local, so we partition by city zone." |

**Never claim:** "reduces food waste by X% in the real world", or "predicts volunteers with Y% accuracy" in the field. And don't show a CO₂e figure without its source.
