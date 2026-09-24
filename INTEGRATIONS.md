# Relay: MongoDB, email (SMTP) and notifications

This guide sets up the database and email and shows how to check they work. Running and presenting the app is in [DEMO.md](DEMO.md). ML and RL are in [GUIDE.md](GUIDE.md).

| What | Where it lives | With nothing configured |
|---|---|---|
| **Database**: event log, accounts, sessions, notifications, emails | `store.py` | A local SQLite file, `relay.db` |
| **Email**: one mail per important moment, per role | `notify.py` (SMTP) | Emails are written as **previews**, readable under admin → System → Mailbox |
| **In-app notifications**: the 🔔 bell | `notify.py` + `/api/notes` | Always on |

Both parts are optional and independent. The app runs the same with or without them.

---

## 1. Install

```bash
pip install -r requirements.txt
```

This adds `pymongo`, which also installs `dnspython` for `mongodb+srv://` addresses. Two more packages are needed only for the automated tests:

```bash
pip install mongomock aiosmtpd
```

---

## 2. MongoDB

### 2.1 MongoDB Atlas (free cloud database, recommended)

1. Sign up at https://www.mongodb.com/cloud/atlas/register.
2. **Create a cluster.** Choose **M0 (Free)** and a region near you (for example Mumbai, `ap-south-1`).
3. **Database Access → Add New Database User.**
   - Authentication: *Password*.
   - Username: `relay`, with a long generated password.
   - Built-in role: **Read and write to any database**. To be stricter, grant `readWrite` on the `relay` database only.
4. **Network Access → Add IP Address.**
   - Click **Add current IP address**.
   - For a hackathon demo from changing networks you can add `0.0.0.0/0` (anywhere). Remove it afterwards, because the password is then the only protection.
5. **Connect → Drivers → Python.** Copy the connection string, which looks like:
   `mongodb+srv://relay:<db_password>@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0`
6. Replace `<db_password>` with the real password. If the password contains `@ : / ? # [ ] %`, URL-encode it; for example `p@ss` becomes `p%40ss`.
7. Put it in `.env`:

   ```
   MONGODB_URI=mongodb+srv://relay:YOUR_PASSWORD@cluster0.abcde.mongodb.net/?retryWrites=true&w=majority
   MONGODB_DB=relay
   ```

8. Check the connection:

   ```bash
   python store.py check
   ```

   You should see `connected: MongoDB (relay @ ...)` and then `write + read back + delete: OK`.

### 2.2 Or: MongoDB on your own machine

Either use Docker (Docker Desktop, then):

```bash
docker run -d --name relay-mongo -p 27017:27017 -v relay-mongo:/data/db mongo:7
```

Or install **MongoDB Community Server** from https://www.mongodb.com/try/download/community, which runs as a Windows service.

Then set this in `.env`:

```
MONGODB_URI=mongodb://localhost:27017
MONGODB_DB=relay
```

and run `python store.py check`.

### 2.3 Bring your existing data across (optional)

Your current city, accounts and history are in `relay.db`. To copy them into MongoDB once:

```bash
python store.py migrate
```

It refuses to run if the MongoDB database already has events, so it can't double-import. Then restart the app. **System → Database** should now say *MongoDB*, and the city carries on from where it was.

Skip this step to start with a fresh city: Relay seeds the Bengaluru city on first start.

### 2.4 What is stored

| Collection | One document per | Notes |
|---|---|---|
| `events` | event, `_id` = sequence number | The source of truth. On start, Relay replays them to rebuild the city |
| `users` | account, `_id` = email | Role, linked driver or shelter, business details, email settings |
| `sessions` | sign-in, `_id` = SHA-256 of the cookie | Raw tokens are never stored. A TTL index deletes expired sessions automatically |
| `notes` | in-app notification | Indexed by person and time |
| `outbox` | email | Subject, text, HTML, status (`sent` / `preview` / `queued` / `retrying` / `failed`), attempts, error |

**How writes work.**
- The app keeps its working state in memory. Web requests never wait on the database, which matters for a cloud database 50–150 ms away.
- Every change goes, in order, through one background writer. It batches up to 500 writes per round trip and retries a failed batch forever with back-off (1 s up to 30 s).
- Writes are idempotent upserts, so a retry after a half-finished batch can't duplicate anything.
- **System → Database** shows the backend, how many writes are still waiting, and the last error if the database is unreachable.
- On shutdown (Ctrl+C) the app writes everything still queued before it exits.

### 2.5 Useful commands

```bash
python store.py check      # connect + write/read/delete test
```

```bash
python store.py stats      # how many events, users, sessions, notes and emails are stored
```

```bash
python store.py wipe       # delete ALL Relay data in the configured database (asks you to type 'wipe')
```

**Reset the demo city.**
- On SQLite: stop the app and delete `relay.db`.
- On MongoDB: stop the app and run `python store.py wipe`.

