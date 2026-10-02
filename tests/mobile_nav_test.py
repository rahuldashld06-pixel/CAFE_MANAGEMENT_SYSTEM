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
    "phone_number": "", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)


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

# Every account this suite made has been shown round already, so the

# first-sign-in tour does not open over the top of what is being

# tested here.

mysql_shim.skip_tour()


path = cdp.find_browser()
if not path:
    print("SKIPPED: no browser")
    sys.exit(0)

# Every account this suite made has been shown round already, so
# the first-sign-in tour does not open over what is being tested.
mysql_shim.skip_tour()

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
        f.querySelector('[name=password]').value='Brew-Latte-42';
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
    # One row: the menu, the name, and the controls at the right.
    check("the header is one modest row",
          0 < chrome < 90, "chrome above content is %spx tall" % chrome)
    check("the sidebar takes no room in the layout while closed",
          b.evaluate("Math.round(document.querySelector('.main').getBoundingClientRect().left)") < 5
          and b.evaluate("Math.round(document.querySelector('.main').getBoundingClientRect().top)")
          <= chrome + 2,
          "the page starts beside or below a nav block, not under the header")

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
    # Where a finger taps "outside": the strip of backdrop the drawer does
    # not cover. The backdrop's own centre is under the drawer on a phone,
    # so tapping there used to land on a nav link - which closed the drawer
    # by navigating away, and passed for the wrong reason.
    outside = b.evaluate("""
        (function () {
            var drawer = document.getElementById('appSidebar').getBoundingClientRect();
            return [Math.round((drawer.right + innerWidth) / 2),
                    Math.round(innerHeight / 2)];
        })()
    """)
    path_before = b.evaluate("location.pathname")
    for kind in ("mousePressed", "mouseReleased"):
        b.call("Input.dispatchMouseEvent", type=kind, x=outside[0], y=outside[1],
               button="left", clickCount=1)
    time.sleep(0.7)
    check("tapping outside closes the drawer",
          not drawer_open() and b.evaluate("location.pathname") == path_before,
          "open=%s, now at %s" % (drawer_open(), b.evaluate("location.pathname")))

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
    # One row is little enough to keep, so it stays - and frosts over, so
    # the page passing under it shows through softly.
    check("the header stays at the top on the way down",
          -2 <= after["top"] <= 2,
          "it is at %spx while scrolling down" % after["top"])
    check("and turns to glass over the page",
          b.evaluate("document.documentElement.classList.contains('is-scrolled')")
          and b.evaluate("""
              (function () {
                  var cs = getComputedStyle(document.querySelector('.topbar'));
                  var filter = cs.backdropFilter || cs.webkitBackdropFilter || '';
                  // color-mix() reads back as color(srgb ... / a).
                  return filter.indexOf('blur') > -1
                         && /rgba\(|\/\s*0?\.\d/.test(cs.backgroundColor);
              }())
          """),
          "the header is still a solid bar with the page scrolled under it")

    b.evaluate("window.scrollTo(0, Math.max(0, window.scrollY - 300))")
    time.sleep(0.6)
    back = json.loads(b.evaluate("""
        (function () {
            var r = document.querySelector('.topbar').getBoundingClientRect();
            return JSON.stringify({top: Math.round(r.top)});
        })()
    """))
    check("and is still there on the way up",
          -2 <= back["top"] <= 2,
          "it is at %spx after scrolling back up" % back["top"])
    check("the hamburger is tappable again once it is back",
          hit_test("#navToggle"),
          "something is covering the top bar")
    check("and the profile menu with it",
          hit_test("#profileTrigger"))

    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.3)

    print("\n=== The cafe's name and logo in the sticky bar ===")
    # On a phone the sidebar is a drawer, so the brand it carries is out of
    # sight on every page. A copy lives in the bar that stays at the top.
    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.3)

    check("the brand is in the top bar on a phone",
          b.evaluate("getComputedStyle("
                     "document.querySelector('.topbar-brand')).display")
          == "flex",
          "it is hidden on a phone, where the sidebar is shut")
    check("it carries the product name",
          "Refero" in (b.evaluate(
              "document.querySelector('.topbar-brand__name').textContent")
              or ""),
          "it reads %r" % b.evaluate(
              "document.querySelector('.topbar-brand__name').textContent"))

    # The hamburger and the profile are the only way off a page. Neither may
    # be pushed off by a long cafe name.
    layout = json.loads(b.evaluate("""
        (function () {
            function box(sel) {
                var r = document.querySelector(sel).getBoundingClientRect();
                return {left: Math.round(r.left), right: Math.round(r.right),
                        top: Math.round(r.top), bottom: Math.round(r.bottom),
                        width: Math.round(r.width)};
            }
            return JSON.stringify({
                toggle: box('#navToggle'),
                brand: box('.topbar-brand'),
                profile: box('#profileTrigger'),
                screen: window.innerWidth,
                scrollWidth: document.documentElement.scrollWidth
            });
        })()
    """))

    check("the hamburger keeps its place at the left",
          layout["toggle"]["left"] >= 0
          and layout["toggle"]["width"] > 30,
          "the hamburger is at %s" % layout["toggle"])
    check("the profile button is wholly on screen",
          layout["profile"]["right"] <= layout["screen"] + 1
          and layout["profile"]["width"] > 30,
          "the profile is at %s on a %spx screen"
          % (layout["profile"], layout["screen"]))
    check("the name leads and the controls hold the right corner",
          layout["brand"]["left"] < layout["profile"]["left"]
          and layout["brand"]["right"] <= layout["profile"]["left"] + 1,
          "they do not line up: %s" % layout)
    # One row: the menu, the name, then the controls. It was two rows -
    # the name on one and the buttons under it - which cost a phone 60px
    # of every screen for nothing a single row could not carry.
    check("and the menu button leads the same row as the name",
          layout["toggle"]["right"] <= layout["brand"]["left"] + 1
          and layout["toggle"]["top"] < layout["brand"]["bottom"]
          and layout["toggle"]["bottom"] > layout["brand"]["top"],
          "the menu is not on the name's row, ahead of it: %s" % layout)
    check("and nothing pushes the page sideways",
          layout["scrollWidth"] <= layout["screen"] + 1,
          "the page scrolls sideways by %dpx"
          % (layout["scrollWidth"] - layout["screen"]))

    check("the brand is still tappable", hit_test(".topbar-brand"),
          "something is covering it")

    b.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    time.sleep(0.6)
    check("it stays with the header on the way down",
          int(b.evaluate(
              "Math.round(document.querySelector('.topbar-brand')"
              ".getBoundingClientRect().top)")) >= 0,
          "the name scrolled away with the page")

    b.evaluate("window.scrollTo(0, Math.max(0, window.scrollY - 300))")
    time.sleep(0.6)
    check("and is still there on the way up",
          int(b.evaluate(
              "Math.round(document.querySelector('.topbar-brand')"
              ".getBoundingClientRect().top)")) >= -2,
          "the name did not come back")
    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.5)

    print("\n=== The order is a bar along the bottom, the menu in the corner ===")
    # Laid out the way a food-delivery app does it on a phone: the order
    # as a bar across the bottom middle, and a small Menu button above it
    # at the right that opens a card of the menu's sections.
    tap("#navToggle")
    tap_nav("New Order")
    wait("!!document.getElementById('orderSummaryTrigger')", "the summary button")
    time.sleep(0.6)

    def box_of(element_id):
        return json.loads(b.evaluate("""
            (function () {
                var r = document.getElementById('%s').getBoundingClientRect();
                return JSON.stringify({
                    width: Math.round(r.width),
                    top: Math.round(r.top),
                    leftGap: Math.round(r.left),
                    rightGap: Math.round(innerWidth - r.right),
                    bottomGap: Math.round(innerHeight - r.bottom),
                    viewport: innerWidth
                });
            })()
        """ % element_id))

    box = box_of("orderSummaryTrigger")
    # position:fixed anchors to the nearest transformed ancestor rather than
    # the viewport, so an animation that leaves a transform on .main pushes
    # this bar hundreds of pixels below the fold on a tall phone page.
    check("the order bar sits along the bottom, on screen",
          0 <= box["bottomGap"] <= 30,
          "bottom gap %(bottomGap)s" % box)
    check("in the middle, most of the width across",
          abs(box["leftGap"] - box["rightGap"]) <= 4
          and box["width"] >= box["viewport"] * 0.8,
          "left=%(leftGap)s right=%(rightGap)s, %(width)spx of %(viewport)spx" % box)
    check("nothing is covering it", hit_test("#orderSummaryTrigger"),
          "a tap at its centre lands on something else")

    jump = box_of("menuJump")
    check("the Menu button sits above it at the right",
          0 <= jump["rightGap"] <= 30 and jump["top"] + 0 < box["top"]
          and jump["width"] < jump["viewport"] * 0.5,
          "menu %s, bar top %s" % (jump, box["top"]))
    check("and can be tapped", hit_test("#menuJump"),
          "a tap at its centre lands on something else")

    tap("#menuJump")
    time.sleep(0.4)
    sections = json.loads(b.evaluate("""
        JSON.stringify([].map.call(
            document.querySelectorAll('#menuJumpCard [data-tab]'),
            function (item) { return item.getAttribute('data-tab'); }))
    """))
    tabs = json.loads(b.evaluate("""
        JSON.stringify([].map.call(
            document.querySelectorAll('#menuTabs .menu-tab'),
            function (tab) { return tab.getAttribute('data-tab'); }))
    """))
    check("it opens a card of the menu's sections",
          b.evaluate("!document.getElementById('menuJumpCard').hidden")
          and sections == tabs and len(sections) >= 2,
          "card %s, tabs %s" % (sections, tabs))
    last = sections[-1] if sections else ""
    b.evaluate("""
        document.querySelector('#menuJumpCard [data-tab="%s"]').click()
    """ % last)
    time.sleep(0.6)
    check("picking one closes the card and shows that section",
          b.evaluate("document.getElementById('menuJumpCard').hidden")
          and b.evaluate("""
              document.querySelector('#menuTabs .menu-tab.is-on')
                  .getAttribute('data-tab') === '%s'
          """ % last),
          "card still open, or another section shown")
    tap("#menuJump")
    time.sleep(0.3)
    b.evaluate("document.getElementById('menuJumpShade').click()")
    time.sleep(0.3)
    check("a tap outside the card closes it",
          b.evaluate("document.getElementById('menuJumpCard').hidden"),
          "the card stayed open")
    b.evaluate("""
        var all = document.querySelector('#menuTabs .menu-tab[data-tab="all"]');
        if (all) all.click();
    """)

    print("\n=== The sidebar only scrolls when the links do not fit ===")
    # The whole sidebar used to scroll as one block, so the name scrolled
    # away with the links and the scrollbar ran the full height even when
    # only the links overflowed. The name is a fixed head now and the list
    # of pages below it is the part that scrolls.
    b.call("Emulation.setTouchEmulationEnabled", enabled=False)

    def overflows(selector):
        return b.evaluate("""
            (function () {
                var el = document.querySelector('%s');
                return el.scrollHeight > el.clientHeight + 1;
            }())
        """ % selector)

    def scrolls(selector):
        """Whether the browser will actually let this element scroll."""
        return b.evaluate("""
            (function () {
                var el = document.querySelector('%s');
                var style = getComputedStyle(el).overflowY;
                return (style === 'auto' || style === 'scroll')
                       && el.scrollHeight > el.clientHeight + 1;
            }())
        """ % selector)

    # Tall enough for every section at once.
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000,
           deviceScaleFactor=1, mobile=False)
    b.evaluate("location.href = '/orders/add'")
    wait("document.readyState === 'complete' && "
         "!!document.querySelector('.sidebar-nav')", "New Order on a tall screen")
    time.sleep(0.5)

    check("with room for every section, nothing scrolls at all",
          not scrolls(".sidebar-nav") and not scrolls(".sidebar"),
          "a scrollbar is offered on a screen where everything fits")
    check("and every page link is on screen",
          b.evaluate("""
              (function () {
                  var links = document.querySelectorAll('.sidebar-nav .nav-link');
                  var nav = document.querySelector('.sidebar-nav')
                      .getBoundingClientRect();
                  for (var i = 0; i < links.length; i++) {
                      var r = links[i].getBoundingClientRect();
                      if (r.bottom > nav.bottom + 1) return false;
                  }
                  return links.length > 0;
              }())
          """),
          "a link is cut off on a screen with room for all of them")

    # Now short enough that they cannot all fit.
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=520,
           deviceScaleFactor=1, mobile=False)
    b.evaluate("location.href = '/orders/add'")
    wait("document.readyState === 'complete' && "
         "!!document.querySelector('.sidebar-nav')", "New Order on a short screen")
    time.sleep(0.5)

    check("when they do not fit, the list of pages scrolls",
          scrolls(".sidebar-nav"),
          "the links overflow but cannot be scrolled to")
    check("and the scroll belongs to the list, not the whole sidebar",
          not scrolls(".sidebar"),
          "the sidebar scrolls as one block, taking the name with it")

    top_before = int(b.evaluate(
        "Math.round(document.querySelector('.sidebar-brand')"
        ".getBoundingClientRect().top)"))
    b.evaluate("document.querySelector('.sidebar-nav').scrollTop = 300")
    time.sleep(0.4)
    moved = int(b.evaluate("document.querySelector('.sidebar-nav').scrollTop"))
    top_after = int(b.evaluate(
        "Math.round(document.querySelector('.sidebar-brand')"
        ".getBoundingClientRect().top)"))

    check("the list actually moved", moved > 0,
          "scrollTop stayed at %d" % moved)
    check("the name does not move with it", top_before == top_after,
          "it went from %dpx to %dpx" % (top_before, top_after))
    check("and the links disappear under it rather than over it",
          b.evaluate("""
              (function () {
                  var nav = document.querySelector('.sidebar-nav')
                      .getBoundingClientRect();
                  var brand = document.querySelector('.sidebar-brand')
                      .getBoundingClientRect();
                  return nav.top >= brand.bottom - 1;
              }())
          """),
          "the scrolling list starts above the bottom of the name")

    print("\n=== A touch tablet keeps its drawer, and scrolls the same way ===")
    b.call("Emulation.setDeviceMetricsOverride", width=1180, height=820,
           deviceScaleFactor=2, mobile=True)
    b.call("Emulation.setTouchEmulationEnabled", enabled=True,
           maxTouchPoints=5)
    b.evaluate("location.href = '/orders/add'")
    wait("document.readyState === 'complete'", "New Order on a tablet")
    time.sleep(0.5)

    check("the hamburger is still there on a touch tablet",
          b.evaluate("getComputedStyle(document.getElementById('navToggle'))"
                     ".display") != "none",
          "the drawer was taken away from a tablet")
    check("and its drawer gives the scroll to the links too",
          b.evaluate("getComputedStyle(document.querySelector('.sidebar-nav'))"
                     ".overflowY") in ("auto", "scroll"),
          "the drawer scrolls as one block")

    print("\n=== A large screen: the header across the top, the rail down the side ===")
    # The header runs the full width above the rail, like any site's, and
    # stays while the page scrolls under it. The page's own title belongs
    # to the page and scrolls with it; the tab and the rail still say
    # where you are.
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=680,
           deviceScaleFactor=1, mobile=False)
    b.call("Emulation.setTouchEmulationEnabled", enabled=False)
    b.evaluate("location.href = '/foods'")
    wait("document.readyState === 'complete' && "
         "!!document.querySelector('.page-header')", "Food Management")
    time.sleep(0.7)

    def box(selector, prop):
        return int(b.evaluate(
            "Math.round(document.querySelector('%s')"
            ".getBoundingClientRect().%s)" % (selector, prop)))

    title = b.evaluate(
        "document.querySelector('.page-header h1').textContent.trim()")

    b.evaluate("window.scrollTo(0, 900)")
    time.sleep(0.5)
    scrolled = int(b.evaluate("Math.round(window.scrollY)"))

    check("the page actually scrolled", scrolled > 100,
          "only reached %dpx - the checks below would prove nothing"
          % scrolled)
    check("the header runs the full width of a large screen",
          box(".topbar", "left") <= 0
          and box(".topbar", "right") >= 1438,
          "it spans %d to %d" % (box(".topbar", "left"), box(".topbar", "right")))
    check("and stays at the top while the page scrolls",
          -2 <= box(".topbar", "top") <= 2,
          "it is at %dpx" % box(".topbar", "top"))
    check("the rail starts under it, not beside it",
          box("#appSidebar", "top") >= box(".topbar", "bottom") - 1,
          "the rail reaches up to %dpx, under a %dpx header"
          % (box("#appSidebar", "top"), box(".topbar", "bottom")))
    check("the page's own title scrolled away with its page",
          box(".page-header", "bottom") < box(".topbar", "bottom"),
          "the title is still pinned at %dpx" % box(".page-header", "top"))
    check("and it says which page this is",
          "Food Management" in title, "it reads %r" % title)

    import json as _json
    placed = _json.loads(b.evaluate("""
        (function () {
            var bar = document.querySelector('.topbar');
            return JSON.stringify({
                profile: bar.contains(document.getElementById('profileTrigger')),
                full: bar.contains(document.getElementById('fullscreenBtn')),
                status: bar.contains(document.getElementById('orderStatusFab')),
                search: bar.contains(document.getElementById('topSearchInput')),
                brand: getComputedStyle(bar.querySelector('.topbar-brand')).display
            });
        }())
    """))
    check("the profile, full screen and Order Status are in the header",
          placed["profile"] and placed["full"] and placed["status"],
          "one of them is somewhere else: %s" % placed)
    check("with the search box and the cafe's name",
          placed["search"] and placed["brand"] != "none", placed)

    # Narrowed to a phone, the header is the same header, and the rail's
    # offset is its real, measured height.
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=780,
           deviceScaleFactor=2, mobile=True)
    time.sleep(0.8)
    check("on a phone the profile is still in the header",
          b.evaluate("document.querySelector('.topbar')"
                     ".contains(document.getElementById('profileTrigger'))"),
          "the profile went into the drawer, out of sight")

    check("the measured header height is what the layout uses",
          b.evaluate("""
              (function () {
                  var declared = parseInt(getComputedStyle(
                      document.documentElement)
                      .getPropertyValue('--topbar-h'), 10);
                  var real = Math.round(document.querySelector('.topbar')
                      .getBoundingClientRect().height);
                  return real > 0 && Math.abs(declared - real) <= 1;
              }())
          """),
          "the title is offset by a guess rather than the bar's real height")

    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=680,
           deviceScaleFactor=1, mobile=False)
    time.sleep(0.5)

    print("\n=== Nothing draws a scrollbar, and everything still scrolls ===")
    for label, selector in (("the page", "document.documentElement"),
                            ("the sidebar links",
                             "document.querySelector('.sidebar-nav')")):
        check("%s draws no scrollbar" % label,
              b.evaluate("getComputedStyle(%s).scrollbarWidth" % selector)
              == "none",
              "a scrollbar is still drawn on %s" % label)

    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.3)
    b.evaluate("window.scrollTo(0, 500)")
    time.sleep(0.3)
    check("the page still scrolls without one",
          int(b.evaluate("Math.round(window.scrollY)")) > 100,
          "hiding the scrollbar stopped the page scrolling")

    print("\n=== On a phone the title costs one row, not half the screen ===")
    # The page header stacks on a narrow screen and its actions go full
    # width. It is kept to one row, and it scrolls with its page.
    b.call("Emulation.setDeviceMetricsOverride", **PHONE)
    b.call("Emulation.setTouchEmulationEnabled", enabled=True,
           maxTouchPoints=5)
    b.evaluate("location.href = '/foods'")
    wait("document.readyState === 'complete' && "
         "!!document.querySelector('.page-header')", "Food Management on a phone")
    time.sleep(0.7)

    header_height = box(".page-header", "height")
    check("the title is a single row",
          header_height <= 90,
          "it is %dpx tall, which is most of the screen" % header_height)
    # The header and the title together, before any content: well inside
    # a quarter of a phone screen.
    check("the content starts inside a quarter of the screen",
          box(".page-header", "bottom") <= 215,
          "content does not begin until %dpx down"
          % box(".page-header", "bottom"))

    # Scrolled down, the title goes with the page and the header stays.
    b.evaluate("window.scrollTo(0, 700)")
    time.sleep(0.7)
    check("scrolled, the title goes with its page",
          box(".page-header", "bottom") <= box(".topbar", "bottom"),
          "the title is still at %dpx" % box(".page-header", "top"))
    check("and the header stays",
          -2 <= box(".topbar", "top") <= 2,
          "the header is at %dpx" % box(".topbar", "top"))

    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.5)
    check("and the name is readable at the top",
          b.evaluate("""
              (function () {
                  var h = document.querySelector('.page-header h1');
                  return h.getBoundingClientRect().width > 60
                         && h.textContent.trim().length > 0;
              }())
          """),
          "the title was squeezed to nothing")

    print("\n=== Tables become readable cards on a phone ===")
    # A table 640px wide inside a 390px screen scrolled sideways inside
    # its own box, and with scrollbars hidden it did not even say so. A
    # cashier taking a payment could not see the amount and the button at
    # the same time.
    b.call("Emulation.setDeviceMetricsOverride", **PHONE)
    b.call("Emulation.setTouchEmulationEnabled", enabled=True,
           maxTouchPoints=5)

    for label, url in (("Food Management", "/foods"),
                       ("Inventory", "/inventory"),
                       ("Categories", "/categories"),
                       ("Billing", "/billing")):
        b.evaluate("location.href = '%s'" % url)
        wait("document.readyState === 'complete'", label)
        time.sleep(0.6)

        report = json.loads(b.evaluate("""
            (function () {
                var table = document.querySelector('.data-table');
                if (!table) return JSON.stringify({none: true});

                var row = table.querySelector('tbody tr');
                if (!row) return JSON.stringify({empty: true});

                var cells = row.children;
                var labelled = 0;
                var widest = 0;
                for (var i = 0; i < cells.length; i++) {
                    if (cells[i].getAttribute('data-label') !== null) labelled++;
                    widest = Math.max(widest,
                        Math.round(cells[i].getBoundingClientRect().right));
                }

                var wrap = table.closest('.table-wrap') || table.parentNode;
                return JSON.stringify({
                    cells: cells.length,
                    labelled: labelled,
                    widest: widest,
                    screen: window.innerWidth,
                    // A block or a grid card; anything but a table row.
                    stacked: ['block', 'grid', 'flex']
                        .indexOf(getComputedStyle(row).display) !== -1,
                    sideways: wrap.scrollWidth > wrap.clientWidth + 1,
                    rowHeight: Math.round(row.getBoundingClientRect().height)
                });
            }())
        """))

        if report.get("none") or report.get("empty"):
            continue

        check("%s: every cell knows its column" % label,
              report["labelled"] == report["cells"],
              "%d of %d cells are labelled"
              % (report["labelled"], report["cells"]))
        check("%s: the row is a stacked card" % label, report["stacked"],
              "the row is still laid out as a table row")
        check("%s: nothing scrolls sideways" % label, not report["sideways"],
              "the table still scrolls sideways inside its box")
        check("%s: the whole row fits the screen" % label,
              report["widest"] <= report["screen"] + 1,
              "a cell reaches %dpx on a %dpx screen"
              % (report["widest"], report["screen"]))

    print("\n=== And stay ordinary tables on a desktop ===")
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
           deviceScaleFactor=1, mobile=False)
    b.call("Emulation.setTouchEmulationEnabled", enabled=False)
    b.evaluate("location.href = '/foods'")
    wait("document.readyState === 'complete'", "Food Management on a desktop")
    time.sleep(0.6)

    check("the header row is back",
          b.evaluate("getComputedStyle(document.querySelector"
                     "('.data-table thead')).display") != "none",
          "the column headers are hidden on a desktop")
    check("and rows are rows again",
          b.evaluate("getComputedStyle(document.querySelector"
                     "('.data-table tbody tr')).display") != "block",
          "rows are still stacked on a desktop")

    print("\n=== Back on a desktop width ===")
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
           deviceScaleFactor=1, mobile=False)
    time.sleep(0.6)
    # The menu button stays: on a large screen it opens the rail out into
    # the full guide, the way YouTube's does.
    check("the menu button is in the header here too",
          b.evaluate("getComputedStyle(document.getElementById('navToggle')).display") != "none")
    check("the rail is on screen permanently",
          b.evaluate("""
              (function () {
                  var s = document.getElementById('appSidebar');
                  var r = s.getBoundingClientRect();
                  return getComputedStyle(s).visibility === 'visible'
                         && r.left >= -1 && r.width > 60
                         && getComputedStyle(s).position !== 'fixed';
              }())
          """),
          "the side is not a rail on a desktop")
    check("and the sidebar's own copy of the brand steps aside",
          b.evaluate("getComputedStyle("
                     "document.querySelector('.sidebar-brand')).display")
          == "none",
          "the cafe name is drawn twice on a desktop")

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
