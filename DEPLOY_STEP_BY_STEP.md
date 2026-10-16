# AMS → PythonAnywhere: complete step-by-step (start to finish)

Written for the site **nazirali789.pythonanywhere.com** and the repository
**https://github.com/rehmanahmedyt-ctrl/ams-for-backup-nazir-ali.git** (public, default branch `main`).

Nothing has been done yet, so follow this in order, **Part 1 → Part 2 → Part 3**. Do not skip Part 2:
that is where you prove the webhook really deploys your pushes.

---

## 0. First, understand what we are building (30 seconds)

```
You push a commit to GitHub (branch: main)
        │
        ▼
GitHub sends a signed webhook (HTTPS POST) to
https://nazirali789.pythonanywhere.com/deploy
        │
        ▼
deploy_hook.py  (runs inside your PythonAnywhere web app, outside Flask)
  1. verifies the HMAC-SHA256 signature  → answers 200 "Deploy queued" in milliseconds
  2. a background thread runs:  git pull --ff-only origin main
  3. touches the WSGI file  →  PythonAnywhere reloads your web app
        │
        ▼
Your live site now runs the new code.
```

Two places let you look at the result at any time:

| URL | What it tells you |
| --- | --- |
| `https://nazirali789.pythonanywhere.com/deploy/health` | the hook is mounted and reachable |
| `https://nazirali789.pythonanywhere.com/deploy/status` | last deploy result (status, commit, time) |

Files the hook uses (all inside `instance/`, which is **never committed** to Git):

| File | Meaning |
| --- | --- |
| `instance/deploy_secret.txt` | the webhook secret — the same string you paste into GitHub |
| `instance/deploy_status.json` | last deploy result, read by `/deploy/status` |
| `instance/deploy.lock` | stops two deploys running at the same time |
| `instance/ahmed_cement_v44_fresh.db` | the live SQLite database |

---

## 1. Facts and limits you must know before starting

1. **Free PythonAnywhere account is enough for this.**
   - `github.com` is on PythonAnywhere's free allowlist, so `git clone` and `git pull` over **HTTPS** work.
   - `pip install` works through PythonAnywhere's own PyPI mirror, so the virtualenv can be built.
   - Free = 512 MB disk, 1 web app, 100 CPU-seconds/day, and **no SSH** (only the in-browser Bash console).
2. **Your repo is public**, so the server needs **no** GitHub password or token to clone and pull.
3. **The webhook only deploys the branch `main`.** Work on any other branch (including the `arena/...`
   branches) is *not* deployed until it is merged into `main`.
4. **Your server copy must stay a plain checkout**: no file editing on the server, no local commits.
   The hook deliberately refuses to deploy if the checkout is dirty or on another branch (`409 Conflict`).
5. **Pick Python 3.12 (or 3.11) in the Web tab.** `requirements.txt` pins `numpy<2`, and `numpy 1.26`
   publishes prebuilt wheels for Python ≤ 3.12 only; on 3.13 pip would try to compile it from source and
   probably fail. The web-app Python version and the virtualenv Python version **must match**.
6. **Free accounts need a monthly click:** Web tab → *Run until 1 month from today*. If you forget, the
   web app is switched off and GitHub webhook deliveries fail with a connection error. PythonAnywhere
   emails you about a week before.
7. **Never commit `instance/`, `*.db`, or the webhook secret.** `.gitignore` already excludes them — keep it that way.

---

# PART 1 — One-time deployment

### Step 1 — Open a Bash console on PythonAnywhere

Dashboard → **Consoles** → **Bash** (or *New console → Bash*).

Confirm which Python versions exist on the account:

```bash
ls /usr/bin/python3.*
```

Note one of them, e.g. `python3.12`. You will use it twice below (web app + virtualenv).

### Step 2 — Clone the repository into your home directory

```bash
cd ~
git clone https://github.com/rehmanahmedyt-ctrl/ams-for-backup-nazir-ali.git
cd ams-for-backup-nazir-ali
git log --oneline -1
```

Expected: the clone completes and prints the newest commit on `main`.

### Step 3 — Create the web app in the Web tab (before running the installer)

Dashboard → **Web** → **Add a new web app** →

1. Domain: keep **`nazirali789.pythonanywhere.com`** → Next
2. **Manual configuration** (do *not* choose the Flask/Django wizard)
3. Python version: **3.12** (or 3.11 — must be a version you saw in Step 1) → Next

