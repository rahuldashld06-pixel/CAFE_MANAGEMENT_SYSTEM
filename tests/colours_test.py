"""
A café's own colours, and the discount that comes off its bills.

Two things that arrived together because they are the same request: let
an admin set the app's colours to anything at all, in one place, and put
a discount beside the tax rate.

The colours are the harder half. Six named accents were a closed set, so
the app could be written for them; "any colour at all" cannot be. The
app is built for dark surfaces with pale text, and somebody choosing a
white background must not end up with cream text on cream - so the whole
palette is derived from whatever they picked, and the guarantee this
suite is really about is that every derivation stays readable. Including
the awkward ones: a mid-toned background has little contrast room on
either side, and that is exactly when a fixed palette falls over.

The product's own watermark has to survive the same treatment. It is the
developer's mark rather than the café's, it is drawn at 12% opacity, and
it was painted in the accent - which is fine on espresso and invisible
on a pale page.

Run with:  python tests/colours_test.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "colours-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import app as application               # noqa: E402

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


def register(client, cafe, user):
    return client.post("/register", data={
        "cafe_name": cafe, "full_name": user.title(), "username": user,
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)


def rates_on_page(client=None):
    """The two rates as numbers, however the page spells them."""
    page = (client or admin).get("/settings/tax").get_data(as_text=True)
    found = {}
    for field in ("tax_percent", "discount_percent"):
        match = re.search(
            r'id="%s"[^>]*value="([\d.]+)"' % field, page, re.S)
        found[field] = float(match.group(1)) if match else None
    return found["tax_percent"], found["discount_percent"]


def root_tag(client, path="/dashboard"):
    page = client.get(path).get_data(as_text=True)
    found = re.search(r"<html[^>]*>", page)
    return found.group(0) if found else ""


def variables(client, path="/dashboard"):
    """The colours the page actually carries, as a dict."""
    tag = root_tag(client, path)
    style = re.search(r'style="([^"]*)"', tag)
    if not style:
        return {}
    return dict(part.split(":", 1)
                for part in style.group(1).split(";") if ":" in part)


admin = app.test_client()
register(admin, "Colour Cafe", "boss")


# =====================================================================
print("\n=== 1. A cafe that has chosen nothing ===")
# =====================================================================

tag = root_tag(admin)
check("the page carries no colours of its own",
      "style=" not in tag,
      "it says %r - the default look must be the stylesheet untouched, "
      "not a derivation that happens to come close" % tag)

check("and still names its theme", 'data-theme="copper"' in tag, tag)


# =====================================================================
print("\n=== 2. One of the six, as before ===")
# =====================================================================

admin.post("/settings/theme", data={"theme": "ocean",
                                    "_csrf_token": csrf(admin)},
           follow_redirects=True)

tag = root_tag(admin)
check("a named accent is still just the attribute",
      'data-theme="ocean"' in tag and "style=" not in tag,
      "it says %r; the six themes live in theme.css and should not be "
      "rewritten into every page" % tag)


# =====================================================================
print("\n=== 3. Any accent at all ===")
# =====================================================================

admin.post("/settings/theme", data={
    "theme": "ocean", "accent_mode": "custom", "accent_hex": "#7B2FF7",
    "_csrf_token": csrf(admin)}, follow_redirects=True)

have = variables(admin)
check("the chosen accent is what the page is painted in",
      have.get("--copper") == "#7b2ff7", have.get("--copper"))

check("with a lighter one for hover and a darker one for pressed",
      have.get("--copper-light") and have.get("--copper-dark")
      and have["--copper-light"] != have["--copper-dark"], have)

check("and the same colour as three numbers, for the rgba() tints",
      have.get("--copper-rgb") == "123, 47, 247",
      "%r - rgba() cannot be handed a hex variable, so the channels have "
      "to travel separately" % have.get("--copper-rgb"))


# =====================================================================
print("\n=== 4. Any background at all, text and all ===")
# =====================================================================

admin.post("/settings/theme", data={
    "theme": "ocean", "surface_mode": "custom", "surface_hex": "#FFFFFF",
    "_csrf_token": csrf(admin)}, follow_redirects=True)

have = variables(admin)
check("the page sits on the colour that was chosen",
      have.get("--espresso-950") == "#ffffff", have.get("--espresso-950"))

check("the surfaces above it are all different",
      len({have.get("--espresso-%s" % level)
           for level in (950, 900, 850, 800, 700, 600)}) == 6,
      "panels, borders and the page would be the same flat colour: %s"
      % [have.get("--espresso-%s" % n) for n in (950, 900, 850, 800, 700, 600)])

check("and a white cafe gets dark text, not cream",
      application._luminance(have["--cream"]) < 0.3,
      "the body text came out %s on a white page, which cannot be read"
      % have.get("--cream"))

check("choosing a background does not disturb the accent",
      "--copper" not in have,
      "the accent was reset to %s by a change to the background"
      % have.get("--copper"))


# =====================================================================
print("\n=== 5. Readable on any background, including the awkward ones ===")
# =====================================================================

# Mid-toned colours are the test that matters. Near either end of the
# scale there is plenty of contrast room and any sensible palette works;
# at half lightness there is little either way, which is where a set of
# fixed lightnesses quietly stops being readable.
# What is asserted here is what is achievable, not what is ideal. The
# derivation aims higher - 7:1 for body text - but against a mid-toned
# background no colour on earth reaches 7:1: pure black on mid grey is
# 5.3:1 and that is the end of the scale. So the guarantee is the
# ordinary readability threshold, which every background does clear.
FLOORS = (("--cream", 4.5, "body text"),
          ("--cream-dim", 3.0, "secondary text"),
          ("--cream-faint", 2.2, "muted hints"))

BACKGROUNDS = (("the app's own espresso", "#170f0b"),
               ("white", "#ffffff"),
               ("black", "#000000"),
               ("navy", "#0b1a3a"),
               ("pale mint", "#e8f5ec"),
               ("a mid blue", "#4a9bd8"),
               ("hot pink", "#ff2d95"),
               ("mid grey", "#808080"),
               ("saturated lime", "#7fff00"))

for label, colour in BACKGROUNDS:
    palette = application.surface_variables(colour)
    page = palette["--espresso-950"]

    worst = min(
        (application._contrast(palette[name], page) / floor, name, what)
        for name, floor, what in FLOORS)

    check("%s stays readable" % label, worst[0] >= 1.0,
          "%s came out at %.1f:1 against %s, under the floor"
          % (worst[2], application._contrast(palette[worst[1]], page),
             page))


# =====================================================================
print("\n=== 6. The mark stays visible whatever is chosen ===")
# =====================================================================

def composited(ink, page, alpha=0.12):
    """What the eye gets: the mark is drawn at 12%, not at full strength."""
    return "#%02x%02x%02x" % tuple(
        int(round(alpha * int(ink[at:at + 2], 16)
                  + (1 - alpha) * int(page[at:at + 2], 16)))
        for at in (1, 3, 5))


# The mark on the app's own espresso, which is the look this is measured
# against - it is already there to be seen, and no choice of background
# is allowed to make it less visible than it is today.
ESPRESSO = application.surface_variables("#170f0b")
BASELINE = application._contrast(
    composited(ESPRESSO["--watermark-ink"], ESPRESSO["--espresso-950"]),
    ESPRESSO["--espresso-950"])

for label, colour in BACKGROUNDS:
    palette = application.surface_variables(colour)
    page = palette["--espresso-950"]
    seen = application._contrast(
        composited(palette["--watermark-ink"], page), page)

    check("the watermark shows on %s" % label,
          seen >= BASELINE * 0.85,
          "it composites to %.2f:1 against %s, against %.2f:1 on the app's "
          "own background - the mark would have faded out"
          % (seen, page, BASELINE))

check("a cafe that chose a background brings its own ink for the mark",
      "--watermark-ink" in variables(admin),
      "the mark would stay painted in the accent, which is a pale line on "
      "a pale page")


# =====================================================================
print("\n=== 7. Nothing arbitrary reaches the style attribute ===")
# =====================================================================

before = root_tag(admin)
for junk in ('" onload="alert(1)', "red", "javascript:alert(1)",
             "#12", "url(evil)", "#ggghhh", "expression(1)"):
    admin.post("/settings/theme", data={
        "theme": "ocean", "accent_mode": "custom", "accent_hex": junk,
        "_csrf_token": csrf(admin)}, follow_redirects=True)

    check("%r is refused" % junk, root_tag(admin) == before,
          "the page became %r" % root_tag(admin))


# =====================================================================
print("\n=== 8. Going back to a preset clears the custom colour ===")
# =====================================================================

admin.post("/settings/theme", data={"theme": "berry",
                                    "_csrf_token": csrf(admin)},
           follow_redirects=True)

tag = root_tag(admin)
check("a preset alone leaves no colours behind",
      'data-theme="berry"' in tag and "style=" not in tag,
      "it says %r - a custom colour that outlives the choice to stop "
      "using it can never be got rid of" % tag)


# =====================================================================
print("\n=== 9. Only an admin, and only their own cafe ===")
# =====================================================================

admin.post("/users/add", data={
    "full_name": "Cash Ier", "username": "cash", "password": "password123",
    "confirm_password": "password123", "role": "staff",
    "_csrf_token": csrf(admin)}, follow_redirects=True)

cashier = app.test_client()
cashier.post("/login", data={"username": "cash", "password": "password123"},
             follow_redirects=True)

check("a cashier is not offered the page",
      "<span>Colours</span>" not in
      cashier.get("/orders/add").get_data(as_text=True))

check("an admin is",
      "<span>Colours</span>" in
      admin.get("/orders/add").get_data(as_text=True))

admin.post("/settings/theme", data={
    "theme": "ocean", "surface_mode": "custom", "surface_hex": "#102030",
    "_csrf_token": csrf(admin)}, follow_redirects=True)
mine = variables(admin)

cashier.post("/settings/theme", data={
    "theme": "gold", "surface_mode": "custom", "surface_hex": "#ff0000",
    "_csrf_token": csrf(cashier)}, follow_redirects=True)

check("and a cashier posting to it changes nothing",
      variables(admin) == mine,
      "the cafe's colours were changed by somebody who is not an admin")

other = app.test_client()
register(other, "Other Cafe", "rival")
check("another cafe's colours are its own",
      "style=" not in root_tag(other),
      "one cafe's background reached another's screens")


# =====================================================================
print("\n=== 10. A discount, beside the tax ===")
# =====================================================================

admin.post("/settings/tax", data={
    "tax_percent": "12.5", "discount_percent": "10",
    "_csrf_token": csrf(admin)}, follow_redirects=True)

page = admin.get("/settings/tax").get_data(as_text=True)
check("both rates come back on the page",
      'name="tax_percent"' in page and 'name="discount_percent"' in page)

check("a cashier cannot reach it",
      cashier.get("/settings/tax", follow_redirects=True).status_code == 200
      and 'name="discount_percent"' not in
      cashier.get("/settings/tax", follow_redirects=True)
      .get_data(as_text=True),
      "a cashier can set what every bill charges")


# ---- and the arithmetic -------------------------------------------
admin.post("/categories/add", data={"category_name": "Coffee",
                                    "description": "",
                                    "_csrf_token": csrf(admin)},
           follow_redirects=True)
category = re.search(r'<option value="(\d+)">',
                     admin.get("/foods/add").get_data(as_text=True)).group(1)
admin.post("/foods/add", data={
    "food_name": "Flat White", "category_id": category, "price": "100",
    "quantity": "40", "minimum_stock": "2", "description": "",
    "_csrf_token": csrf(admin)}, follow_redirects=True)

screen = admin.get("/orders/add").get_data(as_text=True)
food = re.findall(r'id="quantity_(\d+)"', screen)[0]

check("the order screen names the cafe's real rates",
      "Discount (10%)" in screen and "Tax (12.5%)" in screen,
      "it says %r - the panel used to read 'Tax (5%%)' in plain text "
      "whatever the cafe charged"
      % re.findall(r"<span>(?:Discount|Tax) \([\d.]+%\)</span>", screen))

check("and hands the same rates to the running total",
      "var DISCOUNT_RATE = 0.1;" in screen and "var TAX_RATE = 0.125;" in screen,
      "the number on the button would disagree with the bill")

admin.post("/orders/add", data={"quantity_%s" % food: "2",
                                "_csrf_token": csrf(admin)},
           follow_redirects=True)

bill = mysql_shim._DB.execute(
    "SELECT subtotal, discount, tax, total_amount FROM bills "
    "ORDER BY bill_id DESC LIMIT 1").fetchone()

check("the discount comes off before the tax is worked out",
      [str(round(float(value), 2)) for value in bill]
      == ["200.0", "20.0", "22.5", "202.5"],
      "200 less 10%% is 180, and 12.5%% of 180 is 22.50, so the bill is "
      "202.50 - this one says %s. Taxing the full 200 would make it 205.00, "
      "which charges tax on money nobody paid" % (tuple(bill),))

order_total = mysql_shim._DB.execute(
    "SELECT total_amount FROM orders ORDER BY order_id DESC LIMIT 1"
).fetchone()[0]
check("and the order agrees with its own bill",
      round(float(order_total), 2) == round(float(bill[3]), 2),
      "the order says %s and its bill says %s" % (order_total, bill[3]))


# ---- a rate left out is a rate left alone --------------------------
admin.post("/settings/tax", data={"tax_percent": "8",
                                  "_csrf_token": csrf(admin)},
           follow_redirects=True)
check("sending one rate does not reset the other",
      rates_on_page() == (8.0, 10.0),
      "the page now reads %s - the discount was lost by a post that only "
      "mentioned the tax" % (rates_on_page(),))


# ---- and neither may be nonsense -----------------------------------
for field, value in (("tax_percent", "-1"), ("tax_percent", "101"),
                     ("discount_percent", "-5"), ("discount_percent", "200"),
                     ("discount_percent", "ten")):
    admin.post("/settings/tax", data={field: value,
                                      "_csrf_token": csrf(admin)},
               follow_redirects=True)
    check("%s of %r is refused" % (field, value),
          rates_on_page() == (8.0, 10.0),
          "it was accepted; the page now reads %s" % (rates_on_page(),))

admin.post("/settings/tax", data={"discount_percent": "100",
                                  "_csrf_token": csrf(admin)},
           follow_redirects=True)
check("100% is allowed at the boundary",
      rates_on_page()[1] == 100.0,
      "a cafe giving everything away is unusual, not invalid; the page "
      "reads %s" % (rates_on_page(),))


print("\n" + "=" * 62)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 62)
sys.exit(1 if FAILED else 0)
