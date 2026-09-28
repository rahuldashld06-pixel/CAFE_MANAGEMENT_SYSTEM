"""
Pages and files go out gzipped - and the form token stays safe for it.

A phone on mobile data spent most of its wait on bytes: New Order is well
over 100KB of HTML and the stylesheet about as much. Every page, script
and stylesheet is now compressed for any browser that says it can take
it, and left alone for one that does not.

Compressing a page that holds a secret and repeats something typed by a
stranger leaks the secret through the page's size (BREACH). So the form
token on each page is masked with fresh random bytes: never the same
twice on the page, and still accepted when it comes back.

No browser, no database - the app runs on the SQLite stand-in.

Run with:  python tests/compression_test.py
"""
import gzip
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.chdir(ROOT)

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "compression-secret"
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


def session_token(client):
    with client.session_transaction() as sess:
        return sess.get("_csrf_token", "")


GZIP = {"Accept-Encoding": "gzip, deflate, br"}

owner = app.test_client()
owner.post("/register", data={
    "cafe_name": "Zip Cafe", "full_name": "Zara Owner", "username": "zara",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)


# =====================================================================
print("\n=== 1. A page, gzipped for a browser that takes it ===")
# =====================================================================
plain = owner.get("/orders/add")
zipped = owner.get("/orders/add", headers=GZIP)
check("a browser that takes gzip gets it", zipped.headers.get("Content-Encoding") == "gzip")
check("and the page is well under half the size",
      len(zipped.get_data()) * 2 < len(plain.get_data()),
      "%d bytes against %d" % (len(zipped.get_data()), len(plain.get_data())))
opened = gzip.decompress(zipped.get_data()).decode("utf-8")
def same_page(html):
    """The page without what is fresh on every response: token and nonce."""
    html = re.sub(r'csrfToken: "[^"]*"', 'csrfToken: ""', html)
    return re.sub(r'nonce="[^"]*"', 'nonce=""', html)


check("it opens to the whole page", opened.rstrip().endswith("</html>")
      and 'id="page-view"' in opened
      and same_page(opened) == same_page(plain.get_data(as_text=True)),
      repr(opened[-120:]))
check("its length is the compressed length",
      int(zipped.headers["Content-Length"]) == len(zipped.get_data()))
check("and caches are told it depends on what the browser takes",
      "Accept-Encoding" in zipped.headers.get("Vary", ""))
check("a browser that does not say so gets it as it is",
      plain.headers.get("Content-Encoding") is None
      and "</html>" in plain.get_data(as_text=True))
check("the security headers are all still there",
      re.sub(r"'nonce-[^']*'", "", zipped.headers.get("Content-Security-Policy", ""))
      == re.sub(r"'nonce-[^']*'", "", plain.headers.get("Content-Security-Policy", ""))
      and "'nonce-" in zipped.headers.get("Content-Security-Policy", "")
      and zipped.headers.get("X-Frame-Options") == plain.headers.get("X-Frame-Options"))


# =====================================================================
print("\n=== 2. Stylesheets and scripts ===")
# =====================================================================
css_plain = owner.get("/static/css/style.css")
css_zip = owner.get("/static/css/style.css", headers=GZIP)
check("the stylesheet is gzipped", css_zip.headers.get("Content-Encoding") == "gzip")
check("and opens to the same file",
      gzip.decompress(css_zip.get_data()) == css_plain.get_data())
again = owner.get("/static/css/style.css", headers=GZIP)
check("asked again, the same bytes (compressed once and kept)",
      again.get_data() == css_zip.get_data())
js = owner.get("/static/js/instant.js", headers=GZIP)
check("a script is gzipped", js.headers.get("Content-Encoding") == "gzip")
etag = css_plain.headers.get("ETag")
if etag:
    unchanged = owner.get("/static/css/style.css",
                          headers=dict(GZIP, **{"If-None-Match": etag}))
    check("a file the browser already has is still 'not modified'",
          unchanged.status_code == 304 and not unchanged.get_data())
icon = owner.get("/static/icons/app-192.png", headers=GZIP)
check("a picture, already compressed, is left alone",
      icon.status_code == 200 and icon.headers.get("Content-Encoding") is None,
      (icon.status_code, icon.headers.get("Content-Encoding")))
health = app.test_client().get("/healthz", headers=GZIP)
check("a reply too small to gain anything is left alone",
      health.headers.get("Content-Encoding") is None)


# =====================================================================
print("\n=== 3. The form token is masked on every page ===")
# =====================================================================
raw = session_token(owner)
TOKEN = r'name="_csrf_token" value="([^"]+)"'
page_one = re.findall(TOKEN, owner.get("/categories/add").get_data(as_text=True))
page_two = re.findall(TOKEN, owner.get("/categories/add").get_data(as_text=True))
check("a page carries a token", page_one and page_two)
check("but never the session's own", raw not in page_one + page_two)
check("and a different one every time the page is drawn",
      page_one[0] != page_two[0])


def add_category(name, token, header=False):
    data = {"category_name": name, "description": ""}
    headers = {}
    if header:
        headers["X-CSRFToken"] = token
    else:
        data["_csrf_token"] = token
    response = owner.post("/categories/add", data=data, headers=headers)
    kept = mysql_shim._DB.execute(
        "SELECT COUNT(*) FROM categories WHERE category_name = ?", (name,)).fetchone()[0]
    return response.status_code, kept


check("a masked token from the page is accepted",
      add_category("From page", page_one[0]) == (302, 1))
check("so is an older one from the same sign-in",
      add_category("Older page", page_two[0]) == (302, 1))
check("and one sent as a header, the way the app's scripts send it",
      add_category("By header", application.mask_csrf(raw), header=True) == (302, 1))
check("the plain token is still accepted",
      add_category("Plain", raw) == (302, 1))

forged = application.mask_csrf("x" * len(raw))
check("a masked token of some other secret is refused",
      add_category("Forged", forged) == (400, 0))
flipped = page_one[0][:-2] + ("A" if page_one[0][-2] != "A" else "B") + page_one[0][-1]
check("so is a masked token with a character changed",
      add_category("Tampered", flipped) == (400, 0))
check("so is one cut short", add_category("Short", page_one[0][:20]) == (400, 0))
check("so is text that is not base64", add_category("Junk", "@@@@") == (400, 0))
check("and text that is not even ASCII is refused, not a crash",
      add_category("Unicode", "tökén") == (400, 0))
check("and no token at all", add_category("None", "") == (400, 0))

other = app.test_client()
other.post("/register", data={
    "cafe_name": "Other Cafe", "full_name": "Omar Owner", "username": "omar",
    "phone_number": "", "password": "password123",
    "confirm_password": "password123"}, follow_redirects=True)
theirs = re.findall(TOKEN, other.get("/categories/add").get_data(as_text=True))[0]
check("a token from somebody else's page is refused",
      add_category("Theirs", theirs) == (400, 0))

check("the app's scripts get a masked token too",
      raw not in owner.get("/dashboard").get_data(as_text=True)
      and re.search(r'csrfToken: "[A-Za-z0-9_-]{40,}"',
                    owner.get("/dashboard").get_data(as_text=True)) is not None)


# =====================================================================
print("\n=== 4. The rest of what makes a phone quicker ===")
# =====================================================================
def self_hosted_fonts(html):
    """The typefaces come from this site, swap in, and ask nobody else."""
    return ("@font-face" in html and "font-display: swap" in html
            and "/static/fonts/manrope-latin.woff2" in html
            and "fonts.googleapis" not in html and "fonts.gstatic" not in html)


page = owner.get("/orders/add").get_data(as_text=True)
# From Google they were a DNS lookup and a TLS handshake to two more hosts
# before a letter could change - and a crawler read the bare hosts named
# in preconnect hints as broken links.
check("the web fonts come from this site, and never hold up the first paint",
      self_hosted_fonts(page))
check("the reading face is fetched early",
      re.search(r'<link rel="preload" as="font" type="font/woff2" crossorigin\s+'
                r'href="/static/fonts/manrope-latin\.woff2', page) is not None)
menu = app.test_client().get("/m/%s" % application.get_public_token(1)).get_data(as_text=True)
check("so on the customer's menu", self_hosted_fonts(menu))
check("and on the sign-in screen",
      self_hosted_fonts(app.test_client().get("/login").get_data(as_text=True)))
font = owner.get("/static/fonts/manrope-latin.woff2")
check("the font file itself is served, and kept a long time",
      font.status_code == 200 and len(font.get_data()) > 10000)

# The picture beside the sign-in form is the largest thing on it, so it
# is what Largest Contentful Paint times. It was a CSS background, found
# late, with a preload in <head> standing in for it that had drifted to
# a different photo altogether.
for label, path in (("sign-in", "/login"), ("register", "/register")):
    html = app.test_client().get(path).get_data(as_text=True)
    photo = re.search(r'<img class="auth-visual__photo"[^>]*>', html, re.S)
    tag = photo.group(0) if photo else ""
    check("the %s picture is an image in the page itself" % label,
          photo is not None and 'class="auth-visual"' in html, tag)
    check("fetched first, and never lazily",
          'fetchpriority="high"' in tag and "loading=" not in tag, tag)
    check("with no preload of some other picture beside it",
          'rel="preload" as="image"' not in html and "--login-photo" not in html)
    body = html[html.index("<body"):]
    first_img = re.search(r"<img[^>]*>", body)
    check("and the first image the page asks for",
          first_img is not None and "auth-visual__photo" in first_img.group(0),
          first_img and first_img.group(0)[:80])


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
for name in FAILED:
    print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
