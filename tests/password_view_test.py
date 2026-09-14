"""
The "show password" button, driven in a real browser.

There are nine password fields across five screens, three of which are
standalone pages with their own styling and no icon font. The button is
added by script rather than written into each template, so the things
worth pinning down are all runtime behaviour: that it appears at all, that
it actually unmasks the field, that it does not submit the form it sits
inside, and that it is still there after instant navigation swaps a page
in without a fresh load.

Needs a Chromium-family browser (Edge or Chrome) installed; it drives one
headless over the DevTools protocol and skips cleanly if none is found.
No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/password_view_test.py
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "password-view-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
app.config["TESTING"] = False
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0    # keep the browser honest

PORT = 5813
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
    "cafe_name": "Reveal Cafe", "full_name": "Robin Owner", "username": "robin",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
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


def toggles_on(selector):
    """How many password fields on the page carry a reveal button."""
    return browser.evaluate("""
        document.querySelectorAll('%s').length
    """ % selector)


def type_into(selector, text):
    browser.evaluate("""
        (function () {
            var el = document.querySelector('%s');
            el.focus();
            el.value = '%s';
            el.dispatchEvent(new Event('input', { bubbles: true }));
        }())
    """ % (selector, text))


def field_type(selector):
    return browser.evaluate(
        "document.querySelector('%s').getAttribute('type')" % selector)


def click_toggle(index=0):
    """A real mouse click, not el.click() - hit testing matters here."""
    box = browser.evaluate("""
        (function () {
            var b = document.querySelectorAll('.pw-toggle')[%d]
                .getBoundingClientRect();
            return Math.round(b.left + b.width / 2) + ',' +
                   Math.round(b.top + b.height / 2);
        }())
    """ % index)
    x, y = [int(value) for value in box.split(",")]
    for kind in ("mousePressed", "mouseReleased"):
        browser.call("Input.dispatchMouseEvent", type=kind, x=x, y=y,
                     button="left", clickCount=1)
    time.sleep(0.15)


try:
    browser.call("Page.enable")

    print("\n=== 1. The sign-in screen ===")
    browser.call("Page.navigate", url=BASE + "/login")
    wait_for("!!document.querySelector('input[name=password]')",
             "the sign-in form")
    wait_for("!!document.querySelector('.pw-toggle')", "the reveal button")

    check("the password field gets a reveal button",
          toggles_on(".pw-toggle") == 1,
          "found %d buttons" % toggles_on(".pw-toggle"))
    check("it starts masked",
          field_type("input[name=password]") == "password",
          "the field starts as %r" % field_type("input[name=password]"))
    check("the button says what it will do",
          browser.evaluate(
              "document.querySelector('.pw-toggle')"
              ".getAttribute('aria-label')") == "Show password",
          "label is %r" % browser.evaluate(
              "document.querySelector('.pw-toggle').getAttribute('aria-label')"))

    type_into("input[name=password]", "hunter2rocks")
    click_toggle()

    check("clicking it reveals what was typed",
          field_type("input[name=password]") == "text",
          "the field is still %r" % field_type("input[name=password]"))
    check("and the typed value is untouched",
          browser.evaluate(
              "document.querySelector('input[name=password]').value")
          == "hunter2rocks",
          "the value became %r" % browser.evaluate(
              "document.querySelector('input[name=password]').value"))
    check("the label flips to the other action",
          browser.evaluate(
              "document.querySelector('.pw-toggle')"
              ".getAttribute('aria-label')") == "Hide password",
          "label is %r" % browser.evaluate(
              "document.querySelector('.pw-toggle').getAttribute('aria-label')"))

    check("the form was not submitted by the click",
          browser.evaluate("location.pathname") == "/login",
          "the click navigated to %s" % browser.evaluate("location.pathname"))

    click_toggle()
    check("clicking again puts the mask back",
          field_type("input[name=password]") == "password",
          "the field is %r" % field_type("input[name=password]"))

    print("\n=== 1b. Exactly one icon is drawn at a time ===")

    # Both icons live inside the button and CSS decides which one shows.
    # A generic `.pw-toggle svg` rule outranked the hide rule once, and
    # the open eye and the struck-through eye drew side by side on every
    # masked field.
    def visible_icons():
        return browser.evaluate("""
            (function () {
                var button = document.querySelector('.pw-toggle');
                var svgs = button.querySelectorAll('svg');
                var shown = 0;
                for (var i = 0; i < svgs.length; i++) {
                    if (getComputedStyle(svgs[i]).display !== 'none') shown++;
                }
                return shown;
            }())
        """)

    both = browser.evaluate(
        "document.querySelector('.pw-toggle').querySelectorAll('svg').length")
    check("the button carries both icons", both == 2, "found %d" % both)
    check("but only one is drawn while the password is masked",
          visible_icons() == 1, "%d icons drawn at once" % visible_icons())

    click_toggle()
    check("and only one while it is revealed",
          visible_icons() == 1, "%d icons drawn at once" % visible_icons())
    click_toggle()

    print("\n=== 2. Registration has one per field, working separately ===")
    browser.call("Page.navigate", url=BASE + "/register")
    wait_for("document.querySelectorAll('.pw-toggle').length === 2",
             "both reveal buttons")

    check("each password field gets its own button",
          toggles_on(".pw-toggle") == 2,
          "found %d" % toggles_on(".pw-toggle"))

    type_into("input[name=password]", "firstsecret")
    type_into("input[name=confirm_password]", "secondsecret")
    click_toggle(0)

    check("revealing one field leaves the other masked",
          field_type("input[name=password]") == "text"
          and field_type("input[name=confirm_password]") == "password",
          "got %r and %r" % (field_type("input[name=password]"),
                             field_type("input[name=confirm_password]")))
    check("and neither value was disturbed",
          browser.evaluate(
              "document.querySelector('input[name=password]').value")
          == "firstsecret"
          and browser.evaluate(
              "document.querySelector('input[name=confirm_password]').value")
          == "secondsecret",
          "values changed")

    print("\n=== 3. The password reset screen ===")
    browser.call("Page.navigate", url=BASE + "/forgot-password")
    wait_for("document.readyState === 'complete'", "the reset screen")
    check("both new-password fields get a button",
          toggles_on(".pw-toggle") == 2,
          "found %d" % toggles_on(".pw-toggle"))

    print("\n=== 4. Inside the app, after signing in ===")
    browser.call("Page.navigate", url=BASE + "/login")
    wait_for("!!document.querySelector('input[name=password]')",
             "the sign-in form")
    browser.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = 'robin';
            f.querySelector('[name=password]').value = 'password123';
            f.submit();
        }())
    """)
    wait_for("location.pathname !== '/login'", "the app")

    browser.call("Page.navigate", url=BASE + "/account/password")
    wait_for("document.querySelectorAll('.pw-toggle').length === 3",
             "three reveal buttons")
    check("all three change-password fields get one",
          toggles_on(".pw-toggle") == 3,
          "found %d" % toggles_on(".pw-toggle"))

    type_into("input[name=new_password]", "brandnewpass")
    click_toggle(1)
    check("the middle field reveals on its own",
          field_type("input[name=new_password]") == "text"
          and field_type("input[name=current_password]") == "password"
          and field_type("input[name=confirm_password]") == "password",
          "got %r / %r / %r" % (field_type("input[name=current_password]"),
                                field_type("input[name=new_password]"),
                                field_type("input[name=confirm_password]")))

    print("\n=== 5. It survives an instant page swap ===")
    # The buttons are added on load. Instant navigation replaces the page
    # without one, so a swapped-in form would arrive bare without the
    # instant:load hook.
    browser.call("Page.navigate", url=BASE + "/orders/add")
    wait_for("document.readyState === 'complete' && !!window.Instant",
             "New Order")
    check("a page with no password fields has no buttons",
          toggles_on(".pw-toggle") == 0,
          "found %d on New Order" % toggles_on(".pw-toggle"))

    browser.evaluate("window.Instant.visit('/account/password')")
    wait_for("location.pathname === '/account/password'",
             "Change Password via instant nav")
    wait_for("document.querySelectorAll('.pw-toggle').length === 3",
             "the buttons on the swapped-in page")

    check("a swapped-in form still gets its buttons",
          toggles_on(".pw-toggle") == 3,
          "found %d after the swap" % toggles_on(".pw-toggle"))

    type_into("input[name=current_password]", "afterswap")
    click_toggle(0)
    check("and they still work after the swap",
          field_type("input[name=current_password]") == "text",
          "the field is %r" % field_type("input[name=current_password]"))
    check("with no duplicate buttons stacked up",
          browser.evaluate(
              "document.querySelectorAll('.pw-field').length") == 3,
          "found %d wrappers for 3 fields" % browser.evaluate(
              "document.querySelectorAll('.pw-field').length"))

    print("\n=== 6. The button is out of the way of the keyboard ===")
    # Tab should run label -> field -> next field, not stop on every eye.
    check("the reveal buttons are not in the tab order",
          browser.evaluate("""
              (function () {
                  var b = document.querySelectorAll('.pw-toggle');
                  for (var i = 0; i < b.length; i++) {
                      if (b[i].tabIndex !== -1) return false;
                  }
                  return true;
              }())
          """),
          "a reveal button can be tabbed onto")
    check("and they are real buttons that cannot submit",
          browser.evaluate("""
              (function () {
                  var b = document.querySelectorAll('.pw-toggle');
                  for (var i = 0; i < b.length; i++) {
                      if (b[i].type !== 'button') return false;
                  }
                  return true;
              }())
          """),
          "a reveal button would submit its form")

finally:
    browser.close()

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
