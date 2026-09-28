"""
The table-ordering switch, and the notices, in two real browsers.

 - Switching table ordering off from the profile menu redraws the switch
   at once as a red Off, from the server's answer - not from the next
   page, which another worker may draw from a cafe row it remembered
   from before (live, the switch still read a green On).
 - What was said floats under the header as one notice, and is gone
   after a second - it used to be printed twice, once as bare text that
   never went.
 - A teammate signed in elsewhere is told, and their switch follows.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/table_switch_browser_test.py
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
os.environ["SECRET_KEY"] = "table-switch-browser-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
app.config["TESTING"] = False

PORT = 5857
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


print("\n=== 0. A cafe, its owner and a cashier ===")
seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Switch Cafe", "full_name": "Sid Owner", "username": "sid",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Coffee", "description": "",
                                   "_csrf_token": csrf()})
cat = re.search(r'<option value="(\d+)">', seed.get("/foods/add").get_data(as_text=True)).group(1)
seed.post("/foods/add", data={"food_name": "Latte", "category_id": cat, "price": "100",
                              "quantity": "20", "minimum_stock": "1", "description": "",
                              "diet": "veg", "_csrf_token": csrf()})
seed.post("/users/add", data={"full_name": "Cal Cashier", "username": "cal",
                              "role": "cashier", "phone_number": "",
                              "password": "password123", "_csrf_token": csrf()})
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


def wait(b, expression, what, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if b.evaluate(expression):
                return True
        except RuntimeError:
            pass
        time.sleep(0.1)
    raise AssertionError("timed out waiting for " + what)


def sign_in(b, user, path):
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1440, height=900,
           deviceScaleFactor=1, mobile=False)
    b.call("Page.navigate", url=BASE + "/login")
    wait(b, "document.readyState === 'complete' && !!document.getElementById('signInForm')",
         "the sign-in page")
    b.evaluate("""(function(){var f=document.getElementById('signInForm');
        f.querySelector('[name=cafe_name]').value='Switch Cafe';
        f.querySelector('[name=username]').value=%s;
        f.querySelector('[name=password]').value='password123';
        f.querySelector('[type=submit]').click(); return 1;}())""" % json.dumps(user))
    wait(b, "!!document.getElementById('page-view')", "the app")
    b.call("Page.navigate", url=BASE + path)
    wait(b, "document.readyState === 'complete' && !!document.getElementById('qrSwitch')", path)
    time.sleep(0.6)


def state(b):
    return json.loads(b.evaluate("""JSON.stringify({
        text: document.querySelector('.qr-switch__state').textContent,
        on: document.querySelector('.qr-switch__state').classList.contains('is-on'),
        checked: document.getElementById('qrSwitch').getAttribute('aria-checked'),
        toasts: [].map.call(document.querySelectorAll('#toastHost .toast'),
                            function (t) { return t.className + ' | ' + t.textContent; })
    })"""))


owner = cdp.Browser(BROWSER)
team = cdp.Browser(BROWSER)
try:
    sign_in(owner, "sid", "/orders/add")
    sign_in(team, "cal", "/billing")

    print("\n=== 1. Switching it off ===")
    check("it starts on, in green", state(owner)["text"] == "On" and state(owner)["on"])
    owner.evaluate("document.getElementById('profileTrigger').click(), true")
    time.sleep(0.3)
    owner.evaluate("document.getElementById('qrSwitch').click(), true")
    wait(owner, "document.querySelector('.qr-switch__state').textContent === 'Off'", "Off")
    after = state(owner)
    check("the switch redraws at once as Off, not green",
          after["text"] == "Off" and not after["on"] and after["checked"] == "false", after)
    check("drawn in red",
          owner.evaluate("getComputedStyle(document.querySelector('.qr-switch__state')).color")
          == "rgb(225, 97, 79)")
    check("without the page being reloaded",
          owner.evaluate("location.pathname") == "/orders/add")
    check("one notice floats under the header, marked as switched off",
          len(after["toasts"]) == 1 and "toast--off" in after["toasts"][0]
          and "counter" in after["toasts"][0], after["toasts"])
    check("and nothing is printed into the page itself",
          owner.evaluate("(document.getElementById('page-view').textContent"
                         ".match(/ordering is off/g) || []).length") == 0)
    time.sleep(1.6)
    check("the notice is gone after a second",
          owner.evaluate("document.querySelectorAll('#toastHost .toast').length") == 0)
    check("the cafe really stopped taking table orders",
          mysql_shim._DB.execute("SELECT qr_ordering FROM cafes").fetchone()[0] == 0)

    print("\n=== 2. A teammate is told ===")
    # Their page asks the order-status feed on its own every half minute;
    # coming back to the tab asks at once, which is what this does.
    team.evaluate("document.dispatchEvent(new Event('visibilitychange')), true")
    wait(team, "document.querySelectorAll('#toastHost .toast').length > 0", "the teammate's notice")
    told = state(team)
    check("a cashier elsewhere gets a notice that it was turned off",
          told["toasts"] and "turned off" in told["toasts"][0]
          and "toast--off" in told["toasts"][0], told)
    check("and their switch follows", told["text"] == "Off" and not told["on"], told)

    print("\n=== 3. And back on ===")
    team.evaluate("document.getElementById('profileTrigger').click(), true")
    time.sleep(0.3)
    team.evaluate("document.getElementById('qrSwitch').click(), true")
    wait(team, "document.querySelector('.qr-switch__state').textContent === 'On'", "On")
    now = state(team)
    check("switched on again, it reads a green On, with a green notice",
          now["on"] and now["text"] == "On"
          and any("toast--on" in t for t in now["toasts"]), now)
    owner.evaluate("document.dispatchEvent(new Event('visibilitychange')), true")
    wait(owner, "document.querySelector('.qr-switch__state').textContent === 'On'", "owner told")
    check("and the owner hears it too", "turned back on" in " ".join(state(owner)["toasts"]))
finally:
    for b in (owner, team):
        try:
            b.close()
        except Exception:               # noqa: BLE001
            pass

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
