"""
Offline tests for deleting a staff account.

Deleting is permanent and, unlike deactivating, cannot be undone from the
UI - so the refusals matter as much as the deletion. A cafe has one admin,
the account that created it: every food, category and order row is filed
under that id, so it must never go - and nobody can be made a second
admin to try.

Run with:  python tests/user_delete_test.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "user-delete-test-secret"
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


def sign_up(client, cafe, username):
    return client.post("/register", data={
        "cafe_name": cafe, "full_name": username.title() + " Owner",
        "username": username, "phone_number": "",
        "password": "password123", "confirm_password": "password123",
    }, follow_redirects=True)


def add_user(client, username, role):
    return client.post("/users/add", data={
        "full_name": username.title(), "username": username, "role": role,
        "phone_number": "", "password": "password123",
        "_csrf_token": csrf(client),
    }, follow_redirects=True)


def sign_in(client, username):
    return client.post("/login", data={
        "username": username, "password": "password123",
    }, follow_redirects=True)


def user_ids(client):
    """username -> user_id, read off the rendered list.

    Each row names its person and id on itself, so one row can never be
    paired with another's id however the table is laid out."""
    html = client.get("/users").get_data(as_text=True)
    return dict((name, uid) for uid, name in re.findall(
        r'<tr data-user-id="(\d+)" data-username="([^"]+)"', html))


def message(response):
    found = re.findall(r'class="alert">([^<]+)<', response.get_data(as_text=True))
    return found[0].strip() if found else ""


def delete(client, user_id):
    return client.post("/users/%s/delete" % user_id,
                       data={"_csrf_token": csrf(client)},
                       follow_redirects=True)


def usernames(client):
    html = client.get("/users").get_data(as_text=True)
    return re.findall(r'<tr data-user-id="\d+" data-username="([^"]+)"', html)


print("\n=== 1. An admin can delete a staff account ===")
a = app.test_client()
sign_up(a, "Cafe Alpha", "alpha")
add_user(a, "cashier1", "cashier")
ids = user_ids(a)

response = delete(a, ids["cashier1"])
check("deleting a cashier reports success",
      "deleted" in message(response).lower(), "message: %r" % message(response))
check("the account is gone from the list",
      "cashier1" not in usernames(a), "still listed: %s" % usernames(a))


print("\n=== 2. The refusals ===")
add_user(a, "cashier2", "cashier")
response = add_user(a, "admin2", "admin")
ids = user_ids(a)

check("nobody can be made a second admin",
      "one admin" in message(response) and "admin2" not in usernames(a),
      "message: %r, people: %s" % (message(response), usernames(a)))

response = delete(a, ids["alpha"])
check("the admin cannot delete their own account",
      "your own account" in message(response), "message: %r" % message(response))
check("and it is still there - every order and food row is filed under it",
      "alpha" in usernames(a))

# A manager - the most anybody else can be - cannot reach the route.
add_user(a, "manager1", "manager")
mgr = app.test_client()
sign_in(mgr, "manager1")
response = mgr.post("/users/%s/delete" % ids["alpha"],
                    data={"_csrf_token": csrf(mgr)})
check("a manager posting to delete the admin is turned away",
      response.status_code in (301, 302, 303) and "alpha" in usernames(a),
      "status=%d" % response.status_code)

ids = user_ids(a)
response = delete(a, ids.get("cashier2", "0"))
check("a remaining cashier can still be deleted",
      "deleted" in message(response).lower(), "message: %r" % message(response))


print("\n=== 3. Staff cannot reach the route at all ===")
add_user(a, "cashier3", "cashier")
ids = user_ids(a)
target = ids["cashier3"]

staff = app.test_client()
sign_in(staff, "cashier3")
response = staff.post("/users/%s/delete" % target,
                      data={"_csrf_token": csrf(staff)})
check("a cashier posting to the delete route is turned away",
      response.status_code in (301, 302, 303),
      "status=%d" % response.status_code)
