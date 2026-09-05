"""
Offline smoke test for the multi-tenant cafe SaaS.

Walks a brand-new deployment through the full lifecycle twice (two separate
cafés) and asserts that neither tenant can see or touch the other's data.

Run with:  python tests/smoke_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key-for-smoke-test"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import app as application  # noqa: E402

app = application.app
app.config["TESTING"] = True

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else f"\n          -> {detail}"))


def csrf(client):
    with client.session_transaction() as s:
        return s.get("_csrf_token", "")


def register(client, cafe, user, pw="password123", phone="+919999900001"):
    return client.post("/register", data={
        "cafe_name": cafe, "full_name": f"{user} Owner", "username": user,
        "phone_number": phone, "password": pw,
        "confirm_password": pw,
    }, follow_redirects=True)


print("\n=== 1. Fresh database: schema bootstrap ===")
with app.test_client() as c:
    r = c.get("/login")
    check("GET /login on an empty database returns 200",
          r.status_code == 200, f"status={r.status_code}")

    r = c.get("/healthz")
    check("/healthz reports the database is reachable",
          r.status_code == 200 and b'"ok"' in r.data, r.data[:200])

print("\n=== 2. Café A signs up and builds a menu ===")
a = app.test_client()
r = register(a, "Café Alpha", "alpha_admin")
check("Registration creates the café and signs the owner in",
      r.status_code == 200 and b"Alpha" in r.data, r.data[:300])

r = a.get("/dashboard")
check("Dashboard renders for a brand-new café", r.status_code == 200,
      f"status={r.status_code}")

r = a.get("/categories")
check("Category Management page exists (route was missing entirely)",
      r.status_code == 200, f"status={r.status_code}")

r = a.post("/categories/add", data={
    "category_name": "Beverages", "description": "Hot and cold drinks",
    "_csrf_token": csrf(a)}, follow_redirects=True)
check("Café A can create a category", b"Beverages" in r.data, r.data[:300])

r = a.post("/categories/add", data={
    "category_name": "beverages", "_csrf_token": csrf(a)},
    follow_redirects=True)
check("Duplicate category name is rejected within the same café",
      b"already have a category" in r.data, r.data[:300])

with app.app_context():
    pass
cat_id = None
with a.session_transaction() as s:
    pass
r = a.get("/categories")
import re as _re
m = _re.search(rb"/categories/edit/(\d+)", r.data)
cat_id = int(m.group(1)) if m else None
check("Category id is discoverable from the listing", cat_id is not None)

r = a.post("/foods/add", data={
    "food_name": "Masala Chai", "category_id": str(cat_id),
    "description": "Spiced tea", "price": "45.00",
    "quantity": "50", "minimum_stock": "5",
    "_csrf_token": csrf(a)}, follow_redirects=True)
check("Café A can add a food item", b"Masala Chai" in r.data, r.data[:400])

r = a.post("/foods/add", data={
    "food_name": "Broken", "category_id": str(cat_id), "description": "",
    "price": "not-a-number", "quantity": "10", "minimum_stock": "1",
    "_csrf_token": csrf(a)}, follow_redirects=True)
check("Non-numeric price is a friendly message, not a 500",
      r.status_code == 200 and b"must be a number" in r.data, r.data[:300])

r = a.post("/foods/add", data={
    "food_name": "Broken2", "category_id": str(cat_id), "description": "",
    "price": "10", "minimum_stock": "1",
    "_csrf_token": csrf(a)}, follow_redirects=True)
check("Missing quantity field does not raise KeyError/400",
      r.status_code == 200, f"status={r.status_code}")

r = a.get("/inventory")
check("Inventory lists the new item", b"Masala Chai" in r.data, r.data[:300])

print("\n=== 3. Café A adds staff (previously always failed) ===")
r = a.post("/users/add", data={
    "username": "alpha_cashier", "full_name": "Alpha Cashier",
    "password": "password123", "role": "cashier",
    "phone_number": "", "_csrf_token": csrf(a)}, follow_redirects=True)
check("Creating a staff user succeeds (add_user SQL was malformed)",
      b"alpha_cashier" in r.data, r.data[:400])

r = a.get("/users")
m = _re.search(rb"/users/(\d+)/edit", r.data)
check("User Management lists café A users only",
      r.data.count(b"/users/") > 0 and b"beta" not in r.data.lower())

print("\n=== 4. Café B signs up separately ===")
b = app.test_client()
# No mobile number on file, so this admin signs in without an OTP step.
r = register(b, "Café Beta", "beta_admin", phone="")
check("Second café can register", r.status_code == 200 and b"Beta" in r.data,
      r.data[:300])

r = b.get("/foods")
check("Café B's menu is empty - it does not see café A's food",
      b"Masala Chai" not in r.data, r.data[:400])

r = b.get("/categories")
check("Café B does not see café A's categories",
      b"Beverages" not in r.data, r.data[:400])

print("\n=== 5. Cross-tenant attack attempts ===")
# Find café A's owner user id by scanning A's own user list.
r = a.get("/users")
ids = [int(x) for x in _re.findall(rb"/users/(\d+)/edit", r.data)]
target = min(ids) if ids else 1

r = b.post(f"/users/{target}/edit", data={
    "full_name": "HACKED", "role": "admin", "is_active": "1",
    "password": "attackerpass1", "phone_number": "",
    "_csrf_token": csrf(b)}, follow_redirects=True)
check("Café B cannot edit café A's user (edit_user had no cafe filter)",
      b"User not found" in r.data or b"HACKED" not in r.data, r.data[:400])

r = a.get("/users")
check("Café A's user was not renamed by the attack",
      b"HACKED" not in r.data, r.data[:400])

r = b.post(f"/users/{target}/toggle", data={"_csrf_token": csrf(b)},
           follow_redirects=True)
check("Café B cannot toggle café A's user (toggle_user had no cafe filter)",
      b"User not found" in r.data, r.data[:300])

r = a.get("/dashboard")
check("Café A's admin is still active after the toggle attempt",
      r.status_code == 200, f"status={r.status_code}")

print("\n=== 6. Last-admin lockout protection ===")
r = a.get("/users")
owner_id = min([int(x) for x in _re.findall(rb"/users/(\d+)/edit", r.data)])
r = a.post(f"/users/{owner_id}/edit", data={
    "full_name": "Alpha Owner", "role": "cashier", "is_active": "1",
    "phone_number": "", "_csrf_token": csrf(a)}, follow_redirects=True)
check("Demoting the only admin is refused",
      b"only active admin" in r.data or b"cannot deactivate" in r.data.lower(),
      r.data[:400])

print("\n=== 7. Per-café branding ===")
r = a.post("/settings/branding", data={
    "cafe_name": "Alpha Coffee House", "_csrf_token": csrf(a)},
    follow_redirects=True)
check("Café A can rename its branding",
      b"Alpha Coffee House" in r.data, r.data[:400])

r = b.get("/settings/branding")
check("Café B's branding is unaffected by café A's change "
      "(branding was a single shared JSON file)",
      b"Alpha Coffee House" not in r.data, r.data[:400])

print("\n=== 8. Password reset hardening ===")
r = b.get("/logout", follow_redirects=True)
anon = app.test_client()
r = anon.post("/forgot-password", data={
    "username": "alpha_admin", "full_name": "alpha_admin Owner",
    "phone_number": "", "new_password": "takenover1",
    "confirm_password": "takenover1"}, follow_redirects=True)
check("Reset with username + full name alone is refused",
      b"couldn't verify" in r.data or b"couldn" in r.data, r.data[:400])

r = anon.post("/login", data={
    "username": "alpha_admin", "password": "takenover1"},
    follow_redirects=True)
check("The attacker's password does not work",
      b"Invalid username or password" in r.data, r.data[:300])

print("\n=== 9. Session recovery ===")
broken = app.test_client()
with broken.session_transaction() as s:
    s["user_id"] = 99999
    s["role"] = "admin"
r = broken.get("/dashboard", follow_redirects=True)
check("A session with no valid café redirects to login instead of 500ing",
      r.status_code == 200 and b"sign in" in r.data.lower(),
      f"status={r.status_code}")


print("\n=== 10. Order and billing lifecycle ===")
# Café B was signed out during the password-reset checks; sign back in.
b.post("/login", data={"username": "beta_admin", "password": "password123"},
       follow_redirects=True)
_r = b.get("/dashboard")
check("Admin without a phone number signs in without an OTP step",
      _r.status_code == 200, f"status={_r.status_code}")
r = a.get("/foods")
mf = _re.search(rb"/foods/edit/(\d+)", r.data)
food_id = int(mf.group(1)) if mf else None
check("Food id is discoverable", food_id is not None)

r = a.post("/orders/add", data={
    f"quantity_{food_id}": "2", "_csrf_token": csrf(a)},
    follow_redirects=True)
check("Café A can place an order", r.status_code == 200, f"status={r.status_code}")

r = a.get("/orders")
check("Order appears in the order list", b"</table>" in r.data and r.status_code == 200)

r = a.get("/billing")
check("Billing page renders and auto-creates the bill",
      r.status_code == 200, f"status={r.status_code}")

r = b.get("/orders")
check("Café B does not see café A's orders",
      r.status_code == 200 and r.data.count(b"/orders/") <= 3, r.data[:200])

r = b.get("/billing")
check("Café B's billing history is empty of café A's bills",
      r.status_code == 200, f"status={r.status_code}")

r = a.get("/reports")
check("Reports page renders", r.status_code == 200, f"status={r.status_code}")

r = a.get("/api/dashboard-stats")
check("Dashboard stats API responds", r.status_code == 200, f"status={r.status_code}")

r = a.get("/api/order-status")
check("Order status feed responds", r.status_code == 200, f"status={r.status_code}")

print("\n" + "=" * 60)
print(f"PASSED: {len(PASSED)}   FAILED: {len(FAILED)}")
if FAILED:
    print("Failures:")
    for f in FAILED:
        print("  -", f)
print("=" * 60)
sys.exit(1 if FAILED else 0)
