"""
Offline tests for the printable bill and kitchen ticket.

Two documents for one order: a receipt carrying the café's name, logo and
every line, and a kitchen ticket carrying the order number, the dishes and
how many - and deliberately no prices.

Both are counter work, so they are open to staff as well as admins, and
both must stop at the café boundary.

Run with:  python tests/print_test.py
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "print-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import app as application     # noqa: E402

app = application.app
app.config["TESTING"] = True

PASSED, FAILED = [], []

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def build(client, cafe, username, menu, price="120"):
    client.post("/register", data={
        "cafe_name": cafe, "full_name": username.title(), "username": username,
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)
    client.post("/categories/add",
                data={"category_name": "Coffee", "description": "",
                      "_csrf_token": csrf(client)}, follow_redirects=True)
    category = re.search(r'<option value="(\d+)">',
                         client.get("/foods/add").get_data(as_text=True)).group(1)
    for name in menu:
        client.post("/foods/add", data={
            "food_name": name, "category_id": category, "price": price,
            "quantity": "500", "description": "",
            "_csrf_token": csrf(client)}, follow_redirects=True)
    return re.findall(r'id="quantity_(\d+)"',
                      client.get("/orders/add").get_data(as_text=True))


def place(client, pairs):
    """pairs: {food_id: quantity}. Returns the new order id."""
    data = {"quantity_%s" % fid: str(q) for fid, q in pairs.items()}
    data["_csrf_token"] = csrf(client)
    client.post("/orders/add", data=data, follow_redirects=True)
    board = client.get("/api/kitchen/board").get_json()["orders"]
    return max(row["order_id"] for row in board)


print("\n=== 1. The bill carries the café and the order ===")
a = app.test_client()
foods = build(a, "Bean Scene", "bean", ["Latte", "Mocha"])
order_id = place(a, {foods[0]: 2, foods[1]: 1})
a.get("/billing")          # raises the bill

bill = a.get("/orders/%s/bill" % order_id)
html = bill.get_data(as_text=True)

check("the bill renders", bill.status_code == 200, "status=%d" % bill.status_code)
check("it names the café", "Bean Scene" in html)
check("it shows the order number", "#%s" % order_id in html)
check("it lists every item",
      "Latte" in html and "Mocha" in html,
      "items missing from the receipt")
check("it shows quantities and rates",
      re.search(r'col-qty">\s*2\s*<', html) is not None
      and "120.00" in html,
      "line detail is missing")

totals = dict(re.findall(
    r'<span>(Subtotal|Tax|Total)[^<]*</span>\s*<span>[^\d-]*([\d.]+)</span>',
    html))
check("subtotal, tax and total are all printed",
      totals.get("Subtotal") == "360.00" and totals.get("Tax") == "18.00"
      and totals.get("Total") == "378.00",
      "got %s" % totals)
check("it does not drag in the app chrome",
      "sidebar" not in html and "page-view" not in html,
      "the receipt is rendering inside the app shell")


print("\n=== 2. The receipt names the cafe, with no empty logo frame ===")
# Logos are left at the platform default now that there is no page to
# upload one from, so the receipt must not print a frame where a logo
# would have gone.
check("no empty logo box is printed",
      "receipt__logo" not in html, "an empty logo box is being printed")
check("the cafe is still named on the receipt",
      "Bean Scene" in html, "the receipt does not name the cafe")

print("\n=== 3. The kitchen ticket is the kitchen's ===")
kot = a.get("/orders/%s/kot" % order_id)
ticket = kot.get_data(as_text=True)

check("the ticket renders", kot.status_code == 200, "status=%d" % kot.status_code)
check("it leads with the order number",
      re.search(r'kot__order">#%s<' % order_id, ticket) is not None,
      "the order number is not the headline")
check("it lists the dishes", "Latte" in ticket and "Mocha" in ticket)
check("with how many of each",
      re.findall(r'kot__qty">(\d+)<', ticket) == ["2", "1"],
      "quantities: %s" % re.findall(r'kot__qty">(\d+)<', ticket))

# The whole point of a separate ticket: the kitchen sees no money.
check("it carries no prices",
      "120.00" not in ticket and "378.00" not in ticket and "₹" not in ticket,
      "money leaked onto the kitchen ticket")
check("and no totals section",
      "receipt__totals" not in ticket and "Subtotal" not in ticket)


print("\n=== 4. Staff can print, not just admins ===")
a.post("/users/add", data={
    "full_name": "Till One", "username": "till1", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(a)}, follow_redirects=True)

staff = app.test_client()
staff.post("/login", data={"username": "till1", "password": "password123"},
           follow_redirects=True)

check("a cashier can print the bill",
      staff.get("/orders/%s/bill" % order_id).status_code == 200,
      "taking payment is not an admin-only job")
check("a cashier can print the kitchen ticket",
      staff.get("/orders/%s/kot" % order_id).status_code == 200)


print("\n=== 5. Not signed in, nothing printed ===")
stranger = app.test_client()
for path in ["bill", "kot"]:
    response = stranger.get("/orders/%s/%s" % (order_id, path))
    check("a signed-out request for the %s is turned away" % path,
          response.status_code in (301, 302, 303),
          "status=%d" % response.status_code)


print("\n=== 6. One café cannot print another's order ===")
b = app.test_client()
build(b, "Other Cafe", "other", ["Chai"])

for path in ["bill", "kot"]:
    response = b.get("/orders/%s/%s" % (order_id, path), follow_redirects=True)
    body = response.get_data(as_text=True)
    check("café B is refused the %s" % path,
          "Latte" not in body and "Order not found" in body,
          "another café's order was printed")


print("\n=== 7. A new order is billed straight away ===")
fresh = place(a, {foods[0]: 1})
html = a.get("/orders/%s/bill" % fresh).get_data(as_text=True)
check("a brand new order already prints a full tax invoice",
      "Subtotal" in html and "Not billed yet" not in html,
      "creating an order is supposed to raise its bill, so the receipt "
      "should be a complete invoice with no further step")


print("\n=== 8. ...and prints sensibly even without one ===")
# Defensive only: the normal path always has a bill, so this happens just
# when one has been removed. The receipt should still be usable rather than
# an error page, and must not call itself a tax invoice.
mysql_shim._DB.execute("DELETE FROM bills WHERE order_id = ?", (fresh,))
mysql_shim._DB.commit()

html = a.get("/orders/%s/bill" % fresh).get_data(as_text=True)
check("it still prints the items", "Latte" in html, "the receipt is empty")
check("it falls back to the order total", "120.00" in html,
      "no total was printed")
# The heading, not the footnote - which mentions a tax invoice precisely
# to explain that this is not one.
kind = re.search(r'class="receipt__kind">([^<]+)<', html)
check("and does not claim to be a tax invoice",
      kind is not None and kind.group(1).strip() == "Order Summary",
      "the receipt is headed %r"
      % (kind.group(1).strip() if kind else None))


print("\n=== 9. A printed bill keeps the rate it was charged at ===")
TAX_LINE = r'<span>Tax[^<]*</span>\s*<span>[^\d-]*([\d.]+)</span>'

before = re.search(TAX_LINE,
                   a.get("/orders/%s/bill" % order_id).get_data(as_text=True))
a.post("/settings/tax", data={"tax_percent": "25", "_csrf_token": csrf(a)},
       follow_redirects=True)
after = re.search(TAX_LINE,
                  a.get("/orders/%s/bill" % order_id).get_data(as_text=True))
check("changing the tax rate does not rewrite an old receipt",
      before and after and before.group(1) == after.group(1) == "18.00",
      "tax printed %s before and %s after the rate change"
      % (before and before.group(1), after and after.group(1)))


print("\n=== 10. The order page offers both ===")
page = a.get("/orders/%s" % order_id).get_data(as_text=True)
check("there is a Print Bill action",
      "/orders/%s/bill" % order_id in page)
check("there is a Print KOT action",
      "/orders/%s/kot" % order_id in page)
check("they open in their own tab",
      page.count('target="_blank"') >= 2,
      "printing would navigate away from the order")


print("\n=== 11. The customer's bill is dressed for a customer ===")
bill_html = a.get("/orders/%s/bill" % order_id).get_data(as_text=True)

check("the cafe's symbol sits behind the bill",
      'class="receipt__watermark"' in bill_html,
      "there is nothing behind the text")
check("and a cafe with no symbol still gets one drawn",
      "brand-glyph" in bill_html,
      "an empty watermark is rendered when no logo is uploaded")

quote = re.search(r'class="receipt__quote">\s*&ldquo;(.+?)&rdquo;',
                  bill_html, re.S)
check("a line is printed at the foot", quote is not None,
      "no quote on the bill")
check("and it is one of the cafe's own",
      quote and quote.group(1).strip() in application.CAFE_QUOTES,
      "printed %r, which is not in the list"
      % (quote.group(1).strip() if quote else None))

check("the customer's copy carries no bill number",
      re.search(r'<dt>Bill</dt>', bill_html) is None,
      "the receipt still prints the system's bill number")
check("but it does carry the order number",
      re.search(r'<dt>Order</dt>\s*<dd>#\d+</dd>', bill_html) is not None,
      "the order number is missing, and that is the one both sides hold")

check("the bill says who served it",
      "Served by" in bill_html and "Bean Scene" in bill_html,
      "the cafe is not named as the server")
check("and what it was run on",
      "Cafe Manager" in bill_html,
      "the system is not credited")
check("it carries the time it was printed",
      'class="receipt__printed"' in bill_html,
      "a reprint could not be told from the original")

# A reprint must say the same thing. A customer handed two copies should
# not find two different footers.
again = re.search(r'class="receipt__quote">\s*&ldquo;(.+?)&rdquo;',
                  a.get("/orders/%s/bill" % order_id).get_data(as_text=True),
                  re.S)
check("reprinting it says the same line",
      again and quote and again.group(1) == quote.group(1),
      "the second copy quoted something else")

# The date on something a customer keeps needs its year.
check("the date carries the year",
      re.search(r'receipt__meta--wide.*?<dd>\d{2} \w{3} \d{4},', bill_html,
                re.S) is not None,
      "the date is not printed with a year")

# Printers drop pale artwork unless told not to.
print_css = open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "static", "css", "print.css"),
    encoding="utf-8").read()
check("the symbol behind it survives an actual printer",
      "print-color-adjust: exact" in print_css,
      "the watermark would be dropped when printed")

print("\n=== 12. Every address a screen asks to print actually exists ===")
# This section exists because it did not. The kitchen screen and the
# counter screens both asked to print "/print/kot/<id>", which was never
# a route in this app - the ticket that was meant to come out by itself
# came out as the dashboard, silently, in a hidden frame nobody looks at.
#
# The test that was supposed to cover it only checked that the page asked
# to print the string the page itself contained, so it agreed with the
# bug. Nothing checked the address against the app. This does.
import werkzeug.routing                                      # noqa: E402

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "templates")


def printed_paths():
    """Every URL handed to autoPrint, with the ids filled in."""
    found = []
    for name in sorted(os.listdir(TEMPLATES)):
        if not name.endswith(".html"):
            continue
        body = io.open(os.path.join(TEMPLATES, name),
                       encoding="utf-8").read()
        for call in re.findall(r"autoPrint\(\s*(.+?)\)\s*;", body, re.S):
            if "function" in call:          # the helper's own definition
                continue
            # "/orders/" + id + "/kot"  ->  /orders/1/kot
            path = ""
            for piece in re.split(r"\s*\+\s*", call.strip()):
                piece = piece.strip()
                if piece[:1] in ("'", '"'):
                    path += piece[1:-1]
                else:
                    path += "1"
            found.append((name, path))
    return found


calls = printed_paths()
check("the screens do ask to print something",
      len(calls) >= 3,
      "found %d autoPrint calls, so this section is checking nothing"
      % len(calls))

adapter = app.url_map.bind("localhost")
broken = []
for name, path in calls:
    try:
        adapter.match(path)
    except werkzeug.routing.RequestRedirect:
        pass                                 # a real route, just tidier
    except werkzeug.exceptions.HTTPException:
        broken.append("%s asks for %s" % (name, path))

check("and every one of those addresses is a real route",
      not broken,
      "; ".join(broken) or "n/a")

# The two that matter, named outright, so a rename cannot quietly drop
# them from the list above and still pass.
asked = {path for _, path in calls}
check("the kitchen ticket is among them",
      any(path.endswith("/kot") for path in asked),
      "nothing asks to print a kitchen ticket at all: %s" % sorted(asked))

check("and so is the customer's bill",
      any(path.endswith("/bill") for path in asked),
      "nothing asks to print a bill at all: %s" % sorted(asked))

print("\n=== 13. A receipt for the customer who asks for one ===")
# The kitchen ticket prints by itself, because somebody has to cook the
# food. The customer's copy does not: most people walk off without one and
# printing every time burns a roll a day. So it is a button, sitting where
# the money is already being taken.
billing_html = a.get("/billing").get_data(as_text=True)

check("billing offers a print button on the row",
      "print-bill-btn" in billing_html,
      "there is no way to print a receipt from the billing list")

check("and it sits beside the payment status",
      re.search(r"<th>Payment Status</th>\s*<th>Print Bill</th>",
                billing_html) is not None,
      "the column is not where the cashier's hand already is")

printed = re.findall(r'data-print-order="(\d+)"', billing_html)
check("each button names the order it would print",
      bool(printed), "no button carries an order number")

check("and that order really does have a printable bill",
      bool(printed)
      and a.get("/orders/%s/bill" % printed[0]).status_code == 200,
      "the button points at something that does not print")

check("pressing it prints rather than leaving the page",
      "autoPrint('/orders/' + orderId + '/bill')" in billing_html,
      "the cashier loses their place in the list every time somebody "
      "asks for a receipt")

# The row grew a column. If the message shown when a filter finds nothing
# still spans the old number, the table comes apart on that day only -
# which is exactly the sort of thing nobody sees until a customer does.
billing_template = io.open(os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "templates", "billing.html"),
    encoding="utf-8").read()

thead = re.search(
    r'<table class="data-table billing-table">\s*<thead>(.*?)</thead>',
    billing_template, re.S)
columns = len(re.findall(r"<th>", thead.group(1))) if thead else 0
spans = sorted(set(re.findall(r'colspan="(\d+)"', billing_template)))

check("and the empty-day message still spans every column",
      columns > 0 and spans == [str(columns)],
      "%d columns, but colspan says %s" % (columns, spans))

print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
