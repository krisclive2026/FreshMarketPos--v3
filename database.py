import sqlite3
import os
import sys
from contextlib import contextmanager


def resource_path(*relative_parts: str) -> str:
    if relative_parts and relative_parts[0] == '__data__':
        if getattr(sys, 'frozen', False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, *relative_parts[1:])
    else:
        if getattr(sys, 'frozen', False):
            base = sys._MEIPASS
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(base, *relative_parts)


DB_DIR  = resource_path('__data__', 'data')
DB_PATH = resource_path('__data__', 'data', 'pos.db')

def init_db():
    if not os.path.exists(DB_DIR):
        os.makedirs(DB_DIR)

    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sales (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp      DATETIME DEFAULT CURRENT_TIMESTAMP,
                total          REAL NOT NULL,
                subtotal       REAL NOT NULL DEFAULT 0,
                discount_total REAL NOT NULL DEFAULT 0,
                tax_amount     REAL NOT NULL DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sale_items (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                sale_id  INTEGER NOT NULL,
                name     TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                price    REAL NOT NULL,
                discount REAL NOT NULL DEFAULT 0,
                FOREIGN KEY (sale_id) REFERENCES sales (id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inventory (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL,
                price     REAL NOT NULL,
                image_url TEXT NOT NULL,
                category  TEXT NOT NULL DEFAULT 'General',
                stock     INTEGER NOT NULL DEFAULT 999
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shop_settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)

        # --- migrate existing DBs ---
        def _col_exists(table, col):
            rows = cursor.execute(f"PRAGMA table_info({table})").fetchall()
            return any(r[1] == col for r in rows)

        for col in ('category', 'stock'):
            if not _col_exists('inventory', col):
                default = "'General'" if col == 'category' else '999'
                cursor.execute(f"ALTER TABLE inventory ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}")

        for col in ('subtotal', 'discount_total', 'tax_amount'):
            if not _col_exists('sales', col):
                cursor.execute(f"ALTER TABLE sales ADD COLUMN {col} REAL NOT NULL DEFAULT 0")

        if not _col_exists('sale_items', 'discount'):
            cursor.execute("ALTER TABLE sale_items ADD COLUMN discount REAL NOT NULL DEFAULT 0")

        # --- seed defaults ---
        if cursor.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            cursor.executemany("INSERT INTO categories (name) VALUES (?)",
                [("Dairy",), ("Bakery",), ("Produce",), ("Drinks",), ("General",)])

        if cursor.execute("SELECT COUNT(*) FROM inventory").fetchone()[0] == 0:
            cursor.executemany(
                "INSERT INTO inventory (name, price, image_url, category, stock) VALUES (?, ?, ?, ?, ?)",
                [
                    ("Fresh Milk",        2.99, "/static/images/milk.png",    "Dairy",   50),
                    ("Whole Wheat Bread", 3.49, "/static/images/bread.png",   "Bakery",  30),
                    ("Red Apples",        1.99, "/static/images/apples.png",  "Produce", 100),
                    ("Bananas",           0.99, "/static/images/bananas.png", "Produce", 80),
                ]
            )

        # seed default shop settings if absent
        if cursor.execute("SELECT COUNT(*) FROM shop_settings").fetchone()[0] == 0:
            cursor.executemany("INSERT INTO shop_settings (key, value) VALUES (?, ?)", [
                ("shop_name",         "FreshMarket POS"),
                ("address",           ""),
                ("phone",             ""),
                ("gstin",             ""),
                ("gst_mode",          "none"),
                ("gst_rate",          "0"),
                ("sales_tax_enabled", "false"),
                ("sales_tax_rate",    "0"),
                ("sales_tax_name",    "Sales Tax"),
            ])
        else:
            # migrate: add sales_tax keys for existing databases
            existing_keys = {r[0] for r in cursor.execute("SELECT key FROM shop_settings").fetchall()}
            for key, default in [
                ("sales_tax_enabled", "false"),
                ("sales_tax_rate",    "0"),
                ("sales_tax_name",    "Sales Tax"),
            ]:
                if key not in existing_keys:
                    cursor.execute("INSERT INTO shop_settings (key, value) VALUES (?, ?)", (key, default))

        conn.commit()


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
