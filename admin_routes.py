import time
import datetime
from typing import Optional
from fastapi import APIRouter, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse

import database
import core
from mikrotik import live_pool

router = APIRouter()


@router.get("/api/live-speed/{username}")
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


@router.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, background_tasks: BackgroundTasks, edit_id: Optional[int] = None, msg: Optional[str] = None):
    if not core.is_authenticated(request):
        return RedirectResponse(url="/login")

    company = core.h(core.get_company_name())
    customers = database.get_all_customers()
    stats = database.get_stats()
    bkash_num = core.h(database.get_setting("bkash_number", "01XXXXXXXXX"))
    edit_cust = database.get_customer_by_id(edit_id) if edit_id else None

    actives, secrets_list, mk_online, mk_error = {}, [], False, ""
    profiles = core.PROFILES_CACHE["data"]
    router_now = None
    try:
        api = core.get_mikrotik_conn()
        actives = api.get_active_pppoe()
        secrets_list = api.get_all_secrets()
        if time.time() - core.PROFILES_CACHE["ts"] > 300:
            core.PROFILES_CACHE["data"] = api.get_profiles() or profiles
            core.PROFILES_CACHE["ts"] = time.time()
            profiles = core.PROFILES_CACHE["data"]
        router_now = core.parse_mk_dt(api.get_router_clock())
        api.close()
        mk_online = True
    except Exception as e:
        mk_error = str(e)

    secret_by_uname = {(s.get("name") or "").strip().lower(): s for s in secrets_list}

    if mk_online:
        expired_names = database.get_all_expired_usernames()
        to_block = [(u, actives[u]["address"]) for u in actives if u in expired_names]
        if to_block:
            background_tasks.add_task(core.enforce_expired_active_users, to_block)

    mk_badge = '<span class="badge bg-success"><i class="fa-solid fa-circle-check me-1"></i> MikroTik Online</span>' if mk_online else f'<span class="badge bg-danger" title="{core.h(mk_error)}"><i class="fa-solid fa-triangle-exclamation me-1"></i> MikroTik Offline</span>'
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
            online_badge = f'<span class="badge bg-success-subtle text-success border border-success-subtle rounded-pill px-2">● Online</span> <span class="text-secondary small fw-bold">{core.h(uptime)}</span> <button type="button" class="btn btn-link btn-sm p-0 ms-1 text-decoration-none badge bg-info-subtle text-primary border border-info-subtle" data-username="{core.h(uname)}" data-name="{core.h(c["name"] or uname)}" onclick="openLiveSpeedBtn(this)"><i class="fa-solid fa-chart-line"></i> Live</button>'
        else:
            ago = core.format_offline_ago(router_now, sec.get("last_logged_out", ""))
            ago_html = f' <span class="text-danger small fw-bold">• {core.h(ago)}</span>' if ago else ""
            online_badge = f'<span class="badge bg-secondary-subtle text-secondary border border-secondary-subtle rounded-pill px-2">○ Offline</span>{ago_html}'

        time_lbl, time_cls, st_lbl, st_cls = core.calculate_expiry_label(c["expiry_date"], c["status"])
        dname = core.h(c["name"] or uname)
        try:
            exp_fmt = datetime.datetime.strptime(c["expiry_date"][:19], "%Y-%m-%d %H:%M:%S").strftime("%b %d %Y %H:%M")
        except Exception:
            exp_fmt = core.h(c["expiry_date"])

        bind_badge = '<span class="badge bg-primary-subtle text-primary border border-primary-subtle"><i class="fa-solid fa-lock"></i> Bound</span>' if c.get("mac_address") else '<span class="badge bg-light text-muted border"><i class="fa-solid fa-lock-open"></i> Unbound</span>'
        olt_pon, olt_onuid, onu_sn = int(c.get("olt_pon") or 0), int(c.get("olt_onuid") or 0), (c.get("onu_sn") or "")
        onu_badge = f'<button type="button" class="badge bg-dark text-white border-0 mt-1" onclick="openOpticalModal({olt_pon}, {olt_onuid}, \'{core.h(onu_sn)}\')"><i class="fa-solid fa-signal text-info me-1"></i>PON{olt_pon}:{olt_onuid} ({core.h(onu_sn)})</button>' if olt_pon and olt_onuid else ""
        phone_html = core.h(c["phone"]) if c["phone"] else '<span class="text-muted">No Mobile</span>'
        search_txt = core.h((uname + " " + (c["name"] or "") + " " + (c["phone"] or "") + " " + onu_sn + " " + mac).lower())
        net_btn = f'<a href="/admin/toggle-suspend/{c["id"]}" class="btn btn-sm btn-outline-danger fw-bold" title="নেট বন্ধ করুন"><i class="fa-solid fa-power-off"></i> Off</a>' if c["status"] == "active" else f'<a href="/admin/toggle-suspend/{c["id"]}" class="btn btn-sm btn-success fw-bold" title="নেট চালু করুন"><i class="fa-solid fa-power-off"></i> On</a>'

        online_st = "online" if is_online else "offline"
        cust_rows_html += f"""
        <div class="card mb-2 p-2 border customer-card" data-search="{search_txt}" data-status="{core.h(c['status'])}" data-online="{online_st}">
            <div class="row align-items-center g-2">
                <div class="col-12 col-md-3">
                    <div class="d-flex align-items-center flex-wrap gap-1 mb-1">{online_badge}</div>
                    <div class="small text-secondary mb-1"><i class="fa-solid fa-arrow-down-up-across-line text-primary"></i> <b>{usage_gb:.2f} GB</b> &nbsp; <i class="fa-solid fa-network-wired text-muted"></i> <b>{core.h(ip)}</b></div>
                    <div class="small text-muted"><i class="fa-solid fa-fingerprint"></i> <span class="text-secondary">{core.h(mac)}</span> {bind_badge}</div>
                    {onu_badge}
                </div>
                <div class="col-12 col-md-3">
                    <div class="text-primary fw-bold small">#{c['id'] + 40500}</div>
                    <div class="text-primary small mb-1"><i class="fa-solid fa-phone"></i> {phone_html}</div>
                    <div class="fw-bold fs-6 text-dark">{dname}</div>
                    <div class="small text-muted"><i class="fa-solid fa-user text-primary"></i> {core.h(uname)} &nbsp; <i class="fa-solid fa-key text-muted"></i> {core.h(c['password'])}</div>
                </div>
                <div class="col-6 col-md-2">
                    <div class="small text-secondary"><i class="fa-solid fa-server"></i> {core.h(c['profile'])}</div>
                    <div class="small text-secondary mt-1"><i class="fa-solid fa-cubes"></i> {core.h(c['package_name'])}</div>
                </div>
                <div class="col-6 col-md-3">
                    <div class="d-flex align-items-center flex-wrap gap-1 mb-1"><span class="badge {st_cls} rounded-pill">{st_lbl}</span><span class="badge {time_cls} rounded-pill">⌛ {time_lbl}</span></div>
                    <div class="small text-dark fw-bold mb-1"><i class="fa-solid fa-bangladeshi-taka-sign text-primary"></i> {c['package_price']} TK</div>
                    <div class="small text-secondary"><i class="fa-regular fa-calendar-days text-danger"></i> {exp_fmt}</div>
                </div>
                <div class="col-auto ms-auto d-flex align-items-center gap-2">
                    {net_btn}
                    <button type="button" class="btn btn-outline-primary rounded-circle p-0 d-flex align-items-center justify-content-center" style="width:42px;height:42px;"
                        data-id="{c['id']}" data-name="{dname}" data-username="{core.h(uname)}" data-status="{core.h(c['status'])}"
                        data-mac="{core.h(act.get('caller_id', '') if is_online else '')}" data-bound="{core.h(c.get('mac_address') or '')}"
                        data-pon="{olt_pon}" data-onuid="{olt_onuid}" data-sn="{core.h(onu_sn)}" onclick="openActionDrawer(this)">
                        <i class="fa-solid fa-sliders fs-5"></i>
                    </button>
                </div>
            </div>
        </div>"""

    def profile_options(selected):
        opts = list(profiles) + ([selected] if selected and selected not in profiles else [])
        res_opts = ""
        for p in opts:
            sel = "selected" if p == selected else ""
            res_opts += f'<option value="{core.h(p)}" {sel}>{core.h(p)}</option>'
        return res_opts

    edit_form_html = ""
    if edit_cust:
        sel_act = "selected" if edit_cust["status"] == "active" else ""
        sel_exp = "selected" if edit_cust["status"] == "expired" else ""
        edit_form_html = f"""
        <div class="card shadow border-primary mb-4"><div class="card-header bg-primary text-white fw-bold d-flex justify-content-between align-items-center"><span>গ্রাহক এডিট করুন: {core.h(edit_cust['username'])}</span><a href="/admin" class="btn btn-sm btn-light">বাতিল</a></div>
        <div class="card-body"><form method="POST" action="/admin/update-customer"><input type="hidden" name="cust_id" value="{edit_cust['id']}"><div class="row g-3">
            <div class="col-md-2"><label class="form-label fw-bold">PPPoE আইডি</label><input type="text" class="form-control" value="{core.h(edit_cust['username'])}" disabled></div>
            <div class="col-md-3"><label class="form-label fw-bold">গ্রাহকের নাম</label><input type="text" name="name" class="form-control" value="{core.h(edit_cust['name'])}"></div>
            <div class="col-md-2"><label class="form-label fw-bold">মোবাইল</label><input type="text" name="phone" class="form-control" value="{core.h(edit_cust['phone'])}"></div>
            <div class="col-md-2"><label class="form-label fw-bold">PPPoE পাসওয়ার্ড</label><input type="text" name="password" class="form-control" value="{core.h(edit_cust['password'])}"></div>
            <div class="col-md-3"><label class="form-label fw-bold">MikroTik প্রোফাইল</label><select name="profile" class="form-select">{profile_options(edit_cust['profile'])}</select></div>
            <div class="col-md-2"><label class="form-label fw-bold">প্যাকেজের নাম</label><input type="text" name="package_name" class="form-control" value="{core.h(edit_cust['package_name'])}"></div>
            <div class="col-md-2"><label class="form-label fw-bold">মাসিক বিল (৳)</label><input type="number" name="package_price" class="form-control" value="{edit_cust['package_price']}"></div>
            <div class="col-md-3"><label class="form-label fw-bold">মেয়াদ</label><input type="text" name="expiry_date" class="form-control" value="{core.h(edit_cust['expiry_date'])}"></div>
            <div class="col-md-2"><label class="form-label fw-bold">স্ট্যাটাস</label><select name="status" class="form-select"><option value="active" {sel_act}>Active</option><option value="expired" {sel_exp}>Expired</option></select></div>
        </div><div class="mt-3"><button type="submit" class="btn btn-success fw-bold px-4">আপডেট করুন</button> <a href="/admin" class="btn btn-secondary">বাতিল</a></div></form></div></div>"""

    msg_alert = f'<div class="alert alert-info alert-dismissible fade show">{core.h(msg)}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>' if msg else ""

    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - Admin Dashboard</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.3/dist/chart.umd.min.js"></script>
