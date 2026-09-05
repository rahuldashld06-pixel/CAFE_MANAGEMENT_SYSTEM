# What was broken, and how it was fixed

Every item below was present in the original upload. Grouped by severity.

---

## Blockers — the app could not run on a fresh database

### 1. No schema was ever created

`ensure_auth_schema()` ran `ALTER TABLE categories`, `ALTER TABLE foods`
and `ALTER TABLE orders` against tables it never created. On an empty
database every request died with *table doesn't exist*. The README told
you to import a MySQL Workbench "structure only" dump — that dump was
not in the project, so there was no path to a working deployment.

**Fixed.** `ensure_auth_schema()` now creates all nine tables plus their
columns and indexes, idempotently, before running any migration. A fresh
database works on first request. `docs/schema.sql` mirrors it for manual
provisioning.

### 2. Category management did not exist

`categories.html`, `add_category.html` and `edit_category.html` all
shipped and referenced `url_for('categories')`, `add_category`,
`edit_category` and `delete_category` — none of those routes existed,
and there was no navigation link. Since a food item requires a category
and a new cafe starts with none, **a newly registered tenant could never
add a menu item, and therefore never take an order.**

**Fixed.** All four routes implemented, with a Categories nav link for
admins. Category names are unique per cafe rather than globally, and
deletion is refused while food items still reference the category.

### 3. Creating a staff user always failed

```sql
INSERT INTO users
    (username, password_hash, full_name, role, is_active, phone_number)
VALUES (%s, %s, %s, %s, 1, %s, %s)
```

Six columns, seven values, and `cafe_id` missing entirely. Every attempt
to add a manager, cashier or staff account failed with *column count
doesn't match value count*.

**Fixed.** Column list corrected and `cafe_id` included.

---

## Security — cross-tenant access and account takeover

### 4. `edit_user` had no tenant filter

The lookup matched on `user_id` alone. Any cafe admin could edit any
user in any other cafe by changing the id in the URL — including
resetting their password. Complete takeover of every other tenant on
the platform.

**Fixed.** Lookup and both UPDATE statements now filter on `cafe_id`.

### 5. `toggle_user` had no tenant filter

Same flaw, for activate/deactivate.

**Fixed.** Filters on `cafe_id`, and the target is verified before the
update.

### 6. Branding was a single shared file

Every cafe wrote to one `cafe_branding.json` at the project root, with
fixed image filenames `cafe_logo.<ext>` and `cafe_login.<ext>`. Each
cafe overwrote every other cafe's name and logo — the exact opposite of
what the documentation claimed. The sign-in page showed whichever tenant
had saved most recently.

**Fixed.** Branding lives on the tenant's `cafes` row, images included,
served through a cache-busted `/media/cafe/<id>/<kind>` route. Pre-login
pages show the platform name instead of leaking a tenant's branding.

### 7. Password reset was an account-takeover form

Reset required only a username and a full name. Full names are displayed
throughout the UI — order lists, user management — so anyone who could
see a colleague's name could seize their account.

**Fixed.** The account's registered mobile number is now required,
compared on the last 10 digits so `+91`/`0` prefixes both match, using a
constant-time comparison. Failures are deliberately vague so the form
cannot be used to confirm usernames or numbers. Accounts with no number
on file must be reset by an admin.

### 8. Default signing key

`app.secret_key` fell back to `"dev-only-change-me"`. Anyone knowing the
default could forge a session cookie for any cafe.

**Fixed.** The app refuses to start in production without `SECRET_KEY`.

### 9. No lockout protection

Nothing stopped an admin demoting or deactivating the last admin,
leaving the cafe permanently unable to reach User Management, Branding
or Reports.

**Fixed.** Both paths refuse when no other active admin remains.

---

## Reliability

### 10. Unrecoverable 500 loop on a stale session

`get_current_user()` called `require_cafe_session()`, which raised a bare
`RuntimeError` — from inside both a `before_request` hook *and* a
template context processor. Flask surfaced it as a 500 on every page,
including the one the user needed to sign out.

**Fixed.** A dedicated `TenantSessionError` with an error handler that
clears the session and redirects to sign-in. `get_current_user()` returns
`None` rather than raising.

### 11. `before_request` hooks ran in the wrong order

`attach_cafe_to_session` existed to repair a session missing `cafe_id`,
but Flask runs hooks in registration order and it was registered second —
so `require_login` always blew up first. The repair hook was dead code.

**Fixed.** Merged into `require_login`, resolving the cafe before the
user lookup.

### 12. ~10 database connections per page load

Every route, and every helper inside it, opened its own connection.
Managed MySQL plans allow roughly 20 concurrent connections, so a
handful of simultaneous users produced *Too many connections*.

**Fixed.** One pooled connection per request, shared through a wrapper
whose `close()` defers to teardown. Cursors are buffered by default,
which also removes any *Unread result found* risk.

### 13. Uploads were lost on every deploy

Food photos and logos were written under `static/uploads/`. Render,
Railway and Heroku give each deploy a fresh, empty filesystem.

**Fixed.** Images are stored in the database and served through
tenant-scoped routes with immutable cache headers and a version counter
for cache busting.

