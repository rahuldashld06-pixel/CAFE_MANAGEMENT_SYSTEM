"""
Offline tests for the instant-navigation layer.

Covers the parts of it that live on the server or in the templates - the
swap container, the speculative-fetch guards and the caching headers - plus
the one template rule the client layer depends on.

Run with:  python tests/instant_nav_test.py
"""
import glob
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["APP_ENV"] = "development"
os.environ["SECRET_KEY"] = "test-secret-key-for-instant-nav-test"
os.environ["SESSION_COOKIE_SECURE"] = "0"

from tests import mysql_shim  # noqa: E402
mysql_shim.install()

import app as application  # noqa: E402

app = application.app
app.config["TESTING"] = True

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    (PASSED if condition else FAILED).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name +
          ("" if condition else "\n          -> %s" % detail))


def sign_up(client, cafe, user, pw="password123"):
    """Register a café. No phone number, so sign-in skips the OTP step."""
    return client.post("/register", data={
        "cafe_name": cafe, "full_name": "%s Owner" % user, "username": user,
        "phone_number": "", "password": pw, "confirm_password": pw,
    }, follow_redirects=True)


print("\n=== 1. Every page ships the swap container ===")

client = app.test_client()
sign_up(client, "Instant Cafe", "instant_admin")

PAGES = ["/dashboard", "/categories", "/foods", "/inventory",
         "/orders/add", "/orders", "/billing", "/reports", "/users",
         "/account/password", "/settings/branding"]

missing_view, missing_js = [], []
for path in PAGES:
    response = client.get(path)
    body = response.get_data(as_text=True)
    if response.status_code != 200:
        missing_view.append("%s (status %d)" % (path, response.status_code))
        continue
    if 'id="page-view"' not in body:
        missing_view.append(path)
    if "js/instant.js" not in body:
        missing_js.append(path)

check("every sidebar page renders a #page-view region to swap",
      not missing_view, "missing on: %s" % missing_view)
check("every sidebar page loads instant.js",
      not missing_js, "missing on: %s" % missing_js)

response = client.get("/orders/add")
body = response.get_data(as_text=True)
check("the page scope opens inside #page-view, before any page script",
      body.find('id="page-view"') < body.find("window.Instant.beginPage()")
      < body.find("<script>", body.find("beginPage()") + 1),
      "marker is not positioned ahead of the page's own scripts")


print("\n=== 1b. The swap region carries every script its page needs ===")
# instant.js slices the region out of the raw HTML between these markers
# rather than reading it off a parsed tree. That matters because several page
# templates contain a stray </div>: parsed, it closes #page-view early and
# strands the page's own <script> tags outside the element, so after a swap
# they never run - which killed the New Order quantity buttons and Billing's
# payment handler. Slicing the raw text makes that markup defect harmless.
#
# What this checks is the other half of the contract: that base.html really
# does keep every page script between the markers. Each page ships exactly
# two outside them - instant.js in <head>, the shell script at end of <body>.
missing_markers, stranded = [], []
for path in PAGES:
    body = client.get(path).get_data(as_text=True)
    if body.count("<!--pv:start-->") != 1 or body.count("<!--pv:end-->") != 1:
        missing_markers.append(path)
        continue
    region = body[body.index("<!--pv:start-->"):body.rindex("<!--pv:end-->")]
    total = len(re.findall(r"<script", body))
    inside = len(re.findall(r"<script", region))
    if inside != total - 2:
        stranded.append("%s (%d of %d inside)" % (path, inside, total - 2))

check("every page marks its swap region exactly once",
      not missing_markers, "markers wrong on: %s" % missing_markers)
check("no page leaves a script outside the swap region",
      not stranded, "stranded scripts: %s" % stranded)


print("\n=== 2. A speculative fetch does not steal flash messages ===")

with client.session_transaction() as sess:
    sess["_flashes"] = [("message", "Order has been placed.")]

warm = client.get("/foods", headers={"X-Instant-Prefetch": "1"})
check("a prefetched page still renders", warm.status_code == 200,
      "status=%d" % warm.status_code)

