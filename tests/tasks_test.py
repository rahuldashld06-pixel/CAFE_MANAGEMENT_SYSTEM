"""
Tasks: which pages each teammate can open.

Every page in the sidebar is a task. On Add and Edit User an admin ticks
the ones a teammate does; their sidebar shows only those, signing in
opens the first of them, and every other page quietly sends them there -
no "you do not have permission" on the way in. A teammate nobody has
ticked anything for keeps the pages staff always had. Admins have every
task, and User Management and the settings are never a teammate's.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/tasks_test.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "tasks-secret"
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


def sidebar(page):
    nav = page[page.index("<!--nav:start-->"):page.index("<!--nav:end-->")]
    return re.findall(r'class="nav-link[^"]*">\s*<i class="bi [^"]+"></i>\s*<span>([^<]+)</span>', nav)


owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Task Cafe", "full_name": "Tia Owner", "username": "tia",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


def add(username, role, tasks=None):
    data = {"full_name": username.title(), "username": username, "role": role,
            "phone_number": "", "password": "password123",
            "_csrf_token": csrf(owner)}
    if tasks is not None:
        data["tasks_sent"] = "1"
        data["tasks"] = tasks
    owner.post("/users/add", data=data)
    return db("SELECT user_id, tasks FROM users WHERE username = ?", (username,))


def sign_in(username):
    client = app.test_client()
    response = client.post("/login", data={"cafe_name": "Task Cafe",
                                           "username": username,
                                           "password": "password123"})
    return client, response


# =====================================================================
print("\n=== 1. Ticking tasks on Add User ===")
# =====================================================================
form = text(owner.get("/users/add"))
boxes = re.findall(r'name="tasks" value="(\w+)"', form)
check("every page in the sidebar is a task to tick",
      boxes == ["dashboard", "new_order", "kitchen", "billing", "reviews",
                "categories", "foods", "inventory", "reports"], boxes)
ticked = re.findall(r'name="tasks" value="(\w+)"\s+checked', form)
check("a new teammate starts with the pages staff always had",
      ticked == ["new_order", "kitchen", "billing", "reviews", "categories",
                 "foods", "inventory"], ticked)
check("User Management and the settings are not offered",
      "users" not in boxes and not any("settings" in b for b in boxes))

cashier = add("cashy", "cashier", ["billing", "reviews"])
check("the ticks are kept", cashier and cashier[0][1] == "billing,reviews", cashier)
none = add("nobody", "cashier", [])
check("ticking nothing is refused - they would have no page to open",
      none == [] and any("at least one task" in m for m in flashes(owner)))
hacker = add("sneaky", "staff", ["billing", "users", "tax_settings"])
check("a task that is not one is dropped", hacker[0][1] == "billing", hacker)
legacy = add("oldform", "cashier")
check("a form without the task boxes gives the role's usual pages",
      legacy and legacy[0][1] is None, legacy)
boss = add("second", "admin", ["billing"])
check("there is no second admin to give tasks to - a cafe has one",
      boss == [] and any("one admin" in m for m in flashes(owner)), boss)


# =====================================================================
print("\n=== 2. Signing in opens the first of their pages ===")
# =====================================================================
client, response = sign_in("cashy")
check("a cashier with Billing and Reviews opens on Billing",
      response.status_code == 302 and response.headers["Location"].endswith("/billing"),
      response.headers.get("Location"))
page = client.get("/billing", follow_redirects=True)
check("with no permission message on the way in",
      "permission" not in text(page).lower())
check("their sidebar shows just those two",
      sidebar(text(page)) == ["Billing", "Reviews"], sidebar(text(page)))
check("and no New order button in the header",
      'class="topbar-create"' not in text(page))
check("they can switch to Reviews", client.get("/reviews").status_code == 200)
tour = re.findall(r'class="tour__title">([^<]+)<', text(page))
check("their first-time tour walks them only to their own pages",
      tour == ["Welcome", "Billing", "Your name, top right"], tour)

for path in ("/orders/add", "/kitchen", "/foods", "/inventory", "/categories",
             "/", "/dashboard", "/reports", "/users", "/settings/tax"):
    answer = client.get(path)
    check("%s sends them quietly to Billing" % path,
          answer.status_code == 302 and answer.headers["Location"].endswith("/billing"),
          (answer.status_code, answer.headers.get("Location")))
check("and says nothing about it", flashes(client) == [], flashes(client))
feed = client.get("/api/kitchen/board", headers={"Accept": "application/json"})
check("a page's data behind their back is refused too", feed.status_code == 403)
check("their own account is still theirs",
      client.get("/account/password").status_code == 200)
check("and the order-status bell works for everyone",
      client.get("/api/order-status").status_code == 200)
check("but it offers them no Done or print buttons they could not use",
      "canFinishOrders: false" in text(client.get("/billing")))

def hits(query):
    page = text(client.get("/search", query_string={"q": query}))
    return re.findall(r'search-hit__name">([^<]+)<', page)


check("search offers only pages they can open",
      hits("menu") == [] and hits("bills") == ["Billing"], (hits("menu"), hits("bills")))

client, response = sign_in("oldform")
check("someone with the usual pages opens on New Order, as before",
      response.headers["Location"].endswith("/orders/add"))
page = text(client.get("/orders/add"))
check("and sees the usual sidebar",
      sidebar(page) == ["New Order", "Kitchen", "Billing", "Reviews", "Categories",
                        "Food Management", "Inventory"], sidebar(page))
check("still with no dashboard", client.get("/").status_code == 302)


# =====================================================================
print("\n=== 3. Editing someone's tasks ===")
# =====================================================================
cashy_id = cashier[0][0]
edit = text(owner.get("/users/%d/edit" % cashy_id))
check("Edit User shows what they have",
      re.findall(r'name="tasks" value="(\w+)"\s+checked', edit) == ["billing", "reviews"])
owner.post("/users/%d/edit" % cashy_id, data={
    "full_name": "Cashy", "role": "cashier", "is_active": "1", "phone_number": "",
    "email": "", "password": "", "tasks_sent": "1",
    "tasks": ["dashboard", "reports", "billing"], "_csrf_token": csrf(owner)})
client, response = sign_in("cashy")
check("given the dashboard, it is where they open",
      response.headers["Location"].endswith("/"), response.headers.get("Location"))
page = text(client.get("/"))
check("and it opens for them", "Dashboard" in page and "Sales today" in page)
check("given Reports, they can open Reports",
      client.get("/reports").status_code == 200)
check("and find it in search", "Reports" in text(client.get("/search?q=report")))
check("Reviews, taken away, is gone",
      client.get("/reviews").status_code == 302 and "Reviews" not in sidebar(page))
owner.post("/users/%d/edit" % cashy_id, data={
    "full_name": "Cashy", "role": "cashier", "is_active": "1", "phone_number": "",
    "email": "", "password": "", "_csrf_token": csrf(owner)})
check("an edit that does not send the boxes leaves the tasks as they were",
      db("SELECT tasks FROM users WHERE user_id = ?", (cashy_id,))
      == [("dashboard,billing,reports",)])

team = text(owner.get("/users"))
check("the team list shows each teammate's tasks",
      re.search(r'data-username="cashy".*?task-chips.*?Dashboard.*?Billing.*?Reports',
                team, re.S) is not None)


# =====================================================================
print("\n=== 3b. Every button on a page works for whoever can open it ===")
# =====================================================================
cook = add("cook", "staff", ["kitchen"])
client, _ = sign_in("cook")
owner.post("/categories/add", data={"category_name": "Coffee", "description": "",
                                    "_csrf_token": csrf(owner)})
cat = re.search(r'<option value="(\d+)">', text(owner.get("/foods/add"))).group(1)
owner.post("/foods/add", data={"food_name": "Latte", "category_id": cat, "price": "100",
                               "quantity": "20", "minimum_stock": "1", "description": "",
                               "diet": "veg", "_csrf_token": csrf(owner)})
latte = db("SELECT food_id FROM foods")[0][0]
owner.post("/orders/add", data={"quantity_%d" % latte: "1", "_csrf_token": csrf(owner)})
order_id = db("SELECT order_id FROM orders ORDER BY order_id DESC")[0][0]
client.get("/kitchen")
cancelled = client.post("/orders/cancel/%d" % order_id,
                        data={"_csrf_token": csrf(client)},
                        headers={"X-Requested-With": "XMLHttpRequest"})
check("the Kitchen screen's own Cancel works for someone given Kitchen",
      db("SELECT order_status FROM orders WHERE order_id = ?", (order_id,))
      == [("Cancelled",)], cancelled.status_code)
check("and the popup offers them Done",
      "canFinishOrders: true" in text(client.get("/kitchen")))


# =====================================================================
print("\n=== 4. Admins ===")
# =====================================================================
page = text(owner.get("/"))
check("an admin sees every page",
      sidebar(page) == ["Dashboard", "New Order", "Kitchen", "Billing", "Reviews",
                        "Categories", "Food Management", "Inventory", "Reports",
                        "User Management"], sidebar(page))
client, response = sign_in("tia")
check("and opens on the dashboard",
      response.headers.get("Location", "").endswith("/"), response.headers.get("Location"))


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
