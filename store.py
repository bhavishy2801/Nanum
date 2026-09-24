"""Persistence for Relay: the event log, accounts, sessions, in-app notifications and the email outbox.

Backends
  SQLite  (default)  a local file, RELAY_DB (default relay.db)
  MongoDB            set MONGODB_URI (and optionally MONGODB_DB, default "relay")

Design: the app keeps its working state in memory (the city is a projection of the event log). Everything that must
survive a restart is written through ONE background writer thread, in order, with retries. Web requests never wait
on the database, which matters for a cloud database 50-150 ms away.

    python store.py check      # connect, create indexes, write + read back a probe document
    python store.py migrate    # copy a local relay.db (SQLite) into MongoDB
    python store.py stats      # what is stored where
"""
import copy
import json
import os
import queue
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def _plain(x):
    """JSON round-trip: tuples -> lists, detaches the copy from live objects the app may still change."""
    return json.loads(json.dumps(x))


# ---------------------------------------------------------------- backends

class SQLiteBackend:
    name = "SQLite"

    def __init__(self, path):
        self.where = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript("""
            create table if not exists events (seq integer primary key, t real, type text, payload text);
            create table if not exists users (email text primary key, doc text);
            create table if not exists sessions (token_hash text primary key, email text, expires real);
            create table if not exists notes (id text primary key, email text, created real, doc text);
            create table if not exists outbox (id text primary key, created real, doc text);
            create index if not exists notes_email on notes (email, created);""")
        # accounts from the previous (column-per-field) layout, if present
        cols = [r[1] for r in self.db.execute("pragma table_info(users)")]
        if "doc" not in cols:
            rows = self.db.execute("select * from users").fetchall()
            self.db.execute("alter table users rename to users_old")
            self.db.execute("create table users (email text primary key, doc text)")
            for r in rows:
                doc = dict(zip(cols, r))
                self.db.execute("insert into users values (?, ?)", (doc["email"], json.dumps(doc)))
        self.db.commit()

    def load(self):
        now = time.time()
        return {
            "events": [json.loads(p) for (p,) in self.db.execute("select payload from events order by seq")],
            "users": {e: json.loads(d) for e, d in self.db.execute("select email, doc from users")},
            "sessions": {h: (e, x) for h, e, x in self.db.execute("select token_hash, email, expires from sessions") if x > now},
            "notes": [json.loads(d) for (d,) in self.db.execute("select doc from notes order by created")],
            "outbox": [json.loads(d) for (d,) in self.db.execute("select doc from outbox order by created desc limit 500")],
            "seq": self.db.execute("select coalesce(max(seq), 0) from events").fetchone()[0],
        }

    def write(self, ops):
        with self.db:   # one transaction per batch
            for op in ops:
                k = op[0]
                if k == "event":
                    _, seq, e = op
                    self.db.execute("insert or replace into events values (?, ?, ?, ?)", (seq, e["t"], e["type"], json.dumps(e)))
                elif k == "user":
                    self.db.execute("insert or replace into users values (?, ?)", (op[1], json.dumps(op[2])))
                elif k == "session":
                    self.db.execute("insert or replace into sessions values (?, ?, ?)", op[1:])
                elif k == "session_del":
                    self.db.execute("delete from sessions where token_hash = ? or expires < ?", (op[1], time.time()))
                elif k == "note":
                    d = op[1]
                    self.db.execute("insert or replace into notes values (?, ?, ?, ?)", (d["id"], d["email"], d["created"], json.dumps(d)))
                elif k == "outbox":
                    d = op[1]
                    self.db.execute("insert or replace into outbox values (?, ?, ?)", (d["id"], d["created"], json.dumps(d)))
                elif k == "wipe":
                    for tbl in ("events", "users", "sessions", "notes", "outbox"):
                        self.db.execute(f"delete from {tbl}")

    def stats(self):
        return {t: self.db.execute(f"select count(*) from {t}").fetchone()[0] for t in ("events", "users", "sessions", "notes", "outbox")}

    def probe(self):
        """Write, read back and delete a throwaway row (for `python store.py check`)."""
        with self.db:
            self.db.execute("create table if not exists probe (id text primary key)")
            self.db.execute("insert or replace into probe values ('check')")
        ok = self.db.execute("select count(*) from probe where id = 'check'").fetchone()[0] == 1
        with self.db:
            self.db.execute("drop table probe")
        return ok


