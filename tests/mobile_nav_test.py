"""
Real-browser test for the mobile navigation drawer.

On a phone the sidebar used to sit above the page as a block of wrapped
links, so the first screenful was mostly navigation. Below 880px it is now
a drawer: off-canvas until the hamburger in the top-left is tapped.

Driven at a 390x844 viewport: that the nav costs the page almost no height
while closed, that its links are not tabbable while off-canvas, that the
drawer opens with readable labels (an earlier breakpoint hid them for an
icon-only strip), that it closes itself after navigating, on a backdrop tap
and on Escape, and that it survives the page swaps instant.js performs.

Also checks the desktop layout is untouched at 1440px.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without one.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/mobile_nav_test.py
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
os.environ["SECRET_KEY"] = "mobile-test-secret"
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
    "cafe_name": "Mobile Cafe", "full_name": "M Owner", "username": "mob",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as s:
        return s.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Beverages",
                                   "description": "", "_csrf_token": csrf()},
          follow_redirects=True)
cat = re.search(r'<option value="(\d+)">',
                seed.get("/foods/add").get_data(as_text=True)).group(1)
# Enough of a menu that a phone page is taller than the screen. The cards
# were made denser, and with only a couple of items the New Order page now
# fits without scrolling - which left the sticky-bar check below with
# nothing to scroll.
for food in ["Cold Coffee", "Masala Chai", "Espresso", "Flat White",
             "Cortado", "Cold Brew", "Mocha", "Americano",
             "Croissant", "Blueberry Muffin"]:
    seed.post("/foods/add", data={"food_name": food, "category_id": cat,
                                  "price": "40", "quantity": "10",
                                  "description": "", "_csrf_token": csrf()},
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

path = cdp.find_browser()
if not path:
    print("SKIPPED: no browser")
    sys.exit(0)

b = cdp.Browser(path)

PHONE = dict(width=390, height=844, deviceScaleFactor=2, mobile=True)


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



def hit_test(selector):
    """
    What the browser finds painted at this element's own centre.

    Real fingers hit whatever is on top. A JS .click() does not, which is how
    an invisible full-screen backdrop once sat in front of every control here
    without a single test noticing.
    """
    return b.evaluate("""
        (function () {
            var el = document.querySelector(%r);
            if (!el) return false;
            var r = el.getBoundingClientRect();
            var hit = document.elementFromPoint(
                Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2));
            return !!(hit && (hit === el || el.contains(hit)));
        })()
    """ % selector)


def tap(selector):
    """Dispatch genuine input at the element's centre, the way a finger does."""
    point = b.evaluate("""
        (function () {
            var el = document.querySelector(%r);
            if (!el) return null;
            var r = el.getBoundingClientRect();
            return [Math.round(r.left + r.width / 2), Math.round(r.top + r.height / 2)];
        })()
    """ % selector)
    if not point:
        raise AssertionError("no element for " + selector)
    for kind in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=kind, x=point[0], y=point[1],
               button="left", clickCount=1)
    time.sleep(0.7)


def tap_nav(label):
    """Tap a drawer link by its visible name."""
    point = b.evaluate("""
        (function () {
            var links = document.querySelectorAll('#appSidebar .nav-link');
            for (var i = 0; i < links.length; i++) {
                if (links[i].textContent.trim() === %r) {
                    var r = links[i].getBoundingClientRect();
                    return [Math.round(r.left + r.width / 2),
                            Math.round(r.top + r.height / 2)];
                }
            }
            return null;
        })()
    """ % label)
    if not point:
        raise AssertionError("no drawer link named " + label)
    for kind in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=kind, x=point[0], y=point[1],
               button="left", clickCount=1)
    time.sleep(1.4)



def drawer_open():
    return b.evaluate("document.querySelector('.app-shell').classList.contains('nav-open')")


