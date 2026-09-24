# Relay: set up, run, test, present

This is the complete playbook. The ML and RL internals are in [GUIDE.md](GUIDE.md).

---

## 1. One-time setup (10 minutes)

### 1.1 Install

```bash
pip install -r requirements.txt
```

### 1.2 Google sign-in (OAuth)

1. Open https://console.cloud.google.com/apis/credentials, then **Create credentials → OAuth client ID**. If asked, configure the consent screen first: External, with an app name and your email.
2. Choose **Application type: Web application**.
3. Under **Authorized JavaScript origins**, add `http://localhost:8000` and `http://127.0.0.1:8000`, plus your real domain if you deploy. No redirect URI is needed: Relay uses Google's pop-up button, and the server verifies the ID token with Google.
4. Copy the **Client ID** into `.env`:

```
GOOGLE_CLIENT_ID=xxxxxxxx.apps.googleusercontent.com
RELAY_ADMINS=you@gmail.com,teammate@gmail.com     # these Google accounts become admins
RELAY_DEMO_LOGIN=1                                # one-click demo accounts; set 0 for a real launch
RELAY_CARTO_KEY=<your CARTO basemap key>          # removes the "API KEY REQUIRED" map watermark
RELAY_POLICY=Relay-RL
RELAY_SIM=1                                       # the living city (see §3)
```

`.env` is excluded from git. The client ID and the CARTO key are meant to be public, because the browser needs them. Keep your client **secret** out of this project entirely; Relay doesn't use it.

### 1.3 Models and cached results

Skip this if `models/` already holds the trained files.

```bash
python ml.py all
```

```bash
python sim.py bengaluru 30
```

---

## 2. Run it

```bash
python app.py
```

Open **http://localhost:8000**.

- **Reset the city:** stop the app, delete `relay.db`, and start it again. This also removes all accounts.
- **Different port:** `$env:PORT="8010"; python app.py`. Add that origin in Google Cloud too.

---

## 3. Who sees what

| Role | How they get it | What they see |
|---|---|---|
| **Food donor** (restaurant, food chain outlet, kiosk, caterer, bakery, grocer, hotel, campus dining) | Sign in with Google, then choose "I have surplus food" | **Donate food** (paste, check, post), **My donations** (live map and 5-step tracker), **My impact** |
| **Driver** | Google, then "I can drive food" (name, vehicle, start area) | **My rescues** (online/offline switch, offers, the current job with a pickup checklist and the handover code, map), **My impact** |
| **Shelter / kitchen** | Google, then "I run a shelter" (manage an existing site or register a new one) | **Tonight** (free space per food type, "Full" switches, arriving food with the 4-digit code), **Received** |
| **Admin** | Their Google email is in `RELAY_ADMINS` | Everything: **Live board**, **How Relay thinks**, **Donor / Driver / Shelter view** (act as anyone), **Simulation lab**, **Impact**, **System** |

The server enforces all of this. A restaurant can't open another restaurant's donations, the admin board, or a driver's screen; the API returns 403.

**The living city.** Everyone who isn't a signed-in person is simulated from the relay_data behaviour. Simulated restaurants post food, simulated drivers answer offers with the dataset's measured probabilities and delays (×10 speed), pick up and deliver, and shelters receive it. So any single role can be demoed end to end:
- a **restaurant** sees a real-looking driver claim and deliver its food;
- a **driver** gets offers from simulated restaurants;
- a **shelter** gets deliveries with codes.

Signed-in people's drivers and shelters are **never** auto-driven. Admins control the city on **System**: pause or resume, speed, how often food is posted, and "post one now".

---

## 4. Test that everything works

With the app running:

```bash
python ps_check.py
```

It should end with **21/21 checks passed**. It covers every problem-statement capability, plus sign-in and role isolation (signed-out visitors blocked; a restaurant can't see admin or driver data or other restaurants' donations). It signs in with the demo accounts and pauses the city while it runs.

```bash
python test_relay.py
```

These are the 7 property tests: safety, replay, budget, and the simulator against the data.

In the app, **System** shows live health checks: database, policy, models, planner speed, food safety, living city, Google sign-in, map key, and a warning while demo accounts are on. It also shows everyone who has signed in and a live activity stream (simulated actions are tagged "sim").

---

## 5. The live demo (about 3 minutes)

Before you start:
- delete `relay.db` and start `python app.py`;
- open the app full-screen at 110–125% zoom;
- run `python sim.py bengaluru 30` once beforehand;
- record a backup video.

Use Google sign-in, or the demo buttons if you're offline or short of time.