class MongoBackend:
    name = "MongoDB"

    def __init__(self, uri, dbname, client=None):
        from pymongo import ASCENDING, MongoClient
        self.client = client or MongoClient(uri, serverSelectionTimeoutMS=10000, appname="relay", tz_aware=True)
        try:                                       # fail fast with a clear error if unreachable / bad credentials
            self.client.admin.command("ping")
        except Exception as ex:
            msg = str(ex)
            hint = ("Atlas refused this computer's IP address. In Atlas: Security > Network Access > Add IP Address > "
                    "Add Current IP Address (or 0.0.0.0/0 for a demo), wait ~1 minute, then retry."
                    if "TLSV1_ALERT_INTERNAL_ERROR" in msg or "SSL handshake failed" in msg else
                    "Wrong database user or password in MONGODB_URI (Atlas: Security > Database Access)."
                    if "auth" in msg.lower() else
                    "Can't reach MongoDB: check MONGODB_URI, your internet, and Atlas Network Access.")
            raise SystemExit(f"\nMongoDB connection failed: {msg[:300]}\n\n-> {hint}\n"
                             "   (Or empty MONGODB_URI in .env to use the local SQLite file.)\n") from None
        self.db = self.client[dbname]
        host = getattr(self.client, "address", None)
        self.where = f"{dbname} @ {host[0]}:{host[1]}" if host else dbname
        self.db.sessions.create_index("expires_at", expireAfterSeconds=0)          # MongoDB deletes expired sessions
        self.db.notes.create_index([("email", ASCENDING), ("created", ASCENDING)])
        self.db.outbox.create_index([("created", ASCENDING)])
        self.db.events.create_index([("type", ASCENDING)])

    def load(self):
        now = time.time()
        events = [d["e"] for d in self.db.events.find({}, {"e": 1}).sort("_id", 1)]
        users = {d["_id"]: {k: v for k, v in d.items() if k != "_id"} for d in self.db.users.find()}
        sessions = {d["_id"]: (d["email"], d["expires"]) for d in self.db.sessions.find() if d["expires"] > now}
        notes = [{k: v for k, v in d.items() if k != "_id"} for d in self.db.notes.find().sort("created", 1)]
        outbox = [{k: v for k, v in d.items() if k != "_id"} for d in self.db.outbox.find().sort("created", -1).limit(500)]
        last = self.db.events.find_one(sort=[("_id", -1)], projection={"_id": 1})
        return {"events": events, "users": users, "sessions": sessions, "notes": notes, "outbox": outbox, "seq": last["_id"] if last else 0}

    def write(self, ops):
        from pymongo import DeleteOne, ReplaceOne
        groups, order = {}, []
        for op in ops:
            k = op[0]
            if k == "wipe":
                self._flush(groups, order)
                groups, order = {}, []
                for c in ("events", "users", "sessions", "notes", "outbox"):
                    self.db[c].delete_many({})
                continue
            if k == "event":
                # upsert, not insert: a retried batch (after a network error mid-write) must not hit duplicate keys
                coll, req = "events", ReplaceOne({"_id": op[1]}, {"_id": op[1], "t": op[2]["t"], "type": op[2]["type"], "e": op[2]}, upsert=True)
            elif k == "user":
                coll, req = "users", ReplaceOne({"_id": op[1]}, {**op[2], "_id": op[1]}, upsert=True)
            elif k == "session":
                coll, req = "sessions", ReplaceOne({"_id": op[1]}, {"_id": op[1], "email": op[2], "expires": op[3],
                                                                     "expires_at": datetime.fromtimestamp(op[3], timezone.utc)}, upsert=True)
            elif k == "session_del":
                coll, req = "sessions", DeleteOne({"_id": op[1]})
            elif k == "note":
                coll, req = "notes", ReplaceOne({"_id": op[1]["id"]}, {**op[1], "_id": op[1]["id"]}, upsert=True)
            elif k == "outbox":
                coll, req = "outbox", ReplaceOne({"_id": op[1]["id"]}, {**op[1], "_id": op[1]["id"]}, upsert=True)
            else:
                continue
            if coll not in groups:
                groups[coll] = []
                order.append(coll)
            groups[coll].append(req)
        self._flush(groups, order)

    def _flush(self, groups, order):
        for coll in order:   # events first when they came first; each collection keeps its own order
            self.db[coll].bulk_write(groups[coll], ordered=True)

    def stats(self):
        return {c: self.db[c].count_documents({}) for c in ("events", "users", "sessions", "notes", "outbox")}

    def probe(self):
        """Write, read back and delete a throwaway document (for `python store.py check`)."""
        self.db.relay_probe.replace_one({"_id": "check"}, {"_id": "check", "t": time.time()}, upsert=True)
        ok = self.db.relay_probe.find_one({"_id": "check"}) is not None
        self.db.relay_probe.drop()
        return ok


def backend_from_env(env=os.environ.get):
    uri = env("MONGODB_URI", "")
    if uri:
        return MongoBackend(uri, env("MONGODB_DB", "relay"))
    return SQLiteBackend(env("RELAY_DB", os.path.join(HERE, "relay.db")))


# ---------------------------------------------------------------- store (in-memory + ordered write-behind)

