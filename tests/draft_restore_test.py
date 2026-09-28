"""
Nothing typed is lost to a closed browser.

Every form in the app keeps a copy of what has been typed into it on the
device, written the moment it changes. If the page goes before the form
is saved - the browser closed, the power went, the site fell over in the
middle of a save - the form comes back filled in the next time it is
opened, and all that is left is to press Save.

The part that needs a real browser is knowing when a copy may be thrown
away, because bringing back something that WAS saved invites saving it
twice. So most of this is about saves: one that goes through, one the
server refuses, one the server falls over on, and one that never gets
an answer at all. Each is made to happen for real here - a bad category
the server turns away, a view that raises, the network switched off in
the middle of a save.

Needs a Chromium-family browser (Edge or Chrome); skips cleanly without
one. No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/draft_restore_test.py
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
os.environ["SECRET_KEY"] = "draft-restore-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("app").setLevel(logging.CRITICAL)

import app as application               # noqa: E402
from tests import cdp                   # noqa: E402

app = application.app
app.logger.setLevel(logging.CRITICAL)
PORT = 5823
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
    "cafe_name": "Draft Cafe", "full_name": "Dana Owner", "username": "dana",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def csrf():
    with seed.session_transaction() as sess:
        return sess.get("_csrf_token", "")


seed.post("/categories/add", data={"category_name": "Tiffin",
                                   "description": "", "_csrf_token": csrf()},
          follow_redirects=True)
CATEGORY = mysql_shim._DB.execute(
    "SELECT category_id FROM categories WHERE category_name = 'Tiffin'"
).fetchone()[0]

for food, price in (("Filter Coffee", "60"), ("Idli", "50")):
    seed.post("/foods/add", data={
        "food_name": food, "category_id": CATEGORY, "price": price,
        "quantity": "30", "minimum_stock": "1", "description": "",
        "_csrf_token": csrf()}, follow_redirects=True)

# A second person on the same till.
seed.post("/users/add", data={
    "full_name": "Rio Manager", "username": "rio", "role": "manager",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf()}, follow_redirects=True)


def foods_named(name):
    return mysql_shim._DB.execute(
        "SELECT COUNT(*) FROM foods WHERE food_name = ?", (name,)
    ).fetchone()[0]


def orders_now():
    return mysql_shim._DB.execute("SELECT COUNT(*) FROM orders").fetchone()[0]


threading.Thread(target=lambda: app.run(host="127.0.0.1", port=PORT,
                                        threaded=True, use_reloader=False),
                 daemon=True).start()
for _ in range(80):
    try:
        urllib.request.urlopen(BASE + "/healthz", timeout=1).read()
        break
    except Exception:
        time.sleep(0.25)

mysql_shim.skip_tour()

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
        time.sleep(0.12)
    raise AssertionError("timeout: " + what)


def open_page(path):
    b.call("Page.navigate", url=BASE + path)
    wait("location.pathname === %s && document.readyState === 'complete'"
         % json.dumps(path), path)
    time.sleep(0.25)


def sign_in(user):
    # Out first, then a clean /login. Signed out already, /logout sends
    # the browser to /login?next=/logout.
    b.call("Page.navigate", url=BASE + "/logout")
    wait("location.pathname === '/login'", "signed out")
    b.call("Page.navigate", url=BASE + "/login")
    wait("location.pathname === '/login' && location.search === ''"
         " && document.readyState === 'complete'", "the sign-in page")
    b.evaluate("""
        (function () {
            var f = document.querySelector('form');
            f.querySelector('[name=username]').value = %s;
            f.querySelector('[name=password]').value = 'password123';
            f.querySelector('[type=submit]').click();
            return true;
        }())
    """ % json.dumps(user))
    wait("location.pathname !== '/login'", "signed in as " + user)
    wait("document.readyState === 'complete'", "the first page")


def fill(values):
    """Type into the page's form, the way a person's typing arrives."""
    b.evaluate("""
        (function (values) {
            var form = document.querySelector('#page-view form');
            Object.keys(values).forEach(function (name) {
                var el = form.querySelector('[name="' + name + '"]');
                el.value = values[name];
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            });
            return true;
        }(%s))
    """ % json.dumps(values))


def value_of(name):
    return b.evaluate(
        "(document.querySelector('#page-view form [name=\"%s\"]') || {}).value"
        % name)


def drafts():
    return json.loads(b.evaluate("""
        JSON.stringify(Object.keys(localStorage)
            .filter(function (k) { return k.indexOf('cafora-draft:') === 0; })
            .map(function (k) { return [k, JSON.parse(localStorage.getItem(k))]; }))
    """))


def draft_for(path):
    return [record for key, record in drafts() if (":" + path + ":") in key]


def note():
    return b.evaluate("""
        (function () {
            var n = document.querySelector('.draft-note');
            return n ? {text: n.textContent.replace(/\\s+/g, ' ').trim(),
                        warn: n.classList.contains('draft-note--warn')}
                     : null;
        }())
    """)


def discard():
    """Press Discard, if there is anything to discard."""
    b.evaluate("""
        (function () {
            var d = document.querySelector('.draft-note__discard');
            if (d) d.click();
            return !!d;
        }())
    """)
    time.sleep(0.2)


def save():
    # Veg or non-veg is a choice Add Food will not go without; a person
    # saving it would have made one.
    b.evaluate("""
        (function () {
            var form = document.querySelector('#page-view form');
            var diets = form.querySelectorAll('[name=diet]');
            if (diets.length && ![].some.call(diets, function (r) { return r.checked; })) {
                form.querySelector('[name=diet][value=veg]').click();
            }
            form.querySelector('[type=submit]').click();
            return true;
        }())
    """)


def crash():
    """The page goes, with no chance to do anything on the way out."""
    b.call("Page.navigate", url="about:blank")
    wait("location.href === 'about:blank'", "the page gone")


DOSA = {"food_name": "Masala Dosa", "price": "140", "quantity": "12",
        "description": "Crisp, with coconut chutney"}

try:
    b.call("Page.enable")
    b.call("Network.enable")
    b.call("Emulation.setDeviceMetricsOverride", width=1280, height=900,
           deviceScaleFactor=1, mobile=False)
    sign_in("dana")

    # =================================================================
    print("\n=== 1. What is typed is kept as it is typed ===")
    # =================================================================
    open_page("/foods/add")
    fill(DOSA)

    kept = draft_for("/foods/add")
    check("a copy exists the moment something is typed",
          len(kept) == 1
          and kept[0]["fields"].get("food_name") == ["Masala Dosa"],
          "storage holds %s" % kept)

    # Written on every change, not on the way out - so a power cut, which
    # runs nothing on the way out, costs at most a keystroke.
    check("without waiting for the page to be left", bool(kept),
          "nothing is stored until something else happens")

    check("and nothing the page carries for itself goes with it",
          "_csrf_token" not in json.dumps(drafts()),
          "the CSRF token was written to storage")

    # =================================================================
    print("\n=== 2. The page closing does not lose it ===")
    # =================================================================
    crash()
    open_page("/foods/add")
    wait("!!document.querySelector('.draft-note')", "the note", timeout=5)

    check("every field comes back",
          [value_of(n) for n in DOSA] == list(DOSA.values()),
          "the form reads %s" % [value_of(n) for n in DOSA])

    said = note() or {}
    check("with a line saying so", "Brought back" in said.get("text", ""),
          "the note reads %r" % said.get("text"))
    check("which is not a warning - nothing was sent",
          said.get("warn") is False, "it is shown as a warning")

    # =================================================================
    print("\n=== 3. Discard puts the form back as it was ===")
    # =================================================================
    discard()
    time.sleep(0.2)

    check("the typed values go", value_of("food_name") == "",
          "the name still reads %r" % value_of("food_name"))
    check("so does the note", note() is None, "the note is still up")
    check("and so does the copy", not draft_for("/foods/add"),
          "it is still in storage")

    # =================================================================
    print("\n=== 4. A save that goes through leaves nothing behind ===")
    # =================================================================
    fill(dict(DOSA, category_id=str(CATEGORY)))
    save()
    wait("location.pathname === '/foods'", "the food list")

    check("it was saved, once", foods_named("Masala Dosa") == 1,
          "%d foods called Masala Dosa" % foods_named("Masala Dosa"))
    check("and the copy is gone", not draft_for("/foods/add"),
          "a copy of a saved food is still waiting to be brought back")

    open_page("/foods/add")
    check("so the form opens empty, with no note",
          note() is None and value_of("food_name") == "",
          "it opened with %r and note %r" % (value_of("food_name"), note()))

    # =================================================================
    print("\n=== 5. A save the server refuses comes back ===")
    # =================================================================
    # A category that is not this cafe's. The browser is satisfied - the
    # select has a value - and the server is not.
    fill({"food_name": "Uttapam", "price": "120", "quantity": "5"})
    b.evaluate("""
        (function () {
            var s = document.querySelector('#page-view [name=category_id]');
            var o = document.createElement('option');
            o.value = '999999'; o.textContent = 'Somebody else\\'s';
            s.appendChild(o); s.value = '999999';
            s.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        }())
    """)
    save()
    wait("location.pathname === '/foods/add' && !!document.querySelector('.draft-note')",
         "the form back with a note")

    check("nothing was saved", foods_named("Uttapam") == 0,
          "the refused food exists")
    check("what was typed is back in the form",
          value_of("food_name") == "Uttapam" and value_of("price") == "120",
          "the form reads %r / %r" % (value_of("food_name"), value_of("price")))
    check("and the note says it was not saved",
          "not saved" in (note() or {}).get("text", ""),
          "it reads %r" % (note() or {}).get("text"))
    discard()

    # =================================================================
    print("\n=== 6. A save the server falls over on ===")
    # =================================================================
    # An unexpected error ends on the dashboard, with a message - which,
    # from the browser, looks like any successful save moving on.
    real_view = app.view_functions["add_food"]

    def falls_over(*args, **kwargs):
        from flask import request
        if request.method == "POST":
            raise RuntimeError("the database went away mid-save")
        return real_view(*args, **kwargs)

    app.view_functions["add_food"] = falls_over
    try:
        open_page("/foods/add")
        fill({"food_name": "Pesarattu", "price": "110", "quantity": "4",
              "category_id": str(CATEGORY)})
        save()
        wait("location.pathname !== '/foods/add'", "the error page")
        time.sleep(0.4)
    finally:
        app.view_functions["add_food"] = real_view

    kept = draft_for("/foods/add")
    check("the copy survives a save that failed on the server",
          len(kept) == 1
          and kept[0]["fields"].get("food_name") == ["Pesarattu"],
          "storage holds %s" % kept)
    check("marked as failed rather than as maybe-saved",
          kept and kept[0].get("how") == "failed" and not kept[0].get("sent"),
          "it is marked %s" % (kept and {k: kept[0].get(k)
                                          for k in ("how", "sent")}))
    check("and the server's word on it is read once and cleared",
          "cafora_post" not in b.evaluate("document.cookie"),
          "the answer is still sitting in a cookie")

    open_page("/foods/add")
    check("opening the form again brings it back",
          value_of("food_name") == "Pesarattu",
          "the form reads %r" % value_of("food_name"))
    check("saying the save did not go through",
          "did not go through" in (note() or {}).get("text", ""),
          "it reads %r" % (note() or {}).get("text"))
    check("and nothing was written", foods_named("Pesarattu") == 0,
          "the failed save left a food behind")
    discard()

    # =================================================================
    print("\n=== 7. A save that never gets an answer ===")
    # =================================================================
    open_page("/foods/add")
    fill({"food_name": "Appam", "price": "90", "quantity": "6",
          "category_id": str(CATEGORY)})
    b.call("Network.emulateNetworkConditions", offline=True, latency=0,
           downloadThroughput=-1, uploadThroughput=-1)
    save()
    time.sleep(2.0)
    b.call("Network.emulateNetworkConditions", offline=False, latency=0,
           downloadThroughput=-1, uploadThroughput=-1)

    check("it never reached the server", foods_named("Appam") == 0,
          "the offline save was written")

    open_page("/foods/add")
    said = note() or {}
    check("the form comes back filled in",
          value_of("food_name") == "Appam",
          "it reads %r" % value_of("food_name"))
    check("with a warning that it may already have gone through",
          said.get("warn") is True
          and "may already have gone through" in said.get("text", ""),
          "the note reads %r" % said)
    discard()

    # =================================================================
    print("\n=== 8. The New Order basket ===")
    # =================================================================
    open_page("/orders/add")
    b.evaluate("""
        (function () {
            var plus = document.querySelector(
                '.food-card:not(.food-card--mirror) .quantity-plus');
            plus.click(); plus.click();
            return true;
        }())
    """)
    time.sleep(0.2)
    basket = draft_for("/orders/add")
    check("two taps of + are kept", basket and "2" in json.dumps(
          basket[0]["fields"]), "storage holds %s" % basket)

    crash()
    open_page("/orders/add")
    time.sleep(0.4)
    first = b.evaluate("""
        document.querySelector('.food-card:not(.food-card--mirror) .quantity-input').value
    """)
    check("the basket comes back after the page closed", first == "2",
          "the first dish reads %r" % first)

    before = orders_now()
    b.evaluate("document.getElementById('orderForm').requestSubmit()")
    wait("true", "")
    end = time.time() + 15
    while time.time() < end and orders_now() == before:
        time.sleep(0.2)
    time.sleep(0.5)
    check("the order goes to the kitchen", orders_now() == before + 1,
          "%d orders were written" % (orders_now() - before))
    check("and the basket's copy goes with it", not draft_for("/orders/add"),
          "a placed order is still waiting to be put back in the basket")

    open_page("/orders/add")
    again = b.evaluate("""
        document.querySelector('.food-card:not(.food-card--mirror) .quantity-input').value
    """)
    check("so the next order starts empty", again == "0" and note() is None,
          "it opened at %r with note %r" % (again, note()))

    # =================================================================
    print("\n=== 9. One person's work is not shown to the next ===")
    # =================================================================
    open_page("/foods/add")
    fill({"food_name": "Dana's special"})
    sign_in("rio")
    open_page("/foods/add")
    check("somebody else signing in on the same till sees an empty form",
          value_of("food_name") == "" and note() is None,
          "they were shown %r" % value_of("food_name"))

    sign_in("dana")
    open_page("/foods/add")
    check("and the person who typed it gets it back",
          value_of("food_name") == "Dana's special",
          "it reads %r" % value_of("food_name"))
    discard()

    # =================================================================
    print("\n=== 10. A week, and no longer ===")
    # =================================================================
    fill({"food_name": "Last week's idea"})
    b.evaluate("""
        (function () {
            Object.keys(localStorage).forEach(function (k) {
                if (k.indexOf('cafora-draft:') !== 0) return;
                var r = JSON.parse(localStorage.getItem(k));
                r.at = Date.now() - 8 * 24 * 60 * 60 * 1000;
                localStorage.setItem(k, JSON.stringify(r));
            });
            return true;
        }())
    """)
    open_page("/foods/add")
    check("a copy older than a week is not brought back",
          value_of("food_name") == "" and note() is None,
          "it came back: %r" % value_of("food_name"))
    check("and is cleared out", not draft_for("/foods/add"),
          "it is still in storage")

    # =================================================================
    print("\n=== 11. A password is never written down ===")
    # =================================================================
    open_page("/users/add")
    fill({"username": "newbie", "full_name": "New Bie",
          "password": "sup3rsecret!"})
    stored = json.dumps(drafts())
    check("the rest of the form is kept", "newbie" in stored,
          "the new user's name was not kept")
    check("but not the password", "sup3rsecret!" not in stored,
          "the password is sitting in the browser's storage")

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
