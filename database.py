import sqlite3
import datetime
import os
import re
import json
import secrets
import threading

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    from psycopg2.pool import ThreadedConnectionPool
except ImportError:
    psycopg2 = None
    ThreadedConnectionPool = None

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

IS_POSTGRES = DATABASE_URL.startswith("postgresql://") and psycopg2 is not None
DB_PATH = os.environ.get("DB_PATH", "billing.db")

INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + ((psycopg2.IntegrityError,) if psycopg2 else ())

_pg_pool = None
_pool_lock = threading.Lock()
_settings_cache = {}
_settings_lock = threading.Lock()


class _PooledConn:
    def __init__(self, pool, conn):
        self._pool = pool
        self._conn = conn
        self._closed = False

    def cursor(self, *args, **kwargs):
        return self._conn.cursor(*args, **kwargs)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        discard = False
        try:
            if not self._conn.closed:
                self._conn.rollback()
        except Exception:
            discard = True
        try:
            if self._pool:
                self._pool.putconn(self._conn, close=discard)
            else:
                self._conn.close()
        except Exception:
            try:
                self._conn.close()
            except Exception:
                pass


def _get_pg_pool():
    global _pg_pool
    if _pg_pool is None:
        with _pool_lock:
            if _pg_pool is None:
                _pg_pool = ThreadedConnectionPool(
                    1, 6, DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=10
                )
    return _pg_pool


def get_db():
    if IS_POSTGRES:
        pool = _get_pg_pool()
        for _ in range(2):
            conn = pool.getconn()
            try:
                if conn.closed:
                    pool.putconn(conn, close=True)
                    continue
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                conn.rollback()
                return _PooledConn(pool, conn)
            except Exception:
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    pass
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor, connect_timeout=10)
        return _PooledConn(None, conn)

    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _q(sql: str) -> str:
    return sql.replace("?", "%s") if IS_POSTGRES else sql


def _fetchone_dict(cur):
    row = cur.fetchone()
    return dict(row) if row else None


def _fetchall_dicts(cur):
    return [dict(r) for r in cur.fetchall()]


def clean_friendly_name(username: str) -> str:
    u = username.strip()
    if u.lower().startswith("mr@"):
        clean = u[3:]
    elif u.lower().startswith("mrf"):
        clean = "MRF " + u[3:]
    else:
        clean = re.sub(r'\d+$', '', u)
        clean = clean.replace("-", " ").replace("_", " ")
    return clean.strip().capitalize() or username


def _reload_settings_cache():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT key, value FROM settings")
    rows = _fetchall_dicts(cur)
    conn.close()
    with _settings_lock:
        _settings_cache.clear()
        for r in rows:
            _settings_cache[r["key"]] = r["value"]


