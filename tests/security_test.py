"""
The defences that only matter once something else has gone wrong.

Every check here guards a property that is invisible when it is working
and expensive when it is not. They fall into four groups.

What a browser is told it may do with our pages — the headers. There
were none at all, so a page could be framed on somebody else's site over
a Paid button, a stored value that turned out to be script had nothing
standing in its way, and an upload could be sniffed into being treated
as something other than what it was served as.

Whether a password can be guessed at machine speed. The one-time code
path counted attempts from the start; the password path counted nothing.

Whether an upload is the thing it claims to be.

And the things that were already right and must stay right: CSRF on
every writing method, one café never reading another's rows, a session
that cannot be fixed or forged, and an error that does not say which
half of a sign-in was wrong.

Run with:  python tests/security_test.py
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "security-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import app as application               # noqa: E402

app = application.app
app.config["TESTING"] = True

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def register(client, cafe, user, password="password123"):
    return client.post("/register", data={
        "cafe_name": cafe, "full_name": user.title(), "username": user,
        "phone_number": "", "password": password,
        "confirm_password": password}, follow_redirects=True)


def directives(response):
    """The CSP as a dict of directive -> list of sources."""
    policy = response.headers.get("Content-Security-Policy", "")
    out = {}
    for part in policy.split(";"):
        bits = part.split()
        if bits:
            out[bits[0]] = bits[1:]
    return out


# =====================================================================
print("\n=== 1. What a browser is told it may do here ===")
# =====================================================================

anon = app.test_client()
front = anon.get("/login")

check("a page carries a content security policy",
      "Content-Security-Policy" in front.headers,
      "a browser is told nothing about what it may load or run")

rules = directives(front)

# Clickjacking a till is a real thing to want: an invisible copy of this
# page over somebody else's, positioned so a click lands on Paid.
check("the app refuses to be put in a frame",
      rules.get("frame-ancestors") == ["'none'"],
      "frame-ancestors is %s" % rules.get("frame-ancestors"))

check("and says so again for anything that does not read CSP",
      front.headers.get("X-Frame-Options") == "DENY",
      front.headers.get("X-Frame-Options"))

# If a page is ever made to render somebody else's form, this is what
# stops the till's input being posted to them.
check("a form on our pages may only post back to us",
      rules.get("form-action") == ["'self'"],
      "form-action is %s" % rules.get("form-action"))

check("an injected <base> cannot re-point every link on the page",
      rules.get("base-uri") == ["'self'"],
      "base-uri is %s" % rules.get("base-uri"))

check("no plugin content at all",
      rules.get("object-src") == ["'none'"],
      "object-src is %s" % rules.get("object-src"))

# The weak line, and the one worth watching. 'unsafe-inline' is in
# script-src because this app writes its behaviour inline and instant.js
# injects fetched markup into the current document, where a nonce from
# another response would be refused. What must not slip is the other
# half: where a script may be *fetched* from.
script_src = rules.get("script-src", [])
check("scripts may only be fetched from us and one known CDN",
      set(script_src) <= {"'self'", "'unsafe-inline'",
                          "https://cdn.jsdelivr.net",
                          "https://checkout.razorpay.com",
                          "https://api.razorpay.com"},
      "script-src has grown: %s" % script_src)

check("and nothing may be sent to an origin we did not name",
      rules.get("connect-src") == ["'self'"],
      "connect-src is %s" % rules.get("connect-src"))

check("a response's declared type is taken at its word",
      front.headers.get("X-Content-Type-Options") == "nosniff",
      "without this a stored image can be sniffed into being run as HTML")

check("our own addresses are not handed to other sites",
      front.headers.get("Referrer-Policy")
      == "strict-origin-when-cross-origin",
      front.headers.get("Referrer-Policy"))

for feature in ("camera", "microphone", "geolocation", "payment"):
    check("%s is switched off for this app" % feature,
          "%s=()" % feature in front.headers.get("Permissions-Policy", ""),
          front.headers.get("Permissions-Policy"))

# A stylesheet or an image served bare is a gap in the same way.
asset = anon.get("/static/css/style.css")
check("a static file is protected too",
      asset.headers.get("X-Content-Type-Options") == "nosniff",
      "only the HTML routes were covered")


# =====================================================================
print("\n=== 2. A password cannot be guessed at machine speed ===")
# =====================================================================

owner = app.test_client()
register(owner, "Fort Cafe", "fortboss")
owner.get("/logout", follow_redirects=True)

HERE = {"REMOTE_ADDR": "198.51.100.10"}
guesser = app.test_client()

opened = 0
for attempt in range(application.LOGIN_MAX_FAILURES + 3):
    body = guesser.post("/login",
                        data={"username": "fortboss",
                              "password": "guess-%d" % attempt},
                        environ_overrides=HERE,
                        follow_redirects=True).get_data(as_text=True)
    if "page-view" in body:
        opened += 1

check("a wrong password never opens the app", opened == 0,
      "%d of the guesses signed in" % opened)

locked = guesser.post("/login",
                      data={"username": "fortboss",
                            "password": "password123"},
                      environ_overrides=HERE,
                      follow_redirects=True).get_data(as_text=True)

check("and after enough wrong ones the door shuts",
      "Too many failed" in locked,
      "the correct password still worked, so the guessing was never "
      "slowed down at all")

# The obvious way to get this wrong: lock the username, and anybody who
# knows an owner's username can shut them out of their own till.
elsewhere = app.test_client()
opened_elsewhere = elsewhere.post(
    "/login", data={"username": "fortboss", "password": "password123"},
    environ_overrides={"REMOTE_ADDR": "203.0.113.55"},
    follow_redirects=True).get_data(as_text=True)

check("but only for whoever was guessing",
      "page-view" in opened_elsewhere,
      "the owner is locked out of their own till by somebody else's "
      "failed attempts - the protection has become the attack")

# The lock is read before the hash is computed, so a flood costs the
# server nothing. Checked through behaviour: a locked caller is turned
# away whatever they send, including an empty password.
empty = guesser.post("/login",
                     data={"username": "fortboss", "password": ""},
                     environ_overrides=HERE,
                     follow_redirects=True).get_data(as_text=True)
check("a locked caller is turned away before anything is checked",
      "Too many failed" in empty, "the lock is applied too late to help")

# Saying which half was wrong hands over a way to find out whose
# usernames exist.
said = []
prober = app.test_client()
for username in ("fortboss", "no-such-person-here"):
    body = prober.post("/login",
                       data={"username": username, "password": "nope"},
                       environ_overrides={"REMOTE_ADDR": "192.0.2.77"},
                       follow_redirects=True).get_data(as_text=True)
    found = re.search(r"(Invalid username or password|Too many failed)", body)
    said.append(found.group(1) if found else "?")

check("a real username and an invented one are refused alike",
      said[0] == said[1] == "Invalid username or password",
      "the app said %r for a real name and %r for an invented one, which "
      "is a way to discover who banks here" % (said[0], said[1]))


# =====================================================================
print("\n=== 3. An upload is what it says it is ===")
# =====================================================================

def upload(name, blob):
    holder = type("Upload", (), {})()
    holder.filename = name
    holder.read = (lambda data: (lambda: data))(blob)
    return application.read_image_upload(holder)


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
check("a real image is accepted", upload("logo.png", PNG)[1] == "image/png")

for label, name, blob in (
        ("markup in a .png", "x.png",
         b"<html><script>alert(1)</script></html>"),
        ("an SVG in a .png", "y.png",
         b"<svg xmlns='http://www.w3.org/2000/svg'><script>go()</script></svg>"),
        ("a shell script in a .jpg", "z.jpg", b"#!/bin/sh\nrm -rf /\n"),
        ("an empty pretence", "q.png", b"GIF8")):
    try:
        upload(name, blob)
        check("%s is refused" % label, False,
              "it was stored, and will later be served as an image")
    except ValueError:
        check("%s is refused" % label, True)

# The name is a claim; the bytes are the fact. A file whose bytes say
# JPEG must not be filed as a PNG, or it is served under a type it is not.
try:
    upload("wrong.png", b"\xff\xd8\xff" + b"\x00" * 40)
    check("a JPEG named .png is not filed as a PNG", False,
          "it was accepted under the name's type rather than its own")
except ValueError:
    check("a JPEG named .png is not filed as a PNG", True)


# =====================================================================
print("\n=== 4. The things that were already right ===")
# =====================================================================

boss = app.test_client()
register(boss, "Guard Cafe", "guardboss")

# CSRF, on every method that writes.
no_token = boss.post("/categories/add",
                     data={"category_name": "Sneaky", "description": ""},
                     follow_redirects=True)
check("a write without a CSRF token is refused",
      no_token.status_code == 400,
      "status %s - a form on another site could post here"
      % no_token.status_code)

wrong_token = boss.post("/categories/add",
                        data={"category_name": "Sneaky",
                              "description": "",
                              "_csrf_token": "not-the-right-token"},
                        follow_redirects=True)
check("and so is one with the wrong token",
      wrong_token.status_code == 400, "status %s" % wrong_token.status_code)

# One cafe must never read another's rows.
boss.post("/categories/add", data={"category_name": "Mine",
                                   "description": "",
                                   "_csrf_token": csrf(boss)},
          follow_redirects=True)

rival = app.test_client()
register(rival, "Rival Cafe", "rivalboss")
check("a cafe cannot see another cafe's categories",
      "Mine" not in rival.get("/categories").get_data(as_text=True),
      "one tenant is reading another's data")

# Signed out is signed out.
stranger = app.test_client()
for path in ("/categories", "/billing", "/foods", "/inventory", "/reports"):
    reply = stranger.get(path)
    check("%s is closed to a stranger" % path,
          reply.status_code in (302, 401, 403)
          and "/login" in reply.headers.get("Location", ""),
          "status %s" % reply.status_code)

# A signed-in session must be this app's, not one somebody wrote.
forged = app.test_client()
forged.set_cookie("session", "forged-value-not-signed-by-us")
check("a made-up session cookie is not a session",
      "/login" in forged.get("/categories").headers.get("Location", ""),
      "an unsigned cookie was accepted")

# Signing in must not keep the session id an attacker planted.
fixer = app.test_client()
fixer.get("/login")
with fixer.session_transaction() as sess:
    sess["planted"] = "still here"
fixer.post("/login", data={"username": "guardboss",
                           "password": "password123"},
           environ_overrides={"REMOTE_ADDR": "192.0.2.180"},
           follow_redirects=True)
with fixer.session_transaction() as sess:
    kept = sess.get("planted")
check("signing in throws away whatever was in the session before",
      kept is None,
      "a value planted before sign-in survived it, so a session id "
      "planted by somebody else would survive too")

# The cookie itself.
cookie_header = " ".join(
    str(value) for value in
    app.test_client().post("/login",
                           data={"username": "guardboss",
                                 "password": "password123"},
                           environ_overrides={"REMOTE_ADDR": "192.0.2.181"}
                           ).headers.getlist("Set-Cookie"))
check("the session cookie is not readable by script",
      "HttpOnly" in cookie_header, cookie_header[:120])
check("and is not sent on other sites' requests",
      "SameSite=Lax" in cookie_header or "SameSite=Strict" in cookie_header,
      cookie_header[:120])

# Production must not be able to boot on a default key.
source = open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
check("production refuses to start without a signing key",
      "SECRET_KEY is not set" in source and "raise RuntimeError" in source,
      "a deployment could run on a shared key, and anyone could forge a "
      "session cookie for any cafe")

# Nothing may be redirected off-site through ?next.
for hostile in ("//evil.example.com/", "https://evil.example.com/",
                "javascript:alert(1)"):
    hopeful = app.test_client()
    landed = hopeful.post("/login?next=%s" % hostile,
                          data={"username": "guardboss",
                                "password": "password123"},
                          environ_overrides={"REMOTE_ADDR": "192.0.2.182"})
    check("sign-in will not bounce to %r" % hostile,
          "evil.example.com" not in landed.headers.get("Location", "")
          and "javascript:" not in landed.headers.get("Location", ""),
          "it would have sent them to %s"
          % landed.headers.get("Location", ""))

# The database connection is verified, not merely encrypted.
check("the database CA is verified when one is configured",
      'DB_CONFIG["ssl_verify_cert"]' in source,
      "an encrypted connection to the wrong server is still the wrong "
      "server")

check("and TLS is only ever disabled on purpose",
      'os.environ.get("DB_SSL_DISABLED", "0") == "1"' in source,
      "TLS can be switched off without saying so")



# ==========================================================
print("\n=== A flood of orders from one QR code ===")
# ==========================================================
# The route a customer's phone posts to takes no session and no CSRF
# token, and nothing counted how often it was called. One script with a
# table's token could order until the stock ran out, and every one of
# those orders took the same per-cafe lock the kitchen's own orders
# queue on - on a server that holds six requests at a time.

flood = app.test_client()
flood.post("/register", data={
    "cafe_name": "Flood Cafe", "full_name": "Owner", "username": "flood",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
flood.post("/categories/add", data={
    "category_name": "Drinks", "description": "",
    "_csrf_token": csrf(flood)}, follow_redirects=True)
_cats = re.findall(r'<option value="(\d+)">',
                   flood.get("/foods/add").get_data(as_text=True))
flood.post("/foods/add", data={
    "food_name": "Filter Coffee", "category_id": _cats[0], "price": "40",
    "quantity": "5000", "minimum_stock": "5", "description": "",
    "_csrf_token": csrf(flood)}, follow_redirects=True)

# The token is minted when the owner first opens the QR page.
flood.get("/settings/qr")
_token = mysql_shim._DB.execute(
    "SELECT public_token FROM cafes WHERE cafe_name = 'Flood Cafe'"
).fetchone()[0]

check("the cafe has a public ordering address", bool(_token),
      "no token, so there is nothing to flood")

_menu = flood.get("/m/%s" % _token).get_data(as_text=True)
_dish = re.findall(r'name="quantity_(\d+)"', _menu)[0]


def place(client, address):
    """One order from a phone, the way a phone sends it."""
    return client.post(
        "/m/%s/order" % _token,
        data={"quantity_%s" % _dish: "1"},
        environ_base={"REMOTE_ADDR": address},
        follow_redirects=False)


def orders_now():
    return mysql_shim._DB.execute(
        "SELECT COUNT(*) FROM orders o INNER JOIN cafes c "
        "ON o.cafe_id = c.cafe_id WHERE c.cafe_name = 'Flood Cafe'"
    ).fetchone()[0]


# ---- One attacker, a thousand orders ----
attacker = app.test_client()
before = orders_now()
for _ in range(200):
    place(attacker, "203.0.113.9")
got_through = orders_now() - before

check("a flood from one address is capped",
      got_through <= application.QR_ORDER_MAX_PER_SOURCE,
      "200 orders from one address put %d through, and the limit is %d"
      % (got_through, application.QR_ORDER_MAX_PER_SOURCE))

check("and some of them did get through, so this is a cap and not a wall",
      got_through > 0,
      "nothing was accepted at all, which would mean the cafe cannot "
      "take orders either")

# ---- The refusal has to be cheap, or it is not a defence ----
# What makes a flood harmful here is not the HTTP request, it is the
# ten database round trips and the per-cafe lock each order takes. A
# rejected one must take neither.
_seen = []
_real = mysql_shim._Cursor.execute


def _spy(self, sql, params=()):
    _seen.append(" ".join(str(sql).split()))
    return _real(self, sql, params)


mysql_shim._Cursor.execute = _spy
place(attacker, "203.0.113.9")          # already over its limit
mysql_shim._Cursor.execute = _real

check("a refused order costs two queries, not ten",
      len(_seen) <= 3,
      "it ran %d statements to say no: %s" % (len(_seen), _seen))

check("and a refused order takes no lock",
      not any("FOR UPDATE" in sql.upper() for sql in _seen),
      "it took a row lock on the way to refusing: %s"
      % [s for s in _seen if "FOR UPDATE" in s.upper()])

check("and it writes nothing",
      not any(sql.upper().startswith(("INSERT", "UPDATE", "DELETE"))
              for sql in _seen),
      "a refusal wrote to the database: %s" % _seen)

# ---- A different table is not punished for it ----
# Every table in a cafe shares one token, so a limit kept only per cafe
# would let one phone stop everybody else ordering.
neighbour = app.test_client()
before = orders_now()
place(neighbour, "198.51.100.4")

check("a customer at another table can still order",
      orders_now() > before,
      "one address flooding shut the whole cafe out, which is worse "
      "than the flood")

# ---- The cafe-wide ceiling still exists ----
# Spread over enough addresses, the per-address count never trips. The
# cafe-wide one is what is left.
before = orders_now()
for n in range(200):
    place(app.test_client(), "198.51.100.%d" % (100 + n % 120))
spread = orders_now() - before

check("a flood spread over many addresses is capped too",
      spread <= application.QR_ORDER_MAX_PER_CAFE,
      "%d orders got through from many addresses, and the cafe-wide "
      "limit is %d" % (spread, application.QR_ORDER_MAX_PER_CAFE))

# ---- And an ordinary lunchtime is untouched ----
mysql_shim._DB.execute("DELETE FROM order_attempts")
mysql_shim._DB.commit()

before = orders_now()
for n in range(12):
    place(app.test_client(), "198.51.100.%d" % (200 + n))
lunch = orders_now() - before

check("twelve tables ordering in a minute all get through",
      lunch == 12,
      "only %d of twelve ordinary orders were accepted, so the limit "
      "is sitting on real customers" % lunch)


# ==========================================================
print("\n=== Nothing changes state on a GET ===")
# ==========================================================
# CSRF is checked on POST, PUT, PATCH and DELETE. A GET that changes
# something is outside that check entirely, so it can be fired by an
# <img> tag on any page a signed-in member of staff happens to open.
# /orders/delete/<id> was exactly that: a bare alias for cancel_order,
# which is POST-only and properly protected.

audit = app.test_client()
audit.post("/register", data={
    "cafe_name": "Audit Cafe", "full_name": "Owner", "username": "audit",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
audit.post("/categories/add", data={
    "category_name": "Mains", "description": "",
    "_csrf_token": csrf(audit)}, follow_redirects=True)
_ac = re.findall(r'<option value="(\d+)">',
                 audit.get("/foods/add").get_data(as_text=True))
audit.post("/foods/add", data={
    "food_name": "Toast", "category_id": _ac[0], "price": "50",
    "quantity": "100", "minimum_stock": "5", "description": "",
    "_csrf_token": csrf(audit)}, follow_redirects=True)
_aq = re.findall(r'id="quantity_(\d+)"',
                 audit.get("/orders/add").get_data(as_text=True))
audit.post("/orders/add", data={
    "quantity_%s" % _aq[0]: "1", "_csrf_token": csrf(audit)},
    follow_redirects=True)

_oid = mysql_shim._DB.execute(
    "SELECT o.order_id FROM orders o INNER JOIN cafes c "
    "ON o.cafe_id = c.cafe_id WHERE c.cafe_name = 'Audit Cafe' "
    "ORDER BY o.order_id DESC"
).fetchone()[0]


def order_status_of(order_id):
    return mysql_shim._DB.execute(
        "SELECT order_status FROM orders WHERE order_id = %d" % order_id
    ).fetchone()[0]


check("there is an order to try to cancel",
      order_status_of(_oid) == "Pending",
      "the order is %s before anything touched it"
      % order_status_of(_oid))

# The shape of the attack: no token, no form, just a fetched URL.
_got = audit.get("/orders/delete/%d" % _oid, follow_redirects=False)

check("a GET cannot cancel an order",
      order_status_of(_oid) == "Pending",
      "fetching /orders/delete/%d as a signed-in user cancelled it - "
      "which an <img> tag on any other site can do, with no token and "
      "no click" % _oid)

_routes = {str(rule) for rule in app.url_map.iter_rules()}

check("and the route is gone rather than merely refusing",
      not any(r.startswith("/orders/delete") for r in _routes),
      "the address is still registered: %s"
      % [r for r in _routes if r.startswith("/orders/delete")])

check("the unknown address is handled without a stack trace",
      _got.status_code in (302, 404),
      "it answered %d" % _got.status_code)

# And the real way through still works, with its token.
_cancel = audit.post("/orders/cancel/%d" % _oid,
                     data={"_csrf_token": csrf(audit)},
                     follow_redirects=True)

check("the Cancel button still cancels",
      order_status_of(_oid) == "Cancelled",
      "cancelling by POST left it %s, so the fix broke the feature"
      % order_status_of(_oid))


# ==========================================================
print("\n=== The database does not introduce itself ===")
# ==========================================================
# The driver's error text names tables, columns and itself. Shown to
# whoever tripped it, that is a free map of the schema for anybody
# holding the page and trying things.

_broke = app.test_client()
_broke.post("/register", data={
    "cafe_name": "Break Cafe", "full_name": "Owner", "username": "breaker",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)

# Make the next statement fail the way a real outage would.
_real_execute = mysql_shim._Cursor.execute


def _explode(self, sql, params=()):
    if "FROM categories" in str(sql) or "INTO categories" in str(sql):
        raise application.mysql.connector.Error(
            "1054 (42S22): Unknown column 'secret_column' in "
            "'field list' on table cafe_db.categories")
    return _real_execute(self, sql, params)


mysql_shim._Cursor.execute = _explode
_page = _broke.post("/categories/add",
                    data={"category_name": "X", "description": "",
                          "_csrf_token": csrf(_broke)},
                    follow_redirects=True).get_data(as_text=True)
mysql_shim._Cursor.execute = _real_execute

for _leak in ("secret_column", "cafe_db", "42S22", "1054"):
    check("a database failure does not show '%s'" % _leak,
          _leak not in _page,
          "the page handed the visitor the driver's own words, which "
          "name the schema")

check("but it does say something useful",
      "try again" in _page.lower() or "went wrong" in _page.lower(),
      "the visitor was told nothing at all, which is its own problem")


# ==========================================================
print("\n=== Server text is never parsed as markup ===")
# ==========================================================
# Staff can add and edit foods, so a dish name is text somebody else
# typed. Jinja escapes it on the way into a page; the risk is the
# script that writes it into a page afterwards.

_billing_js = io.open(
    os.path.join(ROOT, "templates", "billing.html"),
    encoding="utf-8").read()

check("the billing toast does not build itself from the message",
      "toast.innerHTML =" not in _billing_js,
      "a message from the server is written into innerHTML, so the "
      "day one of them quotes a dish name it is markup")

_order_js = io.open(
    os.path.join(ROOT, "templates", "add_order.html"),
    encoding="utf-8").read()

check("and the order page puts messages in as text",
      "statusBox.textContent = message" in _order_js,
      "the order page stopped using textContent for server messages - "
      "and its messages DO quote dish names")


# ==========================================================
print("\n=== One cafe cannot reach into another ===")
# ==========================================================
# Every route below takes a bare number out of the URL. If any of them
# forgets to say "and it belongs to the cafe asking", a rival counts
# upwards and reads, edits or cancels somebody else's trade.


def open_cafe(name, user, dish, price):
    """A cafe with a menu, an order, a bill and a member of staff."""
    client = app.test_client()
    client.post("/register", data={
        "cafe_name": name, "full_name": name + " Owner",
        "username": user, "phone_number": "",
        "password": "password123", "confirm_password": "password123"},
        follow_redirects=True)
    client.post("/categories/add", data={
        "category_name": "Mains", "description": "",
        "_csrf_token": csrf(client)}, follow_redirects=True)
    cat = re.findall(r'<option value="(\d+)">',
                     client.get("/foods/add").get_data(as_text=True))[-1]
    client.post("/foods/add", data={
        "food_name": dish, "category_id": cat, "price": price,
        "quantity": "500", "minimum_stock": "5", "description": "",
        "_csrf_token": csrf(client)}, follow_redirects=True)
    qty = re.findall(r'id="quantity_(\d+)"',
                     client.get("/orders/add").get_data(as_text=True))
    client.post("/orders/add", data={
        "quantity_%s" % qty[-1]: "2",
        "_csrf_token": csrf(client)}, follow_redirects=True)
    client.get("/billing")
    client.post("/users/add", data={
        "full_name": name + " Till", "username": user + "till",
        "password": "password123", "confirm_password": "password123",
        "role": "staff", "_csrf_token": csrf(client)},
        follow_redirects=True)
    client.get("/settings/qr")

    row = mysql_shim._DB.execute(
        "SELECT cafe_id, owner_user_id, public_token FROM cafes "
        "WHERE cafe_name = '%s'" % name).fetchone()
    ids = {
        "cafe_id": row[0], "owner": row[1], "token": row[2],
        "food": mysql_shim._DB.execute(
            "SELECT food_id FROM foods WHERE user_id = %d" % row[1]
        ).fetchone()[0],
        "category": mysql_shim._DB.execute(
            "SELECT category_id FROM categories WHERE user_id = %d" % row[1]
        ).fetchone()[0],
        "order": mysql_shim._DB.execute(
            "SELECT order_id FROM orders WHERE user_id = %d" % row[1]
        ).fetchone()[0],
        "staff": mysql_shim._DB.execute(
            "SELECT user_id FROM users WHERE cafe_id = %d AND role = 'staff'"
            % row[0]).fetchone()[0],
    }
    ids["bill"] = mysql_shim._DB.execute(
        "SELECT bill_id FROM bills WHERE order_id = %d" % ids["order"]
    ).fetchone()[0]
    ids["item"] = mysql_shim._DB.execute(
        "SELECT order_item_id FROM order_items WHERE order_id = %d"
        % ids["order"]).fetchone()[0]
    return client, ids


alpha, A = open_cafe("Alpha Coffee", "alphaowner", "Alpha Bun", "70")
beta, B = open_cafe("Beta Bakery", "betaowner", "Beta Tart", "310")

check("two separate cafes exist to test with",
      A["owner"] != B["owner"] and A["order"] != B["order"],
      "the fixture built one cafe twice, so nothing below is a "
      "boundary: %s vs %s" % (A, B))


def beta_order_status():
    return mysql_shim._DB.execute(
        "SELECT order_status FROM orders WHERE order_id = %d" % B["order"]
    ).fetchone()[0]


def beta_bill_status():
    return mysql_shim._DB.execute(
        "SELECT payment_status FROM bills WHERE bill_id = %d" % B["bill"]
    ).fetchone()[0]


def beta_food_name():
    return mysql_shim._DB.execute(
        "SELECT food_name FROM foods WHERE food_id = %d" % B["food"]
    ).fetchone()[0]


# ---- Reading ----
# A page that answers with the row is a leak even if it changes nothing.
READS = [
    ("Beta's order", "/orders/%d" % B["order"]),
    ("Beta's printed bill", "/orders/%d/bill" % B["order"]),
    ("Beta's kitchen ticket", "/orders/%d/kot" % B["order"]),
    ("Beta's food, on its edit page", "/foods/edit/%d" % B["food"]),
    ("Beta's category, on its edit page", "/categories/edit/%d" % B["category"]),
    ("Beta's staff member, on their edit page", "/users/%d/edit" % B["staff"]),
    ("Beta's staff photo", "/media/user/%d" % B["staff"]),
    ("Beta's logo", "/media/cafe/%d/logo" % B["cafe_id"]),
]

for what, path in READS:
    page = alpha.get(path, follow_redirects=True)
    body = page.get_data(as_text=True)
    leaked = ("Beta Tart" in body or "Beta Bakery" in body
              or "Beta Till" in body)
    check("Alpha cannot read %s" % what, not leaked,
          "GET %s handed Alpha a page naming Beta's own data" % path)

# ---- Changing ----
# Each of these is a POST with Alpha's own valid token, so CSRF is not
# what is being tested - only whether the row is checked for ownership.
WRITES = [
    ("cancel the order", "/orders/cancel/%d" % B["order"], {}),
    ("complete the order", "/orders/complete/%d" % B["order"], {}),
    ("mark the bill paid", "/billing/mark-paid/%d" % B["bill"], {}),
    ("edit the bill", "/billing/edit/%d" % B["bill"],
     {"payment_method": "Cash"}),
    ("delete the food", "/foods/delete/%d" % B["food"], {}),
    ("delete the category", "/categories/delete/%d" % B["category"], {}),
    ("restock the food", "/inventory/update/%d" % B["food"],
     {"quantity": "1"}),
    ("deactivate the staff member", "/users/%d/toggle" % B["staff"], {}),
    ("delete the staff member", "/users/%d/delete" % B["staff"], {}),
    ("edit the staff member", "/users/%d/edit" % B["staff"],
     {"full_name": "Taken Over", "role": "admin", "is_active": "1"}),
    ("claim the kitchen ticket", "/api/kitchen/claim/%d" % B["order"], {}),
    ("mark a dish made", "/api/kitchen/item/%d" % B["item"], {}),
]

for what, path, extra in WRITES:
    payload = dict(extra)
    payload["_csrf_token"] = csrf(alpha)
    alpha.post(path, data=payload, follow_redirects=True)

check("Beta's order is untouched",
      beta_order_status() == "Pending",
      "Alpha changed it to %s" % beta_order_status())

check("Beta's bill is still unpaid",
      beta_bill_status() != "Paid",
      "Alpha marked another cafe's bill as paid")

check("Beta's food still exists, under its own name",
      beta_food_name() == "Beta Tart",
      "Alpha reached Beta's menu: the dish is now %r" % beta_food_name())

check("Beta's category still exists",
      mysql_shim._DB.execute(
          "SELECT COUNT(*) FROM categories WHERE category_id = %d"
          % B["category"]).fetchone()[0] == 1,
      "Alpha deleted another cafe's category")

check("Beta's staff member is still theirs",
      tuple(mysql_shim._DB.execute(
          "SELECT full_name, role, cafe_id FROM users WHERE user_id = %d"
          % B["staff"]).fetchone()) == ("Beta Bakery Till", "staff", B["cafe_id"]),
      "Alpha edited a user in another cafe: %s"
      % (tuple(mysql_shim._DB.execute(
          "SELECT full_name, role, cafe_id FROM users WHERE user_id = %d"
          % B["staff"]).fetchone()),))

check("Beta's stock was not altered",
      mysql_shim._DB.execute(
          "SELECT quantity FROM inventory WHERE food_id = %d"
          % B["food"]).fetchone()[0] == 498,
      "Alpha moved stock in another cafe's kitchen")

# ---- Merging an order across the boundary ----
# The interesting one: not reading a row, but building an order that
# spans two cafes. The counter takes quantity_<food_id> straight from
# the form, so a hand-written POST can name any id at all.
_before = mysql_shim._DB.execute(
    "SELECT COUNT(*) FROM order_items WHERE food_id = %d" % B["food"]
).fetchone()[0]

alpha.post("/orders/add", data={
    "quantity_%d" % B["food"]: "3",
    "quantity_%d" % A["food"]: "1",
    "_csrf_token": csrf(alpha)}, follow_redirects=True)

_after = mysql_shim._DB.execute(
    "SELECT COUNT(*) FROM order_items WHERE food_id = %d" % B["food"]
).fetchone()[0]

check("an order cannot be built from another cafe's menu",
      _after == _before,
      "Alpha put Beta's dish on an Alpha order - the two cafes' trade "
      "is now mixed in one bill")

# And the same attempt through the QR address, which needs no login.
_public_before = _after
app.test_client().post(
    "/m/%s/order" % A["token"],
    data={"quantity_%d" % B["food"]: "2"},
    follow_redirects=True)

check("and not through the QR address either",
      mysql_shim._DB.execute(
          "SELECT COUNT(*) FROM order_items WHERE food_id = %d"
          % B["food"]).fetchone()[0] == _public_before,
      "a QR order on Alpha's token bought Beta's stock")

# ---- One cafe's token does not open another's menu ----
_alpha_menu = app.test_client().get(
    "/m/%s" % A["token"]).get_data(as_text=True)

check("a QR menu shows only that cafe's food",
      "Beta Tart" not in _alpha_menu and "Alpha Bun" in _alpha_menu,
      "Alpha's public menu is showing Beta's dishes")

# ---- And the boundary is not just the URL ----
# Beta can still do all of this to their own rows. Without this the
# checks above would pass on a site where nothing works at all.
beta.post("/orders/cancel/%d" % B["order"],
          data={"_csrf_token": csrf(beta)}, follow_redirects=True)

check("but Beta can still cancel Beta's own order",
      beta_order_status() == "Cancelled",
      "the order is %s - the isolation checks above may be passing "
      "because the feature is broken for everyone"
      % beta_order_status())


# ==========================================================
print("\n=== The headers a scanner asks for ===")
# ==========================================================
# Reported as four missing headers. Three of them were being sent all
# along. The fourth was not, and for a reason no amount of reading the
# function would show: it asks whether the request arrived securely, and
# behind two proxies the answer it gets describes the last few
# centimetres of the journey rather than the browser's leg of it.

_head = app.test_client().get("/login")
_csp = _head.headers.get("Content-Security-Policy", "")
_directives = {}
for _rule in _csp.split(";"):
    _parts = _rule.split()
    if _parts:
        _directives[_parts[0]] = _parts[1:]

# The four the report named, by name.
for _wanted in ("script-src", "object-src", "base-uri", "frame-src"):
    check("the policy names %s" % _wanted,
          _wanted in _directives,
          "a scanner reads the absence of this as the absence of the "
          "protection; the policy is: %s" % _csp)

check("clickjacking is refused two ways",
      _directives.get("frame-ancestors") == ["'none'"]
      and _head.headers.get("X-Frame-Options") == "DENY",
      "frame-ancestors=%s X-Frame-Options=%s"
      % (_directives.get("frame-ancestors"),
         _head.headers.get("X-Frame-Options")))

check("content sniffing is refused",
      _head.headers.get("X-Content-Type-Options") == "nosniff",
      "X-Content-Type-Options is %r"
      % _head.headers.get("X-Content-Type-Options"))


# ---- HSTS, through however many proxies there are ----
# This runs behind Cloudflare and then Render, so X-Forwarded-Proto
# arrives as a list. ProxyFix is set to trust one hop and reads the last
# entry, which is Render handing the request to gunicorn over plain HTTP
# inside its own network. The browser's leg - the first entry - is the
# one HSTS is about.
_was_production = application.IS_PRODUCTION
application.IS_PRODUCTION = True
try:
    def hsts_for(forwarded):
        headers = {}
        if forwarded is not None:
            headers["X-Forwarded-Proto"] = forwarded
        return app.test_client().get(
            "/login", headers=headers
        ).headers.get("Strict-Transport-Security")

    check("HSTS is sent behind one proxy",
          hsts_for("https"),
          "no Strict-Transport-Security with a single https hop")

    check("HSTS is sent behind two, which is what this runs behind",
          hsts_for("https,http"),
          "the browser reached this over HTTPS and Render handed it on "
          "over HTTP inside its own network; reading only the last hop "
          "calls that insecure and drops the header, which is exactly "
          "what a scan found missing")

    check("and it lasts a year, across subdomains",
          "max-age=31536000" in (hsts_for("https") or "")
          and "includeSubDomains" in (hsts_for("https") or ""),
          "it says %r" % hsts_for("https"))

    # RFC 6797: a host must not send this over plain HTTP, and a browser
    # must ignore it if it arrives that way. So the check stays - it is
    # only asked of the whole chain now rather than the last inch.
    check("but never when nothing in the chain was HTTPS",
          not hsts_for("http"),
          "HSTS was sent on a request that was carried in the open the "
          "whole way")

    check("and never on a development server with no proxy at all",
          not hsts_for(None),
          "a local http server told the browser to pin HTTPS, which "
          "that machine does not serve")
finally:
    application.IS_PRODUCTION = _was_production

check("and not at all outside production",
      not app.test_client().get(
          "/login", headers={"X-Forwarded-Proto": "https"}
      ).headers.get("Strict-Transport-Security"),
      "a development run is pinning browsers to HTTPS")


# ==========================================================
print("\n=== Nothing answers a server error ===")
# ==========================================================
# Reported by a scanner as "500 internal error ... it can also be
# related to a malicious injection gone wrong and breaking the site".
# It was neither an injection nor temporary: POST /razorpay/webhook
# answered 500 to anyone, on any deployment that had not been given
# online payment keys - which is every deployment that does not take
# card payments.
#
# A 500 says the server is broken. "You have not set this up" is not
# broken, and saying so to the whole internet invites exactly the
# attention that was reported.

check("an unconfigured payment webhook is not a server error",
      app.test_client().post("/razorpay/webhook",
                             data="{}").status_code != 500,
      "posting to the webhook with no keys configured answers 500, "
      "which is what a scanner reports as a broken site")

check("it simply is not there",
      app.test_client().post("/razorpay/webhook",
                             data="{}").status_code == 404,
      "it answered %d; with no secret configured there is no webhook "
      "to receive anything"
      % app.test_client().post("/razorpay/webhook",
                               data="{}").status_code)

# ...and with a secret configured it is a real webhook again, refusing
# anything unsigned. Without this the check above could be satisfied by
# deleting the feature.
_had_secret = application.RAZORPAY_WEBHOOK_SECRET
application.RAZORPAY_WEBHOOK_SECRET = "test-secret"
try:
    _unsigned = app.test_client().post("/razorpay/webhook", data="{}")
    check("but a configured webhook still refuses an unsigned call",
          _unsigned.status_code == 400,
          "it answered %d to a call with no signature" % _unsigned.status_code)
finally:
    application.RAZORPAY_WEBHOOK_SECRET = _had_secret

# ---- The sweep, which is what a scanner actually does ----
# Every address the app knows, with the variable parts filled in the way
# somebody probing would fill them. None of it should reach a traceback.
_fillers = {"int": ["1", "0", "999999", "-1"],
            "str": ["x", "..", "%2e%2e", "'", "<script>", "0" * 64]}
_targets = []
for _rule in app.url_map.iter_rules():
    if _rule.endpoint == "static":
        continue
    _path = str(_rule)
    _methods = sorted(_rule.methods - {"HEAD", "OPTIONS"})
    if "<" not in _path:
        _targets.append((_path, _methods))
        continue
    _filled = [_path]
    for _part in re.findall(r"<[^>]+>", _path):
        _kind = "int" if _part.startswith("<int:") else "str"
        _filled = [_f.replace(_part, _v, 1)
                   for _f in _filled for _v in _fillers[_kind]]
    for _one in _filled[:8]:
        _targets.append((_one, _methods))

for _junk in ("/.env", "/admin", "/wp-login.php", "/.git/config",
              "/api/nope", "/static/../app.py", "/media/food/abc"):
    _targets.append((_junk, ["GET"]))

_stranger = app.test_client()
_broken = []
for _path, _methods in _targets:
    for _method in _methods:
        _send = getattr(_stranger, _method.lower(), None)
        if _send is None:
            continue
        try:
            _reply = _send(_path)
        except Exception as _exc:
            _broken.append("%s %s raised %s"
                           % (_method, _path, type(_exc).__name__))
            continue
        if _reply.status_code >= 500:
            _broken.append("%s %s -> %d"
                           % (_method, _path, _reply.status_code))

check("a sweep of every address finds no server error",
      not _broken,
      "these answered 500 or worse to somebody with no account: %s"
      % "; ".join(_broken[:8]))

check("and the sweep actually visited the whole app",
      len(_targets) > 60,
      "only %d addresses were tried, so the check above proves little"
      % len(_targets))

# ==========================================================
print("\n=== Which address a visitor is counted as ===")
# ==========================================================
# Everything counted per address - the login lockout, the QR order
# limit - is only as good as knowing who is asking. ProxyFix is set to
# trust one hop, so request.remote_addr is the LAST entry of
# X-Forwarded-For, and behind Cloudflare and then Render the last entry
# is Render. Every visitor in the world came out as one address, which
# quietly turned the login lockout into one keyed on the username alone
# - the exact thing its own table comment warns against.

CLIENT = "203.0.113.5"


def seen_as(headers):
    with app.test_request_context("/", headers=headers):
        return application.request_source()


check("behind two proxies the visitor is the visitor",
      seen_as({"X-Forwarded-For": "%s, 172.16.0.9" % CLIENT}) == CLIENT,
      "counted as %r, which is a proxy - so every visitor shares one "
      "bucket and one person can lock out everybody"
      % seen_as({"X-Forwarded-For": "%s, 172.16.0.9" % CLIENT}))

check("Cloudflare's own header is believed first",
      seen_as({"CF-Connecting-IP": CLIENT,
               "X-Forwarded-For": "9.9.9.9, 172.16.0.9"}) == CLIENT,
      "the header Cloudflare sets itself was ignored")

# The important one. A client can write whatever it likes into
# X-Forwarded-For and it arrives on the LEFT; each real proxy appends
# on the right. Counting from the right is what makes the forgery
# harmless.
check("a forged address does not become the visitor",
      seen_as({"X-Forwarded-For":
               "1.2.3.4, %s, 172.16.0.9" % CLIENT}) == CLIENT,
      "somebody who writes an address into their own header is counted "
      "as it, and can walk away from any per-address limit by changing "
      "it - counted as %r"
      % seen_as({"X-Forwarded-For": "1.2.3.4, %s, 172.16.0.9" % CLIENT}))

check("with no proxy at all it is whoever connected",
      seen_as({}) not in ("", None),
      "a direct request has no address at all, and the address is half "
      "of a unique key")

# And adding a load balancer is one number, not a silent regression.
_hops = application.TRUSTED_PROXY_HOPS
application.TRUSTED_PROXY_HOPS = 3
try:
    check("another proxy in front is one setting away",
          seen_as({"X-Forwarded-For":
                   "%s, 172.16.0.9, 10.0.0.3" % CLIENT}) == CLIENT,
          "with three proxies and TRUSTED_PROXY_HOPS=3 the visitor came "
          "out as %r"
          % seen_as({"X-Forwarded-For":
                     "%s, 172.16.0.9, 10.0.0.3" % CLIENT}))

    check("and a forged one is still harmless with three",
          seen_as({"X-Forwarded-For":
                   "1.2.3.4, %s, 172.16.0.9, 10.0.0.3" % CLIENT}) == CLIENT,
          "the forgery won once a hop was added")
finally:
    application.TRUSTED_PROXY_HOPS = _hops


print("\n" + "=" * 62)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 62)
sys.exit(1 if FAILED else 0)
