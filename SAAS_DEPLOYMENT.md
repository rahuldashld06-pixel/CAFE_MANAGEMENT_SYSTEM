# Multi-Cafe SaaS Deployment

This version is tenant-isolated: every public registration creates a new `cafes` row and an owner `users` row. Business records are scoped by `cafe_id`.

## Fresh Aiven database
1. Create the `cafe_management` database.
2. Import your **Structure Only** dump from MySQL Workbench (do not import data).
3. Deploy the app to Render.
4. Set `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` and `SECRET_KEY` in Render.
5. On first request, the app adds the SaaS columns/tables automatically.
6. Open `/register` and create the first cafe. It becomes that cafe's Admin.

## Tenant isolation
- Users belong to one cafe.
- Foods, categories and orders belong to one cafe.
- Inventory is isolated through food ownership.
- Bills are isolated through order ownership.
- User management only shows users from the current cafe.
- Branding is stored per cafe.

## No sample data
A fresh deployment does not create a default admin, sample foods, categories, orders or bills. The first real cafe is created through public registration.

Do not commit production passwords, Razorpay secrets, or your Render secret key to the repository.
