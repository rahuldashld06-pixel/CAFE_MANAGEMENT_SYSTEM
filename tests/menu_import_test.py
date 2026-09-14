"""
Building a menu from a photograph of the menu card.

The photo goes to a model, which sends back a list of items. Two halves
are worth pinning down and neither of them needs the network:

  * reading the reply. Models wrap JSON in markdown fences, put a sentence
    in front of it, and write prices as "Rs 180". Anything that cannot be
    read as an item has to be dropped rather than guessed at.
  * writing the result. Categories that do not exist yet get made, items
    already on the menu have their price updated instead of being added a
    second time, and every new food gets a menu number and a stock row.

Nothing reaches the menu straight from the photograph: the owner sees what
was read and confirms it. A misread 8 for a 3 is a price a customer gets
charged, so that step is the point rather than a formality.

The call to the model is stubbed here. What it would return is exactly
what parse_menu_items is fed directly.

Run with:  python tests/menu_import_test.py
"""
import os
import re
import sys
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "menu-import-secret"
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
    return client


ROW = re.compile(
    r"<tr>\s*(?:\{#.*?#\}\s*)?<td>(\d+)</td>.*?cell-strong\">([^<]+)<", re.S)


def menu(client):
    return ROW.findall(client.get("/foods").get_data(as_text=True))


print("\n=== 1. Reading a tidy reply ===")
items = application.parse_menu_items(
    '[{"name": "Flat White", "category": "Coffee", "price": 180},'
    ' {"name": "Scone", "category": "Bakery", "price": 90}]')
check("both items are read", len(items) == 2, "read %s" % items)
check("names, categories and prices come through",
      items[0] == {"name": "Flat White", "category": "Coffee",
                   "price": Decimal("180.00")},
      "first item is %s" % items[0])


print("\n=== 2. Reading the messy replies models actually send ===")
fenced = application.parse_menu_items(
    '```json\n[{"name": "Latte", "category": "Coffee", "price": 150}]\n```')
check("a markdown fence is stripped", len(fenced) == 1,
      "read %s" % fenced)

chatty = application.parse_menu_items(
    'Here is the menu I could read:\n'
    '[{"name": "Chai", "category": "Tea", "price": 60}]\n'
    'Let me know if you need anything else.')
check("a sentence either side is ignored", len(chatty) == 1,
      "read %s" % chatty)

money = application.parse_menu_items(
    '[{"name": "Mocha", "category": "Coffee", "price": "Rs 210.50"},'
    ' {"name": "Tart", "category": "Bakery", "price": "\\u20b9 1,120"}]')
check("a price with a currency symbol is still a number",
      len(money) == 2 and money[0]["price"] == Decimal("210.50"),
      "read %s" % money)
check("and a thousands separator does not break it",
      len(money) == 2 and money[1]["price"] == Decimal("1120.00"),
      "read %s" % money)


print("\n=== 3. What cannot be read is dropped, not guessed ===")
messy = application.parse_menu_items(
    '[{"name": "Good", "category": "Coffee", "price": 100},'
    ' {"name": "", "category": "Coffee", "price": 50},'
    ' {"name": "No price", "category": "Coffee"},'
    ' {"name": "Bad price", "category": "Coffee", "price": "ask inside"},'
    ' {"name": "Negative", "category": "Coffee", "price": -5},'
    ' "not an object",'
    ' {"name": "Fine", "category": "Tea", "price": 40}]')
check("only the readable items survive",
      [item["name"] for item in messy] == ["Good", "Fine"],
      "kept %s" % [item["name"] for item in messy])

check("an item with no category gets a placeholder rather than nothing",
      application.parse_menu_items(
          '[{"name": "Loose", "price": 10}]')[0]["category"]
      == "Uncategorised",
      "category came out empty")

for reply in ("", "Sorry, I cannot read this photo.", "[]", "{}", "null",
              "[[[", '{"name": "x"}'):
    check("a reply of %r yields nothing" % reply[:28],
          application.parse_menu_items(reply) == [],
          "got %s" % application.parse_menu_items(reply))

flood = application.parse_menu_items(
    "[" + ",".join('{"name": "Item %d", "category": "C", "price": 1}' % n
                   for n in range(400)) + "]")
check("a runaway list is capped",
      len(flood) == application.MENU_IMPORT_MAX_ITEMS,
      "kept %d items" % len(flood))


print("\n=== 4. Saving what was read ===")
owner = sign_up("Photo Cafe", "boss")

READING = [
    {"name": "Flat White", "category": "Coffee", "price": Decimal("180.00"),
     "quantity": 10, "minimum_stock": 2},
    {"name": "Cold Brew", "category": "Coffee", "price": Decimal("220.00")},
    {"name": "Almond Croissant", "category": "Bakery",
     "price": Decimal("150.00")},
]


