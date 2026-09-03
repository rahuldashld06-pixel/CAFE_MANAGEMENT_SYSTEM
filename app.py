from functools import wraps
import json
import secrets
import hmac
import time
from datetime import datetime
from config import DB_CONFIG, RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET, RAZORPAY_WEBHOOK_SECRET
from flask import Flask, render_template, request, redirect, url_for, flash, session, g, jsonify
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import os
import mysql.connector
from decimal import Decimal
try:
    import razorpay
except ImportError:
    razorpay = None

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

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", _FILE_RAZORPAY_KEY_ID)
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", _FILE_RAZORPAY_KEY_SECRET)
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", _FILE_RAZORPAY_WEBHOOK_SECRET)
# -----------------------------------------------------------------------

app = Flask(__name__)
app.config["CAFE_NAME"] = os.environ.get("CAFE_NAME", "Coffeehouse")
app.config["CAFE_LOGO"] = os.environ.get("CAFE_LOGO", "")

app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=__import__("datetime").timedelta(hours=8),
)


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


def get_db_connection():
    connection = mysql.connector.connect(**DB_CONFIG, use_pure=True)
    return connection


PAYMENT_SCHEMA_READY = False


def ensure_payment_schema():
    """Create gateway fields needed for online payments, if they are missing."""
    global PAYMENT_SCHEMA_READY
    if PAYMENT_SCHEMA_READY:
        return

    connection = get_db_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'bills'
              AND COLUMN_NAME = 'gateway_order_id'
        """)
        if cursor.fetchone()[0] == 0:
            cursor.execute("ALTER TABLE bills ADD COLUMN gateway_order_id VARCHAR(100) NULL")
            cursor.execute("CREATE INDEX idx_bills_gateway_order_id ON bills (gateway_order_id)")

        for column, definition in [
            ("gateway_payment_id", "VARCHAR(100) NULL"),
            ("payment_reference", "VARCHAR(150) NULL"),
            ("gateway_signature", "VARCHAR(255) NULL"),
        ]:
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = 'bills'
                  AND COLUMN_NAME = %s
            """, (column,))
            if cursor.fetchone()[0] == 0:
                cursor.execute(f"ALTER TABLE bills ADD COLUMN {column} {definition}")

        connection.commit()
        PAYMENT_SCHEMA_READY = True
    finally:
        cursor.close()
        connection.close()


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


def allowed_image(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower()
        in ALLOWED_IMAGE_EXTENSIONS
    )


def get_food_image(food_id):
    upload_dir = os.path.join(
        app.root_path, "static", "uploads", "foods"
    )

    if not os.path.isdir(upload_dir):
        return None

    for extension in ALLOWED_IMAGE_EXTENSIONS:
        filename = f"{food_id}.{extension}"
        if os.path.exists(os.path.join(upload_dir, filename)):
            return f"/static/uploads/foods/{filename}"

    return None


def save_food_image(file, food_id):
    if not file or not file.filename:
        return None

    if not allowed_image(file.filename):
        raise ValueError(
            "Invalid image format. Use JPG, JPEG, PNG, WEBP or GIF."
        )

    upload_dir = os.path.join(
        app.root_path, "static", "uploads", "foods"
    )
    os.makedirs(upload_dir, exist_ok=True)

    for extension in ALLOWED_IMAGE_EXTENSIONS:
        old_file = os.path.join(
            upload_dir, f"{food_id}.{extension}"
        )
        if os.path.exists(old_file):
            os.remove(old_file)

    safe_name = secure_filename(file.filename)
    extension = safe_name.rsplit(".", 1)[1].lower()
    filename = f"{food_id}.{extension}"

    file.save(os.path.join(upload_dir, filename))

    return f"/static/uploads/foods/{filename}"






# ==========================================
# MULTI-USER AUTHENTICATION / DATA ISOLATION
# ==========================================

AUTH_SCHEMA_READY = False

