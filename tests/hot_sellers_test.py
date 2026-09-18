"""
Offline tests for the "Hot Selling" shelf on the New Order screen.

The shelf lifts the best sellers of the recent window to the top of the
menu. What it must not do is mislead the till: cancelled orders went back
on the shelf, stale months should not keep an item pinned, and an item that
has run out has no business being offered first.

Run with:  python tests/hot_sellers_test.py
"""
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "hot-sellers-test-secret"
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


def build_cafe(client, cafe, username, menu):
    client.post("/register", data={
        "cafe_name": cafe, "full_name": username.title(), "username": username,
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)

    for category in sorted({c for _, c in menu}):
        client.post("/categories/add",
                    data={"category_name": category, "description": "",
                          "_csrf_token": csrf(client)}, follow_redirects=True)

    page = client.get("/foods/add").get_data(as_text=True)
    categories = {n.strip(): i for i, n in re.findall(
        r'<option value="(\d+)">\s*([^<]+?)\s*</option>', page, re.S)}

    for food, category in menu:
        client.post("/foods/add", data={
            "food_name": food, "category_id": categories[category],
            "price": "40", "quantity": "200", "description": "",
            "_csrf_token": csrf(client)}, follow_redirects=True)

    html = client.get("/orders/add").get_data(as_text=True)
    # Anchored on the card's own id. `data-food-id` appears on the card *and*
    # on its quantity input, so a looser pattern pairs one card's input with
    # the next card's name. Shelf copies use food_card_hot_<id>, which this
    # deliberately does not match.
    return {name: fid for fid, name in re.findall(
        r'id="food_card_(\d+)".*?food-card-name">([^<]+)<', html, re.S)}


def order(client, food_id, quantity=1):
    return client.post("/orders/add",
                       data={"quantity_%s" % food_id: str(quantity),
                             "_csrf_token": csrf(client)},
                       follow_redirects=True)


HOT_SECTION = '<section class="food-group food-group--hot"'


def hot_shelf(client):
    """
    Names on the Hot Selling shelf, best first ([] when absent).

    Anchored on the opening tag, not on the bare class name: the page also
    carries a `.food-group--hot` CSS rule in its <style> block, and matching
    that made an absent shelf read as the first category's contents.
    """
    html = client.get("/orders/add").get_data(as_text=True)
    start = html.find(HOT_SECTION)
    if start == -1:
        return []
    section = html[start:html.find("</section>", start)]
    return re.findall(r'food-card-name">([^<]+)<', section)


MENU = [("Cold Coffee", "Beverages"), ("Masala Chai", "Beverages"),
        ("Samosa", "Snacks"), ("Veg Puff", "Snacks"), ("Brownie", "Snacks")]


print("\n=== 1. A café with no sales yet ===")
fresh = app.test_client()
ids = build_cafe(fresh, "Fresh Cafe", "fresh", MENU)
check("no Hot Selling shelf before anything has sold",
      hot_shelf(fresh) == [], "got %s" % hot_shelf(fresh))
check("the full menu is still shown",
      len(re.findall(r'id="quantity_(\d+)"',
                     fresh.get("/orders/add").get_data(as_text=True))) == len(MENU))


print("\n=== 2. Ranked by units sold ===")
a = app.test_client()
ids = build_cafe(a, "Alpha Cafe", "alpha", MENU)
for _ in range(9):
    order(a, ids["Samosa"])
for _ in range(5):
    order(a, ids["Cold Coffee"])
for _ in range(2):
    order(a, ids["Brownie"])

check("best seller first, in descending order",
      hot_shelf(a) == ["Samosa", "Cold Coffee", "Brownie"],
      "got %s" % hot_shelf(a))

html = a.get("/orders/add").get_data(as_text=True)
check("the shelf comes before the category sections",
      re.findall(r'food-group__name">([^<]+)<', html)[0] == "Hot Selling",
      "sections: %s" % re.findall(r'food-group__name">([^<]+)<', html))
check("units sold are shown on the card",
      re.findall(r'([0-9]+) sold', html)[:3] == ["9", "5", "2"],
      "got %s" % re.findall(r'([0-9]+) sold', html)[:3])

print("\n=== 3. Shown twice, ordered once ===")
# A best seller appears on the shelf *and* under its category. Two cards for
# one food must still mean one form field, or the order would post that
# quantity twice, and one element id per card, or changeQuantity() would
# drive only the first.
below_shelf = html[html.find("</section>", html.find(HOT_SECTION)):]
check("a promoted item still appears under its own category",
      "Samosa" in re.findall(r'food-card-name">([^<]+)<', below_shelf),
      "Samosa was moved out of Snacks instead of repeated there")
check("the category sections still list the whole menu",
      set(re.findall(r'food-card-name">([^<]+)<', below_shelf)) ==
      {name for name, _ in MENU},
      "got %s" % sorted(set(re.findall(r'food-card-name">([^<]+)<', below_shelf))))

fields = re.findall(r'name="quantity_(\d+)"', html)
check("exactly one submittable field per food",
      sorted(fields) == sorted(set(fields)) and len(fields) == len(MENU),
      "fields %s - a repeated name would post the quantity twice" % fields)

element_ids = re.findall(r'id="quantity_([A-Za-z0-9_]+)"', html)
check("no element id is used twice",
      sorted(element_ids) == sorted(set(element_ids)),
      "duplicated: %s" % [i for i in set(element_ids)
                          if element_ids.count(i) > 1])

# Counted on the rendered attribute, not the bare class name: the page also
# carries that name in its stylesheet and in its own script.
mirrors = html.count('class="quantity-mirror-input"')
check("the shelf copies are marked as mirrors",
      mirrors == len(hot_shelf(a)),
      "%d mirror inputs for %d shelf cards - an unmarked copy would be "
      "counted and submitted like a real one" % (mirrors, len(hot_shelf(a))))


print("\n=== 3b. The shelf is capped at five ===")
big = app.test_client()
BIG_MENU = [("Food %02d" % i, "Beverages" if i % 2 else "Snacks")
            for i in range(9)]
big_ids = build_cafe(big, "Big Cafe", "big", BIG_MENU)
for index, (name, _) in enumerate(BIG_MENU):
    for _ in range(index + 1):
        order(big, big_ids[name])

check("at most five items on the shelf, even with nine sellers",
      len(hot_shelf(big)) == 5,
      "got %d: %s" % (len(hot_shelf(big)), hot_shelf(big)))
check("and they are the five best, best first",
      hot_shelf(big) == ["Food 08", "Food 07", "Food 06", "Food 05", "Food 04"],
      "got %s" % hot_shelf(big))
check("the cap is the configured limit",
      application.HOT_SELLER_LIMIT == 5,
      "HOT_SELLER_LIMIT is %s" % application.HOT_SELLER_LIMIT)


print("\n=== 4. Cancelled orders do not count ===")
b = app.test_client()
ids = build_cafe(b, "Beta Cafe", "beta", MENU)
for _ in range(3):
    order(b, ids["Veg Puff"])
for _ in range(8):
    order(b, ids["Brownie"])

check("Brownie leads while its orders stand",
      hot_shelf(b)[0] == "Brownie", "got %s" % hot_shelf(b))

order_ids = [str(row["order_id"])
             for row in b.get("/api/kitchen/board").get_json()["orders"]]
cancelled = 0
for order_id in order_ids:
    detail = b.get("/orders/%s" % order_id).get_data(as_text=True)
    if "Brownie" in detail:
        b.post("/orders/cancel/%s" % order_id,
               data={"_csrf_token": csrf(b)}, follow_redirects=True)
        cancelled += 1

check("some Brownie orders were actually cancelled", cancelled > 0,
      "the cancel step did nothing, so the next check proves nothing")
check("cancelled units stop counting toward hot selling",
      "Brownie" not in hot_shelf(b),
      "still listed: %s - food that went back on the shelf is being "
      "promoted as a best seller" % hot_shelf(b))
check("the item that really sold takes the lead",
      hot_shelf(b) == ["Veg Puff"], "got %s" % hot_shelf(b))


print("\n=== 5. Only the recent window counts ===")
c = app.test_client()
ids = build_cafe(c, "Gamma Cafe", "gamma", MENU)
for _ in range(6):
    order(c, ids["Masala Chai"])
check("Masala Chai is hot while the orders are fresh",
      hot_shelf(c) == ["Masala Chai"], "got %s" % hot_shelf(c))

stale = (datetime.now() - timedelta(days=application.HOT_SELLER_DAYS + 5))
mysql_shim._DB.execute(
    "UPDATE orders SET order_date = ? WHERE order_id IN "
    "(SELECT order_id FROM orders WHERE user_id = "
    " (SELECT user_id FROM users WHERE username = 'gamma'))",
    (stale.strftime("%Y-%m-%d %H:%M:%S"),))
mysql_shim._DB.commit()

check("an item that only sold months ago drops off the shelf",
      hot_shelf(c) == [],
      "got %s - the window is not being applied" % hot_shelf(c))


print("\n=== 6. Out of stock means off the shelf ===")
d = app.test_client()
ids = build_cafe(d, "Delta Cafe", "delta", MENU)
for _ in range(7):
    order(d, ids["Cold Coffee"])
check("Cold Coffee is hot while it is in stock",
      hot_shelf(d) == ["Cold Coffee"], "got %s" % hot_shelf(d))

mysql_shim._DB.execute(
    "UPDATE inventory SET quantity = 0 WHERE food_id = ?", (ids["Cold Coffee"],))
mysql_shim._DB.commit()

check("a sold-out best seller is not offered at the top",
      "Cold Coffee" not in hot_shelf(d),
      "got %s - the till would be offering food it cannot serve" % hot_shelf(d))


print("\n=== 7. One café's sales never shape another's shelf ===")
check("Alpha still leads with its own best seller",
      hot_shelf(a) == ["Samosa", "Cold Coffee", "Brownie"], "got %s" % hot_shelf(a))
check("Gamma's shelf is unaffected by Alpha's trade", hot_shelf(c) == [],
      "got %s" % hot_shelf(c))


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
