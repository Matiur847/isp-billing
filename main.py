import os
import re
import html
import hashlib
import datetime
import logging
from typing import Optional
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, BackgroundTasks, UploadFile, File, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from itsdangerous import URLSafeSerializer, BadSignature

import database
from mikrotik import get_router_driver, ROUTER_DRIVERS, live_pool
from olt import get_olt_driver, OLT_DRIVERS, normalize_mac

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("billing_app")

COOKIE_NAME = "admin_session"


def h(val) -> str:
    return html.escape("" if val is None else str(val), quote=True)


def get_company_name() -> str:
    return database.get_setting("company_name", "সাইফুল ডিস এন্ড ইন্টারনেট 🛜")


def get_mikrotik_conn():
    driver = database.get_setting("router_driver", "mikrotik_api")
    host = database.get_setting("mikrotik_host", "")
    port = int(database.get_setting("mikrotik_port", "8728") or 8728)
    user = database.get_setting("mikrotik_user", "admin")
    pwd = database.get_setting("mikrotik_pass", "")
    api = get_router_driver(driver, host, port, user, pwd)
    api.connect()
    return api


def get_olt_conn():
    driver = database.get_setting("olt_driver", "vsol_gpon_web")
    url = database.get_setting("olt_url", "")
    user = database.get_setting("olt_user", "admin")
    pwd = database.get_setting("olt_pass", "")
    return get_olt_driver(driver, url, user, pwd)


def get_block_mode() -> str:
    return database.get_setting("block_mode", "address_list")


def unblock_in_mikrotik(username: str):
    try:
        api = get_mikrotik_conn()
        api.unblock_user_by_name(username)
        api.close()
    except Exception as e:
        logger.error(f"Unblock failed for {username}: {e}")


def block_in_mikrotik(username: str, ip_address: Optional[str] = None):
    try:
        api = get_mikrotik_conn()
        api.block_customer(username, ip_address=ip_address, mode=get_block_mode())
        api.close()
    except Exception as e:
        logger.error(f"Block failed for {username}: {e}")


def enforce_expired_active_users(expired_ips: list):
    try:
        api = get_mikrotik_conn()
        mode = get_block_mode()
        for uname, ip in expired_ips:
            api.block_customer(uname, ip_address=ip, mode=mode)
        api.close()
    except Exception as e:
        logger.error(f"Enforce expired failed: {e}")


def save_auto_links_task(links: list):
    for cust_id, pon_id, onu_id, onu_sn in links:
        try:
            database.link_customer_onu(cust_id, pon_id, onu_id, onu_sn)
        except Exception as e:
            logger.error(f"Auto link failed for cust {cust_id}: {e}")


def sync_secret_to_mikrotik(username, password, profile="default"):
    try:
        api = get_mikrotik_conn()
        api.create_pppoe_secret(username, password, profile=profile, comment="Created from Web Portal")
        api.close()
    except Exception as e:
        logger.error(f"Sync secret failed {username}: {e}")


def delete_secret_from_mikrotik(username):
    try:
        api = get_mikrotik_conn()
        api.delete_pppoe_secret(username)
        api.close()
    except Exception as e:
        logger.error(f"Delete secret failed {username}: {e}")


def check_expired_users_task():
    try:
        expired = database.get_expired_active_customers()
        if not expired:
            return
        api = get_mikrotik_conn()
        actives = api.get_active_pppoe()
        mode = get_block_mode()
        for row in expired:
            uname = row["username"]
            database.mark_customer_expired(uname)
            ip = actives[uname]["address"] if uname in actives else None
            api.block_customer(uname, ip_address=ip, mode=mode)
        api.close()
    except Exception as e:
        logger.error(f"check_expired_users_task error: {e}")


