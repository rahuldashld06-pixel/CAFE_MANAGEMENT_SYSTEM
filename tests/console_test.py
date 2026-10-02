"""
The developer's console: the site watched, every fault an alert, every
cafe and its plan under the developer's hand.

What has to hold:

  * the console is behind its own password, guessed slowly, timed out,
    and invisible to a cafe's own sign-in; without a password set, it
    does not exist;
  * every request is counted by the minute - how many, how many failed,
    how long they took - and drawn;
  * a crash, a database that refused, a slow page, a problem somebody
    reported and an error in a browser each become an alert, with the
    detail only the developer sees; the same fault again counts on its
    alert instead of adding one;
  * payments are approved or turned down here; any cafe can be given
    Refero free for life, or more days;
  * with the database down, the console still opens and says so.

Run with:  python tests/console_test.py
"""
import os
import re
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "console-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"
# Plans are what this suite is about: its cafes start on the trial.
os.environ["NEW_CAFE_PLAN"] = "trial"
os.environ["PLATFORM_UPI_ID"] = "refero.dev@okaxis"
os.environ["PLATFORM_UPI_NAME"] = "Rahul Dash"
os.environ["PLATFORM_PASSWORD"] = "the-developers-own-password"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import logging                # noqa: E402
logging.disable(logging.CRITICAL)

import app as application     # noqa: E402

app = application.app
app.config["MONITOR_SYNC"] = True

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % (detail,)))


SECRET = "printer queue table kitchen_tickets is locked"


@app.route("/__console_test/crash")
def __console_test_crash():
    raise RuntimeError(SECRET)


def db(sql, params=()):
    rows = mysql_shim._DB.execute(sql, params).fetchall()
    mysql_shim._DB.commit()
    return [tuple(row) for row in rows]


def text(response):
    return response.get_data(as_text=True)


def token_of(html):
    found = re.search(r'name="_csrf_token" value="([^"]+)"', html)
    return found.group(1) if found else ""


