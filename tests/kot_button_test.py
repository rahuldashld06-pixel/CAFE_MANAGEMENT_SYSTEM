"""
The kitchen ticket, printed when somebody asks for it.

Nothing prints on its own any more - a roll a day was going on tickets
nobody wanted. That left the kitchen ticket (KOT) with no way to be
printed at all, which is the opposite mistake. So it has a button in the
three places somebody is standing when they want one:

  - on every ticket on the Kitchen screen, open or done;
  - on every row of Order Status, which is over every page;
  - on New Order, straight after an order goes: in the notice, and in the
    order panel until the next order replaces it.

Each opens /orders/<id>/kot in a new tab, which prints itself from
there. window.open is replaced before each press so this suite sees what
would have opened without a print dialog appearing; a blocked pop-up is
the same function returning null, and the button has to say so rather
than do nothing.

What none of them may do is print without being pressed, or change the
order they belong to - a ticket is not a Done button.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without
one. No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/kot_button_test.py
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
os.environ["SECRET_KEY"] = "kot-button-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5829
BASE = "http://127.0.0.1:%d" % PORT

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Ticket Room", "full_name": "Kot Owner",
    "username": "kot", "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Hot",
                                   "description": "",
                                   "_csrf_token": csrf()},
          follow_redirects=True)
category = re.search(
    r'<option value="(\d+)">',
    seed.get("/foods/add").get_data(as_text=True)).group(1)
for food, price in (("Masala Chai", "60"), ("Veg Puff", "45")):
    seed.post("/foods/add", data={
        "food_name": food, "category_id": category, "price": price,
        "quantity": "80", "minimum_stock": "1", "description": "",
        "_csrf_token": csrf()}, follow_redirects=True)

FOODS = [int(r[0]) for r in mysql_shim._DB.execute(
    "SELECT food_id FROM foods ORDER BY food_id").fetchall()]


def place(food, qty=1):
    seed.post("/orders/add", data={"quantity_%d" % food: str(qty),
                                   "_csrf_token": csrf()},
              follow_redirects=True)


place(FOODS[0], 2)
place(FOODS[1], 1)
place(FOODS[0], 1)

ORDERS = [int(r[0]) for r in mysql_shim._DB.execute(
    "SELECT order_id FROM orders ORDER BY order_id").fetchall()]
# One done, so the Kitchen screen has a finished ticket to offer too.
seed.post("/orders/complete/%d" % ORDERS[0], data={"_csrf_token": csrf()},
          follow_redirects=True)
mysql_shim.skip_tour()


def status_of(order_id):
    row = mysql_shim._DB.execute(
        "SELECT order_status FROM orders WHERE order_id = ?",
        (order_id,)).fetchone()
    return row[0] if row else None


def order_count():
    return mysql_shim._DB.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


# =====================================================================
print("\n=== 0. The server's half ===")
# =====================================================================
kot = seed.get("/orders/%d/kot" % ORDERS[1])
check("the ticket page is there for an order", kot.status_code == 200,
      "it answered %d" % kot.status_code)
check("and it is a ticket for that order",
      "Veg Puff" in kot.get_data(as_text=True),
      "the dish on the order is not on its ticket")

answer = seed.post("/orders/add",
                   data={"quantity_%d" % FOODS[1]: "1", "_csrf_token": csrf()},
                   headers={"X-Requested-With": "XMLHttpRequest",
                            "Accept": "application/json"})
payload = answer.get_json(silent=True) or {}
check("a new order says which order it was",
      payload.get("success") and payload.get("order_id"),
      "the answer was %r" % payload)
check("and the number the kitchen will call out",
      payload.get("daily_no") is not None,
      "no daily number in %r" % sorted(payload))
check("and the message uses that number, not the database's",
      ("#%s" % payload.get("daily_no")) in (payload.get("message") or ""),
      "the message reads %r" % payload.get("message"))

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
    print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
    sys.exit(1 if FAILED else 0)

b = cdp.Browser(browser_path)


def wait(expr, what="", timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if b.evaluate(expr):
                return True
        except RuntimeError:
            pass
        time.sleep(0.12)
    return False


def open_page(path):
    b.call("Page.navigate", url=BASE + path)
    wait("document.readyState === 'complete'", path)
    time.sleep(0.4)


def trap(blocked=False):
    """Stand in for window.open: note the address, open nothing."""
    b.evaluate("""
        (function () {
            window.__opened = [];
            window.open = function (url) {
                window.__opened.push(String(url));
                return %s;
            };
            return true;
        }())
    """ % ("null" if blocked else "{}"))


def opened():
    return b.evaluate("(window.__opened || []).join(' ')") or ""


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
           deviceScaleFactor=1, mobile=False)
    open_page("/login")
    b.evaluate("""(function(){var f=document.querySelector('form');
        f.querySelector('[name=username]').value='kot';
        f.querySelector('[name=password]').value='password123';
        f.querySelector('[type=submit]').click(); return 1;}())""")
    wait("location.pathname !== '/login'", "signed in")
    time.sleep(0.8)

    # =================================================================
    print("\n=== 1. The Kitchen screen ===")
    # =================================================================
    open_page("/kitchen")
    check("every ticket has a print button",
          wait("document.querySelectorAll('.kitchen-ticket').length > 0 && "
               "document.querySelectorAll('.kitchen-ticket').length === "
               "document.querySelectorAll('.kitchen-ticket [data-print-kot]').length"),
          "%s tickets, %s print buttons" % (
              b.evaluate("document.querySelectorAll('.kitchen-ticket').length"),
              b.evaluate("document.querySelectorAll("
                         "'.kitchen-ticket [data-print-kot]').length")))
    check("a finished ticket too, for the one that needs doing again",
          b.evaluate("!!document.querySelector("
                     "'.kitchen-ticket--done [data-print-kot]')"),
          "the done ticket has no print button")
    check("and it says what it is to a screen reader",
          "kitchen ticket" in (b.evaluate(
              "document.querySelector('.kitchen-ticket [data-print-kot]')"
              ".getAttribute('aria-label')") or ""),
          "the button is an unlabelled printer icon")
    check("nothing opened by itself", b.evaluate(
        "typeof window.__opened === 'undefined'") or opened() == "",
          "something opened before anybody pressed anything")

    open_id = ORDERS[1]
    trap()
    b.evaluate("document.querySelector("
               "'.kitchen-ticket [data-print-kot=\"%d\"]').click()" % open_id)
    time.sleep(0.3)
    check("pressing it opens that order's ticket",
          opened() == "/orders/%d/kot" % open_id,
          "it opened %r" % opened())
    time.sleep(0.6)
    check("and does not touch the order", status_of(open_id) == "Pending",
          "the order is now %r" % status_of(open_id))
    check("which is still on the screen, still open",
          b.evaluate("!!document.querySelector('.kitchen-ticket:not("
                     ".kitchen-ticket--done) [data-print-kot=\"%d\"]')"
                     % open_id),
          "the ticket left the open orders")

    # =================================================================
    print("\n=== 2. Order Status, over every page ===")
    # =================================================================
    open_page("/foods")
    trap()
    b.evaluate("document.getElementById('orderStatusFab').click()")
    check("its rows carry a print button",
          wait("document.querySelectorAll('#orderStatusOverlay "
               "[data-print-kot]').length > 0"),
          "no print button in Order Status")
    first = b.evaluate("document.querySelector('#orderStatusOverlay "
                       "[data-print-kot]').getAttribute('data-print-kot')")
    b.evaluate("document.querySelector('#orderStatusOverlay "
               "[data-print-kot]').click()")
    time.sleep(0.3)
    check("pressing one opens that order's ticket",
          opened() == "/orders/%s/kot" % first,
          "it opened %r" % opened())
    check("and Order Status stays open to press the next",
          b.evaluate("document.getElementById('orderStatusOverlay')"
                     ".classList.contains('open')"),
          "the popup closed on the press")

    trap(blocked=True)
    b.evaluate("document.querySelector('#orderStatusOverlay "
               "[data-print-kot]').click()")
    time.sleep(0.3)
    check("a blocked pop-up is shown, not swallowed",
          b.evaluate("document.querySelector('#orderStatusOverlay "
                     "[data-print-kot]').classList"
                     ".contains('order-status-print--blocked')"),
          "the button looks the same whether it worked or not")
    b.evaluate("document.getElementById('orderStatusClose').click()")

    # =================================================================
    print("\n=== 3. New Order, straight after an order goes ===")
    # =================================================================
    open_page("/orders/add")
    trap()
    check("before any order, there is nothing to print",
          b.evaluate("document.getElementById('receiptLast').hidden"),
          "a Print KOT is offered for no order")

    before = order_count()
    b.evaluate("""
        (function () {
            var plus = document.querySelector(
                '.food-card:not(.food-card--mirror) .quantity-plus');
            plus.click();
            document.getElementById('orderForm').requestSubmit();
            return true;
        }())
    """)
    check("the order goes",
          wait("!document.getElementById('receiptLast').hidden", "sent", 15)
          and order_count() == before + 1,
          "%d orders were written" % (order_count() - before))
    newest = mysql_shim._DB.execute(
        "SELECT order_id, daily_no FROM orders ORDER BY order_id DESC"
    ).fetchone()
    check("the notice offers its ticket",
          b.evaluate("!document.getElementById('orderPlacedKot').hidden"),
          "the notice has no Print KOT")
    check("and names it by the number the kitchen calls",
          ("#%s" % (newest[1] or newest[0])) in (b.evaluate(
              "document.getElementById('receiptLastText').textContent") or ""),
          "the panel reads %r" % b.evaluate(
              "document.getElementById('receiptLastText').textContent"))
    check("nothing printed until asked", opened() == "",
          "it opened %r by itself" % opened())

    b.evaluate("document.getElementById('receiptLastKot').click()")
    time.sleep(0.3)
    check("the order panel's button opens that order's ticket",
          opened() == "/orders/%d/kot" % newest[0],
          "it opened %r" % opened())

    trap()
    b.evaluate("document.getElementById('orderPlacedKot').click()")
    time.sleep(0.3)
    check("and so does the notice's",
          opened() == "/orders/%d/kot" % newest[0],
          "it opened %r" % opened())

    trap(blocked=True)
    b.evaluate("document.getElementById('receiptLastKot').click()")
    time.sleep(0.3)
    check("a blocked pop-up says how to fix it",
          "pop-up" in (b.evaluate(
              "document.getElementById('receiptLastKot').textContent") or ""),
          "the button reads %r" % b.evaluate(
              "document.getElementById('receiptLastKot').textContent"))

    # =================================================================
    print("\n=== 4. On a phone too ===")
    # =================================================================
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=780,
           deviceScaleFactor=1, mobile=True)
    b.call("Emulation.setTouchEmulationEnabled", enabled=True,
           maxTouchPoints=5)
    open_page("/kitchen")
    wait("!!document.querySelector('.kitchen-ticket [data-print-kot]')",
         "phone kitchen")
    box = b.evaluate("""
        (function () {
            var r = document.querySelector('.kitchen-ticket [data-print-kot]')
                .getBoundingClientRect();
            return [Math.round(r.width), Math.round(r.height),
                    Math.round(r.right), window.innerWidth].join(',');
        }())
    """).split(",")
    check("the kitchen's print button is a fingertip wide on a phone",
          int(box[0]) >= 32 and int(box[1]) >= 32,
          "it is %sx%s" % (box[0], box[1]))
    check("and on the screen", int(box[2]) <= int(box[3]),
          "it ends at %s on a %s screen" % (box[2], box[3]))

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
