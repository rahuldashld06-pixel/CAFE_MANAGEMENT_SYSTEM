"""
When something goes wrong: one page, its own number, nothing that broke.

Every error a person can meet - a page that is not there, one not open to
them, a form left open too long, a fault of ours - is answered by the same
page: the status number as big as a 404 page's always is, a sentence in
plain words, and a way out. Never what broke inside: no traceback, no SQL,
no file or table names. That goes to the log, for the developer.

What has to hold:

  * each error keeps its own status code (a 500 answered as a 404 would
    tell search engines and the host's health check the wrong thing);
  * the way out fits who is asking - the app, the sign-in, or a
    customer's own menu;
  * nothing technical reaches the page, and the error is logged;
  * the page still appears when the database is what failed, and a few
    lines of plain HTML stand in if even the page cannot be drawn;
  * scripts and the API still get JSON, and pictures a short answer.

Run with:  python tests/error_pages_test.py
"""
import logging
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "error-pages-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import app as application     # noqa: E402
from flask import abort       # noqa: E402

app = application.app
app.config["TESTING"] = False   # errors are handled, as in production

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % (detail,)))


SECRET = "users_password_hash column in table cafes_secret"


# Routes that fail on purpose, registered before the first request.
@app.route("/__test/crash")
def __test_crash():
    raise RuntimeError(SECRET)


@app.route("/__test/forbidden")
def __test_forbidden():
    abort(403)


@app.route("/__test/busy")
def __test_busy():
    abort(429)


@app.route("/__test/teapot")
def __test_teapot():
    abort(418)


@app.route("/__test/unavailable")
def __test_unavailable():
    abort(503)


# What the developer is told: everything logged at ERROR, kept here.
LOGGED = []


class _Keep(logging.Handler):
    def emit(self, record):
        text = record.getMessage()
        if record.exc_info:
            text += " " + logging.Formatter().formatException(record.exc_info)
        LOGGED.append(text)


app.logger.addHandler(_Keep(level=logging.ERROR))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Error Cafe", "full_name": "Ena Owner", "username": "ena",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
with owner.session_transaction() as sess:
    CAFE = sess["cafe_id"]
TOKEN = application.get_public_token(CAFE)


def page(response):
    html = response.get_data(as_text=True)

    def find(pattern):
        hit = re.search(pattern, html)
        return hit.group(1).strip() if hit else None

    return {
        "status": response.status_code,
        "html": html,
        "marked": find(r'data-error-page="(\d+)"'),
        "code": find(r'class="err__code"[^>]*>(\d+)<'),
        "heading": find(r'<h1 class="err__title">([^<]+)<'),
        "home": find(r'class="err__home" href="([^"]+)"'),
        "home_label": find(r'class="err__home" href="[^"]+">([^<]+)<'),
    }


def is_error_page(p, status):
    return (p["status"] == status and p["marked"] == str(status)
            and p["code"] == str(status) and bool(p["heading"]))


# =====================================================================
print("\n=== 1. A page that is not there ===")
# =====================================================================
p = page(owner.get("/no-such-page"))
check("signed in: a 404 page, with 404 on it", is_error_page(p, 404),
      {k: p[k] for k in ("status", "marked", "code", "heading")})
check("saying so in plain words", p["heading"] == "We could not find that page",
      p["heading"])
check("with the way back into the app", p["home"] == "/" and p["home_label"] == "Back to the app",
      (p["home"], p["home_label"]))
with owner.session_transaction() as sess:
    check("and nothing left queued for the next page", not sess.get("_flashes"),
          sess.get("_flashes"))

stranger = app.test_client()
p = page(stranger.get("/no-such-page"))
check("signed out: the same page, not the sign-in screen", is_error_page(p, 404),
      "status %s" % p["status"])
check("whose way out is to sign in", p["home"] == "/login", p["home"])
check("a page that needs signing in still asks for it",
      stranger.get("/billing").status_code == 302)

p = page(stranger.get("/m/%s/no/such/page" % TOKEN))
check("a customer's mistyped address: the 404 page", is_error_page(p, 404),
      "status %s" % p["status"])
check("leading back to that cafe's menu",
      p["home"] == "/m/%s" % TOKEN and p["home_label"] == "Back to the menu",
      (p["home"], p["home_label"]))
p = page(stranger.get("/m/<script>/x"))
check("a token that is not a token is never put back into a link",
      p["home"] == "/login" and "<script>" not in (p["home"] or ""), p["home"])

check("a missing item of stock is a 404 page too, not a line of text",
      is_error_page(page(owner.get("/inventory/edit/999999")), 404))

# =====================================================================
print("\n=== 2. A fault of ours ===")
# =====================================================================
LOGGED.clear()
p = page(owner.get("/__test/crash"))
check("a crash is a 500 page, with 500 on it", is_error_page(p, 500),
      "status %s" % p["status"])
check("which says it was not their doing",
      p["heading"] == "Something went wrong on our side"
      and "not anything you did" in p["html"], p["heading"])
