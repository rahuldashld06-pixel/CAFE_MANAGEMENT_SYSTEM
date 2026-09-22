"""
Offline tests for what time the café thinks it is.

Times are stored in UTC and read back on the café's own clock. They were
stored in UTC before this too — the app and the database both run on
machines set to it — but nothing converted them on the way out, so a café
in India read every time five and a half hours early. The date looked
right for most of the day, which is what made it easy to miss: UTC and
India only disagree about the date between midnight and half past five in
the morning.

So this suite checks both halves. That one stored instant reads as the
café's wall clock everywhere it is shown — the kitchen, the billing list,
the printed receipt — and that changing the clock does not move an order
to a different day, which is a separate question and one that was settled
separately.

Run with:  python tests/timezone_test.py
"""
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "timezone-test-secret"
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


def build(cafe, username):
    client = app.test_client()
    client.post("/register", data={
        "cafe_name": cafe, "full_name": username.title(),
        "username": username, "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)
    client.post("/categories/add", data={
        "category_name": "Coffee", "description": "",
        "_csrf_token": csrf(client)}, follow_redirects=True)
    category = re.search(
        r'<option value="(\d+)">',
        client.get("/foods/add").get_data(as_text=True)).group(1)
    client.post("/foods/add", data={
        "food_name": "Chai", "category_id": category, "price": "70",
        "quantity": "80", "minimum_stock": "2", "description": "",
        "_csrf_token": csrf(client)}, follow_redirects=True)
    food = re.findall(r'id="quantity_(\d+)"',
                      client.get("/orders/add").get_data(as_text=True))[0]
    return client, food


def set_zone(client, zone):
    return client.post("/settings/timezone",
                       data={"timezone": zone, "_csrf_token": csrf(client)},
                       follow_redirects=True)


def kitchen_says(client):
    rows = client.get("/api/kitchen/board").get_json()["orders"]
    return rows[0]["placed"] if rows else ""


def billing_says(client):
    # The cell carries the date twice - a long form and a short one
    # without the year, and the stylesheet shows whichever fits. This
    # reads the long one, which is the only one with a year in it.
    found = re.search(r'date-long">(\d+ \w+ \d{4}, [\d:]+ [AP]M)<',
                      client.get("/billing").get_data(as_text=True))
    return found.group(1) if found else ""


def receipt_says(client, order_id):
    found = re.search(r"<dt>Date</dt><dd>([^<]+)</dd>",
                      client.get("/orders/%d/bill" % order_id)
                      .get_data(as_text=True))
    return found.group(1) if found else ""


admin, food = build("Clock Cafe", "boss")
admin.post("/orders/add", data={"quantity_%s" % food: "1",
                                "_csrf_token": csrf(admin)},
           follow_redirects=True)
admin.get("/billing")          # raises the bill


print("\n=== 1. Stored in UTC, whatever the machine is set to ===")
stored = mysql_shim._DB.execute(
    "SELECT order_date FROM orders WHERE order_id = 1").fetchone()[0]
written = datetime.strptime(str(stored)[:19], "%Y-%m-%d %H:%M:%S")
apart = abs((datetime.now(timezone.utc).replace(tzinfo=None)
             - written).total_seconds())

check("an order is stamped in UTC by the app",
      apart < 120,
      "the stored time is %s, which is %.0f minutes from UTC now - it was "
      "written on some other clock" % (written, apart / 60))

bill_stored = mysql_shim._DB.execute(
    "SELECT bill_date FROM bills WHERE bill_id = 1").fetchone()[0]
bill_written = datetime.strptime(str(bill_stored)[:19], "%Y-%m-%d %H:%M:%S")
check("and so is its bill",
      abs((written - bill_written).total_seconds()) < 120,
      "the order says %s and its own bill says %s - one of them was "
      "filled in by the database rather than the app"
      % (written, bill_written))


print("\n=== 2. Read back on the cafe's clock ===")
# Left alone a cafe reads UTC, which is what every cafe read before this
# was a choice, so nothing moves under anybody on upgrade.
check("a cafe that has not chosen reads UTC",
      application.cafe_timezone_name() == "UTC",
      "it defaults to %s" % application.cafe_timezone_name())

before = kitchen_says(admin)
set_zone(admin, "Asia/Kolkata")
after = kitchen_says(admin)

check("choosing a zone changes what the time reads as",
      before != after and after,
      "it said %r before and %r after" % (before, after))

expected = written.replace(tzinfo=timezone.utc).astimezone(
    ZoneInfo("Asia/Kolkata"))
check("and it reads as that zone's wall clock",
      after == expected.strftime("%d %b, %I:%M %p"),
      "the kitchen says %r; on that clock it is %r"
      % (after, expected.strftime("%d %b, %I:%M %p")))


print("\n=== 3. The same instant, wherever it is shown ===")
# A bill that disagrees with its own order is how this was noticed in the
# first place, so the three places a time appears are checked together.
same = expected.strftime("%d %b %Y, %I:%M %p")

check("billing shows the cafe's clock",
      billing_says(admin) == same,
      "billing says %r, the kitchen's instant is %r"
      % (billing_says(admin), same))

check("and so does the printed receipt",
      receipt_says(admin, 1) == same,
      "the receipt says %r" % receipt_says(admin, 1))

set_zone(admin, "America/New_York")
moved = written.replace(tzinfo=timezone.utc).astimezone(
    ZoneInfo("America/New_York")).strftime("%d %b %Y, %I:%M %p")

check("moving the clock moves all three together",
      billing_says(admin) == moved and receipt_says(admin, 1) == moved,
      "billing %r, receipt %r, expected %r"
      % (billing_says(admin), receipt_says(admin, 1), moved))


print("\n=== 4. The clock is the cafe's, not the reader's ===")
other, other_food = build("Second Cafe", "bee")
other.post("/orders/add", data={"quantity_%s" % other_food: "1",
                                "_csrf_token": csrf(other)},
           follow_redirects=True)
set_zone(other, "Europe/London")

check("one cafe's zone is its own",
      application.cafe_timezone_name() is not None,
      "no zone resolves at all")
check("and the two cafes disagree, as they should",
      kitchen_says(admin) != kitchen_says(other),
      "both read %r, so one of them is on the other's clock"
      % kitchen_says(admin))


print("\n=== 5. Which day an order belongs to does not move ===")
# Changing what the time reads as is not the same as changing which day
# an order is counted under, and that second question was settled
# separately. An order keeps the day it was taken on.
day, number = mysql_shim._DB.execute(
    "SELECT order_day, daily_no FROM orders WHERE order_id = 1").fetchone()

for zone in ("Pacific/Auckland", "America/Los_Angeles", "Asia/Kolkata"):
    set_zone(admin, zone)

after_day, after_number = mysql_shim._DB.execute(
    "SELECT order_day, daily_no FROM orders WHERE order_id = 1").fetchone()

check("the day it was counted under is unchanged",
      str(after_day) == str(day),
      "it moved from %s to %s when the clock changed" % (day, after_day))
check("and so is the number it was given",
      after_number == number,
      "number %s became %s" % (number, after_number))


print("\n=== 6. Only an owner sets it, and only to a real place ===")
admin.post("/users/add", data={
    "full_name": "Till One", "username": "till", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(admin)}, follow_redirects=True)
cashier = app.test_client()
cashier.post("/login", data={"username": "till", "password": "password123"},
             follow_redirects=True)

def stored_zone(cafe_id=1):
    """Straight from the row - cafe_timezone_name() needs a request."""
    return mysql_shim._DB.execute(
        "SELECT timezone FROM cafes WHERE cafe_id = ?", (cafe_id,)).fetchone()[0]


was = stored_zone()
set_zone(cashier, "Pacific/Auckland")
check("a cashier cannot move the whole cafe's clock",
      stored_zone() == was,
      "it went from %s to %s - somebody on the till changed what time it "
      "is for everyone" % (was, stored_zone()))

set_zone(admin, "Mars/Olympus_Mons")
check("and a place that does not exist is refused",
      stored_zone() == was,
      "it became %s - a zone the server cannot read would leave every "
      "time on every screen unreadable" % stored_zone())



# =====================================================================
print("\n=== The picker asks for a country, not for an IANA name ===")
# =====================================================================
# It used to offer the raw tz database: two thousand entries reading
# "Indian/Chagos", "America/Argentina/Catamarca", "Etc/GMT+7". Those are
# addresses in a database, not answers to "where is this cafe?" - and
# nobody running a cafe in Delhi should have to know that India's clock
# is filed under Kolkata.
choices = application.timezone_choices()
labels = [label for _, label in choices]
values = [zone for zone, _ in choices]

check("every country is offered by its own name",
      not [label for label in labels if "/" in label],
      "these still read as zone names: %s"
      % [label for label in labels if "/" in label][:6])

for country in ("India", "United Kingdom", "Japan", "Nepal", "Sri Lanka",
                "United Arab Emirates", "Bangladesh", "Singapore"):
    check("%s is in the list" % country, country in labels,
          "somebody running a cafe there could not say where they are")

check("India means the clock India actually keeps",
      dict((label, zone) for zone, label in choices)["India"]
      == "Asia/Kolkata",
      "India is offered as %s"
      % dict((label, zone) for zone, label in choices).get("India"))

# A country wide enough to keep several clocks cannot be collapsed to
# one. An "Australia" that quietly meant Sydney would tell Perth the
# wrong time twice a day.
for country, city in (("Australia", "Perth"), ("United States", "Denver"),
                      ("Brazil", "Manaus"), ("Canada", "Vancouver")):
    check("%s still says which part" % country,
          "%s \u2014 %s" % (country, city) in labels,
          "%s is offered as %s" % (country,
                                   [l for l in labels
                                    if l.startswith(country)][:4]))

check("and every value offered is a zone this machine has",
      all(application.known_timezone(zone) is not None for zone in values),
      "the picker offers a clock that would be refused on save")

check("nothing is offered twice",
      len(values) == len(set(values)),
      "the same clock appears under two entries")


# =====================================================================
print("\n=== The spelling a browser reports is not always the list's ===")
# =====================================================================
# A laptop in India says "Asia/Calcutta". The picker, and IANA itself
# these days, say "Asia/Kolkata". Same clock, two names - and a cafe
# stored under the one the picker does not list opens the page to find
# nothing selected, which looks a lot like having lost the setting.
for reported, expected in (("Asia/Calcutta", "Asia/Kolkata"),
                           ("Asia/Saigon", "Asia/Ho_Chi_Minh"),
                           ("Europe/Kiev", "Europe/Kyiv"),
                           ("Asia/Rangoon", "Asia/Yangon"),
                           ("America/Buenos_Aires",
                            "America/Argentina/Buenos_Aires")):
    check("%s is filed as %s" % (reported, expected),
          application.canonical_zone(reported) == expected,
          "it was filed as %s, which the picker does not offer"
          % application.canonical_zone(reported))

check("a name with no alias is left exactly as it is",
      application.canonical_zone("Asia/Tokyo") == "Asia/Tokyo")


# =====================================================================
print("\n=== Signing in sets it, and only while nobody has ===")
# =====================================================================
# The whole point of the exercise: a cafe should never have to go and
# find this page for its screens to tell the right time.
fresh = app.test_client()
fresh.post("/register", data={
    "cafe_name": "Fresh Cafe", "full_name": "New Owner", "username": "newbie",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123",
    "timezone": "Asia/Calcutta"}, follow_redirects=True)

new_cafe = mysql_shim._DB.execute(
    "SELECT cafe_id FROM cafes WHERE cafe_name = 'Fresh Cafe'").fetchone()[0]


def zone_of(cafe_id):
    return mysql_shim._DB.execute(
        "SELECT timezone FROM cafes WHERE cafe_id = ?", (cafe_id,)).fetchone()[0]


check("registering a cafe settles its clock there and then",
      zone_of(new_cafe) == "Asia/Kolkata",
      "it says %r - a brand new cafe would read UTC until somebody "
      "signed out and back in" % zone_of(new_cafe))

check("and the page shows the country, already chosen",
      re.search(r'value="Asia/Kolkata"\s*selected>\s*India',
                fresh.get("/settings/timezone").get_data(as_text=True))
      is not None,
      "the admin opens the page to find their country not selected")

# Set on purpose, and then left alone however many laptops sign in.
fresh.post("/settings/timezone", data={"timezone": "Asia/Tokyo",
                                       "_csrf_token": csrf(fresh)},
           follow_redirects=True)
check("changing it from the profile sticks",
      zone_of(new_cafe) == "Asia/Tokyo", zone_of(new_cafe))

with app.test_request_context():
    application.settle_cafe_clock(new_cafe, "America/New_York")
check("and a later sign-in from elsewhere does not move it back",
      zone_of(new_cafe) == "Asia/Tokyo",
      "a laptop signing in from another country moved the whole cafe "
      "to %s, undoing a choice somebody made on purpose"
      % zone_of(new_cafe))


# =====================================================================
print("\n=== A clock the country list has never heard of ===")
# =====================================================================
# The list is this app's, not IANA's. Dropping somebody's working clock
# because it is not in a list I wrote would be a poor trade.
mysql_shim._DB.execute(
    "UPDATE cafes SET timezone = 'Antarctica/Troll' WHERE cafe_id = ?",
    (new_cafe,))
mysql_shim._DB.commit()

page = fresh.get("/settings/timezone").get_data(as_text=True)
check("it is still offered, and still selected",
      re.search(r'value="Antarctica/Troll"\s*selected', page) is not None,
      "the picker dropped a working clock, so opening the page and "
      "saving would silently move the cafe somewhere else")


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
