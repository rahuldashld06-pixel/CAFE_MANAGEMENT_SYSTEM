"""
Signing in names the cafe; who may reset a password; the eye on User
Management.

 - The sign-in form asks for the cafe or restaurant first. The name
   decides whose place it is and the username whose account there, so
   the right name and the right account sign in, and a wrong name does
   not - with the same message as a wrong password, counted the same.
 - Forgot password is for the cafe's admin. Staff are told, kindly, to
   ask their admin, who sets a new one from User Management.
 - A password an admin sets for somebody can be read again by the
   admins, behind an eye. A password somebody chose for themselves is
   never kept, and choosing one throws away what was.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/sign_in_cafe_test.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "sign-in-cafe-secret"
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


def flashes(client):
    with client.session_transaction() as sess:
        return [message for _, message in sess.pop("_flashes", [])]


def signed_in(client):
    with client.session_transaction() as sess:
        return sess.get("user_id")


def text(response):
    return response.get_data(as_text=True)


# =====================================================================
print("\n=== 0. Two cafes ===")
# =====================================================================
owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Mom's Café", "full_name": "Maya Owner", "username": "maya",
    "phone_number": "9876543210", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
other = app.test_client()
other.post("/register", data={
    "cafe_name": "Dosa Point", "full_name": "Dev Owner", "username": "dev",
    "phone_number": "", "password": "Brew-Latte-42",
    "confirm_password": "Brew-Latte-42"}, follow_redirects=True)
owner.post("/users/add", data={
    "full_name": "Sam Cashier", "username": "sam", "role": "cashier",
    "phone_number": "9123456780", "password": "TillPass-2026",
    "_csrf_token": csrf(owner)})
owner.post("/users/add", data={
    "full_name": "Asha Manager", "username": "asha", "role": "manager",
    "phone_number": "", "password": "AdminPass-77",
    "_csrf_token": csrf(owner)})
flashes(owner)
SAM = db("SELECT user_id FROM users WHERE username = 'sam'")[0][0]
ASHA = db("SELECT user_id FROM users WHERE username = 'asha'")[0][0]
MAYA = db("SELECT user_id FROM users WHERE username = 'maya'")[0][0]
# No sign-in codes in the way: this is about the password step.
db("UPDATE users SET phone_number = NULL, email = NULL")
print("  seeded")


def sign_in(cafe, username, password, **extra):
    client = app.test_client()
    data = {"username": username, "password": password}
    if cafe is not None:
        data["cafe_name"] = cafe
    data.update(extra)
    response = client.post("/login", data=data)
    if signed_in(client):
        # The first page after signing in hands out the form token.
        client.get("/account/password")
    return client, response


def said_on(response):
    """The messages a refused sign-in shows, on the page it draws again."""
    return re.findall(r'class="flash"[^>]*>([^<]+)<', text(response))


# =====================================================================
print("\n=== 1. The form asks which place first ===")
# =====================================================================
page = text(app.test_client().get("/login"))
fields = re.findall(r'<input[^>]*name="(\w+)"', page)
check("the cafe or restaurant name is the first field",
      fields[:3] == ["cafe_name", "username", "password"], fields)
check("labelled as such, and required",
      "Café or restaurant name" in page
      and re.search(r'name="cafe_name"[^>]*required', page, re.S) is not None)
check("Register asks for it first too, by the same name",
      "Café or restaurant name" in text(app.test_client().get("/register")))


# =====================================================================
print("\n=== 2. The name decides whose place it is ===")
# =====================================================================
for typed in ("Mom's Café", "moms cafe", "  MOM'S   CAFE ", "Mom’s Cafe"):
    client, _ = sign_in(typed, "sam", "TillPass-2026")
    check("%r signs Sam in" % typed, signed_in(client) == SAM)
db("UPDATE cafes SET brand_name = 'Maya Kitchen' WHERE cafe_name = ?", ("Mom's Café",))
client, _ = sign_in("Maya Kitchen", "sam", "TillPass-2026")
check("so does the name the cafe shows in its corner", signed_in(client) == SAM)
client, _ = sign_in("mom's café", "maya", "Brew-Latte-42")
check("and the owner, by the same name", signed_in(client) == MAYA)

client, response = sign_in("Dosa Point", "sam", "TillPass-2026")
check("another cafe's name does not sign Sam in", signed_in(client) is None)
said = said_on(response)
check("it says the name, username or password is not right - not which",
      any("café name, username or password" in m for m in said), said)
client, response = sign_in("Nowhere", "sam", "wrong")
check("the same words for a wrong password",
      said_on(response) == said, said_on(response))
client, _ = sign_in("", "sam", "TillPass-2026")
check("a blank name is taken as not given - the form itself asks for it",
      signed_in(client) == SAM)
client, _ = sign_in("   ", "sam", "wrong")
check("and is no way round the password", signed_in(client) is None)

for _ in range(10):
    sign_in("Dosa Point", "sam", "TillPass-2026")
client, response = sign_in("Mom's Café", "sam", "TillPass-2026")
check("a wrong name counts as a failed attempt, so guessing names locks out too",
      signed_in(client) is None
      and any("Too many" in m for m in said_on(response) + flashes(client)),
      said_on(response))
db("DELETE FROM login_attempts")

client, _ = sign_in(None, "sam", "TillPass-2026")
check("a form from before, with no name field at all, still signs in",
      signed_in(client) == SAM)
client, response = sign_in(None, "sam", "wrong")
check("and says what it always said when wrong",
      "Invalid username or password." in said_on(response), said_on(response))


# =====================================================================
print("\n=== 3. Remember me keeps the name of the place ===")
# =====================================================================
client, response = sign_in("Mom's Café", "sam", "TillPass-2026", remember="1")
cookies = response.headers.getlist("Set-Cookie")
kept = [c for c in cookies if c.startswith("cafora_cafe=")]
check("the cafe's name is kept for the sign-in page",
      kept and "HttpOnly" in kept[0] and "Path=/login" in kept[0], kept)
client.get("/logout")
page = text(client.get("/login"))
check("and filled in next time", 'value="Mom&#39;s Café"' in page
      or "value=\"Mom's Café\"" in page, re.findall(r'name="cafe_name"[^>]*', page))
client, response = sign_in("Mom's Café", "sam", "TillPass-2026")
check("signing in without the box forgets it",
      any(c.startswith("cafora_cafe=;") or "cafora_cafe=\"\"" in c
          or ("cafora_cafe=" in c and "Max-Age=0" in c)
          for c in response.headers.getlist("Set-Cookie")))


# =====================================================================
print("\n=== 4. Forgot password is for the cafe's admin ===")
# =====================================================================
db("UPDATE users SET phone_number = '9123456780' WHERE user_id = ?", (SAM,))
db("UPDATE users SET phone_number = '9876543210' WHERE user_id = ?", (MAYA,))
before = db("SELECT password_hash FROM users WHERE user_id = ?", (SAM,))
guest = app.test_client()
guest.post("/forgot-password", data={
    "username": "sam", "full_name": "Sam Cashier", "phone_country": "India",
    "phone_number": "9123456780", "new_password": "Stolen-Pass-1",
    "confirm_password": "Stolen-Pass-1"})
said = flashes(guest)
check("a cashier cannot reset their own password here",
      db("SELECT password_hash FROM users WHERE user_id = ?", (SAM,)) == before)
check("and is told, kindly, to ask their admin",
      any("your café's admin" in m and "User Management" in m for m in said), said)
guest.post("/forgot-password", data={
    "username": "sam", "full_name": "Wrong Name", "phone_country": "India",
    "phone_number": "9123456780", "new_password": "Stolen-Pass-1",
    "confirm_password": "Stolen-Pass-1"})
said = flashes(guest)
check("details that do not match say nothing about the account's role",
      not any("your café's admin" in m and "Staff" in m for m in said), said)
guest.post("/forgot-password", data={
    "username": "maya", "full_name": "Maya Owner", "phone_country": "India",
    "phone_number": "9876543210", "new_password": "Owner-New-Pass-9",
    "confirm_password": "Owner-New-Pass-9"})
said = flashes(guest)
# Numbers off again, so signing in is not a one-time code away.
db("UPDATE users SET phone_number = NULL")
client, _ = sign_in("Mom's Café", "maya", "Owner-New-Pass-9")
check("the owner can", signed_in(client) == MAYA, said)
check("the page says who it is for",
      "For the café's admin." in text(app.test_client().get("/forgot-password")))
db("UPDATE users SET phone_number = NULL")


# =====================================================================
print("\n=== 5. The eye on User Management ===")
# =====================================================================
admin, _ = sign_in("Mom's Café", "maya", "Owner-New-Pass-9")


def reveal(client, user_id, token=True):
    headers = {"X-CSRFToken": csrf(client)} if token else {}
    response = client.post("/users/%d/password" % user_id, headers=headers)
    return response.status_code, response.get_json(silent=True), response


stored = db("SELECT password_view FROM users WHERE user_id = ?", (SAM,))[0][0]
check("a password an admin set is kept - sealed, not as typed",
      stored and "TillPass-2026" not in stored and len(stored) > 40, stored)
status, said, response = reveal(admin, SAM)
check("the owner's eye shows what was set",
      status == 200 and said == {"ok": True, "password": "TillPass-2026"}, said)
check("and it is never cached", response.headers.get("Cache-Control") == "no-store")
page = text(admin.get("/users"))
check("the list has a Password column with an eye for it",
      "<th>Password</th>" in page and 'data-reveal="%d"' % SAM in page)
check("but the password itself is not written into the page",
      "TillPass-2026" not in page)

asha, _ = sign_in("Mom's Café", "asha", "AdminPass-77")
status, said, _ = reveal(asha, SAM)
check("a manager - the most anybody but the admin can be - cannot see it",
      status in (302, 403) and "TillPass" not in str(said), (status, said))
status, said, _ = reveal(admin, MAYA)
check("the owner's own password was never kept - they chose it",
      said and said["ok"] is False and "Their own" in said["message"], said)
check("and the list says so", re.search(
    r'data-user-id="%d".*?Their own' % MAYA, page, re.S) is not None)

status, said, _ = reveal(admin, SAM, token=False)
check("asking without the form token is refused", status == 400)
dev, _ = sign_in("Dosa Point", "dev", "Brew-Latte-42")
status, said, _ = reveal(dev, SAM)
check("another cafe's admin cannot see it", status == 404
      and "TillPass" not in str(said), (status, said))
cashier, _ = sign_in("Mom's Café", "sam", "TillPass-2026")
status, said, _ = reveal(cashier, ASHA)
check("nor can a cashier", status in (302, 403) and "AdminPass" not in str(said),
      (status, said))

admin.post("/users/%d/edit" % SAM, data={
    "full_name": "Sam Cashier", "role": "cashier", "is_active": "1",
    "phone_number": "", "email": "", "password": "Reset-By-Owner-1",
    "_csrf_token": csrf(admin)})
check("a new one set by an admin from Edit is the one shown",
      reveal(admin, SAM)[1] == {"ok": True, "password": "Reset-By-Owner-1"})

cashier, _ = sign_in("Mom's Café", "sam", "Reset-By-Owner-1")
cashier.post("/account/password", data={
    "current_password": "Reset-By-Owner-1", "new_password": "Sams-Own-Secret-5",
    "confirm_password": "Sams-Own-Secret-5", "_csrf_token": csrf(cashier)})
client, _ = sign_in("Mom's Café", "sam", "Sams-Own-Secret-5")
check("a cashier can change their own password", signed_in(client) == SAM)
check("and then it is theirs: nothing is kept to show",
      db("SELECT password_view FROM users WHERE user_id = ?", (SAM,)) == [(None,)]
      and reveal(admin, SAM)[1]["ok"] is False)

admin.post("/users/%d/edit" % MAYA, data={
    "full_name": "Maya Owner", "role": "admin", "is_active": "1",
    "phone_number": "", "email": "", "password": "Own-Choice-Mx-3",
    "_csrf_token": csrf(admin)})
check("an admin changing their own password from Edit keeps nothing either",
      db("SELECT password_view FROM users WHERE user_id = ?", (MAYA,)) == [(None,)])

db("UPDATE users SET password_view = 'not-a-sealed-value' WHERE user_id = ?", (ASHA,))
check("a kept value that no longer opens (the key changed) is simply not shown",
      reveal(admin, ASHA)[1]["ok"] is False)


# =====================================================================
print("\n=== 6. An account the owner switched off ===")
# =====================================================================
DEACTIVATED = "deactivated by your café's admin"
db("DELETE FROM login_attempts")
still_in, _ = sign_in("Mom's Café", "sam", "Sams-Own-Secret-5")
admin.post("/users/%d/toggle" % SAM, data={"_csrf_token": csrf(admin)})
check("the owner switches Sam off",
      db("SELECT is_active FROM users WHERE user_id = ?", (SAM,)) == [(0,)])

import html as _html                     # noqa: E402

before = db("SELECT * FROM login_attempts")
_, response = sign_in("Mom's Café", "sam", "Sams-Own-Secret-5")
said = [_html.unescape(m) for m in said_on(response)]
check("Sam, with the right café, name and password, is told the account "
      "was switched off", any(DEACTIVATED in m for m in said), said)
check("and is not let in", not response.headers.get("Location"))
check("telling Sam why is not counted against Sam as a guess",
      db("SELECT * FROM login_attempts") == before)
for cafe, password, why in (("Mom's Café", "wrong-password", "a wrong password"),
                            ("Dosa Point", "Sams-Own-Secret-5", "another café's name"),
                            (None, "Sams-Own-Secret-5", "no café name at all")):
    _, response = sign_in(cafe, "sam", password)
    said = [_html.unescape(m) for m in said_on(response)]
    check("with %s, only the usual message - nothing about the account" % why,
          said and not any(DEACTIVATED in m for m in said), said)

page = still_in.get("/billing", follow_redirects=True)
check("already signed in, Sam is signed out on the next page and told why",
      signed_in(still_in) is None
      and DEACTIVATED in page.get_data(as_text=True).replace("&#39;", "'"))


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
