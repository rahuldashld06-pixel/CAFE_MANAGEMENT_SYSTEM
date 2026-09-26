"""
How somebody says who they are, and how they say where to reach them.

Two things that used to be free text and are not any more.

The phone field showed "e.g. +91XXXXXXXXXX" and took whatever was
typed. The example taught everybody to write an Indian dial code
wherever they were, the Xs were the only hint of how long a number
should be, and that hint was wrong everywhere except India. A number
one digit short was found out months later, when a one-time code did
not arrive and nobody could sign in. It now asks which country, and
then wants exactly the digits that country uses.

And signing in wanted a username - a thing chosen once and then
forgotten. An email address is one people already know, so either now
works, in the same box, on the sign-in page and on the page that resets
a forgotten password.

No browser needed: all of this is forms and what the server does with
them.

Run with:  python tests/identity_test.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "identity-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

from decimal import Decimal             # noqa: E402
import app as application               # noqa: E402
import countries                        # noqa: E402

app = application.app
PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    line = (("  PASS  " if condition else "  FAIL  ") + name +
            ("" if condition else "\n          -> %s" % detail))
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def signed_in(client):
    """
    Whether the password was accepted.

    An admin who gets that far is not in yet - they are holding a code
    screen - so otp_user_id counts here. What it must NOT be used for is
    asking whether somebody finished; use all_the_way_in for that.
    """
    with client.session_transaction() as sess:
        return bool(sess.get("user_id") or sess.get("otp_user_id"))


def all_the_way_in(client):
    """Whether there is a real session, past any code screen."""
    with client.session_transaction() as sess:
        return bool(sess.get("user_id"))


def refusal_for(country, typed):
    """What somebody is told when their number is the wrong length."""
    try:
        application.clean_phone(country, typed)
        return ""
    except application.PhoneError as wrong:
        return str(wrong)


def refuses_empty_when_required():
    try:
        application.clean_phone("India", "", required=True)
        return False
    except application.PhoneError:
        return True


def signs_in_as(who, password):
    client = app.test_client()
    client.post("/login", data={"username": who, "password": password},
                follow_redirects=True)
    return signed_in(client)


# ==========================================================
print("\n=== 1. Every country has a code and a length ===")
# ==========================================================
check("every country in the clock list has a dial code",
      all(countries.dial_code_of(c) for c, _ in countries.COUNTRY_ZONES),
      "these have none: %s"
      % [c for c, _ in countries.COUNTRY_ZONES
         if not countries.dial_code_of(c)][:6])

check("and every one says how long its numbers are",
      all(countries.number_lengths(c) for c, _ in countries.COUNTRY_ZONES),
      "a country with no length cannot be checked against")

check("a country nobody has heard of still works",
      len(countries.number_lengths("Neverland")) > 3
      and countries.dial_code_of("Neverland") is None,
      "an unknown country must not make the field unusable")

for country, code, digits in (("India", "91", 10),
                              ("United Kingdom", "44", 10),
                              ("United Arab Emirates", "971", 9),
                              ("United States", "1", 10),
                              ("Singapore", "65", 8)):
    check("%s is +%s with %d digits" % (country, code, digits),
          countries.dial_code_of(country) == code
          and digits in countries.number_lengths(country),
          "it says +%s %s" % (countries.dial_code_of(country),
                              countries.number_lengths(country)))


# ==========================================================
print("\n=== 2. A number is the right length or it is refused ===")
# ==========================================================
GOOD = [
    ("India", "98765 43210", "+919876543210"),
    ("India", "+91 98765 43210", "+919876543210"),   # code typed twice
    ("India", "098765 43210", "+919876543210"),      # trunk zero
    ("India", "(98765) 43210", "+919876543210"),     # brackets
    ("United Arab Emirates", "50 123 4567", "+971501234567"),
    ("United Kingdom", "07911 123456", "+447911123456"),
    ("Singapore", "8123 4567", "+6581234567"),
]

for country, typed, expected in GOOD:
    try:
        got = application.clean_phone(country, typed)
    except application.PhoneError as wrong:
        got = "refused: %s" % wrong
    check("%s / %r becomes %s" % (country, typed, expected),
          got == expected, "it became %r" % got)

BAD = [
    ("India", "12345"),
    ("India", "987654321"),           # one short
    ("India", "98765432100"),         # one long
    ("United Arab Emirates", "1234"),
]

for country, typed in BAD:
    try:
        application.clean_phone(country, typed)
        refused = False
    except application.PhoneError:
        refused = True
    check("%s / %r is refused" % (country, typed), refused,
          "a number of the wrong length was accepted")

_short = refusal_for("India", "12345")
check("the refusal says which country and how many digits",
      "India" in _short and "10" in _short,
      "it says %r, which does not tell anybody what would be right"
      % _short)

check("an empty number is fine when it is optional",
      application.clean_phone("India", "") == "",
      "an optional field refused being left blank")

check("but not when it is required",
      refuses_empty_when_required(),
      "a required field accepted nothing at all")


# ==========================================================
print("\n=== 3. The form asks for a country, not an example ===")
# ==========================================================
guest = app.test_client()
register_page = guest.get("/register").get_data(as_text=True)

check("the old example is gone",
      "XXXXXXXXXX" not in register_page
      and "e.g. +91 98765 43210" not in register_page,
      "the form still shows an Indian number as the example, which is "
      "what everybody then types")

check("there is a country to pick",
      'name="phone_country"' in register_page,
      "no country picker on the registration form")

check("and every country is in it",
      register_page.count("data-lengths=") == len(countries.COUNTRY_ZONES),
      "the picker offers %d of %d countries"
      % (register_page.count("data-lengths="), len(countries.COUNTRY_ZONES)))

check("each option carries the digits that country needs",
      'data-code="91"' in register_page
      and 'data-lengths="10"' in register_page,
      "the page cannot tell anybody how long a number should be")


# ==========================================================
print("\n=== 4. Signing in with either name ===")
# ==========================================================
owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Identity Cafe", "full_name": "Ida Owner",
    "username": "ida", "email": "Ida@Cafe.COM",
    "phone_country": "India", "phone_number": "9876500001",
    "password": "password123", "confirm_password": "password123"},
    follow_redirects=True)
mysql_shim.skip_tour()

stored = mysql_shim._DB.execute(
    "SELECT username, email, phone_number FROM users WHERE username = 'ida'"
).fetchone()

check("registration keeps the address, lowercased",
      stored and stored[1] == "ida@cafe.com",
      "it stored %r" % (stored[1] if stored else None))

check("and the number in one international piece",
      stored and stored[2] == "+919876500001",
      "it stored %r" % (stored[2] if stored else None))

for typed in ("ida", "ida@cafe.com", "IDA@CAFE.COM", "  ida@cafe.com  "):
    attempt = app.test_client()
    attempt.post("/login", data={"username": typed,
                                 "password": "password123"},
                 follow_redirects=True)
    check("signing in as %r works" % typed, signed_in(attempt),
          "that spelling was not accepted")

wrong = app.test_client()
wrong.post("/login", data={"username": "ida@cafe.com",
                           "password": "not-the-password"},
           follow_redirects=True)
check("but not with the wrong password",
      not signed_in(wrong),
      "an email address let somebody in without the password")

check("an address nobody has is refused",
      not signs_in_as("nobody@nowhere.com", "password123"),
      "an unknown address signed in")


# ==========================================================
print("\n=== 5. The lockout counts the account, not the spelling ===")
# ==========================================================
# Keyed on what was typed, somebody locked out as "ida" would walk
# straight past it by typing ida@cafe.com instead, and the lock would
# be decoration.
mysql_shim._DB.execute("DELETE FROM login_attempts")
mysql_shim._DB.commit()

for _ in range(application.LOGIN_MAX_FAILURES):
    app.test_client().post("/login", data={"username": "ida",
                                           "password": "wrong"},
                           follow_redirects=True)

locked = app.test_client().post(
    "/login", data={"username": "ida", "password": "password123"},
    follow_redirects=True).get_data(as_text=True)

check("the username is locked after enough wrong answers",
      "Too many failed sign-in attempts" in locked,
      "the lockout did not engage, so the check below proves nothing")

by_email = app.test_client()
by_email.post("/login", data={"username": "ida@cafe.com",
                              "password": "password123"},
              follow_redirects=True)

check("and the email address is locked with it",
      not signed_in(by_email),
      "the same account was reached by its other name while locked out, "
      "which makes the lockout decoration")

mysql_shim._DB.execute("DELETE FROM login_attempts")
mysql_shim._DB.commit()


# ==========================================================
print("\n=== 6. Staff, and resetting a password ===")
# ==========================================================
owner.post("/users/add", data={
    "full_name": "Tess Till", "username": "tess",
    "email": "tess@cafe.com",
    "phone_country": "United Kingdom", "phone_number": "07911 123456",
    "password": "password123", "confirm_password": "password123",
    "role": "staff", "_csrf_token": csrf(owner)}, follow_redirects=True)

tess = mysql_shim._DB.execute(
    "SELECT email, phone_number FROM users WHERE username = 'tess'"
).fetchone()

check("a member of staff can have an address too",
      tess and tess[0] == "tess@cafe.com",
      "it stored %r" % (tess[0] if tess else None))

check("and their number is read as a British one",
      tess and tess[1] == "+447911123456",
      "it stored %r" % (tess[1] if tess else None))

staff = app.test_client()
staff.post("/login", data={"username": "tess@cafe.com",
                           "password": "password123"},
           follow_redirects=True)
check("they can sign in with it",
      signed_in(staff), "staff could not sign in by email")

short = owner.post("/users/add", data={
    "full_name": "Wrong Number", "username": "wrongnum",
    "phone_country": "India", "phone_number": "12345",
    "password": "password123", "confirm_password": "password123",
    "role": "staff", "_csrf_token": csrf(owner)}, follow_redirects=True)

check("a staff number of the wrong length is refused",
      "10 digits" in short.get_data(as_text=True)
      and not mysql_shim._DB.execute(
          "SELECT 1 FROM users WHERE username = 'wrongnum'").fetchone(),
      "a five-digit Indian mobile number was accepted")

reset_page = app.test_client().get("/forgot-password").get_data(as_text=True)
check("the reset page asks for a country too",
      'name="phone_country"' in reset_page
      and "XXXXXXXXXX" not in reset_page,
      "the reset page still shows the old example")

# ==========================================================
print("\n=== 7. The code can come by email ===")
# ==========================================================
# The app already asked an admin for a one-time code after their
# password. It only knew how to text it, and texting costs money per
# message and needs a gateway account. SMTP is a published standard and
# smtplib is in the standard library, so email needs no dependency at
# all - only a mailbox to send from.

check("an address is masked so it can be recognised, not used",
      application.mask_email("ida@cafe.com") == "i••@cafe.com",
      "it masked to %r" % application.mask_email("ida@cafe.com"))

check("and a one-letter name does not give itself away",
      application.mask_email("a@cafe.com").startswith("•"),
      "it masked to %r" % application.mask_email("a@cafe.com"))

otp = app.test_client()
otp.post("/login", data={"username": "ida@cafe.com",
                         "password": "password123"},
         follow_redirects=True)

with otp.session_transaction() as sess:
    check("a password alone does not sign an admin in",
          not sess.get("user_id") and sess.get("otp_user_id"),
          "the password was enough on its own")
    check("and the code goes to the address, not the phone",
          sess.get("otp_channel") == "email",
          "it chose %r" % sess.get("otp_channel"))
    check("the page can say which address without giving it away",
          sess.get("otp_sent_to") == "i••@cafe.com",
          "it would show %r" % sess.get("otp_sent_to"))

shown = otp.get("/login/verify").get_data(as_text=True)
check("the code screen names the address",
      "i••@cafe.com" in shown,
      "somebody with two addresses cannot tell which to look at")

# With no mail server configured the code goes to the log, so a laptop
# can still sign in. That is the development fallback the SMS path has.
again = app.test_client()
body = again.post("/login", data={"username": "ida",
                                  "password": "password123"},
                  follow_redirects=True).get_data(as_text=True)
found = re.search(r"code:\s*(\d{6})", body)

check("with no mail server the code is offered for development",
      found is not None,
      "there is no way to sign in on a machine with no SMTP")

again.post("/login/verify", data={"otp_code": found.group(1)},
           follow_redirects=True)
check("password and then the code signs in",
      all_the_way_in(again),
      "the right code did not complete the sign-in")

wrong_code = app.test_client()
wrong_code.post("/login", data={"username": "ida",
                                "password": "password123"},
                follow_redirects=True)
wrong_code.post("/login/verify", data={"otp_code": "000000"},
                follow_redirects=True)
check("but a wrong code does not",
      not all_the_way_in(wrong_code),
      "any six digits got in")

check("sending needs no extra package",
      "smtplib" not in io.open("requirements.txt", encoding="utf-8").read(),
      "smtplib is in the standard library and must not be a dependency")


# ==========================================================
print("\n=== 8. Ten minutes, a resend, and everybody ===")
# ==========================================================
# The code used to be an admin thing on a five-minute fuse. A password
# on its own is a password on its own whoever holds it, and the account
# that takes the money is the cashier's - so everybody who signs in is
# asked for one. Five minutes was also not long enough for a code
# sitting in a spam folder or on a phone face-down on a counter.

check("the code lasts ten minutes",
      application.OTP_EXPIRY_SECONDS == 600,
      "it lasts %d seconds" % application.OTP_EXPIRY_SECONDS)

check("and .env does not quietly say otherwise",
      "OTP_EXPIRY_SECONDS=300" not in io.open(
          ".env.example", encoding="utf-8").read(),
      "the example env file still sets five minutes, so anybody "
      "copying it gets the old window back")

# A cashier - not an admin - signing in.
owner.post("/users/add", data={
    "full_name": "Cass Ier", "username": "cass",
    "email": "cass@cafe.com",
    "phone_country": "India", "phone_number": "9876500055",
    "password": "password123", "confirm_password": "password123",
    "role": "staff", "_csrf_token": csrf(owner)}, follow_redirects=True)

till = app.test_client()
till_body = till.post("/login", data={"username": "cass",
                                      "password": "password123"},
                      follow_redirects=True).get_data(as_text=True)

check("a cashier is asked for a code too",
      not all_the_way_in(till) and signed_in(till),
      "the password alone let a member of staff straight in")

till_code = re.search(r"code:\s*(\d{6})", till_body)
landing = till.post("/login/verify",
                    data={"otp_code": till_code.group(1)},
                    follow_redirects=False)

check("and the code signs them in",
      all_the_way_in(till), "the code did not complete their sign-in")

check("landing them somewhere they are allowed",
      "/orders/add" in (landing.headers.get("Location") or ""),
      "they were sent to %r - the dashboard is an admin page and "
      "bounces a cashier straight back out"
      % landing.headers.get("Location"))

# Somebody with no way of being reached still gets in on their password.
# A lockout would be a worse answer than a gap.
owner.post("/users/add", data={
    "full_name": "No Contact", "username": "nocontact",
    "phone_country": "India", "phone_number": "",
    "password": "password123", "confirm_password": "password123",
    "role": "staff", "_csrf_token": csrf(owner)}, follow_redirects=True)

stranded = app.test_client()
stranded.post("/login", data={"username": "nocontact",
                              "password": "password123"},
              follow_redirects=True)

check("but somebody with no address and no number is not locked out",
      all_the_way_in(stranded),
      "an account with nowhere to send a code could not sign in at all")

# A code that has run out is refused, and the resend gives a new one.
expired = app.test_client()
expired.post("/login", data={"username": "cass", "password": "password123"},
             follow_redirects=True)
mysql_shim._DB.execute(
    "UPDATE login_otp_codes SET expires_at = datetime(NOW(), '-1 seconds')")
mysql_shim._DB.commit()

stale = expired.post("/login/verify", data={"otp_code": "123456"},
                     follow_redirects=True).get_data(as_text=True)

check("a code past its ten minutes is refused",
      "expired" in stale.lower() and not all_the_way_in(expired),
      "an out-of-date code was still accepted")

check("and the page offers a new one",
      "Resend" in stale or "resend" in stale,
      "there is no way to ask for another code")

# The button has a cooldown, and the code was sent moments ago, so the
# clock is wound back rather than waited out. The cooldown itself is
# checked just below.
blocked = expired.post("/login/resend-otp", follow_redirects=True).get_data(
    as_text=True)
check("asking again straight away is turned down",
      "wait" in blocked.lower(),
      "the resend button can be held down to post somebody a hundred "
      "emails")

with expired.session_transaction() as sess:
    sess["otp_last_sent"] = 0

fresh = expired.post("/login/resend-otp", follow_redirects=True).get_data(
    as_text=True)
fresh_code = re.search(r"code:\s*(\d{6})", fresh)

check("resending issues a working code",
      fresh_code is not None,
      "the resend produced nothing to type in")

expired.post("/login/verify", data={"otp_code": fresh_code.group(1)},
             follow_redirects=True)
check("and the new code signs them in",
      all_the_way_in(expired),
      "the resent code did not work either")

# The resend cannot be used to post somebody a hundred emails.
again_now = expired.post("/login/resend-otp", follow_redirects=True)
check("the resend button has a cooldown",
      application.OTP_RESEND_COOLDOWN_SECONDS > 0,
      "a button that sends an email with no cooldown is a way to "
      "flood somebody's inbox")

# And the customer with the QR code is untouched by any of it: they
# have no account to be challenged for.
# The token is minted the first time the owner opens the QR page, so
# it has to be asked for before there is one to visit.
owner.get("/settings/qr")
menu_token = mysql_shim._DB.execute(
    "SELECT public_token FROM cafes WHERE cafe_name = 'Identity Cafe'"
).fetchone()

check("the cafe has a public address to test with",
      menu_token and menu_token[0],
      "no token was minted, so the check below proves nothing")

stranger = app.test_client()
check("the QR menu still needs no sign-in at all",
      stranger.get("/m/%s" % menu_token[0]).status_code == 200,
      "the customer's page asks for something it never should")

with stranger.session_transaction() as sess:
    check("and the customer is never asked for a code",
          not sess.get("otp_user_id"),
          "somebody with no account was put through a sign-in code")


# ==========================================================
print("\n=== 9. Not asking the database twice ===")
# ==========================================================
# One row - who is signed in, and the cafe around them - was fetched on
# every page load. Name, role, theme, tax rate, branding: all of it the
# same answer every time, and about half a second of waiting at the
# latency this app runs at.
#
# Being fast is the easy half. The half worth testing is that nobody is
# ever shown something that is no longer true.

traffic = []
_real_execute = mysql_shim._Cursor.execute


def _watch(self, sql, params=()):
    traffic.append(" ".join(str(sql).split()))
    return _real_execute(self, sql, params)


USER_ROW = "SELECT u.user_id, u.username"
SCREENS = ["/dashboard", "/orders/add", "/kitchen", "/billing", "/foods"]


def user_row_reads(client, seconds):
    was = application.CACHE_SECONDS
    application.CACHE_SECONDS = seconds
    application.cache_clear()
    traffic[:] = []
    mysql_shim._Cursor.execute = _watch
    for screen in SCREENS:
        client.get(screen)
    mysql_shim._Cursor.execute = _real_execute
    application.CACHE_SECONDS = was
    return sum(1 for q in traffic if q.startswith(USER_ROW))


without = user_row_reads(owner, 0)
with_it = user_row_reads(owner, 30)

check("without the cache the same row is read on every screen",
      without == len(SCREENS),
      "%d reads across %d screens - if this is not one per screen the "
      "comparison below means nothing" % (without, len(SCREENS)))

check("with it, once",
      with_it == 1,
      "%d reads across %d screens" % (with_it, len(SCREENS)))

# ---- and it is never stale for the person who changed it ----
application.cache_clear()
owner.get("/dashboard")                     # warm it

owner.post("/settings/branding", data={
    "cafe_name": "Renamed Mid-Test", "brand_name": "Renamed Mid-Test",
    "brand_tagline": "Changed just now",
    "_csrf_token": csrf(owner)}, follow_redirects=True)

check("renaming the cafe shows at once to whoever renamed it",
      "Renamed Mid-Test" in owner.get("/dashboard").get_data(as_text=True),
      "the page still shows the old name, which is the cache lying")

owner.post("/settings/tax", data={
    "tax_percent": "17", "discount_percent": "3",
    "_csrf_token": csrf(owner)}, follow_redirects=True)

check("and so does a change to what every bill charges",
      "17" in owner.get("/settings/tax").get_data(as_text=True),
      "the rates page shows the old figures")

# The one that got away. timezone_guess writes cafes.timezone, and it
# was on the list of endpoints that skip invalidation because a kitchen
# screen posts to them constantly. It kept the old clock alive and
# printed tickets in UTC.
check("no endpoint that writes the cached row skips invalidation",
      "timezone_guess" not in application._CACHE_KEEPERS,
      "an endpoint that writes cafes.timezone is excused from clearing "
      "the cache, so the cafe's clock goes stale and tickets print in "
      "the wrong time")

# The list used to be checked by its names - anything starting
# "kitchen_" was assumed to be a poll. That was a guess dressed as a
# test, and it stopped being true the moment taking an order joined the
# list for a real measured reason. What the list actually promises is
# behavioural, so it is checked behaviourally further down: each one is
# driven and every statement it runs is watched.

# A menu, because the checks below take a real order and look at real
# stock. Without one they quietly measure nothing: no dishes means no
# quantity boxes to post and no inventory rows to read.
owner.post("/categories/add", data={
    "category_name": "Mains", "description": "",
    "_csrf_token": csrf(owner)}, follow_redirects=True)
_cats = re.findall(r'<option value="(\d+)">',
                   owner.get("/foods/add").get_data(as_text=True))
owner.post("/foods/add", data={
    "food_name": "Cache Bun", "category_id": _cats[-1], "price": "90",
    "quantity": "500", "minimum_stock": "5", "description": "",
    "_csrf_token": csrf(owner)}, follow_redirects=True)

check("there is a dish to order and stock to read",
      "Cache Bun" in owner.get("/inventory").get_data(as_text=True),
      "the checks below would measure an empty cafe and pass on nothing")


# ---- the exclusion list, driven rather than trusted ----
# Each of these skips cache invalidation. That is only safe while none
# of them writes to users or cafes. Rather than read them and hope,
# they are actually called and every statement is watched.
# The columns actually carried on the cached row, asked of the app
# rather than listed here - so adding a column to that query widens
# this check on its own.
application.cache_clear()
owner.get("/dashboard")
with owner.session_transaction() as _sess:
    _who, _where = _sess.get("user_id"), _sess.get("cafe_id")

CACHED_COLUMNS = {
    name.lower() for name in
    (application.cache_get("userrow:%s:%s" % (_who, _where)) or {})
}

check("the cached row was found, so its columns are known",
      len(CACHED_COLUMNS) > 8,
      "only %d columns came back; the check below would pass on an "
      "empty set and mean nothing" % len(CACHED_COLUMNS))


def writes_to_cached_rows(doing):
    """
    Statements that change a column the cached row carries.

    Not "writes to users or cafes": the kitchen heartbeat stamps
    cafes.kitchen_seen_at every few seconds and that column is not on
    the cached row, so it cannot make anything stale. What matters is
    whether a column somebody will later read OUT of the cache was
    changed without the cache being told.
    """
    traffic[:] = []
    mysql_shim._Cursor.execute = _watch
    try:
        doing()
    finally:
        mysql_shim._Cursor.execute = _real_execute

    guilty = []
    for statement in traffic:
        upper = statement.strip().upper()
        if not upper.startswith(("UPDATE", "INSERT", "DELETE")):
            continue
        if "users" not in statement.lower() and                 "cafes" not in statement.lower():
            continue
        touched = [c for c in CACHED_COLUMNS
                   if c not in ("user_id", "cafe_id")
                   and re.search(r"%s" % re.escape(c), statement,
                                 re.I)]
        if touched:
            guilty.append("%s (%s)" % (statement[:60], ", ".join(touched[:3])))
    return guilty


_menu = owner.get("/orders/add").get_data(as_text=True)
_pick = re.findall(r'id="quantity_(\d+)"', _menu)

if _pick:
    def take_an_order():
        owner.post("/orders/add",
                   data={"quantity_%s" % _pick[0]: "1",
                         "_csrf_token": csrf(owner)},
                   follow_redirects=True)

    spilled = writes_to_cached_rows(take_an_order)
    check("taking an order writes nothing that is cached",
          not spilled,
          "add_order skips invalidation but wrote %s - the cached row "
          "is now stale and nothing will clear it" % spilled[:2])

    _latest = mysql_shim._DB.execute(
        "SELECT order_id FROM orders ORDER BY order_id DESC LIMIT 1"
    ).fetchone()

    def cancel_it():
        owner.post("/orders/cancel/%d" % _latest[0],
                   data={"_csrf_token": csrf(owner)},
                   follow_redirects=True)

    check("cancelling one writes nothing that is cached",
          not writes_to_cached_rows(cancel_it),
          "cancel_order skips invalidation but writes a cached row")

check("the kitchen's own polling writes nothing that is cached",
      not writes_to_cached_rows(
          lambda: owner.post("/api/kitchen/heartbeat",
                             data={"_csrf_token": csrf(owner)},
                             follow_redirects=True)),
      "the heartbeat skips invalidation but writes a cached row")


# ---- and stock is deliberately NOT cached ----
# Two tills racing for the last croissant is settled by a row lock
# inside the transaction. A cache in front of that would be a cache in
# front of the only thing keeping the count honest.
#
# Read immediately before the check. This setup used to sit further up
# and a later block overwrote `traffic` in between, so the assertion was
# reading the heartbeat's statements and calling them the inventory
# page's.
application.cache_clear()
owner.get("/inventory")
traffic[:] = []
mysql_shim._Cursor.execute = _watch
owner.get("/inventory")
mysql_shim._Cursor.execute = _real_execute

check("stock is read fresh every time it is shown",
      any("inventory" in q.lower() for q in traffic),
      "the inventory page ran %d statements and none of them read "
      "stock, which would mean somebody could be sold something that "
      "is gone: %s" % (len(traffic), [q[:40] for q in traffic[:3]]))


# ==========================================================
print("\n=== 10. The cache, shared between the workers ===")
# ==========================================================
# Without a Redis every worker keeps its own copy, so a change in one
# is invisible to the other two until their copies age out. With one
# there is a single copy and dropping it drops it for everybody.
#
# Tested against a stand-in rather than a real server: the client this
# app uses is five methods wide, and a stand-in can be made to fail on
# demand, which a real Redis obligingly will not.


class StandInRedis:
    """Enough of a Redis to exercise the code that talks to one."""

    def __init__(self):
        self.kept = {}
        self.broken = False
        self.deletes = 0

    def _check(self):
        if self.broken:
            raise RuntimeError("connection refused")

    def ping(self):
        self._check()
        return True

    def get(self, key):
        self._check()
        return self.kept.get(key)

    def setex(self, key, seconds, value):
        self._check()
        self.kept[key] = value

    def delete(self, *keys):
        self._check()
        self.deletes += 1
        for key in keys:
            self.kept.pop(key, None)

    def scan_iter(self, match=None, count=None):
        self._check()
        import fnmatch
        for key in list(self.kept):
            if match is None or fnmatch.fnmatch(
                    key.decode() if isinstance(key, bytes) else key, match):
                yield key


stand_in = StandInRedis()
shared = object.__new__(application._SharedCache)
shared._redis = stand_in
shared._complained = False

_was_shared = application._SHARED
application._SHARED = shared
try:
    application.cache_clear()
    application.cache_put("userrow:1:1", {"cafe_name": "Shared Cafe"})

    check("a value written goes into the shared store",
          any(b"Shared Cafe" in v for v in stand_in.kept.values()),
          "nothing reached the shared cache: %s" % list(stand_in.kept))

    check("and comes back as what it was",
          application.cache_get("userrow:1:1") == {"cafe_name": "Shared Cafe"},
          "it came back as %r" % application.cache_get("userrow:1:1"))

    check("keys are prefixed, so two deployments can share one Redis",
          all(k.startswith(application.CACHE_PREFIX + ":")
              for k in stand_in.kept),
          "unprefixed keys would let one cafe system read another's: %s"
          % list(stand_in.kept)[:3])

    # Money survives the trip. A tax rate that comes back as a float is
    # a rounding error waiting for a busy Saturday.
    application.cache_put("rates:1", {"tax": Decimal("18.00"),
                                      "discount": Decimal("7.50")})
    came_back = application.cache_get("rates:1")
    check("a rate comes back as a Decimal, not a float",
          isinstance(came_back["tax"], Decimal)
          and came_back["tax"] == Decimal("18.00"),
          "it came back as %r (%s)"
          % (came_back["tax"], type(came_back["tax"]).__name__))

    # Dropping a cafe drops that cafe and nothing else.
    application.cache_put("userrow:9:42", {"cafe_name": "Forty Two"})
    application.cache_put("rates:42", {"tax": Decimal("1")})
    application.cache_put("userrow:9:7", {"cafe_name": "Seven"})
    application.cache_drop_cafe(42)

    check("dropping one cafe forgets that cafe",
          application.cache_get("userrow:9:42") is None
          and application.cache_get("rates:42") is None,
          "a cafe's rows survived being dropped")

    check("and leaves the others alone",
          application.cache_get("userrow:9:7") == {"cafe_name": "Seven"},
          "dropping one cafe emptied another's rows too")

    # ---- and a Redis that falls over must not take the site with it ----
    application.cache_put("userrow:5:5", {"cafe_name": "Before The Fall"})
    stand_in.broken = True

    # The fallback has to actually work, not merely not raise. With
    # the shared store unreachable, a value put now must be readable
    # now - out of this worker's own memory.
    application.cache_put("userrow:5:5", {"cafe_name": "During The Fall"})

    check("a broken cache still stores and returns a value",
          application.cache_get("userrow:5:5")
          == {"cafe_name": "During The Fall"},
          "with Redis down a write then a read gave %r, so the "
          "fallback is not carrying anything"
          % application.cache_get("userrow:5:5"))

    try:
        application.cache_put("userrow:6:6", {"cafe_name": "Also During"})
        application.cache_drop("userrow:6:6")
        application.cache_drop_cafe(6)
        survived = True
    except Exception:
        survived = False

    check("and writes, drops and clears do not raise",
          survived,
          "a Redis outage would have shown customers an error page")

    check("and the value really is in this worker's own memory",
          application._memory_get("userrow:5:5")
          == {"cafe_name": "During The Fall"},
          "nothing was kept locally, so every read during an outage "
          "would go to the database")

    stand_in.broken = False
finally:
    application._SHARED = _was_shared
    application.cache_clear()

check("with no REDIS_URL the cache is the worker's own memory",
      application.cache_backend_name() == "memory",
      "it says %r; nothing in the tests sets REDIS_URL, so a shared "
      "cache here would mean it is reaching a real server"
      % application.cache_backend_name())

check("and the health endpoint says which one is in use",
      "cache" in (app.test_client().get("/healthz").get_json() or {}),
      "setting REDIS_URL and having it quietly not connect looks "
      "exactly like it working, until two workers disagree")


print("\n" + "=" * 62)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 62)
sys.exit(1 if FAILED else 0)
