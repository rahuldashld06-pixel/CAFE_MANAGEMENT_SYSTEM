"""
What a dish shows beside its name: veg or non-veg, and a picture.

 - Veg / non-veg is chosen on the Add and Edit Food forms and marked on
   the dish at the counter, in Food Management and on the customer's QR
   menu, which can also be narrowed to either.
 - A dish without a photo gets a drawing worked out from its name (then
   its category, then its description): a latte a cup, a dosa a dosa.
 - Water and soft drinks are a bottle and a can - "Cold Coke" is still a
   can - and every picture carries a tag that says what it is.
 - Add and Edit Food show that picture as the name is typed, and tick
   Veg or Non-veg from the name until somebody chooses.
 - On New Order, All is the categories; Hot selling is its own tab and no
   longer repeats its dishes at the top of All.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/menu_marks_test.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "menu-marks-secret"
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


def card(page, food_id, prefix="food_card_"):
    """The markup of one dish's card on New Order."""
    start = page.find('id="%s%d"' % (prefix, food_id))
    if start < 0:
        return ""
    end = page.find('id="food_card_', start + 10)
    return page[start:end if end > 0 else start + 6000]


# =====================================================================
print("\n=== 1. A picture for every dish, from what it is called ===")
# =====================================================================
kind = application.food_kind
for name, want in (
        ("Classic cappuccino", "coffee"), ("Iced vanilla latte", "cold"),
        ("Cold coffee", "cold"), ("Masala tea", "tea"), ("Green tea", "tea"),
        ("Oreo frappe", "shake"), ("Mango lassi", "shake"),
        ("Masala Dosa", "dosa"), ("Chicken Biryani", "rice"),
        ("Paneer Butter Masala", "curry"), ("Margherita Pizza", "pizza"),
        ("Crispy chicken burger", "burger"), ("Butter croissant", "croissant"),
        ("Blueberry muffin", "muffin"), ("Chocolate brownie", "cake"),
        ("Veg Hakka Noodles", "noodles"), ("Pasta Alfredo", "pasta"),
        ("French fries", "fries"), ("Tomato soup", "soup"),
        ("Caesar salad", "salad"), ("Vanilla ice cream", "icecream"),
        ("Paneer wrap", "wrap")):
    got = kind(name)
    check("%s is drawn as %s" % (name, want), got == want, got)
check("it reads the dish, not the first word it knows: a chicken burger "
      "is a burger", kind("Chicken burger") == "burger")
check("a name that says nothing falls back to its category",
      kind("House special", "Desserts") == "cake", kind("House special", "Desserts"))
check("then to its description",
      kind("Chef's pick", None, "grilled chicken with herbs") == "chicken")
check("and a dish nothing is known about gets the plain plate",
      kind("Mystery item") == "dish" and kind("") == "dish" and kind(None) == "dish")
check("upper or lower case makes no difference",
      kind("MASALA DOSA") == kind("masala dosa") == "dosa")

print("\n--- drinks from a bottle or a can ---")
for name, want in (
        ("Coca Cola", "soda"), ("Coke", "soda"), ("Diet Coke", "soda"),
        ("Pepsi 500ml", "soda"), ("Sprite", "soda"), ("Thums Up", "soda"),
        ("Mountain Dew", "soda"), ("Red Bull", "soda"), ("Soft drink", "soda"),
        ("Cold drink", "soda"), ("Water Bottle", "water"),
        ("Mineral Water 1L", "water"), ("Bisleri 500 ml", "water"),
        ("Coconut water", "juice"), ("Watermelon juice", "juice"),
        ("Maaza", "juice"), ("Soda bread", "bread"),
        ("Espresso tonic", "cold")):
    got = kind(name)
    check("%s is drawn as %s" % (name, want), got == want, got)
check("a can already cold stays a can: 'Cold Coke', 'Chilled water'",
      kind("Cold Coke") == "soda" and kind("Chilled water") == "water"
      and kind("Iced water") == "water")
check("while an iced latte is still a cold glass", kind("Iced latte") == "cold")
check("a dish called nothing a drink is, under Soft Drinks, is a can",
      kind("House special", "Soft Drinks") == "soda")

label = application.food_kind_label
check("each picture has a tag that says what it is",
      label("soda") == "Soft drink" and label("water") == "Water"
      and label("icecream") == "Ice cream" and label("nonsense") == "Dish")
