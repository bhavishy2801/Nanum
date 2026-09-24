# Deploy Relay for free

The result is a public `https://…onrender.com` address with Google sign-in, MongoDB storage and real email, at no cost.

| Piece | Free service | Why this one |
|---|---|---|
| The app | **Render** (free web service, Docker) | Runs one long-lived Python process, which Relay needs (the living city runs in memory). 512 MB RAM; Relay uses about 215 MB |
| Database | **MongoDB Atlas** (M0, which you already have) | Render's free disk is wiped on every restart or deploy, so data must live outside it |
| Email | **Brevo** HTTPS email API (300 emails a day) | Render's free services **block outbound SMTP ports 25, 465 and 587**, so Gmail SMTP can't work there. Brevo sends over HTTPS (port 443), which isn't blocked |

**Limits of the free plan**
- The app **sleeps after 15 minutes without visitors**. The next visit takes about a minute to wake it (§8 shows how to keep it awake).
- It has little CPU, so the Simulation lab's *Compare 30 nights* is slow for scenarios other than Bengaluru, whose results are pre-computed.
- Nothing is lost when it sleeps or restarts: everything is replayed from MongoDB.

Allow about 30–40 minutes the first time.

---

## 1. Before you start

- A **GitHub** account (https://github.com).
- A **Render** account: sign up at https://render.com **with GitHub**, so it can see your repositories.
- Your **MongoDB Atlas** cluster (you have `NanumCluster`).
- A **Brevo** account (https://www.brevo.com, free plan).
- Git is already installed on your machine (`git version 2.55`).

---

## 2. Brevo: set up email over HTTPS

1. Sign up at https://www.brevo.com and choose the **Free** plan.
2. **Verify the sender address.**
   1. Open the menu at the top right, then **Senders, Domains & Dedicated IPs → Senders → Add a sender**.
   2. Name: `Relay`. Email: the address mails should come from, for example `nanum5613@gmail.com`.
   3. Brevo emails that address a code or link. Confirm it.
3. **Create an API key.**
   1. Open the menu at the top right, then **SMTP & API → API Keys → Generate a new API key**.
   2. Name it `relay-render`.
   3. Copy the key (it starts with `xkeysib-`). It's shown only once, so keep it somewhere safe.

The sender in `SMTP_FROM` (next steps) **must be exactly the address you verified**, or Brevo refuses the email with error 400.

Good to know:
- Mail sent "from" a `@gmail.com` address through Brevo often lands in **spam** at first, because Gmail can't vouch for Brevo. For a demo, just mark it "Not spam" once.
- For better delivery later, verify a domain you own in Brevo (**Senders, Domains → Domains**) and send from it.

---

## 3. MongoDB Atlas: let Render connect

Render's free services have **no fixed IP address**, so Atlas must accept connections from anywhere:

1. Open **Atlas → Security → Network Access → + Add IP Address → Allow Access from Anywhere** (`0.0.0.0/0`).
2. Click **Confirm** and wait until it shows **Active**.

Your database is still protected by the database user's password and by TLS. Keep that password long and random.

**⚠️ One app per database.** Never run your laptop's `python app.py` against the **same** database while the Render app is running. Both would write events with the same sequence numbers and damage the log. Give your laptop its own database name in your local `.env`:

```
MONGODB_DB=relay_local
```

Render uses `MONGODB_DB=relay` (§5). Same cluster, separate data.

---

## 4. Put the code on GitHub (private repository)

The repository includes 4 files from `relay_data/` (28 MB) that the live city needs. Make it **private**, because that dataset came with the problem statement.

### 4.1 Create the repository

1. On https://github.com/new:
   - Repository name: `relay`.
   - Visibility: **Private**.
   - Don't add a README, `.gitignore` or license.
2. Click **Create repository**, then copy its URL, for example `https://github.com/YOUR_USERNAME/relay.git`.

### 4.2 Push your code

Run these in PowerShell, in the project folder:

```bash
cd C:\Coding\AmiHacks\Coding
```

```bash
git init
```

```bash
git add .
```

**Check that no secrets are included** before committing:

```bash
git status
```

- The list must **not** contain `.env`, `relay.db`, `relay_data/bc_dataset.csv` or `relay_data.zip`.
- It **should** contain `Dockerfile`, `requirements.txt`, `models/…`, `static/…` and `relay_data/donations.csv`, `offers.csv`, `recipients.csv`, `volunteers.csv`.

If `.env` shows up, stop and run `git rm --cached .env`.

This command prints `.gitignore:1:.env` if `.env` is correctly ignored:

```bash
git check-ignore -v .env
```

Then commit and push:

```bash
git commit -m "Relay: deployable build"
```

```bash
git branch -M main
```

```bash
git remote add origin https://github.com/YOUR_USERNAME/relay.git
```

```bash
git push -u origin main
```

If Git asks you to sign in, a browser window opens. Sign in to GitHub there.

---

## 5. Render: create the web service

1. On https://dashboard.render.com, click **+ New → Web Service**.
2. **Connect a repository.** Pick `relay`. If it isn't listed, click **Configure account** and give Render access to it.
3. Fill in:

   | Field | Value |
   |---|---|
   | Name | `relay` (your address becomes `https://relay-xxxx.onrender.com`) |
   | Language | **Docker** (Render finds the `Dockerfile` automatically) |
   | Branch | `main` |
   | Region | **Singapore**, the closest to India and to Atlas Mumbai |
   | Instance Type | **Free** |

4. **Environment Variables.** Click **Add from .env**, or add them one by one:

   | Key | Value |
   |---|---|
   | `MONGODB_URI` | your full `mongodb+srv://…` string, the same as in your local `.env` |
   | `MONGODB_DB` | `relay` |
   | `GOOGLE_CLIENT_ID` | `520356074787-4cl6imjafjvhs6crkqlr888e1m5fue9k.apps.googleusercontent.com` |
   | `RELAY_ADMINS` | `agrawalbhavishy2801@gmail.com` |
   | `RELAY_CARTO_KEY` | your CARTO key |
   | `RELAY_POLICY` | `Relay-RL` |
   | `RELAY_SIM` | `1` |
   | `RELAY_DEMO_LOGIN` | `1` while judges try it, `0` afterwards (see §9) |
   | `BREVO_API_KEY` | `xkeysib-…` from §2 |
   | `SMTP_FROM` | `Relay <nanum5613@gmail.com>`, using exactly the sender you verified in Brevo |

   **Leave out** `SMTP_HOST` and `SMTP_PASSWORD`: SMTP is blocked on Render's free plan, and with `BREVO_API_KEY` set Relay uses Brevo anyway.

   Also leave out `PORT`, `HOST` and `RELAY_PUBLIC_URL`. Render supplies the port, the Dockerfile sets the host, and email links automatically use your Render address (Render provides it as `RENDER_EXTERNAL_URL`).

5. Open **Advanced** and set **Health Check Path** to `/api/config`.
6. Click **Deploy Web Service**.

The first build takes about 5–8 minutes while it installs scikit-learn, pandas and the rest. Watch the **Logs** tab. It's ready when you see:

```
Relay storage: MongoDB (relay @ …mongodb.net:27017), email: brevo
INFO:     Uvicorn running on http://0.0.0.0:10000
```

and the page header shows **Live**.

---

## 6. Google sign-in: allow the new address

1. Open https://console.cloud.google.com/apis/credentials and click your **OAuth 2.0 Client ID**.
2. Under **Authorized JavaScript origins**, click **+ Add URI** and paste your Render address, for example `https://relay-xxxx.onrender.com`. Use `https`, and no trailing `/`.
3. Keep the `http://localhost:8000` entries so local development still works.
4. Click **Save** and wait about 5 minutes (Google takes a moment to apply it).

**Map tiles:** if you restricted your CARTO key to certain domains in the CARTO dashboard, add the `onrender.com` address there too.

---

## 7. Check the deployment

1. Open `https://relay-xxxx.onrender.com`. The sign-in page appears with the Google button.
2. Sign in with Google as `agrawalbhavishy2801@gmail.com`. You land on the admin **Live board**.
3. Open **System**. All health checks should be green:
   - **Database (MongoDB):** events are counted, and "0 waiting to be written".
   - **Email:** `BREVO api.brevo.com:443`.
   - **Google sign-in:** configured.
   - The **Demo accounts** check shows a warning while `RELAY_DEMO_LOGIN=1`. That's intended.
4. **Email test.** In **System → Email**, type your own address and click **Send test email**. The Mailbox shows it as **Sent** within seconds; check your inbox (and spam).
5. **Full end-to-end check** from your laptop. It posts a few test donations, pauses the city briefly, then restores it:

   ```bash
   python ps_check.py https://relay-xxxx.onrender.com
   ```

   It should end with **25/25 checks passed**.

---

## 8. Keep it awake (optional, free)

A sleeping app pauses the living city and makes the first visitor wait about a minute. To avoid that, ping it every 5 minutes with UptimeRobot:

1. Sign up at https://uptimerobot.com (free plan).
2. Click **+ New monitor**:
   - Type: **HTTP(s)**.
   - URL: `https://relay-xxxx.onrender.com/api/config`.
   - Interval: **5 minutes**.
3. Click **Create**.

Render's free plan includes 750 hours a month, which covers one app running around the clock (a month is at most 744 hours). If you run a second free service, it will run out.

**Before a live demo:** open the address 2 minutes early, even with the pinger set up.

---

## 9. Security checklist

- **Demo accounts.** While `RELAY_DEMO_LOGIN=1`, **anyone** with the link can click "Admin" and see everything, including people's email addresses. Keep it on only while judges need the one-click demo, then set it to `0` in Render → **Environment** (it redeploys by itself).
- **Secrets** (`MONGODB_URI`, `BREVO_API_KEY`) live only in Render's Environment settings and your local `.env`. They are never in GitHub. If one is ever exposed, rotate it: change the Atlas database user's password, or delete the Brevo key and generate a new one.
- **Private repository**, because it contains part of the dataset.
- **After the event**, remove `0.0.0.0/0` from Atlas Network Access, or delete the Render service.
- **Already built in:** HTTPS (from Render), secure HttpOnly session cookies, Google tokens verified on the server, and role checks on every API route.

---

## 10. Updating the live app

```bash
git add .
```

```bash
git commit -m "describe your change"
```

```bash
git push
```

Render rebuilds and redeploys automatically (a few minutes; watch **Events**). The city, accounts and emails are kept, because they're in MongoDB.

**Reset the live city:**
1. In Render, click **Suspend** on the service.
2. On your laptop, point `.env` at the live database (`MONGODB_DB=relay`) and run `python store.py wipe`.
3. Set `MONGODB_DB` back to `relay_local`.
4. In Render, click **Resume**.

---

## 11. Troubleshooting

| What you see | Fix |
|---|---|
| Build fails in Render logs | Read the last red lines. If a package failed to install, redeploy with **Clear build cache & deploy** |
| Log says `MongoDB connection failed … Atlas refused this computer's IP` | Atlas Network Access needs `0.0.0.0/0` set to **Active** (§3). Then click **Manual Deploy → Deploy latest commit** |
| Log says `Authentication failed` | `MONGODB_URI` in Render has the wrong password, or special characters that need URL-encoding |
| Google button: "origin is not allowed" or `invalid_client` | Add the exact `https://…onrender.com` origin (§6) and wait 5 minutes |
| Signed in but not admin | Your Google email must be in `RELAY_ADMINS` (Render → Environment), then sign out and in again |
| Mailbox shows **Failed: Brevo 401** | Wrong `BREVO_API_KEY`: generate a new one and update it in Render |
| Mailbox shows **Failed: Brevo 400 … sender** | `SMTP_FROM` isn't a verified Brevo sender (§2 step 2). The address must match exactly |
| Emails **Sent** but not received | Look in spam and "Promotions". Brevo → **Transactional → Logs** shows whether each one was delivered |
| Mailbox shows **Failed: timed out** with SMTP | You set `SMTP_HOST` without `BREVO_API_KEY`. SMTP is blocked on Render's free plan, so use Brevo |
| Links in emails open `localhost` | `RELAY_PUBLIC_URL` is set to localhost in Render. Remove it, or set it to your Render address |
| First load takes about a minute | The free app was asleep (§8) |
| Log says "Out of memory" | Unlikely at about 215 MB. Avoid running *Compare 30 nights* on the big synthetic scenarios on the free plan |
| Laptop and Render data mixed up | They share a database. Use `MONGODB_DB=relay_local` locally (§3) |

## What's in the deployment

| File | Role |
|---|---|
| `Dockerfile` | Python 3.14 slim image. Installs the pinned requirements and runs `python app.py` on `0.0.0.0:$PORT`. It trusts Render's proxy headers, so HTTPS is detected and session cookies are marked Secure |
| `.dockerignore` / `.gitignore` | Keep `.env`, local databases and the 165 MB training data out. Only the 4 dataset files the app reads are kept |
| `requirements.txt` | Pinned to the exact versions the models in `models/` were trained with (`joblib` models need the same scikit-learn) |

This setup was checked locally by running the app from a copy containing only what the image contains: 35 MB, no `.env`, bound to `0.0.0.0`. `ps_check.py` passed **25/25**. The Docker build itself runs for the first time on Render, because Docker isn't installed on the development machine.
