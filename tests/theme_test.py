"""
The cafe's accent colour: who may change it, and who has to live with it.

Only an admin can pick the theme, and the choice applies to the whole cafe
rather than to the person who made it - everyone is looking at the same
screens. A cafe that never touches this stays Copper, which is how the app
has always looked.

The value ends up in an HTML attribute, so this also pins down that an
unrecognised theme name never reaches the page.

Run with:  python tests/theme_test.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "theme-test-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import app as application     # noqa: E402

app = application.app
app.config["TESTING"] = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def csrf(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


def theme_of(client, path="/orders/add"):
    """The accent the app is actually painted in, read off <html>."""
    html = client.get(path).get_data(as_text=True)
    match = re.search(r'<html[^>]*\bdata-theme="([^"]*)"', html)
    return match.group(1) if match else None


def sign_up(cafe, username):
    client = app.test_client()
    client.post("/register", data={
        "cafe_name": cafe, "full_name": username.title(), "username": username,
        "phone_number": "", "password": "password123",
        "confirm_password": "password123"}, follow_redirects=True)
    return client


admin = sign_up("Hue Cafe", "boss")


print("\n=== 1. A cafe that never chooses keeps the original look ===")
check("the default accent is copper", theme_of(admin) == "copper",
      "started on %s" % theme_of(admin))
check("the stylesheet that paints it is loaded",
      "css/theme.css" in admin.get("/orders/add").get_data(as_text=True),
      "theme.css is not linked, so no theme can apply")


print("\n=== 2. The picker offers real choices ===")
page = admin.get("/settings/theme")
check("an admin can open it", page.status_code == 200,
      "got HTTP %s" % page.status_code)

html = page.get_data(as_text=True)
offered = re.findall(r'name="theme"\s+value="([a-z]+)"', html)
check("every theme the server knows is on the page",
      sorted(offered) == sorted(application.THEME_IDS),
      "page offers %s, server knows %s" % (sorted(offered),
                                           sorted(application.THEME_IDS)))
check("more than one colour is actually offered", len(offered) >= 2,
      "only %s" % offered)
check("the current one is pre-selected",
      re.search(r'value="copper"[^>]*checked', html) is not None,
      "nothing marks copper as the current choice")

# The swatch has to be painted by the same CSS block as the app, or the
# preview could show a colour the theme does not use.
theme_css = read("static", "css", "theme.css")
for theme_id in sorted(application.THEME_IDS):
    pattern = r'\.theme-%s,\s*:root\[data-theme="%s"\]' % (theme_id, theme_id)
    check("%s is defined once for both the swatch and the app" % theme_id,
          re.search(pattern, theme_css) is not None,
          "theme.css has no shared block for %s" % theme_id)


print("\n=== 3. An admin can change it ===")
admin.post("/settings/theme",
           data={"theme": "ocean", "_csrf_token": csrf(admin)},
           follow_redirects=True)
check("the choice sticks", theme_of(admin) == "ocean",
      "the app is still %s" % theme_of(admin))
check("and it survives a fresh page", theme_of(admin, "/") == "ocean",
      "the dashboard is %s" % theme_of(admin, "/"))


print("\n=== 4. A name we do not recognise never reaches the page ===")
INJECTION = '"><script>alert(1)</script>'
response = admin.post("/settings/theme",
                      data={"theme": INJECTION, "_csrf_token": csrf(admin)},
                      follow_redirects=True)
check("the junk is refused",
      "Pick one of the colours" in response.get_data(as_text=True),
      "no message explained the refusal")
check("and the old theme is untouched", theme_of(admin) == "ocean",
      "the theme became %r" % theme_of(admin))
check("nothing was echoed into the markup",
      "<script>alert(1)</script>" not in admin.get(
          "/orders/add").get_data(as_text=True),
      "the submitted string was written into the page")
check("normalize_theme falls back rather than trusting input",
      application.normalize_theme("../../etc") == application.DEFAULT_THEME
      and application.normalize_theme(None) == application.DEFAULT_THEME,
      "got %r" % application.normalize_theme("../../etc"))


print("\n=== 5. It is the cafe's colour, not one person's ===")
admin.post("/users/add", data={
    "full_name": "Cash", "username": "cash", "role": "cashier",
    "phone_number": "", "password": "password123",
    "_csrf_token": csrf(admin)}, follow_redirects=True)

cashier = app.test_client()
cashier.post("/login", data={"username": "cash", "password": "password123"},
             follow_redirects=True)

check("a cashier sees the theme the admin chose",
      theme_of(cashier) == "ocean",
      "the cashier's app is %s" % theme_of(cashier))


print("\n=== 6. Only an admin may change it ===")
check("a cashier is not offered it in the profile menu",
      "Theme Colour" not in cashier.get("/orders/add").get_data(as_text=True),
      "the menu entry is visible to a cashier")
check("an admin is",
      "Theme Colour" in admin.get("/orders/add").get_data(as_text=True),
      "the admin has no way to reach the page")

blocked = cashier.get("/settings/theme", follow_redirects=False)
check("opening the page directly is turned away",
      blocked.status_code in (302, 303),
      "a cashier got HTTP %s" % blocked.status_code)

cashier.post("/settings/theme",
             data={"theme": "berry", "_csrf_token": csrf(cashier)},
             follow_redirects=True)
check("and posting to it changes nothing", theme_of(admin) == "ocean",
      "a cashier moved the theme to %s" % theme_of(admin))

check("the endpoint is not on the staff allowlist",
      "theme_settings" not in application.STAFF_ALLOWED_ENDPOINTS,
      "the allowlist would let a non-admin through")


print("\n=== 7. One cafe's choice is not another's ===")
other = sign_up("Second Cafe", "owner2")
check("a different cafe is still on the default",
      theme_of(other) == "copper",
      "it picked up %s from the first cafe" % theme_of(other))

other.post("/settings/theme",
           data={"theme": "sage", "_csrf_token": csrf(other)},
           follow_redirects=True)
check("each cafe keeps its own",
      theme_of(other) == "sage" and theme_of(admin) == "ocean",
      "cafe one is %s, cafe two is %s" % (theme_of(admin), theme_of(other)))


print("\n=== 8. The accent is a token, so a theme can move all of it ===")
style = read("static", "css", "style.css")
stray = re.findall(r"rgba\(\s*224,\s*138,\s*62", style)
check("no rgba tint is still hard-coded to copper", not stray,
      "%d tints would stay copper whatever theme is chosen" % len(stray))
check("the channels are available as a token",
      "--copper-rgb" in style, "no --copper-rgb token to tint with")

for page_name in ("add_order.html", "dashboard.html"):
    markup = read("templates", page_name)
    check("%s tints follow the theme too" % page_name,
          not re.search(r"rgba\(\s*224,\s*138,\s*62", markup),
          "a copper tint is still baked into %s" % page_name)


print("\n=== 9. A printed page carries the same accent ===")
check("the print page links the theme sheet",
      "css/theme.css" in read("templates", "print_bill.html"),
      "the print toolbar would stay copper")
check("and the toolbar reads the token rather than a fixed hex",
      "var(--copper" in read("static", "css", "print.css"),
      "the Print button is hard-coded")


print("\n=== 10. An open session picks the colour up on its next page ===")
# The attribute lives on <html>, outside the region instant.js swaps, so
# without this a staff member's session would stay on the old colour.
instant = read("static", "js", "instant.js")
check("instant navigation carries the theme across a swap",
      "data-theme" in instant and "documentElement.setAttribute" in instant,
      "a swapped page would leave the old accent in place")


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
