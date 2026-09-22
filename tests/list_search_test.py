"""
Real-browser tests for the page header on a phone, and for searching a
list.

Two things reported together, both about pages with a lot on them.

The header first. At 390px it is a slim sticky bar, one row, no wrapping
- fine with one action. Category Management has two and a title, and the
three of them do not fit: the title was squeezed to 190px of the 248 it
wanted, with the Dashboard button sitting right up against it. Worse,
the ellipsis that was supposed to cover for that did nothing, because
the title is display:flex and text-overflow has no effect on a flex
container's own text. So the name was chopped mid-word - "Category
Manage" - with nothing to show that anything had been cut.

Then the search. Food Management and Inventory are the two pages where a
cafe with a full menu scrolls to find one thing.

Both of these are geometry and behaviour in a real browser, which is the
only place either can be checked. A test reading server HTML would see a
perfectly good header and a perfectly good search box.

Run with:  python tests/list_search_test.py
"""
import json
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
os.environ["SECRET_KEY"] = "list-search-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5977
BASE = "http://127.0.0.1:%d" % PORT

# A phone, not a small laptop. This is the width the report was about.
PHONE = 390

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
    "cafe_name": "List Cafe", "full_name": "Sam Owner", "username": "sam",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)

for name in ("Coffee", "Bakery"):
    seed.post("/categories/add", data={"category_name": name,
                                       "description": "",
                                       "_csrf_token": csrf(seed)},
              follow_redirects=True)

cats = re.findall(r'<option value="(\d+)">',
                  seed.get("/foods/add").get_data(as_text=True))
MENU = ["Flat White", "Cortado", "Espresso", "Almond Croissant",
        "Banana Bread", "Iced Latte"]
