"""
Real-browser test for automatic printing.

Checks the parts that only a browser can answer: that placing an order
actually arms the kitchen ticket, that it waits the configured delay rather
than firing at once, that marking a bill paid prints the receipt with no
delay at all, and that nothing prints when the switches are off.

window.print() is intercepted rather than left to run, so the test records
what would have gone to the printer without a dialog stopping everything.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without one.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/auto_print_browser_test.py
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
os.environ["SECRET_KEY"] = "auto-print-browser-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5941
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
    "cafe_name": "Print Cafe", "full_name": "Boss", "username": "boss",
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
seed.post("/foods/add", data={
    "food_name": "Latte", "category_id": category, "price": "100",
    "quantity": "500", "description": "", "_csrf_token": csrf()},
    follow_redirects=True)


def set_printing(**form):
    form.setdefault("kot_delay", "2")
    form["_csrf_token"] = csrf()
    seed.post("/settings/printing", data=form, follow_redirects=True)


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


def watch_prints():
    """
    Record what gets sent to the printer instead of printing it.

    The print pages call window.print() on load. Left alone that opens a
    dialog which blocks the browser, so the frame's print call is replaced
    with a note of which document asked.
    """
    b.evaluate("""
        window.__printed = [];
        (function () {
            var realAppend = HTMLElement.prototype.appendChild;
            HTMLElement.prototype.appendChild = function (node) {
                var result = realAppend.call(this, node);
                if (node && node.tagName === 'IFRAME' && node.src) {
                    window.__printed.push({url: node.src, at: Date.now()});
                    node.addEventListener('load', function () {
                        try { node.contentWindow.print = function () {}; }
                        catch (e) { /* nothing to stub */ }
                    });
                }
                return result;
            };
        })();
        true
    """)


def printed():
    return json.loads(b.evaluate("JSON.stringify(window.__printed || [])"))


def sign_in():
    b.call("Page.navigate", url=BASE + "/login")
    wait("document.readyState==='complete' && !!document.querySelector('form')",
         "the login form")
    b.evaluate("""(function(){var f=document.querySelector('form');
        f.querySelector('[name=username]').value='boss';
        f.querySelector('[name=password]').value='password123';
        f.submit(); return 1;})()""")
    wait("!!document.getElementById('page-view')", "the app shell")


def place_order():
    b.evaluate("""
        (function () {
            var input = document.querySelector('.quantity-input');
            input.value = 1;
            input.dispatchEvent(new Event('input', {bubbles: true}));
            document.getElementById('orderForm').dispatchEvent(
                new Event('submit', {cancelable: true, bubbles: true}));
            return true;
        })()
    """)


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
           deviceScaleFactor=1, mobile=False)
    sign_in()

    print("\n=== 1. Off means nothing prints ===")
    set_printing(kot_delay="2")          # both switches omitted = off
    b.call("Page.navigate", url=BASE + "/orders/add")
    wait("!!document.getElementById('orderForm')", "the menu")
    watch_prints()
    place_order()
    time.sleep(5.0)
    check("placing an order prints nothing while the switch is off",
          printed() == [], "sent to the printer anyway: %s" % printed())

    print("\n=== 2. The kitchen ticket, after the delay ===")
    set_printing(auto_kot="on", kot_delay="2")
    b.call("Page.navigate", url=BASE + "/orders/add")
    wait("!!document.getElementById('orderForm')", "the menu")
    watch_prints()

    started = time.time()
    place_order()

    time.sleep(1.0)
    check("it has not printed a second after the order",
          printed() == [],
          "fired immediately, ignoring the delay: %s" % printed())

    deadline = time.time() + 12
    while time.time() < deadline and not printed():
        time.sleep(0.25)

    jobs = printed()
    check("it prints once the delay has passed", len(jobs) == 1,
          "%d print jobs: %s" % (len(jobs), jobs))
    if jobs:
        waited = (jobs[0]["at"] / 1000.0) - started
        check("it is the kitchen ticket, not the bill",
              "/kot" in jobs[0]["url"], "printed %s" % jobs[0]["url"])
        # The evidence, not just the verdict. This measured 0.1s once,
        # in a run where four other suites were timing out and a
        # leftover headless browser was competing for the machine - and
        # 0.1s cannot be squared with the check above it, which had just
        # proved nothing had printed a second in. The clocks were not
        # the answer: python's and the browser's agree here to a
        # millisecond. It has not been reproduced since, so rather than
        # guess at it, a recurrence now prints what it saw.
        check("and it waited roughly the configured two seconds",
              1.5 <= waited <= 6.0,
              "waited %.1fs - ordered at %.3f, printed at %.3f, "
              "all jobs: %s" % (waited, started, jobs[0]["at"] / 1000.0,
                                jobs))

    print("\n=== 3. The receipt, the moment Paid is pressed ===")
    set_printing(auto_bill="on", kot_delay="2")
    b.call("Page.navigate", url=BASE + "/billing")
    wait("!!document.querySelector('form.payment-toggle-form')",
         "a payable bill")
    watch_prints()

    started = time.time()
    b.evaluate("""
        document.querySelector('form.payment-toggle-form')
                .querySelector('button[type=submit]').click()
    """)

    deadline = time.time() + 12
    while time.time() < deadline and not printed():
        time.sleep(0.2)

    jobs = printed()
    check("marking a bill paid prints", len(jobs) >= 1,
          "nothing was sent to the printer")
    if jobs:
        waited = (jobs[0]["at"] / 1000.0) - started
        check("it is the bill, not the kitchen ticket",
              "/bill" in jobs[0]["url"], "printed %s" % jobs[0]["url"])
        check("and it does not wait - the customer is standing there",
              waited <= 3.0, "took %.1fs" % waited)

    print("\n=== 4. The two switches are independent ===")
    # Kitchen ticket on, receipt off: paying must not print a receipt.
    set_printing(auto_kot="on", kot_delay="2")
    b.call("Page.navigate", url=BASE + "/orders/add")
    wait("!!document.getElementById('orderForm')", "the menu")
    place_order()
    time.sleep(1.5)

    b.call("Page.navigate", url=BASE + "/billing")
    wait("!!document.querySelector('form.payment-toggle-form')", "a payable bill")
    watch_prints()
    b.evaluate("""
        document.querySelector('form.payment-toggle-form')
                .querySelector('button[type=submit]').click()
    """)
    time.sleep(4.0)
    receipts = [job for job in printed() if "/bill" in job["url"]]
    check("with the receipt switch off, paying prints no receipt",
          receipts == [], "printed a receipt anyway: %s" % receipts)

    print("\n=== 5. With a kitchen screen on, the till leaves it alone ===")
    # The till stands next to the customer and the kitchen screen stands
    # next to the cook. Both printing would put two tickets out for one
    # order; the till printing would put the only one in the wrong room.
    set_printing(auto_kot="on", kot_delay="2")

    # A kitchen screen checking in, without opening a second browser.
    seed.post("/api/kitchen/heartbeat", data={"_csrf_token": csrf()},
              headers={"X-Requested-With": "XMLHttpRequest"})

    b.call("Page.navigate", url=BASE + "/orders/add")
    wait("!!document.getElementById('orderForm')", "the menu")
    watch_prints()
    place_order()
    time.sleep(6.0)

    check("the till prints nothing while a kitchen screen is watching",
          printed() == [],
          "it printed at the counter anyway: %s" % printed())

    # And the ticket is not lost - it is sitting waiting for that screen.
    waiting = json.loads(seed.get("/api/kitchen/pending")
                         .get_data(as_text=True))
    check("the ticket is waiting for the kitchen instead",
          waiting.get("orders"),
          "nothing is waiting, so that order has no ticket coming: %s"
          % waiting)
    check("and the server agrees a kitchen screen is there",
          waiting.get("kitchen_watching") is True,
          "the till was told to stand down by something else entirely")

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
