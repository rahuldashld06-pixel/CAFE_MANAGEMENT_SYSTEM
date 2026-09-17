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

for path in ["/settings/tax", "/account/photo", "/settings/theme",
             "/account/password"]:
    response = a.get(path)
    check("%s renders" % path, response.status_code == 200,
          "status=%d" % response.status_code)

for path in ["/settings/tax", "/account/photo", "/settings/theme",
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


print("\n=== 10. What the top corner says, by default ===")
SHELL_PAGES = ["/orders/add", "/orders", "/foods", "/inventory",
               "/categories", "/billing"]


def corner(client):
    """The name, the tagline and the symbol the shell actually draws."""
    html = client.get("/orders/add").get_data(as_text=True)
    found = re.search(
        r'<span class="brand-text">(.*?)<small>(.*?)</small>', html, re.S)
    logo = re.search(r'class="brand-mark">\s*<img src="([^"]+)"', html)
    return (found.group(1).strip() if found else None,
            found.group(2).strip() if found else None,
            logo.group(1) if logo else None)


name, tagline, logo = corner(a)
check("a cafe that has chosen nothing reads Cafe Manager",
      name == "Cafe Manager", "it reads %r" % name)
check("with the line that has always gone under it",
      tagline == "Food &amp; Service Admin", "it reads %r" % tagline)
check("and no uploaded symbol beside it", logo is None,
      "a symbol is drawn where none was set")
check("the drawn coffee cup stands in until one is uploaded",
      "brand-glyph" in a.get("/orders/add").get_data(as_text=True),
      "nothing is drawn in the symbol's place")

missing = [path for path in SHELL_PAGES
           if "Food &amp; Service Admin" not in a.get(path).get_data(as_text=True)]
check("it is on every page", not missing, "missing from: %s" % missing)
check("no cup icon is drawn either",
      "bi-cup-hot-fill" not in a.get("/orders/add").get_data(as_text=True),
      "the old cup placeholder is back")


print("\n=== 11. An admin can make it their own ===")
check("the page is reachable",
      a.get("/settings/branding").status_code == 200,
      "got HTTP %s" % a.get("/settings/branding").status_code)
check("and offered in the profile menu",
      "Name &amp; Symbol" in a.get("/orders/add").get_data(as_text=True),
      "there is no way into it")

a.post("/settings/branding", data={
    "brand_name": "Spice Garden", "brand_tagline": "Family Restaurant",
    "_csrf_token": csrf(a)}, content_type="multipart/form-data",
    follow_redirects=True)

name, tagline, logo = corner(a)
check("the name they typed is what every page says", name == "Spice Garden",
      "it reads %r" % name)
check("and the tagline with it", tagline == "Family Restaurant",
      "it reads %r" % tagline)

a.post("/settings/branding", data={
    "brand_name": "Spice Garden", "brand_tagline": "Family Restaurant",
    "logo": (io.BytesIO(PNG), "logo.png"), "_csrf_token": csrf(a)},
    content_type="multipart/form-data", follow_redirects=True)
check("a symbol appears once one is uploaded",
      corner(a)[2] and "/media/cafe/" in corner(a)[2],
      "no symbol is drawn: %r" % (corner(a)[2],))
shell = a.get("/orders/add").get_data(as_text=True)
check("and it is on the small-screen bar too",
      re.search(r'topbar-brand__mark">\s*<img src="/media/cafe/', shell)
      is not None,
      "the phone bar still shows the stand-in rather than the symbol")
check("the stand-in steps aside once a symbol is set",
      "brand-glyph" not in shell,
      "the cup is drawn alongside the uploaded symbol")

a.post("/settings/branding", data={
    "action": "remove_logo", "_csrf_token": csrf(a)}, follow_redirects=True)
check("removing the symbol takes it away", corner(a)[2] is None,
      "the symbol is still drawn")
check("and the cup comes back in its place",
      "brand-glyph" in a.get("/orders/add").get_data(as_text=True),
      "the symbol's place is empty now")
check("but leaves the name alone", corner(a)[0] == "Spice Garden",
      "the name became %r" % corner(a)[0])


print("\n=== 12. Emptying a field means the default, not a blank corner ===")
a.post("/settings/branding", data={
    "brand_name": "", "brand_tagline": "", "_csrf_token": csrf(a)},
    content_type="multipart/form-data", follow_redirects=True)
name, tagline, _ = corner(a)
check("the name falls back", name == "Cafe Manager", "it reads %r" % name)
check("and so does the tagline", tagline == "Food &amp; Service Admin",
      "it reads %r" % tagline)

# Put it back for the checks below.
a.post("/settings/branding", data={
    "brand_name": "Spice Garden", "brand_tagline": "Family Restaurant",
    "_csrf_token": csrf(a)}, content_type="multipart/form-data",
    follow_redirects=True)


print("\n=== 13. Only an admin may change it, but everyone sees it ===")
a.post("/users/add", data={
    "full_name": "Cash", "username": "cash2", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(a)}, follow_redirects=True)

cashier = app.test_client()
cashier.post("/login", data={"username": "cash2", "password": "password123"},
             follow_redirects=True)

check("a cashier sees the name the admin chose",
      corner(cashier)[0] == "Spice Garden",
      "the cashier's corner reads %r" % corner(cashier)[0])
check("but is not offered the page",
      "Name &amp; Symbol" not in
      cashier.get("/orders/add").get_data(as_text=True),
      "the menu entry is visible to a cashier")
check("and cannot open it",
      cashier.get("/settings/branding",
                  follow_redirects=False).status_code in (302, 303),
      "a cashier got in")

cashier.post("/settings/branding", data={
    "brand_name": "Hijacked", "_csrf_token": csrf(cashier)},
    content_type="multipart/form-data", follow_redirects=True)
check("posting to it changes nothing", corner(a)[0] == "Spice Garden",
      "a cashier renamed the cafe to %r" % corner(a)[0])
check("the endpoint is not on the staff allowlist",
      "branding" not in application.STAFF_ALLOWED_ENDPOINTS,
      "the allowlist would let a non-admin through")


print("\n=== 14. One cafe's corner is not another's ===")
sign_in(b, "beta")
check("cafe B is still on the default",
      corner(b)[0] == "Cafe Manager",
      "cafe B reads %r" % corner(b)[0])
check("and cafe A keeps its own", corner(a)[0] == "Spice Garden",
      "cafe A reads %r" % corner(a)[0])


print("\n=== 15. Showing the corner costs no extra query ===")
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

check("asking for it again runs no query at all", after_user == 0,
      "it ran %d extra queries per page" % after_user)
check("and it is the right cafe's name",
      branding["brand_name"] == "Spice Garden",
      "the cached branding says %r" % branding["brand_name"])

print("\n=== 16. A settings screen returns you where you were ===")
# The profile menu lives in the shell, which instant navigation never
# re-renders, so the server cannot work out which page is on screen. The
# page is stamped onto the link when it is clicked and read back here.
BACK = re.compile(r'href="([^"]+)" class="btn btn--ghost" data-back')

SETTINGS = ["/settings/tax", "/settings/theme", "/settings/branding",
            "/settings/printing", "/account/password", "/account/photo"]

wrong = []
for path in SETTINGS:
    html = a.get(path + "?next=/billing").get_data(as_text=True)
    found = BACK.search(html)
    if not found or found.group(1) != "/billing":
        wrong.append((path, found.group(1) if found else None))

check("every settings screen offers to go back where it was opened from",
      not wrong, "these point elsewhere: %s" % wrong)

check("and with nothing to go back to, an admin still gets the dashboard",
      (BACK.search(a.get("/settings/tax").get_data(as_text=True))
       or [None, ""])[1] == "/",
      "the fallback is not the dashboard")


print("\n=== 17. And saving takes you back too ===")
saved = a.post("/settings/tax?next=/billing",
               data={"tax_percent": "9", "_csrf_token": csrf(a)})
check("a saved setting returns to the page behind it",
      saved.headers.get("Location", "").endswith("/billing"),
      "it went to %s" % saved.headers.get("Location"))

refused = a.post("/settings/tax?next=/billing",
                 data={"tax_percent": "not a number",
                       "_csrf_token": csrf(a)})
check("but a refused one stays put, so it can be corrected",
      "/settings/tax" in refused.headers.get("Location", ""),
      "it went to %s" % refused.headers.get("Location"))
check("and still remembers the way back",
      "next=/billing" in refused.headers.get("Location", ""),
      "correcting it would lose the way back: %s"
      % refused.headers.get("Location"))


print("\n=== 18. It will not send anyone off the site ===")
# Somewhere to come back to is worth remembering; somewhere to be sent is
# worth refusing.
for hostile in ("//evil.example.com/", "https://evil.example.com/",
                "javascript:alert(1)", "/\evil.example.com"):
    landed = a.post("/settings/tax?next=" + hostile,
                    data={"tax_percent": "6", "_csrf_token": csrf(a)}
                    ).headers.get("Location", "")
    check("%r is ignored" % hostile[:26],
          "evil.example.com" not in landed and "javascript" not in landed,
          "it would have sent someone to %s" % landed)

print("\n=== 19. Keeping the site awake ===")
# The hosting plan stops the service when nothing asks it for anything,
# and the next visitor then waits for start-up plus a fresh connection to
# a database half a second away. The site calls its own health check on a
# timer so that wait is paid by nobody.
import os as _os  # noqa: E402


def target_when(**environment):
    """What the app would decide to call, given this environment."""
    kept = {}
    for name in ("APP_ENV", "KEEP_AWAKE", "KEEP_AWAKE_URL",
                 "RENDER_EXTERNAL_URL"):
        kept[name] = _os.environ.pop(name, None)
    _os.environ.update({k: v for k, v in environment.items() if v})

    was = application.IS_PRODUCTION
    application.IS_PRODUCTION = (
        environment.get("APP_ENV", "production") != "development")
    try:
        return application.keep_awake_target()
    finally:
        application.IS_PRODUCTION = was
        for name, value in kept.items():
            _os.environ.pop(name, None)
            if value is not None:
                _os.environ[name] = value


check("a live site with an address of its own calls itself",
      target_when(APP_ENV="production",
                  RENDER_EXTERNAL_URL="https://mysite.onrender.com")
      == "https://mysite.onrender.com/healthz",
      "it would call %r" % target_when(
          APP_ENV="production",
          RENDER_EXTERNAL_URL="https://mysite.onrender.com"))

check("a trailing slash does not become a double one",
      target_when(APP_ENV="production",
                  KEEP_AWAKE_URL="https://mysite.example/")
      == "https://mysite.example/healthz",
      "it would call %r" % target_when(
          APP_ENV="production", KEEP_AWAKE_URL="https://mysite.example/"))

check("a machine in development does not ping anything",
      target_when(APP_ENV="development",
                  KEEP_AWAKE_URL="https://mysite.example") is None,
      "a developer's laptop would be calling the live site")

check("with no address it stays quiet rather than guessing one",
      target_when(APP_ENV="production") is None,
      "it would call something it invented")

check("something that is not an address is refused",
      target_when(APP_ENV="production",
                  KEEP_AWAKE_URL="not-a-url") is None,
      "it would try to call nonsense")

check("and it can be switched off",
      target_when(APP_ENV="production", KEEP_AWAKE="0",
                  RENDER_EXTERNAL_URL="https://mysite.onrender.com") is None,
      "there is no way to stop it")

# The health check is the right thing to call: it is cheap, it needs no
# session, and it touches the database, so the connection stays warm too.
check("what it calls needs no sign-in",
      "healthz" in application.app.view_functions
      and app.test_client().get("/healthz").status_code == 200,
      "the keep-awake would be redirected to the sign-in page")

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
