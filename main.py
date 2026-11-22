import re
import datetime
import logging
from typing import Optional
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

import database
from mikrotik import MikrotikAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("billing_app")

def unblock_in_mikrotik(username: str):
    try:
        host = database.get_setting("mikrotik_host")
        port = int(database.get_setting("mikrotik_port", 35105))
        user = database.get_setting("mikrotik_user")
        pwd = database.get_setting("mikrotik_pass")
        
        api = MikrotikAPI(host, port, user, pwd)
        api.connect()
        ip = api.unblock_user_by_name(username)
        api.close()
        logger.info(f"MikroTik unblock executed for {username} (IP: {ip})")
    except Exception as e:
        logger.error(f"Failed to unblock {username} in MikroTik: {e}")

def check_expired_users_task():
    try:
        conn = database.get_db()
        cur = conn.cursor()
        today = datetime.date.today().isoformat()
        cur.execute("SELECT username FROM customers WHERE expiry_date < ? AND status = 'active'", (today,))
        expired = cur.fetchall()
        
        if expired:
            host = database.get_setting("mikrotik_host")
            port = int(database.get_setting("mikrotik_port", 35105))
            user = database.get_setting("mikrotik_user")
            pwd = database.get_setting("mikrotik_pass")
            
            api = MikrotikAPI(host, port, user, pwd)
            api.connect()
            actives = api.get_active_pppoe()
            
            for row in expired:
                uname = row["username"]
                cur.execute("UPDATE customers SET status = 'expired' WHERE username = ?", (uname,))
                if uname in actives:
                    ip = actives[uname]
                    api.block_user(ip, comment=uname)
                    logger.info(f"Blocked expired user: {uname} ({ip})")
                    
            conn.commit()
            api.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error in check_expired_users_task: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    logger.info("Database initialized.")
    yield

app = FastAPI(title="ISP Billing & bKash Automation", lifespan=lifespan)

# Helper regex to parse bKash Send Money SMS
def parse_bkash_sms(text: str):
    # Example: "You have received Tk 525.00 from 01786821788. Fee Tk 0.00. Balance Tk 1,033.02. TrxID DKG1JU47Y7 at 16/11/2026 15:43"
    amount_match = re.search(r'received\s+Tk\s*([0-9,]+(?:\.[0-9]{1,2})?)', text, re.IGNORECASE)
    trx_match = re.search(r'TrxID\s+([A-Za-z0-9]+)', text, re.IGNORECASE)
    sender_match = re.search(r'from\s+(01[3-9][0-9]{8})', text, re.IGNORECASE)
    
    if trx_match and amount_match:
        trx_id = trx_match.group(1).upper()
        amount_str = amount_match.group(1).replace(",", "")
        amount = float(amount_str)
        sender = sender_match.group(1) if sender_match else "Unknown"
        return trx_id, amount, sender
    return None, None, None

# 1. SMS Webhook Endpoint (Called by Android SMS Forwarder)
@app.post("/api/sms-webhook")
async def sms_webhook(request: Request):
    # Support both JSON and Form payload from various SMS Forwarder apps
    text = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
            # Check common keys: content, msg, message, text, body
            text = body.get("content") or body.get("msg") or body.get("message") or body.get("text") or body.get("body") or str(body)
        else:
            text = str(body)
    except Exception:
        body_bytes = await request.body()
        text = body_bytes.decode("utf-8", errors="replace")
        
    logger.info(f"Incoming SMS Webhook: {text}")
    
    trx_id, amount, sender = parse_bkash_sms(text)
    if trx_id and amount:
        success = database.record_payment(trx_id, amount, sender, text)
        if success:
            logger.info(f"Successfully recorded bKash payment: TrxID={trx_id}, Amount={amount}, Sender={sender}")
            return {"status": "success", "message": "Payment recorded", "trx_id": trx_id, "amount": amount}
        else:
            return {"status": "duplicate", "message": "TrxID already exists"}
            
    return {"status": "ignored", "message": "Not a recognized bKash Send Money SMS"}

