"""
"That username is already taken" - said while it is typed.

Register and Add User ask the server whether a username is free as it is
typed, and offer free ones close to it when it is not. The question says
which usernames exist, which the sign-in screen is careful never to do, so
it is held to a pace a person typing never reaches.

Also here, the rest of the account screens' help that lives in the page:
the password-strength meter is on every field where a password is chosen,
the sign-in screen offers the browser's saved password for "remember me",
and search keeps a Back link and a way to clear what was searched.

No browser, no database - the app runs on the SQLite stand-in. What the
page's script does with all of this is driven in a browser by
account_fields_browser_test.py.

Run with:  python tests/username_check_test.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "username-check-secret"
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


def text(response):
    return response.get_data(as_text=True)


owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Name Cafe", "full_name": "Nina Owner", "username": "nina",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
for taken in ("nina1", "nina_cafe"):
    owner.post("/users/add", data={
        "full_name": "Taken " + taken, "username": taken, "role": "cashier",
        "phone_number": "", "password": "password123", "_csrf_token": csrf(owner)})


def ask(client, name, source="10.0.0.1"):
    response = client.get("/api/username-check", query_string={"u": name},
                          environ_base={"REMOTE_ADDR": source})
    return response.status_code, response.get_json()


# =====================================================================
print("\n=== 1. What it says ===")
# =====================================================================
status, said = ask(owner, "nina")
check("a username in use is taken", status == 200 and said["state"] == "taken"
      and said["ok"] is False, said)
check("and says to choose another", "already taken" in said["message"]
      and "choose another" in said["message"], said["message"])
check("offering free ones close to it - not the ones already in use",
      said["suggestions"] == ["nina2", "nina01"], said["suggestions"])
check("which it names in the message", "nina2 or nina01" in said["message"])
status, said = ask(owner, "brand_new_name")
check("a free one is free", said == {"ok": True, "state": "free",
                                     "message": "That username is free."}, said)
status, said = ask(owner, "  nina  ")
check("spaces around it do not hide that it is taken", said["state"] == "taken")
status, said = ask(owner, "ab")
check("too short is said before anything is looked up",
      said["state"] == "bad" and "3 characters" in said["message"])
status, said = ask(owner, "x" * 81)
check("and too long", said["state"] == "bad" and "80" in said["message"])
status, said = ask(owner, "")
check("nothing typed yet is simply empty", said["state"] == "empty")
status, said = ask(owner, "nina'; DROP TABLE users; --")
check("a name full of SQL is just a free name",
      said["state"] == "free"
      and mysql_shim._DB.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 3)
status, said = ask(owner, "nin%")
check("a wildcard matches nothing but itself", said["state"] == "free", said)


# =====================================================================
print("\n=== 2. Who can ask ===")
# =====================================================================
stranger = app.test_client()
status, said = ask(stranger, "nina", source="10.0.0.2")
check("someone registering, not signed in, can ask",
      status == 200 and said["state"] == "taken")
answer = stranger.get("/api/username-check?u=nina",
                      environ_base={"REMOTE_ADDR": "10.0.0.2"})
check("the answer is JSON, not a page", answer.mimetype == "application/json")


# =====================================================================
print("\n=== 3. Held to a person's pace ===")
# =====================================================================
statuses = [ask(stranger, "guess%d" % n, source="10.0.0.9")[0] for n in range(45)]
check("forty questions a minute are answered", statuses[:40] == [200] * 40, statuses)
check("the next are turned away", statuses[40:] == [429] * 5, statuses[40:])
status, said = ask(stranger, "nina", source="10.0.0.9")
check("saying it will be checked on submit, without saying whether it is taken",
      status == 429 and said["state"] == "wait" and "taken" not in said["message"])
status, said = ask(stranger, "nina", source="10.0.0.10")
check("somebody else, elsewhere, is not held up by it",
      status == 200 and said["state"] == "taken")
registered = stranger.post("/register", data={
    "cafe_name": "Late Cafe", "full_name": "Lee Late", "username": "nina",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, environ_base={"REMOTE_ADDR": "10.0.0.9"})
check("and the form's own check still refuses a taken name when it is sent",
      mysql_shim._DB.execute("SELECT COUNT(*) FROM users WHERE username = 'nina'")
      .fetchone()[0] == 1)


# =====================================================================
print("\n=== 4. Where the help is wired in ===")
# =====================================================================
register = text(app.test_client().get("/register"))
check("Register checks the username as it is typed",
      re.search(r'name="username"[^>]*data-username-check', register, re.S) is not None)
check("and measures the password",
      re.search(r'name="password"[^>]*data-strength', register, re.S) is not None)
check("with the script that does both",
      "js/account-fields.js" in register)
add_user = text(owner.get("/users/add"))
check("Add User checks the username",
      re.search(r'name="username"[^>]*data-username-check', add_user, re.S) is not None)
check("and measures the password",
      re.search(r'name="password"[^>]*data-strength', add_user, re.S) is not None)
check("and so does every page inside the app", "js/account-fields.js" in add_user)
change = text(owner.get("/account/password"))
check("Change Password measures the new password only",
      re.search(r'name="new_password"[^>]*data-strength', change) is not None
      and re.search(r'name="current_password"[^>]*data-strength', change) is None)
forgot = text(app.test_client().get("/forgot-password"))
check("and so does the reset",
      re.search(r'name="new_password"[^>]*data-strength', forgot, re.S) is not None
      and "js/account-fields.js" in forgot)
edit_user = text(owner.get("/users/%d/edit" % mysql_shim._DB.execute(
    "SELECT user_id FROM users WHERE username = 'nina1'").fetchone()[0]))
check("editing someone does not check their own name against itself",
      'value="nina1" disabled' in edit_user and "data-username-check" not in edit_user)
check("but a new password for them is measured",
      re.search(r'name="password"[^>]*data-strength', edit_user, re.S) is not None)


# =====================================================================
print("\n=== 5. Remember me, and search ===")
# =====================================================================
login = text(app.test_client().get("/login"))
check("the sign-in screen asks the browser for a saved password",
      'navigator.credentials.get({password: true, mediation: "optional"})' in login)
check("and offers it one to save only when Remember me is ticked",
      "new PasswordCredential" in login and "remember" in login)
check("the app itself keeps no password for it",
      "localStorage" not in login.split("PasswordCredential")[0][-2000:]
      or "password" not in re.sub(r"\s", "", login.split("PasswordCredential")[0][-2000:])
      .split("localStorage")[-1][:200])
search = text(owner.get("/search?q=latte"))
check("search has a way back", 'class="search-back" data-back' in search)
check("a way to clear what was typed", 'id="searchAgainClear"' in search)
check("and keeps recent searches, each with a delete, per person",
      'id="searchRecent"' in search and 'id="recentClearAll"' in search
      and '"cafora.searches." + who' in search)


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
