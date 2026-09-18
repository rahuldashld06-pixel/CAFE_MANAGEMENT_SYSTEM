"""
Regression test for a stale "page could not be found" banner.

Reported from the live site: opening Inventory or Food Management greeted
the user with a 404 message. Two things combined to cause it.

The browser asks for /favicon.ico unprompted; the 404 handler flashed a
message for it, queueing "That page could not be found." for whatever page
came next. Then instant.js warmed the sidebar in the background, rendered
that message into the HTML it caches, and showed the cached copy when the
user clicked Inventory - so the banner arrived on a page that was perfectly
fine.

This drives a real browser through the same sequence: sign in, let the
favicon request and the whole warm-up run, then click the sidebar. It also
checks the opposite direction - a genuinely missing page must still tell the
user, or the fix would have just muted a real error.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without one.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/stale_banner_test.py
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
os.environ["SECRET_KEY"] = "banner-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5807
BASE = "http://127.0.0.1:%d" % PORT

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


lock = threading.Lock()


@app.before_request
def _serialise():
    lock.acquire(timeout=30)


@app.teardown_request
def _release(exception=None):
    try:
        lock.release()
    except RuntimeError:
        pass


seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Banner Cafe", "full_name": "B Owner", "username": "bnr",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as s:
        return s.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Beverages",
                                   "description": "", "_csrf_token": csrf()},
          follow_redirects=True)
page = seed.get("/foods/add").get_data(as_text=True)
cat = re.search(r'<option value="(\d+)">', page).group(1)
seed.post("/foods/add", data={"food_name": "Cold Coffee", "category_id": cat,
                              "price": "40", "quantity": "10",
                              "description": "", "_csrf_token": csrf()},
          follow_redirects=True)

threading.Thread(target=lambda: app.run(host="127.0.0.1", port=PORT,
                                        threaded=True, use_reloader=False),
                 daemon=True).start()
for _ in range(80):
    try:
        urllib.request.urlopen(BASE + "/healthz", timeout=1).read()
        break
    except Exception:
        time.sleep(0.25)

# Every account this suite made has been shown round already, so the

# first-sign-in tour does not open over the top of what is being

# tested here.

mysql_shim.skip_tour()


path = cdp.find_browser()
if not path:
    print("SKIPPED: no browser")
    sys.exit(0)

# Every account this suite made has been shown round already, so
# the first-sign-in tour does not open over what is being tested.
mysql_shim.skip_tour()

b = cdp.Browser(path)

BANNER = "That page could not be found"


def wait(expr, what, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if b.evaluate(expr):
                return
        except RuntimeError:
            pass
        time.sleep(0.15)
    raise AssertionError("timeout: " + what)


def banner_on_page():
    return b.evaluate(
        "(document.getElementById('page-view') || document.body).innerText"
        ".indexOf(%r) !== -1" % BANNER)


def click_nav(label):
    b.evaluate("""
        (function () {
            var links = document.querySelectorAll('.sidebar-nav a');
            for (var i = 0; i < links.length; i++) {
                if (links[i].textContent.trim() === %r) { links[i].click(); return true; }
            }
            return false;
        })()
    """ % label)
    time.sleep(1.2)


try:
    b.call("Page.enable")
    b.call("Page.navigate", url=BASE + "/login")
    wait("document.readyState==='complete' && !!document.querySelector('form')", "login")
    b.evaluate("""(function(){var f=document.querySelector('form');
        f.querySelector('[name=username]').value='bnr';
        f.querySelector('[name=password]').value='password123';
        f.submit(); return 1;})()""")
    wait("!!document.getElementById('page-view')", "shell")

    # Let the favicon request and the whole background warm-up finish - this
    # is exactly the window in which the stale banner used to be cached.
    time.sleep(4.0)

    print("\n=== Clicking through the sidebar as a user would ===")
    for label in ["Inventory", "Food Management", "Inventory", "Billing",
                  "Food Management", "New Order"]:
        click_nav(label)
        here = b.evaluate("location.pathname")
        check("%-16s opens clean (no stale 404 banner)" % label,
              not banner_on_page(), "banner shown at %s" % here)

    print("\n=== The page actually rendered, it is not just blank ===")
    click_nav("Inventory")
    check("Inventory shows its real content",
          b.evaluate("document.body.innerText").lower().find("inventory") != -1)
    click_nav("Food Management")
    check("Food Management shows its real content",
          b.evaluate("document.body.innerText").lower().find("food") != -1)

    check("no full page reloads along the way",
          b.evaluate("performance.getEntriesByType('navigation').length") == 1)

    print("\n=== A genuinely missing page still tells the user ===")
    b.call("Page.navigate", url=BASE + "/no-such-page")
    wait("!!document.getElementById('page-view')", "redirect target")
    check("opening a bad URL still shows the message",
          banner_on_page(), "the real 404 message was suppressed")

finally:
    try:
        b.close()
    except Exception:
        pass

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
