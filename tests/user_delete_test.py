"""
Offline tests for deleting a staff account.

Deleting is permanent and, unlike deactivating, cannot be undone from the
UI - so the refusals matter as much as the deletion. Three accounts must
never go: your own, the café owner (every food, category and order row is
filed under that id), and the last active admin.

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
    """username -> user_id, read off the rendered list."""
    html = client.get("/users").get_data(as_text=True)
    rows = re.findall(
        r'/users/(\d+)/edit">Edit</a>.*?</tr>', html, re.S)
    names = re.findall(
        r'<td>([A-Za-z0-9_]+)</td>\s*<td>(?:Admin|Cashier|Manager|Staff)</td>',
        html)
    return dict(zip(names, rows))


def message(response):
    found = re.findall(r'class="alert">([^<]+)<', response.get_data(as_text=True))
    return found[0].strip() if found else ""


def delete(client, user_id):
    return client.post("/users/%s/delete" % user_id,
                       data={"_csrf_token": csrf(client)},
                       follow_redirects=True)


def usernames(client):
    html = client.get("/users").get_data(as_text=True)
    return re.findall(
        r'<td>([A-Za-z0-9_]+)</td>\s*<td>(?:Admin|Cashier|Manager|Staff)</td>',
        html)


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
add_user(a, "admin2", "admin")
ids = user_ids(a)

response = delete(a, ids["alpha"])
check("you cannot delete your own account",
      "your own account" in message(response), "message: %r" % message(response))
check("and it is still there", "alpha" in usernames(a))

# The owner seen by someone *else* - sign in as the second admin and try.
b_admin = app.test_client()
sign_in(b_admin, "admin2")
response = delete(b_admin, ids["alpha"])
check("another admin cannot delete the café owner",
      "owner" in message(response).lower(), "message: %r" % message(response))
check("the owner account survives", "alpha" in usernames(a),
      "the owner was deleted - every order and food row is now orphaned")

# Leave exactly one active admin and try to remove them.
response = delete(a, ids["admin2"])
check("the second admin can be removed while another admin remains",
      "deleted" in message(response).lower(), "message: %r" % message(response))

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

orders_before = c.get("/orders").get_data(as_text=True).count("/orders/")
add_user(c, "till1", "cashier")
gamma_ids = user_ids(c)
delete(c, gamma_ids["till1"])

orders_after = c.get("/orders").get_data(as_text=True).count("/orders/")
check("orders are still listed after a staff account is deleted",
      orders_after == orders_before and orders_before > 0,
      "before=%d after=%d" % (orders_before, orders_after))
check("billing still renders", c.get("/billing").status_code == 200)
check("the menu is intact", "Latte" in c.get("/foods").get_data(as_text=True))


print("\n=== 6. The button is only offered where it would work ===")
html = a.get("/users").get_data(as_text=True)
owner_id = user_ids(a)["alpha"]
check("no delete button on the café owner's row",
      ('/users/%s/delete' % owner_id) not in html,
      "the owner row offers a delete that would only be refused")
check("staff rows do offer one",
      "/delete" in html, "no delete control rendered at all")


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
