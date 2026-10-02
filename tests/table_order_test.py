"""
Ordering from the table: eaten here or taken away, a pause, and a busy
kitchen said kindly.

 - The customer's menu offers Dine-in or Takeaway. A takeaway carries
   the cafe's packing charge, is shown as one on the order's page, and
   the bill the counter raises later charges the same.
 - Anybody on shift can pause table ordering from the profile menu. The
   menu still shows, with a kind note to order at the counter, and
   nothing is taken until it is switched back on.
 - After a table order, when the kitchen has a lot on - more than three
   orders waiting, or more than one big order (three or more items) in
   the last fifteen minutes, from the table or the counter alike - the
   order's page says so kindly. Not once the food is ready.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/table_order_test.py
"""
import os
import re
import sys
from datetime import timedelta
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "table-order-secret"
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


def flashes(client):
    with client.session_transaction() as sess:
        return [message for _, message in sess.pop("_flashes", [])]


def text(response):
    return response.get_data(as_text=True)


# =====================================================================
print("\n=== 0. A cafe with a table QR ===")
# =====================================================================
owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Table Cafe", "full_name": "Tara Owner", "username": "tara",
    "phone_number": "", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
owner.post("/categories/add", data={"category_name": "Coffee", "description": "",
                                    "_csrf_token": csrf(owner)})
category = re.search(r'<option value="(\d+)">', text(owner.get("/foods/add"))).group(1)
owner.post("/foods/add", data={
    "food_name": "Latte", "category_id": category, "price": "100",
    "quantity": "500", "minimum_stock": "1", "description": "", "diet": "veg",
    "_csrf_token": csrf(owner)})
LATTE = db("SELECT food_id FROM foods")[0][0]
owner.post("/settings/tax", data={"tax_percent": "0", "discount_percent": "0",
                                  "_csrf_token": csrf(owner)})
owner.post("/settings/packing", data={
    "takeaway_packing": "5", "takeaway_packing_mode": "item",
    "delivery_packing": "20", "delivery_packing_mode": "order",
    "_csrf_token": csrf(owner)})
owner.post("/users/add", data={
    "full_name": "Cal Cashier", "username": "cal", "role": "cashier",
    "phone_number": "", "password": "Brew-Latte-42", "_csrf_token": csrf(owner)})
flashes(owner)
with owner.session_transaction() as sess:
    CAFE = sess["cafe_id"]
TOKEN = application.get_public_token(CAFE)
print("  seeded")


def table_order(quantity=1, kind=None):
    guest = app.test_client()
    data = {"quantity_%d" % LATTE: str(quantity)}
    if kind:
        data["order_type"] = kind
    response = guest.post("/m/%s/order" % TOKEN, data=data)
    placed = response.headers.get("Location", "")
    return guest, response, placed


def newest():
    return db("SELECT order_id, order_type, packing, total_amount, public_ref "
              "FROM orders ORDER BY order_id DESC")[0]


# =====================================================================
print("\n=== 1. Dine-in or takeaway, from the table ===")
# =====================================================================
menu = text(app.test_client().get("/m/%s" % TOKEN))
check("the menu offers Dine-in and Takeaway",
      'data-kind="dine_in"' in menu and 'data-kind="takeaway"' in menu)
check("with Dine-in chosen to begin with",
      'id="orderTypeField" value="dine_in"' in menu)
check("and says what a takeaway adds",
      "+&#8377;5.00 each packing" in menu)
check("which the total on the phone adds in too",
      'data-packing="5.0"' in menu and 'data-packing-mode="item"' in menu)

guest, _, placed = table_order(3, "takeaway")
order = newest()
check("a takeaway of three is charged three containers",
      order[1] == "takeaway" and Decimal(str(order[2])) == Decimal("15.00")
      and Decimal(str(order[3])) == Decimal("315.00"), order)
page = text(guest.get(placed))
check("its page says it is a takeaway, packed to take away",
      "Takeaway &middot; packed to take away" in page)
check("and shows the packing line", "Packing (takeaway)" in page and "15.00" in page)
owner.get("/billing")
bill = db("SELECT packing, total_amount FROM bills WHERE order_id = ?", (order[0],))
check("the bill the counter raises charges the same",
      bill == [(Decimal("15.00"), Decimal("315.00"))]
      or [(Decimal(str(a)), Decimal(str(b))) for a, b in bill]
      == [(Decimal("15.00"), Decimal("315.00"))], bill)

guest, _, placed = table_order(2)
order = newest()
check("an order that does not say is eaten in, with no packing",
      order[1] == "dine_in" and Decimal(str(order[2])) == 0, order)
check("and its page says Dine-in",
      "Dine-in" in text(guest.get(placed))
      and "Packing (takeaway)" not in text(guest.get(placed)))
owner.post("/settings/packing", data={
    "takeaway_packing": "8", "takeaway_packing_mode": "item",
    "delivery_packing": "20", "delivery_packing_mode": "order",
    "_csrf_token": csrf(owner)})
