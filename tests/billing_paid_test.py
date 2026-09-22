"""
Nothing marks a bill Paid except the Paid button.

Changing a bill to UPI or Card is a note about how it will be settled, not
a statement that it has been. Even a verified gateway payment only leaves
its reference behind - the counter still presses Paid.

Also pins down which list shows what: Billing history keeps every day until
a period is asked for, while Order Management shows only today - the shift
the till is actually on. Nothing is lost either way; an older order still
opens by its own link and its bill stays in Billing.

Run with:  python tests/billing_paid_test.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "billing-paid-test-secret"
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


def bill_row(bill_id):
    row = mysql_shim._DB.execute(
        "SELECT payment_method, payment_status, gateway_payment_id "
        "FROM bills WHERE bill_id = ?", (bill_id,)).fetchone()
    return {"method": row[0], "status": row[1], "reference": row[2]}


client = app.test_client()
client.post("/register", data={
    "cafe_name": "Settle Cafe", "full_name": "Boss", "username": "boss",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)

client.post("/categories/add", data={"category_name": "Coffee",
                                     "description": "",
                                     "_csrf_token": csrf(client)},
            follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     client.get("/foods/add").get_data(as_text=True)).group(1)
client.post("/foods/add", data={
    "food_name": "Latte", "category_id": category, "price": "100",
    "quantity": "500", "description": "", "_csrf_token": csrf(client)},
    follow_redirects=True)
FOOD = re.findall(r'id="quantity_(\d+)"',
                  client.get("/orders/add").get_data(as_text=True))[0]

for _ in range(4):
    client.post("/orders/add", data={"quantity_%s" % FOOD: "1",
                                     "_csrf_token": csrf(client)},
                follow_redirects=True)
client.get("/billing")          # raises the bills


print("\n=== 1. A new bill waits to be settled ===")
check("bills start Cash and Pending",
      bill_row(1)["status"] == "Pending" and bill_row(1)["method"] == "Cash",
      "got %s" % bill_row(1))


print("\n=== 2. UPI and Card only change how, not whether ===")
for bill_id, method in ((1, "UPI"), (2, "Card")):
    client.post("/billing/edit/%d" % bill_id,
                data={"payment_method": method, "_csrf_token": csrf(client)},
                headers={"X-Requested-With": "XMLHttpRequest"})
    row = bill_row(bill_id)
    check("choosing %s records the method" % method, row["method"] == method,
          "method is %(method)s" % row)
    check("choosing %s does not settle the bill" % method,
          row["status"] == "Pending",
          "the bill went %(status)s without anyone pressing Paid" % row)


print("\n=== 3. The Paid button settles it ===")
response = client.post("/billing/mark-paid/1",
                       data={"_csrf_token": csrf(client)},
                       headers={"X-Requested-With": "XMLHttpRequest"})
payload = json.loads(response.get_data(as_text=True))
check("pressing Paid succeeds", payload.get("success") is True,
      "got %s" % payload)
check("and the bill is Paid", bill_row(1)["status"] == "Paid",
      "status is %s" % bill_row(1)["status"])
check("with the chosen method kept", bill_row(1)["method"] == "UPI",
      "method became %s" % bill_row(1)["method"])


print("\n=== 4. A verified gateway payment records, it does not settle ===")
# This is the one path that used to mark a bill Paid on its own.
mysql_shim._DB.execute(
    "UPDATE bills SET gateway_order_id = 'order_test123' WHERE bill_id = 3")
mysql_shim._DB.commit()

with app.test_request_context("/"):
    from flask import session as flask_session
    flask_session["user_id"] = 1
    flask_session["cafe_id"] = 1
    flask_session["role"] = "admin"
    ok, message = application._record_gateway_payment(
        3, "order_test123", "pay_test456")

row = bill_row(3)
check("the gateway call is accepted", ok is True, "returned %r" % message)
check("the payment reference is stored",
      row["reference"] == "pay_test456",
      "no reference recorded: %s" % row)
check("the method shows it came through the gateway",
      row["method"] == "Online", "method is %(method)s" % row)
check("but the bill is still Pending",
      row["status"] == "Pending",
      "the gateway settled the bill on its own - status is %(status)s" % row)
check("and the message asks someone to press Paid",
      "paid" in (message or "").lower(),
      "message was %r" % message)

client.post("/billing/mark-paid/3", data={"_csrf_token": csrf(client)},
            headers={"X-Requested-With": "XMLHttpRequest"})
check("pressing Paid then settles it", bill_row(3)["status"] == "Paid",
      "status is %s" % bill_row(3)["status"])


print("\n=== 5. The history opens on today, and keeps everything ===")
mysql_shim._DB.execute(
    "UPDATE bills SET bill_date = '2026-02-03 09:00:00' WHERE bill_id = 4")
mysql_shim._DB.commit()

def listed(query=""):
    html = client.get("/billing" + query).get_data(as_text=True)
    return sorted(set(re.findall(r'data-bill-id="(\d+)"', html)))

# Somebody at the till is settling bills from the shift they are
# standing in, so that is what the page opens on.
check("it opens on today, not on everything ever saved",
      "4" not in listed(),
      "listed %s - a February bill is in front of the till" % listed())
check("with today's bills all there",
      sorted(listed()) == ["1", "2", "3"], "listed %s" % listed())

february = listed("?from_date=2026-02-01&to_date=2026-02-28")
check("a period shows that period",
      february == ["4"], "listed %s" % february)

everything = listed("?all=1")
check("and All History brings back every bill ever saved",
      len(everything) == 4, "listed %s" % everything)

check("nothing was deleted to make today's list short",
      "4" in everything,
      "the February bill is gone for good, not merely out of view")


print("\n=== 6. Order Management is today's work ===")
# Bills 1-3 were raised today; bill 4 was moved to February above, and its
# order with it. The till wants the list in front of it to be this shift.
mysql_shim._DB.execute(
    "UPDATE orders SET order_date = '2026-02-03 09:00:00', "
    "order_day = '2026-02-03' WHERE order_id = 4")
mysql_shim._DB.commit()

listed_orders = sorted(
    str(row["order_id"])
    for row in client.get("/api/kitchen/board").get_json()["orders"])

check("a February order is not on the kitchen screen",
      "4" not in listed_orders, "listed %s" % listed_orders)
check("today's orders are",
      {"1", "2", "3"}.issubset(set(listed_orders)),
      "listed %s" % listed_orders)
check("the screen says it is showing today",
      "Today's orders" in client.get("/kitchen").get_data(as_text=True),
      "nothing tells the user why older orders are absent")

# Nothing is lost: the order is still reachable and still billed.
check("the older order still opens by its own link",
      client.get("/orders/4").status_code == 200,
      "an order that dropped off the list became unreachable")
check("and its bill is still in Billing history",
      "4" in listed("?all=1"),
      "the bill vanished along with the order")

print("\n=== 7. Billing shows the number people say out loud ===")
# The kitchen screen, the printed ticket and the customer holding their
# phone all use the number that starts again each morning. Billing was
# showing the permanent row id instead, so one order was #47 on this page
# and #6 everywhere else - and the person reconciling the till had to
# work out that they were the same order.
mysql_shim._DB.execute("UPDATE orders SET daily_no = 7 WHERE order_id = 1")
mysql_shim._DB.commit()

history = client.get("/billing").get_data(as_text=True)
row = re.search(r'data-bill-id="1".*?</tr>', history, re.S)
row = row.group(0) if row else ""

shown = re.search(r'<a href="/orders/1"[^>]*>\s*#(\d+)\s*</a>', row)
check("the order column shows the number the kitchen calls out",
      bool(shown) and shown.group(1) == "7",
      "it shows #%s" % (shown.group(1) if shown else "nothing at all"))

check("and the link still opens the order itself",
      '/orders/1"' in row,
      "the number is no longer a way into the order")

# The bill keeps its own number. They are different things counted
# differently, and the bill number is what the bill is filed under.
check("the bill keeps its own number",
      re.search(r'class="bill-id">#1<', row) is not None,
      "the bill number changed along with the order number")


# =====================================================================
print("\n=== Billing shows what a till needs, and not much else ===")
# =====================================================================
# The tiles across the top - bills, paid, pending, cancelled, revenue -
# were a summary of the very list printed underneath them, and cost a
# query of their own on every load.
#
# Subtotal and Tax went the same way, for a different reason: on a cafe
# that charges neither, Subtotal is the total printed a second time under
# another name, and a column of zeroes beside it is how a useful column
# gets lost among pointless ones. They come back the moment there is
# something to put in them.

def head_of(client):
    """The column names, in the order the page puts them."""
    page = client.get("/billing").get_data(as_text=True)
    head = re.search(r"<thead>(.*?)</thead>", page, re.S)
    return re.findall(r"<th>(.*?)</th>", head.group(1), re.S) if head else []


def cell_count(client):
    """How many cells the first body row has, which must match the head."""
    page = client.get("/billing").get_data(as_text=True)
    body = re.search(r"<tbody>\s*(<tr.*?</tr>)", page, re.S)
    return len(re.findall(r"<td[^>]*>", body.group(1))) if body else 0


plain = app.test_client()
plain.post("/register", data={
    "cafe_name": "Plain Till", "full_name": "Owner", "username": "plainboss",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
plain.post("/settings/tax", data={"tax_percent": "0", "discount_percent": "0",
                                  "_csrf_token": csrf(plain)},
           follow_redirects=True)
plain.post("/categories/add", data={"category_name": "Coffee",
                                    "description": "",
                                    "_csrf_token": csrf(plain)},
           follow_redirects=True)
pcat = re.search(r'<option value="(\d+)">',
                 plain.get("/foods/add").get_data(as_text=True)).group(1)
plain.post("/foods/add", data={
    "food_name": "Cortado", "category_id": pcat, "price": "100",
    "quantity": "50", "minimum_stock": "2", "description": "",
    "_csrf_token": csrf(plain)}, follow_redirects=True)
pfood = re.findall(r'id="quantity_(\d+)"',
                   plain.get("/orders/add").get_data(as_text=True))[0]


def plain_order():
    plain.post("/orders/add", data={"quantity_%s" % pfood: "2",
                                    "_csrf_token": csrf(plain)},
               follow_redirects=True)


plain_order()
columns = head_of(plain)

check("the summary tiles are gone",
      "stats-grid" not in plain.get("/billing").get_data(as_text=True),
      "they still sit above a list that says the same thing")

check("order status is no longer a column of its own",
      "Order Status" not in columns, columns)

check("and nor is subtotal or tax, with nothing to adjust",
      "Subtotal" not in columns and "Tax" not in columns,
      "a cafe charging neither is shown its total twice: %s" % columns)

# The order asked for: what it was, when, what was in it, what it came to.
check("what is left reads in the order it happened",
      columns[:5] == ["Bill ID", "Order ID", "Date", "Order Items", "Total"],
      "the columns run %s" % columns[:5])

check("every row has a cell for every column",
      cell_count(plain) == len(columns),
      "%d columns but %d cells - the table is out by %d"
      % (len(columns), cell_count(plain),
         abs(len(columns) - cell_count(plain))))


# ---- and they come back when they mean something --------------------
plain.post("/settings/tax", data={"tax_percent": "5", "discount_percent": "0",
                                  "_csrf_token": csrf(plain)},
           follow_redirects=True)
plain_order()
columns = head_of(plain)

check("charging tax brings back the tax column",
      "Tax" in columns, columns)

check("and the subtotal with it, since it now differs from the total",
      "Subtotal" in columns,
      "the tax is shown with nothing to say what it was charged on")

check("but not a discount column, with no discount given",
      "Discount" not in columns, columns)

plain.post("/settings/tax", data={"tax_percent": "5", "discount_percent": "10",
                                  "_csrf_token": csrf(plain)},
           follow_redirects=True)
plain_order()
columns = head_of(plain)

check("giving a discount brings back that column too",
      "Discount" in columns, columns)

check("and the sum reads left to right",
      [c for c in columns
       if c in ("Subtotal", "Discount", "Tax", "Total")]
      == ["Subtotal", "Discount", "Tax", "Total"],
      "they are ordered %s" % [c for c in columns if c in
                               ("Subtotal", "Discount", "Tax", "Total")])

check("and every row still has a cell for every column",
      cell_count(plain) == len(columns),
      "%d columns but %d cells" % (len(columns), cell_count(plain)))

# A cafe that stops charging still has to be able to explain old bills.
plain.post("/settings/tax", data={"tax_percent": "0", "discount_percent": "0",
                                  "_csrf_token": csrf(plain)},
           follow_redirects=True)
columns = head_of(plain)
check("bills already charged keep their columns after the rate is dropped",
      "Tax" in columns and "Discount" in columns,
      "the older bills can no longer say what they were charged: %s"
      % columns)


# =====================================================================
print("\n=== The Order Status popup counts the same way ===")
# =====================================================================
# It is the counter's own view of the shift it is on, and it was neither
# of those things. It listed the last fifteen orders whenever they were
# taken, so first thing in the morning the whole popup was yesterday's
# work with a pending badge counting orders served the night before. And
# it numbered them by row id, which climbs for ever - so the counter
# would call "order 4" while the popup said "Order #312" about the same
# one.
import datetime as _dt

feeder = app.test_client()
feeder.post("/register", data={
    "cafe_name": "Popup Cafe", "full_name": "Owner", "username": "popupboss",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
feeder.post("/categories/add", data={"category_name": "Coffee",
                                     "description": "",
                                     "_csrf_token": csrf(feeder)},
            follow_redirects=True)
fcat = re.search(r'<option value="(\d+)">',
                 feeder.get("/foods/add").get_data(as_text=True)).group(1)
feeder.post("/foods/add", data={
    "food_name": "Macchiato", "category_id": fcat, "price": "90",
    "quantity": "900", "minimum_stock": "2", "description": "",
    "_csrf_token": csrf(feeder)}, follow_redirects=True)
ffood = re.findall(r'id="quantity_(\d+)"',
                   feeder.get("/orders/add").get_data(as_text=True))[0]


def feeder_order():
    feeder.post("/orders/add", data={"quantity_%s" % ffood: "1",
                                     "_csrf_token": csrf(feeder)},
                follow_redirects=True)


with feeder.session_transaction() as sess:
    fowner = sess.get("user_id")

# Three yesterday, then the day turns over and three more.
for _ in range(3):
    feeder_order()
mysql_shim._DB.execute(
    "UPDATE orders SET order_day = ? WHERE user_id = ?",
    (str(_dt.date.today() - _dt.timedelta(days=1)), fowner))
mysql_shim._DB.commit()
for _ in range(3):
    feeder_order()

feed = feeder.get("/api/order-status").get_json()
shown = feed["orders"]

check("the popup shows today's orders and no others",
      len(shown) == 3,
      "it lists %d orders, so yesterday's are still on the counter's "
      "screen this morning" % len(shown))

check("numbered the way they are called out, starting again at one",
      sorted(o["daily_no"] for o in shown) == [1, 2, 3],
      "it shows %s - the kitchen board and the customer's slip say 1, 2, 3"
      % sorted(o["daily_no"] for o in shown))

# The id still has to travel: it is what Done posts to, and what the
# order is keyed on. Showing the daily number must not change that.
check("but each row still carries the id the Done button needs",
      all(o["order_id"] > 3 for o in shown)
      and len({o["order_id"] for o in shown}) == 3,
      "the rows carry %s, which are not this order's own ids"
      % [o["order_id"] for o in shown])

check("and the pending badge counts today only",
      feed["pending_count"] == 3,
      "the badge says %s, so it is counting orders that were served "
      "yesterday" % feed["pending_count"])

# Marking one done through the popup still has to find the order.
first = shown[0]
feeder.post("/orders/complete/%s" % first["order_id"],
            data={"_csrf_token": csrf(feeder)}, follow_redirects=True)
after = feeder.get("/api/order-status").get_json()
check("marking one done through the popup works on the id, not the number",
      sum(1 for o in after["orders"] if o["order_status"] == "Pending") == 2,
      "the Done button posted a number that named no order")

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
