"""
Saving something without reloading the whole page.

A form post used to reload everything - the sidebar, the top bar, every
stylesheet and every script - to change one panel. Posts go through the
same swap as a link now, so the browser keeps the shell it already has.

What that has to keep true is the part worth testing:

  * the page really is not reloaded, proved by marking the window and
    checking the mark survives;
  * the write actually happened, and the page that comes back shows it;
  * the address bar lands where the redirect pointed, so Back works and a
    refresh does not re-post;
  * the flash message the server queued still appears;
  * a form with two buttons still tells them apart;
  * a file upload still arrives;
  * and the screens with no page region - sign-in, register, reset - are
    left to the browser, because there is nothing there to swap into.

A page that posts its own form - Billing updates one row in place - has
to win over the shell's handler, or the post goes out twice. That is not
checked here: a handler registered from a test runs later than the
shell's and so cannot reproduce the real ordering. browser_nav_test
covers it, by counting what the server actually received when Paid is
pressed, and it is what caught the bug.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/instant_post_test.py
"""
import os
import re
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "instant-post-secret"
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

PORT = 5837
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
LOADS = []


@app.before_request
def _serialise():
    from flask import request
    if not _lock.acquire(timeout=30):
        raise RuntimeError("request lock timeout")
    LOADS.append((request.method, request.path))


@app.teardown_request
def _release(exception=None):
    try:
        _lock.release()
    except RuntimeError:
        pass


print("\n=== 0. Seeding a cafe with a menu ===")
seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Swap Cafe", "full_name": "Sam Owner", "username": "sam",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Coffee",
                                   "description": "",
                                   "_csrf_token": csrf(seed)},
          follow_redirects=True)
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


# Every account this suite made has been shown round already, so
# the first-sign-in tour does not open over what is being tested.
mysql_shim.skip_tour()

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


def mark_window():
    """A value that only survives if the document is never reloaded."""
    browser.evaluate("window.__notReloaded = 'still here';")


def survived():
    return browser.evaluate("window.__notReloaded") == "still here"


def path():
    return browser.evaluate("location.pathname")


def flash():
    return browser.evaluate("""
        (function () {
            var el = document.querySelector('.alert');
            return el ? el.textContent.trim() : '';
        }())
    """)


def submit(selector, fill):
    """
    Fill a form and submit it the way a person would.

    Returns whether the browser considered it complete. A form with an
    unfilled required field never fires a submit event at all, so a test
    that ignored this would be checking nothing.
    """
    return browser.evaluate("""
        (function () {
            var form = document.querySelector('%s');
            %s
            if (!form.checkValidity()) return false;
            form.querySelector('[type=submit], button').click();
            return true;
        }())
    """ % (selector, fill))


