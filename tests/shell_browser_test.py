"""
The header, the guide, search, and the dashboard - in a real browser.

The header runs across the top of every page, in the page's own colour
with no line under it, and turns to frosted glass once the page scrolls
under it. Down the side of a laptop is a rail of icons with their names
under them; the menu button opens it out into the full guide and folds it
back, and the choice is kept. On a phone the side is a drawer, and search
is an icon that opens across the header.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without
one. No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/shell_browser_test.py
"""
import json
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
os.environ["SECRET_KEY"] = "shell-browser-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5833
BASE = "http://127.0.0.1:%d" % PORT

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Rail Cafe", "full_name": "Rae Owner", "username": "rae",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Coffee",
                                   "description": "", "_csrf_token": csrf()},
          follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     seed.get("/foods/add").get_data(as_text=True)).group(1)
for n in range(18):
    seed.post("/foods/add", data={
        "food_name": "Brew %02d" % n, "category_id": category,
        "price": str(80 + n), "quantity": "40", "minimum_stock": "2",
        "description": "", "_csrf_token": csrf()}, follow_redirects=True)
first_food = mysql_shim._DB.execute(
    "SELECT MIN(food_id) FROM foods").fetchone()[0]
for _ in range(3):
    seed.post("/orders/add", data={"quantity_%d" % first_food: "2",
                                   "_csrf_token": csrf()},
              follow_redirects=True)
for (bill,) in mysql_shim._DB.execute("SELECT bill_id FROM bills").fetchall():
    seed.post("/billing/mark-paid/%d" % bill, data={"_csrf_token": csrf()},
              follow_redirects=True)
mysql_shim.skip_tour()

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


