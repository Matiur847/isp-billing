import time
from typing import Optional
from fastapi import APIRouter, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import database
import core
from olt import normalize_mac

router = APIRouter()


@router.get("/admin/olt", response_class=HTMLResponse)
def olt_dashboard(request: Request, background_tasks: BackgroundTasks, pon_id: str = "2", refresh: int = 0, msg: Optional[str] = None):
    if not core.is_authenticated(request):
        return RedirectResponse(url="/login")

    company = core.h(core.get_company_name())
    customers = database.get_all_customers()
    cust_by_uname = {c["username"].strip().lower(): c for c in customers}

    actives, secrets_list = {}, []
    try:
        api = core.get_mikrotik_conn()
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
    cached = core.OLT_CACHE.get(pon_id)
    if not refresh and cached and (time.time() - cached["ts"] < 30):
        olt_data = cached["data"]
    else:
        try:
            olt_data = core.get_olt_conn().get_onus(pon_id=None if pon_id == "all" else pon_id)
            core.OLT_CACHE[pon_id] = {"ts": time.time(), "data": olt_data}
        except Exception as e:
            olt_error = str(e)

    pon_all_cls = "btn-primary" if pon_id == "all" else "btn-outline-primary"
    pon_buttons = f'<a href="/admin/olt?pon_id=all" class="btn btn-sm {pon_all_cls} fw-bold">সব PON</a>'
    for p in olt_data["pon_ports"]:
        p_cls = "btn-primary" if str(p["id"]) == str(pon_id) else "btn-outline-primary"
        pon_buttons += f' <a href="/admin/olt?pon_id={p["id"]}" class="btn btn-sm {p_cls} fw-bold">{core.h(p["name"])}</a>'
    pon_buttons += f' <a href="/admin/olt?pon_id={core.h(pon_id)}&refresh=1" class="btn btn-sm btn-outline-success fw-bold ms-2"><i class="fa-solid fa-rotate"></i> রিফ্রেশ</a>'

    cust_options = "".join(
        f'<option value="{c["id"]}">{core.h(c["name"] or c["username"])} ({core.h(c["username"])}) {core.h(c.get("phone") or "")}</option>'
        for c in customers
    )
    line_opts = "".join(f'<option value="{core.h(lp)}">{core.h(lp)}</option>' for lp in (olt_data.get("line_profiles") or ["line_vlan_712", "line_vlan_200", "2101", "N/A"]))
    srv_opts = "".join(f'<option value="{core.h(sp)}">{core.h(sp)}</option>' for sp in (olt_data.get("service_profiles") or ["srv_vlan712", "srv_vlan200", "2101", "N/A"]))
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
                cname = core.h(mc["name"] or uname)
                st_pill = '<span class="badge bg-success-subtle text-success">Active</span>' if mc["status"] == "active" else '<span class="badge bg-danger-subtle text-danger">Expired</span>'
                auto_pill = '<span class="badge bg-info-subtle text-primary border border-info-subtle">Auto MAC</span>' if is_auto else ""
                phone_str = f'<div class="small text-muted"><i class="fa-solid fa-phone text-primary"></i> {core.h(mc["phone"])}</div>' if mc.get("phone") else ""
                mac_str = f'<div class="small text-muted"><i class="fa-solid fa-fingerprint"></i> MAC: <b>{core.h(cmac)}</b></div>' if cmac else ""
                linked_blocks.append(f'<div class="mb-1"><div class="fw-bold text-primary">{cname} <small class="text-dark">({core.h(uname)})</small> {st_pill} {auto_pill}</div>{phone_str}{mac_str}</div>')
                cust_search_extra += f" {uname} {mc.get('name','')} {mc.get('phone','')} {cmac}"
            linked_html = "".join(linked_blocks)
        else:
            mac_hint = f'<div class="small text-muted mt-1"><i class="fa-solid fa-fingerprint"></i> {", ".join(core.h(m) for m in onu_macs)}</div>' if onu_macs else ""
            linked_html = f'<span class="text-muted small">গ্রাহক পাওয়া যায়নি</span>{mac_hint}'

        cur_vlan = o.get("vlan", "N/A")
        vlan_badge = f'<span class="badge bg-primary fs-6">VLAN {core.h(cur_vlan)}</span>' if cur_vlan != "N/A" else '<span class="badge bg-secondary">N/A</span>'
        prof_sub = f'<div class="small text-muted">{core.h(o.get("line_profile","N/A"))} / {core.h(o.get("service_profile","N/A"))}</div>'

        quick_vlan_btns += f"""
            <form method="POST" action="/admin/olt/change-vlan" class="d-inline" onsubmit="return ajaxVlanForm(event,this,'{core.h(pr['vlan'])}')">
                <input type="hidden" name="pon_id" value="{o['pon_id']}"><input type="hidden" name="onu_id" value="{o['onu_id']}">
                <input type="hidden" name="linename" value="{core.h(pr['linename'])}"><input type="hidden" name="srvname" value="{core.h(pr['srvname'])}">
                <input type="hidden" name="back_pon" value="{core.h(pon_id)}">
                <button type="submit" class="btn btn-sm {btn_cls} fw-bold vlan-qbtn" data-vlan="{core.h(pr['vlan'])}">VLAN {core.h(pr['vlan'])}</button>
            </form>"""

        search_blob = f"{o['label']} {o['sn']} {o['description']} {o['model']} {cur_vlan} {cust_search_extra}".lower()
        rows_html += f"""
        <tr class="onu-row" id="onu-row-{o['pon_id']}-{o['onu_id']}" data-search="{core.h(search_blob)}">
            <td><div class="fw-bold">{core.h(o['label'])}</div><small class="text-muted">{core.h(o['model'])}</small></td>
            <td>{st_badge}</td>
            <td>{linked_html}</td>
            <td><code>{core.h(o['sn'])}</code><div class="small text-muted">{core.h(o['description'])}</div></td>
            <td id="vlan-cell-{o['pon_id']}-{o['onu_id']}">{vlan_badge}{prof_sub}</td>
            <td><div class="d-flex flex-wrap gap-1">{quick_vlan_btns}
                <button type="button" class="btn btn-sm btn-outline-dark" onclick="openVlanModal({o['pon_id']},{o['onu_id']},'{core.h(o.get('line_profile','N/A'))}','{core.h(o.get('service_profile','N/A'))}')">Custom</button>
            </div></td>
            <td class="text-nowrap">
                <button class="btn btn-sm btn-info text-white fw-bold" onclick="openOpticalModal({o['pon_id']},{o['onu_id']},'{core.h(o['sn'])}')"><i class="fa-solid fa-signal"></i> Power</button>
                <button class="btn btn-sm btn-outline-primary fw-bold" onclick="openLinkModal({o['pon_id']},{o['onu_id']},'{core.h(o['sn'])}')"><i class="fa-solid fa-link"></i> Link</button>
                <a href="/admin/olt/reboot?pon_id={o['pon_id']}&onu_id={o['onu_id']}&back_pon={core.h(pon_id)}" onclick="return ajaxOltReboot(event,this)" class="btn btn-sm btn-outline-danger"><i class="fa-solid fa-rotate-right"></i></a>
            </td>
        </tr>"""

    if auto_links_to_save:
        background_tasks.add_task(core.save_auto_links_task, auto_links_to_save)

    err_html = f'<div class="alert alert-danger"><b>OLT কানেকশন ত্রুটি:</b> {core.h(olt_error)} — <a href="/admin/settings">Settings</a> চেক করুন।</div>' if olt_error else ""
    msg_html = f'<div class="alert alert-success alert-dismissible fade show">{core.h(msg)}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>' if msg else ""

    return f"""<!DOCTYPE html><html lang="bn"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{company} - OLT Monitor</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<style>body{{background:#eef2f6;font-family:'Segoe UI',sans-serif;}}</style>
</head><body class="p-2 p-md-4"><div class="container-fluid">
    {core.admin_navbar("olt")} {msg_html} {err_html}
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
<form method="POST" action="/admin/olt/link"><div class="modal-body"><input type="hidden" name="pon_id" id="linkPon"><input type="hidden" name="onu_id" id="linkOnu"><input type="hidden" name="onu_sn" id="linkSn"><input type="hidden" name="back_pon" value="{core.h(pon_id)}"><p class="small text-muted" id="linkLabel"></p><label class="form-label fw-bold">গ্রাহক সিলেক্ট করুন:</label><select name="cust_id" class="form-select" required>{cust_options}</select></div><div class="modal-footer"><button type="submit" class="btn btn-primary fw-bold">লিংক সেভ করুন</button></div></form></div></div></div>
<div class="modal fade" id="vlanModal" tabindex="-1"><div class="modal-dialog modal-dialog-centered"><div class="modal-content border-0 shadow"><div class="modal-header bg-dark text-white"><h5 class="modal-title fw-bold">কাস্টম VLAN / Profile সেট করুন</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
<form method="POST" action="/admin/olt/change-vlan"><div class="modal-body"><input type="hidden" name="pon_id" id="vPon"><input type="hidden" name="onu_id" id="vOnu"><input type="hidden" name="back_pon" value="{core.h(pon_id)}"><p class="fw-bold" id="vLabel"></p><div class="mb-3"><label class="form-label fw-bold">Line Profile</label><select name="linename" id="vLine" class="form-select">{line_opts}</select></div><div class="mb-3"><label class="form-label fw-bold">Service Profile</label><select name="srvname" id="vSrv" class="form-select">{srv_opts}</select></div></div><div class="modal-footer"><button type="submit" class="btn btn-success fw-bold">VLAN সেভ করুন</button></div></form></div></div></div>

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
function showOltToast(msg,ok=true){{
    let box=document.getElementById("toastBox");
    if(!box){{box=document.createElement("div");box.id="toastBox";box.className="position-fixed bottom-0 end-0 p-3";box.style.zIndex="2000";document.body.appendChild(box);}}
    const el=document.createElement("div");el.className=`alert ${{ok?"alert-success":"alert-danger"}} shadow fw-bold mb-2 py-2 px-3`;
    el.innerText=(ok?"✅ ":"❌ ")+msg;box.appendChild(el);setTimeout(()=>el.remove(),3000);
}}
async function ajaxVlanForm(e,form,vlabel){{
    e.preventDefault();
    const pon=form.pon_id.value,onu=form.onu_id.value;
    if(!confirm(`GPON0/${{pon}}:${{onu}} এর VLAN পরিবর্তন করে ${{vlabel}} করবেন?`))return false;
    const btn=form.querySelector("button");btn.disabled=true;btn.innerText="...";
    try{{
        const res=await fetch(form.action,{{method:"POST",body:new FormData(form),headers:{{"X-Ajax":"1"}}}});
        const d=await res.json();
        if(!d.ok){{showOltToast(d.error||"ব্যর্থ!",false);btn.disabled=false;btn.innerText="VLAN "+vlabel;return false;}}
        showOltToast(d.msg,true);
        const cell=document.getElementById(`vlan-cell-${{pon}}-${{onu}}`);
        if(cell)cell.innerHTML=`<span class="badge bg-primary fs-6">VLAN ${{vlabel}}</span><div class="small text-muted">${{d.linename}} / ${{d.srvname}}</div>`;
        const row=document.getElementById(`onu-row-${{pon}}-${{onu}}`);
        if(row)row.querySelectorAll(".vlan-qbtn").forEach(b=>{{
            const isThis=(b.dataset.vlan===vlabel);
            b.className=isThis?"btn btn-sm btn-secondary disabled fw-bold vlan-qbtn":"btn btn-sm btn-outline-success fw-bold vlan-qbtn";
            b.disabled=isThis;b.innerText="VLAN "+b.dataset.vlan;
        }});
    }}catch(err){{showOltToast("নেটওয়ার্ক ত্রুটি!",false);btn.disabled=false;btn.innerText="VLAN "+vlabel;}}
    return false;
}}
async function ajaxOltReboot(e,el){{
    e.preventDefault();
    if(!confirm("এই ONU টি রিবুট করবেন?"))return false;
    try{{const r=await fetch(el.href,{{headers:{{"X-Ajax":"1"}}}});const d=await r.json();showOltToast(d.ok?d.msg:(d.error||"ব্যর্থ!"),d.ok);}}
    catch(err){{showOltToast("নেটওয়ার্ক ত্রুটি!",false);}}
    return false;
}}
</script></body></html>"""


