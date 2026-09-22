# Cafe Management — multi-tenant SaaS

Flask + MySQL cafe management application. Any number of cafes sign up
through `/register`; each gets its own menu, staff, orders, billing and
branding, fully isolated from the others.

**Setup and deployment instructions: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).**

---

## What it does

- **Public signup.** `/register` creates a cafe and its first admin owner.
- **A daily order number.** Orders are counted from 1 again each
  morning, per cafe, and that is the number customers, the kitchen and
  the printed ticket use. The permanent id keeps climbing underneath,
  because bills and order lines point at it.
- **Order by QR code.** Each cafe gets its own code, printable from
  Profile -> Table QR Code. A customer scans it, sees that cafe's menu with
  no account, sends an order and is given a number to quote at the counter.
  The order lands on the kitchen screen marked as having come from a
  phone, and its kitchen ticket is claimed and printed by exactly one
  staff screen. The page the customer is still holding keeps up with the
  kitchen on its own: each dish is marked Ready as it is ticked off, and
  the whole order says so when it is finished. They are sitting at a
  table with no counter to watch.
- **Categories and menu.** Categories, food items with photos, prices.
- **Inventory.** Stock levels drive availability automatically — an item
  at zero stock disappears from the order screen.
- **Whose name is on it.** A café brands its own sidebar, its own
  printed receipt and the page its customers hold — their name, their
  symbol, their colour. Behind every page inside the app sits one mark
  that is not theirs to change: the coffee cup and the product's own
  name, faint, fixed, and ignoring both the uploaded symbol and the name
  they chose for their sidebar. Nothing appears on the sign-in, register
  or one-time-code screens, which stand outside the app shell entirely.
- **The café's own colours.** Under Profile → Colours an admin sets the
  accent and the background together, because they are one decision — an
  accent is chosen against a background and a background against an
  accent, and separate screens mean choosing each one blind. Six
  ready-made accents, or any colour at all for either. A chosen
  background brings the whole palette with it: six surface levels and
  three weights of text, derived from it in HSL so the hue survives, and
  placed by measured contrast rather than by fixed lightness. That last
  part is what makes "any colour" safe — a pale background gets dark text
  instead of cream on cream, and a mid-toned one, which has little
  contrast room in either direction, still clears the readability
  threshold. Which way the palette runs is decided on perceived
  brightness, not on HSL lightness: #7FFF00 is a lightness of exactly 0.5
  and one of the brightest colours a screen can make. A café that has
  chosen nothing gets the stylesheet untouched, not a derivation that
  comes close. Printed bills and kitchen tickets are unaffected — those
  are black on white paper whatever the screens look like.
- **A discount beside the tax.** Profile → Tax & Discount sets both, admin
  only. The discount comes off the subtotal before the tax is worked out,
  so a customer is taxed on what they actually pay rather than on what
  they would have paid; the page shows the sum worked through on a ₹100
  order rather than explaining it. Bills already raised keep the rates
  they were charged at.
- **The café's own clock.** Times are stored in UTC and read back on the
  zone the café picks under Profile → Time Zone, which asks for a
  *country* — India, not `Asia/Kolkata`. It offered the raw IANA list
  once, two thousand entries reading `Indian/Chagos` and `Etc/GMT+7`;
  those are addresses in a database, not answers to "where is this
  café?". The couple of dozen countries too wide for one clock appear
  once per clock (`Australia — Perth`), because an "Australia" that
  quietly meant Sydney would tell Perth the wrong time twice a day. The
  countries live in `countries.py`, and every zone in it is checked
  against the ones the machine actually has before it reaches a page —
  IANA is the authority, that file is only a way of asking. The kitchen
  board, the
  billing list, the printed receipt and the page a customer is holding all
  agree. Every stored time is written by the app rather than left to a
  column default, because a default is filled by whichever machine the
  database happens to be on: that is how an order and its own bill ended
  up stamped by two different clocks. Nobody has to go and find the
  setting first: the sign-up and sign-in forms carry whatever clock the
  browser is on, so a café's very first page already reads right. It
  settles only once — a café that has chosen keeps its clock however many
  laptops sign in from elsewhere — and stays that way until somebody
  changes it in the profile menu. What the browser reports is filed under
  the name the picker uses before it is stored: a laptop in Delhi says
  `Asia/Calcutta` where the list says `Asia/Kolkata`, and a café stored
  under a spelling the picker does not offer would open the page to find
  nothing selected. Note that this
  changes what a time *reads* as and nothing else — an order keeps the day
  it was counted under and the number it was given.