def apply(client, rows):
    connection = application.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    with app.test_request_context("/"):
        from flask import session as flask_session
        with client.session_transaction() as sess:
            flask_session.update(dict(sess))
        result = application.apply_menu_items(
            cursor, application.scope_user_id(),
            application.require_cafe_session(), rows)
    connection.commit()
    cursor.close()
    connection.close()
    return result


added, updated, made = apply(owner, READING)
check("every item is added", added == 3, "added %d" % added)
check("nothing is counted as an update", updated == 0, "updated %d" % updated)
check("both categories are created", made == 2, "created %d" % made)

listing = menu(owner)
check("the foods are on the menu",
      sorted(name for _, name in listing) ==
      ["Almond Croissant", "Cold Brew", "Flat White"],
      "the menu reads %s" % listing)
check("and they are numbered from one",
      [number for number, _ in listing] == ["1", "2", "3"],
      "the menu reads %s" % listing)

categories = owner.get("/categories").get_data(as_text=True)
check("the categories exist too",
      "Coffee" in categories and "Bakery" in categories,
      "the categories page does not list both")

inventory = owner.get("/inventory").get_data(as_text=True)
check("every food gets a stock row", inventory.count("/inventory/update/") == 3,
      "found %d stock rows" % inventory.count("/inventory/update/"))
check("the stock given on the review is kept", "10" in inventory,
      "the quantity of 10 was not saved")


print("\n=== 5. Importing the same card again updates, it does not duplicate ===")
# A price rise is the obvious reason to do this twice.
RAISED = [
    {"name": "Flat White", "category": "Coffee", "price": Decimal("200.00")},
    {"name": "Banana Bread", "category": "Bakery", "price": Decimal("120.00")},
]
added, updated, made = apply(owner, RAISED)
check("the item already there is updated", updated == 1, "updated %d" % updated)
check("the new one is added", added == 1, "added %d" % added)
check("an existing category is reused", made == 0,
      "created %d categories that already existed" % made)
check("there is still only one Flat White",
      [name for _, name in menu(owner)].count("Flat White") == 1,
      "the menu reads %s" % menu(owner))
check("and its price is the new one",
      "200" in owner.get("/foods").get_data(as_text=True),
      "the price was not updated")

check("matching ignores capitals",
      apply(owner, [{"name": "FLAT WHITE", "category": "Coffee",
                     "price": Decimal("210.00")}])[1] == 1,
      "a differently-capitalised name was added as a second food")


print("\n=== 6. Only an admin may do this ===")
owner.post("/users/add", data={
    "full_name": "Cash", "username": "cash", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(owner)}, follow_redirects=True)

cashier = app.test_client()
cashier.post("/login", data={"username": "cash", "password": "password123"},
             follow_redirects=True)

check("a cashier is not offered it in the profile menu",
      "Menu from a Photo" not in
      cashier.get("/orders/add").get_data(as_text=True),
      "the menu entry is visible to a cashier")
check("an admin is",
      "Menu from a Photo" in owner.get("/orders/add").get_data(as_text=True),
      "the admin has no way to reach it")
check("opening the page is turned away",
      cashier.get("/settings/menu-import",
                  follow_redirects=False).status_code in (302, 303),
      "a cashier got in")

before = len(menu(owner))
cashier.post("/settings/menu-import/apply", data={
    "row": "0", "include_0": "1", "name_0": "Snuck In",
    "category_0": "Coffee", "price_0": "1",
    "_csrf_token": csrf(cashier)}, follow_redirects=True)
check("and posting to it changes nothing", len(menu(owner)) == before,
      "a cashier added a food through the import route")

check("neither endpoint is on the staff allowlist",
      "menu_import" not in application.STAFF_ALLOWED_ENDPOINTS
      and "menu_import_apply" not in application.STAFF_ALLOWED_ENDPOINTS,
      "the allowlist would let a non-admin through")


