"""
The screen left on in the kitchen, driven in a real browser.

This suite exists because of a bug the server-side tests could not see.
Every write in this app is CSRF checked. A test client posting with a
token in the form body passes happily, while the page itself was sending
those same requests from fetch() with no token at all - so the kitchen
screen could not check in and could not claim a ticket, and said so only
on screen. The API was fine; the page calling it was not.

So what is checked here is the page doing its own work: checking in,
drawing what is waiting, and claiming a ticket, all through the browser
rather than around it.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/kitchen_screen_test.py
"""
import json
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "kitchen-screen-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
app.config["TESTING"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

PORT = 5849
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


_lock = threading.Lock()
REJECTED = []


@app.before_request
def _serialise():
    if not _lock.acquire(timeout=30):
        raise RuntimeError("request lock timeout")


@app.after_request
def _watch_for_refusals(response):
    from flask import request
    # A request the server turned away. The CSRF failure this suite was
    # written for showed up here as a 400 and nowhere else.
    if response.status_code >= 400 and request.path.startswith("/api/"):
        REJECTED.append((request.method, request.path, response.status_code))
    return response


@app.teardown_request
def _release(exception=None):
    try:
        _lock.release()
    except RuntimeError:
        pass


print("\n=== 0. A cafe, a menu, and an order from a phone ===")
seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Kitchen Cafe", "full_name": "Sam Owner", "username": "sam",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Coffee",
                                   "description": "",
                                   "_csrf_token": csrf(seed)},
          follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     seed.get("/foods/add").get_data(as_text=True)).group(1)
for name, price in (("Flat White", "180"), ("Masala Chai", "70")):
    seed.post("/foods/add", data={
        "food_name": name, "category_id": category, "price": price,
        "quantity": "20", "minimum_stock": "2", "description": "",
        "_csrf_token": csrf(seed)}, follow_redirects=True)

# Automatic printing on, or the shell watcher never runs at all.
seed.post("/settings/printing", data={
    "auto_kot": "1", "kot_delay": "0", "_csrf_token": csrf(seed)},
    follow_redirects=True)

with seed.session_transaction() as sess:
    cafe_id = sess.get("cafe_id")
token = application.get_public_token(cafe_id)

guest = app.test_client()
menu = guest.get("/m/%s" % token).get_data(as_text=True)
food_ids = re.findall(r'name="quantity_(\d+)"', menu)
guest.post("/m/%s/order" % token,
           data={"quantity_%s" % food_ids[0]: "2"}, follow_redirects=True)
print("  seeded, with one order waiting")

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


browser = cdp.Browser(BROWSER)


def wait_for(expression, what, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if browser.evaluate(expression):
                return True
        except RuntimeError:
            pass
        time.sleep(0.2)
    raise AssertionError("timed out waiting for %s" % what)


def seen_watching():
    """Whether the server currently believes a kitchen screen is there."""
    with app.test_request_context("/"):
        connection = application.get_db_connection()
        cursor = connection.cursor(dictionary=True)
        try:
            return application.kitchen_is_watching(cursor, cafe_id)
        finally:
            cursor.close()
            connection.close()


try:
    browser.call("Page.enable")
    browser.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
                 deviceScaleFactor=1, mobile=False)

    # A real print dialog would block a headless browser, so printing is
    # recorded instead of done. The shell defines window.CafeShell at the
    # end of <body>, and the page claims its first ticket immediately
    # afterwards - so the stand-in has to be waiting on the assignment
    # rather than installed after the fact, or the first ticket prints
    # for real and is never seen here.
    browser.call("Page.addScriptToEvaluateOnNewDocument", source="""
        window.__printed = [];
        var held;
        Object.defineProperty(window, 'CafeShell', {
            configurable: true,
            get: function () { return held; },
            set: function (value) {
                held = value;
                value.autoPrint = function (url) {
                    window.__printed.push(url);
                };
            }
        });
    """)

    print("\n=== 1. Sign in and open the kitchen ===")
    browser.call("Page.navigate", url=BASE + "/login")
    wait_for("!!document.querySelector('form')", "the sign-in form")
    browser.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'sam';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
        }())
    """)
    wait_for("location.pathname !== '/login'", "the app")

    browser.call("Page.navigate", url=BASE + "/kitchen")
    wait_for("!!document.getElementById('kitchenStateText')",
             "the kitchen screen")
    time.sleep(2.5)

    print("\n=== 2. It reaches the server ===")
    state = browser.evaluate(
        "document.getElementById('kitchenStateText').textContent")
    check("the screen says it is watching", state == "Watching",
          "it reads %r - the requests it makes are being turned away" % state)
    check("and the server agrees a kitchen screen is there",
          seen_watching(),
          "the counter screens would still be printing customers' tickets")

    refused = [row for row in REJECTED if "kitchen" in row[1]]
    check("nothing it sent was turned away", not refused,
          "the server refused: %s" % refused)

    print("\n=== 3. It shows what is waiting ===")
    wait_for("document.querySelectorAll('.kitchen-ticket').length > 0",
             "a ticket on the board")

    board = json.loads(browser.evaluate("""
        (function () {
            var card = document.querySelector('.kitchen-ticket');
            return JSON.stringify({
                id: card.querySelector('.kitchen-ticket__id').textContent,
                from: card.querySelector('.kitchen-ticket__from').textContent,
                items: card.querySelectorAll('.kitchen-ticket__items li').length,
                phone: card.classList.contains('kitchen-ticket--phone')
            });
        }())
    """))

    check("the order is on the board", board["id"].startswith("#"),
          "the card shows %r" % board["id"])
    check("with its items, so it can be cooked from",
          board["items"] >= 1, "the card lists no items")
    check("and it is marked as having come from a table",
          board["phone"] and "table" in board["from"].lower(),
          "the card says %r" % board["from"])

    print("\n=== 4. It claims the ticket and prints it ===")
    wait_for("window.__printed.length > 0", "the ticket to be printed")
    printed = browser.evaluate("JSON.stringify(window.__printed)")
    check("the kitchen ticket was sent to the printer",
          "/print/kot/" in printed, "it printed %s" % printed)

    claimed = mysql_shim._DB.execute(
        "SELECT kot_printed FROM orders WHERE source = 'qr' "
        "ORDER BY order_id LIMIT 1").fetchone()
    check("and the order is marked as printed",
          claimed and claimed[0] == 1,
          "kot_printed is %s, so it would print again on the next look"
          % (claimed[0] if claimed else None))

    # It must not print the same thing over and over.
    before = int(browser.evaluate("window.__printed.length"))
    time.sleep(7)
    check("it does not print the same ticket again",
          int(browser.evaluate("window.__printed.length")) == before,
          "the same ticket printed more than once")

    print("\n=== 5. Marking one done clears it ===")
    browser.evaluate("document.querySelector('[data-done]').click()")
    time.sleep(2.0)
    check("the board empties once nothing is waiting",
          browser.evaluate(
              "document.querySelectorAll('.kitchen-ticket').length") == 0,
          "the finished order is still on the board")
    check("nothing was refused while doing it",
          not [row for row in REJECTED if row[2] >= 400
               and "kitchen" in row[1]],
          "the server refused: %s" % REJECTED)

finally:
    browser.close()

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
