import re
import datetime
import logging
from typing import Optional
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
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

# 1. SMS Webhook Endpoint (Called by MacroDroid)
@app.post("/api/sms-webhook")
async def sms_webhook(request: Request):
    text = ""
    try:
        body = await request.json()
        if isinstance(body, dict):
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
async def customer_portal(query: Optional[str] = None, user_id: Optional[int] = None):
    bkash_num = database.get_setting("bkash_number", "01859734056")
    selected_customer = None
    search_results = []
    
    if user_id:
        selected_customer = database.get_customer_by_id(user_id)
    elif query:
        search_results = database.search_customers(query)
        if len(search_results) == 1:
            selected_customer = search_results[0]
            
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
            .bkash-badge {{ background-color: #e2136e; color: white; border-radius: 8px; padding: 8px 16px; font-weight: bold; letter-spacing: 0.5px; }}
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
                        <label class="form-label fw-bold">গ্রাহকের নাম, ইউজার আইডি অথবা মোবাইল নম্বর:</label>
                        <div class="input-group">
                            <input type="text" name="query" class="form-control form-control-lg" placeholder="যেমন: MRF15J বা নাম বা মোবাইল" value="{query or ''}" required>
                            <button class="btn btn-primary px-4 fw-bold" type="submit">সার্চ করুন</button>
                        </div>
                    </form>
    """
    
    if query and not search_results and not selected_customer:
        html += """
        <div class="alert alert-danger">
            ❌ এই নাম বা আইডিতে কোনো গ্রাহক পাওয়া যায়নি! সঠিক নাম বা ইউজার আইডি লিখুন অথবা অ্যাডমিনের সাথে যোগাযোগ করুন।
        </div>
        """
    elif len(search_results) > 1 and not selected_customer:
        html += """
        <div class="alert alert-info">
            🔍 একাধিক গ্রাহক পাওয়া গেছে! আপনার প্রোফাইলটি সিলেক্ট করুন:
        </div>
        <div class="list-group mb-4">
        """
        for c in search_results:
            display_name = c['name'] if c['name'] else c['username']
            html += f"""
            <a href="/?user_id={c['id']}" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center p-3">
                <div>
                    <h6 class="mb-1 fw-bold text-primary">{display_name}</h6>
                    <small class="text-muted">ID: {c['username']} | মোবাইল: {c['phone'] or 'নাই'}</small>
                </div>
                <span class="badge bg-primary rounded-pill">পেমেন্ট করুন ➔</span>
            </a>
            """
        html += "</div>"
        
    elif selected_customer:
        c = selected_customer
        status_badge = '<span class="badge bg-success">সচল (Active)</span>' if c['status'] == 'active' else '<span class="badge bg-danger">মেয়াদ শেষ (Expired)</span>'
        display_name = c['name'] if c['name'] else c['username']
        html += f"""
        <div class="alert alert-light border mb-4">
            <h5 class="fw-bold text-primary mb-2">👤 গ্রাহকের তথ্য:</h5>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>গ্রাহকের নাম:</span> <span class="fw-bold">{display_name}</span>
            </div>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>ইউজার আইডি:</span> <span class="fw-bold">{c['username']}</span>
            </div>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>প্যাকেজ:</span> <span class="fw-bold">{c['package_name']}</span>
            </div>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>মাসিক বিল:</span> <span class="fw-bold text-danger fs-5">{c['package_price']} ৳</span>
            </div>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>বর্তমান মেয়াদ:</span> <span class="fw-bold">{c['expiry_date']}</span>
            </div>
            <div class="d-flex justify-content-between py-1">
                <span>স্ট্যাটাস:</span> <span>{status_badge}</span>
            </div>
        </div>

        <div class="card bg-light border-0 p-3 mb-4">
            <h6 class="fw-bold text-dark mb-2">📱 বিকাশ পেমেন্ট নির্দেশিকা:</h6>
            <ol class="small ps-3 mb-2 text-secondary">
                <li>আপনার বিকাশ অ্যাপে গিয়ে <b>Send Money</b> অপশনে যান।</li>
                <li>নিচের পার্সোনাল বিকাশ নাম্বারে <b>{c['package_price']} টাকা</b> পাঠান:</li>
            </ol>
            <div class="text-center my-3">
                <span class="bkash-badge fs-5">bKash: {bkash_num} (Personal)</span>
            </div>
            <small class="text-muted text-center d-block">টাকা পাঠানোর পর ফিরতি SMS থেকে প্রাপ্ত <b>TrxID</b> নিচে লিখুন।</small>
        </div>

        <form method="POST" action="/api/pay">
            <input type="hidden" name="username" value="{c['username']}">
            <div class="mb-3">
                <label class="form-label fw-bold">বিকাশ TrxID দিন:</label>
                <input type="text" name="trx_id" class="form-control form-control-lg text-uppercase fw-bold" placeholder="যেমন: DKG1JU47Y7" required>
            </div>
            <button type="submit" class="btn btn-success btn-lg w-100 fw-bold">
                🚀 পেমেন্ট নিশ্চিত ও নেট চালু করুন
            </button>
        </form>
        """

    html += f"""
                </div>
            </div>
            <p class="text-center text-muted small">© ISP Automation System | যেকোনো প্রয়োজনে: {bkash_num}</p>
        </div>
    </body>
    </html>
    """
    return html

# 3. Process Payment & MikroTik Trigger
@app.post("/api/pay", response_class=HTMLResponse)
async def process_payment(background_tasks: BackgroundTasks, username: str = Form(...), trx_id: str = Form(...)):
    success, message = database.verify_and_apply_payment(username, trx_id)
    if success:
        background_tasks.add_task(unblock_in_mikrotik, username)
        alert_class = "alert-success"
        icon = "🎉"
        heading = "পেমেন্ট সফল হয়েছে!"
    else:
        alert_class = "alert-danger"
        icon = "⚠️"
        heading = "পেমেন্ট সম্পন্ন হয়নি!"
        
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
                <a href="/?query={username}" class="btn btn-primary mt-2 fw-bold">ফিরে যান</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html

# 4. Admin Management Dashboard
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(edit_id: Optional[int] = None):
    conn = database.get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM customers ORDER BY expiry_date ASC")
    customers = cur.fetchall()
    
    cur.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 20")
    payments = cur.fetchall()
    
    edit_cust = None
    if edit_id:
        cur.execute("SELECT * FROM customers WHERE id = ?", (edit_id,))
        edit_cust = cur.fetchone()
        
    conn.close()
    
    bkash_num = database.get_setting("bkash_number", "01859734056")
    
    # Edit customer modal/card if edit_id is selected
    edit_form_html = ""
    if edit_cust:
        edit_form_html = f"""
        <div class="card shadow-sm border-primary mb-4">
            <div class="card-header bg-primary text-white fw-bold">
                ✏️ গ্রাহকের তথ্য এডিট করুন: {edit_cust['username']}
            </div>
            <div class="card-body">
                <form method="POST" action="/admin/update-customer">
                    <input type="hidden" name="cust_id" value="{edit_cust['id']}">
                    <div class="row g-3">
                        <div class="col-md-3">
                            <label class="form-label fw-bold">ইউজার আইডি (PPPoE)</label>
                            <input type="text" class="form-control" value="{edit_cust['username']}" disabled>
                        </div>
                        <div class="col-md-3">
                            <label class="form-label fw-bold">গ্রাহকের আসল নাম</label>
                            <input type="text" name="name" class="form-control" value="{edit_cust['name'] or ''}" placeholder="যেমন: মোঃ রফিকুল ইসলাম">
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-bold">মোবাইল নম্বর</label>
                            <input type="text" name="phone" class="form-control" value="{edit_cust['phone'] or ''}" placeholder="017xxxxxxxx">
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-bold">মাসিক বিল (৳)</label>
                            <input type="number" name="package_price" class="form-control" value="{edit_cust['package_price']}">
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-bold">মেয়াদ (YYYY-MM-DD)</label>
                            <input type="date" name="expiry_date" class="form-control" value="{edit_cust['expiry_date']}">
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-bold">স্ট্যাটাস</label>
                            <select name="status" class="form-select">
                                <option value="active" {"selected" if edit_cust['status']=='active' else ''}>Active</option>
                                <option value="expired" {"selected" if edit_cust['status']=='expired' else ''}>Expired</option>
                            </select>
                        </div>
                    </div>
                    <div class="mt-3">
                        <button type="submit" class="btn btn-success fw-bold px-4">সংরক্ষণ করুন (Save)</button>
                        <a href="/admin" class="btn btn-secondary px-3">বাতিল</a>
                    </div>
                </form>
            </div>
        </div>
        """
        
    cust_rows = ""
    for c in customers:
        badge = "badge bg-success" if c["status"] == "active" else "badge bg-danger"
        display_name = c['name'] if c['name'] else '<span class="text-muted">নাম দেওয়া হয়নি</span>'
        cust_rows += f"""
        <tr>
            <td><b>{c['username']}</b></td>
            <td>{display_name}</td>
            <td>{c['phone'] or '-'}</td>
            <td>{c['package_price']} ৳</td>
            <td>{c['expiry_date']}</td>
            <td><span class="{badge}">{c['status']}</span></td>
            <td>
                <a href="/admin?edit_id={c['id']}" class="btn btn-sm btn-outline-primary">এডিট</a>
            </td>
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
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <title>ISP Admin Billing Dashboard</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light p-4">
        <div class="container-fluid">
            <div class="d-flex justify-content-between align-items-center mb-4">
                <h2>⚙️ ISP Billing Admin Dashboard</h2>
                <div>
                    <span class="badge bg-dark fs-6 me-2">বিকাশ পার্সোনাল: {bkash_num}</span>
                    <a href="/" target="_blank" class="btn btn-primary fw-bold">গ্রাহক পোর্টাল দেখুন ➔</a>
                </div>
            </div>
            
            {edit_form_html}
            
            <div class="row">
                <div class="col-md-8">
                    <div class="card shadow-sm mb-4">
                        <div class="card-header bg-primary text-white fw-bold d-flex justify-content-between">
                            <span>👥 গ্রাহক তালিকা ({len(customers)} জন)</span>
                            <small>গ্রাহকের আসল নাম ও আসল মেয়াদ সেট করতে 'এডিট' চাপুন</small>
                        </div>
                        <div class="card-body p-0">
                            <table class="table table-hover table-striped mb-0">
                                <thead>
                                    <tr><th>PPPoE ID</th><th>আসল নাম</th><th>মোবাইল</th><th>বিল</th><th>মেয়াদ</th><th>স্ট্যাটাস</th><th>অ্যাকশন</th></tr>
                                </thead>
                                <tbody>
                                    {cust_rows}
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
                
                <div class="col-md-4">
                    <div class="card shadow-sm mb-4">
                        <div class="card-header bg-dark text-white fw-bold">💰 সাম্প্রতিক বিকাশ SMS ও TrxID</div>
                        <div class="card-body p-0">
                            <table class="table table-sm table-hover mb-0">
                                <thead>
                                    <tr><th>TrxID</th><th>টাকা</th><th>নম্বর</th><th>স্ট্যাটাস</th><th>ইউজার</th></tr>
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

@app.post("/admin/update-customer")
async def update_customer(cust_id: int = Form(...), name: str = Form(""), phone: str = Form(""), 
                          package_price: int = Form(525), expiry_date: str = Form(...), status: str = Form("active")):
    database.update_customer_details(cust_id, name, phone, package_price, expiry_date, status)
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/api/cron/check-expiry")
async def trigger_expiry_check():
    check_expired_users_task()
    return {"status": "success", "message": "Expiry check executed"}
