"""
Offline tests for the dashboard's stock alert.

It used to report counts only - "3 items are low" - which told a manager
something was wrong without telling them what to reorder. It now names the
food, at the top of the page, and the 5-second poll keeps those names in
step as stock moves.

Run with:  python tests/stock_alert_test.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "stock-alert-test-secret"
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


def build(client, cafe, username, menu):
    """menu: [(name, quantity, minimum_stock)]"""
    client.post("/register", data={
        "cafe_name": cafe, "full_name": username.title(), "username": username,
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)

    client.post("/categories/add",
                data={"category_name": "Coffee", "description": "",
                      "_csrf_token": csrf(client)}, follow_redirects=True)
    category = re.search(r'<option value="(\d+)">',
                         client.get("/foods/add").get_data(as_text=True)).group(1)

    for name, quantity, minimum in menu:
        client.post("/foods/add", data={
            "food_name": name, "category_id": category, "price": "100",
            "quantity": str(quantity), "description": "",
            "_csrf_token": csrf(client)}, follow_redirects=True)
        if minimum:
            mysql_shim._DB.execute(
                "UPDATE inventory SET minimum_stock = ? WHERE food_id = "
                "(SELECT food_id FROM foods WHERE food_name = ?)",
                (minimum, name))
    mysql_shim._DB.commit()


def alert_html(client):
    html = client.get("/dashboard").get_data(as_text=True)
    start = html.find('id="stock-alert-panel"')
    return html[start:html.find("</div>", html.find("stock-alert__action", start))]


def chips(client):
    return re.findall(r'class="stock-chip">([^<]+)', alert_html(client))


def stats(client):
    return json.loads(client.get("/api/dashboard-stats").get_data(as_text=True))


MENU = [("Latte", 0, 0), ("Mocha", 0, 0),
        ("Espresso", 2, 5), ("Flat White", 1, 5),
        ("Cortado", 40, 5)]

print("\n=== 1. The alert names the food ===")
a = app.test_client()
build(a, "Alpha Cafe", "alpha", MENU)

page = a.get("/dashboard").get_data(as_text=True)
check("the alert is above the statistics",
      0 < page.find('id="stock-alert-panel"') < page.find('class="stats-grid"'),
      "the alert is not the first thing on the page")

names = chips(a)
check("out-of-stock food is named", "Latte" in names and "Mocha" in names,
      "chips: %s" % names)
check("low-stock food is named",
      "Espresso" in names and "Flat White" in names, "chips: %s" % names)
check("healthy stock is not listed", "Cortado" not in names,
      "Cortado has 40 in stock but was flagged")

check("the counts are right",
      "Out of stock" in page and "Running low" in page
      and re.search(r'Out of stock\s*<strong>2</strong>', page)
      and re.search(r'Running low\s*<strong>2</strong>', page),
      "counts are not 2 and 2")

check("a low-stock chip shows how many are left",
      re.search(r'Flat White<em>1</em>', page) is not None,
      "the remaining quantity is missing from the chip")


print("\n=== 2. Zero stock counts as out, not low ===")
# A food at 0 satisfies `quantity <= minimum_stock` too, so the split has to
# be deliberate or everything out of stock would also be reported as low.
data = stats(a)
check("Latte is out, not low",
      "Latte" in [i["name"] for i in data["unavailable_items"]]
      and "Latte" not in [i["name"] for i in data["low_stock_items"]],
      "unavailable=%s low=%s"
      % ([i["name"] for i in data["unavailable_items"]],
         [i["name"] for i in data["low_stock_items"]]))
check("the two sets do not overlap",
      not (set(i["name"] for i in data["unavailable_items"]) &
           set(i["name"] for i in data["low_stock_items"])),
      "an item was counted twice")


print("\n=== 3. The poll carries the names too ===")
check("the API returns out-of-stock names",
      sorted(i["name"] for i in data["unavailable_items"]) == ["Latte", "Mocha"],
      "got %s" % [i["name"] for i in data["unavailable_items"]])
check("the API returns low-stock names with quantities",
      sorted((i["name"], i["quantity"]) for i in data["low_stock_items"])
      == [("Espresso", 2), ("Flat White", 1)],
      "got %s" % [(i["name"], i["quantity"]) for i in data["low_stock_items"]])
check("counts and names agree",
      data["unavailable"] == len(data["unavailable_items"])
      and data["low_stock"] == len(data["low_stock_items"]))


print("\n=== 4. The worst is listed first ===")
check("low stock is ordered by how little is left",
      [i["name"] for i in data["low_stock_items"]] == ["Flat White", "Espresso"],
      "got %s - the most urgent should lead"
      % [i["name"] for i in data["low_stock_items"]])


print("\n=== 5. A long list is capped, and says so ===")
b = app.test_client()
big = [("Item %02d" % i, 0, 0) for i in range(application.STOCK_ALERT_NAMES + 4)]
build(b, "Beta Cafe", "beta", big)

data = stats(b)
check("every missing item is counted",
      data["unavailable"] == len(big),
      "counted %d of %d" % (data["unavailable"], len(big)))
check("only a readable number are named",
      len(data["unavailable_items"]) == application.STOCK_ALERT_NAMES,
      "named %d" % len(data["unavailable_items"]))
check("the page says how many more there are",
      "and 4 more" in b.get("/dashboard").get_data(as_text=True),
      "the overflow is not acknowledged")


print("\n=== 6. Nothing wrong, nothing shown ===")
c = app.test_client()
build(c, "Gamma Cafe", "gamma", [("Cortado", 40, 5), ("Cold Brew", 30, 5)])
page = c.get("/dashboard").get_data(as_text=True)
check("the alert is hidden when stock is healthy",
      re.search(r'id="stock-alert-panel"[^>]*\shidden', page) is not None,
      "the panel is showing with nothing to report")
check("and the API agrees",
      stats(c)["low_stock"] == 0 and stats(c)["unavailable"] == 0)


print("\n=== 7. One café's stock is not another's problem ===")
check("Gamma is not told about Alpha's empty shelves",
      "Latte" not in c.get("/dashboard").get_data(as_text=True),
      "stock alerts leak between tenants")
check("Alpha still sees its own", "Latte" in chips(a))


print("\n=== 8. The Today card names the real day ===")
# It used to carry Bootstrap's calendar-day icon, which has the word
# "Fri" drawn into the font. It read Friday on a Tuesday, for everyone,
# for ever.
import datetime  # noqa: E402

html = a.get("/dashboard").get_data(as_text=True)
shown = re.search(
    r'id="today-weekday"[^>]*title="([^"]+)">([^<]+)<', html)

check("the card carries a day at all", shown is not None,
      "no day is rendered on the Today card")

if shown:
    full, short = shown.group(1), shown.group(2).strip()
    today = datetime.datetime.now()
    check("and it is today's", short == today.strftime("%a"),
          "it reads %r on a %s" % (short, today.strftime("%a")))
    check("with the whole name to hover", full == today.strftime("%A"),
          "the tooltip reads %r" % full)

check("the icon with Fri baked into it is gone",
      "bi-calendar-day" not in html,
      "the fixed-Friday icon is still on the dashboard")

# A till left running overnight refreshes its figures without anyone
# reloading, so the day has to come with them.
feed = a.get("/api/dashboard-stats")
check("the refresh carries the day too", feed.status_code == 200
      and json.loads(feed.get_data(as_text=True)).get("weekday", {})
      .get("short") == datetime.datetime.now().strftime("%a"),
      "a screen left open past midnight would keep yesterday's name")


# =====================================================================
print("\n=== What needs ordering is at the top of Inventory ===")
# =====================================================================
# A cafe opens this page to find out what to reorder. Listed by menu
# number alone, that meant reading forty rows to find the two that
# mattered - so anything short comes to the top, worst first, and drops
# back to its own place the moment it is restocked.

shelf = app.test_client()
shelf.post("/register", data={
    "cafe_name": "Shelf Cafe", "full_name": "Owner", "username": "shelfboss",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
shelf.post("/categories/add", data={"category_name": "All", "description": "",
                                    "_csrf_token": csrf(shelf)},
           follow_redirects=True)
shelf_cat = re.search(r'<option value="(\d+)">',
                      shelf.get("/foods/add").get_data(as_text=True)).group(1)

# Numbered 1..6 in the order they are added. Two of them are short.
for _name, _qty in (("Americano", 40), ("Bagel", 0), ("Cortado", 30),
                    ("Donut", 25), ("Eclair", 3), ("Focaccia", 50)):
    shelf.post("/foods/add", data={
        "food_name": _name, "category_id": shelf_cat, "price": "90",
        "quantity": str(_qty), "minimum_stock": "5", "description": "",
        "_csrf_token": csrf(shelf)}, follow_redirects=True)


def shelf_order():
    """The food names down the Inventory page, in the order shown."""
    page = shelf.get("/inventory").get_data(as_text=True)
    body = re.search(r"<tbody>(.*?)</tbody>", page, re.S)
    if not body:
        return []
    names = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) >= 2:
            names.append(re.sub(r"<[^>]+>", "", cells[1]).strip())
    return names


listed = shelf_order()

check("every food is on the page",
      len(listed) == 6,
      "only %d rows, so the order below proves nothing" % len(listed))

check("nothing left comes first",
      listed[0] == "Bagel",
      "the list starts %s - the one with no stock at all is what "
      "somebody opened this page for" % listed[:3])

check("then what is running low",
      listed[1] == "Eclair",
      "the list runs %s" % listed[:3])

check("and the rest keep their own order",
      listed[2:] == ["Americano", "Cortado", "Donut", "Focaccia"],
      "the rest run %s, which is not menu-number order" % listed[2:])

# The other half: restocking must put a food back where it belongs,
# not leave it stranded at the top or send it to the end.
_rows = mysql_shim._DB.execute(
    "SELECT i.inventory_id, f.food_name FROM inventory i "
    "JOIN foods f ON f.food_id = i.food_id "
    "WHERE f.user_id = (SELECT user_id FROM users "
    "WHERE username = 'shelfboss')").fetchall()
_by_name = {name: inv for inv, name in _rows}

for _name in ("Bagel", "Eclair"):
    shelf.post("/inventory/update/%s" % _by_name[_name],
               data={"quantity": "60", "minimum_stock": "5",
                     "_csrf_token": csrf(shelf)}, follow_redirects=True)

restocked = shelf_order()
check("a restocked food goes back to its own position",
      restocked == ["Americano", "Bagel", "Cortado", "Donut",
                    "Eclair", "Focaccia"],
      "after restocking the list runs %s" % restocked)

# And the badge a row carries has to agree with where it was sorted.
shelf.post("/inventory/update/%s" % _by_name["Focaccia"],
           data={"quantity": "2", "minimum_stock": "5",
                 "_csrf_token": csrf(shelf)}, follow_redirects=True)

page = shelf.get("/inventory").get_data(as_text=True)
first_row = re.search(r"<tbody>\s*<tr[^>]*>(.*?)</tr>", page, re.S)
check("a row sorted to the top is badged as short",
      first_row is not None
      and "Focaccia" in first_row.group(1)
      and "LOW STOCK" in first_row.group(1),
      "the food at the top is not the one the page calls low, so the "
      "sort and the badge disagree about what is short")

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