# The warm-up's HTML is cached by instant.js and shown later, on whatever
# section the user clicks. A message drawn into it would resurface there -
# this is how a stray "page could not be found" ended up greeting people on
# Inventory and Food Management.
check("a prefetched page never draws a queued message into its HTML",
      "Order has been placed." not in warm.get_data(as_text=True),
      "the warm-up baked a flash into the HTML instant.js caches")

real = client.get("/orders").get_data(as_text=True)
check("the message survives the prefetch and reaches the next real page",
      "Order has been placed." in real,
      "the warm-up consumed the flash queue")

# Control: an ordinary request is still supposed to consume the queue, or
# the same message would follow the user from page to page forever.
with client.session_transaction() as sess:
    sess["_flashes"] = [("message", "Consumed once.")]

first = client.get("/foods").get_data(as_text=True)
second = client.get("/foods").get_data(as_text=True)
check("an ordinary request still consumes the queue exactly once",
      "Consumed once." in first and "Consumed once." not in second)


print("\n=== 2b. A 404 only speaks up for a page a person opened ===")

# Every browser asks for /favicon.ico unprompted. Routing that through the
# 404 handler queued "That page could not be found." for whichever screen
# the user opened next - and the warm-up then cached it onto that screen.
client.get("/foods")                       # drain the queue first

icon = client.get("/favicon.ico")
check("/favicon.ico is answered, not sent to the 404 handler",
      icon.status_code == 204, "status=%d" % icon.status_code)

# Signed out too. Otherwise the auth guard redirects the icon request to
# /login and the browser renders the whole sign-in page a second time -
# a wasted database round-trip on every visit, to answer a favicon.
signed_out = app.test_client()
icon_out = signed_out.get("/favicon.ico")
check("/favicon.ico is answered without a session as well",
      icon_out.status_code == 204,
      "status=%d - the auth guard is intercepting it" % icon_out.status_code)

with client.session_transaction() as sess:
    check("a favicon request queues no message for the user",
          not sess.get("_flashes"), "queued: %r" % sess.get("_flashes"))

client.get("/definitely-missing.png")
with client.session_transaction() as sess:
    check("a missing subresource queues no message either",
          not sess.get("_flashes"), "queued: %r" % sess.get("_flashes"))

client.get("/definitely-missing", headers={"Sec-Fetch-Dest": "document"})
with client.session_transaction() as sess:
    check("a person opening a missing page is still told",
          bool(sess.get("_flashes")), "the real 404 message was suppressed")

warm404 = client.get("/inventory", headers={"X-Instant-Prefetch": "1"})
check("and that message is not baked into a warmed page",
      "That page could not be found." not in warm404.get_data(as_text=True))
check("it reaches the next real page instead",
      "That page could not be found." in client.get("/inventory").get_data(as_text=True))


print("\n=== 3. Caching headers ===")

page = client.get("/orders")
check("rendered pages are never written to a shared disk cache",
      "no-store" in (page.headers.get("Cache-Control") or ""),
      "Cache-Control=%r" % page.headers.get("Cache-Control"))

asset = client.get("/static/js/instant.js")
cache_control = asset.headers.get("Cache-Control") or ""
check("instant.js is served", asset.status_code == 200 and len(asset.data) > 1000,
      "status=%d len=%d" % (asset.status_code, len(asset.data)))
check("static assets are cached hard (a ?v= stamp busts them on deploy)",
      "max-age=" in cache_control and "31536000" in cache_control,
      "Cache-Control=%r" % cache_control)
check("the asset stamp is exposed to templates",
      application.ASSET_VERSION.isdigit() and
      ("css/style.css?v=%s" % application.ASSET_VERSION)
      in client.get("/orders").get_data(as_text=True),
      "ASSET_VERSION=%r" % application.ASSET_VERSION)


print("\n=== 4. Signed-out prefetches cannot leak a page ===")

stranger = app.test_client()
for path in ["/orders", "/billing", "/dashboard"]:
    response = stranger.get(path, headers={"X-Instant-Prefetch": "1"})
    check("a prefetch of %s without a session is redirected, not served" % path,
          response.status_code in (301, 302, 303, 307, 308),
          "status=%d" % response.status_code)


