"""
The subscription: 15 days free, then Monthly (Rs 750) or Yearly (Rs 7,800 -
Rs 650 a month),
paid by UPI straight into the developer's account and confirmed by hand.

What has to hold:

  * a new cafe starts a 15-day free trial; cafes from before plans get
    one too;
  * only the cafe's admin sees and pays for the plan;
  * the UPI link carries the developer's UPI ID, the name on the account,
    the exact amount and our reference - nothing for the admin to type;
  * a UPI reference is twelve digits, and one payment cannot be claimed
    twice;
  * a confirmed payment runs on from the day the current plan ends, and
    its invoice is the cafe's alone;
  * reminders from a week before; three days' grace; then fifteen days
    of Refero Free - still taking orders, with Pro's parts locked; then
    the cafe rests - staff are told who can bring it back, the admin is
    taken to pay, the table QR sends customers to the counter - and
    paying brings everything back at once;
  * a trial and Free both lock sales figures, stock alerts, reviews,
    reports, the table QR, other roles and the cafe's own branding - they
    are for subscribers; Free has room for one teammate as staff; every
    lock leads to its line on the Subscription page, and the figures
    behind one are never sent;
  * a lifetime plan never ends.

Run with:  python tests/subscription_test.py
"""
import os
import re
import sys
from datetime import datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "subscription-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"
# Plans are what this suite is about: its cafes start on the trial.
os.environ["NEW_CAFE_PLAN"] = "trial"
os.environ["PLATFORM_UPI_ID"] = "refero.dev@okaxis"
os.environ["PLATFORM_UPI_NAME"] = "Rahul Dash"
os.environ["PLATFORM_PASSWORD"] = "console-pass-for-tests"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import logging                # noqa: E402
logging.disable(logging.CRITICAL)

import app as application     # noqa: E402

app = application.app
app.config["TESTING"] = True
app.config["MONITOR_SYNC"] = True

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % (detail,)))


def db(sql, params=()):
    rows = mysql_shim._DB.execute(sql, params).fetchall()
    mysql_shim._DB.commit()
    return [tuple(row) for row in rows]


def text(response):
    return response.get_data(as_text=True)


def token_of(html):
    found = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    return found.group(1) if found else ""


def flashes(client):
    with client.session_transaction() as sess:
        return [m for _, m in sess.pop("_flashes", [])]


