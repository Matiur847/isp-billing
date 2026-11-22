import re
import datetime
import logging
from typing import Optional
from fastapi import FastAPI, Request, Form, BackgroundTasks, Depends, HTTPException, status, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.security import APIKeyCookie
from contextlib import asynccontextmanager

import database
from mikrotik import MikrotikAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("billing_app")

COMPANY_NAME = "সাইফুল ডিস এন্ড ইন্টারনেট 🛜"
COOKIE_NAME = "admin_session"
DEFAULT_ADMIN_PASS = "matiur847"

def get_mikrotik_conn():
    host = database.get_setting("mikrotik_host")
    port = int(database.get_setting("mikrotik_port", 35105))
    user = database.get_setting("mikrotik_user")
    pwd = database.get_setting("mikrotik_pass")
    api = MikrotikAPI(host, port, user, pwd)
    api.connect()
    return api

def unblock_in_mikrotik(username: str):
    try:
        api = get_mikrotik_conn()
        ip = api.unblock_user_by_name(username)
        api.close()
        logger.info(f"MikroTik unblock executed for {username} (IP: {ip})")
    except Exception as e:
        logger.error(f"Failed to unblock {username} in MikroTik: {e}")

def block_in_mikrotik(username: str):
    try:
        api = get_mikrotik_conn()
        actives = api.get_active_pppoe()
        if username in actives:
            ip = actives[username]["address"]
            api.block_user(ip, comment=username)
            logger.info(f"Blocked user {username} ({ip})")
        api.close()
    except Exception as e:
        logger.error(f"Failed to block {username} in MikroTik: {e}")

def sync_secret_to_mikrotik(username, password, profile="default"):
    try:
        api = get_mikrotik_conn()
        api.create_pppoe_secret(username, password, profile=profile, comment="Created from Web Portal")
        api.close()
        logger.info(f"Synced secret {username} to MikroTik")
    except Exception as e:
        logger.error(f"Failed to sync secret {username} to MikroTik: {e}")

def delete_secret_from_mikrotik(username):
    try:
        api = get_mikrotik_conn()
        api.delete_pppoe_secret(username)
        api.close()
        logger.info(f"Deleted secret {username} from MikroTik")
    except Exception as e:
        logger.error(f"Failed to delete secret {username} in MikroTik: {e}")

def check_expired_users_task():
    try:
        conn = database.get_db()
        cur = conn.cursor()
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cur.execute("SELECT username FROM customers WHERE expiry_date < ? AND status = 'active'", (now_str,))
        expired = cur.fetchall()
        
        if expired:
            api = get_mikrotik_conn()
            actives = api.get_active_pppoe()
            for row in expired:
                uname = row["username"]
                cur.execute("UPDATE customers SET status = 'expired' WHERE username = ?", (uname,))
                if uname in actives:
                    ip = actives[uname]["address"]
                    api.block_user(ip, comment=uname)
                    logger.info(f"Auto-Blocked expired user: {uname} ({ip})")
            conn.commit()
            api.close()
        conn.close()
    except Exception as e:
        logger.error(f"Error in check_expired_users_task: {e}")

