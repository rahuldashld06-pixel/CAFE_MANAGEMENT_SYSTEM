from flask import Flask, render_template, request, redirect, url_for, flash
import mysql.connector
from decimal import Decimal
from config import DB_CONFIG

app= Flask(__name__)
app.secret_key = "cafe-management-secret-key"

def get_db_connection():
    connection = mysql.connector.connect(**DB_CONFIG, use_pure=True)
    return connection

@app.route("/")
def home():
    return render_template("dashboard.html")

@app.route("/test-db")
def test_db():
    try:
        connection = get_db_connection()

        cursor = connection.cursor()
        cursor.execute("SELECT DATABASE()")

        database_name = cursor.fetchone()[0]

        cursor.close()
        connection.close()

        return f"Database connected successfully!<br>Database: {database_name}"

    except mysql.connector.Error as error:
        return f"Database connection failed: {error}"

    # -----------------------------
# Dashboard
# -----------------------------
@app.route("/dashboard")
def dashboard():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    # Total food items
    cursor.execute("SELECT COUNT(*) AS total_foods FROM foods")
    total_foods = cursor.fetchone()["total_foods"]

    # Total categories
    cursor.execute("SELECT COUNT(*) AS total_categories FROM categories")
    total_categories = cursor.fetchone()["total_categories"]

    # Total orders
    cursor.execute("SELECT COUNT(*) AS total_orders FROM orders")
    total_orders = cursor.fetchone()["total_orders"]

    # Today's orders
    cursor.execute("""
        SELECT COUNT(*) AS today_orders
        FROM orders
        WHERE DATE(order_date) = CURDATE()
    """)
    today_orders = cursor.fetchone()["today_orders"]

    # Today's sales
    cursor.execute("""
        SELECT COALESCE(SUM(total_amount), 0) AS today_sales
        FROM orders
        WHERE DATE(order_date) = CURDATE()
        AND order_status != 'Cancelled'
    """)
    today_sales = cursor.fetchone()["today_sales"]

    # Low stock items
    cursor.execute("""
        SELECT COUNT(*) AS low_stock
        FROM inventory
        WHERE quantity <= minimum_stock
    """)
    low_stock = cursor.fetchone()["low_stock"]

    cursor.close()
    connection.close()

    return render_template(
        "dashboard.html",
        total_foods=total_foods,
        total_categories=total_categories,
        total_orders=total_orders,
        today_orders=today_orders,
        today_sales=today_sales,
        low_stock=low_stock
    )

# ==========================================
# FOOD MANAGEMENT - VIEW
# ==========================================

@app.route("/foods")
def foods():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            f.food_id,
            f.food_name,
            f.description,
            f.price,
            f.availability,
            c.category_name,
            COALESCE(i.quantity, 0) AS quantity

        FROM foods f

        JOIN categories c
            ON f.category_id = c.category_id

        LEFT JOIN inventory i
            ON f.food_id = i.food_id

        ORDER BY f.food_id DESC
    """)

    food_list = cursor.fetchall()

    cursor.close()
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

        availability = True if request.form.get("availability") else False

        quantity = request.form["quantity"]
        minimum_stock = request.form["minimum_stock"]


        # Insert food
        cursor.execute("""
            INSERT INTO foods
            (
                category_id,
                food_name,
                description,
                price,
                availability
            )

            VALUES (%s, %s, %s, %s, %s)
        """, (
            category_id,
            food_name,
            description,
            price,
            availability
        ))


        # Get newly created food ID
        food_id = cursor.lastrowid


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

        cursor.close()
        connection.close()


        flash("Food added successfully!")

        return redirect(url_for("foods"))


    cursor.close()
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

        availability = True if request.form.get("availability") else False

        quantity = request.form["quantity"]
        minimum_stock = request.form["minimum_stock"]


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
        """, (
            category_id,
            food_name,
            description,
            price,
            availability,
            food_id
        ))


        # Update inventory
        cursor.execute("""
            UPDATE inventory

            SET
                quantity = %s,
                minimum_stock = %s

            WHERE food_id = %s
        """, (
            quantity,
            minimum_stock,
            food_id
        ))


        connection.commit()

        cursor.close()
        connection.close()


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
    """, (food_id,))


    food = cursor.fetchone()


    cursor.close()
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

    connection = get_db_connection()
    cursor = connection.cursor()


    # Delete inventory first
    cursor.execute("""
        DELETE FROM inventory
        WHERE food_id = %s
    """, (food_id,))


    # Delete food
    cursor.execute("""
        DELETE FROM foods
        WHERE food_id = %s
    """, (food_id,))


    connection.commit()

    cursor.close()
    connection.close()


    flash("Food deleted successfully!")

    return redirect(url_for("foods"))

# ==========================================
# CATEGORY MANAGEMENT - VIEW
# ==========================================

@app.route("/categories")
def categories():

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
            ON c.category_id = f.category_id

        GROUP BY
            c.category_id,
            c.category_name,
            c.description

        ORDER BY c.category_id DESC
    """)

    category_list = cursor.fetchall()

    cursor.close()
    connection.close()

    return render_template(
        "categories.html",
        categories=category_list
    )

