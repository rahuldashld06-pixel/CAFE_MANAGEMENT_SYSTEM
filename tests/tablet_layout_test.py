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