def sidebar_on_screen():
    """Is the drawer actually visible and inside the viewport?"""
    return b.evaluate("""
        (function () {
            var s = document.getElementById('appSidebar');
            var r = s.getBoundingClientRect();
            var v = getComputedStyle(s).visibility;
            return v === 'visible' && r.left > -5 && r.width > 100;
        })()
    """)


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", **PHONE)

    b.call("Page.navigate", url=BASE + "/login")
    wait("document.readyState==='complete' && !!document.querySelector('form')", "login")
    b.evaluate("""(function(){var f=document.querySelector('form');
        f.querySelector('[name=username]').value='mob';
        f.querySelector('[name=password]').value='password123';
        f.submit(); return 1;})()""")
    wait("!!document.getElementById('page-view')", "shell")
    time.sleep(2.5)

    print("\n=== On a phone, the page owns the screen ===")
    check("the hamburger is visible",
          b.evaluate("getComputedStyle(document.getElementById('navToggle')).display") != "none")
    check("nothing is covering the hamburger",
          hit_test("#navToggle"),
          "a tap at its centre lands on something else - check for an "
          "invisible overlay still accepting pointer events")
    check("the drawer starts closed and off-screen", not sidebar_on_screen())
    check("the closed backdrop does not intercept taps",
          b.evaluate("""
              getComputedStyle(document.getElementById('sidebarBackdrop'))
                  .pointerEvents === 'none'
              || document.getElementById('sidebarBackdrop').hidden
          """),
          "the invisible backdrop is still swallowing every tap")
    check("its links are not reachable by tab while closed",
          b.evaluate("getComputedStyle(document.getElementById('appSidebar')).visibility") == "hidden")

    # The point of the drawer: the nav must cost the page almost no height.
    # Measured on the chrome itself, not on content - a flash banner or a
    # page header is content the user wants, and would muddy the number.
    chrome = b.evaluate("""
        Math.round(document.querySelector('.topbar').getBoundingClientRect().bottom)
    """)
    check("the nav costs the page barely any height now",
          0 < chrome < 90, "chrome above content is %spx tall" % chrome)
    check("the sidebar takes no room in the layout while closed",
          b.evaluate("Math.round(document.querySelector('.main').getBoundingClientRect().top)") < 5,
          "the page still starts below a nav block")

    print("\n=== Tapping the hamburger ===")
    tap("#navToggle")
    check("the drawer opens", drawer_open() and sidebar_on_screen())
    check("aria-expanded is announced",
          b.evaluate("document.getElementById('navToggle').getAttribute('aria-expanded')") == "true")
    check("the backdrop is shown",
          b.evaluate("!document.getElementById('sidebarBackdrop').hidden"))
    check("the page behind it cannot scroll",
          b.evaluate("document.body.classList.contains('nav-locked')"))

    links = b.evaluate("""
        Array.prototype.slice.call(document.querySelectorAll('#appSidebar .nav-link'))
             .map(function (a) { return a.textContent.trim(); })
    """)
    labelled = b.evaluate("""
        Array.prototype.slice.call(document.querySelectorAll('#appSidebar .nav-link span'))
             .every(function (s) { return getComputedStyle(s).display !== 'none'; })
    """)
    check("the links show their names, not just icons", labelled,
          "labels are hidden - the drawer would be guesswork")

    check("every section is listed in the drawer",
          all(x in links for x in ["Dashboard", "Food Management", "Billing",
                                   "Inventory", "New Order", "Reports"]),
          "got %s" % links)

    print("\n=== Choosing a section ===")
    tap_nav("Billing")
    check("it navigates to that page",
          b.evaluate("location.pathname") == "/billing",
          "at %s" % b.evaluate("location.pathname"))
    check("the drawer closes itself after navigating", not drawer_open())
    check("the scroll lock is released",
          not b.evaluate("document.body.classList.contains('nav-locked')"))

    print("\n=== Backdrop and Escape ===")
    tap("#navToggle")
    tap("#sidebarBackdrop")
    check("tapping outside closes the drawer", not drawer_open())

    tap("#navToggle")
    b.evaluate("""document.dispatchEvent(
        new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))""")
    time.sleep(0.5)
    check("Escape closes the drawer", not drawer_open())

    print("\n=== Still works several swaps later ===")
    for section in ["Inventory", "Food Management", "New Order"]:
        tap("#navToggle")
        tap_nav(section)
        check("%-16s reached from the drawer" % section, not drawer_open(),
              "drawer stuck open at %s" % b.evaluate("location.pathname"))

    check("no full page reloads throughout",
          b.evaluate("performance.getEntriesByType('navigation').length") == 1)

    print("\n=== The top bar survives scrolling ===")
    # The hamburger and the profile menu are the only way off a page on a
    # phone. They used to scroll away with everything else, stranding the
    # user at the bottom of a long list.
    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.3)
    before = json.loads(b.evaluate("""
        (function () {
            var r = document.querySelector('.topbar').getBoundingClientRect();
            return JSON.stringify({top: Math.round(r.top),
                                   height: Math.round(r.height)});
        })()
    """))

    b.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(0.6)
    after = json.loads(b.evaluate("""
        (function () {
            var r = document.querySelector('.topbar').getBoundingClientRect();
            return JSON.stringify({top: Math.round(r.top),
                                   scrolled: Math.round(window.scrollY)});
        })()
    """))

    check("the page actually scrolled", after["scrolled"] > 50,
          "only moved %(scrolled)spx - the check below would prove nothing"
          % after)
    check("the top bar is still at the top of the screen",
          -2 <= after["top"] <= 2,
          "it moved to %spx after scrolling (was %spx)"
          % (after["top"], before["top"]))
    check("the hamburger is still tappable after scrolling",
          hit_test("#navToggle"),
          "something scrolled over the top bar")
    check("and the profile menu with it",
          hit_test("#profileTrigger"))

    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.3)

    print("\n=== The floating order summary stays in the corner ===")
    tap("#navToggle")
    tap_nav("New Order")
    wait("!!document.getElementById('orderSummaryTrigger')", "the summary button")
    time.sleep(0.6)

    box = json.loads(b.evaluate("""
        (function () {
            var r = document.getElementById('orderSummaryTrigger')
                            .getBoundingClientRect();
            return JSON.stringify({
                width: Math.round(r.width),
                rightGap: Math.round(innerWidth - r.right),
                bottomGap: Math.round(innerHeight - r.bottom),
                viewport: innerWidth
            });
        })()
    """))

    # position:fixed anchors to the nearest transformed ancestor rather than
    # the viewport, so an animation that leaves a transform on .main pushes
    # this button hundreds of pixels below the fold on a tall phone page.
    check("it sits in the bottom-right corner, on screen",
          0 <= box["rightGap"] <= 30 and 0 <= box["bottomGap"] <= 30,
          "gaps right=%(rightGap)s bottom=%(bottomGap)s" % box)
    check("it is a compact pill, not a bar across the bottom",
          box["width"] < box["viewport"] * 0.8,
          "%(width)spx wide on a %(viewport)spx screen" % box)
    check("nothing is covering it", hit_test("#orderSummaryTrigger"),
          "a tap at its centre lands on something else")

    print("\n=== Back on a desktop width ===")
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
           deviceScaleFactor=1, mobile=False)
    time.sleep(0.6)
    check("the hamburger is hidden again",
          b.evaluate("getComputedStyle(document.getElementById('navToggle')).display") == "none")
    check("the sidebar is back on screen permanently", sidebar_on_screen())

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
