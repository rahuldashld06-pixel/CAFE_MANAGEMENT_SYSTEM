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

## Caching

One row - who is signed in and the café around them - was read on every
page load: name, role, theme, tax rate, branding, all the same answer
every time. At roughly half a second per round trip to the database that
was half a second before any screen appeared. It is now read once and
reused, which takes eight reads across eight screens down to one.

Stock is deliberately **not** cached. Two tills racing for the last
croissant is settled by a row lock inside the transaction, and a cache
in front of that would be a cache in front of the only thing keeping the
count honest.

Anything that changes something drops the cached rows on its way out of
the request, rather than each write site remembering to. A short list of
endpoints skips that - the ones a kitchen screen posts to every few
seconds, and taking an order, which writes only orders, stock and bills.
That list is checked rather than trusted: the suite drives each one and
fails if any statement changes a column the cached row carries. It has
already caught one - `timezone_guess` writes `cafes.timezone`, and while
it sat on that list the café's clock went stale and kitchen tickets
printed in UTC.

**Sharing it between workers.** Gunicorn runs three worker processes, so
by default each keeps its own copy and a change in one is invisible to
the others until their copies age out (`CACHE_SECONDS`, 30 by default).
Whoever made the change always sees it immediately. Setting `REDIS_URL`
replaces that with a single shared copy, so a drop is a drop for
everybody and the window disappears. It is optional and fail-soft: with
no Redis, or with one that stops answering, each worker falls back to
its own memory rather than showing anybody an error. `/healthz` reports
which is actually in use, because a `REDIS_URL` that quietly fails to
connect looks exactly like one that works.

