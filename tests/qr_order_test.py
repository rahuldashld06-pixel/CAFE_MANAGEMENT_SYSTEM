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
board = admin.get("/api/kitchen/board").get_json()["orders"]
check("it shows up on the kitchen screen",
      any(row["order_id"] == order_id for row in board),
      "the order is not on the kitchen screen")
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

print("\n=== 13. The number people say out loud starts again each day ===")
# order_id cannot do this: it is the key every bill and order line points
# at, so reusing it would tie today's order to yesterday's bill. The
# daily number sits alongside it.
import datetime as _dt  # noqa: E402


def numbers_for(owner):
    return [tuple(row) for row in mysql_shim._DB.execute(
        "SELECT order_id, order_day, daily_no FROM orders "
        "WHERE user_id = ? ORDER BY order_id", (owner,)).fetchall()]


with admin.session_transaction() as sess:
    owner = sess.get("user_id")

todays = [row for row in numbers_for(owner)
          if str(row[1]) == str(_dt.date.today())]
check("today's orders are numbered from one, in order",
      [row[2] for row in todays] == list(range(1, len(todays) + 1)),
      "they are numbered %s" % [row[2] for row in todays])

check("and every order still has its own permanent id",
      len({row[0] for row in numbers_for(owner)})
      == len(numbers_for(owner)),
      "two orders share an id")

# Move everything to yesterday and order again: the count restarts.
mysql_shim._DB.execute(
    "UPDATE orders SET order_day = '2020-01-01' WHERE user_id = ?", (owner,))
mysql_shim._DB.commit()

after_menu = guest.get("/m/%s" % fresh).get_data(as_text=True)
after_ids = re.findall(r'name="quantity_(\d+)"', after_menu)
placed_today = guest.post("/m/%s/order" % fresh,
                          data={"quantity_%s" % after_ids[0]: "1"},
                          follow_redirects=True)

newest = mysql_shim._DB.execute(
    "SELECT order_id, daily_no FROM orders WHERE user_id = ? "
    "ORDER BY order_id DESC LIMIT 1", (owner,)).fetchone()

check("a new day starts again at one", newest and newest[1] == 1,
      "the first order of the new day is number %s"
      % (newest[1] if newest else None))
check("while its permanent id carries on climbing",
      newest and newest[0] > len(todays),
      "the id restarted too, which would collide with yesterday's rows")

check("and the customer is shown that number, not the id",
      ("#%d<" % newest[1]) in placed_today.get_data(as_text=True),
      "the confirmation shows something other than the day's number")


print("\n=== 14. Two cafes both have a number one today ===")
other_menu = guest.get("/m/%s" % other_token).get_data(as_text=True)
other_ids = re.findall(r'name="quantity_(\d+)"', other_menu)
guest.post("/m/%s/order" % other_token,
           data={"quantity_%s" % other_ids[0]: "1"}, follow_redirects=True)

with other_admin.session_transaction() as sess:
    other_owner = sess.get("user_id")

theirs = mysql_shim._DB.execute(
    "SELECT daily_no FROM orders WHERE user_id = ? ORDER BY order_id DESC "
    "LIMIT 1", (other_owner,)).fetchone()

check("the second cafe counts its own day, from one",
      theirs and theirs[0] == 1,
      "the second cafe's first order is number %s"
      % (theirs[0] if theirs else None))

print("\n=== 15. More than one person using it at once ===")
# The site runs several worker processes, so a counter order and a phone
# order really can land at the same moment. Two customers waiting for the
# same number to be called is the failure worth guarding against.
source = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app.py"), encoding="utf-8").read()

# The query is written across two adjacent string literals, so this reads
# the lines around it rather than matching one statement in one go.
at = source.find("COALESCE(MAX(daily_no), 0) + 1")
nearby = source[at:at + 400] if at != -1 else ""

check("the number is claimed with a lock, not just read",
      at != -1 and "FOR UPDATE" in nearby,
      "two orders at once could read the same highest number and both "
      "take it")

check("and there is an index for that lock to be narrow on",
      "orders(user_id, order_day)" in source,
      "without it one cafe's rush would hold up every other cafe")

# Stock is the other thing two tills can race for.
check("stock is taken with the check inside the update",
      re.search(r"UPDATE inventory.{0,400}quantity >= %s", source, re.S)
      is not None,
      "two orders could each be told there was one left")

# And the kitchen ticket, which two screens watch at once.
check("a kitchen ticket is claimed by whoever asks first",
      re.search(r"SET kot_printed = 1.{0,200}kot_printed = 0", source, re.S)
      is not None,
      "two screens could print the same ticket")

print("\n=== 16. The kitchen screen is where orders are managed now ===")
# Order Management was a second list of the same orders, on a page nobody
# in a kitchen is standing in front of. It is gone, and its two useful
# buttons moved onto the screen that is already on.
rules = {str(rule.rule) for rule in app.url_map.iter_rules()}

check("the Order Management page is gone",
      "/orders" not in rules and "orders" not in app.view_functions,
      "it is still registered")

check("but a single order still opens by its own link",
      "/orders/<int:order_id>" in rules,
      "billing links to an order that cannot be opened")

