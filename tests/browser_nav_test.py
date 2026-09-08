"""
Real-browser check of the instant-navigation layer.

Drives headless Edge over the DevTools Protocol against the app running on a
SQLite stand-in, and walks the exact path that would expose a duplicated
delegated listener:

    New Order  ->  create an order
               ->  Orders  ->  Billing  ->  New Order  ->  Billing
               ->  click "Paid"

Billing registers its payment handler with document.addEventListener('submit').
If leaving and re-entering the page stacked a second copy, that one click
would post twice - so the test counts what the server actually received.
Removing the listener teardown in instant.js turns this run red with two
POSTs, which is what makes the check worth having.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/browser_nav_test.py
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "browser-test-secret"
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

PORT = 5799
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
REQUESTS = []
_lock = threading.Lock()


@app.before_request
def _record_and_serialise():
    from flask import request
    if not _lock.acquire(timeout=30):
        raise RuntimeError("request lock timeout")
    REQUESTS.append((request.method, request.path,
                     request.headers.get("X-Instant-Prefetch") == "1"))


STATUSES = []


@app.after_request
def _record_status(response):
    from flask import request
    if request.method != "GET":
        STATUSES.append((request.method, request.path, response.status_code))
    return response


@app.teardown_request
def _release(exception=None):
    try:
        _lock.release()
    except RuntimeError:
        pass


def posts_to(prefix):
    return [r for r in REQUESTS if r[0] == "POST" and r[1].startswith(prefix)]


def gets_to(prefix):
    return [r for r in REQUESTS if r[0] == "GET" and r[1].startswith(prefix)]


# ------------------------------------------------------------------ seed ----
print("\n=== 0. Seeding a cafe with a menu ===")
seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Browser Cafe", "full_name": "Bee Owner", "username": "bee",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123",
}, follow_redirects=True)

def csrf_of(client):
    with client.session_transaction() as s:
        return s.get("_csrf_token", "")


seed.post("/categories/add", data={
    "category_name": "Coffee", "description": "Hot drinks",
    "_csrf_token": csrf_of(seed),
}, follow_redirects=True)

listing = seed.get("/foods/add").get_data(as_text=True)
import re                                # noqa: E402
category_id = re.search(r'name="category_id"[^>]*>.*?value="(\d+)"',
                        listing, re.S)
category_id = category_id.group(1) if category_id else "1"

seed.post("/foods/add", data={
    "food_name": "Filter Coffee", "category_id": category_id,
    "price": "50", "quantity": "25", "description": "",
    "_csrf_token": csrf_of(seed),
}, follow_redirects=True)

foods_page = seed.get("/orders/add").get_data(as_text=True)
food_ids = re.findall(r'id="quantity_(\d+)"', foods_page)
check("a food item exists to order", bool(food_ids), "no .quantity-input found")
FOOD_ID = food_ids[0] if food_ids else None

REQUESTS.clear()

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
    except Exception:
        time.sleep(0.25)

print("  server up on %s" % BASE)


# --------------------------------------------------------------- driver -----
browser = cdp.Browser(BROWSER)


def wait_for(expression, what, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if browser.evaluate(expression):
                return True
        except RuntimeError:
            pass                      # mid-navigation; the page is swapping
        time.sleep(0.15)
    raise AssertionError("timed out waiting for %s" % what)


def path():
    return browser.evaluate("location.pathname")


try:
    browser.call("Page.enable")

    print("\n=== 1. Sign in ===")
    browser.call("Page.navigate", url=BASE + "/login")
    wait_for("document.readyState === 'complete' && !!document.querySelector('form')",
             "the login form")

    browser.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'bee';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
            return true;
        })()
    """)
    wait_for("!!document.getElementById('page-view')", "the app shell")
    check("signed in and landed on the app shell", "/login" not in path(),
          "still at %s" % path())
    check("instant.js is live in the page",
          browser.evaluate("typeof window.Instant === 'object'"),
          "window.Instant missing")

    print("\n=== 2. Background warm-up ===")
    time.sleep(3.0)          # let the idle warm-up run
    warmed = sorted({r[1] for r in REQUESTS if r[2]})
    check("sidebar pages are prefetched in the background", len(warmed) >= 3,
          "prefetched: %s" % warmed)
    check("the warm-up never prefetches /logout",
          not any(r[1].startswith("/logout") for r in REQUESTS),
          "logout was fetched: %s" % [r for r in REQUESTS if "logout" in r[1]])

    print("\n=== 3. Create an order on New Order ===")
    browser.evaluate("window.Instant.visit('%s/orders/add', {})" % BASE, False)
    wait_for("!!document.getElementById('orderForm')", "the New Order form")
    check("New Order arrived without a full page load",
          browser.evaluate("typeof changeQuantity === 'function'"),
          "page script did not run after the swap")
    check("the second New Order script ran too (floating summary)",
          browser.evaluate("!!document.getElementById('orderSummaryTrigger')"),
          "summary trigger missing from the swapped view")

    before_orders = len(posts_to("/orders/add"))
    browser.evaluate("""
        (function () {
            var input = document.getElementById('quantity_%s');
            input.value = 2;
            // Drive the summary the way the page does, not through
            // calculateOrderTotal(): that function references ids this
            // template has never contained and throws on every load.
            input.dispatchEvent(new Event('input', {bubbles: true}));
            document.getElementById('orderForm')
                    .dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
            return true;
        })()
    """ % FOOD_ID)

    wait_for("true", "the order POST to land", timeout=2)
    time.sleep(2.0)
    created = len(posts_to("/orders/add")) - before_orders
    check("creating an order posted exactly once", created == 1,
          "POSTs to /orders/add: %d" % created)
    check("the order POST was accepted (not a 400/500)",
          ("POST", "/orders/add", 200) in STATUSES,
          "non-GET results: %s" % STATUSES)
    check("the CSRF field was stamped onto the swapped-in form",
          browser.evaluate(
              "!!document.querySelector('#orderForm input[name=_csrf_token]')"),
          "no _csrf_token input in #orderForm after the swap")

    print("\n=== 4. Orders -> Billing -> New Order -> Billing ===")
    for destination in ["/orders", "/billing", "/orders/add", "/billing"]:
        browser.evaluate("window.Instant.visit('%s%s', {})" % (BASE, destination), False)
        wait_for("location.pathname === '%s'" % destination, "arrival at %s" % destination)
        time.sleep(0.6)

    check("ended up on Billing", path() == "/billing", "at %s" % path())
    check("Billing's page script ran after the swap",
          browser.evaluate("typeof calculateReturn === 'function'"),
          "calculateReturn is not defined")

    reloads = browser.evaluate(
        "performance.getEntriesByType('navigation').length")
    check("the whole run used a single document (no full reloads)",
          reloads == 1, "navigation entries: %s" % reloads)

    print("\n=== 5. The payment posts exactly once ===")
    wait_for("!!document.querySelector('form.payment-toggle-form')",
             "a payable bill on Billing")

    before_pay = len(posts_to("/billing/mark-paid"))
    browser.evaluate("""
        (function () {
            var form = document.querySelector('form.payment-toggle-form');
            form.querySelector('button[type=submit]').click();
            return true;
        })()
    """)
    time.sleep(3.0)
    pay_posts = posts_to("/billing/mark-paid")
    fired = len(pay_posts) - before_pay

    check("clicking Paid posted exactly once after re-entering Billing",
          fired == 1,
          "POSTs to /billing/mark-paid: %d (%s)" % (fired, pay_posts))

    print("\n=== 6. Timers do not survive the page that started them ===")
    browser.evaluate("window.Instant.visit('%s/dashboard', {})" % BASE, False)
    wait_for("location.pathname === '/dashboard'", "the dashboard")
    time.sleep(1.0)
    browser.evaluate("window.Instant.visit('%s/foods', {})" % BASE, False)
    wait_for("location.pathname === '/foods'", "the foods page")

    REQUESTS.clear()
    time.sleep(6.0)          # dashboard polls every 5s; it must be silent now
    stats = gets_to("/api/dashboard-stats")
    check("the dashboard's 5s poll stops when you navigate away",
          len(stats) == 0, "still polling: %d hits" % len(stats))

finally:
    try:
        browser.close()
    except Exception:
        pass

print("\n" + "=" * 62)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("Failures:")
    for name in FAILED:
        print("  -", name)
print("=" * 62)
sys.exit(1 if FAILED else 0)