Now **stay on the Web tab** and copy these two values — you need them in Step 5:

- **WSGI configuration file** — normally
  `/var/www/nazirali789_pythonanywhere_com_wsgi.py`
  (the file only exists after the web app is created — that is why we create it first)
- Source code / Working directory — `/home/nazirali789/ams-for-backup-nazir-ali`

The installer fills in Source code, Working directory, the virtualenv, `/static/` mapping and the WSGI
block *automatically* if you give it a PythonAnywhere API token (next step, optional but recommended);
otherwise it prints the exact list of fields for you to type.

### Step 4 — (Optional but recommended) create a PythonAnywhere API token

Dashboard → **Account** → **API token** → *Create a new API token* → copy it.

In the Bash console:

```bash
umask 077
printf '%s' 'PASTE_YOUR_PA_TOKEN_HERE' > ~/pa_token.txt
chmod 600 ~/pa_token.txt
```

With this token, the installer configures the Web tab and clicks *Reload* for you.

### Step 5 — Run the guided installer

**Before running it, look at the WSGI path you copied in Step 3** and use that exact path below.

```bash
cd ~/ams-for-backup-nazir-ali

python3 setup_Deploy.py \
  --target "$HOME/ams-for-backup-nazir-ali" \
  --repo https://github.com/rehmanahmedyt-ctrl/ams-for-backup-nazir-ali.git \
  --branch main \
  --domain nazirali789.pythonanywhere.com \
  --wsgi-file /var/www/nazirali789_pythonanywhere_com_wsgi.py \
  --python python3.12 \
  --api-token-file "$HOME/pa_token.txt"
```

(Drop the last line if you skipped Step 4.)

What it does, in order — it prints a plan first and asks before anything material:

1. checks the checkout, origin and branch (installs/updates the clone if needed)
2. creates `~/ams-for-backup-nazir-ali/.venv` and runs `pip install -r requirements.txt` (a few minutes)
3. creates `instance/deploy_secret.txt` (random 64 hex chars, mode 0600) if missing
4. adds **one managed block** to the WSGI file (with a backup of the old file) that sets
   `AMS_WSGI_FILE`, `AMS_DEPLOY_BRANCH=main`, `SQLITE_JOURNAL_MODE=DELETE` and imports `wsgi.application`
5. (with the API token) sets Source code, Working directory, virtualenv, `/static/` mapping and reloads
6. runs validation: files, virtualenv, WSGI, site reachable, `/deploy/health`, a **signed ping**, and the push/pull state

Useful flags:

| Flag | Use |
| --- | --- |
| `--dry-run` | show what would change, write nothing |
| `--check-only` | only check the current state, change nothing |
| `--show-secret` | print the webhook secret once (needed in Step 6) |
| `--rotate-secret` | make a new secret (then paste the new value into GitHub) |
| `--skip-deps` | do not touch the virtualenv |
| `--skip-wsgi`, `--skip-webhook`, `--skip-http` | skip one stage (for a partial repair run) |
| `--help` | full list of options and their exact meaning |

**Exit codes:** `0` = everything done · `2` = done, but manual steps remain (read the `[todo]` lines) ·
`1` = blocked/failed (read the `[fail]` lines and the log under `instance/setup/logs/`).

**Shorter form for later runs** (from inside the checkout). The domain `nazirali789.pythonanywhere.com` and
the WSGI path are then derived automatically from the account:

```bash
cd ~/ams-for-backup-nazir-ali
python3 setup_Deploy.py --target "$PWD" --check-only     # just check, change nothing
python3 setup_Deploy.py --target "$PWD"                  # full install / repair
```

Always pass `--target "$PWD"`: without it the installer assumes `$HOME` (`/home/nazirali789`), which is the
wrong directory, because your project lives in `~/ams-for-backup-nazir-ali`.

If any step says *manual*, do exactly what the `fix:` text says and run the same command again — reruns
are safe, they resume instead of starting over.

### Step 6 — Add the GitHub webhook (one time)

This must be done by an account that **administers** the repo (the owner account
`rehmanahmedyt-ctrl`, or a collaborator with admin rights).

Get the secret from the server:

```bash
cat ~/ams-for-backup-nazir-ali/instance/deploy_secret.txt
```

(or, equivalently: `cd ~/ams-for-backup-nazir-ali && python3 setup_Deploy.py --target "$PWD" --show-secret`)