kitchen_html = admin.get("/kitchen").get_data(as_text=True)

check("and nothing offers it in the sidebar any more",
      "Order Management" not in kitchen_html,
      "the link is still there, pointing at nothing")

check("the kitchen offers Done",
      "data-done=" in kitchen_html, "there is no way to finish an order")
check("and Cancel",
      "data-cancel=" in kitchen_html, "there is no way to call one off")
check("and does not offer View",
      "bi-eye" not in kitchen_html,
      "the kitchen was asked for two buttons, not three")

# Something of our own to act on, rung up at the counter.
counter_foods = re.findall(r'id="quantity_(\d+)"',
                           admin.get("/orders/add").get_data(as_text=True))
admin.post("/orders/add",
           data={"quantity_%s" % counter_foods[0]: "1",
                 "_csrf_token": csrf(admin)}, follow_redirects=True)
mine = max(row["order_id"] for row in
           admin.get("/api/kitchen/board").get_json()["orders"])


def board_row(order_id):
    for row in admin.get("/api/kitchen/board").get_json()["orders"]:
        if row["order_id"] == order_id:
            return row
    return None


check("a waiting order says so on the board",
      (board_row(mine) or {}).get("status") == "Pending",
      "the board says %s" % (board_row(mine) or {}).get("status"))

# The screen asks over fetch, so it wants an answer - not a page, and not
# a message left behind for whatever is opened next.
with admin.session_transaction() as sess:
    sess.pop("_flashes", None)

done = admin.post("/orders/complete/%d" % mine,
                  data={"_csrf_token": csrf(admin)},
                  headers={"X-Requested-With": "XMLHttpRequest"})

check("marking one done answers the screen instead of redirecting it",
      done.status_code == 200
      and (done.get_json() or {}).get("success") is True,
      "got HTTP %s: %s" % (done.status_code,
                           done.get_data(as_text=True)[:120]))

with admin.session_transaction() as sess:
    left_behind = sess.get("_flashes") or []
check("and leaves no message to ambush the next page opened",
      not left_behind,
      "a shift of these would arrive in a heap: %s" % (left_behind,))

check("the finished order stays on the board",
      board_row(mine) is not None,
      "it vanished, so the dot that turned green cannot be seen")
check("and the dot it shows is now the done one",
      (board_row(mine) or {}).get("status") == "Completed",
      "the board says %s" % (board_row(mine) or {}).get("status"))

# And cancelling, the other button.
admin.post("/orders/add",
           data={"quantity_%s" % counter_foods[0]: "1",
                 "_csrf_token": csrf(admin)}, follow_redirects=True)
doomed = max(row["order_id"] for row in
             admin.get("/api/kitchen/board").get_json()["orders"])

cancelled = admin.post("/orders/cancel/%d" % doomed,
                       data={"_csrf_token": csrf(admin)},
                       headers={"X-Requested-With": "XMLHttpRequest"})

check("cancelling answers the screen too",
      cancelled.status_code == 200
      and (cancelled.get_json() or {}).get("success") is True,
      "got HTTP %s: %s" % (cancelled.status_code,
                           cancelled.get_data(as_text=True)[:120]))

check("and the cancelled order says so rather than disappearing",
      (board_row(doomed) or {}).get("status") == "Cancelled",
      "the board says %s" % (board_row(doomed) or {}).get("status"))

# Printing must not follow orders onto the finished pile. This reads the
# screen's own filter, because the guard lives in the browser.
check("a finished order is not sent to the printer again",
      'order.status === "Pending"' in kitchen_html,
      "now that finished orders stay on the board, the printing filter "
      "has to exclude them or every one reprints on the next sweep")

print("\n=== 17. Yesterday's forgotten orders do not wait for ever ===")
# The board shows one day. An order nobody pressed Done or Cancel on
# before closing would otherwise sit waiting behind it for ever - and
# every count of what is outstanding would go on including it.


def status_of(order_id):
    row = mysql_shim._DB.execute(
        "SELECT order_status FROM orders WHERE order_id = ?",
        (order_id,)).fetchone()
    return row[0] if row else None


def place_one():
    admin.post("/orders/add",
               data={"quantity_%s" % counter_foods[0]: "1",
                     "_csrf_token": csrf(admin)}, follow_redirects=True)
    return mysql_shim._DB.execute(
        "SELECT MAX(order_id) FROM orders").fetchone()[0]


def move_to_yesterday(order_id):
    yesterday = _dt.date.today() - _dt.timedelta(days=1)
    mysql_shim._DB.execute(
        "UPDATE orders SET order_day = ?, order_date = ? WHERE order_id = ?",
        (yesterday.isoformat(), yesterday.isoformat() + " 20:40:00",
         order_id))
    mysql_shim._DB.commit()


forgotten = place_one()
called_off = place_one()
admin.post("/orders/cancel/%d" % called_off,
           data={"_csrf_token": csrf(admin)}, follow_redirects=True)

still_today = place_one()

move_to_yesterday(forgotten)
move_to_yesterday(called_off)

check("an order left open yesterday is still open before anyone looks",
      status_of(forgotten) == "Pending",
      "it is %s already" % status_of(forgotten))