def init_db():
    conn = get_db()
    cur = conn.cursor()

    if IS_POSTGRES:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id SERIAL PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            password TEXT DEFAULT '123456',
            name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            package_name TEXT DEFAULT '25 Mbps',
            package_price INTEGER DEFAULT 525,
            profile TEXT DEFAULT 'default',
            expiry_date TEXT,
            status TEXT DEFAULT 'active',
            mac_address TEXT DEFAULT '',
            olt_pon INTEGER DEFAULT 0,
            olt_onuid INTEGER DEFAULT 0,
            onu_sn TEXT DEFAULT '',
            created_at TEXT
        )
        """)
        for col, col_type in [
            ("mac_address", "TEXT DEFAULT ''"),
            ("olt_pon", "INTEGER DEFAULT 0"),
            ("olt_onuid", "INTEGER DEFAULT 0"),
            ("onu_sn", "TEXT DEFAULT ''"),
        ]:
            cur.execute(f"ALTER TABLE customers ADD COLUMN IF NOT EXISTS {col} {col_type}")

        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            trx_id TEXT UNIQUE NOT NULL,
            amount REAL NOT NULL,
            sender_phone TEXT,
            raw_sms TEXT,
            received_at TEXT,
            used_by TEXT,
            used_at TEXT,
            status TEXT DEFAULT 'unused'
        )
        """)
    else:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT DEFAULT '123456',
            name TEXT DEFAULT '',
            phone TEXT DEFAULT '',
            package_name TEXT DEFAULT '25 Mbps',
            package_price INTEGER DEFAULT 525,
            profile TEXT DEFAULT 'default',
            expiry_date TEXT,
            status TEXT DEFAULT 'active',
            mac_address TEXT DEFAULT '',
            olt_pon INTEGER DEFAULT 0,
            olt_onuid INTEGER DEFAULT 0,
            onu_sn TEXT DEFAULT '',
            created_at TEXT
        )
        """)
        for col, col_type in [
            ("password", "TEXT DEFAULT '123456'"),
            ("name", "TEXT DEFAULT ''"),
            ("phone", "TEXT DEFAULT ''"),
            ("profile", "TEXT DEFAULT 'default'"),
            ("mac_address", "TEXT DEFAULT ''"),
            ("olt_pon", "INTEGER DEFAULT 0"),
            ("olt_onuid", "INTEGER DEFAULT 0"),
            ("onu_sn", "TEXT DEFAULT ''"),
            ("created_at", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE customers ADD COLUMN {col} {col_type}")
            except Exception:
                pass

        cur.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trx_id TEXT UNIQUE NOT NULL,
            amount REAL NOT NULL,
            sender_phone TEXT,
            raw_sms TEXT,
            received_at TEXT,
            used_by TEXT,
            used_at TEXT,
            status TEXT DEFAULT 'unused'
        )
        """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

    default_settings = {
        "bkash_number": os.environ.get("BKASH_NUMBER", "01XXXXXXXXX"),
        "company_name": os.environ.get("COMPANY_NAME", "সাইফুল ডিস এন্ড ইন্টারনেট 🛜"),
        "admin_password": os.environ.get("ADMIN_PASSWORD", "admin123"),
        "session_secret": os.environ.get("SESSION_SECRET", secrets.token_hex(32)),
        "router_driver": os.environ.get("ROUTER_DRIVER", "mikrotik_api"),
        "mikrotik_host": os.environ.get("MIKROTIK_HOST", ""),
        "mikrotik_port": os.environ.get("MIKROTIK_PORT", "8728"),
        "mikrotik_user": os.environ.get("MIKROTIK_USER", "admin"),
        "mikrotik_pass": os.environ.get("MIKROTIK_PASS", ""),
        "block_mode": os.environ.get("BLOCK_MODE", "address_list"),
        "olt_driver": os.environ.get("OLT_DRIVER", "vsol_gpon_web"),
        "olt_url": os.environ.get("OLT_URL", ""),
        "olt_user": os.environ.get("OLT_USER", "admin"),
        "olt_pass": os.environ.get("OLT_PASS", ""),
    }

    for k, v in default_settings.items():
        cur.execute(
            _q("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING"),
            (k, str(v))
        )

    conn.commit()
    conn.close()
    _reload_settings_cache()


def get_setting(key: str, default: str = "") -> str:
    if not _settings_cache:
        try:
            _reload_settings_cache()
        except Exception:
            pass
    with _settings_lock:
        val = _settings_cache.get(key)
    return val if val is not None else default


def set_setting(key: str, value: str):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        _q("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"),
        (key, str(value))
    )
    conn.commit()
    conn.close()
    with _settings_lock:
        _settings_cache[key] = str(value)


def get_all_settings() -> dict:
    if not _settings_cache:
        try:
            _reload_settings_cache()
        except Exception:
            pass
    with _settings_lock:
        return dict(_settings_cache)


def record_payment(trx_id, amount, sender_phone, raw_sms):
    trx_id = trx_id.strip().upper()
    conn = get_db()
    cur = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur.execute(
            _q("""
            INSERT INTO payments (trx_id, amount, sender_phone, raw_sms, received_at, status)
            VALUES (?, ?, ?, ?, ?, 'unused')
            """),
            (trx_id, float(amount), sender_phone, raw_sms, now)
        )
        conn.commit()
        success = True
    except INTEGRITY_ERRORS:
        conn.rollback()
        success = False
    conn.close()
    return success


def get_recent_payments(limit=30):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM payments ORDER BY id DESC LIMIT ?"), (int(limit),))
    rows = _fetchall_dicts(cur)
    conn.close()
    return rows