# ==========================================
# ADD CATEGORY
# ==========================================

@app.route("/categories/add", methods=["GET", "POST"])
def add_category():

    if request.method == "POST":

        category_name = request.form["category_name"].strip()
        description = request.form["description"].strip()

        if not category_name:
            flash("Category name is required!")
            return redirect(url_for("add_category"))

        connection = get_db_connection()
        cursor = connection.cursor()

        try:

            cursor.execute("""
                INSERT INTO categories
                (
                    category_name,
                    description
                )

                VALUES (%s, %s)
            """, (
                category_name,
                description
            ))

            connection.commit()

            flash("Category added successfully!")

        except mysql.connector.Error as error:

            connection.rollback()

            if error.errno == 1062:
                flash("Category already exists!")

            else:
                flash(f"Error adding category: {error}")

        finally:

            cursor.close()
            connection.close()

        return redirect(url_for("categories"))

    return render_template("add_category.html")

# ==========================================
# EDIT CATEGORY
# ==========================================

@app.route("/categories/edit/<int:category_id>", methods=["GET", "POST"])
def edit_category(category_id):

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    if request.method == "POST":

        category_name = request.form["category_name"].strip()
        description = request.form["description"].strip()

        if not category_name:

            cursor.close()
            connection.close()

            flash("Category name is required!")

            return redirect(
                url_for(
                    "edit_category",
                    category_id=category_id
                )
            )

        try:

            cursor.execute("""
                UPDATE categories

                SET
                    category_name = %s,
                    description = %s

                WHERE category_id = %s
            """, (
                category_name,
                description,
                category_id
            ))

            connection.commit()

            flash("Category updated successfully!")

        except mysql.connector.Error as error:

            connection.rollback()

            if error.errno == 1062:
                flash("Category already exists!")

            else:
                flash(f"Error updating category: {error}")

        finally:

            cursor.close()
            connection.close()

        return redirect(url_for("categories"))


    # Get existing category

    cursor.execute("""
        SELECT
            category_id,
            category_name,
            description

        FROM categories

        WHERE category_id = %s
    """, (category_id,))

    category = cursor.fetchone()

    cursor.close()
    connection.close()


    if category is None:
        return "Category not found", 404


    return render_template(
        "edit_category.html",
        category=category
    )

# ==========================================
# DELETE CATEGORY
# ==========================================

