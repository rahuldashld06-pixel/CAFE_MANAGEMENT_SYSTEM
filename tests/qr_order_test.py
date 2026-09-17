"""
Ordering by scanning the code on the table.

A customer points a phone at the code, lands on that cafe's menu, sends
an order to the kitchen and is given a number to quote at the counter.
They never sign in and never see anything but that one cafe.

What has to hold:

  * the address carries a random token, not the cafe's row id, so walking
    /m/1, /m/2 is not a way to browse every cafe on the system;
  * each cafe's code is its own, and one cafe's code reaches nothing
    belonging to another;
  * a customer's order is a real order - same stock checks, same prices,
    same tax as the counter, because it goes through the same code;
  * it lands in Order Management, marked as having come from a phone;
  * its kitchen ticket is claimed by exactly one screen, so two tills
    watching the same kitchen print one ticket between them;
  * and prices come from the menu, never from the request.

Run with:  python tests/qr_order_test.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "qr-order-secret"
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


def build_cafe(cafe, username, foods):
    """A signed-in admin with a menu, and the token its code points at."""
    client = app.test_client()
    client.post("/register", data={
        "cafe_name": cafe, "full_name": username.title(), "username": username,
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)

    client.post("/categories/add", data={
        "category_name": "Coffee", "description": "",
        "_csrf_token": csrf(client)}, follow_redirects=True)
    category = re.search(
        r'<option value="(\d+)">',
        client.get("/foods/add").get_data(as_text=True)).group(1)

    for name, price, stock in foods:
        client.post("/foods/add", data={
            "food_name": name, "category_id": category, "price": price,
            "quantity": stock, "minimum_stock": "1", "description": "",
            "_csrf_token": csrf(client)}, follow_redirects=True)

    with client.session_transaction() as sess:
        cafe_id = sess.get("cafe_id")

    return client, cafe_id, application.get_public_token(cafe_id)


def stock_of(food_id):
    row = mysql_shim._DB.execute(
        "SELECT quantity FROM inventory WHERE food_id = ?",
        (food_id,)).fetchone()
    return row[0] if row else None


admin, cafe_id, token = build_cafe(
    "Bluebird", "sam", [("Flat White", "180", "10"), ("Chai", "70", "4")])


print("\n=== 1. The address is a token, not a row id ===")
check("a token is minted the first time it is asked for",
      bool(token) and len(token) >= 16, "got %r" % token)
check("asking again gives the same one",
      application.get_public_token(cafe_id) == token,
      "the code would change under a printed sticker")
check("the cafe's id is not in the address", str(cafe_id) != token,
      "the address gives away which cafe this is")


print("\n=== 2. A customer needs no account ===")
guest = app.test_client()
menu = guest.get("/m/%s" % token)
check("the menu opens without signing in", menu.status_code == 200,
      "got HTTP %s" % menu.status_code)

html = menu.get_data(as_text=True)
check("it names the cafe", "Bluebird" in html, "the cafe is not named")
check("it offers the foods", html.count('name="quantity_') == 2,
      "found %d items" % html.count('name="quantity_'))
check("and none of the staff app comes with it",
      "sidebar" not in html and "page-view" not in html
      and "profile-dropdown" not in html,
      "the staff shell leaked onto a customer's phone")

check("an address that belongs to nobody is a dead end",
      guest.get("/m/not-a-real-token").status_code == 404,
      "an unknown code did not 404")


print("\n=== 3. Sending an order ===")
ids = re.findall(r'name="quantity_(\d+)"', html)
before = [stock_of(int(ids[0])), stock_of(int(ids[1]))]

placed = guest.post("/m/%s/order" % token,
                    data={"quantity_%s" % ids[0]: "2",
                          "quantity_%s" % ids[1]: "1"},
                    follow_redirects=True)
body = placed.get_data(as_text=True)
shown = re.search(r'p-done__number">#(\d+)<', body)

check("the customer is given a number", shown is not None,
      "no order number on the page they were sent to")
check("and shown what they ordered",
      "Flat White" in body and "Chai" in body,
      "the confirmation does not list the items")

order_id = int(shown.group(1)) if shown else 0
row = mysql_shim._DB.execute(
    "SELECT source, kot_printed, order_status, user_id FROM orders "
    "WHERE order_id = ?", (order_id,)).fetchone()

check("the order really exists", row is not None, "no order was written")
check("marked as having come from a phone",
      row and row[0] == "qr", "source is %r" % (row[0] if row else None))
check("and waiting, like any other new order",
      row and row[2] == "Pending", "status is %r" % (row[2] if row else None))

check("stock came off the shelf",
      [stock_of(int(ids[0])), stock_of(int(ids[1]))]
      == [before[0] - 2, before[1] - 1],
      "stock went from %s to %s" % (
          before, [stock_of(int(ids[0])), stock_of(int(ids[1]))]))


print("\n=== 4. It is an order like any other ===")
listing = admin.get("/orders").get_data(as_text=True)
check("it shows up in Order Management",
      ("/orders/%d" % order_id) in listing,
      "the order is not on the Order Management page")
check("and the counter can tell where it came from",
      row and row[0] == "qr",
      "nothing distinguishes it from one a member of staff rang up")


print("\n=== 5. The price is the menu's, not the phone's ===")
# A request that tries to say what something costs must be ignored: only
# the quantity is read from it. The figure expected here is worked out
# from the food's own price rather than guessed at, so this cannot pass
# by accident on whichever item happens to be listed first.
priced = mysql_shim._DB.execute(
    "SELECT price FROM foods WHERE food_id = ?", (int(ids[0]),)).fetchone()
honest = round(float(priced[0]) * 1.05, 2)

guest.post("/m/%s/order" % token,
           data={"quantity_%s" % ids[0]: "1",
                 "price_%s" % ids[0]: "1",
                 "price": "0.01", "total_amount": "0.01"},
           follow_redirects=True)
latest = mysql_shim._DB.execute(
    "SELECT total_amount FROM orders ORDER BY order_id DESC LIMIT 1"
).fetchone()

check("a made-up price in the request is ignored",
      latest and abs(float(latest[0]) - honest) < 0.01,
      "the order was written for %s, and the menu says %s"
      % (latest[0] if latest else None, honest))


print("\n=== 6. What is not on the menu cannot be ordered ===")
empty = guest.post("/m/%s/order" % token, data={}, follow_redirects=True)
check("an empty order is refused",
      "at least one food item" in empty.get_data(as_text=True).lower(),
      "an order with nothing in it was accepted")

over = guest.post("/m/%s/order" % token,
                  data={"quantity_%s" % ids[1]: "999"},
                  follow_redirects=True)
check("more than there is in stock is refused",
      "not enough stock" in over.get_data(as_text=True).lower(),
      "the kitchen was oversold")


print("\n=== 7. One code reaches one cafe ===")
other_admin, other_cafe, other_token = build_cafe(
    "Second Cafe", "bee", [("Their Latte", "150", "5")])

check("each cafe gets its own code", other_token != token,
      "two cafes share one code")

other_menu = guest.get("/m/%s" % other_token).get_data(as_text=True)
check("the second code shows the second cafe's menu",
      "Their Latte" in other_menu, "the wrong menu was shown")
check("and not the first cafe's",
      "Flat White" not in other_menu,
      "one cafe's food appeared on another's menu")

check("an order number cannot be read through another cafe's code",
      guest.get("/m/%s/placed/%d" % (other_token, order_id)).status_code
      == 404,
      "one cafe's order was readable through another's code")


print("\n=== 8. Exactly one screen prints the ticket ===")
pending = json.loads(
    admin.get("/api/kitchen/pending").get_data(as_text=True))
check("the kitchen feed lists the waiting order",
      order_id in pending.get("orders", []),
      "the feed says %s" % pending)

first = json.loads(admin.post(
    "/api/kitchen/claim/%d" % order_id,
    data={"_csrf_token": csrf(admin)}).get_data(as_text=True))
second = json.loads(admin.post(
    "/api/kitchen/claim/%d" % order_id,
    data={"_csrf_token": csrf(admin)}).get_data(as_text=True))

check("the first screen to ask gets it", first.get("claimed") is True,
      "the first claim returned %s" % first)
check("and the second is told no",
      second.get("claimed") is False,
      "two tills would both print the same ticket: %s" % second)

after = json.loads(admin.get("/api/kitchen/pending").get_data(as_text=True))
check("a claimed order drops off the feed",
      order_id not in after.get("orders", []),
      "it would be printed again on the next poll")

check("another cafe cannot claim this one's ticket",
      json.loads(other_admin.post(
          "/api/kitchen/claim/%d" % order_id,
          data={"_csrf_token": csrf(other_admin)}).get_data(as_text=True)
      ).get("claimed") is False,
      "one cafe claimed another's kitchen ticket")


print("\n=== 9. The code page is the admin's ===")
check("an admin can open it",
      admin.get("/settings/qr").status_code == 200,
      "got HTTP %s" % admin.get("/settings/qr").status_code)

page = admin.get("/settings/qr").get_data(as_text=True)
check("it draws a real QR code", "<svg" in page and "</svg>" in page,
      "no code is drawn on the page")
check("and shows the address it opens", token in page,
      "the address is not shown")
check("it is offered in the profile menu",
      "Table QR Code" in admin.get("/orders/add").get_data(as_text=True),
      "there is no way into it")

admin.post("/users/add", data={
    "full_name": "Cash", "username": "cash", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(admin)}, follow_redirects=True)
cashier = app.test_client()
cashier.post("/login", data={"username": "cash", "password": "password123"},
             follow_redirects=True)

check("a cashier is not offered it",
      "Table QR Code" not in
      cashier.get("/orders/add").get_data(as_text=True),
      "the menu entry is visible to a cashier")
check("and cannot open it",
      cashier.get("/settings/qr",
                  follow_redirects=False).status_code in (302, 303),
      "a cashier reached the code page")
check("but can still pull a kitchen ticket",
      cashier.get("/api/kitchen/pending").status_code == 200,
      "a cashier's screen could not print a customer's ticket")


print("\n=== 10. Replacing the code retires the old one ===")
admin.post("/settings/qr", data={"_csrf_token": csrf(admin)},
           follow_redirects=True)
fresh = application.get_public_token(cafe_id)

check("a new code is different from the old", fresh != token,
      "the code did not change")
check("the old one stops working",
      guest.get("/m/%s" % token).status_code == 404,
      "a code that was replaced still opens the menu")
check("and the new one works", guest.get("/m/%s" % fresh).status_code == 200,
      "the replacement does not open the menu")


print("\n=== 11. The screen that lives in the kitchen ===")
board = admin.get("/api/kitchen/board")
check("the kitchen screen opens", admin.get("/kitchen").status_code == 200,
      "got HTTP %s" % admin.get("/kitchen").status_code)
check("a cashier can open it too",
      cashier.get("/kitchen").status_code == 200,
      "the kitchen screen is admin-only, which is not what a kitchen is")

check("its board answers", board.status_code == 200,
      "got HTTP %s" % board.status_code)

# Something to look at: a fresh order from a phone.
fresh_menu = guest.get("/m/%s" % fresh).get_data(as_text=True)
fresh_ids = re.findall(r'name="quantity_(\d+)"', fresh_menu)
guest.post("/m/%s/order" % fresh, data={"quantity_%s" % fresh_ids[0]: "1"},
           follow_redirects=True)

shown = json.loads(admin.get("/api/kitchen/board").get_data(as_text=True))
waiting = shown.get("orders", [])
check("a customer's order appears on the board", len(waiting) >= 1,
      "the board is empty: %s" % shown)
check("with its items, so the kitchen can cook from it",
      waiting and waiting[-1].get("items"),
      "the board shows no items")
check("and says it came from a table",
      any(order.get("source") == "qr" for order in waiting),
      "nothing on the board says where an order came from")


print("\n=== 12. While the kitchen watches, the counter stands down ===")
# Otherwise a customer's ticket comes out of whichever printer somebody
# happened to leave a tab in front of.
quiet = json.loads(admin.get("/api/kitchen/pending").get_data(as_text=True))
check("with no kitchen screen open, the counter takes the job",
      quiet.get("kitchen_watching") is False,
      "the counter was told to stand down with no kitchen screen open")

admin.post("/api/kitchen/heartbeat", data={"_csrf_token": csrf(admin)})
watched = json.loads(admin.get("/api/kitchen/pending").get_data(as_text=True))
check("once one checks in, the counter is told to leave it",
      watched.get("kitchen_watching") is True,
      "the counter would print a ticket meant for the kitchen")

# And a screen that went away must not hold the job for ever.
mysql_shim._DB.execute(
    "UPDATE cafes SET kitchen_seen_at = '2020-01-01 00:00:00' "
    "WHERE cafe_id = ?", (cafe_id,))
mysql_shim._DB.commit()
stale = json.loads(admin.get("/api/kitchen/pending").get_data(as_text=True))
check("a kitchen screen that stopped checking in loses the job",
      stale.get("kitchen_watching") is False,
      "a tablet switched off would leave tickets unprinted for ever")

check("one cafe's kitchen screen does not silence another's counter",
      json.loads(other_admin.get("/api/kitchen/pending")
                 .get_data(as_text=True)).get("kitchen_watching") is False,
      "one cafe's kitchen screen stopped another cafe printing")

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