- **Switching pages costs nothing.** Every sidebar page is fetched in the
  background after sign-in and kept, so going to one you have already
  opened paints immediately and then refreshes underneath. A save empties
  that store — it has to, or a screen would go on showing what was true
  before — and the store refills itself straight afterwards, including the
  page the save happened on. What does *not* empty it is the app talking
  to itself: the kitchen screen saying it is still switched on every
  twenty seconds, a till claiming a ticket, the tour recording that it was
  seen. Those used to, and that alone was most of why moving around felt
  slow: measured with the database next door, one heartbeat turned a page
  that painted in 1ms into 258ms.
- **A tour, once.** The first time somebody signs in they are shown
  round. Each step puts a ring round the thing it is describing and says
  what pressing it does - and pressing that thing is how the tour moves
  on, so the first time somebody opens a screen is while it is being
  explained. On a phone it opens the drawer to point into the sidebar. An owner and
  somebody on the till get different ones, because somebody on the till
  never opens Reports and never adds staff. It is remembered against the
  person rather than the browser, so it does not start again on the
  tablet in the kitchen, and skipping counts as having seen it. "How this
  works", under your name, opens it again.

  This is why the screens no longer explain themselves. A paragraph under
  every heading is read once, by one person, on their first day, and is
  in everybody's way after that. What stayed is what a tour cannot hand
  back at the moment it matters: the help on individual settings fields,
  and any warning that an effect is shared or cannot be undone - that a
  tax change is not retrospective, that the café's name is everyone's,
  that a new QR code stops the printed ones working.
- **Orders.** Multi-item orders, with a live order-status feed. The
  day's orders are worked from the Kitchen screen. Every dish on a ticket
  has its own circle: tap it as that dish is made, and when the last one
  on a ticket is tapped the order closes itself. Done finishes a whole
  ticket at once, Cancel puts the food back on the shelf. Finished and
  cancelled orders stay on the board, dimmed, with their marks still
  readable. The board holds one day. Anything still waiting when the day
  turns over is marked done the next time the screen is opened, rather
  than waiting for ever behind a board that no longer shows it.
- **Billing.** A bill per order, cash/card/UPI, and optional Razorpay
  online payment with server-side signature verification.
- **Reports.** Revenue, top items and trends for the current cafe.
- **Staff accounts.** Admin, manager, cashier and staff roles. Non-admins
  are restricted to ordering, food, inventory and billing.
- **Admin OTP login.** Admins with a mobile number on file confirm a
  6-digit code after their password.
- **Name and symbol in the top corner.** An admin sets the name, the
  tagline and the symbol the sidebar shows, under Profile -> Name & Symbol.
  Left alone it reads "Cafe Manager / Food & Service Admin" beside a coffee
  cup, and clearing a field puts that default back rather than leaving a
  blank corner. The same name is in the bar that stays at the top on a
  phone.
- **A customer's bill is dressed for a customer.** The cafe's name in a
  serif, its symbol pale behind the text, a line at the foot chosen from
  the order's own number so a reprint reads the same, and who served it.
- **Nothing reloads the whole page.** Following a link swaps only the page
  region, and saving a form now does the same: the post is sent in the
  background and the page that comes back is swapped in, so the sidebar,
  the top bar, the stylesheets and the scripts are never fetched twice.
  The address bar follows the server's redirect, so Back works and a
  refresh does not re-post.
- **No scrollbars.** Everything still scrolls by wheel, trackpad, touch and
  keyboard; the bar is simply not drawn.
- **The page title stays put.** The heading and its actions stay at the top
  while the content scrolls underneath, offset by the measured height of
  the bar above rather than a guess.
- **The sidebar scrolls only when it has to.** The name is a fixed head and
  the page links below it are what scrolls, starting at Dashboard. A screen
  tall enough for every section has no scrollbar at all; a shorter one
  scrolls the links under the name, which does not move.
- **Theme colour.** An admin picks the accent the whole cafe's screens are
  painted in. Copper by default.
- **Installs as an app.** A web manifest asking for fullscreen display, so
  "Install" in a browser or "Add to Home Screen" on a phone opens the site
  with no address bar and no browser chrome. In an ordinary tab a button in
  the top bar fills the screen instead, and remembers the choice.