@app.route("/categories/delete/<int:category_id>", methods=["POST"])
def delete_category(category_id):

    connection = get_db_connection()
    cursor = connection.cursor()

    try:

        # Check whether foods use this category

        cursor.execute("""
            SELECT COUNT(*)
            FROM foods

            WHERE category_id = %s
        """, (category_id,))

        food_count = cursor.fetchone()[0]


        if food_count > 0:

            flash(
                "Cannot delete this category because "
                f"{food_count} food item(s) use it."
            )

        else:

            cursor.execute("""
                DELETE FROM categories

                WHERE category_id = %s
            """, (category_id,))

            connection.commit()

            flash("Category deleted successfully!")


    except mysql.connector.Error as error:

        connection.rollback()

        flash(f"Error deleting category: {error}")


    finally:

        cursor.close()
        connection.close()


    return redirect(url_for("categories"))

# ==========================================
# INVENTORY MANAGEMENT - VIEW
# ==========================================

@app.route("/inventory")
def inventory():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            i.inventory_id,
            i.food_id,
            f.food_name,
            c.category_name,
            f.price,
            f.availability,
            i.quantity,
            i.minimum_stock,
            i.last_updated

        FROM inventory i

        JOIN foods f
            ON i.food_id = f.food_id

        JOIN categories c
            ON f.category_id = c.category_id

        ORDER BY f.food_name
    """)

    inventory_list = cursor.fetchall()

    cursor.close()
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

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    if request.method == "POST":

        quantity = request.form["quantity"]
        minimum_stock = request.form["minimum_stock"]

        try:

            cursor.execute("""
                UPDATE inventory

                SET
                    quantity = %s,
                    minimum_stock = %s

                WHERE food_id = %s
            """, (
                quantity,
                minimum_stock,
                food_id
            ))

            connection.commit()

            flash("Stock updated successfully!")

        except mysql.connector.Error as error:

            connection.rollback()

            flash(f"Error updating stock: {error}")

        finally:

            cursor.close()
            connection.close()

        return redirect(url_for("inventory"))


    # Get current inventory information

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
    """, (food_id,))

    item = cursor.fetchone()

    cursor.close()
    connection.close()

    if item is None:

        return "Inventory item not found", 404


    return render_template(
        "update_stock.html",
        item=item
    )

@app.route("/orders")
def orders():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    cursor.execute("""
        SELECT
            order_id,
            order_date,
            total_amount,
            order_status
        FROM orders
        ORDER BY order_id DESC
    """)

    orders = cursor.fetchall()

    cursor.close()
    connection.close()

    return render_template(
        "orders.html",
        orders=orders
    )

# ==========================================
# CREATE MULTIPLE-ITEM ORDER
# ==========================================

