"""Storage (SQLite + MongoDB) and notifications (who gets which message, real SMTP). Run: python test_store_notify.py

MongoDB is tested with mongomock (pip install mongomock) and SMTP with a local aiosmtpd server (pip install aiosmtpd);
each of those tests is skipped with a note if the package is missing. For a live MongoDB: python store.py check
"""
import os
import tempfile
import time

import core
import notify
import store

INF = core.INF


def _roundtrip(backend_factory):
    s = store.Store(backend_factory())
    s.event({"type": "Genesis", "t": 0, "base": 1.0})
    s.event({"type": "BackupSet", "t": 1, "loc": (12.9, 77.6)})           # tuple -> list, must survive
    s.put_user({"email": "a@x.com", "role": "donor", "prefs": {"email": False}})
    s.put_session("h1", "a@x.com", time.time() + 60)
    s.put_session("old", "a@x.com", time.time() - 1)                       # expired: must not come back
    s.put_note({"id": "n1", "email": "a@x.com", "created": 1.0, "read": False, "kind": "posted"})
    s.put_note({"id": "n1", "email": "a@x.com", "created": 1.0, "read": True, "kind": "posted"})   # update
    s.put_outbox({"id": "m1", "created": 2.0, "to": "a@x.com", "subject": "hi", "status": "sent", "kind": "test"})
    assert s.flush(), s.status()
    assert s.status()["error"] is None
    d = s.backend.load()
    assert [e["type"] for e in d["events"]] == ["Genesis", "BackupSet"] and d["events"][1]["loc"] == [12.9, 77.6]
    assert d["users"]["a@x.com"]["prefs"] == {"email": False}
    assert set(d["sessions"]) == {"h1"}
    assert len(d["notes"]) == 1 and d["notes"][0]["read"] is True
    assert d["outbox"][0]["subject"] == "hi"
    s2 = store.Store(s.backend)                                            # restart: continues the sequence
    assert s2.seq == 2 and s2.notes["a@x.com"][0]["read"] is True
    s2.event({"type": "BackupSet", "t": 2, "loc": (1, 2)})
    assert s2.flush() and len(s2.backend.load()["events"]) == 3


def test_sqlite_store():
    path = os.path.join(tempfile.mkdtemp(), "t.db")
    _roundtrip(lambda: store.SQLiteBackend(path))


def test_mongo_store():
    try:
        import mongomock
    except ImportError:
        print("  (skipped: pip install mongomock)")
        return
    from pymongo import DeleteOne, ReplaceOne

    def bulk_write(self, reqs, ordered=True):   # mongomock can't take pymongo 4.x request objects: apply them one by one
        for r in reqs:
            if isinstance(r, ReplaceOne):
                self.replace_one(r._filter, r._doc, upsert=r._upsert)
            elif isinstance(r, DeleteOne):
                self.delete_one(r._filter)
            else:
                raise TypeError(r)
    mongomock.collection.Collection.bulk_write = bulk_write
    client = mongomock.MongoClient()
    _roundtrip(lambda: store.MongoBackend("mongodb://unused", "relay_test", client=client))


def test_sqlite_migrates_old_users_table():
    import sqlite3
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    db = sqlite3.connect(path)
    db.execute("create table users (email text primary key, name text, picture text, role text, entity text, org text, kind text, lat real, lon real, created real)")
    db.execute("insert into users values ('o@x.com', 'Old', '', 'shelter', 'R001', 'Home', null, 1, 2, 5)")
    db.commit()
    db.close()
    u = store.SQLiteBackend(path).load()["users"]["o@x.com"]
    assert u["role"] == "shelter" and u["entity"] == "R001"


# ---------------------------------------------------------------- notifications

class FakeMailer:
    configured = True

    def __init__(self):
        self.sent = []

    def enqueue(self, to, subject, text, body_html, kind, meta=None):
        self.sent.append({"to": to, "subject": subject, "text": text, "html": body_html, "kind": kind})
        return self.sent[-1]