Copy the printed value, then in GitHub:

Repository → **Settings** → **Webhooks** → **Add webhook**

| Field | Value |
| --- | --- |
| Payload URL | `https://nazirali789.pythonanywhere.com/deploy` |
| Content type | `application/json` |
| Secret | the value from `instance/deploy_secret.txt` (keep it private) |
| SSL verification | Enable (default) |
| Which events | **Just the push event** |
| Active | ticked |

Click **Add webhook**. GitHub immediately sends a signed `ping`:

- In GitHub → Webhooks → your hook → **Recent Deliveries**: newest delivery = `ping`, **200**, response `pong`.
- If it is red instead, see the troubleshooting table in Part 4.

### Step 7 — First check of your live site

Open `https://nazirali789.pythonanywhere.com/`.

- First load is slow: the app creates `instance/ahmed_cement_v44_fresh.db`, all tables and the default admin.
- Default login: **user `Admin`, password `Admin@fbm12345`** → change the password immediately.

If the browser shows *"AMS failed to start"* plus a traceback, that traceback **is** the error — copy it,
fix it, then Web tab → **Reload**. PythonAnywhere's own log is Web tab → *Error log*.

**Deployment is now complete.** Your site is live, and the webhook is armed.

---

# PART 2 — How to check that pushes really deploy (do this now)

Do the four checks in order (A–D). Each one proves a different link in the chain.

### Check A — the hook is reachable

Open in your browser (or use curl):

```bash
curl -s https://nazirali789.pythonanywhere.com/deploy/health
curl -s https://nazirali789.pythonanywhere.com/deploy/status
```

Expected:

```
AMS deploy hook is reachable.
```

and, if a deploy has run before:

```
AMS deploy hook is reachable.
last deploy: 200 OK at 2026-10-16T09:12:44Z
Deployed a1b2c3d; WSGI reload requested.
```

`no deployment attempted yet` is normal the first time.

### Check B — make a real (tiny) test push

On your own machine:

```bash
cd ams-for-backup-nazir-ali
git checkout main
git pull
# change something harmless and visible, e.g. a line in README.md
git commit -am "test: verify pythonanywhere webhook deploy"
git push origin main
```

Now watch the chain, in this order:

1. **GitHub** → repo → Settings → Webhooks → your hook → **Recent Deliveries**
   Newest row: event `push`, **200**, response body
   `Deploy queued; pulling and reloading in the background.`
   (The hook answers instantly on purpose — GitHub marks slow responses as failed.)
2. **Wait 5–60 seconds**, then reload `https://nazirali789.pythonanywhere.com/deploy/status`
   → it should report `200 OK` and `Deployed <short-sha>; WSGI reload requested.`
3. **On PythonAnywhere** (Bash console):

   ```bash
   cd ~/ams-for-backup-nazir-ali
   git log --oneline -1          # must show the commit you just pushed
   git status --porcelain        # must print nothing
   ```
4. **In the browser**: open the site and confirm the change is visible. If the page looks unchanged,
   hard-refresh (Ctrl+Shift+R).

If all four are true, **push → webhook → pull → reload is working end to end.**

### Check C — let the installer verify it for you

```bash
cd ~/ams-for-backup-nazir-ali
python3 setup_Deploy.py --target "$PWD" --check-only --verify-push
```

Read the summary line: it should report `Push -> pull -> reload  PASS`, with the server checkout matching
`origin/main`. `--check-only` changes nothing on the server.

### Check D — the manual fallback (know it, in case a deploy ever stalls)

```bash
cd ~/ams-for-backup-nazir-ali
git pull --ff-only origin main
touch /var/www/nazirali789_pythonanywhere_com_wsgi.py   # this is what reloads the web app
```

That is exactly what the webhook does — doing it by hand always works, and proves the code is fine even
if the hook is not.

---

# PART 3 — Everyday workflow: shipping your new changes

1. Make the change on a branch (or in the Arena session branch), open a Pull Request, review it.
2. **Merge the PR into `main`** — this is the only event that deploys.
   ```bash
   gh pr merge <number> --merge      # or the green "Merge" button on GitHub
   ```
3. Nothing else to do: GitHub calls the webhook, PythonAnywhere pulls `main` and reloads within seconds.
4. Confirm with the same three places as Check B: GitHub *Recent Deliveries* → 200,
   `/deploy/status` → `Deployed <sha>`, and the browser.

