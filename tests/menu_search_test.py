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
