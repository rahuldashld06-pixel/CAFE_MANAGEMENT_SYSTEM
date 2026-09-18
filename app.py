from functools import wraps
import json
import logging
import secrets
import hashlib
import hmac
import threading
import time
from datetime import date, datetime, timedelta
from flask import (
    Flask, render_template, request, redirect, url_for, flash, session, g,
    jsonify, abort, has_request_context, send_file
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix
import io
import os
import mysql.connector
from decimal import Decimal, InvalidOperation
try:
    import razorpay
except ImportError:
    razorpay = None

# Load a local .env file when python-dotenv is installed. On Render/Railway the
# real environment variables are already set, so this is a no-op there.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# --- App configuration (DB + Razorpay) -------------------------------
# Works two ways so nothing breaks anywhere:
#   1) Locally: if a config.py file exists (it's gitignored and never
#      pushed), its values are used automatically, same as before.
#   2) On any host (Railway, Render, etc.): config.py won't exist there,
#      so we fall back to reading everything from environment variables
#      that you set in that host's dashboard.
# Environment variables always win if both are present.
try:
    from config import (
        DB_CONFIG as _FILE_DB_CONFIG,
        RAZORPAY_KEY_ID as _FILE_RAZORPAY_KEY_ID,
        RAZORPAY_KEY_SECRET as _FILE_RAZORPAY_KEY_SECRET,
        RAZORPAY_WEBHOOK_SECRET as _FILE_RAZORPAY_WEBHOOK_SECRET,
    )
except ImportError:
    _FILE_DB_CONFIG = {}
    _FILE_RAZORPAY_KEY_ID = ""
    _FILE_RAZORPAY_KEY_SECRET = ""
    _FILE_RAZORPAY_WEBHOOK_SECRET = ""

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", _FILE_DB_CONFIG.get("host", "localhost")),
    "user": os.environ.get("DB_USER", _FILE_DB_CONFIG.get("user", "root")),
    "password": os.environ.get("DB_PASSWORD", _FILE_DB_CONFIG.get("password", "")),
    "database": os.environ.get("DB_NAME", _FILE_DB_CONFIG.get("database", "cafe_management")),
    # Asked for here rather than assigned afterwards. Setting
    # connection.autocommit runs a SET on the server and reading it
    # runs a SELECT, so a pool that touched the property on every
    # checkout would pay a round trip to change nothing. Given here,
    # the connector applies it once while the connection is being
    # established, and again by itself after any reconnect.
    "autocommit": False,
}
_db_port = os.environ.get("DB_PORT", _FILE_DB_CONFIG.get("port"))
if _db_port:
    DB_CONFIG["port"] = int(_db_port)

# TLS. Managed providers (Aiven, PlanetScale, DigitalOcean) require an
# encrypted connection and publish a CA certificate for verifying it.
# mysql-connector negotiates TLS on its own, but without a CA it cannot
# confirm it is really talking to your database rather than something in
# the middle - so point DB_SSL_CA at the downloaded ca.pem in production.
_ssl_ca = os.environ.get("DB_SSL_CA", _FILE_DB_CONFIG.get("ssl_ca", "")).strip()
if _ssl_ca:
    if not os.path.isabs(_ssl_ca):
        _ssl_ca = os.path.join(os.path.dirname(os.path.abspath(__file__)), _ssl_ca)
    DB_CONFIG["ssl_ca"] = _ssl_ca
    DB_CONFIG["ssl_verify_cert"] = (
        os.environ.get("DB_SSL_VERIFY", "1") == "1"
    )

# Escape hatch for a local MySQL with no TLS configured at all.
if os.environ.get("DB_SSL_DISABLED", "0") == "1":
    DB_CONFIG["ssl_disabled"] = True

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", _FILE_RAZORPAY_KEY_ID)
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", _FILE_RAZORPAY_KEY_SECRET)
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", _FILE_RAZORPAY_WEBHOOK_SECRET)
# -----------------------------------------------------------------------

app = Flask(__name__)

# Render/Railway/Heroku terminate TLS at a proxy. Without this, url_for(...,
# _external=True) and redirects build http:// URLs and secure cookies are
# never sent back.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

app.config["CAFE_NAME"] = os.environ.get("CAFE_NAME", "Coffeehouse")
app.config["CAFE_LOGO"] = os.environ.get("CAFE_LOGO", "")

# "production" everywhere except your own machine. Controls the fail-fast
# checks below and the secure-cookie default.
APP_ENV = os.environ.get("APP_ENV", "production").strip().lower()
IS_PRODUCTION = APP_ENV not in {"development", "dev", "local", "test"}

_DEFAULT_DEV_SECRET = "dev-only-change-me"
_secret_key = os.environ.get("SECRET_KEY", "").strip()