def _city(confidential=False):
    st = store.Store(store.SQLiteBackend(os.path.join(tempfile.mkdtemp(), "n.db")))
    S, mailer = core.State(), FakeMailer()
    n = notify.Notifier(st, mailer, S, lambda t: f"T{t:.0f}", "http://relay.test", {"cooked": "Cooked food"})
    for u in ({"email": "donor@x.com", "role": "donor", "name": "Hotel A"},
              {"email": "driver@x.com", "role": "volunteer", "entity": "V1", "name": "Ravi"},
              {"email": "shelter@x.com", "role": "shelter", "entity": "R1", "name": "Home"},
              {"email": "boss@x.com", "role": "admin", "name": "Boss"}):
        st.put_user(u)
    events = [
        {"type": "RecipientAdded", "r": dict(id="R1", name="Hope Home", loc=(12.95, 77.6), alpha=0, beta=INF, mu=30,
                                             cap={"hot": 50, "cold": 20, "ambient": 50}, confidential=confidential)},
        {"type": "VolunteerAdded", "v": dict(id="V1", name="Ravi Kumar", loc=(12.93, 77.62), cap_kg=30)},
        {"type": "DonationPosted", "d": dict(id="d001", donor="Hotel A", loc=(12.935, 77.625), category="cooked", holding="hot",
                                             veg=True, kg=10, meals=18, a=0, b=120, safe_until=240, posted=0, owner="donor@x.com")},
        {"type": "RecipientChosen", "d": "d001", "r": "R1"},
        {"type": "OfferSent", "id": "d001:V1", "d": "d001", "v": "V1", "expires": 20, "p": .7},
        {"type": "OfferAccepted", "offer": "d001:V1", "eta_pick": 15, "code": "4821"},
        {"type": "PickedUp", "d": "d001"},
        {"type": "Delivered", "d": "d001"},
    ]
    for t, e in enumerate(events):
        e["t"] = float(t)
        core.apply(S, e)
        n.on_event(e)
    return st, mailer, n


def test_each_role_gets_its_messages():
    st, mailer, _ = _city()
    kinds = lambda email: [x["kind"] for x in st.notes.get(email, [])]
    assert kinds("donor@x.com") == ["posted", "claimed", "picked_up", "delivered"], kinds("donor@x.com")
    assert kinds("driver@x.com") == ["offer", "job", "thanks"], kinds("driver@x.com")
    assert kinds("shelter@x.com") == ["incoming", "on_the_way", "received"], kinds("shelter@x.com")
    by = {}
    for m in mailer.sent:
        by.setdefault(m["to"], []).append(m)
    assert len(by["donor@x.com"]) == 4 and len(by["driver@x.com"]) == 3 and len(by["shelter@x.com"]) == 3
    for m in by["donor@x.com"]:                        # the handover code proves delivery: never to the donor
        assert "4821" not in m["html"] and "4821" not in m["text"], m["subject"]
    assert any("4821" in m["html"] and "4821" in m["text"] for m in by["shelter@x.com"])
    job = next(m for m in by["driver@x.com"] if m["kind"] == "job")
    assert "Hope Home" in job["html"] and "google.com/maps" in job["html"]
    assert "http://relay.test/receipt/d001" in next(m for m in by["donor@x.com"] if m["kind"] == "delivered")["text"]
    for m in mailer.sent:                              # well-formed: subject, plain text, html with the settings link
        assert m["subject"] and m["text"].strip() and m["html"].startswith("<!doctype html>") and "#settings" in m["html"]


def test_confidential_shelter_and_prefs():
    st, mailer, _ = _city(confidential=True)
    job = next(m for m in mailer.sent if m["kind"] == "job")
    assert "Hope Home" not in job["html"] and "Confidential" in job["html"]
    # email off -> in-app note only; offers off -> no offer emails, other emails still sent
    st.put_user({**st.users["driver@x.com"], "prefs": {"email": True, "offers": False}})
    st.put_user({**st.users["donor@x.com"], "prefs": {"email": False}})
    mailer.sent.clear()
    n = notify.Notifier(st, mailer, core.State(), str, "http://x", {})
    n.tell(st.users["driver@x.com"], "offer", "t", "b", ("s", "t", "h"))
    n.tell(st.users["driver@x.com"], "job", "t", "b", ("s", "t", "h"))
    n.tell(st.users["donor@x.com"], "claimed", "t", "b", ("s", "t", "h"))
    assert [m["kind"] for m in mailer.sent] == ["job"]
    assert st.notes["donor@x.com"][-1]["kind"] == "claimed"


