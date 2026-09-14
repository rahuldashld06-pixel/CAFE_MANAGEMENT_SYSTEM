"""
The menu-import review screen, driven in a real browser.

Everything on this screen that matters is DOM behaviour: the tick-all box,
and the control that fills stock into every row at once. Setting stock one
row at a time is fine for five items and miserable for forty, which is the
size of menu this feature exists for.

Its server side is covered by menu_import_test. What is checked here is
that the controls move the fields they claim to, and that what is on
screen is what gets posted - the two bugs found on this project's other
screens were both invisible in the HTML and obvious in a browser.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.
No network and no database - the app runs on the SQLite stand-in, and the
call that reads the photo is stubbed.

Run with:  python tests/menu_review_test.py
"""
import os
import re
import sys
import threading
import time
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "menu-review-secret"
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

PORT = 5823
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


@app.before_request
def _serialise():
    if not _lock.acquire(timeout=30):
        raise RuntimeError("request lock timeout")


@app.teardown_request
def _release(exception=None):
    try:
        _lock.release()
    except RuntimeError:
        pass


# The reading itself is not what this tests, so it is stood in for.
application.ANTHROPIC_API_KEY = "stub-key-never-sent"
READING = [
    {"name": "Flat White", "category": "Coffee", "price": Decimal("180.00")},
    {"name": "Cold Brew", "category": "Coffee", "price": Decimal("220.00")},
    {"name": "Cortado", "category": "Coffee", "price": Decimal("160.00")},
    {"name": "Almond Croissant", "category": "Bakery",
     "price": Decimal("150.00")},
    {"name": "Masala Chai", "category": "Tea", "price": Decimal("70.00")},
]
application.read_menu_photo = lambda data, mime: [dict(row) for row in READING]


print("\n=== 0. Seeding a cafe ===")
seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Review Cafe", "full_name": "Ash Owner", "username": "ash",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
print("  seeded")

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
        time.sleep(0.15)
    raise AssertionError("timed out waiting for %s" % what)


def values(prefix):
    """Every review field whose name starts with prefix, in row order."""
    raw = browser.evaluate("""
        (function () {
            var out = [];
            var fields = document.querySelectorAll('input[name^="%s"]');
            for (var i = 0; i < fields.length; i++) out.push(fields[i].value);
            return out.join('|');
        }())
    """ % prefix)
    return (raw or "").split("|") if raw else []


def ticked():
    return browser.evaluate("""
        (function () {
            var n = 0;
            var ticks = document.querySelectorAll('.row-tick');
            for (var i = 0; i < ticks.length; i++) if (ticks[i].checked) n++;
            return n;
        }())
    """)


def click(selector):
    """A real mouse click - hit testing has mattered on this project."""
    box = browser.evaluate("""
        (function () {
            var r = document.querySelector('%s').getBoundingClientRect();
            return Math.round(r.left + r.width / 2) + ',' +
                   Math.round(r.top + r.height / 2);
        }())
    """ % selector)
    x, y = [int(value) for value in box.split(",")]
    for kind in ("mousePressed", "mouseReleased"):
        browser.call("Input.dispatchMouseEvent", type=kind, x=x, y=y,
                     button="left", clickCount=1)
    time.sleep(0.15)


def set_value(selector, value):
    browser.evaluate("""
        (function () {
            var el = document.querySelector('%s');
            el.value = '%s';
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }())
    """ % (selector, value))


def open_review():
    browser.call("Page.navigate", url=BASE + "/settings/menu-import")
    wait_for("!!document.querySelector('input[name=photo]')",
             "the upload form")
    browser.evaluate("""
        (function () {
            var form = document.querySelector('.form-panel form');
            var dt = new DataTransfer();
            dt.items.add(new File(['x'], 'menu.jpg', { type: 'image/jpeg' }));
            form.querySelector('[name=photo]').files = dt.files;
            form.submit();
        }())
    """)
    wait_for("!!document.getElementById('bulkApply')", "the review screen")


