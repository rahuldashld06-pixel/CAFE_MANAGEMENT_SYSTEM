from functools import wraps
import json
import logging
import secrets
import hashlib
import hmac
import time
from datetime import datetime, timedelta
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
)

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


_CONNECTION_POOL = None


def _build_pool():
    global _CONNECTION_POOL
    if _CONNECTION_POOL is None:
        _CONNECTION_POOL = mysql.connector.pooling.MySQLConnectionPool(
            pool_name="cafe_pool",
            pool_size=int(os.environ.get("DB_POOL_SIZE", "5")),
            pool_reset_session=True,
            **DB_CONFIG,
        )
    return _CONNECTION_POOL


def _new_raw_connection():
    """A pooled connection when possible, otherwise a direct one."""
    try:
        connection = _build_pool().get_connection()
    except Exception:
        # Pool exhausted or unavailable (e.g. during start-up migrations
        # outside a request) - fall back to a direct connection.
        connection = mysql.connector.connect(**DB_CONFIG)
    connection.autocommit = False
    return connection


def get_db_connection():
    """
    Return this request's database connection.

    Outside a request context (start-up migrations, scripts) a real,
    independently closeable connection is returned instead.
    """
    if not has_request_context():
        return _new_raw_connection()

    connection = getattr(g, "_db_connection", None)

    if connection is not None:
        try:
            connection.ping(reconnect=True, attempts=2, delay=1)
        except Exception:
            try:
                connection.close()
            except Exception:
                pass
            connection = None

    if connection is None:
        connection = _new_raw_connection()
        g._db_connection = connection

    return _SharedConnection(connection)


@app.teardown_appcontext
def _close_db_connection(exception=None):
    connection = getattr(g, "_db_connection", None)
    if connection is None:
        return
    try:
        if exception is not None:
            connection.rollback()
    except Exception:
        pass
    try:
        connection.close()
    except Exception:
        pass


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
            user_id INT NULL,
            cafe_id INT NULL,
            INDEX idx_orders_user_id (user_id),
            INDEX idx_orders_cafe_id (cafe_id),
            INDEX idx_orders_order_date (order_date)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """),
    ("order_items", """
        CREATE TABLE IF NOT EXISTS order_items (
            order_item_id INT AUTO_INCREMENT PRIMARY KEY,
            order_id INT NOT NULL,
            food_id INT NULL,
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
    ("orders", "user_id", "INT NULL"),
    ("orders", "cafe_id", "INT NULL"),
    ("cafes", "logo_mime", "VARCHAR(80) NULL"),
    ("cafes", "logo_blob", "MEDIUMBLOB NULL"),
    ("cafes", "login_photo_mime", "VARCHAR(80) NULL"),
    ("cafes", "login_photo_blob", "MEDIUMBLOB NULL"),
    ("cafes", "branding_version", "INT NOT NULL DEFAULT 1"),
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

        connection.commit()
        AUTH_SCHEMA_READY = True
    except Exception:
        connection.rollback()
        raise
    finally:
        cursor.close()
        connection.close()


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
        cursor.execute("""
            SELECT user_id, username, full_name, role, is_active
            FROM users
            WHERE user_id = %s AND cafe_id = %s
        """, (user_id, cafe_id))
        user = cursor.fetchone()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

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
# only: New Order, Food Management, Inventory, and Billing (history).
# order_details is included because creating a new order redirects there
# to show the receipt/summary of the order just placed.
# Applies to every role except 'admin', so 'staff' automatically gets the
# same access as manager/cashier with no extra wiring.
STAFF_ALLOWED_ENDPOINTS = {
    "add_order", "order_details",
    "orders", "cancel_order", "complete_order", "delete_order",
    "foods", "add_food", "edit_food", "delete_food",
    "inventory", "update_stock",
    "billing", "mark_bill_paid", "start_online_payment",
    "verify_online_payment", "edit_bill",
    "order_status_feed",
    "change_password", "logout",
}


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

        cursor.execute("SELECT COUNT(*) AS total FROM foods WHERE user_id = %s", (scope_user_id(),))
        total_foods = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(DISTINCT category_id) AS total
            FROM foods
            WHERE user_id = %s
        """, (scope_user_id(),))
        total_categories = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM orders WHERE user_id = %s", (scope_user_id(),))
        total_orders = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM inventory i
            INNER JOIN foods f ON i.food_id = f.food_id
            WHERE f.user_id = %s
        """, (scope_user_id(),))
        total_inventory = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) AS total FROM orders
            WHERE DATE(order_date) = CURDATE()
            AND user_id = %s
            AND LOWER(COALESCE(order_status, '')) != 'cancelled'
        """, (scope_user_id(),))
        today_orders = cursor.fetchone()["total"]

        # Revenue is admin-only data (same rule as the Billing page).
        today_revenue = None
        if session.get("role") == "admin":
            cursor.execute("""
                SELECT COALESCE(SUM(b.total_amount), 0) AS total
                FROM bills b
                INNER JOIN orders o ON b.order_id = o.order_id
                WHERE DATE(b.bill_date) = CURDATE()
                AND o.user_id = %s
                AND LOWER(COALESCE(o.order_status, '')) != 'cancelled'
                AND LOWER(COALESCE(b.payment_status, '')) = 'paid'
            """, (scope_user_id(),))
            today_revenue = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM inventory i INNER JOIN foods f ON i.food_id=f.food_id
            WHERE f.user_id=%s AND i.quantity > 0 AND i.quantity <= i.minimum_stock
        """, (scope_user_id(),))
        low_stock = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM inventory i INNER JOIN foods f ON i.food_id=f.food_id
            WHERE f.user_id=%s AND i.quantity = 0
        """, (scope_user_id(),))
        unavailable = cursor.fetchone()["total"]

        return render_template(
            "dashboard.html",
            total_foods=total_foods,
            total_categories=total_categories,
            total_orders=total_orders,
            total_inventory=total_inventory,
            today_orders=today_orders,
            today_revenue=today_revenue,
            low_stock=low_stock,
            unavailable=unavailable
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

            ORDER BY f.food_id DESC
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


            # Insert food
            cursor.execute("""
                INSERT INTO foods
                (
                    category_id,
                    food_name,
                    description,
                    price,
                    availability,
                    user_id,
                    cafe_id
                )

                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
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

            JOIN categories c
                ON f.category_id = c.category_id

            WHERE f.user_id = %s

            ORDER BY i.inventory_id ASC
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

