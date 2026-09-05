-- Multi-cafe SaaS migration.
-- The Flask application also performs these changes automatically on startup.
-- Run against an EMPTY cafe_management database after importing your structure.

CREATE TABLE IF NOT EXISTS cafes (
    cafe_id INT AUTO_INCREMENT PRIMARY KEY,
    cafe_name VARCHAR(150) NOT NULL,
    owner_user_id INT NULL,
    logo_path VARCHAR(255) NULL,
    login_photo_path VARCHAR(255) NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    is_active TINYINT(1) NOT NULL DEFAULT 1
);

ALTER TABLE users ADD COLUMN cafe_id INT NULL;
ALTER TABLE categories ADD COLUMN cafe_id INT NULL;
ALTER TABLE foods ADD COLUMN cafe_id INT NULL;
ALTER TABLE orders ADD COLUMN cafe_id INT NULL;

CREATE INDEX idx_users_cafe_id ON users(cafe_id);
CREATE INDEX idx_categories_cafe_id ON categories(cafe_id);
CREATE INDEX idx_foods_cafe_id ON foods(cafe_id);
CREATE INDEX idx_orders_cafe_id ON orders(cafe_id);

-- Do not import local rows for a new SaaS deployment.
-- New cafe tenants are created by /register.
