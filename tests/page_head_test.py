"""
What a crawler and a link preview see: robots.txt, and the description on
every page.

Both of these were reported by an outside site checker rather than by
anything here, which is the reason for this file. There was no
/robots.txt at all: asking for it fell through to the sign-in guard,
which answered a redirect to /login, and the checker read that page's
markup as robots directives - three hundred and sixty-seven lines of
HTML, three hundred and sixty-seven "errors". Nothing was broken; there
was no file. A test that only asked whether the route answers would have
passed the whole time, so these ask what it answers with.

The descriptions are the line under the link when an address is shared,
and a QR ordering page gets shared by its nature. The trap there is not
a missing tag but a useless one: an attribute value keeps its whitespace
exactly as written, so a description wrapped across lines in the
template arrives with a newline and a run of indentation inside the
sentence.

No network and no database - the app runs on the SQLite stand-in.

Run with:  python tests/page_head_test.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "page-head-secret"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim            # noqa: E402
mysql_shim.install()

import logging                          # noqa: E402
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import app as application               # noqa: E402

app = application.app
app.config["TESTING"] = True

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


CAFE = "Rahul Cafe"

print("\n=== 0. Seeding a cafe ===")
owner = app.test_client()
owner.post("/register", data={
    "cafe_name": CAFE, "full_name": "Owner", "username": "owner",
    "phone_country": "India", "phone_number": "9876500001",
    "password": "password123", "confirm_password": "password123",
}, follow_redirects=True)
mysql_shim.skip_tour()
landed = owner.get("/dashboard").status_code
assert landed == 200, "could not sign in to seed: /dashboard gave %s" % landed
print("  seeded and signed in")


# ==========================================
# 1. robots.txt
# ==========================================
print("\n=== 1. robots.txt ===")

stranger = app.test_client()
reply = stranger.get("/robots.txt")

check("a signed-out crawler gets the file, not a redirect to sign in",
      reply.status_code == 200,
      "status %s, location %r" % (reply.status_code,
                                  reply.headers.get("Location")))

check("served as plain text", reply.mimetype == "text/plain",
      "mimetype %r" % reply.mimetype)

body = reply.get_data(as_text=True)

# The original failure: the checker was reading HTML as directives.
check("it is not a web page",
      "<html" not in body.lower() and "<!doctype" not in body.lower()
      and "<div" not in body.lower(),
      "there is markup in here: %r" % body[:120])

lines = body.splitlines()
offenders = [line for line in lines
             if line.strip() and not line.startswith("#") and ":" not in line]
check("every line is a comment, a directive or blank", not offenders,
      "%d line(s) are neither, e.g. %r" % (len(offenders), offenders[:3]))

fields = set()
for line in lines:
    if line.strip() and not line.startswith("#"):
        fields.add(line.split(":", 1)[0].strip().lower())
check("only fields a crawler understands",
      fields <= {"user-agent", "disallow", "allow", "sitemap", "crawl-delay"},
      "unexpected: %s" % sorted(fields - {"user-agent", "disallow", "allow",
                                          "sitemap", "crawl-delay"}))

check("it says who it is addressing", "user-agent" in fields,
      "no User-agent line, so no rule below it applies")

check("a cafe's ordering pages are kept out", "Disallow: /m/" in body,
      "the QR ordering address is not disallowed")

for private in ("/billing", "/reports", "/users", "/api/"):
    check("%s is kept out" % private, "Disallow: %s" % private in body,
          "not disallowed")

# Whatever is disallowed, the two pages somebody arriving new needs must
# not be - it is easy to write a broad rule and hide the front door.
disallowed = [line.split(":", 1)[1].strip() for line in lines
              if line.lower().startswith("disallow:")]
for public in ("/login", "/register", "/static/css/style.css"):
    blocked = [rule for rule in disallowed
               if rule and rule != "/" and public.startswith(rule)]
    check("%s is still reachable" % public, not blocked,
          "hidden by %r" % blocked)

check("nothing disallows the whole site", "/" not in disallowed,
      "a bare Disallow: / hides everything")

check("a signed-in browser gets the same file",
      owner.get("/robots.txt").get_data(as_text=True) == body,
      "the answer depends on who is asking")


# ==========================================
# 2. A description on every page
# ==========================================
print("\n=== 2. A description on every page ===")

TAG = re.compile(r'<meta\s+name="description"\s+content="(.*?)"\s*/?>', re.S)


def description_of(client, path):
    reply = client.get(path, follow_redirects=True)
    html = reply.get_data(as_text=True)
    return reply.status_code, TAG.findall(html), html


SIGNED_OUT = [("/login", "sign in"), ("/register", "register"),
              ("/forgot-password", "reset a password")]

SIGNED_IN = [("/dashboard", "dashboard"), ("/billing", "billing"),
             ("/kitchen", "kitchen"), ("/inventory", "stock"),
             ("/foods", "menu"), ("/reports", "reports"),
             ("/users", "users"), ("/orders/add", "new order"),
             ("/account/password", "change password"),
             ("/settings/branding", "branding")]

seen = {}
for path, what in SIGNED_OUT + SIGNED_IN:
    client = stranger if (path, what) in SIGNED_OUT else owner
    status, found, _ = description_of(client, path)
    if status != 200:
        check("the %s page answers" % what, False, "status %s" % status)
        continue

    check("the %s page has exactly one description" % what, len(found) == 1,
          "found %d" % len(found))
    if len(found) != 1:
        continue

    value = found[0]
    seen[path] = value

    check("the %s description is one clean line" % what,
          value == " ".join(value.split()) and value == value.strip(),
          # This is the one that was actually wrong: wrapped in the
          # template, so a newline and a run of indentation arrived
          # inside the sentence.
          "it carries its own whitespace: %r" % value)

    check("the %s description says something" % what,
          20 <= len(value) <= 165 and value.endswith("."),
          "%d characters: %r" % (len(value), value))

    check("the %s description is not a template left unrendered" % what,
          "{{" not in value and "{%" not in value and "None" not in value,
          "%r" % value)

check("a page inside the app names the cafe",
      CAFE in seen.get("/dashboard", ""),
      "dashboard reads %r" % seen.get("/dashboard"))

check("the sign-in page does not, having no cafe yet",
      CAFE not in seen.get("/login", ""),
      # Branding is withheld before sign-in on purpose, so that one
      # tenant's name cannot appear on the shared page.
      "login reads %r" % seen.get("/login"))

check("the pages outside the layout say their own thing, not the default",
      len({seen.get("/login"), seen.get("/register"),
           seen.get("/forgot-password")}) == 3,
      "sign in, register and reset do not have three distinct descriptions")

check("a page inside the layout inherits the default",
      seen.get("/billing") == seen.get("/kitchen") != None,
      "billing %r vs kitchen %r" % (seen.get("/billing"),
                                    seen.get("/kitchen")))


# ==========================================
# 3. The customer's own pages, which get shared by their nature
# ==========================================
print("\n=== 3. The customer's pages ===")

link = owner.get("/settings/qr", follow_redirects=True).get_data(as_text=True)
token = re.search(r"/m/([A-Za-z0-9_-]{8,})", link)
check("the cafe has an ordering address to test", bool(token),
      "no /m/<token> found on the QR settings page")

if token:
    menu = stranger.get("/m/%s" % token.group(1), follow_redirects=True)
    html = menu.get_data(as_text=True)
    found = TAG.findall(html)

    check("the ordering page has a description", len(found) == 1,
          "found %d" % len(found))
    if found:
        check("it names the cafe, so a pasted link says whose it is",
              CAFE in found[0], "%r" % found[0])
        check("and it is one clean line",
              found[0] == " ".join(found[0].split()), "%r" % found[0])

    # noindex is the part that binds; the description is only for a
    # messaging app's preview.
    check("the ordering page still tells a crawler to stay out",
          'name="robots"' in html and "noindex" in html,
          "noindex is missing from the QR page")

    # The page a customer holds up at the counter: their own orders of
    # the day. Reached from the menu, so it gets shared the same way.
    mine = stranger.get("/m/%s/orders" % token.group(1),
                        follow_redirects=True)
    mine_html = mine.get_data(as_text=True)
    mine_found = TAG.findall(mine_html)

    check("the customer's own list answers", mine.status_code == 200,
          "status %s" % mine.status_code)

    check("it has one description, on one clean line",
          len(mine_found) == 1
          and mine_found[0] == " ".join(mine_found[0].split()),
          "found %d: %r" % (len(mine_found), mine_found[:1]))

    check("and it stays out of a search engine too",
          'name="robots"' in mine_html and "noindex" in mine_html,
          "a list of somebody's orders is indexable")


# ==========================================
# 4. The tab: the Refero mark and name
# ==========================================
print("\n=== 4. What the browser's tab shows ===")
# There was no icon at all. /favicon.ico answered "no content" and no
# page named one, so every tab showed the browser's blank globe beside
# the title - on the sign-in page, on the till, on a customer's phone.

ICON = re.compile(r'<link rel="icon" href="([^"]+)"([^>]*)>')
TITLE = re.compile(r"<title>(.*?)</title>", re.S)

pages = [(stranger, path, what) for path, what in SIGNED_OUT]
pages += [(owner, path, what) for path, what in SIGNED_IN]
if token:
    pages += [(stranger, "/m/%s" % token.group(1), "customer's menu"),
              (stranger, "/m/%s/orders" % token.group(1),
               "customer's own orders")]
pages.append((stranger, "/m/no-such-cafe-token", "dead QR code"))

missing, titles = [], {}
for client, path, what in pages:
    html = client.get(path, follow_redirects=True).get_data(as_text=True)
    hrefs = [href for href, _rest in ICON.findall(html)]
    if not (any("favicon.svg" in h for h in hrefs)
            and any("favicon.ico" in h for h in hrefs)):
        missing.append(what)
    found = TITLE.findall(html)
    titles[what] = " ".join(found[0].split()) if found else ""

check("every page names the icon for its tab", not missing,
      "no icon on: %s" % ", ".join(missing))

check("the pages inside the app say Refero on the tab",
      all(titles[what].endswith("· Refero") for _p, what in SIGNED_IN),
      "; ".join("%s: %r" % (what, titles[what]) for _p, what in SIGNED_IN
                if not titles[what].endswith("· Refero")))

check("so do the ones before anybody has signed in",
      all("Refero" in titles[what] for _p, what in SIGNED_OUT),
      "; ".join("%s: %r" % (what, titles[what]) for _p, what in SIGNED_OUT))

check("and each tab still says which page it is",
      len({titles[what] for _p, what in SIGNED_OUT + SIGNED_IN}) ==
      len(SIGNED_OUT + SIGNED_IN),
      "two pages share a title, so two tabs cannot be told apart: %s"
      % sorted("%s = %r" % (what, titles[what])
               for _p, what in SIGNED_OUT + SIGNED_IN
               if [titles[w] for _q, w in SIGNED_OUT + SIGNED_IN]
               .count(titles[what]) > 1))

svg = stranger.get("/static/icons/favicon.svg")
svg_text = svg.get_data(as_text=True)
check("the icon itself is served, as an SVG",
      svg.status_code == 200 and svg.mimetype == "image/svg+xml"
      and svg_text.lstrip().startswith("<svg"),
      "status %s, type %r" % (svg.status_code, svg.mimetype))

# The tab and the sidebar are drawn from the same shapes. Built by a
# script from the mark in the template; if somebody redraws one and not
# the other, this is where it shows.
mark_source = io.open(os.path.join(ROOT, "templates", "_brand_mark.html"),
                      encoding="utf-8").read()
shapes = re.findall(r'<path d="([^"]+)"', mark_source)
check("and it is the same mark the sidebar draws",
      shapes and all(d in svg_text for d in shapes),
      "%d of the mark's %d shapes are in the tab icon"
      % (sum(d in svg_text for d in shapes), len(shapes)))

root_icon = stranger.get("/favicon.ico")
data = root_icon.get_data()
check("/favicon.ico is a real icon, not an empty answer",
      root_icon.status_code == 200 and data[:4] == b"\x00\x00\x01\x00",
      "status %s, %d bytes, starts %r"
      % (root_icon.status_code, len(data), data[:4]))

count = int.from_bytes(data[4:6], "little") if len(data) >= 6 else 0
entries = [data[6 + 16 * n: 22 + 16 * n] for n in range(count)]
sizes = sorted(entry[0] for entry in entries)
pictures_ok = all(
    data[int.from_bytes(e[12:16], "little"):
         int.from_bytes(e[12:16], "little") + 8] == b"\x89PNG\r\n\x1a\n"
    for e in entries)
check("with a picture for each size a tab or a bookmark asks for",
      sizes == [16, 32, 48] and pictures_ok,
      "sizes %s, every picture readable: %s" % (sizes, pictures_ok))

check("answered to anybody, signed in or not",
      "Location" not in root_icon.headers,
      "it redirects to %s" % root_icon.headers.get("Location"))


print("\n=== What a site scan reads: llms.txt, canonical, landmarks ===")
# A scan reported llms.txt with no H1 and no links - it was reading the
# sign-in page the missing file redirected to - and pages with no main
# landmark and no level-one heading.
_llms = app.test_client().get("/llms.txt", headers={"X-Forwarded-Proto": "https,http"})
_text = _llms.get_data(as_text=True)
check("llms.txt is answered signed out, as plain Markdown",
      _llms.status_code == 200 and _llms.mimetype == "text/plain"
      and "Location" not in _llms.headers, (_llms.status_code, _llms.mimetype))
check("with an H1 title", _text.startswith("# Refero\n"), _text[:40])
check("and links to follow, as the visitor's https addresses",
      "[Sign in](https://localhost/login)" in _text
      and "(https://localhost/robots.txt)" in _text, _text)
check("robots.txt points crawlers at it", "Allow: /llms.txt" in
      app.test_client().get("/robots.txt").get_data(as_text=True))

for _path in ("/.well-known/ai-catalog.json", "/ai-catalog.json"):
    _cat = app.test_client().get(_path)
    _json = _cat.get_json(silent=True)
    check("%s is a valid, empty agent catalog, not a sign-in page" % _path,
          _cat.status_code == 200 and _cat.mimetype == "application/json"
          and _json == {"specVersion": "1.0", "host": {"displayName": "Refero"},
                        "entries": []}, (_cat.status_code, _json))

for _path in ("/login", "/register", "/forgot-password"):
    _page = app.test_client().get(_path + "?next=/",
                                  headers={"X-Forwarded-Proto": "https,http"}
                                  ).get_data(as_text=True)
    check("%s names one canonical address, without the query" % _path,
          ('<link rel="canonical" href="https://localhost%s">' % _path) in _page)
    check("%s has one main landmark and one level-one heading" % _path,
          _page.count('role="main"') + _page.count("<main") == 1
          and len(re.findall(r"<h1[\s>]", _page)) == 1)


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
