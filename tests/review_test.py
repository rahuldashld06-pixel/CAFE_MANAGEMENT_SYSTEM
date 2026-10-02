"""
Customer reviews from the table QR.

Once the kitchen has finished a table-QR order, the customer's phone asks
for stars on each dish and on the visit, with a comment and their name if
they want. The
cafe sees them as a chart on the dashboard and on a Reviews page that
everyone who serves customers can open.

What matters, and is checked here: a review can only be left once the
food is ready, only on that order, only by whoever holds its link, only
with stars from one to five; sending again replaces what was said rather
than counting it twice; and one cafe never sees another's reviews.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/review_test.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "review-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.disable(logging.CRITICAL)

import app as application               # noqa: E402

app = application.app
app.config["TESTING"] = True

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % (detail,)))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def db(sql, params=()):
    rows = mysql_shim._DB.execute(sql, params).fetchall()
    mysql_shim._DB.commit()
    return [tuple(row) for row in rows]


def text(response):
    return response.get_data(as_text=True)


XHR = {"X-Requested-With": "XMLHttpRequest"}


def open_cafe(name, username, dishes):
    client = app.test_client()
    client.post("/register", data={
        "cafe_name": name, "full_name": name + " Owner", "username": username,
        "phone_number": "", "password": "Brew-Latte-42",
        "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
    client.post("/categories/add", data={"category_name": "Menu", "description": "",
                                         "_csrf_token": csrf(client)})
    category = re.search(r'<option value="(\d+)">',
                         text(client.get("/foods/add"))).group(1)
    for dish in dishes:
        client.post("/foods/add", data={
            "food_name": dish, "category_id": category, "price": "100",
            "quantity": "100", "minimum_stock": "1", "description": "",
            "_csrf_token": csrf(client)})
    with client.session_transaction() as sess:
        cafe_id = sess["cafe_id"]
    ids = {}
    for dish in dishes:
        ids[dish] = db("SELECT food_id FROM foods WHERE food_name = ?", (dish,))[0][0]
    return client, application.get_public_token(cafe_id), ids


def qr_order(token, quantities):
    guest = app.test_client()
    guest.post("/m/%s/order" % token,
               data={"quantity_%d" % food: str(n) for food, n in quantities.items()})
    ref, order_id = db("SELECT public_ref, order_id FROM orders WHERE source = 'qr' "
                       "ORDER BY order_id DESC")[0]
    lines = dict((name, line) for line, name in db(
        "SELECT order_item_id, item_name FROM order_items WHERE order_id = ?",
        (order_id,)))
    return guest, ref, order_id, lines


def finish(owner, order_id):
    owner.post("/orders/complete/%d" % order_id, data={"_csrf_token": csrf(owner)})


def review(guest, token, ref, data, json=True):
    response = guest.post("/m/%s/review/%s" % (token, ref), data=data,
                          headers=XHR if json else {})
    return response


def rows_for(order_id):
    return db("SELECT order_item_id, item_name, rating, comment FROM reviews "
              "WHERE order_id = ? ORDER BY order_item_id IS NULL, order_item_id",
              (order_id,))


# =====================================================================
print("\n=== 0. Two cafes, each with a table QR ===")
# =====================================================================
owner, TOKEN, FOOD = open_cafe("Star Cafe", "stella", ["Masala Dosa", "Filter Coffee"])
other, OTHER_TOKEN, OTHER_FOOD = open_cafe("Far Cafe", "farah", ["Croissant"])
guest, REF, ORDER, LINES = qr_order(TOKEN, {FOOD["Masala Dosa"]: 1,
                                            FOOD["Filter Coffee"]: 2})
DOSA, COFFEE = LINES["Masala Dosa"], LINES["Filter Coffee"]
print("  seeded")


# =====================================================================
print("\n=== 1. Nothing to rate before the food is ready ===")
# =====================================================================
placed = text(guest.get("/m/%s/placed/%s" % (TOKEN, REF)))
check("the order page carries the review panel, hidden",
      re.search(r'id="reviewPanel"[^>]*\s+hidden', placed) is not None)
early = review(guest, TOKEN, REF, {"rating_%d" % DOSA: "5"})
check("a review sent early is turned away",
      early.status_code == 409 and early.get_json()["ok"] is False, early.get_json())
check("and nothing is kept", rows_for(ORDER) == [])

finish(owner, ORDER)
placed = text(guest.get("/m/%s/placed/%s" % (TOKEN, REF)))
check("once it is ready the panel is shown",
      re.search(r'id="reviewPanel"[^>]*\s+hidden', placed) is None
      and "How was it?" in placed)
check("asking about each dish on the order",
      'name="rating_%d"' % DOSA in placed and 'name="rating_%d"' % COFFEE in placed)


# =====================================================================
print("\n=== 2. A review ===")
# =====================================================================
sent = review(guest, TOKEN, REF, {
    "rating_%d" % DOSA: "5", "rating_%d" % COFFEE: "2",
    "note_%d" % COFFEE: "  Too bitter  ", "overall": "4",
    "comment": "Lovely staff, slow coffee."})
check("is thanked", sent.status_code == 200 and sent.get_json()["ok"] is True,
      sent.get_json())
check("each dish keeps its stars and its note, the visit its own",
      rows_for(ORDER) == [(DOSA, "Masala Dosa", 5, None),
                          (COFFEE, "Filter Coffee", 2, "Too bitter"),
                          (None, None, 4, "Lovely staff, slow coffee.")],
      rows_for(ORDER))
placed = text(guest.get("/m/%s/placed/%s" % (TOKEN, REF)))
check("coming back shows what they said, to change",
      "Your review" in placed and "Update my review" in placed
      and "Lovely staff, slow coffee." in placed)

again = review(guest, TOKEN, REF, {"rating_%d" % COFFEE: "3"})
check("sending again replaces it - it is not counted twice",
      again.get_json()["ok"] is True
      and rows_for(ORDER) == [(COFFEE, "Filter Coffee", 3, None)], rows_for(ORDER))

form_post = review(guest, TOKEN, REF, {"rating_%d" % DOSA: "4", "overall": "5",
                                       "comment": "Better"}, json=False)
check("without script it goes back to the order page",
      form_post.status_code == 302
      and form_post.headers["Location"].endswith("/placed/%s#reviewed" % REF),
      form_post.headers.get("Location"))


# =====================================================================
print("\n=== 3. What is refused ===")
# =====================================================================
kept = rows_for(ORDER)
for label, data in (("no stars and no words", {}),
                    ("six stars", {"rating_%d" % DOSA: "6"}),
                    ("no stars", {"rating_%d" % DOSA: "0"}),
                    ("stars that are not a number", {"overall": "five"})):
    response = review(guest, TOKEN, REF, data)
    check("%s is turned away, and what was said stands" % label,
          response.status_code == 400 and rows_for(ORDER) == kept,
          (response.status_code, rows_for(ORDER)))

long_words = review(guest, TOKEN, REF, {"overall": "3", "comment": "x" * 5000,
                                        "rating_%d" % DOSA: "3",
                                        "note_%d" % DOSA: "y" * 5000})
lengths = db("SELECT LENGTH(comment) FROM reviews WHERE order_id = ? "
             "ORDER BY order_item_id IS NULL", (ORDER,))
check("a very long comment is cut, not refused or stored whole",
      long_words.get_json()["ok"] and lengths == [(300,), (600,)], lengths)

stranger_line = review(guest, TOKEN, REF, {"rating_999999": "1", "overall": "4"})
check("stars for a dish not on the order are ignored",
      stranger_line.get_json()["ok"]
      and [r[0] for r in rows_for(ORDER)] == [None], rows_for(ORDER))

wrong_ref = review(guest, TOKEN, "not-a-real-ref", {"overall": "1"})
check("an order reference that does not exist is not found",
      wrong_ref.status_code == 404)
wrong_menu = review(guest, "no-such-menu", REF, {"overall": "1"})
check("nor is a menu that does not exist", wrong_menu.status_code == 404)
crossed = review(guest, OTHER_TOKEN, REF, {"overall": "1"})
check("and another cafe's menu cannot reach this order",
      crossed.status_code == 404 and rows_for(ORDER)[-1][2] == 4, rows_for(ORDER))

owner.post("/orders/add", data={"quantity_%d" % FOOD["Masala Dosa"]: "1",
                                "_csrf_token": csrf(owner)})
counter_ref, counter_id = db("SELECT public_ref, order_id FROM orders "
                             "WHERE source = 'counter' ORDER BY order_id DESC")[0]
finish(owner, counter_id)
if not counter_ref:
    # Give it a reference as a QR order would have, so that only its
    # being a counter order can be what turns the review away.
    counter_ref = "counterRef0000001"
    db("UPDATE orders SET public_ref = ? WHERE order_id = ?", (counter_ref, counter_id))
counter = review(guest, TOKEN, counter_ref, {"overall": "5"})
check("an order taken at the counter cannot be reviewed from a QR link",
      counter.status_code == 404 and rows_for(counter_id) == [])


# =====================================================================
print("\n=== 4. The cafe sees them ===")
# =====================================================================
g2, ref2, order2, lines2 = qr_order(TOKEN, {FOOD["Masala Dosa"]: 1})
finish(owner, order2)
review(g2, TOKEN, ref2, {"rating_%d" % lines2["Masala Dosa"]: "2",
                         "note_%d" % lines2["Masala Dosa"]: "Cold",
                         "overall": "2", "comment": "Waited too long"})
review(guest, TOKEN, REF, {"rating_%d" % DOSA: "5", "rating_%d" % COFFEE: "4",
                           "overall": "5", "comment": "Perfect breakfast",
                           "reviewer_name": "  Priya \t  Sharma \x07 "})
names = db("SELECT DISTINCT reviewer_name FROM reviews WHERE order_id = ?", (ORDER,))
check("the name they typed is kept, tidied, on every row of the review",
      names == [("Priya Sharma",)], names)
check("a review left without a name has none",
      db("SELECT DISTINCT reviewer_name FROM reviews WHERE order_id = ?", (order2,))
      == [(None,)])
placed = text(guest.get("/m/%s/placed/%s" % (TOKEN, REF)))
check("their page shows the name back to them",
      'name="reviewer_name"' in placed and 'value="Priya Sharma"' in placed)
check("and keeps it on the phone for next time",
      '"cafora.reviewer"' in placed)
long_name = review(guest, TOKEN, REF, {"overall": "5", "comment": "Perfect breakfast",
                                       "rating_%d" % DOSA: "5", "rating_%d" % COFFEE: "4",
                                       "reviewer_name": "N" * 500})
check("a name is cut at 60 characters",
      db("SELECT DISTINCT LENGTH(reviewer_name) FROM reviews WHERE order_id = ?",
         (ORDER,)) == [(60,)])
review(guest, TOKEN, REF, {"rating_%d" % DOSA: "5", "rating_%d" % COFFEE: "4",
                           "overall": "5", "comment": "Perfect breakfast",
                           "reviewer_name": "<b>Priya</b> Sharma"})
# A table order is billed when the counter next opens Billing.
owner.get("/billing")
BILL = db("SELECT bill_id FROM bills WHERE order_id = ?", (ORDER,))

page = text(owner.get("/reviews"))
check("the Reviews page opens", "What they said" in page)
check("with how many visits were rated", re.search(
    r'Visits rated</span>.*?kpi__value">2<', page, re.S) is not None)
check("the visit's average: (5 + 2) / 2",
      re.search(r'The visit, on average</span>.*?kpi__value">3\.5', page, re.S)
      is not None)
check("the dishes' average: (5 + 4 + 2) / 3",
      re.search(r'Dishes, on average</span>.*?kpi__value">3\.7', page, re.S)
      is not None)
check("each comment is there", "Perfect breakfast" in page
      and "Waited too long" in page and "Cold" in page)
check("with who said it, written out as text and not as markup",
      "&lt;b&gt;Priya&lt;/b&gt; Sharma" in page and "<b>Priya</b>" not in page)
check("and 'A customer' where no name was given", "A customer" in page)
check("with the bill it was on",
      BILL and ("Bill #%d" % BILL[0][0]) in page, BILL)
dishes = application.dish_ratings
with app.test_request_context():
    connection = application.get_db_connection()
    cursor = connection.cursor(dictionary=True)
    owner_id = db("SELECT user_id FROM users WHERE username = 'stella'")[0][0]
    ratings = dishes(cursor, owner_id)
    cursor.close()
    connection.close()
check("dishes rank highest first, with their own averages",
      [(d["name"], d["average"], d["votes"]) for d in ratings]
      == [("Filter Coffee", 4.0, 1), ("Masala Dosa", 3.5, 2)], ratings)
check("and a picture to go with each", all(d["kind"] for d in ratings))

dash = text(owner.get("/dashboard"))
rows = re.findall(r'class="dish-chart__row(?: ([^"]*))?".*?dish-chart__name">([^<]+)<'
                  r'.*?width: (\d+)%.*?</span>([\d.]+)<', dash, re.S)
check("the dashboard charts the dishes' ratings, best at the top",
      [(name.strip(), width, score) for _, name, width, score in rows]
      == [("Filter Coffee", "80", "4.0"), ("Masala Dosa", "70", "3.5")], rows)
check("each bar coloured by how it is rated: 4 is good, 3.5 okay",
      [band.strip() for band, _, _, _ in rows] == ["", "is-mid"], rows)
check("on a scale from none to five stars",
      "dish-chart__ticks" in dash and "5&#9733;" in dash)
check("naming the best dish",
      re.search(r"Best rated</span>.*?<b>Filter Coffee</b>", dash, re.S) is not None)
check("and the lowest", re.search(
    r"Lowest rated</span>.*?<b>Masala Dosa</b>", dash, re.S) is not None)
check("just the chart: what was written is on the Reviews page",
      "Perfect breakfast" not in dash and "Waited too long" not in dash)
check("the sidebar leads to the Reviews page",
      'href="/reviews"' in dash)

check("another cafe sees none of it",
      "Perfect breakfast" not in text(other.get("/reviews"))
      and "No reviews yet" in text(other.get("/reviews")))

owner.post("/users/add", data={
    "full_name": "Sam Staff", "username": "sam_staff", "role": "staff",
    "phone_number": "", "password": "Brew-Latte-42", "_csrf_token": csrf(owner)})
staff = app.test_client()
staff.post("/login", data={"username": "sam_staff", "password": "Brew-Latte-42"})
staff_page = staff.get("/reviews")
check("the staff who serve customers can read them too",
      staff_page.status_code == 200 and "Perfect breakfast" in text(staff_page))
check("the page is not open to someone signed out",
      app.test_client().get("/reviews").status_code == 302)


# =====================================================================
print("\n=== 5. A deleted order takes its review with it ===")
# =====================================================================
mysql_shim._DB.execute("PRAGMA foreign_keys = ON")
db("DELETE FROM orders WHERE order_id = ?", (order2,))
check("no review is left pointing at nothing",
      db("SELECT COUNT(*) FROM reviews WHERE order_id = ?", (order2,)) == [(0,)])


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
