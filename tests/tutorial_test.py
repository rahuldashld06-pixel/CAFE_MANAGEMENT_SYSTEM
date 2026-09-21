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
import re
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

# Checked by what each tour points at rather than by its wording, which
# is a thing somebody may reasonably reword one day.
def points_at(html):
    return set(re.findall(r'data-at="\[data-tour=([a-z_]+)\]"', html))


admin_at = points_at(page(admin))
cashier_at = points_at(page(cashier))

check("the admin's points at reports and at staff",
      {"reports", "users"} <= admin_at,
      "an owner's tour points at %s" % sorted(admin_at))
check("and the cashier's points at neither",
      not ({"reports", "users"} & cashier_at),
      "somebody on the till is walked through screens they cannot open - "
      "the surest way to have the whole thing skipped: %s"
      % sorted(cashier_at))

check("both are pointed at the kitchen screen",
      "kitchen" in admin_at and "kitchen" in cashier_at,
      "the screen the food is made from is left out of one of them")

check("and at the counter screen and the till",
      {"add_order", "billing"} <= admin_at
      and {"add_order", "billing"} <= cashier_at,
      "admin %s, cashier %s" % (sorted(admin_at), sorted(cashier_at)))


print("\n=== 3. It points at the thing it is describing ===")
# A paragraph naming a screen is what this replaced. A step that cannot
# point at anything falls back to the middle of the page, which is right
# for the welcome and wrong for everything else.
shown = page(admin)

for who, html in (("an owner", page(admin)), ("a cashier", page(cashier))):
    steps = re.findall(r'<section class="tour__step".*?</section>',
                       html, re.S)
    aimed = [s for s in steps if "data-at=" in s]

    check("%s is pointed at something on all but the welcome" % who,
          len(aimed) == len(steps) - 1,
          "%d steps, %d of them point at anything"
          % (len(steps), len(aimed)))

    check("and every one of those says what pressing it does for %s" % who,
          all('class="tour__then"' in s for s in aimed),
          "%d pointing steps, %d say what happens"
          % (len(aimed), len([s for s in aimed
                              if 'class="tour__then"' in s])))

# A selector matching nothing would quietly become a card in the middle
# of the screen with no ring - the failure that looks like a design.
for name in points_at(shown):
    check("the tour can actually find %s on the page" % name,
          ('data-tour="%s"' % name) in shown,
          "the tour points at something that is not there")


print("\n=== 4. Once, not every morning ===")
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

# Last thing in the menu before signing out, which is where somebody
# looking for help looks.
menu = after[after.index('id="profileDropdown"'):]
check("sitting at the bottom of that menu",
      menu.index('id="tourReplay"') > menu.index("Automatic Printing"),
      "it is above the settings rather than under them")
check("and styled as the links beside it, not as a raw button",
      "button.profile-dropdown__item" in io.open(
          os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
              __file__))), "static", "css", "style.css"),
          encoding="utf-8").read(),
      "a button among links keeps its own border, background and centred "
      "text, and is plainly the odd one out")

check("marking one person shown does not mark another",
      seen_flag("till") == 0,
      "the cashier was marked shown by the admin finishing theirs")


print("\n=== 5. Who may say they have seen it ===")
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


print("\n=== 6. The screens no longer explain themselves ===")
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


print("\n=== 7. What a tour cannot hand back stays put ===")
# A tour is given once, on somebody's first day. These are needed at the
# moment of acting, by somebody who was shown round months ago.
kept = {
    "that a rate change is not retrospective":
        ("tax_settings.html", "keep the rates they were"),
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