Only worth setting if the Redis is in the **same region** as the app. One
that is not costs about what the database round trip it replaces costs,
and this app's database is already half a second away.

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
tests/list_search_test.py Real-browser phone-header, list-search and billing-layout suite
tests/identity_test.py Phone numbers by country, and signing in by email
tests/security_test.py Headers, login lockout, upload sniffing and tenant isolation
tests/cdp.py         Minimal DevTools-protocol client used by that suite
```

## Security

What is in place, and what each part is actually for.

**Getting in.** Passwords are hashed by Werkzeug (PBKDF2). An admin with
a mobile number on file is challenged for a one-time code, which expires
and gives up after five wrong answers. The password path is rate
limited the same way: five failures from one address against one
username buys a 15-minute lockout, counted in the database so it holds
across workers rather than resetting whenever the next request lands on
a different one. It is keyed on the pair on purpose — keyed on the
username alone, anyone who knew an owner's username could lock them out
of their own till, which turns a protection into the attack. A wrong
username and a wrong password give the same message, so the form cannot
be used to discover who banks here.

**Staying in.** Session cookies are HttpOnly, SameSite=Lax, and Secure
in production. Signing in clears the session first, so a session id
planted beforehand does not survive it. The app refuses to boot in
production without `SECRET_KEY`, because a default key means anyone can
forge a cookie for any café.

**Doing things.** Every POST, PUT, PATCH and DELETE carries a CSRF
token, compared in constant time. Every query is scoped to the café's
owner, so one tenant cannot read or write another's rows by guessing an
id. Staff are held to an endpoint allowlist. `?next=` is checked before
any redirect follows it.

**What the browser is allowed to do.** A Content-Security-Policy on
every response, plus `nosniff`, `frame-ancestors 'none'` (and
`X-Frame-Options` for anything that does not read CSP),
`Referrer-Policy`, `Permissions-Policy` and `Cross-Origin-Opener-Policy`.
HSTS is sent in production over HTTPS only. Taking a card payment hands
the browser to Razorpay, so that one route gets its own policy rather
than opening the whole app up.

One honest limit: `script-src` still carries `'unsafe-inline'`. The app
writes its behaviour as inline `<script>` in eighteen templates, and the
usual fix — a per-response nonce — cannot work here, because instant.js
fetches a page and injects its markup into the *current* document, where
a nonce belonging to another response is refused and the script never
runs. Removing it means moving that JavaScript into files under
`static/`. Until then the rest of the policy is what does the work:
scripts cannot be *fetched* from anywhere unexpected, the page cannot be
framed, forms cannot post elsewhere, and no plugin or base-tag trick is
available.

**Uploads.** Checked by content, not by name. A file called `logo.png`
holding markup, an SVG, or a shell script is refused rather than stored
and later served as an image. Size is capped in two places: the request
body and the image itself.

**The database.** TLS with the provider's CA verified, not merely
encrypted — an encrypted connection to the wrong server is still the
wrong server. `certs/ca.pem` is Aiven's public certificate and is meant
to be committed; it contains no private key. Turning verification off
takes a deliberate environment variable.

**Taking money.** Razorpay checkout responses are signature-verified on
the server, and webhooks are HMAC-verified with an amount check, so a
reply claiming a payment is not taken at its word. One-time codes are
single-use and deleted once verified, and the self-service password
reset needs the account's registered mobile number.

**Ordering from a QR code.** The address a customer's phone posts to
takes no session and no CSRF token — it cannot, because a customer
never signs in — so how often it is called is the only thing limiting
it. Two counts are kept, and one query reads both: 30 orders a minute
from one address, and 120 a minute for the whole café. Either limit
alone is wrong for the same reason the login lockout gives. Every table
in a café shares one token, so a café-wide count on its own would let
one phone stop everybody else ordering; and a per-address count on its
own does nothing about a flood spread over a few proxies.

The numbers sit in a wide gap. A phone places one order and then eats.
A whole café behind one router is nowhere near 30 a minute at its
busiest, and 120 a minute is a café nobody has. A flood is thousands.
Change them with `QR_ORDER_MAX_PER_SOURCE`, `QR_ORDER_MAX_PER_CAFE` and
`QR_ORDER_WINDOW` if your café is busier than that.

What matters as much as the limit is what a refusal costs. An accepted
order is twelve database round trips and takes the per-café lock that
every other order queues on; a refused one is two round trips, takes no
lock and writes nothing, so a flood bounces off without standing on the
kitchen's own orders. The order is counted only once it is real, so an
order that fails because the last sandwich went is not also held
against the table that tried.

Rotating the table code on the QR settings page invalidates every
existing token at once, which is the kill switch if one ever leaks.

**Headers, and counting the proxies.** Every response carries a
content security policy naming `script-src`, `object-src`, `base-uri`,
`frame-src` and `frame-ancestors 'none'`, plus `X-Frame-Options: DENY`,
`nosniff`, a referrer policy and a permissions policy. HSTS is the one
that was genuinely missing in production, and the reason is worth
keeping: it was sent only when `request.is_secure`, which is true behind
one proxy and false behind two. This runs behind Cloudflare and then
Render, so `X-Forwarded-Proto` arrives as a list and ProxyFix — set to
trust one hop — read the last entry, which is Render handing the request
to gunicorn over plain HTTP inside its own network. The logic was right
and its input was not. It now asks whether any hop in the chain was
HTTPS, which does not care how long the chain is, and still refuses to
send the header when nothing in it was.

**The sign-in code.** Everybody who signs in is asked for a one-time
code after their password - admin, manager, cashier and staff alike,
because a password on its own is a password on its own whoever holds
it. An account with no email and no mobile number on file has nowhere
to be sent one and signs in on its password as before; filling in those
details closes the gap. The customer with the QR code never signs in at
all and is untouched by any of it. The code lasts ten minutes, allows
five wrong guesses, and can be resent from the code screen with a
thirty-second cooldown so the button cannot be used to flood an inbox. It can go by SMS, as before, or by email — email first
when there is an address and a mail server to send with, because it
costs nothing per message. Sending uses `smtplib` from the standard
library, so no package was added: any SMTP server will do, including a
Gmail app password. Set `SMTP_HOST`, `SMTP_PORT` (587, or 465 for
implicit TLS), `SMTP_USER`, `SMTP_PASSWORD` and `SMTP_FROM`. With none
of them set the code goes to the log, which is how a laptop signs in
and how a half-configured deploy stays diagnosable. The screen names
where it went — `i••@cafe.com` — because somebody with two addresses
needs to know which to open.

**Which address somebody is counted as.** The login lockout and the QR
order limit are both counted per address, so they are only as good as
knowing who is asking. `request.remote_addr` is not that: ProxyFix
trusts one hop, which makes it the *last* entry of `X-Forwarded-For`,
and behind Cloudflare and then Render the last entry is Render. Every
visitor in the world came out as one address — which quietly turned the
login lockout into one keyed on the username alone, the exact thing its
own table comment warns against.

`request_source()` now believes `CF-Connecting-IP` first, since
Cloudflare sets it itself and overwrites whatever arrived. Failing that
it counts `TRUSTED_PROXY_HOPS` from the *right* of `X-Forwarded-For` —
the only trustworthy end, because anything a client invents arrives on
the left while each real proxy appends on the right. Someone writing
`1.2.3.4` into their own header ends up with it sitting harmlessly to
the left of their real address.

Putting a load balancer in front of the service is therefore one
number: `TRUSTED_PROXY_HOPS=3`. There are tests for two hops, three
hops, a forged header at each, and Cloudflare's own header.

**Secrets.** `.env` and `config.py` are gitignored and the history has
been checked for credential-shaped strings. On Render these are
environment variables, never files.

`tests/security_test.py` holds this to account — 107 checks covering the
headers, the lockout (including that it does not lock the wrong person),
the upload sniffing, the QR order limit (including that a refusal is
cheap and that one flooding table does not shut the café), and the parts
that were already right.

## Tests

```bash
python tests/smoke_test.py        # expect PASSED: 39   FAILED: 0
python tests/upgrade_test.py      # expect PASSED: 19   FAILED: 0
python tests/instant_nav_test.py  # expect PASSED: 25   FAILED: 0
python tests/instant_post_test.py # expect PASSED: 16   FAILED: 0
python tests/user_delete_test.py  # expect PASSED: 18   FAILED: 0
python tests/hot_sellers_test.py  # expect PASSED: 23   FAILED: 0
python tests/hot_mirror_test.py   # expect PASSED: 16   FAILED: 0
python tests/settings_test.py     # expect PASSED: 104  FAILED: 0
python tests/tablet_layout_test.py # expect PASSED: 62   FAILED: 0
python tests/stock_alert_test.py  # expect PASSED: 30   FAILED: 0
python tests/print_test.py        # expect PASSED: 55   FAILED: 0
python tests/staff_access_test.py # expect PASSED: 33   FAILED: 0
python tests/auto_print_test.py   # expect PASSED: 39   FAILED: 0
python tests/billing_paid_test.py # expect PASSED: 44   FAILED: 0
python tests/auto_print_browser_test.py # expect PASSED: 12  FAILED: 0
python tests/browser_nav_test.py  # expect PASSED: 15   FAILED: 0
python tests/menu_search_test.py  # expect PASSED: 17   FAILED: 0
python tests/stale_banner_test.py # expect PASSED: 10   FAILED: 0
python tests/mobile_nav_test.py   # expect PASSED: 84   FAILED: 0
python tests/theme_test.py        # expect PASSED: 33   FAILED: 0
python tests/theme_browser_test.py # expect PASSED: 13  FAILED: 0
python tests/food_number_test.py  # expect PASSED: 31   FAILED: 0
python tests/qr_order_test.py     # expect PASSED: 123  FAILED: 0
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
python tests/list_search_test.py  # expect PASSED: 107  FAILED: 0
python tests/identity_test.py     # expect PASSED: 88   FAILED: 0
python tests/security_test.py     # expect PASSED: 107  FAILED: 0
```

All thirty-six run in memory against a SQLite stand-in — no database or
network needed. The seventeen that drive a browser use a headless Edge or
Chrome when one is installed, and skip themselves when none is.

Each browser suite binds its own fixed port. A run that is killed part
way can leave its server listening, and every later run of that suite
then fails to bind and times out waiting for a page that was never
served — which reads exactly like a broken test. `netstat -ano | findstr
:<port>` names the process; it is safe to end.

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
