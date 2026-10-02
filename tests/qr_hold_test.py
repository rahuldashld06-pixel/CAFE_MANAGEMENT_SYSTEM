"""
The minute between "Send to kitchen" and the kitchen.

Ordering from a table is not one decision. Somebody sends a coffee and
remembers a pastry ten seconds later, and that was a second order: a
second number to quote, a second ticket in the kitchen, and two lines at
the counter for one table. So the button opens a window instead of
sending. Anything added during it goes on the same order, because
nothing has been sent - the basket is still the hidden fields on the
page. Confirming ends the window early; letting it run out sends it too,
so a customer who has put the phone down still gets their food.

All of that is behaviour in a browser, and none of it can be seen from
the server: every one of these checks is about what has NOT been sent
yet, which reads on the server as a cafe with no orders.

The length of the window is on the form as data-hold, so this suite can
shorten it to two seconds and watch it run out. That it is a minute on
the real page is checked separately, against the template.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without
one. No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/qr_hold_test.py
"""
import os
import re
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "qr-hold-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5811
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
    "cafe_name": "The Slow Pour", "full_name": "Hold Owner",
    "username": "hold", "phone_number": "", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "All Day",
                                   "description": "",
                                   "_csrf_token": csrf()},
          follow_redirects=True)
category = re.search(
    r'<option value="(\d+)">',
    seed.get("/foods/add").get_data(as_text=True)).group(1)

for food, price in (("Cortado", "150"), ("Almond Croissant", "120")):
    seed.post("/foods/add", data={
        "food_name": food, "category_id": category, "price": price,
        "quantity": "50", "minimum_stock": "1", "description": "",
        "_csrf_token": csrf()}, follow_redirects=True)

with seed.session_transaction() as sess:
    CAFE = sess.get("cafe_id")
TOKEN = application.get_public_token(CAFE)
MENU = BASE + "/m/" + TOKEN


def orders_now():
    """How many orders this cafe has. Nothing sent means nothing here."""
    row = mysql_shim._DB.execute(
        "SELECT COUNT(*) FROM orders WHERE cafe_id = ?", (CAFE,)).fetchone()
    return row[0] if row else 0


threading.Thread(target=lambda: app.run(host="127.0.0.1", port=PORT,
                                        threaded=True, use_reloader=False),
                 daemon=True).start()
for _ in range(80):
    try:
        urllib.request.urlopen(BASE + "/healthz", timeout=1).read()
        break
    except Exception:
        time.sleep(0.25)

browser_path = cdp.find_browser()
if not browser_path:
    print("SKIPPED: no browser")
    sys.exit(0)

b = cdp.Browser(browser_path)