# The sweep runs once per cafe per day in each worker, and this cafe has
# been swept in this process already. Clearing that is the difference
# between testing the sweep and testing the guard in front of it.
application._ORDERS_SWEPT_FOR.clear()

admin.get("/api/kitchen/board")

check("opening the kitchen on the next day marks it done",
      status_of(forgotten) == "Completed",
      "it is %s" % status_of(forgotten))

check("but one that was cancelled stays cancelled",
      status_of(called_off) == "Cancelled",
      "somebody said no to that one on purpose, and it is now %s"
      % status_of(called_off))

check("and today's own orders are left alone",
      status_of(still_today) == "Pending",
      "an order from today was closed off: it is %s"
      % status_of(still_today))

today_board = [row["order_id"] for row in
               admin.get("/api/kitchen/board").get_json()["orders"]]
check("yesterday's order is not on today's board",
      forgotten not in today_board,
      "the board is showing %s" % today_board)
check("and today's is",
      still_today in today_board,
      "the board is showing %s" % today_board)

# Running twice must not undo anything or cost a second write.
application._ORDERS_SWEPT_FOR.clear()
admin.get("/api/kitchen/board")
check("sweeping again changes nothing",
      status_of(forgotten) == "Completed"
      and status_of(called_off) == "Cancelled"
      and status_of(still_today) == "Pending",
      "a second sweep moved something")

print("\n=== 18. The customer is told when their food is ready ===")
# Somebody at a table has no counter to watch and no staff account. The
# page they were left holding asks, and says so when the kitchen presses
# Done - which is the whole reason that button now reaches them at all.
shopper = app.test_client()
shop_menu = shopper.get("/m/%s" % fresh).get_data(as_text=True)
shop_ids = re.findall(r'name="quantity_(\d+)"', shop_menu)
sent = shopper.post("/m/%s/order" % fresh,
                    data={"quantity_%s" % shop_ids[0]: "1"},
                    follow_redirects=False)
waiting_id = int(sent.headers["Location"].rstrip("/").split("/")[-1])

asking = "/m/%s/status/%d" % (fresh, waiting_id)
nobody = app.test_client()          # never signed in, never will be

answer = nobody.get(asking)
check("a customer can ask without an account",
      answer.status_code == 200
      and (answer.get_json() or {}).get("status") == "Pending",
      "got HTTP %s: %s" % (answer.status_code,
                           answer.get_data(as_text=True)[:120]))

check("and the answer carries nothing but the status",
      sorted((answer.get_json() or {}).keys()) == ["status"],
      "it also hands over %s"
      % sorted((answer.get_json() or {}).keys()))

check("another cafe's code cannot be used to watch this order",
      nobody.get("/m/%s/status/%d" % (other_token, waiting_id))
      .status_code == 404,
      "one cafe can follow another cafe's orders")

check("and a retired code cannot either",
      nobody.get("/m/%s/status/%d" % (token, waiting_id)).status_code == 404,
      "the code that was replaced still works")

page = shopper.get("/m/%s/placed/%d" % (fresh, waiting_id)) \
              .get_data(as_text=True)
# Both wordings are in the page - the dictionary it changes itself from
# is embedded as JSON - so what matters is which one is being shown.
check("while it waits the page says it is with the kitchen",
      'data-state="pending"' in page
      and '<span id="orderStateText">In the kitchen</span>' in page,
      "the page is telling the customer the wrong thing")
check("and it asks on its own rather than waiting to be reloaded",
      asking in page,
      "the customer would have to refresh to find out")

# The kitchen presses Done.
admin.post("/orders/complete/%d" % waiting_id,
           data={"_csrf_token": csrf(admin)},
           headers={"X-Requested-With": "XMLHttpRequest"})

check("once the kitchen presses Done the answer changes",
      (nobody.get(asking).get_json() or {}).get("status") == "Completed",
      "it still says %s"
      % (nobody.get(asking).get_json() or {}).get("status"))

ready_page = shopper.get("/m/%s/placed/%d" % (fresh, waiting_id)) \
                    .get_data(as_text=True)
check("and someone opening the page fresh is told straight away",
      "Your food is ready" in ready_page,
      "a customer reloading after it is done still sees the waiting page")
check("without it still asking, now that there is nothing left to ask",
      'data-state="ready"' in ready_page,
      "the page would go on polling a finished order for ever")

# Cancelling has to reach them too, or they sit there waiting for food
# that is not coming.
cancel_menu = shopper.post("/m/%s/order" % fresh,
                           data={"quantity_%s" % shop_ids[0]: "1"},
                           follow_redirects=False)
doomed_id = int(cancel_menu.headers["Location"].rstrip("/").split("/")[-1])
admin.post("/orders/cancel/%d" % doomed_id,
           data={"_csrf_token": csrf(admin)},
           headers={"X-Requested-With": "XMLHttpRequest"})

check("a cancelled order tells the customer as well",
      (nobody.get("/m/%s/status/%d" % (fresh, doomed_id)).get_json()
       or {}).get("status") == "Cancelled",
      "they would wait for food nobody is making")

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
