"""
A minimal MySQL-on-SQLite shim used ONLY to smoke-test app.py offline.

This is a test harness, not part of the deployed application. It translates
the subset of MySQL that app.py uses into SQLite so the routes can be
exercised without a real database server.
"""
import re
import sqlite3
import sys
import types
import datetime


class Error(Exception):
    def __init__(self, msg="database error"):
        super().__init__(msg)
        self.msg = msg


class IntegrityError(Error):
    pass


from decimal import Decimal

sqlite3.register_adapter(Decimal, lambda d: float(d))

_DB = sqlite3.connect(":memory:", check_same_thread=False)
_DB.row_factory = sqlite3.Row
_DB.execute("PRAGMA foreign_keys=ON")

# Emulate the MySQL functions app.py calls.
_DB.create_function("DATABASE", 0, lambda: "cafe_management")
_DB.create_function("NOW", 0, lambda: datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
_DB.create_function("CURDATE", 0, lambda: datetime.date.today().isoformat())
_DB.create_function("LOWER", 1, lambda s: (s or "").lower())


# Test hooks: names listed here are reported as NOT existing, so a legacy
# database missing a unique key can be simulated.
MISSING_INDEXES = set()

# Primary key column per table, used to answer COLUMN_KEY = 'PRI' probes.
PRIMARY_KEYS = {
    "inventory": "inventory_id", "bills": "bill_id", "foods": "food_id",
    "orders": "order_id", "users": "user_id", "categories": "category_id",
    "cafes": "cafe_id", "order_items": "order_item_id",
    "login_otp_codes": "otp_id",
}


def _translate(sql):
    """Rewrite MySQL-specific syntax into something SQLite accepts."""
    s = sql

    # INFORMATION_SCHEMA probes -> answer from sqlite_master via a marker.
    if "INFORMATION_SCHEMA.COLUMNS" in s.upper() or \
       "INFORMATION_SCHEMA.STATISTICS" in s.upper():
        return "__INFOSCHEMA__", s

    s = re.sub(r"\bENGINE=InnoDB\b.*?(?=\)|$)", "", s, flags=re.I | re.S)
    s = re.sub(r"DEFAULT CHARSET=\w+", "", s, flags=re.I)
    s = re.sub(r"\bINT AUTO_INCREMENT PRIMARY KEY\b", "INTEGER PRIMARY KEY AUTOINCREMENT", s, flags=re.I)
    s = re.sub(r"\bAUTO_INCREMENT\b", "", s, flags=re.I)
    s = re.sub(r"\bMEDIUMBLOB\b", "BLOB", s, flags=re.I)
    s = re.sub(r"\bENUM\([^)]*\)", "TEXT", s, flags=re.I)
    s = re.sub(r"\bTINYINT\(\d+\)", "INTEGER", s, flags=re.I)
    s = re.sub(r"\bINT\(\d+\)", "INTEGER", s, flags=re.I)
    s = re.sub(r"\bDECIMAL\(\d+,\s*\d+\)", "NUMERIC", s, flags=re.I)
    s = re.sub(r"\bVARCHAR\(\d+\)", "TEXT", s, flags=re.I)
    s = re.sub(r"\bDATETIME\b", "TEXT", s, flags=re.I)
    s = re.sub(r"\s+ON\s+UPDATE\s+CURRENT_TIMESTAMP\b", "", s, flags=re.I)
    # Inline INDEX definitions inside CREATE TABLE are not valid in SQLite.
    s = re.sub(r",\s*INDEX \w+ \([^)]*\)", "", s, flags=re.I)
    s = re.sub(r",\s*UNIQUE KEY \w+ \(([^)]*)\)", r", UNIQUE(\1)", s, flags=re.I)
    s = re.sub(r",\s*CONSTRAINT \w+\s+FOREIGN KEY", ", FOREIGN KEY", s, flags=re.I)
    s = s.replace("`", '"')
    m = re.match(
        r'\s*ALTER TABLE\s+"?(\w+)"?\s+ADD UNIQUE KEY\s+"?(\w+)"?\s*\(([^)]*)\)',
        s, re.I)
    if m:
        table, key, cols = m.group(1), m.group(2), m.group(3)
        s = f'CREATE UNIQUE INDEX {key} ON {table} ({cols})' 
    s = re.sub(r"DATE_ADD\(NOW\(\),\s*INTERVAL\s*%s\s*SECOND\)",
               "datetime('now', '+' || %s || ' seconds')", s, flags=re.I)
    s = re.sub(r"\bDATE\((\w+\.?\w*)\)", r"date(\1)", s)
    return "__SQL__", s


class _Cursor:
    def __init__(self, conn, dictionary=False, buffered=False):
        self._conn = conn
        self._cur = conn._raw.cursor()
        self.dictionary = dictionary
        self.lastrowid = None
        self.rowcount = -1
        self._rows = []
        self._i = 0

    def execute(self, sql, params=()):
        kind, s = _translate(sql)

        if kind == "__INFOSCHEMA__":
            up = s.upper()

            # Primary-key lookup.
            if "COLUMN_KEY" in up and "PRI" in up:
                table = params[0] if params else None
                pk = PRIMARY_KEYS.get(table)
                self._rows = [{"pk": pk}] if pk else []
                self._i = 0
                return

            # Unique-index-by-shape probe: params are (table, column).
            # Answered from SQLite's real index metadata so the test
            # exercises the actual detection logic rather than a canned
            # "yes". A single-column UNIQUE index on that column counts;
            # a composite one does not.
            if "NON_UNIQUE" in up and "MAX(COLUMN_NAME)" in up:
                table = params[0] if params else None
                column = params[1] if len(params) > 1 else None
                raw = self._conn._raw
                found = []
                try:
                    indexes = raw.execute(
                        f'PRAGMA index_list("{table}")').fetchall()
                except sqlite3.Error:
                    indexes = []
                for entry in indexes:
                    name, is_unique = entry[1], entry[2]
                    if not is_unique or name in MISSING_INDEXES:
                        continue
                    cols = raw.execute(
                        f'PRAGMA index_info("{name}")').fetchall()
                    if len(cols) == 1 and cols[0][2] == column:
                        found.append(name)
                self._rows = ([{"name": n} for n in found] if self.dictionary
                              else [(n,) for n in found])
                self._i = 0
                return

            # Index existence probe: params are (table, index_name).
            if "STATISTICS" in up:
                index_name = params[1] if len(params) > 1 else None
                exists = 0 if index_name in MISSING_INDEXES else 1
                self._rows = [{"n": exists}] if self.dictionary else [(exists,)]
                self._i = 0
                return

            # Everything else (column existence, role enum type) already
            # exists, because the CREATE TABLE statements define it.
            row = {"n": 1, "COUNT(*)": 1,
                   "col_type": "enum('admin','manager','cashier','staff')"}
            self._rows = [row] if self.dictionary else [(1,)]
            self._i = 0
            return

        # ON DUPLICATE KEY UPDATE -> INSERT OR REPLACE (close enough here)
        if "ON DUPLICATE KEY UPDATE" in s.upper():
            s = re.sub(r"ON DUPLICATE KEY UPDATE.*$", "", s, flags=re.I | re.S)
            s = re.sub(r"^\s*INSERT INTO", "INSERT OR REPLACE INTO", s, flags=re.I)

        # Multi-table UPDATE ... JOIN is not supported by SQLite; the
        # backfill it is used for is a no-op on a fresh database.
        if re.search(r"UPDATE\s+\"?\w+\"?\s+\w+\s+JOIN", s, re.I):
            self._rows = []
            self._i = 0
            return

        m = re.match(
            r'\s*ALTER TABLE\s+"?(\w+)"?\s+ADD UNIQUE KEY\s+"?(\w+)"?\s*\(\s*"?(\w+)"?\s*\)',
            s, re.I)
        if m:
            s = (f'CREATE UNIQUE INDEX "{m.group(2)}" '
                 f'ON "{m.group(1)}" ("{m.group(3)}")')

        s = s.replace("%s", "?")
        try:
            self._cur.execute(s, tuple(params))
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e))
        except sqlite3.Error as e:
            raise Error(f"{e} :: {s[:300]}")

        self.lastrowid = self._cur.lastrowid
        self.rowcount = self._cur.rowcount
        try:
            fetched = self._cur.fetchall()
        except sqlite3.Error:
            fetched = []
        self._rows = [dict(r) for r in fetched] if self.dictionary else \
                     [tuple(r) for r in fetched]
        self._i = 0

    def fetchone(self):
        if self._i < len(self._rows):
            self._i += 1
            return self._rows[self._i - 1]
        return None

    def fetchall(self):
        rest = self._rows[self._i:]
        self._i = len(self._rows)
        return rest

    def close(self):
        try:
            self._cur.close()
        except Exception:
            pass


class _Connection:
    def __init__(self):
        self._raw = _DB
        self.autocommit = False

    def cursor(self, dictionary=False, buffered=False):
        return _Cursor(self, dictionary=dictionary, buffered=buffered)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        try:
            self._raw.rollback()
        except Exception:
            pass

    def ping(self, **kwargs):
        return True

    def close(self):
        return None


def connect(**kwargs):
    return _Connection()


class _Pooling:
    class MySQLConnectionPool:
        def __init__(self, **kwargs):
            raise Error("pooling disabled in test shim")


def install():
    """Register this shim as `mysql.connector` before app.py is imported."""
    mysql = types.ModuleType("mysql")
    connector = types.ModuleType("mysql.connector")
    connector.connect = connect
    connector.Error = Error
    connector.IntegrityError = IntegrityError
    connector.pooling = _Pooling
    mysql.connector = connector
    sys.modules["mysql"] = mysql
    sys.modules["mysql.connector"] = connector
    sys.modules["mysql.connector.pooling"] = _Pooling
