"""
Real-browser test for the Hot Selling shelf's mirrored cards.

A best seller is drawn twice: once on the shelf, once under its category.
Only the card under the category carries the form field, so the two must be
kept in step by hand - and, above all, ordering two of something that is on
screen twice must charge for two, not four.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without one.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/hot_mirror_test.py
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
os.environ["SECRET_KEY"] = "hot-mirror-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5851
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
    "cafe_name": "Mirror Cafe", "full_name": "M Owner", "username": "mir",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as s:
        return s.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Beverages",
                                   "description": "", "_csrf_token": csrf()},
          follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     seed.get("/foods/add").get_data(as_text=True)).group(1)
for food in ["Cold Coffee", "Masala Chai"]:
    seed.post("/foods/add", data={
        "food_name": food, "category_id": category, "price": "50",
        "quantity": "100", "description": "", "_csrf_token": csrf()},
        follow_redirects=True)

ids = {name: fid for fid, name in re.findall(
    r'id="food_card_(\d+)".*?food-card-name">([^<]+)<',
    seed.get("/orders/add").get_data(as_text=True), re.S)}
HOT = ids["Cold Coffee"]

# Make Cold Coffee the best seller so it lands on the shelf.
for _ in range(4):
    seed.post("/orders/add", data={"quantity_%s" % HOT: "1",
                                   "_csrf_token": csrf()},
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
        time.sleep(0.15)
    raise AssertionError("timeout: " + what)


def tap(selector):
    """Genuine input at the element's centre, so an overlay cannot be missed."""
    point = b.evaluate("""
        (function () {
            var el = document.querySelector(%r);
            if (!el) return null;
            var r = el.getBoundingClientRect();
            el.scrollIntoView({block: 'center'});
            r = el.getBoundingClientRect();
            return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)];
        })()
    """ % selector)
    if not point:
        raise AssertionError("no element for " + selector)
    for kind in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=kind, x=point[0], y=point[1],
               button="left", clickCount=1)
    time.sleep(0.4)


def value(element_id):
    return b.evaluate("document.getElementById(%r).value" % element_id)


def totals():
    """
    Read the floating summary - the one the page actually shows.

    (The older bottom-bar ids that calculateOrderTotal() writes to are not in
    the markup at all, so they cannot be used to check anything.)
    """
    return {
        "items": b.evaluate(
            "document.getElementById('selectedItemCount').textContent.trim()"),
        "total": b.evaluate(
            "document.getElementById('floatingTotal').textContent.trim()"),
    }


SHELF_PLUS = '#food_card_hot_%s .quantity-plus' % HOT
SHELF_MINUS = '#food_card_hot_%s .quantity-minus' % HOT
CATEGORY_PLUS = '#food_card_%s .quantity-plus' % HOT

try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000,
           deviceScaleFactor=1, mobile=False)
    b.call("Page.navigate", url=BASE + "/login")
    wait("document.readyState==='complete' && !!document.querySelector('form')", "login")
    b.evaluate("""(function(){var f=document.querySelector('form');
        f.querySelector('[name=username]').value='mir';
        f.querySelector('[name=password]').value='password123';
        f.submit(); return 1;})()""")
    wait("!!document.getElementById('page-view')", "shell")
    b.call("Page.navigate", url=BASE + "/orders/add")
    wait("!!document.getElementById('orderForm')", "the menu")
    time.sleep(0.8)

    print("\n=== The same food is on screen twice ===")
    check("it is on the Hot Selling shelf",
          b.evaluate("!!document.getElementById('food_card_hot_%s')" % HOT))
    check("and under its category",
          b.evaluate("!!document.getElementById('food_card_%s')" % HOT))
    check("only one of them carries the form field",
          b.evaluate("document.getElementsByName('quantity_%s').length" % HOT) == 1,
          "two named fields would post the quantity twice")

    print("\n=== Adding from the shelf ===")
    tap(SHELF_PLUS)
    check("the shelf card shows 1", value("quantity_hot_%s" % HOT) == "1")
    check("the category card follows", value("quantity_%s" % HOT) == "1",
          "the two cards are out of step")
    check("the summary counts one unit, not one per card",
          totals()["items"] == "1", "summary says %s" % totals())

    print("\n=== Adding from the category card ===")
    tap(CATEGORY_PLUS)
    check("both cards show 2",
          value("quantity_hot_%s" % HOT) == "2" and value("quantity_%s" % HOT) == "2",
          "shelf=%s category=%s" % (value("quantity_hot_%s" % HOT),
                                    value("quantity_%s" % HOT)))
    # The badge counts units. Two presses on one food must read 2, not the
    # 4 that two live inputs for the same food would have produced.
    check("two presses on one food read as two units, not four",
          totals()["items"] == "2", "summary says %s" % totals())
    check("the money follows the real quantity, not the number of cards",
          totals()["total"] == "105.00",
          "2 x 50 plus 5%% tax should be 105.00, got %s - a mirrored input "
          "would double it" % totals()["total"])

    print("\n=== Taking one back from the shelf ===")
    tap(SHELF_MINUS)
    check("both cards show 1",
          value("quantity_hot_%s" % HOT) == "1" and value("quantity_%s" % HOT) == "1",
          "shelf=%s category=%s" % (value("quantity_hot_%s" % HOT),
                                    value("quantity_%s" % HOT)))

    print("\n=== Placing the order ===")
    stock_before = int(re.search(
        r'Stock: (\d+)',
        b.evaluate("document.getElementById('food_status_%s').textContent" % HOT)
    ).group(1))

    tap(CATEGORY_PLUS)          # back to 2
    b.evaluate("""
        document.getElementById('orderForm')
                .dispatchEvent(new Event('submit', {cancelable: true, bubbles: true}));
        true
    """)
    time.sleep(3.0)

    latest = seed.get("/orders").get_data(as_text=True)
    newest = re.findall(r'/orders/(\d+)"', latest)[0]
    detail = seed.get("/orders/%s" % newest).get_data(as_text=True)
    quantities = re.findall(r'<span class="badge">\s*(\d+)\s*</span>', detail)

    check("the order was placed", "Cold Coffee" in detail,
          "the newest order does not contain the item")
    check("it charged for two, not four",
          "2" in quantities and "4" not in quantities,
          "quantities on the receipt: %s - a mirrored field would double it"
          % quantities)

    stock_after = int(re.search(
        r'Stock: (\d+)',
        b.evaluate("document.getElementById('food_status_%s').textContent" % HOT)
    ).group(1))
    check("stock fell by two, not four",
          stock_before - stock_after == 2,
          "stock went %d -> %d" % (stock_before, stock_after))
    check("the shelf card shows the new stock too",
          b.evaluate("document.getElementById('food_status_hot_%s').textContent" % HOT)
          == b.evaluate("document.getElementById('food_status_%s').textContent" % HOT),
          "the shelf copy is showing stale stock")
    check("both cards reset to zero",
          value("quantity_hot_%s" % HOT) == "0" and value("quantity_%s" % HOT) == "0",
          "shelf=%s category=%s" % (value("quantity_hot_%s" % HOT),
                                    value("quantity_%s" % HOT)))

    print("\n=== Search still counts each food once ===")
    b.evaluate("""
        (function () {
            var i = document.getElementById('foodSearch');
            i.value = 'cold';
            i.dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        })()
    """)
    time.sleep(0.4)
    check("the badge counts the food once though two cards are shown",
          b.evaluate("document.getElementById('foodCount').textContent.trim()")
          == "1 of 2 Food Items",
          b.evaluate("document.getElementById('foodCount').textContent"))

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