def session_token(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def register(cafe, username):
    client = app.test_client()
    client.post("/register", data={
        "cafe_name": cafe, "full_name": cafe + " Admin", "username": username,
        "phone_number": "", "password": "Brew-Latte-42",
        "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
    return client, db("SELECT cafe_id FROM cafes WHERE cafe_name = ?", (cafe,))[0][0]


def console_sign_in(password="the-developers-own-password", address="10.9.0.1"):
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = address
    page = text(client.get("/platform/login"))
    response = client.post("/platform/login", data={"password": password,
                                                     "_csrf_token": token_of(page)})
    return client, response


owner, CAFE = register("Watched Cafe", "watched")
other, OTHER = register("Second Cafe", "second")

# =====================================================================
print("\n=== 1. Behind its own password ===")
# =====================================================================
check("a stranger is sent to the console's sign-in",
      app.test_client().get("/platform").headers.get("Location", "").endswith("/platform/login"))
check("a cafe's admin, signed in, is no closer",
      owner.get("/platform").headers.get("Location", "").endswith("/platform/login"))

guesser, response = console_sign_in("wrong", address="10.9.0.66")
check("a wrong password is refused, plainly", "not the console" in text(response))
for _ in range(5):
    console_sign_in("wrong-again", address="10.9.0.66")
_, response = console_sign_in(address="10.9.0.66")
check("after five wrong guesses, even the right one waits",
      "Too many wrong tries" in text(response), text(response)[:200])

dev, response = console_sign_in()
check("the right password opens it", response.headers.get("Location", "").endswith("/platform"))
home = dev.get("/platform")
check("the console is kept out of search and out of caches",
      'content="noindex, nofollow"' in text(home)
      and "no-store" in home.headers.get("Cache-Control", ""))

no_token = dev.post("/platform/cafes/%d" % CAFE, data={"action": "lifetime"})
check("its changes need the page's own token", no_token.status_code == 400
      and db("SELECT plan FROM cafes WHERE cafe_id = ?", (CAFE,))[0][0] == "trial")

with dev.session_transaction() as sess:
    sess["platform_at"] = sess["platform_at"] - 3 * 3600
check("after two hours it asks again",
      dev.get("/platform").headers.get("Location", "").endswith("/platform/login"))
dev, _ = console_sign_in()

saved = application.PLATFORM_PASSWORD
application.PLATFORM_PASSWORD = ""
try:
    check("with no password set, there is no console at all",
          app.test_client().get("/platform/login").status_code == 404
          and dev.get("/platform").status_code == 404)
finally:
    application.PLATFORM_PASSWORD = saved

# =====================================================================
print("\n=== 2. Every request counted ===")
# =====================================================================
db("DELETE FROM monitor_minutes")
for _ in range(12):
    owner.get("/dashboard")
owner.get("/__console_test/crash")
application.monitor_flush(include_current=True)
requests, errors = db("SELECT SUM(requests), SUM(errors) FROM monitor_minutes")[0]
check("requests are written, minute by minute", requests and requests >= 13, requests)
check("and the one that failed is counted as failed", errors == 1, errors)
check("each row says which server process counted it",
      db("SELECT COUNT(DISTINCT worker) FROM monitor_minutes")[0][0] == 1)

page = text(dev.get("/platform"))
check("the console draws requests, response times and errors",
      page.count('class="con-chart__line"') == 3)
check("and gives the last hour's numbers",
      re.search(r"Requests, last hour</span>\s*<strong>(\d+)</strong>", page) is not None)
for key in ("1h", "7d"):
    check("over %s too" % key, 'class="con-chart__line"' in text(dev.get("/platform?range=%s" % key)))

# =====================================================================
print("\n=== 3. Faults become alerts ===")
# =====================================================================
application.monitor_flush(include_current=True)
row = db("SELECT kind, level, title, detail, hits, cafe_id FROM monitor_events WHERE kind = 'crash'")
check("a crash is an alert, at error", row and row[0][1] == "error", row)
check("titled with what happened", row and SECRET in row[0][2], row)
check("with its traceback, for the developer", row and "Traceback" in row[0][3])
check("and which cafe it happened to", row and row[0][5] == CAFE, row)
check("the cafe's own page shows none of it",
      SECRET not in text(owner.get("/__console_test/crash")))
application.monitor_flush(include_current=True)
check("the same crash again counts on its alert instead of adding one",
      db("SELECT COUNT(*), MAX(hits) FROM monitor_events WHERE kind = 'crash'")[0] == (1, 2))

page = text(dev.get("/platform"))
check("the console lists it, with its details", SECRET in page and "Traceback" in page)
check("and says the site has faults to look at", "Degraded" in page)
check("its tab title carries the count", re.search(r"<title>\(\d+\) Console", page) is not None)

real_connect = application.get_db_connection
calls = {"n": 0}


def fails_once(*args, **kwargs):
    calls["n"] += 1
    if calls["n"] == 1:
        raise application.mysql.connector.Error("Lost connection to MySQL server during query")
    return real_connect(*args, **kwargs)


application.get_db_connection = fails_once
try:
    owner.get("/billing")
finally:
    application.get_db_connection = real_connect
application.monitor_flush(include_current=True)
check("a database that refused is an alert of its own",
      db("SELECT COUNT(*) FROM monitor_events WHERE kind = 'database'")[0][0] >= 1,
      db("SELECT kind, title FROM monitor_events"))

application.MONITOR_SLOW_MS, saved_slow = 0, application.MONITOR_SLOW_MS
try:
    owner.get("/foods")
finally:
    application.MONITOR_SLOW_MS = saved_slow
application.monitor_flush(include_current=True)
check("a slow page is noted, with how long it took",
      db("SELECT COUNT(*) FROM monitor_events WHERE kind = 'slow' AND detail LIKE 'Took %'")[0][0] >= 1)

# Reported by hand, by anybody on the team.
with owner.session_transaction() as sess:
    sess_token = sess.get("_csrf_token", "")
owner.post("/users/add", data={"full_name": "Kim Kitchen", "username": "kimk", "role": "staff",
                               "phone_number": "", "password": "Brew-Latte-42",
                               "_csrf_token": sess_token})
staff = app.test_client()
staff.post("/login", data={"cafe_name": "Watched Cafe", "username": "kimk", "password": "Brew-Latte-42"})
check("anybody on the team can open Report a problem",
      staff.get("/report-problem").status_code == 200)
response = staff.post("/report-problem", data={"what": "The kitchen screen froze at lunch.",
                                               "where": "/kitchen",
                                               "_csrf_token": session_token(staff)})
check("sending it thanks them", response.status_code == 302)
row = db("SELECT title, detail, path FROM monitor_events WHERE kind = 'report'")
check("and it reaches the console with what they wrote, who and where",
      row and "Kim Kitchen" in row[0][0] and "froze at lunch" in row[0][1] and row[0][2] == "/kitchen",
      row)
check("a report with nothing in it is not sent",
      staff.post("/report-problem", data={"what": "", "_csrf_token": session_token(staff)})
      .status_code == 302
      and db("SELECT COUNT(*) FROM monitor_events WHERE kind = 'report'")[0][0] == 1)

# Sent by the page itself.
headers = {"X-CSRFToken": session_token(staff), "X-Requested-With": "XMLHttpRequest"}
for n in range(8):
    staff.post("/api/client-error", json={"message": "TypeError: x is undefined (%d)" % n,
                                          "source": "http://localhost/static/js/app.js",
                                          "line": 10, "page": "/kitchen"}, headers=headers)
application.monitor_flush(include_current=True)
check("an error in a browser is an alert too",
      db("SELECT COUNT(*) FROM monitor_events WHERE kind = 'browser'")[0][0] >= 1)
check("but one browser can send only a few",
      db("SELECT COUNT(*) FROM monitor_events WHERE kind = 'browser'")[0][0] <= 5)
check("and only with the page's token",
      app.test_client().post("/api/client-error", json={"message": "x"}).status_code in (302, 400))

page = text(dev.get("/platform"))
crash_id = db("SELECT event_id FROM monitor_events WHERE kind = 'crash'")[0][0]
dev.post("/platform/events/%d/resolve" % crash_id, data={"_csrf_token": token_of(page)})
check("an alert dealt with leaves the list",
      db("SELECT resolved_at FROM monitor_events WHERE event_id = ?", (crash_id,))[0][0] is not None
      and SECRET not in text(dev.get("/platform")))
owner.get("/__console_test/crash")
application.monitor_flush(include_current=True)
check("and if it happens again, it is a new alert",
      db("SELECT COUNT(*) FROM monitor_events WHERE kind = 'crash' AND resolved_at IS NULL")[0][0] == 1)
page = text(dev.get("/platform"))
dev.post("/platform/events/all/resolve", data={"_csrf_token": token_of(page)})
check("Mark all dealt with clears them all",
      db("SELECT COUNT(*) FROM monitor_events WHERE resolved_at IS NULL")[0][0] == 0)

# =====================================================================
print("\n=== 4. Payments and plans ===")
# =====================================================================
page = text(owner.get("/subscription"))
location = owner.post("/subscription/choose", data={"plan": "monthly",
                                                    "_csrf_token": token_of(page)}).headers["Location"]
page = text(owner.get(location))
owner.post(location, data={"utr": "427855556666", "_csrf_token": token_of(page)})

page = text(dev.get("/platform"))
check("a payment to check is listed with its UPI reference and the cafe",
      "427855556666" in page and "Watched Cafe" in page and "Approve</button>" in page)
payment_id = re.search(r"/platform/payments/(\d+)/approve", page).group(1)
dev.post("/platform/payments/%s/approve" % payment_id, data={"_csrf_token": token_of(page)})
check("approving it puts the cafe on its plan",
      db("SELECT plan FROM cafes WHERE cafe_id = ?", (CAFE,))[0][0] == "monthly")
check("and the cafe's page shows it paid",
      "Download PDF" in text(owner.get("/subscription")))

page = text(dev.get("/platform"))
dev.post("/platform/cafes/%d" % OTHER, data={"action": "lifetime", "_csrf_token": token_of(page)})
plan, until = db("SELECT plan, plan_until FROM cafes WHERE cafe_id = ?", (OTHER,))[0]
check("any cafe can be given Refero free for life", plan == "lifetime", plan)
check("which its admin sees", "never ends" in text(other.get("/subscription")))
dev.post("/platform/cafes/%d" % OTHER, data={"action": "extend", "days": "30",
                                              "_csrf_token": token_of(page)})
check("a lifetime plan is not shortened by days added",
      db("SELECT plan FROM cafes WHERE cafe_id = ?", (OTHER,))[0][0] == "lifetime")
dev.post("/platform/cafes/%d" % OTHER, data={"action": "end_lifetime",
                                              "_csrf_token": token_of(page)})
plan, until = db("SELECT plan, plan_until FROM cafes WHERE cafe_id = ?", (OTHER,))[0]
check("and ended, it gives a week to choose a plan",
      plan == "trial" and application._moment(until) > application.utc_now() + timedelta(days=6))

before = application._moment(db("SELECT plan_until FROM cafes WHERE cafe_id = ?", (CAFE,))[0][0])
dev.post("/platform/cafes/%d" % CAFE, data={"action": "extend", "days": "45",
                                             "_csrf_token": token_of(page)})
after = application._moment(db("SELECT plan_until FROM cafes WHERE cafe_id = ?", (CAFE,))[0][0])
check("days added go on the end of the plan", after - before == timedelta(days=45), (before, after))

page = text(dev.get("/platform"))
check("every cafe is listed with its admin and plan",
      "Watched Cafe" in page and "Second Cafe" in page and "watched" in page
      and page.count('data-find="') == 2)

# =====================================================================
print("\n=== 5. With the database down ===")
# =====================================================================
application.get_db_connection = lambda *a, **k: (_ for _ in ()).throw(
    application.mysql.connector.Error("Can't connect to MySQL server on db.internal"))
try:
    application.record_event("crash", "error", "Held in memory while the database is down")
    down = dev.get("/platform")
finally:
    application.get_db_connection = real_connect
check("the console still opens", down.status_code == 200)
check("and says the database is not answering",
      "Database down" in text(down) and "No answer" in text(down))
check("showing the faults this server is holding",
      "Held in memory while the database is down" in text(down))
application.monitor_flush(include_current=True)
check("which are written once the database is back",
      db("SELECT COUNT(*) FROM monitor_events WHERE title = 'Held in memory while the database is down'")[0][0] == 1)

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