check("every kind has one", all(k in application.FOOD_KIND_LABELS
                                for k in application.FOOD_KIND_NAMES))

print("\n--- veg or non-veg, read from the name ---")
guess = application.diet_guess
for name, want in (("Chicken Biryani", "nonveg"), ("Egg Fried Rice", "nonveg"),
                   ("Fish fingers", "nonveg"), ("Non veg thali", "nonveg"),
                   ("Paneer tikka", "veg"), ("Veg Hakka Noodles", "veg"),
                   ("Eggless brownie", "veg"), ("Coke", "veg"),
                   ("Water Bottle", "veg"), ("Latte", "veg"),
                   ("Club sandwich", None), ("Brownie", None),
                   ("Burger", None), ("", None)):
    check("%r reads as %s" % (name, want or "unsaid"), guess(name) == want,
          guess(name))

sprite = open(os.path.join(ROOT, "templates", "_food_art.html"), encoding="utf-8").read()
missing = [k for k in application.FOOD_KIND_NAMES if 'id="food-%s"' % k not in sprite]
check("every kind it can pick has a drawing", not missing, missing)
style = open(os.path.join(ROOT, "static", "css", "style.css"), encoding="utf-8").read()
uncoloured = [k for k in application.FOOD_KIND_NAMES
              if k != "dish" and ".food-art--%s" % k not in style]
check("and its own colour", not uncoloured, uncoloured)


# =====================================================================
print("\n=== 2. Veg or non-veg, chosen on the form ===")
# =====================================================================
owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Mark Cafe", "full_name": "Mira Owner", "username": "mira",
    "phone_number": "", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
for name in ("Coffee", "Mains"):
    owner.post("/categories/add", data={"category_name": name, "description": "",
                                        "_csrf_token": csrf(owner)})
options = dict((label.strip(), value) for value, label in re.findall(
    r'<option value="(\d+)">\s*([^<]+?)\s*</option>', text(owner.get("/foods/add"))))

form = text(owner.get("/foods/add"))
check("Add Food shows the picture a name will get, as it is typed",
      'id="foodGuess"' in form and "/api/food-guess" in form)
said = owner.get("/api/food-guess?name=Coca%20Cola").get_json()
check("asking what 'Coca Cola' would be: a soft drink, veg",
      said == {"kind": "soda", "label": "Soft drink", "diet": "veg"}, said)
said = owner.get("/api/food-guess?name=Special&category=Soft%20Drinks").get_json()
check("the category chosen counts when the name says nothing",
      said["kind"] == "soda", said)
check("the answer is only for someone signed in",
      app.test_client().get("/api/food-guess?name=Coke").status_code == 302)
check("Add Food asks veg or non-veg",
      'name="diet" value="veg" required' in form
      and 'name="diet" value="nonveg" required' in form)


def add(name, category, diet=None):
    data = {"food_name": name, "category_id": options[category], "price": "150",
            "quantity": "50", "minimum_stock": "1", "description": "",
            "_csrf_token": csrf(owner)}
    if diet is not None:
        data["diet"] = diet
    owner.post("/foods/add", data=data)
    return db("SELECT food_id, diet FROM foods WHERE food_name = ?", (name,))[0]


dosa = add("Masala Dosa", "Mains", "veg")
curry = add("Chicken Curry", "Mains", "nonveg")
latte = add("Cafe Latte", "Coffee")
odd = add("Odd Dish", "Mains", "vegan-ish")
check("veg is kept", dosa[1] == "veg", dosa)
check("non-veg is kept", curry[1] == "nonveg", curry)
check("a dish added without saying is left unmarked, not guessed",
      latte[1] is None, latte)
check("and so is one that says something else", odd[1] is None, odd)

edit = text(owner.get("/foods/edit/%d" % dosa[0]))
check("Edit Food shows the dish's picture and its tag",
      'id="foodGuess"' in edit and "food-art--dosa food-guess__art" in edit
      and 'id="foodGuessTag">Dosa<' in edit)
check("Edit Food shows what was chosen",
      re.search(r'name="diet" value="veg" required\s+checked', edit) is not None)


