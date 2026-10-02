"""
Saves shown at once and sent behind, in a real browser.

Every save is held back 1.2 seconds here - about what the live site takes
to save and draw the next page - and the checks are on what a person
sees meanwhile:

 - Add Category puts Categories on screen at once; the new row arrives
   when the save lands, and a quiet "Saving..." shows while it is away.
 - Delete takes the row away at once; switching a teammate off shows
   them Inactive at once. Both stick.
 - A save the server refuses puts things back: the form returns with what
   was typed and why; a row comes back; a switch flips back.
 - Saves go in the order they were made.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/save_queue_browser_test.py
"""
import json
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "save-queue-browser-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("app").setLevel(logging.CRITICAL)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
app.config["TESTING"] = False
app.logger.disabled = True

PORT = 5859
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
          ("" if condition else "\n          -> %s" % (detail,)))


SLOW = 1.2
BREAK = {"path": None}      # a save the server falls over on, when set


@app.before_request
def _like_the_live_site():
    from flask import request
    if request.method == "POST" and request.path != "/login":
        time.sleep(SLOW)
        if BREAK["path"] and request.path.startswith(BREAK["path"]):
            raise RuntimeError("the database went away mid-save")


def db(sql):
    return [tuple(r) for r in mysql_shim._DB.execute(sql).fetchall()]


print("\n=== 0. A cafe ===")
seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Queue Cafe", "full_name": "Quin Owner", "username": "quin",
    "phone_number": "", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as sess:
        return sess.get("_csrf_token", "")


for name in ("Coffee", "Snacks", "Soon Gone", "Stays Put"):
    seed.post("/categories/add", data={"category_name": name, "description": "",
                                       "_csrf_token": csrf()})
seed.post("/users/add", data={"full_name": "Tia Team", "username": "tia",
                              "role": "cashier", "phone_number": "",
                              "password": "Brew-Latte-42", "_csrf_token": csrf()})
mysql_shim.skip_tour()

threading.Thread(
    target=lambda: app.run(host="127.0.0.1", port=PORT, threaded=True,
                           use_reloader=False, debug=False),
    daemon=True).start()
for _ in range(80):
    try:
        urllib.request.urlopen(BASE + "/healthz", timeout=1).read()
        break
    except Exception:                   # noqa: BLE001
        time.sleep(0.25)
print("  server up on %s" % BASE)

b = cdp.Browser(BROWSER)


