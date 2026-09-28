"""
Dine-in, takeaway and delivery - and what a container costs.

The owner sets a takeaway charge and a delivery charge under Profile ->
Packing Charges, each either once per order or once per item. A counter
order says which of the three it is; takeaway and delivery then carry the
charge as its own line on the bill, after the discount and the tax.
Dine-in never does, and neither does an order from the table QR.

Checked on the numbers a cafe would see: what the settings page saves and
refuses, what each kind of order is charged, and that the charge and the
kind are shown wherever the bill is - the order page, the printed bill,
the kitchen ticket and the Billing list.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/order_type_test.py
"""
import os
import re
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "order-type-secret"
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
          ("" if condition else "\n          -> %s" % detail))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def db(sql, params=()):
    rows = mysql_shim._DB.execute(sql, params).fetchall()
    mysql_shim._DB.commit()
    return rows


def flashes(client):
    with client.session_transaction() as sess:
        return [message for _, message in sess.pop("_flashes", [])]


def text(response):
    return response.get_data(as_text=True)


# =====================================================================
print("\n=== 0. A cafe with a menu ===")
# =====================================================================
owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Packed Cafe", "full_name": "Pia Owner", "username": "pia",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
owner.post("/categories/add", data={"category_name": "Coffee", "description": "",
                                    "_csrf_token": csrf(owner)})
category = re.search(r'<option value="(\d+)">',
                     text(owner.get("/foods/add"))).group(1)
for name in ("Latte", "Brownie"):
    owner.post("/foods/add", data={
        "food_name": name, "category_id": category, "price": "100",
        "quantity": "200", "minimum_stock": "1", "description": "",
        "diet": "veg", "_csrf_token": csrf(owner)})
LATTE, BROWNIE = [int(r[0]) for r in db("SELECT food_id FROM foods ORDER BY food_id")]
# No tax and no discount, so every total below is the dishes plus the
# container and nothing else.
owner.post("/settings/tax", data={"tax_percent": "0", "discount_percent": "0",
                                  "_csrf_token": csrf(owner)})
flashes(owner)
print("  seeded")


def place(client, order_type=None, latte=1, brownie=0):
    data = {"quantity_%d" % LATTE: str(latte),
            "quantity_%d" % BROWNIE: str(brownie),
            "_csrf_token": csrf(client)}
    if order_type is not None:
        data["order_type"] = order_type
    client.post("/orders/add", data=data)
    row = db("SELECT o.order_id, o.order_type, b.bill_id, b.subtotal, b.packing, "
             "b.total_amount FROM orders o JOIN bills b ON b.order_id = o.order_id "
             "ORDER BY o.order_id DESC")[0]
    return {"order_id": row[0], "type": row[1], "bill_id": row[2],
            "subtotal": Decimal(str(row[3])), "packing": Decimal(str(row[4])),
            "total": Decimal(str(row[5]))}


# =====================================================================
print("\n=== 1. Before anything is set, a container costs nothing ===")
# =====================================================================
page = text(owner.get("/settings/packing"))
check("the page opens for the owner", "Packing Charges" in page)
check("both charges start at nothing",
      page.count('value="0.00"') >= 2, "starting values not 0.00")
check("each starts as once per order",
      len(re.findall(r'name="takeaway_packing_mode" value="order"\s+checked', page)) == 1
      and len(re.findall(r'name="delivery_packing_mode" value="order"\s+checked', page)) == 1,
      "a mode other than per-order was chosen")
first = place(owner, "takeaway", latte=2)
check("so a takeaway is charged only for its dishes",
      first["packing"] == 0 and first["total"] == Decimal("200.00"), first)
check("and is still written down as a takeaway", first["type"] == "takeaway", first)
check("the profile menu leads to it",
      "/settings/packing" in text(owner.get("/dashboard")))


# =====================================================================
print("\n=== 2. The owner sets them ===")
# =====================================================================
saved = owner.post("/settings/packing", data={
    "takeaway_packing": "10", "takeaway_packing_mode": "order",
    "delivery_packing": "5", "delivery_packing_mode": "item",
    "_csrf_token": csrf(owner)})
check("saving goes back where it came from", saved.status_code == 302)
check("and says so", any("Saved" in m for m in flashes(owner)))
row = db("SELECT takeaway_packing, takeaway_packing_mode, delivery_packing, "
         "delivery_packing_mode FROM cafes")[0]
check("the cafe keeps both amounts and both ways of charging",
      Decimal(str(row[0])) == 10 and row[1] == "order"
      and Decimal(str(row[2])) == 5 and row[3] == "item", repr(tuple(row)))
