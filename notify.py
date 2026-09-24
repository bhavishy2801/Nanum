"""Notifications: every moment a person cares about becomes an in-app note and (if they allow it) an email.

Who gets what
  Food donor   posted · driver on the way · picked up · delivered (receipt) · couldn't be rescued
  Driver       new offer · rescue confirmed (pickup address + map link) · thank-you after delivery · rescue ended
  Shelter      food coming (with the 4-digit HANDOVER CODE) · on its way now · received · driver changed
  Admin        a rescue needs a human · someone joined
  Everyone     welcome email after sign-up

Email transport, first one configured wins:
  BREVO_API_KEY   Brevo's HTTPS email API (port 443). Use it on hosts that block SMTP ports (Render free, HF Spaces).
  SMTP_HOST       SMTP (SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_SECURITY).
SMTP_FROM is the From address for either. With neither, emails are kept in the outbox as previews (admin -> System -> Mailbox).
Sending happens on a background thread with retries; nothing here blocks a web request.
"""
import html
import json
import queue
import smtplib
import ssl
import threading
import time
import urllib.error
import urllib.request
import uuid
from email.message import EmailMessage
from email.utils import formataddr, make_msgid, parseaddr

E = html.escape
BRAND = "#0f9d74"


# ---------------------------------------------------------------- email layout (inline CSS: email clients ignore <style>)

def layout(title, preheader, body, cta=None, footer_link=""):
    button = (f'<a href="{E(cta[1])}" style="display:inline-block;background:{BRAND};color:#ffffff;text-decoration:none;'
              f'font-weight:700;padding:12px 22px;border-radius:12px;font-size:15px">{E(cta[0])}</a>') if cta else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(title)}</title></head>
<body style="margin:0;padding:0;background:#f4f6fb;font-family:Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#0f172a">
<span style="display:none!important;opacity:0;color:transparent;height:0;width:0;overflow:hidden">{E(preheader)}</span>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f4f6fb;padding:28px 12px"><tr><td align="center">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px">
<tr><td style="padding:0 6px 16px"><table role="presentation" cellspacing="0" cellpadding="0"><tr>
<td style="width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,#0f9d74,#0ea5e9);background-color:{BRAND};color:#fff;font-weight:800;text-align:center;font-size:18px">R</td>
<td style="padding-left:10px;font-weight:800;font-size:19px;letter-spacing:-.02em">Relay</td></tr></table></td></tr>
<tr><td style="background:#ffffff;border:1px solid #e6eaf2;border-radius:18px;padding:28px">
<h1 style="margin:0 0 12px;font-size:22px;line-height:1.25;letter-spacing:-.02em">{E(title)}</h1>
<div style="font-size:15px;line-height:1.6;color:#334155">{body}</div>
{f'<div style="margin-top:22px">{button}</div>' if button else ''}
</td></tr>
<tr><td style="padding:16px 8px;font-size:12px;line-height:1.5;color:#64748b;text-align:center">
Relay rescues surplus food before the clock runs out.<br>
You get this because of your Relay account. {footer_link}</td></tr>
</table></td></tr></table></body></html>"""


def facts(rows):
    """A small two-column facts table."""
    return ('<table role="presentation" cellspacing="0" cellpadding="0" style="width:100%;margin:14px 0;border-collapse:collapse">'
            + "".join(f'<tr><td style="padding:8px 0;border-bottom:1px solid #eef2f7;color:#64748b;font-size:13px;width:42%">{E(k)}</td>'
                      f'<td style="padding:8px 0;border-bottom:1px solid #eef2f7;font-weight:600;font-size:14px">{v}</td></tr>' for k, v in rows)
            + "</table>")


def code_box(code):
    return (f'<div style="margin:16px 0;padding:16px;border-radius:14px;background:#e7f7f1;text-align:center">'
            f'<div style="font-size:12px;color:#0f766e;font-weight:700;letter-spacing:.06em;text-transform:uppercase">Handover code</div>'
            f'<div style="font-size:34px;font-weight:800;letter-spacing:.3em;color:{BRAND};padding-left:.3em">{E(code)}</div>'
            f'<div style="font-size:12px;color:#334155">Tell this to the driver when they arrive. It confirms the delivery.</div></div>')


def maps(loc):
    return f"https://www.google.com/maps/dir/?api=1&destination={loc[0]:.6f},{loc[1]:.6f}"


# ---------------------------------------------------------------- mailer (background thread, retries, outbox)

class PermanentMailError(Exception):
    """Retrying won't help (bad API key, unverified sender, invalid address)."""