def register(cafe, username):
    client = app.test_client()
    client.post("/register", data={
        "cafe_name": cafe, "full_name": cafe + " Admin", "username": username,
        "phone_number": "", "password": "Brew-Latte-42",
        "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
    cafe_id = db("SELECT cafe_id FROM cafes WHERE cafe_name = ?", (cafe,))[0][0]
    return client, cafe_id


def sign_in(cafe, username):
    client = app.test_client()
    client.post("/login", data={"cafe_name": cafe, "username": username,
                                "password": "Brew-Latte-42"})
    return client


def moment(value):
    return application._moment(value)


def set_until(cafe_id, until, plan=None, pending=None):
    db("UPDATE cafes SET plan_until = ?, plan_pending_at = ?%s WHERE cafe_id = ?"
       % (", plan = '%s'" % plan if plan else ""),
       (until.isoformat(sep=" "), pending.isoformat(sep=" ") if pending else None, cafe_id))
    application.cache_clear()


def choose(client, plan):
    page = text(client.get("/subscription"))
    return client.post("/subscription/choose", data={"plan": plan, "_csrf_token": token_of(page)})


def pay(client, location, utr):
    page = text(client.get(location))
    return client.post(location, data={"utr": utr, "_csrf_token": token_of(page)})


def approve(payment_id):
    connection = application.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    end = application.approve_plan_payment(cursor, payment_id)
    connection.commit()
    cursor.close()
    connection.close()
    application.cache_clear()
    return end


# =====================================================================
print("\n=== 1. A new cafe starts with 15 days free ===")
# =====================================================================
admin, CAFE = register("Plan Cafe", "plana")
plan, until = db("SELECT plan, plan_until FROM cafes WHERE cafe_id = ?", (CAFE,))[0]
now = application.utc_now()
check("its plan is the trial", plan == "trial", plan)
check("which runs fifteen days",
      abs((moment(until) - now) - timedelta(days=15)) < timedelta(minutes=1), (until, now))

with admin.session_transaction() as _sess:
    _token = _sess.get("_csrf_token", "")
admin.post("/users/add", data={"full_name": "Cara Staff", "username": "cara",
                               "role": "staff", "phone_number": "", "password": "Brew-Latte-42",
                               "_csrf_token": _token})
cashier = sign_in("Plan Cafe", "cara")
check("(a teammate on the team, as staff - the trial's one role)",
      db("SELECT role FROM users WHERE username = 'cara'") == [("staff",)])

page = text(admin.get("/subscription"))
check("the admin's Plan & billing page says when the trial ends",
      "Your trial ends" in page and application.plan_day(moment(until)) in page)
check("with a pill saying the trial is on", "Free trial active" in page)
check("offering Monthly at Rs 750 and Yearly at Rs 7,800 - saving Rs 1,200",
      "Choose monthly · ₹750" in page and "Choose yearly · ₹7,800" in page
      and "save ₹1,200" in page)
check("the yearly plan says it is Rs 650 a month", "Just ₹650 a month" in page)
check("rupees are grouped the Indian way",
      application.rupees(Decimal("123456")) == "₹1,23,456"
      and application.rupees(Decimal("12345678.50")) == "₹1,23,45,678.50"
      and application.rupees(Decimal("750.00")) == "₹750"
      and application.rupees(Decimal("7800")) == "₹7,800", application.rupees(Decimal("123456")))
check("and showing who is paid", "Paid to Rahul Dash" in page)
check("with no invoices yet, it says where they will be",
      "Your invoices will live here" in page)

# "What's included?" is read from the plan rules, not typed: the trial's
# column shows what it has and locks what is for subscribers.
def row(html, key):
    found = re.search(r'<tr id="unlock-%s"[^>]*>(.*?)</tr>' % key, html, re.S)
    return found.group(1) if found else ""


check("what's included compares the free trial with Refero Pro",
      re.search(r'<th scope="col"[^>]*>Free trial</th>\s*<th scope="col"[^>]*>Refero Pro</th>', page))
check("the trial's column locks reviews, reports, sales figures and the table QR",
      all("bill-locked" in row(page, key) and "bill-yes" in row(page, key)
          for key in ("reviews", "reports", "revenue", "stock_alerts", "table_qr")))
check("and says the trial's team is staff only, Pro's unlimited",
      "Staff only" in row(page, "team") and "Unlimited" in row(page, "team"))
check("its chips list what the trial includes, and nothing it does not",
      "Orders &amp; billing" in page and "Your team, as staff" in page
      and ">Reviews<" not in page.split('class="bill-chips"')[1].split("</ul>")[0])

dash = text(admin.get("/dashboard"))
check("the profile menu carries Subscription, with the trial's days left",
      'href="/subscription"' in dash and re.search(r"Trial · \d+ days left", dash))
check("a teammate is not offered it in the profile menu",
      'class="profile-dropdown__item">\n                            <i class="bi bi-gem">' not in text(cashier.get("/dashboard")))
staff_view = text(cashier.get("/subscription"))
check("but can read what's included - where every lock leads",
      "What's included?" in staff_view and 'id="unlocks"' in staff_view)
check("not choose, pay or see the invoices - that is the admin's",
      "Choose monthly" not in staff_view and "Invoices" not in staff_view
      and "Only your café's admin can subscribe" in staff_view)

# A cafe from before plans: its trial starts when this arrives.
db("UPDATE cafes SET plan = NULL, plan_until = NULL WHERE cafe_id = ?", (CAFE,))
connection = application.get_db_connection()
cursor = connection.cursor(dictionary=True)
application._start_plans(cursor)
connection.commit()
cursor.close()
connection.close()
plan, until = db("SELECT plan, plan_until FROM cafes WHERE cafe_id = ?", (CAFE,))[0]
check("a cafe from before plans is given its month too", plan == "trial" and until)

# =====================================================================
print("\n=== 2. The dates ===")
# =====================================================================
add = application.add_months
check("31 January and a month is the last of February",
      add(datetime(2027, 1, 31), 1) == datetime(2027, 2, 28)
      and add(datetime(2028, 1, 31), 1) == datetime(2028, 2, 29))
check("December and a month is January next year",
      add(datetime(2026, 12, 15), 1) == datetime(2027, 1, 15))
check("a year is twelve months", add(datetime(2026, 10, 1), 12) == datetime(2027, 10, 1))

state = application.plan_state
t = datetime(2026, 10, 1, 12, 0)
check("in its trial, a trial",
      state({"plan": "trial", "plan_until": t + timedelta(days=20)}, t)["status"] == "trial")
check("reminded from a week before",
      state({"plan": "monthly", "plan_until": t + timedelta(days=6)}, t)["remind"]
      and not state({"plan": "monthly", "plan_until": t + timedelta(days=9)}, t)["remind"])
check("ended, three days' grace",
      state({"plan": "monthly", "plan_until": t - timedelta(days=2)}, t)["status"] == "grace")
check("then it is Refero Free",
      state({"plan": "monthly", "plan_until": t - timedelta(days=4)}, t)["status"] == "free")
check("unless a payment is being checked",
      state({"plan": "monthly", "plan_until": t - timedelta(days=4),
             "plan_pending_at": t - timedelta(days=1)}, t)["status"] == "checking")
check("for three days, not for ever",
      state({"plan": "monthly", "plan_until": t - timedelta(days=9),
             "plan_pending_at": t - timedelta(days=4)}, t)["status"] == "free")
check("fifteen days of Free after the grace, then it rests",
      state({"plan": "monthly", "plan_until": t - timedelta(days=17)}, t)["status"] == "free"
      and state({"plan": "monthly", "plan_until": t - timedelta(days=19)}, t)["paused"])
check("the same after a trial",
      state({"plan": "trial", "plan_until": t - timedelta(days=19)}, t)["paused"])
check("a payment being checked keeps a resting cafe working",
      state({"plan": "monthly", "plan_until": t - timedelta(days=40),
             "plan_pending_at": t - timedelta(days=1)}, t)["status"] == "checking")
check("a lifetime plan never ends",
      state({"plan": "lifetime", "plan_until": t - timedelta(days=900)}, t)["status"] == "lifetime")

# =====================================================================
print("\n=== 3. Choosing a plan and paying by UPI ===")
# =====================================================================
response = choose(admin, "monthly")
location = response.headers.get("Location", "")
check("choosing Monthly makes its payment ready and goes to pay",
      response.status_code == 302 and "/subscription/pay/RF" in location, location)
reference = location.rsplit("/", 1)[-1]
check("choosing it again picks up the same one",
      choose(admin, "monthly").headers.get("Location") == location)
check("a plan that is not one is refused",
      choose(admin, "weekly").headers.get("Location", "").endswith("/subscription")
      and any("Monthly or Yearly" in m for m in flashes(admin)))

page = text(admin.get(location))
link = re.search(r'href="(upi://pay\?[^"]+)"', page)
check("the pay page opens the UPI app", link is not None)
query = parse_qs(urlparse(link.group(1).replace("&amp;", "&")).query) if link else {}
check("paid to the developer's UPI ID", query.get("pa") == ["refero.dev@okaxis"], query)
check("under the name on the account", query.get("pn") == ["Rahul Dash"], query)
check("for exactly Rs 750.00, in rupees", query.get("am") == ["750.00"] and query.get("cu") == ["INR"], query)

# Started at an old price and not yet paid: it asks today's.
db("UPDATE plan_payments SET amount = 650 WHERE reference = ?", (reference,))
again = re.search(r'href="(upi://pay\?[^"]+)"', text(admin.get(location)))
again = parse_qs(urlparse(again.group(1).replace("&amp;", "&")).query) if again else {}
check("a payment started before the price changed asks today's price",
      again.get("am") == ["750.00"]
      and Decimal(str(db("SELECT amount FROM plan_payments WHERE reference = ?",
                         (reference,))[0][0])) == Decimal("750"), again)
check("with our reference in the note, to match it in the statement",
      reference in (query.get("tn") or [""])[0], query)
check("and a code to scan from a computer", "<svg" in page and "Scan with any UPI app" in page)
check("the admin types nothing but the reference afterwards",
      page.count('<input type="text"') == 1 and 'name="utr"' in page)

for wrong in ("ABCDEFGHIJKL", "12345678901", "1234567890123"):
    pay(admin, location, wrong)
check("a UPI reference that is not twelve digits is refused, saying what it is",
      db("SELECT status FROM plan_payments WHERE reference = ?", (reference,))[0][0] == "awaiting"
      and any("12-digit" in m for m in flashes(admin)))

response = pay(admin, location, "4278 1234 5678")
status, utr = db("SELECT status, utr FROM plan_payments WHERE reference = ?", (reference,))[0]
check("twelve digits, spaces and all, are taken", status == "pending" and utr == "427812345678",
      (status, utr))
check("and the cafe is marked as waiting on a check",
      db("SELECT plan_pending_at FROM cafes WHERE cafe_id = ?", (CAFE,))[0][0] is not None)
page = text(admin.get("/subscription"))
check("the invoice list shows it being checked", "Being checked" in page and "427812345678" in page)

other, OTHER = register("Other Cafe", "otherx")
location2 = choose(other, "yearly").headers["Location"]
pay(other, location2, "427812345678")
check("the same UPI reference cannot be claimed by another cafe",
      db("SELECT status FROM plan_payments WHERE cafe_id = ?", (OTHER,))[0][0] == "awaiting"
      and any("already been used" in m for m in flashes(other)))

# =====================================================================
print("\n=== 4. Confirmed, it runs on from the end of the current plan ===")
# =====================================================================
trial_end = moment(db("SELECT plan_until FROM cafes WHERE cafe_id = ?", (CAFE,))[0][0])
payment_id = db("SELECT payment_id FROM plan_payments WHERE reference = ?", (reference,))[0][0]
end = approve(payment_id)
plan, until, pending = db("SELECT plan, plan_until, plan_pending_at FROM cafes WHERE cafe_id = ?", (CAFE,))[0]
check("the cafe is on Monthly", plan == "monthly", plan)
check("from the day its trial ended - no free day lost",
      moment(until) == application.add_months(trial_end, 1), (until, trial_end))
check("and nothing is waiting any more", pending is None)
check("approving it twice does nothing", approve(payment_id) is None)


def session_token(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


# Paid: the locks that were on the page open, once, and are gone.
dash = text(admin.get("/dashboard"))
check("paid, the sidebar's locks are drawn opening",
      dash.count('class="lock-opening lock-opening--nav"') == 2)
check("and so are the dashboard's",
      dash.count('class="lock-opening lock-opening--card"') == 3)
seen = admin.post("/api/plan/unlocked", headers={"X-CSRFToken": session_token(admin),
                                                 "X-Requested-With": "XMLHttpRequest"})
check("seen, the page says so", seen.status_code == 200 and (seen.get_json() or {}).get("ok"))
check("and they are not drawn again", 'class="lock-opening' not in text(admin.get("/dashboard")))
fresh = sign_in("Plan Cafe", "cara")
check("someone signing in after it, who never saw the locks, is not shown them opening",
      'class="lock-opening' not in text(fresh.get("/orders/add"))
      and 'class="lock-opening' not in text(fresh.get("/orders/add")))

page = text(admin.get("/subscription"))
check("the invoice is listed as paid, to download", "Paid" in page and "Download PDF" in page)
invoice = admin.get("/subscription/invoice/%d" % payment_id)
html = text(invoice)
check("the invoice is numbered and says who paid whom, how much",
      invoice.status_code == 200 and re.search(r"RF-\d{4}-\d{5}", html)
      and "Plan Cafe" in html and "Rahul Dash" in html and "₹750" in html
      and "427812345678" in html)
check("another cafe's admin cannot open it", other.get("/subscription/invoice/%d" % payment_id).status_code == 404)
check("nor can a cashier", cashier.get("/subscription/invoice/%d" % payment_id).status_code in (302, 303))

# Yearly, paid while Monthly still runs: added to the end of it - and
# said so before paying.
page = text(admin.get("/subscription"))
check("with a plan running, the page says one bought now continues from its end",
      "You're covered until" in page and application.plan_day(moment(until)) in page
      and "continues automatically" in page)
loc = choose(admin, "yearly").headers["Location"]
pay_page = text(admin.get(loc))
check("the yearly plan's page says it starts when the month ends, and runs a year from then",
      "This plan starts on" in pay_page and application.plan_day(moment(until)) in pay_page
      and application.plan_day(application.add_months(moment(until), 12)) in pay_page)
pay(admin, loc, "427800000001")
monthly_end = moment(until)
yearly_id = db("SELECT payment_id FROM plan_payments WHERE utr = '427800000001'")[0][0]
approve(yearly_id)
plan, until = db("SELECT plan, plan_until FROM cafes WHERE cafe_id = ?", (CAFE,))[0]
check("a year bought early is added to the end of the month already paid",
      plan == "yearly" and moment(until) == application.add_months(monthly_end, 12), (plan, until))
page = text(admin.get("/subscription"))
check("the plan, in order: the trial now, then the month, then the year, each on its own",
      page.index("Now · Free trial") < page.index("Next · Monthly") < page.index("Next · Yearly")
      and "starts automatically" in page)
check("and the pill names the plan paid for first, not the trial",
      "Refero Pro · Monthly" in page)

# Turned down: said, and the cafe is not left waiting.
loc = choose(other, "monthly").headers["Location"]
pay(other, loc, "999911112222")
rejected_id = db("SELECT payment_id FROM plan_payments WHERE utr = '999911112222'")[0][0]
connection = application.get_db_connection()
cursor = connection.cursor(dictionary=True)
application.reject_plan_payment(cursor, rejected_id, "No credit with this reference reached us.")
connection.commit()
cursor.close()
connection.close()
application.cache_clear()
page = text(other.get("/subscription"))
check("a payment that could not be found says why, on the cafe's own page",
      "No credit with this reference reached us." in page and "Not found" in page)
check("and the cafe is not left marked as waiting",
      db("SELECT plan_pending_at FROM cafes WHERE cafe_id = ?", (OTHER,))[0][0] is None)

# =====================================================================
print("\n=== 5. Reminders, grace, and then Refero Free ===")
# =====================================================================
now = application.utc_now()
set_until(OTHER, now + timedelta(days=5), plan="trial")
cashier_other = None
dash = text(other.get("/dashboard"))
check("five days from the end of the trial, the admin is reminded on every page",
      "Your free trial ends in 5 days" in dash and "See plans" in dash)

set_until(CAFE, now - timedelta(days=1), plan="monthly")
check("ended a day ago, the admin is told how long everything keeps working",
      "Everything keeps working until" in text(admin.get("/dashboard")))
check("the cashier is told to let the admin know - and still works",
      "please let your admin know" in text(cashier.get("/orders/add"))
      and cashier.get("/orders/add").status_code == 200)

set_until(CAFE, now - timedelta(days=4), plan="monthly")
check("past its grace the cafe is on Refero Free - and still works",
      cashier.get("/orders/add").status_code == 200
      and admin.get("/dashboard").status_code == 200
      and cashier.get("/api/kitchen/board").status_code == 200)
page = text(admin.get("/subscription"))
check("its page says so, and what still works", "Refero Free" in page
      and "keep working" in page)
check("and can be paid from", choose(admin, "monthly").status_code == 302)
check("its profile menu says Free",
      re.search(r'plan-chip--info">Free<', text(admin.get("/dashboard"))) is not None)

token = db("SELECT public_token FROM cafes WHERE cafe_id = ?", (CAFE,))[0][0] or \
    application.get_public_token(CAFE)
menu_page = app.test_client().get("/m/%s" % token)
check("customers can still order from the tables - nothing is paused",
      menu_page.status_code == 200 and "taking a little break" not in text(menu_page))

set_until(CAFE, now - timedelta(days=4), plan="monthly", pending=now - timedelta(hours=2))
check("with a payment being checked, Pro's parts open meanwhile",
      admin.get("/reviews").status_code == 200)

# =====================================================================
print("\n=== 5b. What Free, a trial and Pro each open ===")
# =====================================================================
set_until(CAFE, now - timedelta(days=9), plan="monthly")      # Free
dash = text(admin.get("/dashboard"))
check("on Free the dashboard keeps today's orders and the live list",
      'id="today-orders"' in dash and 'id="liveList"' in dash)
check("and locks sales, the average bill and stock to check, with a way to unlock",
      dash.count("kpi--locked") == 3 and "Unlock with Refero Pro" in dash)
check("the sales chart, best sellers, stock list and dish ratings are veiled",
      dash.count("panel--locked") == 4 and 'id="curveSvg"' not in dash
      and 'id="stock-alert-content"' not in dash)
stats = admin.get("/api/dashboard-stats").get_json()
check("the live figures are not sent at all - not hidden, absent",
      stats.get("today_revenue") is None and "low_stock" not in stats
      and "low_stock_items" not in stats, stats)
feed = admin.get("/api/dashboard-insights?period=week").get_json()
check("nor are the period's takings, comparisons or best sellers",
      feed.get("locked") is True and "sales" not in feed and "popular" not in feed
      and "curve" not in feed, sorted(feed))
check("Reviews leads to its line on the Subscription page",
      admin.get("/reviews").headers.get("Location", "").endswith("/subscription?feature=reviews#unlocks"))
check("so do Reports, and their download",
      admin.get("/reports").headers.get("Location", "").endswith("/subscription?feature=reports#unlocks")
      and admin.get("/reports/export").headers.get("Location", "").endswith("feature=reports#unlocks"))
check("and Name & Symbol",
      admin.get("/settings/branding").headers.get("Location", "").endswith("feature=branding#unlocks"))
check("a teammate is sent there too", cashier.get("/reviews").headers.get("Location", "")
      .endswith("/subscription?feature=reviews#unlocks"))
page = text(admin.get("/subscription?feature=reviews"))
check("which opens on what it unlocks, the asked-for line picked out",
      re.search(r'id="unlock-reviews" class="bill-row--asked"', page) is not None)
check("on Free the first column is Refero Free, with room for one teammate",
      re.search(r'<th scope="col"[^>]*>Refero Free</th>', page) and "1 teammate" in row(page, "team"))
check("the sidebar shows Reviews and Reports locked",
      dash.count('class="nav-lock"') >= 2 and 'href="/subscription?feature=reviews#unlocks"' in dash)

# One teammate, as staff.
with admin.session_transaction() as _sess:
    _token = _sess.get("_csrf_token", "")
response = admin.post("/users/add", data={"full_name": "Second Person", "username": "second1",
                                          "role": "staff", "phone_number": "",
                                          "password": "Brew-Latte-42", "_csrf_token": _token})
check("Free has room for one teammate - a second is sent to unlock the team",
      response.headers.get("Location", "").endswith("feature=team#unlocks")
      and db("SELECT COUNT(*) FROM users WHERE username = 'second1'")[0][0] == 0)
check("and Add teammate wears a lock", "bi-lock-fill\"></i> Add teammate" in text(admin.get("/users")))
db("DELETE FROM users WHERE username = 'cara'")
application.cache_clear()
response = admin.post("/users/add", data={"full_name": "Cash Ier", "username": "cashier9",
                                          "role": "cashier", "phone_number": "",
                                          "password": "Brew-Latte-42", "_csrf_token": _token})
check("with the room free, a cashier is still refused - a Pro role",
      db("SELECT COUNT(*) FROM users WHERE username = 'cashier9'")[0][0] == 0)
form = text(admin.get("/users/add"))
check("the form offers Manager and Cashier only as Pro",
      "Manager - Refero Pro" in form and "Cashier - Refero Pro" in form)
admin.post("/users/add", data={"full_name": "Stef Staff", "username": "stef",
                               "role": "staff", "phone_number": "",
                               "password": "Brew-Latte-42", "_csrf_token": _token})
check("and one teammate as staff is welcome",
      db("SELECT role FROM users WHERE username = 'stef'") == [("staff",)])
cashier = sign_in("Plan Cafe", "stef")

# Own branding: kept, not shown, until Pro.
db("UPDATE cafes SET brand_name = 'Plan Cafe Brand', brand_tagline = 'Our own words' WHERE cafe_id = ?",
   (CAFE,))
application.cache_clear()
dash = text(admin.get("/dashboard"))
check("on Free the sidebar carries Refero's name, not the cafe's own",
      "Plan Cafe Brand" not in dash and "Refero" in dash)

def table_qr_open():
    with app.test_request_context():
        _conn = application.get_db_connection()
        _cur = _conn.cursor(dictionary=True)
        _found = application.cafe_for_token(_cur, token, fresh=True)
        _cur.close()
        _conn.close()
    return _found is not None and application.table_ordering_open(_found)


set_until(CAFE, now - timedelta(days=9), plan="monthly")      # Free again
check("on Free the table QR takes no orders - customers are sent to the counter",
      not table_qr_open())
check("Table QR Code leads to its line on the Subscription page",
      admin.get("/settings/qr").headers.get("Location", "").endswith("feature=table_qr#unlocks"))
check("and the profile menu shows both table QR items locked",
      dash.count('href="/subscription?feature=table_qr#unlocks"') == 2
      and 'class="qr-switch-form"' not in dash)

# A trial: the cafe runs - orders, kitchen, billing, menu, stock - and
# what is for subscribers stays locked.
set_until(CAFE, now + timedelta(days=20), plan="trial")
dash = text(admin.get("/dashboard"))
check("on a trial sales, the average bill and stock to check are locked",
      dash.count("kpi--locked") == 3 and dash.count("panel--locked") == 4)
check("and their figures are not sent",
      admin.get("/api/dashboard-stats").get_json().get("today_revenue") is None
      and admin.get("/api/dashboard-insights?period=week").get_json().get("locked") is True)
check("Reviews and Reports lead to the Subscription page",
      admin.get("/reviews").headers.get("Location", "").endswith("feature=reviews#unlocks")
      and admin.get("/reports").headers.get("Location", "").endswith("feature=reports#unlocks"))
check("so does the table QR, and it takes no orders",
      admin.get("/settings/qr").headers.get("Location", "").endswith("feature=table_qr#unlocks")
      and not table_qr_open())
check("the till, the kitchen and billing are all open",
      admin.get("/orders/add").status_code == 200 and admin.get("/kitchen").status_code == 200
      and admin.get("/billing").status_code == 200)
page = text(admin.get("/subscription"))
check("the Subscription page says the trial runs the cafe and the rest is Pro's",
      "Your free trial" in page and "runs the café" in page and "In your free trial" not in page)
check("own branding is still Pro's", "Plan Cafe Brand" not in dash
      and admin.get("/settings/branding").status_code == 302)
check("and so are other roles", "Cashier - Refero Pro" in text(admin.get("/users/add")))

# Pro: all of it, the saved branding back at once.
set_until(CAFE, now + timedelta(days=20), plan="monthly")
dash = text(admin.get("/dashboard"))
check("on Pro the cafe's own name comes back - it was kept", "Plan Cafe Brand" in dash)
check("Name & Symbol opens", admin.get("/settings/branding").status_code == 200)
check("and any role can be added",
      "Refero Pro" not in text(admin.get("/users/add")).split('id="userRoleSelect"')[1].split("</select>")[0])
check("the figures, Reviews and Reports open",
      "kpi--locked" not in dash and admin.get("/reviews").status_code == 200
      and admin.get("/reports").status_code == 200)
check("and the table QR takes orders again",
      table_qr_open() and admin.get("/settings/qr").status_code == 200
      and 'class="qr-switch-form"' in dash)

# =====================================================================
print("\n=== 5c. After fifteen days of Free, the cafe rests ===")
# =====================================================================
set_until(CAFE, now - timedelta(days=16), plan="monthly")
dash = text(admin.get("/dashboard"))
check("two days before, the admin is told when it will pause",
      "your café pauses in" in dash and "See plans" in dash)
check("and so is the team", "please let your admin know" in text(cashier.get("/orders/add")))

set_until(CAFE, now - timedelta(days=19), plan="monthly")
staff_page = cashier.get("/orders/add")
check("then a teammate gets the plan-ended page, not the till",
      staff_page.status_code == 402 and "plan has ended" in text(staff_page), staff_page.status_code)
check("which says nothing has been lost, and offers to sign out",
      "Nothing has been lost" in text(staff_page) and 'href="/logout"' in text(staff_page))
check("the cafe's script requests are answered 402 too",
      cashier.get("/api/kitchen/board").status_code == 402)
check("the admin is taken to Subscription",
      admin.get("/dashboard").headers.get("Location", "").endswith("/subscription"))
page = text(admin.get("/subscription"))
check("which says it is paused and that nothing is lost",
      "paused since" in page and "Nothing has been lost" in page)
check("and can be paid from", choose(admin, "monthly").status_code == 302)
check("its profile menu says Paused",
      re.search(r'plan-chip--bad">Paused<', page) is not None)
check("the table QR takes no orders - customers are sent kindly to the counter",
      not table_qr_open())
check("a teammate can still report a problem",
      cashier.get("/report-problem").status_code == 200)

set_until(CAFE, now - timedelta(days=19), plan="monthly", pending=now - timedelta(hours=2))
check("a payment being checked brings it back meanwhile",
      cashier.get("/orders/add").status_code == 200
      and admin.get("/reviews").status_code == 200)

# =====================================================================
print("\n=== 6. Free for life ===")
# =====================================================================
set_until(CAFE, application.PLATFORM_LIFETIME_UNTIL, plan="lifetime")
page = text(admin.get("/subscription"))
check("a lifetime cafe's page says it never ends, and offers no plans",
      "never ends" in page and "Choose monthly" not in page)
check("its people work, with no reminders",
      cashier.get("/orders/add").status_code == 200
      and "plan-notice" not in text(admin.get("/dashboard")))

# =====================================================================
print("\n=== 7. Until the UPI ID is set, nobody is sent to pay ===")
# =====================================================================
saved = application.PLATFORM_UPI_ID
application.PLATFORM_UPI_ID = ""
try:
    response = choose(other, "monthly")
    check("choosing a plan says paying is being set up",
          response.headers.get("Location", "").endswith("/subscription")
          and any("being set up" in m for m in flashes(other)))
finally:
    application.PLATFORM_UPI_ID = saved

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