page = text(owner.get("/settings/packing"))
check("the page shows what was saved",
      'value="10.00"' in page and 'value="5.00"' in page)


def refused(values, words):
    before = tuple(db("SELECT takeaway_packing, delivery_packing FROM cafes")[0])
    data = {"takeaway_packing": "10", "takeaway_packing_mode": "order",
            "delivery_packing": "5", "delivery_packing_mode": "item",
            "_csrf_token": csrf(owner)}
    data.update(values)
    response = owner.post("/settings/packing", data=data)
    said = flashes(owner)
    after = tuple(db("SELECT takeaway_packing, delivery_packing FROM cafes")[0])
    return (response.status_code == 302 and before == after
            and any(words in m for m in said)), said


ok, said = refused({"takeaway_packing": "-1"}, "between")
check("a charge below nothing is refused", ok, said)
ok, said = refused({"delivery_packing": "1000.01"}, "between")
check("and one above the ceiling", ok, said)
ok, said = refused({"takeaway_packing": "ten"}, "as an amount")
check("and one that is not a number", ok, said)
ok, said = refused({"takeaway_packing": "nan"}, "as an amount")
check("'nan' is not a number either - it once sent the owner home "
      "with 'Something went wrong'", ok, said)
owner.post("/settings/packing", data={
    "takeaway_packing": "10", "takeaway_packing_mode": "weekly",
    "delivery_packing": "5", "delivery_packing_mode": "item",
    "_csrf_token": csrf(owner)})
flashes(owner)
check("a way of charging it does not know becomes once per order",
      db("SELECT takeaway_packing_mode FROM cafes")[0][0] == "order")


# =====================================================================
print("\n=== 3. What each kind of order is charged ===")
# =====================================================================
dine = place(owner, "dine_in", latte=3)
check("dine-in carries no container charge",
      dine["packing"] == 0 and dine["total"] == Decimal("300.00"), dine)
away = place(owner, "takeaway", latte=2, brownie=1)
check("a takeaway is charged once for the order, whatever is in it",
      away["packing"] == Decimal("10.00") and away["total"] == Decimal("310.00"), away)
out = place(owner, "delivery", latte=2, brownie=1)
check("a delivery is charged per item: three items, three containers",
      out["packing"] == Decimal("15.00") and out["total"] == Decimal("315.00"), out)
check("and each is written down as what it was",
      (dine["type"], away["type"], out["type"]) == ("dine_in", "takeaway", "delivery"))
plain = place(owner, None)
check("an order that does not say is dine-in",
      plain["type"] == "dine_in" and plain["packing"] == 0, plain)
odd = place(owner, "drive-thru")
check("and so is one that names a kind there is not",
      odd["type"] == "dine_in" and odd["packing"] == 0, odd)
dashed = place(owner, "Dine-In")
check("'Dine-In' however it is written is dine-in", dashed["type"] == "dine_in")

# The charge goes on after the discount and the tax, never through them.
owner.post("/settings/tax", data={"tax_percent": "10", "discount_percent": "50",
                                  "_csrf_token": csrf(owner)})
flashes(owner)
taxed = place(owner, "takeaway", latte=2)
check("the discount and the tax leave the container charge alone",
      taxed["packing"] == Decimal("10.00") and taxed["total"] == Decimal("120.00"),
      "200 less half is 100, plus 10%% tax is 110, plus 10 packing is 120 - got %s"
      % taxed)
owner.post("/settings/tax", data={"tax_percent": "0", "discount_percent": "0",
                                  "_csrf_token": csrf(owner)})
flashes(owner)

earlier = db("SELECT packing, total_amount FROM bills WHERE bill_id = %d"
             % first["bill_id"])[0]
check("a bill raised before the charges were set keeps what it was charged",
      Decimal(str(earlier[0])) == 0 and Decimal(str(earlier[1])) == Decimal("200.00"),
      repr(tuple(earlier)))

owner.post("/settings/packing", data={
    "takeaway_packing": "12.50", "takeaway_packing_mode": "item",
    "delivery_packing": "5", "delivery_packing_mode": "item",
    "_csrf_token": csrf(owner)})
flashes(owner)
changed = place(owner, "takeaway", latte=2)
check("a changed charge is used straight away, not the one remembered",
      changed["packing"] == Decimal("25.00"), changed)
owner.post("/settings/packing", data={
    "takeaway_packing": "10", "takeaway_packing_mode": "order",
    "delivery_packing": "5", "delivery_packing_mode": "item",
    "_csrf_token": csrf(owner)})