1. **Sign in as a restaurant** (Google, or the **Restaurant** demo). Tap the **🍛 Biryani, hot** example, then **Post donation**. The toast names the shelter.
   *Say:* "Thirty seconds, from the message a restaurant already types on WhatsApp."
2. **My donations.** Watch the tracker: *Matched → Claimed* (a simulated driver usually accepts within a minute), then *Picked up → Delivered*. The map shows the route.
3. **Sign out, then sign in as Admin.** On the **Live board**, open a card's **Why this shelter?**. Point at a shelter ruled out because its "next service is 8.0 h after arrival".
   *Say:* "Food must be safe when it's eaten, not just when it's delivered."
4. **How Relay thinks** (the showpiece, 90 seconds). Press **▶ Play tour**, or click through the 8 steps:
   1. food posted (the safety clock ring fills);
   2. rules test every shelter (✓/✕ with reasons) and pick one;
   3. the ML model's probability for each driver;
   4. the RL agent scores "ask 0/1/2/3/5/8" (bars rise, the winner glows);
   5. offers fly out, with the claim-odds maths;
   6. a sampled outcome (a driver accepts, the scooter delivers) and the reward ledger;
   7. training replay: the agent's opinion changing over 20 rounds as the error curve settles;
   8. results on 30 held-out nights.

   Then use **Try it yourself**: drag "Minutes until latest pickup" down and watch the agent start asking more drivers.
5. **Simulation lab.** Click **Replay one night** (412 rule vs Relay + RL side by side), then **Compare 30 nights**.
6. **System** (optional). All checks are green, and the activity stream shows the living city in real time.

---

## 6. The pitch (5 minutes)

| Time | Say |
|---|---|
| 0:00 | "It's 10:45 on a Friday. A restaurant has 30 plates of biryani and 4 hours before it's unsafe. It still gets binned, because nobody **commits** in time." |
| 0:25 | "This isn't a matching problem. In the best public data, 1 in 6 rescues went unclaimed or needed a last-hour call. **You can't dispatch a volunteer. You can only ask.**" |
| 1:00 | Demo, steps 1–3 |
| 2:00 | "How Relay thinks": rules for safety, ML for who'll say yes, RL for how many to ask, and a human alerted in time. |
| 3:30 | "On 30 held-out nights: 89.6% of food rescued with 12.7 pings per rescue, versus 145 for a WhatsApp broadcast, with zero safety violations. The 412-style rule rescues 4 points more but pings 5× as much. That's a burnout setting, not a hidden flaw." |
| 4:30 | "Every role signs in with Google and sees only its part. The pilot: our campus dining hall and one NGO chapter, 6 weeks in shadow mode." |

### Hard questions

| Question | Answer |
|---|---|
| "It's simulated." | "Yes, the city replays real donation streams, and our driver model reproduces the dataset's probabilities exactly; there's a test for that. The field proof is the shadow-mode pilot." |
| "Does the RL learn live?" | "No, it was trained offline on 31,502 logged decisions (the explainer replays that training). Live outcomes become new training data when we retrain. That's deliberate: nobody wants a policy changing itself mid-shift." |
| "Why RL at all?" | "Only for how many people to ask, where it halved the pings. Safety and escalation stay rules." |
| "Is sign-in secure?" | "Google ID tokens are verified with Google, including audience, issuer, expiry and verified email. Sessions are random tokens stored hashed, in HttpOnly SameSite cookies. Every API route checks the role and ownership." |
| "What about the demo buttons?" | "Development only. `RELAY_DEMO_LOGIN=0` turns them off, and the System page warns while they're on." |

**Never claim** real-world percentages, field accuracy, or a CO₂e number without its source.

---

## 7. Troubleshooting

| Problem | Fix |
|---|---|
| Google button missing, with a yellow note | `GOOGLE_CLIENT_ID` is empty in `.env`. Restart after setting it |
| Google says "origin not allowed" / `invalid_client` | Add the exact origin you're using (for example `http://localhost:8000`) under **Authorized JavaScript origins**, then wait a few minutes |
| Signed in but you're not an admin | Add your Google email to `RELAY_ADMINS`, restart, and sign in again |
| Map says "API KEY REQUIRED" | Set `RELAY_CARTO_KEY` |
| Nothing happens after a restaurant posts | Check **System**: the living city may be paused. Drivers also reply more slowly late at night (that's the data) |
| `Address already in use` | Another `python app.py` is running. Close it, or use `$env:PORT="8010"` |
| Someone should pick a different role | Admin → **System → People → Reset**. They choose again on next sign-in |
| Old UI after changes | A normal reload works, because the page and its files are cache-busted |
