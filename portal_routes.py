import re
import hmac
import logging
from typing import Optional
from fastapi import APIRouter, Request, Form, BackgroundTasks, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import database
import core

logger = logging.getLogger("billing_app")
router = APIRouter()


def parse_bkash_sms(text: str):
    amount_m = re.search(r'received\s+Tk\s*([0-9,]+(?:\.[0-9]{1,2})?)', text, re.IGNORECASE)
    trx_m = re.search(r'TrxID\s+([A-Za-z0-9]+)', text, re.IGNORECASE)
    sender_m = re.search(r'from\s+(01[3-9][0-9]{8})', text, re.IGNORECASE)
    if trx_m and amount_m:
        return trx_m.group(1).upper(), float(amount_m.group(1).replace(",", "")), (sender_m.group(1) if sender_m else "Unknown")
    return None, None, None


@router.get("/api/search")
def api_search(q: str = ""):
    return database.search_customers(q, limit=8)


@router.post("/api/sms-webhook")
async def sms_webhook(request: Request):
    provided = (request.query_params.get("key") or request.headers.get("x-webhook-secret") or "").strip()
    expected = core.get_webhook_secret()
    if not provided or not hmac.compare_digest(provided, expected):
        logger.warning("Blocked unauthorized /api/sms-webhook request")
        return JSONResponse({"status": "error", "message": "Unauthorized: Invalid webhook key"}, status_code=403)

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


@router.get("/api/cron/check-expiry")
def trigger_expiry_check():
    core.check_expired_users_task()
    return {"status": "success", "message": "Expiry check executed"}