<style>body{{background:#eef2f6;font-family:'Segoe UI',sans-serif;}}.card{{border-radius:12px;}}.stat-card{{border-radius:12px;padding:18px;color:white;}}.action-btn{{border-radius:10px;padding:10px 12px;font-weight:600;display:inline-flex;align-items:center;justify-content:center;gap:6px;font-size:14px;text-decoration:none;}}</style>
</head><body class="p-2 p-md-4"><div class="container-fluid">
    {core.admin_navbar("dashboard")} {msg_alert}
    <div class="d-flex flex-wrap justify-content-between align-items-center mb-3 gap-2">
        <div class="d-flex flex-wrap align-items-center gap-2">{mk_badge} {db_badge} <span class="badge bg-dark"><i class="fa-solid fa-mobile-screen me-1 text-danger"></i> বিকাশ: {bkash_num}</span></div>
        <div class="d-flex gap-2">
            <a href="/admin/sync-mikrotik" class="btn btn-outline-primary fw-bold btn-sm" onclick="return confirm('MikroTik থেকে সব PPPoE ইউজার Import করবেন?');"><i class="fa-solid fa-rotate me-1"></i> MikroTik থেকে ইউজার সিঙ্ক</a>
            <button class="btn btn-success fw-bold btn-sm" data-bs-toggle="modal" data-bs-target="#newCustomerModal"><i class="fa-solid fa-user-plus me-1"></i> নতুন গ্রাহক</button>
        </div>
    </div>
    <div class="row g-3 mb-4">
        <div class="col-6 col-md-3"><div class="stat-card bg-primary shadow-sm" style="cursor:pointer;" onclick="setCardFilter('all')"><small class="fw-bold opacity-75">মোট গ্রাহক</small><h2 class="fw-bold mb-0">{stats['total']}</h2></div></div>
        <div class="col-6 col-md-3"><div class="stat-card bg-success shadow-sm" style="cursor:pointer;" onclick="setCardFilter('active')"><small class="fw-bold opacity-75">সচল গ্রাহক</small><h2 class="fw-bold mb-0">{stats['active']}</h2></div></div>
        <div class="col-6 col-md-3"><div class="stat-card bg-danger shadow-sm" style="cursor:pointer;" onclick="setCardFilter('expired')"><small class="fw-bold opacity-75">মেয়াদোত্তীর্ণ</small><h2 class="fw-bold mb-0">{stats['expired']}</h2></div></div>
        <div class="col-6 col-md-3"><a href="/admin/payments" class="text-decoration-none"><div class="stat-card bg-dark shadow-sm"><small class="fw-bold opacity-75">মোট কালেকশন (দেখুন ➔)</small><h2 class="fw-bold mb-0">{stats['total_revenue']} ৳</h2></div></a></div>
    </div>
    {edit_form_html}
    <div class="card p-3 mb-3 bg-white border-0 shadow-sm"><div class="row align-items-center g-2">
        <div class="col-md-5"><input type="text" id="custSearchInput" class="form-control" placeholder="গ্রাহকের নাম, ইউজার আইডি, মোবাইল, MAC বা ONU SN দিয়ে খুঁজুন..." onkeyup="filterCustomerCards()"></div>
        <div class="col-md-7 text-md-end d-flex flex-wrap justify-content-md-end gap-1">
            <button type="button" class="btn btn-sm btn-outline-primary fw-bold filter-pill active" data-filter="all" onclick="setCardFilter('all')">সব ({len(customers)})</button>
            <button type="button" class="btn btn-sm btn-outline-success fw-bold filter-pill" data-filter="online" onclick="setCardFilter('online')">অনলাইন ({sum(1 for c in customers if c['username'] in actives)})</button>
            <button type="button" class="btn btn-sm btn-outline-secondary fw-bold filter-pill" data-filter="offline" onclick="setCardFilter('offline')">অফলাইন ({sum(1 for c in customers if c['username'] not in actives)})</button>
            <button type="button" class="btn btn-sm btn-outline-primary fw-bold filter-pill" data-filter="active" onclick="setCardFilter('active')">Active ({stats['active']})</button>
            <button type="button" class="btn btn-sm btn-outline-danger fw-bold filter-pill" data-filter="expired" onclick="setCardFilter('expired')">Expired ({stats['expired']})</button>
        </div>
    </div></div>
    <div id="customerCardsContainer">{cust_rows_html or '<div class="alert alert-light text-center">কোনো গ্রাহক নেই।</div>'}</div>
</div>

<div class="modal fade" id="actionModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content border-0 shadow"><div class="modal-header border-bottom"><h5 class="modal-title fw-bold" id="actionModalTitle">Actions</h5><button type="button" class="btn-close" data-bs-dismiss="modal"></button></div><div class="modal-body p-4"><div class="row g-2" id="actionButtonsGrid"></div></div></div></div></div>

<div class="modal fade" id="speedModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content border-0 shadow" style="border-radius:16px;">
    <div class="modal-header border-0 pb-1"><h6 class="modal-title fw-bold text-secondary" id="speedUserLabel">Live Traffic Monitor</h6><span class="badge bg-success ms-2" id="speedOnlineStatus">● Live</span></div>
    <div class="modal-body pt-2 pb-3 px-4">
        <div class="mb-2" style="font-size:15px;"><b style="color:#16a34a;">Download:</b> <span id="dlText" class="fw-bold text-dark">0 bps</span></div>
        <hr class="my-2 opacity-25">
        <div class="mb-3" style="font-size:15px;"><b style="color:#dc2626;">Upload:</b> <span id="ulText" class="fw-bold text-dark">0 bps</span></div>
        <div class="d-flex justify-content-end gap-3 small mb-2 text-secondary">
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#ef4444;"></span> Upload</span>
            <span><span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:#22c55e;"></span> Download</span>
        </div>
        <div style="height:220px;"><canvas id="speedChart"></canvas></div>
        <div class="text-end mt-3"><button type="button" class="btn px-4 fw-bold text-white" style="background:#5b6b7f;border-radius:10px;" data-bs-dismiss="modal">Close</button></div>
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
function formatBps(bps){{bps=Number(bps)||0;if(bps<=0)return "0 bps";if(bps<1000)return bps.toFixed(0)+" bps";if(bps<1000000)return (bps/1000).toFixed(2).replace(/\\.00$/,"")+" Kbps";return (bps/1000000).toFixed(2).replace(/\\.00$/,"")+" Mbps";}}
let speedTimer=null,speedChartObj=null,speedSid=0,speedAbort=null;
function initSpeedChart(){{
    const ctx=document.getElementById("speedChart").getContext("2d");
    const gUl=ctx.createLinearGradient(0,0,0,200);gUl.addColorStop(0,"rgba(239,68,68,0.28)");gUl.addColorStop(1,"rgba(239,68,68,0.0)");
    const gDl=ctx.createLinearGradient(0,0,0,200);gDl.addColorStop(0,"rgba(34,197,94,0.28)");gDl.addColorStop(1,"rgba(34,197,94,0.0)");
    if(speedChartObj)speedChartObj.destroy();
    speedChartObj=new Chart(ctx,{{type:"line",data:{{labels:[],datasets:[{{label:"Upload",data:[],borderColor:"#ef4444",backgroundColor:gUl,borderWidth:2.5,tension:0.35,fill:true,pointRadius:0}},{{label:"Download",data:[],borderColor:"#22c55e",backgroundColor:gDl,borderWidth:2.5,tension:0.35,fill:true,pointRadius:0}}]}},options:{{responsive:true,maintainAspectRatio:false,animation:false,plugins:{{legend:{{display:false}}}},scales:{{x:{{grid:{{display:false}},ticks:{{maxTicksLimit:6,color:"#64748b",font:{{size:11}}}}}},y:{{beginAtZero:true,border:{{display:false,dash:[4,4]}},grid:{{color:"#e2e8f0",tickBorderDash:[4,4]}},ticks:{{maxTicksLimit:5,color:"#64748b",font:{{size:11}},callback:v=>v===0?"0":formatBps(v)}}}}}}}}}});
}}
function stopSpeedMonitor(){{speedSid++;if(speedTimer){{clearTimeout(speedTimer);speedTimer=null;}}if(speedAbort){{speedAbort.abort();speedAbort=null;}}}}
function openLiveSpeed(u,n){{stopSpeedMonitor();const mySid=speedSid;document.getElementById("speedUserLabel").innerText=n+" ("+u+")";document.getElementById("dlText").innerText="লোড হচ্ছে...";document.getElementById("ulText").innerText="লোড হচ্ছে...";initSpeedChart();new bootstrap.Modal(document.getElementById("speedModal")).show();pollSpeedLoop(u,mySid);}}
async function pollSpeedLoop(u,mySid){{
    if(mySid!==speedSid)return;
    if(document.hidden){{speedTimer=setTimeout(()=>pollSpeedLoop(u,mySid),1000);return;}}
    speedAbort=new AbortController();
    try{{const r=await fetch('/api/live-speed/'+encodeURIComponent(u),{{signal:speedAbort.signal}});if(mySid!==speedSid)return;const d=await r.json();const dl=d.dl_bps!==undefined?d.dl_bps:Math.round((d.rx_mbps||0)*1000000);const ul=d.ul_bps!==undefined?d.ul_bps:Math.round((d.tx_mbps||0)*1000000);document.getElementById("dlText").innerText=formatBps(dl);document.getElementById("ulText").innerText=formatBps(ul);const sb=document.getElementById("speedOnlineStatus");sb.className=d.online?"badge bg-success ms-2":"badge bg-secondary ms-2";sb.innerText=d.online?"● Live":"○ Offline";if(speedChartObj){{const t=new Date().toLocaleTimeString('en-GB',{{hour12:false}});speedChartObj.data.labels.push(t);speedChartObj.data.datasets[0].data.push(ul);speedChartObj.data.datasets[1].data.push(dl);if(speedChartObj.data.labels.length>20){{speedChartObj.data.labels.shift();speedChartObj.data.datasets[0].data.shift();speedChartObj.data.datasets[1].data.shift();}}speedChartObj.update('none');}}}}catch(e){{if(e.name==="AbortError")return;}}
    if(mySid===speedSid)speedTimer=setTimeout(()=>pollSpeedLoop(u,mySid),700);
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


@router.get("/admin/payments", response_class=HTMLResponse)
def payments_history_page(request: Request, msg: Optional[str] = None):
    if not core.is_authenticated(request):
        return RedirectResponse(url="/login")

    company = core.h(core.get_company_name())
    stats = database.get_stats()
    payments = database.get_recent_payments(500)
    used_cnt = sum(1 for p in payments if p["status"] == "used")
    unused_cnt = len(payments) - used_cnt

    pay_rows = ""
    for p in payments:
        st = p["status"]
        ub = '<span class="badge bg-secondary">Used</span>' if st == "used" else '<span class="badge bg-success">Unused</span>'
        del_btn = f'<a href="/admin/delete-payment/{p["id"]}" onclick="return confirm(\'এই পেমেন্ট রেকর্ডটি মুছে ফেলবেন?\');" class="btn btn-sm btn-outline-danger py-0 px-2"><i class="fa-solid fa-trash"></i></a>'
        s_blob = core.h(f"{p['trx_id']} {p.get('sender_phone','')} {p.get('used_by') or ''} {p.get('raw_sms','')}".lower())
        pay_rows += f"""
        <tr class="pay-row" data-status="{core.h(st)}" data-search="{s_blob}">
            <td><b>{core.h(p['trx_id'])}</b></td>
            <td><b class="text-success">{p['amount']} ৳</b></td>
            <td>{core.h(p.get('sender_phone',''))}</td>
            <td><small>{core.h(p.get('received_at',''))}</small></td>
            <td>{ub}</td>
            <td><b>{core.h(p.get('used_by') or '-')}</b></td>
            <td><small>{core.h(p.get('used_at') or '-')}</small></td>
            <td>{del_btn}</td>
        </tr>"""

    msg_html = f'<div class="alert alert-info alert-dismissible fade show">{core.h(msg)}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>' if msg else ""

    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - পেমেন্ট হিস্ট্রি</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>body{{background:#eef2f6;font-family:'Segoe UI',sans-serif;}}.card{{border-radius:12px;}}</style>
</head><body class="p-2 p-md-4"><div class="container-fluid">
    {core.admin_navbar("payments")} {msg_html}
    <div class="row g-3 mb-4">
        <div class="col-6 col-md-3"><div class="card p-3 bg-dark text-white border-0"><small class="opacity-75">মোট কালেকশন</small><h3 class="fw-bold mb-0">{stats['total_revenue']} ৳</h3></div></div>
        <div class="col-6 col-md-3"><div class="card p-3 bg-primary text-white border-0"><small class="opacity-75">আজকের কালেকশন</small><h3 class="fw-bold mb-0">{stats['today_revenue']} ৳</h3></div></div>
        <div class="col-6 col-md-3"><div class="card p-3 bg-secondary text-white border-0"><small class="opacity-75">ব্যবহৃত (Used)</small><h3 class="fw-bold mb-0">{used_cnt} টি</h3></div></div>
        <div class="col-6 col-md-3"><div class="card p-3 bg-success text-white border-0"><small class="opacity-75">অব্যবহৃত (Unused)</small><h3 class="fw-bold mb-0">{unused_cnt} টি</h3></div></div>
    </div>
    <div class="card p-3 mb-3 border-0 shadow-sm">
        <div class="d-flex flex-wrap justify-content-between align-items-center gap-2">
            <div class="btn-group">
                <button class="btn btn-sm btn-primary fw-bold" onclick="setFilter('all', this)">সব ({len(payments)})</button>
                <button class="btn btn-sm btn-outline-secondary fw-bold" onclick="setFilter('used', this)">Used ({used_cnt})</button>
                <button class="btn btn-sm btn-outline-success fw-bold" onclick="setFilter('unused', this)">Unused ({unused_cnt})</button>
            </div>
            <input type="text" id="paySearch" class="form-control" style="max-width:340px;" placeholder="TrxID, নম্বর বা ইউজার দিয়ে খুঁজুন..." onkeyup="filterPayments()">
        </div>
    </div>
    <div class="card border-0 shadow-sm"><div class="table-responsive"><table class="table table-hover align-middle mb-0">
        <thead class="table-light"><tr><th>TrxID</th><th>টাকা</th><th>নম্বর</th><th>SMS সময়</th><th>স্ট্যাটাস</th><th>ইউজার</th><th>ব্যবহারের সময়</th><th></th></tr></thead>
        <tbody>{pay_rows or '<tr><td colspan="8" class="text-center py-4 text-muted">কোনো পেমেন্ট হিস্ট্রি নেই</td></tr>'}</tbody>
    </table></div></div>
</div>
<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
let curStatus='all';
function setFilter(st,btn){{curStatus=st;btn.parentElement.querySelectorAll('button').forEach(b=>b.classList.remove('active'));btn.classList.add('active');filterPayments();}}
function filterPayments(){{
    const q=document.getElementById('paySearch').value.toLowerCase();
    document.querySelectorAll('.pay-row').forEach(r=>{{
        const okSt=(curStatus==='all'||r.dataset.status===curStatus);
        const okQ=r.dataset.search.includes(q);
        r.style.display=(okSt&&okQ)?'':'none';
    }});
}}
</script></body></html>"""


@router.get("/admin/delete-payment/{pay_id}")
def delete_payment_action(request: Request, pay_id: int):
    if not core.is_authenticated(request):
        return RedirectResponse(url="/login")
    conn = database.get_db()
    cur = conn.cursor()
    cur.execute(database._q("DELETE FROM payments WHERE id = ?"), (int(pay_id),))
    conn.commit()
    conn.close()
    return RedirectResponse(url="/admin/payments?msg=পেমেন্ট+রেকর্ড+মুছে+ফেলা+হয়েছে!", status_code=303)


@router.post("/admin/create-customer")
def create_customer_action(request: Request, background_tasks: BackgroundTasks, username: str = Form(...), password: str = Form(...), name: str = Form(""), phone: str = Form(""), package_name: str = Form("25 Mbps"), package_price: int = Form(525), profile: str = Form("default"), days_valid: int = Form(30)):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    ok, _ = database.create_customer(username, password, name, phone, package_name, package_price, profile, days_valid)
    if ok: background_tasks.add_task(core.sync_secret_to_mikrotik, username, password, profile)
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/admin/update-customer")
def update_customer_action(request: Request, background_tasks: BackgroundTasks, cust_id: int = Form(...), name: str = Form(""), phone: str = Form(""), password: str = Form(...), package_name: str = Form("25 Mbps"), package_price: int = Form(525), profile: str = Form("default"), expiry_date: str = Form(...), status: str = Form("active")):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    database.update_customer_full(cust_id, name, phone, password, package_name, package_price, profile, expiry_date, status)
    cust = database.get_customer_by_id(cust_id)
    if cust:
        background_tasks.add_task(core.sync_secret_to_mikrotik, cust["username"], password, profile)
        background_tasks.add_task(core.unblock_in_mikrotik if status == "active" else core.block_in_mikrotik, cust["username"])
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin/quick-extend/{cust_id}")
def quick_extend_action(request: Request, background_tasks: BackgroundTasks, cust_id: int):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    username, _ = database.extend_customer(cust_id, days=30)
    if username: background_tasks.add_task(core.unblock_in_mikrotik, username)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin/toggle-suspend/{cust_id}")
def toggle_suspend_action(request: Request, background_tasks: BackgroundTasks, cust_id: int):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    cust = database.get_customer_by_id(cust_id)
    if cust:
        ns = "expired" if cust["status"] == "active" else "active"
        database.update_customer_full(cust_id, cust["name"], cust["phone"], cust["password"], cust["package_name"], cust["package_price"], cust["profile"], cust["expiry_date"], ns)
        background_tasks.add_task(core.unblock_in_mikrotik if ns == "active" else core.block_in_mikrotik, cust["username"])
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin/kick/{username}")
def kick_user_action(request: Request, username: str):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    try:
        api = core.get_mikrotik_conn(); api.kick_user(username); api.close()
    except Exception: pass
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin/bind-mac/{cust_id}")
def bind_mac_action(request: Request, cust_id: int, mac: str = "", unbind: int = 0):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    cust = database.get_customer_by_id(cust_id)
    if cust:
        tmac = "" if unbind else mac.strip()
        try:
            api = core.get_mikrotik_conn(); api.bind_mac(cust["username"], tmac); api.close()
            database.update_customer_mac(cust_id, tmac)
        except Exception as e:
            return RedirectResponse(url=f"/admin?msg=MAC+Bind+ত্রুটি:+{e}", status_code=303)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin/delete/{cust_id}")
def delete_customer_action(request: Request, background_tasks: BackgroundTasks, cust_id: int):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    username = database.delete_customer(cust_id)
    if username: background_tasks.add_task(core.delete_secret_from_mikrotik, username)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin/sync-mikrotik")
def sync_mikrotik_users(request: Request):
    if not core.is_authenticated(request): return RedirectResponse(url="/login")
    try:
        api = core.get_mikrotik_conn(); secrets_list = api.get_all_secrets(); api.close()
        added = database.upsert_synced_secrets(secrets_list)
        return RedirectResponse(url=f"/admin?msg=MikroTik+থেকে+{len(secrets_list)}+টি+পাওয়া+গেছে,+নতুন+{added}+জন+যুক্ত+হয়েছে।", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/admin?msg=MikroTik+সিঙ্ক+ব্যর্থ:+{e}", status_code=303)