for index, food in enumerate(MENU):
    seed.post("/foods/add", data={
        "food_name": food, "category_id": cats[index % 2],
        "price": str(80 + index * 10), "quantity": "20",
        "minimum_stock": "5", "description": "",
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


def wait(expr, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if b.evaluate(expr):
                return True
        except RuntimeError:
            pass
        time.sleep(0.05)
    return False


def header_shape():
    """Where the title and the actions actually are, in pixels."""
    return json.loads(b.evaluate("""
        (function () {
            var h = document.querySelector('.page-header');
            var t = h.querySelector('h1');
            var a = h.querySelector('.page-header__actions') ||
                    h.querySelector('.btn');
            var tb = t.getBoundingClientRect();
            var ab = a ? a.getBoundingClientRect() : null;
            return JSON.stringify({
                viewport: window.innerWidth,
                titleLeft: Math.round(tb.left),
                titleRight: Math.round(tb.right),
                titleWanted: Math.round(t.scrollWidth),
                titleHas: Math.round(t.clientWidth),
                actionsLeft: ab ? Math.round(ab.left) : null,
                actionsRight: ab ? Math.round(ab.right) : null,
                clipped: !!ab && ab.right > window.innerWidth + 1,
                overlap: ab ? Math.round(tb.right - ab.left) : null
            });
        }())
    """))


def rows_showing():
    return json.loads(b.evaluate("""
        (function () {
            var rows = document.querySelectorAll('tbody tr');
            var out = [];
            for (var i = 0; i < rows.length; i++) {
                if (rows[i].offsetParent !== null) {
                    out.push((rows[i].textContent || '')
                             .replace(/\\s+/g, ' ').trim());
                }
            }
            return JSON.stringify(out);
        }())
    """))


def search_for(text):
    b.evaluate("""
        (function () {
            var box = document.querySelector('[data-table-search]');
            box.value = %s;
            box.dispatchEvent(new Event('input', {bubbles: true}));
        }())
    """ % json.dumps(text))
    time.sleep(0.3)


def counter():
    return b.evaluate(
        "document.querySelector('[data-table-count]').textContent.trim()")


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=PHONE, height=780,
           deviceScaleFactor=2, mobile=True)

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
    check("the app opens", wait("!!document.getElementById('page-view')"),
          "never got past the sign-in screen")

    # =================================================================
    print("\n=== 1. The header fits the phone it is on ===")
    # =================================================================

    for label, url in (("Category Management", "/categories"),
                       ("Food Management", "/foods"),
                       ("Inventory Management", "/inventory"),
                       ("Billing", "/billing")):
        b.call("Page.navigate", url=BASE + url)
        wait("!!document.querySelector('.page-header h1')")
        time.sleep(0.5)
        shape = header_shape()

        check("%s: nothing runs off the screen" % label,
              not shape["clipped"],
              "the actions end at %spx on a %spx screen"
              % (shape["actionsRight"], shape["viewport"]))

        check("%s: the title and the buttons do not touch" % label,
              shape["overlap"] is None or shape["overlap"] <= 0,
              "the title ends at %s and the buttons start at %s, so they "
              "overlap by %spx"
              % (shape["titleRight"], shape["actionsLeft"],
                 shape["overlap"]))

        # The point of the exercise: the page should be able to say its
        # own name. Truncation is survivable; losing it mid-word is what
        # looked broken.
        check("%s: the page can say its own name in full" % label,
              shape["titleWanted"] <= shape["titleHas"] + 1,
              "it wants %spx and has %spx, so it reads as “%s”"
              % (shape["titleWanted"], shape["titleHas"],
                 b.evaluate("document.querySelector('.page-header h1')"
                            ".textContent.trim()")))

    # If a title ever does have to be cut, it must look cut.
    check("a title that must be cut shows an ellipsis",
          b.evaluate("(function () {"
                     "  var s = getComputedStyle("
                     "    document.querySelector('.page-header h1'));"
                     "  return s.textOverflow === 'ellipsis'"
                     "      && s.display !== 'flex';"
                     "}())"),
          "text-overflow does nothing on a flex container, so the name "
          "would be chopped mid-word with no ellipsis to show for it")

    # =================================================================
    print("\n=== 2. What the header drops on a phone, and why ===")
    # =================================================================

    b.call("Page.navigate", url=BASE + "/categories")
    wait("!!document.querySelector('.page-header')")
    time.sleep(0.4)

    check("the shortcut the drawer already carries is not shown",
          b.evaluate("(function () {"
                     "  var e = document.querySelector('[data-drawer-echo]');"
                     "  return !e || e.offsetParent === null;"
                     "}())"),
          "it is still taking the room the page's own name needs")

    # Dropped only because it is a duplicate. If it were the only way to
    # reach the page, hiding it would be taking something away.
    check("and it is genuinely reachable from the drawer instead",
          b.evaluate("""
              (function () {
                  var links = document.querySelectorAll('.sidebar-nav a');
                  for (var i = 0; i < links.length; i++) {
                      if ((links[i].getAttribute('href') || '') === '/') {
                          return true;
                      }
                  }
                  return false;
              }())
          """),
          "the drawer has no Dashboard entry, so hiding the header's "
          "shortcut would leave no way there at all")

    check("the page's own action is still offered",
          b.evaluate("(function () {"
                     "  var b = document.querySelector("
                     "    '.page-header .btn--primary');"
                     "  return !!b && b.offsetParent !== null;"
                     "}())"),
          "Add Category went with it, which was not the idea")

    # =================================================================
    print("\n=== 3. Searching a list ===")
    # =================================================================

    for label, url, hits in (("Food Management", "/foods", "Almond Croissant"),
                             ("Inventory", "/inventory", "Almond Croissant")):
        print("\n  -- %s --" % label)
        b.call("Page.navigate", url=BASE + url)
        wait("!!document.querySelector('[data-table-search]')")
        time.sleep(0.6)

        check("%s: there is a search box" % label,
              b.evaluate("!!document.querySelector('[data-table-search]')"))

        check("%s: everything is listed to begin with" % label,
              len(rows_showing()) == len(MENU),
              "%d rows of %d" % (len(rows_showing()), len(MENU)))

        # A fresh load, not a swap. The box renders above its own table,
        # so a script that ran at parse time would find no rows yet - and
        # the page would work perfectly the moment you navigated away and
        # back, which is a miserable way to find a bug.
        search_for("cro")
        showing = rows_showing()
        check("%s: searching a name narrows the list" % label,
              len(showing) == 1 and hits in showing[0],
              "it shows %s" % showing)

        check("%s: and says how many of how many" % label,
              counter().startswith("1 of %d" % len(MENU)),
              "the count reads %r" % counter())

        search_for("bakery")
        check("%s: a category matches too" % label,
              len(rows_showing()) == 3,
              "searching a category found %d rows" % len(rows_showing()))

        search_for("zzz")
        check("%s: nothing matching says so" % label,
              len(rows_showing()) == 0
              and b.evaluate("!document.querySelector("
                             "'[data-table-empty]').hidden"),
              "%d rows still showing, and the message is %s"
              % (len(rows_showing()),
                 "hidden" if b.evaluate("document.querySelector("
                                        "'[data-table-empty]').hidden")
                 else "shown"))

        check("%s: and the empty table goes with it" % label,
              b.evaluate("document.querySelector('.table-wrap')"
                         ".classList.contains('is-filtered-out')"),
              "the column headings are left sitting above nothing")

        b.evaluate("document.querySelector('[data-table-reset]').click()")
        time.sleep(0.3)
        check("%s: and it can all be put back" % label,
              len(rows_showing()) == len(MENU)
              and b.evaluate("document.querySelector("
                             "'[data-table-search]').value") == "",
              "%d rows after reset" % len(rows_showing()))

    # =================================================================
    print("\n=== 4. And it still works after a page swap ===")
    # =================================================================
    # instant.js swaps the markup without reloading, so a page's own
    # scripts run again against new elements. A search wired up once, to
    # elements that have since been replaced, would silently do nothing.

    b.evaluate("""
        (function () {
            var links = document.querySelectorAll('.sidebar-nav a');
            for (var i = 0; i < links.length; i++) {
                if ((links[i].getAttribute('href') || '')
                        .indexOf('foods') > -1) {
                    links[i].click();
                    return;
                }
            }
        }())
    """)
    wait("location.pathname.indexOf('foods') > -1")
    wait("!!document.querySelector('[data-table-search]')")
    time.sleep(0.7)

    search_for("latte")
    showing = rows_showing()
    check("the search still filters after switching pages",
          len(showing) == 1 and "Iced Latte" in showing[0],
          "it shows %s - the box was wired to elements that have since "
          "been swapped out" % showing)

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