flashes(owner)
_, _, _ = table_order(1, "takeaway")
changed = newest()
owner.get("/billing")
check("a takeaway is billed what it was placed with, even if the charge "
      "changes before the counter bills it",
      Decimal(str(db("SELECT packing FROM bills WHERE order_id = ?",
                     (order[0] - 1,))[0][0])) == Decimal("15.00")
      and Decimal(str(changed[2])) == Decimal("8.00"))


# =====================================================================
print("\n=== 2. Pausing table ordering ===")
# =====================================================================
cashier = app.test_client()
cashier.post("/login", data={"username": "cal", "password": "Brew-Latte-42"})
page = text(cashier.get("/orders/add"))
check("everyone on the team is offered the switch",
      'id="qrSwitch"' in page and 'aria-checked="true"' in page
      and "Table QR ordering" in page)
paused = cashier.post("/settings/table-ordering",
                      data={"open": "0", "_csrf_token": csrf(cashier)})
said = flashes(cashier)
check("a cashier can pause it", paused.status_code == 302
      and db("SELECT qr_ordering FROM cafes WHERE cafe_id = ?", (CAFE,)) == [(0,)])
check("and is told what customers will see",
      any("is off" in m and "counter" in m for m in said), said)
page = text(cashier.get("/orders/add"))
check("the switch then reads Off",
      re.search(r'id="qrSwitch".*?qr-switch__state ">Off<', page, re.S) is not None
      and 'aria-checked="false"' in page)
check("and each message is on the page once, not once more in the page's own block",
      page.count("Table QR ordering is off") <= 1)

menu = text(app.test_client().get("/m/%s" % TOKEN))
check("the customer's menu still shows",
      "Latte" in menu)
check("with a kind note to order at the counter",
      'class="p-paused"' in menu and "counter" in menu
      and "Thank you for understanding" in menu)
check("and nothing to send", 'class="is-paused"' in menu)
before = db("SELECT COUNT(*) FROM orders")[0][0]
guest, response, where = table_order(1)
check("an order sent anyway is not taken",
      db("SELECT COUNT(*) FROM orders")[0][0] == before)
check("it goes back to the menu, which says why",
      where.endswith("/m/%s" % TOKEN)
      and any("counter" in m for m in flashes(guest)), where)

# The team hears through the order-status feed every open page asks.
feed = cashier.get("/api/order-status").get_json()
check("the order-status feed tells every page it is off",
      feed.get("table_ordering") is False, feed)

# The profile menu switches it by script and draws itself from the answer.
answer = cashier.post("/settings/table-ordering",
                      data={"open": "1", "_csrf_token": csrf(cashier)},
                      headers={"X-Requested-With": "XMLHttpRequest"})
check("asked by script, the switch answers with its new state, not a page",
      answer.status_code == 200 and answer.get_json()["ok"] is True
      and answer.get_json()["open"] is True
      and "on again" in answer.get_json()["message"], answer.get_json())
check("and leaves no message behind for the next page", flashes(cashier) == [])
check("the feed says it is on again",
      cashier.get("/api/order-status").get_json().get("table_ordering") is True)
check("switched back on, it takes orders again",
      "/m/%s/placed/" % TOKEN in table_order(1)[2])
check("without the form token the switch does nothing",
      cashier.post("/settings/table-ordering", data={"open": "0"}).status_code == 400
      and db("SELECT qr_ordering FROM cafes WHERE cafe_id = ?", (CAFE,)) == [(1,)])
check("and someone signed out cannot touch it",
      app.test_client().post("/settings/table-ordering", data={"open": "0"}).status_code == 302
      and db("SELECT qr_ordering FROM cafes WHERE cafe_id = ?", (CAFE,)) == [(1,)])


# =====================================================================
print("\n=== 3. A busy kitchen, said kindly ===")
# =====================================================================
# A clean slate: every order so far is done, and an hour old.
db("UPDATE orders SET order_status = 'Completed', order_date = ?",
   (application.utc_now() - timedelta(hours=1),))
RUSH = "It is a busy time in our kitchen."

guest, _, placed = table_order(1)
check("a quiet kitchen says nothing of the sort", RUSH not in text(guest.get(placed)))

for _ in range(3):
    owner.post("/orders/add", data={"quantity_%d" % LATTE: "1",
                                    "_csrf_token": csrf(owner)})
page = text(guest.get(placed))
check("four orders waiting - three at the counter and this one - is busy",
      RUSH in page and "thank you so much for your patience" in page)
db("UPDATE orders SET order_status = 'Completed' WHERE source = 'counter'")
check("three waiting is not", RUSH not in text(guest.get(placed)))

owner.post("/orders/add", data={"quantity_%d" % LATTE: "3", "_csrf_token": csrf(owner)})
check("one big order in the last quarter hour is not a rush yet",
      RUSH not in text(guest.get(placed)))