def edit_food(food_id, name, diet=None):
    data = {"food_name": name, "category_id": options["Mains"], "price": "150",
            "minimum_stock": "1", "description": "", "_csrf_token": csrf(owner)}
    if diet is not None:
        data["diet"] = diet
    owner.post("/foods/edit/%d" % food_id, data=data)
    return db("SELECT diet FROM foods WHERE food_id = ?", (food_id,))[0][0]


check("and it can be changed", edit_food(odd[0], "Odd Dish", "nonveg") == "nonveg")
check("an edit that does not send it leaves it as it was",
      edit_food(odd[0], "Odd Dish") == "nonveg")


# =====================================================================
print("\n=== 3. Marked wherever the dish is ===")
# =====================================================================
for _ in range(3):
    owner.post("/orders/add", data={"quantity_%d" % dosa[0]: "2",
                                    "_csrf_token": csrf(owner)})
page = text(owner.get("/orders/add"))
dosa_card = card(page, dosa[0])
check("New Order marks a veg dish veg", "diet-mark--veg" in dosa_card, dosa_card[:300])
check("and a non-veg one non-veg", "diet-mark--nonveg" in card(page, curry[0]))
check("and marks nothing it was not told",
      "diet-mark" not in card(page, latte[0]))
check("a dish without a photo gets its drawing",
      "food-art--dosa" in dosa_card and "food-art--coffee" in card(page, latte[0]))
check("the drawings are on the page once, for every card to use",
      page.count('<symbol id="food-dosa"') == 1)

foods_page = text(owner.get("/foods"))
check("Food Management marks them too",
      "diet-mark--veg" in foods_page and "diet-mark--nonveg" in foods_page)
check("with the drawings", "food-art--curry" in foods_page)

stock_page = owner.get("/inventory").get_data(as_text=True)
check("Inventory marks each dish beside its name",
      re.search(r'diet-mark--veg"[^>]*></span>Masala Dosa', stock_page) is not None
      and re.search(r'diet-mark--nonveg"[^>]*></span>Chicken Curry', stock_page) is not None)
check("and marks nothing it was not told",
      re.search(r'</span>Cafe Latte', stock_page) is None
      and "Cafe Latte" in stock_page)


# =====================================================================
print("\n=== 4. All is the categories; Hot selling is its own tab ===")
# =====================================================================
hot_section = re.search(r'<section class="food-group food-group--hot"[^>]*>', page)
check("the Hot selling shelf is there", hot_section is not None)
check("and has its own tab", 'data-tab="hot"' in page)
check("but is hidden when All is chosen, as the page opens",
      hot_section is not None and " hidden" in hot_section.group(0),
      hot_section and hot_section.group(0))
check("All leaves the shelf out when it filters",
      'tab === "all"' in page
      and 'group.getAttribute("data-tab") !== "hot"' in page)
check("the dish is still under its category on All",
      card(page, dosa[0]) != "" and card(page, dosa[0], "food_card_hot_") != "")


# =====================================================================
print("\n=== 5. The customer's QR menu ===")
# =====================================================================
with owner.session_transaction() as sess:
    token = application.get_public_token(sess["cafe_id"])
menu = text(app.test_client().get("/m/%s" % token))
check("offers Veg and Non-veg to narrow the menu",
      'id="menuDiet"' in menu and 'data-diet="veg"' in menu
      and 'data-diet="nonveg"' in menu)
check("each dish says which it is",
      re.search(r'class="p-item" data-diet="veg"', menu) is not None
      and re.search(r'class="p-item" data-diet="nonveg"', menu) is not None)
check("and carries the mark", "diet-mark--veg" in menu and "diet-mark--nonveg" in menu)
check("and a picture", "food-art--dosa" in menu and "p-item__pic" in menu)
check("with the drawings on the page",
      '<symbol id="food-dosa"' in menu)

guest = app.test_client()
guest.post("/m/%s/order" % token, data={"quantity_%d" % dosa[0]: "1",
                                         "quantity_%d" % curry[0]: "1"})
ref = mysql_shim._DB.execute(
    "SELECT public_ref FROM orders WHERE source = 'qr' ORDER BY order_id DESC").fetchone()[0]
placed = guest.get("/m/%s/placed/%s" % (token, ref)).get_data(as_text=True)
check("the customer's own order marks each dish too",
      re.search(r'diet-mark--veg"[^>]*></span>Masala Dosa', placed) is not None
      and re.search(r'diet-mark--nonveg"[^>]*></span>Chicken Curry', placed) is not None)


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
