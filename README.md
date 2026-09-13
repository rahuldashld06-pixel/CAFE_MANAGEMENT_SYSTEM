# Cafe Management — multi-tenant SaaS

Flask + MySQL cafe management application. Any number of cafes sign up
through `/register`; each gets its own menu, staff, orders, billing and
branding, fully isolated from the others.

**Setup and deployment instructions: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).**

---

## What it does

- **Public signup.** `/register` creates a cafe and its first admin owner.
- **Categories and menu.** Categories, food items with photos, prices.
- **Inventory.** Stock levels drive availability automatically — an item
  at zero stock disappears from the order screen.
- **Orders.** Multi-item orders, with cancel and complete actions and a
  live order-status feed.
- **Billing.** A bill per order, cash/card/UPI, and optional Razorpay
  online payment with server-side signature verification.
- **Reports.** Revenue, top items and trends for the current cafe.
- **Staff accounts.** Admin, manager, cashier and staff roles. Non-admins
  are restricted to ordering, food, inventory and billing.
- **Admin OTP login.** Admins with a mobile number on file confirm a
  6-digit code after their password.
- **Per-cafe branding.** Each cafe sets its own display name, logo and
  sign-in photo.

## How tenant isolation works

Every business row (`categories`, `foods`, `orders`) is tagged with the
owning cafe's `user_id`, and carries a `cafe_id` alongside it. Bills are
isolated through their order; inventory through its food item. User
management, branding and reports filter on `cafe_id` directly.

The owner id is written back to `cafes.owner_user_id` the first time it
is resolved, so it cannot drift when admins are added or deactivated.

## Requirements

- Python 3.12
- MySQL 8 (any managed provider — Aiven, PlanetScale, Railway)

No database dump to import: the app creates and migrates its own schema
on first run. `docs/schema.sql` is a reference copy for review, or for
provisioning by hand when the runtime user cannot execute DDL.

## Layout

```
app.py               All routes and application logic
wsgi.py              WSGI entry point for gunicorn
config.py            Optional local config (gitignored)
requirements.txt     Python dependencies
Procfile             Process definition for Render/Heroku
render.yaml          Render blueprint
runtime.txt          Pinned Python version
.env.example         Every supported environment variable
docs/DEPLOYMENT.md   Step-by-step setup and deployment guide
docs/schema.sql      Reference database schema
docs/FIXES.md        What was broken before, and how it was fixed
templates/           Jinja2 templates
static/              CSS and static assets
static/js/instant.js Instant page navigation (prefetch + swap)
tests/smoke_test.py  Offline end-to-end test suite
tests/upgrade_test.py Legacy-database upgrade test suite
tests/instant_nav_test.py Instant-navigation test suite
tests/user_delete_test.py Staff-account deletion test suite
tests/hot_sellers_test.py Hot-selling shelf test suite
tests/hot_mirror_test.py Real-browser mirrored-card test suite
tests/settings_test.py Tax-rate and profile-photo test suite
tests/tablet_layout_test.py Real-browser tablet layout test suite
tests/stock_alert_test.py Dashboard stock-alert test suite
tests/browser_nav_test.py Real-browser navigation test suite
tests/menu_search_test.py Real-browser New Order menu/search test suite
tests/stale_banner_test.py Real-browser stale-flash regression suite
tests/mobile_nav_test.py Real-browser mobile drawer test suite
tests/cdp.py         Minimal DevTools-protocol client used by that suite
```

## Tests

```bash
python tests/smoke_test.py        # expect PASSED: 37   FAILED: 0
python tests/upgrade_test.py      # expect PASSED: 19   FAILED: 0
python tests/instant_nav_test.py  # expect PASSED: 25   FAILED: 0
python tests/user_delete_test.py  # expect PASSED: 18   FAILED: 0
python tests/hot_sellers_test.py  # expect PASSED: 23   FAILED: 0
python tests/hot_mirror_test.py   # expect PASSED: 16   FAILED: 0
python tests/settings_test.py     # expect PASSED: 39   FAILED: 0
python tests/tablet_layout_test.py # expect PASSED: 33   FAILED: 0
python tests/stock_alert_test.py  # expect PASSED: 19   FAILED: 0
python tests/browser_nav_test.py  # expect PASSED: 15   FAILED: 0
python tests/menu_search_test.py  # expect PASSED: 17   FAILED: 0
python tests/stale_banner_test.py # expect PASSED: 10   FAILED: 0
python tests/mobile_nav_test.py   # expect PASSED: 31   FAILED: 0
```

All thirteen run in memory against a SQLite stand-in — no database or
network needed. The six suites that drive a browser use a headless Edge or
Chrome when one is installed, and skip themselves when none is.

