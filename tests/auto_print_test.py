"""
Offline tests for automatic printing.

Two independent switches, both off until a café asks for them: the kitchen
ticket after an order is saved (on a delay it chooses), and the receipt the
moment a bill is marked paid.

They belong to the café rather than to one person, and anyone who works
there can change them - whoever is on the till is the one who notices the
tickets are coming out wrong.

Run with:  python tests/auto_print_test.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "auto-print-test-secret"
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


def message(response):
    found = re.findall(r'class="alert">([^<]+)<', response.get_data(as_text=True))
    return found[0].strip() if found else ""


def config(client, path="/orders/add"):
    """The printing settings as the page hands them to the browser."""
    html = client.get(path).get_data(as_text=True)
    return json.loads(re.search(r'printing: (\{.*?\}),', html, re.S).group(1))


def save(client, **form):
    form.setdefault("kot_delay", "5")
    form["_csrf_token"] = csrf(client)
    return client.post("/settings/printing", data=form, follow_redirects=True)


admin = app.test_client()
admin.post("/register", data={
    "cafe_name": "Print Cafe", "full_name": "Boss", "username": "boss",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)

admin.post("/categories/add", data={"category_name": "Coffee",
                                    "description": "", "_csrf_token": csrf(admin)},
           follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     admin.get("/foods/add").get_data(as_text=True)).group(1)
admin.post("/foods/add", data={
    "food_name": "Latte", "category_id": category, "price": "100",
    "quantity": "200", "description": "", "_csrf_token": csrf(admin)},
    follow_redirects=True)
FOOD = re.findall(r'id="quantity_(\d+)"',
                  admin.get("/orders/add").get_data(as_text=True))[0]


print("\n=== 1. Off until asked for ===")
settings = config(admin)
check("the kitchen ticket does not print by itself out of the box",
      settings["auto_kot"] is False, "auto_kot is %(auto_kot)s" % settings)
check("nor does the receipt",
      settings["auto_bill"] is False, "auto_bill is %(auto_bill)s" % settings)
check("the page still carries the settings so it can check them",
      "kot_delay" in settings, "the browser has nothing to read")

check("the settings page opens",
      admin.get("/settings/printing").status_code == 200)
check("it is linked from the profile menu",
      "/settings/printing" in admin.get("/orders/add").get_data(as_text=True))


print("\n=== 2. Turning them on ===")
save(admin, auto_kot="on", kot_delay="8", auto_bill="on")
settings = config(admin)
check("the kitchen ticket switch sticks", settings["auto_kot"] is True)
check("the delay sticks", settings["kot_delay"] == 8,
      "delay is %(kot_delay)s" % settings)
check("the receipt switch sticks", settings["auto_bill"] is True)


print("\n=== 3. Each switch is its own ===")
save(admin, auto_kot="on", kot_delay="3")          # auto_bill omitted = off
settings = config(admin)
check("the receipt can be off while the ticket is on",
      settings["auto_kot"] is True and settings["auto_bill"] is False,
      "got %s" % settings)

save(admin, auto_bill="on", kot_delay="3")         # auto_kot omitted = off
settings = config(admin)
check("and the other way round",
      settings["auto_kot"] is False and settings["auto_bill"] is True,
      "got %s" % settings)


print("\n=== 4. The delay is checked ===")
save(admin, auto_kot="on", kot_delay="0")
check("zero is allowed - print immediately",
      config(admin)["kot_delay"] == 0)

for bad, why in [("-1", "negative"), ("9999", "beyond the cap"),
                 ("soon", "not a number")]:
    before = config(admin)["kot_delay"]
    response = save(admin, auto_kot="on", kot_delay=bad)
    after = config(admin)["kot_delay"]
    check("%r is refused (%s)" % (bad, why),
          after == before and message(response) != "",
          "delay went %s -> %s with no complaint" % (before, after))

save(admin, auto_kot="on", kot_delay=str(application.MAX_KOT_DELAY))
check("the maximum itself is allowed",
      config(admin)["kot_delay"] == application.MAX_KOT_DELAY)


print("\n=== 5. Anyone who works here can change them ===")
admin.post("/users/add", data={
    "full_name": "Till One", "username": "till", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(admin)}, follow_redirects=True)

staff = app.test_client()
staff.post("/login", data={"username": "till", "password": "password123"},
           follow_redirects=True)

check("a cashier can open the settings",
      staff.get("/settings/printing").status_code == 200,
      "they would have to find a manager to fix a misbehaving printer")
check("the entry is in their menu too",
      "/settings/printing" in staff.get("/orders/add").get_data(as_text=True))

save(staff, auto_kot="on", kot_delay="12", auto_bill="on")
check("a change they make takes effect", config(staff)["kot_delay"] == 12)
check("and applies to the whole café, not just them",
      config(admin)["kot_delay"] == 12,
      "the admin still sees %s - the setting is per-user, not per-café"
      % config(admin)["kot_delay"])


print("\n=== 6. What the pages need to act on it ===")
# The kitchen ticket is addressed by order, so the order-created reply has
# to say which order it was.
created = admin.post("/orders/add",
                     data={"quantity_%s" % FOOD: "2",
                           "_csrf_token": csrf(admin)},
                     headers={"X-Requested-With": "XMLHttpRequest"})
payload = json.loads(created.get_data(as_text=True))
check("creating an order returns its id", payload.get("order_id"),
      "without it the kitchen ticket cannot be printed: %s" % payload)
order_id = payload["order_id"]

# The receipt is addressed by order too, but Paid is pressed on a bill.
admin.get("/billing")
bill_id = mysql_shim._DB.execute(
    "SELECT bill_id FROM bills WHERE order_id = ?", (order_id,)).fetchone()[0]

paid = admin.post("/billing/mark-paid/%s" % bill_id,
                  data={"_csrf_token": csrf(admin)},
                  headers={"X-Requested-With": "XMLHttpRequest"})
payload = json.loads(paid.get_data(as_text=True))
check("marking a bill paid returns the order id",
      payload.get("order_id") == order_id,
      "without it the receipt cannot be printed: %s" % payload)

check("the printable ticket for that order exists",
      admin.get("/orders/%s/kot" % order_id).status_code == 200)
check("and the printable bill",
      admin.get("/orders/%s/bill" % order_id).status_code == 200)


print("\n=== 7. One café's settings are its own ===")
other = app.test_client()
other.post("/register", data={
    "cafe_name": "Other Cafe", "full_name": "Other", "username": "other",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)

check("a new café starts with printing off",
      config(other)["auto_kot"] is False and config(other)["auto_bill"] is False,
      "got %s" % config(other))
check("and is unaffected by the first café's settings",
      config(other)["kot_delay"] != 12,
      "settings leaked between tenants")


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