class Mailer:
    def __init__(self, store, env):
        self.store = store
        self.host = env("SMTP_HOST", "")
        self.port = int(env("SMTP_PORT", "587") or 587)
        self.user = env("SMTP_USER", "")
        self.password = env("SMTP_PASSWORD", "")
        self.sender = env("SMTP_FROM", "") or (f"Relay <{self.user}>" if "@" in self.user else "Relay <no-reply@relay.local>")
        self.security = (env("SMTP_SECURITY", "") or ("ssl" if self.port == 465 else "starttls")).lower()
        self.brevo_key = env("BREVO_API_KEY", "")
        self.q = queue.Queue()
        self.last_error = None
        for doc in list(store.outbox.values()):    # resume emails that were queued when the app stopped
            if doc["status"] in ("queued", "retrying") and self.sends_to(doc["to"]):
                self.q.put(doc["id"])
        threading.Thread(target=self._worker, name="relay-mailer", daemon=True).start()

    @property
    def configured(self):
        return bool(self.brevo_key or self.host)

    def sends_to(self, to):
        """Real SMTP delivery? Demo accounts (@relay.demo) never leave the outbox: that domain doesn't exist."""
        return self.configured and not to.lower().endswith(".demo")

    def enqueue(self, to, subject, text, body_html, kind, meta=None):
        doc = {"id": uuid.uuid4().hex, "created": time.time(), "to": to, "subject": " ".join(subject.split()),
               "text": text, "html": body_html, "kind": kind, "meta": meta or {},
               "status": "queued" if self.sends_to(to) else "preview", "attempts": 0, "error": None, "sent_at": None}
        self.store.put_outbox(doc)
        if doc["status"] == "queued":
            self.q.put(doc["id"])
        return doc

    def resend(self, mid):
        with self.store.lock:
            doc = dict(self.store.outbox[mid])
        doc.update(status="queued" if self.sends_to(doc["to"]) else "preview", attempts=0, error=None)
        self.store.put_outbox(doc)
        if doc["status"] == "queued":
            self.q.put(mid)
        return doc

    def _message(self, doc):
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = doc["to"]
        msg["Subject"] = doc["subject"]
        msg["Message-ID"] = make_msgid(domain=(parseaddr(self.sender)[1].split("@")[-1] or "relay.local"))
        msg.set_content(doc["text"])
        msg.add_alternative(doc["html"], subtype="html")
        return msg

    def send_now(self, doc):
        if self.brevo_key:
            return self._send_brevo(doc)
        ctx = ssl.create_default_context()
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, context=ctx, timeout=20)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=20)
        with server:
            server.ehlo()
            if self.security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if self.user:
                server.login(self.user, self.password)
            server.send_message(self._message(doc))

    def _send_brevo(self, doc):
        name, addr = parseaddr(self.sender)
        body = {"sender": {"name": name or "Relay", "email": addr}, "to": [{"email": doc["to"]}],
                "subject": doc["subject"], "htmlContent": doc["html"], "textContent": doc["text"]}
        req = urllib.request.Request("https://api.brevo.com/v3/smtp/email", data=json.dumps(body).encode(), method="POST",
                                     headers={"api-key": self.brevo_key, "Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:240]
            if e.code in (400, 401, 403):
                raise PermanentMailError(f"Brevo {e.code}: {detail}") from None
            raise RuntimeError(f"Brevo {e.code}: {detail}") from None   # 429 / 5xx: retry

    def _worker(self):
        while True:
            mid = self.q.get()
            with self.store.lock:
                doc = dict(self.store.outbox.get(mid, {}))
            if not doc or doc["status"] == "sent":
                continue
            for wait in (0, 3, 15, 60):          # up to 4 attempts
                time.sleep(wait)
                doc["attempts"] += 1
                try:
                    self.send_now(doc)
                    doc.update(status="sent", sent_at=time.time(), error=None)
                    self.last_error = None
                    break
                except (smtplib.SMTPAuthenticationError, smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, PermanentMailError) as ex:
                    doc.update(status="failed", error=f"{type(ex).__name__}: {ex}"[:300])   # retrying won't help
                    self.last_error = doc["error"]
                    break
                except Exception as ex:
                    doc.update(status="retrying", error=f"{type(ex).__name__}: {ex}"[:300])
                    self.last_error = doc["error"]
            if doc["status"] == "retrying":
                doc["status"] = "failed"
            self.store.put_outbox(doc)

    def status(self):
        with self.store.lock:
            docs = list(self.store.outbox.values())
        counts = {}
        for d in docs:
            counts[d["status"]] = counts.get(d["status"], 0) + 1
        if self.brevo_key:
            mode, host, port, sec = "brevo", "api.brevo.com", 443, "https"
        else:
            mode, host, port, sec = ("smtp" if self.host else "preview"), self.host, self.port, self.security
        return {"mode": mode, "host": host, "port": port, "security": sec, "from": self.sender, "counts": counts, "last_error": self.last_error}


# ---------------------------------------------------------------- who gets which message

class Notifier:
    """Called for every event the app emits. Looks up the real people involved (simulated ones have no email)."""

    def __init__(self, store, mailer, state, clock, public_url, cat_names, cfg=None):
        import core
        self.store, self.mailer, self.S, self.clock, self.url, self.CAT = store, mailer, state, clock, public_url.rstrip("/"), cat_names
        self.cfg = cfg or core.CFG
        self.sent_keys = set()

    # -- people
    def _by_entity(self, entity):
        if not entity:
            return None
        return next((u for u in self.store.users.values() if u.get("entity") == entity and u.get("role") in ("volunteer", "shelter")), None)

    def _user(self, email):
        return self.store.users.get(email) if email else None

    def _admins(self):
        return [u for u in self.store.users.values() if u.get("role") == "admin"]

    # -- delivery of one message to one person
    def tell(self, user, kind, title, body_text, email=None, link="/", once=None):
        """In-app note always; email if the person allows it. `email` = (subject, text, html)."""
        if not user:
            return
        if once:
            key = (user["email"], kind, once)
            if key in self.sent_keys:
                return
            self.sent_keys.add(key)
        note = {"id": uuid.uuid4().hex, "email": user["email"], "kind": kind, "title": title, "body": body_text,
                "link": link, "created": time.time(), "read": False}
        self.store.put_note(note)
        prefs = user.get("prefs") or {}
        if email and prefs.get("email", True) and not (kind == "offer" and not prefs.get("offers", True)):
            self.mailer.enqueue(user["email"], email[0], email[1], email[2], kind, {"note": note["id"]})

    def _link(self, route):
        return f"{self.url}/#{route}"

    def _footer(self):
        return f'<a href="{self._link("settings")}" style="color:#64748b">Email settings</a>'

    def _food(self, d):
        return f"{d.kg:g} kg {self.CAT.get(d.category, d.category).lower()} (~{d.meals:.0f} meals)"

    # -- the event hook
    def on_event(self, e):
        k, S = e["type"], self.S
        if k == "DonationPosted":
            d = S.donations[e["d"]["id"]]
            u = self._user(d.owner)
            if u and u.get("role") == "donor":
                self.tell(u, "posted", "Donation posted", f"{self._food(d)} is live. We're finding a shelter and driver.",
                          self._mail_posted(u, d), link="/#mine", once=d.id)
        elif k == "OfferSent":
            o = S.offers[e["id"]]
            u = self._by_entity(o.v)
            if u:
                d = S.donations[o.d]
                self.tell(u, "offer", "New rescue offer", f"{self._food(d)}, {self._km(u, d)} away. Reply in the app.",
                          self._mail_offer(u, d, o), link="/#drive", once=o.id)
        elif k in ("OfferAccepted", "BackupDispatched"):
            d = S.donations[S.offers[e["offer"]].d] if k == "OfferAccepted" else S.donations[e["d"]]
            self._claimed(d)
        elif k == "PickedUp":
            d = S.donations[e["d"]]
            donor, shelter = self._user(d.owner), self._by_entity(d.recipient)
            if donor and donor.get("role") == "donor":
                self.tell(donor, "picked_up", "Your food is on its way", f"{self._food(d)} was picked up and is heading to {self._rname(d)}.",
                          self._mail_simple(donor, "Your food is on its way",
                                            f"<p>{E(self._driver_name(d, first=True))} picked up <b>{E(self._food(d))}</b> and is heading to <b>{E(self._rname(d))}</b>.</p>",
                                            ("Track it live", self._link("mine"))), link="/#mine", once=d.id)
            if shelter:
                self.tell(shelter, "on_the_way", "Food is on its way to you", f"{self._food(d)} from {d.donor} is on its way now. Code {d.code}.",
                          self._mail_simple(shelter, f"On its way now: {self._food(d)}",
                                            f"<p><b>{E(self._driver_name(d))}</b> has the food from <b>{E(d.donor)}</b> and is driving to you now.</p>{code_box(d.code or '----')}",
                                            ("Open Relay", self._link("shelter"))), link="/#shelter", once=d.id)
        elif k == "Delivered":
            self._delivered(S.donations[e["d"]])
        elif k in ("Expired", "Diverted"):
            d = S.donations[e["d"]]
            donor = self._user(d.owner)
            if donor and donor.get("role") == "donor":
                self.tell(donor, "lost", "We couldn't rescue your food", f"{self._food(d)}: {e.get('reason', 'no longer safe')}. Please dispose of it safely.",
                          self._mail_simple(donor, "We couldn't rescue this one",
                                            f"<p>We're sorry: <b>{E(self._food(d))}</b> couldn't reach a shelter in time ({E(e.get('reason') or 'no longer safe')}).</p>"
                                            "<p>For everyone's safety, please <b>don't serve it</b>. Dispose of it safely.</p>"
                                            "<p>Tip: posting as early as you know about surplus gives drivers more time.</p>", ("Open Relay", self._link("donate"))),
                          link="/#mine", once=d.id)
            driver = self._by_entity(d.volunteer)
            if driver:
                self.tell(driver, "job_ended", "Rescue cancelled", f"The rescue from {d.donor} was cancelled: {e.get('reason', '')}. Nothing more to do.",
                          None, link="/#drive", once=d.id)
        elif k == "ClaimCancelled":
            d = S.donations[e["d"]]
            shelter = self._by_entity(d.recipient)
            if shelter:
                self.tell(shelter, "driver_changed", "Driver changed", f"The driver for {d.donor}'s food cancelled; Relay is finding another. The old code no longer works.",
                          None, link="/#shelter")
        elif k == "EscalationRaised":
            d = S.donations[e["d"]]
            real = d.owner not in (None, "sim")   # ponytail: simulated city donations alert in-app only, so admins' inboxes stay usable
            for a in self._admins():
                self.tell(a, "escalation", "A rescue needs a human", f"{d.donor}: {e.get('reason', '')}. Act before {self.clock(e['L']) if e.get('L') else 'soon'}.",
                          self._mail_simple(a, f"Needs a human: {d.donor}",
                                            f"<p><b>{E(d.donor)}</b>, {E(self._food(d))}: <b>{E(e.get('reason') or '')}</b>.</p>"
                                            + facts([("Act before", E(self.clock(e["L"])) if e.get("L") else "as soon as possible"),
                                                     ("Safe to eat until", E(self.clock(d.safe_until))),
                                                     ("Shelter", E(self._rname(d)))])
                                            + "<p>Open the Live board to send a backup courier or call a regular driver.</p>",
                                            ("Open the Live board", self._link("board"))) if real else None,
                          link="/#board", once=d.id)

    # -- composite moments
    def _claimed(self, d):
        donor, driver, shelter = self._user(d.owner), self._by_entity(d.volunteer), self._by_entity(d.recipient)
        r = self.S.recipients.get(d.recipient)
        who = self._driver_name(d, first=True)
        if donor and donor.get("role") == "donor":
            self.tell(donor, "claimed", f"{who} is coming for your food", f"Pickup around {self.clock(d.eta_pick)}. Going to {self._rname(d)}.",
                      self._mail_simple(donor, f"{who} is coming for your food",
                                        f"<p>Good news: <b>{E(who)}</b> accepted your donation.</p>"
                                        + facts([("Food", E(self._food(d))), ("Pickup around", E(self.clock(d.eta_pick))), ("Going to", E(self._rname(d)))])
                                        + "<p>Please keep it hot (or cold) and packed until they arrive.</p>", ("Track it live", self._link("mine"))),
                      link="/#mine", once=f"{d.id}:{d.t_claim}")
        if driver:
            drop = "the coordinator will share the handover point" if (r and r.confidential) else f"{r.name}" if r else "the shelter"
            self.tell(driver, "job", "Rescue confirmed", f"Pick up {self._food(d)} from {d.donor} by {self.clock(self._pickup_by(d))}, then drop at {drop}.",
                      self._mail_job(driver, d, r), link="/#drive", once=f"{d.id}:{d.t_claim}")
        if shelter:
            self.tell(shelter, "incoming", "Food is coming to you", f"{self._food(d)} from {d.donor}. Handover code {d.code}.",
                      self._mail_simple(shelter, f"Food is coming: {self._food(d)}",
                                        f"<p><b>{E(self._driver_name(d))}</b> is bringing food from <b>{E(d.donor)}</b>.</p>"
                                        + facts([("Food", E(self._food(d))), ("Pickup around", E(self.clock(d.eta_pick))), ("Kept", E(d.holding))])
                                        + code_box(d.code or "----"), ("Open Relay", self._link("shelter"))),
                      link="/#shelter", once=f"{d.id}:{d.t_claim}")

    def _delivered(self, d):
        donor, driver, shelter = self._user(d.owner), self._by_entity(d.volunteer), self._by_entity(d.recipient)
        at = self.clock(d.t_drop)
        if donor and donor.get("role") == "donor":
            self.tell(donor, "delivered", "Delivered. Thank you!", f"{self._food(d)} reached {self._rname(d)} at {at}.",
                      self._mail_simple(donor, "Delivered. Thank you!",
                                        f"<p>Your food reached <b>{E(self._rname(d))}</b> at <b>{E(at)}</b>. About <b>{d.meals:.0f} meals</b> for people who needed them.</p>"
                                        + facts([("Food", E(self._food(d))), ("Delivered", E(at)), ("Receipt no.", E(d.id.upper()))])
                                        + "<p>Your receipt is ready for your records or CSR report.</p>",
                                        ("View receipt", f"{self.url}/receipt/{d.id}")), link=f"/receipt/{d.id}", once=d.id)
        if driver:
            self.tell(driver, "thanks", "Delivered. Thank you!", f"You delivered ~{d.meals:.0f} meals to {self._rname(d)}.",
                      self._mail_simple(driver, "You just rescued food. Thank you!",
                                        f"<p>You delivered <b>{E(self._food(d))}</b> from <b>{E(d.donor)}</b> to <b>{E(self._rname(d))}</b> at {E(at)}.</p>"
                                        "<p>That's about <b>" + f"{d.meals:.0f}" + " meals</b> that didn't go to waste.</p>", ("See your impact", self._link("vimpact"))),
                      link="/#vimpact", once=d.id)
        if shelter:
            self.tell(shelter, "received", "Handover confirmed", f"{self._food(d)} from {d.donor} received at {at}.",
                      self._mail_simple(shelter, "Handover confirmed",
                                        f"<p>You received <b>{E(self._food(d))}</b> from <b>{E(d.donor)}</b> at {E(at)}.</p>", ("Open Relay", self._link("received"))),
                      link="/#received", once=d.id)

    def welcome(self, u):
        role = u.get("role")
        what = {"donor": ("post surplus food in 30 seconds and follow it to a shelter.", "donate", "Post your first donation"),
                "volunteer": ("get rescue offers that fit you. Go online in the app to receive them.", "drive", "Open My rescues"),
                "shelter": ("tell Relay how much food you can take tonight, and confirm handovers with a code.", "shelter", "Set tonight's space"),
                "admin": ("see the whole city, every rescue and how Relay decides.", "board", "Open the Live board")}.get(role)
        if not what:
            return
        self.tell(u, "welcome", "Welcome to Relay", f"You're set up. You can {what[0]}",
                  self._mail_simple(u, "Welcome to Relay",
                                    f"<p>Hi {E((u.get('name') or '').split(' ')[0] or 'there')},</p><p>You're all set. With Relay you can {E(what[0])}</p>"
                                    "<p>We'll email you at each important moment. You can change that any time in Email settings.</p>",
                                    (what[2], self._link(what[1]))), link=f"/#{what[1]}", once="welcome")

    def test(self, u):
        subject = "Relay test email"
        body = "<p>If you can read this, email from Relay works.</p>" + facts([("Sent at", E(time.strftime("%H:%M:%S"))), ("Transport", {"brevo": "Brevo API", "smtp": "SMTP"}.get(self.mailer.status()["mode"], "preview (no email service configured)"))])
        return self.mailer.enqueue(u["email"], subject, _text(subject, body), layout(subject, "Relay email check", body, None, self._footer()), "test")

    # -- helpers
    def _rname(self, d):
        r = self.S.recipients.get(d.recipient)
        return r.name if r else "a shelter"

    def _driver_name(self, d, first=False):
        if d.volunteer == "backup":
            return "Our backup courier"
        v = self.S.volunteers.get(d.volunteer)
        n = v.name if v else "A driver"
        return n.split(" ")[0] if first else n

    def _km(self, u, d):
        import core
        v = self.S.volunteers.get(u.get("entity"))
        return f"{core.km(v.loc, d.loc):.1f} km" if v else "nearby"

    def _pickup_by(self, d):
        import core
        r = self.S.recipients.get(d.recipient)
        return core.latest_pickup(d, r, self.cfg) if r else d.b

    def _mail_simple(self, u, subject, body, cta=None):
        return subject, _text(subject, body, cta), layout(subject, _plain_text(body)[:120], body, cta, self._footer())

    def _mail_posted(self, u, d):
        r = self.S.recipients.get(d.recipient)
        body = (f"<p>Thanks! Your donation is live.</p>"
                + facts([("Food", E(self._food(d))), ("Safe to eat until", E(self.clock(d.safe_until))),
                         ("Going to", E(r.name) if r else "finding the right shelter")])
                + "<p>We'll email you when a driver accepts, when it's picked up and when it's delivered.</p>")
        return self._mail_simple(u, f"Posted: {self._food(d)}", body, ("Track it live", self._link("mine")))

    def _mail_offer(self, u, d, o):
        body = (f"<p>There's food that needs rescuing near you.</p>"
                + facts([("Food", E(self._food(d))), ("Distance", E(self._km(u, d))), ("Must be eaten by", E(self.clock(d.safe_until))),
                         ("Reply by", E(self.clock(o.expires)) if o.expires < 1e8 else "as soon as you can")])
                + "<p>The exact address appears in the app after you accept.</p>")
        return self._mail_simple(u, f"Rescue offer: {self._food(d)} nearby", body, ("Accept or decline", self._link("drive")))

    def _mail_job(self, u, d, r):
        drop = (facts([("Drop-off", "Confidential site: the coordinator will share the handover point")]) if r and r.confidential else
                facts([("Drop-off", E(r.name)), ("Directions", f'<a href="{E(maps(r.loc))}" style="color:{BRAND}">Open in Google Maps</a>')]) if r else "")
        body = (f"<p>You're on it. Here are the details.</p>"
                + facts([("Pick up", E(self._food(d))), ("From", E(d.donor)), ("Pick up by", E(self.clock(self._pickup_by(d)))),
                         ("Directions", f'<a href="{E(maps(d.loc))}" style="color:{BRAND}">Open in Google Maps</a>')])
                + drop + "<p>At pickup, check the food is still hot/cold and packed. At the shelter, ask for their <b>4-digit handover code</b> and enter it in the app.</p>")
        return self._mail_simple(u, f"Confirmed: pick up from {d.donor}", body, ("Open My rescues", self._link("drive")))


def _plain_text(body_html):
    import re
    txt = re.sub(r"<(br|/p|/tr|/div)[^>]*>", "\n", body_html)
    txt = re.sub(r"<td[^>]*>", " ", txt)
    return html.unescape(re.sub(r"<[^>]+>", "", txt)).replace("\n\n\n", "\n\n").strip()


def _text(subject, body_html, cta=None):
    return f"{subject}\n\n{_plain_text(body_html)}" + (f"\n\n{cta[0]}: {cta[1]}" if cta else "") + "\n\n-- Relay"
