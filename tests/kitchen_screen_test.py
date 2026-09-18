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
ASKED = []


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
    # Every time a customer's page asks whether the food is ready.
    if request.path.startswith("/m/") and "/status/" in request.path:
        ASKED.append(request.path)
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
    ticket = mysql_shim._DB.execute(
        "SELECT order_id FROM orders WHERE source = 'qr' "
        "ORDER BY order_id LIMIT 1").fetchone()
    check("the kitchen ticket was sent to the printer",
          ticket is not None
          and ("/orders/%d/kot" % ticket[0]) in printed,
          "it printed %s" % printed)

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

    print("\n=== 5. Marking one done marks its dishes ===")
    browser.evaluate("document.querySelector('[data-done]').click()")
    time.sleep(2.0)

    check("the order stays on the board",
          browser.evaluate(
              "document.querySelectorAll('.kitchen-ticket').length") >= 1,
          "it vanished, so there is nothing left to have turned green")
    check("and every dish on it reads as made",
          browser.evaluate(
              "document.querySelectorAll("
              "'.kitchen-ticket--done .kitchen-line--made').length") >= 1
          and browser.evaluate(
              "document.querySelectorAll("
              "'.kitchen-ticket--done .kitchen-line--todo').length") == 0,
          "a finished ticket still has lines reading as still to make")
    check("and it offers no buttons once it is done",
          browser.evaluate(
              "document.querySelectorAll("
              "'.kitchen-ticket--done [data-done], "
              ".kitchen-ticket--done [data-cancel]').length") == 0,
          "a finished order can still be finished again")

    check("nothing was refused while doing it",
          not [row for row in REJECTED if row[2] >= 400
               and "kitchen" in row[1]],
          "the server refused: %s" % REJECTED)

    print("\n=== 6. And it does not print again once it is done ===")
    settled = int(browser.evaluate("window.__printed.length"))
    time.sleep(7)
    check("a finished order is not sent to the printer again",
          int(browser.evaluate("window.__printed.length")) == settled,
          "now that finished orders stay on the board, one of them "
          "reprinted on the next sweep")

    print("\n=== 7. The customer at the table is told, without reloading ===")
    # The whole point of Done reaching them: someone sitting at a table has
    # no counter to watch. Their page has to change under them.
    waiting = guest.post("/m/%s/order" % token,
                         data={"quantity_%s" % food_ids[0]: "1"},
                         follow_redirects=False)
    theirs = int(waiting.headers["Location"].rstrip("/").split("/")[-1])

    browser.call("Page.navigate",
                 url=BASE + "/m/%s/placed/%d" % (token, theirs))
    wait_for("!!document.getElementById('orderStateText')",
             "the customer's page")

    def said():
        return browser.evaluate(
            "document.getElementById('orderStateText').textContent")

    check("it starts by saying the order is with the kitchen",
          said() == "In the kitchen", "it says %r" % said())

    # Pressed from the till rather than in this browser, because what is
    # being tested is that the customer's page hears about it by itself.
    seed.post("/orders/complete/%d" % theirs,
              data={"_csrf_token": csrf(seed)}, follow_redirects=True)

    told = wait_for(
        "document.getElementById('orderStateText').textContent"
        " === 'Your food is ready'",
        "the customer to be told", timeout=25)

    check("and it changes to say the food is ready, on its own", told,
          "after the kitchen pressed Done the page still says %r" % said())

    check("the panel reads as ready, not just the words",
          browser.evaluate(
              "document.getElementById('orderPanel')"
              ".getAttribute('data-state')") == "ready",
          "the page still reads as waiting")

    # And it lets go. A page that went on asking every five seconds would
    # sit on a table all evening doing it.
    settled = len(ASKED)
    time.sleep(12)
    check("and it stops asking once there is nothing left to ask",
          len(ASKED) == settled,
          "it asked %d more times after the answer arrived"
          % (len(ASKED) - settled))

    print("\n=== 8. Ticking dishes off one at a time ===")
    # A ticket with two things on it is two jobs. This is the part a cook
    # actually touches, so it is worth pressing for real rather than
    # trusting that the markup looks right.
    two = guest.post("/m/%s/order" % token,
                     data={"quantity_%s" % food_ids[0]: "1",
                           "quantity_%s" % food_ids[1]: "1"},
                     follow_redirects=False)
    two_id = int(two.headers["Location"].rstrip("/").split("/")[-1])

    browser.call("Page.navigate", url=BASE + "/kitchen")
    wait_for("!!document.querySelector('.kitchen-tick')",
             "a ticket with dishes on it")
    time.sleep(2.0)

    def ticket():
        return ("document.querySelector('.kitchen-ticket [data-done=\"%d\"]')"
                ".closest('.kitchen-ticket')" % two_id)

    check("each dish on the ticket has something to press",
          browser.evaluate("%s.querySelectorAll('button.kitchen-tick').length"
                           % ticket()) == 2,
          "found %s"
          % browser.evaluate("%s.querySelectorAll('button.kitchen-tick')"
                             ".length" % ticket()))

    browser.evaluate("%s.querySelectorAll('button.kitchen-tick')[0].click()"
                     % ticket())
    time.sleep(2.5)

    check("pressing one marks that one and leaves the other",
          browser.evaluate("%s.querySelectorAll('.kitchen-line--made').length"
                           % ticket()) == 1
          and browser.evaluate("%s.querySelectorAll('.kitchen-line--todo')"
                               ".length" % ticket()) == 1,
          "made %s, still to make %s"
          % (browser.evaluate("%s.querySelectorAll('.kitchen-line--made')"
                              ".length" % ticket()),
             browser.evaluate("%s.querySelectorAll('.kitchen-line--todo')"
                              ".length" % ticket())))

    settled = mysql_shim._DB.execute(
        "SELECT order_status FROM orders WHERE order_id = ?",
        (two_id,)).fetchone()
    check("and the order is still with the kitchen",
          settled and settled[0] == "Pending",
          "the order went to %s with a dish still to make"
          % (settled[0] if settled else None))

    # The board redraws after each tap, so the one still to make is found
    # by its state rather than by its position - tapping [0] again would
    # simply un-tick the dish just ticked.
    browser.evaluate("%s.querySelector('.kitchen-line--todo "
                     "button.kitchen-tick').click()" % ticket())
    time.sleep(2.5)

    closed = mysql_shim._DB.execute(
        "SELECT order_status FROM orders WHERE order_id = ?",
        (two_id,)).fetchone()
    check("pressing the last one closes the order by itself",
          closed and closed[0] == "Completed",
          "the order is %s - somebody still has to press Done"
          % (closed[0] if closed else None))

    check("nothing was refused while ticking",
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
