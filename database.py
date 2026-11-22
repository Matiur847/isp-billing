import sqlite3
import datetime
import os

DB_PATH = os.environ.get("DB_PATH", "billing.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    
    # Customers table with exact date-time expiry, password and line profile
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
        created_at TEXT
    )
    """)
    
    # Check and add new columns if upgrading from earlier version
    columns = [
        ("password", "TEXT DEFAULT '123456'"),
        ("name", "TEXT DEFAULT ''"),
        ("phone", "TEXT DEFAULT ''"),
        ("profile", "TEXT DEFAULT 'default'"),
        ("created_at", "TEXT")
    ]
    for col, col_type in columns:
        try:
            cur.execute(f"ALTER TABLE customers ADD COLUMN {col} {col_type}")
        except Exception:
            pass
            
    # Payments table
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
    
    # Settings table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)
    
    # Initial settings
    default_settings = {
        "bkash_number": "01859734056",
        "admin_password": "admin",
        "mikrotik_host": "remote1.dslabbd.com",
        "mikrotik_port": "35105",
        "mikrotik_user": "admin",
        "mikrotik_pass": "matiur847@#"
    }
    for k, v in default_settings.items():
        cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, v))
        
    # Pre-seed default users if empty
    cur.execute("SELECT COUNT(*) FROM customers")
    if cur.fetchone()[0] == 0:
        # 3 Line group profiles
        line1_users = ["Anisur97", "Azam55", "Babu85", "MR@obaidul", "MR@sarul", "MRF11", "MRF15J"]
        line2_users = ["MRF107", "MRF117", "MRF141", "MRF200S", "Mamidul01", "Mizan", "Moynul"]
        line3_users = ["Owab1234", "Primary-school", "Rafi81", "Romana005", "Sahinur-74", "Shofik1", "juyel1"]
        
        now = datetime.datetime.now()
        next_month = (now + datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        
        for u in line1_users:
            cur.execute("""
            INSERT INTO customers (username, password, name, package_name, package_price, profile, expiry_date, status, created_at)
            VALUES (?, '123456', ?, '25 Mbps', 525, 'default', ?, 'active', ?)
            """, (u, u, next_month, now_str))
            
        for u in line2_users:
            cur.execute("""
            INSERT INTO customers (username, password, name, package_name, package_price, profile, expiry_date, status, created_at)
            VALUES (?, '123456', ?, '25 Mbps', 525, 'Profile-Line2', ?, 'active', ?)
            """, (u, u, next_month, now_str))
            
        for u in line3_users:
            cur.execute("""
            INSERT INTO customers (username, password, name, package_name, package_price, profile, expiry_date, status, created_at)
            VALUES (?, '123456', ?, '25 Mbps', 525, 'Profile-Line3', ?, 'active', ?)
            """, (u, u, next_month, now_str))
            
    conn.commit()
    conn.close()

def get_setting(key, default=""):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def set_setting(key, value):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def record_payment(trx_id, amount, sender_phone, raw_sms):
    trx_id = trx_id.strip().upper()
    conn = get_db()
    cur = conn.cursor()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur.execute("""
        INSERT INTO payments (trx_id, amount, sender_phone, raw_sms, received_at, status)
        VALUES (?, ?, ?, ?, ?, 'unused')
        """, (trx_id, amount, sender_phone, raw_sms, now))
        conn.commit()
        success = True
    except sqlite3.IntegrityError:
        success = False
    conn.close()
    return success

def search_customers(query):
    query = query.strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    SELECT * FROM customers 
    WHERE LOWER(username) = LOWER(?) 
       OR phone = ? 
       OR LOWER(name) LIKE LOWER(?)
    """, (query, query, f"%{query}%"))
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_all_customers():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers ORDER BY expiry_date ASC")
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_customer_by_id(cust_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE id = ?", (cust_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_customer_by_username(username):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE LOWER(username) = LOWER(?)", (username.strip(),))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def create_customer(username, password, name, phone, package_name, package_price, profile, days_valid=30):
    username = username.strip()
    conn = get_db()
    cur = conn.cursor()
    now = datetime.datetime.now()
    expiry = (now + datetime.timedelta(days=int(days_valid))).strftime("%Y-%m-%d %H:%M:%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur.execute("""
        INSERT INTO customers (username, password, name, phone, package_name, package_price, profile, expiry_date, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """, (username, password.strip(), name.strip(), phone.strip(), package_name.strip(), int(package_price), profile.strip(), expiry, now_str))
        conn.commit()
        success = True
        err = ""
    except sqlite3.IntegrityError:
        success = False
        err = f"Username '{username}' already exists!"
    conn.close()
    return success, err

def update_customer_full(cust_id, name, phone, password, package_name, package_price, profile, expiry_date, status):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE customers 
    SET name = ?, phone = ?, password = ?, package_name = ?, package_price = ?, profile = ?, expiry_date = ?, status = ?
    WHERE id = ?
    """, (name.strip(), phone.strip(), password.strip(), package_name.strip(), int(package_price), profile.strip(), expiry_date.strip(), status, cust_id))
    conn.commit()
    conn.close()

def delete_customer(cust_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT username FROM customers WHERE id = ?", (cust_id,))
    row = cur.fetchone()
    username = row["username"] if row else None
    if username:
        cur.execute("DELETE FROM customers WHERE id = ?", (cust_id,))
        conn.commit()
    conn.close()
    return username

def extend_customer(cust_id, days=30):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE id = ?", (cust_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return None, None
        
    cust = dict(row)
    now = datetime.datetime.now()
    try:
        if len(cust["expiry_date"]) == 10:
            curr_expiry = datetime.datetime.strptime(cust["expiry_date"], "%Y-%m-%d")
        else:
            curr_expiry = datetime.datetime.strptime(cust["expiry_date"][:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        curr_expiry = now
        
    if curr_expiry >= now:
        new_expiry = curr_expiry + datetime.timedelta(days=int(days))
    else:
        new_expiry = now + datetime.timedelta(days=int(days))
        
    new_expiry_str = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
    cur.execute("UPDATE customers SET expiry_date = ?, status = 'active' WHERE id = ?", (new_expiry_str, cust_id))
    conn.commit()
    conn.close()
    return cust["username"], new_expiry_str

# --- PAYMENT VERIFICATION WITH PRECISE 30-DAY TIMESTAMP ---
def verify_and_apply_payment(username_or_id, trx_id):
    trx_id = trx_id.strip().upper()
    conn = get_db()
    cur = conn.cursor()
    
    cur.execute("SELECT * FROM customers WHERE LOWER(username) = LOWER(?) OR id = ?", (str(username_or_id).strip(), str(username_or_id).strip()))
    cust = cur.fetchone()
    if not cust:
        conn.close()
        return False, "কাস্টমার পাওয়া যায়নি! সঠিক ইউজার আইডি বা নাম দিন।"
        
    cust = dict(cust)
    
    # Check TrxID
    cur.execute("SELECT * FROM payments WHERE trx_id = ?", (trx_id,))
    payment = cur.fetchone()
    if not payment:
        conn.close()
        return False, "এই TrxID দিয়ে কোনো পেমেন্ট পাওয়া যায়নি! টাকা পাঠানোর ১-২ মিনিট পর আবার ট্রাই করুন।"
        
    payment = dict(payment)
    if payment["status"] != "unused":
        conn.close()
        return False, f"এই TrxID টি ইতিমধ্যে ব্যবহার করা হয়েছে ({payment['used_by']})!"
        
    if payment["amount"] < cust["package_price"]:
        conn.close()
        return False, f"টাকার পরিমাণ কম! আপনার প্যাকেজের বিল {cust['package_price']} টাকা, কিন্তু পেমেন্ট এসেছে {payment['amount']} টাকা।"
        
    # EXACT 30 DAYS TO THE EXACT MINUTE AND SECOND
    now = datetime.datetime.now()
    try:
        if len(cust["expiry_date"]) == 10:
            curr_expiry = datetime.datetime.strptime(cust["expiry_date"], "%Y-%m-%d")
        else:
            curr_expiry = datetime.datetime.strptime(cust["expiry_date"][:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        curr_expiry = now
        
    if curr_expiry >= now:
        new_expiry = curr_expiry + datetime.timedelta(days=30)
    else:
        new_expiry = now + datetime.timedelta(days=30)
        
    new_expiry_str = new_expiry.strftime("%Y-%m-%d %H:%M:%S")
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    
    # Update payment
    cur.execute("""
    UPDATE payments SET status = 'used', used_by = ?, used_at = ? WHERE id = ?
    """, (cust["username"], now_str, payment["id"]))
    
    # Update customer
    cur.execute("""
    UPDATE customers SET expiry_date = ?, status = 'active' WHERE id = ?
    """, (new_expiry_str, cust["id"]))
    
    conn.commit()
    conn.close()
    
    formatted_date = new_expiry.strftime("%d-%b-%Y %I:%M %p")
    return True, f"🎉 অভিনন্দন! আপনার {payment['amount']} টাকা জমা হয়েছে। আপনার ইন্টারনেট ঠিক ৩০ দিন অর্থাৎ {formatted_date} পর্যন্ত সচল থাকবে।"

def get_stats():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM customers")
    total_cust = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM customers WHERE status = 'active'")
    active_cust = cur.fetchone()[0]
    
    cur.execute("SELECT COUNT(*) FROM customers WHERE status = 'expired'")
    expired_cust = cur.fetchone()[0]
    
    cur.execute("SELECT SUM(amount) FROM payments WHERE status = 'used'")
    total_revenue = cur.fetchone()[0] or 0
    
    today_str = datetime.date.today().isoformat()
    cur.execute("SELECT SUM(amount) FROM payments WHERE status = 'used' AND used_at LIKE ?", (f"{today_str}%",))
    today_revenue = cur.fetchone()[0] or 0
    
    conn.close()
    return {
        "total": total_cust,
        "active": active_cust,
        "expired": expired_cust,
        "total_revenue": total_revenue,
        "today_revenue": today_revenue
    }