big, _, big_placed = table_order(4)
check("a second big one - from the table this time - is",
      RUSH in text(big.get(big_placed)) and RUSH in text(guest.get(placed)))
db("UPDATE orders SET order_date = ? WHERE order_id IN "
   "(SELECT order_id FROM orders ORDER BY order_id DESC LIMIT 2)",
   (application.utc_now() - timedelta(minutes=20),))
check("big orders from more than fifteen minutes ago do not count",
      RUSH not in text(guest.get(placed)))

for _ in range(4):
    owner.post("/orders/add", data={"quantity_%d" % LATTE: "1",
                                    "_csrf_token": csrf(owner)})
ref = placed.rsplit("/", 1)[-1]
db("UPDATE orders SET order_status = 'Completed' WHERE public_ref = ?", (ref,))
check("once the food is ready, a busy kitchen is not mentioned",
      RUSH not in text(guest.get(placed)))


# =====================================================================
print("\n=== 4. Many table orders billed at once ===")
# =====================================================================
# Billing writes the bills for table orders the first time it is opened
# after they arrive. That was two round trips an order - twenty orders,
# forty trips before the page could be drawn - and is two in all now. The
# bills must come out exactly as they did one at a time.


def statements_during(fn):
    """How many statements reach the database while fn runs.

    A batch counts once: the driver sends a batched INSERT as a single
    statement, though the stand-in plays it back row by row.
    """
    seen = []
    real_execute = mysql_shim._Cursor.execute
    real_many = mysql_shim._Cursor.executemany

    def execute(self, sql, params=()):
        seen.append(sql)
        return real_execute(self, sql, params)

    def many(self, sql, rows):
        seen.append(sql)
        mysql_shim._Cursor.execute = real_execute
        try:
            return real_many(self, sql, rows)
        finally:
            mysql_shim._Cursor.execute = execute

    mysql_shim._Cursor.execute = execute
    mysql_shim._Cursor.executemany = many
    try:
        fn()
    finally:
        mysql_shim._Cursor.execute = real_execute
        mysql_shim._Cursor.executemany = real_many
    return len(seen)


owner.get("/billing")               # everything so far billed first
owner.post("/settings/tax", data={"tax_percent": "5", "discount_percent": "10",
                                  "_csrf_token": csrf(owner)})
flashes(owner)

BATCH = [(1, None), (2, "takeaway"), (3, None), (1, "takeaway"), (4, "dine_in"), (2, None)]
count_before = db("SELECT COUNT(*) FROM orders")[0][0]
for quantity, kind in BATCH:
    table_order(quantity, kind)
placed_now = db("SELECT order_id, packing FROM orders ORDER BY order_id DESC LIMIT %d"
                % len(BATCH))
check("six table orders go in", db("SELECT COUNT(*) FROM orders")[0][0]
      == count_before + len(BATCH))

owner.get("/orders/add")            # the shell's own lookups settled first
many_trips = statements_during(lambda: owner.get("/billing"))

holes = ",".join("?" * len(placed_now))
bills = {row[0]: row[1:] for row in db(
    "SELECT order_id, subtotal, tax, discount, packing, total_amount, "
    "payment_method, payment_status FROM bills WHERE order_id IN (%s)" % holes,
    tuple(order_id for order_id, _ in placed_now))}

check("every one of them has its bill", len(bills) == len(BATCH),
      "%d bills for %d orders" % (len(bills), len(BATCH)))

wrong = []
for order_id, packing in placed_now:
    sold = db("SELECT COALESCE(SUM(quantity * price), 0) FROM order_items "
              "WHERE order_id = ?", (order_id,))[0][0]
    want = application.bill_totals(Decimal(str(sold)), Decimal("0.05"),
                                   Decimal("0.10"), packing=packing)
    got = bills.get(order_id)
    if not got or [Decimal(str(v)) for v in got[:5]] != [
            want["subtotal"], want["tax"], want["discount"],
            want["packing"], want["total"]] or tuple(got[5:]) != ("Cash", "Pending"):
        wrong.append((order_id, got, want))
check("each bill is what it was one order at a time: subtotal, discount, "
      "tax, packing and total", not wrong, wrong)
check("(and the orders really differ - takeaways carry their packing)",
      len({Decimal(str(b[4])) for b in bills.values()}) > 1
      and len({Decimal(str(b[0])) for b in bills.values()}) > 1, bills)

table_order(1)
owner.get("/orders/add")
one_trips = statements_during(lambda: owner.get("/billing"))
check("billing six new orders takes no more trips than billing one",
      many_trips == one_trips, "six: %d statements, one: %d" % (many_trips, one_trips))

owner.get("/orders/add")
none_trips = statements_during(lambda: owner.get("/billing"))
check("and one more than a Billing with nothing new to bill - the one insert",
      one_trips == none_trips + 1, "one new: %d, none: %d" % (one_trips, none_trips))


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
