"""
Offline tests for the tour somebody is given on their first sign-in.

It exists so the pages do not have to explain themselves. Every screen
used to carry a paragraph under its heading saying what it was for - read
once, by one person, on their first day, and in everybody's way after
that. So this suite checks both halves of that trade: that the tour is
actually given, to the right person, once; and that the instructions it
replaced are gone from the screens used all day.

It also checks what deliberately stayed. Field help on the settings
screens behind the profile menu, and any warning about an effect that is
shared or cannot be undone, are not things a tour can hand back at the
moment somebody needs them.

Run with:  python tests/tutorial_test.py
"""
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "tutorial-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import app as application     # noqa: E402

app = application.app
app.config["TESTING"] = True

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def page(client, path="/orders/add"):
    return client.get(path).get_data(as_text=True)


def template(name):
    return io.open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "templates", name),
        encoding="utf-8").read()


def seen_flag(username):
    row = mysql_shim._DB.execute(
        "SELECT tutorial_seen FROM users WHERE username = ?",
        (username,)).fetchone()
    return row[0] if row else None


admin = app.test_client()
admin.post("/register", data={
    "cafe_name": "Tour Cafe", "full_name": "Boss", "username": "boss",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)

admin.post("/users/add", data={
    "full_name": "Till One", "username": "till", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(admin)}, follow_redirects=True)

cashier = app.test_client()
cashier.post("/login", data={"username": "till", "password": "password123"},
             follow_redirects=True)


print("\n=== 1. Somebody new is shown round ===")
first = page(admin)

check("the tour is on the page",
      'id="tour"' in first,
      "a new admin is never offered it")
check("and is marked as owed, so it opens by itself",
      'data-due="1"' in first,
      "it is there but nothing opens it")
check("nobody has been marked as shown yet",
      seen_flag("boss") == 0,
      "the flag is %s before anyone has seen anything" % seen_flag("boss"))


print("\n=== 2. The two jobs get different tours ===")
admin_steps = page(admin).count('class="tour__step"')
cashier_steps = page(cashier).count('class="tour__step"')

check("an admin is walked through more than a cashier",
      admin_steps > cashier_steps > 0,
      "admin %d steps, cashier %d" % (admin_steps, cashier_steps))

check("the admin's covers reports and staff",
      "User Management adds" in page(admin),
      "an owner is not told where to add the people who work for them")
check("and the cashier's does not",
      "User Management adds" not in page(cashier),
      "somebody on the till is walked through screens they cannot open - "
      "the surest way to have the whole thing skipped")

check("both are told about the kitchen screen",
      "kitchen screen" in page(admin).lower()
      and "kitchen screen" in page(cashier).lower(),
      "the screen the food is made from is left out of one of them")


print("\n=== 3. Once, not every morning ===")
done = admin.post("/api/tutorial/seen", data={"_csrf_token": csrf(admin)},
                  headers={"X-Requested-With": "XMLHttpRequest"})

check("finishing it is remembered",
      done.status_code == 200 and (done.get_json() or {}).get("ok") is True,
      "it answered HTTP %s" % done.status_code)
check("against the person, not the browser",
      seen_flag("boss") == 1,
      "the flag is %s" % seen_flag("boss"))

after = page(admin)
check("so it no longer opens by itself",
      'data-due="0"' in after,
      "it would start again on every page load")
check("but it is still there to be asked for",
      'id="tour"' in after,
      "somebody who skipped it has no way back to it")

check("and the way back to it is in the profile menu",
      'id="tourReplay"' in after,
      "nothing on the pages explains them any more, so this has to be "
      "findable")

check("marking one person shown does not mark another",
      seen_flag("till") == 0,
      "the cashier was marked shown by the admin finishing theirs")


print("\n=== 4. Who may say they have seen it ===")
check("a cashier can mark their own",
      cashier.post("/api/tutorial/seen",
                   data={"_csrf_token": csrf(cashier)},
                   headers={"X-Requested-With": "XMLHttpRequest"}
                   ).status_code == 200,
      "staff would be shown it again every single morning")

stranger = app.test_client()
check("and nobody signed out can",
      stranger.post("/api/tutorial/seen").status_code in (302, 403),
      "an endpoint that writes to a user row is open to anyone")


print("\n=== 5. The screens no longer explain themselves ===")
# What the tour now says, taken off the pages that carry the day's work.
gone = {
    "New Order": ("add_order.html", "set quantities"),
    "the kitchen screen": ("kitchen.html", "Tap the circle beside a dish"),
    "Billing": ("billing.html", "Use the date filter to view any period"),
    "the QR page": ("qr_settings.html", "prints by itself on whichever"),
}
for where, (name, wording) in gone.items():
    check("%s no longer instructs the user" % where,
          wording not in template(name),
          "still telling them: %r" % wording)

check("and the QR page no longer names a page that was removed",
      "Order Management" not in template("qr_settings.html"),
      "it points at a screen that has not existed since the kitchen took "
      "that job over")


print("\n=== 6. What a tour cannot hand back stays put ===")
# A tour is given once, on somebody's first day. These are needed at the
# moment of acting, by somebody who was shown round months ago.
kept = {
    "that a rate change is not retrospective":
        ("tax_settings.html", "keep the rate they were"),
    "that the cafe's name is everybody's":
        ("branding.html", "whole cafe's name"),
    "that a new QR code breaks the printed ones":
        ("qr_settings.html", "already printed"),
    "how to make a printer stop asking":
        ("print_settings.html", "kiosk-printing"),
}
for what, (name, wording) in kept.items():
    check("the warning %s is still there" % what,
          wording in template(name),
          "removed from %s, and the tour cannot give it back at the "
          "moment it matters" % name)


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