@router.get("/login", response_class=HTMLResponse)
def login_page(error: Optional[str] = None):
    company = core.h(core.get_company_name())
    err_html = f"<div class='alert alert-danger py-2 small'>{core.h(error)}</div>" if error else ""
    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - অ্যাডমিন লগইন</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>body{{background:#f0f4f9;height:100vh;display:flex;align-items:center;justify-content:center;font-family:'Segoe UI',sans-serif;}}.card{{border-radius:18px;border:none;box-shadow:0 10px 30px rgba(0,0,0,0.08);width:100%;max-width:400px;}}.header-banner{{background:linear-gradient(135deg,#0d6efd,#004ecc);color:white;padding:25px;border-radius:18px 18px 0 0;text-align:center;}}</style>
</head><body><div class="card"><div class="header-banner"><i class="fa-solid fa-shield-halved fa-3x mb-2"></i><h4 class="fw-bold mb-1">{company}</h4><p class="mb-0 opacity-75 small">সুরক্ষিত অ্যাডমিন প্যানেল</p></div>
<div class="card-body p-4">{err_html}<form method="POST" action="/login"><div class="mb-3"><label class="form-label fw-bold">অ্যাডমিন পাসওয়ার্ড:</label>
<div class="input-group"><span class="input-group-text"><i class="fa-solid fa-lock"></i></span><input type="password" name="password" class="form-control form-control-lg" placeholder="পাসওয়ার্ড লিখুন" required autofocus></div></div>
<button type="submit" class="btn btn-primary btn-lg w-100 fw-bold"><i class="fa-solid fa-right-to-bracket me-2"></i> লগইন করুন</button></form></div></div></body></html>"""


@router.post("/login")
def do_login(password: str = Form(...)):
    admin_pass = database.get_setting("admin_password", "admin123")
    if password == admin_pass:
        token = core.get_serializer().dumps({"auth": True, "ph": core.pass_hash(admin_pass)})
        response = RedirectResponse(url="/admin", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key=core.COOKIE_NAME, value=token, max_age=86400 * 30, httponly=True, samesite="lax")
        return response
    return RedirectResponse(url="/login?error=ভুল+পাসওয়ার্ড!+আবার+চেষ্টা+করুন।", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/logout")
def do_logout():
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(core.COOKIE_NAME)
    return response


@router.get("/", response_class=HTMLResponse)
def customer_portal(query: Optional[str] = None, user_id: Optional[int] = None):
    company = core.h(core.get_company_name())
    bkash_num = core.h(database.get_setting("bkash_number", "01XXXXXXXXX"))
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
            ph_txt = core.h(c["phone"] or "নাই")
            items += f'<a href="/?user_id={c["id"]}" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center p-3"><div><h6 class="mb-1 fw-bold text-primary">{core.h(c["name"] or c["username"])}</h6><small class="text-muted">আইডি: {core.h(c["username"])} | মোবাইল: {ph_txt}</small></div><span class="badge bg-primary rounded-pill">পেমেন্ট ➔</span></a>'
        body_content = f'<div class="alert alert-info">🔍 একাধিক গ্রাহক পাওয়া গেছে! আপনার প্রোফাইলটি সিলেক্ট করুন:</div><div class="list-group mb-4">{items}</div>'
    elif selected_customer:
        c = selected_customer
        sb = '<span class="badge bg-success">সচল (Active)</span>' if c["status"] == "active" else '<span class="badge bg-danger">মেয়াদ শেষ (Expired)</span>'
        body_content = f"""
        <div class="alert alert-light border mb-4">
            <h5 class="fw-bold text-primary mb-3"><i class="fa-solid fa-id-card me-2"></i>গ্রাহকের তথ্য:</h5>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>গ্রাহকের নাম:</span><span class="fw-bold">{core.h(c['name'] or c['username'])}</span></div>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>ইউজার আইডি:</span><span class="fw-bold">{core.h(c['username'])}</span></div>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>প্যাকেজ:</span><span class="fw-bold">{core.h(c['package_name'])}</span></div>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>মাসিক বিল:</span><span class="fw-bold text-danger fs-5">{c['package_price']} ৳</span></div>
            <div class="d-flex justify-content-between py-1 border-bottom"><span>বর্তমান মেয়াদ:</span><span class="fw-bold">{core.h(c['expiry_date'])}</span></div>
            <div class="d-flex justify-content-between py-1"><span>স্ট্যাটাস:</span><span>{sb}</span></div>
        </div>
        <div class="card bg-light border-0 p-3 mb-4">
            <h6 class="fw-bold text-dark mb-2"><i class="fa-solid fa-mobile-screen me-1 text-danger"></i> বিকাশ পেমেন্ট নির্দেশিকা:</h6>
            <ol class="small ps-3 mb-2 text-secondary"><li>আপনার বিকাশ অ্যাপে গিয়ে <b>Send Money</b> অপশনে যান।</li><li>নিচের পার্সোনাল বিকাশ নাম্বারে ঠিক <b>{c['package_price']} টাকা</b> পাঠান:</li></ol>
            <div class="text-center my-3"><div class="d-inline-flex align-items-center gap-2"><span class="bkash-badge fs-5">bKash: {bkash_num}</span><button type="button" onclick="copyBkash('{bkash_num}')" class="btn btn-sm btn-light border shadow-sm px-2" id="copyBtn"><i class="fa-regular fa-copy text-danger"></i></button></div><div class="mt-1"><small class="text-muted">Personal Number</small></div></div>
            <small class="text-muted text-center d-block">টাকা পাঠানোর পর ফিরতি এসএমএসের <b>TrxID</b> নিচে লিখুন।</small>
        </div>
        <form method="POST" action="/api/pay"><input type="hidden" name="username" value="{core.h(c['username'])}"><div class="mb-3"><label class="form-label fw-bold">বিকাশ TrxID দিন:</label><input type="text" name="trx_id" class="form-control form-control-lg text-uppercase fw-bold" placeholder="যেমন: DKG1JU47Y7" required></div><button type="submit" class="btn btn-success btn-lg w-100 fw-bold"><i class="fa-solid fa-paper-plane me-1"></i> পেমেন্ট নিশ্চিত ও নেট চালু করুন</button></form>"""

    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - বিল পরিশোধ পোর্টাল</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>body{{background:#f0f4f9;font-family:'Segoe UI',sans-serif;}}.card{{border-radius:18px;box-shadow:0 12px 35px rgba(0,0,0,0.07);border:none;}}.bkash-badge{{background-color:#e2136e;color:white;border-radius:12px;padding:12px 22px;font-weight:bold;display:inline-block;}}.header-banner{{background:linear-gradient(135deg,#0d6efd,#004ecc);color:white;padding:30px 20px;border-radius:18px 18px 0 0;text-align:center;}}</style>
</head><body class="py-4"><div class="container" style="max-width: 530px;"><div class="card mb-4"><div class="header-banner"><h3 class="mb-1 fw-bold">{company}</h3><p class="mb-0 opacity-75 fs-6">বিকাশ দিয়ে অটোমেটিক ইনস্ট্যান্ট রিচার্জ</p></div>
<div class="card-body p-4"><form method="GET" action="/" class="mb-3 position-relative" autocomplete="off"><label class="form-label fw-bold">গ্রাহকের নাম, ইউজার আইডি বা মোবাইল নম্বর:</label>
<div class="input-group"><input type="text" id="searchInput" name="query" class="form-control form-control-lg" placeholder="যেমন: anisur বা 017..." value="{core.h(query or '')}" required><button class="btn btn-primary px-4 fw-bold" type="submit"><i class="fa-solid fa-magnifying-glass me-1"></i> সার্চ</button></div>
<div id="searchResults" class="list-group shadow position-absolute w-100 mt-1 d-none" style="z-index:1050;max-height:280px;overflow-y:auto;"></div></form>{body_content}</div></div>
<p class="text-center text-muted small">© {company} | হেল্পলাইন: {bkash_num}</p></div>
<script>
function copyBkash(n){{navigator.clipboard.writeText(n).then(()=>{{const b=document.getElementById('copyBtn');b.innerHTML='<i class="fa-solid fa-check text-success"></i>';setTimeout(()=>b.innerHTML='<i class="fa-regular fa-copy text-danger"></i>',2000);}});}}
const si=document.getElementById('searchInput'),sr=document.getElementById('searchResults');
if(si&&sr){{si.addEventListener('input',async function(){{const q=this.value.trim();if(!q){{sr.classList.add('d-none');return;}}const res=await fetch('/api/search?q='+encodeURIComponent(q));const data=await res.json();if(!data.length){{sr.innerHTML='<div class="list-group-item text-muted small p-3">কোনো মিল পাওয়া যায়নি</div>';sr.classList.remove('d-none');return;}}sr.innerHTML=data.map(i=>`<a href="/?user_id=${{i.id}}" class="list-group-item list-group-item-action d-flex justify-content-between align-items-center p-3"><div><div class="fw-bold text-primary">${{i.name||i.username}}</div><small class="text-muted">আইডি: ${{i.username}} ${{i.phone?'| '+i.phone:''}}</small></div><span class="badge bg-primary rounded-pill">সিলেক্ট ➔</span></a>`).join('');sr.classList.remove('d-none');}});document.addEventListener('click',e=>{{if(!si.contains(e.target)&&!sr.contains(e.target))sr.classList.add('d-none');}});}}
</script></body></html>"""


@router.post("/api/pay", response_class=HTMLResponse)
def process_payment(background_tasks: BackgroundTasks, username: str = Form(...), trx_id: str = Form(...)):
    company = core.h(core.get_company_name())
    success, message = database.verify_and_apply_payment(username, trx_id)
    if success:
        background_tasks.add_task(core.unblock_in_mikrotik, username)
        ac, ic, hd = "alert-success", "🎉", "পেমেন্ট সফল হয়েছে!"
    else:
        ac, ic, hd = "alert-danger", "⚠️", "পেমেন্ট সম্পন্ন হয়নি!"
    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>{company}</title><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body class="bg-light py-5"><div class="container text-center" style="max-width:500px;"><div class="card p-4 shadow-sm border-0"><div class="display-3 mb-3">{ic}</div><h3 class="fw-bold mb-3">{hd}</h3><div class="alert {ac} text-start">{message}</div><a href="/?query={core.h(username)}" class="btn btn-primary mt-2 fw-bold">ফিরে যান</a></div></div></body></html>"""
