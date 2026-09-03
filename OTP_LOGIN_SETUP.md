# Admin One-Time Code (OTP) Login

## What it does
When an **admin** account has a mobile number on file, signing in now takes
two steps:

1. Username + password (checked as before).
2. A 6-digit code sent to that admin's registered mobile number, entered on
   a new "Verify it's you" screen.

**Manager and cashier accounts are never asked for a code** — they sign in
with just username + password, unchanged.

If an admin account does **not** have a mobile number on file yet, they sign
in normally (no code step) and see a reminder to add one. This avoids
locking out the bootstrap admin account before anyone has had a chance to
set a number.

## 1. Install
    pip install -r requirements.txt

`requests` was added to `requirements.txt` — it's only used to call an SMS
gateway (see step 3).

## 2. Database
The Flask app adds these automatically on startup:
- `users.phone_number` column
- `login_otp_codes` table

You can also run `auth_migration.sql` manually if you prefer migrating
outside the app.

## 3. Set each admin's mobile number
Go to **User Management → Edit** for the admin account and fill in
**Mobile Number**. Until that's set, that admin's login skips the code step.

You can also set the very first bootstrap admin's number without touching
the database, via an environment variable:

    ADMIN_PHONE_NUMBER=+91XXXXXXXXXX

## 4. Connect a real SMS gateway (optional but recommended for production)
Without any gateway configured, codes are written to the server log
(`[LOGIN OTP] Code for ...`) and shown directly on the verification screen
with a "Development mode" notice, so you can test the whole flow without
signing up for anything.

To send real text messages, set these environment variables to match your
SMS provider (Fast2SMS, MSG91, Twilio's HTTP API, or any provider that
accepts a simple form-encoded POST):

    SMS_GATEWAY_URL=https://your-provider.example/send
    SMS_GATEWAY_API_KEY=your-api-key
    SMS_GATEWAY_SENDER_ID=CAFEAPP   # optional, provider-dependent

`send_login_otp_sms()` in `app.py` posts `api_key`, `sender_id`, `to`, and
`message` as form fields. If your provider expects a different request
shape (JSON body, different field names, auth header instead of a field,
etc.), adjust that one function — everything else in the OTP flow is
provider-agnostic.

## 5. Other settings
    OTP_EXPIRY_SECONDS=300     # how long a code is valid (default 5 minutes)
    OTP_MAX_ATTEMPTS=5         # wrong tries allowed before requiring a resend
    CAFE_OTP_SENDER_NAME=...   # name shown inside the SMS text itself

## Security notes
- Codes are stored hashed (`werkzeug.security`), never in plain text.
- A code is single-use: it's deleted as soon as it's verified.
- Old/unused codes for a user are invalidated whenever a new one is issued
  (login retry or "Resend code").
- The Flask session only gets `user_id` (i.e. the user is actually logged
  in) after the code is verified — the password-only step does not grant
  access to anything.
- Resending is rate-limited to once every 30 seconds per pending login.