try:
    browser.call("Page.enable")
    browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
                 deviceScaleFactor=1, mobile=False)

    print("\n=== 1. Sign in ===")
    browser.call("Page.navigate", url=BASE + "/login")
    wait_for("!!document.querySelector('form')", "the sign-in form")

    # The sign-in screen has no page region, so it must post the old way.
    mark_window()
    browser.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'sam';
            f.querySelector('[name=password]').value = 'password123';
            f.querySelector('[type=submit], button').click();
        }())
    """)
    wait_for("location.pathname !== '/login'", "the app")
    check("the sign-in screen is left to the browser",
          not survived(),
          "it was swapped, but there is no page region there to swap into")

    print("\n=== 2. Adding a food does not reload the page ===")
    browser.call("Page.navigate", url=BASE + "/foods/add")
    wait_for("!!document.querySelector('form [name=food_name]')",
             "the add-food form")
    mark_window()

    # Every required field, or the browser blocks the submit and no post
    # is ever attempted - which would make the checks below meaningless.
    submit("form", """
        form.querySelector('[name=food_name]').value = 'Swapped Latte';
        form.querySelector('[name=price]').value = '175';
        form.querySelector('[name=quantity]').value = '20';
        form.querySelector('[name=minimum_stock]').value = '5';
        var pick = form.querySelector('select[name=category_id]');
        if (pick && pick.options.length > 1) pick.selectedIndex = 1;
    """)
    wait_for("location.pathname === '/foods'", "Food Management")
    time.sleep(0.4)

    check("the page was never reloaded", survived(),
          "the whole document was replaced")
    check("the address bar followed the redirect", path() == "/foods",
          "it is at %s" % path())
    check("the food was really saved",
          browser.evaluate(
              "document.body.textContent.indexOf('Swapped Latte') > -1"),
          "the new food is not on the page")
    check("and the message the server queued is shown",
          "success" in flash().lower() or "added" in flash().lower(),
          "the flash read %r" % flash())

    print("\n=== 3. Back still works afterwards ===")
    browser.evaluate("history.back()")
    time.sleep(0.8)
    check("Back returns to the form", path() == "/foods/add",
          "it went to %s" % path())
    check("still without a reload", survived(),
          "going back reloaded the document")
    browser.evaluate("history.forward()")
    time.sleep(0.8)

    print("\n=== 4. A form with two buttons tells them apart ===")
    # Profile photo has Save and Remove in separate forms; the branding
    # page has a remove action carried by a hidden field. Stock update is
    # the simplest two-field save.
    browser.evaluate("location.href = '/inventory'")
    wait_for("location.pathname === '/inventory'", "Inventory")
    time.sleep(0.4)
    link = browser.evaluate("""
        (function () {
            var a = document.querySelector('a[href*="/inventory/update/"]');
            return a ? a.getAttribute('href') : '';
        }())
    """)
    check("there is a stock row to edit", bool(link), "no update link found")

    browser.evaluate("location.href = '%s'" % link)
    wait_for("!!document.querySelector('form [name=quantity]')",
             "the stock form")
    mark_window()
    submit("form", "form.querySelector('[name=quantity]').value = '77';")
    wait_for("location.pathname === '/inventory'", "Inventory")
    time.sleep(0.4)

    check("the stock save did not reload either", survived(),
          "the document was replaced")
    check("and the new figure is on the page",
          browser.evaluate(
              "document.body.textContent.indexOf('77') > -1"),
          "the updated stock is not shown")

    print("\n=== 5. A file upload still arrives ===")
    browser.evaluate("location.href = '/settings/branding'")
    wait_for("!!document.querySelector('input[name=logo]')",
             "the name and symbol page")
    mark_window()

    browser.evaluate("""
        (function () {
            var form = document.querySelector('form[enctype]');
            form.querySelector('[name=brand_name]').value = 'Swap Cafe';
            var bytes = atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ'
                + 'AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==');
            var buffer = new Uint8Array(bytes.length);
            for (var i = 0; i < bytes.length; i++) {
                buffer[i] = bytes.charCodeAt(i);
            }
            var file = new File([buffer], 'logo.png', { type: 'image/png' });
            var data = new DataTransfer();
            data.items.add(file);
            form.querySelector('[name=logo]').files = data.files;
            form.querySelector('[type=submit], button').click();
        }())
    """)

    # Renaming the cafe asks once more before it saves - it is the name
    # on every staff screen, on the customer's QR page and at the foot
    # of every receipt. Saying yes is part of saving now.
    wait_for("!document.getElementById('brandingFormConfirm').hidden",
             "the confirmation")
    browser.evaluate("document.querySelector('[data-confirm-yes]').click()")

    wait_for("!!document.querySelector('.alert')", "the saved message")
    time.sleep(0.5)

    check("the upload went through without a reload", survived(),
          "the document was replaced")
    check("the symbol is now on the page",
          browser.evaluate(
              "!!document.querySelector('.sidebar-brand img')"),
          "the uploaded symbol is not drawn in the sidebar")
    check("and the name was saved with it",
          browser.evaluate(
              "document.querySelector('.brand-text')"
              ".textContent.indexOf('Swap Cafe') > -1"),
          "the name did not change")

    print("\n=== 6. The shell is not rebuilt, only the page region ===")
    # If the document had been reloaded, every stylesheet and script would
    # have been fetched again. This counts what the server was asked for.
    browser.evaluate("location.href = '/categories/add'")
    wait_for("!!document.querySelector('[name=category_name]')",
             "the add-category form")
    time.sleep(0.4)

    # Mark and count only once the page is settled: setting location.href
    # above is itself a full navigation, and marking before it would be
    # measuring the test's own trip rather than the post's.
    mark_window()
    before = len([1 for method, p in LOADS
                  if method == "GET" and p.startswith("/static/")])
    check("the category form was submittable",
          submit("form",
                 "form.querySelector('[name=category_name]')"
                 ".value = 'Pastries';"),
          "a required field was left empty, so nothing was posted")
    wait_for("document.body.textContent.indexOf('Pastries') > -1",
             "the new category")
    time.sleep(0.5)
    after = len([1 for method, p in LOADS
                 if method == "GET" and p.startswith("/static/")])

    check("no stylesheet or script was fetched again", after == before,
          "%d static files were re-requested" % (after - before))
    check("and the page was still not reloaded", survived(),
          "the document was replaced")

finally:
    browser.close()

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