def test_real_smtp_send():
    try:
        from aiosmtpd.controller import Controller
    except ImportError:
        print("  (skipped: pip install aiosmtpd)")
        return
    got = []

    class Handler:
        async def handle_DATA(self, server, session, envelope):
            got.append(envelope)
            return "250 OK"

    ctl = Controller(Handler(), hostname="127.0.0.1", port=8026)
    ctl.start()
    try:
        st = store.Store(store.SQLiteBackend(os.path.join(tempfile.mkdtemp(), "m.db")))
        env = {"SMTP_HOST": "127.0.0.1", "SMTP_PORT": "8026", "SMTP_SECURITY": "none", "SMTP_FROM": "Relay <relay@test.local>"}.get
        m = notify.Mailer(st, lambda k, d=None: env(k) or d)
        doc = m.enqueue("chef@restaurant.test", "Hello", "plain body", "<p>html body</p>", "test")
        demo = m.enqueue("x@relay.demo", "Hello", "plain", "<p>h</p>", "test")
        assert demo["status"] == "preview"                     # demo addresses never leave the outbox
        for _ in range(100):
            if st.outbox[doc["id"]]["status"] == "sent":
                break
            time.sleep(.05)
        assert st.outbox[doc["id"]]["status"] == "sent", st.outbox[doc["id"]]
        assert got and got[0].rcpt_tos == ["chef@restaurant.test"]
        raw = got[0].content.decode()
        assert "Subject: Hello" in raw and "plain body" in raw and "text/html" in raw
    finally:
        ctl.stop()


def test_brevo_api_send():
    """BREVO_API_KEY: email goes over HTTPS (for hosts that block SMTP ports). The HTTP call is captured, not sent."""
    import json
    import urllib.error
    sent = []

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"messageId":"x"}'

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        if json.loads(req.data)["to"][0]["email"] == "bad@x.com":
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, __import__("io").BytesIO(b'{"message":"invalid email"}'))
        return Resp()

    real = notify.urllib.request.urlopen
    notify.urllib.request.urlopen = fake_urlopen
    try:
        st = store.Store(store.SQLiteBackend(os.path.join(tempfile.mkdtemp(), "b.db")))
        env = {"BREVO_API_KEY": "xkeysib-test", "SMTP_FROM": "Relay <team@gmail.com>"}.get
        m = notify.Mailer(st, lambda k, d=None: env(k) or d)
        assert m.status()["mode"] == "brevo"
        ok, bad = m.enqueue("chef@restaurant.test", "Hello", "plain", "<p>html</p>", "test"), m.enqueue("bad@x.com", "Hi", "t", "<p>h</p>", "test")
        for _ in range(100):
            if st.outbox[ok["id"]]["status"] == "sent" and st.outbox[bad["id"]]["status"] == "failed":
                break
            time.sleep(.05)
        assert st.outbox[ok["id"]]["status"] == "sent", st.outbox[ok["id"]]
        assert st.outbox[bad["id"]]["status"] == "failed" and "400" in st.outbox[bad["id"]]["error"]   # no pointless retries
        r = sent[0]
        body = json.loads(r.data)
        assert r.full_url == "https://api.brevo.com/v3/smtp/email" and r.get_header("Api-key") == "xkeysib-test"
        assert body["sender"] == {"name": "Relay", "email": "team@gmail.com"} and body["htmlContent"] == "<p>html</p>" and body["textContent"] == "plain"
    finally:
        notify.urllib.request.urlopen = real


def test_preview_mode_without_smtp():
    st = store.Store(store.SQLiteBackend(os.path.join(tempfile.mkdtemp(), "p.db")))
    m = notify.Mailer(st, lambda k, d=None: d)
    assert m.enqueue("a@b.com", "s", "t", "<p>h</p>", "test")["status"] == "preview"
    assert m.status()["mode"] == "preview"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
