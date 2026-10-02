"""
Reports: a period, everything in it, the period before, and the CSV.

Worked out from rows whose answer is known - including the ones that must
not count: a cancelled order, an unpaid bill, a sale from before the period.
The page and the export are built from the same function, so the file a
cafe downloads is checked against the same figures as the page.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/reports_test.py
"""
import os
import re
import sys
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "reports-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

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


def window(**args):
    with app.test_request_context("/reports", query_string=args):
        return application.report_window(application.request.args)


def report(client, **args):
    """The report's own numbers, as the page was built from them."""
    with app.test_request_context("/reports", query_string=args):
        with client.session_transaction() as sess:
            for key, value in sess.items():
                application.session[key] = value
        connection = application.get_db_connection()
        cursor = connection.cursor(dictionary=True)
        try:
            return application.build_report(
                cursor, application.scope_user_id(), application.request.args)
        finally:
            cursor.close()
            connection.close()


# =====================================================================
print("\n=== 1. Which days a report covers ===")
# =====================================================================
today = application.utc_now().date()
plain = window()
check("with nothing asked, the last seven days",
      plain["period"] == "7d" and plain["first"] == today - timedelta(days=6)
      and plain["last"] == today)
check("and it is set against the seven before",
      plain["prev_last"] == today - timedelta(days=7)
      and plain["prev_first"] == today - timedelta(days=13), plain)
check("today, against this time yesterday",
      window(period="today")["versus"] == "vs. this time yesterday")
check("this month starts on the first",
      window(period="month")["first"] == today.replace(day=1))
check("all time has no edges and nothing to compare with",
      window(period="all")["first"] is None
      and window(period="all")["prev_first"] is None)
old_link = window(from_date="2026-09-01", to_date="2026-09-10")
check("an address with only dates on it is a custom period, as links were",
      old_link["period"] == "custom" and old_link["first"].day == 1
      and old_link["last"].day == 10)
check("dates the wrong way round are put the right way round",
      window(period="custom", from_date="2026-09-10",
             to_date="2026-09-01")["first"].day == 1)
check("a date that is not a date is ignored",
      window(from_date="soon")["period"] == "7d")