def wait(expression, what, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if b.evaluate(expression):
                return True
        except RuntimeError:
            pass
        time.sleep(0.03)
    raise AssertionError("timed out waiting for " + what)


def seconds_until(expression, what, timeout=15):
    started = time.time()
    wait(expression, what, timeout)
    return time.time() - started


def go(path, ready):
    b.evaluate("window.Instant.visit(%s, {}), true" % json.dumps(BASE + path))
    wait("location.pathname === %s && %s" % (json.dumps(path), ready), path)


def row_shown(text):
    return b.evaluate("""[].some.call(document.querySelectorAll('#page-view tr[data-row-key]'),
        function (r) { return !r.hidden && r.textContent.indexOf(%s) !== -1; })""" % json.dumps(text))


try:
    b.call("Page.enable")
    b.call("Page.addScriptToEvaluateOnNewDocument", source="""
        window.__errors = [];
        window.addEventListener('error', function (e) { window.__errors.push(e.message); });
    """)
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
           deviceScaleFactor=1, mobile=False)
    b.call("Page.navigate", url=BASE + "/login")
    wait("!!document.getElementById('signInForm')", "the sign-in page")
    b.evaluate("""(function(){var f=document.getElementById('signInForm');
        f.querySelector('[name=cafe_name]').value='Queue Cafe';
        f.querySelector('[name=username]').value='quin';
        f.querySelector('[name=password]').value='Brew-Latte-42';
        f.querySelector('[type=submit]').click(); return 1;}())""")
    wait("!!document.getElementById('page-view')", "the app")
    b.call("Page.navigate", url=BASE + "/categories")
    wait("document.readyState === 'complete' && !!document.querySelector('[data-row-key]')",
         "Categories")
    b.evaluate("window.confirm = function () { return true; }; true")
    time.sleep(1.2)

    print("\n=== 1. Adding a category ===")
    go("/categories/add", "!!document.querySelector('#page-view form[data-then]')")
    time.sleep(1.0)                     # typing - and Categories fetched ahead
    b.evaluate("""(function(){var f=document.querySelector('#page-view form[data-then]');
        f.querySelector('[name=category_name]').value='Desserts';
        f.querySelector('[type=submit]').click(); return 1;}())""")
    shown = seconds_until("location.pathname === '/categories'", "Categories")
    check("Categories is on screen at once, not after the save",
          shown < 0.4, "%.2fs" % shown)
    time.sleep(0.45)
    check("with a quiet 'Saving' while it is away",
          not b.evaluate("document.getElementById('saveState').hidden"))
    arrived = seconds_until("document.getElementById('page-view').innerText.indexOf('Desserts') !== -1",
                            "the new row")
    check("the new row arrives when the save lands", arrived < SLOW + 1.0, "%.2fs" % arrived)
    check("and it is saved", db("SELECT COUNT(*) FROM categories WHERE category_name = 'Desserts'") == [(1,)])
    time.sleep(0.3)
    check("'Saving' goes once nothing is on its way",
          b.evaluate("document.getElementById('saveState').hidden"))

    print("\n=== 2. Deleting one ===")
    b.evaluate("""[].filter.call(document.querySelectorAll('#page-view tr[data-row-key]'),
        function (r) { return r.textContent.indexOf('Soon Gone') !== -1; })[0]
        .querySelector('form[data-optimistic=remove] [type=submit]').click(), true""")
    gone = seconds_until("!(%s)" % """[].some.call(document.querySelectorAll('#page-view tr[data-row-key]'),
        function (r) { return !r.hidden && r.textContent.indexOf('Soon Gone') !== -1; })""", "the row gone")
    check("the row goes at once", gone < 0.3, "%.2fs" % gone)
    time.sleep(SLOW + 0.5)
    check("and it is deleted, and stays gone",
          db("SELECT COUNT(*) FROM categories WHERE category_name = 'Soon Gone'") == [(0,)]
          and not row_shown("Soon Gone"))

    print("\n=== 3. A save the server refuses ===")
    go("/categories/add", "!!document.querySelector('#page-view form[data-then]')")
    time.sleep(1.0)
    b.evaluate("""(function(){var f=document.querySelector('#page-view form[data-then]');
        var box=f.querySelector('[name=category_name]'); box.value='Coffee';
        box.dispatchEvent(new Event('input', {bubbles:true}));
        f.querySelector('[type=submit]').click(); return 1;}())""")
    wait("location.pathname === '/categories'", "Categories, ahead of the answer")
    back = seconds_until("location.pathname === '/categories/add'", "back on the form")
    check("refused, it comes back to the form", back < SLOW + 1.0, "%.2fs" % back)
    time.sleep(0.3)
    check("with what was typed still in it",
          b.evaluate("document.querySelector('#page-view [name=category_name]').value") == "Coffee")
    said = b.evaluate("document.getElementById('toastHost').innerText")
    check("and why", "already have a category called 'Coffee'" in said, said)
    check("and no word left over from an earlier save", "deleted" not in said.lower(), said)
    check("nothing was added", db("SELECT COUNT(*) FROM categories WHERE category_name = 'Coffee'") == [(1,)])

    print("\n=== 4. When the server falls over ===")
    go("/categories", "!!document.querySelector('#page-view tr[data-row-key]')")
    time.sleep(1.2)
    BREAK["path"] = "/categories/delete/"
    b.evaluate("""[].filter.call(document.querySelectorAll('#page-view tr[data-row-key]'),
        function (r) { return r.textContent.indexOf('Stays Put') !== -1; })[0]
        .querySelector('form[data-optimistic=remove] [type=submit]').click(), true""")
    wait("!(%s)" % """[].some.call(document.querySelectorAll('#page-view tr[data-row-key]'),
        function (r) { return !r.hidden && r.textContent.indexOf('Stays Put') !== -1; })""", "the row gone")
    back = seconds_until("""[].some.call(document.querySelectorAll('#page-view tr[data-row-key]'),
        function (r) { return !r.hidden && r.textContent.indexOf('Stays Put') !== -1; })""", "the row back")
    check("a delete that did not happen puts the row back", back < SLOW + 1.0, "%.2fs" % back)
    check("and says it was not saved",
          b.evaluate("document.getElementById('toastHost').innerText") != "")
    BREAK["path"] = None
    check("the category is still there",
          db("SELECT COUNT(*) FROM categories WHERE category_name = 'Stays Put'") == [(1,)])

    print("\n=== 5. Switching a teammate off, and a switch that did not take ===")
    go("/users", "!!document.querySelector('#page-view form.team-toggle')")
    time.sleep(0.8)
    pill = "document.querySelector('#page-view tr[data-username=\"tia\"] .team-pill').textContent.trim()"
    b.evaluate("document.querySelector('#page-view tr[data-username=\"tia\"] form.team-toggle [type=submit]').click(), true")
    flipped = seconds_until("%s === 'Inactive'" % pill, "Inactive")
    check("the teammate shows Inactive at once", flipped < 0.3, "%.2fs" % flipped)
    time.sleep(SLOW + 0.8)
    check("and is switched off, and stays shown so",
          db("SELECT is_active FROM users WHERE username = 'tia'") == [(0,)]
          and b.evaluate(pill) == "Inactive")
    BREAK["path"] = "/users/"
    b.evaluate("document.querySelector('#page-view tr[data-username=\"tia\"] form.team-toggle [type=submit]').click(), true")
    wait("%s === 'Active'" % pill, "Active at once")
    back = seconds_until("%s === 'Inactive'" % pill, "flipped back")
    check("a switch the server did not take flips back", back < SLOW + 1.0, "%.2fs" % back)
    BREAK["path"] = None
    check("and nothing changed", db("SELECT is_active FROM users WHERE username = 'tia'") == [(0,)])

    print("\n=== 6. In the order they were made ===")
    go("/categories", "!!document.querySelector('#page-view tr[data-row-key]')")
    time.sleep(1.2)
    b.evaluate("""(function () {
        var rows = [].filter.call(document.querySelectorAll('#page-view tr[data-row-key]'),
            function (r) { return /Snacks|Desserts/.test(r.textContent); });
        rows.forEach(function (r) {
            r.querySelector('form[data-optimistic=remove] [type=submit]').click();
        });
        return rows.length; }())""")
    time.sleep(0.45)
    check("two at once: both go from the screen, and 'Saving' counts them",
          not row_shown("Snacks") and not row_shown("Desserts")
          and "2" in b.evaluate("document.getElementById('saveState').innerText"),
          b.evaluate("document.getElementById('saveState').innerText"))
    time.sleep(2 * SLOW + 1.0)
    check("both are deleted, one after the other",
          db("SELECT COUNT(*) FROM categories WHERE category_name IN ('Snacks', 'Desserts')") == [(0,)])

    check("no script errors along the way",
          b.evaluate("JSON.stringify(window.__errors)") == "[]",
          b.evaluate("JSON.stringify(window.__errors)"))
finally:
    try:
        b.close()
    except Exception:                   # noqa: BLE001
        pass

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
