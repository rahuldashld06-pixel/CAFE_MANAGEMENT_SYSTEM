"""
Real-browser tests for a café's clock settling itself.

Times are stored in UTC and read back on the café's own zone. Which zone
was a setting somebody had to go and find, and the report that came back
was that the kitchen still showed the wrong time — because of course it
did: nobody had been to look for the setting, and until they had, every
screen read UTC.

The browser already knows what clock it is on. So the first person to
sign in settles it, and the setting stays for anyone who wants to change
it on purpose. That only answers in a browser, because it is the browser
that knows.

The machine running this is on whatever clock it is on, so the zone is
overridden through the devtools protocol — otherwise this would pass in
one country and fail in another.

Run with:  python tests/clock_browser_test.py
"""
import os
import re
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "clock-browser-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5971
BASE = "http://127.0.0.1:%d" % PORT

# A long way from UTC in both directions, so a time that failed to
# convert cannot pass by looking close enough.
PRETEND = "Asia/Kolkata"

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


lock = threading.Lock()


@app.before_request
def _serialise():
    if not lock.acquire(timeout=40):
        raise RuntimeError("request lock timeout")


@app.teardown_request
def _release(exception=None):
    try:
        lock.release()
    except RuntimeError:
        pass


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Clock Cafe", "full_name": "Sam Owner", "username": "sam",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
seed.post("/categories/add", data={"category_name": "Coffee",
                                   "description": "",
                                   "_csrf_token": csrf(seed)},
          follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     seed.get("/foods/add").get_data(as_text=True)).group(1)
seed.post("/foods/add", data={
    "food_name": "Masala Chai", "category_id": category, "price": "70",
    "quantity": "40", "minimum_stock": "2", "description": "",
    "_csrf_token": csrf(seed)}, follow_redirects=True)
food = re.findall(r'id="quantity_(\d+)"',
                  seed.get("/orders/add").get_data(as_text=True))[0]
seed.post("/orders/add", data={"quantity_%s" % food: "1",
                               "_csrf_token": csrf(seed)},
          follow_redirects=True)

mysql_shim.skip_tour()


def same_clock(one, other):
    """
    Two names for the same clock.

    Browsers still report legacy aliases - a laptop in India says
    "Asia/Calcutta" where the list calls it "Asia/Kolkata". Both resolve,
    both give the same offset, and which spelling arrives is no business
    of this test. What matters is that the cafe ends up on the right
    clock.
    """
    if not one or not other:
        return False
    now = datetime.now(timezone.utc)
    return (now.astimezone(ZoneInfo(one)).utcoffset()
            == now.astimezone(ZoneInfo(other)).utcoffset())


def stored_zone():
    return mysql_shim._DB.execute(
        "SELECT timezone FROM cafes WHERE cafe_id = 1").fetchone()[0]


threading.Thread(target=lambda: app.run(host="127.0.0.1", port=PORT,
                                        threaded=True, use_reloader=False),
                 daemon=True).start()
for _ in range(160):
    try:
        urllib.request.urlopen(BASE + "/healthz", timeout=2).read()
        break
    except Exception:
        time.sleep(0.25)

browser_path = cdp.find_browser()
if not browser_path:
    print("SKIPPED: no browser")
    sys.exit(0)

b = cdp.Browser(browser_path)


def wait(expr, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if b.evaluate(expr):
                return True
        except RuntimeError:
            pass
        time.sleep(0.05)
    return False


def kitchen_time():
    rows = b.evaluate(
        "JSON.stringify(Array.prototype.map.call("
        "document.querySelectorAll('.kitchen-ticket__foot span'),"
        "function (s) { return s.textContent.trim(); }))")
    import json
    times = [t for t in json.loads(rows) if re.match(r"\d+ \w+,", t)]
    return times[0] if times else ""


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
           deviceScaleFactor=1, mobile=False)

    print("\n=== 1. A cafe that has never been asked ===")
    check("no clock is set to begin with",
          stored_zone() in (None, ""),
          "it already says %r, so this proves nothing" % stored_zone())

    # A laptop sitting in the cafe, wherever that is.
    b.call("Emulation.setTimezoneOverride", timezoneId=PRETEND)

    b.call("Page.navigate", url=BASE + "/login")
    wait("!!document.querySelector('form')")
    b.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'sam';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
        }())
    """)
    check("the app opens", wait("!!document.getElementById('page-view')"),
          "never got past the sign-in screen")

    print("\n=== 2. The browser settles it, without being asked ===")
    deadline = time.time() + 20
    while time.time() < deadline and stored_zone() in (None, ""):
        time.sleep(0.25)

    check("signing in sets the cafe's clock from the browser",
          same_clock(stored_zone(), PRETEND),
          "the database says %r, the browser is on %r - somebody still "
          "has to go and find a settings page" % (stored_zone(), PRETEND))

    print("\n=== 3. And the kitchen then agrees with the laptop ===")
    b.call("Page.navigate", url=BASE + "/kitchen")
    wait("!!document.querySelector('.kitchen-ticket')")
    time.sleep(2.0)

    shown = kitchen_time()
    here = datetime.now(timezone.utc).astimezone(ZoneInfo(PRETEND))

    check("the kitchen shows a time at all", bool(shown),
          "no time on any ticket")

    # A minute either side, because the order was placed seconds before
    # this and the clock may have ticked over in between.
    def reading(when):
        return when.strftime("%d %b, %I:%M %p")

    near = {reading(here + timedelta(minutes=n)) for n in (-1, 0, 1)}
    in_utc = reading(datetime.now(timezone.utc))

    check("and it is the laptop's time, not UTC",
          shown in near,
          "the ticket says %r; this laptop says %r and UTC says %r"
          % (shown, reading(here), in_utc))

    print("\n=== 4. A browser somewhere else does not move it ===")
    b.call("Emulation.setTimezoneOverride", timezoneId="America/New_York")
    b.call("Page.navigate", url=BASE + "/billing")
    wait("!!document.getElementById('page-view')")
    time.sleep(2.5)

    check("a cafe that has chosen keeps its clock",
          same_clock(stored_zone(), PRETEND),
          "opening the till on a laptop in another country moved the "
          "whole cafe to %r" % stored_zone())

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
