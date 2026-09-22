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

# Orders, so the billing checks further down have rows to measure.
# Without these the table is empty and half of them pass on nothing.
_ordered = re.findall(r'id="quantity_(\d+)"',
                      seed.get("/orders/add").get_data(as_text=True))
for _each in (1, 2, 1):
    seed.post("/orders/add",
              data={"quantity_%s" % _ordered[0]: str(_each),
                    "_csrf_token": csrf(seed)}, follow_redirects=True)
# Orders again, so the kitchen board has tickets to measure.
for _each in (1, 2):
    seed.post("/orders/add",
              data={"quantity_%s" % _ordered[0]: str(_each),
                    "_csrf_token": csrf(seed)}, follow_redirects=True)

seed.get("/billing")          # raises the bills

# The Order Status button belongs to the people on the till, not the
# owner, so there has to be one of those to sign in as.
seed.post("/users/add", data={
    "full_name": "Cash Ier", "username": "cash", "password": "password123",
    "confirm_password": "password123", "role": "staff",
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


    # =================================================================
    print("\n=== 5. The billing table, at the sizes a till is used at ===")
    # =================================================================
    # The Paid button is the one control on this page that has to be
    # reachable. It was width:100% inside a column the table sized from
    # whatever was left over, so it stretched to about 250px beside a
    # 34px print button; and a phone held sideways landed in a band that
    # forced the table wider than the screen, putting Paid off the edge.

    for label, width, height in (("a desktop", 1280, 860),
                                 ("a phone held sideways", 844, 390),
                                 ("a phone upright", 390, 780)):
        b.call("Emulation.setDeviceMetricsOverride", width=width,
               height=height, deviceScaleFactor=1,
               mobile=(width < 1000))
        b.call("Page.navigate", url=BASE + "/billing")
        wait("!!document.querySelector('.billing-table')")
        time.sleep(0.9)

        facts = json.loads(b.evaluate("""
            (function () {
                var doc = document.documentElement;
                var panel = document.querySelector('.billing-table-panel');
                var table = document.querySelector('.billing-table');
                var firstPaid = document.querySelector(
                    '.payment-status-toggle');
                var heads = document.querySelectorAll('.billing-table th');
                var lastHead = heads[heads.length - 1];
                var buttons = document.querySelectorAll(
                    '.payment-status-toggle');
                var widths = [];
                var spans = [];
                var reachable = true;
                for (var i = 0; i < buttons.length; i++) {
                    var box = buttons[i].getBoundingClientRect();
                    widths.push(Math.round(box.width));
                    spans.push(Math.round(box.left) + '..'
                               + Math.round(box.right));
                    if (box.right > window.innerWidth + 1 || box.left < -1) {
                        reachable = false;
                    }
                }
                return JSON.stringify({
                    sideways: doc.scrollWidth - doc.clientWidth,
                    panelSideways: panel
                        ? panel.scrollWidth - panel.clientWidth : 0,
                    cards: table
                        ? getComputedStyle(table).display !== 'table' : null,
                    lastColumnRight: lastHead
                        ? Math.round(
                            lastHead.getBoundingClientRect().right) : null,
                    firstPaidBottom: firstPaid
                        ? Math.round(
                            firstPaid.getBoundingClientRect().bottom) : null,
                    fold: window.innerHeight,
                    widths: widths,
                    spans: spans,
                    width: window.innerWidth,
                    reachable: reachable
                });
            }())
        """))

        check("%s: the page does not scroll sideways" % label,
              facts["sideways"] <= 1,
              "it overflows by %spx, so the Paid button is off the edge"
              % facts["sideways"])

        # Asserted first, because "every button is on screen" and
        # "they are all the same size" are both true of no buttons at
        # all - and this suite once ran all three against an empty
        # table and reported two passes.
        check("%s: there are bills to look at" % label,
              len(facts["widths"]) >= 3,
              "only %d Paid buttons on the page, so the checks below "
              "would pass without looking at anything"
              % len(facts["widths"]))

        check("%s: every Paid button is on the screen" % label,
              facts["widths"] and facts["reachable"],
              "buttons at %s in a %spx viewport"
              % (facts["spans"], facts["width"]))

        check("%s: and they are all the same size" % label,
              facts["widths"] and len(set(facts["widths"])) == 1,
              "the buttons measure %s - a column of different-sized "
              "buttons is what made this look unfinished"
              % facts["widths"])

        # Nothing scrolls sideways, including the table's own panel -
        # which is its own scroller, so the page can look settled while
        # half the columns are off the end of it.
        check("%s: the table does not scroll sideways either" % label,
              facts["panelSideways"] <= 1,
              "the panel holding it scrolls by %spx, so the last "
              "columns are off the end of the screen"
              % facts["panelSideways"])

        check("%s: the last column is on the screen" % label,
              facts["lastColumnRight"] is None
              or facts["lastColumnRight"] <= facts["width"] + 1,
              "the last column ends at %spx on a %spx screen"
              % (facts["lastColumnRight"], facts["width"]))

    # =================================================================
    print("\n=== 6. A rotated phone gets the wide view, and it fits ===")
    # =================================================================
    # Rotating a phone is what somebody does *to* see more at once. An
    # earlier pass sent this size to the card layout, which is the
    # narrow view - the opposite of what the gesture asked for. The
    # table stays; what changes is that everything on it is sized for
    # the room, and everything above it gives up height so the first
    # Paid button is on screen without scrolling down to it.

    b.call("Emulation.setDeviceMetricsOverride", width=844, height=390,
           deviceScaleFactor=1, mobile=True)
    b.call("Page.navigate", url=BASE + "/billing")
    wait("!!document.querySelector('.billing-table')")
    time.sleep(1.0)

    rotated = json.loads(b.evaluate("""
        (function () {
            var table = document.querySelector('.billing-table');
            var paid = document.querySelector('.payment-status-toggle');
            var heads = document.querySelectorAll('.billing-table th');
            var last = heads[heads.length - 1];
            return JSON.stringify({
                display: getComputedStyle(table).display,
                columns: heads.length,
                lastRight: last
                    ? Math.round(last.getBoundingClientRect().right) : null,
                paidBottom: paid
                    ? Math.round(paid.getBoundingClientRect().bottom) : null,
                fold: window.innerHeight,
                width: window.innerWidth,
                shortDate: (function () {
                    var s = document.querySelector('.date-short');
                    return s && getComputedStyle(s).display !== 'none';
                }())
            });
        }())
    """))

    check("it is still a table, not a stack of cards",
          rotated["display"] == "table",
          "it renders as %r - rotating the phone gave back the narrow "
          "view it was rotated to escape" % rotated["display"])

    check("and every column of it is on the screen",
          rotated["lastRight"] is not None
          and rotated["lastRight"] <= rotated["width"] + 1,
          "the last of %d columns ends at %spx on a %spx screen"
          % (rotated["columns"], rotated["lastRight"], rotated["width"]))

    # The other half of the same request. A button that is on screen
    # only after scrolling down to it is not on screen.
    check("the first Paid button is reachable without scrolling",
          rotated["paidBottom"] is not None
          and rotated["paidBottom"] <= rotated["fold"],
          "it ends %spx down a %spx screen, so the till has to scroll "
          "to take a payment"
          % (rotated["paidBottom"], rotated["fold"]))

    check("and the date drops the year to make the room",
          rotated["shortDate"],
          "it still prints the year, which is the easiest thing in the "
          "row to do without")

    b.call("Emulation.setDeviceMetricsOverride", width=1280, height=860,
           deviceScaleFactor=1, mobile=False)

    # The box and the button are used one after the other, so they read
    # as a line. They were ten pixels apart vertically.
    b.call("Emulation.setDeviceMetricsOverride", width=1280, height=860,
           deviceScaleFactor=1, mobile=False)
    b.call("Page.navigate", url=BASE + "/billing")
    wait("!!document.querySelector('.cash-received-input')")
    time.sleep(0.7)

    offset = b.evaluate("""
        (function () {
            var box = document.querySelector('.cash-received-input');
            var paid = document.querySelector('.payment-status-toggle');
            if (!box || !paid) return 999;
            var a = box.getBoundingClientRect();
            var c = paid.getBoundingClientRect();
            return Math.round(Math.abs((a.top + a.height / 2)
                                       - (c.top + c.height / 2)));
        }())
    """)
    check("the amount box sits on the same line as the Paid button",
          offset <= 2,
          "they are %spx apart, so the row reads as two half-rows"
          % offset)

    # The line price was the row's own Total again, two columns along.
    check("the item list does not repeat the total",
          b.evaluate("""
              (function () {
                  var cell = document.querySelector('.bill-line');
                  return cell ? cell.textContent.indexOf('₹') === -1
                              : false;
              }())
          """),
          "each line still carries a price, which for a one-line bill is "
          "the Total printed twice")

    # And the floating Order Status button, which sits over every page.
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=780,
           deviceScaleFactor=1, mobile=True)
    # The owner gets it too. It was staff-only on the reasoning that an
    # admin has the Dashboard for this - but the Dashboard is a page you
    # have to go to, and this is a button on whichever page you are
    # already on.
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=780,
           deviceScaleFactor=1, mobile=True)
    b.call("Page.navigate", url=BASE + "/orders/add")
    check("the owner is offered the order status button as well",
          wait("!!document.getElementById('orderStatusFab')"),
          "an owner working the counter cannot see what is outstanding "
          "without leaving the page they are on")

    check("and it opens for them",
          b.evaluate("""
              (function () {
                  document.getElementById('orderStatusFab').click();
                  return true;
              }())
          """) and wait("document.getElementById('orderStatusOverlay')"
                        ".classList.contains('open')"),
          "the button is there but does nothing")

    b.evaluate("document.getElementById('orderStatusClose').click()")
    time.sleep(0.4)

    # Signed in again as the cashier, because the button is theirs too.
    b.call("Page.navigate", url=BASE + "/logout")
    wait("!!document.querySelector('form [name=username]')")
    b.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'cash';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
        }())
    """)
    wait("!!document.getElementById('page-view')")

    b.call("Page.navigate", url=BASE + "/billing")
    check("the order status button is there for a cashier",
          wait("!!document.getElementById('orderStatusFab')"),
          "it never appeared, so there is nothing below to measure")
    time.sleep(1.0)

    fab = json.loads(b.evaluate("""
        (function () {
            var el = document.getElementById('orderStatusFab');
            if (!el) return JSON.stringify({missing: true});
            var label = el.querySelector('.order-status-fab__label');
            var box = el.getBoundingClientRect();
            return JSON.stringify({
                width: Math.round(box.width),
                share: Math.round(box.width / window.innerWidth * 100),
                labelShown: label ?
                    getComputedStyle(label).display !== 'none' : false,
                iconShown: !!el.querySelector('i')
            });
        }())
    """))

    check("the order status button is small on a small screen",
          fab["share"] <= 20,
          "it takes %d%% of the width (%spx), permanently, over whatever "
          "is underneath it" % (fab["share"], fab["width"]))

    check("but it still shows what it is",
          fab["iconShown"] and not fab["labelShown"],
          "it kept the words and lost nothing, or lost the icon too: %s"
          % fab)


    # =================================================================
    print("\n=== 7. The bar does not flap at the foot of a page ===")
    # =================================================================
    # Reported as the page shaking continuously once you reach the
    # bottom. Measured: twenty-four small nudges there produced
    # twenty-three flips of the top bar, each one a 220ms slide. The
    # document height never moved - what was shaking was the bar.

    def nudge(times, delta, width, height):
        for step in range(times):
            b.call("Input.dispatchMouseEvent", type="mouseWheel",
                   x=width // 2, y=height // 2, deltaX=0,
                   deltaY=(delta if step % 2 == 0 else -delta))
            time.sleep(0.07)
        time.sleep(0.5)

    def bar_hidden():
        return b.evaluate(
            "document.documentElement.classList.contains('bar-away')")

    b.call("Emulation.setDeviceMetricsOverride", width=390, height=780,
           deviceScaleFactor=1, mobile=True)
    b.call("Page.navigate", url=BASE + "/foods")
    wait("!!document.querySelector('.page-header')")
    time.sleep(1.0)

    b.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(0.7)

    b.evaluate("""
        (function () {
            window.__flips = 0;
            var root = document.documentElement;
            var was = root.classList.contains('bar-away');
            window.__watch = setInterval(function () {
                var now = root.classList.contains('bar-away');
                if (now !== was) { window.__flips++; was = now; }
            }, 16);
        }())
    """)

    nudge(20, 9, 390, 780)
    flips = b.evaluate("clearInterval(window.__watch); window.__flips")

    check("small movements at the bottom do not move the bar",
          flips == 0,
          "the bar changed state %d times while somebody scrolled at the "
          "foot of the page - each one a slide, which is the shaking "
          "that was reported" % flips)

    # And the guard against fixing it by switching the feature off.
    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.6)
    check("the bar still starts visible", not bar_hidden())

    for _ in range(8):
        b.call("Input.dispatchMouseEvent", type="mouseWheel",
               x=195, y=390, deltaX=0, deltaY=120)
        time.sleep(0.05)
    time.sleep(0.6)
    check("a real scroll down still hides it", bar_hidden(),
          "the shaking was cured by stopping the bar working at all")

    for _ in range(8):
        b.call("Input.dispatchMouseEvent", type="mouseWheel",
               x=195, y=390, deltaX=0, deltaY=-120)
        time.sleep(0.05)
    time.sleep(0.6)
    check("and scrolling back up brings it straight back",
          not bar_hidden(), "it stayed away")

    # =================================================================
    print("\n=== 8. The kitchen board, on a screen held sideways ===")
    # =================================================================
    # The board is drawn for a screen with room. At 390px tall barely a
    # row of tickets was above the fold, and the rest was a scroll away
    # on the one screen in the building nobody is holding.

    b.call("Emulation.setDeviceMetricsOverride", width=844, height=390,
           deviceScaleFactor=1, mobile=True)
    b.call("Page.navigate", url=BASE + "/kitchen")
    if wait("!!document.querySelector('.kitchen-ticket')", 12):
        board = json.loads(b.evaluate("""
            (function () {
                var t = document.querySelectorAll('.kitchen-ticket');
                var lefts = {}, visible = 0;
                for (var i = 0; i < t.length; i++) {
                    var r = t[i].getBoundingClientRect();
                    lefts[Math.round(r.left)] = 1;
                    if (r.top >= 0 && r.bottom <= window.innerHeight + 1) {
                        visible++;
                    }
                }
                var tick = document.querySelector('.kitchen-tick');
                var line = document.querySelector('.kitchen-line');
                return JSON.stringify({
                    across: Object.keys(lefts).length,
                    visible: visible,
                    height: Math.round(
                        t[0].getBoundingClientRect().height),
                    tick: tick
                        ? Math.round(tick.getBoundingClientRect().width) : 0,
                    line: parseFloat(getComputedStyle(line).fontSize),
                    fab: (function () {
                        var el = document.getElementById('orderStatusFab');
                        return !!el
                            && getComputedStyle(el).display !== 'none';
                    }()),
                    fabOverTicket: (function () {
                        var el = document.getElementById('orderStatusFab');
                        if (!el || getComputedStyle(el).display === 'none') {
                            return false;
                        }
                        var f = el.getBoundingClientRect();
                        for (var k = 0; k < t.length; k++) {
                            var q = t[k].getBoundingClientRect();
                            if (q.left < f.right && q.right > f.left
                                    && q.top < f.bottom
                                    && q.bottom > f.top) {
                                return true;
                            }
                        }
                        return false;
                    }()),
                    sideways: document.documentElement.scrollWidth
                              - document.documentElement.clientWidth
                });
            }())
        """))

        check("several tickets fit across", board["across"] >= 3,
              "only %d column(s) on an 844px screen" % board["across"])

        check("and more than one is fully on screen",
              board["visible"] >= 3,
              "%d tickets are wholly visible in 390px of height"
              % board["visible"])

        check("nothing scrolls sideways", board["sideways"] <= 1,
              "the board overflows by %spx" % board["sideways"])

        # The one thing that must not be traded for space. It is pressed
        # with a thumb, in a kitchen, by somebody holding a pan.
        check("the tick is still the size of a thumb",
              board["tick"] >= 28,
              "it shrank to %spx to make room, which is the one control "
              "on this page" % board["tick"])

        check("and a dish is still readable", board["line"] >= 13,
              "dish names are %spx" % board["line"])

        # The button is back on this screen, and on every other, but it
        # lives up in the top bar in landscape now - so what matters is
        # that it is not sitting on a ticket's order number, which is
        # what took it off this page in the first place.
        check("the order status button is offered here too",
              board["fab"],
              "it is missing on the kitchen screen")

        check("and it is not sitting on a ticket",
              not board["fabOverTicket"],
              "it overlaps a ticket, which is what took it off this "
              "page the first time")
    else:
        check("the kitchen board has tickets to measure", False,
              "no tickets rendered, so nothing above was checked")


    # =================================================================
    print("\n=== 9. The order summary, on a screen held sideways ===")
    # =================================================================
    # The sheet was a plain block with a scrolling list inside it. The
    # list took the height it wanted, pushed the footer past the
    # sheet's own bottom edge, and overflow:hidden cut it off - so on a
    # phone held sideways Continue Selecting and Create Order were
    # measured 137px below the sheet, and simply were not there.

    b.call("Emulation.setDeviceMetricsOverride", width=844, height=390,
           deviceScaleFactor=1, mobile=True)
    b.call("Page.navigate", url=BASE + "/orders/add")
    wait("!!document.querySelector('.food-card')")
    time.sleep(1.0)

    b.evaluate("""
        (function () {
            var picked = 0;
            document.querySelectorAll('input[id^=quantity_]')
                .forEach(function (box) {
                    if (picked < 3) {
                        box.value = '2';
                        box.dispatchEvent(
                            new Event('input', {bubbles: true}));
                        box.dispatchEvent(
                            new Event('change', {bubbles: true}));
                        picked++;
                    }
                });
        }())
    """)
    time.sleep(0.6)
    b.evaluate("document.getElementById('orderSummaryTrigger').click()")
    time.sleep(1.0)

    sheet = json.loads(b.evaluate("""
        (function () {
            var acts = document.querySelector('.order-summary-actions');
            var head = document.querySelector(
                '.order-summary-popup__header');
            var bar = document.querySelector('.topbar');
            var btns = acts ? acts.querySelectorAll('.btn') : [];
            var seen = [];
            for (var i = 0; i < btns.length; i++) {
                var r = btns[i].getBoundingClientRect();
                seen.push({
                    label: btns[i].textContent.replace(/\s+/g, ' ').trim(),
                    onScreen: r.bottom <= window.innerHeight + 1
                              && r.top >= -1,
                    left: Math.round(r.left)
                });
            }
            var hr = head ? head.getBoundingClientRect() : null;
            var br = bar ? bar.getBoundingClientRect() : null;
            return JSON.stringify({
                buttons: seen,
                headerClear: (hr && br) ? hr.top >= br.bottom - 1 : null,
                justify: acts
                    ? getComputedStyle(acts).justifyContent : null
            });
        }())
    """))

    check("the summary offers both choices",
          len(sheet["buttons"]) == 2,
          "it shows %s" % [x["label"] for x in sheet["buttons"]])

    for choice in sheet["buttons"]:
        check("%r is on the screen" % choice["label"],
              choice["onScreen"],
              "it is off the bottom of the sheet, which is where both "
              "of them were")

    check("and the heading is not behind the top bar",
          sheet["headerClear"],
          "the sheet slid its own title up under the bar")

    # Nearest the hand holding the device, rather than out at the far
    # corner of a screen that is mostly width.
    check("the choices sit at the near edge",
          sheet["justify"] == "flex-start",
          "they are aligned %r" % sheet["justify"])

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