@app.route("/orders")
def orders():

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
        """, (scope_user_id(),))

        orders = cursor.fetchall()

    except mysql.connector.Error as error:
        flash(f"Database error: {error}")
        return redirect(url_for("home"))

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()

    return render_template(
        "orders.html",
        orders=orders
    )


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
                "order_date": row["order_date"].strftime("%d %b, %I:%M %p") if row["order_date"] else "",
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

            return render_template(
                "add_order.html",
                foods=foods
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
        # ==================================================

        selected_items = []


        # Get ALL food records that are available
        cursor.execute("""
            SELECT
                f.food_id,
                f.food_name,
                f.price,
                f.availability,
                COALESCE(i.quantity, 0) AS stock

            FROM foods f

            LEFT JOIN inventory i
                ON f.food_id = i.food_id

            WHERE f.availability = 1
              AND f.user_id = %s
        """, (scope_user_id(),))

        available_foods = cursor.fetchall()


        # ==================================================
        # READ QUANTITIES
        # ==================================================

        for food in available_foods:

            field_name = f"quantity_{food['food_id']}"

            quantity_text = request.form.get(
                field_name,
                "0"
            ).strip()


            if quantity_text == "":
                quantity_text = "0"


            try:
                quantity = int(quantity_text)

            except ValueError:

                return order_result(
                    False,
                    f"Invalid quantity for {food['food_name']}.",
                    status_code=400
                )


            # Ignore zero
            if quantity == 0:
                continue


            # Reject negative values
            if quantity < 0:

                return order_result(
                    False,
                    f"Quantity cannot be negative for {food['food_name']}.",
                    status_code=400
                )


            # Check stock
            if quantity > food["stock"]:

                return order_result(
                    False,
                    f"Not enough stock for {food['food_name']}. "
                    f"Available stock: {food['stock']}.",
                    status_code=400
                )


            price = food["price"]

            item_subtotal = price * quantity


            selected_items.append({
                "food_id": food["food_id"],
                "food_name": food["food_name"],
                "quantity": quantity,
                "price": price,
                "subtotal": item_subtotal
            })


        # ==================================================
        # AT LEAST ONE FOOD REQUIRED
        # ==================================================

        if not selected_items:

            return order_result(
                False,
                "Please select at least one food item.",
                status_code=400
            )


        # ==================================================
        # CALCULATE TOTAL
        # ==================================================

        subtotal = sum(
            (
                item["subtotal"]
                for item in selected_items
            ),
            Decimal("0.00")
        )


        tax = subtotal * Decimal("0.05")

        discount = Decimal("0.00")

        total_amount = (
            subtotal
            + tax
            - discount
        )


        # ==================================================
        # CREATE ORDER
        # ==================================================

        cursor.execute("""
            INSERT INTO orders
            (
                total_amount,
                order_status,
                user_id
            )

            VALUES
            (
                %s,
                %s,
                %s
            )
        """, (
            total_amount,
            "Pending",
            scope_user_id()
        ))


        order_id = cursor.lastrowid


        # ==================================================
        # CREATE ORDER ITEMS + REDUCE INVENTORY
        # ==================================================

        for item in selected_items:

            # ----------------------------------------------
            # Re-check inventory immediately before update
            # ----------------------------------------------

            cursor.execute("""
                SELECT i.quantity
                FROM inventory i
                INNER JOIN foods f ON i.food_id = f.food_id
                WHERE i.food_id = %s
                  AND f.user_id = %s
                FOR UPDATE
            """, (
                item["food_id"],
                scope_user_id()
            ))

            stock_record = cursor.fetchone()


            if stock_record is None:

                raise Exception(
                    f"Inventory record not found for "
                    f"{item['food_name']}."
                )


            current_stock = stock_record["quantity"]


            if current_stock < item["quantity"]:

                raise Exception(
                    f"Not enough stock for "
                    f"{item['food_name']}. "
                    f"Available: {current_stock}."
                )


            # ----------------------------------------------
            # Insert order item
            # ----------------------------------------------

            cursor.execute("""
                INSERT INTO order_items
                (
                    order_id,
                    food_id,
                    quantity,
                    price,
                    subtotal
                )

                VALUES
                (
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
            """, (
                order_id,
                item["food_id"],
                item["quantity"],
                item["price"],
                item["subtotal"]
            ))


            # ----------------------------------------------
            # Reduce inventory
            # ----------------------------------------------

            cursor.execute("""
                UPDATE inventory

                SET
                    quantity = quantity - %s,
                    last_updated = CURRENT_TIMESTAMP

                WHERE food_id = %s
                  AND quantity >= %s
                  AND EXISTS (
                      SELECT 1 FROM foods f
                      WHERE f.food_id = inventory.food_id
                        AND f.user_id = %s
                  )
            """, (
                item["quantity"],
                item["food_id"],
                item["quantity"],
                scope_user_id()
            ))


            if cursor.rowcount != 1:

                raise Exception(
                    f"Could not update inventory for "
                    f"{item['food_name']}."
                )


            # Automatically mark food unavailable when stock reaches 0.
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
            url_for("orders")
        )


    except Exception as error:

        if connection:
            connection.rollback()

        message = f"Order could not be created: {error}"

        if wants_json_response():
            return jsonify({"success": False, "message": message}), 500

        flash(message)

        return redirect(
            url_for("orders")
        )


    finally:

        if cursor:
            cursor.close()
        if connection:
            connection.close()

# ==========================================
# ORDER DETAILS
# ==========================================

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
                url_for("orders")
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
                f.food_name

            FROM order_items oi

            INNER JOIN foods f
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
            url_for("orders")
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
            flash("Order not found.")
            return redirect(url_for("orders"))

        # Do not restore stock twice.
        if order["order_status"] == "Cancelled":
            flash(f"Order #{order_id} is already cancelled.")
            return redirect(url_for("orders"))

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

        flash(
            f"Order #{order_id} cancelled successfully. "
            "The bill and order history have been preserved."
        )

        return redirect(url_for("orders"))

    except mysql.connector.Error as error:

        if connection:
            connection.rollback()

        flash(f"Database error: {error}")
        return redirect(url_for("orders"))

    except Exception as error:

        if connection:
            connection.rollback()

        flash(f"Order could not be cancelled: {error}")
        return redirect(url_for("orders"))

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
            flash("Order not found.")
            return redirect(url_for("orders"))

        if order["order_status"] == "Cancelled":
            flash(f"Order #{order_id} is cancelled and cannot be marked done.")
            return redirect(url_for("orders"))

        if order["order_status"] == "Completed":
            flash(f"Order #{order_id} is already marked as done.")
            return redirect(url_for("orders"))

        cursor.execute("""
            UPDATE orders
            SET order_status = 'Completed'
            WHERE order_id = %s
              AND user_id = %s
        """, (order_id, scope_user_id()))

        connection.commit()

        flash(f"Order #{order_id} marked as done.")
        return redirect(url_for("orders"))

    except mysql.connector.Error as error:

        if connection:
            connection.rollback()

        flash(f"Database error: {error}")
        return redirect(url_for("orders"))

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
            return jsonify(payload), status_code
        flash(message)
        return redirect(url_for("billing"))

    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT b.bill_id, b.payment_status, b.total_amount, o.order_status
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


def _mark_gateway_payment_paid(bill_id, order_id, payment_id, signature=None):
    """Mark a bill paid only after a verified gateway event."""
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

        cursor.execute("""
            UPDATE bills
            SET payment_method = 'Online',
                payment_status = 'Paid',
                gateway_payment_id = %s,
                payment_reference = %s,
                gateway_signature = COALESCE(%s, gateway_signature)
            WHERE bill_id = %s
              AND gateway_order_id = %s
        """, (payment_id, payment_id, signature, bill_id, order_id))

        connection.commit()
        return True, "Payment verified successfully."
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

        ok, message = _mark_gateway_payment_paid(
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

        cursor.execute("""
            UPDATE bills
            SET payment_method = 'Online',
                payment_status = 'Paid',
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
            subtotal = row["subtotal"] or 0
            tax = subtotal * Decimal("0.05")
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
                    f.food_name
                FROM order_items oi
                INNER JOIN foods f
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
        # Filtered-period summary
        # --------------------------------------

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

            {where_sql}
        """, tuple(params))

        summary = cursor.fetchone()

        return render_template(
            "billing.html",
            bills=bills,
            bills_today=summary["bills_count"] or 0,
            paid_today=summary["paid_count"] or 0,
            pending_today=summary["pending_count"] or 0,
            cancelled_count=summary["cancelled_count"] or 0,
            revenue_today=summary["revenue"] or Decimal("0.00"),
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

                f.food_name,

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

            INNER JOIN foods f
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

            GROUP BY
                f.food_id,
                f.food_name

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

        cursor.execute("SELECT COUNT(*) AS total FROM foods WHERE user_id=%s", (uid,))
        total_foods = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(DISTINCT category_id) AS total
            FROM foods
            WHERE user_id = %s
        """, (uid,))
        total_categories = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM orders WHERE user_id=%s", (uid,))
        total_orders = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM inventory i INNER JOIN foods f ON i.food_id=f.food_id
            WHERE f.user_id=%s
        """, (uid,))
        total_inventory = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) AS total FROM orders
            WHERE user_id=%s
              AND DATE(order_date)=CURDATE()
              AND LOWER(COALESCE(order_status,''))!='cancelled'
        """, (uid,))
        today_orders = cursor.fetchone()["total"]

        # Revenue is admin-only data (same rule as the Billing page),
        # so it's only computed and sent back for admins.
        today_revenue = None
        if session.get("role") == "admin":
            cursor.execute("""
                SELECT COALESCE(SUM(b.total_amount),0) AS total
                FROM bills b
                INNER JOIN orders o ON b.order_id=o.order_id
                WHERE o.user_id=%s
                  AND DATE(b.bill_date)=CURDATE()
                  AND LOWER(COALESCE(o.order_status,''))!='cancelled'
                  AND LOWER(COALESCE(b.payment_status,''))='paid'
            """, (uid,))
            today_revenue = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM inventory i INNER JOIN foods f ON i.food_id=f.food_id
            WHERE f.user_id=%s
              AND i.quantity>0
              AND i.quantity<=i.minimum_stock
        """, (uid,))
        low_stock = cursor.fetchone()["total"]

        cursor.execute("""
            SELECT COUNT(*) AS total
            FROM inventory i INNER JOIN foods f ON i.food_id=f.food_id
            WHERE f.user_id=%s AND i.quantity=0
        """, (uid,))
        unavailable = cursor.fetchone()["total"]

        return {
            "total_foods": total_foods,
            "total_categories": total_categories,
            "total_orders": total_orders,
            "total_inventory": total_inventory,
            "today_orders": today_orders,
            "today_revenue": (float(today_revenue) if today_revenue is not None else None),
            "low_stock": low_stock,
            "unavailable": unavailable
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
            return redirect(url_for("change_password"))

        if new_password != confirm:
            flash("New passwords do not match.")
            return redirect(url_for("change_password"))

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
                return redirect(url_for("change_password"))

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
        return render_template("users.html", users=user_list)
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


# ==========================================
# CAFE BRANDING / CUSTOMIZATION
# ==========================================
#
# Branding used to be a single cafe_branding.json file at the project root
# with fixed image filenames (cafe_logo.png / cafe_login.png). On a
# multi-tenant deployment that meant every café overwrote every other café's
# name and logo, and on Render the file and images were wiped on each deploy.
# Branding now lives on the cafes row for that tenant, images included.


def get_cafe_branding(cafe_id):
    """Branding for one café, or the platform defaults when unknown."""
    defaults = {
        "cafe_name": app.config["CAFE_NAME"],
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
        "logo": (
            url_for("cafe_media", cafe_id=cafe_id, kind="logo", v=version)
            if row["has_logo"] else ""
        ),
        "login_photo": (
            url_for("cafe_media", cafe_id=cafe_id, kind="login", v=version)
            if row["has_login_photo"] else ""
        ),
    }


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


@app.route("/settings/branding", methods=["GET", "POST"])
def branding():
    denied = require_role("admin")
    if denied:
        return denied

    cafe_id = require_cafe_session()

    if request.method == "POST":
        connection = None
        cursor = None
        try:
            cafe_name = form_text("cafe_name", "Café name", max_length=150)

            logo_data, logo_mime = read_image_upload(
                request.files.get("logo")
            )
            photo_data, photo_mime = read_image_upload(
                request.files.get("login_photo")
            )

            connection = get_db_connection()
            cursor = connection.cursor(dictionary=True)

            updates = ["cafe_name = %s", "branding_version = branding_version + 1"]
            params = [cafe_name]

            if logo_data is not None:
                updates += ["logo_blob = %s", "logo_mime = %s"]
                params += [logo_data, logo_mime]

            if photo_data is not None:
                updates += ["login_photo_blob = %s", "login_photo_mime = %s"]
                params += [photo_data, photo_mime]

            params.append(cafe_id)
            cursor.execute(
                f"UPDATE cafes SET {', '.join(updates)} WHERE cafe_id = %s",
                tuple(params)
            )
            connection.commit()

            flash("Café branding updated successfully.")
            return redirect(url_for("branding"))

        except (ValidationError, ValueError) as error:
            if connection:
                connection.rollback()
            flash(str(error))
            return redirect(url_for("branding"))
        except mysql.connector.Error as error:
            if connection:
                connection.rollback()
            app.logger.exception("branding update failed")
            flash(f"Could not update branding: {error.msg}")
            return redirect(url_for("branding"))
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("branding.html", branding=get_cafe_branding(cafe_id))


@app.context_processor
def inject_cafe_branding():
    """
    Branding for the current café.

    Sign-in, registration and password-reset pages have no café yet, so they
    fall back to the platform name rather than leaking the branding of
    whichever tenant happened to save last.
    """
    return {"cafe_branding": get_cafe_branding(session.get("cafe_id"))}


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
        connection = get_db_connection()
        cursor = connection.cursor()
        cursor.execute("SELECT 1")
        cursor.fetchone()
        cursor.close()
        connection.close()
        return jsonify({"status": "ok", "database": "ok"}), 200
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


@app.errorhandler(404)
def handle_not_found(error):
    if wants_json_response() or request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
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
