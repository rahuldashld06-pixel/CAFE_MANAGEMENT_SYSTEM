"""
Real-browser check that choosing a theme actually repaints the app.

The server-side test next door proves the right value reaches the markup.
This one proves the markup is worth anything: it reads the colour the
browser has actually resolved onto a button, switches theme, and reads it
again. A theme that is written into <html> but never reaches the paint -
a stylesheet that failed to load, a specificity mistake, a token nothing
uses - looks fine in HTML and is caught only here.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/theme_browser_test.py
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "theme-browser-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
app.config["TESTING"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0    # keep the browser honest

PORT = 5807
BASE = "http://127.0.0.1:%d" % PORT
BROWSER = cdp.find_browser()
if not BROWSER:
    print("SKIPPED: no Chromium-family browser found "
          "(Edge or Chrome). Nothing to drive.")
    sys.exit(0)

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


# ---------------------------------------------------------------- server ----
# The shim is one shared in-memory SQLite connection, so requests are
# serialised here rather than left to race in the threaded dev server.
_lock = threading.Lock()


@app.before_request
def _serialise():
    if not _lock.acquire(timeout=30):
        raise RuntimeError("request lock timeout")


@app.teardown_request
def _release(exception=None):
    try:
        _lock.release()
    except RuntimeError:
        pass


# ------------------------------------------------------------------ seed ----
print("\n=== 0. Seeding a cafe with a menu ===")
seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Palette Cafe", "full_name": "Pat Owner", "username": "pat",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123",
}, follow_redirects=True)


def csrf_of(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed.post("/categories/add", data={
    "category_name": "Coffee", "description": "Hot drinks",
    "_csrf_token": csrf_of(seed),
}, follow_redirects=True)

import re                               # noqa: E402
category = re.search(r'<option value="(\d+)">',
                     seed.get("/foods/add").get_data(as_text=True)).group(1)
seed.post("/foods/add", data={
    "food_name": "Flat White", "category_id": category, "price": "180",
    "quantity": "40", "description": "", "_csrf_token": csrf_of(seed),
}, follow_redirects=True)
print("  seeded")


# ---------------------------------------------------------------- serve -----
server = threading.Thread(
    target=lambda: app.run(host="127.0.0.1", port=PORT,
                           threaded=True, use_reloader=False, debug=False),
    daemon=True,
)
server.start()

for _ in range(80):
    try:
        import urllib.request
        urllib.request.urlopen(BASE + "/healthz", timeout=1).read()
        break
    except Exception:                   # noqa: BLE001
        time.sleep(0.25)

print("  server up on %s" % BASE)


# --------------------------------------------------------------- driver -----
# Every account this suite made has been shown round already, so
# the first-sign-in tour does not open over what is being tested.
mysql_shim.skip_tour()

browser = cdp.Browser(BROWSER)


def wait_for(expression, what, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if browser.evaluate(expression):
                return True
        except RuntimeError:
            pass                        # mid-navigation; the page is swapping
        time.sleep(0.15)
    raise AssertionError("timed out waiting for %s" % what)


def accent():
    """The accent the browser has resolved, not the one we asked for."""
    return browser.evaluate(
        "getComputedStyle(document.documentElement)"
        ".getPropertyValue('--copper').trim()")


def painted_button():
    """The colour actually painted onto a primary button."""
    return browser.evaluate("""
        (function () {
            var b = document.querySelector('.btn--primary');
            if (!b) return '';
            var s = getComputedStyle(b);
            return (s.backgroundImage || '') + ' | ' + (s.backgroundColor || '');
        }())
    """)


try:
    browser.call("Page.enable")

    print("\n=== 1. Sign in ===")
    browser.call("Page.navigate", url=BASE + "/login")
    wait_for("document.readyState === 'complete' && "
             "!!document.querySelector('form')", "the login form")

    browser.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'pat';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
        }())
    """)
    wait_for("location.pathname !== '/login'", "the app after sign-in")
    check("signed in", browser.evaluate("location.pathname") != "/login",
          "still on the login page")

    print("\n=== 2. The default really is copper on screen ===")
    browser.call("Page.navigate", url=BASE + "/orders/add")
    wait_for("document.readyState === 'complete'", "New Order")

    copper = accent()
    copper_button = painted_button()
    check("the browser resolves an accent at all", bool(copper),
          "--copper resolved to nothing; theme.css may not have loaded")
    check("and it is the original copper",
          copper.upper().replace(" ", "") == "#E08A3E",
          "resolved to %r" % copper)
    # The button is a gradient of the light and dark ends of the accent,
    # so those are what to look for - the mid tone never appears in it.
    check("a primary button is painted in it",
          "240, 168, 95" in copper_button and "185, 106, 40" in copper_button,
          "the button is painted %r" % copper_button)

    print("\n=== 3. The picker shows six different colours ===")
    browser.call("Page.navigate", url=BASE + "/settings/theme")
    wait_for("!!document.querySelector('.theme-option__swatch')",
             "the theme picker")

    swatches = browser.evaluate("""
        (function () {
            var out = [];
            var nodes = document.querySelectorAll('.theme-option__swatch');
            for (var i = 0; i < nodes.length; i++) {
                out.push(getComputedStyle(nodes[i])
                    .getPropertyValue('--copper').trim());
            }
            return out.join(',');
        }())
    """)
    shown = [s for s in (swatches or "").split(",") if s]
    check("every theme has a swatch", len(shown) == 6,
          "found %d swatches: %s" % (len(shown), shown))
    check("and no two are the same colour", len(set(shown)) == len(shown),
          "duplicates among %s" % shown)

    print("\n=== 4. Choosing one repaints the app ===")
    browser.evaluate("""
        (function () {
            var radio = document.querySelector('input[name=theme][value=ocean]');
            radio.checked = true;
            radio.form.submit();
        }())
    """)
    wait_for("document.readyState === 'complete' && "
             "document.documentElement.getAttribute('data-theme') === 'ocean'",
             "the saved theme")

    ocean = accent()
    check("the accent moved off copper", ocean != copper,
          "still %r" % ocean)
    check("and it is the ocean blue",
          ocean.upper().replace(" ", "") == "#4A9BD8",
          "resolved to %r" % ocean)

    browser.call("Page.navigate", url=BASE + "/orders/add")
    wait_for("document.readyState === 'complete'", "New Order again")

    check("New Order is repainted too", accent() == ocean,
          "New Order resolved %r while the setting says %r" % (accent(), ocean))
    check("the button follows",
          "111, 180, 230" in painted_button()
          and "46, 116, 168" in painted_button(),
          "the button is painted %r" % painted_button())

    print("\n=== 5. The tints move with it, not just the solid colour ===")
    # These are the rgba() washes behind focus rings and stat icons. They
    # were hard-coded to copper before --copper-rgb existed, so a themed
    # app would have had blue buttons sitting on orange glows.
    tint = browser.evaluate(
        "getComputedStyle(document.documentElement)"
        ".getPropertyValue('--copper-rgb').trim()")
    check("the rgb channels track the theme",
          tint.replace(" ", "") == "74,155,216",
          "--copper-rgb is %r while the accent is %r" % (tint, ocean))

    print("\n=== 6. An instant navigation keeps the new colour ===")
    # The attribute sits on <html>, outside the swapped region, so this is
    # the case that needed wiring up by hand in instant.js.
    browser.evaluate("""
        (function () {
            var link = document.querySelector('.sidebar-nav a[href="/billing"]');
            if (link) link.click();
        }())
    """)
    wait_for("location.pathname === '/billing'", "Billing via instant nav")

    check("the theme survives the swap", accent() == ocean,
          "after swapping to Billing the accent is %r" % accent())
    check("and the attribute is still right",
          browser.evaluate(
              "document.documentElement.getAttribute('data-theme')") == "ocean",
          "the attribute was lost in the swap")

finally:
    browser.close()

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
