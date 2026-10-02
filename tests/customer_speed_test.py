"""
The customer's pages, quick on a phone - and never out of date.

On the live service each trip to the database is about half a second, so
a page's wait is mostly its count of them. The table's pages now keep
what every table asks the same - the cafe behind the code, its menu, its
name and logo - for a few seconds, and forget it the moment the cafe's
staff save anything or an order moves stock. Orders write every dish in
the same few trips however many there are.

Checked both ways: the counts, so they cannot creep back up; and that
nothing is ever shown stale - a new price, a dish sold out, a paused
table, a replaced code.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/customer_speed_test.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "customer-speed-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.disable(logging.CRITICAL)

import app as application               # noqa: E402

app = application.app
app.config["TESTING"] = True

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % (detail,)))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def db(sql, params=()):
    rows = mysql_shim._DB.execute(sql, params).fetchall()
    mysql_shim._DB.commit()
    return [tuple(row) for row in rows]


def text(response):
    return response.get_data(as_text=True)


_real = mysql_shim._Cursor.execute


def trips(fn):
    """How many statements fn sends to the database, and its answer."""
    seen = []

    def spy(self, sql, params=()):
        seen.append(" ".join(str(sql).split())[:80])
        return _real(self, sql, params)

    mysql_shim._Cursor.execute = spy
    try:
        answer = fn()
    finally:
        mysql_shim._Cursor.execute = _real
    return len(seen), answer, seen


owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Quick Cafe", "full_name": "Quinn Owner", "username": "quinn",
    "phone_number": "", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
owner.post("/categories/add", data={"category_name": "Coffee", "description": "",
                                    "_csrf_token": csrf(owner)})
category = re.search(r'<option value="(\d+)">', text(owner.get("/foods/add"))).group(1)
for name, stock in (("Latte", 50), ("Mocha", 50), ("Last Muffin", 1)):
    owner.post("/foods/add", data={
        "food_name": name, "category_id": category, "price": "100",
        "quantity": str(stock), "minimum_stock": "0", "description": "",
        "diet": "veg", "_csrf_token": csrf(owner)})
FOOD = dict((name, fid) for fid, name in db("SELECT food_id, food_name FROM foods"))
with owner.session_transaction() as sess:
    CAFE = sess["cafe_id"]
TOKEN = application.get_public_token(CAFE)
guest = app.test_client()
MENU = "/m/%s" % TOKEN


# =====================================================================
print("\n=== 1. Few trips to the database ===")
# =====================================================================
guest.get(MENU)
count, page, _ = trips(lambda: guest.get(MENU))
check("the menu, opened again, costs nothing from the database",
      count == 0 and "Latte" in text(page), count)

# The first order from a code also starts its rate-limit rows; measured
# from the second, as every order after that runs.
app.test_client().post("/m/%s/order" % TOKEN, data={"quantity_%d" % FOOD["Mocha"]: "1"})
one, _, _ = trips(lambda: app.test_client().post(
    "/m/%s/order" % TOKEN, data={"quantity_%d" % FOOD["Mocha"]: "1"}))
count, placed, seen = trips(lambda: guest.post(
    "/m/%s/order" % TOKEN, data={"quantity_%d" % FOOD["Latte"]: "1",
                                  "quantity_%d" % FOOD["Mocha"]: "2"}))
where = placed.headers.get("Location", "")
# The stand-in writes the lines one statement each where the real
# connector sends them as one, so a second line counts once more here.
check("an order of two dishes takes the same trips as one of one",
      "/placed/" in where and count <= one + 1, (one, count, seen))
count, _, _ = trips(lambda: guest.get(where))
check("the order's page: its order and lines together, and is the kitchen busy",
      count <= 3, count)
ref = where.rsplit("/", 1)[-1]
count, status, _ = trips(lambda: guest.get("/m/%s/status/%s" % (TOKEN, ref)))
check("the status check every few seconds is one trip",
      count == 1 and status.get_json()["status"] == "Pending", (count, status.get_json()))


# =====================================================================
print("\n=== 2. ...and never out of date ===")
# =====================================================================
owner.post("/foods/edit/%d" % FOOD["Latte"], data={
    "food_name": "Latte", "category_id": category, "price": "140",
    "quantity": "49", "minimum_stock": "0", "description": "", "diet": "veg",
    "_csrf_token": csrf(owner)})
check("a new price is on the menu at once", "140.00" in text(guest.get(MENU))
      or "140" in re.findall(r'data-price="([\d.]+)"', text(guest.get(MENU))),
      re.findall(r'data-price="([\d.]+)"', text(guest.get(MENU))))

stock = dict((fid, qty) for fid, qty in db("SELECT food_id, quantity FROM inventory"))
check("each dish on the order came off its own shelf",
      stock[FOOD["Latte"]] == 49 and stock[FOOD["Mocha"]] == 46, stock)

check("the last muffin is on the menu", "Last Muffin" in text(guest.get(MENU)))
other = app.test_client()
other.post("/m/%s/order" % TOKEN, data={"quantity_%d" % FOOD["Last Muffin"]: "1"})
check("sold, it is off the menu for the next table straight away",
      "Last Muffin" not in text(guest.get(MENU)))

late = app.test_client()
late.post("/m/%s/order" % TOKEN, data={
    "quantity_%d" % FOOD["Latte"]: "1", "quantity_%d" % FOOD["Last Muffin"]: "1"})
newest = db("SELECT order_id FROM orders ORDER BY order_id DESC")[0][0]
check("a table still holding the muffin cannot buy it twice: it is left off",
      db("SELECT item_name FROM order_items WHERE order_id = ?", (newest,))
      == [("Latte",)]
      and db("SELECT quantity FROM inventory WHERE food_id = ?",
             (FOOD["Last Muffin"],)) == [(0,)])

db("UPDATE inventory SET quantity = 1 WHERE food_id = ?", (FOOD["Mocha"],))
db("UPDATE foods SET availability = 1 WHERE food_id = ?", (FOOD["Mocha"],))
application.cache_clear()
short = app.test_client()
short.post("/m/%s/order" % TOKEN, data={"quantity_%d" % FOOD["Mocha"]: "3"})
check("asking for more than the shelf holds takes nothing",
      db("SELECT quantity FROM inventory WHERE food_id = ?", (FOOD["Mocha"],)) == [(1,)])

owner.post("/settings/table-ordering", data={"open": "0", "_csrf_token": csrf(owner)})
check("pausing table ordering shows on the menu at once",
      'class="p-paused"' in text(guest.get(MENU)))
blocked = app.test_client().post("/m/%s/order" % TOKEN,
                                 data={"quantity_%d" % FOOD["Latte"]: "1"})
check("and an order is refused at once", "/placed/" not in blocked.headers.get("Location", ""))
owner.post("/settings/table-ordering", data={"open": "1", "_csrf_token": csrf(owner)})

guest.get(MENU)                                   # the name, remembered


def shown_name():
    with app.test_request_context("/m/%s" % TOKEN):
        return application.get_cafe_branding(CAFE)["brand_name"]


before_name = shown_name()
owner.post("/settings/branding", data={"brand_name": "Quick & Co", "brand_tagline": "",
                                       "_csrf_token": csrf(owner)})
check("a new name for the cafe is what the table's pages read at once",
      before_name != "Quick & Co" and shown_name() == "Quick & Co",
      (before_name, shown_name()))

old_token = TOKEN
guest.get("/m/%s" % old_token)
owner.post("/settings/qr", data={"action": "regenerate", "_csrf_token": csrf(owner)})
new_token = application.get_public_token(CAFE)
if new_token != old_token:
    app.test_client().get("/m/%s" % new_token)     # the new code, cached
    check("a replaced code stops working at once, even with the new one in use",
          app.test_client().get("/m/%s" % old_token).status_code == 404
          and app.test_client().get("/m/%s/status/%s" % (old_token, ref)).status_code == 404)
else:
    check("a replaced code stops working at once, even with the new one in use",
          False, "the code was not replaced - check the form fields")


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