Rules that keep this working:

- Do **not** edit files on the server (dirty checkout ⇒ the hook answers `409 Conflict` and stops deploying).
- Do **not** commit `instance/`, database files, or `deploy_secret.txt`.
- Do **not** commit to a different branch and expect a deploy — only `main` is watched.
- The deploy branch is set by `AMS_DEPLOY_BRANCH` in the WSGI managed block (default `main`). Changing it
  means also changing the branch GitHub sends pushes from, so leave it as `main`.

---

# PART 4 — Troubleshooting (what the hook returns and what to do)

| Symptom | Cause | Fix |
| --- | --- | --- |
| Delivery shows **401 Invalid signature** | GitHub's secret ≠ `instance/deploy_secret.txt` | `cat ~/ams-for-backup-nazir-ali/instance/deploy_secret.txt`, paste the value into the GitHub hook (or rotate with `python3 setup_Deploy.py --target "$PWD" --rotate-secret` and paste the new one) |
| Delivery shows **503 Deployment secret is not configured** | `instance/deploy_secret.txt` missing/empty | rerun the installer without `--skip-*` flags |
| Delivery shows **409 Conflict — wrong branch** | server checkout is not on `main` | `cd ~/ams-for-backup-nazir-ali && git checkout main` |
| Delivery shows **409 Conflict — local changes** | a file on the server was edited | undo the edit deliberately (never `git reset --hard`); for the installer file use `--restore-installer-file` |
| Delivery shows **409 Conflict — another deployment is running** | two pushes at once | nothing to do; the second push already re-queued the newest commit |
| Delivery shows **500** | the pull failed (network/auth) | read Web tab → *Error log*; test manually: `git pull --ff-only origin main` |
| Delivery **200** but `/deploy/status` never updates | the background thread did not finish (worker recycled) | just use Check D (manual `git pull` + `touch`), then rerun `--check-only --verify-push` |
| Delivery red with a connection error | web app is **switched off** (free plan) | Web tab → *Run until 1 month from today*, then resend the delivery |
| Site shows 502/500 or "Something went wrong" | app failed to start | the page/site shows the traceback; also Web tab → *Error log*; fix, then *Reload* |
| Site loads but the old design/code appears | browser cache | hard-refresh; confirm `git log --oneline -1` on the server matches GitHub |
| `pip install` fails | wrong Python version / versions mismatched | reuse `--python python3.12` **and** set the Web tab Python to 3.12 |
| `sqlite3.OperationalError: unable to open database file` | WAL journal on a network filesystem | already handled: `SQLITE_JOURNAL_MODE=DELETE` is set by the WSGI block / `wsgi.py` |

Rerunning the installer is always safe and is the recommended first repair:

```bash
cd ~/ams-for-backup-nazir-ali && python3 setup_Deploy.py --target "$PWD" --check-only
```

---

## Appendix — cheat sheet

**Commands on PythonAnywhere**

```bash
cd ~/ams-for-backup-nazir-ali                          # project directory
source .venv/bin/activate                              # use the app's virtualenv
python3 setup_Deploy.py --target "$PWD" --check-only   # health check, changes nothing
python3 setup_Deploy.py --target "$PWD" --verify-push  # + push/pull/reload verification
cat instance/deploy_secret.txt                         # the webhook secret
git log --oneline -1                                   # which commit is live
git pull --ff-only origin main                         # manual deploy (pull)
touch /var/www/nazirali789_pythonanywhere_com_wsgi.py  # manual deploy (reload)
```

**URLs**

```text
https://nazirali789.pythonanywhere.com/             live site (login: Admin / Admin@fbm12345, change it)
https://nazirali789.pythonanywhere.com/deploy/health hook reachable
https://nazirali789.pythonanywhere.com/deploy/status last deploy result
https://github.com/rehmanahmedyt-ctrl/ams-for-backup-nazir-ali/settings/hooks   webhook + deliveries
```

**Layout on the server**

```text
/home/nazirali789/ams-for-backup-nazir-ali/     the Git checkout (the app)
/home/nazirali789/ams-for-backup-nazir-ali/.venv  virtualenv with requirements.txt installed
/home/nazirali789/ams-for-backup-nazir-ali/instance/  secret, status, logs, live SQLite database
/var/www/nazirali789_pythonanywhere_com_wsgi.py  the web app's WSGI file (touched to reload)
```