`smoke_test.py` takes two cafes through the full lifecycle and asserts
that neither can read or modify the other's data.

`upgrade_test.py` covers the migration of a database carried over from
the older single-cafe app: missing unique constraints, duplicate rows
already present, the rule that a paid bill is never deleted in favour of
a pending duplicate, and constraint detection by shape rather than by
index name.

`instant_nav_test.py` covers the fast-navigation layer: that every page
ships the region instant.js swaps, that a background warm-up cannot
swallow a flash message or serve a page to a signed-out browser, and that
no page script declares `const`/`let`/`class` at the top level - which
would throw the second time that page is opened.

`browser_nav_test.py` drives a real browser through the order-taking path:
create an order, move Orders -> Billing -> New Order -> Billing, then pay.
It is the check that catches a duplicated delegated listener - the failure
that would post a payment twice from a single click.

`menu_search_test.py` covers the New Order menu: categories A-Z with their
items A-Z inside, and the search box filtering by food name or category -
including that filtering only hides cards, so a quantity already keyed in
survives a search and still goes out with the order.

`stale_banner_test.py` guards a bug seen live: the browser's unprompted
`/favicon.ico` request went through the 404 handler, queueing "That page
could not be found." for the next page — and the background warm-up then
cached that message onto Inventory and Food Management. It checks both
directions: no banner on a good page, and a real 404 still says so.

`mobile_nav_test.py` drives the phone layout at 390x844: the sidebar is an
off-canvas drawer behind a hamburger, so the page keeps the screen. It
checks the drawer opens with readable labels, is not tabbable while closed,
closes after navigating, and that desktop is unaffected. It drives real
input events rather than JavaScript `.click()`, so an invisible overlay
covering a control is caught rather than clicked straight through.

`user_delete_test.py` covers permanently deleting a staff account, and
especially the refusals: never your own account, never the café owner
(every food, category and order row is filed under that id), and never the
last active admin. Also checks one café cannot delete another's staff and
that order history survives the person who took it.

`hot_sellers_test.py` covers the Hot Selling shelf on New Order: ranking by
units sold over the recent window, and the things that would mislead a till
if they were wrong — cancelled orders must not count, a stale month must not
keep an item pinned, a sold-out item must not be offered first, and the
shelf never holds more than five.

A best seller is drawn twice — on the shelf and under its own category —
so `hot_mirror_test.py` drives a real browser to check the pair stays in
step and, above all, that ordering two of a food shown twice charges for
two rather than four. The shelf copy carries no form field name and a
different input class, so only the card under the category heading is ever
submitted or totalled.

`settings_test.py` covers the pages behind the profile menu: that only an
admin can change the café's tax rate, that the rate actually reaches the
bill, that bills already raised keep the rate they were charged at, and
that a profile photo cannot be fetched from another café.

`tablet_layout_test.py` walks the common tablet sizes in both orientations.
A tablet held upright was narrow enough to get the drawer, but rotating it
crossed back over the old breakpoint and handed the user the laptop layout
mid-session. Width cannot tell a tablet from a small laptop, so the
stylesheet also asks whether the pointer is coarse — the test checks every
tablet gets the drawer and finger-sized controls while a 1366px laptop with
a mouse, the exact width of an iPad Pro in landscape, is left alone.

`stock_alert_test.py` covers the dashboard's stock alert, which names the
food that needs reordering rather than only counting it. Checks that zero
stock is reported as out rather than low (a zero satisfies the minimum test
too), that the most urgent is listed first, that a long list is capped and
says how many more there are, and that one café is never told about
another's empty shelves.

## Security notes

- `SECRET_KEY` is mandatory in production; the app refuses to start
  without it.
- Passwords and OTP codes are hashed (`werkzeug.security`); OTP codes are
  single-use and deleted once verified.
- CSRF tokens are required on every state-changing request.
- Razorpay checkout responses are signature-verified server-side and
  webhooks are HMAC-verified with an amount check.
- Session cookies are HttpOnly, SameSite=Lax, and Secure in production.
- Self-service password reset requires the account's registered mobile
  number.
- Never commit `.env` or `config.py`.

## Known limitations

- **Usernames are unique platform-wide**, not per cafe. Two cafes cannot
  both have a user called `admin`. Sign-in therefore needs only a
  username and password, with no cafe selector.
- **CSRF tokens are injected into forms by JavaScript** in `base.html`.
  Forms built dynamically after page load need the token added manually,
  and the app will not accept form submissions with JavaScript disabled.
- **Images are stored in the database** as `MEDIUMBLOB`. This is the
  right trade-off on hosts with ephemeral disks and no object storage,
  but at large scale you would move them to S3 or similar.
- There is no billing/subscription layer for the cafes themselves —
  signup is open to anyone with the URL.
