"""
The customer's menu, in a browser: Veg and Non-veg, and the bell.

Veg means veg. The filter used to mark every non-veg dish hidden and
leave every one of them on screen - a dish is a flex row, and a page's
own display rule beats the hidden attribute unless something says
otherwise. Nothing on the server can see that; only a browser laying
the page out can. So this counts what is actually drawn.

The rest is how the page reads on a phone: which of the two is on has
to be plain at a glance, sections with nothing of that kind step aside,
the search box has room under the header, and the bell at the top right
lists what this phone has ordered today and says when it is ready.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without
one. No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/customer_menu_browser_test.py
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
os.environ["SECRET_KEY"] = "customer-menu-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5863
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
    "cafe_name": "Green Door", "full_name": "Menu Owner",
    "username": "greendoor", "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as sess:
        return sess.get("_csrf_token", "")


for name in ("Starters", "Drinks", "Meat"):
    seed.post("/categories/add", data={"category_name": name,
                                       "description": "",
                                       "_csrf_token": csrf()},
              follow_redirects=True)
page = seed.get("/foods/add").get_data(as_text=True)
CATEGORY = {n.strip(): i for i, n in re.findall(
    r'<option value="(\d+)">\s*([^<]+?)\s*</option>', page, re.S)}

DISHES = [("Paneer Tikka", "Starters", "veg"),
          ("Chicken Wings", "Starters", "nonveg"),
          ("Masala Chai", "Drinks", "veg"),
          ("Cold Coffee", "Drinks", "veg"),
          ("Mutton Curry", "Meat", "nonveg")]
VEG = sorted(n for n, _, d in DISHES if d == "veg")
NONVEG = sorted(n for n, _, d in DISHES if d == "nonveg")
EVERY = sorted(n for n, _, _ in DISHES)

for food, category, diet in DISHES:
    seed.post("/foods/add", data={
        "food_name": food, "category_id": CATEGORY[category], "price": "120",
        "quantity": "50", "minimum_stock": "1", "description": "",
        "diet": diet, "_csrf_token": csrf()}, follow_redirects=True)

with seed.session_transaction() as sess:
    CAFE = sess.get("cafe_id")
TOKEN = application.get_public_token(CAFE)
MENU = BASE + "/m/" + TOKEN

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
                return True
        except RuntimeError:
            pass
        time.sleep(0.12)
    raise AssertionError("timeout: " + what)


def open_page(url):
    b.call("Page.navigate", url=url)
    wait("document.readyState === 'complete'"
         " && !!document.getElementById('orderBell')", "the page at " + url)


def drawn():
    """The dishes a customer can actually see, by name."""
    return sorted(b.evaluate("""
        [].filter.call(document.querySelectorAll('.p-item'), function (item) {
            return item.getClientRects().length > 0
                && getComputedStyle(item).display !== 'none';
        }).map(function (item) {
            return item.querySelector('.p-item__name').textContent.trim();
        })
    """) or [])


def press(diet):
    b.evaluate("document.querySelector('#menuDiet [data-diet=%s]').click()" % diet)
    # The buttons ease into their new look over .15s; what is measured
    # is where they end up, not the middle of the change.
    time.sleep(0.3)


def tab_shown(name):
    return b.evaluate("""
        (function () {
            var tab = [].filter.call(document.querySelectorAll('.p-tab'), function (t) {
                return t.textContent.trim() === %r;
            })[0];
            return !!tab && tab.getClientRects().length > 0;
        }())
    """ % name)


def tap_tab(name):
    b.evaluate("""
        [].filter.call(document.querySelectorAll('.p-tab'), function (t) {
            return t.textContent.trim() === %r;
        })[0].click()
    """ % name)
    time.sleep(0.1)


def on_tab():
    return b.evaluate("document.querySelector('.p-tab.is-on').textContent.trim()")


def look(diet):
    """How the diet button is drawn: shadow, gradient, lift, colour."""
    return b.evaluate("""
        (function () {
            var s = getComputedStyle(document.querySelector('#menuDiet [data-diet=%s]'));
            return {shadow: s.boxShadow, image: s.backgroundImage,
                    transform: s.transform, colour: s.backgroundColor,
                    text: s.color};
        }())
    """ % diet)


def rgb(text):
    return [float(n) for n in re.findall(r"[\d.]+", text)[:3]]


def orders_placed():
    row = mysql_shim._DB.execute(
        "SELECT COUNT(*) FROM orders WHERE cafe_id = ?", (CAFE,)).fetchone()
    return row[0] if row else 0


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=780,
           deviceScaleFactor=1, mobile=True)
    open_page(MENU)

    # =================================================================
    print("\n=== 1. The buttons say how many of each there are ===")
    # =================================================================
    counts = b.evaluate("""
        ({veg: document.querySelector('[data-count=veg]').textContent,
          nonveg: document.querySelector('[data-count=nonveg]').textContent})
    """)
    check("Veg says 3", counts.get("veg") == "3", "it says %r" % counts.get("veg"))
    check("Non-veg says 2", counts.get("nonveg") == "2",
          "it says %r" % counts.get("nonveg"))
    check("with neither on, every dish is drawn", drawn() == EVERY,
          "drawn: %s" % drawn())

    # =================================================================
    print("\n=== 2. Veg shows veg, and only veg ===")
    # =================================================================
    press("veg")
    check("only the veg dishes are drawn", drawn() == VEG, "drawn: %s" % drawn())
    check("the Meat section steps aside, heading and all",
          b.evaluate("""
              [].every.call(document.querySelectorAll('.p-group'), function (g) {
                  var meat = g.querySelector('.p-group__name').textContent.trim() === 'Meat';
                  return !meat || g.getClientRects().length === 0;
              })
          """), "the Meat heading is still on screen")
    check("and so does its tab", not tab_shown("Meat"), "the Meat tab is still there")
    check("the others keep theirs", tab_shown("Starters") and tab_shown("Drinks"),
          "a section with veg dishes lost its tab")

    # =================================================================
    print("\n=== 3. The one that is on is plain to see ===")
    # =================================================================
    on, off = look("veg"), look("nonveg")
    check("it has a shadow under it", on["shadow"] not in ("", "none"),
          "box-shadow is %r" % on["shadow"])
    check("glass: lit from above", "gradient" in on["image"],
          "background-image is %r" % on["image"])
    check("lifted off the page", on["transform"] not in ("", "none"),
          "transform is %r" % on["transform"])
    red, green, _ = rgb(on["colour"])
    check("in green, for veg", green > red + 40,
          "background is %s" % on["colour"])
    check("with white writing", rgb(on["text"]) == [255.0, 255.0, 255.0],
          "the text is %s" % on["text"])
    check("the one that is off is flat", off["shadow"] in ("", "none")
          and "gradient" not in off["image"],
          "the off button is drawn %r / %r" % (off["shadow"], off["image"]))
    check("and the page says which is on",
          b.evaluate("document.querySelector('[data-diet=veg]').getAttribute('aria-pressed')") == "true",
          "aria-pressed is not true on Veg")

    # =================================================================
    print("\n=== 4. Non-veg shows non-veg, and only non-veg ===")
    # =================================================================
    press("nonveg")
    check("only the non-veg dishes are drawn", drawn() == NONVEG,
          "drawn: %s" % drawn())
    check("Veg is off again, one at a time",
          b.evaluate("document.querySelector('[data-diet=veg]').getAttribute('aria-pressed')") == "false",
          "both are pressed")
    red, green, _ = rgb(look("nonveg")["colour"])
    check("in red, for non-veg", red > green + 40,
          "background is %s" % look("nonveg")["colour"])
    check("Drinks has no non-veg, so its tab steps aside", not tab_shown("Drinks"),
          "the Drinks tab is still there")
    check("and Meat is back", tab_shown("Meat"), "the Meat tab is missing")

    # =================================================================
    print("\n=== 5. A section that empties sends the menu back to All ===")
    # =================================================================
    tap_tab("Meat")
    check("the Meat tab shows Mutton Curry", drawn() == ["Mutton Curry"],
          "drawn: %s" % drawn())
    press("veg")
    check("Veg on the Meat tab goes back to All", on_tab() == "All",
          "the tab on is %r" % on_tab())
    check("showing the veg dishes, not an empty page", drawn() == VEG,
          "drawn: %s" % drawn())
    check("with no 'nothing found' over them",
          b.evaluate("(function () { var n = document.getElementById('menuNone');"
                     " return !n || n.getClientRects().length === 0; }())"),
          "the empty message is up over a full menu")

    # =================================================================
    print("\n=== 6. The search and the filter work together ===")
    # =================================================================
    b.evaluate("""
        (function () {
            var s = document.getElementById('menuSearch');
            s.value = 'chicken';
            s.dispatchEvent(new Event('input', {bubbles: true}));
        }())
    """)
    time.sleep(0.1)
    check("veg and 'chicken' finds nothing", drawn() == [], "drawn: %s" % drawn())
    check("and says so",
          b.evaluate("document.getElementById('menuNone').getClientRects().length > 0"),
          "no message for an empty search")
    b.evaluate("document.getElementById('menuSearchClear').click()")
    press("veg")
    check("pressing Veg again shows everything", drawn() == EVERY,
          "drawn: %s" % drawn())
    check("and every tab is back",
          all(tab_shown(n) for n in ("Starters", "Drinks", "Meat")),
          "a tab is still missing")

    # =================================================================
    print("\n=== 7. Room under the header, and the bell at the top right ===")
    # =================================================================
    box = b.evaluate("""
        (function () {
            function r(el) { var x = el.getBoundingClientRect();
                return {top: x.top, bottom: x.bottom, left: x.left, right: x.right}; }
            return {head: r(document.querySelector('.p-head')),
                    name: r(document.querySelector('.p-head__name')),
                    bell: r(document.getElementById('orderBellButton')),
                    search: r(document.getElementById('menuSearch')),
                    wide: document.documentElement.scrollWidth};
        }())
    """)
    gap = box["search"]["top"] - box["head"]["bottom"]
    check("the search box sits clear of the header line", gap >= 16,
          "only %.0fpx between them" % gap)
    check("the bell is at the right-hand end of the header",
          abs(box["bell"]["right"] - box["head"]["right"]) <= 2,
          "bell ends at %.0f, header at %.0f" % (box["bell"]["right"], box["head"]["right"]))
    check("beside the cafe's name, not under it",
          box["bell"]["left"] > box["name"]["right"]
          and box["bell"]["top"] >= box["head"]["top"]
          and box["bell"]["bottom"] <= box["head"]["bottom"],
          "bell %s, name %s" % (box["bell"], box["name"]))
    check("and nothing is pushed off the side of a phone", box["wide"] <= 390,
          "the page is %dpx wide" % box["wide"])

    field = b.evaluate("""
        (function () {
            var s = getComputedStyle(document.getElementById('menuSearch'));
            return {shadow: s.boxShadow, image: s.backgroundImage};
        }())
    """)
    check("the search box is glass: lit from above",
          "gradient" in field["image"], "background-image is %r" % field["image"])
    check("with a shine along its top edge and a shadow under it",
          "inset" in field["shadow"] and field["shadow"].count("rgb") >= 3,
          "box-shadow is %r" % field["shadow"])

    # =================================================================
    print("\n=== 8. Nothing ordered yet ===")
    # =================================================================
    check("the bell has no number on it",
          b.evaluate("document.getElementById('orderBellCount').hidden"),
          "a count shows with no orders")
    b.evaluate("document.getElementById('orderBellButton').click()")
    check("tapping it opens the list",
          not b.evaluate("document.getElementById('orderBellPanel').hidden")
          and b.evaluate("document.getElementById('orderBellButton')"
                         ".getAttribute('aria-expanded')") == "true",
          "the panel did not open")
    check("which says nothing is ordered yet",
          "Nothing ordered" in b.evaluate(
              "document.getElementById('orderBellPanel').textContent"),
          "the empty panel reads %r" % b.evaluate(
              "document.getElementById('orderBellPanel').textContent"))
    b.call("Input.dispatchKeyEvent", type="keyDown", key="Escape",
           code="Escape", windowsVirtualKeyCode=27)
    b.call("Input.dispatchKeyEvent", type="keyUp", key="Escape",
           code="Escape", windowsVirtualKeyCode=27)
    time.sleep(0.1)
    check("Escape closes it",
          b.evaluate("document.getElementById('orderBellPanel').hidden"),
          "the panel is still open")

    # =================================================================
    print("\n=== 9. An order, and the bell keeps count ===")
    # =================================================================
    before = orders_placed()
    b.evaluate("document.querySelectorAll(\".p-step [data-step='1']\")[0].click()")
    b.evaluate("document.getElementById('sendBtn').click()")
    wait("!document.getElementById('holdWindow').hidden", "the window")
    b.evaluate("document.getElementById('holdNow').click()")
    wait("location.pathname.indexOf('/placed/') > -1", "the order page")
    check("the order went", orders_placed() == before + 1,
          "%d orders written" % (orders_placed() - before))

    open_page(MENU)
    check("back on the menu, the bell says 1",
          b.evaluate("!document.getElementById('orderBellCount').hidden"
                     " && document.getElementById('orderBellCount').textContent.trim() === '1'"),
          "the count reads %r" % b.evaluate(
              "document.getElementById('orderBellCount').textContent"))
    check("the numbers are under the bell, not in a strip above the search",
          b.evaluate("!document.querySelector('.p-mine')"),
          "the menu still opens with a strip of order numbers")
    rows = b.evaluate("""
        [].map.call(document.querySelectorAll('#orderBellPanel li'), function (li) {
            return {state: li.querySelector('.p-bell__state').textContent.trim(),
                    href: li.querySelector('a').getAttribute('href')};
        })
    """) or []
    check("and says so to a screen reader too",
          b.evaluate("document.getElementById('orderBellButton').getAttribute('aria-label')")
          == "Your orders today, 1 being made",
          "the label is %r" % b.evaluate(
              "document.getElementById('orderBellButton').getAttribute('aria-label')"))
    check("the list has the order, being made",
          len(rows) == 1 and rows[0]["state"] == "Being made",
          "the list reads %s" % rows)
    check("leading to its own page",
          len(rows) == 1 and "/m/%s/placed/" % TOKEN in rows[0]["href"],
          "the link is %s" % rows)
    b.evaluate("document.getElementById('orderBellButton').click()")
    b.evaluate("document.querySelector('.p-head__name').click()")
    time.sleep(0.1)
    check("a tap elsewhere closes the list",
          b.evaluate("document.getElementById('orderBellPanel').hidden"),
          "the panel stayed open")

    # =================================================================
    print("\n=== 10. The kitchen finishes it, and the bell says so ===")
    # =================================================================
    mysql_shim._DB.execute(
        "UPDATE orders SET order_status = 'Completed' WHERE cafe_id = ?", (CAFE,))
    mysql_shim._DB.commit()
    # The bell asks every fifteen seconds, and again whenever the page
    # comes back into view; the second is the one a test can wait for.
    b.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    wait("document.querySelector('#orderBellPanel li')"
         ".getAttribute('data-status') === 'Completed'", "the order to read ready")
    check("the order now reads Ready",
          b.evaluate("document.querySelector('#orderBellPanel .p-bell__state')"
                     ".textContent.trim()") == "Ready",
          "it reads %r" % b.evaluate(
              "document.querySelector('#orderBellPanel .p-bell__state').textContent"))
    check("the number comes off the bell",
          b.evaluate("document.getElementById('orderBellCount').hidden"),
          "the count still shows")
    check("and off its label",
          b.evaluate("document.getElementById('orderBellButton').getAttribute('aria-label')")
          == "Your orders today",
          "the label is %r" % b.evaluate(
              "document.getElementById('orderBellButton').getAttribute('aria-label')"))
    said = b.evaluate("document.getElementById('pToast').textContent")
    check("and the customer is told, in words", "is ready" in (said or ""),
          "the note reads %r" % said)
    check("with the bell rung",
          b.evaluate("document.getElementById('orderBell').classList.contains('is-ringing')"),
          "the bell was not marked")

    # =================================================================
    print("\n=== 11. The same bell on 'Your orders' ===")
    # =================================================================
    open_page(MENU + "/orders")
    check("the orders page has the bell, with the order ready",
          b.evaluate("document.querySelector('#orderBellPanel .p-bell__state')"
                     ".textContent.trim()") == "Ready",
          "the orders page bell reads %r" % b.evaluate(
              "(document.querySelector('#orderBellPanel') || {}).textContent"))
    check("and nothing still being made, so no number",
          b.evaluate("document.getElementById('orderBellCount').hidden"),
          "a count shows for a finished order")

    # =================================================================
    print("\n=== 12. The next phone at the same table, same QR code ===")
    # =================================================================
    # A phone is its cookies. Clearing them is a different phone that
    # has scanned the very same code.
    b.call("Network.enable")
    b.call("Network.clearBrowserCookies")
    open_page(MENU)
    check("a phone that has not ordered has nothing under its bell",
          b.evaluate("document.getElementById('orderBellCount').hidden"
                     " && !document.querySelector('#orderBellPanel li')"),
          "the other phone's order is under this one's bell")

    first_order = orders_placed()
    b.evaluate("document.querySelectorAll(\".p-step [data-step='1']\")[1].click()")
    b.evaluate("document.getElementById('sendBtn').click()")
    wait("!document.getElementById('holdWindow').hidden", "the window")
    b.evaluate("document.getElementById('holdNow').click()")
    wait("location.pathname.indexOf('/placed/') > -1", "the order page")
    theirs = b.evaluate("location.pathname.split('/').pop()")
    open_page(MENU)
    listed = b.evaluate("""
        [].map.call(document.querySelectorAll('#orderBellPanel li'), function (li) {
            return li.getAttribute('data-ref');
        })
    """) or []
    check("its own order, and only its own",
          orders_placed() == first_order + 1 and listed == [theirs],
          "this phone's bell lists %s (its order is %s)" % (listed, theirs))
    check("which is its own number, not the first phone's #1",
          b.evaluate("document.querySelector('#orderBellPanel .p-bell__no')"
                     ".textContent.trim()") == "#2",
          "the number reads %r" % b.evaluate(
              "document.querySelector('#orderBellPanel .p-bell__no').textContent"))

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
