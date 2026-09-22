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


print("\n" + "=" * 62)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 62)
sys.exit(1 if FAILED else 0)
