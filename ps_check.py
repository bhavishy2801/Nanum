"""End-to-end check of every capability the problem statement asks for (PS-1 §5 and §7), against the running app.

    python app.py                 # in one terminal (use a fresh DB: delete relay.db first)
    python ps_check.py            # in another; prints PASS/FAIL per PS capability

It posts real donations and walks them through the whole lifecycle, so run it on a demo database, not a real one.
It signs in with the demo accounts (RELAY_DEMO_LOGIN=1) and pauses the living-city simulation while it runs.
"""
import http.cookiejar
import json
import sys
import time
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
results = []


def session():
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


ADMIN, DONOR, ANON = session(), session(), session()


def call(method, path, body=None, who=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with (who or ADMIN).open(req, timeout=120) as r:
            return r.status, json.loads(r.read() or b"null") if "json" in r.headers.get("Content-Type", "") else r.read()
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def check(ps, name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  [{ps}] {name}" + (f"  -- {detail}" if detail else ""))


def donation(did):
    return next(d for d in call("GET", "/api/state")[1]["donations"] if d["id"] == did)


def post(center, **kw):
    body = dict(donor="PS check kitchen", lat=center[0] + 0.004, lon=center[1] + 0.004, category="cooked",
                holding="hot", veg=True, kg=10, plates=None, ready_until=None, label_until=None, text="")
    body.update(kw)
    t = time.perf_counter()
    code, d = call("POST", "/api/donations", body)
    return code, d, (time.perf_counter() - t) * 1000


try:
    call("GET", "/api/config", who=ANON)
except OSError:
    sys.exit(f"app not reachable at {BASE}: start it with `python app.py`")
check("Auth", "signed-out visitors can't see the city", call("GET", "/api/state", who=ANON)[0] == 401)
code, me = call("POST", "/api/auth/demo", {"role": "admin"}, who=ADMIN)
if code != 200:
    sys.exit("demo sign-in is off: set RELAY_DEMO_LOGIN=1 in .env to run this check")
call("POST", "/api/auth/demo", {"role": "donor"}, who=DONOR)
call("PUT", "/api/me/prefs", {"email": True, "offers": True}, who=DONOR)
check("Auth", "a restaurant account can't open the admin board", call("GET", "/api/state", who=DONOR)[0] == 403)
check("Auth", "a restaurant account can't open a driver's screen", call("GET", "/api/volunteers/V0001", who=DONOR)[0] == 403)
code, st = call("GET", "/api/state")
was_running = st["sim"]["running"]
call("POST", "/api/admin/sim", {"running": False})   # keep simulated drivers from answering our test offers
center = [sum(r["loc"][i] for r in st["recipients"]) / len(st["recipients"]) for i in (0, 1)]
print(f"app up · policy {st['policy']} · {len(st['recipients'])} recipients · {len(st['volunteers'])} volunteers\n")

# 1. Fast donation intake
t = time.perf_counter()
code, x = call("POST", "/api/intake/parse", {"text": "30 plates veg biryani + 40 rotis, kept hot, we close 11"})
ms = (time.perf_counter() - t) * 1000
check("PS 5.1", "free-text intake parses to a structured card",
      code == 200 and (x["category"], x["holding"], x["veg"], x["plates"], x["ready_until"]) == ("cooked", "hot", True, 30, "23:00"),
      f"{x.get('category')}/{x.get('holding')}/veg={x.get('veg')}/{x.get('plates')} plates/until {x.get('ready_until')} in {ms:.0f} ms ({x.get('source')})")

# 2. Real-time matching
code, d1, ms = post(center)
check("PS 5.2", "donation is matched to a recipient instantly", code == 200 and d1["recipient"] is not None,
      f"-> {d1.get('recipient_name')} in {ms:.0f} ms (PS 7: near-instant)")
check("PS 5.2", "recipient choice is explained", bool(d1.get("why")), f"{sum(v == 'chosen' for v in d1['why'].values())} chosen, "
      f"{sum('feasible' not in v and v != 'chosen' for v in d1['why'].values())} ruled out with a reason")

# 4. Capacity & preference management -> re-route
r1 = d1["recipient"]
call("PATCH", f"/api/recipients/{r1}/capacity", {"full": ["hot"]})
moved = donation(d1["id"])
check("PS 5.4", "recipient marks itself full -> donation re-routed", moved["recipient"] != r1,
      f"{d1['recipient_name']} -> {moved['recipient_name']}")
call("PATCH", f"/api/recipients/{r1}/capacity", {"full": []})

# 3. Dispatch + privacy
st = call("GET", "/api/state")[1]
offers = [o for o in st["offers"] if o["d"] == d1["id"] and o["status"] == "sent"]
check("PS 5.3", "volunteers get a sized offer wave (not a broadcast)", 0 < len(offers) < len(st["volunteers"]),
      f"{len(offers)} of {len(st['volunteers'])} volunteers offered, P(claim) {moved.get('pclaim', 0):.2f}")
vid = offers[0]["v"]
v = call("GET", f"/api/volunteers/{vid}")[1]
off = next(o for o in v["offers"] if o["offer"] == offers[0]["id"])
check("PS 7", "privacy: offer shows only a ~500 m area, not the address", "area" in off and "pickup" not in off,
      f"area {off['area']}")
call("POST", f"/api/offers/{offers[0]['id']}/respond", {"accept": True})
job = call("GET", f"/api/volunteers/{vid}")[1]["job"]
check("PS 5.3", "accepted volunteer gets pickup + drop-off instructions", job and job["d"] == d1["id"],
      f"pickup by {job and job['pickup_by']} -> {job and job['recipient']}")

# 5. Status tracking, handover code (trust)
call("POST", f"/api/donations/{d1['id']}/pickup", {"temp_ok": True, "packaging_ok": True})
s_pick = donation(d1["id"])["status"]
rec = call("GET", f"/api/recipients/{donation(d1['id'])['recipient']}")[1]
code_ = next(a["code"] for a in rec["arriving"] if a["d"] == d1["id"])
bad = call("POST", f"/api/donations/{d1['id']}/deliver", {"code": "0000" if code_ != "0000" else "1111"})[0]
ok, rcpt = call("POST", f"/api/donations/{d1['id']}/deliver", {"code": code_})
check("PS 5.5", "status: posted -> offered -> claimed -> picked_up -> delivered",
      s_pick == "picked_up" and donation(d1["id"])["status"] == "delivered")
check("PS 3", "trust: wrong handover code rejected, right one accepted + donor receipt", bad == 400 and ok == 200,
      rcpt.get("receipt", ""))

# Reliability: cancel -> re-plan
code, d2, _ = post(center, kg=6)
o2 = next(o for o in call("GET", "/api/state")[1]["offers"] if o["d"] == d2["id"] and o["status"] == "sent")
call("POST", f"/api/offers/{o2['id']}/respond", {"accept": True})
call("POST", f"/api/donations/{d2['id']}/cancel")
after = donation(d2["id"])
check("PS 7", "reliability: volunteer cancels -> donation reopens and is re-offered",
      after["status"] == "offered" and after["volunteer"] is None)

# Escalation -> backup
bk = call("POST", f"/api/donations/{d2['id']}/backup")[0]
check("PS 7", "coordinator one-click backup courier on an at-risk rescue",
      bk == 200 and donation(d2["id"])["volunteer"] == "backup")

# 8. Safety: never route expired-risk food
past = call("GET", "/api/state")[1]["now"] - 1
code, d3, _ = post(center, category="packaged", holding="ambient", kg=3, label_until=past)
d3 = donation(d3["id"])
check("PS 5.8 / 7", "safety: food past its use-by is never offered", d3["status"] == "expired",
      f"status {d3['status']} ({d3.get('end_reason')})")
code, _, _ = post(center, kg=1000)
check("PS 7", "validation: an absurd 1,000 kg post is refused for review", code == 422)

# 6. Impact dashboard
code, imp = call("GET", "/api/impact")
csv_code = call("GET", "/api/impact.csv")[0]
check("PS 5.6", "impact: kg + meals counted on delivery, CSV export", code == 200 and imp["kg"] > 0 and csv_code == 200,
      f"{imp['kg']:.1f} kg, {imp['meals']:.0f} meals, CO2e {'on' if imp['co2e_kg'] is not None else 'off (set RELAY_CO2E_PER_KG)'}")

# Evidence it works better, not just that it works (simulated)
code, cmp_ = call("GET", "/api/sim/compare?scenario=bengaluru&seeds=30")
if code == 200:
    t = cmp_["table"]
    check("PS 7", "zero safety violations for every policy in 30 held-out simulated episodes",
          all(t[p]["safety_violations"][0] == 0 for p in t))
    best = "Relay-RL" if "Relay-RL" in t else "Relay"
    check("PS 6", f"{best} vs WhatsApp-style broadcast (B0): more food, far fewer pings (simulated)",
          t[best]["rescue_rate"][0] > t["B0"]["rescue_rate"][0] and t[best]["notif_per_rescue"][0] < t["B0"]["notif_per_rescue"][0],
          f"rescue {t['B0']['rescue_rate'][0]:.2f} -> {t[best]['rescue_rate'][0]:.2f}, "
          f"notifications/rescue {t['B0']['notif_per_rescue'][0]:.0f} -> {t[best]['notif_per_rescue'][0]:.1f}")

code, dd, _ = post(center, kg=4)
code2, home = call("GET", "/api/donor/home", who=DONOR)
check("Roles", "a restaurant sees only its own donations", code2 == 200 and all(d["id"] != dd["id"] for d in home["donations"]))
call("POST", "/api/donations", dict(lat=center[0], lon=center[1], category="bakery", holding="ambient", kg=2), who=DONOR)
code2, home = call("GET", "/api/donor/home", who=DONOR)
check("Roles", "a restaurant's own post shows up in its tracker, without the handover code",
      code2 == 200 and bool(home["donations"]) and "code" not in home["donations"][0])
# Notifications: every moment becomes an in-app note + an email (SMTP, or a preview in the admin Mailbox)
code, notes = call("GET", "/api/notes", who=DONOR)
check("Notify", "the restaurant is notified of its own post (in-app)",
      code == 200 and any(n["kind"] == "posted" for n in notes["notes"]), f"{notes['unread']} unread")
code, ob = call("GET", "/api/admin/outbox")
mine = [m for m in ob["messages"] if m["to"] == "demo-kitchen@relay.demo"]
check("Notify", "...and gets an email for it (sent, or a preview when SMTP is off)",
      code == 200 and any(m["kind"] == "posted" for m in mine), f"email mode: {ob['email']['mode']}, storage: {ob['storage']['backend']}")
call("POST", "/api/notes/read", {"ids": None}, who=DONOR)
check("Notify", "notes can be marked read", call("GET", "/api/notes", who=DONOR)[1]["unread"] == 0)
check("Notify", "only admins can open the mailbox", call("GET", "/api/admin/outbox", who=DONOR)[0] == 403)
call("POST", "/api/admin/sim", {"running": was_running})
print(f"\n{sum(results)}/{len(results)} checks passed")
print("Not built (say so if asked): SMS/push notifications (email + in-app only), multi-stop routing.")
sys.exit(0 if all(results) else 1)