---

## 3. Email (SMTP)

### 3.1 Pick a provider

**Deploying to a host that blocks SMTP** (Render free, Hugging Face Spaces)? Use **Brevo's HTTPS API** instead: set `BREVO_API_KEY` plus `SMTP_FROM` (a Brevo-verified sender) and no `SMTP_*` server. When both are set, Relay uses Brevo. The steps are in [DEPLOY.md](DEPLOY.md) §2.

| Provider | `SMTP_HOST` | `SMTP_PORT` | `SMTP_SECURITY` | `SMTP_USER` / `SMTP_PASSWORD` | Good for |
|---|---|---|---|---|---|
| **Gmail** | `smtp.gmail.com` | `587` | `starttls` | your Gmail address / a 16-letter **App Password** | Quickest for a demo (≈500 mails a day) |
| Outlook / Microsoft 365 | `smtp.office365.com` | `587` | `starttls` | your address / your password or app password | Work or school accounts where the admin allows SMTP |
| Brevo (free 300 a day) | `smtp-relay.brevo.com` | `587` | `starttls` | your Brevo login / an SMTP key | Sending from your own domain |
| SendGrid | `smtp.sendgrid.net` | `587` | `starttls` | `apikey` / your API key | Production |
| Amazon SES | `email-smtp.<region>.amazonaws.com` | `587` | `starttls` | SMTP credentials from the SES console | Production |
| Any server on port 465 | … | `465` | `ssl` | … | Networks that block 587 |

**Gmail App Password, step by step.**
1. Turn on 2-Step Verification at https://myaccount.google.com/security.
2. Open https://myaccount.google.com/apppasswords, create one named "Relay", and copy the 16 letters **without spaces**.

Relay never uses your normal Google password.

### 3.2 Configure `.env`

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=yourteam@gmail.com
SMTP_PASSWORD=abcdefghijklmnop
SMTP_FROM=Relay <yourteam@gmail.com>
SMTP_SECURITY=starttls
RELAY_PUBLIC_URL=http://localhost:8000
```

- `SMTP_FROM` must be an address your provider lets you send from. For Gmail that's your own address or a verified alias.
- `RELAY_PUBLIC_URL` is used in the links inside emails ("Track it live", "View receipt", "Email settings"). Set it to your real address when you deploy.

Restart `python app.py`. The console prints `Relay storage: … email: smtp`.

### 3.3 Check it

1. Sign in as an admin and open **System**. The **Email** card shows the server it will use.
2. Type your own address under **Send test email**. The **Mailbox** below shows it within a few seconds as **Sent**, or as **Failed** with the exact error.
3. Any signed-in person can also use **avatar menu → Email & notifications → Send me a test email**.

**No real account handy?** Run a local catch-all SMTP server that prints every mail to the terminal:

```bash
python -m aiosmtpd -n -l 127.0.0.1:8025
```

Then set `SMTP_HOST=127.0.0.1`, `SMTP_PORT=8025`, `SMTP_SECURITY=none` and leave `SMTP_USER` empty.

### 3.4 How sending works

- Web requests never wait for email. Each message is saved to the outbox first, then sent by a background thread.
- Up to 4 attempts: immediately, then after 3 s, 15 s and 60 s.
- A wrong password or a rejected address fails at once (retrying can't help), and the error shows in System → Mailbox.
- Emails still waiting when the app stops are sent after it restarts.
- Every email has a plain-text part and an HTML part. The HTML works in Gmail, Outlook and phone mail apps (inline styles, no images).
- **Demo accounts (`@relay.demo`) never receive real email**, because that domain doesn't exist. Their emails are kept as previews so you can show them in the Mailbox.

---

## 4. Who gets what

Each moment below produces an **in-app notification** (the 🔔 bell, with an unread badge and a toast) **and an email**, unless that person turned email off.

| Moment | 🍲 Food donor | 🛵 Driver | 🏠 Shelter | 🛡️ Admin |
|---|---|---|---|---|
| Account set up | Welcome | Welcome | Welcome | Welcome |
| Food posted | "Posted": food, safe-until time, shelter | | | |
| Driver asked | | **Rescue offer**: food, distance, reply-by (approximate area only) | | |
| Driver accepts | "*Name* is coming": pickup time, shelter | **Confirmed**: pickup + drop-off with Google Maps links, pick-up-by time, checklist | **Food is coming**, with the **4-digit handover code** | |
| Picked up | "Your food is on its way" | | "On its way now", with the code | |
| Delivered | "Delivered. Thank you!" + **receipt link** | "Thank you", with meals delivered | "Handover confirmed" | |
| Couldn't be rescued | "We couldn't rescue this one: don't serve it" | "Rescue cancelled" (in-app) | | |
| Driver cancels | | | "Driver changed: old code no longer works" (in-app) | |
| Needs a human | | | | **Escalation**, with the act-before time (email only for real donors) |

**Safety and privacy rules**
- The handover code proves delivery, so it goes only to the shelter. It never goes to the donor.
- A confidential shelter's name and address are never emailed to a driver.
- Offers show only an approximate area. The exact pickup address is sent after the driver accepts.
- Simulated city donations raise escalations in-app only, so admins' inboxes stay usable.

**Settings.** Avatar menu → **Email & notifications**, or the "Email settings" link at the bottom of every email (it opens `/#settings`):
- *Email me at each important moment*: on or off.
- Drivers only: *Also email every new rescue offer*.

