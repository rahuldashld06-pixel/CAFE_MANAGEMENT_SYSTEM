"""
The account screens' live help, and search's memory, in a real browser.

 - Typing a username that is in use says so at once, offers free ones
   and will not let the form go; picking one clears it.
 - The password meter reads Weak, Medium or Strong as the password is
   typed, and a password everyone guesses first is Weak however long.
 - Search remembers what was looked for, lets each one be deleted or all
   of them cleared, and its Back link goes back.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/account_fields_browser_test.py
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
os.environ["SECRET_KEY"] = "account-fields-browser-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
app.config["TESTING"] = False

PORT = 5853
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


print("\n=== 0. A cafe whose owner is called robin ===")
seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Field Cafe", "full_name": "Robin Owner", "username": "robin",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
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


def wait(expression, what, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if b.evaluate(expression):
                return True
        except RuntimeError:
            pass
        time.sleep(0.15)
    raise AssertionError("timed out waiting for " + what)


def type_into(selector, value):
    b.evaluate("""
        (function () {
            var el = document.querySelector(%s);
            el.focus();
            el.value = %s;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            return true;
        }())
    """ % (json.dumps(selector), json.dumps(value)))


def meter(selector):
    return json.loads(b.evaluate("""
        (function () {
            var field = document.querySelector(%s);
            var anchor = field.closest('.pw-field') || field;
            var box = anchor.parentNode;
            var m = box.querySelector('.pw-meter'), say = box.querySelector('.pw-meter__say');
            return JSON.stringify({level: m && m.dataset.level,
                                   word: say && (say.querySelector('b') || {}).textContent,
                                   said: say && say.textContent});
        }())
    """ % json.dumps(selector)))


def open_page(path, ready):
    b.call("Page.navigate", url=BASE + path)
    wait("document.readyState === 'complete' && " + ready, path)
    time.sleep(0.3)


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
           deviceScaleFactor=1, mobile=False)

    # =================================================================
    print("\n=== 1. A username in use, said while it is typed ===")
    # =================================================================
    open_page("/register", "!!document.querySelector('[data-username-check]')")
    type_into("#username", "robin")
    wait("(document.querySelector('.field-check') || {}).dataset && "
         "document.querySelector('.field-check').dataset.state === 'taken'",
         "the taken note")
    note = b.evaluate("document.querySelector('.field-check').textContent")
    check("it says the name is taken and to choose another",
          "already taken" in note and "choose another" in note, note)
    picks = json.loads(b.evaluate(
        "JSON.stringify([].map.call(document.querySelectorAll('.field-check button'),"
        " function (x) { return x.textContent; }))"))
    check("offering free ones to use instead",
          picks and all(p.startswith("Use robin") for p in picks), picks)
    check("and the form will not be sent with it",
          b.evaluate("!document.getElementById('username').checkValidity()"))

    b.evaluate("document.querySelector('.field-check button').click(), true")
    wait("document.querySelector('.field-check').dataset.state === 'free'",
         "the free note")
    check("picking one fills it in and says it is free",
          b.evaluate("document.getElementById('username').value") == picks[0][4:]
          and "free" in b.evaluate("document.querySelector('.field-check').textContent"))
    check("and the form may go again",
          b.evaluate("document.getElementById('username').checkValidity()"))

    type_into("#username", "ro")
    wait("document.querySelector('.field-check').dataset.state === 'bad'",
         "the too-short note")
    check("too short is said too, but does not block the name on its own",
          b.evaluate("document.getElementById('username').validationMessage") == "")

    # =================================================================
    print("\n=== 2. How strong the password is ===")
    # =================================================================
    field = "#password"
    for typed, level, word in (("abc", "1", "Too short"),
                               ("coffee12", "1", "Weak"),
                               ("password123", "1", "Weak"),
                               ("aaaaaaaaaaaa", "1", "Weak"),
                               ("Coffee123", "1", "Weak"),
                               ("Mocha4321", "2", "Medium"),
                               ("Coffee-Break-2026!", "3", "Strong")):
        type_into(field, typed)
        time.sleep(0.1)
        got = meter(field)
        check("%r reads %s" % (typed, word),
              got.get("level") == level and got.get("word") == word, got)
    check("a strong one says it is hard to guess",
          "hard to guess" in meter(field)["said"])
    type_into(field, "coffee12")
    check("a weak one says it is easy to guess and what would help",
          "easy to guess" in meter(field)["said"]
          and "numbers or symbols" in meter(field)["said"])
    type_into(field, "")
    check("nothing typed, nothing said",
          meter(field).get("level") == "0" and not meter(field).get("word"))

    # =================================================================
    print("\n=== 3. Inside the app ===")
    # =================================================================
    open_page("/login", "!!document.querySelector('form')")
    b.evaluate("""(function(){var f=document.querySelector('form');
        f.querySelector('[name=username]').value='robin';
        f.querySelector('[name=password]').value='password123';
        f.submit(); return 1;})()""")
    wait("!!document.getElementById('page-view')", "the app")
    open_page("/users/add", "!!document.querySelector('[data-username-check]')")
    type_into("input[name=username]", "robin")
    wait("document.querySelector('.field-check') && "
         "document.querySelector('.field-check').dataset.state === 'taken'",
         "the taken note on Add User")
    check("Add User says so too", True)
    type_into("input[name=password]", "Coffee-Break-2026!")
    check("and measures the password", meter("input[name=password]")["word"] == "Strong")

    # Reached the way people move about - swapped in without a reload, a
    # couple of pages along. The script's listener for that used to go
    # with the first page, so Add User arrived with no meter at all.
    for path in ("/foods", "/billing", "/users/add"):
        b.evaluate("window.Instant.visit(%s, {}), true" % json.dumps(BASE + path))
        wait("location.pathname === %s" % json.dumps(path), path)
        time.sleep(0.6)
    check("still there after moving between pages without a reload",
          b.evaluate("!!document.querySelector('#page-view .pw-meter')"
                     " && !!document.querySelector('#page-view .field-check')"))

    # =================================================================
    print("\n=== 4. Search remembers, and forgets when asked ===")
    # =================================================================
    open_page("/dashboard", "!!document.getElementById('page-view')")
    b.evaluate("localStorage.clear(), true")
    open_page("/search?q=latte", "!!document.getElementById('recentList')")
    open_page("/search?q=mocha", "!!document.getElementById('recentList')")

    def recent():
        return json.loads(b.evaluate(
            "JSON.stringify([].map.call(document.querySelectorAll('#recentList a'),"
            " function (a) { return a.textContent.trim(); }))"))

    check("what was searched is listed, newest first", recent() == ["mocha", "latte"],
          recent())
    b.evaluate("""
        [].filter.call(document.querySelectorAll('#recentList li'), function (li) {
            return li.textContent.trim() === 'latte';
        })[0].querySelector('button').click(), true
    """)
    check("one can be deleted", recent() == ["mocha"], recent())
    open_page("/search", "!!document.getElementById('recentList')")
    check("and stays deleted", recent() == ["mocha"], recent())
    b.evaluate("document.getElementById('recentClearAll').click(), true")
    check("Clear all empties the list and hides it",
          recent() == [] and b.evaluate("document.getElementById('searchRecent').hidden"))
    open_page("/search?q=scone", "!!document.getElementById('searchAgainClear')")
    check("the x in the box shows once something is searched",
          not b.evaluate("document.getElementById('searchAgainClear').hidden"))
    b.evaluate("document.getElementById('searchAgainClear').click(), true")
    wait("location.search === ''", "the emptied search")
    check("and empties the search", b.evaluate("document.getElementById('searchAgain').value") == "")

    open_page("/foods", "!!document.getElementById('page-view')")
    b.evaluate("document.getElementById('topSearchInput').value = 'latte';"
               "document.getElementById('topSearchInput').form.requestSubmit(); true")
    wait("location.pathname === '/search'", "the search page")
    time.sleep(0.4)
    b.evaluate("document.querySelector('.search-back').click(), true")
    wait("location.pathname === '/foods'", "going back")
    check("Back returns to the page the search was made from",
          b.evaluate("location.pathname") == "/foods")

    # The same kind of link is Cancel on every settings page. It once
    # both followed its href and stepped back, and so went nowhere.
    b.evaluate("""
        (function () {
            var a = document.createElement('a');
            a.href = '/settings/tax';
            document.getElementById('page-view').appendChild(a);
            a.click();
            return true;
        }())
    """)
    wait("location.pathname === '/settings/tax' && "
         "!!document.querySelector('#page-view [data-back]')", "Tax settings")
    time.sleep(0.3)
    b.evaluate("document.querySelector('#page-view [data-back]').click(), true")
    wait("location.pathname === '/foods'", "cancelling")
    time.sleep(0.5)
    check("Cancel on a settings page goes back where it came from, and stays",
          b.evaluate("location.pathname") == "/foods")

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
