"""
An order found again on another phone or browser.

A phone knows the orders it placed by a cookie. Clear it, or scan the
table's code with a different browser than the one the order was sent
from - a camera app's own browser, then Chrome - and the bell is empty,
though the order is still in the kitchen. So every order carries a
four-digit code beside its number, and the two together bring it back.

What has to hold:

  * the code is on the order's page and on the phone's list, and is the
    same every time the order is shown;
  * number and code together find it, from a phone that never had it,
    and put it on that phone's list;
  * a wrong code finds nothing, and says so with the bell open;
  * guessing is slow: five wrong from one address shuts that address out
    for a while - not the whole cafe, and not the next table;
  * only this cafe's orders, only today's, only ones sent from a table.

Run with:  python tests/find_order_test.py
"""
import datetime as _dt
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "find-order-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import app as application     # noqa: E402

app = application.app
app.config["TESTING"] = True

date, timedelta = _dt.date, _dt.timedelta

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def build_cafe(cafe, username):
    """A cafe with two dishes, and the token its table code points at."""
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
    for name in ("Cortado", "Masala Chai"):
        client.post("/foods/add", data={
            "food_name": name, "category_id": category, "price": "120",
            "quantity": "200", "minimum_stock": "1", "description": "",
            "diet": "veg", "_csrf_token": csrf(client)}, follow_redirects=True)
    with client.session_transaction() as sess:
        cafe_id = sess.get("cafe_id")
    return cafe_id, application.get_public_token(cafe_id)


CAFE, TOKEN = build_cafe("The Long Table", "tess")
OTHER_CAFE, OTHER_TOKEN = build_cafe("Across The Road", "rhea")


def phone(address="10.0.0.1"):
    """A phone at a table: its own cookies, its own address."""
    client = app.test_client()
    client.environ_base["REMOTE_ADDR"] = address
    return client


def dish_on(token):
    menu = phone().get("/m/%s" % token).get_data(as_text=True)
    return re.findall(r'name="quantity_(\d+)"', menu)[0]


DISH = dish_on(TOKEN)
OTHER_DISH = dish_on(OTHER_TOKEN)


def order_from(client, token=TOKEN, dish=None):
    """One order from this phone, and the ref it was given."""
    reply = client.post("/m/%s/order" % token,
                        data={"quantity_%s" % (dish or DISH): "1"})
    return reply.headers.get("Location", "").rsplit("/", 1)[-1]


def number_of(ref):
    row = mysql_shim._DB.execute(
        "SELECT daily_no FROM orders WHERE public_ref = ?", (ref,)).fetchone()
    return str(row[0])


def find(client, number, code, token=TOKEN):
    return client.post("/m/%s/find" % token,
                       data={"number": number, "code": code})


def where(reply):
    return reply.headers.get("Location", "")


def bell_numbers(client, token=TOKEN):
    menu = client.get("/m/%s" % token).get_data(as_text=True)
    return re.findall(r'p-bell__no">#(\d+)<', menu)


def wrong(code):
    """A code that is not this one."""
    return "%04d" % ((int(code) + 1) % 10000)


# =====================================================================
print("\n=== 1. Every order has a code beside its number ===")
# =================================================================
first_phone = phone("10.0.0.1")
ref = order_from(first_phone)
code = application.order_code(ref)
page = first_phone.get("/m/%s/placed/%s" % (TOKEN, ref)).get_data(as_text=True)

check("the order's page shows it", 'id="orderCode">%s<' % code in page,
      "the page does not carry the code %s" % code)
check("four digits", re.fullmatch(r"\d{4}", code) is not None, "the code is %r" % code)
check("and says what it is for", "another phone or browser" in page,
      "nothing on the page says why the code is there")
check("the same code every time the order is shown",
      application.order_code(ref) == code
      and 'id="orderCode">%s<' % code in first_phone.get(
          "/m/%s/placed/%s" % (TOKEN, ref)).get_data(as_text=True),
      "the code changed between two looks at the same order")

listing = first_phone.get("/m/%s/orders" % TOKEN).get_data(as_text=True)
check("the phone's list shows it too", "code %s" % code in listing,
      "Your orders does not show the code")

refs = [ref] + [order_from(first_phone) for _ in range(4)]
codes = [application.order_code(r) for r in refs]
check("orders have codes of their own, not one code for everybody",
      len(set(codes)) > 1, "five orders share the code %s" % codes[0])

check("the code is not the order number, or anything counted from it",
      codes != sorted(codes) or len(set(codes)) == len(codes),
      "the codes run in order: %s" % codes)

# =====================================================================
print("\n=== 2. Number and code find it on a phone that never had it ===")
# =================================================================
new_browser = phone("10.0.0.2")
check("a new browser's bell is empty", bell_numbers(new_browser) == [],
      "it lists %s" % bell_numbers(new_browser))

menu = new_browser.get("/m/%s" % TOKEN).get_data(as_text=True)
check("the bell offers to find an order",
      'action="/m/%s/find"' % TOKEN in menu and "another phone or browser" in menu,
      "there is no search under the bell")
check("folded away until it is asked for",
      re.search(r'id="orderBellPanel"[^>]*hidden', menu) is not None
      and re.search(r'<details class="p-refind" id="orderFind"\s*>', menu) is not None,
      "the bell opens on its own")

reply = find(new_browser, number_of(ref), code)
check("the right number and code go straight to the order",
      where(reply).endswith("/m/%s/placed/%s" % (TOKEN, ref)),
      "sent to %r" % where(reply))
cookie = reply.headers.get("Set-Cookie", "")
check("which this phone now holds", ref in cookie and "HttpOnly" in cookie
      and "Path=/m/%s" % TOKEN in cookie, "the cookie is %r" % cookie[:160])
