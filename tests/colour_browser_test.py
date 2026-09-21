"""
Real-browser tests for a café choosing its own colours.

Three things that only a browser can answer.

The picker paints the live page as you drag it, because a colour is not
something anybody can judge from a swatch the size of a stamp - what
matters is a whole screen of it. That preview has to go away again if
you leave without saving, or the café would appear to have changed
colour when it has not.

And the colours themselves ride on an inline style on <html>, which sits
outside the region instant.js swaps. The theme already needed copying
across by hand for exactly this reason; anything else on that element
needs the same treatment, and a test that only reads server HTML would
never notice it was missing.

Run with:  python tests/colour_browser_test.py
"""
import os
import re
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "colour-browser-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5973
BASE = "http://127.0.0.1:%d" % PORT

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


lock = threading.Lock()


@app.before_request
def _serialise():
    if not lock.acquire(timeout=40):
        raise RuntimeError("request lock timeout")


@app.teardown_request
def _release(exception=None):
    try:
        lock.release()
    except RuntimeError:
        pass


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Palette Cafe", "full_name": "Sam Owner", "username": "sam",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
seed.post("/categories/add", data={"category_name": "Coffee",
                                   "description": "",
                                   "_csrf_token": csrf(seed)},
          follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     seed.get("/foods/add").get_data(as_text=True)).group(1)
seed.post("/foods/add", data={
    "food_name": "Cortado", "category_id": category, "price": "90",
    "quantity": "40", "minimum_stock": "2", "description": "",
    "_csrf_token": csrf(seed)}, follow_redirects=True)

mysql_shim.skip_tour()


def stored():
    """The two colours as a plain pair - a sqlite3.Row equals no tuple."""
    return tuple(mysql_shim._DB.execute(
        "SELECT accent_hex, surface_hex FROM cafes WHERE cafe_id = 1"
    ).fetchone())


threading.Thread(target=lambda: app.run(host="127.0.0.1", port=PORT,
                                        threaded=True, use_reloader=False),
                 daemon=True).start()
for _ in range(160):
    try:
        urllib.request.urlopen(BASE + "/healthz", timeout=2).read()
        break
    except Exception:
        time.sleep(0.25)

browser_path = cdp.find_browser()
if not browser_path:
    print("SKIPPED: no browser")
    sys.exit(0)

b = cdp.Browser(browser_path)