**Receipts.**
- Every delivered donation has a printable receipt at `/receipt/<id>`, for a donor's records or CSR report.
- It's linked from the delivery email, the donor's **My donations** and **My impact** screens, and the bell.
- Only the donor who posted it, or an admin, can open it.

---

## 5. Everything new in the app

- **🔔 Bell (all roles):**
  - unread badge, a ring animation and a toast for each new update;
  - click an item to jump to the right screen, or to open the receipt;
  - **confetti** when food is delivered.
- **Email & notifications** settings dialog, with a "send me a test email" button.
- **Receipts:** printable, and "Save as PDF" works in any browser.
- **Admin → System:**
  - health checks for **Database**, **Email** and **Notifications**;
  - a **Database** card: backend, location, events stored, writes waiting, last error;
  - an **Email** card: mode, server, sent/preview/waiting/failed counts, and a test-send form;
  - a **Mailbox**: every email, filter by status, click to see the exact email as the person received it, and **Send again**.
- **How Relay thinks (explainer) upgrades:**
  - a **camera** that glides and zooms to what each step is about;
  - **typed narration**, one plain sentence per step;
  - data points **flowing into the model** each training round, with a pulse on the update rule;
  - the reward **counting up**, with a "+N meals" burst at the shelter;
  - **presenter mode**: ⛶ Present (or `F`) goes full screen with larger text; `←` `→` step, `Space` plays or pauses, `1`–`8` jump to a step.
- Map pins **drop in** with a bounce when they appear or change status.

---

## 6. Tests

With the app running:

```bash
python ps_check.py
```

It should end with **25/25 checks passed**: the 21 problem-statement and role checks, plus notifications, the email outbox, mark-as-read and Mailbox access control.

```bash
python test_store_notify.py
```

These 7 tests cover:
- SQLite and MongoDB (mongomock) round trips, including restart and continuing the event numbering;
- upgrading an old `relay.db`;
- every role getting exactly the right messages;
- the donor never seeing the handover code;
- confidential shelters staying hidden;
- email preferences;
- a **real SMTP send** to a local test server;
- preview mode.

```bash
python test_relay.py
```

These are the original 7 property tests.

For a live MongoDB, also run `python store.py check`.

---

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `ServerSelectionTimeoutError` on start / `store.py check` | Your IP isn't allowed in Atlas **Network Access**, the URI is wrong, or the network blocks port 27017. Add your IP, re-copy the URI and try again |
| `Authentication failed` | Wrong database-user password (this is the database user, not your Atlas login), or it needs URL-encoding (§2.1 step 6) |
| `mongodb+srv` error about DNS / `dnspython` | `pip install "pymongo[srv]"`. Some office networks block SRV lookups: use the "standard connection string" from Atlas instead |
| System → Database shows **retrying** | The database is unreachable. Nothing is lost: writes wait in order and go through when it's back. The error is shown on the card |
| `SMTPAuthenticationError (535)` | Gmail needs an **App Password**, not your normal password. Outlook: SMTP AUTH may be disabled for your account |
| Test email stays **Sending…** then **Failed: timed out** | Your network blocks port 587. Try `SMTP_PORT=465` and `SMTP_SECURITY=ssl` |
| Emails arrive in spam | Normal for a new sender. Mark "Not spam" once. For your own domain, set up SPF/DKIM with your provider |
| Links in emails open `localhost` | Set `RELAY_PUBLIC_URL` to the address people actually use |
| A person gets no emails | Check that they didn't switch email off (their settings), that their account isn't a `@relay.demo` one, and look in System → Mailbox for their messages and errors |
| Old `relay.db` from before this update | Nothing to do: accounts are upgraded to the new layout automatically on first start |

## 8. Security notes

- All secrets (`MONGODB_URI`, `SMTP_PASSWORD`) live only in `.env`, which git ignores. Never paste them into code or slides.
- Use a database user that can only read and write the `relay` database. Use an App Password or API key for email: both can be revoked on their own without changing your main password.
- Session cookies are HttpOnly and SameSite, and stored only as SHA-256 hashes. Signing out deletes the session from the database.
- The email preview in the Mailbox is shown in a sandboxed frame, so any content in it can't run scripts.