def search_customers(query, limit=10):
    query = query.strip()
    if not query:
        return []
    conn = get_db()
    cur = conn.cursor()
    like_q = f"%{query}%"
    cur.execute(
        _q("""
        SELECT id, username, name, phone, package_name, package_price, status
        FROM customers
        WHERE LOWER(username) LIKE LOWER(?)
           OR phone LIKE ?
           OR LOWER(name) LIKE LOWER(?)
           OR LOWER(onu_sn) LIKE LOWER(?)
        ORDER BY name ASC
        LIMIT ?
        """),
        (like_q, like_q, like_q, like_q, int(limit))
    )
    rows = _fetchall_dicts(cur)
    conn.close()
    return rows


def get_all_customers():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers ORDER BY expiry_date ASC")
    rows = _fetchall_dicts(cur)
    conn.close()
    return rows


def get_customer_by_id(cust_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM customers WHERE id = ?"), (int(cust_id),))
    row = _fetchone_dict(cur)
    conn.close()
    return row


def get_customer_by_username(username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM customers WHERE LOWER(username) = LOWER(?)"), (username.strip(),))
    row = _fetchone_dict(cur)
    conn.close()
    return row


def get_expired_active_customers():
    conn = get_db()
    cur = conn.cursor()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(_q("SELECT * FROM customers WHERE expiry_date < ? AND status = 'active'"), (now_str,))
    rows = _fetchall_dicts(cur)
    conn.close()
    return rows


def get_all_expired_usernames():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username FROM customers WHERE status = 'expired'")
    rows = _fetchall_dicts(cur)
    conn.close()
    return {r["username"] for r in rows}


def mark_customer_expired(username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("UPDATE customers SET status = 'expired' WHERE username = ?"), (username,))
    conn.commit()
    conn.close()


def create_customer(username, password, name, phone, package_name, package_price, profile, days_valid=30):
    username = username.strip()
    friendly = name.strip() or clean_friendly_name(username)
    conn = get_db()
    cur = conn.cursor()
    now = datetime.datetime.now()
    expiry = (now + datetime.timedelta(days=int(days_valid))).strftime("%Y-%m-%d %H:%M:%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur.execute(
            _q("""
            INSERT INTO customers (username, password, name, phone, package_name, package_price, profile, expiry_date, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
            """),
            (username, password.strip(), friendly, phone.strip(), package_name.strip(), int(package_price), profile.strip(), expiry, now_str)
        )
        conn.commit()
        success, err = True, ""
    except INTEGRITY_ERRORS:
        conn.rollback()
        success, err = False, f"Username '{username}' already exists!"
    conn.close()
    return success, err


def upsert_synced_secrets(secrets_list):
    conn = get_db()
    cur = conn.cursor()
    now = datetime.datetime.now()
    next_month = (now + datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    added = 0
    for s in secrets_list:
        uname = (s.get("name") or "").strip()
        if not uname:
            continue
        cur.execute(_q("SELECT id FROM customers WHERE LOWER(username) = LOWER(?)"), (uname,))
        if cur.fetchone():
            continue
        pwd = s.get("password", "123456")
        prof = s.get("profile", "default")
        mac = s.get("caller_id", "") or s.get("last_caller_id", "")
        status = "expired" if s.get("disabled") else "active"
        friendly = clean_friendly_name(uname)
        cur.execute(
            _q("""
            INSERT INTO customers (username, password, name, package_name, package_price, profile, expiry_date, status, mac_address, created_at)
            VALUES (?, ?, ?, '25 Mbps', 525, ?, ?, ?, ?, ?)
            """),
            (uname, pwd, friendly, prof, next_month, status, mac, now_str)
        )
        added += 1
    conn.commit()
    conn.close()
    return added


def update_customer_full(cust_id, name, phone, password, package_name, package_price, profile, expiry_date, status):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        _q("""
        UPDATE customers
        SET name = ?, phone = ?, password = ?, package_name = ?, package_price = ?, profile = ?, expiry_date = ?, status = ?
        WHERE id = ?
        """),
        (
            (name or "").strip(),
            (phone or "").strip(),
            (password or "").strip(),
            (package_name or "25 Mbps").strip(),
            int(package_price),
            (profile or "default").strip(),
            (expiry_date or "").strip(),
            status,
            int(cust_id),
        )
    )
    conn.commit()
    conn.close()


def update_customer_mac(cust_id, mac_address):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("UPDATE customers SET mac_address = ? WHERE id = ?"), ((mac_address or "").strip(), int(cust_id)))
    conn.commit()
    conn.close()


def link_customer_onu(cust_id, pon_id, onu_id, onu_sn=""):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        _q("UPDATE customers SET olt_pon = ?, olt_onuid = ?, onu_sn = ? WHERE id = ?"),
        (int(pon_id), int(onu_id), (onu_sn or "").strip(), int(cust_id))
    )
    conn.commit()
    conn.close()