# =====================================================================
print("\n=== 2. A cafe with two weeks behind it ===")
# =====================================================================
a = app.test_client()
a.post("/register", data={
    "cafe_name": "Report Cafe", "full_name": "Rita Owner", "username": "rita",
    "phone_number": "", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
for category in ("Coffee", "Bakes"):
    a.post("/categories/add", data={"category_name": category,
                                    "description": "", "_csrf_token": csrf(a)},
           follow_redirects=True)
options = dict((name.strip(), value) for value, name in re.findall(
    r'<option value="(\d+)">\s*([^<]+?)\s*</option>',
    a.get("/foods/add").get_data(as_text=True)))
for food, category, price in (("Mocha", "Coffee", "150"),
                              ("Scone", "Bakes", "50")):
    a.post("/foods/add", data={
        "food_name": food, "category_id": options[category], "price": price,
        "quantity": "90", "minimum_stock": "2", "description": "",
        "_csrf_token": csrf(a)}, follow_redirects=True)
FOOD = dict((name, fid) for fid, name in db("SELECT food_id, food_name FROM foods"))


def order(**lines):
    a.post("/orders/add", data=dict(
        [("quantity_%d" % FOOD[name], str(qty)) for name, qty in lines.items()]
        + [("_csrf_token", csrf(a))]), follow_redirects=True)
    return db("SELECT MAX(order_id) FROM orders")[0][0]


def pay(order_id, method="Cash"):
    db("UPDATE bills SET payment_method = ? WHERE order_id = ?", (method, order_id))
    bill = db("SELECT bill_id FROM bills WHERE order_id = ?", (order_id,))[0][0]
    a.post("/billing/mark-paid/%d" % bill, data={"_csrf_token": csrf(a)},
           follow_redirects=True)


def total(order_id):
    return float(db("SELECT total_amount FROM bills WHERE order_id = ?",
                    (order_id,))[0][0])


o1 = order(Mocha=2)          # paid in cash
o2 = order(Scone=4)          # paid by UPI
o3 = order(Mocha=1)          # never paid
o4 = order(Mocha=3)          # cancelled
pay(o1, "Cash")
pay(o2, "UPI")
a.post("/orders/cancel/%d" % o4, data={"_csrf_token": csrf(a)},
       follow_redirects=True)

# Ten days ago: in the seven days before this week, and not in this week.
owner, cafe = db("SELECT user_id, cafe_id FROM users WHERE username = 'rita'")[0]
earlier = application.utc_now() - timedelta(days=10)
stamp = earlier.strftime("%Y-%m-%d %H:%M:%S")
db("INSERT INTO orders (total_amount, order_status, user_id, cafe_id, source, "
   "order_day, daily_no, order_date, public_ref) "
   "VALUES (100, 'Completed', ?, ?, 'counter', ?, 1, ?, 'old-report-ref')",
   (owner, cafe, earlier.date().isoformat(), stamp))
old_order = db("SELECT MAX(order_id) FROM orders")[0][0]
db("INSERT INTO bills (order_id, subtotal, tax, discount, total_amount, "
   "payment_method, payment_status, bill_date) "
   "VALUES (?, 100, 0, 0, 100, 'Cash', 'Paid', ?)", (old_order, stamp))
db("INSERT INTO order_items (order_id, food_id, item_name, quantity, price, subtotal) "
   "VALUES (?, ?, 'Mocha', 1, 100, 100)", (old_order, FOOD["Mocha"]))

PAID = total(o1) + total(o2)


# =====================================================================
print("\n=== 3. The four figures ===")
# =====================================================================
week = report(a)
k = week["kpis"]
check("net sales are the paid bills of orders that went through",
      abs(k["sales"]["value"] - PAID) < 0.01,
      "sales read %.2f, the paid bills come to %.2f" % (k["sales"]["value"], PAID))
check("orders leave the cancelled one out", k["orders"]["value"] == 3,
      "%s orders" % k["orders"]["value"])
check("the average ticket is sales over paid bills",
      abs(k["average"]["value"] - PAID / 2) < 0.01, k["average"])
check("the cancelled one is counted as cancelled", k["cancelled"]["value"] == 1)
check("and as a share of everything taken", k["cancelled"]["rate"] == 25,
      "%s%%" % k["cancelled"]["rate"])
check("sales are set against the week before",
      k["sales"]["change"] == round((PAID - 100) / 100 * 100, 1),
      "change reads %s" % k["sales"]["change"])
check("and so are orders",
      k["orders"]["change"] == round((3 - 1) / 1 * 100, 1), k["orders"])
check("bills raised and bills paid",
      week["bills"] == 4 and week["paid_bills"] == 2,
      "%s raised, %s paid" % (week["bills"], week["paid_bills"]))
alltime = report(a, period="all")
check("all time takes the older sale in and compares with nothing",
      abs(alltime["kpis"]["sales"]["value"] - (PAID + 100)) < 0.01
      and alltime["kpis"]["sales"]["change"] is None, alltime["kpis"]["sales"])


# =====================================================================
print("\n=== 4. The curve, the items, the categories ===")
# =====================================================================
check("seven days is seven points", len(week["curve"]) == 7)
check("and they add up to the sales",
      abs(sum(p["sales"] for p in week["curve"]) - PAID) < 0.01)
check("and to the orders", sum(p["orders"] for p in week["curve"]) == 3)
check("a day is an hour-by-hour curve",
      all(p["label"].endswith(("AM", "PM")) for p in report(a, period="today")["curve"]))

names = [i["name"] for i in week["items"]]
check("the dish sold most leads", names[0] == "Scone",
      "order: %s" % names)
mocha = [i for i in week["items"] if i["name"] == "Mocha"][0]
check("cancelled units are not counted as sold", mocha["qty"] == 3,
      "Mocha sold %s - the cancelled three were counted" % mocha["qty"])
check("each dish is set against the week before",
      mocha["change"] == 200.0, "Mocha's change reads %s" % mocha["change"])
shares = dict((c["name"], c["share"]) for c in week["categories"])
check("sales are split by category",
      set(shares) == {"Coffee", "Bakes"} and 99 <= sum(shares.values()) <= 101,
      shares)


# =====================================================================
print("\n=== 5. Payments and cancellations ===")
# =====================================================================
methods = dict((p["method"], p) for p in week["payments"])
check("the payment summary is kept, by method",
      set(methods) == {"Cash", "UPI"}
      and abs(methods["UPI"]["amount"] - total(o2)) < 0.01, methods)
check("unpaid and cancelled bills are not in it",
      sum(p["bills"] for p in week["payments"]) == 2)
check("the cancelled order is listed",
      [row["order_id"] for row in week["cancelled"]] == [o4])

page = a.get("/reports").get_data(as_text=True)
check("the page shows the payment summary",
      "Payment summary" in page and "UPI" in page and "Cash" in page)
check("and every dish sold, by name", "Mocha" in page and "Scone" in page)
check("and which days it covers", "report-shown" in page)
check("a period by name answers too",
      all(a.get("/reports?period=" + p).status_code == 200
          for p in ("today", "7d", "30d", "month", "all", "custom")))


# =====================================================================
print("\n=== 6. The export ===")
# =====================================================================
export = a.get("/reports/export?period=7d")
text = export.get_data(as_text=True)
check("it is a CSV download",
      export.status_code == 200
      and "text/csv" in export.headers.get("Content-Type", "")
      and "attachment" in export.headers.get("Content-Disposition", ""),
      "%s %s" % (export.status_code, export.headers.get("Content-Type")))
check("with the same net sales as the page",
      ("Net sales,%.2f" % PAID) in text, text[:300])
check("every dish sold",
      "Mocha,Coffee,3," in text and "Scone,Bakes,4," in text)
check("and the payment summary", "UPI,1," in text and "Cash,1," in text)

a.post("/users/add", data={"full_name": "Cal Cashier", "username": "calc",
                           "role": "cashier", "phone_number": "",
                           "password": "Brew-Latte-42", "_csrf_token": csrf(a)},
       follow_redirects=True)
staff = app.test_client()
staff.post("/login", data={"username": "calc", "password": "Brew-Latte-42"},
           follow_redirects=True)
for path in ("/reports", "/reports/export"):
    reply = staff.get(path)
    check("a cashier cannot open %s" % path,
          reply.status_code in (302, 303, 403)
          and "text/csv" not in reply.headers.get("Content-Type", ""),
          "status %s" % reply.status_code)

b = app.test_client()
b.post("/register", data={
    "cafe_name": "Other Cafe", "full_name": "Otto", "username": "otto",
    "phone_number": "", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
other = report(b, period="all")
check("another cafe's report starts from nothing",
      other["kpis"]["sales"]["value"] == 0 and not other["items"]
      and "Mocha" not in b.get("/reports/export?period=all").get_data(as_text=True))


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