class Store:
    def __init__(self, backend):
        self.backend = backend
        data = backend.load()
        self.events = data["events"]              # only used at boot for replay
        self.users = data["users"]
        self.sessions = data["sessions"]
        self.notes = {}
        for n in data["notes"]:
            self.notes.setdefault(n["email"], []).append(n)
        self.outbox = {d["id"]: d for d in data["outbox"]}
        self.seq = data["seq"]                    # highest stored event number: new events never overwrite old ones
        self.q = queue.Queue()
        self.lock = threading.Lock()              # guards outbox (touched by the mail thread) and the counters below
        self.written, self.error, self.error_at = 0, None, None
        threading.Thread(target=self._writer, name="relay-store-writer", daemon=True).start()

    # -- writes (called on the app's event-loop thread, except outbox which the mailer also calls)
    def event(self, e):
        self.seq += 1
        self.q.put(("event", self.seq, _plain(e)))

    def put_user(self, doc):
        self.users[doc["email"]] = doc
        self.q.put(("user", doc["email"], _plain(doc)))

    def put_session(self, token_hash, email, expires):
        self.sessions[token_hash] = (email, expires)
        self.q.put(("session", token_hash, email, expires))

    def del_session(self, token_hash):
        self.sessions.pop(token_hash, None)
        self.q.put(("session_del", token_hash))

    def put_note(self, doc):
        lst = self.notes.setdefault(doc["email"], [])
        i = next((i for i, n in enumerate(lst) if n["id"] == doc["id"]), None)
        if i is None:
            lst.append(doc)
            del lst[:-200]                         # keep the newest 200 per person in memory
        else:
            lst[i] = doc                           # an update (e.g. read) keeps its place
        self.q.put(("note", _plain(doc)))

    def put_outbox(self, doc):
        with self.lock:
            self.outbox[doc["id"]] = copy.deepcopy(doc)
        self.q.put(("outbox", _plain(doc)))

    def wipe(self):
        """Delete everything (used by `python store.py wipe`)."""
        self.q.put(("wipe",))
        self.flush()

    # -- the writer thread: batches, retries forever with backoff, never reorders or drops
    def _writer(self):
        while True:
            batch = [self.q.get()]
            while len(batch) < 500:
                try:
                    batch.append(self.q.get_nowait())
                except queue.Empty:
                    break
            delay = 1
            while True:
                try:
                    self.backend.write(batch)
                    with self.lock:
                        self.written += len(batch)
                        self.error = None
                    break
                except Exception as ex:   # network blip, DB restart: keep the batch and retry
                    with self.lock:
                        self.error, self.error_at = f"{type(ex).__name__}: {ex}"[:300], time.time()
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
            for _ in batch:
                self.q.task_done()

    def flush(self, timeout=15):
        end = time.time() + timeout
        while self.q.unfinished_tasks and time.time() < end:
            time.sleep(.05)
        return not self.q.unfinished_tasks

    def status(self):
        with self.lock:
            return {"backend": self.backend.name, "where": self.backend.where, "pending": self.q.unfinished_tasks,
                    "written": self.written, "error": self.error, "events": self.seq}


# ---------------------------------------------------------------- CLI

def _load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        with open(p) as f:
            for line in f:
                k, sep, v = line.strip().partition("=")
                if sep and not k.startswith("#"):
                    os.environ.setdefault(k.strip(), v.strip())


if __name__ == "__main__":
    _load_env()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        b = backend_from_env()
        print(f"connected: {b.name} ({b.where})")
        ok = b.probe()
        print("write + read back + delete:", "OK" if ok else "FAILED")
        print("contents:", b.stats())
        sys.exit(0 if ok else 1)
    if cmd == "stats":
        b = backend_from_env()
        print(b.name, b.where, b.stats())
    elif cmd == "migrate":
        if not os.environ.get("MONGODB_URI"):
            sys.exit("set MONGODB_URI in .env first")
        src = SQLiteBackend(os.environ.get("RELAY_DB", os.path.join(HERE, "relay.db")))
        dst = backend_from_env()
        if dst.stats()["events"]:
            sys.exit(f"MongoDB database already has events ({dst.stats()}); refusing to overwrite. Use `python store.py wipe` first.")
        d = src.load()
        ops = [("event", i + 1, e) for i, e in enumerate(d["events"])]
        ops += [("user", k, v) for k, v in d["users"].items()]
        ops += [("session", h, e, x) for h, (e, x) in d["sessions"].items()]
        ops += [("note", n) for n in d["notes"]] + [("outbox", o) for o in d["outbox"]]
        for i in range(0, len(ops), 1000):
            dst.write(ops[i:i + 1000])
        print("migrated:", dst.stats())
    elif cmd == "wipe":
        b = backend_from_env()
        if input(f"Delete ALL Relay data in {b.name} ({b.where})? type 'wipe' to confirm: ").strip() != "wipe":
            sys.exit("cancelled")
        b.write([("wipe",)])
        print("wiped:", b.stats())
    else:
        sys.exit(__doc__)