check("and shows nothing of what broke",
      SECRET not in p["html"] and "Traceback" not in p["html"]
      and "RuntimeError" not in p["html"] and "app.py" not in p["html"],
      "the page gives away the error")
check("which the developer has, in the log",
      any(SECRET in line and "Traceback" in line for line in LOGGED),
      "nothing logged: %r" % LOGGED[-1:] )

# =====================================================================
print("\n=== 3. Every other kind, each at its own number ===")
# =====================================================================
for path, status, heading in (
        ("/__test/forbidden", 403, "That page is not open to you"),
        ("/__test/busy", 429, "A little too quick"),
        ("/__test/unavailable", 503, "We are briefly unavailable"),
        ("/categories/delete/1", 405, "That cannot be done from here")):
    p = page(owner.get(path))
    check("%d: the error page, saying %r" % (status, heading),
          is_error_page(p, status) and p["heading"] == heading,
          (p["status"], p["heading"]))

p = page(owner.get("/__test/teapot"))
check("an error with no words of its own borrows the nearest",
      is_error_page(p, 418) and p["heading"] == "Something was not quite right",
      (p["status"], p["heading"]))

before = mysql_shim._DB.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
p = page(owner.post("/categories/add", data={"category_name": "Stale",
                                             "_csrf_token": "an-old-token"}))
check("a form left open too long: a 400 page that says so",
      is_error_page(p, 400) and p["heading"] == "This page had been open a while",
      (p["status"], p["heading"]))
check("and nothing was changed by it",
      mysql_shim._DB.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == before)

# =====================================================================
print("\n=== 4. When the database is what failed ===")
# =====================================================================
real_connect = application.get_db_connection


def broken(*args, **kwargs):
    raise application.mysql.connector.Error("Can't connect to MySQL server on db.internal:3306")


application.get_db_connection = broken
try:
    LOGGED.clear()
    down = page(owner.get("/dashboard"))
    missing = page(owner.get("/no-such-page"))
finally:
    application.get_db_connection = real_connect

check("a page that needs the database shows the 500 page",
      is_error_page(down, 500), "status %s" % down["status"])
check("without the database's own words",
      "db.internal" not in down["html"] and "MySQL" not in down["html"],
      "the page names the database")
check("a missing page needs no database at all", is_error_page(missing, 404),
      "status %s" % missing["status"])
check("and the developer is told what failed",
      any("db.internal" in line for line in LOGGED), LOGGED[-1:])

real_template = app.jinja_env.get_template


def no_template(name, *args, **kwargs):
    if name == "error.html":
        raise RuntimeError("the error page's template is broken")
    return real_template(name, *args, **kwargs)


app.jinja_env.get_template = no_template
try:
    plain = owner.get("/no-such-page")
finally:
    app.jinja_env.get_template = real_template
check("if even the error page cannot be drawn, plain HTML says the same",
      plain.status_code == 404
      and "We could not find that page" in plain.get_data(as_text=True)
      and "broken" not in plain.get_data(as_text=True),
      plain.get_data(as_text=True)[:200])

# =====================================================================
print("\n=== 5. Scripts, the API and pictures ===")
# =====================================================================
api = owner.get("/api/no-such-thing")
check("the API answers JSON, not a page",
      api.status_code == 404 and api.get_json() == {"error": "Not found"}, api.get_json())
xhr = owner.get("/__test/crash", headers={"X-Requested-With": "XMLHttpRequest"})
check("a script's request that crashes gets JSON too, and no detail",
      xhr.status_code == 500 and xhr.get_json() == {"error": "Something went wrong."},
      xhr.get_json())
media = owner.get("/media/food/999999")
check("a missing picture is a short 404, not a page for an <img>",
      media.status_code == 404 and "data-error-page" not in media.get_data(as_text=True))
warm = owner.get("/no-such-page", headers={"X-Instant-Prefetch": "1"})
check("a background warm-up of a missing page gets the short answer",
      warm.status_code == 404 and "data-error-page" not in warm.get_data(as_text=True))

p = page(owner.get("/no-such-page"))
check("the page tells a script-sent save what to say, out of sight",
      re.search(r'<div class="flash-stack" hidden><div class="alert">[^<]+<', p["html"]) is not None)

# =====================================================================
print("\n=== 6. The page itself ===")
# =====================================================================
response = owner.get("/no-such-page")
html = response.get_data(as_text=True)
check("in English, with a main region and a title",
      '<html lang="en"' in html and '<main class="err"' in html
      and re.search(r"<title>Page not found · \w+</title>", html) is not None)
check("kept out of search results", 'name="robots" content="noindex"' in html)
nonce = re.search(r"'nonce-([^']+)'", response.headers.get("Content-Security-Policy", ""))
check("its one script carries this response's nonce",
      nonce is not None and ('<script nonce="%s">' % nonce.group(1)) in html,
      response.headers.get("Content-Security-Policy", "")[:120])
check("in the app's own stylesheet and colours",
      "css/error.css" in html and "css/theme.css" in html and 'data-theme="' in html)

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