### 14. Unvalidated form input crashed with 400s and 500s

`request.form["food_name"]` raised `KeyError` (a bare 400 page) and
`int(request.form["quantity"])` raised `ValueError` — neither caught by
the `mysql.connector.Error` handler.

**Fixed.** `form_text()`, `form_int()` and `form_decimal()` helpers with
clear messages, applied across `add_food`, `edit_food` and
`update_stock`.

### 15. Duplicate, broken payment migration

`ensure_payment_schema()` probed `INFORMATION_SCHEMA` with a
non-dictionary cursor and crashed on `fetchone()[0]`. **This was found by
the new test suite, not by reading the code.**

**Fixed.** It now delegates to `ensure_auth_schema()`, which already
creates those columns — one migration path instead of two.

### 16. OTP expiry compared against the wrong clock

`otp_row["expires_at"] < datetime.now()` compared a database timestamp
to the web server's clock. With the app and database in different time
zones, codes arrived already expired or stayed valid for hours.

**Fixed.** Expiry is evaluated by the database via `NOW()`.

### 17. Flash messages were invisible on many pages

Roughly half the templates never called `get_flashed_messages()`, so
errors and confirmations on those pages were silently discarded — the
user saw a no-op.

**Fixed.** Rendered centrally in `base.html`. Pages with their own flash
block still work, since the queue is consumed once.

### 18. Foods vanished when a category was removed

The food list used `JOIN categories`, so any item whose category was
deleted or null disappeared from the menu entirely.

**Fixed.** Changed to `LEFT JOIN`.

### 19. Stock edits silently discarded

`edit_food` updated `inventory` with a plain `UPDATE`. A food item
without a stock row matched zero rows and the entered quantity was
thrown away with a success message.

**Fixed.** Upsert via `INSERT ... ON DUPLICATE KEY UPDATE`, backed by a
new unique key on `inventory.food_id`.

### 20. Cafe owner id could drift

`get_cafe_owner_id()` fell back to "oldest admin" without persisting it.
Deactivating the owner and promoting someone else would change the id
that every food, category and order row is filtered by — **the cafe's
entire menu and order history would appear to vanish.**

**Fixed.** The resolved owner is written back to `cafes.owner_user_id`.

### 21. Raw errors and stack traces shown to users

Handlers flashed `f"Database error: {error}"`, exposing SQL and schema
details, and unhandled exceptions rendered tracebacks.

**Fixed.** Handlers for 404, 413, 500 and uncaught exceptions log the
detail server-side and show a plain message. `/healthz` added for the
host's health check.

### 22. Other hardening

`ProxyFix` so redirects and secure cookies work behind Render's TLS
terminator; `MAX_CONTENT_LENGTH` on uploads; logging configured so
gunicorn captures `app.logger` output; a `__main__` block that never
enables debug mode in production.

---

### 23. Missing unique constraints on an upgraded database

`CREATE TABLE IF NOT EXISTS` cannot add a constraint to a table that
already exists, so a database carried over from the older single-cafe
app kept `inventory` and `bills` with no unique key. Without
`inventory(food_id)`, editing a food item's stock inserts a second
inventory row instead of updating the first, so the item reports two
different stock levels. Without `bills(order_id)`, the "create any
missing bills" pass on the Billing page can add a duplicate bill every
time the page is opened, inflating revenue.

The first version of this fix checked whether the constraint existed **by
name**, which was wrong: a database whose old schema already enforced
uniqueness through a differently-named index (commonly just `food_id`)
would get a second, functionally identical index added on every fresh
deployment — MySQL warning 1831.

**Fixed.** Existence is now determined by shape, not name:
`_unique_index_on_column()` asks `INFORMATION_SCHEMA.STATISTICS` for any
index on the table with `NON_UNIQUE = 0` that covers exactly that one
column. A composite unique index on `(food_id, counted_on)` correctly
does *not* satisfy it, since that does not make `food_id` unique on its
own. When an existing index already does the job it is left in place and
logged. `_ensure_unique_keys()` adds both constraints automatically,
collapsing any pre-existing duplicates first. The survivor is chosen by
an explicit rule rather than by row id: newest stock count for
inventory, and for bills a settled row always outranks an unsettled one,
so a duplicate can never cost you a payment record. If an order has more
than one row that genuinely looks paid, the migration deletes nothing,
logs the query to investigate, and lets the app boot — that is an
accounting question, not something a migration should decide.

## Verification

`tests/smoke_test.py` runs offline against a SQLite stand-in for
`mysql.connector`. It takes two cafes through registration, category and
menu setup, staff creation, ordering, billing and reports, then attempts
cross-tenant reads and writes between them.

37 assertions, all passing — including direct regression tests for
items 1, 2, 3, 4, 5, 6, 7, 9, 10 and 14.

`tests/upgrade_test.py` covers item 23 separately: it builds a
legacy-shaped database with duplicate inventory rows and duplicate
bills, runs the upgrade, and asserts the right rows survive, the
constraints are installed, a re-run is a safe no-op, and two paid bills
for one order are left untouched. It also covers the 1831 case directly:
a pre-existing unique index under a different name is detected and
honoured, and a composite unique index is correctly rejected.
19 assertions, all passing.