@router.post("/admin/olt/change-vlan")
def olt_change_vlan(request: Request, pon_id: int = Form(...), onu_id: int = Form(...), linename: str = Form(...), srvname: str = Form(...), back_pon: str = Form("2")):
    if not core.is_authenticated(request):
        return RedirectResponse(url="/login")
    try:
        core.get_olt_conn().set_onu_vlan_profile(pon_id, onu_id, linename, srvname)
        core.OLT_CACHE.clear()
        if request.headers.get("x-ajax") == "1":
            return JSONResponse({"ok": True, "linename": linename, "srvname": srvname, "msg": f"GPON0/{pon_id}:{onu_id} এর VLAN ({linename}) পরিবর্তন হয়েছে!"})
        return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&refresh=1&msg=GPON0/{pon_id}:{onu_id}+এর+VLAN+({linename})+সফলভাবে+পরিবর্তন+হয়েছে!", status_code=303)
    except Exception as e:
        if request.headers.get("x-ajax") == "1":
            return JSONResponse({"ok": False, "error": str(e)})
        return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&msg=VLAN+পরিবর্তন+ব্যর্থ:+{e}", status_code=303)

@router.get("/api/olt/optical")
def api_olt_optical(request: Request, pon_id: int, onu_id: int):
    if not core.is_authenticated(request):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        return core.get_olt_conn().get_onu_optical(pon_id, onu_id)
    except Exception as e:
        return {"error": str(e)}