# 2. Customer Portal UI
@app.get("/", response_class=HTMLResponse)
async def customer_portal(user: Optional[str] = None):
    bkash_num = database.get_setting("bkash_number", "01786821788")
    cust_info = database.get_customer(user) if user else None
    
    html = f"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>ইন্টারনেট বিল পরিশোধ ও রিচার্জ পোর্টাল</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background: #f0f4f8; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }}
            .card {{ border-radius: 15px; box-shadow: 0 8px 24px rgba(0,0,0,0.08); border: none; }}
            .bkash-badge {{ background-color: #e2136e; color: white; border-radius: 8px; padding: 6px 14px; font-weight: bold; }}
            .header-banner {{ background: linear-gradient(135deg, #0d6efd, #0b5ed7); color: white; padding: 25px; border-radius: 15px 15px 0 0; text-align: center; }}
        </style>
    </head>
    <body class="py-4">
        <div class="container" style="max-width: 550px;">
            <div class="card mb-4">
                <div class="header-banner">
                    <h3 class="mb-1">⚡ ডিজিটাল ইন্টারনেট বিলিং</h3>
                    <p class="mb-0 opacity-75">বিকাশ দিয়ে অটোমেটিক ইনস্ট্যান্ট রিচার্জ</p>
                </div>
                
                <div class="card-body p-4">
                    <form method="GET" action="/" class="mb-3">
                        <label class="form-label fw-bold">আপনার ইউজার আইডি (PPPoE Username):</label>
                        <div class="input-group">
                            <input type="text" name="user" class="form-control form-control-lg" placeholder="যেমন: MRF107" value="{user or ''}" required>
                            <button class="btn btn-primary px-4" type="submit">সার্চ করুন</button>
                        </div>
                    </form>
    """
    
    if user and not cust_info:
        html += """
        <div class="alert alert-danger">
            ❌ এই ইউজার আইডিটি পাওয়া যায়নি! দয়া করে সঠিক ইউজার আইডি দিন অথবা অ্যাডমিনের সাথে যোগাযোগ করুন।
        </div>
        """
    elif cust_info:
        status_badge = '<span class="badge bg-success">সচল (Active)</span>' if cust_info['status'] == 'active' else '<span class="badge bg-danger">মেয়াদ শেষ (Expired)</span>'
        html += f"""
        <div class="alert alert-light border mb-4">
            <h5 class="fw-bold text-primary mb-2">👤 গ্রাহকের তথ্য:</h5>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>ইউজার আইডি:</span> <span class="fw-bold">{cust_info['username']}</span>
            </div>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>প্যাকেজ:</span> <span class="fw-bold">{cust_info['package_name']}</span>
            </div>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>মাসিক বিল:</span> <span class="fw-bold text-danger fs-5">{cust_info['package_price']} ৳</span>
            </div>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>বর্তমান মেয়াদ:</span> <span class="fw-bold">{cust_info['expiry_date']}</span>
            </div>
            <div class="d-flex justify-content-between py-1">
                <span>স্ট্যাটাস:</span> <span>{status_badge}</span>
            </div>
        </div>

        <div class="card bg-light border-0 p-3 mb-4">
            <h6 class="fw-bold text-dark mb-2">📱 বিকাশ পেমেন্ট নির্দেশিকা:</h6>
            <ol class="small ps-3 mb-2 text-secondary">
                <li>আপনার বিকাশ অ্যাপে গিয়ে <b>Send Money</b> অপশনে যান।</li>
                <li>নিচের নাম্বারে <b>{cust_info['package_price']} টাকা</b> পাঠান:</li>
            </ol>
            <div class="text-center my-2">
                <span class="bkash-badge fs-5">bKash: {bkash_num} (Personal)</span>
            </div>
            <small class="text-muted text-center d-block">টাকা পাঠানোর পর বিকাশ থেকে পাওয়া <b>TrxID</b> নিচে লিখুন।</small>
        </div>

        <form method="POST" action="/api/pay">
            <input type="hidden" name="username" value="{cust_info['username']}">
            <div class="mb-3">
                <label class="form-label fw-bold">বিকাশ TrxID দিন:</label>
                <input type="text" name="trx_id" class="form-control form-control-lg text-uppercase" placeholder="যেমন: DKG1JU47Y7" required>
            </div>
            <button type="submit" class="btn btn-success btn-lg w-100 fw-bold">
                🚀 পেমেন্ট নিশ্চিত ও নেট চালু করুন
            </button>
        </form>
        """

    html += """
                </div>
            </div>
            <p class="text-center text-muted small">© ISP Automation System | যেকোনো সমস্যায় যোগাযোগ: 01786821788</p>
        </div>
    </body>
    </html>
    """
    return html

# 3. Process Customer Payment & Instantly Trigger MikroTik API
@app.post("/api/pay", response_class=HTMLResponse)
async def process_payment(background_tasks: BackgroundTasks, username: str = Form(...), trx_id: str = Form(...)):
    success, message = database.verify_and_apply_payment(username, trx_id)
    if success:
        # Trigger MikroTik unblock in background task
        background_tasks.add_task(unblock_in_mikrotik, username)
        alert_class = "alert-success"
        icon = "🎉"
        heading = "পেমেন্ট সফল হয়েছে!"
    else:
        alert_class = "alert-danger"
        icon = "⚠️"
        heading = "পেমেন্ট ব্যর্থ হয়েছে!"
        
    html = f"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>পেমেন্ট স্ট্যাটাস</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light py-5">
        <div class="container text-center" style="max-width: 500px;">
            <div class="card p-4 shadow-sm border-0">
                <div class="display-3 mb-3">{icon}</div>
                <h3 class="fw-bold mb-3">{heading}</h3>
                <div class="alert {alert_class} text-start">
                    {message}
                </div>
                <a href="/?user={username}" class="btn btn-primary mt-2">ফিরে যান</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html

# 4. Admin Dashboard
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    conn = database.get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers ORDER BY expiry_date ASC")
    customers = cur.fetchall()
    
    cur.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 20")
    payments = cur.fetchall()
    conn.close()
    
    cust_rows = ""
    for c in customers:
        badge = "badge bg-success" if c["status"] == "active" else "badge bg-danger"
        cust_rows += f"""
        <tr>
            <td>{c['username']}</td>
            <td>{c['phone'] or '-'}</td>
            <td>{c['package_price']} ৳</td>
            <td>{c['expiry_date']}</td>
            <td><span class="{badge}">{c['status']}</span></td>
        </tr>
        """
        
    pay_rows = ""
    for p in payments:
        used_badge = '<span class="badge bg-secondary">Used</span>' if p["status"] == "used" else '<span class="badge bg-success">Unused</span>'
        pay_rows += f"""
        <tr>
            <td><b>{p['trx_id']}</b></td>
            <td>{p['amount']} ৳</td>
            <td>{p['sender_phone']}</td>
            <td>{p['received_at']}</td>
            <td>{used_badge}</td>
            <td>{p['used_by'] or '-'}</td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>ISP Admin Billing Dashboard</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light p-4">
        <div class="container-fluid">
            <h2 class="mb-4">⚙️ ISP Billing Admin Dashboard</h2>
            
            <div class="row">
                <div class="col-md-7">
                    <div class="card shadow-sm mb-4">
                        <div class="card-header bg-primary text-white fw-bold">👥 গ্রাহক তালিকা ({len(customers)} Users)</div>
                        <div class="card-body p-0">
                            <table class="table table-hover mb-0">
                                <thead>
                                    <tr><th>Username</th><th>Phone</th><th>Price</th><th>Expiry</th><th>Status</th></tr>
                                </thead>
                                <tbody>
                                    {cust_rows}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
                
                <div class="col-md-5">
                    <div class="card shadow-sm mb-4">
                        <div class="card-header bg-dark text-white fw-bold">💰 সাম্প্রতিক বিকাশ পেমেন্ট (Last 20)</div>
                        <div class="card-body p-0">
                            <table class="table table-sm table-hover mb-0">
                                <thead>
                                    <tr><th>TrxID</th><th>টাকা</th><th>নম্বর</th><th>সময়</th><th>স্ট্যাটাস</th><th>ব্যবহৃত</th></tr>
                                </thead>
                                <tbody>
                                    {pay_rows}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return html

# 5. Daily Expiry Check Trigger
@app.get("/api/cron/check-expiry")
async def trigger_expiry_check():
    check_expired_users_task()
    return {"status": "success", "message": "Expiry check executed"}
