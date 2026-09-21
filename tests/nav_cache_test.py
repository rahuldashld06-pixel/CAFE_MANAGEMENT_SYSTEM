"""
Real-browser tests for switching pages without waiting for the server.

Pages are cached once seen, so going back to one should paint at once.
What kept undoing that was the rule for emptying the cache: any POST at
all counted as a write, and the app POSTs to itself constantly - the
kitchen screen says it is still switched on every twenty seconds, a till
claims a ticket, the tour records that it has been seen. None of those
changes anything a page shows, and every one of them threw away every
cached page.

Measured before the fix, with the database in memory next door: a page
that painted in 1ms took 258ms after one heartbeat. On the deployed pair,
where a round trip is most of half a second and Billing makes five of
them, that is instant against roughly two and a half seconds.

The database is in memory here, so the difference between cached and
fetched would be a millisecond either way and nothing could be told
apart. Each query is given a delay standing in for the distance to the
real one, which is what makes a fetch visibly cost something.

Run with:  python tests/nav_cache_test.py
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
os.environ["SECRET_KEY"] = "nav-cache-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

# Stands in for the distance to the database. Small enough that a run
# does not take all day, large enough that one round trip is unmistakable
# next to a paint from cache.
PER_QUERY = 0.05

# A page that had to be fetched costs at least one of those plus the
# request itself; one painted from cache costs neither.
FETCHED_MS = PER_QUERY * 1000

_real_cursor = mysql_shim._Connection.cursor


def slow_cursor(self, *args, **kwargs):
    cur = _real_cursor(self, *args, **kwargs)
    real_execute = cur.execute

    def execute(sql, params=()):
        time.sleep(PER_QUERY)
        return real_execute(sql, params)

    cur.execute = execute
    return cur


mysql_shim._Connection.cursor = slow_cursor

app = application.app
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
PORT = 5961
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
    "cafe_name": "Speed Cafe", "full_name": "Sam Owner", "username": "sam",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
seed.post("/categories/add", data={"category_name": "Coffee",
                                   "description": "",
                                   "_csrf_token": csrf(seed)},
          follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     seed.get("/foods/add").get_data(as_text=True)).group(1)
for food, price in (("Flat White", "180"), ("Masala Chai", "70")):
    seed.post("/foods/add", data={
        "food_name": food, "category_id": category, "price": price,
        "quantity": "40", "minimum_stock": "2", "description": "",
        "_csrf_token": csrf(seed)}, follow_redirects=True)

mysql_shim.skip_tour()

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


def wait(expr, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if b.evaluate(expr):
                return True
        except RuntimeError:
            pass
        time.sleep(0.05)
    return False


def heading():
    try:
        return b.evaluate(
            "(document.querySelector('#page-view h1') || {}).textContent "
            "|| ''").strip()
    except RuntimeError:
        return ""


def switch(href, expect):
    """Click a sidebar link; milliseconds until that page is on screen."""
    b.evaluate("document.querySelector(\"a[href='%s']\").click()" % href)
    began = time.time()
    while time.time() - began < 30:
        if expect.lower() in heading().lower():
            return (time.time() - began) * 1000
        time.sleep(0.02)
    return -1


def housekeeping(path):
    b.evaluate("""
        fetch('%s', {
            method: 'POST',
            credentials: 'same-origin',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRFToken': window.CafeShell.csrfToken
            }
        })
    """ % path)
    time.sleep(1.2)


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
           deviceScaleFactor=1, mobile=False)

    print("\n=== 1. Sign in, and let the sidebar warm up ===")
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
    check("the app is open", wait("!!document.getElementById('page-view')"),
          "never got past the sign-in screen")

    time.sleep(22)

    first = switch("/billing", "Billing")
    check("a warmed page paints without waiting for the server",
          0 <= first < FETCHED_MS,
          "took %.0fms, and a fetch costs about %.0fms - so it fetched"
          % (first, FETCHED_MS))

    print("\n=== 2. The app talking to itself is not a write ===")
    housekeeping("/api/kitchen/heartbeat")
    beat = switch("/foods", "Food")
    check("a kitchen heartbeat leaves the cached pages alone",
          0 <= beat < FETCHED_MS,
          "took %.0fms - the kitchen saying it is still switched on threw "
          "away every cached page, which it does every twenty seconds"
          % beat)

    housekeeping("/api/tutorial/seen")
    seen = switch("/billing", "Billing")
    check("and so does the tour recording that it was seen",
          0 <= seen < FETCHED_MS,
          "took %.0fms after a one-off flag was written" % seen)

    print("\n=== 3. A write is never hidden by a cached page ===")
    # The cache is emptied on a write and refills itself moments later, so
    # catching it empty means catching a state that lasts half a second.
    # What matters is the thing that state exists to protect: no screen
    # may go on showing what was true before somebody saved something.
    switch("/billing", "Billing")
    bills_before = b.evaluate(
        "document.querySelectorAll('tr[data-bill-id]').length")  # the row, not
        # the two payment forms inside it, which carry the same attribute
    # Placing an order: a real write, made from inside the app the way a
    # person makes one, rather than a page loaded fresh from the address
    # bar - which would build a new cache and prove nothing.
    switch("/orders/add", "New Order")
    check("the counter screen is ready to take an order",
          wait("!!document.querySelector('.quantity-input')"),
          "no quantity box to fill in")
    time.sleep(0.5)
    b.evaluate("""
        (function () {
            var box = document.querySelector('.quantity-input');
            box.value = 1;
            box.dispatchEvent(new Event('input', {bubbles: true}));
            document.getElementById('orderForm').dispatchEvent(
                new Event('submit', {cancelable: true, bubbles: true}));
        }())
    """)
    time.sleep(4.0)

    switch("/billing", "Billing")
    bills_after = b.evaluate(
        "document.querySelectorAll('tr[data-bill-id]').length")  # the row, not
        # the two payment forms inside it, which carry the same attribute

    check("the new order is on the billing page straight away",
          bills_after == bills_before + 1,
          "%d bills before the order and %d after - a page cached from "
          "before the write was served" % (bills_before, bills_after))

    # The refill is the other half. Without it, one save sent every other
    # screen back to paying its full round trips until somebody opened it.
    time.sleep(9)
    refilled = switch("/foods", "Food")
    check("and the cache fills itself back up without being asked",
          0 <= refilled < FETCHED_MS,
          "took %.0fms, so nothing refilled after the write" % refilled)

    print("\n=== 4. Which is the whole point ===")
    trips = [switch(href, name) for href, name in
             (("/foods", "Food"), ("/billing", "Billing"),
              ("/inventory", "Inventor"), ("/orders/add", "New Order"),
              ("/billing", "Billing"))]
    worst = max(trips)
    check("moving around the app costs nothing anywhere",
          worst < FETCHED_MS,
          "the slowest switch took %.0fms: %s"
          % (worst, ["%.0f" % t for t in trips]))

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
