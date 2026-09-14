"""
Menu numbers on Food Management, and what deleting a food does to history.

Two things the owner sees as one feature:

  * The ID column counts 1, 2, 3 within the cafe, and a number is handed
    back out when its food is deleted - so the list never grows holes.
  * Deleting a food does not rewrite the past. The real primary key is
    never reused, and every order line keeps the name it was sold under,
    so an old bill still reads correctly and still adds up.

The second half is why the first is safe. A menu number is a label; the
food_id that order history points at is left alone.

Run with:  python tests/food_number_test.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "food-number-secret"
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


def sign_up(cafe, username):
    client = app.test_client()
    client.post("/register", data={
        "cafe_name": cafe, "full_name": username.title(), "username": username,
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)
    client.post("/categories/add", data={
        "category_name": "Coffee", "description": "",
        "_csrf_token": csrf(client)}, follow_redirects=True)
    return client


def category_of(client):
    return re.search(r'<option value="(\d+)">',
                     client.get("/foods/add").get_data(as_text=True)).group(1)


def add_food(client, name, price="100", stock="20"):
    client.post("/foods/add", data={
        "food_name": name, "category_id": category_of(client), "price": price,
        "quantity": stock, "description": "", "_csrf_token": csrf(client)},
        follow_redirects=True)


ROW = re.compile(
    r"<tr>\s*(?:\{#.*?#\}\s*)?<td>(\d+)</td>.*?cell-strong\">([^<]+)<", re.S)


def menu(client):
    """[(number, name), ...] exactly as Food Management prints it."""
    return ROW.findall(client.get("/foods").get_data(as_text=True))


def food_ids(client):
    return re.findall(r'/foods/edit/(\d+)',
                      client.get("/foods").get_data(as_text=True))


def id_of(client, name):
    """The real food_id of a named food, read off its own row."""
    html = client.get("/foods").get_data(as_text=True)
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        if re.search(r'cell-strong">%s<' % re.escape(name), row):
            found = re.search(r"/foods/edit/(\d+)", row)
            if found:
                return found.group(1)
    return None


def delete_food(client, food_id):
    client.post("/foods/delete/%s" % food_id,
                data={"_csrf_token": csrf(client)}, follow_redirects=True)


owner = sign_up("Number Cafe", "boss")
for name in ("Latte", "Mocha", "Chai", "Toast", "Bun"):
    add_food(owner, name)


print("\n=== 1. The ID column sits under the ID heading ===")
html = owner.get("/foods").get_data(as_text=True)
headings = re.findall(r"<th>([^<]+)</th>",
                      re.search(r"<thead>.*?</thead>", html, re.S).group(0))
check("the table still leads with ID then Photo",
      headings[:2] == ["ID", "Photo"], "headings are %s" % headings[:3])

first_row = re.search(r"<tbody>.*?<tr>(.*?)</tr>", html, re.S).group(1)
check("and the first cell of a row is the number, not the photo",
      re.match(r"\s*(?:\{#.*?#\}\s*)?<td>\d+</td>", first_row, re.S) is not None,
      "the row starts with %r" % first_row.strip()[:70])
check("the photo cell follows it",
      re.search(r"<td>\d+</td>\s*<td>\s*(?:<img|<span class=\"badge\")",
                first_row, re.S) is not None,
      "photo is not the second cell")


print("\n=== 2. Numbering counts from one, with no holes ===")
check("five foods are numbered one to five",
      [number for number, _ in menu(owner)] == ["1", "2", "3", "4", "5"],
      "the list reads %s" % menu(owner))
check("and they read down in that order",
      [name for _, name in menu(owner)] ==
      ["Latte", "Mocha", "Chai", "Toast", "Bun"],
      "the list reads %s" % menu(owner))


print("\n=== 3. Deleting frees the number for the next food ===")
chai_id = food_ids(owner)[2]
delete_food(owner, chai_id)
check("the deleted food leaves a gap",
      [number for number, _ in menu(owner)] == ["1", "2", "4", "5"],
      "the list reads %s" % menu(owner))

add_food(owner, "Cookie")
check("the next food added takes the freed number",
      ("3", "Cookie") in menu(owner),
      "the list reads %s" % menu(owner))
check("and nothing else moved",
      [number for number, _ in menu(owner)] == ["1", "2", "3", "4", "5"],
      "the list reads %s" % menu(owner))

add_food(owner, "Donut")
check("with no gap left, numbering carries on from the end",
      ("6", "Donut") in menu(owner), "the list reads %s" % menu(owner))


print("\n=== 4. The number is a label, not the key ===")
# Cookie reuses menu number 3. If the primary key were reused with it,
# every past order containing Chai would silently become an order for
# Cookie.
cookie_id = None
for food_id in food_ids(owner):
    page = owner.get("/foods/edit/%s" % food_id).get_data(as_text=True)
    if "Cookie" in page:
        cookie_id = food_id
        break
check("the refilled food has its own primary key",
      cookie_id is not None and cookie_id != chai_id,
      "Cookie was given the deleted food's id %s" % chai_id)


print("\n=== 5. Each cafe numbers its own menu ===")
other = sign_up("Second Cafe", "owner2")
add_food(other, "Masala Chai")
check("a second cafe starts at one", menu(other) == [("1", "Masala Chai")],
      "the second cafe reads %s" % menu(other))
check("and the first cafe is untouched", len(menu(owner)) == 6,
      "the first cafe reads %s" % menu(owner))


print("\n=== 6. A deleted food does not erase the orders it was in ===")
seller = sign_up("History Cafe", "hist")
add_food(seller, "Espresso", price="100", stock="50")
add_food(seller, "Croissant", price="150", stock="50")

espresso_id = id_of(seller, "Espresso")
croissant_id = id_of(seller, "Croissant")
seller.post("/orders/add", data={"quantity_%s" % espresso_id: "2",
                                 "quantity_%s" % croissant_id: "1",
                                 "_csrf_token": csrf(seller)},
            follow_redirects=True)
seller.get("/billing")          # raises the bill


def names_on(path):
    body = seller.get(path).get_data(as_text=True)
    return sorted(set(re.findall(r"Espresso|Croissant", body)))


check("both items are on the order to begin with",
      names_on("/orders/1") == ["Croissant", "Espresso"],
      "the order shows %s" % names_on("/orders/1"))

delete_food(seller, espresso_id)   # Espresso leaves the menu

check("the order still lists what was sold",
      names_on("/orders/1") == ["Croissant", "Espresso"],
      "after deleting Espresso the order shows %s" % names_on("/orders/1"))
check("so does the printed bill",
      names_on("/orders/1/bill") == ["Croissant", "Espresso"],
      "the bill shows %s" % names_on("/orders/1/bill"))
check("and the kitchen ticket",
      names_on("/orders/1/kot") == ["Croissant", "Espresso"],
      "the ticket shows %s" % names_on("/orders/1/kot"))

check("the deleted food is off the menu all the same",
      "Espresso" not in seller.get("/foods").get_data(as_text=True),
      "it is still on Food Management")
check("and off the New Order screen",
      "Espresso" not in seller.get("/orders/add").get_data(as_text=True),
      "it can still be ordered")


print("\n=== 7. The bill still adds up ===")
bill_page = seller.get("/orders/1/bill").get_data(as_text=True)
amounts = [float(value.replace(",", ""))
           for value in re.findall(r"(\d[\d,]*\.\d\d)", bill_page)]
lines = mysql_shim._DB.execute(
    "SELECT subtotal FROM order_items WHERE order_id = 1").fetchall()
line_total = sum(float(row[0]) for row in lines)
check("the line subtotals still sum to the order's subtotal",
      any(abs(value - line_total) < 0.01 for value in amounts),
      "lines add to %.2f but the bill shows %s" % (line_total, amounts))


print("\n=== 8. Reports keep revenue that actually happened ===")
report = seller.get("/reports").get_data(as_text=True)
check("a removed food's sales are still reported",
      "Espresso" in report,
      "the sale vanished from the report when the food was deleted")

# Two different removed foods must not be heaped together.
add_food(seller, "Danish", price="90", stock="10")
danish_id = id_of(seller, "Danish")
seller.post("/orders/add",
            data={"quantity_%s" % danish_id: "3",
                  "_csrf_token": csrf(seller)}, follow_redirects=True)
delete_food(seller, danish_id)

report = seller.get("/reports").get_data(as_text=True)
check("each removed food is reported under its own name",
      "Espresso" in report and "Danish" in report,
      "the two removed foods were merged into one row")
check("and neither shows as an unnamed row",
      report.count("Removed item") == 0,
      "a sold item lost its name")


print("\n=== 9. An upgraded database gets numbers without losing order ===")
# Existing menus predate the column, so the backfill decides what they are
# numbered. It must follow the order the owner already knows.
mysql_shim._DB.execute("UPDATE foods SET food_no = NULL")
mysql_shim._DB.commit()

connection = application.get_db_connection()
cursor = connection.cursor(dictionary=True)
application._backfill_food_numbers(cursor)
connection.commit()
cursor.close()
connection.close()

rows = mysql_shim._DB.execute(
    "SELECT food_id, user_id, food_no FROM foods ORDER BY user_id, food_id"
).fetchall()
by_owner = {}
for food_id, user_id, food_no in rows:
    by_owner.setdefault(user_id, []).append((food_id, food_no))

check("every food ends up with a number",
      all(number is not None for pairs in by_owner.values()
          for _, number in pairs),
      "some foods were left unnumbered: %s" % by_owner)
check("numbers are unique within each cafe",
      all(len({number for _, number in pairs}) == len(pairs)
          for pairs in by_owner.values()),
      "a cafe has two foods sharing a number: %s" % by_owner)
check("and they follow the order the menu was built in",
      all([number for _, number in pairs] ==
          sorted(number for _, number in pairs)
          for pairs in by_owner.values()),
      "the backfill scrambled the order: %s" % by_owner)


print("\n=== 10. Inventory shows the same numbers as Food Management ===")
# These were two different sequences: Food Management counted foods and
# Inventory counted its own rows, so the same item had two ids depending
# on which screen you were looking at.
numbered = sign_up("Shelf Cafe", "shelf")
for item in ("Espresso", "Muffin", "Scone", "Tart"):
    add_food(numbered, item)


def inventory(client):
    return ROW.findall(client.get("/inventory").get_data(as_text=True))


check("both screens list the same items with the same numbers",
      inventory(numbered) == menu(numbered),
      "Food Management %s, Inventory %s"
      % (menu(numbered), inventory(numbered)))

delete_food(numbered, id_of(numbered, "Scone"))
add_food(numbered, "Flapjack")

check("and they stay in step when a number is freed and refilled",
      inventory(numbered) == menu(numbered),
      "Food Management %s, Inventory %s"
      % (menu(numbered), inventory(numbered)))
check("the refilled number is the freed one on both",
      ("3", "Flapjack") in inventory(numbered),
      "Inventory reads %s" % inventory(numbered))


print("\n=== 11. Deleting a category does not empty the shelf ===")
# foods.category_id is ON DELETE SET NULL, and Inventory used to join
# categories with an inner join - so every food of a deleted category
# dropped off Inventory while still sitting in Food Management.
listing = numbered.get("/categories").get_data(as_text=True)
category_id = re.search(r"/categories/edit/(\d+)", listing).group(1)
numbered.post("/categories/delete/%s" % category_id,
              data={"_csrf_token": csrf(numbered)}, follow_redirects=True)

check("the food is still on Food Management", len(menu(numbered)) == 4,
      "Food Management reads %s" % menu(numbered))
check("and still on Inventory", len(inventory(numbered)) == 4,
      "Inventory reads %s" % inventory(numbered))
check("with the two still agreeing",
      inventory(numbered) == menu(numbered),
      "Food Management %s, Inventory %s"
      % (menu(numbered), inventory(numbered)))

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