def wait(expr, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if b.evaluate(expr):
                return True
        except RuntimeError:
            pass
        time.sleep(0.12)
    return False


def size(width, height, phone=False):
    b.call("Emulation.setDeviceMetricsOverride", width=width, height=height,
           deviceScaleFactor=1, mobile=phone)
    b.call("Emulation.setTouchEmulationEnabled", enabled=phone,
           maxTouchPoints=5 if phone else 1)


def open_page(path):
    b.call("Page.navigate", url=BASE + path)
    wait("document.readyState === 'complete' && !!document.querySelector('.topbar')")
    time.sleep(0.6)


def rect(selector):
    return json.loads(b.evaluate("""
        (function () {
            var el = document.querySelector(%r);
            if (!el) return 'null';
            var r = el.getBoundingClientRect();
            return JSON.stringify({left: Math.round(r.left), top: Math.round(r.top),
                                   right: Math.round(r.right), bottom: Math.round(r.bottom),
                                   width: Math.round(r.width), height: Math.round(r.height)});
        }())
    """ % selector))


def style(selector, prop):
    return b.evaluate("getComputedStyle(document.querySelector(%r))[%r]"
                      % (selector, prop))


def see_through(colour):
    """Whether a computed colour lets what is behind it through.

    color-mix() comes back as color(srgb r g b / a), not rgba()."""
    found = re.search(r"rgba\([^)]*,\s*([\d.]+)\)|/\s*([\d.]+)\s*\)", colour or "")
    if not found:
        return False
    return float(found.group(1) or found.group(2)) < 1


def root_has(name):
    return b.evaluate("document.documentElement.classList.contains(%r)" % name)


try:
    b.call("Page.enable")
    size(1440, 900)
    open_page("/login")
    b.evaluate("""(function(){var f=document.querySelector('form');
        f.querySelector('[name=username]').value='rae';
        f.querySelector('[name=password]').value='password123';
        f.submit(); return 1;}())""")
    wait("location.pathname !== '/login'")
    b.evaluate("try { localStorage.removeItem('cafora.guide'); "
               "localStorage.removeItem('cafora.dashPeriod'); } catch (e) {} 1")

    # =================================================================
    print("\n=== 1. The header, on a laptop ===")
    # =================================================================
    open_page("/foods")
    bar = rect(".topbar")
    check("it runs the full width of the screen",
          bar["left"] <= 0 and bar["right"] >= 1439, bar)
    check("above the rail, not beside it",
          rect("#appSidebar")["top"] >= bar["bottom"] - 1,
          "rail %s, header %s" % (rect("#appSidebar"), bar))
    check("in the page's own colour",
          style(".topbar", "backgroundColor") == style("body", "backgroundColor"),
          "header %s, page %s" % (style(".topbar", "backgroundColor"),
                                  style("body", "backgroundColor")))
    check("with no line under it",
          style(".topbar", "borderBottomWidth") == "0px"
          and "inset" not in (style(".topbar", "boxShadow") or ""),
          "border %s, shadow %s" % (style(".topbar", "borderBottomWidth"),
                                    style(".topbar", "boxShadow")))
    check("the name and the mark lead it",
          rect(".topbar-brand")["left"] < rect(".topbar-search")["left"]
          and "Cafora" in b.evaluate(
              "document.querySelector('.topbar-brand').textContent"))
    check("the search box sits in the middle",
          abs((rect(".topbar-search")["left"] + rect(".topbar-search")["right"]) / 2
              - 720) < 160, rect(".topbar-search"))
    check("New order, full screen, Order Status and the profile at the right",
          all(rect(sel)["left"] > rect(".topbar-search")["right"]
              for sel in (".topbar-create", "#orderStatusFab", "#profileTrigger")),
          "one of them is not to the right of search")
    check("the profile is the picture alone",
          rect("#profileTrigger")["width"] <= 48,
          "it is %spx wide" % rect("#profileTrigger")["width"])

    b.evaluate("window.scrollTo(0, 600)")
    time.sleep(0.5)
    check("scrolled, it stays at the top",
          -1 <= rect(".topbar")["top"] <= 1, rect(".topbar"))
    check("and turns to glass",
          root_has("is-scrolled")
          and "blur" in (style(".topbar", "backdropFilter") or "")
          and see_through(style(".topbar", "backgroundColor")),
          "filter %s, colour %s" % (style(".topbar", "backdropFilter"),
                                    style(".topbar", "backgroundColor")))
    check("while the page's own title goes with the page",
          rect(".page-header")["bottom"] < rect(".topbar")["bottom"],
          rect(".page-header"))
    b.evaluate("window.scrollTo(0, 0)")
    time.sleep(0.4)
    check("back at the top it is solid again", not root_has("is-scrolled"))

    # =================================================================
    print("\n=== 2. The rail, and the full guide ===")
    # =================================================================
    rail = rect("#appSidebar")
    check("the side starts as a slim rail", 60 <= rail["width"] <= 110,
          "it is %spx wide" % rail["width"])
    check("each icon over its name",
          style(".sidebar-nav .nav-link", "flexDirection") == "column")
    check("the section headings wait for the full guide",
          style(".nav-section", "display") == "none")

    b.evaluate("document.getElementById('navToggle').click()")
    time.sleep(0.4)
    full = rect("#appSidebar")
    check("the menu button opens it out", full["width"] >= 200,
          "it is %spx wide" % full["width"])
    check("into rows, with section headings",
          style(".sidebar-nav .nav-link", "flexDirection") == "row"
          and style(".nav-section", "display") != "none")
    check("and the page makes room rather than being covered",
          rect(".main")["left"] >= full["right"] - 1,
          "main %s, guide %s" % (rect(".main"), full))
    check("it says what it did",
          b.evaluate("document.getElementById('navToggle')"
                     ".getAttribute('aria-expanded')") == "true")

    open_page("/billing")
    check("the choice is kept on the next page",
          root_has("guide-full") and rect("#appSidebar")["width"] >= 200)
    b.call("Page.reload")
    wait("document.readyState === 'complete'")
    time.sleep(0.5)
    check("and after a reload", root_has("guide-full"))

    b.evaluate("document.getElementById('navToggle').click()")
    time.sleep(0.4)
    check("pressed again, it folds back to the rail",
          not root_has("guide-full") and rect("#appSidebar")["width"] <= 110)
    check("and stays folded",
          b.evaluate("localStorage.getItem('cafora.guide')") == "rail")

    # =================================================================
    print("\n=== 3. Search ===")
    # =================================================================
    open_page("/foods")
    b.evaluate("document.body.dispatchEvent(new KeyboardEvent('keydown', "
               "{key: '/', bubbles: true}))")
    time.sleep(0.2)
    check("'/' goes straight to the search box",
          b.evaluate("document.activeElement && "
                     "document.activeElement.id === 'topSearchInput'"))
    b.evaluate("""
        (function () {
            var input = document.getElementById('topSearchInput');
            input.value = 'Brew 07';
            document.getElementById('topSearch').requestSubmit();
        }())
    """)
    wait("location.pathname === '/search'")
    time.sleep(0.6)
    check("searching opens the results", b.evaluate("location.search") == "?q=Brew+07"
          or "Brew" in b.evaluate("decodeURIComponent(location.search)"),
          b.evaluate("location.href"))
    check("with the dish in them",
          "Brew 07" in b.evaluate("document.body.innerText"))
    check("and the box still says what was searched for",
          b.evaluate("document.getElementById('topSearchInput').value") == "Brew 07")
    check("without a full page load",
          b.evaluate("performance.getEntriesByType('navigation').length") == 1)

    # =================================================================
    print("\n=== 4. Order Status, from the bell ===")
    # =================================================================
    check("the bell is in the header",
          b.evaluate("document.querySelector('.topbar')"
                     ".contains(document.getElementById('orderStatusFab'))"))
    b.evaluate("document.getElementById('orderStatusFab').click()")
    wait("document.getElementById('orderStatusOverlay').classList.contains('open')")
    time.sleep(0.6)
    popup = rect(".order-status-popup")
    check("it drops from the header, at the right",
          popup["top"] >= rect(".topbar")["bottom"] - 1
          and popup["right"] >= 1440 - 40, popup)
    b.evaluate("document.getElementById('orderStatusClose').click()")

    # =================================================================
    print("\n=== 5. The dashboard ===")
    # =================================================================
    open_page("/dashboard")
    check("the curve is drawn",
          wait("!!document.querySelector('#curveSvg .curve__line')"),
          "no line in the chart")
    check("the live orders are listed",
          wait("document.querySelectorAll('#liveList .live-row').length === 3"),
          b.evaluate("document.getElementById('liveList').innerText"))
    check("the best sellers are listed",
          wait("document.querySelector('#popularBody').innerText.indexOf('Brew 00') > -1"))
    check("sales today match the paid bills",
          b.evaluate("document.getElementById('today-revenue').textContent.replace(/,/g, '')")
          == str(int(round(float(mysql_shim._DB.execute(
              "SELECT SUM(total_amount) FROM bills").fetchone()[0])))),
          b.evaluate("document.getElementById('today-revenue').textContent"))

    b.evaluate("""
        (function () {
            var s = document.getElementById('dashPeriod');
            s.value = 'week';
            s.dispatchEvent(new Event('change', {bubbles: true}));
        }())
    """)
    check("switching to seven days relabels the figures",
          wait("document.querySelector('#kpiSales .kpi__label').textContent"
               ".indexOf('last 7 days') > -1"))
    check("and the chart follows",
          wait("document.querySelector('.seg [data-period=week]')"
               ".getAttribute('aria-pressed') === 'true'")
          and wait("document.querySelectorAll('#curveSvg .curve__x text').length >= 6"))
    check("and the best sellers say so",
          b.evaluate("document.getElementById('popularTitle').textContent")
          == "Popular this week")
    open_page("/dashboard")
    check("the period is kept for next time",
          wait("document.getElementById('dashPeriod').value === 'week'"))

    # =================================================================
    print("\n=== 5b. The team page filters in place ===")
    # =================================================================
    for full, user, role in (("Sam Cashier", "samc", "cashier"),
                             ("Mona Manager", "mona", "manager")):
        seed.post("/users/add", data={
            "full_name": full, "username": user, "role": role,
            "phone_number": "", "password": "password123",
            "_csrf_token": csrf()}, follow_redirects=True)
    open_page("/users")

    def visible_people():
        return b.evaluate("""
            Array.prototype.slice.call(
                document.querySelectorAll('#teamRows tr[data-user-id]'))
                .filter(function (row) { return row.offsetParent !== null; })
                .map(function (row) { return row.getAttribute('data-username'); })
                .join(',')
        """)

    check("everyone is listed to begin with",
          set(visible_people().split(",")) == {"rae", "samc", "mona"},
          visible_people())
    b.evaluate("""
        (function () {
            var s = document.getElementById('teamRole');
            s.value = 'manager';
            s.dispatchEvent(new Event('change', {bubbles: true}));
        }())
    """)
    time.sleep(0.2)
    check("a role narrows it to that role", visible_people() == "mona",
          visible_people())
    b.evaluate("""
        (function () {
            var s = document.getElementById('teamRole');
            s.value = '';
            s.dispatchEvent(new Event('change', {bubbles: true}));
            var q = document.getElementById('teamSearch');
            q.value = 'sam';
            q.dispatchEvent(new Event('input', {bubbles: true}));
        }())
    """)
    time.sleep(0.2)
    check("and typing narrows it by name", visible_people() == "samc",
          visible_people())
    check("with no 'nobody matches' while somebody does",
          b.evaluate("document.getElementById('teamNone').offsetParent === null"))
    b.evaluate("""
        (function () {
            var q = document.getElementById('teamSearch');
            q.value = 'zzzz';
            q.dispatchEvent(new Event('input', {bubbles: true}));
        }())
    """)
    time.sleep(0.2)
    check("and says so when nobody does",
          visible_people() == ""
          and b.evaluate("document.getElementById('teamNone').offsetParent !== null"))

    # The same on a phone, where each person is a card.
    size(390, 844, phone=True)
    open_page("/users")
    b.evaluate("""
        (function () {
            var q = document.getElementById('teamSearch');
            q.value = 'mona';
            q.dispatchEvent(new Event('input', {bubbles: true}));
        }())
    """)
    time.sleep(0.2)
    check("a phone's cards filter the same way", visible_people() == "mona",
          visible_people())
    check("with no stray 'nobody matches' under them",
          b.evaluate("document.getElementById('teamNone').offsetParent === null"))
    size(1440, 900)

    # =================================================================
    print("\n=== 6. On a phone ===")
    # =================================================================
    size(390, 844, phone=True)
    open_page("/foods")
    bar = rect(".topbar")
    check("the header is one row", bar["height"] <= 70, bar)
    check("nothing pushes the page sideways",
          b.evaluate("document.documentElement.scrollWidth") <= 391)
    check("search is an icon here",
          style(".topbar-search", "display") == "none"
          and style("#topSearchOpen", "display") != "none")
    for sel in ("#navToggle", ".topbar-brand", "#topSearchOpen",
                "#orderStatusFab", "#profileTrigger"):
        box = rect(sel)
        check("%s is on the screen" % sel,
              box["left"] >= 0 and box["right"] <= 390 and box["width"] > 20, box)

    b.evaluate("document.getElementById('topSearchOpen').click()")
    time.sleep(0.4)
    check("the icon opens search across the header",
          style(".topbar-search", "display") != "none"
          and rect(".topbar-search")["width"] >= 370
          and b.evaluate("document.activeElement.id") == "topSearchInput",
          rect(".topbar-search"))
    b.evaluate("document.getElementById('topSearchBack').click()")
    time.sleep(0.3)
    check("and the arrow closes it", style(".topbar-search", "display") == "none")

    b.evaluate("document.getElementById('navToggle').click()")
    time.sleep(0.5)
    check("the menu button opens the drawer, not the rail",
          b.evaluate("document.querySelector('.app-shell').classList.contains('nav-open')")
          and not root_has("guide-full")
          and style("#appSidebar", "position") == "fixed")
    check("with the cafe's name at its head",
          style(".sidebar-brand", "display") != "none")
    b.evaluate("document.getElementById('navToggle').click()")
    time.sleep(0.4)

    b.evaluate("window.scrollTo(0, 900)")
    time.sleep(0.5)
    check("scrolled, the header stays, as glass",
          -1 <= rect(".topbar")["top"] <= 1 and root_has("is-scrolled"))

    # =================================================================
    print("\n=== 7. The profile menu's page, and search boxes, in glass ===")
    # =================================================================
    size(1440, 900)
    b.call("Emulation.setFocusEmulationEnabled", enabled=True)

    def current_items():
        return b.evaluate("""
            [].map.call(document.querySelectorAll('#profileDropdown .is-current'),
                function (a) { return a.getAttribute('href') + '|' +
                                      a.getAttribute('aria-current'); })
        """) or []

    def only_inside(shadow):
        """Every shadow drawn inside the box: glass, nothing cast outside."""
        return bool(shadow) and shadow != "none" \
            and shadow.count("inset") == shadow.count("rgb")

    open_page("/settings/tax")
    check("on Tax & Discount, the profile menu marks Tax & Discount",
          current_items() == ["/settings/tax|page"], current_items())
    look = b.evaluate("""
        (function () {
            var s = getComputedStyle(document.querySelector('#profileDropdown .is-current'));
            return {image: s.backgroundImage, shadow: s.boxShadow, lift: s.transform};
        }())
    """)
    check("in glass: lit from above", "gradient" in look["image"], look["image"])
    check("with its edges lit and shaded, and no shadow cast",
          only_inside(look["shadow"]), look["shadow"])
    check("and not lifted", look["lift"] in ("none", ""), look["lift"])

    b.evaluate("window.Instant.visit('%s/settings/packing', {})" % BASE, False)
    wait("location.pathname === '/settings/packing'")
    time.sleep(0.4)
    check("the mark moves with the page, without a reload",
          current_items() == ["/settings/packing|page"], current_items())
    b.evaluate("window.Instant.visit('%s/dashboard', {})" % BASE, False)
    wait("location.pathname === '/dashboard'")
    time.sleep(0.4)
    check("and a page the menu does not lead to marks nothing",
          current_items() == [], current_items())

    check("the top bar's search is glass with no shadow under it",
          only_inside(style(".topbar-search__field", "boxShadow"))
          and only_inside(style(".topbar-search__go", "boxShadow")),
          style(".topbar-search__field", "boxShadow"))
    b.evaluate("document.getElementById('topSearchInput').focus()")
    time.sleep(0.4)
    ring = style(".topbar-search__box", "boxShadow")
    check("typing in it draws a ring round the whole bar, and no glow",
          ring.count("rgb") == 1 and "0px 0px 0px 3px" in ring, ring)
    b.evaluate("document.getElementById('topSearchInput').blur()")

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
