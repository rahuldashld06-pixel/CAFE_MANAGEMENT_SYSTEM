-- ===================================================================
-- Cafe Management SaaS - reference schema
-- ===================================================================
--
-- YOU DO NOT NEED TO RUN THIS FILE.
--
-- The application creates and migrates every table below automatically
-- on first request (see ensure_auth_schema() in app.py). This file is
-- provided so you can review the schema, or provision it by hand if
-- your database user is not allowed to run DDL at runtime.
--
-- It is safe to run against an existing database: every statement is
-- guarded with IF NOT EXISTS.
--
-- Point it at an EMPTY database for a new SaaS deployment. Do not
-- import rows from a previous single-cafe install; the first real cafe
-- is created through /register.
-- ===================================================================

CREATE DATABASE IF NOT EXISTS cafe_management
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE cafe_management;


-- One row per tenant. Branding (name, logo, login photo) lives here so
-- each cafe is independent; it used to be a single shared JSON file on
-- disk that every cafe overwrote and that was wiped on each deploy.
CREATE TABLE IF NOT EXISTS cafes (
    cafe_id            INT AUTO_INCREMENT PRIMARY KEY,
    cafe_name          VARCHAR(150) NOT NULL,
    owner_user_id      INT NULL,
    logo_mime          VARCHAR(80) NULL,
    logo_blob          MEDIUMBLOB NULL,
    login_photo_mime   VARCHAR(80) NULL,
    login_photo_blob   MEDIUMBLOB NULL,
    branding_version   INT NOT NULL DEFAULT 1,
    created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active          TINYINT(1) NOT NULL DEFAULT 1
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- Usernames are unique across the whole platform, so a person signs in
-- with just a username and password and the cafe is resolved from their
-- account.
CREATE TABLE IF NOT EXISTS users (
    user_id        INT AUTO_INCREMENT PRIMARY KEY,
    username       VARCHAR(80) NOT NULL UNIQUE,
    password_hash  VARCHAR(255) NOT NULL,
    full_name      VARCHAR(120) NOT NULL,
    role           ENUM('admin','manager','cashier','staff')
                       NOT NULL DEFAULT 'staff',
    is_active      TINYINT(1) NOT NULL DEFAULT 1,
    phone_number   VARCHAR(20) NULL,
    cafe_id        INT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_users_cafe_id (cafe_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS categories (
    category_id    INT AUTO_INCREMENT PRIMARY KEY,
    category_name  VARCHAR(120) NOT NULL,
    description    TEXT NULL,
    user_id        INT NULL,
    cafe_id        INT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_categories_user_id (user_id),
    INDEX idx_categories_cafe_id (cafe_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- image_blob keeps food photos in the database. Container hosts give
-- each deploy a fresh, empty filesystem, so anything written under
-- static/uploads/ disappears on the next deploy or restart.
CREATE TABLE IF NOT EXISTS foods (
    food_id        INT AUTO_INCREMENT PRIMARY KEY,
    category_id    INT NULL,
    food_name      VARCHAR(150) NOT NULL,
    description    TEXT NULL,
    price          DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    availability   TINYINT(1) NOT NULL DEFAULT 1,
    image_mime     VARCHAR(80) NULL,
    image_blob     MEDIUMBLOB NULL,
    image_version  INT NOT NULL DEFAULT 1,
    user_id        INT NULL,
    cafe_id        INT NULL,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_foods_category_id (category_id),
    INDEX idx_foods_user_id (user_id),
    INDEX idx_foods_cafe_id (cafe_id),
    CONSTRAINT fk_foods_category
        FOREIGN KEY (category_id) REFERENCES categories(category_id)
        ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS inventory (
    inventory_id   INT AUTO_INCREMENT PRIMARY KEY,
    food_id        INT NOT NULL,
    quantity       INT NOT NULL DEFAULT 0,
    minimum_stock  INT NOT NULL DEFAULT 0,
    last_updated   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                       ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY uq_inventory_food (food_id),
    CONSTRAINT fk_inventory_food
        FOREIGN KEY (food_id) REFERENCES foods(food_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS orders (
    order_id       INT AUTO_INCREMENT PRIMARY KEY,
    order_date     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_amount   DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    order_status   VARCHAR(30) NOT NULL DEFAULT 'Pending',
    user_id        INT NULL,
    cafe_id        INT NULL,
    INDEX idx_orders_user_id (user_id),
    INDEX idx_orders_cafe_id (cafe_id),
    INDEX idx_orders_order_date (order_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


CREATE TABLE IF NOT EXISTS order_items (
    order_item_id  INT AUTO_INCREMENT PRIMARY KEY,
    order_id       INT NOT NULL,
    food_id        INT NULL,
    quantity       INT NOT NULL DEFAULT 1,
    price          DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    subtotal       DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    INDEX idx_order_items_order_id (order_id),
    INDEX idx_order_items_food_id (food_id),
    CONSTRAINT fk_order_items_order
        FOREIGN KEY (order_id) REFERENCES orders(order_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- One bill per order (uq_bills_order), so the "create any missing
-- bills" pass on the Billing page can never produce duplicates.
CREATE TABLE IF NOT EXISTS bills (
    bill_id             INT AUTO_INCREMENT PRIMARY KEY,
    order_id            INT NOT NULL,
    subtotal            DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    tax                 DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    discount            DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    total_amount        DECIMAL(10,2) NOT NULL DEFAULT 0.00,
    payment_method      VARCHAR(30) NOT NULL DEFAULT 'Cash',
    payment_status      VARCHAR(30) NOT NULL DEFAULT 'Pending',
    bill_date           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    gateway_order_id    VARCHAR(100) NULL,
    gateway_payment_id  VARCHAR(100) NULL,
    payment_reference   VARCHAR(150) NULL,
    gateway_signature   VARCHAR(255) NULL,
    UNIQUE KEY uq_bills_order (order_id),
    INDEX idx_bills_gateway_order_id (gateway_order_id),
    INDEX idx_bills_bill_date (bill_date),
    CONSTRAINT fk_bills_order
        FOREIGN KEY (order_id) REFERENCES orders(order_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;


-- One-time codes for admin sign-in. Codes are stored hashed and are
-- deleted as soon as they are used.
CREATE TABLE IF NOT EXISTS login_otp_codes (
    otp_id      INT AUTO_INCREMENT PRIMARY KEY,
    user_id     INT NOT NULL,
    code_hash   VARCHAR(255) NOT NULL,
    purpose     VARCHAR(20) NOT NULL DEFAULT 'login',
    expires_at  DATETIME NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_login_otp_user (user_id),
    CONSTRAINT fk_login_otp_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
