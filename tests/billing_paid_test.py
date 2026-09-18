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

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
