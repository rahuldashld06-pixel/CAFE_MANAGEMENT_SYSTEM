-- Authentication and ownership migration for the Cafe Management System.
-- The Flask application also performs this migration automatically on startup.
-- Run this manually only if you prefer database migrations outside the app.

CREATE TABLE IF NOT EXISTS users (
    user_id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(80) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    full_name VARCHAR(120) NOT NULL,
    role ENUM('admin','manager','cashier','staff') NOT NULL DEFAULT 'cashier',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    phone_number VARCHAR(20) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Add this column if the table already existed before the OTP feature:
-- ALTER TABLE users ADD COLUMN phone_number VARCHAR(20) NULL;

-- Widen the role ENUM if the table already existed before 'staff' was added:
-- ALTER TABLE users MODIFY COLUMN role
--     ENUM('admin','manager','cashier','staff') NOT NULL DEFAULT 'cashier';

-- One-time codes issued when an admin (with a phone_number on file) signs in.
-- Only 'admin' accounts are challenged for a code; manager/cashier accounts
-- are not.
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
);

-- Add these columns if they do not already exist:
-- ALTER TABLE foods ADD COLUMN user_id INT NULL;
-- ALTER TABLE orders ADD COLUMN user_id INT NULL;

-- Existing rows should be assigned to the initial administrator before
-- making user_id NOT NULL or adding foreign keys.
