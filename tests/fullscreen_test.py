"""
Filling the screen, and the manifest that makes it automatic.

A page cannot go full screen when it loads. Every browser requires a user
gesture first, or any page could take over the display the moment it
opened. So there are two mechanisms and this covers both:

  * the button in the top bar, which remembers. Once used on a device, the
    next visit fills the screen on the first tap anywhere - the earliest
    moment a browser allows it. Leaving full screen forgets the
    preference, so Escape means what it looks like it means.
  * the web manifest, which is what actually delivers "no browser bar at
    all, every time": installed to a home screen or a desktop, the site
    opens with no chrome. It is also the only route on an iPhone, where
    Safari has no Fullscreen API.

The real API is stubbed here. Headless Chrome will not give a test a
genuine full screen, and what is worth pinning down is the logic around
the call rather than the browser's own implementation of it.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.

Run with:  python tests/fullscreen_test.py
"""
import json
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "fullscreen-secret"
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

PORT = 5827
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


@app.before_request
def _serialise():
    if not _lock.acquire(timeout=30):
        raise RuntimeError("request lock timeout")


@app.teardown_request
def _release(exception=None):
    try:
        _lock.release()
    except RuntimeError:
        pass


print("\n=== 0. Seeding a cafe ===")
seed = app.test_client()
seed.post("/register", data={
    "cafe_name": "Bluebird Coffee House", "full_name": "Sam Owner",
    "username": "sam", "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
print("  seeded")


print("\n=== 1. The manifest, which is what makes it automatic ===")
# Checked on the server: an installed app is the only thing that opens
# with no browser chrome without anyone tapping anything.
anon = app.test_client()
response = anon.get("/manifest.webmanifest")
check("a signed-out browser can fetch it", response.status_code == 200,
      "got HTTP %s" % response.status_code)
check("and it is served as a manifest",
      response.headers.get("Content-Type", "").startswith(
          "application/manifest+json"),
      "content type is %r" % response.headers.get("Content-Type"))

manifest = json.loads(response.get_data(as_text=True))
check("it asks for a full screen", manifest["display"] == "fullscreen",
      "display is %r" % manifest["display"])
check("with standalone as the fallback",
      "standalone" in manifest.get("display_override", []),
      "display_override is %s" % manifest.get("display_override"))
check("it declares the two icon sizes browsers want to install",
      sorted(icon["sizes"] for icon in manifest["icons"])
      == ["192x192", "512x512"],
      "icons are %s" % [icon["sizes"] for icon in manifest["icons"]])
check("and they are maskable, so a launcher can crop them",
      all("maskable" in icon.get("purpose", "")
          for icon in manifest["icons"]),
      "purposes are %s" % [icon.get("purpose") for icon in manifest["icons"]])

for size in (192, 512):
    icon = anon.get("/static/icons/app-%d.png" % size)
    check("the %dpx icon is really there" % size,
          icon.status_code == 200 and icon.data[:8] == b"\x89PNG\r\n\x1a\n",
          "got HTTP %s, %d bytes" % (icon.status_code, len(icon.data)))

signed_in = app.test_client()
signed_in.post("/login", data={"username": "sam", "password": "password123"},
               follow_redirects=True)
mine = json.loads(
    signed_in.get("/manifest.webmanifest").get_data(as_text=True))
check("an installed copy carries the cafe's own name",
      mine["name"] == "Bluebird Coffee House",
      "it would install as %r" % mine["name"])
check("and a short name that stops at a word",
      mine["short_name"] == "Bluebird",
      "the home screen would read %r" % mine["short_name"])


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


# Headless Chrome will not hand out a real full screen, so the API is
# replaced with something that records what was asked of it and reports a
# matching state back. Installed before any page script runs.
# fullscreenElement is an accessor on Document.prototype, not an own
# property of the document, and redefining it on the instance throws -
# which silently abandoned the rest of the stub and let the real API
# through. Each piece is wrapped so a failure here is loud.
STUB = """
    (function () {
        window.__fs = { entered: 0, exited: 0, on: false, installed: [] };

        function fire() {
            document.dispatchEvent(new Event('fullscreenchange'));
        }

        try {
            Object.defineProperty(Document.prototype, 'fullscreenElement', {
                configurable: true,
                get: function () {
                    return window.__fs.on ? document.documentElement : null;
                }
            });
            window.__fs.installed.push('fullscreenElement');
        } catch (error) {
            window.__fs.installed.push('FAILED fullscreenElement: ' + error);
        }

        try {
            Object.defineProperty(Element.prototype, 'requestFullscreen', {
                configurable: true,
                writable: true,
                value: function () {
                    window.__fs.entered++;
                    window.__fs.on = true;
                    fire();
                    return Promise.resolve();
                }
            });
            window.__fs.installed.push('requestFullscreen');
        } catch (error) {
            window.__fs.installed.push('FAILED requestFullscreen: ' + error);
        }

        try {
            Object.defineProperty(Document.prototype, 'exitFullscreen', {
                configurable: true,
                writable: true,
                value: function () {
                    window.__fs.exited++;
                    window.__fs.on = false;
                    fire();
                    return Promise.resolve();
                }
            });
            window.__fs.installed.push('exitFullscreen');
        } catch (error) {
            window.__fs.installed.push('FAILED exitFullscreen: ' + error);
        }
    }());
"""


def install_stub():
    browser.call("Page.addScriptToEvaluateOnNewDocument", source=STUB)


def counts():
    return json.loads(browser.evaluate("JSON.stringify(window.__fs)"))


def stored():
    return browser.evaluate("localStorage.getItem('cafe.fullscreen')")


def click(selector):
    box = browser.evaluate("""
        (function () {
            var el = document.querySelector('%s');
            if (!el) return '';
            var r = el.getBoundingClientRect();
            return Math.round(r.left + r.width / 2) + ',' +
                   Math.round(r.top + r.height / 2);
        }())
    """ % selector)
    x, y = [int(value) for value in box.split(",")]
    for kind in ("mousePressed", "mouseReleased"):
        browser.call("Input.dispatchMouseEvent", type=kind, x=x, y=y,
                     button="left", clickCount=1)
    time.sleep(0.2)


try:
    browser.call("Page.enable")
    browser.call("Runtime.enable")
    install_stub()

    print("\n=== 2. Sign in ===")
    browser.call("Page.navigate", url=BASE + "/login")
    wait_for("!!document.querySelector('form')", "the sign-in form")
    browser.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'sam';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
        }())
    """)
    wait_for("location.pathname !== '/login'", "the app")
    browser.evaluate("localStorage.removeItem('cafe.fullscreen')")

    print("\n=== 3. The button appears and fills the screen ===")
    browser.call("Page.navigate", url=BASE + "/orders/add")
    wait_for("!!document.getElementById('fullscreenBtn')", "the button")
    time.sleep(0.3)

    installed = json.loads(
        browser.evaluate("JSON.stringify(window.__fs.installed)"))
    check("the test's stand-in for the Fullscreen API is in place",
          installed == ["fullscreenElement", "requestFullscreen",
                        "exitFullscreen"],
          "the stub reported %s - anything else means this suite is "
          "measuring the real browser, not the app" % installed)
    check("and the app sees it rather than the native call",
          browser.evaluate(
              "String(document.documentElement.requestFullscreen)"
              ".indexOf('__fs') > -1"),
          "the app would call the browser's own implementation")

    check("the button is shown where the browser supports it",
          browser.evaluate(
              "!document.getElementById('fullscreenBtn').hidden"),
          "it stayed hidden despite a working Fullscreen API")
    check("it starts in the off state",
          browser.evaluate("document.getElementById('fullscreenBtn')"
                           ".getAttribute('aria-pressed')") == "false",
          "it claims to already be full screen")
    check("nothing went full screen on its own",
          counts()["entered"] == 0,
          "the page asked for a full screen before anyone tapped anything")

    click("#fullscreenBtn")
    check("tapping it fills the screen", counts()["entered"] == 1,
          "requestFullscreen was called %d times" % counts()["entered"])
    check("the button flips to the leave state",
          browser.evaluate("document.getElementById('fullscreenBtn')"
                           ".getAttribute('aria-pressed')") == "true",
          "the button still reads as off")
    check("and it says what it will do next",
          browser.evaluate("document.getElementById('fullscreenBtn')"
                           ".getAttribute('aria-label')")
          == "Leave full screen",
          "the label is %r" % browser.evaluate(
              "document.getElementById('fullscreenBtn')"
              ".getAttribute('aria-label')"))
    check("the choice is remembered for next time", stored() == "1",
          "nothing was stored, so the next visit would not fill the screen")

    print("\n=== 4. Leaving brings the browser back, and is remembered ===")
    click("#fullscreenBtn")
    check("tapping again leaves", counts()["exited"] == 1,
          "exitFullscreen was called %d times" % counts()["exited"])
    check("the button goes back to the fill state",
          browser.evaluate("document.getElementById('fullscreenBtn')"
                           ".getAttribute('aria-pressed')") == "false",
          "the button still reads as on")
    check("and the preference is dropped", stored() is None,
          "leaving full screen left the preference set, so the next tap "
          "would drag the user back in")

    print("\n=== 5. Escape means what it looks like it means ===")
    click("#fullscreenBtn")
    check("back in, and remembered", stored() == "1", "nothing stored")

    # Escape is handled by the browser itself, which leaves full screen and
    # fires the same event. That is what this reproduces.
    browser.evaluate("""
        (function () {
            window.__fs.on = false;
            document.dispatchEvent(new Event('fullscreenchange'));
        }())
    """)
    time.sleep(0.2)
    check("the app notices it was let out", stored() is None,
          "Escape left the preference set")
    check("and the button agrees",
          browser.evaluate("document.getElementById('fullscreenBtn')"
                           ".getAttribute('aria-pressed')") == "false",
          "the button still shows the leave icon")

    print("\n=== 6. With the preference set, the first tap fills it ===")
    browser.evaluate("localStorage.setItem('cafe.fullscreen', '1')")
    browser.call("Page.navigate", url=BASE + "/kitchen")
    wait_for("!!document.getElementById('fullscreenBtn')", "the button")
    time.sleep(0.3)

    check("still nothing on load alone", counts()["entered"] == 0,
          "the page filled the screen without a gesture, which a real "
          "browser would have refused anyway")

    # A tap anywhere, not on the button.
    click(".page-header")
    check("the first tap anywhere fills the screen",
          counts()["entered"] == 1,
          "requestFullscreen was called %d times" % counts()["entered"])

    print("\n=== 7. Without the preference, taps are left alone ===")
    browser.evaluate("localStorage.removeItem('cafe.fullscreen')")
    browser.call("Page.navigate", url=BASE + "/orders/add")
    wait_for("!!document.getElementById('fullscreenBtn')", "the button")
    time.sleep(0.3)

    click(".page-header")
    click(".page-header")
    check("a first-time visitor is never grabbed",
          counts()["entered"] == 0,
          "the page filled the screen uninvited %d times"
          % counts()["entered"])

    print("\n=== 8. No API, no button ===")
    # An iPhone has no Fullscreen API at all. A button that cannot work is
    # worse than no button, and the installed app covers that case.
    browser.call("Page.addScriptToEvaluateOnNewDocument", source="""
        (function () {
            // Deleting only uncovers the prototype's own copy, so both are
            // shadowed with nothing instead.
            Object.defineProperty(Element.prototype, 'requestFullscreen', {
                configurable: true, writable: true, value: undefined
            });
            Object.defineProperty(Element.prototype,
                                  'webkitRequestFullscreen', {
                configurable: true, writable: true, value: undefined
            });
        }());
    """)
    browser.call("Page.navigate", url=BASE + "/billing")
    wait_for("document.readyState === 'complete'", "Billing")
    time.sleep(0.4)
    check("the button stays hidden when it could do nothing",
          browser.evaluate(
              "document.getElementById('fullscreenBtn').hidden") is True,
          "a button is offered that the browser cannot honour")

finally:
    browser.close()

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
