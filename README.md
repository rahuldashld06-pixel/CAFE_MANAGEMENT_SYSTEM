# Cafe Management System — Multi-Cafe SaaS

Flask + MySQL cafe management application with public cafe registration and tenant-isolated data.

## SaaS behavior
- `/register` creates a new cafe and its first Admin owner.
- Each user belongs to exactly one cafe.
- Foods, categories and orders are scoped by `cafe_id`.
- Inventory follows food ownership; bills follow order ownership.
- User Management and Branding are scoped to the current cafe.
- A fresh deployment does not create sample users or business data.

## Render + Aiven
Set these Render environment variables:
- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME` = `cafe_management`
- `SECRET_KEY`
- Razorpay variables when online payments are enabled.

Import your MySQL Workbench **Structure Only** dump into the Aiven `cafe_management` database. Do not import your local rows. The app then adds the SaaS tenant columns/tables on startup.

See `SAAS_DEPLOYMENT.md` for the deployment flow.