if not _secret_key:
    if IS_PRODUCTION:
        # Booting with a shared/default key would let anyone forge a session
        # cookie for any café. Fail loudly at start-up instead of silently
        # shipping an insecure deployment.
        raise RuntimeError(
            "SECRET_KEY is not set. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"` "
            "and set it as an environment variable before starting the app."
        )
    _secret_key = _DEFAULT_DEV_SECRET

app.secret_key = _secret_key

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "SESSION_COOKIE_SECURE", "1" if IS_PRODUCTION else "0"
    ) == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(
        hours=int(os.environ.get("SESSION_HOURS", "8"))
    ),
    # Uploaded logos/food photos are stored in the database; cap the request
    # body so a large file cannot exhaust a web worker's memory.
    MAX_CONTENT_LENGTH=int(os.environ.get("MAX_UPLOAD_MB", "5")) * 1024 * 1024,
    TEMPLATES_AUTO_RELOAD=not IS_PRODUCTION,
    # style.css and instant.js are requested on every cold load. They are
    # served with a ?v= stamp taken from their own modification times
    # (see ASSET_VERSION), so a year-long cache is safe: a deploy changes
    # the stamp and browsers fetch the new file immediately.
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=365),
)


def _asset_version():
    """
    Cache-busting stamp for the static files, from their newest mtime.

    Without it the long SEND_FILE_MAX_AGE_DEFAULT above would pin a stale
    stylesheet in every browser that had already loaded the old one.
    """
    newest = 0.0
    static_root = os.path.join(app.root_path, "static")
    for folder, _dirs, files in os.walk(static_root):
        if "uploads" in folder:
            continue
        for name in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(folder, name)))
            except OSError:
                continue
    return str(int(newest))


ASSET_VERSION = _asset_version()


@app.context_processor
def inject_asset_version():
    return {"asset_version": ASSET_VERSION}


def came_from(fallback=None):
    """
    The page someone was looking at before they opened a settings screen.

    Carried as ?next= on the link, put there by the profile menu at the
    moment it is clicked. It cannot be worked out on the server: that menu
    lives in the shell, which instant navigation never re-renders, so the
    server's idea of "the page you are on" there is whatever it was at the
    last full page load.

    Only a path on this site is accepted. Anything that could send someone
    off it is ignored rather than followed - a redirect somebody else
    chose is a fine thing to trick a person with.
    """
    wanted = (request.values.get("next") or "").strip()

    looks_local = (
        wanted.startswith("/")
        and not wanted.startswith("//")
        and "\\" not in wanted
        and "\r" not in wanted
        and "\n" not in wanted
        and len(wanted) <= 300
    )
    if looks_local:
        return wanted

    if fallback:
        return fallback
    if session.get("role") == "admin":
        return url_for("home")
    return url_for("add_order")


def stay_on(endpoint):
    """
    The same settings screen again, still remembering where it was opened
    from - so a refused save does not also lose someone their way back.
    """
    wanted = (request.values.get("next") or "").strip()
    if wanted:
        return url_for(endpoint, next=wanted)
    return url_for(endpoint)


@app.context_processor
def inject_back_url():
    """Where Back and Cancel go on a settings screen."""
    return {"back_url": came_from()}


@app.context_processor
def inject_home_url():
    """
    Where "home" is for whoever is signed in.

    Only an admin can open the dashboard, so for everyone else it is the
    New Order screen - the page the permission guard sends them to anyway.
    Used as the fallback for every Back link, so a staff member who lands
    on a settings page directly is not bounced off the dashboard.
    """
    if session.get("role") == "admin":
        return {"home_url": url_for("home")}
    return {"home_url": url_for("add_order")}


@app.context_processor
def inject_print_settings():
    """Every page needs these to decide whether to arm an automatic print."""
    if not session.get("user_id"):
        return {"print_settings": dict(DEFAULT_PRINT_SETTINGS)}
    return {"print_settings": get_print_settings()}


def _is_prefetch():
    """True for a page instant.js fetched speculatively, not one a user asked for."""
    return request.headers.get("X-Instant-Prefetch") == "1"


def _is_page_request():
    """
    True when this request is a person opening a page.

    Browsers fetch /favicon.ico and friends on their own, and instant.js
    warms pages nobody asked for. Neither should be able to put a message
    in front of the user.
    """
    if _is_prefetch():
        return False
    destination = request.headers.get("Sec-Fetch-Dest")
    if destination:
        return destination == "document"
    # Older browsers send no Sec-Fetch-Dest; a trailing file extension is
    # the next best signal that this is a subresource, not a page.
    return "." not in request.path.rsplit("/", 1)[-1]


@app.before_request
def _hide_flashes_from_prefetch():
    """
    Render a speculative fetch as though no message were queued.

    instant.js warms the sidebar pages in the background and *caches the
    HTML it gets back*, showing it later when the user clicks that section.
    So a flash must not simply survive a warm-up - it must never be drawn
    into one. Rendering it there would both bake a stale banner into the
    cached page (a 404 message reappearing on Inventory, say) and consume
    the queue, so the page the message was actually meant for never showed
    it at all.

    The queue is lifted out for the duration of the request and put back in
    _restore_flashes_after_prefetch.
    """
    if _is_prefetch():
        g._prefetch_flashes = session.pop("_flashes", None)


@app.after_request
def _restore_flashes_after_prefetch(response):
    saved = getattr(g, "_prefetch_flashes", None)
    if saved:
        # Anything queued while the warm-up was in flight keeps its place
        # at the back of the queue rather than being overwritten.
        session["_flashes"] = list(saved) + list(session.get("_flashes") or [])
    return response


@app.after_request
def _no_store_pages(response):
    """
    Rendered pages are per-user and per-café, so they must never be written
    to a disk cache that another sign-in could read back. instant.js keeps
    its own in-memory copy for the length of a session instead.

    Routes that set their own Cache-Control (the immutable /media images)
    are left alone.
    """
    if "Cache-Control" not in response.headers and response.mimetype == "text/html":
        response.headers["Cache-Control"] = "no-store, private"
    return response


if IS_PRODUCTION:
    # gunicorn captures the app logger; without this, app.logger.info(...)
    # (including the OTP dev fallback) is silently dropped.
    app.logger.setLevel(logging.INFO)
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s in %(module)s: %(message)s"
    ))
    app.logger.addHandler(_handler)


def login_required(f):
    """Require an authenticated user before accessing a protected route."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def wants_json_response():
    """True when the current request was made via fetch()/XHR from a popup
    that wants to refresh itself in place, instead of a plain <form> submit
    that expects a full page redirect."""
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


class ValidationError(ValueError):
    """A user-fixable problem with submitted form data."""


def form_text(field, label, required=True, max_length=255):
    """
    Read a text field without raising KeyError on a missing input.

    Reading request.form["x"] directly returns a 400 Bad Request page with no
    explanation whenever a field is renamed, disabled, or dropped by the
    browser, so every field goes through here instead.
    """
    value = (request.form.get(field) or "").strip()
    if required and not value:
        raise ValidationError(f"{label} is required.")
    if len(value) > max_length:
        raise ValidationError(
            f"{label} must be {max_length} characters or fewer."
        )
    return value


def form_int(field, label, minimum=0, maximum=1_000_000, default=None):
    raw = (request.form.get(field) or "").strip()
    if not raw:
        if default is not None:
            return default
        raise ValidationError(f"{label} is required.")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValidationError(f"{label} must be a whole number.")
    if value < minimum or value > maximum:
        raise ValidationError(
            f"{label} must be between {minimum} and {maximum}."
        )
    return value


def form_decimal(field, label, minimum=Decimal("0"), maximum=Decimal("1000000")):
    raw = (request.form.get(field) or "").strip()
    if not raw:
        raise ValidationError(f"{label} is required.")
    try:
        value = Decimal(raw)
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"{label} must be a number.")
    if value < minimum or value > maximum:
        raise ValidationError(
            f"{label} must be between {minimum} and {maximum}."
        )
    return value.quantize(Decimal("0.01"))


def assert_category_belongs_to_cafe(cursor, category_id):
    """
    Reject a category_id that belongs to another café.

    The dropdown only ever offers this café's categories, but the id travels
    in the request body and a crafted POST could otherwise attach a food item
    to another tenant's category.
    """
    cursor.execute("""
        SELECT category_id FROM categories
        WHERE category_id = %s AND user_id = %s
    """, (category_id, scope_user_id()))
    if not cursor.fetchone():
        raise ValidationError("Please choose a valid category.")


class _SharedConnection:
    """
    Wraps the one real connection held for the current request.

    Every route in this file opens a connection and closes it in a `finally`
    block, and several routes nest helpers that do the same. Handing each of
    them the same underlying connection (whose close() is a no-op until the
    request ends) keeps that code untouched while cutting a page load from
    ~10 database connections down to one. Managed MySQL plans allow only a
    handful of concurrent connections, so this is the difference between the
    app running and the app returning "Too many connections".
    """

    def __init__(self, raw):
        self._raw = raw

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def cursor(self, *args, **kwargs):
        # Buffered by default so that fetching one row from a result set and
        # then running another statement on the same connection cannot raise
        # "Unread result found".
        kwargs.setdefault("buffered", True)
        return self._raw.cursor(*args, **kwargs)

    def close(self):
        # Real close happens in teardown_appcontext.
        return None


# A pool of our own, because the connector's own pool does two things on
# every single checkout that together cost more than most pages spend on
# their real work.
#
# It asks the database "are you still there?" before handing a connection
# over - a real round trip, and from this app to this database that is
# about 270ms. And it asks while holding a lock shared by the whole
# process, so when two requests arrive together the second one waits for
# the first one's question before it may even ask its own. Two people
# ordering at the same moment were making each other slower for nothing.
#
# Removing the app's own extra ping earlier only removed the second of
# the two; this one was underneath it the whole time, which is why the
# health check went from about 540ms to about 270ms rather than to nearly
# nothing.
#
# A connection that was in use seconds ago does not need to be asked. So
# the question is only put to one that has sat unused long enough to have
# plausibly been dropped at the far end, and the lock below is held only
# long enough to move a connection on or off a list - never across
# anything that touches the network.

# How long a connection may sit unused before it is worth checking.
#
# Half an hour, not a couple of minutes. A cafe with a handful of orders
# an hour has gaps of several minutes all day, and at two minutes almost
# every real visitor was paying for the check anyway - which is the whole
# thing this is meant to avoid. MySQL drops an idle connection after
# hours, not minutes, so half an hour is still well inside the window,
# and the keep-awake timer touches the database every ten minutes on top
# of that, so in practice this rarely fires at all.
POOL_PING_AFTER_SECONDS = int(
    os.environ.get("POOL_PING_AFTER_SECONDS", "1800"))

DB_POOL_SIZE = int(os.environ.get("DB_POOL_SIZE", "5"))


def _discard(connection):
    """Close a connection we are not keeping, without fuss."""
    try:
        connection.close()
    except Exception:
        pass


class _ConnectionPool:
    """
    Connections nobody is using, each with the time it was handed back.

    Newest first: the one returned most recently is the one least likely
    to have gone stale, so taking from the end means the check above
    almost never has to fire.
    """

    def __init__(self, size):
        self._size = size
        self._free = []
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                entry = self._free.pop() if self._free else None

            if entry is None:
                # Comes back with autocommit already off: it is in the
                # config above, so the connector applies it as part of
                # establishing the connection.
                return mysql.connector.connect(**DB_CONFIG)

            connection, last_used = entry

            if time.time() - last_used <= POOL_PING_AFTER_SECONDS:
                return connection

            try:
                # A reconnect makes a new session, but the connector
                # reapplies the config to it, so autocommit survives.
                connection.ping(reconnect=True, attempts=2, delay=1)
                return connection
            except Exception:
                # Gone. Round again for another free one, or a new one.
                _discard(connection)

    def release(self, connection):
        # A route that opened a transaction and neither committed nor
        # rolled it back would otherwise hand the next request a
        # connection with somebody else's half-finished work sitting on
        # it, ready to be committed by whatever that request does next.
        # In a system where each cafe must only ever see its own rows,
        # that is the one leak worth spending a round trip on - and only
        # when it happens, because asking whether a transaction is open
        # reads a flag the connection already has and costs nothing.
        try:
            if connection.in_transaction:
                connection.rollback()
        except Exception:
            _discard(connection)
            return

        with self._lock:
            if len(self._free) < self._size:
                self._free.append((connection, time.time()))
                return

        _discard(connection)


_CONNECTION_POOL = _ConnectionPool(DB_POOL_SIZE)


def get_db_connection():
    """
    Return this request's database connection.

    Outside a request context (start-up migrations, scripts) a real,
    independently closeable connection is returned instead.
    """
    if not has_request_context():
        return _CONNECTION_POOL.acquire()

    connection = getattr(g, "_db_connection", None)

    # Nothing is checked here. This connection was taken for this same
    # request moments ago; anything beyond that is a live connection
    # being asked, over the network, whether it is alive. A page that
    # calls this five times was paying for five of those.
    if connection is None:
        connection = _CONNECTION_POOL.acquire()
        g._db_connection = connection

    return _SharedConnection(connection)


@app.teardown_appcontext
def _close_db_connection(exception=None):
    connection = getattr(g, "_db_connection", None)
    if connection is None:
        return
    g._db_connection = None

    if exception is not None:
        try:
            connection.rollback()
        except Exception:
            _discard(connection)
            return

    _CONNECTION_POOL.release(connection)


PAYMENT_SCHEMA_READY = False


def ensure_payment_schema():
    """
    Ensure the gateway columns on `bills` exist.

    This used to be a second, independent migration path that probed
    INFORMATION_SCHEMA with a non-dictionary cursor (and crashed on
    `fetchone()[0]`). ensure_auth_schema() now creates every one of those
    columns, so this simply delegates - one migration path, one source of
    truth.
    """
    ensure_auth_schema()


def require_razorpay():
    if razorpay is None:
        raise RuntimeError("Razorpay SDK is not installed. Run: pip install razorpay")
    if not RAZORPAY_KEY_ID or not RAZORPAY_KEY_SECRET:
        raise RuntimeError("Razorpay test/live credentials are not configured in .env.")
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


# ==========================================
# FOOD IMAGE HELPERS
# ==========================================

ALLOWED_IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif"}

_MIME_BY_EXTENSION = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}

MAX_IMAGE_BYTES = int(os.environ.get("MAX_UPLOAD_MB", "5")) * 1024 * 1024


def allowed_image(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_IMAGE_EXTENSIONS
    )


def read_image_upload(file):
    """
    Validate an uploaded image and return (bytes, mime_type).

    Images are kept in the database rather than under static/uploads because
    Render, Railway, Heroku and most container hosts give each deploy a fresh,
    empty filesystem - every logo and food photo uploaded by every café would
    disappear on the next deploy or restart.
    """
    if not file or not file.filename:
        return None, None

    if not allowed_image(file.filename):
        raise ValueError(
            "Invalid image format. Use JPG, JPEG, PNG, WEBP or GIF."
        )

    data = file.read()
    if not data:
        return None, None

    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(
            f"Image is too large. Maximum size is "
            f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB."
        )

    extension = secure_filename(file.filename).rsplit(".", 1)[-1].lower()
    return data, _MIME_BY_EXTENSION.get(extension, "application/octet-stream")


def food_image_url(food_id, version=1):
    """Public URL for a food photo, or None when the food has no image."""
    if not food_id:
        return None
    return url_for("food_image", food_id=food_id, v=version or 1)


@app.route("/media/food/<int:food_id>")
def food_image(food_id):
    """
    Serve a food photo from the database.

    Scoped to the signed-in café so one tenant cannot enumerate another
    tenant's menu images by guessing food ids.
    """
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT image_blob, image_mime
            FROM foods
            WHERE food_id = %s AND user_id = %s
        """, (food_id, scope_user_id()))
        row = cursor.fetchone()
    finally:
        cursor.close()
        connection.close()

    if not row or not row["image_blob"]:
        abort(404)

    response = send_file(
        io.BytesIO(row["image_blob"]),
        mimetype=row["image_mime"] or "image/jpeg",
    )
    # Safe to cache hard: the URL carries an image_version that changes
    # whenever the photo is replaced.
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


def save_food_image(cursor, file, food_id):
    """Store/replace a food photo. Returns True when an image was written."""
    data, mime = read_image_upload(file)
    if data is None:
        return False

    cursor.execute("""
        UPDATE foods
        SET image_blob = %s,
            image_mime = %s,
            image_version = image_version + 1
        WHERE food_id = %s
    """, (data, mime, food_id))
    return True






# ==========================================
# THE CAFE'S OWN QR CODE
# ==========================================
#
# A customer points a phone at the code on the table and lands on that
# cafe's menu. The address carries a random token rather than the cafe's
# row id: ids are guessable, and walking /m/1, /m/2, /m/3 should not be a
# way to browse every cafe on the system.


def new_public_token():
    return secrets.token_urlsafe(18)


def get_public_token(cafe_id, create=True):
    """
    The token in this cafe's QR address, minting one the first time.

    Returns None for a cafe that has none and is not to be given one, so
    a read-only caller cannot accidentally write.
    """
    if not cafe_id:
        return None

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "SELECT public_token FROM cafes WHERE cafe_id = %s", (cafe_id,))
        row = cursor.fetchone()

        if not row:
            return None

        token = (row["public_token"] or "").strip()
        if token or not create:
            return token or None

        token = new_public_token()
        cursor.execute(
            "UPDATE cafes SET public_token = %s WHERE cafe_id = %s",
            (token, cafe_id))
        connection.commit()
        return token
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def cafe_for_token(cursor, token):
    """
    The cafe a QR address belongs to, or None.

    Also hands back the owner id every other query needs to scope by, so
    the public pages never have to guess at it.
    """
    token = (token or "").strip()
    if not token or len(token) > 40:
        return None

    cursor.execute("""
        SELECT cafe_id, cafe_name, owner_user_id, is_active
        FROM cafes
        WHERE public_token = %s
    """, (token,))
    row = cursor.fetchone()

    if not row or not row["is_active"] or not row["owner_user_id"]:
        return None
    return row


def qr_svg(url, box=10, border=2):
    """
    The QR code as an SVG path, drawn here rather than by a web service.

    SVG rather than a bitmap: it stays sharp printed at any size, which
    matters for something taped to a table, and it needs no image library
    on the server.
    """
    import qrcode
    import qrcode.image.svg

    code = qrcode.QRCode(
        box_size=box,
        border=border,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
    )
    code.add_data(url)
    code.make(fit=True)

    buffer = io.BytesIO()
    code.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buffer)
    return buffer.getvalue().decode("utf-8")



# ==========================================
# PLACING AN ORDER
# ==========================================
#
# One implementation, used by the counter and by a customer's phone. The
# two screens look nothing alike, but what happens to stock, to prices and
# to the order rows behind them has to be identical - a QR order that
# checked stock differently would oversell the kitchen.


class OrderError(ValueError):
    """Something a person did wrong, phrased for that person."""


def available_foods_for(cursor, owner_id):
    """Every food a cafe is currently willing to sell, with its stock."""
    cursor.execute("""
        SELECT f.food_id, f.food_name, f.price, f.availability,
               COALESCE(i.quantity, 0) AS stock
        FROM foods f
        LEFT JOIN inventory i ON f.food_id = i.food_id
        WHERE f.availability = 1 AND f.user_id = %s
    """, (owner_id,))
    return cursor.fetchall()


def collect_order_items(foods, wanted):
    """
    Turn "how many of each" into priced lines.

    `wanted` maps food_id to a quantity, however it arrived - a counter
    form or a phone. Prices come from the food rows, never from the
    request: a customer's browser does not get to say what a coffee costs.
    """
    lines = []

    for food in foods:
        raw = wanted.get(food["food_id"], wanted.get(str(food["food_id"]), 0))
        text = str(raw).strip() or "0"

        try:
            quantity = int(text)
        except ValueError:
            raise OrderError("Invalid quantity for %s." % food["food_name"])

        if quantity == 0:
            continue
        if quantity < 0:
            raise OrderError(
                "Quantity cannot be negative for %s." % food["food_name"])
        if quantity > food["stock"]:
            raise OrderError(
                "Not enough stock for %s. Available stock: %s."
                % (food["food_name"], food["stock"]))

        lines.append({
            "food_id": food["food_id"],
            "food_name": food["food_name"],
            "quantity": quantity,
            "price": food["price"],
            "subtotal": food["price"] * quantity,
        })

    if not lines:
        raise OrderError("Please select at least one food item.")

    return lines


def write_order(cursor, owner_id, cafe_id, lines, tax_mult, source="counter"):
    """
    Write one order, its lines, and take the stock off the shelf.

    Stock is read again with FOR UPDATE immediately before it is reduced.
    The check in collect_order_items() is for telling someone early; this
    one is what actually stops two tills selling the last croissant.

    The caller owns the transaction: nothing here commits, so a failure
    half way leaves no order behind.
    """
    subtotal = sum((line["subtotal"] for line in lines), Decimal("0.00"))
    tax = (subtotal * tax_mult).quantize(Decimal("0.01"))
    total = subtotal + tax

    # The number people say out loud. Counted within this cafe's own day,
    # so two cafes both have a number 1 this morning and neither sees the
    # other's.
    #
    # FOR UPDATE, because this is read-then-write and the site runs
    # several workers: a counter order and a phone order landing together
    # would otherwise both read the same highest number and both claim it,
    # and two customers would be waiting for the same number to be called.
    # The lock is held over this cafe's rows for today only, so one cafe's
    # busy lunchtime never makes another wait. Nothing else is read here,
    # so it costs no extra trip to the database.
    today = date.today()
    cursor.execute(
        "SELECT COALESCE(MAX(daily_no), 0) + 1 AS next_no FROM orders "
        "WHERE user_id = %s AND order_day = %s FOR UPDATE",
        (owner_id, today)
    )
    row = cursor.fetchone()
    daily_no = (row["next_no"] if row and row["next_no"] else 1)

    cursor.execute(
        "INSERT INTO orders (total_amount, order_status, user_id, cafe_id, "
        "source, order_day, daily_no) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (total, "Pending", owner_id, cafe_id, source, today, daily_no)
    )
    order_id = cursor.lastrowid

    for line in lines:
        cursor.execute("""
            SELECT i.quantity
            FROM inventory i
            INNER JOIN foods f ON i.food_id = f.food_id
            WHERE i.food_id = %s AND f.user_id = %s
            FOR UPDATE
        """, (line["food_id"], owner_id))
        held = cursor.fetchone()

        if held is None:
            raise OrderError(
                "Inventory record not found for %s." % line["food_name"])
        if held["quantity"] < line["quantity"]:
            raise OrderError(
                "Not enough stock for %s. Available: %s."
                % (line["food_name"], held["quantity"]))

        cursor.execute("""
            INSERT INTO order_items
            (order_id, food_id, item_name, quantity, price, subtotal)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (order_id, line["food_id"], line["food_name"],
              line["quantity"], line["price"], line["subtotal"]))

        # The WHERE carries the check as well as the change, so two tills
        # racing for the last croissant cannot both win: whichever runs
        # second matches no row.
        cursor.execute("""
            UPDATE inventory
            SET quantity = quantity - %s,
                last_updated = CURRENT_TIMESTAMP
            WHERE food_id = %s
              AND quantity >= %s
              AND EXISTS (
                  SELECT 1 FROM foods f
                  WHERE f.food_id = inventory.food_id AND f.user_id = %s
              )
        """, (line["quantity"], line["food_id"], line["quantity"], owner_id))

        if cursor.rowcount != 1:
            raise OrderError(
                "Could not update inventory for %s." % line["food_name"])

        # Stock at zero takes the food off the menu by itself.
        cursor.execute("""
            UPDATE foods f
            INNER JOIN inventory i ON f.food_id = i.food_id
            SET f.availability = CASE WHEN i.quantity > 0 THEN 1 ELSE 0 END
            WHERE f.food_id = %s AND f.user_id = %s
        """, (line["food_id"], owner_id))

    return {
        "order_id": order_id,
        "daily_no": daily_no,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }



# ==========================================
# MULTI-USER AUTHENTICATION / DATA ISOLATION
# ==========================================

AUTH_SCHEMA_READY = False

# Every table this application needs, in dependency order. Creating them here
# means a brand-new empty database works on first boot: no MySQL Workbench
# "structure only" dump has to be imported by hand, and no request can fail
# because a table the migration tries to ALTER does not exist yet.
_CORE_TABLES = [
    ("cafes", """
        CREATE TABLE IF NOT EXISTS cafes (
            cafe_id INT AUTO_INCREMENT PRIMARY KEY,
            cafe_name VARCHAR(150) NOT NULL,
            owner_user_id INT NULL,
            logo_mime VARCHAR(80) NULL,
            logo_blob MEDIUMBLOB NULL,
            login_photo_mime VARCHAR(80) NULL,
            login_photo_blob MEDIUMBLOB NULL,
            branding_version INT NOT NULL DEFAULT 1,
            brand_name VARCHAR(150) NULL,
            brand_tagline VARCHAR(150) NULL,
            public_token VARCHAR(40) NULL,
            kitchen_seen_at DATETIME NULL,
            tax_percent DECIMAL(5,2) NOT NULL DEFAULT 5.00,
            theme VARCHAR(20) NOT NULL DEFAULT 'copper',
            auto_kot_enabled TINYINT(1) NOT NULL DEFAULT 0,
            auto_kot_delay INT NOT NULL DEFAULT 5,
            auto_bill_enabled TINYINT(1) NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active TINYINT(1) NOT NULL DEFAULT 1
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
    ("users", """
        CREATE TABLE IF NOT EXISTS users (
            user_id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(80) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            full_name VARCHAR(120) NOT NULL,
            role ENUM('admin','manager','cashier','staff') NOT NULL DEFAULT 'staff',
            is_active TINYINT(1) NOT NULL DEFAULT 1,
            phone_number VARCHAR(20) NULL,
            cafe_id INT NULL,
            photo_mime VARCHAR(80) NULL,
            photo_blob MEDIUMBLOB NULL,
            photo_version INT NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_users_cafe_id (cafe_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
    ("categories", """
        CREATE TABLE IF NOT EXISTS categories (
            category_id INT AUTO_INCREMENT PRIMARY KEY,
            category_name VARCHAR(120) NOT NULL,
            description TEXT NULL,
            user_id INT NULL,
            cafe_id INT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_categories_user_id (user_id),
            INDEX idx_categories_cafe_id (cafe_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
    ("foods", """
        CREATE TABLE IF NOT EXISTS foods (
            food_id INT AUTO_INCREMENT PRIMARY KEY,
            -- The number shown on the Food Management list. Counts from
            -- 1 within each cafe and is handed back out when a food is
            -- deleted, so the list never grows holes. Deliberately not
            -- the primary key: order history points at food_id, and a
            -- reused primary key would silently re-label old bills.
            food_no INT NULL,
            category_id INT NULL,
            food_name VARCHAR(150) NOT NULL,
            description TEXT NULL,
            price DECIMAL(10,2) NOT NULL DEFAULT 0.00,
            availability TINYINT(1) NOT NULL DEFAULT 1,
            image_mime VARCHAR(80) NULL,
            image_blob MEDIUMBLOB NULL,
            image_version INT NOT NULL DEFAULT 1,
            user_id INT NULL,
            cafe_id INT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_foods_category_id (category_id),
            INDEX idx_foods_user_id (user_id),
            INDEX idx_foods_cafe_id (cafe_id),
            CONSTRAINT fk_foods_category
                FOREIGN KEY (category_id) REFERENCES categories(category_id)
                ON DELETE SET NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
    ("inventory", """
        CREATE TABLE IF NOT EXISTS inventory (
            inventory_id INT AUTO_INCREMENT PRIMARY KEY,
            food_id INT NOT NULL,
            quantity INT NOT NULL DEFAULT 0,
            minimum_stock INT NOT NULL DEFAULT 0,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY uq_inventory_food (food_id),
            CONSTRAINT fk_inventory_food
                FOREIGN KEY (food_id) REFERENCES foods(food_id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
    ("orders", """
        CREATE TABLE IF NOT EXISTS orders (
            order_id INT AUTO_INCREMENT PRIMARY KEY,
            order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            total_amount DECIMAL(10,2) NOT NULL DEFAULT 0.00,
            order_status VARCHAR(30) NOT NULL DEFAULT 'Pending',
            source VARCHAR(20) NOT NULL DEFAULT 'counter',
            order_day DATE NULL,
            daily_no INT NULL,
            kot_printed TINYINT(1) NOT NULL DEFAULT 0,
            user_id INT NULL,
            cafe_id INT NULL,
            INDEX idx_orders_user_id (user_id),
            INDEX idx_orders_cafe_id (cafe_id),
            INDEX idx_orders_day (user_id, order_day),
            INDEX idx_orders_order_date (order_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
    ("order_items", """
        CREATE TABLE IF NOT EXISTS order_items (
            order_item_id INT AUTO_INCREMENT PRIMARY KEY,
            order_id INT NOT NULL,
            food_id INT NULL,
            -- What the item was called when it was sold. An order line
            -- has to keep reading correctly after the food is removed
            -- from the menu, and the price beside it is already a
            -- snapshot for the same reason.
            item_name VARCHAR(150) NULL,
            quantity INT NOT NULL DEFAULT 1,
            price DECIMAL(10,2) NOT NULL DEFAULT 0.00,
            subtotal DECIMAL(10,2) NOT NULL DEFAULT 0.00,
            INDEX idx_order_items_order_id (order_id),
            INDEX idx_order_items_food_id (food_id),
            CONSTRAINT fk_order_items_order
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
    ("bills", """
        CREATE TABLE IF NOT EXISTS bills (
            bill_id INT AUTO_INCREMENT PRIMARY KEY,
            order_id INT NOT NULL,
            subtotal DECIMAL(10,2) NOT NULL DEFAULT 0.00,
            tax DECIMAL(10,2) NOT NULL DEFAULT 0.00,
            discount DECIMAL(10,2) NOT NULL DEFAULT 0.00,
            total_amount DECIMAL(10,2) NOT NULL DEFAULT 0.00,
            payment_method VARCHAR(30) NOT NULL DEFAULT 'Cash',
            payment_status VARCHAR(30) NOT NULL DEFAULT 'Pending',
            bill_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            gateway_order_id VARCHAR(100) NULL,
            gateway_payment_id VARCHAR(100) NULL,
            payment_reference VARCHAR(150) NULL,
            gateway_signature VARCHAR(255) NULL,
            UNIQUE KEY uq_bills_order (order_id),
            INDEX idx_bills_gateway_order_id (gateway_order_id),
            INDEX idx_bills_bill_date (bill_date),
            CONSTRAINT fk_bills_order
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
    ("login_otp_codes", """
        CREATE TABLE IF NOT EXISTS login_otp_codes (
            otp_id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            code_hash VARCHAR(255) NOT NULL,
            purpose VARCHAR(20) NOT NULL DEFAULT 'login',
            expires_at DATETIME NOT NULL,
            attempts INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_login_otp_user (user_id),
            CONSTRAINT fk_login_otp_user
                FOREIGN KEY (user_id) REFERENCES users(user_id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
]

# Columns added after the first release. Existing installations get them via
# ALTER TABLE; fresh ones already have them from the CREATE statements above.
_COLUMN_MIGRATIONS = [
    ("users", "phone_number", "VARCHAR(20) NULL"),
    ("users", "cafe_id", "INT NULL"),
    ("categories", "user_id", "INT NULL"),
    ("categories", "cafe_id", "INT NULL"),
    ("categories", "description", "TEXT NULL"),
    ("foods", "user_id", "INT NULL"),
    ("foods", "cafe_id", "INT NULL"),
    ("foods", "image_mime", "VARCHAR(80) NULL"),
    ("foods", "image_blob", "MEDIUMBLOB NULL"),
    ("foods", "image_version", "INT NOT NULL DEFAULT 1"),
    # Menu numbering. Backfilled from food_id order on first boot by
    # _backfill_food_numbers below.
    ("foods", "food_no", "INT NULL"),
    ("orders", "user_id", "INT NULL"),
    ("orders", "cafe_id", "INT NULL"),
    # 'counter' for one a member of staff rang up, 'qr' for one a
    # customer placed from their own phone. The kitchen needs to know
    # which, because nobody is standing there to carry the second one.
    ("orders", "source", "VARCHAR(20) NOT NULL DEFAULT 'counter'"),
    # The number a customer is told and the kitchen calls out. It
    # starts again at 1 every morning, per cafe. order_id cannot do
    # this: it is the key every bill and order line points at, and
    # reusing it would tie today's order to yesterday's bill.
    ("orders", "order_day", "DATE NULL"),
    ("orders", "daily_no", "INT NULL"),
    # Claimed by whichever till prints the ticket, so two screens
    # watching the same kitchen do not print it twice.
    ("orders", "kot_printed", "TINYINT(1) NOT NULL DEFAULT 0"),
    # The item's name as sold, so a deleted food does not blank out the
    # lines of every bill it ever appeared on.
    ("order_items", "item_name", "VARCHAR(150) NULL"),
    ("cafes", "logo_mime", "VARCHAR(80) NULL"),
    ("cafes", "logo_blob", "MEDIUMBLOB NULL"),
    ("cafes", "login_photo_mime", "VARCHAR(80) NULL"),
    ("cafes", "login_photo_blob", "MEDIUMBLOB NULL"),
    ("cafes", "branding_version", "INT NOT NULL DEFAULT 1"),
    # What the sidebar reads. Left empty a cafe gets the product's own
    # name and line, which is what every cafe had before this was a
    # choice - so nothing changes appearance on upgrade.
    ("cafes", "brand_name", "VARCHAR(150) NULL"),
    ("cafes", "brand_tagline", "VARCHAR(150) NULL"),
    # What a cafe's QR code points at. Random and per-cafe, so the
    # address cannot be guessed and does not leak the cafe's row id.
    ("cafes", "public_token", "VARCHAR(40) NULL"),
    # When a kitchen screen last said it was watching. Stale means
    # nobody is there, and the other screens take the job back.
    ("cafes", "kitchen_seen_at", "DATETIME NULL"),
    # Per-café tax rate. 5.00 is what every bill was hard-coded to before
    # this was configurable, so existing cafés keep their current totals.
    ("cafes", "tax_percent", "DECIMAL(5,2) NOT NULL DEFAULT 5.00"),
    # The accent colour. 'copper' is the look every café had before this
    # was a choice, so nothing changes appearance on upgrade.
    ("cafes", "theme", "VARCHAR(20) NOT NULL DEFAULT 'copper'"),
    # Automatic printing. Off by default: a café that has not asked for it
    # should never have a print dialog appear on its own.
    ("cafes", "auto_kot_enabled", "TINYINT(1) NOT NULL DEFAULT 0"),
    ("cafes", "auto_kot_delay", "INT NOT NULL DEFAULT 5"),
    ("cafes", "auto_bill_enabled", "TINYINT(1) NOT NULL DEFAULT 0"),
    # A staff member's own photo, shown in place of their initial.
    ("users", "photo_mime", "VARCHAR(80) NULL"),
    ("users", "photo_blob", "MEDIUMBLOB NULL"),
    ("users", "photo_version", "INT NOT NULL DEFAULT 1"),
    ("bills", "gateway_order_id", "VARCHAR(100) NULL"),
    ("bills", "gateway_payment_id", "VARCHAR(100) NULL"),
    ("bills", "payment_reference", "VARCHAR(150) NULL"),
    ("bills", "gateway_signature", "VARCHAR(255) NULL"),
    ("login_otp_codes", "purpose", "VARCHAR(20) NOT NULL DEFAULT 'login'"),
]

_INDEX_MIGRATIONS = [
    ("users", "idx_users_cafe_id", "users(cafe_id)"),
    ("categories", "idx_categories_user_id", "categories(user_id)"),
    ("categories", "idx_categories_cafe_id", "categories(cafe_id)"),
    ("foods", "idx_foods_user_id", "foods(user_id)"),
    ("foods", "idx_foods_cafe_id", "foods(cafe_id)"),
    ("orders", "idx_orders_user_id", "orders(user_id)"),
    ("orders", "idx_orders_cafe_id", "orders(cafe_id)"),
    # Not only for speed. The daily number is allocated with a locking
    # read over one cafe's rows for today, and without this index InnoDB
    # has no narrow range to lock - it would take a far wider lock and one
    # cafe's lunchtime rush would hold up every other cafe on the system.
    ("orders", "idx_orders_day", "orders(user_id, order_day)"),
    ("bills", "idx_bills_gateway_order_id", "bills(gateway_order_id)"),
]

# Unique constraints the application depends on for correctness. A database
# created from _CORE_TABLES already has them, but one carried over from the
# older single-cafe app does not, and CREATE TABLE IF NOT EXISTS will not add
# them to a table that already exists.
#
# These are not merely for speed:
#   * inventory(food_id) - without it, editing a food item's stock inserts a
#     second inventory row instead of updating the existing one, so the food
#     shows two different stock levels and availability becomes unpredictable.
#   * bills(order_id) - without it, the "create any missing bills" pass on the
#     Billing page can add a duplicate bill for an order every time the page
#     is opened, inflating revenue reports.
#
# Each entry: (table, key_name, column, keep_order)
# keep_order is an ORDER BY fragment; the FIRST row of each duplicate group
# survives. Put the row you most want to keep first.
_UNIQUE_KEY_MIGRATIONS = [
    # Newest stock count wins - it reflects the most recent count taken.
    ("inventory", "uq_inventory_food", "food_id",
     "inventory_id DESC"),
    # A settled bill outranks an unsettled one regardless of age, so a
    # duplicate can never cost you a payment record. Among equals, the
    # original (lowest id) is kept.
    ("bills", "uq_bills_order", "order_id",
     "(payment_status = 'Paid') DESC, "
     "(gateway_payment_id IS NOT NULL) DESC, "
     "bill_id ASC"),
]

# Tables where deleting a row destroys a financial record. If de-duplication
# here would have to choose between two rows that both look settled, the
# migration refuses and asks a human instead.
_FINANCIAL_TABLES = {"bills"}


def _column_exists(cursor, table, column):
    cursor.execute("""
        SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, (table, column))
    return cursor.fetchone()["n"] > 0


def _index_exists(cursor, table, index_name):
    cursor.execute("""
        SELECT COUNT(*) AS n FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s AND INDEX_NAME = %s
    """, (table, index_name))
    return cursor.fetchone()["n"] > 0


def _primary_key_column(cursor, table):
    """The single-column primary key of `table`, or None."""
    cursor.execute("""
        SELECT COLUMN_NAME AS pk FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s AND COLUMN_KEY = 'PRI'
    """, (table,))
    rows = cursor.fetchall()
    return rows[0]["pk"] if len(rows) == 1 else None


def _unique_index_on_column(cursor, table, column):
    """
    The name of an existing UNIQUE index that constrains `column` on its own,
    or None.

    Checked by shape rather than by name. An upgraded database may already
    enforce uniqueness through an index inherited from the old schema (often
    named after the column itself, e.g. `food_id`), and looking only for our
    own name would add a second, functionally identical index - MySQL warning
    1831 - on every fresh deployment.

    COUNT(*) = 1 matters: a unique index on (food_id, counted_on) does NOT
    make food_id unique by itself, so a composite index must not satisfy this.
    """
    cursor.execute("""
        SELECT INDEX_NAME AS name
        FROM INFORMATION_SCHEMA.STATISTICS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = %s
          AND NON_UNIQUE = 0
        GROUP BY INDEX_NAME
        HAVING COUNT(*) = 1 AND MAX(COLUMN_NAME) = %s
    """, (table, column))
    rows = cursor.fetchall()
    return rows[0]["name"] if rows else None


def _ensure_unique_keys(cursor):
    """
    Add the unique constraints listed in _UNIQUE_KEY_MIGRATIONS.

    A database upgraded from the older single-cafe app may already contain
    duplicate rows, which would make ALTER TABLE ... ADD UNIQUE KEY fail. So
    duplicates are collapsed first, keeping one row per group according to the
    entry's keep_rule, and only then is the constraint applied.

    Anything that goes wrong here is logged and skipped rather than raised:
    a missing unique key degrades behaviour but must not stop the app from
    booting.
    """
    for table, key_name, column, keep_order in _UNIQUE_KEY_MIGRATIONS:
        try:
            existing = _unique_index_on_column(cursor, table, column)
            if existing:
                if existing != key_name:
                    app.logger.info(
                        "%s.%s is already unique via index '%s'; leaving it "
                        "alone rather than adding a duplicate %s.",
                        table, column, existing, key_name
                    )
                continue

            primary_key = _primary_key_column(cursor, table)
            if not primary_key:
                app.logger.warning(
                    "Skipping unique key %s: no single-column primary key "
                    "found on %s", key_name, table
                )
                continue

            # Count first so the cleanup is visible in the deploy logs rather
            # than silently discarding rows.
            cursor.execute(f"""
                SELECT COUNT(*) AS n FROM (
                    SELECT `{column}` FROM `{table}`
                    WHERE `{column}` IS NOT NULL
                    GROUP BY `{column}` HAVING COUNT(*) > 1
                ) AS duplicated
            """)
            duplicate_groups = cursor.fetchone()["n"]

            if duplicate_groups and table in _FINANCIAL_TABLES:
                # Two settled bills for one order is a real accounting
                # question, not something a migration should decide. Leave
                # every row in place and let the operator resolve it.
                cursor.execute(f"""
                    SELECT COUNT(*) AS n FROM (
                        SELECT `{column}` FROM `{table}`
                        WHERE `{column}` IS NOT NULL
                          AND payment_status = 'Paid'
                        GROUP BY `{column}` HAVING COUNT(*) > 1
                    ) AS ambiguous
                """)
                if cursor.fetchone()["n"]:
                    app.logger.error(
                        "Cannot add unique key %s: some orders have more than "
                        "one PAID bill. No rows have been deleted. Resolve "
                        "these by hand, then restart:  SELECT %s, COUNT(*) "
                        "FROM %s WHERE payment_status='Paid' GROUP BY %s "
                        "HAVING COUNT(*) > 1;",
                        key_name, column, table, column
                    )
                    continue

            if duplicate_groups:
                app.logger.warning(
                    "Found %s duplicated %s.%s value(s) while adding %s; "
                    "collapsing each group to a single row (keep order: %s).",
                    duplicate_groups, table, column, key_name, keep_order
                )
                # ROW_NUMBER lets the survivor be chosen by a meaningful rule
                # rather than merely by lowest/highest id. The extra subquery
                # layer is required: MySQL refuses to read from the same table
                # it is deleting from otherwise.
                cursor.execute(f"""
                    DELETE FROM `{table}`
                    WHERE `{primary_key}` IN (
                        SELECT doomed_id FROM (
                            SELECT `{primary_key}` AS doomed_id,
                                   ROW_NUMBER() OVER (
                                       PARTITION BY `{column}`
                                       ORDER BY {keep_order}
                                   ) AS row_rank
                            FROM `{table}`
                            WHERE `{column}` IS NOT NULL
                        ) AS ranked
                        WHERE ranked.row_rank > 1
                    )
                """)
                app.logger.warning(
                    "Removed %s duplicate row(s) from %s.",
                    cursor.rowcount, table
                )

            cursor.execute(
                f"ALTER TABLE `{table}` "
                f"ADD UNIQUE KEY `{key_name}` (`{column}`)"
            )
            app.logger.info("Added unique key %s on %s.", key_name, table)

        except mysql.connector.Error as error:
            app.logger.warning(
                "Could not add unique key %s on %s: %s. The application will "
                "still run, but duplicate %s rows are possible.",
                key_name, table, getattr(error, "msg", error), table
            )


def ensure_auth_schema():
    """
    Create every table the app needs and migrate older single-cafe installs.

    Safe to call on every request: it is fully idempotent and short-circuits
    after the first successful run in each worker process.
    """
    global AUTH_SCHEMA_READY
    if AUTH_SCHEMA_READY:
        return

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        for _table_name, create_sql in _CORE_TABLES:
            cursor.execute(create_sql)

        # Widen the role ENUM on installs that predate the 'staff' role.
        cursor.execute("""
            SELECT COLUMN_TYPE AS col_type FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users' AND COLUMN_NAME = 'role'
        """)
        role = cursor.fetchone()
        if role and "'staff'" not in role["col_type"]:
            cursor.execute("""
                ALTER TABLE users MODIFY COLUMN role
                ENUM('admin','manager','cashier','staff')
                NOT NULL DEFAULT 'staff'
            """)

        for table, column, definition in _COLUMN_MIGRATIONS:
            if not _column_exists(cursor, table, column):
                cursor.execute(
                    f"ALTER TABLE `{table}` ADD COLUMN `{column}` {definition}"
                )

        for table, index_name, target in _INDEX_MIGRATIONS:
            if not _index_exists(cursor, table, index_name):
                cursor.execute(f"CREATE INDEX {index_name} ON {target}")

        # Must run before the constraints below are relied upon by any route.
        _ensure_unique_keys(cursor)

        _adopt_legacy_single_cafe_data(cursor)
        _backfill_cafe_ids(cursor)
        _backfill_food_numbers(cursor)
        _backfill_order_item_names(cursor)

        connection.commit()
        AUTH_SCHEMA_READY = True
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


def next_food_no(cursor, owner_id):
    """
    The lowest menu number this cafe is not already using.

    Delete food number 3 and the next one added takes 3 back, so the list
    stays 1..N with no gaps. The cafe's menu is a short list, so walking it
    is cheaper than anything clever.
    """
    cursor.execute(
        "SELECT food_no FROM foods "
        "WHERE user_id = %s AND food_no IS NOT NULL",
        (owner_id,)
    )
    taken = set()
    for row in cursor.fetchall():
        value = row["food_no"] if isinstance(row, dict) else row[0]
        if value:
            taken.add(int(value))

    candidate = 1
    while candidate in taken:
        candidate += 1
    return candidate


def _backfill_order_item_names(cursor):
    """
    Name the order lines that were written before names were recorded.

    Anything whose food still exists can be named from the menu. A line
    whose food was already deleted cannot be recovered - that name is gone -
    and it reads as "Removed item" from here on.
    """
    cursor.execute("""
        UPDATE order_items
        SET item_name = (
            SELECT f.food_name FROM foods f
            WHERE f.food_id = order_items.food_id
        )
        WHERE item_name IS NULL
          AND food_id IS NOT NULL
          AND EXISTS (
              SELECT 1 FROM foods f WHERE f.food_id = order_items.food_id
          )
    """)


def _backfill_food_numbers(cursor):
    """
    Give menu numbers to food that predates them.

    Runs once: after the first pass there is nothing left with a NULL
    number, so this costs one cheap SELECT on every later boot. Numbers are
    handed out in food_id order, so an existing menu keeps the order the
    owner already knows.
    """
    cursor.execute(
        "SELECT food_id, user_id FROM foods "
        "WHERE food_no IS NULL ORDER BY user_id, food_id"
    )
    pending = cursor.fetchall()
    if not pending:
        return

    def field(row, name, index):
        return row[name] if isinstance(row, dict) else row[index]

    owners = sorted({field(row, "user_id", 1) for row in pending},
                    key=lambda value: (value is None, value))

    # What each owner is already using, so a half-finished backfill - or a
    # menu that gained numbered food in between - is not handed a duplicate.
    used = {}
    for owner in owners:
        if owner is None:
            cursor.execute("SELECT food_no FROM foods "
                           "WHERE user_id IS NULL AND food_no IS NOT NULL")
        else:
            cursor.execute("SELECT food_no FROM foods "
                           "WHERE user_id = %s AND food_no IS NOT NULL",
                           (owner,))
        used[owner] = {int(field(row, "food_no", 0))
                       for row in cursor.fetchall()
                       if field(row, "food_no", 0)}

    nxt = {owner: 1 for owner in owners}
    for row in pending:
        owner = field(row, "user_id", 1)
        number = nxt[owner]
        while number in used[owner]:
            number += 1
        used[owner].add(number)
        nxt[owner] = number + 1

        cursor.execute("UPDATE foods SET food_no = %s WHERE food_id = %s",
                       (number, field(row, "food_id", 0)))


def _adopt_legacy_single_cafe_data(cursor):
    """
    Move data from a pre-SaaS, single-cafe install into one café tenant.

    Only runs when there is no café yet but users already exist, so a fresh
    SaaS deployment creates nothing and the first real café comes from
    /register.
    """
    cursor.execute("SELECT cafe_id FROM cafes ORDER BY cafe_id ASC LIMIT 1")
    if cursor.fetchone():
        return

    cursor.execute("SELECT user_id FROM users ORDER BY user_id ASC LIMIT 1")
    owner = cursor.fetchone()
    if not owner:
        return

    cursor.execute(
        "INSERT INTO cafes (cafe_name, owner_user_id) VALUES (%s, %s)",
        (os.environ.get("CAFE_NAME", "Coffeehouse"), owner["user_id"])
    )
    cafe_id = cursor.lastrowid

    cursor.execute(
        "UPDATE users SET cafe_id = %s WHERE cafe_id IS NULL", (cafe_id,)
    )
    for table in ("foods", "orders", "categories"):
        cursor.execute(
            f"UPDATE `{table}` SET user_id = %s WHERE user_id IS NULL",
            (owner["user_id"],)
        )


def _backfill_cafe_ids(cursor):
    """
    Keep the denormalised cafe_id columns in step with owner user_id.

    Business rows are filtered by the café owner's user_id throughout the app;
    cafe_id is maintained alongside it so tenancy can be verified directly and
    so reporting/administration queries can group by café.
    """
    for table in ("categories", "foods", "orders"):
        cursor.execute(f"""
            UPDATE `{table}` t
            JOIN users u ON u.user_id = t.user_id
            SET t.cafe_id = u.cafe_id
            WHERE t.cafe_id IS NULL AND u.cafe_id IS NOT NULL
        """)

class TenantSessionError(RuntimeError):
    """
    Raised when a request has a logged-in user but no usable café tenant.

    A dedicated error handler turns this into a clean "please sign in again"
    redirect. Previously this was a bare RuntimeError raised from inside a
    before_request hook and a context processor, which Flask surfaced as an
    unrecoverable 500 on every single page.
    """


def get_current_cafe_id():
    cafe_id = session.get("cafe_id")
    if cafe_id:
        return int(cafe_id)

    user_id = session.get("user_id")
    if not user_id:
        return None

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT cafe_id FROM users WHERE user_id = %s", (user_id,)
        )
        row = cursor.fetchone()
        if row and row.get("cafe_id"):
            session["cafe_id"] = int(row["cafe_id"])
            return int(row["cafe_id"])
    finally:
        cursor.close()
        connection.close()

    return None


def require_cafe_session():
    cafe_id = get_current_cafe_id()
    if not cafe_id:
        raise TenantSessionError(
            "No café is associated with the current session."
        )
    return cafe_id


# ==========================================
# ADMIN LOGIN OTP (ONE-TIME MOBILE CODE)
# ==========================================
#
# Only the 'admin' role is challenged for a one-time code on login;
# manager/cashier accounts sign in with just their username + password.
#
# To send real text messages, set these environment variables to point
# at your SMS provider (see OTP_LOGIN_SETUP.md):
#   SMS_GATEWAY_URL, SMS_GATEWAY_API_KEY, SMS_GATEWAY_SENDER_ID (optional)
#
# Without a gateway configured, the code is written to the server log
# and shown on-screen with a "development mode" notice instead of being
# texted out, so the flow can still be exercised end-to-end locally.

OTP_EXPIRY_SECONDS = int(os.environ.get("OTP_EXPIRY_SECONDS", "300"))
OTP_MAX_ATTEMPTS = int(os.environ.get("OTP_MAX_ATTEMPTS", "5"))
OTP_RESEND_COOLDOWN_SECONDS = 30


def mask_phone_number(phone_number):
    digits = (phone_number or "").strip()
    if len(digits) <= 4:
        return digits
    return ("•" * (len(digits) - 4)) + digits[-4:]


def generate_otp_code():
    # secrets.randbelow keeps this cryptographically random; +100000
    # guarantees a full 6 digits (no leading-zero codes).
    return str(secrets.randbelow(900000) + 100000)


def issue_login_otp(cursor, connection, user_id):
    """Invalidate any existing codes for this user and store a fresh one."""
    code = generate_otp_code()

    cursor.execute(
        "DELETE FROM login_otp_codes WHERE user_id = %s",
        (user_id,)
    )
    cursor.execute("""
        INSERT INTO login_otp_codes (user_id, code_hash, expires_at)
        VALUES (%s, %s, DATE_ADD(NOW(), INTERVAL %s SECOND))
    """, (user_id, generate_password_hash(code), OTP_EXPIRY_SECONDS))

    connection.commit()
    return code


def send_login_otp_sms(phone_number, code):
    """
    Text the OTP to the admin's mobile number.

    Returns True if it was handed off to a real SMS gateway, False if it
    fell back to the development-mode console log (no gateway configured,
    or the gateway request failed).
    """
    cafe_name = os.environ.get("CAFE_OTP_SENDER_NAME", "Cafe Manager")
    message = (
        f"Your {cafe_name} login code is {code}. "
        f"It expires in {OTP_EXPIRY_SECONDS // 60} minutes."
    )

    gateway_url = os.environ.get("SMS_GATEWAY_URL", "").strip()
    api_key = os.environ.get("SMS_GATEWAY_API_KEY", "").strip()

    if gateway_url and api_key and phone_number:
        try:
            import requests
            response = requests.post(
                gateway_url,
                data={
                    "api_key": api_key,
                    "sender_id": os.environ.get("SMS_GATEWAY_SENDER_ID", ""),
                    "to": phone_number,
                    "message": message,
                },
                timeout=8,
            )
            response.raise_for_status()
            return True
        except Exception as error:
            app.logger.warning(
                f"SMS gateway request failed, falling back to console log: {error}"
            )

    # Development fallback: no gateway configured, or the request above
    # failed. Log the code so the login flow is still testable.
    app.logger.info(f"[LOGIN OTP] Code for {phone_number or 'unknown number'}: {code}")
    return False


def get_current_user():
    """
    The signed-in user, or None.

    Cached for the duration of the request: this used to run a fresh query
    (on a fresh connection) from both the before_request hook and the
    template context processor on every single page load.

    Returns None rather than raising when the session has no café, so that
    it is safe to call from a context processor.
    """
    if has_request_context() and "current_user_row" in g:
        return g.current_user_row

    user_id = session.get("user_id")
    if not user_id:
        return None

    cafe_id = get_current_cafe_id()
    if not cafe_id:
        return None

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        # The café's owner id is joined in rather than fetched separately.
        # Both lookups ran on every single request, against the same café
        # row - so that was a whole extra network round trip per page, on
        # every page, for one integer.
        cursor.execute("""
            SELECT u.user_id, u.username, u.full_name, u.role, u.is_active,
                   (u.photo_blob IS NOT NULL) AS has_photo, u.photo_version,
                   c.owner_user_id, c.is_active AS cafe_active,
                   c.auto_kot_enabled, c.auto_kot_delay, c.auto_bill_enabled,
                   c.theme,
                   c.cafe_name, c.branding_version,
                   c.brand_name, c.brand_tagline,
                   (c.logo_blob IS NOT NULL) AS has_logo,
                   (c.login_photo_blob IS NOT NULL) AS has_login_photo
            FROM users u
            LEFT JOIN cafes c ON c.cafe_id = u.cafe_id
            WHERE u.user_id = %s AND u.cafe_id = %s
        """, (user_id, cafe_id))
        row = cursor.fetchone()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    # Café columns carried on this row for the shell to use. They are kept
    # out of the user dict so nothing mistakes a café's name for a person's.
    PRINTING_KEYS = ("auto_kot_enabled", "auto_kot_delay",
                     "auto_bill_enabled", "theme",
                     "cafe_name", "branding_version",
                     "brand_name", "brand_tagline",
                     "has_logo", "has_login_photo")

    user = None
    if row:
        user = {key: row[key] for key in row
                if key not in ("owner_user_id", "cafe_active") + PRINTING_KEYS}

        # Carried on the same row, so the printing settings cost no query of
        # their own - every page needs them to decide whether to arm the
        # automatic print.
        if has_request_context():
            g.print_settings = {
                "auto_kot": bool(row["auto_kot_enabled"]),
                "kot_delay": int(row["auto_kot_delay"] or 0),
                "auto_bill": bool(row["auto_bill_enabled"]),
            }
            # Same idea for the accent colour: every page is painted in
            # it, so it must not cost a query of its own.
            g.cafe_theme = normalize_theme(row["theme"])

            # And the café's own name and logo, which the sidebar and the
            # mobile top bar show on every page. Reading them here rather
            # than letting the template ask for them saves a whole extra
            # query per page load, on every page.
            version = row["branding_version"] or 1
            g.cafe_branding_id = cafe_id
            g.cafe_branding = {
                "cafe_name": row["cafe_name"] or app.config["CAFE_NAME"],
                "brand_name": ((row["brand_name"] or "").strip()
                               or DEFAULT_BRAND_NAME),
                "brand_tagline": ((row["brand_tagline"] or "").strip()
                                  or DEFAULT_BRAND_TAGLINE),
                "logo": (
                    url_for("cafe_media", cafe_id=cafe_id, kind="logo",
                            v=version)
                    if row["has_logo"] else ""
                ),
                "login_photo": (
                    url_for("cafe_media", cafe_id=cafe_id, kind="login",
                            v=version)
                    if row["has_login_photo"] else ""
                ),
            }

        # Only cache a settled answer. A missing owner still has to go
        # through get_cafe_owner_id(), which adopts the oldest admin and
        # writes it back; an inactive café must keep resolving to None so
        # the session is sent back to sign-in rather than carrying on.
        if row["owner_user_id"] and row["cafe_active"] and has_request_context():
            g.cafe_owner_id = row["owner_user_id"]

    if has_request_context():
        g.current_user_row = user
    return user


def require_role(*roles):
    user = get_current_user()
    if not user or user["role"] not in roles:
        flash("You do not have permission to access that page.")
        return redirect(url_for("home"))
    return None


# Non-admin staff (manager/cashier/staff) are limited to these sections
# only: New Order, the Kitchen screen, Food Management, Inventory, and
# Billing (history).
# order_details is included because creating a new order redirects there
# to show the receipt/summary of the order just placed.
# Applies to every role except 'admin', so 'staff' automatically gets the
# same access as manager/cashier with no extra wiring.
STAFF_ALLOWED_ENDPOINTS = {
    "add_order", "order_details",
    # Printing a bill or a kitchen ticket is counter work, not admin work.
    "print_bill", "print_kot",
    "cancel_order", "complete_order", "delete_order",
    "foods", "add_food", "edit_food", "delete_food",
    # Categories sit alongside food management, which staff already run.
    "categories", "add_category", "edit_category", "delete_category",
    "inventory", "update_stock",
    "billing", "mark_bill_paid", "start_online_payment",
    "verify_online_payment", "edit_bill",
    "order_status_feed",
    "change_password", "logout",
    "account_photo", "user_media",
    # Whoever is on the till is the one who notices the tickets are wrong.
    "print_settings",
    # A QR order arrives with nobody at the counter, so whichever
    # staff screen is open has to be able to pull its ticket.
    "kitchen_pending", "kitchen_claim",
    # The screen that lives in the kitchen, and its own feed.
    "kitchen_display", "kitchen_board", "kitchen_heartbeat",
}


# The accent colours an admin can choose between. The hexes are not here on
# purpose: they live once in static/css/theme.css, where the same block
# paints both the picker's swatch and the app itself, so a swatch can never
# show a colour the theme does not actually use.
THEMES = [
    ("copper", "Copper", "The original. Warm and roasted."),
    ("sage", "Sage", "Soft green, easy on the eyes over a long shift."),
    ("ocean", "Ocean", "Cool blue against the dark surfaces."),
    ("berry", "Berry", "Deep red, high contrast."),
    ("violet", "Violet", "Quieter than copper, still warm."),
    ("gold", "Gold", "Bright brass, the boldest of the set."),
]

DEFAULT_THEME = "copper"
THEME_IDS = {theme_id for theme_id, _, _ in THEMES}


def normalize_theme(value):
    """
    A theme id we are willing to put in an HTML attribute.

    Anything unrecognised falls back to the default rather than being
    echoed into the page - the value reaches the template as
    data-theme="...", so it is never allowed to be arbitrary text.
    """
    value = (value or "").strip().lower()
    return value if value in THEME_IDS else DEFAULT_THEME


def get_cafe_theme():
    """
    The café's accent colour.

    Café-wide, not per person: it is the same room, and the admin who sets
    it is setting how the café's screens look. get_current_user() already
    reads it off the café row it joins, so this is normally just a lookup.
    """
    if has_request_context() and "cafe_theme" in g:
        return g.cafe_theme

    cafe_id = session.get("cafe_id")
    if not cafe_id:
        return DEFAULT_THEME

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("SELECT theme FROM cafes WHERE cafe_id = %s", (cafe_id,))
        row = cursor.fetchone()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    theme = normalize_theme(row["theme"] if row else None)
    if has_request_context():
        g.cafe_theme = theme
    return theme


@app.context_processor
def inject_theme():
    return {"cafe_theme": get_cafe_theme(), "themes": THEMES}


# A kitchen screen checks in every 20 seconds; three missed and the
# counter screens assume the tablet is off and take the printing
# back, rather than leaving a customer's ticket unprinted.
KITCHEN_STALE_SECONDS = 70


DEFAULT_PRINT_SETTINGS = {"auto_kot": False, "kot_delay": 5, "auto_bill": False}

# A delay long enough to be useful, short enough that the ticket is still
# ahead of the food. Zero means print the moment the order is saved.
MAX_KOT_DELAY = 120


def get_print_settings():
    """
    Whether this café prints its tickets by itself, and how long it waits.

    get_current_user() already reads these off the café row it joins, so in
    a normal request this is just a lookup. The fallback query is for the
    handful of paths that reach here without a user row in hand.
    """
    if has_request_context() and "print_settings" in g:
        return g.print_settings

    cafe_id = session.get("cafe_id")
    if not cafe_id:
        return dict(DEFAULT_PRINT_SETTINGS)

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("""
            SELECT auto_kot_enabled, auto_kot_delay, auto_bill_enabled
            FROM cafes WHERE cafe_id = %s
        """, (cafe_id,))
        row = cursor.fetchone()
        settings = dict(DEFAULT_PRINT_SETTINGS) if not row else {
            "auto_kot": bool(row["auto_kot_enabled"]),
            "kot_delay": int(row["auto_kot_delay"] or 0),
            "auto_bill": bool(row["auto_bill_enabled"]),
        }
    except mysql.connector.Error:
        # Printing preferences must never take a page down.
        settings = dict(DEFAULT_PRINT_SETTINGS)
    finally:
        cursor.close()
        connection.close()

    if has_request_context():
        g.print_settings = settings
    return settings


def format_percent(value):
    """5.00 -> "5", 12.50 -> "12.5" - no trailing zeros in the message."""
    text = ("%s" % Decimal(str(value)).quantize(Decimal("0.01"))).rstrip("0").rstrip(".")
    return text or "0"


def user_photo_url(user_id, version=None):
    """Versioned URL for a user's photo, so a new upload is picked up."""
    return url_for("user_media", user_id=user_id, v=version or 1)


DEFAULT_TAX_PERCENT = Decimal("5.00")
MAX_TAX_PERCENT = Decimal("100.00")


def get_tax_percent(cafe_id=None):
    """
    The café's tax rate, as a percentage.

    Cached for the request: order creation and the billing page both need it,
    and it is read again to render the New Order screen. Falls back to the
    5% every bill used before the rate was configurable, so a café that has
    never opened the settings page keeps the totals it already had.
    """
    if cafe_id is None and has_request_context() and "cafe_tax_percent" in g:
        return g.cafe_tax_percent

    target = cafe_id if cafe_id is not None else session.get("cafe_id")
    if not target:
        return DEFAULT_TAX_PERCENT

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT tax_percent FROM cafes WHERE cafe_id = %s", (target,)
        )
        row = cursor.fetchone()
        rate = row["tax_percent"] if row and row["tax_percent"] is not None             else DEFAULT_TAX_PERCENT
        rate = Decimal(str(rate))
    except mysql.connector.Error:
        # A café that predates the column must still be able to take orders.
        rate = DEFAULT_TAX_PERCENT
    finally:
        cursor.close()
        connection.close()

    if cafe_id is None and has_request_context():
        g.cafe_tax_percent = rate
    return rate


def tax_multiplier(cafe_id=None):
    """The rate as a fraction, e.g. 5.00% -> Decimal('0.05')."""
    return get_tax_percent(cafe_id) / Decimal("100")


def get_cafe_owner_id():
    """
    The user_id that owns this café's business data.

    Every food, category and order row is tagged with this id, so it must be
    stable for the lifetime of the café. When cafes.owner_user_id is missing
    (legacy rows only) the oldest admin is adopted *and written back*, so the
    value cannot silently change later when admins are added or deactivated -
    which would have made a café's entire menu and order history disappear.
    """
    if "cafe_owner_id" in g:
        return g.cafe_owner_id

    cafe_id = get_current_cafe_id()
    if not cafe_id:
        return None

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT owner_user_id FROM cafes WHERE cafe_id = %s AND is_active = 1",
            (cafe_id,)
        )
        row = cursor.fetchone()
        owner = row["owner_user_id"] if row and row.get("owner_user_id") else None

        if not owner:
            cursor.execute("""
                SELECT user_id FROM users
                WHERE cafe_id = %s AND role = 'admin'
                ORDER BY user_id LIMIT 1
            """, (cafe_id,))
            row = cursor.fetchone()
            owner = row["user_id"] if row else None

            if owner:
                cursor.execute(
                    "UPDATE cafes SET owner_user_id = %s WHERE cafe_id = %s",
                    (owner, cafe_id)
                )
                connection.commit()

        g.cafe_owner_id = owner
        return owner
    finally:
        cursor.close()
        connection.close()


def scope_user_id():
    owner = get_cafe_owner_id()
    if not owner:
        raise TenantSessionError(
            "No café is associated with the current session."
        )
    return owner



@app.route("/")
@app.route("/dashboard", endpoint="dashboard")
def home():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        counts = dashboard_counts(
            cursor, scope_user_id(), session.get("role") == "admin")
        total_foods = counts["total_foods"]
        total_categories = counts["total_categories"]
        total_orders = counts["total_orders"]
        total_inventory = counts["total_inventory"]
        today_orders = counts["today_orders"]
        today_revenue = counts["today_revenue"]

        alerts = stock_alerts(cursor, scope_user_id())

        return render_template(
            "dashboard.html",
            total_foods=total_foods,
            total_categories=total_categories,
            total_orders=total_orders,
            total_inventory=total_inventory,
            today_orders=today_orders,
            today_revenue=today_revenue,
            weekday=today_weekday(),
            low_stock=alerts["low_stock"],
            unavailable=alerts["unavailable"],
            low_stock_items=alerts["low_stock_items"],
            unavailable_items=alerts["unavailable_items"],
            alert_name_limit=STOCK_ALERT_NAMES
        )

    except mysql.connector.Error as error:
        flash(f"Dashboard database error: {error}")
        return "Dashboard database error", 500

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# ==========================================
# CATEGORY MANAGEMENT
# ==========================================
#
# These routes were missing entirely: categories.html, add_category.html and
# edit_category.html all shipped and reference url_for('categories'),
# 'add_category', 'edit_category' and 'delete_category', but nothing served
# them. Because a food item requires a category and a newly registered café
# starts with none, a new tenant could never add a menu item and therefore
# never take an order.


@app.route("/categories")
def categories():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT
                c.category_id,
                c.category_name,
                c.description,
                COUNT(f.food_id) AS food_count
            FROM categories c
            LEFT JOIN foods f
                ON f.category_id = c.category_id
               AND f.user_id = c.user_id
            WHERE c.user_id = %s
            GROUP BY c.category_id, c.category_name, c.description
            ORDER BY c.category_name
        """, (scope_user_id(),))
        category_list = cursor.fetchall()
    except mysql.connector.Error as error:
        app.logger.exception("categories listing failed")
        flash(f"Could not load categories: {error.msg}")
        return redirect(url_for("home"))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    return render_template("categories.html", categories=category_list)


@app.route("/categories/add", methods=["GET", "POST"])
def add_category():
    if request.method == "POST":
        connection = None
        cursor = None
        try:
            category_name = form_text(
                "category_name", "Category name", max_length=120
            )
            description = form_text(
                "description", "Description", required=False, max_length=2000
            )

            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            # Category names are unique per café, not globally: two different
            # cafés may both have a "Beverages" category.
            cursor.execute("""
                SELECT category_id FROM categories
                WHERE user_id = %s AND LOWER(category_name) = LOWER(%s)
            """, (scope_user_id(), category_name))
            if cursor.fetchone():
                raise ValidationError(
                    f"You already have a category called '{category_name}'."
                )

            cursor.execute("""
                INSERT INTO categories
                    (category_name, description, user_id, cafe_id)
                VALUES (%s, %s, %s, %s)
            """, (
                category_name,
                description or None,
                scope_user_id(),
                require_cafe_session(),
            ))
            connection.commit()
            flash(f"Category '{category_name}' created.")
            return redirect(url_for("categories"))

        except ValidationError as error:
            if connection:
                connection.rollback()
            flash(str(error))
            return redirect(url_for("add_category"))
        except mysql.connector.Error as error:
            if connection:
                connection.rollback()
            app.logger.exception("add_category failed")
            flash(f"Could not create the category: {error.msg}")
            return redirect(url_for("add_category"))
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("add_category.html")


@app.route("/categories/edit/<int:category_id>", methods=["GET", "POST"])
def edit_category(category_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT category_id, category_name, description
            FROM categories
            WHERE category_id = %s AND user_id = %s
        """, (category_id, scope_user_id()))
        category = cursor.fetchone()

        if not category:
            flash("Category not found.")
            return redirect(url_for("categories"))

        if request.method == "POST":
            category_name = form_text(
                "category_name", "Category name", max_length=120
            )
            description = form_text(
                "description", "Description", required=False, max_length=2000
            )

            cursor.execute("""
                SELECT category_id FROM categories
                WHERE user_id = %s
                  AND LOWER(category_name) = LOWER(%s)
                  AND category_id != %s
            """, (scope_user_id(), category_name, category_id))
            if cursor.fetchone():
                raise ValidationError(
                    f"You already have a category called '{category_name}'."
                )

            cursor.execute("""
                UPDATE categories
                SET category_name = %s, description = %s
                WHERE category_id = %s AND user_id = %s
            """, (
                category_name,
                description or None,
                category_id,
                scope_user_id(),
            ))
            connection.commit()
            flash("Category updated.")
            return redirect(url_for("categories"))

        return render_template("edit_category.html", category=category)

    except ValidationError as error:
        if connection:
            connection.rollback()
        flash(str(error))
        return redirect(url_for("edit_category", category_id=category_id))
    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        app.logger.exception("edit_category failed")
        flash(f"Could not update the category: {error.msg}")
        return redirect(url_for("categories"))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/categories/delete/<int:category_id>", methods=["POST"])
def delete_category(category_id):
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT category_id FROM categories
            WHERE category_id = %s AND user_id = %s
        """, (category_id, scope_user_id()))
        if not cursor.fetchone():
            flash("Category not found.")
            return redirect(url_for("categories"))

        # Refuse rather than cascade: deleting a category that still has menu
        # items would orphan them out of the food list and break historical
        # order receipts.
        cursor.execute("""
            SELECT COUNT(*) AS n FROM foods
            WHERE category_id = %s AND user_id = %s
        """, (category_id, scope_user_id()))
        in_use = cursor.fetchone()["n"]

        if in_use:
            flash(
                f"This category still has {in_use} food item(s). "
                "Move or delete them first."
            )
            return redirect(url_for("categories"))

        cursor.execute("""
            DELETE FROM categories
            WHERE category_id = %s AND user_id = %s
        """, (category_id, scope_user_id()))
        connection.commit()
        flash("Category deleted.")
        return redirect(url_for("categories"))

    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        app.logger.exception("delete_category failed")
        flash(f"Could not delete the category: {error.msg}")
        return redirect(url_for("categories"))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# ==========================================
# FOOD MANAGEMENT - VIEW
# ==========================================

@app.route("/foods")
def foods():

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                f.food_id,
                COALESCE(f.food_no, f.food_id) AS food_no,
                f.food_name,
                f.description,
                f.price,
                CASE
                    WHEN COALESCE(i.quantity, 0) > 0 THEN 1
                    ELSE 0
                END AS availability,
                c.category_name,
                COALESCE(i.quantity, 0) AS quantity,
                f.image_version,
                (f.image_blob IS NOT NULL) AS has_image

            FROM foods f

            LEFT JOIN categories c
                ON f.category_id = c.category_id

            LEFT JOIN inventory i
                ON f.food_id = i.food_id

            WHERE f.user_id = %s

            -- By menu number, not newest first. The number is the column
            -- the owner reads down, and a refilled gap would otherwise put
            -- food 3 above food 5.
            ORDER BY COALESCE(f.food_no, f.food_id) ASC
        """, (scope_user_id(),))

        food_list = cursor.fetchall()

        for food in food_list:
            food["image_path"] = (
                food_image_url(food["food_id"], food["image_version"])
                if food["has_image"] else None
            )

    except mysql.connector.Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("home"))

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    return render_template(
        "foods.html",
        foods=food_list
    )


# ==========================================
# ADD FOOD
# ==========================================

@app.route("/foods/add", methods=["GET", "POST"])
def add_food():

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)


        # Get categories for dropdown
        cursor.execute("""
            SELECT category_id, category_name
            FROM categories
            WHERE user_id = %s
            ORDER BY category_name
        """, (scope_user_id(),))

        categories = cursor.fetchall()


        # If form submitted
        if request.method == "POST":

            if not categories:
                flash("Create a category first, then add food items to it.")
                return redirect(url_for("add_category"))

            food_name = form_text("food_name", "Food name", max_length=150)
            category_id = form_int("category_id", "Category", minimum=1)
            description = form_text(
                "description", "Description", required=False, max_length=2000
            )
            price = form_decimal("price", "Price")

            quantity = form_int("quantity", "Quantity", default=0)
            minimum_stock = form_int(
                "minimum_stock", "Minimum stock", default=0
            )
            food_image = request.files.get("food_image")

            assert_category_belongs_to_cafe(cursor, category_id)

            # Availability is controlled automatically by stock.
            # Stock > 0 = Available; Stock = 0 = Unavailable.
            availability = 1 if quantity > 0 else 0


            # The menu number shown on the Food Management list. Taken
            # inside this transaction so it reflects anything deleted a
            # moment ago, and low enough to fill a gap left behind.
            food_no = next_food_no(cursor, scope_user_id())

            # Insert food
            cursor.execute("""
                INSERT INTO foods
                (
                    food_no,
                    category_id,
                    food_name,
                    description,
                    price,
                    availability,
                    user_id,
                    cafe_id
                )

                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                food_no,
                category_id,
                food_name,
                description,
                price,
                availability,
                scope_user_id(),
                require_cafe_session()
            ))


            # Get newly created food ID
            food_id = cursor.lastrowid

            save_food_image(cursor, food_image, food_id)


            # Insert inventory
            cursor.execute("""
                INSERT INTO inventory
                (
                    food_id,
                    quantity,
                    minimum_stock
                )

                VALUES (%s, %s, %s)
            """, (
                food_id,
                quantity,
                minimum_stock
            ))


            connection.commit()

            flash("Food added successfully!")

            return redirect(url_for("foods"))

    except (ValidationError, ValueError) as error:
        if connection:
            connection.rollback()
        flash(str(error))
        return redirect(url_for("add_food"))

    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        app.logger.exception("add_food failed")
        flash(f"Could not save the food item: {error.msg}")
        return redirect(url_for("foods"))

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


    return render_template(
        "add_food.html",
        categories=categories
    )


# ==========================================
# EDIT FOOD
# ==========================================

@app.route("/foods/edit/<int:food_id>", methods=["GET", "POST"])
def edit_food(food_id):

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)


        # Get categories
        cursor.execute("""
            SELECT category_id, category_name
            FROM categories
            WHERE user_id = %s
            ORDER BY category_name
        """, (scope_user_id(),))

        categories = cursor.fetchall()


        if request.method == "POST":

            food_name = form_text("food_name", "Food name", max_length=150)
            category_id = form_int("category_id", "Category", minimum=1)
            description = form_text(
                "description", "Description", required=False, max_length=2000
            )
            price = form_decimal("price", "Price")

            quantity = form_int("quantity", "Quantity", default=0)
            minimum_stock = form_int(
                "minimum_stock", "Minimum stock", default=0
            )
            food_image = request.files.get("food_image")

            assert_category_belongs_to_cafe(cursor, category_id)

            # Availability is controlled automatically by stock.
            availability = 1 if quantity > 0 else 0


            # Update food
            cursor.execute("""
                UPDATE foods

                SET
                    category_id = %s,
                    food_name = %s,
                    description = %s,
                    price = %s,
                    availability = %s

                WHERE food_id = %s
                  AND user_id = %s
            """, (
                category_id,
                food_name,
                description,
                price,
                availability,
                food_id,
                scope_user_id()
            ))


            save_food_image(cursor, food_image, food_id)


            # Update inventory
            # Upsert: a food row created before the inventory table was
            # populated has no stock row, and a plain UPDATE would silently
            # affect zero rows and discard the entered quantity.
            cursor.execute("""
                INSERT INTO inventory (food_id, quantity, minimum_stock)
                SELECT %s, %s, %s
                FROM foods
                WHERE foods.food_id = %s AND foods.user_id = %s
                ON DUPLICATE KEY UPDATE
                    quantity = VALUES(quantity),
                    minimum_stock = VALUES(minimum_stock)
            """, (
                food_id,
                quantity,
                minimum_stock,
                food_id,
                scope_user_id()
            ))


            connection.commit()

            flash("Food updated successfully!")

            return redirect(url_for("foods"))


        # Get existing food
        cursor.execute("""
            SELECT
                f.food_id,
                f.category_id,
                f.food_name,
                f.description,
                f.price,
                f.availability,
                COALESCE(i.quantity, 0) AS quantity,
                COALESCE(i.minimum_stock, 5) AS minimum_stock,
                f.image_version,
                (f.image_blob IS NOT NULL) AS has_image

            FROM foods f

            LEFT JOIN inventory i
                ON f.food_id = i.food_id

            WHERE f.food_id = %s
              AND f.user_id = %s
        """, (food_id, scope_user_id()))


        food = cursor.fetchone()

        if food:
            food["image_path"] = (
                food_image_url(food_id, food["image_version"])
                if food["has_image"] else None
            )

    except (ValidationError, ValueError) as error:
        if connection:
            connection.rollback()
        flash(str(error))
        return redirect(url_for("edit_food", food_id=food_id))

    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        app.logger.exception("edit_food failed")
        flash(f"Could not update the food item: {error.msg}")
        return redirect(url_for("foods"))

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


    if food is None:
        flash("Food item not found.")
        return redirect(url_for("foods"))


    return render_template(
        "edit_food.html",
        food=food,
        categories=categories
    )


# ==========================================
# DELETE FOOD
# ==========================================

@app.route("/foods/delete/<int:food_id>", methods=["POST"])
def delete_food(food_id):

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor()


        # Delete inventory first
        cursor.execute("""
            DELETE FROM inventory
            WHERE food_id = %s
              AND EXISTS (
                  SELECT 1 FROM foods f
                  WHERE f.food_id = inventory.food_id
                    AND f.user_id = %s
              )
        """, (food_id, scope_user_id()))


        # Delete food
        cursor.execute("""
            DELETE FROM foods
            WHERE food_id = %s
              AND user_id = %s
        """, (food_id, scope_user_id()))


        connection.commit()

        flash("Food deleted successfully!")

    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        flash(f"Database error: {error}")

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    return redirect(url_for("foods"))

# ==========================================
# CATEGORY MANAGEMENT - VIEW
# ==========================================

@app.route("/inventory")
def inventory():

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                i.inventory_id,
                i.food_id,
                -- The same number Food Management shows, so a member of
                -- staff reading one list against the other is looking at
                -- one set of ids rather than two.
                COALESCE(f.food_no, f.food_id) AS food_no,
                f.food_name,
                c.category_name,
                f.price,
                CASE
                    WHEN i.quantity > 0 THEN 1
                    ELSE 0
                END AS availability,
                i.quantity,
                i.minimum_stock,
                i.last_updated

            FROM inventory i

            JOIN foods f
                ON i.food_id = f.food_id

            -- LEFT, not INNER: a food whose category was deleted has a
            -- NULL category_id, and an inner join dropped it out of
            -- Inventory altogether while it still sat in Food Management.
            LEFT JOIN categories c
                ON f.category_id = c.category_id

            WHERE f.user_id = %s

            -- Same order as Food Management, for the same reason.
            ORDER BY COALESCE(f.food_no, f.food_id) ASC
        """, (scope_user_id(),))

        inventory_list = cursor.fetchall()

    except mysql.connector.Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("home"))

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    return render_template(
        "inventory.html",
        inventory=inventory_list
    )

# ==========================================
# UPDATE STOCK
# ==========================================

@app.route("/inventory/update/<int:food_id>",
           methods=["GET", "POST"])
def update_stock(food_id):

    if request.method == "POST":

        try:
            quantity = form_int("quantity", "Quantity", default=0)
            minimum_stock = form_int(
                "minimum_stock", "Minimum stock", default=0
            )
        except ValidationError as error:
            flash(str(error))
            return redirect(url_for("update_stock", food_id=food_id))

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            cursor.execute("""
                UPDATE inventory

                SET
                    quantity = %s,
                    minimum_stock = %s,
                    last_updated = CURRENT_TIMESTAMP

                WHERE food_id = %s
                  AND EXISTS (
                      SELECT 1
                      FROM foods f
                      WHERE f.food_id = inventory.food_id
                        AND f.user_id = %s
                  )
            """, (
                quantity,
                minimum_stock,
                food_id,
                scope_user_id()
            ))

            # Automatically synchronize food availability with stock.
            cursor.execute("""
                UPDATE foods
                SET availability = CASE
                    WHEN %s > 0 THEN 1
                    ELSE 0
                END
                WHERE food_id = %s
                  AND user_id = %s
            """, (quantity, food_id, scope_user_id()))

            connection.commit()

            flash("Stock updated successfully!")

        except mysql.connector.Error as error:

            if connection:
                connection.rollback()

            flash(f"Error updating stock: {error}")

        finally:

            if cursor:
                cursor.close()
            if connection:
                connection.close()

        return redirect(url_for("inventory"))


    # Get current inventory information

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                i.inventory_id,
                i.food_id,
                f.food_name,
                i.quantity,
                i.minimum_stock

            FROM inventory i

            JOIN foods f
                ON i.food_id = f.food_id

            WHERE i.food_id = %s
              AND f.user_id = %s
        """, (food_id, scope_user_id()))

        item = cursor.fetchone()

    except mysql.connector.Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("inventory"))

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    if item is None:

        return "Inventory item not found", 404


    return render_template(
        "update_stock.html",
        item=item
    )



#ORDER MANAGEMENT SYSTEM

def format_order_time(value):
    """
    An order's time as "13 Sep, 07:45 PM", whatever shape it arrives in.

    MySQL hands back a datetime here. Other drivers - including the SQLite
    stand-in the offline tests run on - hand back the same instant as text,
    and the popup used to crash on it rather than render. A feed that every
    page polls should not be the one place that trusts the driver.
    """
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value
    return value.strftime("%d %b, %I:%M %p")


def format_receipt_time(value):
    """
    The same instant on a printed bill, with the year.

    A receipt outlives the day it was printed on, so "13 Sep" alone is not
    enough on something a customer keeps. Handles the same two shapes
    format_order_time does, for the same reason.
    """
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return value
    return value.strftime("%d %b %Y, %I:%M %p")


app.jinja_env.filters["receipt_time"] = format_receipt_time


@app.route("/api/order-status")
def order_status_feed():
    """Lightweight JSON feed of the most recent orders and their status,
    used to power the floating 'Order Status' popup shown on every page."""

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                order_id,
                order_date,
                total_amount,
                order_status
            FROM orders
            WHERE user_id = %s
            ORDER BY order_id DESC
            LIMIT 15
        """, (scope_user_id(),))

        rows = cursor.fetchall()

        orders_out = []
        for row in rows:
            orders_out.append({
                "order_id": row["order_id"],
                "order_date": format_order_time(row["order_date"]),
                "total_amount": f'{row["total_amount"]:.2f}',
                "order_status": row["order_status"] or "Pending",
            })

        pending_count = sum(1 for o in orders_out if o["order_status"] == "Pending")

        return {
            "orders": orders_out,
            "pending_count": pending_count,
        }

    except mysql.connector.Error as error:
        return jsonify({"orders": [], "pending_count": 0, "error": str(error)}), 500

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

# ==========================================
# CREATE MULTIPLE-ITEM ORDER
# ==========================================

def dashboard_counts(cursor, owner_id, include_revenue):
    """
    Every dashboard figure in one round trip.

    These were six separate COUNT statements. Against a managed database
    each one is its own network round trip, and the dashboard polls itself
    every few seconds - so six trips became the dominant cost of the busiest
    page in the app. Scalar subqueries fold them into a single statement
    without changing what any of them mean.

    Revenue stays admin-only, the same rule the Billing page follows, so it
    is only added to the statement when the viewer is entitled to it.
    """
    revenue_select = """,
            (SELECT COALESCE(SUM(b.total_amount), 0)
               FROM bills b
               INNER JOIN orders o ON b.order_id = o.order_id
              WHERE DATE(b.bill_date) = CURDATE()
                AND o.user_id = %s
                AND LOWER(COALESCE(o.order_status, '')) != 'cancelled'
                AND LOWER(COALESCE(b.payment_status, '')) = 'paid') AS today_revenue"""

    sql = """
        SELECT
            (SELECT COUNT(*) FROM foods WHERE user_id = %s) AS total_foods,
            (SELECT COUNT(DISTINCT category_id) FROM foods
              WHERE user_id = %s) AS total_categories,
            (SELECT COUNT(*) FROM orders WHERE user_id = %s) AS total_orders,
            (SELECT COUNT(*)
               FROM inventory i
               INNER JOIN foods f ON i.food_id = f.food_id
              WHERE f.user_id = %s) AS total_inventory,
            (SELECT COUNT(*) FROM orders
              WHERE DATE(order_date) = CURDATE()
                AND user_id = %s
                AND LOWER(COALESCE(order_status, '')) != 'cancelled')
                AS today_orders""" + (revenue_select if include_revenue else "")

    params = [owner_id] * 5 + ([owner_id] if include_revenue else [])
    cursor.execute(sql, tuple(params))
    row = cursor.fetchone()

    return {
        "total_foods": row["total_foods"],
        "total_categories": row["total_categories"],
        "total_orders": row["total_orders"],
        "total_inventory": row["total_inventory"],
        "today_orders": row["today_orders"],
        "today_revenue": row["today_revenue"] if include_revenue else None,
    }


# How many names the stock alert lists before it stops and says "and N more".
STOCK_ALERT_NAMES = 12


def stock_alerts(cursor, owner_id):
    """
    Which food is out, and which is nearly out - by name.

    The dashboard used to report only counts ("3 items are low"), which told
    a manager something was wrong but not what to reorder. One query covers
    both sets: quantity 0 is unavailable, anything at or below its minimum
    is low. A zero quantity always satisfies the minimum test too, so the
    split happens here rather than in a second round trip.
    """
    cursor.execute("""
        SELECT f.food_name, i.quantity, i.minimum_stock
        FROM inventory i
        INNER JOIN foods f ON i.food_id = f.food_id
        WHERE f.user_id = %s
          AND i.quantity <= i.minimum_stock
        ORDER BY i.quantity ASC, f.food_name ASC
    """, (owner_id,))

    out, low = [], []
    for row in cursor.fetchall():
        entry = {
            "name": row["food_name"],
            "quantity": int(row["quantity"] or 0),
            "minimum": int(row["minimum_stock"] or 0),
        }
        (out if entry["quantity"] <= 0 else low).append(entry)

    return {
        "unavailable": len(out),
        "low_stock": len(low),
        "unavailable_items": out[:STOCK_ALERT_NAMES],
        "low_stock_items": low[:STOCK_ALERT_NAMES],
    }


UNCATEGORISED_LABEL = "Other"

# How far back "hot selling" looks, and how many items it lifts to the top
# of the New Order menu. Thirty days is long enough that a quiet week does
# not empty the section, recent enough to follow what is actually selling
# now rather than what sold last season.
HOT_SELLER_DAYS = 30
HOT_SELLER_LIMIT = 5


def top_selling_food_ids(cursor, owner_id):
    """
    (food_id, units sold) for the best sellers of the recent window, best
    first.

    Cancelled orders do not count - the items went back on the shelf, so
    counting them would promote food that was never actually served. The
    cutoff is computed here rather than with DATE_SUB so the statement is
    plain SQL that any backend can run.
    """
    cutoff = datetime.now() - timedelta(days=HOT_SELLER_DAYS)
    cursor.execute("""
        SELECT oi.food_id, SUM(oi.quantity) AS sold
        FROM order_items oi
        INNER JOIN orders o
            ON o.order_id = oi.order_id
        WHERE o.user_id = %s
          AND o.order_status != 'Cancelled'
          AND o.order_date >= %s
          AND oi.food_id IS NOT NULL
        GROUP BY oi.food_id
        ORDER BY sold DESC, oi.food_id ASC
        LIMIT %s
    """, (owner_id, cutoff, HOT_SELLER_LIMIT))

    return [
        (row["food_id"], int(row["sold"] or 0))
        for row in cursor.fetchall()
        if row["sold"]
    ]


def pick_hot_sellers(foods, ranked):
    """
    The best sellers, in order, for the shelf at the top of the menu.

    These stay in their category sections as well - someone looking under
    "Beverages" should still find the coffee that happens to be selling
    well. The shelf therefore renders a *second* card for the same food,
    which the template marks as a mirror: it carries no form field name and
    a different input class, so only the card under the category heading is
    ever submitted or counted. add_order.html keeps the pair in step.
    """
    by_id = {food["food_id"]: food for food in foods}

    hot = []
    for food_id, sold in ranked:
        food = by_id.get(food_id)
        if food is None:
            continue          # no longer on the menu, or out of stock
        food["sold_recently"] = sold
        hot.append(food)

    return hot


def group_foods_by_category(foods):
    """
    Split the menu into category sections for the New Order screen.

    Categories come back A-Z and the items inside each one A-Z, so a cashier
    can find a dish by eye instead of scanning one long grid. Food with no
    category is collected under a single "Other" heading at the end rather
    than sorted under a blank name.
    """
    grouped = {}
    for food in foods:
        name = (food.get("category_name") or "").strip() or UNCATEGORISED_LABEL
        grouped.setdefault(name, []).append(food)

    # The key is "foods", not "items": in a template `group.items` resolves
    # to the dict's own .items method before it ever looks for the key, so
    # `group.items|length` would blow up on a built-in method.
    sections = [
        {
            "name": name,
            "foods": sorted(rows, key=lambda f: (f.get("food_name") or "").lower()),
        }
        for name, rows in grouped.items()
    ]
    sections.sort(key=lambda s: (s["name"] == UNCATEGORISED_LABEL, s["name"].lower()))
    return sections


@app.route("/orders/add", methods=["GET", "POST"])
def add_order():

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # ==================================================
        # SHOW ORDER FORM
        # ==================================================

        if request.method == "GET":

            cursor.execute("""
                SELECT
                    f.food_id,
                    f.food_name,
                    f.price,
                    f.availability,
                    c.category_name,
                    i.quantity,
                    f.image_version,
                    (f.image_blob IS NOT NULL) AS has_image

                FROM foods f

                LEFT JOIN categories c
                    ON f.category_id = c.category_id

                LEFT JOIN inventory i
                    ON f.food_id = i.food_id

                WHERE f.availability = 1
                  AND f.user_id = %s
                  AND COALESCE(i.quantity, 0) > 0

                ORDER BY
                    c.category_name,
                    f.food_name
            """, (scope_user_id(),))

            foods = cursor.fetchall()

            for food in foods:
                food["image_path"] = (
                    food_image_url(food["food_id"], food.get("image_version"))
                    if food.get("has_image") else None
                )

            hot_foods = pick_hot_sellers(
                foods, top_selling_food_ids(cursor, scope_user_id())
            )

            return render_template(
                "add_order.html",
                foods=foods,
                hot_foods=hot_foods,
                hot_days=HOT_SELLER_DAYS,
                # The on-screen totals must agree with what the server will
                # charge, so both read the same café rate.
                tax_rate=float(tax_multiplier()),
                tax_percent=get_tax_percent(),
                # Every food, hot ones included, still appears under its
                # own category heading.
                food_groups=group_foods_by_category(foods)
            )


        # ==================================================
        # HELPERS FOR IN-POPUP (AJAX) RESPONSES
        #
        # The "New Order" popup submits this form with fetch() and sets
        # X-Requested-With, so on success/failure we hand back JSON and
        # let the popup update itself in place instead of the browser
        # doing a full page navigation.
        # ==================================================

        def fetch_food_stock_summary():
            cursor.execute("""
                SELECT
                    f.food_id,
                    f.availability,
                    COALESCE(i.quantity, 0) AS quantity
                FROM foods f
                LEFT JOIN inventory i ON f.food_id = i.food_id
                WHERE f.user_id = %s
            """, (scope_user_id(),))

            return [
                {
                    "food_id": row["food_id"],
                    "quantity": row["quantity"],
                    "availability": row["availability"],
                }
                for row in cursor.fetchall()
            ]

        def order_result(success, message, order_id=None, status_code=200):
            if wants_json_response():
                payload = {"success": success, "message": message}
                if order_id is not None:
                    payload["order_id"] = order_id
                payload["foods"] = fetch_food_stock_summary()
                return jsonify(payload), status_code

            flash(message)
            return redirect(url_for("add_order"))


        # ==================================================
        # CREATE ORDER
        #
        # The same code a customer's phone goes through. This used to be a
        # second copy of it, which is exactly the kind of pair that drifts:
        # one of them gets a stock check tightened and the other quietly
        # does not, and the kitchen is oversold from whichever side was
        # forgotten.
        # ==================================================

        wanted = {
            field[len("quantity_"):]: value
            for field, value in request.form.items()
            if field.startswith("quantity_")
        }

        try:
            available = available_foods_for(cursor, scope_user_id())
            selected_items = collect_order_items(available, wanted)
            written = write_order(
                cursor,
                scope_user_id(),
                require_cafe_session(),
                selected_items,
                tax_multiplier(),
                source="counter",
            )
        except OrderError as error:
            connection.rollback()
            return order_result(False, str(error), status_code=400)

        order_id = written["order_id"]
        subtotal = written["subtotal"]
        tax = written["tax"]
        discount = Decimal("0.00")
        total_amount = written["total"]

        # ==================================================
        # CREATE BILL
        # ==================================================

        cursor.execute("""
            INSERT INTO bills
            (
                order_id,
                subtotal,
                tax,
                discount,
                total_amount,
                payment_method,
                payment_status
            )

            VALUES
            (
                %s,
                %s,
                %s,
                %s,
                %s,
                %s,
                %s
            )
        """, (
            order_id,
            subtotal,
            tax,
            discount,
            total_amount,
            "Cash",
            "Pending"
        ))


        # ==================================================
        # SAVE EVERYTHING
        # ==================================================

        connection.commit()


        return order_result(
            True,
            f"Order #{order_id} created successfully!",
            order_id=order_id
        )


    except mysql.connector.Error as error:

        if connection:
            connection.rollback()

        message = f"Database error: {error}"

        if wants_json_response():
            return jsonify({"success": False, "message": message}), 500

        flash(message)

        return redirect(
            url_for("kitchen_display")
        )


    except Exception as error:

        if connection:
            connection.rollback()

        message = f"Order could not be created: {error}"

        if wants_json_response():
            return jsonify({"success": False, "message": message}), 500

        flash(message)

        return redirect(
            url_for("kitchen_display")
        )


    finally:

        if cursor:
            cursor.close()
        if connection:
            connection.close()

# ==========================================
# ORDER DETAILS
# ==========================================

def load_order_for_print(cursor, order_id):
    """
    One order with its lines and bill, scoped to the café.

    Shared by the bill and the kitchen ticket so the two documents can never
    disagree about what was ordered. Returns None when the order belongs to
    another café, which the callers turn into a redirect rather than a 404 -
    the same treatment the order page gives an id that is not yours.
    """
    cursor.execute("""
        SELECT order_id, order_date, total_amount, order_status, daily_no
        FROM orders
        WHERE order_id = %s AND user_id = %s
    """, (order_id, scope_user_id()))
    order = cursor.fetchone()
    if order is None:
        return None

    cursor.execute("""
        SELECT oi.quantity, oi.price, oi.subtotal,
               COALESCE(oi.item_name, f.food_name, 'Removed item')
                   AS food_name
        FROM order_items oi
        LEFT JOIN foods f ON oi.food_id = f.food_id
        WHERE oi.order_id = %s
        ORDER BY oi.order_item_id
    """, (order_id,))
    items = cursor.fetchall()

    cursor.execute("""
        SELECT bill_id, subtotal, tax, discount, total_amount,
               payment_method, payment_status, bill_date
        FROM bills
        WHERE order_id = %s
    """, (order_id,))
    bill = cursor.fetchone()

    return {"order": order, "items": items, "bill": bill}


@app.route("/orders/<int:order_id>/bill")
def print_bill(order_id):
    """
    A printable receipt: the café's name and logo, and every line of one
    order. Open to anyone who can see the order - taking payment is not an
    admin-only job.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        data = load_order_for_print(cursor, order_id)
        if data is None:
            flash("Order not found.")
            return redirect(url_for("kitchen_display"))

        return render_template(
            "print_bill.html",
            branding=get_cafe_branding(session.get("cafe_id")),
            tax_percent=get_tax_percent(),
            quote=quote_for(order_id),
            printed_at=datetime.now(),
            **data
        )
    except mysql.connector.Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("kitchen_display"))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/orders/<int:order_id>/kot")
def print_kot(order_id):
    """
    The kitchen's copy: order number, what to make, how many. No prices -
    the kitchen does not need them and they only crowd the ticket.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        data = load_order_for_print(cursor, order_id)
        if data is None:
            flash("Order not found.")
            return redirect(url_for("kitchen_display"))

        return render_template(
            "print_kot.html",
            branding=get_cafe_branding(session.get("cafe_id")),
            order=data["order"],
            items=data["items"],
        )
    except mysql.connector.Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("kitchen_display"))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/orders/<int:order_id>")
def order_details(order_id):

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # --------------------------------------
        # Get Order
        # --------------------------------------

        cursor.execute("""
            SELECT
                order_id,
                order_date,
                total_amount,
                order_status
            FROM orders
            WHERE order_id = %s
              AND user_id = %s
        """, (order_id, scope_user_id()))

        order = cursor.fetchone()


        # --------------------------------------
        # Check Order Exists
        # --------------------------------------

        if order is None:

            flash("Order not found.")

            return redirect(
                url_for("kitchen_display")
            )


        # --------------------------------------
        # Get Order Items
        # --------------------------------------

        cursor.execute("""
            SELECT
                oi.order_item_id,
                oi.order_id,
                oi.food_id,
                oi.quantity,
                oi.price,
                oi.subtotal,
                COALESCE(oi.item_name, f.food_name, 'Removed item')
                    AS food_name

            FROM order_items oi

            LEFT JOIN foods f
                ON oi.food_id = f.food_id

            WHERE oi.order_id = %s

            ORDER BY oi.order_item_id
        """, (order_id,))

        items = cursor.fetchall()


        # --------------------------------------
        # Get Bill
        # --------------------------------------

        cursor.execute("""
            SELECT
                bill_id,
                order_id,
                subtotal,
                tax,
                discount,
                total_amount,
                payment_method,
                payment_status,
                bill_date

            FROM bills

            WHERE order_id = %s
        """, (order_id,))

        bill = cursor.fetchone()


        # --------------------------------------
        # Show Order Details
        # --------------------------------------

        return render_template(
            "order_details.html",
            order=order,
            items=items,
            bill=bill
        )


    except mysql.connector.Error as error:

        flash(
            f"Database error: {error}"
        )

        return redirect(
            url_for("kitchen_display")
        )


    finally:

        if cursor:
            cursor.close()
        if connection:
            connection.close()

# ==========================================
# ==========================================
# CANCEL ORDER
# ==========================================

def order_action_result(ok, message):
    """
    The answer to "mark this done" or "cancel this".

    The kitchen screen asks over fetch and then redraws itself from the
    board, so what it wants back is yes or no - not a page, and not a
    message to be shown later. Anything that is not a fetch gets the flash
    and the redirect it came expecting.

    Without this every tap on that screen left a flash sitting in the
    session, and a shift's worth of them arrived in a heap on whatever
    page somebody opened next.
    """
    if wants_json_response():
        return jsonify({"success": bool(ok), "message": message}), 200
    flash(message)
    return redirect(url_for("kitchen_display"))


@app.route("/orders/cancel/<int:order_id>", methods=["POST"])
def cancel_order(order_id):

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # Lock the order so it cannot be cancelled twice at the same time.
        cursor.execute("""
            SELECT order_id, order_status
            FROM orders
            WHERE order_id = %s
              AND user_id = %s
            FOR UPDATE
        """, (order_id, scope_user_id()))

        order = cursor.fetchone()

        if order is None:
            return order_action_result(
                False,
                "Order not found.")

        # Do not restore stock twice.
        if order["order_status"] == "Cancelled":
            # Already where the caller wanted it, so not a failure.
            return order_action_result(
                True,
                f"Order #{order_id} is already cancelled.")

        # Get all items so their stock can be returned to inventory.
        cursor.execute("""
            SELECT food_id, quantity
            FROM order_items
            WHERE order_id = %s
        """, (order_id,))

        items = cursor.fetchall()

        for item in items:

            cursor.execute("""
                UPDATE inventory
                SET
                    quantity = quantity + %s,
                    last_updated = CURRENT_TIMESTAMP
                WHERE food_id = %s
                  AND EXISTS (
                      SELECT 1
                      FROM foods f
                      WHERE f.food_id = inventory.food_id
                        AND f.user_id = %s
                  )
            """, (
                item["quantity"],
                item["food_id"],
                scope_user_id()
            ))

            # Re-enable food automatically if stock is now available.
            cursor.execute("""
                UPDATE foods f
                INNER JOIN inventory i
                    ON f.food_id = i.food_id
                SET f.availability = CASE
                    WHEN i.quantity > 0 THEN 1
                    ELSE 0
                END
                WHERE f.food_id = %s
                  AND f.user_id = %s
            """, (item["food_id"], scope_user_id()))

        # IMPORTANT: do not delete the order.
        # Keeping it preserves order_items and the linked bill/history.
        cursor.execute("""
            UPDATE orders
            SET order_status = 'Cancelled'
            WHERE order_id = %s
              AND user_id = %s
        """, (order_id, scope_user_id()))

        connection.commit()

        return order_action_result(
            True,
            f"Order #{order_id} cancelled successfully. "
            "The bill and order history have been preserved.")

    except mysql.connector.Error as error:

        if connection:
            connection.rollback()

        return order_action_result(
            False,
            f"Database error: {error}")

    except Exception as error:

        if connection:
            connection.rollback()

        return order_action_result(
            False,
            f"Order could not be cancelled: {error}")

    finally:

        if cursor:
            cursor.close()
        if connection:
            connection.close()


# Backward-compatible old delete URL.
# It now cancels instead of physically deleting, so billing history is safe.
@app.route("/orders/delete/<int:order_id>", methods=["GET", "POST"])
def delete_order(order_id):
    return cancel_order(order_id)


@app.route("/orders/complete/<int:order_id>", methods=["POST"])
def complete_order(order_id):

    connection = None
    cursor = None

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT order_id, order_status
            FROM orders
            WHERE order_id = %s
              AND user_id = %s
            FOR UPDATE
        """, (order_id, scope_user_id()))

        order = cursor.fetchone()

        if order is None:
            return order_action_result(
                False,
                "Order not found.")

        if order["order_status"] == "Cancelled":
            return order_action_result(
                False,
                f"Order #{order_id} is cancelled and cannot be marked done.")

        if order["order_status"] == "Completed":
            return order_action_result(
                True,
                f"Order #{order_id} is already marked as done.")

        cursor.execute("""
            UPDATE orders
            SET order_status = 'Completed'
            WHERE order_id = %s
              AND user_id = %s
        """, (order_id, scope_user_id()))

        connection.commit()

        return order_action_result(
            True,
            f"Order #{order_id} marked as done.")

    except mysql.connector.Error as error:

        if connection:
            connection.rollback()

        return order_action_result(
            False,
            f"Database error: {error}")

    finally:

        if cursor:
            cursor.close()
        if connection:
            connection.close()

# ==========================================
# BILL PAYMENT STATUS TOGGLE
# ==========================================

@app.route("/billing/mark-paid/<int:bill_id>", methods=["POST"])
def mark_bill_paid(bill_id):
    """Mark a pending bill Paid once. Paid bills cannot be changed back here."""
    connection = None
    cursor = None

    def result(success, message, status_code=200, bill_row=None):
        if wants_json_response():
            payload = {"success": success, "message": message, "bill_id": bill_id}
            if bill_row is not None:
                payload["total_amount"] = float(bill_row.get("total_amount") or 0)
                # The printable bill is addressed by order, not by bill, so
                # the page needs this to print the receipt automatically.
                payload["order_id"] = bill_row.get("order_id")
            return jsonify(payload), status_code
        flash(message)
        return redirect(url_for("billing"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT b.bill_id, b.order_id, b.payment_status, b.total_amount,
                   o.order_status
            FROM bills b
            INNER JOIN orders o ON b.order_id = o.order_id
            WHERE b.bill_id = %s
              AND o.user_id = %s
        """, (bill_id, scope_user_id()))
        bill = cursor.fetchone()

        if not bill:
            return result(False, "Bill not found.", 404)

        if bill["order_status"] == "Cancelled":
            return result(False, "Cancelled orders cannot be marked Paid.", 400, bill)

        if bill["payment_status"] == "Paid":
            return result(False, f"Bill #{bill_id} is already Paid.", 409, bill)

        cursor.execute("""
            UPDATE bills
            SET payment_status = 'Paid'
            WHERE bill_id = %s
              AND payment_status <> 'Paid'
              AND order_id IN (
                  SELECT order_id FROM orders WHERE user_id = %s
              )
        """, (bill_id, scope_user_id()))

        connection.commit()
        return result(True, f"Bill #{bill_id} marked Paid successfully.", 200, bill)

    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        return result(False, f"Payment status update failed: {error}", 500)
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# ==========================================
# RAZORPAY ONLINE PAYMENT
# ==========================================

def _verify_razorpay_signature(order_id, payment_id, signature):
    """Verify Checkout's payment signature without trusting browser data."""
    message = f"{order_id}|{payment_id}".encode("utf-8")
    expected = hmac.new(
        RAZORPAY_KEY_SECRET.encode("utf-8"),
        message,
        digestmod=__import__("hashlib").sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def _record_gateway_payment(bill_id, order_id, payment_id, signature=None):
    """
    Record a verified gateway payment against a bill.

    Deliberately does not mark the bill Paid. Settling a bill is a person
    pressing Paid on the Billing page - the gateway only leaves the evidence
    that money arrived, so the counter can see it and confirm.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT b.bill_id, b.total_amount, b.gateway_order_id, b.payment_status
            FROM bills b
            INNER JOIN orders o ON b.order_id = o.order_id
            WHERE b.bill_id = %s AND o.user_id = %s
        """, (bill_id, scope_user_id()))
        bill = cursor.fetchone()

        if not bill:
            return False, "Bill not found."

        if bill["gateway_order_id"] != order_id:
            return False, "Gateway order does not match this bill."

        # The gateway records what it received; it does not settle the bill.
        # Marking a bill Paid is a person pressing Paid on the Billing page,
        # so nothing on the till changes status without someone deciding it
        # has. The reference is stored either way, so the counter can see an
        # online payment arrived and confirm it.
        cursor.execute("""
            UPDATE bills
            SET payment_method = 'Online',
                gateway_payment_id = %s,
                payment_reference = %s,
                gateway_signature = COALESCE(%s, gateway_signature)
            WHERE bill_id = %s
              AND gateway_order_id = %s
        """, (payment_id, payment_id, signature, bill_id, order_id))

        connection.commit()
        return True, "Payment received. Press Paid on the bill to settle it."
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/billing/pay/<int:bill_id>", methods=["POST"])
def start_online_payment(bill_id):
    """Create a Razorpay order and show the hosted Checkout dialog."""
    try:
        ensure_payment_schema()
        client = require_razorpay()

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT b.bill_id, b.order_id, b.total_amount, b.payment_status,
                   b.gateway_order_id, o.order_status
            FROM bills b
            INNER JOIN orders o ON b.order_id = o.order_id
            WHERE b.bill_id = %s AND o.user_id = %s
        """, (bill_id, scope_user_id()))
        bill = cursor.fetchone()

        if not bill:
            flash("Bill not found.")
            return redirect(url_for("billing"))

        if bill["order_status"] == "Cancelled":
            flash("Cancelled orders cannot be paid online.")
            return redirect(url_for("billing"))

        if bill["payment_status"] == "Paid":
            flash("This bill is already paid.")
            return redirect(url_for("billing"))

        amount_paise = int((Decimal(str(bill["total_amount"])) * Decimal("100")).quantize(Decimal("1")))

        if amount_paise <= 0:
            flash("Online payment cannot be started for a zero-value bill.")
            return redirect(url_for("billing"))

        gateway_order_id = bill["gateway_order_id"]

        if not gateway_order_id:
            gateway_order = client.order.create({
                "amount": amount_paise,
                "currency": "INR",
                "receipt": f"bill_{bill_id}",
                "notes": {
                    "bill_id": str(bill_id),
                    "order_id": str(bill["order_id"]),
                    "user_id": str(scope_user_id())
                }
            })
            gateway_order_id = gateway_order["id"]

            cursor.execute("""
                UPDATE bills
                SET gateway_order_id = %s,
                    payment_method = 'Online',
                    payment_status = 'Pending'
                WHERE bill_id = %s
                  AND order_id = %s
            """, (gateway_order_id, bill_id, bill["order_id"]))
            connection.commit()

        return render_template(
            "razorpay_checkout.html",
            bill=bill,
            gateway_order_id=gateway_order_id,
            razorpay_key_id=RAZORPAY_KEY_ID,
            amount_paise=amount_paise,
            csrf_token=session.get("_csrf_token", "")
        )

    except Exception as error:
        if "connection" in locals() and connection:
            connection.rollback()
        flash(f"Online payment could not be started: {error}")
        return redirect(url_for("billing"))
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "connection" in locals() and connection:
            connection.close()


@app.route("/billing/pay/verify", methods=["POST"])
def verify_online_payment():
    """Verify Razorpay Checkout response on the server."""
    try:
        ensure_payment_schema()
        order_id = request.form.get("razorpay_order_id", "").strip()
        payment_id = request.form.get("razorpay_payment_id", "").strip()
        signature = request.form.get("razorpay_signature", "").strip()
        bill_id = request.form.get("bill_id", "").strip()

        if not all([order_id, payment_id, signature, bill_id]):
            flash("Incomplete payment response.")
            return redirect(url_for("billing"))

        if not _verify_razorpay_signature(order_id, payment_id, signature):
            flash("Payment verification failed. The bill was not marked Paid.")
            return redirect(url_for("billing"))

        ok, message = _record_gateway_payment(
            int(bill_id), order_id, payment_id, signature
        )
        flash(message if ok else f"Payment verification failed: {message}")
        return redirect(url_for("billing"))

    except Exception as error:
        flash(f"Payment verification error: {error}")
        return redirect(url_for("billing"))


@app.route("/razorpay/webhook", methods=["POST"])
def razorpay_webhook():
    """Receive verified Razorpay payment events. This endpoint is server-to-server."""
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if not RAZORPAY_WEBHOOK_SECRET:
        return "Webhook secret is not configured.", 500

    expected = hmac.new(
        RAZORPAY_WEBHOOK_SECRET.encode("utf-8"),
        raw_body,
        digestmod=__import__("hashlib").sha256
    ).hexdigest()

    if not hmac.compare_digest(expected, signature):
        return "Invalid webhook signature.", 400

    try:
        payload = request.get_json(force=True)
        event = payload.get("event", "")

        if event not in {"payment.captured", "order.paid"}:
            return "ok", 200

        payment_entity = payload.get("payload", {}).get("payment", {}).get("entity", {})
        order_entity = payload.get("payload", {}).get("order", {}).get("entity", {})

        payment_id = payment_entity.get("id")
        order_id = payment_entity.get("order_id") or order_entity.get("id")
        amount = payment_entity.get("amount") or order_entity.get("amount")

        if not payment_id or not order_id:
            return "Missing payment/order ID.", 400

        ensure_payment_schema()
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT bill_id, total_amount
            FROM bills
            WHERE gateway_order_id = %s
        """, (order_id,))
        bill = cursor.fetchone()

        if not bill:
            return "Bill not found.", 404

        expected_amount = int(
            (Decimal(str(bill["total_amount"])) * Decimal("100")).quantize(Decimal("1"))
        )

        if amount is not None and int(amount) != expected_amount:
            return "Payment amount does not match bill.", 400

        # Same rule as the verify path: the webhook records the payment, it
        # does not mark the bill Paid. Only the Paid button does.
        cursor.execute("""
            UPDATE bills
            SET payment_method = 'Online',
                gateway_payment_id = %s,
                payment_reference = %s
            WHERE bill_id = %s
              AND gateway_order_id = %s
        """, (payment_id, payment_id, bill["bill_id"], order_id))
        connection.commit()
        return "ok", 200

    except Exception as error:
        if "connection" in locals() and connection:
            connection.rollback()
        return f"Webhook processing error: {error}", 500
    finally:
        if "cursor" in locals() and cursor:
            cursor.close()
        if "connection" in locals() and connection:
            connection.close()


# ==========================================
# EDIT BILL PAYMENT
# ==========================================

@app.route("/billing/edit/<int:bill_id>", methods=["POST"])
@login_required
def edit_bill(bill_id):
    """Change only the payment mode from the Billing page."""
    payment_method = request.form.get("payment_method", "Cash").strip()
    allowed_methods = {"Cash", "UPI", "Card"}

    def result(success, message, status_code=200):
        if wants_json_response():
            return jsonify({
                "success": success,
                "message": message,
                "bill_id": bill_id,
                "payment_method": payment_method,
            }), status_code
        flash(message)
        return redirect(url_for("billing"))

    if payment_method not in allowed_methods:
        return result(False, "Invalid payment mode.", 400)

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT b.bill_id
            FROM bills b
            JOIN orders o ON o.order_id = b.order_id
            WHERE b.bill_id = %s AND o.user_id = %s
            """,
            (bill_id, scope_user_id()),
        )
        if not cursor.fetchone():
            return result(False, "Bill not found.", 404)

        cursor.execute(
            "UPDATE bills SET payment_method = %s WHERE bill_id = %s",
            (payment_method, bill_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

    return result(True, f"Bill #{bill_id} payment mode changed to {payment_method}.")


def ensure_missing_bills_for_user(user_id):
    """Create a basic Pending bill for any order owned by the user that has no bill."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT o.order_id
            FROM orders o
            LEFT JOIN bills b ON b.order_id = o.order_id
            WHERE o.user_id = %s
              AND b.bill_id IS NULL
            """,
            (user_id,),
        )
        missing_orders = cursor.fetchall()

        for order in missing_orders:
            cursor.execute(
                """
                SELECT COALESCE(SUM(quantity * price), 0) AS subtotal
                FROM order_items
                WHERE order_id = %s
                """,
                (order["order_id"],),
            )
            row = cursor.fetchone()
            subtotal = Decimal(str(row["subtotal"] or 0))
            tax = (subtotal * tax_multiplier()).quantize(Decimal("0.01"))
            discount = 0
            total = subtotal + tax - discount

            cursor.execute(
                """
                INSERT INTO bills
                    (order_id, subtotal, tax, discount, total_amount,
                     payment_method, payment_status)
                VALUES (%s, %s, %s, %s, %s, 'Cash', 'Pending')
                """,
                (
                    order["order_id"],
                    subtotal,
                    tax,
                    discount,
                    total,
                ),
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

@app.route("/billing")
def billing():

    connection = None
    cursor = None

    try:
        # Guarantee that every existing order has a bill visible in Billing Management.
        ensure_missing_bills_for_user(scope_user_id())

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # End users can choose any billing-history period.
        from_date = request.args.get("from_date", "").strip()
        to_date = request.args.get("to_date", "").strip()

        where_parts = ["o.user_id = %s"]
        params = [scope_user_id()]

        if from_date:
            where_parts.append("DATE(b.bill_date) >= %s")
            params.append(from_date)

        if to_date:
            where_parts.append("DATE(b.bill_date) <= %s")
            params.append(to_date)

        where_sql = ""
        if where_parts:
            where_sql = "WHERE " + " AND ".join(where_parts)

        # --------------------------------------
        # Bill history
        # --------------------------------------

        cursor.execute(f"""
            SELECT
                b.bill_id,
                b.order_id,
                b.subtotal,
                b.tax,
                b.discount,
                b.total_amount,
                b.payment_method,
                b.payment_status,
                b.bill_date,
                COALESCE(o.order_status, 'Unknown') AS order_status

            FROM bills b

            LEFT JOIN orders o
                ON b.order_id = o.order_id

            {where_sql}

            ORDER BY b.bill_date DESC, b.bill_id DESC
        """, tuple(params))

        bills = cursor.fetchall()

        # --------------------------------------
        # Get complete order-item details for each bill
        # --------------------------------------
        # Billing history keeps the bill even when an order is cancelled.
        # Fetch the original ordered foods/quantities so the billing page can
        # show the complete order without deleting or changing historical data.
        if bills:
            bill_order_ids = [bill["order_id"] for bill in bills]
            placeholders = ",".join(["%s"] * len(bill_order_ids))

            cursor.execute(f"""
                SELECT
                    oi.order_id,
                    oi.food_id,
                    oi.quantity,
                    oi.price,
                    oi.subtotal,
                    COALESCE(oi.item_name, f.food_name, 'Removed item')
                        AS food_name
                FROM order_items oi
                LEFT JOIN foods f
                    ON oi.food_id = f.food_id
                WHERE oi.order_id IN ({placeholders})
                ORDER BY oi.order_id DESC, oi.order_item_id ASC
            """, tuple(bill_order_ids))

            order_items_history = cursor.fetchall()

            items_by_order = {}
            for item in order_items_history:
                items_by_order.setdefault(item["order_id"], []).append(item)

            for bill in bills:
                bill["items"] = items_by_order.get(bill["order_id"], [])
        else:
            bills = []

        # --------------------------------------
        # Summary
        #
        # An admin is looking at whatever period the filter says. A cashier
        # is looking at their shift, so theirs is fixed to today whatever
        # the history below is filtered to - "12 bills, 9 paid" only means
        # something at the till if it means today.
        # --------------------------------------

        is_admin = session.get("role") == "admin"

        if is_admin:
            summary_where, summary_params = where_sql, params
        else:
            summary_where = ("WHERE o.user_id = %s "
                             "AND DATE(b.bill_date) = CURDATE()")
            summary_params = [scope_user_id()]

        cursor.execute(f"""
            SELECT
                COUNT(*) AS bills_count,
                SUM(CASE WHEN b.payment_status = 'Paid' THEN 1 ELSE 0 END) AS paid_count,
                SUM(CASE WHEN b.payment_status = 'Pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN COALESCE(o.order_status, '') = 'Cancelled' THEN 1 ELSE 0 END) AS cancelled_count,
                COALESCE(SUM(
                    CASE
                        WHEN b.payment_status = 'Paid'
                         AND COALESCE(o.order_status, '') != 'Cancelled'
                        THEN b.total_amount
                        ELSE 0
                    END
                ), 0) AS revenue

            FROM bills b

            LEFT JOIN orders o
                ON b.order_id = o.order_id

            {summary_where}
        """, tuple(summary_params))

        summary = cursor.fetchone()

        return render_template(
            "billing.html",
            bills=bills,
            bills_today=summary["bills_count"] or 0,
            paid_today=summary["paid_count"] or 0,
            pending_today=summary["pending_count"] or 0,
            cancelled_count=summary["cancelled_count"] or 0,
            revenue_today=summary["revenue"] or Decimal("0.00"),
            # Tells the page whether those figures describe the filtered
            # period or just today, so it can label them honestly.
            summary_is_today=not is_admin,
            from_date=from_date,
            to_date=to_date
        )

    except mysql.connector.Error as error:

        flash(f"Database error: {error}")
        return redirect(url_for("home"))

    finally:

        if cursor:
            cursor.close()
        if connection:
            connection.close()

# ==========================================
# REPORTS MANAGEMENT
# ==========================================

@app.route("/reports")
def reports():

    connection = None
    cursor = None

    try:

        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # ======================================
        # DATE FILTER
        # ======================================

        from_date = request.args.get("from_date")
        to_date = request.args.get("to_date")


        # If no dates are selected,
        # use all available records.

        if not from_date:
            from_date = None

        if not to_date:
            to_date = None


        # ======================================
        # TOTAL ORDERS
        # Excludes cancelled orders
        # ======================================

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM orders
            WHERE user_id = %s
              AND (%s IS NULL OR DATE(order_date) >= %s)
              AND (%s IS NULL OR DATE(order_date) <= %s)
              AND LOWER(COALESCE(order_status, '')) != 'cancelled'
        """, (
            scope_user_id(),
            from_date,
            from_date,
            to_date,
            to_date
        ))

        total_orders = cursor.fetchone()["total"]


        # ======================================
        # TOTAL BILLS
        # ======================================

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM bills b
            INNER JOIN orders o ON b.order_id=o.order_id
            WHERE o.user_id = %s
              AND (%s IS NULL OR DATE(b.bill_date) >= %s)
              AND (%s IS NULL OR DATE(b.bill_date) <= %s)
        """, (
            scope_user_id(),
            from_date,
            from_date,
            to_date,
            to_date
        ))

        total_bills = cursor.fetchone()["total"]


        # ======================================
        # TOTAL SALES
        #
        # Cancelled orders are excluded.
        # Only Paid bills are counted as sales.
        # ======================================

        cursor.execute("""
            SELECT
                COALESCE(SUM(b.total_amount), 0) AS total

            FROM bills b

            INNER JOIN orders o
                ON b.order_id = o.order_id

            WHERE
                (%s IS NULL OR DATE(b.bill_date) >= %s)

                AND

                (%s IS NULL OR DATE(b.bill_date) <= %s)

                AND

                o.user_id = %s
                AND
                LOWER(COALESCE(o.order_status, '')) != 'cancelled'

                AND

                LOWER(COALESCE(b.payment_status, '')) = 'paid'
        """, (
            from_date,
            from_date,
            to_date,
            to_date,
            scope_user_id()
        ))

        total_sales = cursor.fetchone()["total"]


        # ======================================
        # CANCELLED ORDERS
        # ======================================

        cursor.execute("""
            SELECT COUNT(*) AS total

            FROM orders

            WHERE user_id = %s
              AND LOWER(
                COALESCE(order_status, '')
            ) = 'cancelled'

            AND (%s IS NULL OR DATE(order_date) >= %s)

            AND (%s IS NULL OR DATE(order_date) <= %s)
        """, (
            scope_user_id(),
            from_date,
            from_date,
            to_date,
            to_date
        ))

        cancelled_orders = cursor.fetchone()["total"]


        # ======================================
        # PAYMENT SUMMARY
        # ======================================

        cursor.execute("""
            SELECT

                COALESCE(
                    b.payment_method,
                    'Not Selected'
                ) AS payment_method,

                COUNT(*) AS bill_count,

                COALESCE(
                    SUM(b.total_amount),
                    0
                ) AS amount

            FROM bills b

            INNER JOIN orders o
                ON b.order_id = o.order_id

            WHERE

                (%s IS NULL OR DATE(b.bill_date) >= %s)

                AND

                (%s IS NULL OR DATE(b.bill_date) <= %s)

                AND

                o.user_id = %s
                AND
                LOWER(
                    COALESCE(o.order_status, '')
                ) != 'cancelled'

                AND

                LOWER(
                    COALESCE(b.payment_status, '')
                ) = 'paid'

            GROUP BY b.payment_method

            ORDER BY amount DESC
        """, (
            from_date,
            from_date,
            to_date,
            to_date,
            scope_user_id()
        ))

        payment_summary = cursor.fetchall()


        # ======================================
        # FOOD SALES REPORT
        # ======================================

        cursor.execute("""
            SELECT

                COALESCE(oi.item_name, f.food_name, 'Removed item')
                    AS food_name,

                COALESCE(
                    SUM(oi.quantity),
                    0
                ) AS quantity_sold,

                COALESCE(
                    SUM(
                        oi.quantity * oi.price
                    ),
                    0
                ) AS sales

            FROM order_items oi

            LEFT JOIN foods f
                ON oi.food_id = f.food_id

            INNER JOIN orders o
                ON oi.order_id = o.order_id

            WHERE

                o.user_id = %s
                AND
                LOWER(
                    COALESCE(o.order_status, '')
                ) != 'cancelled'

                AND

                (%s IS NULL OR DATE(o.order_date) >= %s)

                AND

                (%s IS NULL OR DATE(o.order_date) <= %s)

            -- Grouped on the order line's own food_id, not the menu row's.
            -- After a deletion the menu row is gone and every removed food
            -- would otherwise collapse into one anonymous heap.
            GROUP BY
                oi.food_id,
                COALESCE(oi.item_name, f.food_name, 'Removed item')

            ORDER BY
                quantity_sold DESC
        """, (
            scope_user_id(),
            from_date,
            from_date,
            to_date,
            to_date
        ))

        food_sales = cursor.fetchall()


        # ======================================
        # INVENTORY REPORT
        #
        # This is current stock, so it is not
        # restricted by the report dates.
        # ======================================

        cursor.execute("""
            SELECT

                f.food_name,

                COALESCE(
                    i.quantity,
                    0
                ) AS quantity,

                COALESCE(
                    i.minimum_stock,
                    0
                ) AS minimum_stock

            FROM foods f

            LEFT JOIN inventory i
                ON f.food_id = i.food_id

            WHERE f.user_id = %s

            ORDER BY
                quantity ASC,

                f.food_name ASC
        """, (scope_user_id(),))

        inventory_report = cursor.fetchall()


        # ======================================
        # CANCELLED ORDER LIST
        # ======================================

        cursor.execute("""
            SELECT

                order_id,

                order_date,

                total_amount,

                order_status

            FROM orders

            WHERE user_id = %s
              AND LOWER(
                COALESCE(order_status, '')
            ) = 'cancelled'

            AND (%s IS NULL OR DATE(order_date) >= %s)

            AND (%s IS NULL OR DATE(order_date) <= %s)

            ORDER BY order_date DESC
        """, (
            scope_user_id(),
            from_date,
            from_date,
            to_date,
            to_date
        ))

        cancelled_order_list = cursor.fetchall()


        # ======================================
        # SEND DATA TO reports.html
        # ======================================

        return render_template(
            "reports.html",

            from_date=from_date,
            to_date=to_date,

            total_orders=total_orders,
            total_bills=total_bills,
            total_sales=total_sales,
            cancelled_orders=cancelled_orders,

            payment_summary=payment_summary,

            food_sales=food_sales,

            inventory_report=inventory_report,

            cancelled_order_list=cancelled_order_list
        )


    except mysql.connector.Error as error:

        flash(
            f"Reports database error: {error}"
        )

        return redirect(
            url_for("home")
        )


    finally:

        if cursor:
            cursor.close()

        if connection:
            connection.close()

# Dashboard live statistics
@app.route("/api/dashboard-stats")
def dashboard_stats():
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        uid = scope_user_id()

        # Same single statement the page itself uses. This endpoint is polled
        # continuously, so it is the one place where trimming round trips
        # matters most.
        counts = dashboard_counts(cursor, uid, session.get("role") == "admin")
        total_foods = counts["total_foods"]
        total_categories = counts["total_categories"]
        total_orders = counts["total_orders"]
        total_inventory = counts["total_inventory"]
        today_orders = counts["today_orders"]
        today_revenue = counts["today_revenue"]

        alerts = stock_alerts(cursor, uid)

        return {
            "total_foods": total_foods,
            "total_categories": total_categories,
            "total_orders": total_orders,
            "total_inventory": total_inventory,
            "weekday": today_weekday(),
            "today_orders": today_orders,
            "today_revenue": (float(today_revenue) if today_revenue is not None else None),
            "low_stock": alerts["low_stock"],
            "unavailable": alerts["unavailable"],
            # Names as well as counts, so the poll can keep the alert at the
            # top of the dashboard naming the right items as stock moves.
            "low_stock_items": alerts["low_stock_items"],
            "unavailable_items": alerts["unavailable_items"],
            "alert_name_limit": STOCK_ALERT_NAMES
        }
    except Exception as e:
        return {"error": str(e)}, 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# START FLASK


# ==========================================
# LOGIN / AUTHENTICATION
# ==========================================

@app.route("/login", methods=["GET", "POST"])
def login():

    try:
        ensure_auth_schema()
        ensure_payment_schema()
    except mysql.connector.Error as error:
        return f"Authentication database setup error: {error}", 500
    except RuntimeError as error:
        return f"Application setup error: {error}", 500

    if session.get("user_id"):
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        connection = None
        cursor = None

        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            cursor.execute("""
                SELECT user_id, username, password_hash,
                       full_name, role, is_active, phone_number, cafe_id
                FROM users
                WHERE username = %s
            """, (username,))

            user = cursor.fetchone()

            if (
                user
                and user["is_active"]
                and check_password_hash(user["password_hash"], password)
            ):
                next_page = request.args.get("next", "")
                next_is_safe = (
                    next_page.startswith("/")
                    and not next_page.startswith("//")
                )

                # Admins with a mobile number on file are challenged for a
                # one-time code before the session is actually created.
                # Managers/cashiers, and admins without a number yet, sign
                # in immediately as before.
                if user["role"] == "admin" and user["phone_number"]:
                    code = issue_login_otp(cursor, connection, user["user_id"])
                    sent_live = send_login_otp_sms(user["phone_number"], code)

                    session.clear()
                    session["otp_user_id"] = user["user_id"]
                    session["otp_last_sent"] = time.time()
                    if next_is_safe:
                        session["otp_next"] = next_page

                    if sent_live:
                        flash(
                            "Enter the verification code sent to your "
                            "registered mobile number."
                        )
                    else:
                        flash(
                            "Development mode — SMS is not configured, so "
                            f"here is your code: {code}"
                        )

                    return redirect(url_for("login_verify_otp"))

                session.clear()
                session.permanent = True
                session["user_id"] = user["user_id"]
                session["username"] = user["username"]
                session["role"] = user["role"]
                session["cafe_id"] = user.get("cafe_id")

                if user["role"] == "admin" and not user["phone_number"]:
                    flash(
                        "Add a mobile number for this account in User "
                        "Management to turn on one-time code login."
                    )

                if next_is_safe:
                    return redirect(next_page)

                if user["role"] != "admin":
                    return redirect(url_for("add_order"))

                return redirect(url_for("home"))

            flash("Invalid username or password.")

        except mysql.connector.Error as error:
            flash(f"Login database error: {error}")
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("login.html")


@app.route("/login/verify", methods=["GET", "POST"])
def login_verify_otp():

    pending_user_id = session.get("otp_user_id")
    if not pending_user_id:
        flash("Please sign in again.")
        return redirect(url_for("login"))

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT user_id, username, full_name, role, is_active, phone_number, cafe_id
            FROM users
            WHERE user_id = %s
        """, (pending_user_id,))
        user = cursor.fetchone()

        if not user or not user["is_active"] or user["role"] != "admin":
            session.pop("otp_user_id", None)
            session.pop("otp_next", None)
            session.pop("otp_last_sent", None)
            flash("Please sign in again.")
            return redirect(url_for("login"))

        if request.method == "POST":
            submitted_code = request.form.get("otp_code", "").strip()

            # Expiry is evaluated by the database (NOW()) rather than by
            # comparing against the web server's clock: the app and the
            # managed database can sit in different time zones, which made
            # codes appear expired on arrival or stay valid for hours.
            cursor.execute("""
                SELECT otp_id, code_hash, attempts,
                       (expires_at < NOW()) AS is_expired
                FROM login_otp_codes
                WHERE user_id = %s
                ORDER BY otp_id DESC
                LIMIT 1
            """, (pending_user_id,))
            otp_row = cursor.fetchone()

            if not otp_row:
                flash("Your code has expired. Request a new one.")
                return redirect(url_for("login_verify_otp"))

            if otp_row["is_expired"]:
                cursor.execute(
                    "DELETE FROM login_otp_codes WHERE otp_id = %s",
                    (otp_row["otp_id"],)
                )
                connection.commit()
                flash("Your code has expired. Request a new one.")
                return redirect(url_for("login_verify_otp"))

            if otp_row["attempts"] >= OTP_MAX_ATTEMPTS:
                cursor.execute(
                    "DELETE FROM login_otp_codes WHERE otp_id = %s",
                    (otp_row["otp_id"],)
                )
                connection.commit()
                flash("Too many incorrect attempts. Request a new code.")
                return redirect(url_for("login_verify_otp"))

            if submitted_code and check_password_hash(otp_row["code_hash"], submitted_code):
                cursor.execute(
                    "DELETE FROM login_otp_codes WHERE user_id = %s",
                    (pending_user_id,)
                )
                connection.commit()

                next_page = session.get("otp_next", "")
                session.clear()
                session.permanent = True
                session["user_id"] = user["user_id"]
                session["username"] = user["username"]
                session["role"] = user["role"]
                session["cafe_id"] = user.get("cafe_id")

                if next_page.startswith("/") and not next_page.startswith("//"):
                    return redirect(next_page)

                return redirect(url_for("home"))

            cursor.execute("""
                UPDATE login_otp_codes SET attempts = attempts + 1
                WHERE otp_id = %s
            """, (otp_row["otp_id"],))
            connection.commit()

            remaining = max(0, OTP_MAX_ATTEMPTS - (otp_row["attempts"] + 1))
            flash(f"Incorrect code. {remaining} attempt(s) left.")

        return render_template(
            "verify_otp.html",
            masked_phone=mask_phone_number(user["phone_number"])
        )

    except mysql.connector.Error as error:
        flash(f"Verification error: {error}")
        return redirect(url_for("login_verify_otp"))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/login/resend-otp", methods=["POST"])
def login_resend_otp():

    pending_user_id = session.get("otp_user_id")
    if not pending_user_id:
        flash("Please sign in again.")
        return redirect(url_for("login"))

    last_sent = session.get("otp_last_sent")
    if last_sent and (time.time() - last_sent) < OTP_RESEND_COOLDOWN_SECONDS:
        flash("Please wait a few seconds before requesting another code.")
        return redirect(url_for("login_verify_otp"))

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT user_id, role, is_active, phone_number
            FROM users
            WHERE user_id = %s
        """, (pending_user_id,))
        user = cursor.fetchone()

        if not user or not user["is_active"] or user["role"] != "admin" or not user["phone_number"]:
            session.pop("otp_user_id", None)
            session.pop("otp_next", None)
            session.pop("otp_last_sent", None)
            flash("Please sign in again.")
            return redirect(url_for("login"))

        code = issue_login_otp(cursor, connection, pending_user_id)
        sent_live = send_login_otp_sms(user["phone_number"], code)
        session["otp_last_sent"] = time.time()

        if sent_live:
            flash("A new code has been sent to your mobile number.")
        else:
            flash(
                "Development mode — SMS is not configured, so here is "
                f"your new code: {code}"
            )

    except mysql.connector.Error as error:
        flash(f"Could not send a new code: {error}")
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    return redirect(url_for("login_verify_otp"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route('/register', methods=['GET','POST'])
def register():
    """Public SaaS signup: one new cafe plus its first admin owner."""
    try: ensure_auth_schema()
    except mysql.connector.Error as error: return f'Authentication database setup error: {error}',500
    if session.get('user_id'): return redirect(url_for('home'))
    if request.method=='POST':
        cafe_name=request.form.get('cafe_name','').strip(); full_name=request.form.get('full_name','').strip()
        username=request.form.get('username','').strip(); phone=request.form.get('phone_number','').strip()
        password=request.form.get('password',''); confirm=request.form.get('confirm_password','')
        if not cafe_name or not full_name or not username: flash('Café name, full name and username are required.'); return redirect(url_for('register'))
        if len(password)<8: flash('Password must be at least 8 characters.'); return redirect(url_for('register'))
        if password!=confirm: flash('Password and confirm password do not match.'); return redirect(url_for('register'))
        c=None; cur=None
        try:
            c=get_db_connection(); cur=c.cursor()
            cur.execute('SELECT user_id FROM users WHERE username=%s',(username,))
            if cur.fetchone(): flash('That username is already in use.'); return redirect(url_for('register'))
            cur.execute('INSERT INTO cafes(cafe_name) VALUES(%s)',(cafe_name,)); cid=cur.lastrowid
            cur.execute("""INSERT INTO users(username,password_hash,full_name,role,is_active,phone_number,cafe_id)
                         VALUES(%s,%s,%s,'admin',1,%s,%s)""",(username,generate_password_hash(password),full_name,phone or None,cid))
            uid=cur.lastrowid; cur.execute('UPDATE cafes SET owner_user_id=%s WHERE cafe_id=%s',(uid,cid)); c.commit()
            session.clear(); session.permanent=True; session['user_id']=uid; session['username']=username; session['role']='admin'; session['cafe_id']=cid
            flash(f'Welcome! Your café "{cafe_name}" has been created.')
            return redirect(url_for('home'))
        except mysql.connector.Error as error:
            if c: c.rollback()
            flash(f'Registration error: {error}')
        finally:
            if cur: cur.close()
            if c: c.close()
    return render_template('register.html')


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():

    try:
        ensure_auth_schema()
    except mysql.connector.Error as error:
        return f"Authentication database setup error: {error}", 500

    if session.get("user_id"):
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        phone_number = request.form.get("phone_number", "").strip()
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if not username or not full_name:
            flash("Please enter your username and full name.")
            return redirect(url_for("forgot_password"))

        if len(new_password) < 8:
            flash("New password must be at least 8 characters.")
            return redirect(url_for("forgot_password"))

        if new_password != confirm:
            flash("New passwords do not match.")
            return redirect(url_for("forgot_password"))

        connection = None
        cursor = None
        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            cursor.execute("""
                SELECT user_id, full_name, is_active, phone_number
                FROM users
                WHERE username = %s
            """, (username,))

            user = cursor.fetchone()

            # Username + full name alone was NOT a security check: full names
            # are displayed throughout the UI (order lists, user management),
            # so anyone who could see a colleague's name could take over that
            # account. The registered mobile number is required as a shared
            # secret, and an account with no number on file cannot be reset
            # self-service at all - an admin must reset it from User
            # Management instead.
            digits = "".join(ch for ch in phone_number if ch.isdigit())
            on_file = "".join(
                ch for ch in (user["phone_number"] or "") if ch.isdigit()
            ) if user else ""

            identity_ok = (
                user
                and user["is_active"]
                and user["full_name"].strip().lower() == full_name.lower()
                and on_file
                and digits
                # Compare the last 10 digits so +91 / 0 prefixes both match.
                and hmac.compare_digest(on_file[-10:], digits[-10:])
            )

            if not identity_ok:
                # Deliberately vague: revealing which field was wrong would
                # let an attacker confirm usernames and phone numbers.
                flash(
                    "We couldn't verify that account. Check your username, "
                    "full name and registered mobile number. If no mobile "
                    "number is on file, ask an admin to reset your password."
                )
                return redirect(url_for("forgot_password"))

            cursor.execute("""
                UPDATE users
                SET password_hash=%s
                WHERE user_id=%s
            """, (generate_password_hash(new_password), user["user_id"]))
            connection.commit()

            flash("Password reset. You can sign in with your new password now.")
            return redirect(url_for("login"))

        except mysql.connector.Error as error:
            flash(f"Password reset error: {error}")
            return redirect(url_for("forgot_password"))
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("forgot_password.html")


@app.before_request
def require_login():
    # Initialize/migrate authentication schema before protected requests.
    # Login/forgot-password/static remain publicly reachable.
    if request.endpoint in {
        "login", "login_verify_otp", "login_resend_otp",
        "register", "forgot_password", "static", "razorpay_webhook",
        "healthz", "cafe_media",
        # The manifest is what lets the site be installed as an app.
        # It falls back to the platform name with no session, so it
        # is safe to answer before sign-in.
        "web_manifest",
        # A customer scanning the code on their table is not a user
        # of this system and never signs in. These are the whole of
        # what they can reach.
        "public_menu", "public_place_order", "public_order_placed",
        # And the one their page asks, over and over, to find out
        # whether the food is ready.
        "public_order_status",
        # The browser asks for the icon on the sign-in screen too. Without
        # this it is redirected to /login, and the browser then renders the
        # whole login page again - a wasted database round-trip on every
        # signed-out visit, to answer a request for a favicon.
        "favicon",
    }:
        # Razorpay webhooks are authenticated with their own HMAC signature.
        return

    try:
        ensure_auth_schema()
    except mysql.connector.Error as error:
        return f"Authentication database setup error: {error}", 500

    if not session.get("user_id"):
        return redirect(url_for("login", next=request.path))

    # CSRF protection for all state-changing requests.
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        token = request.form.get("_csrf_token") or request.headers.get("X-CSRFToken")
        expected = session.get("_csrf_token")
        if not expected:
            expected = secrets.token_urlsafe(32)
            session["_csrf_token"] = expected
        if not token or not hmac.compare_digest(token, expected):
            return "Invalid CSRF token. Please refresh the page and try again.", 400

    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_urlsafe(32)

    # Resolve the café before looking the user up. This used to live in a
    # second before_request hook that Flask ran *after* this one, so a session
    # without cafe_id blew up here before the repair hook ever executed.
    if not get_current_cafe_id():
        session.clear()
        flash("Your session has expired. Please sign in again.")
        return redirect(url_for("login"))

    user = get_current_user()
    if not user or not user["is_active"]:
        session.clear()
        return redirect(url_for("login"))

    if user["role"] != "admin" and request.endpoint not in STAFF_ALLOWED_ENDPOINTS:
        flash("You do not have permission to access that page.")
        return redirect(url_for("add_order"))

    return None



# The former attach_cafe_to_session hook has been folded into require_login
# above: Flask runs before_request hooks in registration order, so as a
# separate hook it ran too late to repair anything.


@app.context_processor
def inject_security_context():
    return {
        "current_user": get_current_user(),
        "csrf_token_value": session.get("_csrf_token", "")
    }


# ==========================================
# USER MANAGEMENT
# ==========================================


@app.route("/account/password", methods=["GET", "POST"])
def change_password():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    if request.method == "POST":
        current = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")

        if len(new_password) < 8:
            flash("New password must be at least 8 characters.")
            return redirect(stay_on('change_password'))

        if new_password != confirm:
            flash("New passwords do not match.")
            return redirect(stay_on('change_password'))

        connection = None
        cursor = None
        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                "SELECT password_hash FROM users WHERE user_id=%s",
                (user["user_id"],)
            )
            row = cursor.fetchone()

            if not row or not check_password_hash(row["password_hash"], current):
                flash("Current password is incorrect.")
                return redirect(stay_on('change_password'))

            cursor.execute("""
                UPDATE users
                SET password_hash=%s
                WHERE user_id=%s
            """, (generate_password_hash(new_password), user["user_id"]))
            connection.commit()
            flash("Password changed successfully.")
            return redirect(url_for("home"))
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("change_password.html")


@app.route("/users")
def users():
    denied = require_role("admin")
    if denied:
        return denied

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT user_id, username, full_name, role, is_active, created_at, phone_number
            FROM users
            WHERE cafe_id = %s
            ORDER BY user_id DESC
        """, (require_cafe_session(),))
        user_list = cursor.fetchall()
        return render_template(
            "users.html",
            users=user_list,
            # The owner account cannot be deleted; the template uses this to
            # leave the button off that row rather than offer an action that
            # would just come back refused.
            cafe_owner_id=get_cafe_owner_id(),
        )
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/users/add", methods=["GET", "POST"])
def add_user():
    denied = require_role("admin")
    if denied:
        return denied

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "cashier")
        phone_number = request.form.get("phone_number", "").strip()

        if not username or not full_name or len(password) < 8:
            flash("Username, full name and a password of at least 8 characters are required.")
            return redirect(url_for("add_user"))

        if role not in {"admin", "manager", "cashier", "staff"}:
            flash("Invalid role.")
            return redirect(url_for("add_user"))

        connection = None
        cursor = None
        try:
            connection = get_db_connection()
            cursor = connection.cursor()
            # Was: six columns declared against seven values, with cafe_id
            # missing from the column list - every staff account creation
            # failed with "Column count doesn't match value count".
            cursor.execute("""
                INSERT INTO users
                    (username, password_hash, full_name, role,
                     is_active, phone_number, cafe_id)
                VALUES (%s, %s, %s, %s, 1, %s, %s)
            """, (
                username,
                generate_password_hash(password),
                full_name,
                role,
                phone_number or None,
                require_cafe_session(),
            ))
            connection.commit()
            flash(f"User '{username}' created successfully.")

            if role == "admin" and not phone_number:
                flash(
                    "Tip: add a mobile number for this admin to turn on "
                    "one-time code login."
                )

            return redirect(url_for("users"))
        except mysql.connector.IntegrityError:
            if connection:
                connection.rollback()
            flash("That username is already in use.")
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("user_form.html", user=None)


@app.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
def edit_user(user_id):
    denied = require_role("admin")
    if denied:
        return denied

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # The cafe_id filter is the tenant boundary. Without it any café
        # admin could edit - and reset the password of - any user in any
        # other café just by changing the id in the URL.
        cursor.execute("""
            SELECT user_id, username, full_name, role, is_active, phone_number
            FROM users
            WHERE user_id = %s AND cafe_id = %s
        """, (user_id, require_cafe_session()))
        user = cursor.fetchone()

        if not user:
            flash("User not found.")
            return redirect(url_for("users"))

        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            role = request.form.get("role", "cashier")
            is_active = 1 if request.form.get("is_active") else 0
            new_password = request.form.get("password", "")
            phone_number = request.form.get("phone_number", "").strip()

            if not full_name or role not in {"admin", "manager", "cashier", "staff"}:
                flash("Please provide valid user details.")
                return redirect(url_for("edit_user", user_id=user_id))

            if user_id == session.get("user_id") and not is_active:
                flash("You cannot deactivate your own account.")
                return redirect(url_for("edit_user", user_id=user_id))

            # Demoting or deactivating the last active admin would leave the
            # café with no one who can reach User Management, Branding or
            # Reports - an unrecoverable lockout for that tenant.
            if user["role"] == "admin" and (role != "admin" or not is_active):
                cursor.execute("""
                    SELECT COUNT(*) AS n FROM users
                    WHERE cafe_id = %s AND role = 'admin'
                      AND is_active = 1 AND user_id != %s
                """, (require_cafe_session(), user_id))
                if cursor.fetchone()["n"] == 0:
                    flash(
                        "This is the only active admin for your café. "
                        "Promote another user to admin first."
                    )
                    return redirect(url_for("edit_user", user_id=user_id))

            if new_password:
                if len(new_password) < 8:
                    flash("New password must be at least 8 characters.")
                    return redirect(url_for("edit_user", user_id=user_id))
                cursor.execute("""
                    UPDATE users
                    SET full_name=%s, role=%s, is_active=%s,
                        phone_number=%s, password_hash=%s
                    WHERE user_id=%s AND cafe_id=%s
                """, (
                    full_name, role, is_active,
                    phone_number or None,
                    generate_password_hash(new_password),
                    user_id, require_cafe_session()
                ))
            else:
                cursor.execute("""
                    UPDATE users
                    SET full_name=%s, role=%s, is_active=%s, phone_number=%s
                    WHERE user_id=%s AND cafe_id=%s
                """, (
                    full_name, role, is_active, phone_number or None,
                    user_id, require_cafe_session()
                ))

            connection.commit()
            flash("User updated successfully.")

            if role == "admin" and not phone_number:
                flash(
                    "Tip: add a mobile number for this admin to turn on "
                    "one-time code login."
                )

            return redirect(url_for("users"))

        return render_template("user_form.html", user=user)

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/users/<int:user_id>/toggle", methods=["POST"])
def toggle_user(user_id):
    denied = require_role("admin")
    if denied:
        return denied

    if user_id == session.get("user_id"):
        flash("You cannot deactivate your own account.")
        return redirect(url_for("users"))

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        # Same tenant boundary as edit_user: this UPDATE previously matched
        # on user_id alone, so one café's admin could deactivate staff
        # belonging to every other café on the platform.
        cursor.execute("""
            SELECT user_id, role, is_active FROM users
            WHERE user_id = %s AND cafe_id = %s
        """, (user_id, require_cafe_session()))
        target = cursor.fetchone()

        if not target:
            flash("User not found.")
            return redirect(url_for("users"))

        if target["role"] == "admin" and target["is_active"]:
            cursor.execute("""
                SELECT COUNT(*) AS n FROM users
                WHERE cafe_id = %s AND role = 'admin'
                  AND is_active = 1 AND user_id != %s
            """, (require_cafe_session(), user_id))
            if cursor.fetchone()["n"] == 0:
                flash(
                    "This is the only active admin for your café. "
                    "Promote another user to admin first."
                )
                return redirect(url_for("users"))

        cursor.execute("""
            UPDATE users
            SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
            WHERE user_id = %s AND cafe_id = %s
        """, (user_id, require_cafe_session()))
        connection.commit()
        flash("User status updated.")
        return redirect(url_for("users"))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/users/<int:user_id>/delete", methods=["POST"])
def delete_user(user_id):
    """
    Remove a staff account for good.

    Deactivating keeps someone in the list; this is for accounts that should
    not be there at all - a mistyped signup, someone who has left. Orders and
    bills are untouched: they are tagged with the café owner's id, not the id
    of whoever was on the till, so history survives a staff account going
    away.

    Three accounts are refused, because deleting them breaks something that
    cannot be undone from the UI:
      * your own, so an admin cannot lock themselves out mid-session;
      * the café owner, whose id every food, category and order row carries -
        losing it would orphan the entire café's data;
      * the last active admin, which would leave nobody able to administer.
    """
    denied = require_role("admin")
    if denied:
        return denied

    if user_id == session.get("user_id"):
        flash("You cannot delete your own account.")
        return redirect(url_for("users"))

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cafe_id = require_cafe_session()

        # Same tenant boundary as edit_user and toggle_user: match on cafe_id
        # as well as user_id, or one café's admin could delete another's staff.
        cursor.execute("""
            SELECT user_id, username, full_name, role, is_active
            FROM users
            WHERE user_id = %s AND cafe_id = %s
        """, (user_id, cafe_id))
        target = cursor.fetchone()

        if not target:
            flash("User not found.")
            return redirect(url_for("users"))

        if user_id == get_cafe_owner_id():
            flash(
                "This is the café's owner account. Every food item, category "
                "and order is filed under it, so it cannot be deleted."
            )
            return redirect(url_for("users"))

        if target["role"] == "admin" and target["is_active"]:
            cursor.execute("""
                SELECT COUNT(*) AS n FROM users
                WHERE cafe_id = %s AND role = 'admin'
                  AND is_active = 1 AND user_id != %s
            """, (cafe_id, user_id))
            if cursor.fetchone()["n"] == 0:
                flash(
                    "This is the only active admin for your café. "
                    "Promote another user to admin first."
                )
                return redirect(url_for("users"))

        # Pending one-time codes cascade with the row, but say so explicitly
        # rather than relying on the constraint being present on every
        # deployment - older databases were created before it existed.
        cursor.execute("DELETE FROM login_otp_codes WHERE user_id = %s", (user_id,))
        cursor.execute(
            "DELETE FROM users WHERE user_id = %s AND cafe_id = %s",
            (user_id, cafe_id)
        )
        connection.commit()

        flash("%s has been deleted." % (target["full_name"] or target["username"]))
        return redirect(url_for("users"))

    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        flash(f"Could not delete that user: {error}")
        return redirect(url_for("users"))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


# ==========================================
# CAFE BRANDING / CUSTOMIZATION
# ==========================================
#
# Branding used to be a single cafe_branding.json file at the project root
# with fixed image filenames (cafe_logo.png / cafe_login.png). On a
# multi-tenant deployment that meant every café overwrote every other café's
# name and logo, and on Render the file and images were wiped on each deploy.
# Branding now lives on the cafes row for that tenant, images included.


# What the sidebar reads when a café has not chosen its own. These are
# exactly what every café saw before the wording was customisable.
DEFAULT_BRAND_NAME = "Cafe Manager"
DEFAULT_BRAND_TAGLINE = "Food & Service Admin"


# A line for the foot of a customer's bill. Plain sayings rather than
# quotations, so nothing is put in anyone's mouth: attributing a famous
# line to the wrong person on a printed receipt is not a mistake a café
# should be making on our behalf.
CAFE_QUOTES = [
    "Life begins after coffee.",
    "Brewed with care, served with a smile.",
    "Come for the coffee, stay for the company.",
    "A little coffee, a lot of happiness.",
    "Good food, good mood.",
    "Where every cup tells a story.",
    "Made fresh this morning, served warm just now.",
    "Happiness is a full cup and good company.",
    "The best conversations start over a warm cup.",
    "Every order here is made one at a time.",
]


def quote_for(seed):
    """
    The line printed on one bill.

    Chosen from the order's own number rather than at random, so the same
    bill reprinted says the same thing - a customer handed two copies
    should not find two different footers.
    """
    try:
        index = int(seed)
    except (TypeError, ValueError):
        index = 0
    return CAFE_QUOTES[index % len(CAFE_QUOTES)]


def today_weekday():
    """
    Today's name, for the label on the dashboard's Today card.

    The card used to carry Bootstrap's calendar-day icon, which has the
    word "Fri" drawn into the font: it read Friday on a Tuesday, for
    everyone, for ever.
    """
    return {
        "short": datetime.now().strftime("%a"),
        "full": datetime.now().strftime("%A"),
    }


def get_cafe_branding(cafe_id):
    """Branding for one café, or the platform defaults when unknown."""
    # get_current_user() already read this off the café row it joins, so in
    # a normal signed-in request there is nothing left to fetch.
    if (cafe_id and has_request_context() and "cafe_branding" in g
            and g.get("cafe_branding_id") == cafe_id):
        return g.cafe_branding

    defaults = {
        "cafe_name": app.config["CAFE_NAME"],
        "brand_name": DEFAULT_BRAND_NAME,
        "brand_tagline": DEFAULT_BRAND_TAGLINE,
        "logo": "",
        "login_photo": "",
    }

    if not cafe_id:
        return defaults

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT cafe_name,
                   brand_name,
                   brand_tagline,
                   branding_version,
                   (logo_blob IS NOT NULL) AS has_logo,
                   (login_photo_blob IS NOT NULL) AS has_login_photo
            FROM cafes
            WHERE cafe_id = %s
        """, (cafe_id,))
        row = cursor.fetchone()
    except mysql.connector.Error:
        # Branding must never take a page down.
        app.logger.exception("branding lookup failed")
        return defaults
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    if not row:
        return defaults

    version = row["branding_version"] or 1
    return {
        "cafe_name": row["cafe_name"] or defaults["cafe_name"],
        "brand_name": (row["brand_name"] or "").strip() or DEFAULT_BRAND_NAME,
        "brand_tagline": ((row["brand_tagline"] or "").strip()
                          or DEFAULT_BRAND_TAGLINE),
        "logo": (
            url_for("cafe_media", cafe_id=cafe_id, kind="logo", v=version)
            if row["has_logo"] else ""
        ),
        "login_photo": (
            url_for("cafe_media", cafe_id=cafe_id, kind="login", v=version)
            if row["has_login_photo"] else ""
        ),
    }


@app.route("/settings/branding", methods=["GET", "POST"])
def branding():
    """
    What the sidebar says, and the symbol beside it.

    Admin only. This is the name every member of the café sees on every
    page, so it is a decision about the business rather than a personal
    preference.

    Left alone it reads "Cafe Manager / Food & Service Admin", which is
    what it has always said. Clearing a field puts that default back
    rather than leaving a blank corner.
    """
    denied = require_role("admin")
    if denied:
        return denied

    cafe_id = require_cafe_session()

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        if request.method == "POST":
            if request.form.get("action") == "remove_logo":
                cursor.execute("""
                    UPDATE cafes
                    SET logo_blob = NULL,
                        logo_mime = NULL,
                        branding_version = branding_version + 1
                    WHERE cafe_id = %s
                """, (cafe_id,))
                connection.commit()
                g.pop("cafe_branding", None)
                flash("Symbol removed. The name now stands on its own.")
                return redirect(came_from())

            # Empty means "use the default", not "show nothing".
            name = (request.form.get("brand_name") or "").strip()[:150]
            tagline = (request.form.get("brand_tagline") or "").strip()[:150]

            try:
                data, mime = read_image_upload(request.files.get("logo"))
            except ValueError as error:
                flash(str(error))
                return redirect(stay_on('branding'))

            if data is not None:
                cursor.execute("""
                    UPDATE cafes
                    SET brand_name = %s,
                        brand_tagline = %s,
                        logo_blob = %s,
                        logo_mime = %s,
                        branding_version = branding_version + 1
                    WHERE cafe_id = %s
                """, (name or None, tagline or None, data, mime, cafe_id))
            else:
                cursor.execute("""
                    UPDATE cafes
                    SET brand_name = %s,
                        brand_tagline = %s
                    WHERE cafe_id = %s
                """, (name or None, tagline or None, cafe_id))

            connection.commit()
            g.pop("cafe_branding", None)

            flash("Saved. Every page now reads %s."
                  % (name or DEFAULT_BRAND_NAME))
            return redirect(came_from())

        return render_template(
            "branding.html",
            branding=get_cafe_branding(cafe_id),
            default_name=DEFAULT_BRAND_NAME,
            default_tagline=DEFAULT_BRAND_TAGLINE,
        )
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/settings/qr", methods=["GET", "POST"])
def qr_settings():
    """
    The cafe's own code, to print and put on the tables.

    Admin only, because replacing it invalidates every code already
    printed and stuck to a table.
    """
    denied = require_role("admin")
    if denied:
        return denied

    cafe_id = require_cafe_session()

    if request.method == "POST":
        connection = None
        cursor = None
        try:
            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)
            cursor.execute(
                "UPDATE cafes SET public_token = %s WHERE cafe_id = %s",
                (new_public_token(), cafe_id))
            connection.commit()
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

        flash("A new code was made. Every code already printed has stopped "
              "working - print and put out the new one.")
        return redirect(url_for("qr_settings"))

    token = get_public_token(cafe_id)
    menu_url = url_for("public_menu", token=token, _external=True)

    return render_template(
        "qr_settings.html",
        token=token,
        menu_url=menu_url,
        qr=qr_svg(menu_url),
        branding=get_cafe_branding(cafe_id),
    )


@app.route("/kitchen")
def kitchen_display():
    """
    The screen left on in the kitchen.

    Its whole job is to be open. It shows what is waiting, prints the
    ticket for anything a customer sent from their phone, and says it is
    there so the counter screens stop trying to print those tickets
    themselves - otherwise a customer's order comes out of whichever
    printer somebody happened to leave a tab in front of.
    """
    return render_template(
        "kitchen.html",
        stale_after=KITCHEN_STALE_SECONDS,
    )


@app.route("/api/kitchen/heartbeat", methods=["POST"])
def kitchen_heartbeat():
    """A kitchen screen saying it is still there."""
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute(
            "UPDATE cafes SET kitchen_seen_at = %s WHERE cafe_id = %s",
            (datetime.now(), require_cafe_session()))
        connection.commit()
        return jsonify({"ok": True})
    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        return jsonify({"ok": False, "error": str(error)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def kitchen_is_watching(cursor, cafe_id):
    """
    Whether a kitchen screen has checked in recently enough to trust.

    Stale means the tablet was switched off or the browser closed, and
    the counter screens should take the printing back rather than leave
    tickets unprinted.
    """
    if not cafe_id:
        return False

    cursor.execute(
        "SELECT kitchen_seen_at FROM cafes WHERE cafe_id = %s", (cafe_id,))
    row = cursor.fetchone()
    seen = row["kitchen_seen_at"] if row else None

    if not seen:
        return False
    if isinstance(seen, str):
        try:
            seen = datetime.strptime(seen[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False

    return (datetime.now() - seen).total_seconds() <= KITCHEN_STALE_SECONDS


# Which day each cafe was last tidied up for, per worker process. The
# sweep below changes nothing on a second run, so the worst a worker's
# separate copy of this costs is one more UPDATE that matches no rows.
_ORDERS_SWEPT_FOR = {}
_ORDERS_SWEPT_LOCK = threading.Lock()


def close_yesterdays_orders(cursor, owner_id, today):
    """
    Yesterday's untouched orders, marked done.

    The kitchen screen shows one day at a time. An order nobody pressed
    Done or Cancel on before closing would otherwise sit waiting for
    ever - off the board, out of sight, and still counted as outstanding
    by everything that asks. Once its day is over there is nothing left
    to decide: the food went out or it did not, and the kitchen is
    certainly not making it now.

    Cancelled orders are left alone. Somebody said no to those on
    purpose, and that is not the same as forgetting.

    Orders older than this column have no day recorded at all. They are
    from before it existed, so they are as finished as the rest.
    """
    with _ORDERS_SWEPT_LOCK:
        if _ORDERS_SWEPT_FOR.get(owner_id) == today:
            return 0

    cursor.execute(
        "UPDATE orders SET order_status = 'Completed' "
        "WHERE user_id = %s AND order_status = 'Pending' "
        "  AND (order_day < %s OR order_day IS NULL)",
        (owner_id, today)
    )
    closed = cursor.rowcount or 0

    with _ORDERS_SWEPT_LOCK:
        _ORDERS_SWEPT_FOR[owner_id] = today

    return closed


@app.route("/api/kitchen/board")
def kitchen_board():
    """
    Everything the kitchen screen draws: the day's orders and their lines.

    Waiting orders come first, oldest first, because that is the order a
    kitchen works in. Finished and cancelled ones follow, newest first, so
    the one just dealt with is at the top of them and the dot that turned
    green is where the eye already is. When there are more than fit, it is
    the oldest finished ones that fall off the end rather than anything
    still to be made.

    Grouped by order_day rather than by the timestamp, because that is the
    column the order numbers are counted within - a customer holding number
    7 and a kitchen that disagreed about which day it was would be looking
    for different orders.

    One request rather than one per order - a screen refreshing every few
    seconds must not cost a query per ticket on it.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        owner = scope_user_id()
        today = date.today()

        # The screen that is always on is the one that notices the day
        # turned over, so this is where the previous day is closed off.
        if close_yesterdays_orders(cursor, owner, today):
            connection.commit()

        cursor.execute("""
            SELECT order_id, order_date, total_amount, source,
                   kot_printed, daily_no, order_status
            FROM orders
            WHERE user_id = %s AND order_day = %s
            ORDER BY CASE WHEN order_status = 'Pending' THEN 0 ELSE 1 END,
                     CASE WHEN order_status = 'Pending'
                          THEN order_id ELSE -order_id END
            LIMIT 60
        """, (owner, today))
        orders = cursor.fetchall()

        lines = {}
        if orders:
            ids = [row["order_id"] for row in orders]
            marks = ", ".join(["%s"] * len(ids))
            cursor.execute("""
                SELECT order_id, item_name, quantity
                FROM order_items
                WHERE order_id IN (%s)
                ORDER BY order_item_id
            """ % marks, tuple(ids))
            for row in cursor.fetchall():
                lines.setdefault(row["order_id"], []).append({
                    "name": row["item_name"],
                    "quantity": row["quantity"],
                })

        return jsonify({
            "orders": [
                {
                    "order_id": row["order_id"],
                    "daily_no": row["daily_no"],
                    "placed": format_order_time(row["order_date"]),
                    "total": "%.2f" % float(row["total_amount"] or 0),
                    "source": row["source"] or "counter",
                    "status": row["order_status"],
                    "printed": bool(row["kot_printed"]),
                    "items": lines.get(row["order_id"], []),
                }
                for row in orders
            ],
        })
    except mysql.connector.Error as error:
        return jsonify({"orders": [], "error": str(error)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/api/kitchen/pending")
def kitchen_pending():
    """
    Orders a customer sent from their phone that no till has printed yet.

    Polled by whichever staff screens are open. Nobody is standing at the
    counter when a QR order arrives, so the ticket has to be pulled rather
    than pushed.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT order_id
            FROM orders
            WHERE user_id = %s
              AND source = 'qr'
              AND kot_printed = 0
              AND order_status = 'Pending'
            ORDER BY order_id
            LIMIT 10
        """, (scope_user_id(),))
        return jsonify({
            "orders": [row["order_id"] for row in cursor.fetchall()],
            # A counter screen reads this and leaves the printing to the
            # kitchen while one is watching.
            "kitchen_watching": kitchen_is_watching(
                cursor, session.get("cafe_id")),
        })
    except mysql.connector.Error as error:
        return jsonify({"orders": [], "error": str(error)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/api/kitchen/claim/<int:order_id>", methods=["POST"])
def kitchen_claim(order_id):
    """
    Claim one order's kitchen ticket, so only one screen prints it.

    The claim is the UPDATE itself: whichever request matches the row
    first flips kot_printed and every other one matches nothing. Two
    tills watching the same kitchen therefore print one ticket between
    them, not two.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            UPDATE orders
            SET kot_printed = 1
            WHERE order_id = %s AND user_id = %s AND kot_printed = 0
        """, (order_id, scope_user_id()))
        won = cursor.rowcount == 1
        connection.commit()
        return jsonify({"claimed": won})
    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        return jsonify({"claimed": False, "error": str(error)}), 500
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/settings/tax", methods=["GET", "POST"])
def tax_settings():
    """
    The café's tax rate, in one place.

    Admin only: the rate decides what every future bill charges, so it is
    not something a cashier should be able to move. Bills already issued
    keep the tax they were raised with - changing the rate is not a way to
    rewrite history.
    """
    denied = require_role("admin")
    if denied:
        return denied

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cafe_id = require_cafe_session()

        if request.method == "POST":
            raw = (request.form.get("tax_percent") or "").strip()
            try:
                rate = Decimal(raw)
            except (InvalidOperation, ValueError):
                flash("Enter the tax rate as a number, for example 5 or 12.5.")
                return redirect(stay_on('tax_settings'))

            if rate < 0 or rate > MAX_TAX_PERCENT:
                flash("The tax rate must be between 0 and 100 percent.")
                return redirect(stay_on('tax_settings'))

            cursor.execute(
                "UPDATE cafes SET tax_percent = %s WHERE cafe_id = %s",
                (rate.quantize(Decimal("0.01")), cafe_id)
            )
            connection.commit()
            g.pop("cafe_tax_percent", None)

            flash("Tax rate saved. New orders will use %s%%."
                  % format_percent(rate))
            return redirect(came_from())

        return render_template(
            "tax_settings.html",
            tax_percent=get_tax_percent(cafe_id),
        )
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/m/<token>")
def public_menu(token):
    """
    The menu a customer sees after scanning the code on their table.

    No sign-in: whoever is holding the phone is a customer, not a user of
    this system. What they can reach is one cafe's menu and nothing else -
    the token names the cafe, and every query below is scoped by it.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cafe = cafe_for_token(cursor, token)
        if cafe is None:
            return render_template("public_gone.html"), 404

        cursor.execute("""
            SELECT f.food_id, f.food_name, f.price, f.description,
                   f.image_version, (f.image_blob IS NOT NULL) AS has_image,
                   COALESCE(c.category_name, 'Other') AS category_name,
                   COALESCE(i.quantity, 0) AS stock
            FROM foods f
            LEFT JOIN categories c ON c.category_id = f.category_id
            LEFT JOIN inventory i ON i.food_id = f.food_id
            WHERE f.user_id = %s
              AND f.availability = 1
              AND COALESCE(i.quantity, 0) > 0
            ORDER BY c.category_name, f.food_name
        """, (cafe["owner_user_id"],))
        foods = cursor.fetchall()

        return render_template(
            "public_menu.html",
            token=token,
            cafe=cafe,
            branding=get_cafe_branding(cafe["cafe_id"]),
            groups=group_foods_by_category(foods),
            food_count=len(foods),
            tax_percent=get_tax_percent(cafe["cafe_id"]),
            tax_rate=float(tax_multiplier(cafe["cafe_id"])),
        )
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/m/<token>/order", methods=["POST"])
def public_place_order(token):
    """
    A customer's order, from their own phone.

    Goes through exactly the same code the counter does, so stock, prices
    and tax cannot drift apart between the two ways of ordering. Marked
    'qr' so the kitchen knows nobody is standing there waiting to carry
    the ticket over.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cafe = cafe_for_token(cursor, token)
        if cafe is None:
            return render_template("public_gone.html"), 404

        wanted = {}
        for field, value in request.form.items():
            if field.startswith("quantity_"):
                wanted[field[len("quantity_"):]] = value

        try:
            foods = available_foods_for(cursor, cafe["owner_user_id"])
            lines = collect_order_items(foods, wanted)
            result = write_order(
                cursor,
                cafe["owner_user_id"],
                cafe["cafe_id"],
                lines,
                tax_multiplier(cafe["cafe_id"]),
                source="qr",
            )
            connection.commit()
        except OrderError as error:
            connection.rollback()
            flash(str(error))
            return redirect(url_for("public_menu", token=token))

        return redirect(url_for("public_order_placed", token=token,
                                order_id=result["order_id"]))
    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        app.logger.exception("public order failed")
        flash("Sorry, that could not be sent to the kitchen. Please try "
              "again, or ask a member of staff.")
        return redirect(url_for("public_menu", token=token))
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/m/<token>/placed/<int:order_id>")
def public_order_placed(token, order_id):
    """
    The number to quote at the counter, and what was ordered.

    The order is looked up by cafe as well as by id, so one cafe's code
    cannot be used to read another's orders by changing the number in the
    address.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cafe = cafe_for_token(cursor, token)
        if cafe is None:
            return render_template("public_gone.html"), 404

        cursor.execute("""
            SELECT order_id, order_date, total_amount, order_status,
                   daily_no
            FROM orders
            WHERE order_id = %s AND user_id = %s AND source = 'qr'
        """, (order_id, cafe["owner_user_id"]))
        order = cursor.fetchone()

        if order is None:
            return render_template("public_gone.html"), 404

        cursor.execute("""
            SELECT item_name, quantity, price, subtotal
            FROM order_items
            WHERE order_id = %s
            ORDER BY order_item_id
        """, (order_id,))
        items = cursor.fetchall()

        return render_template(
            "public_placed.html",
            token=token,
            cafe=cafe,
            branding=get_cafe_branding(cafe["cafe_id"]),
            order=order,
            items=items,
        )
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/m/<token>/status/<int:order_id>")
def public_order_status(token, order_id):
    """
    Whether the kitchen has finished a customer's order yet.

    The page a customer is left holding asks this every few seconds, so
    it answers with the status and nothing else - no totals, no lines, no
    other orders. Looked up by cafe as well as by id, the same way the
    page itself is, so one cafe's code cannot be used to watch another's
    orders by changing the number in the address.
    """
    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cafe = cafe_for_token(cursor, token)
        if cafe is None:
            return jsonify({"status": "gone"}), 404

        cursor.execute(
            "SELECT order_status FROM orders "
            "WHERE order_id = %s AND user_id = %s AND source = 'qr'",
            (order_id, cafe["owner_user_id"])
        )
        order = cursor.fetchone()
        if order is None:
            return jsonify({"status": "gone"}), 404

        return jsonify({"status": order["order_status"]})
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/manifest.webmanifest")
def web_manifest():
    """
    What the browser needs to install this as an app.

    Installed from the browser's "Add to Home Screen" or "Install", the
    site then opens with no address bar and no browser chrome at all, every
    time, on phones and on desktop. That is the only way a web page gets a
    genuinely full screen on open: the Fullscreen API refuses to run
    without a user gesture, so nothing a page does on load can take over
    the screen.

    Named and iconed per café where there is a session, so a till installed
    at the Bluebird says Bluebird on the home screen rather than the
    platform name. The link tag carries crossorigin="use-credentials" for
    that reason - a manifest is fetched without cookies otherwise.
    """
    branding = get_cafe_branding(session.get("cafe_id"))
    name = branding["cafe_name"] or app.config["CAFE_NAME"]

    # A home screen gives a name about twelve characters before it
    # ellipsises, so the short one stops at a word rather than mid-syllable:
    # "Bluebird", not "Bluebird Cof".
    short = name
    if len(short) > 12:
        short = ""
        for word in name.split():
            if not short:
                short = word[:12]
            elif len(short) + 1 + len(word) <= 12:
                short += " " + word
            else:
                break

    icons = [
        {
            "src": url_for("static", filename="icons/app-192.png",
                           v=ASSET_VERSION),
            "sizes": "192x192",
            "type": "image/png",
            "purpose": "any maskable",
        },
        {
            "src": url_for("static", filename="icons/app-512.png",
                           v=ASSET_VERSION),
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "any maskable",
        },
    ]

    manifest = {
        "name": name,
        "short_name": short,
        "description": "Orders, stock and billing for %s." % name,
        "start_url": url_for("home"),
        "scope": "/",
        # fullscreen first, standalone as the fallback: a platform that
        # will not give up its status bar should still drop the address
        # bar rather than refusing to install.
        "display": "fullscreen",
        "display_override": ["fullscreen", "standalone", "minimal-ui"],
        "orientation": "any",
        "background_color": "#170F0B",
        "theme_color": "#170F0B",
        "icons": icons,
    }

    response = app.response_class(
        json.dumps(manifest, indent=2),
        mimetype="application/manifest+json",
    )
    # Per-café, and it follows the signed-in session.
    response.headers["Cache-Control"] = "private, max-age=300"
    return response


@app.route("/settings/theme", methods=["GET", "POST"])
def theme_settings():
    """
    The café's accent colour.

    Admin only, and café-wide. Everyone signed in to the same café works off
    the same screens, so this is a decision about the room rather than a
    personal preference - which is also why a cashier sees the admin's
    choice rather than being able to pick their own.

    Leave it alone and it stays Copper, which is how the app has always
    looked.
    """
    denied = require_role("admin")
    if denied:
        return denied

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cafe_id = require_cafe_session()

        if request.method == "POST":
            chosen = (request.form.get("theme") or "").strip().lower()
            if chosen not in THEME_IDS:
                flash("Pick one of the colours shown.")
                return redirect(stay_on('theme_settings'))

            cursor.execute(
                "UPDATE cafes SET theme = %s WHERE cafe_id = %s",
                (chosen, cafe_id)
            )
            connection.commit()
            g.pop("cafe_theme", None)

            label = dict((t[0], t[1]) for t in THEMES)[chosen]
            flash("Theme saved. Your cafe is now %s." % label)
            return redirect(came_from())

        return render_template("theme_settings.html")
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/settings/printing", methods=["GET", "POST"])
def print_settings():
    """
    Automatic printing: the kitchen ticket after an order, the bill when it
    is marked paid.

    Open to everyone, not just admins. Whoever is on the till is the person
    who notices the tickets are coming out too early or not at all, and they
    should be able to fix it without finding a manager. The settings belong
    to the café, so a change applies to every device in it.
    """
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cafe_id = require_cafe_session()

        if request.method == "POST":
            auto_kot = request.form.get("auto_kot") == "on"
            auto_bill = request.form.get("auto_bill") == "on"

            raw = (request.form.get("kot_delay") or "").strip()
            try:
                delay = int(raw)
            except (TypeError, ValueError):
                flash("Enter the delay as a whole number of seconds.")
                return redirect(stay_on('print_settings'))

            if delay < 0 or delay > MAX_KOT_DELAY:
                flash("The delay must be between 0 and %d seconds."
                      % MAX_KOT_DELAY)
                return redirect(stay_on('print_settings'))

            cursor.execute("""
                UPDATE cafes
                SET auto_kot_enabled = %s,
                    auto_kot_delay = %s,
                    auto_bill_enabled = %s
                WHERE cafe_id = %s
            """, (1 if auto_kot else 0, delay,
                  1 if auto_bill else 0, cafe_id))
            connection.commit()
            g.pop("print_settings", None)

            flash("Printing settings saved.")
            return redirect(came_from())

        return render_template(
            "print_settings.html",
            settings=get_print_settings(),
            max_delay=MAX_KOT_DELAY,
        )
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/account/photo", methods=["GET", "POST"])
def account_photo():
    """
    The signed-in user's own profile photo.

    Everyone gets this, not just admins - it is their own picture. Stored in
    the database beside the food and branding images rather than on disk,
    because the filesystem does not survive a deploy on this host.
    """
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        if request.method == "POST":
            if request.form.get("action") == "remove":
                cursor.execute("""
                    UPDATE users
                    SET photo_blob = NULL, photo_mime = NULL,
                        photo_version = photo_version + 1
                    WHERE user_id = %s
                """, (user["user_id"],))
                connection.commit()
                flash("Profile photo removed.")
                return redirect(came_from())

            upload = request.files.get("photo")
            if not upload or not upload.filename:
                flash("Choose an image first.")
                return redirect(stay_on('account_photo'))

            data = upload.read()
            if not data:
                flash("That file was empty.")
                return redirect(stay_on('account_photo'))

            mime = (upload.mimetype or "").lower()
            if not mime.startswith("image/"):
                flash("Profile photos must be an image file.")
                return redirect(stay_on('account_photo'))

            cursor.execute("""
                UPDATE users
                SET photo_blob = %s, photo_mime = %s,
                    photo_version = photo_version + 1
                WHERE user_id = %s
            """, (data, mime, user["user_id"]))
            connection.commit()
            flash("Profile photo updated.")
            return redirect(came_from())

        cursor.execute(
            "SELECT (photo_blob IS NOT NULL) AS has_photo, photo_version "
            "FROM users WHERE user_id = %s",
            (user["user_id"],)
        )
        row = cursor.fetchone() or {}
        return render_template(
            "account_photo.html",
            has_photo=bool(row.get("has_photo")),
            photo_url=user_photo_url(user["user_id"], row.get("photo_version")),
        )
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


@app.route("/media/user/<int:user_id>")
def user_media(user_id):
    """Serve a staff member's profile photo from the database."""
    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        # Scoped to the viewer's café: a photo is not something another
        # tenant should be able to fetch by guessing user ids.
        cursor.execute("""
            SELECT photo_blob AS data, photo_mime AS mime
            FROM users WHERE user_id = %s AND cafe_id = %s
        """, (user_id, session.get("cafe_id")))
        row = cursor.fetchone()
    finally:
        cursor.close()
        connection.close()

    if not row or not row["data"]:
        abort(404)

    response = send_file(
        io.BytesIO(row["data"]),
        mimetype=row["mime"] or "image/png",
    )
    # Private: it is a person's photo, and the URL carries a version that
    # changes whenever they upload a new one.
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


@app.route("/media/cafe/<int:cafe_id>/<kind>")
def cafe_media(cafe_id, kind):
    """Serve a café's logo or login photo from the database."""
    if kind not in {"logo", "login"}:
        abort(404)

    column = "logo_blob" if kind == "logo" else "login_photo_blob"
    mime_column = "logo_mime" if kind == "logo" else "login_photo_mime"

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            f"SELECT {column} AS data, {mime_column} AS mime "
            "FROM cafes WHERE cafe_id = %s",
            (cafe_id,)
        )
        row = cursor.fetchone()
    finally:
        cursor.close()
        connection.close()

    if not row or not row["data"]:
        abort(404)

    response = send_file(
        io.BytesIO(row["data"]),
        mimetype=row["mime"] or "image/png",
    )
    # Public rather than private: the logo is shown on the sign-in page, and
    # the URL carries a branding_version that changes on every upload.
    response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response


class _LazyBranding:
    """
    Café branding that is fetched only if a template actually asks for it.

    Branding is shown on four signed-out pages - sign-in, registration,
    password reset and the OTP step. The context processor below runs on
    every render, though, so eagerly loading it cost a query against `cafes`
    on every authenticated page view as well: a wasted network round-trip
    per page against a managed database, for a value nothing on the page
    reads.
    """

    __slots__ = ("_cafe_id", "_loaded", "_data")

    def __init__(self, cafe_id):
        self._cafe_id = cafe_id
        self._loaded = False
        self._data = None

    def _resolve(self):
        if not self._loaded:
            self._data = get_cafe_branding(self._cafe_id)
            self._loaded = True
        return self._data

    def __getattr__(self, name):
        # Guard the private slots, or a lookup during __init__ would recurse.
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._resolve()[name]
        except KeyError:
            raise AttributeError(name)

    def __getitem__(self, key):
        return self._resolve()[key]

    def get(self, key, default=None):
        return self._resolve().get(key, default)

    def __contains__(self, key):
        return key in self._resolve()

    def __iter__(self):
        return iter(self._resolve())

    def __repr__(self):
        return repr(self._resolve())


@app.context_processor
def inject_cafe_branding():
    """
    Branding for the current café.

    Sign-in, registration and password-reset pages have no café yet, so they
    fall back to the platform name rather than leaking the branding of
    whichever tenant happened to save last.
    """
    return {"cafe_branding": _LazyBranding(session.get("cafe_id"))}


# ==========================================
# KEEPING THE SITE AWAKE
# ==========================================
#
# The hosting plan stops the service when nothing has asked it for
# anything in a while. The next visitor then waits for Python to start
# and for a fresh connection to a database half a second away - which is
# the pause someone notices when they come back after a quiet afternoon.
#
# So the site asks itself for its health check on a timer. The request
# has to go out and come back through the public address, because it is
# inbound traffic that the host counts, not work happening inside.
#
# This also absorbs the cost rather than moving it: the reconnect after a
# long idle spell is paid by this timer, on nobody's behalf, instead of
# by whoever happens to arrive first.
#
# Two things worth knowing. It only helps while the process is alive - it
# cannot restart a service that has already stopped, so a deploy or a
# crash still leaves the first visitor waiting. And on a free plan it
# uses most of the month's allowance of running hours, because the point
# of it is to never be idle. An external pinger avoids both; there is one
# in .github/workflows/keep-awake.yml.

KEEP_AWAKE_SECONDS = int(os.environ.get("KEEP_AWAKE_SECONDS", "600"))


def keep_awake_target():
    """
    The address to call, or None if the site should not do this.

    Render publishes the service's own address as RENDER_EXTERNAL_URL.
    Anywhere else it has to be given, because a site that guessed at its
    own public name would be pinging somebody else's.
    """
    if not IS_PRODUCTION:
        return None
    if os.environ.get("KEEP_AWAKE", "1").strip().lower() in ("0", "off",
                                                             "false", "no"):
        return None

    base = (os.environ.get("KEEP_AWAKE_URL")
            or os.environ.get("RENDER_EXTERNAL_URL")
            or "").strip().rstrip("/")

    if not base.startswith("https://") and not base.startswith("http://"):
        return None
    return base + "/healthz"


def _keep_awake_loop(target, every):
    import urllib.request

    while True:
        time.sleep(every)
        try:
            request = urllib.request.Request(
                target, headers={"User-Agent": "cafe-manager-keep-awake"})
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
        except Exception as error:                  # noqa: BLE001
            # Never worth taking the site down over. The next tick tries
            # again, and a missed one only costs the wait it was there to
            # avoid.
            app.logger.info("keep-awake ping failed: %s", error)


def start_keep_awake():
    """Start the timer, once, if this deployment should have one."""
    target = keep_awake_target()
    if not target:
        return False

    thread = threading.Thread(
        target=_keep_awake_loop,
        args=(target, max(60, KEEP_AWAKE_SECONDS)),
        name="keep-awake",
        daemon=True,
    )
    thread.start()
    app.logger.info("keep-awake: calling %s every %ds",
                    target, max(60, KEEP_AWAKE_SECONDS))
    return True


start_keep_awake()



# ==========================================
# HEALTH CHECK AND ERROR HANDLING
# ==========================================


@app.route("/healthz")
def healthz():
    """
    Liveness/readiness probe for the host's health check.

    Verifies the database round-trips, so a deploy with bad credentials is
    reported as unhealthy instead of serving 500s to customers.
    """
    try:
        # Timed, because "the site is slow" is almost always a question
        # about how far away the database is, and guessing at that from
        # the outside means measuring this machine's distance to the app
        # as well. These two numbers separate the app from the database.
        started = time.perf_counter()
        connection = get_db_connection()
        acquired = time.perf_counter()

        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        queried = time.perf_counter()

        connection.close()

        return jsonify({
            "status": "ok",
            "database": "ok",
            "connect_ms": round((acquired - started) * 1000, 1),
            "query_ms": round((queried - acquired) * 1000, 1),
        }), 200
    except Exception as error:
        app.logger.exception("health check failed")
        return jsonify({"status": "error", "database": str(error)}), 503


@app.errorhandler(TenantSessionError)
def handle_tenant_session_error(error):
    """
    A signed-in session with no usable café.

    This used to be a bare RuntimeError raised from a before_request hook and
    from a template context processor, which Flask turned into a permanent
    500 with no way for the user to sign out and recover.
    """
    session.clear()
    flash("Your session has expired. Please sign in again.")
    return redirect(url_for("login"))


@app.errorhandler(413)
def handle_too_large(error):
    flash(
        "That file is too large. Please upload an image under "
        f"{app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB."
    )
    return redirect(request.referrer or url_for("home")), 302


@app.route("/favicon.ico")
def favicon():
    """
    Browsers ask for this unprompted on every visit.

    Answering "no content" keeps it out of the 404 handler below, which
    would otherwise queue a "page could not be found" message for a request
    the user never made.
    """
    return "", 204


@app.errorhandler(404)
def handle_not_found(error):
    if wants_json_response() or request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404

    # Media is fetched by <img>, not navigated to. Redirecting it to the
    # dashboard would hand an image tag a page of HTML and hide the real
    # answer, which is simply that there is no such picture.
    if request.path.startswith("/media/"):
        return "Not found", 404

    # Only a real page navigation earns a message. A missing subresource -
    # an icon, a stylesheet, a background warm-up - must not leave a banner
    # sitting in the queue for whatever page the user opens next.
    if _is_page_request():
        flash("That page could not be found.")

    return redirect(url_for("home") if session.get("user_id")
                    else url_for("login")), 302


@app.errorhandler(500)
@app.errorhandler(Exception)
def handle_unexpected_error(error):
    """
    Catch-all so an unexpected exception never shows a stack trace.

    Werkzeug HTTP exceptions keep their own status codes and messages.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException):
        return error

    app.logger.exception("Unhandled application error")

    if wants_json_response() or request.path.startswith("/api/"):
        return jsonify({"error": "Something went wrong."}), 500

    flash("Something went wrong. The error has been logged.")
    target = url_for("home") if session.get("user_id") else url_for("login")
    return redirect(target), 302


if __name__ == "__main__":
    # Local development only. Production runs through gunicorn (see Procfile),
    # where debug mode is never enabled.
    app.run(
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5000")),
        debug=not IS_PRODUCTION,
    )
