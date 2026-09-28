"""
The dashboard's figures, and the search box in the header.

The dashboard reads the whole period at once: sales, orders and the
average bill against the period before; the sales curve; today's orders
with the open ones first; the best sellers; the stock to check. Each is
worked out here from rows whose answer is known, including the rows that
must NOT count - a cancelled order, an unpaid bill, a sale later in the
day yesterday than it is now.

Search answers from pages, food, categories, orders and people, and only
ever offers somebody what they could open anyway.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/dashboard_feed_test.py
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
os.environ["SECRET_KEY"] = "dashboard-feed-secret"
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


def open_cafe(cafe, user):
    client = app.test_client()
    client.post("/register", data={
        "cafe_name": cafe, "full_name": user.title(), "username": user,
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)
    return client


def db(sql, params=()):
    rows = mysql_shim._DB.execute(sql, params).fetchall()
    mysql_shim._DB.commit()
    return rows


def stamp(moment):
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def feed(client, period="today"):
    return client.get("/api/dashboard-insights?period=" + period).get_json()


# =====================================================================
print("\n=== 0. A cafe with a day behind it ===")
# =====================================================================
a = open_cafe("Feed Cafe", "feedboss")
a.post("/categories/add", data={"category_name": "Hot", "description": "",
                                "_csrf_token": csrf(a)}, follow_redirects=True)
a.post("/categories/add", data={"category_name": "Bakes", "description": "",
                                "_csrf_token": csrf(a)}, follow_redirects=True)
options = dict((name.strip(), value) for value, name in re.findall(
    r'<option value="(\d+)">\s*([^<]+?)\s*</option>',
    a.get("/foods/add").get_data(as_text=True)))
for food, category, price in (("Masala Chai", "Hot", "100"),
                              ("Filter Coffee", "Hot", "60"),
                              ("Banana Bread", "Bakes", "50")):
    a.post("/foods/add", data={
        "food_name": food, "category_id": options[category], "price": price,
        "quantity": "80", "minimum_stock": "2", "description": "Fresh",
        "_csrf_token": csrf(a)}, follow_redirects=True)
FOOD = dict((name, fid) for fid, name in db(
    "SELECT food_id, food_name FROM foods"))


def order(client, **lines):
    client.post("/orders/add", data=dict(
        [("quantity_%d" % FOOD[name.replace("_", " ")], str(qty))
         for name, qty in lines.items()] + [("_csrf_token", csrf(client))]),
        follow_redirects=True)
    return db("SELECT MAX(order_id) FROM orders")[0][0]


o1 = order(a, Masala_Chai=2)                  # paid
o2 = order(a, Banana_Bread=1)                 # paid, and made
o3 = order(a, Filter_Coffee=1)                # left unpaid
o4 = order(a, Masala_Chai=5)                  # cancelled
for oid in (o1, o2):
    bill = db("SELECT bill_id FROM bills WHERE order_id = ?", (oid,))[0][0]
    a.post("/billing/mark-paid/%d" % bill, data={"_csrf_token": csrf(a)},
           follow_redirects=True)
a.post("/orders/complete/%d" % o2, data={"_csrf_token": csrf(a)},
       follow_redirects=True)
a.post("/orders/cancel/%d" % o4, data={"_csrf_token": csrf(a)},
       follow_redirects=True)


def total_of(oid):
    return float(db("SELECT total_amount FROM bills WHERE order_id = ?",
                    (oid,))[0][0])


PAID_TODAY = total_of(o1) + total_of(o2)
print("  paid today: %.2f over 2 bills" % PAID_TODAY)

# Yesterday, written in by hand: one sale earlier in the day than now,
# which today is compared against, and one later, which it is not.
now = application.utc_now()
cafe_owner = db("SELECT user_id, cafe_id FROM users WHERE username = 'feedboss'")[0]


def old_sale(moment, amount):
    db("INSERT INTO orders (total_amount, order_status, user_id, cafe_id, "
       "source, order_day, daily_no, order_date, public_ref) "
       "VALUES (?, 'Completed', ?, ?, 'counter', ?, 1, ?, ?)",
       (amount, cafe_owner[0], cafe_owner[1], moment.date().isoformat(),
        stamp(moment), "ref%d" % int(amount * 100 + moment.hour)))
    oid = db("SELECT MAX(order_id) FROM orders")[0][0]
    db("INSERT INTO bills (order_id, subtotal, tax, discount, total_amount, "
       "payment_method, payment_status, bill_date) "
       "VALUES (?, ?, 0, 0, ?, 'Cash', 'Paid', ?)",
       (oid, amount, amount, stamp(moment)))


earlier = now - timedelta(days=1, minutes=5)
later = now - timedelta(days=1) + timedelta(minutes=30)
same_clock = (earlier.date() == now.date() - timedelta(days=1)
              and later.date() == now.date() - timedelta(days=1))
old_sale(earlier, 125.0)
old_sale(later, 999.0)
if not same_clock:
    print("  (close to midnight: yesterday's two sales do not both fall "
          "on yesterday, so the comparison checks are skipped)")


# =====================================================================
print("\n=== 1. Today's three figures ===")
# =====================================================================
today = feed(a)
check("sales are the paid bills, and only those",
      abs(today["sales"]["value"] - round(PAID_TODAY, 2)) < 0.01,
      "sales read %s, the paid bills come to %.2f"
      % (today["sales"]["value"], PAID_TODAY))
check("orders count everything taken except the cancelled one",
      today["orders"]["value"] == 3,
      "%s orders counted" % today["orders"]["value"])
check("the average bill is the sales over the paid bills",
      abs(today["average"]["value"] - round(PAID_TODAY / 2, 2)) < 0.01,
      "the average reads %s" % today["average"]["value"])
if same_clock:
    check("today is set against the same part of yesterday",
          today["sales"]["change"] == round((PAID_TODAY - 125.0) / 125.0 * 100, 1),
          "change reads %s - the later sale yesterday was counted too"
          % today["sales"]["change"])
check("and it says what it is comparing with",
      today["versus"] == "vs. this time yesterday", today["versus"])

week = feed(a, "week")
if same_clock:
    check("seven days take all of yesterday",
          abs(week["sales"]["value"] - round(PAID_TODAY + 125.0 + 999.0, 2)) < 0.01,
          "the week reads %s" % week["sales"]["value"])
check("a period the feed does not know is today",
      feed(a, "fortnight")["period"] == "today")


# =====================================================================
print("\n=== 2. The curve ===")
# =====================================================================
check("today's curve is by the hour",
      today["curve"] and all(p["label"].endswith(("AM", "PM"))
                             for p in today["curve"]),
      "labels: %s" % [p["label"] for p in today["curve"]])
check("and it adds up to today's sales",
      abs(sum(p["value"] for p in today["curve"]) - PAID_TODAY) < 0.01,
      "the curve adds to %.2f" % sum(p["value"] for p in today["curve"]))
check("seven days is seven points", len(week["curve"]) == 7,
      "%d points" % len(week["curve"]))
check("thirty days is thirty", len(feed(a, "month")["curve"]) == 30)
check("the curve says when it was busiest",
      today["story"].startswith("Busiest at"), today["story"])


# =====================================================================
print("\n=== 3. Live orders ===")
# =====================================================================
live = today["live"]
statuses = [row["status"] for row in live]
check("today's orders are listed", len(live) == 4, "%d listed" % len(live))
check("the ones still in the kitchen come first",
      statuses[:2] == ["Pending", "Pending"]
      and set(statuses[2:]) == {"Completed", "Cancelled"},
      "order: %s" % statuses)
check("and the count waiting says how many",
      today["waiting"] == 2, "waiting reads %s" % today["waiting"])
check("each carries its item count",
      [row["items"] for row in live if row["order_id"] == o1] == [2])


# =====================================================================
print("\n=== 4. Best sellers ===")
# =====================================================================
names = [row["name"] for row in today["popular"]]
check("sold is counted from orders that went through",
      names[0] == "Masala Chai" and today["popular"][0]["sold"] == 2,
      "top is %s - the cancelled five chai were counted"
      % today["popular"][:1])
check("with its category", today["popular"][0]["category"] == "Hot")
check("every dish sold today is there",
      set(names) == {"Masala Chai", "Banana Bread", "Filter Coffee"}, names)


# =====================================================================
print("\n=== 5. Who can read it ===")
# =====================================================================
a.post("/users/add", data={"full_name": "Kiran Cashier", "username": "kiran",
                           "role": "cashier", "phone_number": "",
                           "password": "password123", "_csrf_token": csrf(a)},
       follow_redirects=True)
staff = app.test_client()
staff.post("/login", data={"username": "kiran", "password": "password123"},
           follow_redirects=True)
answer = staff.get("/api/dashboard-insights")
check("a cashier cannot read the owner's figures",
      answer.status_code in (302, 303, 403)
      or "sales" not in (answer.get_json(silent=True) or {}),
      "status %s" % answer.status_code)

b = open_cafe("Other Cafe", "otherboss")
other = feed(b)
check("another cafe starts from nothing",
      other["sales"]["value"] == 0 and other["orders"]["value"] == 0
      and not other["live"] and not other["popular"],
      "it sees %s" % {k: other[k] for k in ("sales", "orders")})

page = a.get("/dashboard").get_data(as_text=True)
check("the dashboard page draws the four figures",
      all(marker in page for marker in
          ('id="kpiSales"', 'id="kpiOrders"', 'id="kpiAverage"', 'id="kpiStock"')))
check("the curve, live orders, best sellers and stock",
      all(marker in page for marker in
          ('id="curveSvg"', 'id="liveList"', 'id="popularBody"',
           'id="stock-alert-panel"')))


# =====================================================================
print("\n=== 6. Search ===")
# =====================================================================
def hits(client, query):
    html = client.get("/search", query_string={"q": query}).get_data(as_text=True)
    return re.findall(r'search-hit__name">([^<]+)<', html)


check("a dish by part of its name", "Masala Chai" in hits(a, "chai"))
check("a dish by its category", set(hits(a, "bakes")) >= {"Banana Bread", "Bakes"},
      hits(a, "bakes"))
check("a page by what it does", "Tax &amp; Discount" in hits(a, "tax"),
      hits(a, "tax"))
check("an order by the number the kitchen calls",
      any(h.startswith("Order #") for h in hits(a, "#1")), hits(a, "#1"))
check("a person, for the owner", "Kiran Cashier" in hits(a, "kiran"),
      hits(a, "kiran"))
check("% is a percent sign, not everything",
      hits(a, "%") == [], hits(a, "%"))
check("nothing asked, nothing listed", hits(a, "") == [])

check("a cashier can search", staff.get("/search?q=chai").status_code == 200)
check("and finds the menu", "Masala Chai" in hits(staff, "chai"))
check("but not the owner's settings", hits(staff, "tax") == [],
      hits(staff, "tax"))
check("nor the people", "Kiran Cashier" not in hits(staff, "kiran"),
      hits(staff, "kiran"))
check("one cafe cannot find another's food",
      hits(b, "chai") == [], hits(b, "chai"))



# =====================================================================
print("\n=== 7. The team page ===")
# =====================================================================
# kiran signed in above; that is the time the list shows for them.
seen = db("SELECT last_login_at FROM users WHERE username = 'kiran'")[0][0]
check("signing in records when", seen is not None, "no time stamped")

team = a.get("/users").get_data(as_text=True)


def row_of(username):
    found = re.search(r'<tr data-user-id="\d+" data-username="%s".*?</tr>'
                      % re.escape(username), team, re.S)
    return found.group(0) if found else ""


owner_row, kiran_row = row_of("feedboss"), row_of("kiran")
check("the owner is marked as the owner",
      ">Owner</span>" in owner_row, owner_row[:300])
check("and, looking at the list, is active now",
      '>Now</td>' in owner_row)
check("a teammate shows when they last signed in",
      "Today, " in kiran_row, re.sub(r"\s+", " ", kiran_row)[:400])
check("every row carries what the filter reads",
      'data-kind="owner"' in owner_row and 'data-kind="cashier"' in kiran_row)
check("the owner's menu offers no delete",
      "/delete" not in owner_row)
check("a teammate's offers edit, deactivate and delete",
      all(word in kiran_row for word in ("/edit", "/toggle", "/delete")))

counts = dict(re.findall(
    r'<strong>(\w+)</strong>\s*<small>[^<]*</small>\s*</span>\s*'
    r'<span class="count-pill[^"]*">\s*(\d+) member', team))
check("the roles are counted",
      counts.get("Owner") == "1" and counts.get("Cashier") == "1"
      and counts.get("Admin") == "0",
      "counts: %s" % counts)


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