print("\n=== 7. The upload leads to a review, not straight to the menu ===")
original = application.read_menu_photo
application.read_menu_photo = lambda data, mime: [
    {"name": "Stubbed Latte", "category": "Coffee", "price": Decimal("175.00")},
    {"name": "Flat White", "category": "Coffee", "price": Decimal("250.00")},
]
try:
    import io as _io
    before = len(menu(owner))
    response = owner.post(
        "/settings/menu-import",
        data={"photo": (_io.BytesIO(b"not-really-a-jpeg"), "menu.jpg"),
              "_csrf_token": csrf(owner)},
        content_type="multipart/form-data", follow_redirects=True)
    page = response.get_data(as_text=True)

    check("the reading is shown for checking", "Stubbed Latte" in page,
          "the review page does not list what was read")
    check("nothing was saved yet", len(menu(owner)) == before,
          "the upload wrote to the menu without being confirmed")
    check("an item already on the menu is marked as an update",
          "Updates" in page, "the review does not say which rows are updates")
    # A bare `badge` class has no background and no colour of its own, so
    # the marker rendered as loose text beside the pill next to it.
    check("and that marker is a styled pill, not bare text",
          "badge badge--update" in page,
          "the update marker has no badge modifier")
    check("a new item is marked as new", "New" in page,
          "the review does not say which rows are new")
    check("the rows are editable before saving",
          'name="price_0"' in page and 'name="name_0"' in page,
          "the review is not editable")
    check("existing categories are offered as suggestions",
          "known-categories" in page and "<datalist" in page,
          "no category suggestions on the review")
finally:
    application.read_menu_photo = original


print("\n=== 8. Confirming the review is what writes it ===")
before = len(menu(owner))
owner.post("/settings/menu-import/apply", data={
    "row": ["0", "1"],
    "include_0": "1", "name_0": "Stubbed Latte", "category_0": "Coffee",
    "price_0": "175.00", "quantity_0": "5", "minimum_stock_0": "1",
    # Row 1 is left unticked.
    "name_1": "Rejected Item", "category_1": "Coffee", "price_1": "99",
    "_csrf_token": csrf(owner)}, follow_redirects=True)

names = [name for _, name in menu(owner)]
check("the ticked row is saved", "Stubbed Latte" in names,
      "the menu reads %s" % names)
check("the unticked row is not", "Rejected Item" not in names,
      "an unticked row was saved anyway")
check("exactly one food was added", len(menu(owner)) == before + 1,
      "the menu went from %d to %d" % (before, len(menu(owner))))

check("a price that is not a number is refused outright",
      "not a number" in owner.post("/settings/menu-import/apply", data={
          "row": "0", "include_0": "1", "name_0": "Bad", "category_0": "C",
          "price_0": "free", "_csrf_token": csrf(owner)},
          follow_redirects=True).get_data(as_text=True),
      "a non-numeric price was accepted")
check("and nothing was written when it was refused",
      "Bad" not in [name for _, name in menu(owner)],
      "the bad row was saved")

check("ticking nothing says so rather than failing silently",
      "No items were ticked" in owner.post(
          "/settings/menu-import/apply",
          data={"row": "0", "name_0": "Ignored", "price_0": "1",
                "_csrf_token": csrf(owner)},
          follow_redirects=True).get_data(as_text=True),
      "an empty submission gave no message")


print("\n=== 9. One cafe's import does not reach another ===")
other = sign_up("Second Cafe", "owner2")
apply(other, [{"name": "Their Own Coffee", "category": "Coffee",
               "price": Decimal("100.00")}])

check("the second cafe has only its own item",
      [name for _, name in menu(other)] == ["Their Own Coffee"],
      "the second cafe reads %s" % menu(other))
check("and the first cafe is untouched",
      "Their Own Coffee" not in [name for _, name in menu(owner)],
      "an item leaked between cafes")
check("its numbering starts at one of its own",
      [number for number, _ in menu(other)] == ["1"],
      "the second cafe reads %s" % menu(other))


print("\n=== 10. With no API key the page explains itself ===")
key = application.ANTHROPIC_API_KEY
try:
    application.ANTHROPIC_API_KEY = ""
    check("the feature reports itself as unavailable",
          application.menu_import_available() is False,
          "it claims to be available with no key")

    page = owner.get("/settings/menu-import").get_data(as_text=True)
    check("the page says what is missing", "ANTHROPIC_API_KEY" in page,
          "the page does not explain why it cannot work")
    check("and offers no upload box that would fail",
          'name="photo"' not in page,
          "an upload form is shown that cannot possibly work")

    raised = ""
    try:
        application.read_menu_photo(b"x", "image/jpeg")
    except ValueError as error:
        raised = str(error)
    check("reading refuses with something worth reading",
          "ANTHROPIC_API_KEY" in raised, "the error said %r" % raised)
finally:
    application.ANTHROPIC_API_KEY = key

bad_format = ""
try:
    application.ANTHROPIC_API_KEY = "test-key-not-used"
    application.read_menu_photo(b"x", "application/pdf")
except ValueError as error:
    bad_format = str(error)
finally:
    application.ANTHROPIC_API_KEY = key
check("a format the reader cannot take is refused before any request",
      "JPG" in bad_format, "the error said %r" % bad_format)


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
