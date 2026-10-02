# Refero — calm service, beautifully run

A multi-tenant café and restaurant system: Flask + MySQL. Its emblem is a
café drawn in a brush-stroke ring - a lamp over a table and its chairs, a
plant - with "Refero" in brush script, a coffee bean for the o, two
leaves, and "Restaurant and Cafe / Food Management System" beneath
(`static/brand/refero-emblem.webp`, shown as a round badge on the sign-in pages, the
console and invoices). Everywhere small - logo tiles, the tab, the home
screen - it is the round monogram drawn from it: the emblem's script R, its bean
and its leaves on a cream disc (`templates/_brand_mark.html`; the R is Kaushan
Script's, OFL, traced to a path). Any number of cafes sign up
through `/register`; each gets its own menu, staff, orders, billing and
branding, fully isolated from the others.

**Setup and deployment instructions: [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).**

---

## What it does

- **Public signup.** `/register` creates a cafe and its admin, with a
  month of Refero Pro free.
- **One admin per cafe.** The account that created the cafe is its admin,
  and the only one: the settings, the team and the subscription are its,
  and every food, category and order is filed under it, so it cannot be
  switched off or deleted. Everybody else is a manager, cashier or staff,
  with the pages their admin ticks. There is no separate "owner". Cafes
  from before this keep their creating account as admin; any other admin
  they had becomes a manager.
- **Refero Pro: a month free, then Monthly or Yearly.** Rs 650 a month,
  or Rs 6,000 a year (Rs 500 a month). Under Subscription in the admin's
  profile menu: the plan and when it ends, the two plans, how it is paid,
  and every invoice - each one printable or saved as a PDF. Paid by UPI
  straight into the developer's account: "Pay by UPI" opens the admin's
  UPI app with the developer's UPI ID, the name on that account, the
  exact amount and our reference all filled in (or a QR code to scan
  from a computer), and the admin then gives the 12-digit UPI reference
  from its receipt. The developer finds the credit and approves it in
  the console; the plan runs on from the day the current one (or the
  trial) ends, so paying early loses nothing. A UPI reference can be
  used once. Prepaid - nothing is taken again by itself: a reminder on
  every page from a week before the end, three days' grace after it,
  and then the cafe is on Refero Free. Nothing is deleted, and a payment
  being checked counts as Pro for three days meanwhile. Cards cannot be
  taken this way: a UPI app can, and the app cannot tell on its own that
  money arrived, which is why a person confirms it.
- **Free, trial and Pro.** What each plan opens is `PLAN_FEATURES` in
  app.py, one list:

  | | Free | Free trial | Refero Pro |
  |---|---|---|---|
  | Orders (counter and table QR), kitchen, billing, menu, stock | yes | yes | yes |
  | Sales figures, the sales chart, best sellers | locked | yes | yes |
  | Stock alerts | locked | yes | yes |
  | Reviews and dish ratings | locked | yes | yes |
  | Reports and their download | locked | yes | yes |
  | Teammates | one, as staff | staff only | any role, any number |
  | The cafe's own name, symbol and sign-in photo | Refero's shown | Refero's shown | yes |

  A locked option wears a lock - in the sidebar, the profile menu, on a
  dashboard card - and leads to its own line under "What Refero Pro
  unlocks" on the Subscription page, picked out. The whole team can read
  that page; choosing, paying and the invoices are the admin's. The
  figures behind a lock are never worked out or sent - the dashboard
  and its feeds leave them out, and the veil is drawn over a shape, not
  over the numbers. A cafe's own branding is kept while it is locked,
  and comes back the moment it subscribes; teammates it already has
  keep working. Tests start every new cafe on Pro (`NEW_CAFE_PLAN`,
  ignored on the live site) except the suites about plans.
- **The developer's console (`/platform`).** Behind its own password
  (`PLATFORM_PASSWORD`; unset, it does not exist), five wrong guesses
  lock that address out for fifteen minutes, and it asks again after
  two hours. One page: whether the site is healthy, the database's
  answer time, the last hour's requests, failures and response times,
  and charts of all three over an hour, a day or a week; every alert;
  the payments to check, approve or turn down; and every cafe with its
  admin, plan and what it has paid - with Lifetime free (Refero for
  good, no reminders, never paused), End lifetime (a week to choose a
  plan) and days added to any plan. It refreshes itself every minute.
- **Alerts.** A crash (with its traceback), a database that refused, a
  failed health check, a page slower than `MONITOR_SLOW_MS` (3 s), a
  wrong console password, an error in somebody's browser, and anything
  a cafe reports from Report a problem in the profile menu - each an
  alert in the console, never a word of it on a cafe's page. The same
  fault again counts on its open alert instead of adding one, so an
  outage is one alert with a number on it. Counting costs no request a
  database trip: each server process keeps its minutes and faults in
  memory and a background thread writes them; with the database down,
  the console still opens, says so, and shows what that process is
  holding. What nothing inside the site can do is say the whole site is
  down - for that, point a free uptime monitor (UptimeRobot, Better
  Stack) at `/healthz`, which answers 503 when the database does not:
  it emails or texts you.
- **A daily order number.** Orders are counted from 1 again each
  morning, per cafe, and that is the number customers, the kitchen and
  the printed ticket use. The permanent id keeps climbing underneath,
  because bills and order lines point at it.
- **Order by QR code.** Each cafe gets its own code, printable from
  Profile -> Table QR Code. A customer scans it, sees that cafe's menu with
  no account, sends an order and is given a number to quote at the counter.
  The order lands on the kitchen screen marked as having come from a
  phone. The page the customer is still holding keeps up with the
  kitchen on its own: each dish is marked Ready as it is ticked off, and
  the whole order says so when it is finished. They are sitting at a
  table with no counter to watch.
- **A minute before it goes.** Send to kitchen opens a window with a
  countdown rather than sending. Anything added during it goes on the
  same order, because nothing has been sent yet; confirming ends the
  minute early and letting it run out sends it anyway. Ordering at a
  table is not one decision, and without this a coffee and the pastry
  remembered ten seconds later were two orders, two numbers and two
  tickets for one table.
- **What this phone has ordered today.** Every order a phone sends is
  listed on its own page under that cafe's code, newest first, with the
  number, the time, the total and a link to each order. A customer who
  ordered twice can say which order was theirs at the counter. The list
  is kept in a cookie on the phone, scoped to that cafe's pages and to
  that day, and every order on it is still checked against the cafe
  before it is shown - an address will not do it, because a cafe's
  tables sit behind one router.
- **The counter's New Order screen.** The menu reads the way the
  customer's does - every section as a tab across the top, All first -
  and each card has the price beside the quantity. On a laptop, desktop
  or TV the order is written beside it like a receipt, always open: a
  line per dish with its own - and +, Clear all, the totals, and a
  button that says what it will charge. On a phone or tablet it is the
  sheet behind the Order Summary button, as before.
- **Dine-in, takeaway or delivery.** New Order says which it is, at the
  top of the receipt. Takeaway and delivery carry a container charge the
  owner sets under Profile -> Packing Charges - one for each, each either
  once per order or once per item. It is its own line on the bill, added
  after the discount and the tax, and the kind of order is on the bill,
  the kitchen ticket (marked to be packed) and the kitchen screen.
  Dine-in never carries it, nor does an order from the table QR.
- **Veg or non-veg.** Chosen on Add and Edit Food and marked beside the
  dish the way India's menus print it - a green square with a dot, a
  brown-red one with a triangle - at the counter, in Food Management, in
  Inventory, on the customer's menu (which can be narrowed to either) and
  on the customer's own order. A dish nobody
  has marked shows no mark rather than a guess.
- **The customer's menu, narrowed to Veg or Non-veg.** Veg shows the veg
  dishes and nothing else; Non-veg the same. Each button says how many
  there are, and the one that is on is lifted off the page in glass over
  its own colour - green for veg, red for non-veg - so which the menu is
  showing is plain at a glance. A section with nothing of that kind
  loses its tab (and the menu goes back to All if it was the one open),
  and the search and the filter work together. A name typed is looked for
  on the whole menu, whichever tab is open - All lights up to say so -
  and a dish the Veg or Non-veg button is hiding is said to be there
  ("is on the menu, but not as veg"), with Show it, rather than missing. The filter once marked
  dishes hidden and left them all on screen: a dish is a flex row, and a
  page's own `display` beats the `hidden` attribute, so public.css now
  says `[hidden] { display: none !important }` for every customer page.
- **A bell on the customer's pages.** Top right of the menu and of Your
  orders, beside the cafe's name: what this phone has ordered here today,
  each with Being made, Ready or Cancelled, and a number on the bell for
  those still being made. While any is, the page asks its status every
  fifteen seconds (and whenever the phone comes back to the page) - one
  small query each - and when one is ready the bell rings and says so,
  so somebody reading the menu for a second round hears that the first
  is waiting. Nothing extra is fetched to draw it: the orders are the
  ones the page already had. The menu itself no longer opens with a strip
  of today's numbers - they are under the bell - so it starts with the
  menu; the page after a new order still names the earlier ones.
- **Twenty people, one QR code.** The code only says which cafe it is;
  it knows nobody. Each order is given its own random reference
  (`secrets.token_urlsafe(12)`, not the order number, which counts up),
  and the phone that placed it keeps that reference in a cookie of its
  own - HttpOnly, for this cafe's pages only, for today only. The bell
  and Your orders list the references this phone holds and nothing
  else, each checked against the cafe and the day, so twenty phones at
  one table see twenty separate lists, and the kitchen twenty separate
  orders, #1 to #20. Two people sharing one phone share its list.
  qr_order_test and customer_menu_browser_test both place orders from a
  second phone and check it sees only its own.
- **An order found again on another phone or browser.** Cookies cleared,
  or the code scanned with a different browser than the one the order
  was sent from (a camera app's own browser, then Chrome), and the bell
  starts empty. So every order has a four-digit code beside its number -
  on its page and on Your orders - and under the bell, "Ordered on
  another phone or browser?" takes the two and brings the order onto
  this phone, straight to its page. The number alone could not do it:
  numbers count up, so anybody could type the next table's. The code is
  worked out from the order's random reference under the secret key
  (`order_code()`), so nothing is stored and nothing migrates. Wrong
  guesses are counted by the sign-in lockout's own table and rules -
  five in fifteen minutes shuts that address out of that cafe's search
  for fifteen more, and nobody else - against odds of one in ten
  thousand a guess. Only this cafe's orders, only today's, only ones
  sent from a table. Opening an order's own link on another phone still
  adds it there too. `find_order_test.py` covers it.
- **Glass for what is chosen, and where to type.** Every search box - the
  top bar, the lists, New Order's menu, Search, the team list, the
  customer's menu - is glass: lit along its top edge, shaded along its
  foot, with no shadow cast on the page; typing in one draws a ring round
  it (round the whole bar, button and all, in the top bar). The page the
  sidebar is on is the same glass over the accent colour, lifted off the
  page with its shadow; the page the profile menu is on is that glass
  flat, with no shadow, since the menu already floats. The profile menu
  is part of the shell, which page swaps do not redraw, so its mark
  follows the address on every swap. The recipe is `--glass-*` in
  theme.css, once, so the staff pages and the customer's menu cannot
  drift apart.
- **A picture for every dish.** A dish without a photo gets a drawing of
  what it is, on a tile in its own colour: a latte a cup, a dosa a dosa,
  a Coca-Cola a can, a Bisleri a bottle - thirty-two of them, read from
  the name (then the category, then the description), with a tag that
  says what each is. "Cold Coke" is still a can; an iced latte is a cold
  glass. Add and Edit Food show the picture as the name is typed, and
  tick Veg or Non-veg from the name - chicken, egg, fish; paneer, a soft
  drink - until somebody chooses.
- **New Order on a phone reads like a delivery app.** The order is a bar
  across the bottom; a Menu button above it at the right opens a card of
  the menu's sections. All is the categories only - Hot selling has its
  own tab rather than repeating its dishes at the top.
- **Reviews from the table.** Once a table-QR order is ready, the
  customer's phone asks for stars on each dish and on the visit, a
  comment, and their name (kept on the phone for next time). Sending
  again changes the review rather than adding one. The dashboard charts
  every rated dish on the same five-star scale, best at the top, with
  the best and the lowest named; the Reviews page - open to everyone who
  serves customers - has the averages, how the stars fall, and each
  review with who wrote it and the bill it was on.
- **Tasks: the pages each teammate can open.** Every page in the
  sidebar is a task - Dashboard, New Order, Kitchen, Billing, Reviews,
  Categories, Food Management, Inventory, Reports. Add and Edit User
  tick the ones a teammate does (a new one starts with the pages staff
  always had). Their sidebar shows only those, signing in opens the
  first of them - a cashier given Billing and Reviews lands on Billing -
  and any other page quietly takes them there, with no "you do not have
  permission" on the way in. Their first-time tour covers only their
  pages, and search finds only what they can open. User Management and
  the settings are never a teammate's task: whoever can add users could
  make themselves an admin. Admins have every task.
- **Ordering from the table, eaten here or taken away.** The customer's
  menu asks Dine-in or Takeaway beside the total; a takeaway carries the
  café's packing charge, says so on the order's page, and the bill the
  counter raises later charges what the customer was shown.
- **Table ordering can be paused.** Anyone on shift switches it off or on
  from the profile menu; the switch reads a green On or a red Off, drawn
  from the server's answer without reloading the page. Everyone else on
  shift gets a one-second notice when it changes (every open page asks
  the order-status feed, which carries the switch). Customers still see
  the menu, with a kind note asking them to order at the counter, and
  nothing is sent until it is back on.
- **Notices float for a second.** Every message a page brings, and the
  shell's own, floats under the header as one notice and goes after a
  second - nothing on the page moves for it. Pages used to print their
  messages a second time in blocks of their own.
- **A busy kitchen, said kindly.** After a table order, when the kitchen
  has more than three orders waiting or more than one big order (three
  or more items) came in within fifteen minutes - from the table or the
  counter - the order's page says the food may take a little longer and
  thanks them for their patience. Not once it is ready.
- **Signing in names the café.** The sign-in form asks for the café or
  restaurant first, then the username or email and the password. The
  name decides whose place it is - the one it registered with, or the
  one it shows in its corner, typed in any case or spacing - and the
  username whose account there. A wrong name is refused with the same
  words as a wrong password, and counts towards the lockout the same
  way. Remember me keeps the café's name too.
- **A switched-off account is told why.** Somebody whose account the
  owner or an admin has deactivated is told so - but only once they have
  given the right café name, username and password; anything less gets
  the usual message, so nobody can list a café's staff by guessing. Signed
  in when it happens, they are signed out on their next page and told.
- **Forgot password is for owners and admins.** Staff are told, kindly,
  that their owner or admin sets a new one for them from User
  Management; everyone can still change their own under Change Password.
- **The eye on User Management.** A password an admin sets for a
  teammate is shown behind an eye in the team list, fetched only when
  the eye is pressed and hidden again after half a minute. It is kept
  encrypted (`cryptography`'s Fernet, with `PASSWORD_VIEW_KEY` or a key
  drawn from `SECRET_KEY`) beside the one-way hash that sign-in checks.
  A password somebody chooses for themselves - the owner's own, Change
  Password, a reset - is never kept, and choosing one discards what was.
- **Help while an account is set up.** Register and Add User say "That
  username is already taken" as it is typed, and offer free ones near
  it. Every field where a password is chosen reads it as Weak, Medium or
  Strong, and says what would make it harder to guess.
- **Search remembers.** The search page lists what this person looked
  for lately, each with a delete and a Clear all, has an x to empty the
  box, and a Back link.
- **A header across the top, a guide down the side.** The header runs
  the full width of every page, the way YouTube's does: the menu button
  and the café's name at the left, search in the middle, and New order,
  full screen, Order Status (a bell with the count of orders waiting)
  and the profile picture at the right. It is the page's own colour with
  no line under it, and turns to frosted glass once the page scrolls
  under it. Down the side of a laptop or desktop is a slim rail of icons
  with their names under them; the menu button opens it out into the
  full guide, in sections, and folds it back, and the choice is kept for
  that browser. On a phone or tablet the side is a drawer, and search is
  an icon that opens across the header.
- **The team.** User Management lists each person with their picture or
  initials, username, role (the café's owner marked as Owner), when they
  last signed in and whether they can, with Edit, Deactivate and Delete in
  a menu at the end of the row - Delete never offered for yourself or the
  owner. Search and a role filter narrow the list in place, and a role
  overview says what each role can reach and how many hold it. Managers,
  cashiers and staff reach the same pages, and the overview says so.
- **Reports.** Pick today, the last 7 or 30 days, this month, all time or
  your own dates. Net sales, orders, the average ticket and cancelled
  orders sit across the top, each against the same number of days before
  (today against this time yesterday). Then sales over time - by the hour,
  day, week or month to suit the period, as sales or as orders - beside
  sales by category as a donut; every dish sold, most first, with how it
  moved; the payment summary by method; the cancelled orders; and stock as
  it stands. Export report downloads the same report as a CSV, and Print
  prints it.
- **Search.** The box in the header finds pages ("tax" finds Tax &
  Discount), dishes by name, description or category, categories, an
  order by the number the kitchen calls ("#12"), and - for an owner -
  people. Everything it offers is something that person can open; `/`
  anywhere goes straight to it.
- **The dashboard.** Sales, orders, the average bill and the stock to
  check across the top, each against the period before ("vs. this time
  yesterday" for today, so a morning never reads as a slump). Then the
  sales curve for the period beside the orders still in the kitchen, and
  the best sellers beside the stock that needs reordering, by name.
  Today, the last 7 days or the last 30, chosen once for the whole page
  and kept for next time. The curve, live orders and best sellers load
  after the page has drawn and refresh every 20 seconds; today's own
  figures and the stock refresh every two.
- **Categories and menu.** Categories, food items with photos, prices.
- **Inventory.** Stock levels drive availability automatically — an item
  at zero stock disappears from the order screen.
- **Whose name is on it.** A café brands its own sidebar, its own
  printed receipt and the page its customers hold — their name, their
  symbol, their colour. Behind every page inside the app sits one mark
  that is not theirs to change: the Refero mark and the product's own
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
  to itself - the tour recording that it was seen. Posts like that used
  to, and that alone was most of why moving around felt slow: measured
  with the database next door, one of them turned a page that painted in
  1ms into 258ms.
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
- **Refero on the tab.** Every page shows the Refero mark as its browser
  icon and ends its title with the name - "Kitchen · Refero" - the way
  any site reads in a tab. The icon is built from the same shapes as
  the mark in the sidebar, drawn heavier for sixteen pixels, and
  /favicon.ico answers with it for bookmarks and history lists.
- **Remember me.** A sign-in ends once it has gone unused for eight
  hours, which is right for a till: in use all day it stays signed in,
  left overnight it is not. Ticking the box on the sign-in page makes
  that a week instead - closing the browser signs nobody out and neither
  does working all week, but a week away does. The window is counted
  from the last request, not from signing in, and enforced on the server
  as well as in the cookie, so a cookie kept past its expiry is not a way
  round it. `SESSION_HOURS` and `REMEMBER_DAYS` set the two. Ticking it
  also keeps the name or email that was typed, so the sign-in page has
  it filled in from then on; the password is left to the browser's own
  password manager, because a site that wrote it down would hand it to
  anybody holding the device. The sign-in page asks that manager for the
  saved password as the name is typed, and offers it the new one to
  save when the box is ticked.
- **Nothing typed is lost.** Every form keeps a copy of what has been
  typed into it on the device, written as it changes. If the page goes
  before it is saved - the browser closed, the power went, the site fell
  over in the middle of a save - the form comes back filled in the next
  time it is opened, and all that is left is to press Save. A copy is
  thrown away only once the save is known to have landed: every save the
  server answers says whether it went through, so one that failed keeps
  its copy, and one that never got an answer comes back with a warning
  that it may already have gone through. Passwords and files are never
  kept, copies belong to the person who typed them, and they last a
  week.
- **Name and symbol in the top corner.** An admin sets the name, the
  tagline and the symbol the sidebar shows, under Profile -> Name & Symbol.
  Left alone it reads "Refero / Calm service, beautifully run" beside the Refero
  mark - a cup of coffee inside the C it is named for - and clearing a
  field puts that default back rather than leaving a blank corner. The same name is in the bar that stays at the top on a
  phone.
- **A customer's bill is dressed for a customer.** The cafe's name in a
  serif, its symbol pale behind the text, a line at the foot chosen from
  the order's own number so a reprint reads the same, and who served it.
- **Saves show at once and go behind.** What a save will do is on screen
  the moment it is pressed; the post goes to the server behind it, in a
  queue, one at a time in the order they were made. Add or edit a food, a
  category or a teammate and the list it leads to (the form's `data-then`,
  fetched ahead while the form is filled in) is shown straight away; the
  server's own copy, with the new row, replaces it when the save lands. A
  Delete takes its row away at once (`data-optimistic="remove"`); switching
  a teammate off shows it at once (`data-optimistic="toggle"`). A quiet
  "Saving..." sits in the header while anything is on its way, and closing
  the tab with a save still going asks first. A save the server refuses
  puts things back - the row returns, the switch flips back, the form comes
  back with what was typed and why. Nothing is sent twice: a save that gets
  no answer is not retried by itself, because it may already have gone
  through; the person is told, and drafts.js still has what they typed.
- **Nothing reloads the whole page.** Following a link swaps only the page
  region, and saving a form now does the same: the post is sent in the
  background and the page that comes back is swapped in, so the sidebar,
  the top bar, the stylesheets and the scripts are never fetched twice.
  The address bar follows the server's redirect, so Back works and a
  refresh does not re-post.
- **No scrollbars.** Everything still scrolls by wheel, trackpad, touch and
  keyboard; the bar is simply not drawn.
- **The sidebar scrolls only when it has to.** The name is a fixed head and
  the page links below it are what scrolls, starting at Dashboard. A screen
  tall enough for every section has no scrollbar at all; a shorter one
  scrolls the links under the name, which does not move.
- **Theme colour.** An admin picks the accent the whole cafe's screens are
  painted in. By default the app is coffee-brown surfaces with latte cream
  and caramel for everything you press (the "Latte" theme).
- **Installs as an app.** A web manifest asking for fullscreen display, so
  "Install" in a browser or "Add to Home Screen" on a phone opens the site
  with no address bar and no browser chrome. In an ordinary tab a button in
  the header fills the screen instead, and remembers the choice.

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
endpoints skips that - the ones a kitchen screen uses as it works, and
taking an order, which writes only orders, stock and bills.
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
that is not costs about what the database round trip it replaces costs -
about 40ms now - and what it would save is small beside the distance
between the people using the app and the server (see A note on speed).

## Money

Prices, tax and discounts are `Decimal` end to end - never float. A
float cannot hold 1299.50 exactly, and a fraction of a rupee out on
every line is how a till stops balancing at the end of a week.

That matters for the offline stand-in too. It used to rewrite
`DECIMAL(10,2)` to `NUMERIC` and store Decimals as float, so a price
with paise came back a float and order creation died on
`Decimal + float` - offline only, since production is Decimal on both
sides. It now declares the column with TEXT affinity, stores the exact
string and converts it back, so a dish priced 1299.50 can be ordered in
a test and three of them come to exactly 3898.50.

One gap, named rather than buried: `PARSE_DECLTYPES` only applies to a
column read straight from a table, so `SUM(...)` and `COALESCE(...)`
still come back as floats in the stand-in where MySQL gives a Decimal.
Every place the app adds money up it does so in Python over rows it has
already read, so nothing reaches that gap today.

## A note on speed

Measured on 30 September 2026. A database round trip from inside the
deployed app costs about 40ms, and taking a pooled connection nothing
(`/healthz` reports `query_ms` and `connect_ms`, so this is checked rather
than guessed at). It was about 543ms before the database moved next to
the app, which is when a page doing four lookups spent over two seconds
travelling. Staff pages now make one to three queries; Billing three.

What is left is the distance to the people using it. From India a
request to the live site takes about half a second, and roughly 0.38s of
that is the trip to the server and back - it is not in India. The
Cloudflare in front of the host caches nothing (`cf-cache-status:
DYNAMIC`), not even the versioned stylesheet, so a phone scanning a table
code for the first time pays that trip for each file too; after that the
browser keeps them for a year. Caching more data on the server would not
touch either: a page that opens from the warm-up cache in a millisecond
has nothing to gain, and a till must not show an old stock count or
bill. The two changes that would are:

- the app **and** the database in the region nearest the customers
  (Singapore, for India) - about 0.3s off every request, but only if they
  move together; the host cannot move a service, so it is a new one;
- the site on its own domain behind Cloudflare with `/static/*` cached at
  the edge - the first visit's files from a nearby city instead.

Billing writes the bills for table orders the first time it is opened
after they arrive. That was two round trips an order; it is two in all
now, however many there are.

The table's pages are where that shows most - every phone at every table,
and each waiting one asking every five seconds whether the food is
ready. So what they all ask the same is kept for a few seconds: the café
behind the code, its menu, its name and logo. It is kept under the café's
own key and forgotten the moment its staff save anything or an order
moves stock (placing an order still reads the café fresh, and checks the
shelf itself), so nothing is shown out of date. With Redis the forgetting
reaches every worker at once; without it, the other workers' copies age
out within `CACHE_SECONDS`. A menu opened again now costs no database
trip, an order's page two or three instead of eight, and the status check
one instead of three. An order writes all its dishes in the same few
trips however many there are, where it was four trips a dish.

The typefaces (Manrope and Fraunces, both open-licensed) are served from
`static/fonts` rather than Google Fonts: two fewer hosts to find and shake
hands with before the first letter can change, nothing said to Google about
each visit, and the reading face preloaded. The picture beside the sign-in
and register forms, which is what Largest Contentful Paint times there, is
an `<img>` in the page with `fetchpriority="high"` rather than a CSS
background the browser only finds late.
The headline face is cut to what the headlines use - weights 500-700 at
one optical size - which halves it, 67KB to 32KB.

On a phone the other half was bytes. Pages, the stylesheet and the
scripts now go out gzipped to any browser that takes it (about a fifth
of the size; files are compressed once per version and kept), the web
fonts load without holding up the first paint, dish photos load as they
come into view, and a touch screen skips the hover effects. Measured on
a throttled phone, the first paint came about 2.3 times sooner. And a
phone, or a slow connection, fetches only the next couple of its pages
ahead (none on Data Saver or a real 2G line); the rest load the moment a
link is touched. "Real" because Chrome's `effectiveType` is worked out
mostly from how long answers have taken: a busy laptop, or a server slow
for a moment, reads as 2G over good wifi - which once switched the
warm-up off in the middle of a test run. A 2G reading only counts when
`downlink` agrees that the line is slow to carry anything.

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
tests/dashboard_feed_test.py The dashboard's figures and the header's search
tests/reports_test.py  Reports: periods, figures, comparisons and the CSV export
tests/shell_browser_test.py Real-browser suite for the header, the guide and the dashboard
tests/print_test.py  Printable bill and kitchen ticket test suite
tests/staff_access_test.py Non-admin permission test suite
tests/billing_paid_test.py Bill settlement test suite
tests/browser_nav_test.py Real-browser navigation test suite
tests/menu_search_test.py Real-browser New Order menu, tabs, search and docked order suite
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
tests/page_head_test.py robots.txt, llms.txt, canonical addresses, landmarks, the description a shared link shows, and the tab
tests/qr_hold_test.py Real-browser suite for the minute before an order is sent
tests/draft_restore_test.py Real-browser suite for forms that survive a closed browser
tests/kot_button_test.py Real-browser suite for the Print KOT buttons
tests/cdp.py         Minimal DevTools-protocol client used by that suite
```

## Security

What is in place, and what each part is actually for.

**Who can change the code.** Only someone who can push to this
repository's `main` branch: the host deploys whatever is pushed there,
and nothing else. The website itself has no way in to its own code -
no page writes a file, nothing a visitor sends is run, uploads are
images only (checked by their bytes, not their name, kept in the
database and served with `nosniff`), and the debugger that would run
code from an error page is never on in production. What a browser
receives - the HTML, the stylesheets, the scripts - anybody can read
and edit in their own browser's tools, as on every website; that
changes their own screen and nothing on the server or anybody else's.
The repository is public, so its code can be read and copied, but not
changed: keep it that way by giving nobody else write access, turning
on two-factor sign-in for GitHub and for the host, and protecting
`main` against force-pushes and deletion. Making the repository private
also stops it being read; the host can still deploy from it once its
GitHub app is allowed the private repository. No secrets are in it -
`.env` has never been committed, and `.env.example` holds placeholders.

**When something goes wrong.** Every error is one page
(`templates/error.html`): the status number as big as a 404 page's
always is, a sentence in plain words, and a way out - back into the
app, to the sign-in, or for a customer back to that cafe's menu. Never
what broke: no traceback, no SQL, no file or table names - those go to
the log, for the developer, and only there. Each error keeps its own
status code (404, 403, 400 for a form left open too long, 405, 429,
500, 503), because a 500 answered as a 404 tells search engines and the
host's health check the wrong thing. The page is drawn without the
database, so it still appears when the database is what failed, and a
few lines of plain HTML stand in if even it cannot be drawn. A save sent
from the page by script is told in a notice and stays on its form; the
API answers in JSON; a missing picture, icon or background warm-up gets
a short 404 rather than a page drawn for nobody. An address that is no
page at all is a 404 signed in or out - a customer who mistypes one is
no longer shown the staff's sign-in.

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

Log out holds. Every response sends the session cookie again, which is
what keeps a till in use signed in, so an answer to something that left
the browser just before Log out used to land afterwards and sign it back
in. Each sign-in now carries an id that the browser also holds in a
cookie of its own, set only by signing in and removed only by Log out; a
session whose id the browser no longer holds is dropped.

**Doing things.** Every POST, PUT, PATCH and DELETE carries a CSRF
token, compared in constant time. Pages are gzipped, and some repeat
what was typed, so the token on each page is masked with fresh random
bytes (the BREACH defence): the same secret underneath, different bytes
every time the page is drawn. An amount typed as `nan` is refused as
not a number rather than crashing the save. Every query is scoped to the café's
owner, so one tenant cannot read or write another's rows by guessing an
id. A teammate reaches only the pages of their own tasks; the check is
on the server, for every request, not just in the sidebar. `?next=` is checked before
any redirect follows it.

**What the browser is allowed to do.** A Content-Security-Policy on
every response, plus `nosniff`, `frame-ancestors 'none'` (and
`X-Frame-Options` for anything that does not read CSP),
`Referrer-Policy`, `Permissions-Policy` and `Cross-Origin-Opener-Policy`.
HSTS is sent in production over HTTPS only. Production is also whatever
runs on a hosting platform (Render, Railway, Heroku, Fly.io name themselves
in the environment): the live site was once found running with the
`APP_ENV=development` line of `.env.example` copied into Render's dashboard
- no HSTS, session cookies without `Secure`, and the keep-awake timer off.
A copied `development` there is now overridden, with a warning in the log;
set `APP_ENV=production` (or leave it out) to make that explicit. Taking a card payment hands
the browser to Razorpay, so that one route gets its own policy rather
than opening the whole app up.

`script-src` carries no `'unsafe-inline'`. Every HTML response makes a
nonce, puts it on each `<script>` its page writes (`csp_nonce()` in the
templates) and names it in the header, so an inline script without it -
one smuggled in through a stored value, say - is refused. instant.js
swaps fetched pages into the *current* document, whose policy names a
different nonce, so it re-creates each swapped-in script under the nonce
of the document it is going into, which it reads from its own tag. There
are no inline event attributes (`onclick=` and the like), which no nonce
can cover: forms that ask first carry `data-confirm`, print buttons
`data-print-now`, and the shell attaches the handlers. `style-src` keeps
`'unsafe-inline'`: the café's colours and a good deal of layout are style
attributes, and CSS runs no code.

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
python tests/smoke_test.py        # expect PASSED: 43   FAILED: 0
python tests/upgrade_test.py      # expect PASSED: 19   FAILED: 0
python tests/instant_nav_test.py  # expect PASSED: 26   FAILED: 0
python tests/instant_post_test.py # expect PASSED: 16   FAILED: 0
python tests/user_delete_test.py  # expect PASSED: 19   FAILED: 0
python tests/hot_sellers_test.py  # expect PASSED: 23   FAILED: 0
python tests/hot_mirror_test.py   # expect PASSED: 20   FAILED: 0
python tests/settings_test.py     # expect PASSED: 104  FAILED: 0
python tests/tablet_layout_test.py # expect PASSED: 65   FAILED: 0
python tests/stock_alert_test.py  # expect PASSED: 33   FAILED: 0
python tests/print_test.py        # expect PASSED: 56   FAILED: 0
python tests/staff_access_test.py # expect PASSED: 33   FAILED: 0
python tests/billing_paid_test.py # expect PASSED: 44   FAILED: 0
python tests/browser_nav_test.py  # expect PASSED: 18   FAILED: 0
python tests/menu_search_test.py  # expect PASSED: 31   FAILED: 0
python tests/stale_banner_test.py # expect PASSED: 11   FAILED: 0
python tests/mobile_nav_test.py   # expect PASSED: 94   FAILED: 0
python tests/theme_test.py        # expect PASSED: 33   FAILED: 0
python tests/theme_browser_test.py # expect PASSED: 13  FAILED: 0
python tests/food_number_test.py  # expect PASSED: 31   FAILED: 0
python tests/qr_order_test.py     # expect PASSED: 146  FAILED: 0
python tests/kitchen_screen_test.py # expect PASSED: 21  FAILED: 0
python tests/password_view_test.py # expect PASSED: 29  FAILED: 0
python tests/fullscreen_test.py   # expect PASSED: 29   FAILED: 0
python tests/tutorial_test.py     # expect PASSED: 40   FAILED: 0
python tests/tour_browser_test.py # expect PASSED: 26  FAILED: 0
python tests/nav_cache_test.py    # expect PASSED: 7    FAILED: 0
python tests/timezone_test.py     # expect PASSED: 41   FAILED: 0
python tests/clock_browser_test.py # expect PASSED: 6   FAILED: 0
python tests/colours_test.py      # expect PASSED: 54   FAILED: 0
python tests/colour_browser_test.py # expect PASSED: 14  FAILED: 0
python tests/list_search_test.py  # expect PASSED: 109  FAILED: 0
python tests/identity_test.py     # expect PASSED: 97   FAILED: 0
python tests/security_test.py     # expect PASSED: 146  FAILED: 0
python tests/page_head_test.py    # expect PASSED: 101   FAILED: 0
python tests/qr_hold_test.py      # expect PASSED: 24   FAILED: 0
python tests/draft_restore_test.py # expect PASSED: 36  FAILED: 0
python tests/kot_button_test.py   # expect PASSED: 26   FAILED: 0
python tests/dashboard_feed_test.py # expect PASSED: 43  FAILED: 0
python tests/reports_test.py      # expect PASSED: 39   FAILED: 0
python tests/shell_browser_test.py # expect PASSED: 66  FAILED: 0
python tests/order_type_test.py   # expect PASSED: 45   FAILED: 0
python tests/review_test.py       # expect PASSED: 46   FAILED: 0
python tests/menu_marks_test.py   # expect PASSED: 99   FAILED: 0
python tests/compression_test.py  # expect PASSED: 42   FAILED: 0
python tests/username_check_test.py # expect PASSED: 34  FAILED: 0
python tests/account_fields_browser_test.py # expect PASSED: 27  FAILED: 0
python tests/sign_in_cafe_test.py # expect PASSED: 49   FAILED: 0
python tests/table_order_test.py  # expect PASSED: 41   FAILED: 0
python tests/tasks_test.py        # expect PASSED: 45   FAILED: 0
python tests/customer_speed_test.py # expect PASSED: 14  FAILED: 0
python tests/table_switch_browser_test.py # expect PASSED: 12  FAILED: 0
python tests/save_queue_browser_test.py # expect PASSED: 22  FAILED: 0
python tests/customer_menu_browser_test.py # expect PASSED: 69  FAILED: 0
python tests/find_order_test.py   # expect PASSED: 34  FAILED: 0
python tests/error_pages_test.py  # expect PASSED: 36  FAILED: 0
python tests/subscription_test.py # expect PASSED: 83  FAILED: 0
python tests/console_test.py      # expect PASSED: 50  FAILED: 0
```

All fifty-eight run in memory against a SQLite stand-in — no database or
network needed. The twenty-four that drive a browser use a headless Edge or
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

`stock_alert_test.py` covers the dashboard's Stock to check panel, which
names the food that needs reordering rather than only counting it. Checks that zero
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

Nothing prints by itself. A kitchen ticket comes out when somebody presses
Print KOT - on each ticket on the Kitchen screen, on each row of Order
Status, and on New Order straight after an order goes - and a bill when
somebody presses Print Bill, on Billing or on the order's own page. There used to be automatic printing,
with a settings page, a kitchen screen that checked in so the tills would
stand down, and a queue the screens claimed tickets from; all of it is
gone. `print_test.py` checks that no page prints on its own and that both
buttons lead to real, printable pages, and `kitchen_screen_test.py` checks
in a real browser that a ticket arriving on the kitchen screen reaches no
printer. `kot_button_test.py` presses each Print KOT button and checks it
opens that order's ticket, leaves the order as it was, and says so when the
browser blocks the pop-up. The old settings columns are left in the database, unread.

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

Billing's Print Bill sits beside the payment status, where the money is
already being taken, and opens the bill where it can be read.

## Known limitations

- **Usernames are unique platform-wide**, not per cafe. Two cafes cannot
  both have a user called `admin`. The café's name on the sign-in form
  is checked against the account's own café when given; one that leaves
  it out (an older page, a script) signs in on the username alone.
  Café names are not unique, and the name is not a secret, so it is a
  check of the right place, not a second password.
- **CSRF tokens are injected into forms by JavaScript** in `base.html`.
  Forms built dynamically after page load need the token added manually,
  and the app will not accept form submissions with JavaScript disabled.
- **Images are stored in the database** as `MEDIUMBLOB`. This is the
  right trade-off on hosts with ephemeral disks and no object storage,
  but at large scale you would move them to S3 or similar.
- There is no billing/subscription layer for the cafes themselves —
  signup is open to anyone with the URL.