def wait(expr, what, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if b.evaluate(expr):
                return
        except RuntimeError:
            pass
        time.sleep(0.12)
    raise AssertionError("timeout: " + what)


def open_menu(hold=None):
    """The customer's menu, with the window optionally shortened."""
    b.call("Page.navigate", url=MENU)
    # Complete, not merely present. The page's script sits at the end of
    # the body, so a tap on Send to kitchen before it has run posts the
    # form the way it always did - which is right for a phone with no
    # JavaScript, and is not what any of this is about.
    wait("document.readyState === 'complete'"
         " && !!document.getElementById('orderForm')", "the menu")
    if hold is not None:
        b.evaluate("document.getElementById('orderForm')"
                   ".setAttribute('data-hold', '%d')" % hold)


def add(which=0, taps=1):
    """Tap + on the nth dish."""
    b.evaluate("""
        (function () {
            var plus = document.querySelectorAll(".p-step [data-step='1']");
            for (var n = 0; n < %d; n++) { plus[%d].click(); }
            return true;
        }())
    """ % (taps, which))


def tap(selector):
    b.evaluate("document.querySelector(%r).click()" % selector)


def window_up():
    return b.evaluate(
        "!document.getElementById('holdWindow').hidden")


def clock():
    return b.evaluate("document.getElementById('holdClock').textContent")


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=780,
           deviceScaleFactor=1, mobile=True)

    # =================================================================
    print("\n=== 1. The button opens a window, it does not send ===")
    # =================================================================
    before = orders_now()
    open_menu()
    add(0)
    tap("#sendBtn")
    wait("!document.getElementById('holdWindow').hidden", "the window")

    check("Send to kitchen opens the window", window_up(),
          "the window did not appear")

    check("and nothing has been sent", orders_now() == before,
          "%d order(s) appeared while the customer was still deciding"
          % (orders_now() - before))

    check("the page is still the menu",
          b.evaluate("location.pathname") == "/m/" + TOKEN,
          "it went to %s" % b.evaluate("location.pathname"))

    check("the window starts at a full minute", clock() == "1:00",
          "it reads %r" % clock())

    check("and says what is about to go",
          b.evaluate("document.getElementById('holdSum').textContent")
          == b.evaluate(
              "document.getElementById('barCount').textContent + ' \\u00b7 ' "
              "+ document.getElementById('barTotal').textContent"),
          "the window and the bar disagree about the order")

    def seconds_left():
        left = clock().split(":")
        return int(left[0]) * 60 + int(left[1])

    was = seconds_left()
    time.sleep(1.4)
    check("the clock is running", seconds_left() < was,
          "it still reads %r a second and a half later" % clock())

    # =================================================================
    print("\n=== 2. Adding something else keeps one order ===")
    # =================================================================
    tap("#holdMore")
    check("Add something else closes the window", not window_up(),
          "the window is still up")
    check("and still nothing has been sent", orders_now() == before,
          "an order went while the window was being closed")

    check("what was already chosen is still chosen",
          b.evaluate("document.querySelector('.p-step input').value") == "1",
          "the basket was cleared, so the second dish would be a second "
          "order after all")

    add(1)
    two = b.evaluate("document.getElementById('barCount').textContent")
    check("and the second dish joins it",
          "2 items" in two, "the bar reads %r" % two)

    tap("#sendBtn")
    wait("!document.getElementById('holdWindow').hidden", "the window again")
    check("the window opens again at a full minute", clock() == "1:00",
          "it reads %r, so the minute carried on from last time" % clock())

    # =================================================================
    print("\n=== 3. Escape and the backdrop mean 'not yet' ===")
    # =================================================================
    b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape",
           code="Escape", windowsVirtualKeyCode=27)
    b.call("Input.dispatchKeyEvent", type="keyUp", key="Escape",
           code="Escape", windowsVirtualKeyCode=27)
    time.sleep(0.2)
    check("Escape closes the window", not window_up(),
          "the window is still up after Escape")
    check("and sends nothing", orders_now() == before,
          "Escape sent the order")

    # =================================================================
    print("\n=== 4. Send it now ends the minute early ===")
    # =================================================================
    tap("#sendBtn")
    wait("!document.getElementById('holdWindow').hidden", "the window")
    started = time.time()
    tap("#holdNow")
    wait("location.pathname.indexOf('/placed/') > -1", "the order page")
    took = time.time() - started

    check("it goes at once, rather than at the end of the minute",
          took < 20, "it took %.1f seconds" % took)

    check("and it is one order, not two",
          orders_now() == before + 1,
          "%d orders were written" % (orders_now() - before))

    both = b.evaluate("document.body.textContent")
    check("carrying both dishes",
          "Cortado" in both and "Almond Croissant" in both,
          "the order that arrived is not the one that was being held")

    check("and the customer is given their number",
          bool(re.search(r"#\d+", b.evaluate(
              "document.querySelector('.p-done__number').textContent"))),
          "no number on the page")

    # =================================================================
    print("\n=== 5. Left alone, it sends itself ===")
    # =================================================================
    before = orders_now()
    open_menu(hold=2)
    add(0)
    tap("#sendBtn")
    wait("!document.getElementById('holdWindow').hidden", "the window")

    check("a shortened window starts where it was told to",
          clock() == "0:02", "it reads %r" % clock())
    check("and sends nothing on opening", orders_now() == before,
          "it sent as it opened")

    wait("location.pathname.indexOf('/placed/') > -1",
         "the order sending itself", timeout=15)
    check("the window runs out and the order goes",
          orders_now() == before + 1,
          "%d orders were written" % (orders_now() - before))

    # Settles here: a second submission would arrive after the first
    # page had already loaded, and would be a second order.
    time.sleep(1.5)
    check("exactly one order, once",
          orders_now() == before + 1,
          "it sent %d times" % (orders_now() - before))

    # =================================================================
    print("\n=== 6. A minute is what the page says ===")
    # =================================================================
    # Shortened above, so the real length is read from the template
    # rather than from the page this suite has been editing.
    source = urllib.request.urlopen(MENU, timeout=10).read().decode("utf-8")
    check("the page asks for sixty seconds",
          re.search(r'data-hold="60"', source) is not None,
          "the form does not ask for a minute")

    check("and nothing in the page hard-codes a different one",
          "HOLD_SECONDS" not in source,
          "the length is written down twice, and the two can drift")

    # The window is the whole reason the menu can be returned to, so the
    # one and the other are checked together: with no script at all the
    # form still posts to the same place it always did.
    check("with no script, Send to kitchen is still a plain submit",
          'type="submit"' in source
          and 'action="/m/%s/order"' % TOKEN in source,
          "the form no longer posts on its own")

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