check("and its bell lists it", bell_numbers(new_browser) == [number_of(ref)],
      "the bell lists %s" % bell_numbers(new_browser))
check("without the other four the first phone sent",
      len(bell_numbers(new_browser)) == 1,
      "a code for one order brought back %s" % bell_numbers(new_browser))

hashed = phone("10.0.0.3")
check("a number typed with its # works as well",
      where(find(hashed, "#" + number_of(refs[1]), codes[1])).endswith(refs[1]),
      "#%s was not understood" % number_of(refs[1]))

check("nobody has to sign in for any of it",
      "/login" not in where(find(phone("10.0.0.4"), number_of(ref), code)),
      "the search asks for a sign-in")

# =====================================================================
print("\n=== 3. A wrong code finds nothing, and says so ===")
# =================================================================
guesser = phone("10.0.0.5")
reply = find(guesser, number_of(ref), wrong(code))
check("back to the menu, with the bell asked to open",
      where(reply).endswith("/m/%s?find=missed" % TOKEN), "sent to %r" % where(reply))
check("and nothing given to hold", ref not in reply.headers.get("Set-Cookie", ""),
      "a wrong code was handed the order")
check("the bell still empty", bell_numbers(guesser) == [],
      "it lists %s" % bell_numbers(guesser))

told = guesser.get("/m/%s?find=missed" % TOKEN).get_data(as_text=True)
check("the bell is open on the search",
      re.search(r'id="orderBellPanel"[^>]*hidden', told) is None
      and re.search(r'<details class="p-refind" id="orderFind"\s+open', told) is not None,
      "the customer has to find the message for themselves")
check("which says what went wrong, kindly",
      "could not find an order with that number and code" in told,
      "no word of why")
check("an unknown reason is not echoed onto the page",
      "p-refind__said" not in guesser.get("/m/%s?find=<b>hi</b>" % TOKEN)
      .get_data(as_text=True), "the address wrote onto the page")

reply = find(guesser, number_of(ref), "")
check("a half-filled form is asked again, not counted",
      where(reply).endswith("?find=blank"), "sent to %r" % where(reply))

# =====================================================================
print("\n=== 4. Guessing is slow ===")
# =================================================================
blanks = phone("10.0.0.6")
for _ in range(8):
    find(blanks, number_of(ref), "")
check("eight empty forms cost nothing",
      where(find(blanks, number_of(ref), code)).endswith(ref),
      "empty forms were counted as wrong guesses")

pest = phone("10.0.0.7")
for n in range(5):
    find(pest, number_of(ref), wrong(code))
reply = find(pest, number_of(ref), code)
check("after five wrong, even the right code waits",
      where(reply).endswith("?find=wait"), "sent to %r" % where(reply))
check("and is told to try later, or ask at the counter",
      "try again in a few minutes" in pest.get("/m/%s?find=wait" % TOKEN)
      .get_data(as_text=True), "no word of waiting")
check("the next table is not shut out with them",
      where(find(phone("10.0.0.8"), number_of(ref), code)).endswith(ref),
      "one address's guesses locked everybody out")

careful = phone("10.0.0.9")
for _ in range(4):
    find(careful, number_of(ref), wrong(code))
find(careful, number_of(ref), code)
for _ in range(4):
    find(careful, number_of(ref), wrong(code))
check("a right answer wipes the slate",
      where(find(careful, number_of(ref), code)).endswith(ref),
      "four and four wrong, with a right one between, locked the phone out")

# =====================================================================
print("\n=== 5. Only this cafe's, only today's, only from a table ===")
# =================================================================
theirs = order_from(phone("10.0.1.1"), OTHER_TOKEN, OTHER_DISH)
theirs_code = application.order_code(theirs)
ours_same_no = mysql_shim._DB.execute(
    "SELECT public_ref FROM orders WHERE user_id = "
    "(SELECT owner_user_id FROM cafes WHERE cafe_id = ?) AND daily_no = ?",
    (CAFE, int(number_of(theirs)))).fetchone()
check("(the two cafes' orders of that number have different codes)",
      ours_same_no is not None
      and application.order_code(ours_same_no[0]) != theirs_code,
      "the test cannot tell the cafes apart")
check("another cafe's number and code find nothing here",
      where(find(phone("10.0.1.2"), number_of(theirs), theirs_code))
      .endswith("?find=missed"), "one cafe's order was found through another's code")
check("but do at their own cafe",
      where(find(phone("10.0.1.3"), number_of(theirs), theirs_code, OTHER_TOKEN))
      .endswith(theirs), "the other cafe's own search failed")

old = order_from(phone("10.0.2.1"))
mysql_shim._DB.execute(
    "UPDATE orders SET order_day = ? WHERE public_ref = ?",
    ((date.today() - timedelta(days=1)).isoformat(), old))
mysql_shim._DB.commit()
check("yesterday's order is not found today",
      where(find(phone("10.0.2.2"), number_of(old), application.order_code(old)))
      .endswith("?find=missed"), "an order from another day was found")

counter = order_from(phone("10.0.3.1"))
mysql_shim._DB.execute(
    "UPDATE orders SET source = 'counter' WHERE public_ref = ?", (counter,))
mysql_shim._DB.commit()
check("an order rung up at the counter is not a phone's to find",
      where(find(phone("10.0.3.2"), number_of(counter),
                 application.order_code(counter))).endswith("?find=missed"),
      "a counter order was handed to a phone")

check("a code that is not a cafe's answers as a missing cafe",
      find(phone("10.0.4.1"), "1", "1234", "notarealtoken").status_code == 404,
      "an unknown token did not 404")

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