try:
    browser.call("Page.enable")
    browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000,
                 deviceScaleFactor=1, mobile=False)

    print("\n=== 1. Sign in and reach the review ===")
    browser.call("Page.navigate", url=BASE + "/login")
    wait_for("!!document.querySelector('form')", "the sign-in form")
    browser.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'ash';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
        }())
    """)
    wait_for("location.pathname !== '/login'", "the app")
    open_review()

    check("every read item has a row", len(values("name_")) == 5,
          "found %d rows" % len(values("name_")))
    check("stock starts at zero on every row",
          values("quantity_") == ["0"] * 5,
          "stock reads %s" % values("quantity_"))

    print("\n=== 2. Filling stock for every row at once ===")
    set_value("#bulkStock", "25")
    set_value("#bulkMin", "4")
    click("#bulkApply")

    check("every row's stock is filled in",
          values("quantity_") == ["25"] * 5,
          "stock reads %s" % values("quantity_"))
    check("and every row's minimum stock",
          values("minimum_stock_") == ["4"] * 5,
          "minimum reads %s" % values("minimum_stock_"))
    check("the button says it did something",
          browser.evaluate(
              "!document.getElementById('bulkDone').hidden"),
          "nothing confirmed the fill, so it looks like a dead button")
    check("filling stock did not disturb the prices",
          values("price_") == ["180.00", "220.00", "160.00", "150.00",
                               "70.00"],
          "prices read %s" % values("price_"))
    check("or the names",
          values("name_")[0] == "Flat White",
          "names read %s" % values("name_"))

    print("\n=== 3. A row can still be set on its own afterwards ===")
    set_value('input[name="quantity_0"]', "3")
    check("one row overrides the bulk value",
          values("quantity_") == ["3", "25", "25", "25", "25"],
          "stock reads %s" % values("quantity_"))

    print("\n=== 4. The tick-all box ===")
    check("every row starts ticked", ticked() == 5,
          "%d of 5 are ticked" % ticked())

    click("#checkAll")
    check("unticking the header unticks every row", ticked() == 0,
          "%d rows are still ticked" % ticked())

    click("#checkAll")
    check("and ticking it puts them all back", ticked() == 5,
          "%d of 5 are ticked" % ticked())

    print("\n=== 5. What is on screen is what gets saved ===")
    # Untick one row, then save, and check the menu matches the screen.
    click('.row-tick[name="include_2"]')
    check("one row is now unticked", ticked() == 4,
          "%d rows are ticked" % ticked())

    browser.evaluate(
        "document.querySelector('form[action*=apply]').submit()")
    wait_for("location.pathname === '/foods'", "Food Management")

    rows = browser.evaluate("""
        (function () {
            var out = [];
            var cells = document.querySelectorAll('.data-table .cell-strong');
            for (var i = 0; i < cells.length; i++) {
                out.push(cells[i].textContent.trim());
            }
            return out.join('|');
        }())
    """)
    saved = (rows or "").split("|") if rows else []

    check("the four ticked rows were saved", len(saved) == 4,
          "the menu has %d items: %s" % (len(saved), saved))
    check("the unticked row was not", "Cortado" not in saved,
          "an unticked row was saved: %s" % saved)

    stock = browser.evaluate("""
        (function () {
            location.href = '/inventory';
            return true;
        }())
    """)
    wait_for("location.pathname === '/inventory'", "Inventory")

    quantities = browser.evaluate("""
        (function () {
            var out = [];
            var rows = document.querySelectorAll('.data-table tbody tr');
            for (var i = 0; i < rows.length; i++) {
                var cells = rows[i].querySelectorAll('td');
                if (cells.length > 4) out.push(cells[4].textContent.trim());
            }
            return out.join('|');
        }())
    """)
    counts = (quantities or "").split("|") if quantities else []

    check("the stock filled in on the review reached Inventory",
          sorted(counts) == sorted(["3", "25", "25", "25"]),
          "Inventory reads %s" % counts)

finally:
    browser.close()

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