def delete_customer(cust_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT username FROM customers WHERE id = ?"), (int(cust_id),))
    row = _fetchone_dict(cur)
    username = row["username"] if row else None
    if username:
        cur.execute(_q("DELETE FROM customers WHERE id = ?"), (int(cust_id),))
        conn.commit()
    conn.close()
    return username


def extend_customer(cust_id, days=30):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(_q("SELECT * FROM customers WHERE id = ?"), (int(cust_id),))
    cust = _fetchone_dict(cur)
    if not cust:
        conn.close()
        return None, None

    now = datetime.datetime.now()
    try:
        if len(cust["expiry_date"]) == 10:
            curr_expiry = datetime.datetime.strptime(cust["expiry_date"], "%Y-%m-%d")
        else:
            curr_expiry = datetime.datetime.strptime(cust["expiry_date"][:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        curr_expiry = now

    new_expiry = (curr_expiry if curr_expiry >= now else now) + datetime.timedelta(days=int(days))
    new_expiry_str = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
    cur.execute(_q("UPDATE customers SET expiry_date = ?, status = 'active' WHERE id = ?"), (new_expiry_str, int(cust_id)))
    conn.commit()
    conn.close()
    return cust["username"], new_expiry_str


def verify_and_apply_payment(username_or_id, trx_id):
    trx_id = trx_id.strip().upper()
    conn = get_db()
    cur = conn.cursor()

    if str(username_or_id).strip().isdigit():
        cur.execute(_q("SELECT * FROM customers WHERE LOWER(username) = LOWER(?) OR id = ?"), (str(username_or_id).strip(), int(username_or_id)))
    else:
        cur.execute(_q("SELECT * FROM customers WHERE LOWER(username) = LOWER(?)"), (str(username_or_id).strip(),))
    cust = _fetchone_dict(cur)
    if not cust:
        conn.close()
        return False, "কাস্টমার পাওয়া যায়নি! সঠিক ইউজার আইডি বা নাম দিন।"

    cur.execute(_q("SELECT * FROM payments WHERE trx_id = ?"), (trx_id,))
    payment = _fetchone_dict(cur)
    if not payment:
        conn.close()
        return False, "এই TrxID দিয়ে কোনো পেমেন্ট পাওয়া যায়নি! টাকা পাঠানোর ১-২ মিনিট পর আবার ট্রাই করুন।"

    if payment["status"] != "unused":
        conn.close()
        return False, f"এই TrxID টি ইতিমধ্যে ব্যবহার করা হয়েছে ({payment['used_by']})!"

    if float(payment["amount"]) < float(cust["package_price"]):
        conn.close()
        return False, f"টাকার পরিমাণ কম! আপনার প্যাকেজের বিল {cust['package_price']} টাকা, কিন্তু পেমেন্ট এসেছে {payment['amount']} টাকা।"

    now = datetime.datetime.now()
    try:
        if len(cust["expiry_date"]) == 10:
            curr_expiry = datetime.datetime.strptime(cust["expiry_date"], "%Y-%m-%d")
        else:
            curr_expiry = datetime.datetime.strptime(cust["expiry_date"][:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        curr_expiry = now

    new_expiry = (curr_expiry if curr_expiry >= now else now) + datetime.timedelta(days=30)
    new_expiry_str = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    cur.execute(_q("UPDATE payments SET status = 'used', used_by = ?, used_at = ? WHERE id = ?"), (cust["username"], now_str, payment["id"]))
    cur.execute(_q("UPDATE customers SET expiry_date = ?, status = 'active' WHERE id = ?"), (new_expiry_str, cust["id"]))
    conn.commit()
    conn.close()

    formatted_date = new_expiry.strftime("%d-%b-%Y %I:%M %p")
    return True, f"🎉 অভিনন্দন! আপনার {payment['amount']} টাকা জমা হয়েছে। আপনার ইন্টারনেট ঠিক ৩০ দিন অর্থাৎ {formatted_date} পর্যন্ত সচল থাকবে।"


def get_stats():
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active,
               COALESCE(SUM(CASE WHEN status = 'expired' THEN 1 ELSE 0 END), 0) AS expired
        FROM customers
    """)
    crow = _fetchone_dict(cur) or {}

    today_str = datetime.date.today().isoformat()
    cur.execute(
        _q("""
        SELECT COALESCE(SUM(amount), 0) AS total_rev,
               COALESCE(SUM(CASE WHEN used_at LIKE ? THEN amount ELSE 0 END), 0) AS today_rev
        FROM payments WHERE status = 'used'
        """),
        (f"{today_str}%",)
    )
    prow = _fetchone_dict(cur) or {}
    conn.close()

    return {
        "total": int(crow.get("total") or 0),
        "active": int(crow.get("active") or 0),
        "expired": int(crow.get("expired") or 0),
        "total_revenue": round(float(prow.get("total_rev") or 0), 2),
        "today_revenue": round(float(prow.get("today_rev") or 0), 2),
    }


def export_backup_json() -> str:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers ORDER BY id ASC")
    customers = _fetchall_dicts(cur)
    cur.execute("SELECT * FROM payments ORDER BY id ASC")
    payments = _fetchall_dicts(cur)
    cur.execute("SELECT * FROM settings")
    settings = _fetchall_dicts(cur)
    conn.close()
    return json.dumps({
        "exported_at": datetime.datetime.now().isoformat(),
        "customers": customers,
        "payments": payments,
        "settings": settings,
    }, ensure_ascii=False, indent=2)


def import_backup_json(json_text: str) -> dict:
    data = json.loads(json_text)
    conn = get_db()
    cur = conn.cursor()
    c_count, p_count = 0, 0

    for c in data.get("customers", []):
        cur.execute(
            _q("""
            INSERT INTO customers (username, password, name, phone, package_name, package_price, profile, expiry_date, status, mac_address, olt_pon, olt_onuid, onu_sn, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                password = excluded.password,
                name = excluded.name,
                phone = excluded.phone,
                package_name = excluded.package_name,
                package_price = excluded.package_price,
                profile = excluded.profile,
                expiry_date = excluded.expiry_date,
                status = excluded.status,
                mac_address = excluded.mac_address,
                olt_pon = excluded.olt_pon,
                olt_onuid = excluded.olt_onuid,
                onu_sn = excluded.onu_sn
            """),
            (
                c["username"], c.get("password", "123456"), c.get("name", ""), c.get("phone", ""),
                c.get("package_name", "25 Mbps"), int(c.get("package_price", 525)), c.get("profile", "default"),
                c.get("expiry_date", ""), c.get("status", "active"), c.get("mac_address", ""),
                int(c.get("olt_pon", 0) or 0), int(c.get("olt_onuid", 0) or 0),
                c.get("onu_sn", ""), c.get("created_at", "")
            )
        )
        c_count += 1

    for p in data.get("payments", []):
        cur.execute(
            _q("""
            INSERT INTO payments (trx_id, amount, sender_phone, raw_sms, received_at, used_by, used_at, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trx_id) DO NOTHING
            """),
            (
                p["trx_id"], float(p["amount"]), p.get("sender_phone", ""), p.get("raw_sms", ""),
                p.get("received_at", ""), p.get("used_by"), p.get("used_at"), p.get("status", "unused")
            )
        )
        p_count += 1

    conn.commit()
    conn.close()
    return {"customers": c_count, "payments": p_count}
