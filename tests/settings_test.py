"""
Offline tests for the pages reached from the profile menu.

Covers the two new ones - the café's tax rate and a user's own profile
photo - plus the things that are easy to get wrong about them: who may
change the rate, that it actually reaches the money, and that a photo is
not readable across café boundaries.

Run with:  python tests/settings_test.py
"""
import io
import os
import re
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "settings-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import app as application     # noqa: E402

app = application.app
app.config["TESTING"] = True

PASSED, FAILED = [], []

# A 1x1 PNG - the smallest thing that is genuinely an image.
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def sign_up(client, cafe, username):
    return client.post("/register", data={
        "cafe_name": cafe, "full_name": username.title(), "username": username,
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)


def sign_in(client, username):
    return client.post("/login", data={"username": username,
                                       "password": "password123"},
                       follow_redirects=True)


def message(response):
    found = re.findall(r'class="alert">([^<]+)<', response.get_data(as_text=True))
    return found[0].strip() if found else ""


def set_tax(client, value):
    return client.post("/settings/tax",
                       data={"tax_percent": str(value),
                             "_csrf_token": csrf(client)},
                       follow_redirects=True)


def build_menu(client, price="100"):
    client.post("/categories/add",
                data={"category_name": "Coffee", "description": "",
                      "_csrf_token": csrf(client)}, follow_redirects=True)
    category = re.search(r'<option value="(\d+)">',
                         client.get("/foods/add").get_data(as_text=True)).group(1)
    client.post("/foods/add", data={
        "food_name": "Latte", "category_id": category, "price": price,
        "quantity": "500", "description": "", "_csrf_token": csrf(client)},
        follow_redirects=True)
    return re.findall(r'id="quantity_(\d+)"',
                      client.get("/orders/add").get_data(as_text=True))[0]


def place_order(client, food_id, quantity=1):
    return client.post("/orders/add",
                       data={"quantity_%s" % food_id: str(quantity),
                             "_csrf_token": csrf(client)},
                       follow_redirects=True)


def newest_bill_tax(client):
    """The tax on the most recent bill, straight from the database."""
    client.get("/billing")          # raises any missing bill
    row = mysql_shim._DB.execute(
        "SELECT tax, subtotal FROM bills ORDER BY bill_id DESC LIMIT 1"
    ).fetchone()
    return (Decimal(str(row[0])), Decimal(str(row[1])))


print("\n=== 1. The pages exist and are reachable ===")
a = app.test_client()
sign_up(a, "Alpha Cafe", "alpha")

for path in ["/settings/tax", "/account/photo", "/settings/branding",
             "/account/password"]:
    response = a.get(path)
    check("%s renders" % path, response.status_code == 200,
          "status=%d" % response.status_code)

for path in ["/settings/tax", "/account/photo", "/settings/branding",
             "/account/password"]:
    html = a.get(path).get_data(as_text=True)
    check("%s offers a back button" % path, "data-back" in html,
          "no back control on the page")
    check("%s uses the themed form panel" % path, "form-panel" in html,
          "fields would render as unstyled browser defaults")

check("the profile menu links to the tax page",
      "/settings/tax" in a.get("/dashboard").get_data(as_text=True))
check("the profile menu links to the photo page",
      "/account/photo" in a.get("/dashboard").get_data(as_text=True))


print("\n=== 2. Only an admin may change the rate ===")
a.post("/users/add", data={
    "full_name": "Till One", "username": "till1", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(a)}, follow_redirects=True)

staff = app.test_client()
sign_in(staff, "till1")

check("a cashier is redirected away from the tax page",
      staff.get("/settings/tax").status_code in (301, 302, 303),
      "status=%d" % staff.get("/settings/tax").status_code)
check("and cannot post a new rate",
      staff.post("/settings/tax", data={"tax_percent": "99",
                                        "_csrf_token": csrf(staff)}
                 ).status_code in (301, 302, 303))
check("the rate is unchanged after that attempt",
      application.get_tax_percent(1) == Decimal("5.00"),
      "rate is now %s" % application.get_tax_percent(1))

check("a cashier can still reach their own profile photo",
      staff.get("/account/photo").status_code == 200,
      "staff are locked out of their own picture")
check("the tax link is not even shown to them",
      "/settings/tax" not in staff.get("/orders/add").get_data(as_text=True))


print("\n=== 3. The rate reaches the money ===")
food = build_menu(a, price="100")

set_tax(a, "12")
place_order(a, food, 1)
tax, subtotal = newest_bill_tax(a)
check("a 12% rate charges 12% of the subtotal",
      subtotal == Decimal("100.00") and tax == Decimal("12.00"),
      "subtotal=%s tax=%s" % (subtotal, tax))

set_tax(a, "0")
place_order(a, food, 1)
tax, subtotal = newest_bill_tax(a)
check("a 0% rate charges no tax", tax == Decimal("0.00"), "tax=%s" % tax)

set_tax(a, "7.5")
place_order(a, food, 2)
tax, subtotal = newest_bill_tax(a)
check("a fractional rate is honoured",
      subtotal == Decimal("200.00") and tax == Decimal("15.00"),
      "subtotal=%s tax=%s" % (subtotal, tax))

check("the New Order screen totals with the same rate",
      "var TAX_RATE = 0.075" in a.get("/orders/add").get_data(as_text=True),
      "the on-screen total would disagree with the bill")


print("\n=== 4. Bills already raised keep their rate ===")
first_bill = mysql_shim._DB.execute(
    "SELECT tax FROM bills ORDER BY bill_id ASC LIMIT 1").fetchone()
check("the 12% bill still shows 12%, not the current 7.5%",
      Decimal(str(first_bill[0])) == Decimal("12.00"),
      "the earlier bill was rewritten to %s" % first_bill[0])


print("\n=== 5. Bad rates are refused ===")
for bad, why in [("-1", "negative"), ("101", "over 100"), ("abc", "not a number")]:
    before = application.get_tax_percent(1)
    response = set_tax(a, bad)
    after = application.get_tax_percent(1)
    check("%r is refused (%s)" % (bad, why),
          after == before and message(response) != "",
          "rate went %s -> %s" % (before, after))

check("100% is allowed at the boundary",
      set_tax(a, "100") is not None
      and application.get_tax_percent(1) == Decimal("100.00"),
      "rate is %s" % application.get_tax_percent(1))
set_tax(a, "5")


print("\n=== 6. Profile photos ===")
response = a.post("/account/photo", data={
    "photo": (io.BytesIO(PNG), "me.png"), "_csrf_token": csrf(a)},
    content_type="multipart/form-data", follow_redirects=True)
check("uploading a photo is accepted", "updated" in message(response).lower(),
      "message: %r" % message(response))

owner_id = mysql_shim._DB.execute(
    "SELECT user_id FROM users WHERE username = 'alpha'").fetchone()[0]

served = a.get("/media/user/%d" % owner_id)
check("the photo is served back", served.status_code == 200 and served.data == PNG,
      "status=%d len=%d" % (served.status_code, len(served.data)))
check("it is cached privately, not publicly",
      "private" in (served.headers.get("Cache-Control") or ""),
      "Cache-Control=%r" % served.headers.get("Cache-Control"))

check("the avatar now shows the photo instead of an initial",
      "/media/user/%d" % owner_id in a.get("/dashboard").get_data(as_text=True),
      "the topbar is still rendering the initial")

response = a.post("/account/photo", data={
    "photo": (io.BytesIO(b"not an image"), "notes.txt"),
    "_csrf_token": csrf(a)},
    content_type="multipart/form-data", follow_redirects=True)
check("a non-image upload is refused",
      "image" in message(response).lower(), "message: %r" % message(response))
check("and the old photo survives that attempt",
      a.get("/media/user/%d" % owner_id).data == PNG)

print("\n=== 7. A photo is not readable across cafés ===")
b = app.test_client()
sign_up(b, "Beta Cafe", "beta")
check("café B cannot fetch café A's photo",
      b.get("/media/user/%d" % owner_id).status_code == 404,
      "status=%d - photos leak between tenants"
      % b.get("/media/user/%d" % owner_id).status_code)

print("\n=== 8. Removing a photo ===")
response = a.post("/account/photo",
                  data={"action": "remove", "_csrf_token": csrf(a)},
                  follow_redirects=True)
check("removing it is accepted", "removed" in message(response).lower(),
      "message: %r" % message(response))
check("the photo is gone", a.get("/media/user/%d" % owner_id).status_code == 404)
check("the avatar falls back to the initial",
      "/media/user/%d" % owner_id not in a.get("/dashboard").get_data(as_text=True))


print("\n=== 9. Each café keeps its own rate ===")
set_tax(a, "18")
with b.session_transaction() as sess:
    beta_cafe = sess.get("cafe_id")
check("café B is untouched by café A's rate",
      application.get_tax_percent(beta_cafe) == Decimal("5.00"),
      "café B is on %s" % application.get_tax_percent(beta_cafe))


print("\n=== 10. The cafe's own name and logo in the shell ===")
# The sidebar used to read "Cafe Manager / Food & Service Admin" on every
# page of every cafe. It shows the name and logo the admin set instead.
SHELL_PAGES = ["/orders/add", "/orders", "/foods", "/inventory",
               "/categories", "/billing"]

a.post("/settings/branding", data={
    "cafe_name": "Bluebird Coffee House", "_csrf_token": csrf(a)},
    content_type="multipart/form-data", follow_redirects=True)

missing = [path for path in SHELL_PAGES
           if "Bluebird Coffee House" not in a.get(path).get_data(as_text=True)]
check("the cafe's name is on every page", not missing,
      "missing from: %s" % missing)

shell = a.get("/orders/add").get_data(as_text=True)
check("the hard-coded platform name is gone",
      "Food &amp; Service Admin" not in shell,
      "the old sidebar subtitle is still there")
check("a cafe with no logo falls back to the cup",
      shell.count("bi-cup-hot-fill") == 2,
      "found %d fallback icons, expected one per brand spot"
      % shell.count("bi-cup-hot-fill"))
check("the small-screen top bar carries it too",
      'class="topbar-brand"' in shell,
      "no brand in the bar that stays at the top on a phone")

a.post("/settings/branding", data={
    "cafe_name": "Bluebird Coffee House",
    "logo": (io.BytesIO(PNG), "logo.png"),
    "_csrf_token": csrf(a)}, content_type="multipart/form-data",
    follow_redirects=True)

shell = a.get("/orders/add").get_data(as_text=True)
alpha_logos = re.findall(r'src="(/media/cafe/\d+/logo[^"]*)"', shell)
check("an uploaded logo is used in both brand spots", len(alpha_logos) == 2,
      "found %d logo images" % len(alpha_logos))
check("and the fallback icon steps aside",
      "bi-cup-hot-fill" not in shell,
      "the cup is still drawn next to the logo")


print("\n=== 11. One cafe's name never shows in another's shell ===")
sign_in(b, "beta")
b.post("/settings/branding", data={
    "cafe_name": "Second Cafe", "_csrf_token": csrf(b)},
    content_type="multipart/form-data", follow_redirects=True)

other_shell = b.get("/orders/add").get_data(as_text=True)
check("cafe B sees its own name", "Second Cafe" in other_shell,
      "cafe B's shell does not name it")
check("and not cafe A's", "Bluebird Coffee House" not in other_shell,
      "cafe A's name leaked into cafe B's shell")
# Each logo URL carries its own cafe's id, so cafe A's exact URL appearing
# in cafe B's page would be a leak of the image itself.
check("nor cafe A's logo",
      not any(url in other_shell for url in alpha_logos),
      "cafe A's logo URL appears in cafe B's shell")


print("\n=== 12. Showing the brand costs no extra query ===")
# It rides on the cafe row get_current_user() already joins. Asking the
# template to fetch it instead would be a whole extra round trip on every
# page load, on every page.
_real_connect = application.get_db_connection
_queries = {"n": 0}


class _CountingCursor:
    def __init__(self, inner):
        self._inner = inner

    def execute(self, *args, **kwargs):
        _queries["n"] += 1
        return self._inner.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _CountingConnection:
    def __init__(self, inner):
        self._inner = inner

    def cursor(self, *args, **kwargs):
        return _CountingCursor(self._inner.cursor(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._inner, name)


application.get_db_connection = (
    lambda *args, **kwargs: _CountingConnection(_real_connect(*args, **kwargs)))
try:
    with app.test_request_context("/orders/add"):
        from flask import session as flask_session
        with a.session_transaction() as sess:
            flask_session.update(dict(sess))

        application.get_current_user()       # what every request does anyway
        cafe_id = flask_session.get("cafe_id")

        _queries["n"] = 0
        branding = application.get_cafe_branding(cafe_id)
        after_user = _queries["n"]
finally:
    application.get_db_connection = _real_connect

check("asking for the branding again runs no query at all",
      after_user == 0,
      "it ran %d extra queries per page" % after_user)
check("and it is the right cafe's branding",
      branding["cafe_name"] == "Bluebird Coffee House",
      "the cached branding says %r" % branding["cafe_name"])
check("the logo is in there too", bool(branding["logo"]),
      "the cached branding has no logo")

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