flashes(owner)


# =====================================================================
print("\n=== 4. The New Order screen ===")
# =====================================================================
page = text(owner.get("/orders/add"))
check("offers Dine-in, Takeaway and Delivery",
      all('data-order-type="%s"' % kind in page
          for kind in ("dine_in", "takeaway", "delivery")))
check("with Dine-in chosen to begin with",
      'id="orderTypeField" value="dine_in"' in page)
packing_js = re.search(r"var PACKING = (\{.*?\});", page)
check("and knows the cafe's charges, to add them up as the order is built",
      packing_js is not None and '"takeaway"' in packing_js.group(1)
      and '"item"' in packing_js.group(1), packing_js and packing_js.group(1))


# =====================================================================
print("\n=== 5. Wherever the bill is, the charge and the kind are ===")
# =====================================================================
detail = text(owner.get("/orders/%d" % out["order_id"]))
check("the order page names it a delivery", "Delivery" in detail)
check("and shows the packing line",
      "Packing (delivery)" in detail and "15.00" in detail)
bill = text(owner.get("/orders/%d/bill" % out["order_id"]))
check("the printed bill has the packing line",
      "Packing (delivery)" in bill and "15.00" in bill)
dine_bill = text(owner.get("/orders/%d/bill" % dine["order_id"]))
check("a dine-in bill has none", "Packing (" not in dine_bill)
kot = text(owner.get("/orders/%d/kot" % away["order_id"]))
check("the kitchen ticket says takeaway, marked out to be packed",
      "kot__type--packed" in kot and "Takeaway" in kot)
kot_dine = text(owner.get("/orders/%d/kot" % dine["order_id"]))
check("and a dine-in ticket is not marked for packing",
      "kot__type--packed" not in kot_dine and "Dine-in" in kot_dine)
billing = text(owner.get("/billing"))
check("the Billing list has a Packing column once any bill has packing",
      '<th class="col-tax">Packing</th>' in billing)
check("with the charge in it", 'data-label="Packing">₹15.00' in billing)
board = owner.get("/api/kitchen/board").get_json()
kinds = {row["order_id"]: row.get("order_type") for row in board["orders"]}
check("the kitchen screen is told which orders are to go",
      kinds.get(away["order_id"]) == "takeaway"
      and kinds.get(out["order_id"]) == "delivery", kinds)


# =====================================================================
print("\n=== 6. A table QR order is always eaten in ===")
# =====================================================================
with owner.session_transaction() as sess:
    cafe_id = sess["cafe_id"]
token = application.get_public_token(cafe_id)
guest = app.test_client()
guest.post("/m/%s/order" % token, data={"quantity_%d" % LATTE: "2",
                                         "order_type": "delivery"})
qr = db("SELECT order_id, order_type, source FROM orders "
        "ORDER BY order_id DESC")[0]
check("it is written down as dine-in, whatever the form says",
      qr[2] == "qr" and qr[1] == "dine_in", repr(tuple(qr)))
# Its bill is raised when the counter next opens Billing.
owner.get("/billing")
qr_bill = db("SELECT packing FROM bills WHERE order_id = %d" % qr[0])
check("and its bill carries no packing",
      len(qr_bill) == 1 and Decimal(str(qr_bill[0][0])) == 0, repr(qr_bill))


# =====================================================================
print("\n=== 7. Only the owner sets the charges ===")
# =====================================================================
owner.post("/users/add", data={
    "full_name": "Cara Cashier", "username": "cara", "role": "cashier",
    "phone_number": "", "password": "password123", "_csrf_token": csrf(owner)})
cashier = app.test_client()
cashier.post("/login", data={"username": "cara", "password": "password123"})
flashes(cashier)
check("a cashier cannot open the page",
      cashier.get("/settings/packing").status_code in (302, 403))
before = tuple(db("SELECT takeaway_packing, delivery_packing FROM cafes")[0])
cashier.post("/settings/packing", data={
    "takeaway_packing": "0", "takeaway_packing_mode": "order",
    "delivery_packing": "0", "delivery_packing_mode": "order",
    "_csrf_token": csrf(cashier)})
check("or change the charges",
      tuple(db("SELECT takeaway_packing, delivery_packing FROM cafes")[0]) == before)
check("and is not offered the link",
      "/settings/packing" not in text(cashier.get("/orders/add")))
staff_order = place(cashier, "takeaway", latte=1)
check("but a cashier's takeaway is charged like anyone's",
      staff_order["packing"] == Decimal("10.00"), staff_order)


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