check("and the account is untouched", "cashier3" in usernames(a))


print("\n=== 4. One café cannot delete another's staff ===")
b = app.test_client()
sign_up(b, "Cafe Beta", "beta")
add_user(b, "betastaff", "cashier")

alpha_ids = user_ids(a)
response = delete(b, alpha_ids["cashier3"])
check("café B's admin is told the user does not exist",
      "not found" in message(response).lower(), "message: %r" % message(response))
check("café A's staff account is untouched",
      "cashier3" in usernames(a), "cross-tenant delete succeeded")
check("café B still sees only its own people",
      sorted(usernames(b)) == ["beta", "betastaff"], "got %s" % usernames(b))


print("\n=== 5. Order history survives the person who took it ===")
c = app.test_client()
sign_up(c, "Cafe Gamma", "gamma")
c.post("/categories/add", data={"category_name": "Coffee", "description": "",
                                "_csrf_token": csrf(c)}, follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     c.get("/foods/add").get_data(as_text=True)).group(1)
c.post("/foods/add", data={"food_name": "Latte", "category_id": category,
                           "price": "50", "quantity": "10", "description": "",
                           "_csrf_token": csrf(c)}, follow_redirects=True)
food = re.findall(r'id="quantity_(\d+)"',
                  c.get("/orders/add").get_data(as_text=True))[0]
c.post("/orders/add", data={"quantity_%s" % food: "2",
                            "_csrf_token": csrf(c)}, follow_redirects=True)

orders_before = len(c.get("/api/kitchen/board").get_json()["orders"])
add_user(c, "till1", "cashier")
gamma_ids = user_ids(c)
delete(c, gamma_ids["till1"])

orders_after = len(c.get("/api/kitchen/board").get_json()["orders"])
check("orders are still listed after a staff account is deleted",
      orders_after == orders_before and orders_before > 0,
      "before=%d after=%d" % (orders_before, orders_after))
check("billing still renders", c.get("/billing").status_code == 200)
check("the menu is intact", "Latte" in c.get("/foods").get_data(as_text=True))


print("\n=== 6. The button is only offered where it would work ===")
html = a.get("/users").get_data(as_text=True)
owner_id = user_ids(a)["alpha"]
check("no delete button on the café admin's row",
      ('/users/%s/delete' % owner_id) not in html,
      "the admin row offers a delete that would only be refused")
check("staff rows do offer one",
      "/delete" in html, "no delete control rendered at all")


print("\n=== 7. Cafes from before: one admin, settled once ===")
from tests import mysql_shim as _shim  # noqa: E402
d = app.test_client()
sign_up(d, "Cafe Delta", "delta")
cafe_d = _shim._DB.execute("SELECT cafe_id, owner_user_id FROM cafes WHERE cafe_name = 'Cafe Delta'").fetchone()
# The old app let a cafe make anybody an admin, and even demote the account
# that created it.
_shim._DB.execute("INSERT INTO users (username, password_hash, full_name, role, is_active, cafe_id) "
                  "VALUES ('oldadmin', 'x', 'Old Admin', 'admin', 1, ?)", (cafe_d[0],))
_shim._DB.execute("UPDATE users SET role = 'manager', is_active = 0 WHERE user_id = ?", (cafe_d[1],))
_shim._DB.commit()
_conn = application.get_db_connection()
_cur = _conn.cursor(dictionary=True)
application._settle_one_admin(_cur)
_conn.commit()
_cur.close()
_conn.close()
roles = dict(_shim._DB.execute("SELECT username, role || ':' || is_active FROM users "
                               "WHERE cafe_id = ?", (cafe_d[0],)).fetchall())
check("the account that created the cafe is its admin again, switched on",
      roles.get("delta") == "admin:1", roles)
check("and any other admin becomes a manager", roles.get("oldadmin") == "manager:1", roles)


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
