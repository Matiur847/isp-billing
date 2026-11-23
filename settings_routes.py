import datetime
from typing import Optional
from fastapi import APIRouter, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response

import database
import core
from mikrotik import ROUTER_DRIVERS
from olt import OLT_DRIVERS

router = APIRouter()


@router.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: Optional[str] = None):
    if not core.is_authenticated(request):
        return RedirectResponse(url="/login")
    s = database.get_all_settings()
    company = core.h(s.get("company_name", "সাইফুল ডিস এন্ড ইন্টারনেট 🛜"))
    wh_secret = core.get_webhook_secret()
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host_hdr = request.headers.get("host", request.url.netloc)
    wh_url = f"{proto}://{host_hdr}/api/sms-webhook?key={wh_secret}"

    r_opts = "".join(f'<option value="{k}" {"selected" if s.get("router_driver")==k else ""}>{k}</option>' for k in ROUTER_DRIVERS.keys())
    o_opts = "".join(f'<option value="{k}" {"selected" if s.get("olt_driver")==k else ""}>{k}</option>' for k in OLT_DRIVERS.keys())
    bm = s.get("block_mode", "address_list")
    bm_al = "selected" if bm == "address_list" else ""
    bm_ds = "selected" if bm == "disable_secret" else ""
    bm_bo = "selected" if bm == "both" else ""
    db_status = '<span class="badge bg-success fs-6">PostgreSQL চালু আছে (ডেটা স্থায়ী থাকবে ✅)</span>' if database.IS_POSTGRES else '<span class="badge bg-warning text-dark fs-6">SQLite চলছে (Render-এ স্থায়ী করতে DATABASE_URL দিন ⚠️)</span>'
    msg_html = f'<div class="alert alert-success alert-dismissible fade show">{core.h(msg)}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>' if msg else ""

    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - Settings</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>body{{background:#eef2f6;font-family:'Segoe UI',sans-serif;}}</style>
</head><body class="p-2 p-md-4"><div class="container">
    {core.admin_navbar("settings")} {msg_html}
    <form method="POST" action="/admin/settings/save"><div class="row g-4">
        <div class="col-md-6"><div class="card border-0 shadow-sm h-100"><div class="card-header bg-primary text-white fw-bold">MikroTik / রাউটার কনফিগারেশন</div><div class="card-body">
            <div class="mb-2"><label class="form-label fw-bold">Router Driver</label><select name="router_driver" class="form-select">{r_opts}</select></div>
            <div class="mb-2"><label class="form-label fw-bold">Host / DDNS</label><input type="text" name="mikrotik_host" class="form-control" value="{core.h(s.get('mikrotik_host',''))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">API Port</label><input type="text" name="mikrotik_port" class="form-control" value="{core.h(s.get('mikrotik_port','8728'))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">Username</label><input type="text" name="mikrotik_user" class="form-control" value="{core.h(s.get('mikrotik_user','admin'))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">Password (না বদলালে ফাঁকা রাখুন)</label><input type="password" name="mikrotik_pass" class="form-control" placeholder="••••••••"></div>
            <div class="mb-3"><label class="form-label fw-bold">ব্লকিং পদ্ধতি</label><select name="block_mode" class="form-select"><option value="address_list" {bm_al}>Expired-Users Address List</option><option value="disable_secret" {bm_ds}>Disable PPPoE Secret</option><option value="both" {bm_bo}>উভয়ই</option></select></div>
            <button type="button" class="btn btn-outline-primary btn-sm fw-bold" onclick="testConn('/api/test-mikrotik','mkTestRes')">MikroTik টেস্ট করুন</button><div id="mkTestRes" class="mt-2 small"></div>
        </div></div></div>
        <div class="col-md-6"><div class="card border-0 shadow-sm h-100"><div class="card-header bg-dark text-white fw-bold">OLT ও সাধারণ সেটিংস</div><div class="card-body">
            <div class="mb-2"><label class="form-label fw-bold">OLT Driver</label><select name="olt_driver" class="form-select">{o_opts}</select></div>
            <div class="mb-2"><label class="form-label fw-bold">OLT Web URL</label><input type="text" name="olt_url" class="form-control" value="{core.h(s.get('olt_url',''))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">OLT Username</label><input type="text" name="olt_user" class="form-control" value="{core.h(s.get('olt_user','admin'))}"></div>
            <div class="mb-3"><label class="form-label fw-bold">OLT Password (না বদলালে ফাঁকা রাখুন)</label><input type="password" name="olt_pass" class="form-control" placeholder="••••••••"></div>
            <button type="button" class="btn btn-outline-dark btn-sm fw-bold" onclick="testConn('/api/test-olt','oltTestRes')">OLT টেস্ট করুন</button><div id="oltTestRes" class="mt-2 small"></div><hr class="my-3">
            <div class="mb-2"><label class="form-label fw-bold">ISP নাম</label><input type="text" name="company_name" class="form-control" value="{company}"></div>
            <div class="mb-2"><label class="form-label fw-bold">বিকাশ নম্বর</label><input type="text" name="bkash_number" class="form-control" value="{core.h(s.get('bkash_number',''))}"></div>
            <div class="mb-2"><label class="form-label fw-bold">নতুন অ্যাডমিন পাসওয়ার্ড</label><input type="password" name="new_admin_password" class="form-control" placeholder="••••••••"></div>
            <div class="mb-2"><label class="form-label fw-bold text-danger">SMS Webhook Secret Key (গোপন রাখুন)</label><input type="text" name="webhook_secret" class="form-control" value="{core.h(wh_secret)}"></div>
        </div></div></div>
    </div><div class="text-end mt-3"><button type="submit" class="btn btn-success btn-lg fw-bold px-5">সব Settings সেভ করুন</button></div></form>

    <div class="card border-0 shadow-sm mt-4 p-4">
        <h5 class="fw-bold mb-2"><i class="fa-solid fa-shield-halved text-success me-2"></i>নিরাপদ bKash SMS Webhook</h5>
        <p class="small text-muted mb-2">আপনার ফোনের <b>SMS Forwarder</b> অ্যাপে নিচের পুরো URL-টি বসান:</p>
        <div class="input-group mb-3">
            <input type="text" id="whUrlBox" class="form-control bg-light" value="{core.h(wh_url)}" readonly>
            <button type="button" class="btn btn-primary fw-bold" onclick="navigator.clipboard.writeText(document.getElementById('whUrlBox').value);alert('Webhook URL কপি হয়েছে!');">Copy URL</button>
        </div>
        <hr>
        <h6 class="fw-bold mb-2"><i class="fa-solid fa-flask text-warning me-2"></i>ডামি পেমেন্ট টেস্ট (শুধু অ্যাডমিনের জন্য)</h6>
        <form method="POST" action="/admin/test-dummy-sms" class="row g-2 align-items-end">
            <div class="col-md-4"><label class="form-label small fw-bold">টেস্ট TrxID</label><input type="text" name="trx_id" class="form-control" value="TEST998877" required></div>
            <div class="col-md-4"><label class="form-label small fw-bold">টাকার পরিমাণ</label><input type="number" name="amount" class="form-control" value="525" required></div>
            <div class="col-md-4"><button type="submit" class="btn btn-warning fw-bold w-100">ডামি SMS জমা করুন</button></div>
        </form>
    </div>

    <div class="card border-0 shadow-sm mt-4 p-4"><div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2"><h5 class="fw-bold mb-0">ডাটাবেস ও ব্যাকআপ</h5>{db_status}</div>
    <div class="row g-3 align-items-center"><div class="col-md-5"><a href="/admin/backup/download" class="btn btn-outline-primary fw-bold w-100">ব্যাকআপ ডাউনলোড (.JSON)</a></div>
    <div class="col-md-7"><form method="POST" action="/admin/backup/restore" enctype="multipart/form-data" class="d-flex gap-2"><input type="file" name="backup_file" accept=".json" class="form-control" required><button type="submit" class="btn btn-warning fw-bold">রিস্টোর</button></form></div></div></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>async function testConn(u,t){{const e=document.getElementById(t);e.innerHTML='টেস্ট হচ্ছে...';try{{const r=await fetch(u);const d=await r.json();e.innerHTML=d.ok?`<span class="text-success fw-bold">✅ সংযোগ সফল! ${{JSON.stringify(d)}}</span>`:`<span class="text-danger fw-bold">❌ ${{d.error}}</span>`;}}catch(x){{e.innerHTML=x;}}}}</script>
</body></html>"""


@router.post("/admin/test-dummy-sms")
def admin_test_dummy_sms(request: Request, trx_id: str = Form(...), amount: float = Form(...)):
    if not core.is_authenticated(request):
        return RedirectResponse(url="/login")
    tid = trx_id.strip().upper()
    ok = database.record_payment(tid, amount, "01700000000", f"ADMIN DUMMY TEST Tk {amount} TrxID {tid}")
    msg = f"ডামি পেমেন্ট ({tid} - {amount} ৳) যুক্ত হয়েছে! এখন পোর্টালে টেস্ট করুন।" if ok else f"TrxID {tid} ইতিমধ্যে আছে, নতুন একটি নাম দিন।"
    return RedirectResponse(url=f"/admin/settings?msg={msg}", status_code=303)


@router.post("/admin/settings/save")
def save_settings_action(request: Request, router_driver: str = Form("mikrotik_api"), mikrotik_host: str = Form(""), mikrotik_port: str = Form("8728"), mikrotik_user: str = Form("admin"), mikrotik_pass: str = Form(""), block_mode: str = Form("address_list"), olt_driver: str = Form("vsol_gpon_web"), olt_url: str = Form(""), olt_user: str = Form("admin"), olt_pass: str = Form(""), company_name: str = Form(""), bkash_number: str = Form(""), new_admin_password: str = Form(""), webhook_secret: str = Form("")):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    for k, v in [("router_driver", router_driver), ("mikrotik_host", mikrotik_host.strip()), ("mikrotik_port", mikrotik_port.strip()), ("mikrotik_user", mikrotik_user.strip()), ("block_mode", block_mode), ("olt_driver", olt_driver), ("olt_url", olt_url.strip()), ("olt_user", olt_user.strip()), ("company_name", company_name.strip()), ("bkash_number", bkash_number.strip())]:
        database.set_setting(k, v)
    if mikrotik_pass.strip(): database.set_setting("mikrotik_pass", mikrotik_pass.strip())
    if olt_pass.strip(): database.set_setting("olt_pass", olt_pass.strip())
    if webhook_secret.strip(): database.set_setting("webhook_secret", webhook_secret.strip())
    core.clear_caches()
    if new_admin_password.strip():
        database.set_setting("admin_password", new_admin_password.strip())
        token = core.get_serializer().dumps({"auth": True, "ph": core.pass_hash(new_admin_password.strip())})
        resp = RedirectResponse(url="/admin/settings?msg=Settings+ও+নতুন+পাসওয়ার্ড+সেভ+হয়েছে!", status_code=303)
        resp.set_cookie(key=core.COOKIE_NAME, value=token, max_age=86400 * 30, httponly=True, samesite="lax")
        return resp
    return RedirectResponse(url="/admin/settings?msg=সব+Settings+সফলভাবে+সেভ+হয়েছে!", status_code=303)


@router.get("/api/test-mikrotik")
def api_test_mikrotik(request: Request):
    if not core.is_authenticated(request): return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    try:
        api = core.get_mikrotik_conn(); res = api.test_connection(); api.close(); return res
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/api/test-olt")
def api_test_olt(request: Request):
    if not core.is_authenticated(request): return JSONResponse({"ok": False, "error": "Unauthorized"}, status_code=401)
    try:
        info = core.get_olt_conn().get_system_info()
        return {"ok": True, "name": info.get("System Name", "OLT"), "model": info.get("Device Model", "")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.get("/admin/backup/download")
def download_backup(request: Request):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    return Response(content=database.export_backup_json(), media_type="application/json", headers={"Content-Disposition": f"attachment; filename=isp_backup_{datetime.date.today().isoformat()}.json"})


@router.post("/admin/backup/restore")
async def restore_backup(request: Request, backup_file: UploadFile = File(...)):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    try:
        res = database.import_backup_json((await backup_file.read()).decode("utf-8"))
        return RedirectResponse(url=f"/admin/settings?msg=রিস্টোর+সফল!+{res['customers']}+গ্রাহক+ও+{res['payments']}+পেমেন্ট।", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/admin/settings?msg=রিস্টোর+ব্যর্থ:+{e}", status_code=303)