# Helper for Time Left / Expired pill calculation
def calculate_expiry_label(expiry_str: str, status_str: str):
    now = datetime.datetime.now()
    try:
        if len(expiry_str) == 10:
            exp_dt = datetime.datetime.strptime(expiry_str, "%Y-%m-%d")
        else:
            exp_dt = datetime.datetime.strptime(expiry_str[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return "Expired", "bg-danger text-white", "Active", "bg-success text-white"

    if status_str != "active" or exp_dt < now:
        diff = now - exp_dt
        days = diff.days
        hours = diff.seconds // 3600
        label = f"Expired {days} দিন" if days > 0 else f"Expired {hours} ঘণ্টা"
        return label, "bg-danger-subtle text-danger border border-danger-subtle", "Suspended", "bg-warning-subtle text-warning-emphasis border border-warning-subtle"
    else:
        diff = exp_dt - now
        days = diff.days
        hours = diff.seconds // 3600
        label = f"{days} দিন left" if days > 0 else f"{hours} ঘণ্টা left"
        return label, "bg-success-subtle text-success border border-success-subtle", "Active", "bg-success-subtle text-success border border-success-subtle"

@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    # Set default admin pass if not set
    if not database.get_setting("admin_password"):
        database.set_setting("admin_password", DEFAULT_ADMIN_PASS)
    logger.info("Database initialized.")
    yield

app = FastAPI(title="ISP Billing & bKash Automation", lifespan=lifespan)

# Helper regex to parse bKash Send Money SMS
def parse_bkash_sms(text: str):
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

# Real-time Auto-Suggest Search API
@app.get("/api/search")
async def api_search(q: str = ""):
    results = database.search_customers(q, limit=8)
    return results

# Live Speed API for MikroTik
@app.get("/api/live-speed/{username}")
async def api_live_speed(username: str):
    try:
        api = get_mikrotik_conn()
        speed = api.get_live_speed(username)
        api.close()
        return speed
    except Exception as e:
        return {"rx_mbps": 0.0, "tx_mbps": 0.0, "online": False, "error": str(e)}

# SMS Webhook Endpoint
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
            logger.info(f"Recorded bKash payment: TrxID={trx_id}, Amount={amount}, Sender={sender}")
            return {"status": "success", "message": "Payment recorded", "trx_id": trx_id, "amount": amount}
        else:
            return {"status": "duplicate", "message": "TrxID already exists"}
            
    return {"status": "ignored", "message": "Not a recognized bKash Send Money SMS"}

# --- AUTHENTICATION & LOGIN ---
def is_authenticated(request: Request) -> bool:
    session_val = request.cookies.get(COOKIE_NAME)
    admin_pass = database.get_setting("admin_password", DEFAULT_ADMIN_PASS)
    return session_val == admin_pass

@app.get("/login", response_class=HTMLResponse)
async def login_page(error: Optional[str] = None):
    html = f"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{COMPANY_NAME} - অ্যাডমিন লগইন</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            body {{ background: #f0f4f9; height: 100vh; display: flex; align-items: center; justify-content: center; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }}
            .card {{ border-radius: 18px; border: none; box-shadow: 0 10px 30px rgba(0,0,0,0.08); width: 100%; max-width: 400px; }}
            .header-banner {{ background: linear-gradient(135deg, #0d6efd, #004ecc); color: white; padding: 25px; border-radius: 18px 18px 0 0; text-align: center; }}
        </style>
    </head>
    <body>
        <div class="card">
            <div class="header-banner">
                <i class="fa-solid fa-shield-halved fa-3x mb-2"></i>
                <h4 class="fw-bold mb-1">{COMPANY_NAME}</h4>
                <p class="mb-0 opacity-75 small">সুরক্ষিত অ্যাডমিন প্যানেল</p>
            </div>
            <div class="card-body p-4">
                {"<div class='alert alert-danger py-2 small'>" + error + "</div>" if error else ""}
                <form method="POST" action="/login">
                    <div class="mb-3">
                        <label class="form-label fw-bold">অ্যাডমিন পাসওয়ার্ড:</label>
                        <div class="input-group">
                            <span class="input-group-text"><i class="fa-solid fa-lock"></i></span>
                            <input type="password" name="password" class="form-control form-control-lg" placeholder="পাসওয়ার্ড লিখুন" required autofocus>
                        </div>
                    </div>
                    <button type="submit" class="btn btn-primary btn-lg w-100 fw-bold">
                        <i class="fa-solid fa-right-to-bracket me-2"></i> লগইন করুন
                    </button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """
    return html

@app.post("/login")
async def do_login(password: str = Form(...)):
    admin_pass = database.get_setting("admin_password", DEFAULT_ADMIN_PASS)
    if password == admin_pass:
        response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key=COOKIE_NAME, value=admin_pass, max_age=86400*30, httponly=True)
        return response
    return RedirectResponse(url="/login?error=ভুল+পাসওয়ার্ড!+আবার+চেষ্টা+করুন।", status_code=status.HTTP_303_SEE_OTHER)

@app.get("/logout")
async def do_logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response

# Customer Portal UI with Live Dropdown Search
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
        <title>{COMPANY_NAME} - বিল পরিশোধ পোর্টাল</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            body {{ background: #f0f4f9; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }}
            .card {{ border-radius: 18px; box-shadow: 0 12px 35px rgba(0,0,0,0.07); border: none; }}
            .bkash-badge {{ background-color: #e2136e; color: white; border-radius: 12px; padding: 12px 22px; font-weight: bold; letter-spacing: 0.8px; display: inline-block; }}
            .header-banner {{ background: linear-gradient(135deg, #0d6efd, #004ecc); color: white; padding: 30px 20px; border-radius: 18px 18px 0 0; text-align: center; }}
            .dropdown-item-hover:hover {{ background-color: #eef5ff; cursor: pointer; }}
        </style>
    </head>
    <body class="py-4">
        <div class="container" style="max-width: 530px;">
            <div class="card mb-4">
                <div class="header-banner">
                    <h3 class="mb-1 fw-bold">{COMPANY_NAME}</h3>
                    <p class="mb-0 opacity-75 fs-6">বিকাশ দিয়ে অটোমেটিক ইনস্ট্যান্ট রিচার্জ</p>
                </div>
                
                <div class="card-body p-4">
                    <form method="GET" action="/" class="mb-3 position-relative" autocomplete="off">
                        <label class="form-label fw-bold">গ্রাহকের নাম, ইউজার আইডি বা মোবাইল নম্বর:</label>
                        <div class="input-group">
                            <input type="text" id="searchInput" name="query" class="form-control form-control-lg" placeholder="যেমন: anisur বা azam বা 017..." value="{query or ''}" required>
                            <button class="btn btn-primary px-4 fw-bold" type="submit"><i class="fa-solid fa-magnifying-glass me-1"></i> সার্চ</button>
                        </div>
                        <div id="searchResults" class="list-group shadow position-absolute w-100 mt-1 d-none" style="z-index: 1050; max-height: 280px; overflow-y: auto;"></div>
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
                    <small class="text-muted">আইডি: {c['username']} | মোবাইল: {c['phone'] or 'নাই'}</small>
                </div>
                <span class="badge bg-primary rounded-pill">পেমেন্ট ➔</span>
            </a>
            """
        html += "</div>"
        
    elif selected_customer:
        c = selected_customer
        status_badge = '<span class="badge bg-success">সচল (Active)</span>' if c['status'] == 'active' else '<span class="badge bg-danger">মেয়াদ শেষ (Expired)</span>'
        display_name = c['name'] if c['name'] else c['username']
        html += f"""
        <div class="alert alert-light border mb-4">
            <h5 class="fw-bold text-primary mb-3"><i class="fa-solid fa-id-card me-2"></i>গ্রাহকের তথ্য:</h5>
            <div class="d-flex justify-content-between py-1 border-bottom">
                <span>গ্রাহকের নাম:</span> <span class="fw-bold fs-6">{display_name}</span>
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
                <span>বর্তমান মেয়াদ:</span> <span class="fw-bold text-dark">{c['expiry_date']}</span>
            </div>
            <div class="d-flex justify-content-between py-1">
                <span>স্ট্যাটাস:</span> <span>{status_badge}</span>
            </div>
        </div>

        <div class="card bg-light border-0 p-3 mb-4">
            <h6 class="fw-bold text-dark mb-2"><i class="fa-solid fa-mobile-screen me-1 text-danger"></i> বিকাশ পেমেন্ট নির্দেশিকা:</h6>
            <ol class="small ps-3 mb-2 text-secondary">
                <li>আপনার বিকাশ অ্যাপে গিয়ে <b>Send Money</b> অপশনে যান।</li>
                <li>নিচের পার্সোনাল বিকাশ নাম্বারে ঠিক <b>{c['package_price']} টাকা</b> পাঠান:</li>
            </ol>
            <div class="text-center my-3">
                <div class="d-inline-flex align-items-center gap-2">
                    <span class="bkash-badge fs-5" id="bkashNum">bKash: {bkash_num}</span>
                    <button onclick="copyBkash('{bkash_num}')" class="btn btn-sm btn-light border shadow-sm px-2" title="নম্বর কপি করুন" id="copyBtn" style="border-radius: 8px;">
                        <i class="fa-regular fa-copy text-danger"></i>
                    </button>
                </div>
                <div class="mt-1"><small class="text-muted opacity-75">Personal Number</small></div>
            </div>
            <small class="text-muted text-center d-block">টাকা পাঠানোর পর বিকাশ থেকে প্রাপ্ত ফিরতি এসএমএসের <b>TrxID</b> নিচে লিখুন।</small>
        </div>

        <form method="POST" action="/api/pay">
            <input type="hidden" name="username" value="{c['username']}">
            <div class="mb-3">
                <label class="form-label fw-bold">বিকাশ TrxID দিন:</label>
                <input type="text" name="trx_id" class="form-control form-control-lg text-uppercase fw-bold" placeholder="যেমন: DKG1JU47Y7" required>
            </div>
            <button type="submit" class="btn btn-success btn-lg w-100 fw-bold">
                <i class="fa-solid fa-paper-plane me-1"></i> পেমেন্ট নিশ্চিত ও নেট চালু করুন
            </button>
        </form>
        """

    html += f"""
                </div>
            </div>
            <p class="text-center text-muted small">© {COMPANY_NAME} | হেল্পলাইন: {bkash_num}</p>
        </div>

        <script>
            function copyBkash(number) {{
                navigator.clipboard.writeText(number).then(() => {{
                    const btn = document.getElementById('copyBtn');
                    btn.innerHTML = '<i class="fa-solid fa-check text-success"></i>';
                    btn.classList.add('btn-success');
                    btn.classList.remove('btn-light');
                    setTimeout(() => {{
                        btn.innerHTML = '<i class="fa-regular fa-copy text-danger"></i>';
                        btn.classList.remove('btn-success');
                        btn.classList.add('btn-light');
                    }}, 2000);
                }}).catch(() => {{
                    // Fallback for older browsers
                    const el = document.createElement('textarea');
                    el.value = number;
                    document.body.appendChild(el);
                    el.select();
                    document.execCommand('copy');
                    document.body.removeChild(el);
                    alert('কপি হয়েছে: ' + number);
                }});
            }}

            const searchInput = document.getElementById('searchInput');
            const searchResults = document.getElementById('searchResults');
            if (searchInput && searchResults) {{
                searchInput.addEventListener('input', async function() {{
                    const q = this.value.trim();
                    if (q.length < 1) {{
                        searchResults.classList.add('d-none');
                        searchResults.innerHTML = '';
                        return;
                    }}
                    try {{
                        const res = await fetch('/api/search?q=' + encodeURIComponent(q));
                        const data = await res.json();
                        if (data.length === 0) {{
                            searchResults.innerHTML = '<div class="list-group-item text-muted small p-3">কোনো মিল পাওয়া যায়নি</div>';
                            searchResults.classList.remove('d-none');
                            return;
                        }}
                        let html = '';
                        data.forEach(item => {{
                            const name = item.name || item.username;
                            html += `
                            <a href="/?user_id=${{item.id}}" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center p-3 dropdown-item-hover">
                                <div>
                                    <div class="fw-bold text-primary">${{name}}</div>
                                    <small class="text-muted">আইডি: ${{item.username}} ${{item.phone ? '| মোবাইল: ' + item.phone : ''}}</small>
                                </div>
                                <span class="badge bg-primary rounded-pill">সিলেক্ট করুন ➔</span>
                            </a>
                            `;
                        }});
                        searchResults.innerHTML = html;
                        searchResults.classList.remove('d-none');
                    }} catch (e) {{
                        console.error(e);
                    }}
                }});

                document.addEventListener('click', function(e) {{
                    if (!searchInput.contains(e.target) && !searchResults.contains(e.target)) {{
                        searchResults.classList.add('d-none');
                    }}
                }});
            }}
        </script>
    </body>
    </html>
    """
    return html

# Process Customer Payment
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
        <title>{COMPANY_NAME} - পেমেন্ট স্ট্যাটাস</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light py-5">
        <div class="container text-center" style="max-width: 500px;">
            <div class="card p-4 shadow-sm border-0">
                <div class="display-3 mb-3">{icon}</div>
                <h3 class="fw-bold mb-3">{heading}</h3>
                <div class="alert {alert_class} text-start fs-6">
                    {message}
                </div>
                <a href="/?query={username}" class="btn btn-primary mt-2 fw-bold">ফিরে যান</a>
            </div>
        </div>
    </body>
    </html>
    """
    return html

# --- ADMIN DASHBOARD (EXACT REPLICA OF USER'S REFERENCE IMAGES) ---
@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request, edit_id: Optional[int] = None):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
        
    customers = database.get_all_customers()
    stats = database.get_stats()
    bkash_num = database.get_setting("bkash_number", "01859734056")
    
    conn = database.get_db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM payments ORDER BY id DESC LIMIT 20")
    payments = [dict(r) for r in cur.fetchall()]
    
    edit_cust = None
    if edit_id:
        cur.execute("SELECT * FROM customers WHERE id = ?", (edit_id,))
        row = cur.fetchone()
        if row:
            edit_cust = dict(row)
    conn.close()
    
    mikrotik_status_badge = '<span class="badge bg-success"><i class="fa-solid fa-circle-check me-1"></i> MikroTik Online</span>'
    actives = {}
    try:
        api = get_mikrotik_conn()
        actives = api.get_active_pppoe()
        api.close()
    except Exception:
        mikrotik_status_badge = '<span class="badge bg-danger"><i class="fa-solid fa-triangle-exclamation me-1"></i> MikroTik Offline</span>'

    # Build customer rows matching Image 1
    cust_rows_html = ""
    for c in customers:
        uname = c["username"]
        is_online = uname in actives
        act_info = actives.get(uname, {})
        ip = act_info.get("address", "192.168.20.x")
        uptime = act_info.get("uptime", "Offline")
        mac = act_info.get("caller_id", "DC:8E:8D:0D:B8:82")
        
        online_badge = f'<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill px-2">● Online</span> <span class="text-secondary small fw-bold">{uptime}</span> <a href="javascript:void(0)" onclick="openLiveSpeed(\'{uname}\', \'{c["name"] or uname}\')" class="badge bg-info-subtle text-primary border border-info-subtle text-decoration-none"><i class="fa-solid fa-chart-line"></i> Live</a>' if is_online else '<span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle rounded-pill px-2">○ Offline</span>'
        
        time_left_label, time_left_class, status_label, status_class = calculate_expiry_label(c["expiry_date"], c["status"])
        display_name = c["name"] if c["name"] else uname
        
        # Format expiry date display e.g. Nov 24 2026 20:06
        try:
            exp_formatted = datetime.datetime.strptime(c["expiry_date"][:19], "%Y-%m-%d %H:%M:%S").strftime("%b %d %Y %H:%M")
        except Exception:
            exp_formatted = c["expiry_date"]
            
        cust_rows_html += f"""
        <div class="card mb-2 p-2 border customer-card" data-search="{uname.lower()} {display_name.lower()} {c['phone']}">
            <div class="row align-items-center g-2">
                <!-- Col 1: Select Checkbox -->
                <div class="col-auto">
                    <input type="checkbox" class="form-check-input rounded-circle p-2">
                </div>
                
                <!-- Col 2: Online/Offline, Live Speed, IP, MAC -->
                <div class="col-12 col-md-3">
                    <div class="d-flex align-items-center gap-1 mb-1">
                        {online_badge}
                    </div>
                    <div class="small text-secondary mb-1">
                        <i class="fa-solid fa-arrow-down-up-across-line text-primary"></i> <b>22.64 GB</b> &nbsp;
                        <i class="fa-solid fa-network-wired text-muted"></i> <b>{ip}</b>
                    </div>
                    <div class="small text-muted">
                        <i class="fa-solid fa-fingerprint"></i> <span class="text-secondary">{mac}</span> &nbsp;
                        <span class="badge bg-light text-primary border"><i class="fa-solid fa-lock"></i> Bind</span>
                    </div>
                </div>
                
                <!-- Col 3: Customer ID, Mobile, Name, Username, Password -->
                <div class="col-12 col-md-3">
                    <div class="text-primary fw-bold small">#{c['id'] + 40500}</div>
                    <div class="text-primary small mb-1"><i class="fa-solid fa-phone"></i> {c['phone'] if c['phone'] else '<span class="text-muted">No Mobile</span>'}</div>
                    <div class="fw-bold fs-6 text-dark">{display_name}</div>
                    <div class="small text-muted">
                        <i class="fa-solid fa-user text-primary"></i> {uname} &nbsp;
                        <i class="fa-solid fa-key text-muted"></i> {c['password']}
                    </div>
                </div>

                <!-- Col 4: Server / Balance -->
                <div class="col-6 col-md-2">
                    <div class="small text-secondary"><i class="fa-solid fa-server"></i> {c['profile']}</div>
                    <div class="small text-secondary mt-1"><i class="fa-solid fa-scale-balanced"></i> Balance: <b>0.00 BDT</b></div>
                </div>

                <!-- Col 5: Badges, Package, Expiry, Quota -->
                <div class="col-6 col-md-3">
                    <div class="d-flex align-items-center gap-1 mb-1">
                        <span class="badge {status_class} rounded-pill">{status_label}</span>
                        <span class="badge {time_left_class} rounded-pill">⌛ {time_left_label}</span>
                    </div>
                    <div class="small text-dark fw-bold mb-1">
                        <i class="fa-solid fa-cube text-primary"></i> {c['package_price']}-TK-{c['package_name']}
                    </div>
                    <div class="small text-secondary">
                        <i class="fa-regular fa-calendar-days text-danger"></i> {exp_formatted}
                    </div>
                    <div class="small text-secondary">
                        <i class="fa-solid fa-globe text-info"></i> Unlimited
                    </div>
                </div>

                <!-- Col 6: Action Drawer Button -->
                <div class="col-auto ms-auto">
                    <button class="btn btn-outline-primary rounded-circle p-0 d-flex align-items-center justify-content-center" 
                            onclick="openActionDrawer({c['id']}, '{display_name}', '{uname}', '{c['package_price']}', '{c['expiry_date']}', '{c['status']}')" 
                            style="width: 42px; height: 42px;">
                        <i class="fa-solid fa-sliders fs-5"></i>
                    </button>
                </div>
            </div>
        </div>
        """

    # Payment log rows
    pay_rows = ""
    for p in payments:
        used_badge = '<span class="badge bg-secondary">Used</span>' if p["status"] == "used" else '<span class="badge bg-success">Unused</span>'
        pay_rows += f"""
        <tr>
            <td><b>{p['trx_id']}</b></td>
            <td><b class="text-success">{p['amount']} ৳</b></td>
            <td><small>{p['sender_phone']}</small></td>
            <td><small>{p['received_at']}</small></td>
            <td>{used_badge}</td>
            <td><b>{p['used_by'] or '-'}</b></td>
        </tr>
        """

    # Edit Form Modal/Card
    edit_form_html = ""
    if edit_cust:
        edit_form_html = f"""
        <div class="card shadow border-primary mb-4">
            <div class="card-header bg-primary text-white fw-bold d-flex justify-content-between align-items-center">
                <span><i class="fa-solid fa-pen-to-square me-2"></i>গ্রাহকের তথ্য এডিট করুন: {edit_cust['username']}</span>
                <a href="/admin" class="btn btn-sm btn-light">বাতিল (Close)</a>
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
                            <input type="text" name="name" class="form-control" value="{edit_cust['name'] or ''}" placeholder="যেমন: আনিসুর">
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-bold">মোবাইল নম্বর</label>
                            <input type="text" name="phone" class="form-control" value="{edit_cust['phone'] or ''}" placeholder="017xxxxxxxx">
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-bold">PPPoE পাসওয়ার্ড</label>
                            <input type="text" name="password" class="form-control" value="{edit_cust['password'] or '123456'}">
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-bold">লাইন প্রোফাইল</label>
                            <select name="profile" class="form-select">
                                <option value="default" {"selected" if edit_cust['profile']=='default' else ''}>Line 1 (default)</option>
                                <option value="Profile-Line2" {"selected" if edit_cust['profile']=='Profile-Line2' else ''}>Line 2 (Profile-Line2)</option>
                                <option value="Profile-Line3" {"selected" if edit_cust['profile']=='Profile-Line3' else ''}>Line 3 (Profile-Line3)</option>
                            </select>
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-bold">মাসিক বিল (৳)</label>
                            <input type="number" name="package_price" class="form-control" value="{edit_cust['package_price']}">
                        </div>
                        <div class="col-md-3">
                            <label class="form-label fw-bold">মেয়াদ (YYYY-MM-DD HH:MM:SS)</label>
                            <input type="text" name="expiry_date" class="form-control" value="{edit_cust['expiry_date']}">
                        </div>
                        <div class="col-md-2">
                            <label class="form-label fw-bold">স্ট্যাটাস</label>
                            <select name="status" class="form-select">
                                <option value="active" {"selected" if edit_cust['status']=='active' else ''}>Active (সচল)</option>
                                <option value="expired" {"selected" if edit_cust['status']=='expired' else ''}>Expired (মেয়াদ শেষ)</option>
                            </select>
                        </div>
                    </div>
                    <div class="mt-3">
                        <button type="submit" class="btn btn-success fw-bold px-4"><i class="fa-solid fa-floppy-disk me-1"></i> আপডেট করুন</button>
                        <a href="/admin" class="btn btn-secondary px-3">বাতিল</a>
                    </div>
                </form>
            </div>
        </div>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>{COMPANY_NAME} - Admin Dashboard</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
        <style>
            body {{ background: #eef2f6; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }}
            .card {{ border-radius: 12px; }}
            .customer-card {{ background: #ffffff; border-color: #e2e8f0; transition: transform 0.1s ease; }}
            .customer-card:hover {{ background: #fbfcfe; border-color: #cbd5e1; }}
            .stat-card {{ border-radius: 12px; padding: 18px; color: white; }}
            .action-btn {{ border-radius: 10px; padding: 10px 16px; font-weight: 600; text-align: center; display: inline-flex; align-items: center; justify-content: center; gap: 8px; font-size: 14px; text-decoration: none; }}
        </style>
    </head>
    <body class="p-2 p-md-4">
        <div class="container-fluid">
            <!-- Top Navbar -->
            <div class="d-flex flex-wrap justify-content-between align-items-center mb-4 pb-2 border-bottom">
                <div>
                    <h3 class="fw-bold mb-1"><i class="fa-solid fa-satellite-dish text-primary me-2"></i>{COMPANY_NAME}</h3>
                    <div class="d-flex align-items-center gap-2">
                        {mikrotik_status_badge}
                        <span class="badge bg-dark"><i class="fa-solid fa-mobile-screen me-1 text-danger"></i> বিকাশ: {bkash_num}</span>
                    </div>
                </div>
                <div class="mt-2 mt-md-0 d-flex gap-2 align-items-center">
                    <button class="btn btn-success fw-bold" data-bs-toggle="modal" data-bs-target="#newCustomerModal">
                        <i class="fa-solid fa-user-plus me-1"></i> নতুন গ্রাহক
                    </button>
                    <a href="/" target="_blank" class="btn btn-outline-primary fw-bold">
                        <i class="fa-solid fa-arrow-up-right-from-square me-1"></i> গ্রাহক পোর্টাল
                    </a>
                    <a href="/logout" class="btn btn-outline-danger" title="লগআউট">
                        <i class="fa-solid fa-power-off"></i>
                    </a>
                </div>
            </div>

            <!-- Stats Overview -->
            <div class="row g-3 mb-4">
                <div class="col-6 col-md-3">
                    <div class="stat-card bg-primary shadow-sm">
                        <div class="d-flex justify-content-between">
                            <div>
                                <small class="text-uppercase fw-bold opacity-75">মোট গ্রাহক</small>
                                <h2 class="fw-bold mb-0">{stats['total']}</h2>
                            </div>
                            <i class="fa-solid fa-users fa-2x opacity-50"></i>
                        </div>
                    </div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="stat-card bg-success shadow-sm">
                        <div class="d-flex justify-content-between">
                            <div>
                                <small class="text-uppercase fw-bold opacity-75">সচল গ্রাহক</small>
                                <h2 class="fw-bold mb-0">{stats['active']}</h2>
                            </div>
                            <i class="fa-solid fa-circle-check fa-2x opacity-50"></i>
                        </div>
                    </div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="stat-card bg-danger shadow-sm">
                        <div class="d-flex justify-content-between">
                            <div>
                                <small class="text-uppercase fw-bold opacity-75">মেয়াদোত্তীর্ণ</small>
                                <h2 class="fw-bold mb-0">{stats['expired']}</h2>
                            </div>
                            <i class="fa-solid fa-circle-xmark fa-2x opacity-50"></i>
                        </div>
                    </div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="stat-card bg-dark shadow-sm">
                        <div class="d-flex justify-content-between">
                            <div>
                                <small class="text-uppercase fw-bold opacity-75">মোট কালেকশন</small>
                                <h2 class="fw-bold mb-0">{stats['total_revenue']} ৳</h2>
                            </div>
                            <i class="fa-solid fa-wallet fa-2x opacity-50 text-warning"></i>
                        </div>
                    </div>
                </div>
            </div>

            {edit_form_html}

            <!-- Search and Filter Bar -->
            <div class="card p-3 mb-3 bg-white border-0 shadow-sm">
                <div class="row align-items-center g-2">
                    <div class="col-md-6">
                        <div class="input-group">
                            <span class="input-group-text bg-light border-end-0"><i class="fa-solid fa-magnifying-glass text-muted"></i></span>
                            <input type="text" id="custSearchInput" class="form-control border-start-0" placeholder="গ্রাহকের নাম, ইউজার আইডি বা মোবাইল দিয়ে খুঁজুন..." onkeyup="filterCustomerCards()">
                        </div>
                    </div>
                    <div class="col-md-6 text-md-end text-muted small">
                        সর্বমোট গ্রাহক: <b>{len(customers)} জন</b> | অনলাইন: <b>{len(actives)} জন</b>
                    </div>
                </div>
            </div>

            <!-- Customer Rows Container -->
            <div id="customerCardsContainer">
                {cust_rows_html}
            </div>

            <!-- bKash Logs Table at bottom -->
            <div class="card shadow-sm mt-4">
                <div class="card-header bg-white py-3">
                    <h5 class="fw-bold mb-0 text-dark"><i class="fa-solid fa-receipt me-2 text-danger"></i>সাম্প্রতিক বিকাশ পেমেন্ট হিস্ট্রি</h5>
                </div>
                <div class="card-body p-0 table-responsive">
                    <table class="table table-sm table-hover align-middle mb-0">
                        <thead class="table-light">
                            <tr><th>TrxID</th><th>টাকা</th><th>নম্বর</th><th>সময়</th><th>স্ট্যাটাস</th><th>ইউজার</th></tr>
                        </thead>
                        <tbody>
                            {pay_rows}
                        </tbody>
                    </table>
                </div>
            </div>
        </div>

        <!-- ACTION DRAWER / MODAL (EXACT REPLICA OF IMAGE 2) -->
        <div class="modal fade" id="actionModal" tabindex="-1">
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content border-0 shadow" style="border-radius: 16px;">
                    <div class="modal-header border-bottom py-3">
                        <h5 class="modal-title fw-bold text-dark" id="actionModalTitle">Actions for customer</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
                    </div>
                    <div class="modal-body p-4">
                        <div class="row g-2" id="actionButtonsGrid">
                            <!-- Populated dynamically via JavaScript -->
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- LIVE SPEED TEST MODAL -->
        <div class="modal fade" id="speedModal" tabindex="-1">
            <div class="modal-dialog modal-dialog-centered">
                <div class="modal-content border-0 shadow text-center" style="border-radius: 16px;">
                    <div class="modal-header bg-primary text-white">
                        <h5 class="modal-title fw-bold" id="speedModalTitle"><i class="fa-solid fa-chart-line me-2"></i>Live Real-Time Traffic</h5>
                        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal" onclick="stopSpeedMonitor()"></button>
                    </div>
                    <div class="modal-body p-4">
                        <h6 class="text-muted mb-3" id="speedUserLabel">গ্রাহকের লাইভ স্পিড ট্র্যাকিং</h6>
                        <div class="row g-3 my-2">
                            <div class="col-6">
                                <div class="p-3 bg-light rounded-3 border border-success">
                                    <div class="text-success small fw-bold"><i class="fa-solid fa-arrow-down"></i> ডাউনলোড (Rx)</div>
                                    <h2 class="fw-bold text-success my-2" id="rxSpeed">0.00</h2>
                                    <small class="text-muted">Mbps</small>
                                </div>
                            </div>
                            <div class="col-6">
                                <div class="p-3 bg-light rounded-3 border border-primary">
                                    <div class="text-primary small fw-bold"><i class="fa-solid fa-arrow-up"></i> আপলোড (Tx)</div>
                                    <h2 class="fw-bold text-primary my-2" id="txSpeed">0.00</h2>
                                    <small class="text-muted">Mbps</small>
                                </div>
                            </div>
                        </div>
                        <div class="mt-3">
                            <span class="badge bg-success" id="speedOnlineStatus">● Connected & Monitoring</span>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <!-- Modal: Add New Customer -->
        <div class="modal fade" id="newCustomerModal" tabindex="-1">
            <div class="modal-dialog modal-lg">
                <div class="modal-content">
                    <div class="modal-header bg-primary text-white">
                        <h5 class="modal-title fw-bold"><i class="fa-solid fa-user-plus me-2"></i>নতুন গ্রাহক যুক্ত করুন (অটো MikroTik সিঙ্ক)</h5>
                        <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                    </div>
                    <form method="POST" action="/admin/create-customer">
                        <div class="modal-body">
                            <div class="row g-3">
                                <div class="col-md-6">
                                    <label class="form-label fw-bold">PPPoE ইউজার আইডি *</label>
                                    <input type="text" name="username" class="form-control" placeholder="যেমন: MRF300" required>
                                </div>
                                <div class="col-md-6">
                                    <label class="form-label fw-bold">PPPoE পাসওয়ার্ড *</label>
                                    <input type="text" name="password" class="form-control" value="1234" required>
                                </div>
                                <div class="col-md-6">
                                    <label class="form-label fw-bold">গ্রাহকের আসল নাম</label>
                                    <input type="text" name="name" class="form-control" placeholder="যেমন: আনিসুর">
                                </div>
                                <div class="col-md-6">
                                    <label class="form-label fw-bold">মোবাইল নম্বর</label>
                                    <input type="text" name="phone" class="form-control" placeholder="017xxxxxxxx">
                                </div>
                                <div class="col-md-4">
                                    <label class="form-label fw-bold">প্যাকেজ নাম</label>
                                    <input type="text" name="package_name" class="form-control" value="25Mbps">
                                </div>
                                <div class="col-md-4">
                                    <label class="form-label fw-bold">মাসিক বিল (৳)</label>
                                    <input type="number" name="package_price" class="form-control" value="525">
                                </div>
                                <div class="col-md-4">
                                    <label class="form-label fw-bold">লাইন প্রোফাইল *</label>
                                    <select name="profile" class="form-select">
                                        <option value="default">Line 1 (default)</option>
                                        <option value="Profile-Line2">Line 2 (Profile-Line2)</option>
                                        <option value="Profile-Line3">Line 3 (Profile-Line3)</option>
                                    </select>
                                </div>
                                <div class="col-md-6">
                                    <label class="form-label fw-bold">প্রাথমিক মেয়াদ (দিন)</label>
                                    <input type="number" name="days_valid" class="form-control" value="30">
                                </div>
                            </div>
                        </div>
                        <div class="modal-footer">
                            <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">বন্ধ করুন</button>
                            <button type="submit" class="btn btn-primary fw-bold">সংরক্ষণ ও MikroTik-এ যুক্ত করুন</button>
                        </div>
                    </form>
                </div>
            </div>
        </div>

        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
        <script>
            function filterCustomerCards() {{
                let input = document.getElementById("custSearchInput").value.toLowerCase();
                let cards = document.querySelectorAll(".customer-card");
                cards.forEach(c => {{
                    let searchData = c.getAttribute("data-search");
                    c.style.display = searchData.includes(input) ? "" : "none";
                }});
            }}

            let actionModalInstance = null;
            function openActionDrawer(id, name, username, price, expiry, status) {{
                document.getElementById("actionModalTitle").innerText = "Actions for customer ID: #" + (id + 40500) + " (" + name + ")";
                
                const grid = document.getElementById("actionButtonsGrid");
                grid.innerHTML = `
                    <div class="col-6">
                        <a href="/admin?edit_id=${{id}}" class="btn btn-outline-primary w-100 action-btn"><i class="fa-solid fa-pen-to-square"></i> Edit</a>
                    </div>
                    <div class="col-6">
                        <a href="/admin/toggle-suspend/${{id}}" class="btn btn-outline-warning w-100 action-btn"><i class="fa-solid fa-circle-pause"></i> Suspend</a>
                    </div>
                    <div class="col-6">
                        <a href="/admin/quick-extend/${{id}}" class="btn btn-outline-success w-100 action-btn"><i class="fa-solid fa-calendar-plus"></i> Recharge (+30d)</a>
                    </div>
                    <div class="col-6">
                        <a href="/admin/kick/${{username}}" class="btn btn-outline-info w-100 action-btn"><i class="fa-solid fa-plug-circle-xmark"></i> Disconnect</a>
                    </div>
                    <div class="col-6">
                        <button onclick="actionModalInstance.hide(); openLiveSpeed('${{username}}', '${{name}}')" class="btn btn-outline-primary w-100 action-btn"><i class="fa-solid fa-chart-line"></i> Live Speed</button>
                    </div>
                    <div class="col-6">
                        <a href="/?query=${{username}}" target="_blank" class="btn btn-outline-secondary w-100 action-btn"><i class="fa-solid fa-arrow-up-right-from-square"></i> Portal View</a>
                    </div>
                    <div class="col-12 mt-2">
                        <a href="/admin/delete/${{id}}" onclick="return confirm('আপনি কি নিশ্চিত এই গ্রাহককে সম্পূর্ণ মুছে ফেলতে চান?');" class="btn btn-outline-danger w-100 action-btn"><i class="fa-solid fa-trash"></i> Delete Account</a>
                    </div>
                `;
                
                actionModalInstance = new bootstrap.Modal(document.getElementById("actionModal"));
                actionModalInstance.show();
            }}

            let speedInterval = null;
            let speedModalInstance = null;

            function openLiveSpeed(username, name) {{
                document.getElementById("speedUserLabel").innerText = name + " (" + username + ") এর রিয়েল-টাইম ইন্টারনেট ট্র্যাফিক";
                speedModalInstance = new bootstrap.Modal(document.getElementById("speedModal"));
                speedModalInstance.show();

                fetchSpeed(username);
                if (speedInterval) clearInterval(speedInterval);
                speedInterval = setInterval(() => fetchSpeed(username), 1500);
            }}

            async function fetchSpeed(username) {{
                try {{
                    const res = await fetch('/api/live-speed/' + encodeURIComponent(username));
                    const data = await res.json();
                    document.getElementById("rxSpeed").innerText = data.rx_mbps.toFixed(2);
                    document.getElementById("txSpeed").innerText = data.tx_mbps.toFixed(2);
                    const statusBadge = document.getElementById("speedOnlineStatus");
                    if (data.online) {{
                        statusBadge.className = "badge bg-success";
                        statusBadge.innerText = "● Connected & Pulling Traffic";
                    }} else {{
                        statusBadge.className = "badge bg-secondary";
                        statusBadge.innerText = "○ Idle or Offline";
                    }}
                }} catch (e) {{
                    console.error(e);
                }}
            }}

            function stopSpeedMonitor() {{
                if (speedInterval) clearInterval(speedInterval);
            }}
            
            document.getElementById("speedModal").addEventListener('hidden.bs.modal', function () {{
                stopSpeedMonitor();
            }});
        </script>
    </body>
    </html>
    """
    return html

# --- CRUD Actions ---

@app.post("/admin/create-customer")
async def create_customer_action(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), password: str = Form(...),
                                 name: str = Form(""), phone: str = Form(""), package_name: str = Form("25Mbps"),
                                 package_price: int = Form(525), profile: str = Form("default"), days_valid: int = Form(30)):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    success, err = database.create_customer(username, password, name, phone, package_name, package_price, profile, days_valid)
    if success:
        background_tasks.add_task(sync_secret_to_mikrotik, username, password, profile)
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/admin/update-customer")
async def update_customer_action(request: Request, background_tasks: BackgroundTasks, cust_id: int = Form(...), name: str = Form(""),
                                 phone: str = Form(""), password: str = Form(...), package_name: str = Form("25Mbps"),
                                 package_price: int = Form(525), profile: str = Form("default"),
                                 expiry_date: str = Form(...), status: str = Form("active")):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    database.update_customer_full(cust_id, name, phone, password, package_name, package_price, profile, expiry_date, status)
    cust = database.get_customer_by_id(cust_id)
    if cust:
        background_tasks.add_task(sync_secret_to_mikrotik, cust["username"], password, profile)
        if status == "active":
            background_tasks.add_task(unblock_in_mikrotik, cust["username"])
        else:
            background_tasks.add_task(block_in_mikrotik, cust["username"])
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/quick-extend/{cust_id}")
async def quick_extend_action(request: Request, background_tasks: BackgroundTasks, cust_id: int):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    username, new_expiry = database.extend_customer(cust_id, days=30)
    if username:
        background_tasks.add_task(unblock_in_mikrotik, username)
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/toggle-suspend/{cust_id}")
async def toggle_suspend_action(request: Request, background_tasks: BackgroundTasks, cust_id: int):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    cust = database.get_customer_by_id(cust_id)
    if cust:
        new_status = "expired" if cust["status"] == "active" else "active"
        database.update_customer_full(cust_id, cust["name"], cust["phone"], cust["password"], cust["package_name"], cust["package_price"], cust["profile"], cust["expiry_date"], new_status)
        if new_status == "active":
            background_tasks.add_task(unblock_in_mikrotik, cust["username"])
        else:
            background_tasks.add_task(block_in_mikrotik, cust["username"])
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/kick/{username}")
async def kick_user_action(request: Request, username: str):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    try:
        api = get_mikrotik_conn()
        api.kick_user(username)
        api.close()
    except Exception as e:
        logger.error(f"Failed to kick {username}: {e}")
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/admin/delete/{cust_id}")
async def delete_customer_action(request: Request, background_tasks: BackgroundTasks, cust_id: int):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    username = database.delete_customer(cust_id)
    if username:
        background_tasks.add_task(delete_secret_from_mikrotik, username)
    return RedirectResponse(url="/admin", status_code=303)

@app.get("/api/cron/check-expiry")
async def trigger_expiry_check():
    check_expired_users_task()
    return {"status": "success", "message": "Expiry check executed"}
