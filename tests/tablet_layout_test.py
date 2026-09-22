"""
Real-browser test for tablet layouts.

A tablet held upright was narrow enough to get the drawer, but rotating it
crossed back over the old 880px line and handed the user the laptop layout
mid-session. Width alone cannot tell a tablet from a small laptop, so the
stylesheet also asks whether the device is touch-operated.

This walks the common tablet sizes in both orientations and checks each one
gets the drawer, keeps the page full width, and offers targets a finger can
actually hit - while a same-width laptop with a mouse is left alone.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without one.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/tablet_layout_test.py
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
os.environ["SECRET_KEY"] = "tablet-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5891
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
    "cafe_name": "Tablet Cafe", "full_name": "T Owner", "username": "tab",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as s:
        return s.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Coffee",
                                   "description": "", "_csrf_token": csrf()},
          follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     seed.get("/foods/add").get_data(as_text=True)).group(1)
for food in ["Latte", "Mocha", "Espresso", "Cortado"]:
    seed.post("/foods/add", data={
        "food_name": food, "category_id": category, "price": "100",
        "quantity": "50", "description": "", "_csrf_token": csrf()},
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


def viewport(width, height, touch):
    b.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
           deviceScaleFactor=2 if touch else 1, mobile=touch)
    # The metrics override alone leaves `(pointer: coarse)` false - it only
    # changes the size. This is the command that makes the browser report a
    # touch screen to the stylesheet, which is the whole basis of the tablet
    # rules being tested here.
    b.call("Emulation.setTouchEmulationEnabled", enabled=touch,
           maxTouchPoints=5 if touch else 1)
    time.sleep(0.5)


def layout():
    return json.loads(b.evaluate("""
        (function () {
            var toggle = document.getElementById('navToggle');
            var side = document.getElementById('appSidebar');
            var main = document.querySelector('.main');
            var link = document.querySelector('.sidebar-nav .nav-link');
            return JSON.stringify({
                hamburger: getComputedStyle(toggle).display !== 'none',
                sidebarVisible: getComputedStyle(side).visibility === 'visible',
                mainLeft: Math.round(main.getBoundingClientRect().left),
                mainWidth: Math.round(main.getBoundingClientRect().width),
                navLinkHeight: Math.round(link.getBoundingClientRect().height),
                viewport: innerWidth,
                coarse: matchMedia('(pointer: coarse)').matches
            });
        })()
    """))


TABLETS = [
    ("iPad mini    portrait ", 768, 1024),
    ("iPad mini    landscape", 1024, 768),
    ("Galaxy Tab S portrait ", 800, 1280),
    ("Galaxy Tab S landscape", 1280, 800),
    ("iPad 10.9    portrait ", 820, 1180),
    ("iPad 10.9    landscape", 1180, 820),
    ("iPad Pro 11  landscape", 1194, 834),
    ("iPad Pro 12.9 landscape", 1366, 1024),
]

try:
    b.call("Page.enable")
    b.call("Page.navigate", url=BASE + "/login")
    wait("document.readyState==='complete' && !!document.querySelector('form')", "login")
    b.evaluate("""(function(){var f=document.querySelector('form');
        f.querySelector('[name=username]').value='tab';
        f.querySelector('[name=password]').value='password123';
        f.submit(); return 1;})()""")
    wait("!!document.getElementById('page-view')", "shell")

    print("\n=== Every tablet gets the drawer, in either orientation ===")
    for label, width, height in TABLETS:
        viewport(width, height, touch=True)
        state = layout()

        check("%s (%4dpx) shows the hamburger" % (label, width),
              state["hamburger"],
              "got the laptop sidebar instead: %s" % state)
        check("%s (%4dpx) gives the page full width" % (label, width),
              state["mainLeft"] < 5 and state["mainWidth"] >= width - 5,
              "page starts at %(mainLeft)spx and is %(mainWidth)spx wide" % state)
        check("%s (%4dpx) keeps the sidebar off-canvas" % (label, width),
              not state["sidebarVisible"],
              "the sidebar is on screen as well as the hamburger")

    print("\n=== Rotating does not change the layout mid-session ===")
    viewport(820, 1180, touch=True)
    upright = layout()
    viewport(1180, 820, touch=True)
    rotated = layout()
    check("an iPad 10.9 looks the same upright and rotated",
          upright["hamburger"] == rotated["hamburger"] is True,
          "upright hamburger=%s rotated=%s"
          % (upright["hamburger"], rotated["hamburger"]))

    print("\n=== Touch targets are big enough to hit ===")
    viewport(1194, 834, touch=True)
    state = layout()
    check("the media query sees a coarse pointer", state["coarse"],
          "touch emulation is not reaching the stylesheet")
    check("sidebar links are at least 44px tall",
          state["navLinkHeight"] >= 44,
          "nav links are %(navLinkHeight)spx" % state)

    sizes = json.loads(b.evaluate("""
        (function () {
            function h(sel) {
                var el = document.querySelector(sel);
                return el ? Math.round(el.getBoundingClientRect().height) : 0;
            }
            return JSON.stringify({
                plus: h('.quantity-plus'),
                search: h('.menu-search__input'),
                summary: h('#orderSummaryTrigger')
            });
        })()
    """)) if b.evaluate("location.pathname === '/orders/add'") else None

    b.call("Page.navigate", url=BASE + "/orders/add")
    wait("!!document.querySelector('.quantity-plus')", "the menu")
    time.sleep(0.5)
    sizes = json.loads(b.evaluate("""
        (function () {
            function h(sel) {
                var el = document.querySelector(sel);
                return el ? Math.round(el.getBoundingClientRect().height) : 0;
            }
            return JSON.stringify({
                plus: h('.quantity-plus'),
                search: h('.menu-search__input'),
                summary: h('#orderSummaryTrigger')
            });
        })()
    """))
    check("the quantity buttons are finger-sized",
          sizes["plus"] >= 40, "the + button is %(plus)spx tall" % sizes)
    check("the search field is finger-sized",
          sizes["search"] >= 44, "the search box is %(search)spx tall" % sizes)

    print("\n=== The menu packs in without shrinking the type ===")

    def menu_metrics():
        return json.loads(b.evaluate("""
            (function () {
                var cards = [].slice.call(
                    document.querySelectorAll('.food-card:not(.food-card--mirror)'));
                if (!cards.length) return JSON.stringify({});
                var first = cards[0].getBoundingClientRect();
                var rows = {};
                cards.forEach(function (c) {
                    rows[Math.round(c.getBoundingClientRect().top)] = 1;
                });
                return JSON.stringify({
                    height: Math.round(first.height),
                    perRow: Math.round(cards.length / Object.keys(rows).length),
                    name: parseFloat(getComputedStyle(
                        cards[0].querySelector('.food-card-name')).fontSize),
                    price: parseFloat(getComputedStyle(
                        cards[0].querySelector('.food-card-price')).fontSize),
                    hasStock: !!cards[0].querySelector('.food-card-stock'),
                    hasCategory: !!cards[0].querySelector('.food-card-category'),
                    hasControl: !!cards[0].querySelector('.quantity-plus')
                });
            })()
        """))

    for label, width, height in [("tablet landscape", 1194, 834),
                                 ("tablet portrait ", 820, 1180),
                                 ("phone           ", 390, 844)]:
        viewport(width, height, touch=True)
        b.call("Page.navigate", url=BASE + "/orders/add")
        wait("!!document.querySelector('.food-card')", "the menu")
        time.sleep(0.7)
        m = menu_metrics()

        check("%s fits at least two cards per row" % label,
              m["perRow"] >= 2, "%(perRow)s per row" % m)
        check("%s keeps the card short enough to stack" % label,
              m["height"] <= 275, "cards are %(height)spx tall" % m)
        # Denser must not mean unreadable: this is the floor for a name read
        # at arm's length across a counter.
        check("%s keeps the name readable" % label,
              m["name"] >= 14, "name font is %(name)spx" % m)
        check("%s keeps the price readable" % label,
              m["price"] >= 16, "price font is %(price)spx" % m)
        check("%s still shows everything the card carried" % label,
              m["hasStock"] and m["hasCategory"] and m["hasControl"],
              "something was dropped to save space: %s" % m)
    print("\n=== A laptop with a mouse is left alone ===")
    # Same width as an iPad Pro 12.9 in landscape, so this is precisely the
    # case width alone cannot separate.
    viewport(1366, 768, touch=False)
    state = layout()
    check("a 1366px laptop keeps its sidebar",
          not state["hamburger"] and state["sidebarVisible"],
          "the laptop layout was replaced with the drawer: %s" % state)
    check("the stylesheet sees a fine pointer there", not state["coarse"],
          "touch emulation did not reset, so the next check proves nothing")

    # Measured on a control whose touch and mouse sizes actually differ -
    # the sidebar links are ~46px tall either way, so they prove nothing.
    desktop_plus = int(b.evaluate("""
        Math.round(document.querySelector('.quantity-plus')
                           .getBoundingClientRect().height)
    """))
    check("the quantity buttons are back to their compact size",
          desktop_plus < 42,
          "the + button is %dpx on a mouse-driven laptop, so touch sizing "
          "leaked to the desktop" % desktop_plus)

    viewport(1440, 900, touch=False)
    state = layout()
    check("a full desktop is unchanged",
          not state["hamburger"] and state["sidebarVisible"] and state["mainLeft"] > 100,
          "%s" % state)


    # =================================================================
    print("\n=== The top of the app does not move on a big screen ===")
    # =================================================================
    # Both bars are sticky, so on a wide screen they end up flush with
    # the top of the window as soon as anything is scrolled. That left
    # the same shell looking like two different designs: opened and left
    # alone it carried a band of bare background above the top bar and a
    # gap between the two, and both closed the moment you touched the
    # wheel.
    #
    # Reported as one page looking right and another looking wrong -
    # which they did, because the two screenshots were of the same shell
    # at different scroll positions.

    def make_scrollable():
        """Give the page room to scroll, whatever is on it.

        Without this the check below is asserting that a page which
        cannot move has not moved.

        The spacer goes inside the title bar's own parent, because that
        box is what bounds a sticky element's travel. Hung on .main
        instead - outside it - the container ran out underneath the bar
        and carried it up the screen, which looked exactly like the bar
        failing to stick and was the test's own doing.
        """
        return b.evaluate("""
            (function () {
                var old = document.getElementById('__tall');
                if (old) old.remove();
                var pad = document.createElement('div');
                pad.id = '__tall';
                pad.style.height = '2000px';
                document.querySelector('.page-header')
                        .parentElement.appendChild(pad);
                return document.documentElement.scrollHeight >
                       window.innerHeight + 200;
            })()
        """)

    def masthead():
        return json.loads(b.evaluate("""
            (function () {
                var bar = document.querySelector('.topbar');
                var head = document.querySelector('.page-header');
                var b1 = bar.getBoundingClientRect();
                var h1 = head.getBoundingClientRect();
                var root = document.documentElement;
                return JSON.stringify({
                    barTop: Math.round(b1.top),
                    barBottom: Math.round(b1.bottom),
                    headTop: Math.round(h1.top),
                    barH: Math.round(b1.height),
                    away: root.classList.contains('bar-away'),
                    titleTop: getComputedStyle(root)
                                .getPropertyValue('--title-top').trim(),
                    topbarH: getComputedStyle(root)
                                .getPropertyValue('--topbar-h').trim(),
                    y: Math.round(window.scrollY),
                    scrollable: document.documentElement.scrollHeight >
                                window.innerHeight + 200
                });
            })()
        """))

    for width, height in ((1440, 900), (1280, 800), (1920, 1080)):
        viewport(width, height, touch=False)
        b.call("Page.navigate", url=BASE + "/foods")
        wait("!!document.querySelector('.page-header')", "foods header")
        time.sleep(0.8)

        check("%dx%d: the page can be scrolled at all" % (width, height),
              make_scrollable(),
              "nothing below is a test of what happens on a scroll")

        b.evaluate("window.scrollTo(0, 0)")
        time.sleep(0.3)
        rest = masthead()
        check("%dx%d: the bar starts at the top of the window"
              % (width, height),
              rest["barTop"] == 0,
              "it rests %dpx down, on a band of bare background that "
              "disappears on the first scroll" % rest["barTop"])

        check("%dx%d: the page title sits on the bar above it"
              % (width, height),
              abs(rest["headTop"] - rest["barBottom"]) <= 1,
              "a %dpx gap between the two bars, which closes as soon as "
              "anything is scrolled"
              % (rest["headTop"] - rest["barBottom"]))

        if rest["scrollable"]:
            b.evaluate("window.scrollTo(0, 400)")
            time.sleep(0.5)
            moved = masthead()
            check("%dx%d: and neither moves when the page is scrolled"
                  % (width, height),
                  moved["barTop"] == rest["barTop"] and
                  moved["headTop"] == rest["headTop"],
                  "at rest %s; scrolled to %d it is %s - the top of "
                  "the app shifts under the pointer" % (rest, moved["y"],
                                                        moved))
            b.evaluate("window.scrollTo(0, 0)")
            time.sleep(0.3)

    # The other half: a touch device still gets its own spacing, and
    # still slides the bar away on the way down the page. That behaviour
    # is the reason the change above is scoped to wide mouse-driven
    # screens rather than applied to every size.
    viewport(834, 1112, touch=True)
    b.call("Page.navigate", url=BASE + "/foods")
    wait("!!document.querySelector('.page-header')", "tablet foods header")
    time.sleep(0.8)

    check("the tablet page can be scrolled at all", make_scrollable(),
          "a page that cannot move cannot show the bar moving")

    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.3)
    for _ in range(10):
        b.call("Input.dispatchMouseEvent", type="mouseWheel",
               x=400, y=500, deltaX=0, deltaY=120)
        time.sleep(0.05)
    time.sleep(0.6)

    check("a tablet still gets its bar out of the way on the way down",
          b.evaluate(
              "document.documentElement.classList.contains('bar-away')"),
          "the bar stayed put on a touch screen, so the wide-screen rule "
          "has leaked onto the devices that need the room back")

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
