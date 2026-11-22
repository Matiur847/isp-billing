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
    
    # Customers table
    cur.execute("""
    CREATE TABLE IF NOT EXISTS customers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        phone TEXT,
        package_name TEXT DEFAULT 'Standard',
        package_price INTEGER DEFAULT 500,
        expiry_date TEXT,
        status TEXT DEFAULT 'active'
    )
    """)
    
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
        "bkash_number": "01786821788",
        "admin_password": "admin",
        "mikrotik_host": "remote1.dslabbd.com",
        "mikrotik_port": "35105",
        "mikrotik_user": "admin",
        "mikrotik_pass": "matiur847@#"
    }
    for k, v in default_settings.items():
        cur.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
        
    # Pre-seed 21 users with expiry set to 30 days from now if table empty
    cur.execute("SELECT COUNT(*) FROM customers")
    if cur.fetchone()[0] == 0:
        default_users = [
            "Anisur97", "Azam55", "Babu85", "MR@obaidul", "MR@sarul", "MRF11", "MRF15J",
            "MRF107", "MRF117", "MRF141", "MRF200S", "Mamidul01", "Mizan", "Moynul",
            "Owab1234", "Primary-school", "Rafi81", "Romana005", "Sahinur-74", "Shofik1", "juyel1"
        ]
        next_month = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
        for u in default_users:
            cur.execute("""
            INSERT INTO customers (username, package_name, package_price, expiry_date, status)
            VALUES (?, '10 Mbps', 500, ?, 'active')
            """, (u, next_month))
            
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

def get_customer(query):
    query = query.strip()
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers WHERE LOWER(username) = LOWER(?) OR phone = ?", (query, query))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def verify_and_apply_payment(username_or_phone, trx_id):
    trx_id = trx_id.strip().upper()
    conn = get_db()
    cur = conn.cursor()
    
    # 1. Check customer
    cur.execute("SELECT * FROM customers WHERE LOWER(username) = LOWER(?) OR phone = ?", (username_or_phone.strip(), username_or_phone.strip()))
    cust = cur.fetchone()
    if not cust:
        conn.close()
        return False, "কাস্টমার পাওয়া যায়নি! সঠিক ইউজারনেম বা ফোন নাম্বার দিন।"
        
    cust = dict(cust)
    
    # 2. Check TrxID
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
        return False, f"টাকার পরিমাণ কম! আপনার প্যাকেজের মূল্য {cust['package_price']} টাকা, কিন্তু পেমেন্ট এসেছে {payment['amount']} টাকা।"
        
    # 3. Calculate new expiry
    today = datetime.date.today()
    try:
        curr_expiry = datetime.date.fromisoformat(cust["expiry_date"])
    except Exception:
        curr_expiry = today
        
    if curr_expiry >= today:
        new_expiry = curr_expiry + datetime.timedelta(days=30)
    else:
        new_expiry = today + datetime.timedelta(days=30)
        
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # Update payment
    cur.execute("""
    UPDATE payments SET status = 'used', used_by = ?, used_at = ? WHERE id = ?
    """, (cust["username"], now, payment["id"]))
    
    # Update customer
    cur.execute("""
    UPDATE customers SET expiry_date = ?, status = 'active' WHERE id = ?
    """, (new_expiry.isoformat(), cust["id"]))
    
    conn.commit()
    conn.close()
    
    return True, f"অভিনন্দন! আপনার {payment['amount']} টাকা জমা হয়েছে। মেয়াদ {new_expiry.strftime('%d-%b-%Y')} পর্যন্ত বাড়ানো হয়েছে।"
