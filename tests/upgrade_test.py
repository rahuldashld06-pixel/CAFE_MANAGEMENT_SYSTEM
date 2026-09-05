"""
Upgrade test: a database carried over from the older single-cafe app.

Simulates the two unique constraints being absent AND duplicate rows
already present, then asserts that ensure_auth_schema() collapses the
duplicates and installs the constraints without raising.

Run with:  python tests/upgrade_test.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "upgrade-test-key"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402

# Pretend this legacy database never had these unique keys.
mysql_shim.MISSING_INDEXES = {"uq_inventory_food", "uq_bills_order"}
mysql_shim.install()

import app as application  # noqa: E402

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else f"\n          -> {detail}"))


def query(sql, params=()):
    conn = mysql_shim.connect()
    cur = conn.cursor(dictionary=True)
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    return rows


print("\n=== Building a legacy database with duplicate rows ===")

raw = mysql_shim._DB

# Legacy tables WITHOUT the unique keys, so duplicates can be inserted.
raw.executescript("""
CREATE TABLE users (
    user_id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE,
    password_hash TEXT, full_name TEXT, role TEXT DEFAULT 'admin',
    is_active INTEGER DEFAULT 1, phone_number TEXT, cafe_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE categories (
    category_id INTEGER PRIMARY KEY AUTOINCREMENT, category_name TEXT,
    description TEXT, user_id INTEGER, cafe_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE foods (
    food_id INTEGER PRIMARY KEY AUTOINCREMENT, category_id INTEGER,
    food_name TEXT, description TEXT, price NUMERIC, availability INTEGER,
    image_mime TEXT, image_blob BLOB, image_version INTEGER DEFAULT 1,
    user_id INTEGER, cafe_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE orders (
    order_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, total_amount NUMERIC,
    order_status TEXT DEFAULT 'Pending', user_id INTEGER, cafe_id INTEGER);
CREATE TABLE order_items (
    order_item_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
    food_id INTEGER, quantity INTEGER, price NUMERIC, subtotal NUMERIC);

-- No UNIQUE on food_id: this is the legacy shape.
CREATE TABLE inventory (
    inventory_id INTEGER PRIMARY KEY AUTOINCREMENT, food_id INTEGER,
    quantity INTEGER, minimum_stock INTEGER,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP);

-- No UNIQUE on order_id: this is the legacy shape.
CREATE TABLE bills (
    bill_id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
    subtotal NUMERIC, tax NUMERIC, discount NUMERIC, total_amount NUMERIC,
    payment_method TEXT, payment_status TEXT,
    bill_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, gateway_order_id TEXT,
    gateway_payment_id TEXT, payment_reference TEXT, gateway_signature TEXT);

INSERT INTO users (username, password_hash, full_name, role)
    VALUES ('legacy_admin', 'x', 'Legacy Admin', 'admin');
INSERT INTO categories (category_name, user_id) VALUES ('Drinks', 1);
INSERT INTO foods (category_id, food_name, price, availability, user_id)
    VALUES (1, 'Filter Coffee', 30.00, 1, 1);
INSERT INTO orders (total_amount, order_status, user_id)
    VALUES (60.00, 'Completed', 1);

-- Two stock rows for the same food: the bug the unique key prevents.
INSERT INTO inventory (food_id, quantity, minimum_stock) VALUES (1, 10, 2);
INSERT INTO inventory (food_id, quantity, minimum_stock) VALUES (1, 47, 5);

-- Three bills for one order: the other bug the unique key prevents.
INSERT INTO bills (order_id, subtotal, tax, discount, total_amount,
                   payment_method, payment_status)
    VALUES (1, 60.00, 3.00, 0, 63.00, 'Cash', 'Paid');
INSERT INTO bills (order_id, subtotal, tax, discount, total_amount,
                   payment_method, payment_status)
    VALUES (1, 60.00, 3.00, 0, 63.00, 'Cash', 'Pending');
INSERT INTO bills (order_id, subtotal, tax, discount, total_amount,
                   payment_method, payment_status)
    VALUES (1, 60.00, 3.00, 0, 63.00, 'Cash', 'Pending');
""")
raw.commit()

before_inv = query("SELECT COUNT(*) AS n FROM inventory")[0]["n"]
before_bills = query("SELECT COUNT(*) AS n FROM bills")[0]["n"]
check("Legacy database starts with duplicate inventory rows",
      before_inv == 2, f"rows={before_inv}")
check("Legacy database starts with duplicate bills",
      before_bills == 3, f"rows={before_bills}")

print("\n=== Running the automatic upgrade ===")
try:
    with application.app.app_context():
        application.ensure_auth_schema()
    upgrade_ok, upgrade_err = True, ""
except Exception as error:  # noqa: BLE001
    upgrade_ok, upgrade_err = False, repr(error)

check("ensure_auth_schema() completes on a legacy database",
      upgrade_ok, upgrade_err)

print("\n=== Verifying the result ===")
after_inv = query("SELECT * FROM inventory")
check("Duplicate inventory rows collapsed to one",
      len(after_inv) == 1, f"rows={len(after_inv)}")
check("The NEWEST stock count survived (quantity 47, not 10)",
      len(after_inv) == 1 and int(after_inv[0]["quantity"]) == 47,
      f"kept={after_inv}")

after_bills = query("SELECT * FROM bills")
check("Duplicate bills collapsed to one",
      len(after_bills) == 1, f"rows={len(after_bills)}")
check("The ORIGINAL bill survived (the Paid one, bill_id 1)",
      len(after_bills) == 1 and after_bills[0]["bill_id"] == 1
      and after_bills[0]["payment_status"] == "Paid",
      f"kept={after_bills}")

idx = query("SELECT name FROM sqlite_master WHERE type='index'")
names = {r["name"] for r in idx}
check("uq_inventory_food constraint was created",
      "uq_inventory_food" in names, f"indexes={sorted(names)}")
check("uq_bills_order constraint was created",
      "uq_bills_order" in names, f"indexes={sorted(names)}")

# The constraints must actually bite from now on.
try:
    raw.execute("INSERT INTO inventory (food_id, quantity, minimum_stock) "
                "VALUES (1, 99, 1)")
    raw.commit()
    blocked = False
except Exception:
    raw.rollback()
    blocked = True
check("A second inventory row for the same food is now rejected", blocked)

try:
    raw.execute("INSERT INTO bills (order_id, subtotal, tax, discount, "
                "total_amount, payment_method, payment_status) "
                "VALUES (1, 1, 0, 0, 1, 'Cash', 'Pending')")
    raw.commit()
    blocked = False
except Exception:
    raw.rollback()
    blocked = True
check("A second bill for the same order is now rejected", blocked)

check("Legacy data was adopted into a cafe tenant",
      len(query("SELECT * FROM cafes")) == 1,
      str(query("SELECT * FROM cafes")))

print("\n=== Re-running the migration (must be idempotent) ===")
application.AUTH_SCHEMA_READY = False
try:
    with application.app.app_context():
        application.ensure_auth_schema()
    second_ok, second_err = True, ""
except Exception as error:  # noqa: BLE001
    second_ok, second_err = False, repr(error)
check("Running the upgrade a second time is a safe no-op",
      second_ok, second_err)
check("No rows were lost on the second run",
      len(query("SELECT * FROM inventory")) == 1
      and len(query("SELECT * FROM bills")) == 1)


print("\n=== Paid-bill safety rules ===")

def _reset_bills(cur):
    cur.execute("DELETE FROM bills")

_c = application.get_db_connection()
_cur = _c.cursor(dictionary=True)

# A duplicate where the NEWER bill is the paid one. A naive "keep the
# oldest row" rule would delete the payment record.
_reset_bills(_cur)
_cur.execute("DROP INDEX IF EXISTS uq_bills_order")
_c.commit()
_cur.execute("INSERT INTO bills (order_id, subtotal, tax, discount, "
             "total_amount, payment_method, payment_status) "
             "VALUES (7001, 0, 0, 0, 250.00, 'Cash', 'Pending')")
_cur.execute("INSERT INTO bills (order_id, subtotal, tax, discount, "
             "total_amount, payment_method, payment_status) "
             "VALUES (7001, 0, 0, 0, 250.00, 'Cash', 'Paid')")
_c.commit()

application._ensure_unique_keys(_cur)
_c.commit()
_cur.execute("SELECT payment_status FROM bills WHERE order_id = 7001")
_left = _cur.fetchall()
check("Duplicate collapsed when the newer bill is the paid one",
      len(_left) == 1, _left)
check("The PAID bill survived, not the older Pending one",
      _left and _left[0]["payment_status"] == "Paid", _left)

# Two genuinely paid bills: ambiguous, so nothing may be deleted.
_reset_bills(_cur)
_cur.execute("DROP INDEX IF EXISTS uq_bills_order")
_cur.execute("INSERT INTO bills (order_id, subtotal, tax, discount, "
             "total_amount, payment_method, payment_status) "
             "VALUES (7002, 0, 0, 0, 100.00, 'Cash', 'Paid')")
_cur.execute("INSERT INTO bills (order_id, subtotal, tax, discount, "
             "total_amount, payment_method, payment_status) "
             "VALUES (7002, 0, 0, 0, 100.00, 'Cash', 'Paid')")
_c.commit()
application._ensure_unique_keys(_cur)
_c.commit()
_cur.execute("SELECT bill_id FROM bills WHERE order_id = 7002")
_both = _cur.fetchall()
check("Two PAID bills for one order: migration refuses and deletes nothing",
      len(_both) == 2, _both)


print("\n=== Pre-existing unique index under a different name ===")
# Reproduces MySQL warning 1831: the old schema already enforced
# uniqueness on inventory(food_id) via an index simply named `food_id`.
# Looking for our own key name would add a second, identical index.
_c2 = application.get_db_connection()
_cur2 = _c2.cursor(dictionary=True)
_cur2.execute("DROP TABLE IF EXISTS legacy_inv")
_cur2.execute("""
    CREATE TABLE legacy_inv (
        inventory_id INTEGER PRIMARY KEY AUTOINCREMENT,
        food_id INTEGER NOT NULL,
        quantity INTEGER NOT NULL DEFAULT 0
    )
""")
_cur2.execute("CREATE UNIQUE INDEX food_id ON legacy_inv (food_id)")
_c2.commit()

_found = application._unique_index_on_column(_cur2, "legacy_inv", "food_id")
check("An existing unique index is detected under its own name",
      _found == "food_id", f"found={_found!r}")

# A composite unique index must NOT satisfy a single-column requirement.
_cur2.execute("DROP TABLE IF EXISTS composite_inv")
_cur2.execute("""
    CREATE TABLE composite_inv (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        food_id INTEGER NOT NULL,
        counted_on TEXT NOT NULL
    )
""")
_cur2.execute("CREATE UNIQUE INDEX uq_pair ON composite_inv (food_id, counted_on)")
_c2.commit()

_found2 = application._unique_index_on_column(_cur2, "composite_inv", "food_id")
check("A composite unique index does not count as constraining the column "
      "on its own",
      _found2 is None, f"found={_found2!r}")

print("\n" + "=" * 60)
print(f"PASSED: {len(PASSED)}   FAILED: {len(FAILED)}")
for f in FAILED:
    print("  -", f)
print("=" * 60)
sys.exit(1 if FAILED else 0)