def _parse_mk_dt(dt_str: str):
    s = (dt_str or "").strip()
    if not s or "1970" in s:
        return None
    for fmt in ("%b/%d/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(s, fmt)
        except Exception:
            pass
    try:
        return datetime.datetime.strptime(s.title(), "%b/%d/%Y %H:%M:%S")
    except Exception:
        return None


def _format_offline_ago(router_now, last_logout_str: str) -> str:
    dt = _parse_mk_dt(last_logout_str)
    if not dt or not router_now:
        return ""
    secs = int((router_now - dt).total_seconds())
    if secs < 0:
        return ""
    if secs < 60:
        return f"{secs} সেকেন্ড আগে"
    mins = secs // 60
    if mins < 60:
        return f"{mins} মিনিট আগে"
    hrs = mins // 60
    rem_mins = mins % 60
    if hrs < 24:
        return f"{hrs} ঘণ্টা {rem_mins} মিনিট আগে" if rem_mins else f"{hrs} ঘণ্টা আগে"
    days = hrs // 24
    rem_hrs = hrs % 24
    return f"{days} দিন {rem_hrs} ঘণ্টা আগে" if rem_hrs else f"{days} দিন আগে"


def calculate_expiry_label(expiry_str: str, status_str: str):
    now = datetime.datetime.now()
    try:
        if len(expiry_str or "") == 10:
            exp_dt = datetime.datetime.strptime(expiry_str, "%Y-%m-%d")
        else:
            exp_dt = datetime.datetime.strptime((expiry_str or "")[:19], "%Y-%m-%d %H:%M:%S")
    except Exception:
        return "Expired", "bg-danger text-white", "Suspended", "bg-warning-subtle text-warning-emphasis border border-warning-subtle"

    if status_str != "active" or exp_dt < now:
        diff = now - exp_dt
        days, hours = max(0, diff.days), diff.seconds // 3600
        label = f"Expired {days} দিন" if days > 0 else f"Expired {hours} ঘণ্টা"
        return label, "bg-danger-subtle text-danger border border-danger-subtle", "Suspended", "bg-warning-subtle text-warning-emphasis border border-warning-subtle"
    diff = exp_dt - now
    days, hours = diff.days, diff.seconds // 3600
    label = f"{days} দিন left" if days > 0 else f"{hours} ঘণ্টা left"
    return label, "bg-success-subtle text-success border border-success-subtle", "Active", "bg-success-subtle text-success border border-success-subtle"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    logger.info(f"Database initialized (PostgreSQL={database.IS_POSTGRES}).")
    yield


app = FastAPI(title="ISP Billing, MikroTik & OLT Automation", lifespan=lifespan)


def _pass_hash(pwd: str) -> str:
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest()[:16]


def _get_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(database.get_setting("session_secret", "isp-default-secret-key"), salt="admin-auth")


def is_authenticated(request: Request) -> bool:
    cookie_val = request.cookies.get(COOKIE_NAME)
    if not cookie_val:
        return False
    admin_pass = database.get_setting("admin_password", "admin123")
    try:
        data = _get_serializer().loads(cookie_val)
        return data.get("auth") is True and data.get("ph") == _pass_hash(admin_pass)
    except BadSignature:
        return False


def admin_navbar(active_tab: str = "dashboard") -> str:
    company = h(get_company_name())
    def cls(t): return "btn btn-primary fw-bold" if active_tab == t else "btn btn-outline-secondary fw-bold"
    return f"""
    <div class="d-flex flex-wrap justify-content-between align-items-center mb-4 pb-3 border-bottom gap-2 bg-white p-3 rounded-3 shadow-sm">
        <div><h4 class="fw-bold mb-0"><i class="fa-solid fa-satellite-dish text-primary me-2"></i>{company}</h4></div>
        <div class="d-flex flex-wrap gap-2 align-items-center">
            <a href="/admin" class="{cls('dashboard')} btn-sm"><i class="fa-solid fa-users me-1"></i> গ্রাহক তালিকা</a>
            <a href="/admin/olt" class="{cls('olt')} btn-sm"><i class="fa-solid fa-diagram-project me-1"></i> OLT মনিটর</a>
            <a href="/admin/settings" class="{cls('settings')} btn-sm"><i class="fa-solid fa-gear me-1"></i> Settings ও ব্যাকআপ</a>
            <a href="/" target="_blank" class="btn btn-outline-info btn-sm fw-bold"><i class="fa-solid fa-arrow-up-right-from-square me-1"></i> পোর্টাল</a>
            <a href="/logout" class="btn btn-outline-danger btn-sm" title="লগআউট"><i class="fa-solid fa-power-off"></i></a>
        </div>
    </div>"""


def parse_bkash_sms(text: str):
    amount_m = re.search(r'received\s+Tk\s*([0-9,]+(?:\.[0-9]{1,2})?)', text, re.IGNORECASE)
    trx_m = re.search(r'TrxID\s+([A-Za-z0-9]+)', text, re.IGNORECASE)
    sender_m = re.search(r'from\s+(01[3-9][0-9]{8})', text, re.IGNORECASE)
    if trx_m and amount_m:
        return trx_m.group(1).upper(), float(amount_m.group(1).replace(",", "")), (sender_m.group(1) if sender_m else "Unknown")
    return None, None, None


@app.get("/api/search")
def api_search(q: str = ""):
    return database.search_customers(q, limit=8)


@app.get("/api/live-speed/{username}")
def api_live_speed(username: str):
    try:
        driver = database.get_setting("router_driver", "mikrotik_api")
        host = database.get_setting("mikrotik_host", "")
        port = int(database.get_setting("mikrotik_port", "8728") or 8728)
        user = database.get_setting("mikrotik_user", "admin")
        pwd = database.get_setting("mikrotik_pass", "")
        return live_pool.get_live_speed(driver, host, port, user, pwd, username)
    except Exception as e:
        return {"rx_mbps": 0.0, "tx_mbps": 0.0, "dl_bps": 0, "ul_bps": 0, "online": False, "error": str(e)}


@app.post("/api/sms-webhook")
async def sms_webhook(request: Request):
    try:
        body = await request.json()
        text = (body.get("content") or body.get("msg") or body.get("message") or body.get("text") or body.get("body") or str(body)) if isinstance(body, dict) else str(body)
    except Exception:
        text = (await request.body()).decode("utf-8", errors="replace")
    trx_id, amount, sender = parse_bkash_sms(text)
    if trx_id and amount:
        if database.record_payment(trx_id, amount, sender, text):
            return {"status": "success", "message": "Payment recorded", "trx_id": trx_id, "amount": amount}
        return {"status": "duplicate", "message": "TrxID already exists"}
    return {"status": "ignored", "message": "Not a recognized bKash Send Money SMS"}


@app.get("/api/cron/check-expiry")
def trigger_expiry_check():
    check_expired_users_task()
    return {"status": "success", "message": "Expiry check executed"}


@app.get("/login", response_class=HTMLResponse)
def login_page(error: Optional[str] = None):
    company = h(get_company_name())
    err_html = f"<div class='alert alert-danger py-2 small'>{h(error)}</div>" if error else ""
    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - অ্যাডমিন লগইন</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>body{{background:#f0f4f9;height:100vh;display:flex;align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;}}.card{{border-radius:18px;border:none;box-shadow:0 10px 30px rgba(0,0,0,0.08);width:100%;max-width:400px;}}.header-banner{{background:linear-gradient(135deg,#0d6efd,#004ecc);color:white;padding:25px;border-radius:18px 18px 0 0;text-align:center;}}</style>
</head><body><div class="card"><div class="header-banner"><i class="fa-solid fa-shield-halved fa-3x mb-2"></i><h4 class="fw-bold mb-1">{company}</h4><p class="mb-0 opacity-75 small">সুরক্ষিত অ্যাডমিন প্যানেল</p></div>
<div class="card-body p-4">{err_html}<form method="POST" action="/login"><div class="mb-3"><label class="form-label fw-bold">অ্যাডমিন পাসওয়ার্ড:</label>
<div class="input-group"><span class="input-group-text"><i class="fa-solid fa-lock"></i></span><input type="password" name="password" class="form-control form-control-lg" placeholder="পাসওয়ার্ড লিখুন" required autofocus></div></div>
<button type="submit" class="btn btn-primary btn-lg w-100 fw-bold"><i class="fa-solid fa-right-to-bracket me-2"></i> লগইন করুন</button></form></div></div></body></html>"""


@app.post("/login")
def do_login(password: str = Form(...)):
    admin_pass = database.get_setting("admin_password", "admin123")
    if password == admin_pass:
        token = _get_serializer().dumps({"auth": True, "ph": _pass_hash(admin_pass)})
        response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key=COOKIE_NAME, value=token, max_age=86400 * 30, httponly=True, samesite="lax")
        return response
    return RedirectResponse(url="/login?error=ভুল+পাসওয়ার্ড!+আবার+চেষ্টা+করুন।", status_code=status.HTTP_303_SEE_OTHER)


@app.get("/logout")
def do_logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
def customer_portal(query: Optional[str] = None, user_id: Optional[int] = None):
    company = h(get_company_name())
    bkash_num = h(database.get_setting("bkash_number", "01XXXXXXXXX"))
    selected_customer = database.get_customer_by_id(user_id) if user_id else None
    search_results = []
    if not selected_customer and query:
        search_results = database.search_customers(query)
        if len(search_results) == 1:
            selected_customer = database.get_customer_by_id(search_results[0]["id"])

    body_content = ""
    if query and not search_results and not selected_customer:
        body_content = '<div class="alert alert-danger">❌ এই নাম বা আইডিতে কোনো গ্রাহক পাওয়া যায়নি! সঠিক নাম বা ইউজার আইডি লিখুন।</div>'
    elif len(search_results) > 1 and not selected_customer:
        items = ""
        for c in search_results:
            ph_txt = h(c["phone"] or "নাই")
            items += f'<a href="/?user_id={c["id"]}" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center p-3"><div><h6 class="mb-1 fw-bold text-primary">{h(c["name"] or c["username"])}</h6><small class="text-muted">আইডি: {h(c["username"])} | মোবাইল: {ph_txt}</small></div><span class="badge bg-primary rounded-pill">পেমেন্ট ➔</span></a>'
        body_content = f'<div class="alert alert-info">🔍 একাধিক গ্রাহক পাওয়া গেছে! আপনার প্রোফাইলটি সিলেক্ট করুন:</div><div class="list-group mb-4">{items}</div>'
    elif selected_customer:
        c = selected_customer
        sb = '<span class="badge bg-success">সচল (Active)</span>' if c["status"] == "active" else '<span class="badge bg-danger">মেয়াদ শেষ (Expired)</span>'
        body_content = f"""
        <div class="alert alert-light border mb-4">
            <h5 class="fw-bold text-primary mb-3"><i class="fa-solid fa-id-card me-2"></i>গ্রাহকের তথ্য:</h5>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>গ্রাহকের নাম:</span><span class="fw-bold">{h(c['name'] or c['username'])}</span></div>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>ইউজার আইডি:</span><span class="fw-bold">{h(c['username'])}</span></div>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>প্যাকেজ:</span><span class="fw-bold">{h(c['package_name'])}</span></div>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>মাসিক বিল:</span><span class="fw-bold text-danger fs-5">{c['package_price']} ৳</span></div>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>বর্তমান মেয়াদ:</span><span class="fw-bold">{h(c['expiry_date'])}</span></div>
            <div class="d-flex justify-content-between py-1"><span>স্ট্যাটাস:</span><span>{sb}</span></div>
        </div>
        <div class="card bg-light border-0 p-3 mb-4">
            <h6 class="fw-bold text-dark mb-2"><i class="fa-solid fa-mobile-screen me-1 text-danger"></i> বিকাশ পেমেন্ট নির্দেশিকা:</h6>
            <ol class="small ps-3 mb-2 text-secondary"><li>আপনার বিকাশ অ্যাপে গিয়ে <b>Send Money</b> অপশনে যান।</li><li>নিচের পার্সোনাল বিকাশ নাম্বারে ঠিক <b>{c['package_price']} টাকা</b> পাঠান:</li></ol>
            <div class="text-center my-3"><div class="d-inline-flex align-items-center gap-2"><span class="bkash-badge fs-5">bKash: {bkash_num}</span><button type="button" onclick="copyBkash('{bkash_num}')" class="btn btn-sm btn-light border shadow-sm px-2" id="copyBtn"><i class="fa-regular fa-copy text-danger"></i></button></div><div class="mt-1"><small class="text-muted">Personal Number</small></div></div>
            <small class="text-muted text-center d-block">টাকা পাঠানোর পর ফিরতি এসএমএসের <b>TrxID</b> নিচে লিখুন।</small>
        </div>
        <form method="POST" action="/api/pay"><input type="hidden" name="username" value="{h(c['username'])}"><div class="mb-3"><label class="form-label fw-bold">বিকাশ TrxID দিন:</label><input type="text" name="trx_id" class="form-control form-control-lg text-uppercase fw-bold" placeholder="যেমন: DKG1JU47Y7" required></div><button type="submit" class="btn btn-success btn-lg w-100 fw-bold"><i class="fa-solid fa-paper-plane me-1"></i> পেমেন্ট নিশ্চিত ও নেট চালু করুন</button></form>"""

    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - বিল পরিশোধ পোর্টাল</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>body{{background:#f0f4f9;font-family:'Segoe UI',sans-serif;}}.card{{border-radius:18px;box-shadow:0 12px 35px rgba(0,0,0,0.07);border:none;}}.bkash-badge{{background-color:#e2136e;color:white;border-radius:12px;padding:12px 22px;font-weight:bold;display:inline-block;}}.header-banner{{background:linear-gradient(135deg,#0d6efd,#004ecc);color:white;padding:30px 20px;border-radius:18px 18px 0 0;text-align:center;}}</style>
</head><body class="py-4"><div class="container" style="max-width: 530px;"><div class="card mb-4"><div class="header-banner"><h3 class="mb-1 fw-bold">{company}</h3><p class="mb-0 opacity-75 fs-6">বিকাশ দিয়ে অটোমেটিক ইনস্ট্যান্ট রিচার্জ</p></div>
<div class="card-body p-4"><form method="GET" action="/" class="mb-3 position-relative" autocomplete="off"><label class="form-label fw-bold">গ্রাহকের নাম, ইউজার আইডি বা মোবাইল নম্বর:</label>
<div class="input-group"><input type="text" id="searchInput" name="query" class="form-control form-control-lg" placeholder="যেমন: anisur বা 017..." value="{h(query or '')}" required><button class="btn btn-primary px-4 fw-bold" type="submit"><i class="fa-solid fa-magnifying-glass me-1"></i> সার্চ</button></div>
<div id="searchResults" class="list-group shadow position-absolute w-100 mt-1 d-none" style="z-index:1050;max-height:280px;overflow-y:auto;"></div></form>{body_content}</div></div>
<p class="text-center text-muted small">© {company} | হেল্পলাইন: {bkash_num}</p></div>
<script>
function copyBkash(n){{navigator.clipboard.writeText(n).then(()=>{{const b=document.getElementById('copyBtn');b.innerHTML='<i class="fa-solid fa-check text-success"></i>';setTimeout(()=>b.innerHTML='<i class="fa-regular fa-copy text-danger"></i>',2000);}});}}
const si=document.getElementById('searchInput'),sr=document.getElementById('searchResults');
if(si&&sr){{si.addEventListener('input',async function(){{const q=this.value.trim();if(!q){{sr.classList.add('d-none');return;}}const res=await fetch('/api/search?q='+encodeURIComponent(q));const data=await res.json();if(!data.length){{sr.innerHTML='<div class="list-group-item text-muted small p-3">কোনো মিল পাওয়া যায়নি</div>';sr.classList.remove('d-none');return;}}sr.innerHTML=data.map(i=>`<a href="/?user_id=${{i.id}}" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center p-3"><div><div class="fw-bold text-primary">${{i.name||i.username}}</div><small class="text-muted">আইডি: ${{i.username}} ${{i.phone?'| '+i.phone:''}}</small></div><span class="badge bg-primary rounded-pill">সিলেক্ট ➔</span></a>`).join('');sr.classList.remove('d-none');}});document.addEventListener('click',e=>{{if(!si.contains(e.target)&&!sr.contains(e.target))sr.classList.add('d-none');}});}}
</script></body></html>"""


@app.post("/api/pay", response_class=HTMLResponse)
def process_payment(background_tasks: BackgroundTasks, username: str = Form(...), trx_id: str = Form(...)):
    company = h(get_company_name())
    success, message = database.verify_and_apply_payment(username, trx_id)
    if success:
        background_tasks.add_task(unblock_in_mikrotik, username)
        ac, ic, hd = "alert-success", "🎉", "পেমেন্ট সফল হয়েছে!"
    else:
        ac, ic, hd = "alert-danger", "⚠️", "পেমেন্ট সম্পন্ন হয়নি!"
    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{company}</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body class="bg-light py-5"><div class="container text-center" style="max-width:500px;"><div class="card p-4 shadow-sm border-0"><div class="display-3 mb-3">{ic}</div><h3 class="fw-bold mb-3">{hd}</h3><div class="alert {ac} text-start">{message}</div><a href="/?query={h(username)}" class="btn btn-primary mt-2 fw-bold">ফিরে যান</a></div></div></body></html>"""


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, background_tasks: BackgroundTasks, edit_id: Optional[int] = None, msg: Optional[str] = None):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")

    company = h(get_company_name())
    customers = database.get_all_customers()
    stats = database.get_stats()
    payments = database.get_recent_payments(25)
    bkash_num = h(database.get_setting("bkash_number", "01XXXXXXXXX"))
    edit_cust = database.get_customer_by_id(edit_id) if edit_id else None

    actives, secrets_list, profiles, mk_online, mk_error = {}, [], ["default", "Profile-Line2", "Profile-Line3"], False, ""
    router_now = None
    try:
        api = get_mikrotik_conn()
        actives = api.get_active_pppoe()
        secrets_list = api.get_all_secrets()
        profiles = api.get_profiles() or profiles
        router_now = _parse_mk_dt(api.get_router_clock())
        api.close()
        mk_online = True
    except Exception as e:
        mk_error = str(e)

    secret_by_uname = {(s.get("name") or "").strip().lower(): s for s in secrets_list}

    if mk_online:
        expired_names = database.get_all_expired_usernames()
        to_block = [(u, actives[u]["address"]) for u in actives if u in expired_names]
        if to_block:
            background_tasks.add_task(enforce_expired_active_users, to_block)

    mk_badge = '<span class="badge bg-success"><i class="fa-solid fa-circle-check me-1"></i> MikroTik Online</span>' if mk_online else f'<span class="badge bg-danger" title="{h(mk_error)}"><i class="fa-solid fa-triangle-exclamation me-1"></i> MikroTik Offline</span>'
    db_badge = '<span class="badge bg-success-subtle text-success border border-success-subtle">PostgreSQL (Permanent)</span>' if database.IS_POSTGRES else '<span class="badge bg-warning-subtle text-warning-emphasis border border-warning-subtle">SQLite (Local)</span>'

    cust_rows_html = ""
    for c in customers:
        uname = c["username"]
        sec = secret_by_uname.get(uname.strip().lower(), {})
        is_online = uname in actives
        act = actives.get(uname, {})
        ip, uptime = act.get("address", "Offline"), act.get("uptime", "Offline")
        mac = act.get("caller_id") or c.get("mac_address") or sec.get("last_caller_id") or "No MAC"
        usage_gb = act.get("usage_gb", 0.0) if is_online else 0.0

        if is_online:
            online_badge = f'<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill px-2">● Online</span> <span class="text-secondary small fw-bold">{h(uptime)}</span> <button type="button" class="btn btn-link btn-sm p-0 ms-1 text-decoration-none badge bg-info-subtle text-primary border border-info-subtle" data-username="{h(uname)}" data-name="{h(c["name"] or uname)}" onclick="openLiveSpeedBtn(this)"><i class="fa-solid fa-chart-line"></i> Live</button>'
        else:
            ago = _format_offline_ago(router_now, sec.get("last_logged_out", ""))
            ago_html = f' <span class="text-danger small fw-bold">• {h(ago)}</span>' if ago else ""
            online_badge = f'<span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle rounded-pill px-2">○ Offline</span>{ago_html}'

        time_lbl, time_cls, st_lbl, st_cls = calculate_expiry_label(c["expiry_date"], c["status"])
        dname = h(c["name"] or uname)
        try:
            exp_fmt = datetime.datetime.strptime(c["expiry_date"][:19], "%Y-%m-%d %H:%M:%S").strftime("%b %d %Y %H:%M")
        except Exception:
            exp_fmt = h(c["expiry_date"])

        bind_badge = '<span class="badge bg-primary-subtle text-primary border border-primary-subtle"><i class="fa-solid fa-lock"></i> Bound</span>' if c.get("mac_address") else '<span class="badge bg-light text-muted border"><i class="fa-solid fa-lock-open"></i> Unbound</span>'
        olt_pon, olt_onuid, onu_sn = int(c.get("olt_pon") or 0), int(c.get("olt_onuid") or 0), (c.get("onu_sn") or "")
        onu_badge = f'<button type="button" class="badge bg-dark text-white border-0 mt-1" onclick="openOpticalModal({olt_pon}, {olt_onuid}, \'{h(onu_sn)}\')"><i class="fa-solid fa-signal text-info me-1"></i>PON{olt_pon}:{olt_onuid} ({h(onu_sn)})</button>' if olt_pon and olt_onuid else ""
        phone_html = h(c["phone"]) if c["phone"] else '<span class="text-muted">No Mobile</span>'
        search_txt = h((uname + " " + (c["name"] or "") + " " + (c["phone"] or "") + " " + onu_sn + " " + mac).lower())

        cust_rows_html += f"""
        <div class="card mb-2 p-2 border customer-card" data-search="{search_txt}">
            <div class="row align-items-center g-2">
                <div class="col-12 col-md-3">
                    <div class="d-flex align-items-center flex-wrap gap-1 mb-1">{online_badge}</div>
                    <div class="small text-secondary mb-1"><i class="fa-solid fa-arrow-down-up-across-line text-primary"></i> <b>{usage_gb:.2f} GB</b> &nbsp; <i class="fa-solid fa-network-wired text-muted"></i> <b>{h(ip)}</b></div>
                    <div class="small text-muted"><i class="fa-solid fa-fingerprint"></i> <span class="text-secondary">{h(mac)}</span> {bind_badge}</div>
                    {onu_badge}
                </div>
                <div class="col-12 col-md-3">
                    <div class="text-primary fw-bold small">#{c['id'] + 40500}</div>
                    <div class="text-primary small mb-1"><i class="fa-solid fa-phone"></i> {phone_html}</div>
                    <div class="fw-bold fs-6 text-dark">{dname}</div>
                    <div class="small text-muted"><i class="fa-solid fa-user text-primary"></i> {h(uname)} &nbsp; <i class="fa-solid fa-key text-muted"></i> {h(c['password'])}</div>
                </div>
                <div class="col-6 col-md-2">
                    <div class="small text-secondary"><i class="fa-solid fa-server"></i> {h(c['profile'])}</div>
                    <div class="small text-secondary mt-1"><i class="fa-solid fa-cubes"></i> {h(c['package_name'])}</div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="d-flex align-items-center flex-wrap gap-1 mb-1"><span class="badge {st_cls} rounded-pill">{st_lbl}</span><span class="badge {time_cls} rounded-pill">⌛ {time_lbl}</span></div>
                    <div class="small text-dark fw-bold mb-1"><i class="fa-solid fa-bangladeshi-taka-sign text-primary"></i> {c['package_price']} TK</div>
                    <div class="small text-secondary"><i class="fa-regular fa-calendar-days text-danger"></i> {exp_fmt}</div>
                </div>
                <div class="col-auto ms-auto">
                    <button type="button" class="btn btn-outline-primary rounded-circle p-0 d-flex align-items-center justify-content-center" style="width:42px;height:42px;"
                        data-id="{c['id']}" data-name="{dname}" data-username="{h(uname)}" data-status="{h(c['status'])}"
                        data-mac="{h(act.get('caller_id', '') if is_online else '')}" data-bound="{h(c.get('mac_address') or '')}"
                        data-pon="{olt_pon}" data-onuid="{olt_onuid}" data-sn="{h(onu_sn)}" onclick="openActionDrawer(this)">
                        <i class="fa-solid fa-sliders fs-5"></i>
                    </button>
                </div>
            </div>
        </div>"""

    pay_rows = ""
    for p in payments:
        ub = '<span class="badge bg-secondary">Used</span>' if p["status"] == "used" else '<span class="badge bg-success">Unused</span>'
        pay_rows += f"<tr><td><b>{h(p['trx_id'])}</b></td><td><b class='text-success'>{p['amount']} ৳</b></td><td><small>{h(p['sender_phone'])}</small></td><td><small>{h(p['received_at'])}</small></td><td>{ub}</td><td><b>{h(p['used_by'] or '-')}</b></td></tr>"

    def profile_options(selected):
        opts = list(profiles) + ([selected] if selected and selected not in profiles else [])
        res_opts = ""
        for p in opts:
            sel = "selected" if p == selected else ""
            res_opts += f'<option value="{h(p)}" {sel}>{h(p)}</option>'
        return res_opts

    edit_form_html = ""
    if edit_cust:
        sel_act = "selected" if edit_cust["status"] == "active" else ""
        sel_exp = "selected" if edit_cust["status"] == "expired" else ""
        edit_form_html = f"""
        <div class="card shadow border-primary mb-4"><div class="card-header bg-primary text-white fw-bold d-flex justify-content-between align-items-center"><span>গ্রাহক এডিট করুন: {h(edit_cust['username'])}</span><a href="/admin" class="btn btn-sm btn-light">বাতিল</a></div>
        <div class="card-body"><form method="POST" action="/admin/update-customer"><input type="hidden" name="cust_id" value="{edit_cust['id']}"><div class="row g-3">
            <div class="col-md-2"><label class="form-label fw-bold">PPPoE আইডি</label><input type="text" class="form-control" value="{h(edit_cust['username'])}" disabled></div>
            <div class="col-md-3"><label class="form-label fw-bold">গ্রাহকের নাম</label><input type="text" name="name" class="form-control" value="{h(edit_cust['name'])}"></div>
            <div class="col-md-2"><label class="form-label fw-bold">মোবাইল</label><input type="text" name="phone" class="form-control" value="{h(edit_cust['phone'])}"></div>
            <div class="col-md-2"><label class="form-label fw-bold">PPPoE পাসওয়ার্ড</label><input type="text" name="password" class="form-control" value="{h(edit_cust['password'])}"></div>
            <div class="col-md-3"><label class="form-label fw-bold">MikroTik প্রোফাইল</label><select name="profile" class="form-select">{profile_options(edit_cust['profile'])}</select></div>
            <div class="col-md-2"><label class="form-label fw-bold">প্যাকেজের নাম</label><input type="text" name="package_name" class="form-control" value="{h(edit_cust['package_name'])}"></div>
            <div class="col-md-2"><label class="form-label fw-bold">মাসিক বিল (৳)</label><input type="number" name="package_price" class="form-control" value="{edit_cust['package_price']}"></div>
            <div class="col-md-3"><label class="form-label fw-bold">মেয়াদ</label><input type="text" name="expiry_date" class="form-control" value="{h(edit_cust['expiry_date'])}"></div>
            <div class="col-md-2"><label class="form-label fw-bold">স্ট্যাটাস</label><select name="status" class="form-select"><option value="active" {sel_act}>Active</option><option value="expired" {sel_exp}>Expired</option></select></div>
        </div><div class="mt-3"><button type="submit" class="btn btn-success fw-bold px-4">আপডেট করুন</button> <a href="/admin" class="btn btn-secondary">বাতিল</a></div></form></div></div>"""

    msg_alert = f'<div class="alert alert-info alert-dismissible fade show">{h(msg)}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>' if msg else ""

    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - Admin Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>body{{background:#eef2f6;font-family:'Segoe UI',sans-serif;}}.card{{border-radius:12px;}}.stat-card{{border-radius:12px;padding:18px;color:white;}}.action-btn{{border-radius:10px;padding:10px 12px;font-weight:600;display:inline-flex;align-items:center;justify-content:center;gap:6px;font-size:14px;text-decoration:none;}}</style>
</head><body class="p-2 p-md-4"><div class="container-fluid">
    {admin_navbar("dashboard")} {msg_alert}
    <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
        <div class="d-flex flex-wrap align-items-center gap-2">{mk_badge} {db_badge} <span class="badge bg-dark"><i class="fa-solid fa-mobile-screen me-1 text-danger"></i> বিকাশ: {bkash_num}</span></div>
        <div class="d-flex gap-2">
            <a href="/admin/sync-mikrotik" class="btn btn-outline-primary fw-bold btn-sm" onclick="return confirm('MikroTik থেকে সব PPPoE ইউজার Import করবেন?');"><i class="fa-solid fa-rotate me-1"></i> MikroTik থেকে ইউজার সিঙ্ক</a>
            <button class="btn btn-success fw-bold btn-sm" data-bs-toggle="modal" data-bs-target="#newCustomerModal"><i class="fa-solid fa-user-plus me-1"></i> নতুন গ্রাহক</button>
        </div>
    </div>
    <div class="row g-3 mb-4">
        <div class="col-6 col-md-3"><div class="stat-card bg-primary shadow-sm"><small class="fw-bold opacity-75">মোট গ্রাহক</small><h2 class="fw-bold mb-0">{stats['total']}</h2></div></div>
        <div class="col-6 col-md-3"><div class="stat-card bg-success shadow-sm"><small class="fw-bold opacity-75">সচল গ্রাহক</small><h2 class="fw-bold mb-0">{stats['active']}</h2></div></div>
        <div class="col-6 col-md-3"><div class="stat-card bg-danger shadow-sm"><small class="fw-bold opacity-75">মেয়াদোত্তীর্ণ</small><h2 class="fw-bold mb-0">{stats['expired']}</h2></div></div>
        <div class="col-6 col-md-3"><div class="stat-card bg-dark shadow-sm"><small class="fw-bold opacity-75">মোট কালেকশন</small><h2 class="fw-bold mb-0">{stats['total_revenue']} ৳</h2></div></div>
    </div>
    {edit_form_html}
    <div class="card p-3 mb-3 bg-white border-0 shadow-sm"><div class="row align-items-center g-2"><div class="col-md-6"><input type="text" id="custSearchInput" class="form-control" placeholder="গ্রাহকের নাম, ইউজার আইডি, মোবাইল, MAC বা ONU SN দিয়ে খুঁজুন..." onkeyup="filterCustomerCards()"></div><div class="col-md-6 text-md-end text-muted small">মোট: <b>{len(customers)} জন</b> | অনলাইন: <b>{len(actives)} জন</b></div></div></div>
    <div id="customerCardsContainer">{cust_rows_html or '<div class="alert alert-light text-center">কোনো গ্রাহক নেই।</div>'}</div>
    <div class="card shadow-sm mt-4"><div class="card-header bg-white py-3"><h5 class="fw-bold mb-0"><i class="fa-solid fa-receipt me-2 text-danger"></i>সাম্প্রতিক বিকাশ পেমেন্ট হিস্ট্রি</h5></div><div class="card-body p-0 table-responsive"><table class="table table-sm table-hover align-middle mb-0"><thead class="table-light"><tr><th>TrxID</th><th>টাকা</th><th>নম্বর</th><th>সময়</th><th>স্ট্যাটাস</th><th>ইউজার</th></tr></thead><tbody>{pay_rows}</tbody></table></div></div>
</div>

<div class="modal fade" id="actionModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content border-0 shadow"><div class="modal-header border-bottom"><h5 class="modal-title fw-bold" id="actionModalTitle">Actions</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body p-4"><div class="row g-2" id="actionButtonsGrid"></div></div></div></div></div>

<div class="modal fade" id="speedModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content border-0 shadow" style="border-radius:16px;">
    <div class="modal-header border-0 pb-1">
        <h6 class="modal-title fw-bold text-secondary" id="speedUserLabel">Live Traffic Monitor</h6>
        <span class="badge bg-success ms-2" id="speedOnlineStatus">● Live</span>
    </div>
    <div class="modal-body pt-2 pb-3 px-4">
        <div class="mb-2" style="font-size:15px;"><b style="color:#16a34a;">Download:</b> <span id="dlText" class="fw-bold text-dark">0 bps</span></div>
        <hr class="my-2 opacity-25">
        <div class="mb-3" style="font-size:15px;"><b style="color:#dc2626;">Upload:</b> <span id="ulText" class="fw-bold text-dark">0 bps</span></div>
        <div class="d-flex justify-content-end gap-3 small mb-2 text-secondary">
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#ef4444;"></span> Upload</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#22c55e;"></span> Download</span>
        </div>
        <div style="height:220px;"><canvas id="speedChart"></canvas></div>
        <div class="text-end mt-3">
            <button type="button" class="btn px-4 fw-bold text-white" style="background:#5b6b7f;border-radius:10px;" data-bs-dismiss="modal">Close</button>
        </div>
    </div>
</div></div></div>

<div class="modal fade" id="opticalModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content border-0 shadow"><div class="modal-header bg-dark text-white"><h5 class="modal-title fw-bold"><i class="fa-solid fa-signal text-info me-2"></i>ONU Optical Signal</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><div class="modal-body" id="opticalModalBody">লোড হচ্ছে...</div></div></div></div>
<div class="modal fade" id="newCustomerModal" tabindex="-1"><div class="modal-dialog modal-lg"><div class="modal-content"><div class="modal-header bg-primary text-white"><h5 class="modal-title fw-bold">নতুন গ্রাহক যুক্ত করুন</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><form method="POST" action="/admin/create-customer"><div class="modal-body"><div class="row g-3"><div class="col-md-6"><label class="form-label fw-bold">PPPoE ইউজার আইডি *</label><input type="text" name="username" class="form-control" required></div><div class="col-md-6"><label class="form-label fw-bold">PPPoE পাসওয়ার্ড *</label><input type="text" name="password" class="form-control" value="123456" required></div><div class="col-md-6"><label class="form-label fw-bold">গ্রাহকের নাম</label><input type="text" name="name" class="form-control"></div><div class="col-md-6"><label class="form-label fw-bold">মোবাইল নম্বর</label><input type="text" name="phone" class="form-control"></div><div class="col-md-4"><label class="form-label fw-bold">প্যাকেজ নাম</label><input type="text" name="package_name" class="form-control" value="25 Mbps"></div><div class="col-md-4"><label class="form-label fw-bold">মাসিক বিল (৳)</label><input type="number" name="package_price" class="form-control" value="525"></div><div class="col-md-4"><label class="form-label fw-bold">প্রোফাইল</label><select name="profile" class="form-select">{profile_options('default')}</select></div><div class="col-md-6"><label class="form-label fw-bold">প্রাথমিক মেয়াদ (দিন)</label><input type="number" name="days_valid" class="form-control" value="30"></div></div></div><div class="modal-footer"><button type="button" class="btn btn-secondary" data-bs-dismiss="modal">বন্ধ</button><button type="submit" class="btn btn-primary fw-bold">সংরক্ষণ ও সিঙ্ক</button></div></form></div></div></div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
function filterCustomerCards(){{const q=document.getElementById("custSearchInput").value.toLowerCase();document.querySelectorAll(".customer-card").forEach(c=>c.style.display=c.getAttribute("data-search").includes(q)?"":"none");}}
let actionModalInstance=null;
function openActionDrawer(btn){{
    const d=btn.dataset;
    document.getElementById("actionModalTitle").innerText="Customer: #"+(parseInt(d.id)+40500)+" ("+d.name+")";
    const suspendLabel=d.status==="active"?'<i class="fa-solid fa-circle-pause"></i> Suspend':'<i class="fa-solid fa-circle-play"></i> Activate';
    const macBtn=d.bound?`<a href="/admin/bind-mac/${{d.id}}?unbind=1" class="btn btn-outline-dark w-100 action-btn"><i class="fa-solid fa-lock-open"></i> Unbind MAC</a>`:(d.mac?`<a href="/admin/bind-mac/${{d.id}}?mac=${{encodeURIComponent(d.mac)}}" class="btn btn-outline-dark w-100 action-btn"><i class="fa-solid fa-lock"></i> Bind MAC</a>`:`<button disabled class="btn btn-outline-secondary w-100 action-btn"><i class="fa-solid fa-lock"></i> Offline</button>`);
    const onuBtn=(parseInt(d.pon)>0&&parseInt(d.onuid)>0)?`<div class="col-6"><button onclick="actionModalInstance.hide();openOpticalModal(${{d.pon}},${{d.onuid}},'${{d.sn}}')" class="btn btn-outline-dark w-100 action-btn"><i class="fa-solid fa-signal text-info"></i> ONU Power</button></div>`:'';
    document.getElementById("actionButtonsGrid").innerHTML=`
        <div class="col-6"><a href="/admin?edit_id=${{d.id}}" class="btn btn-outline-primary w-100 action-btn"><i class="fa-solid fa-pen-to-square"></i> Edit</a></div>
        <div class="col-6"><a href="/admin/toggle-suspend/${{d.id}}" class="btn btn-outline-warning w-100 action-btn">${{suspendLabel}}</a></div>
        <div class="col-6"><a href="/admin/quick-extend/${{d.id}}" class="btn btn-outline-success w-100 action-btn"><i class="fa-solid fa-calendar-plus"></i> Recharge (+30d)</a></div>
        <div class="col-6"><a href="/admin/kick/${{encodeURIComponent(d.username)}}" class="btn btn-outline-info w-100 action-btn"><i class="fa-solid fa-plug-circle-xmark"></i> Disconnect</a></div>
        <div class="col-6"><button onclick="actionModalInstance.hide();openLiveSpeed('${{d.username}}','${{d.name}}')" class="btn btn-outline-primary w-100 action-btn"><i class="fa-solid fa-chart-line"></i> Live Speed</button></div>
        <div class="col-6">${{macBtn}}</div>${{onuBtn}}
        <div class="col-6"><a href="/?query=${{encodeURIComponent(d.username)}}" target="_blank" class="btn btn-outline-secondary w-100 action-btn"><i class="fa-solid fa-arrow-up-right-from-square"></i> Portal</a></div>
        <div class="col-12 mt-2"><a href="/admin/delete/${{d.id}}" onclick="return confirm('আপনি কি নিশ্চিত এই গ্রাহককে মুছে ফেলতে চান?');" class="btn btn-outline-danger w-100 action-btn"><i class="fa-solid fa-trash"></i> Delete Account</a></div>`;
    actionModalInstance=new bootstrap.Modal(document.getElementById("actionModal"));actionModalInstance.show();
}}
function openLiveSpeedBtn(btn){{openLiveSpeed(btn.dataset.username,btn.dataset.name);}}

function formatBps(bps){{
    bps=Number(bps)||0;
    if(bps<=0)return "0 bps";
    if(bps<1000)return bps.toFixed(0)+" bps";
    if(bps<1000000)return (bps/1000).toFixed(2).replace(/\\.00$/,"")+" Kbps";
    return (bps/1000000).toFixed(2).replace(/\\.00$/,"")+" Mbps";
}}

let speedTimer=null, speedChartObj=null, speedSid=0, speedAbort=null;
function initSpeedChart(){{
    const canvas=document.getElementById("speedChart");
    const ctx=canvas.getContext("2d");
    const gUl=ctx.createLinearGradient(0,0,0,200);
    gUl.addColorStop(0,"rgba(239,68,68,0.28)");gUl.addColorStop(1,"rgba(239,68,68,0.0)");
    const gDl=ctx.createLinearGradient(0,0,0,200);
    gDl.addColorStop(0,"rgba(34,197,94,0.28)");gDl.addColorStop(1,"rgba(34,197,94,0.0)");
    if(speedChartObj)speedChartObj.destroy();
    speedChartObj=new Chart(ctx,{{
        type:"line",
        data:{{
            labels:[],
            datasets:[
                {{label:"Upload",data:[],borderColor:"#ef4444",backgroundColor:gUl,borderWidth:2.5,tension:0.35,fill:true,pointRadius:0}},
                {{label:"Download",data:[],borderColor:"#22c55e",backgroundColor:gDl,borderWidth:2.5,tension:0.35,fill:true,pointRadius:0}}
            ]
        }},
        options:{{
            responsive:true,maintainAspectRatio:false,animation:false,
            plugins:{{legend:{{display:false}}}},
            scales:{{
                x:{{grid:{{display:false}},ticks:{{maxTicksLimit:6,color:"#64748b",font:{{size:11}}}}}},
                y:{{beginAtZero:true,border:{{display:false,dash:[4,4]}},grid:{{color:"#e2e8f0",tickBorderDash:[4,4]}},ticks:{{maxTicksLimit:5,color:"#64748b",font:{{size:11}},callback:v=>v===0?"0":formatBps(v)}}}}
            }}
        }}
    }});
}}

function stopSpeedMonitor(){{
    speedSid++;
    if(speedTimer){{clearTimeout(speedTimer);speedTimer=null;}}
    if(speedAbort){{speedAbort.abort();speedAbort=null;}}
}}

function openLiveSpeed(u,n){{
    stopSpeedMonitor();
    const mySid=speedSid;
    document.getElementById("speedUserLabel").innerText=n+" ("+u+")";
    document.getElementById("dlText").innerText="লোড হচ্ছে...";
    document.getElementById("ulText").innerText="লোড হচ্ছে...";
    initSpeedChart();
    new bootstrap.Modal(document.getElementById("speedModal")).show();
    pollSpeedLoop(u,mySid);
}}

async function pollSpeedLoop(u,mySid){{
    if(mySid!==speedSid)return;
    speedAbort=new AbortController();
    try{{
        const r=await fetch('/api/live-speed/'+encodeURIComponent(u),{{signal:speedAbort.signal}});
        if(mySid!==speedSid)return;
        const d=await r.json();
        const dl=d.dl_bps!==undefined?d.dl_bps:Math.round((d.rx_mbps||0)*1000000);
        const ul=d.ul_bps!==undefined?d.ul_bps:Math.round((d.tx_mbps||0)*1000000);
        document.getElementById("dlText").innerText=formatBps(dl);
        document.getElementById("ulText").innerText=formatBps(ul);
        const sb=document.getElementById("speedOnlineStatus");
        sb.className=d.online?"badge bg-success ms-2":"badge bg-secondary ms-2";
        sb.innerText=d.online?"● Live":"○ Offline";
        if(speedChartObj){{
            const t=new Date().toLocaleTimeString('en-GB',{{hour12:false}});
            speedChartObj.data.labels.push(t);
            speedChartObj.data.datasets[0].data.push(ul);
            speedChartObj.data.datasets[1].data.push(dl);
            if(speedChartObj.data.labels.length>20){{
                speedChartObj.data.labels.shift();
                speedChartObj.data.datasets[0].data.shift();
                speedChartObj.data.datasets[1].data.shift();
            }}
            speedChartObj.update('none');
        }}
    }}catch(e){{
        if(e.name==="AbortError")return;
    }}
    if(mySid===speedSid){{
        speedTimer=setTimeout(()=>pollSpeedLoop(u,mySid),250);
    }}
}}
document.getElementById("speedModal").addEventListener('hidden.bs.modal',stopSpeedMonitor);

async function openOpticalModal(pon,onuid,sn){{
    const b=document.getElementById("opticalModalBody");b.innerHTML='<div class="text-center py-4"><div class="spinner-border text-primary"></div><div class="mt-2">লোড হচ্ছে...</div></div>';
    new bootstrap.Modal(document.getElementById("opticalModal")).show();
    try{{const r=await fetch(`/api/olt/optical?pon_id=${{pon}}&onu_id=${{onuid}}`);const d=await r.json();if(d.error){{b.innerHTML=`<div class="alert alert-danger">${{d.error}}</div>`;return;}}
    let sb='<span class="badge bg-secondary">Unknown</span>';if(d.rx_dbm!==null&&d.rx_dbm!==undefined){{sb=d.rx_dbm>=-25?'<span class="badge bg-success">ভালো (Good)</span>':(d.rx_dbm>=-27.5?'<span class="badge bg-warning text-dark">দুর্বল হচ্ছে</span>':'<span class="badge bg-danger">খুব দুর্বল / Critical</span>');}}
    b.innerHTML=`<div class="mb-2"><b>ONU:</b> GPON0/${{pon}}:${{onuid}} ${{sn?'('+sn+')':''}} ${{sb}}</div><table class="table table-bordered table-sm mb-0"><tr><th>Rx Power</th><td class="fw-bold text-primary">${{d.rx_power}}</td></tr><tr><th>Tx Power</th><td>${{d.tx_power}}</td></tr><tr><th>Distance</th><td>${{d.distance}}</td></tr><tr><th>Temperature</th><td>${{d.temperature}}</td></tr><tr><th>Voltage / Bias</th><td>${{d.voltage}} / ${{d.bias_current}}</td></tr></table>`;}}catch(e){{b.innerHTML=`<div class="alert alert-danger">${{e}}</div>`;}}
}}
</script></body></html>"""


@app.post("/admin/create-customer")
def create_customer_action(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), password: str = Form(...), name: str = Form(""), phone: str = Form(""), package_name: str = Form("25 Mbps"), package_price: int = Form(525), profile: str = Form("default"), days_valid: int = Form(30)):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    ok, _ = database.create_customer(username, password, name, phone, package_name, package_price, profile, days_valid)
    if ok: background_tasks.add_task(sync_secret_to_mikrotik, username, password, profile)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/update-customer")
def update_customer_action(request: Request, background_tasks: BackgroundTasks, cust_id: int = Form(...), name: str = Form(""), phone: str = Form(""), password: str = Form(...), package_name: str = Form("25 Mbps"), package_price: int = Form(525), profile: str = Form("default"), expiry_date: str = Form(...), status: str = Form("active")):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    database.update_customer_full(cust_id, name, phone, password, package_name, package_price, profile, expiry_date, status)
    cust = database.get_customer_by_id(cust_id)
    if cust:
        background_tasks.add_task(sync_secret_to_mikrotik, cust["username"], password, profile)
        background_tasks.add_task(unblock_in_mikrotik if status == "active" else block_in_mikrotik, cust["username"])
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/quick-extend/{cust_id}")
def quick_extend_action(request: Request, background_tasks: BackgroundTasks, cust_id: int):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    username, _ = database.extend_customer(cust_id, days=30)
    if username: background_tasks.add_task(unblock_in_mikrotik, username)
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/toggle-suspend/{cust_id}")
def toggle_suspend_action(request: Request, background_tasks: BackgroundTasks, cust_id: int):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    cust = database.get_customer_by_id(cust_id)
    if cust:
        ns = "expired" if cust["status"] == "active" else "active"
        database.update_customer_full(cust_id, cust["name"], cust["phone"], cust["password"], cust["package_name"], cust["package_price"], cust["profile"], cust["expiry_date"], ns)
        background_tasks.add_task(unblock_in_mikrotik if ns == "active" else block_in_mikrotik, cust["username"])
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/kick/{username}")
def kick_user_action(request: Request, username: str):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    try:
        api = get_mikrotik_conn(); api.kick_user(username); api.close()
    except Exception: pass
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/bind-mac/{cust_id}")
def bind_mac_action(request: Request, cust_id: int, mac: str = "", unbind: int = 0):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    cust = database.get_customer_by_id(cust_id)
    if cust:
        tmac = "" if unbind else mac.strip()
        try:
            api = get_mikrotik_conn(); api.bind_mac(cust["username"], tmac); api.close()
            database.update_customer_mac(cust_id, tmac)
        except Exception as e:
            return RedirectResponse(url=f"/admin?msg=MAC+Bind+ত্রুটি:+{e}", status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/delete/{cust_id}")
def delete_customer_action(request: Request, background_tasks: BackgroundTasks, cust_id: int):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    username = database.delete_customer(cust_id)
    if username: background_tasks.add_task(delete_secret_from_mikrotik, username)
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/sync-mikrotik")
def sync_mikrotik_users(request: Request):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    try:
        api = get_mikrotik_conn(); secrets_list = api.get_all_secrets(); api.close()
        added = database.upsert_synced_secrets(secrets_list)
        return RedirectResponse(url=f"/admin?msg=MikroTik+থেকে+{len(secrets_list)}+টি+পাওয়া+গেছে,+নতুন+{added}+জন+যুক্ত+হয়েছে।", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/admin?msg=MikroTik+সিঙ্ক+ব্যর্থ:+{e}", status_code=303)


# --- OLT MONITOR ---
@app.get("/admin/olt", response_class=HTMLResponse)
def olt_dashboard(request: Request, background_tasks: BackgroundTasks, pon_id: str = "2", msg: Optional[str] = None):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")

    company = h(get_company_name())
    customers = database.get_all_customers()
    cust_by_uname = {c["username"].strip().lower(): c for c in customers}

    actives, secrets_list = {}, []
    try:
        api = get_mikrotik_conn()
        actives = api.get_active_pppoe()
        secrets_list = api.get_all_secrets()
        api.close()
    except Exception:
        pass

    cust_by_mac = {}
    for c in customers:
        if c.get("mac_address"):
            cust_by_mac[normalize_mac(c["mac_address"])] = c
    for s in secrets_list:
        uname_l = (s.get("name") or "").strip().lower()
        if uname_l not in cust_by_uname:
            continue
        for raw_mac in (s.get("caller_id", ""), s.get("last_caller_id", "")):
            cid = normalize_mac(raw_mac)
            if cid:
                cust_by_mac[cid] = cust_by_uname[uname_l]
    for uname, act in actives.items():
        cid = normalize_mac(act.get("caller_id", ""))
        if cid and uname.strip().lower() in cust_by_uname:
            cust_by_mac[cid] = cust_by_uname[uname.strip().lower()]

    cust_by_onu, cust_by_sn = {}, {}
    for c in customers:
        if c.get("olt_pon") and c.get("olt_onuid"):
            cust_by_onu[(int(c["olt_pon"]), int(c["olt_onuid"]))] = c
        if c.get("onu_sn"):
            cust_by_sn[c["onu_sn"].strip().upper()] = c

    olt_data = {"pon_ports": [], "onus": [], "online": 0, "offline": 0, "total": 0, "line_profiles": [], "service_profiles": [], "vlan_presets": []}
    olt_error = ""
    try:
        olt_data = get_olt_conn().get_onus(pon_id=None if pon_id == "all" else pon_id)
    except Exception as e:
        olt_error = str(e)

    pon_all_cls = "btn-primary" if pon_id == "all" else "btn-outline-primary"
    pon_buttons = f'<a href="/admin/olt?pon_id=all" class="btn btn-sm {pon_all_cls} fw-bold">সব PON</a>'
    for p in olt_data["pon_ports"]:
        p_cls = "btn-primary" if str(p["id"]) == str(pon_id) else "btn-outline-primary"
        pon_buttons += f' <a href="/admin/olt?pon_id={p["id"]}" class="btn btn-sm {p_cls} fw-bold">{h(p["name"])}</a>'

    cust_options = "".join(
        f'<option value="{c["id"]}">{h(c["name"] or c["username"])} ({h(c["username"])}) {h(c.get("phone") or "")}</option>'
        for c in customers
    )
    line_opts = "".join(f'<option value="{h(lp)}">{h(lp)}</option>' for lp in (olt_data.get("line_profiles") or ["line_vlan_712", "line_vlan_200", "2101", "N/A"]))
    srv_opts = "".join(f'<option value="{h(sp)}">{h(sp)}</option>' for sp in (olt_data.get("service_profiles") or ["srv_vlan712", "srv_vlan200", "2101", "N/A"]))
    presets = olt_data.get("vlan_presets") or []

    auto_links_to_save = []
    rows_html = ""
    for o in olt_data["onus"]:
        st_badge = '<span class="badge bg-success">Online</span>' if o["online"] else '<span class="badge bg-danger">Offline</span>'
        db_linked = cust_by_onu.get((o["pon_id"], o["onu_id"])) or cust_by_sn.get((o["sn"] or "").strip().upper())
        onu_macs = o.get("macs", [])

        matched_list, is_auto = [], False
        if db_linked:
            matched_list = [db_linked]
        else:
            seen_ids = set()
            for omac in onu_macs:
                mc = cust_by_mac.get(omac)
                if mc and mc["id"] not in seen_ids:
                    matched_list.append(mc)
                    seen_ids.add(mc["id"])
                    is_auto = True
            if len(matched_list) == 1:
                auto_links_to_save.append((matched_list[0]["id"], o["pon_id"], o["onu_id"], o["sn"]))

        cust_search_extra = " ".join(onu_macs)
        if matched_list:
            linked_blocks = []
            for mc in matched_list:
                uname = mc["username"]
                act = actives.get(uname, {})
                cmac = act.get("caller_id") or mc.get("mac_address") or (onu_macs[0] if onu_macs else "")
                cname = h(mc["name"] or uname)
                st_pill = '<span class="badge bg-success-subtle text-success">Active</span>' if mc["status"] == "active" else '<span class="badge bg-danger-subtle text-danger">Expired</span>'
                auto_pill = '<span class="badge bg-info-subtle text-primary border border-info-subtle">Auto MAC</span>' if is_auto else ""
                phone_str = f'<div class="small text-muted"><i class="fa-solid fa-phone text-primary"></i> {h(mc["phone"])}</div>' if mc.get("phone") else ""
                mac_str = f'<div class="small text-muted"><i class="fa-solid fa-fingerprint"></i> MAC: <b>{h(cmac)}</b></div>' if cmac else ""
                linked_blocks.append(f'<div class="mb-1"><div class="fw-bold text-primary">{cname} <small class="text-dark">({h(uname)})</small> {st_pill} {auto_pill}</div>{phone_str}{mac_str}</div>')
                cust_search_extra += f" {uname} {mc.get('name','')} {mc.get('phone','')} {cmac}"
            linked_html = "".join(linked_blocks)
        else:
            mac_hint = f'<div class="small text-muted mt-1"><i class="fa-solid fa-fingerprint"></i> {", ".join(h(m) for m in onu_macs)}</div>' if onu_macs else ""
            linked_html = f'<span class="text-muted small">গ্রাহক পাওয়া যায়নি</span>{mac_hint}'

        cur_vlan = o.get("vlan", "N/A")
        vlan_badge = f'<span class="badge bg-primary fs-6">VLAN {h(cur_vlan)}</span>' if cur_vlan != "N/A" else '<span class="badge bg-secondary">N/A</span>'
        prof_sub = f'<div class="small text-muted">{h(o.get("line_profile","N/A"))} / {h(o.get("service_profile","N/A"))}</div>'

        quick_vlan_btns = ""
        for pr in presets:
            is_cur = (pr["linename"] == o.get("line_profile"))
            btn_cls = "btn-secondary disabled" if is_cur else "btn-outline-success"
            quick_vlan_btns += f"""
            <form method="POST" action="/admin/olt/change-vlan" class="d-inline">
                <input type="hidden" name="pon_id" value="{o['pon_id']}"><input type="hidden" name="onu_id" value="{o['onu_id']}">
                <input type="hidden" name="linename" value="{h(pr['linename'])}"><input type="hidden" name="srvname" value="{h(pr['srvname'])}">
                <input type="hidden" name="back_pon" value="{h(pon_id)}">
                <button type="submit" class="btn btn-sm {btn_cls} fw-bold" onclick="return confirm('GPON0/{o['pon_id']}:{o['onu_id']} এর VLAN পরিবর্তন করে {h(pr['vlan'])} করবেন?');">VLAN {h(pr['vlan'])}</button>
            </form>"""

        search_blob = f"{o['label']} {o['sn']} {o['description']} {o['model']} {cur_vlan} {cust_search_extra}".lower()
        rows_html += f"""
        <tr class="onu-row" data-search="{h(search_blob)}">
            <td><div class="fw-bold">{h(o['label'])}</div><small class="text-muted">{h(o['model'])}</small></td>
            <td>{st_badge}</td>
            <td>{linked_html}</td>
            <td><code>{h(o['sn'])}</code><div class="small text-muted">{h(o['description'])}</div></td>
            <td>{vlan_badge}{prof_sub}</td>
            <td><div class="d-flex flex-wrap gap-1">{quick_vlan_btns}
                <button type="button" class="btn btn-sm btn-outline-dark" onclick="openVlanModal({o['pon_id']},{o['onu_id']},'{h(o.get('line_profile','N/A'))}','{h(o.get('service_profile','N/A'))}')">Custom</button>
            </div></td>
            <td class="text-nowrap">
                <button class="btn btn-sm btn-info text-white fw-bold" onclick="openOpticalModal({o['pon_id']},{o['onu_id']},'{h(o['sn'])}')"><i class="fa-solid fa-signal"></i> Power</button>
                <button class="btn btn-sm btn-outline-primary fw-bold" onclick="openLinkModal({o['pon_id']},{o['onu_id']},'{h(o['sn'])}')"><i class="fa-solid fa-link"></i> Link</button>
                <a href="/admin/olt/reboot?pon_id={o['pon_id']}&onu_id={o['onu_id']}&back_pon={h(pon_id)}" onclick="return confirm('এই ONU টি রিবুট করবেন?');" class="btn btn-sm btn-outline-danger"><i class="fa-solid fa-rotate-right"></i></a>
            </td>
        </tr>"""

    if auto_links_to_save:
        background_tasks.add_task(save_auto_links_task, auto_links_to_save)

    err_html = f'<div class="alert alert-danger"><b>OLT কানেকশন ত্রুটি:</b> {h(olt_error)} — <a href="/admin/settings">Settings</a> চেক করুন।</div>' if olt_error else ""
    msg_html = f'<div class="alert alert-success alert-dismissible fade show">{h(msg)}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>' if msg else ""

    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - OLT Monitor</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>body{{background:#eef2f6;font-family:'Segoe UI',sans-serif;}}</style>
</head><body class="p-2 p-md-4"><div class="container-fluid">
    {admin_navbar("olt")} {msg_html} {err_html}
    <div class="row g-3 mb-3">
        <div class="col-4"><div class="card p-3 bg-primary text-white border-0"><small>মোট ONU</small><h3 class="fw-bold mb-0">{olt_data['total']}</h3></div></div>
        <div class="col-4"><div class="card p-3 bg-success text-white border-0"><small>অনলাইন</small><h3 class="fw-bold mb-0">{olt_data['online']}</h3></div></div>
        <div class="col-4"><div class="card p-3 bg-danger text-white border-0"><small>অফলাইন</small><h3 class="fw-bold mb-0">{olt_data['offline']}</h3></div></div>
    </div>
    <div class="card p-3 mb-3 border-0 shadow-sm"><div class="d-flex flex-wrap justify-content-between align-items-center gap-2"><div class="d-flex flex-wrap gap-1">{pon_buttons}</div><input type="text" id="onuSearch" class="form-control" style="max-width:340px;" placeholder="গ্রাহকের নাম, MAC, SN, VLAN বা ONU খুঁজুন..." onkeyup="filterOnus()"></div></div>
    <div class="card border-0 shadow-sm"><div class="table-responsive"><table class="table table-hover align-middle mb-0">
        <thead class="table-light"><tr><th>ONU / Model</th><th>Status</th><th>গ্রাহক ও MAC</th><th>Serial (SN)</th><th>বর্তমান VLAN</th><th>১-ক্লিক VLAN বদল</th><th>Action</th></tr></thead>
        <tbody>{rows_html or '<tr><td colspan="7" class="text-center py-4 text-muted">কোনো ONU পাওয়া যায়নি</td></tr>'}</tbody>
    </table></div></div>
</div>

<div class="modal fade" id="opticalModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content border-0 shadow"><div class="modal-header bg-dark text-white"><h5 class="modal-title fw-bold"><i class="fa-solid fa-signal text-info me-2"></i>ONU Optical Power</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div><div class="modal-body" id="opticalModalBody">লোড হচ্ছে...</div></div></div></div>

<div class="modal fade" id="linkModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content border-0 shadow"><div class="modal-header bg-primary text-white"><h5 class="modal-title fw-bold">গ্রাহকের সাথে ONU লিংক করুন</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
<form method="POST" action="/admin/olt/link"><div class="modal-body"><input type="hidden" name="pon_id" id="linkPon"><input type="hidden" name="onu_id" id="linkOnu"><input type="hidden" name="onu_sn" id="linkSn"><input type="hidden" name="back_pon" value="{h(pon_id)}"><p class="small text-muted" id="linkLabel"></p><label class="form-label fw-bold">গ্রাহক সিলেক্ট করুন:</label><select name="cust_id" class="form-select" required>{cust_options}</select></div><div class="modal-footer"><button type="submit" class="btn btn-primary fw-bold">লিংক সেভ করুন</button></div></form></div></div></div>

<div class="modal fade" id="vlanModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content border-0 shadow"><div class="modal-header bg-dark text-white"><h5 class="modal-title fw-bold">কাস্টম VLAN / Profile সেট করুন</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
<form method="POST" action="/admin/olt/change-vlan"><div class="modal-body"><input type="hidden" name="pon_id" id="vPon"><input type="hidden" name="onu_id" id="vOnu"><input type="hidden" name="back_pon" value="{h(pon_id)}"><p class="fw-bold" id="vLabel"></p><div class="mb-3"><label class="form-label fw-bold">Line Profile</label><select name="linename" id="vLine" class="form-select">{line_opts}</select></div><div class="mb-3"><label class="form-label fw-bold">Service Profile</label><select name="srvname" id="vSrv" class="form-select">{srv_opts}</select></div></div><div class="modal-footer"><button type="submit" class="btn btn-success fw-bold">VLAN সেভ করুন</button></div></form></div></div></div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
function filterOnus(){{const q=document.getElementById("onuSearch").value.toLowerCase();document.querySelectorAll(".onu-row").forEach(r=>r.style.display=r.getAttribute("data-search").includes(q)?"":"none");}}
function openLinkModal(p,o,sn){{document.getElementById("linkPon").value=p;document.getElementById("linkOnu").value=o;document.getElementById("linkSn").value=sn;document.getElementById("linkLabel").innerText=`ONU: GPON0/${{p}}:${{o}} (SN: ${{sn}})`;new bootstrap.Modal(document.getElementById("linkModal")).show();}}
function openVlanModal(p,o,lp,sp){{document.getElementById("vPon").value=p;document.getElementById("vOnu").value=o;document.getElementById("vLabel").innerText=`ONU: GPON0/${{p}}:${{o}}`;document.getElementById("vLine").value=lp;document.getElementById("vSrv").value=sp;new bootstrap.Modal(document.getElementById("vlanModal")).show();}}
async function openOpticalModal(p,o,sn){{
    const b=document.getElementById("opticalModalBody");b.innerHTML='<div class="text-center py-4"><div class="spinner-border text-primary"></div><div class="mt-2">লোড হচ্ছে...</div></div>';
    new bootstrap.Modal(document.getElementById("opticalModal")).show();
    try{{const r=await fetch(`/api/olt/optical?pon_id=${{p}}&onu_id=${{o}}`);const d=await r.json();if(d.error){{b.innerHTML=`<div class="alert alert-danger">${{d.error}}</div>`;return;}}
    let sb='<span class="badge bg-secondary">Unknown</span>';if(d.rx_dbm!==null&&d.rx_dbm!==undefined){{sb=d.rx_dbm>=-25?'<span class="badge bg-success">ভালো (Good)</span>':(d.rx_dbm>=-27.5?'<span class="badge bg-warning text-dark">দুর্বল হচ্ছে</span>':'<span class="badge bg-danger">খুব দুর্বল / Critical</span>');}}
    b.innerHTML=`<div class="mb-2"><b>ONU:</b> GPON0/${{p}}:${{o}} (${{sn}}) ${{sb}}</div><table class="table table-bordered table-sm mb-0"><tr><th>Rx Power</th><td class="fw-bold text-primary">${{d.rx_power}}</td></tr><tr><th>Tx Power</th><td>${{d.tx_power}}</td></tr><tr><th>Distance</th><td>${{d.distance}}</td></tr><tr><th>Temperature</th><td>${{d.temperature}}</td></tr><tr><th>Voltage / Bias</th><td>${{d.voltage}} / ${{d.bias_current}}</td></tr></table>`;}}catch(e){{b.innerHTML=`<div class="alert alert-danger">${{e}}</div>`;}}
}}
</script></body></html>"""


@app.post("/admin/olt/change-vlan")
def olt_change_vlan(request: Request, pon_id: int = Form(...), onu_id: int = Form(...), linename: str = Form(...), srvname: str = Form(...), back_pon: str = Form("2")):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    try:
        get_olt_conn().set_onu_vlan_profile(pon_id, onu_id, linename, srvname)
        return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&msg=GPON0/{pon_id}:{onu_id}+এর+VLAN+({linename})+সফলভাবে+পরিবর্তন+হয়েছে!", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&msg=VLAN+পরিবর্তন+ব্যর্থ:+{e}", status_code=303)


@app.get("/api/olt/optical")
def api_olt_optical(request: Request, pon_id: int, onu_id: int):
    if not is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        return get_olt_conn().get_onu_optical(pon_id, onu_id)
    except Exception as e:
        return {"error": str(e)}


@app.post("/admin/olt/link")
def olt_link_customer(request: Request, cust_id: int = Form(...), pon_id: int = Form(...), onu_id: int = Form(...), onu_sn: str = Form(""), back_pon: str = Form("2")):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    database.link_customer_onu(cust_id, pon_id, onu_id, onu_sn)
    return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&msg=গ্রাহকের+সাথে+ONU+লিংক+করা+হয়েছে!", status_code=303)


@app.get("/admin/olt/reboot")
def olt_reboot_onu(request: Request, pon_id: int, onu_id: int, back_pon: str = "2"):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    try:
        get_olt_conn().reboot_onu(pon_id, onu_id)
        return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&msg=GPON0/{pon_id}:{onu_id}+রিবুট+কমান্ড+পাঠানো+হয়েছে!", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&msg=Reboot+ব্যর্থ:+{e}", status_code=303)


# --- SETTINGS & BACKUP ---
@app.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: Optional[str] = None):
    if not is_authenticated(request):
        return RedirectResponse(url="/login")
    s = database.get_all_settings()
    company = h(s.get("company_name", "সাইফুল ডিস এন্ড ইন্টারনেট 🛜"))
    r_opts = "".join(f'<option value="{k}" {"selected" if s.get("router_driver")==k else ""}>{k}</option>' for k in ROUTER_DRIVERS.keys())
    o_opts = "".join(f'<option value="{k}" {"selected" if s.get("olt_driver")==k else ""}>{k}</option>' for k in OLT_DRIVERS.keys())
    bm = s.get("block_mode", "address_list")
    bm_al = "selected" if bm == "address_list" else ""
    bm_ds = "selected" if bm == "disable_secret" else ""
    bm_bo = "selected" if bm == "both" else ""
    db_status = '<span class="badge bg-success fs-6">PostgreSQL চালু আছে (ডেটা স্থায়ী থাকবে ✅)</span>' if database.IS_POSTGRES else '<span class="badge bg-warning text-dark fs-6">SQLite চলছে (Render-এ স্থায়ী করতে DATABASE_URL দিন ⚠️)</span>'
    msg_html = f'<div class="alert alert-success alert-dismissible fade show">{h(msg)}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>' if msg else ""

    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - Settings</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>body{{background:#eef2f6;font-family:'Segoe UI',sans-serif;}}</style>
</head><body class="p-2 p-md-4"><div class="container">
    {admin_navbar("settings")} {msg_html}
    <form method="POST" action="/admin/settings/save"><div class="row g-4">
        <div class="col-md-6"><div class="card border-0 shadow-sm h-100"><div class="card-header bg-primary text-white fw-bold">MikroTik / রাউটার কনফিগারেশন</div><div class="card-body">
            <div class="mb-2"><label class="form-label fw-bold">Router Driver</label><select name="router_driver" class="form-select">{r_opts}</select></div>
            <div class="mb-2"><label class="form-label fw-bold">Host / DDNS</label><input type="text" name="mikrotik_host" class="form-control" value="{h(s.get('mikrotik_host',''))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">API Port</label><input type="text" name="mikrotik_port" class="form-control" value="{h(s.get('mikrotik_port','8728'))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">Username</label><input type="text" name="mikrotik_user" class="form-control" value="{h(s.get('mikrotik_user','admin'))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">Password (না বদলালে ফাঁকা রাখুন)</label><input type="password" name="mikrotik_pass" class="form-control" placeholder="••••••••"></div>
            <div class="mb-3"><label class="form-label fw-bold">ব্লকিং পদ্ধতি</label><select name="block_mode" class="form-select"><option value="address_list" {bm_al}>Expired-Users Address List</option><option value="disable_secret" {bm_ds}>Disable PPPoE Secret</option><option value="both" {bm_bo}>উভয়ই</option></select></div>
            <button type="button" class="btn btn-outline-primary btn-sm fw-bold" onclick="testConn('/api/test-mikrotik','mkTestRes')">MikroTik টেস্ট করুন</button><div id="mkTestRes" class="mt-2 small"></div>
        </div></div></div>
        <div class="col-md-6"><div class="card border-0 shadow-sm h-100"><div class="card-header bg-dark text-white fw-bold">OLT ও সাধারণ সেটিংস</div><div class="card-body">
            <div class="mb-2"><label class="form-label fw-bold">OLT Driver</label><select name="olt_driver" class="form-select">{o_opts}</select></div>
            <div class="mb-2"><label class="form-label fw-bold">OLT Web URL</label><input type="text" name="olt_url" class="form-control" value="{h(s.get('olt_url',''))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">OLT Username</label><input type="text" name="olt_user" class="form-control" value="{h(s.get('olt_user','admin'))}"></div>
            <div class="mb-3"><label class="form-label fw-bold">OLT Password (না বদলালে ফাঁকা রাখুন)</label><input type="password" name="olt_pass" class="form-control" placeholder="••••••••"></div>
            <button type="button" class="btn btn-outline-dark btn-sm fw-bold" onclick="testConn('/api/test-olt','oltTestRes')">OLT টেস্ট করুন</button><div id="oltTestRes" class="mt-2 small"></div><hr class="my-3">
            <div class="mb-2"><label class="form-label fw-bold">ISP নাম</label><input type="text" name="company_name" class="form-control" value="{company}"></div>
            <div class="mb-2"><label class="form-label fw-bold">বিকাশ নম্বর</label><input type="text" name="bkash_number" class="form-control" value="{h(s.get('bkash_number',''))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">নতুন অ্যাডমিন পাসওয়ার্ড</label><input type="password" name="new_admin_password" class="form-control" placeholder="••••••••"></div>
        </div></div></div>
    </div><div class="text-end mt-3"><button type="submit" class="btn btn-success btn-lg fw-bold px-5">সব Settings সেভ করুন</button></div></form>
    <div class="card border-0 shadow-sm mt-4 p-4"><div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2"><h5 class="fw-bold mb-0">ডাটাবেস ও ব্যাকআপ</h5>{db_status}</div>
    <div class="row g-3 align-items-center"><div class="col-md-5"><a href="/admin/backup/download" class="btn btn-outline-primary fw-bold w-100">ব্যাকআপ ডাউনলোড (.JSON)</a></div>
    <div class="col-md-7"><form method="POST" action="/admin/backup/restore" enctype="multipart/form-data" class="d-flex gap-2"><input type="file" name="backup_file" accept=".json" class="form-control" required><button type="submit" class="btn btn-warning fw-bold">রিস্টোর</button></form></div></div></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>async function testConn(u,t){{const e=document.getElementById(t);e.innerHTML='টেস্ট হচ্ছে...';try{{const r=await fetch(u);const d=await r.json();e.innerHTML=d.ok?`<span class="text-success fw-bold">✅ সংযোগ সফল! ${{JSON.stringify(d)}}</span>`:`<span class="text-danger fw-bold">❌ ${{d.error}}</span>`;}}catch(x){{e.innerHTML=x;}}}}</script>
</body></html>"""


@app.post("/admin/settings/save")
def save_settings_action(request: Request, router_driver: str = Form("mikrotik_api"), mikrotik_host: str = Form(""), mikrotik_port: str = Form("8728"), mikrotik_user: str = Form("admin"), mikrotik_pass: str = Form(""), block_mode: str = Form("address_list"), olt_driver: str = Form("vsol_gpon_web"), olt_url: str = Form(""), olt_user: str = Form("admin"), olt_pass: str = Form(""), company_name: str = Form(""), bkash_number: str = Form(""), new_admin_password: str = Form("")):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    for k, v in [("router_driver", router_driver), ("mikrotik_host", mikrotik_host.strip()), ("mikrotik_port", mikrotik_port.strip()), ("mikrotik_user", mikrotik_user.strip()), ("block_mode", block_mode), ("olt_driver", olt_driver), ("olt_url", olt_url.strip()), ("olt_user", olt_user.strip()), ("company_name", company_name.strip()), ("bkash_number", bkash_number.strip())]:
        database.set_setting(k, v)
    if mikrotik_pass.strip(): database.set_setting("mikrotik_pass", mikrotik_pass.strip())
    if olt_pass.strip(): database.set_setting("olt_pass", olt_pass.strip())
    if new_admin_password.strip():
        database.set_setting("admin_password", new_admin_password.strip())
        token = _get_serializer().dumps({"auth": True, "ph": _pass_hash(new_admin_password.strip())})
        resp = RedirectResponse(url="/admin/settings?msg=Settings+ও+নতুন+পাসওয়ার্ড+সেভ+হয়েছে!", status_code=303)
        resp.set_cookie(key=COOKIE_NAME, value=token, max_age=86400 * 30, httponly=True, samesite="lax")
        return resp
    return RedirectResponse(url="/admin/settings?msg=সব+Settings+সফলভাবে+সেভ+হয়েছে!", status_code=303)


@app.get("/api/test-mikrotik")
def api_test_mikrotik(request: Request):
    if not is_authenticated(request): return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    try:
        api = get_mikrotik_conn(); res = api.test_connection(); api.close(); return res
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/test-olt")
def api_test_olt(request: Request):
    if not is_authenticated(request): return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    try:
        info = get_olt_conn().get_system_info()
        return {"ok": True, "name": info.get("System Name", "OLT"), "model": info.get("Device Model", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/admin/backup/download")
def download_backup(request: Request):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    return Response(content=database.export_backup_json(), media_type="application/json", headers={"Content-Disposition": f"attachment; filename=isp_backup_{datetime.date.today().isoformat()}.json"})


@app.post("/admin/backup/restore")
async def restore_backup(request: Request, backup_file: UploadFile = File(...)):
    if not is_authenticated(request): return RedirectResponse(url="/login")
    try:
        res = database.import_backup_json((await backup_file.read()).decode("utf-8"))
        return RedirectResponse(url=f"/admin/settings?msg=রিস্টোর+সফল!+{res['customers']}+গ্রাহক+ও+{res['payments']}+পেমেন্ট।", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/admin/settings?msg=রিস্টোর+ব্যর্থ:+{e}", status_code=303)
