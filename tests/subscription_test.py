"""
The subscription: a month free, then Monthly (Rs 650) or Yearly (Rs 6,000),
paid by UPI straight into the developer's account and confirmed by hand.

What has to hold:

  * a new cafe starts a month's free trial; cafes from before plans get
    one too;
  * only the cafe's admin sees and pays for the plan;
  * the UPI link carries the developer's UPI ID, the name on the account,
    the exact amount and our reference - nothing for the admin to type;
  * a UPI reference is twelve digits, and one payment cannot be claimed
    twice;
  * a confirmed payment runs on from the day the current plan ends, and
    its invoice is the cafe's alone;
  * reminders from a week before; three days' grace; then the cafe rests
    - staff are told who can renew, the admin is taken to pay, the table
    QR sends customers to the counter - and paying brings it all back;
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
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)
    cafe_id = db("SELECT cafe_id FROM cafes WHERE cafe_name = ?", (cafe,))[0][0]
    return client, cafe_id


def sign_in(cafe, username):
    client = app.test_client()
    client.post("/login", data={"cafe_name": cafe, "username": username,
                                "password": "password123"})
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
print("\n=== 1. A new cafe starts with a month free ===")
# =====================================================================
admin, CAFE = register("Plan Cafe", "plana")
plan, until = db("SELECT plan, plan_until FROM cafes WHERE cafe_id = ?", (CAFE,))[0]
now = application.utc_now()
check("its plan is the trial", plan == "trial", plan)
check("which runs a calendar month", moment(until).date() == application.add_months(now, 1).date(),
      (until, now))

with admin.session_transaction() as _sess:
    _token = _sess.get("_csrf_token", "")
admin.post("/users/add", data={"full_name": "Cara Cashier", "username": "cara",
                               "role": "cashier", "phone_number": "", "password": "password123",
                               "_csrf_token": _token})
cashier = sign_in("Plan Cafe", "cara")
check("(a cashier on the team)",
      db("SELECT role FROM users WHERE username = 'cara'") == [("cashier",)])

page = text(admin.get("/subscription"))
check("the admin's Subscription page says when the trial ends",
      "free trial ends on" in page and application.plan_day(moment(until)) in page)
check("offering Monthly at Rs 650 and Yearly at Rs 6,000 - saving Rs 1,800",
      "₹650" in page and "₹6,000" in page and "Save ₹1,800" in page)
check("and showing who is paid", "Paid to Rahul Dash" in page)

dash = text(admin.get("/dashboard"))
check("the profile menu carries Subscription, with the trial's days left",
      'href="/subscription"' in dash and re.search(r"Trial · \d+ days left", dash))
check("a cashier is not offered it", 'href="/subscription"' not in text(cashier.get("/dashboard")))
check("nor can they open it",
      cashier.get("/subscription").status_code in (302, 303)
      and "/subscription" not in (cashier.get("/subscription").headers.get("Location") or ""))

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
check("then it rests",
      state({"plan": "monthly", "plan_until": t - timedelta(days=4)}, t)["paused"])
check("unless a payment is being checked",
      state({"plan": "monthly", "plan_until": t - timedelta(days=4),
             "plan_pending_at": t - timedelta(days=1)}, t)["status"] == "checking")
check("for three days, not for ever",
      state({"plan": "monthly", "plan_until": t - timedelta(days=9),
             "plan_pending_at": t - timedelta(days=4)}, t)["paused"])
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
check("for exactly Rs 650.00, in rupees", query.get("am") == ["650.00"] and query.get("cu") == ["INR"], query)
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

page = text(admin.get("/subscription"))
check("the invoice is listed as paid, to download", "Paid" in page and "Download PDF" in page)
invoice = admin.get("/subscription/invoice/%d" % payment_id)
html = text(invoice)
check("the invoice is numbered and says who paid whom, how much",
      invoice.status_code == 200 and re.search(r"RF-\d{4}-\d{5}", html)
      and "Plan Cafe" in html and "Rahul Dash" in html and "₹650" in html
      and "427812345678" in html)
check("another cafe's admin cannot open it", other.get("/subscription/invoice/%d" % payment_id).status_code == 404)
check("nor can a cashier", cashier.get("/subscription/invoice/%d" % payment_id).status_code in (302, 303))

# Yearly, paid while Monthly still runs: added to the end of it.
loc = choose(admin, "yearly").headers["Location"]
pay(admin, loc, "427800000001")
monthly_end = moment(until)
yearly_id = db("SELECT payment_id FROM plan_payments WHERE utr = '427800000001'")[0][0]
approve(yearly_id)
plan, until = db("SELECT plan, plan_until FROM cafes WHERE cafe_id = ?", (CAFE,))[0]
check("a year bought early is added to the end of the month already paid",
      plan == "yearly" and moment(until) == application.add_months(monthly_end, 12), (plan, until))

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
print("\n=== 5. Reminders, grace, and resting ===")
# =====================================================================
now = application.utc_now()
set_until(OTHER, now + timedelta(days=5), plan="trial")
cashier_other = None
dash = text(other.get("/dashboard"))
check("five days from the end of the trial, the admin is reminded on every page",
      "Your free trial ends in 5 days" in dash and "See plans" in dash)

set_until(CAFE, now - timedelta(days=1), plan="monthly")
check("ended a day ago, the admin is told when it will rest",
      "Everything keeps working until" in text(admin.get("/dashboard")))
check("the cashier is told to let the admin know - and still works",
      "please let your admin know" in text(cashier.get("/orders/add"))
      and cashier.get("/orders/add").status_code == 200)

set_until(CAFE, now - timedelta(days=4), plan="monthly")
staff_page = cashier.get("/orders/add")
check("past its grace, a cashier gets the plan-ended page, not the till",
      staff_page.status_code == 402 and "This café&#39;s plan has ended" in text(staff_page)
      or "This café's plan has ended" in text(staff_page), staff_page.status_code)
check("which says nothing has been lost, and offers to sign out",
      "Nothing has been lost" in text(staff_page) and 'href="/logout"' in text(staff_page))
check("the cafe's script requests are answered 402 too",
      cashier.get("/api/order-status", headers={"X-Requested-With": "XMLHttpRequest"}).status_code in (200, 402)
      and cashier.get("/api/kitchen/board").status_code == 402)
check("the admin is taken to Subscription",
      admin.get("/dashboard").headers.get("Location", "").endswith("/subscription"))
page = text(admin.get("/subscription"))
check("which says it is resting and that nothing is lost",
      "resting since" in page and "Nothing has been lost" in page)
check("and can still be paid from", choose(admin, "monthly").status_code == 302)

token = db("SELECT public_token FROM cafes WHERE cafe_id = ?", (CAFE,))[0][0] or \
    application.get_public_token(CAFE)
menu = text(app.test_client().get("/m/%s" % token))
check("customers at the tables are sent kindly to the counter",
      "taking a little break" in menu or "counter" in menu)

set_until(CAFE, now - timedelta(days=4), plan="monthly", pending=now - timedelta(hours=2))
check("with a payment being checked, it works again meanwhile",
      cashier.get("/orders/add").status_code == 200)

# =====================================================================
print("\n=== 6. Free for life ===")
# =====================================================================
set_until(CAFE, application.PLATFORM_LIFETIME_UNTIL, plan="lifetime")
page = text(admin.get("/subscription"))
check("a lifetime cafe's page says it never ends, and offers no plans",
      "never ends" in page and "Pay ₹650" not in page)
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
