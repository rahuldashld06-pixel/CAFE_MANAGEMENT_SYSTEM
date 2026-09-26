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

# str, not float. float(Decimal("1299.50")) is 1299.5 and float
# cannot hold a money value exactly; the text can, and the converter
# below turns it straight back into the Decimal it was.
sqlite3.register_adapter(Decimal, str)

# MySQL hands back a Decimal for a DECIMAL column and app.py relies on
# it - line totals are summed onto Decimal("0.00"), which will not add
# a float. Without this a dish priced with paise could not be ordered
# at all offline, so no test could use one.
sqlite3.register_converter("DECTEXT", lambda raw: Decimal(raw.decode()))

_DB = sqlite3.connect(":memory:", check_same_thread=False,
                      detect_types=sqlite3.PARSE_DECLTYPES)
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


_JOIN_UPDATE = re.compile(
    r"UPDATE\s+foods\s+f\s+"
    r"INNER\s+JOIN\s+inventory\s+i\s+"
    r"ON\s+f\.food_id\s*=\s*i\.food_id\s+"
    r"SET\s+f\.availability\s*=\s*(?P<expr>CASE.*?END)\s+"
    r"WHERE\s+(?P<where>f\.\w+\s*=\s*(?:%s|\?)"
    r"(?:\s+AND\s+f\.\w+\s*=\s*(?:%s|\?))*)",
    re.I | re.S,
)


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
    # DECTEXT, not NUMERIC. SQLite picks a column's affinity out of the
    # letters in its declared type: anything containing "TEXT" is stored
    # as given, while NUMERIC quietly turns "1299.50" into a float on
    # the way in and the exactness is gone before anything reads it
    # back. The name is also what PARSE_DECLTYPES looks up to find the
    # converter registered above.
    s = re.sub(r"\bDECIMAL\(\d+,\s*\d+\)", "DECTEXT", s, flags=re.I)
    s = re.sub(r"\bVARCHAR\(\d+\)", "TEXT", s, flags=re.I)
    s = re.sub(r"\bDATETIME\b", "TEXT", s, flags=re.I)
    s = re.sub(r"\s+ON\s+UPDATE\s+CURRENT_TIMESTAMP\b", "", s, flags=re.I)
    # SQLite's CURRENT_TIMESTAMP is UTC, but this shim answers NOW()
    # and CURDATE() from the local clock. On a machine that is not on
    # UTC the two disagree for part of every day - between midnight and
    # 05:30 in India a row stamped "yesterday" by the column default is
    # not found by a query asking for today, and whole suites fail for
    # the length of that window. A real deployment has one clock behind
    # both of these, so the double should have one too.
    s = re.sub(r"\bDEFAULT CURRENT_TIMESTAMP\b", "DEFAULT (NOW())",
               s, flags=re.I)
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
    # Seconds from now, on the SAME clock NOW() answers with. SQLite's
    # datetime('now') is UTC while this shim's NOW() is local, so an
    # expiry written by one and read by the other is wrong by the
    # machine's offset - five and a half hours in India, which made a
    # one-time code expire before it was ever shown. Production never
    # had it: there both sides are MySQL's own NOW().
    s = re.sub(r"DATE_ADD\(NOW\(\),\s*INTERVAL\s*%s\s*SECOND\)",
               "datetime(NOW(), '+' || %s || ' seconds')", s, flags=re.I)
    s = re.sub(r"\bDATE\((\w+\.?\w*)\)", r"date(\1)", s)

    # Multi-table UPDATE ... JOIN. MySQL can drive an UPDATE from a join;
    # SQLite cannot. app.py uses exactly one shape of it - resetting a food's
    # availability from its inventory row after stock moves - in two places,
    # so it is rewritten into the correlated-subquery form rather than
    # attempting a general translator. Without this the whole AJAX
    # order-taking path (and order cancellation) is untestable offline.
    def _join_update(match):
        # The WHERE clause keeps its columns but loses the alias, which the
        # rewritten single-table UPDATE no longer defines.
        where = re.sub(r"\bf\.", "", match.group("where"))
        return (
            "UPDATE foods SET availability = ("
            " SELECT %s FROM inventory i WHERE i.food_id = foods.food_id"
            " ) WHERE %s" % (match.group("expr"), where)
        )

    s = _JOIN_UPDATE.sub(_join_update, s)

    # Row locking. app.py takes SELECT ... FOR UPDATE on inventory before it
    # decrements stock, which is how it stops two tills overselling the last
    # item. SQLite has no such clause and needs none here: the shim is a
    # single connection, so statements are already serialised. Dropping the
    # clause keeps the AJAX order path testable offline - without it the
    # statement does not even parse.
    s = re.sub(r"\s+FOR\s+UPDATE\s*$", "", s, flags=re.I)
    s = re.sub(r"\s+LOCK\s+IN\s+SHARE\s+MODE\s*$", "", s, flags=re.I)

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

            # Column existence, answered from the database rather than
            # assumed. It used to return a flat "yes", on the grounds that
            # the CREATE TABLE statements define every column - true for a
            # fresh database, false for the hand-built legacy schema in
            # upgrade_test, where the ADD COLUMN migrations are the whole
            # point of the test and were being skipped.
            if "COLUMN_NAME" in up and "COLUMN_TYPE" not in up                     and len(params or ()) >= 2:
                table, column = params[0], params[1]
                raw = self._conn._raw
                try:
                    info = raw.execute(
                        f'PRAGMA table_info("{table}")').fetchall()
                except sqlite3.Error:
                    info = []
                found = 1 if any(entry[1] == column for entry in info) else 0
                self._rows = ([{"n": found, "COUNT(*)": found}]
                              if self.dictionary else [(found,)])
                self._i = 0
                return

            # The role enum type, and anything else that gets this far.
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

    @property
    def in_transaction(self):
        # The app's pool asks this before putting a connection back, so
        # the shim has to answer it honestly or the tests would never
        # exercise the path that catches a route leaving a write open.
        return self._raw.in_transaction

    def ping(self, **kwargs):
        return True

    def close(self):
        return None


def connect(**kwargs):
    return _Connection()


class _Pooling:
    # The app keeps its own pool and no longer asks for this one. It is
    # still here so that `import mysql.connector.pooling` anywhere else
    # does not fail, and it still refuses, so a return to the connector's
    # pool would be noticed rather than silently reinstating the ping on
    # every checkout that this shim never simulated.
    class MySQLConnectionPool:
        def __init__(self, **kwargs):
            raise Error("pooling disabled in test shim")


def skip_tour():
    """
    Mark every account as already shown round.

    Every browser suite registers a brand-new account and signs straight
    in, which is precisely the case the first-sign-in tour exists for - so
    without this it opens over the app and swallows the clicks the suite
    is trying to make. A suite that is not about the tour should not have
    to close one.

    tests/tour_browser_test.py deliberately does not call this, and
    tests/tutorial_test.py needs the flag left alone to test it.
    """
    _DB.execute("UPDATE users SET tutorial_seen = 1")
    _DB.commit()


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
