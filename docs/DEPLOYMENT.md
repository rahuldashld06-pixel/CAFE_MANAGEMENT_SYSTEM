# Step-by-step: from this zip to a live SaaS

Follow the parts in order. Part A gets it running on your machine,
Part B puts it online. If you only care about deploying, you can skip
straight to Part B — but running it locally first makes problems much
easier to diagnose.

---

## Part A — Run it locally (about 15 minutes)

### Step 1. Unzip and open a terminal in the project folder

```bash
cd cafe-management-saas
```

You should see `app.py`, `wsgi.py`, `requirements.txt`, `templates/`
and `static/`.

### Step 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it:

```bash
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# macOS / Linux
source .venv/bin/activate
```

Your prompt should now start with `(.venv)`.

### Step 3. Install dependencies

```bash
pip install -r requirements.txt
```

### Step 4. Create the database

> **Upgrading from the older single-cafe app?** You can point `.env` at
> your existing database instead of creating a new one. On first run the
> app adds the missing tables and columns, installs the two unique
> constraints the older schema lacked (cleaning up any duplicate
> inventory or bill rows first), and adopts your existing data as
> cafe #1 with the oldest user as its owner. Sign in with your existing
> credentials rather than using `/register`. Take a backup first, and
> read the migration warnings in the startup log.

Open MySQL (Workbench, or the `mysql` command line) and run:

```sql
CREATE DATABASE cafe_management
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
```

That is all you need to do. **Do not import any table dump.** The app
creates every table it needs the first time it runs. If your database
user is not permitted to run `CREATE TABLE` at runtime, run
`docs/schema.sql` once instead.

### Step 5. Create your `.env` file

Copy the example:

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

Open `.env` and set at minimum:

```
APP_ENV=development
SECRET_KEY=paste-a-generated-value-here
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=your-mysql-password
DB_NAME=cafe_management
```

Generate the secret key with:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

### Step 6. Start the app

```bash
python app.py
```

Open <http://127.0.0.1:5000>. You should land on the sign-in page.

### Step 7. Create your first cafe

Click **Create an account** (or go to `/register`) and fill in the cafe
name, your name, a username and a password of at least 8 characters.

Submitting creates the cafe, makes you its admin, and signs you in.

> **Add a mobile number** on this account (profile menu → User
> Management → Edit). Two things depend on it: one-time-code login, and
> self-service password reset. Without a number on file, a forgotten
> password can only be reset by another admin.

### Step 8. Set up the menu, in this order

The order matters — a food item must belong to a category.

1. **Categories → Add Category.** Create at least one (e.g. "Beverages").
2. **Food Management → Add Food.** Pick the category, set a price, and
   set an opening stock quantity. Stock drives availability: quantity 0
   means the item will not appear on the New Order screen.
3. **New Order.** Choose quantities and place an order.
4. **Billing.** The bill for that order appears automatically. Mark it
   paid, or change the payment mode.

If all four steps work, the deployment is sound.

### Step 9. Run the test suite (optional but recommended)

```bash
python tests/smoke_test.py
```

This runs entirely in memory and needs no database. It walks two
separate cafes through registration, menu setup, staff creation,
ordering and billing, and asserts that neither can reach the other's
data. You should see `PASSED: 37   FAILED: 0`.

---

## Part B — Deploy to Render with a managed MySQL database

### Step 1. Create the hosted database

Any managed MySQL works (Aiven, PlanetScale, Railway, Clever Cloud).
Using **Aiven** as the example:

1. Create a **MySQL** service on the free plan.
2. Wait for it to reach *Running* (a few minutes).
3. From the service overview, under *Connection information*, note
   **Host**, **Port**, **User** (`avnadmin`) and **Password**.
   The port is **not 3306** — Aiven assigns a random high port. Copy the
   exact value.
4. On the same panel, click **CA certificate → Download**. Save it into
   the project as `certs/ca.pem`.
5. Open the **Databases** tab → **Add database** → name it
   `cafe_management`.

Leave the new database empty. Do not import anything.

Aiven ships a default database called `defaultdb`. You can use it, but a
purpose-named database is easier to reason about later.

Aiven requires an encrypted connection, so set `DB_SSL_CA=certs/ca.pem`
along with the other variables in the next steps. Without it the
connection is still encrypted but unverified; with it, the client
confirms it is really talking to your database. The CA certificate is
public, not a credential — commit it so Render gets a copy.

### Step 2. Push this project to GitHub

```bash
git init
git add .
git commit -m "Cafe management SaaS"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/YOUR-REPO.git
git push -u origin main
```