@app.route("/orders/add", methods=["GET", "POST"])
def add_order():

    connection = get_db_connection()
    cursor = connection.cursor(dictionary=True)

    try:

        # ======================================
        # POST - CREATE ORDER
        # ======================================

        if request.method == "POST":

            selected_items = []


            # ----------------------------------
            # Get all available foods
            # ----------------------------------

            cursor.execute("""
                SELECT
                    f.food_id,
                    f.food_name,
                    f.price,
                    f.availability,
                    i.quantity,
                    c.category_name

                FROM foods f

                INNER JOIN inventory i
                    ON f.food_id = i.food_id

                LEFT JOIN categories c
                    ON f.category_id = c.category_id

                WHERE f.availability = 1
                  AND i.quantity > 0

                ORDER BY c.category_name, f.food_name
            """)

            foods = cursor.fetchall()


            # ----------------------------------
            # Read quantity for every food
            # ----------------------------------

            for food in foods:

                field_name = f"quantity_{food['food_id']}"

                quantity_text = request.form.get(
                    field_name,
                    "0"
                )


                try:

                    quantity = int(quantity_text)

                except ValueError:

                    flash(
                        f"Invalid quantity for "
                        f"{food['food_name']}."
                    )

                    return redirect(
                        url_for("add_order")
                    )


                # Ignore zero quantity
                if quantity == 0:
                    continue


                # Reject negative quantity
                if quantity < 0:

                    flash(
                        f"Invalid quantity for "
                        f"{food['food_name']}."
                    )

                    return redirect(
                        url_for("add_order")
                    )


                # ----------------------------------
                # Check stock
                # ----------------------------------

                if quantity > food["quantity"]:

                    flash(
                        f"Not enough stock for "
                        f"{food['food_name']}. "
                        f"Available stock: "
                        f"{food['quantity']}."
                    )

                    return redirect(
                        url_for("add_order")
                    )


                # ----------------------------------
                # Calculate item subtotal
                # ----------------------------------

                price = food["price"]

                item_subtotal = price * quantity


                selected_items.append({
                    "food_id": food["food_id"],
                    "food_name": food["food_name"],
                    "quantity": quantity,
                    "price": price,
                    "subtotal": item_subtotal
                })


            # ==================================
            # AT LEAST ONE ITEM REQUIRED
            # ==================================

            if not selected_items:

                flash(
                    "Please select at least one food item."
                )

                return redirect(
                    url_for("add_order")
                )


            # ==================================
            # CALCULATE ORDER SUBTOTAL
            # ==================================

            subtotal = sum(
                (
                    item["subtotal"]
                    for item in selected_items
                ),
                Decimal("0.00")
            )


            # ==================================
            # TAX
            # ==================================

            tax = subtotal * Decimal("0.05")


            # ==================================
            # DISCOUNT
            # ==================================

            discount = Decimal("0.00")


            # ==================================
            # FINAL TOTAL
            # ==================================

            total_amount = (
                subtotal
                + tax
                - discount
            )


            # ==================================
            # CREATE ORDER
            # ==================================

            cursor.execute("""
                INSERT INTO orders
                (
                    total_amount,
                    order_status
                )

                VALUES
                (
                    %s,
                    %s
                )
            """, (
                total_amount,
                "Pending"
            ))


            order_id = cursor.lastrowid


            # ==================================
            # CREATE ORDER ITEMS
            # ==================================

            for item in selected_items:

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


                # --------------------------------
                # REDUCE INVENTORY
                # --------------------------------

                cursor.execute("""
                    UPDATE inventory

                    SET quantity = quantity - %s

                    WHERE food_id = %s
                      AND quantity >= %s
                """, (
                    item["quantity"],
                    item["food_id"],
                    item["quantity"]
                ))


                # --------------------------------
                # Make sure stock was actually
                # reduced
                # --------------------------------

                if cursor.rowcount == 0:

                    raise Exception(
                        f"Inventory changed for "
                        f"{item['food_name']}. "
                        f"Please try again."
                    )


            # ==================================
            # CREATE ONE BILL
            # ==================================

            # Default values for now
            payment_method = "Cash"
            payment_status = "Paid"


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
                payment_method,
                payment_status
            ))


            # ==================================
            # COMMIT EVERYTHING
            # ==================================

            connection.commit()


            flash(
                f"Order #{order_id} created successfully!"
            )


            return redirect(
                url_for(
                    "order_details",
                    order_id=order_id
                )
            )


        # ======================================
        # GET - SHOW AVAILABLE FOOD
        # ======================================

        cursor.execute("""
            SELECT
                f.food_id,
                f.food_name,
                f.price,
                f.availability,
                i.quantity,
                c.category_name

            FROM foods f

            INNER JOIN inventory i
                ON f.food_id = i.food_id

            LEFT JOIN categories c
                ON f.category_id = c.category_id

            WHERE f.availability = 1
              AND i.quantity > 0

            ORDER BY
                c.category_name,
                f.food_name
        """)

        foods = cursor.fetchall()


        return render_template(
            "add_order.html",
            foods=foods
        )


    except mysql.connector.Error as error:

        connection.rollback()

        flash(
            f"Database error: {error}"
        )

        return redirect(
            url_for("orders")
        )


    except Exception as error:

        connection.rollback()

        flash(
            f"Error creating order: {error}"
        )

        return redirect(
            url_for("orders")
        )


    finally:

        cursor.close()
        connection.close()


if __name__ =="__main__":
    app.run(debug=True)