def ensure_auth_schema():
    """Create the users table and add ownership columns to existing data."""
    global AUTH_SCHEMA_READY
    if AUTH_SCHEMA_READY:
        return

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INT AUTO_INCREMENT PRIMARY KEY,
                username VARCHAR(80) NOT NULL UNIQUE,
                password_hash VARCHAR(255) NOT NULL,
                full_name VARCHAR(120) NOT NULL,
                role ENUM('admin','manager','cashier','staff') NOT NULL DEFAULT 'cashier',
                is_active TINYINT(1) NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Widen the role ENUM for installs created before 'staff' existed.
        cursor.execute("""
            SELECT COLUMN_TYPE AS col_type
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users'
              AND COLUMN_NAME = 'role'
        """)
        role_column = cursor.fetchone()
        if role_column and "'staff'" not in role_column["col_type"]:
            cursor.execute("""
                ALTER TABLE users
                MODIFY COLUMN role
                ENUM('admin','manager','cashier','staff')
                NOT NULL DEFAULT 'cashier'
            """)

        # Add ownership to the two root business tables if missing.
        for table in ("foods", "orders"):
            cursor.execute("""
                SELECT COUNT(*) AS n
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = DATABASE()
                  AND TABLE_NAME = %s
                  AND COLUMN_NAME = 'user_id'
            """, (table,))
            if cursor.fetchone()["n"] == 0:
                cursor.execute(
                    f"ALTER TABLE `{table}` ADD COLUMN user_id INT NULL"
                )
                cursor.execute(
                    f"CREATE INDEX idx_{table}_user_id ON `{table}` (user_id)"
                )

        # Mobile number used to send the admin login OTP.
        cursor.execute("""
            SELECT COUNT(*) AS n
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND TABLE_NAME = 'users'
              AND COLUMN_NAME = 'phone_number'
        """)
        if cursor.fetchone()["n"] == 0:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN phone_number VARCHAR(20) NULL"
            )

        # One-time codes issued for admin login verification.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS login_otp_codes (
                otp_id INT AUTO_INCREMENT PRIMARY KEY,
                user_id INT NOT NULL,
                code_hash VARCHAR(255) NOT NULL,
                expires_at DATETIME NOT NULL,
                attempts INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_login_otp_user
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                    ON DELETE CASCADE
            )
        """)

        # Create the first administrator from environment variables.
        admin_username = os.environ.get("ADMIN_USERNAME", "admin").strip()
        admin_password = os.environ.get("ADMIN_PASSWORD", "admin123")
        admin_name = os.environ.get("ADMIN_FULL_NAME", "Cafe Administrator")
        admin_phone = os.environ.get("ADMIN_PHONE_NUMBER", "").strip()

        cursor.execute(
            "SELECT user_id FROM users WHERE username = %s",
            (admin_username,)
        )
        admin = cursor.fetchone()

        if admin is None:
            cursor.execute("""
                INSERT INTO users
                    (username, password_hash, full_name, role, is_active, phone_number)
                VALUES (%s, %s, %s, 'admin', 1, %s)
            """, (
                admin_username,
                generate_password_hash(admin_password),
                admin_name,
                admin_phone or None
            ))
            admin_id = cursor.lastrowid
        else:
            admin_id = admin["user_id"]

            # Back-fill the phone number for the bootstrap admin if one was
            # provided later and the account doesn't have one on file yet.
            if admin_phone:
                cursor.execute("""
                    UPDATE users
                    SET phone_number = %s
                    WHERE user_id = %s
                      AND (phone_number IS NULL OR phone_number = '')
                """, (admin_phone, admin_id))

        # Existing records from before multi-user authentication belong
        # to the first administrator.
        cursor.execute(
            "UPDATE foods SET user_id = %s WHERE user_id IS NULL",
            (admin_id,)
        )
        cursor.execute(
            "UPDATE orders SET user_id = %s WHERE user_id IS NULL",
            (admin_id,)
        )

        connection.commit()
        AUTH_SCHEMA_READY = True
    finally:
        cursor.close()
        connection.close()


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
    user_id = session.get("user_id")
    if not user_id:
        return None

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT user_id, username, full_name, role, is_active
            FROM users
            WHERE user_id = %s
        """, (user_id,))
        return cursor.fetchone()
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


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
    The user_id of the cafe's primary admin account (the earliest
    created admin). All shared business data (foods, inventory,
    orders, bills) is scoped to this one account so that every
    staff login sees/writes the exact same data the admin does.
    Cached on flask.g so it's only looked up once per request.
    """
    if "cafe_owner_id" in g:
        return g.cafe_owner_id

    connection = None
    cursor = None
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)
        cursor.execute("""
            SELECT user_id FROM users
            WHERE role = 'admin'
            ORDER BY user_id ASC
            LIMIT 1
        """)
        row = cursor.fetchone()
        g.cafe_owner_id = row["user_id"] if row else session.get("user_id")
        return g.cafe_owner_id
    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


def scope_user_id():
    """
    The user_id to use for every business-data read/write in the
    current request. Admins use their own account. Non-admin staff
    (manager/cashier/staff) share the cafe owner's (admin's) data
    instead of having their own separate/isolated foods, inventory,
    orders, and bills -- so staff and admin always see the exact
    same data.
    """
    if session.get("role") == "admin":
        return session.get("user_id")
    return get_cafe_owner_id()


@app.route("/")
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
                COALESCE(i.quantity, 0) AS quantity

            FROM foods f

            JOIN categories c
                ON f.category_id = c.category_id

            LEFT JOIN inventory i
                ON f.food_id = i.food_id

            WHERE f.user_id = %s

            ORDER BY f.food_id DESC
        """, (scope_user_id(),))

        
        food_list = cursor.fetchall()

        for food in food_list:
            food["image_path"] = get_food_image(food["food_id"])

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
            ORDER BY category_name
        """)

        categories = cursor.fetchall()


        # If form submitted
        if request.method == "POST":

            food_name = request.form["food_name"]
            category_id = request.form["category_id"]
            description = request.form["description"]
            price = request.form["price"]

            quantity = int(request.form["quantity"])
            minimum_stock = int(request.form["minimum_stock"])
            food_image = request.files.get("food_image")

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
                    user_id
                )

                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                category_id,
                food_name,
                description,
                price,
                availability,
                scope_user_id()
            ))


            # Get newly created food ID
            food_id = cursor.lastrowid

            if food_image and food_image.filename:
                save_food_image(food_image, food_id)


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

    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        flash(f"Database error: {error}")
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
            ORDER BY category_name
        """)

        categories = cursor.fetchall()


        if request.method == "POST":

            food_name = request.form["food_name"]
            category_id = request.form["category_id"]
            description = request.form["description"]
            price = request.form["price"]

            quantity = int(request.form["quantity"])
            minimum_stock = int(request.form["minimum_stock"])
            food_image = request.files.get("food_image")

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


            if food_image and food_image.filename:
                save_food_image(food_image, food_id)


            # Update inventory
            cursor.execute("""
                UPDATE inventory

                SET
                    quantity = %s,
                    minimum_stock = %s

                WHERE food_id = %s
                  AND EXISTS (
                      SELECT 1 FROM foods
                      WHERE foods.food_id = inventory.food_id
                        AND foods.user_id = %s
                  )
            """, (
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
                COALESCE(i.minimum_stock, 5) AS minimum_stock

            FROM foods f

            LEFT JOIN inventory i
                ON f.food_id = i.food_id

            WHERE f.food_id = %s
              AND f.user_id = %s
        """, (food_id, scope_user_id()))


        food = cursor.fetchone()

        if food:
            food["image_path"] = get_food_image(food_id)

    except mysql.connector.Error as error:
        if connection:
            connection.rollback()
        flash(f"Database error: {error}")
        return redirect(url_for("foods"))

    finally:
        if cursor:
            cursor.close()
        if connection:
            connection.close()


    if food is None:

        return "Food item not found", 404


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

        quantity = request.form["quantity"]
        minimum_stock = request.form["minimum_stock"]

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
                    i.quantity

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
                food["image_path"] = get_food_image(food["food_id"])

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
                       full_name, role, is_active, phone_number
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
            SELECT user_id, username, full_name, role, is_active, phone_number
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

            cursor.execute("""
                SELECT otp_id, code_hash, expires_at, attempts
                FROM login_otp_codes
                WHERE user_id = %s
                ORDER BY otp_id DESC
                LIMIT 1
            """, (pending_user_id,))
            otp_row = cursor.fetchone()

            if not otp_row:
                flash("Your code has expired. Request a new one.")
                return redirect(url_for("login_verify_otp"))

            if otp_row["expires_at"] < datetime.now():
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


@app.route("/register", methods=["GET", "POST"])
def register():
    """
    Public self-service account creation, reached from the login page.

    Each staff member sets their own password here instead of an admin
    picking one for them in User Management. New accounts can only be
    created as cashier, staff, or manager — 'admin' is deliberately left
    out of this public form so anyone who finds the link can't hand
    themselves full access; admin accounts are still created from
    User Management by an existing admin.
    """

    try:
        ensure_auth_schema()
    except mysql.connector.Error as error:
        return f"Authentication database setup error: {error}", 500

    if session.get("user_id"):
        return redirect(url_for("home"))

    SELF_SIGNUP_ROLES = {"cashier", "staff", "manager"}

    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        username = request.form.get("username", "").strip()
        phone_number = request.form.get("phone_number", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        role = request.form.get("role", "cashier")

        if not full_name or not username:
            flash("Please enter your full name and a username.")
            return redirect(url_for("register"))

        if role not in SELF_SIGNUP_ROLES:
            flash("Please choose a valid role.")
            return redirect(url_for("register"))

        if len(password) < 8:
            flash("Password must be at least 8 characters.")
            return redirect(url_for("register"))

        if password != confirm_password:
            flash("Password and confirm password do not match.")
            return redirect(url_for("register"))

        connection = None
        cursor = None
        try:
            connection = get_db_connection()
            cursor = connection.cursor()
            cursor.execute("""
                INSERT INTO users
                    (username, password_hash, full_name, role, is_active, phone_number)
                VALUES (%s, %s, %s, %s, 1, %s)
            """, (
                username,
                generate_password_hash(password),
                full_name,
                role,
                phone_number or None
            ))
            connection.commit()
            flash("Account created. You can sign in now.")
            return redirect(url_for("login"))
        except mysql.connector.IntegrityError:
            if connection:
                connection.rollback()
            flash("That username is already in use.")
        except mysql.connector.Error as error:
            if connection:
                connection.rollback()
            flash(f"Account creation error: {error}")
        finally:
            if cursor:
                cursor.close()
            if connection:
                connection.close()

    return render_template("register.html")


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
                SELECT user_id, full_name, is_active
                FROM users
                WHERE username = %s
            """, (username,))

            user = cursor.fetchone()

            # Identity is confirmed with username + full name so this stays
            # self-service without needing an email/SMS provider configured.
            if (
                not user
                or not user["is_active"]
                or user["full_name"].strip().lower() != full_name.lower()
            ):
                flash(
                    "We couldn't verify that account. Check your username "
                    "and full name and try again."
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
        "register", "forgot_password", "static", "razorpay_webhook"
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

    user = get_current_user()
    if not user or not user["is_active"]:
        session.clear()
        return redirect(url_for("login"))

    if user["role"] != "admin" and request.endpoint not in STAFF_ALLOWED_ENDPOINTS:
        flash("You do not have permission to access that page.")
        return redirect(url_for("add_order"))

    return None



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
            ORDER BY user_id DESC
        """)
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
            cursor.execute("""
                INSERT INTO users
                    (username, password_hash, full_name, role, is_active, phone_number)
                VALUES (%s, %s, %s, %s, 1, %s)
            """, (
                username,
                generate_password_hash(password),
                full_name,
                role,
                phone_number or None
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

        cursor.execute("""
            SELECT user_id, username, full_name, role, is_active, phone_number
            FROM users
            WHERE user_id = %s
        """, (user_id,))
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

            if new_password:
                if len(new_password) < 8:
                    flash("New password must be at least 8 characters.")
                    return redirect(url_for("edit_user", user_id=user_id))
                cursor.execute("""
                    UPDATE users
                    SET full_name=%s, role=%s, is_active=%s,
                        phone_number=%s, password_hash=%s
                    WHERE user_id=%s
                """, (
                    full_name, role, is_active,
                    phone_number or None,
                    generate_password_hash(new_password),
                    user_id
                ))
            else:
                cursor.execute("""
                    UPDATE users
                    SET full_name=%s, role=%s, is_active=%s, phone_number=%s
                    WHERE user_id=%s
                """, (full_name, role, is_active, phone_number or None, user_id))

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
        cursor = connection.cursor()
        cursor.execute("""
            UPDATE users
            SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END
            WHERE user_id = %s
        """, (user_id,))
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

@app.route("/settings/branding", methods=["GET", "POST"])
def branding():

    denied = require_role("admin")
    if denied:
        return denied

    if request.method == "POST":

        cafe_name = request.form.get("cafe_name", "").strip()

        if not cafe_name:
            flash("Cafe name cannot be empty.")
            return redirect(url_for("branding"))

        # Store the cafe name in a small JSON file so no new MySQL table
        # is required for this customization.
        branding_file = os.path.join(
            app.root_path,
            "cafe_branding.json"
        )

        branding_data = {
            "cafe_name": cafe_name,
            "logo": "",
            "login_photo": ""
        }

        if os.path.exists(branding_file):
            try:
                with open(branding_file, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    branding_data["logo"] = old_data.get("logo", "")
                    branding_data["login_photo"] = old_data.get("login_photo", "")
            except Exception:
                pass

        allowed = {"png", "jpg", "jpeg", "webp", "gif"}

        upload_dir = os.path.join(
            app.root_path,
            "static",
            "uploads",
            "branding"
        )

        os.makedirs(upload_dir, exist_ok=True)

        logo = request.files.get("logo")

        if logo and logo.filename:
            ext = logo.filename.rsplit(".", 1)[-1].lower()

            if ext not in allowed:
                flash("Logo must be PNG, JPG, JPEG, WEBP or GIF.")
                return redirect(url_for("branding"))

            filename = "cafe_logo." + ext
            logo_path = os.path.join(upload_dir, filename)

            logo.save(logo_path)

            branding_data["logo"] = (
                "uploads/branding/" + filename
            )

        login_photo = request.files.get("login_photo")

        if login_photo and login_photo.filename:
            ext = login_photo.filename.rsplit(".", 1)[-1].lower()

            if ext not in allowed:
                flash("Login photo must be PNG, JPG, JPEG, WEBP or GIF.")
                return redirect(url_for("branding"))

            filename = "cafe_login." + ext
            login_photo_path = os.path.join(upload_dir, filename)

            login_photo.save(login_photo_path)

            branding_data["login_photo"] = (
                "uploads/branding/" + filename
            )

        with open(
            branding_file,
            "w",
            encoding="utf-8"
        ) as f:
            json.dump(branding_data, f, indent=4)

        flash("Cafe branding updated successfully.")
        return redirect(url_for("branding"))

    branding_file = os.path.join(
        app.root_path,
        "cafe_branding.json"
    )

    branding_data = {
        "cafe_name": "Coffeehouse",
        "logo": "",
        "login_photo": ""
    }

    if os.path.exists(branding_file):
        try:
            with open(
                branding_file,
                "r",
                encoding="utf-8"
            ) as f:
                branding_data.update(json.load(f))
        except Exception:
            pass

    return render_template(
        "branding.html",
        branding=branding_data
    )


@app.context_processor
def inject_cafe_branding():

    branding_file = os.path.join(
        app.root_path,
        "cafe_branding.json"
    )

    branding = {
        "cafe_name": "Coffeehouse",
        "logo": "",
        "login_photo": ""
    }

    if os.path.exists(branding_file):
        try:
            with open(
                branding_file,
                "r",
                encoding="utf-8"
            ) as f:
                branding.update(json.load(f))
        except Exception:
            pass

    return {
        "cafe_branding": branding
    }

if __name__ == "__main__":
    app.run(debug=True)

