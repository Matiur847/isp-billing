import html
import hashlib
import secrets
import datetime
import logging
from typing import Optional
from fastapi import Request
from itsdangerous import URLSafeSerializer, BadSignature

import database
from mikrotik import get_router_driver
from olt import get_olt_driver

logger = logging.getLogger("billing_app")
COOKIE_NAME = "admin_session"

PROFILES_CACHE = {"ts": 0.0, "data": ["default", "Profile-Line2", "Profile-Line3"]}
OLT_CACHE = {}


def clear_caches():
    PROFILES_CACHE["ts"] = 0.0
    OLT_CACHE.clear()


def h(val) -> str:
    return html.escape("" if val is None else str(val), quote=True)


def get_company_name() -> str:
    return database.get_setting("company_name", "সাইফুল ডিস এন্ড ইন্টারনেট 🛜")


def get_webhook_secret() -> str:
    sec = database.get_setting("webhook_secret", "").strip()
    if not sec:
        sec = secrets.token_urlsafe(20)
        database.set_setting("webhook_secret", sec)
    return sec


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


def parse_mk_dt(dt_str: str):
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


def format_offline_ago(router_now, last_logout_str: str) -> str:
    dt = parse_mk_dt(last_logout_str)
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


def pass_hash(pwd: str) -> str:
    return hashlib.sha256(pwd.encode("utf-8")).hexdigest()[:16]


def get_serializer() -> URLSafeSerializer:
    return URLSafeSerializer(database.get_setting("session_secret", "isp-default-secret-key"), salt="admin-auth")


def is_authenticated(request: Request) -> bool:
    cookie_val = request.cookies.get(COOKIE_NAME)
    if not cookie_val:
        return False
    admin_pass = database.get_setting("admin_password", "admin123")
    try:
        data = get_serializer().loads(cookie_val)
        return data.get("auth") is True and data.get("ph") == pass_hash(admin_pass)
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
            <a href="/admin/payments" class="{cls('payments')} btn-sm"><i class="fa-solid fa-receipt me-1"></i> পেমেন্ট হিস্ট্রি</a>
            <a href="/admin/olt" class="{cls('olt')} btn-sm"><i class="fa-solid fa-diagram-project me-1"></i> OLT মনিটর</a>
            <a href="/admin/settings" class="{cls('settings')} btn-sm"><i class="fa-solid fa-gear me-1"></i> Settings</a>
            <a href="/" target="_blank" class="btn btn-outline-info btn-sm fw-bold"><i class="fa-solid fa-arrow-up-right-from-square me-1"></i> পোর্টাল</a>
            <a href="/logout" class="btn btn-outline-danger btn-sm" title="লগআউট"><i class="fa-solid fa-power-off"></i></a>
        </div>
    </div>"""