@router.post("/admin/olt/link")
def olt_link_customer(request: Request, cust_id: int = Form(...), pon_id: int = Form(...), onu_id: int = Form(...), onu_sn: str = Form(""), back_pon: str = Form("2")):
    if not core.is_authenticated(request):
        return RedirectResponse(url="/login")
    database.link_customer_onu(cust_id, pon_id, onu_id, onu_sn)
    return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&msg=গ্রাহকের+সাথে+ONU+লিংক+করা+হয়েছে!", status_code=303)


@router.get("/admin/olt/reboot")
def olt_reboot_onu(request: Request, pon_id: int, onu_id: int, back_pon: str = "2"):
    if not core.is_authenticated(request):
        return RedirectResponse(url="/login")
    try:
        core.get_olt_conn().reboot_onu(pon_id, onu_id)
        core.OLT_CACHE.clear()
        if request.headers.get("x-ajax") == "1":
            return JSONResponse({"ok": True, "msg": f"GPON0/{pon_id}:{onu_id} রিবুট কমান্ড পাঠানো হয়েছে!"})
        return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&refresh=1&msg=GPON0/{pon_id}:{onu_id}+রিবুট+কমান্ড+পাঠানো+হয়েছে!", status_code=303)
    except Exception as e:
        if request.headers.get("x-ajax") == "1":
            return JSONResponse({"ok": False, "error": str(e)})
        return RedirectResponse(url=f"/admin/olt?pon_id={back_pon}&msg=Reboot+ব্যর্থ:+{e}", status_code=303)