## A note on speed

Measured from inside the deployed app, a single database round trip costs
about 543ms - both opening a pooled connection and running `SELECT 1` take
exactly that. The app and the database are half a second apart, so a page
doing four lookups spends over two seconds travelling and no amount of
code tuning changes it. `/healthz` reports `connect_ms` and `query_ms` so
this can be checked rather than guessed at.

The largest single improvement available is to host the database in the
same region as the app.

The free hosting tier also stops the service when idle, so the first
visitor pays start-up plus a fresh connection. Two things guard against
that, and it is worth having both:

- The app calls its own `/healthz` every ten minutes. On Render the
  address comes from `RENDER_EXTERNAL_URL` and needs no setting up. This
  only works while the process is alive - it cannot wake a service that
  has already stopped - and on a free plan it uses most of the month's
  running hours, because never being idle is the point.
- `.github/workflows/keep-awake.yml` does the same from outside, on
  GitHub's schedule, which does wake a stopped service. Set the
  repository variable `SITE_URL` to switch it on.

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
countries.py         Countries and the clocks they keep, for the time zone picker
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
tests/settings_test.py Tax rate, profile photo, name and symbol suite
tests/tablet_layout_test.py Real-browser tablet layout test suite
tests/stock_alert_test.py Dashboard stock-alert test suite
tests/print_test.py  Printable bill and kitchen ticket test suite
tests/staff_access_test.py Non-admin permission test suite
tests/auto_print_test.py Automatic-printing settings test suite
tests/billing_paid_test.py Bill settlement test suite
tests/auto_print_browser_test.py Real-browser automatic-printing suite
tests/browser_nav_test.py Real-browser navigation test suite
tests/menu_search_test.py Real-browser New Order menu/search test suite
tests/stale_banner_test.py Real-browser stale-flash regression suite
tests/mobile_nav_test.py Real-browser mobile drawer test suite
tests/tutorial_test.py First-sign-in tour test suite
tests/tour_browser_test.py Real-browser first-sign-in tour suite
tests/nav_cache_test.py Real-browser page-cache and navigation-speed suite
tests/timezone_test.py Café clock and stored-time test suite
tests/clock_browser_test.py Real-browser suite for a café's clock settling itself
tests/colours_test.py Café colours, palette contrast and the bill discount
tests/colour_browser_test.py Real-browser colour-picker and page-swap suite
tests/cdp.py         Minimal DevTools-protocol client used by that suite
```

## Tests

```bash
python tests/smoke_test.py        # expect PASSED: 38   FAILED: 0
python tests/upgrade_test.py      # expect PASSED: 19   FAILED: 0
python tests/instant_nav_test.py  # expect PASSED: 25   FAILED: 0
python tests/instant_post_test.py # expect PASSED: 16   FAILED: 0
python tests/user_delete_test.py  # expect PASSED: 18   FAILED: 0
python tests/hot_sellers_test.py  # expect PASSED: 23   FAILED: 0
python tests/hot_mirror_test.py   # expect PASSED: 16   FAILED: 0
python tests/settings_test.py     # expect PASSED: 104  FAILED: 0
python tests/tablet_layout_test.py # expect PASSED: 48   FAILED: 0
python tests/stock_alert_test.py  # expect PASSED: 24   FAILED: 0
python tests/print_test.py        # expect PASSED: 52   FAILED: 0
python tests/staff_access_test.py # expect PASSED: 36   FAILED: 0
python tests/auto_print_test.py   # expect PASSED: 39   FAILED: 0
python tests/billing_paid_test.py # expect PASSED: 27   FAILED: 0
python tests/auto_print_browser_test.py # expect PASSED: 12  FAILED: 0
python tests/browser_nav_test.py  # expect PASSED: 15   FAILED: 0
python tests/menu_search_test.py  # expect PASSED: 17   FAILED: 0
python tests/stale_banner_test.py # expect PASSED: 10   FAILED: 0
python tests/mobile_nav_test.py   # expect PASSED: 84   FAILED: 0
python tests/theme_test.py        # expect PASSED: 33   FAILED: 0
python tests/theme_browser_test.py # expect PASSED: 13  FAILED: 0
python tests/food_number_test.py  # expect PASSED: 31   FAILED: 0
python tests/qr_order_test.py     # expect PASSED: 112  FAILED: 0
python tests/kitchen_screen_test.py # expect PASSED: 23  FAILED: 0
python tests/password_view_test.py # expect PASSED: 23  FAILED: 0
python tests/fullscreen_test.py   # expect PASSED: 29   FAILED: 0
python tests/tutorial_test.py     # expect PASSED: 41   FAILED: 0
python tests/tour_browser_test.py # expect PASSED: 26  FAILED: 0
python tests/nav_cache_test.py    # expect PASSED: 8    FAILED: 0
python tests/timezone_test.py     # expect PASSED: 41   FAILED: 0
python tests/clock_browser_test.py # expect PASSED: 6   FAILED: 0
python tests/colours_test.py      # expect PASSED: 54   FAILED: 0
python tests/colour_browser_test.py # expect PASSED: 14  FAILED: 0
```

All thirty-three run in memory against a SQLite stand-in — no database or
network needed. The sixteen that drive a browser use a headless Edge or
Chrome when one is installed, and skip themselves when none is.

`nav_cache_test.py` is the odd one out: it puts a deliberate delay on every
query, because the stand-in database answers instantly and the difference
between a page served from cache and one fetched from the server would
otherwise be a millisecond either way. With the delay in, a cache hit and a
round trip are unmistakable.

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
a mouse, the exact width of an iPad Pro in landscape, is left alone. It
also measures the New Order menu at each size: at least two cards to a row,
cards short enough to stack, and type no smaller than a name can be read
at — density must not be bought with unreadable text.

`stock_alert_test.py` covers the dashboard's stock alert, which names the
food that needs reordering rather than only counting it. Checks that zero
stock is reported as out rather than low (a zero satisfies the minimum test
too), that the most urgent is listed first, that a long list is capped and
says how many more there are, and that one café is never told about
another's empty shelves.

`print_test.py` covers the two printable documents. The bill carries the
café's name, logo and every line of one order, with totals taken from the
bill record so a receipt reprinted after a tax-rate change still shows what
was actually charged. The kitchen ticket carries the order number, the
dishes and how many — and the test asserts no price reaches it. Both are
open to staff, and both stop at the café boundary.

`staff_access_test.py` pins down what a non-admin can reach. Staff run the
counter — orders, the menu, categories, stock and taking payment — and get
neither the dashboard, the reports, nor user management. They do get the
Billing summary, fixed to today rather than the filtered period, with
revenue left off. It checks the sidebar and the server guard
agree, because a link that is hidden but still served is a hole.

`auto_print_test.py` and `auto_print_browser_test.py` cover automatic
printing: that both switches start off, that the delay is validated, that
anyone who works at the café can change them, and — in a real browser —
that the kitchen ticket waits its configured delay while the receipt does
not, and that nothing prints while a switch is off.

Kitchen tickets are pulled rather than pushed, for counter orders as well
as ones sent from a table. Whichever screen asks first claims the ticket
and prints it, so two tills watching print one ticket between them. When a
kitchen screen is on it takes them all, and the till stands down — the
till is next to the customer and the food is made in the kitchen. The
café's delay is applied to counter orders wherever they end up printing,
because it exists to leave room to catch an order tapped in wrong; an
order from a table is not held back, because there is no till to have
slipped and a customer is waiting. An order taken while nothing was going
to print it is marked dealt with there and then, so turning the setting on
after lunch does not run off a ticket for every order already served.

Note that a browser always shows its print dialog. To print silently, start
Chrome or Edge with `--kiosk-printing` and set the till's receipt printer as
the default.

`billing_paid_test.py` pins down what settles a bill: only the Paid button.
Choosing UPI or Card records how a bill will be paid, not that it has been,
and a verified gateway payment leaves its reference against the bill while
still waiting for someone to press Paid. It also checks which list shows what:
both Billing and the Kitchen screen open on today — the shift the till is on —
and Billing reaches everything else through the date filter or All History.
Nothing is ever deleted to make that list short: an older order still opens by
its own link and its bill is still there under All History.

Billing shows the order number the kitchen calls out and the customer was
given, not the permanent row id, so the same order is not #47 on one screen
and #6 on another. The bill keeps its own number: they are different things,
counted differently, and the bill number is what the bill is filed under.

Billing prints a customer's receipt on request rather than automatically: the
kitchen ticket has to print because somebody must cook the food, but most
customers walk off without a receipt, and printing every one burns a roll a
day. The button sits beside the payment status, where the money is already
being taken.

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