`.gitignore` already excludes `.env` and `config.py`. Confirm with
`git status` that neither is staged before you push — those files hold
your database password.

### Step 3. Create the Render web service

1. Go to <https://dashboard.render.com> → **New** → **Web Service**.
2. Connect the GitHub repository.
3. Set:
   - **Runtime**: Python 3
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**:
     `gunicorn --workers 3 --threads 2 --timeout 60 --bind 0.0.0.0:$PORT wsgi:application`
   - **Health check path**: `/healthz`

(If you import the repo as a **Blueprint** instead, `render.yaml`
sets all of this for you.)

### Step 4. Set environment variables

In the service's **Environment** tab add:

| Key | Value |
|---|---|
| `APP_ENV` | `production` |
| `SECRET_KEY` | a fresh generated value — **not** the one from your laptop |
| `DB_HOST` | from your database provider |
| `DB_PORT` | from your database provider |
| `DB_USER` | from your database provider |
| `DB_PASSWORD` | from your database provider |
| `DB_NAME` | `cafe_management` |
| `DB_SSL_CA` | `certs/ca.pem` (required by Aiven and most managed providers) |
| `CAFE_NAME` | the platform name shown before sign-in |

The app **will not start** in production without `SECRET_KEY`. That is
deliberate: the old default let anyone forge a session cookie for any
cafe. Keep the value stable — changing it signs everyone out.

Add the Razorpay and SMS variables only when you are ready to use those
features. Both are optional and stay switched off while blank.

### Step 5. Deploy and verify

Render builds and starts the service. When it goes live:

1. Visit `https://your-app.onrender.com/healthz`.
   You want `{"status": "ok", "database": "ok"}`. If the database part
   reports an error, your `DB_*` variables are wrong — fix them and
   redeploy. Nothing else will work until this passes.
2. Visit `https://your-app.onrender.com/register` and create your first
   real cafe.
3. Walk through Step 8 from Part A on the live site.

### Step 6. Onboard more cafes

Send new customers to `/register`. Each signup creates its own cafe with
its own menu, staff, orders, billing and branding. Nothing else is
needed per tenant.

---

## Part C — Optional features

### Online payments (Razorpay)

1. In the Razorpay dashboard, grab your **Test mode** Key ID and Key
   Secret.
2. Add `RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` in Render and
   redeploy.
3. Add a webhook pointing at
   `https://your-app.onrender.com/razorpay/webhook`, subscribing to
   `payment.captured` and `order.paid`.
4. Put the webhook's signing secret in `RAZORPAY_WEBHOOK_SECRET`.

A bill is only marked Paid after the signature is verified server-side,
and webhook amounts are checked against the bill total. A cancelled or
failed checkout leaves the bill Pending.

### One-time code (OTP) login for admins

An admin with a mobile number on file is asked for a 6-digit code after
their password. Managers, cashiers and staff are never challenged.

With no SMS gateway configured, the code is written to the server log
and shown on screen with a "development mode" notice, so you can test
the flow immediately. To send real messages set `SMS_GATEWAY_URL`,
`SMS_GATEWAY_API_KEY` and optionally `SMS_GATEWAY_SENDER_ID`.

The app posts `api_key`, `sender_id`, `to` and `message` as form fields.
If your provider expects a different shape (JSON body, auth header,
different field names), edit `send_login_otp_sms()` in `app.py` — that
one function is the only provider-specific code.

---

## Troubleshooting

**`/healthz` says the database errored.**
Check `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`. On
Aiven, confirm the port is the assigned high port and not 3306.

If the error mentions SSL, TLS or certificate verification, set
`DB_SSL_CA` to your provider's CA file. If it mentions the certificate
not matching the host, confirm `DB_HOST` is the exact hostname from the
provider — not an IP address.

**"Too many connections".**
Lower `DB_POOL_SIZE`, or reduce gunicorn workers. Workers x pool size
must stay under your plan's connection limit.

**Sign-in works but every page bounces back to the sign-in screen.**
`SECRET_KEY` is changing between restarts. Set a fixed value.

**"Create a category first".**
Expected on a brand-new cafe. Categories → Add Category, then add food.

**Logos or food photos vanished after a deploy.**
They should not any more — images are stored in the database. If you are
seeing this, you are running an older copy of `app.py`.

**An admin is locked out and has no mobile number on file.**
Another admin resets the password from User Management → Edit. The app
refuses to demote or deactivate a cafe's last active admin, so there is
always at least one account that can do this.
