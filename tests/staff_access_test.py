"""
What a non-admin can and cannot reach.

Staff run the counter: orders, the menu, categories, stock and taking
payment. They do not get the dashboard, the reports, user management, or
the manager's summary figures on the Billing page.

The sidebar and the server guard have to agree. A link that is hidden but
still reachable is a hole; a page that is allowed but not linked is a
feature nobody finds.

Run with:  python tests/staff_access_test.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "staff-access-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import app as application     # noqa: E402

app = application.app
app.config["TESTING"] = True

PASSED, FAILED = [], []

REDIRECTS = (301, 302, 303, 307, 308)


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


admin = app.test_client()
admin.post("/register", data={
    "cafe_name": "Role Cafe", "full_name": "Boss", "username": "boss",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)

admin.post("/users/add", data={
    "full_name": "Till One", "username": "till", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(admin)}, follow_redirects=True)

staff = app.test_client()
staff.post("/login", data={"username": "till", "password": "password123"},
           follow_redirects=True)


def sidebar(client):
    html = client.get("/orders/add").get_data(as_text=True)
    # The whitespace between </i> and <span> matters: some links put the
    # label on the next line, and a tighter pattern silently skipped them -
    # which made "staff cannot see Reports" pass without proving anything.
    return [name.strip() for name in re.findall(
        r'nav-link[^>]*>\s*<i[^>]*></i>\s*<span>([^<]+)', html)]


print("\n=== 1. The counter's sidebar ===")
links = sidebar(staff)
check("no Dashboard link for staff", "Dashboard" not in links,
      "sidebar: %s" % links)
check("no Reports link", "Reports" not in links, "sidebar: %s" % links)
check("no User Management link", "User Management" not in links,
      "sidebar: %s" % links)
check("Categories is offered", "Categories" in links, "sidebar: %s" % links)
check("the counter's own sections are all there",
      all(item in links for item in
          ["Food Management", "Inventory", "New Order",
           "Order Management", "Billing"]),
      "sidebar: %s" % links)

admin_links = sidebar(admin)
check("an admin still sees everything",
      all(item in admin_links for item in
          ["Dashboard", "Categories", "Reports", "User Management"]),
      "admin sidebar: %s" % admin_links)


print("\n=== 2. Hidden means unreachable, not just unlinked ===")
for path in ["/dashboard", "/", "/reports", "/users", "/settings/tax",
             "/settings/theme"]:
    response = staff.get(path)
    check("staff are turned away from %s" % path,
          response.status_code in REDIRECTS,
          "status=%d - the page is hidden but still served"
          % response.status_code)


print("\n=== 3. Categories, with every part of it ===")
check("the list opens", staff.get("/categories").status_code == 200)
check("the add form opens", staff.get("/categories/add").status_code == 200)

staff.post("/categories/add",
           data={"category_name": "Pastries", "description": "Baked",
                 "_csrf_token": csrf(staff)}, follow_redirects=True)
listing = staff.get("/categories").get_data(as_text=True)
check("staff can create one", "Pastries" in listing,
      "the category was not created")

category_id = re.search(r'/categories/edit/(\d+)', listing).group(1)
check("the edit form opens",
      staff.get("/categories/edit/%s" % category_id).status_code == 200)

staff.post("/categories/edit/%s" % category_id,
           data={"category_name": "Pastries & Cakes", "description": "Baked",
                 "_csrf_token": csrf(staff)}, follow_redirects=True)
check("staff can rename one",
      "Pastries &amp; Cakes" in staff.get("/categories").get_data(as_text=True)
      or "Pastries & Cakes" in staff.get("/categories").get_data(as_text=True),
      "the rename did not take")

staff.post("/categories/delete/%s" % category_id,
           data={"_csrf_token": csrf(staff)}, follow_redirects=True)
check("staff can delete one",
      "Pastries" not in staff.get("/categories").get_data(as_text=True),
      "the category is still listed")

check("the admin sees the same category list",
      staff.get("/categories").status_code == 200
      and admin.get("/categories").status_code == 200)


print("\n=== 4. Billing: today's counts for staff, the period for admins ===")
staff_billing = staff.get("/billing").get_data(as_text=True)
admin_billing = admin.get("/billing").get_data(as_text=True)

check("staff can still use Billing",
      staff.get("/billing").status_code == 200)

# Staff get the same four counts, but fixed to today: a tally only helps at
# the till if it describes the shift they are on.
check("staff see the counts, labelled as today's",
      "Bills Today" in staff_billing and "Paid Today" in staff_billing
      and "Pending Today" in staff_billing
      and "Cancelled Today" in staff_billing,
      "the cashier's summary is missing or mislabelled")
check("an admin sees the same counts for the filtered period",
      "Bills in Period" in admin_billing and "Paid Bills" in admin_billing,
      "the admin summary changed")
check("staff are not shown the period labels",
      "Bills in Period" not in staff_billing,
      "a cashier is being told these are period figures when they are not")

# Revenue is the one figure that stays a manager's.
check("revenue is not on a cashier's screen",
      'id="stat-revenue"' not in staff_billing,
      "takings are visible to staff")
check("but it is on the admin's", 'id="stat-revenue"' in admin_billing)

check("the bill history itself is untouched",
      "Billing History" in staff_billing,
      "staff lost the part of Billing they actually need")

# The filter drives the history, not the cashier's tally.
filtered = staff.get("/billing?from_date=2020-01-01&to_date=2020-01-31"
                     ).get_data(as_text=True)
check("a date filter does not move the cashier's today figures",
      "Bills Today" in filtered,
      "filtering the history rewrote the shift summary")


print("\n=== 5. Taking payment still works for staff ===")
check("the order feed is open to them",
      staff.get("/api/order-status").status_code == 200,
      "the order-status popup would never load")
check("but the dashboard feed is not",
      staff.get("/api/dashboard-stats").status_code in REDIRECTS + (403,),
      "a cashier can read the revenue figures through the API")


print("\n=== 6. No page offers a non-admin the dashboard ===")
# Hiding it from the sidebar was not enough: several page headers carried
# their own "Dashboard" button, and every Back link fell back to it. For a
# cashier those all bounced off the permission guard.
for username, role in [("mgr", "manager"), ("cash2", "cashier"),
                       ("stf", "staff")]:
    admin.post("/users/add", data={
        "full_name": username.title(), "username": username, "role": role,
        "phone_number": "", "password": "password123",
        "_csrf_token": csrf(admin)}, follow_redirects=True)

PAGES = ["/orders/add", "/orders", "/foods", "/inventory", "/categories",
         "/billing", "/account/password", "/account/photo",
         "/settings/printing"]

for username, role in [("mgr", "manager"), ("cash2", "cashier"),
                       ("stf", "staff")]:
    client = app.test_client()
    client.post("/login", data={"username": username,
                                "password": "password123"},
                follow_redirects=True)

    leaks = []
    for path in PAGES:
        response = client.get(path)
        if response.status_code != 200:
            continue
        html = response.get_data(as_text=True)
        if re.search(r'href="(/dashboard|/)"', html):
            leaks.append(path)

    check("a %s is offered no dashboard link anywhere" % role,
          not leaks, "found one on: %s" % leaks)

# And the Back links land somewhere the role can actually open.
cashier = app.test_client()
cashier.post("/login", data={"username": "cash2", "password": "password123"},
             follow_redirects=True)
back = re.search(r'href="([^"]+)" class="btn btn--ghost" data-back',
                 cashier.get("/account/photo").get_data(as_text=True))
check("a cashier's Back button goes somewhere they can open",
      back and back.group(1) == "/orders/add",
      "it points at %s" % (back and back.group(1)))

admin_back = re.search(r'href="([^"]+)" class="btn btn--ghost" data-back',
                       admin.get("/account/photo").get_data(as_text=True))
check("an admin's still goes to the dashboard",
      admin_back and admin_back.group(1) == "/",
      "it points at %s" % (admin_back and admin_back.group(1)))

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
