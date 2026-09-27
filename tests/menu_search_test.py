"""
Real-browser test for the New Order menu: category grouping and search.

The menu is grouped into category sections (A-Z, and A-Z inside each) with a
search box in the panel header. This drives a headless browser through it:
searching by food name, by category, case-insensitively, the empty state, and
- the one that matters at a till - that filtering only ever hides cards, so a
quantity already keyed in is never silently dropped from the order.

Also re-checks the page after an instant navigation, since instant.js re-runs
the search script every time the page is swapped back in.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without one.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/menu_search_test.py
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
os.environ["SECRET_KEY"] = "search-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5803
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
    "cafe_name": "Search Cafe", "full_name": "S Owner", "username": "see",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as s:
        return s.get("_csrf_token", "")


for name in ["Snacks", "Beverages", "Desserts"]:
    seed.post("/categories/add",
              data={"category_name": name, "description": "",
                    "_csrf_token": csrf()}, follow_redirects=True)

page = seed.get("/foods/add").get_data(as_text=True)
by_name = {n.strip(): i for i, n in re.findall(
    r'<option value="(\d+)">\s*([^<]+?)\s*</option>', page, re.S)}

MENU = [("Zebra Cake", "Desserts"), ("apple pie", "Desserts"),
        ("Masala Chai", "Beverages"), ("Cold Coffee", "Beverages"),
        ("Samosa", "Snacks"), ("Veg Puff", "Snacks")]
for food, category in MENU:
    seed.post("/foods/add", data={
        "food_name": food, "category_id": by_name[category], "price": "40",
        "quantity": "10", "description": "", "_csrf_token": csrf()},
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


browser_path = cdp.find_browser()
if not browser_path:
    print("SKIPPED: no browser")
    sys.exit(0)

# Every account this suite made has been shown round already, so
# the first-sign-in tour does not open over what is being tested.
mysql_shim.skip_tour()

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


def visible_names():
    return b.evaluate("""
        Array.prototype.slice
            .call(document.querySelectorAll('.food-card'))
            .filter(function (c) { return !c.hidden; })
            .map(function (c) {
                return c.querySelector('.food-card-name').textContent.trim();
            })
    """)


def visible_sections():
    return b.evaluate("""
        Array.prototype.slice
            .call(document.querySelectorAll('.food-group'))
            .filter(function (g) { return !g.hidden; })
            .map(function (g) {
                return g.querySelector('.food-group__name').textContent.trim();
            })
    """)


def type_query(text):
    b.evaluate("""
        (function () {
            var i = document.getElementById('foodSearch');
            i.value = %r;
            i.dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        })()
    """ % text)
    time.sleep(0.25)


try:
    b.call("Page.enable")
    b.call("Page.navigate", url=BASE + "/login")
    wait("document.readyState==='complete' && !!document.querySelector('form')", "login")
    b.evaluate("""(function(){var f=document.querySelector('form');
        f.querySelector('[name=username]').value='see';
        f.querySelector('[name=password]').value='password123';
        f.submit(); return 1;})()""")
    wait("!!document.getElementById('page-view')", "shell")

    print("\n=== Menu grouping (full page load) ===")
    b.call("Page.navigate", url=BASE + "/orders/add")
    wait("!!document.getElementById('foodSearch')", "the search box")

    check("categories render A-Z",
          visible_sections() == ["Beverages", "Desserts", "Snacks"],
          "got %s" % visible_sections())
    check("items are A-Z inside each category",
          visible_names() == ["Cold Coffee", "Masala Chai", "apple pie",
                              "Zebra Cake", "Samosa", "Veg Puff"],
          "got %s" % visible_names())

    print("\n=== Searching by food name ===")
    type_query("coffee")
    check("a name search keeps only matching cards",
          visible_names() == ["Cold Coffee"], "got %s" % visible_names())
    check("categories with no match are hidden",
          visible_sections() == ["Beverages"], "got %s" % visible_sections())
    check("the badge reports the filtered count",
          b.evaluate("document.getElementById('foodCount').textContent.trim()")
          == "1 of 6 Food Items",
          b.evaluate("document.getElementById('foodCount').textContent"))
    check("the section count follows the filter, not the page total",
          b.evaluate("document.querySelector('.food-group:not([hidden]) .food-group__count').textContent.trim()") == "1",
          "section badge still shows the unfiltered total")

    print("\n=== Searching by category ===")
    type_query("snacks")
    check("a category search keeps that whole section",
          visible_names() == ["Samosa", "Veg Puff"], "got %s" % visible_names())

    print("\n=== Search is case-insensitive ===")
    type_query("ZEBRA")
    check("upper-case query still matches",
          visible_names() == ["Zebra Cake"], "got %s" % visible_names())

    print("\n=== No matches ===")
    type_query("pizza")
    check("the empty state appears",
          b.evaluate("!document.getElementById('foodSearchEmpty').hidden"))
    check("it quotes what was typed",
          b.evaluate("document.getElementById('foodSearchTerm').textContent") == "pizza")

    print("\n=== Filtering never clears a selection ===")
    type_query("")
    b.evaluate("""
        (function () {
            var input = document.querySelector('.quantity-input');
            input.value = 3;
            input.dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        })()
    """)
    picked = b.evaluate("document.querySelector('.quantity-input').id")
    type_query("pizza")          # hides everything, including the picked card
    time.sleep(0.5)
    check("a hidden card keeps its quantity",
          b.evaluate("document.getElementById('%s').value" % picked) == "3",
          "quantity was reset by filtering")

    type_query("")
    check("clearing the search restores the whole menu",
          len(visible_names()) == 6, "got %s" % visible_names())

    print("\n=== Clear button and Escape ===")
    type_query("coffee")
    check("the clear button appears once there is a query",
          not b.evaluate("document.getElementById('foodSearchClear').hidden"))
    b.evaluate("document.getElementById('foodSearchClear').click()")
    time.sleep(0.25)
    check("the clear button empties the box",
          b.evaluate("document.getElementById('foodSearch').value") == ""
          and len(visible_names()) == 6)

    print("\n=== Still works after an instant navigation ===")
    b.evaluate("window.Instant.visit(location.origin + '/billing', {})", False)
    wait("location.pathname === '/billing'", "billing")
    b.evaluate("window.Instant.visit(location.origin + '/orders/add', {})", False)
    wait("location.pathname === '/orders/add'", "back on new order")
    time.sleep(0.6)
    check("the search box is wired up again after a swap",
          b.evaluate("!!document.getElementById('foodSearch')"))
    type_query("samosa")
    check("searching works after returning to the page",
          visible_names() == ["Samosa"], "got %s" % visible_names())
    check("no full page reload happened",
          b.evaluate("performance.getEntriesByType('navigation').length") == 1)
    type_query("")

    print("\n=== Sections as tabs, the way the customer's menu reads ===")
    tabs = b.evaluate("""
        Array.prototype.map.call(
            document.querySelectorAll('#menuTabs [data-tab]'),
            function (t) { return t.textContent.replace(/\\s+/g, ' ').trim(); })
    """)
    check("every section is a tab, All first",
          len(tabs) == 4 and tabs[0].startswith("All items"),
          "the tabs read %s" % tabs)

    b.evaluate("""
        Array.prototype.filter.call(
            document.querySelectorAll('#menuTabs [data-tab]'),
            function (t) { return t.textContent.indexOf('Snacks') !== -1; }
        )[0].click()
    """)
    time.sleep(0.2)
    check("choosing a tab shows only that section",
          visible_sections() == ["Snacks"]
          and sorted(visible_names()) == ["Samosa", "Veg Puff"],
          "showing %s: %s" % (visible_sections(), visible_names()))

    type_query("puff")
    check("and a search looks inside the chosen tab",
          visible_names() == ["Veg Puff"], "got %s" % visible_names())
    type_query("")

    b.evaluate("document.querySelector('#menuTabs [data-tab=\"all\"]').click()")
    time.sleep(0.2)
    check("All brings the whole menu back",
          len(visible_names()) == 6, "got %s" % visible_names())

    print("\n=== On a large screen the order is written beside the menu ===")
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
           deviceScaleFactor=1, mobile=False)
    b.call("Page.navigate", url=BASE + "/orders/add")
    wait("document.readyState === 'complete' && "
         "!!document.getElementById('orderForm')", "new order")
    time.sleep(0.8)

    import json as _json
    layout = _json.loads(b.evaluate("""
        (function () {
            var menu = document.querySelector('.order-menu').getBoundingClientRect();
            var sheet = document.querySelector('.order-summary-popup');
            var box = sheet.getBoundingClientRect();
            var trigger = document.getElementById('orderSummaryTrigger');
            return JSON.stringify({
                beside: box.left >= menu.right - 1 && box.width > 250,
                shown: box.height > 150,
                fixed: getComputedStyle(document.getElementById(
                    'orderSummaryOverlay')).position === 'fixed',
                trigger: trigger ? getComputedStyle(trigger).display : 'none',
                modal: sheet.getAttribute('aria-modal'),
                hidden: document.getElementById('orderSummaryOverlay')
                                .getAttribute('aria-hidden')
            });
        }())
    """))
    check("the order sits beside the menu, always open",
          layout["beside"] and layout["shown"] and not layout["fixed"],
          "the order is not docked beside the menu: %s" % layout)
    check("so there is no floating button to open it",
          layout["trigger"] == "none",
          "the Order Summary button still floats over the menu")
    check("and it is part of the page, not a hidden dialog",
          layout["modal"] == "false" and layout["hidden"] == "false",
          "a screen reader is told it is a modal, or not there: %s" % layout)

    def basket():
        return b.evaluate("""
            Array.prototype.reduce.call(
                document.querySelectorAll('.quantity-input'),
                function (n, box) { return n + (Number(box.value) || 0); }, 0)
        """)

    # The sections above left dishes in the basket, and the page brings a
    # basket back after a reload on purpose - so start from an empty one.
    b.evaluate("""
        (function () {
            var clear = document.getElementById('receiptClear');
            if (clear && !clear.disabled) clear.click();
            var note = document.querySelector('.draft-note__discard');
            if (note) note.click();
            return true;
        }())
    """)
    time.sleep(0.4)

    b.evaluate("""
        (function () {
            var plus = document.querySelector(
                '.food-card:not(.food-card--mirror) .quantity-plus');
            plus.click(); plus.click();
            return true;
        }())
    """)
    time.sleep(0.5)
    check("a dish added on its card is a line on the order",
          b.evaluate("document.querySelectorAll('#selectedOrderItems "
                     ".selected-order-item').length") == 1
          and b.evaluate("document.querySelector('#selectedOrderItems "
                         ".receipt-step__qty').textContent") == "2",
          "the order does not show the dish")

    b.evaluate("document.querySelector('#selectedOrderItems "
               "[data-receipt-step=\"1\"]').click()")
    time.sleep(0.4)
    check("its own + on the order adds one more",
          basket() == 3, "the basket holds %s" % basket())

    b.evaluate("document.querySelector('#selectedOrderItems "
               "[data-receipt-step=\"-1\"]').click()")
    time.sleep(0.4)
    check("and its - takes one away",
          basket() == 2, "the basket holds %s" % basket())

    check("the button says what it will charge",
          b.evaluate("document.getElementById('placeTotal').textContent")
          == b.evaluate("document.getElementById('popupTotal').textContent")
          and b.evaluate("document.getElementById('placeTotal').textContent")
          != "0.00",
          "the button's total and the order's total disagree")

    b.evaluate("window.scrollTo(0, 700)")
    time.sleep(0.5)
    kept = _json.loads(b.evaluate("""
        (function () {
            var sheet = document.querySelector('.order-summary-popup')
                                .getBoundingClientRect();
            var head = document.querySelector('.page-header')
                               .getBoundingClientRect();
            return JSON.stringify({top: Math.round(sheet.top),
                                   headBottom: Math.round(head.bottom),
                                   bottom: Math.round(sheet.bottom)});
        }())
    """))
    check("the order stays in view while the menu scrolls",
          kept["top"] >= kept["headBottom"] - 1 and kept["bottom"] <= 900,
          "the order scrolled away or under the title: %s" % kept)
    b.evaluate("window.scrollTo(0, 0)")

    b.evaluate("document.getElementById('receiptClear').click()")
    time.sleep(0.4)
    check("Clear all empties the order",
          basket() == 0 and b.evaluate(
              "!!document.querySelector('#selectedOrderItems "
              ".order-summary-empty')"),
          "the basket still holds %s" % basket())

    b.call("Emulation.setDeviceMetricsOverride", width=390, height=780,
           deviceScaleFactor=2, mobile=True)
    time.sleep(0.6)
    phone = _json.loads(b.evaluate("""
        JSON.stringify({
            trigger: getComputedStyle(document.getElementById(
                'orderSummaryTrigger')).display,
            overlay: getComputedStyle(document.getElementById(
                'orderSummaryOverlay')).display
        })
    """))
    check("on a phone the order is the sheet behind a button again",
          phone["trigger"] != "none" and phone["overlay"] == "none",
          "on a phone: %s" % phone)


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