print("\n=== 5. Page scripts stay safe to re-execute ===")
# instant.js re-runs a page's scripts in global scope each time that page is
# swapped back in. `function` and `var` tolerate it; `const`, `let` and
# `class` at the top level throw "already declared" on the second visit.

SCRIPT = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)
DECL = re.compile(r"\b(const|let|class)\b")


NOT_CODE = -1


def top_level_depths(code):
    """
    Nesting depth at each index.

    Anything that is not executable code - comments, strings, template
    literals, regex bodies - is marked NOT_CODE so a keyword mentioned in
    prose (this file's own comments say "const/let" more than once) is never
    mistaken for a declaration.
    """
    depths = [0] * (len(code) + 1)
    depth = 0
    i, n = 0, len(code)
    prev = ""

    while i < n:
        ch = code[i]

        if ch == "/" and i + 1 < n and code[i + 1] == "/":
            while i < n and code[i] != "\n":
                depths[i] = NOT_CODE
                i += 1
            continue

        if ch == "/" and i + 1 < n and code[i + 1] == "*":
            end = code.find("*/", i + 2)
            end = n if end == -1 else end + 2
            while i < end:
                depths[i] = NOT_CODE
                i += 1
            continue

        if ch in "\"'`":
            quote = ch
            depths[i] = NOT_CODE
            i += 1
            while i < n:
                depths[i] = NOT_CODE
                if code[i] == "\\":
                    depths[i + 1] = NOT_CODE
                    i += 2
                    continue
                if code[i] == quote:
                    i += 1
                    break
                i += 1
            prev = "x"
            continue

        if ch == "/" and (prev == "" or prev in "(,=:[!&|?{};+-*%~^<>"):
            depths[i] = NOT_CODE
            i += 1
            in_class = False
            while i < n:
                depths[i] = NOT_CODE
                if code[i] == "\\":
                    depths[i + 1] = NOT_CODE
                    i += 2
                    continue
                if code[i] == "[":
                    in_class = True
                elif code[i] == "]":
                    in_class = False
                elif code[i] == "/" and not in_class:
                    i += 1
                    break
                elif code[i] == "\n":
                    break
                i += 1
            prev = "x"
            continue

        if ch in "{(":
            depths[i] = depth
            depth += 1
        elif ch in "})":
            depth -= 1
            depths[i] = depth
        else:
            depths[i] = depth

        if ch not in " \t\r\n":
            prev = ch
        i += 1

    depths[n] = depth
    return depths


offenders = []
for path in sorted(glob.glob(os.path.join(ROOT, "templates", "*.html"))):
    source = io.open(path, encoding="utf-8").read()
    if "{% extends" not in source:
        continue          # standalone auth pages are never swapped

    for block in SCRIPT.finditer(source):
        code = block.group(1)
        depths = top_level_depths(code)
        base_line = source.count("\n", 0, block.start(1)) + 1

        for decl in DECL.finditer(code):
            if depths[decl.start()] != 0:
                continue
            offenders.append("%s:%d %s" % (
                os.path.basename(path),
                base_line + code.count("\n", 0, decl.start()),
                decl.group(1),
            ))

check("no page script declares const/let/class at the top level",
      not offenders, "would throw on a return visit: %s" % offenders)

# Guard the guard: the scan above is only meaningful if it can actually see
# a violation, and must not fire on the same keywords used inside a function.
probe = """
// a comment mentioning const and let must not count
/* nor a block comment naming class */
var note = "a string with const in it";
const flagged = 1;
function fine() { const nested = 2; let inner = 3; return nested + inner; }
(function () { const hidden = 4; })();
"""
probe_depths = top_level_depths(probe)
hits = [d.group(1) for d in DECL.finditer(probe) if probe_depths[d.start()] == 0]
check("the scan flags a real top-level declaration and nothing else",
      hits == ["const"],
      "hits=%r (expected exactly the one top-level const; comments, "
      "strings and nested declarations must be ignored)" % hits)


print("\n" + "=" * 60)
print("PASSED: %d   FAILED: %d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("Failures:")
    for name in FAILED:
        print("  -", name)
print("=" * 60)
sys.exit(1 if FAILED else 0)
