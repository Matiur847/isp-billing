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
        name TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        package_name TEXT DEFAULT '25 Mbps',
        package_price INTEGER DEFAULT 525,
        expiry_date TEXT,
        status TEXT DEFAULT 'active'
    )
    """)
    
    # Try adding columns if existing database had older schema
    for col, col_type in [("name", "TEXT DEFAULT ''"), ("phone", "TEXT DEFAULT ''")]:
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
        
    # Pre-seed 21 users with 25 Mbps, 525 Tk if not already present
    default_users = [
        "Anisur97", "Azam55", "Babu85", "MR@obaidul", "MR@sarul", "MRF11", "MRF15J",
        "MRF107", "MRF117", "MRF141", "MRF200S", "Mamidul01", "Mizan", "Moynul",
        "Owab1234", "Primary-school", "Rafi81", "Romana005", "Sahinur-74", "Shofik1", "juyel1"
    ]
    next_month = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    for u in default_users:
        cur.execute("SELECT id FROM customers WHERE username = ?", (u,))
        row = cur.fetchone()
        if not row:
            cur.execute("""
            INSERT INTO customers (username, name, package_name, package_price, expiry_date, status)
            VALUES (?, ?, '25 Mbps', 525, ?, 'active')
            """, (u, u, next_month))
        else:
            # Update default package and price
            cur.execute("""
            UPDATE customers SET package_name = '25 Mbps', package_price = 525 WHERE username = ?
            """, (u,))
            
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
    # Search by exact username, partial name, or phone
    cur.execute("""
    SELECT * FROM customers 
    WHERE LOWER(username) = LOWER(?) 
       OR phone = ? 
       OR LOWER(name) LIKE LOWER(?)
    """, (query, query, f"%{query}%"))
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

def update_customer_details(cust_id, name, phone, package_price, expiry_date, status):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
    UPDATE customers 
    SET name = ?, phone = ?, package_price = ?, expiry_date = ?, status = ?
    WHERE id = ?
    """, (name.strip(), phone.strip(), int(package_price), expiry_date.strip(), status, cust_id))
    conn.commit()
    conn.close()

def verify_and_apply_payment(username_or_id, trx_id):
    trx_id = trx_id.strip().upper()
    conn = get_db()
    cur = conn.cursor()
    
    # Check customer by username or id
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
        
    # Calculate new expiry
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