def wait(expr, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if b.evaluate(expr):
                return True
        except RuntimeError:
            pass
        time.sleep(0.05)
    return False


def painted(name):
    """What the page is actually painted in, as the browser resolves it."""
    return b.evaluate(
        "getComputedStyle(document.documentElement)"
        ".getPropertyValue('%s').trim()" % name)


def page_background():
    return b.evaluate(
        "getComputedStyle(document.body).backgroundColor")


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
           deviceScaleFactor=1, mobile=False)

    b.call("Page.navigate", url=BASE + "/login")
    wait("!!document.querySelector('form')")
    b.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'sam';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
        }())
    """)
    check("the app opens", wait("!!document.getElementById('page-view')"),
          "never got past the sign-in screen")

    # =================================================================
    print("\n=== 1. The picker paints the page as you drag it ===")
    # =================================================================

    b.call("Page.navigate", url=BASE + "/settings/theme")
    wait("!!document.getElementById('colourForm')")
    wait("!!document.getElementById('surfaceHex')")

    before = page_background()

    b.evaluate("""
        (function () {
            var well = document.getElementById('surfaceHex');
            well.value = '#f2f0eb';
            well.dispatchEvent(new Event('change', {bubbles: true}));
        }())
    """)
    time.sleep(0.4)

    after = page_background()
    check("choosing a background repaints the page at once",
          after != before,
          "the page stayed %s, so an admin would be picking a colour "
          "with no idea what it looks like" % before)

    check("and a pale background brings dark text with it",
          wait("(function () {"
               "  var c = getComputedStyle(document.documentElement)"
               "    .getPropertyValue('--cream').trim();"
               "  var m = /^#(..)(..)(..)$/.exec(c);"
               "  if (!m) return false;"
               "  return (parseInt(m[1], 16) + parseInt(m[2], 16)"
               "          + parseInt(m[3], 16)) / 3 < 120;"
               "}())"),
          "the body text stayed pale on a pale page: %s"
          % painted("--cream"))

    check("touching the picker selects its option, without a second click",
          b.evaluate("document.querySelector("
                     "'[name=surface_mode][value=custom]').checked"),
          "an admin would drag a colour, save, and find nothing had "
          "been saved")

    # =================================================================
    print("\n=== 2. Leaving without saving leaves nothing behind ===")
    # =================================================================

    check("nothing was written by the preview alone",
          stored()[1] in (None, ""),
          "the database says %r - dragging a picker is not saving"
          % (stored()[1],))

    b.evaluate("""
        (function () {
            var link = document.querySelector('.sidebar-nav a[href*=billing]')
                || document.querySelector('.sidebar-nav a');
            link.click();
        }())
    """)
    wait("location.pathname !== '/settings/theme'")
    time.sleep(0.6)

    check("and the preview does not follow you to the next page",
          page_background() == before,
          "the rest of the app is still wearing a colour nobody saved: "
          "%s against the %s it should be"
          % (page_background(), before))

    # =================================================================
    print("\n=== 3. Saving it reaches every page, without a reload ===")
    # =================================================================

    b.call("Page.navigate", url=BASE + "/settings/theme")
    wait("!!document.getElementById('colourForm')")

    b.evaluate("""
        (function () {
            var form = document.getElementById('colourForm');
            form.querySelector('#surfaceHex').value = '#102a43';
            form.querySelector('[name=surface_mode][value=custom]')
                .checked = true;
            form.querySelector('#accentHex').value = '#7b2ff7';
            form.querySelector('[name=accent_mode][value=custom]')
                .checked = true;
            form.querySelector('button[type=submit]').click();
        }())
    """)

    deadline = time.time() + 20
    while time.time() < deadline and stored()[1] in (None, ""):
        time.sleep(0.25)

    check("the colours are saved", stored() == ("#7b2ff7", "#102a43"),
          "the database says %r" % (stored(),))

    check("and the page being looked at is wearing them",
          wait("getComputedStyle(document.documentElement)"
               ".getPropertyValue('--copper').trim() === '#7b2ff7'"),
          "--copper is %r" % painted("--copper"))

    # The point of the exercise: <html> is outside the swapped region.
    was = painted("--espresso-950")
    b.evaluate("""
        (function () {
            var links = document.querySelectorAll('.sidebar-nav a');
            for (var i = 0; i < links.length; i++) {
                if (links[i].getAttribute('href').indexOf('dashboard') > -1) {
                    links[i].click();
                    return;
                }
            }
            links[0].click();
        }())
    """)
    wait("location.pathname.indexOf('settings') === -1")
    time.sleep(0.8)

    check("switching pages keeps them",
          painted("--espresso-950") == was and was == "#102a43",
          "after a swap the background is %r, against the %r that was "
          "saved - <html> is outside the swapped region, so its colours "
          "have to be carried across by hand"
          % (painted("--espresso-950"), was))

    check("and so does the accent",
          painted("--copper") == "#7b2ff7", painted("--copper"))

    # =================================================================
    print("\n=== 4. Going back to a preset really goes back ===")
    # =================================================================

    b.call("Page.navigate", url=BASE + "/settings/theme")
    wait("!!document.getElementById('colourForm')")

    # The page arrives already wearing the saved colours, so unpicking
    # the custom options has to take them off in front of the admin -
    # not only once the form has been saved. A preview that restores
    # what the page arrived with can never show a colour being removed.
    # Clicked in a loop rather than once after a sleep. The page's own
    # script attaches these handlers when it runs, and a click that
    # lands before it does is swallowed in silence - which showed up as
    # this test passing against a bug it was written to catch.
    def unpick():
        b.evaluate("""
            (function () {
                var form = document.getElementById('colourForm');
                form.querySelector('[name=theme][value=sage]').checked = true;
                form.querySelector('[name=surface_mode][value=default]')
                    .checked = true;
                form.querySelector('[data-clear=accent]').click();
            }())
        """)
        return not b.evaluate(
            "document.documentElement.style.getPropertyValue("
            "'--espresso-950') || document.documentElement.style"
            ".getPropertyValue('--copper')")

    cleared = False
    for _ in range(40):
        if unpick():
            cleared = True
            break
        time.sleep(0.25)

    check("unpicking a custom colour takes it off there and then", cleared,
          "the page still shows background %r and accent %r, so going "
          "back to a preset looks like it does nothing"
          % (b.evaluate("document.documentElement.style"
                        ".getPropertyValue('--espresso-950')"),
             b.evaluate("document.documentElement.style"
                        ".getPropertyValue('--copper')")))

    b.evaluate("""
        (function () {
            var form = document.getElementById('colourForm');
            form.querySelector('button[type=submit]').click();
        }())
    """)

    deadline = time.time() + 20
    while time.time() < deadline and stored()[1] not in (None, ""):
        time.sleep(0.25)

    check("the custom colours are cleared",
          stored() == (None, None), "the database says %r" % (stored(),))

    check("and the page stops wearing them",
          wait("!document.documentElement.style.getPropertyValue"
               "('--espresso-950')"),
          "the background stayed %r, so a cafe could never be rid of a "
          "colour it had tried" % painted("--espresso-950"))

    check("the chosen preset is what is showing",
          wait("document.documentElement.getAttribute('data-theme')"
               " === 'sage'"),
          b.evaluate("document.documentElement.getAttribute('data-theme')"))

finally:
    try:
        b.close()
    except Exception:
        pass

print("\n" + "=" * 62)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 62)
sys.exit(1 if FAILED else 0)
