"""
Real-browser tests for the tour given on somebody's first sign-in.

The offline suite checks that it is on the page and that the flag is kept
against the right person. None of that says it actually works: whether it
opens by itself, whether Next reaches the end, whether finishing it makes
it stay shut on the next page, and whether it can be found again
afterwards. Those only answer in a browser.

Run with:  python tests/tour_browser_test.py
"""
import os
import sys
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "tour-browser-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
PORT = 5951
BASE = "http://127.0.0.1:%d" % PORT

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


lock = threading.Lock()


@app.before_request
def _serialise():
    if not lock.acquire(timeout=30):
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
    "cafe_name": "Tour Cafe", "full_name": "Sam Owner", "username": "sam",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)

seed.post("/users/add", data={
    "full_name": "Ravi Till", "username": "ravi", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(seed)}, follow_redirects=True)

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
                return True
        except RuntimeError:
            pass
        time.sleep(0.15)
    return False


def open_now():
    return b.evaluate("!!document.getElementById('tour') "
                      "&& !document.getElementById('tour').hidden")


def showing():
    return b.evaluate(
        "(document.querySelector('.tour__step:not([hidden]) .tour__title')"
        " || {}).textContent || ''")


def sign_in(username):
    b.call("Page.navigate", url=BASE + "/logout")
    time.sleep(0.6)
    b.call("Page.navigate", url=BASE + "/login")
    wait("!!document.querySelector('form')", "the sign-in form")
    b.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = '%s';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
        }())
    """ % username)
    wait("location.pathname !== '/login'", "the app")


try:
    b.call("Page.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
           deviceScaleFactor=1, mobile=False)

    print("\n=== 1. It opens on a first sign-in, without being asked ===")
    sign_in("sam")
    check("the tour opens by itself",
          wait("!document.getElementById('tour').hidden",
               "the tour", 12),
          "somebody new is dropped into the app with nothing explained "
          "and no instructions left on the pages either")
    check("starting at the beginning", showing() == "Welcome",
          "it opened on %r" % showing())
    check("and the rest of the page cannot be scrolled behind it",
          b.evaluate("document.body.classList.contains('tour-open')"),
          "the page scrolls under the dialog")

    print("\n=== 2. It goes forwards and backwards ===")
    steps = b.evaluate("document.querySelectorAll('.tour__step').length")
    check("an owner is given every step", steps >= 6,
          "only %d steps" % steps)

    check("Back is dead on the first step",
          b.evaluate("document.getElementById('tourBack').disabled") is True,
          "Back would step off the front of it")

    b.evaluate("document.getElementById('tourNext').click()")
    time.sleep(0.4)
    second = showing()
    check("Next moves on", second and second != "Welcome",
          "it is still showing %r" % second)

    b.evaluate("document.getElementById('tourBack').click()")
    time.sleep(0.4)
    check("and Back comes home", showing() == "Welcome",
          "Back left it on %r" % showing())

    print("\n=== 3. The end of it closes it, for good ===")
    for _ in range(20):
        if b.evaluate(
                "document.getElementById('tourNext').textContent") == "Finish":
            break
        b.evaluate("document.getElementById('tourNext').click()")
        time.sleep(0.2)

    check("the last step offers to finish rather than go on",
          b.evaluate(
              "document.getElementById('tourNext').textContent") == "Finish",
          "it says %r on the last step"
          % b.evaluate("document.getElementById('tourNext').textContent"))

    b.evaluate("document.getElementById('tourNext').click()")
    time.sleep(1.2)
    check("finishing shuts it", not open_now(), "it is still open")

    b.call("Page.navigate", url=BASE + "/billing")
    wait("!!document.getElementById('page-view')", "the next page")
    time.sleep(1.5)
    check("and it stays shut on the next page opened", not open_now(),
          "it starts again on every page, which is worse than the "
          "paragraphs it replaced")

    print("\n=== 4. But it can be asked for again ===")
    b.evaluate("document.getElementById('tourReplay').click()")
    time.sleep(0.7)
    check("the profile menu opens it again", open_now(),
          "somebody who skipped it has no way back to it")
    check("from the beginning", showing() == "Welcome",
          "it reopened on %r" % showing())

    b.evaluate("document.getElementById('tourSkip').click()")
    time.sleep(0.8)
    check("and Skip shuts it too", not open_now(), "Skip did nothing")

    print("\n=== 5. Somebody on the till gets a shorter one ===")
    sign_in("ravi")
    check("it opens for them as well",
          wait("!document.getElementById('tour').hidden", "the tour", 12),
          "a new cashier is shown nothing at all")

    till_steps = b.evaluate("document.querySelectorAll('.tour__step').length")
    check("and it is shorter than the owner's", 0 < till_steps < steps,
          "cashier %d steps against the owner's %d" % (till_steps, steps))

    check("with nothing in it about managing staff",
          b.evaluate(
              "!document.body.innerText.includes('User Management adds')"),
          "they are walked through a screen they cannot open")

    print("\n=== 6. On a phone ===")
    b.call("Emulation.setDeviceMetricsOverride", width=390, height=844,
           deviceScaleFactor=2, mobile=True)
    time.sleep(0.8)

    check("it still fits the screen sideways",
          not b.evaluate("document.documentElement.scrollWidth "
                         "> window.innerWidth + 1"),
          "the tour pushes the page sideways on a phone")
    check("and the buttons are still reachable",
          b.evaluate(
              "document.getElementById('tourNext').getBoundingClientRect()"
              ".bottom <= window.innerHeight + 1"),
          "Next is off the bottom of the screen")

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
