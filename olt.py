import re
import logging
import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
logger = logging.getLogger("olt")


class BaseOLT:
    def __init__(self, base_url: str, user: str = "admin", password: str = ""):
        self.base_url = self._normalize_url(base_url)
        self.user = user
        self.password = password

    @staticmethod
    def _normalize_url(url: str) -> str:
        url = (url or "").strip()
        if not url:
            return ""
        # যদি ইউজার পুরো /action/main.html সহ পেস্ট করে, মূল বেস URL বের করে নেওয়া
        m = re.match(r'^(https?://[^/]+)', url, re.IGNORECASE)
        return m.group(1).rstrip("/") if m else url.rstrip("/")

    def test_connection(self) -> dict: raise NotImplementedError
    def get_system_info(self) -> dict: raise NotImplementedError
    def get_onus(self, pon_id=None) -> dict: raise NotImplementedError
    def get_onu_optical(self, pon_id: int, onu_id: int) -> dict: raise NotImplementedError
    def reboot_onu(self, pon_id: int, onu_id: int) -> bool: raise NotImplementedError
    def deactivate_onu(self, pon_id: int, onu_id: int) -> bool: raise NotImplementedError


class VsolGponWebOLT(BaseOLT):
    """VSOL GPON OLT (V2.3.1R / GoAhead Web Interface) Driver"""

    def __init__(self, base_url: str, user: str = "admin", password: str = ""):
        super().__init__(base_url, user, password)
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })

    def login(self):
        if not self.base_url:
            raise ValueError("OLT URL সেট করা হয়নি! Settings পেজ থেকে OLT URL দিন।")
        try:
            self.session.get(f"{self.base_url}/action/login.html", timeout=10)
        except Exception:
            pass

        resp = self.session.post(
            f"{self.base_url}/action/main.html",
            data={
                "user": self.user,
                "pass": self.password,
                "button": "Submit",
                "who": "100",
            },
            headers={"Referer": f"{self.base_url}/action/login.html"},
            timeout=12,
        )
        resp.raise_for_status()
        if 'name="lgform"' in resp.text or "login_check()" in resp.text:
            raise ValueError("OLT লগইন ব্যর্থ হয়েছে! ইউজারনেম বা পাসওয়ার্ড ভুল হতে পারে।")
        return True

    def logout(self):
        try:
            self.session.get(f"{self.base_url}/action/loginout.html", timeout=5)
        except Exception:
            pass

    def test_connection(self) -> dict:
        info = self.get_system_info()
        return {"ok": True, "info": info}

    def get_system_info(self) -> dict:
        self.login()
        try:
            resp = self.session.get(f"{self.base_url}/action/systeminfo.html", timeout=12)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            info = {}
            for tr in soup.find_all("tr"):
                tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
                for i in range(0, len(tds) - 1, 2):
                    if tds[i]:
                        info[tds[i]] = tds[i + 1]
            return info
        finally:
            self.logout()

    @staticmethod
    def _parse_onu_table(html: str, default_pon_id: int = 1):
        soup = BeautifulSoup(html, "html.parser")

        # ডাইনামিক PON পোর্ট তালিকা বের করা (৪, ৮ বা ১৬ পোর্ট যাই হোক)
        pon_ports = []
        sel = soup.find("select", {"name": "select"})
        if sel:
            for opt in sel.find_all("option"):
                val = opt.get("value", "").strip()
                if val.isdigit():
                    pon_ports.append({"id": int(val), "name": opt.get_text(strip=True)})

        onus = []
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 7:
                continue
            onu_label = tds[0].get_text(strip=True)
            m = re.search(r'(\d+):(\d+)$', onu_label)
            if not m:
                continue
            pon_id = int(m.group(1))
            onu_id = int(m.group(2))
            status_text = tds[1].get_text(strip=True)
            desc = tds[2].get_text(strip=True)
            model = tds[3].get_text(strip=True)
            profile = tds[4].get_text(strip=True)
            mode = tds[5].get_text(strip=True)
            sn_info = tds[6].get_text(strip=True)

            onus.append({
                "label": onu_label,
                "pon_id": pon_id,
                "onu_id": onu_id,
                "status": status_text,
                "online": status_text.lower() == "online",
                "description": desc,
                "model": model,
                "profile": profile,
                "mode": mode,
                "sn": sn_info,
            })

        return pon_ports, onus

    def get_onus(self, pon_id=None) -> dict:
        self.login()
        try:
            resp = self.session.get(f"{self.base_url}/action/onuauthinfo.html", timeout=15)
            resp.raise_for_status()
            pon_ports, first_onus = self._parse_onu_table(resp.text)

            if not pon_ports:
                pon_ports = [{"id": 1, "name": "PON1"}]

            all_onus = []
            if pon_id and str(pon_id).isdigit():
                target_pons = [int(pon_id)]
            else:
                target_pons = [p["id"] for p in pon_ports]

            # প্রথম পেজে কোন PON আগে থেকে লোড হয়ে আছে দেখা
            loaded_pon = first_onus[0]["pon_id"] if first_onus else None

            for pid in target_pons:
                if loaded_pon == pid and first_onus:
                    all_onus.extend(first_onus)
                else:
                    r_pon = self.session.post(
                        f"{self.base_url}/action/onuauthinfo.html",
                        data={"select": str(pid), "authmode": "0", "who": "100", "onuid": "0"},
                        timeout=15,
                    )
                    r_pon.raise_for_status()
                    _, pon_onus = self._parse_onu_table(r_pon.text, default_pon_id=pid)
                    all_onus.extend(pon_onus)

            online_count = sum(1 for o in all_onus if o["online"])
            total_count = len(all_onus)
            return {
                "pon_ports": pon_ports,
                "onus": all_onus,
                "online": online_count,
                "offline": total_count - online_count,
                "total": total_count,
            }
        finally:
            self.logout()

    def get_onu_optical(self, pon_id: int, onu_id: int) -> dict:
        self.login()
        try:
            resp = self.session.get(
                f"{self.base_url}/action/onuoptical.html?ponid={int(pon_id)}&onuid={int(onu_id)}",
                timeout=12,
            )
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            raw = {}
            for tr in soup.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    k = tds[0].get_text(strip=True)
                    v = tds[1].get_text(strip=True)
                    if k:
                        raw[k] = v

            def _num(text):
                m = re.search(r'(-?\d+(?:\.\d+)?)', text or "")
                return float(m.group(1)) if m else None

            rx_str = raw.get("Rx optical level", "N/A")
            tx_str = raw.get("Tx optical level", "N/A")
            return {
                "pon_id": int(pon_id),
                "onu_id": int(onu_id),
                "pon_interface": raw.get("ONU PON Interface", f"GPON0/{pon_id}:{onu_id}"),
                "rx_power": rx_str,
                "rx_dbm": _num(rx_str),
                "tx_power": tx_str,
                "tx_dbm": _num(tx_str),
                "distance": raw.get("Distance", "N/A"),
                "temperature": raw.get("Temperature", "N/A"),
                "voltage": raw.get("Power feed voltage", "N/A"),
                "bias_current": raw.get("Laser bias current", "N/A"),
                "raw": raw,
            }
        finally:
            self.logout()

    def reboot_onu(self, pon_id: int, onu_id: int) -> bool:
        self.login()
        try:
            resp = self.session.get(
                f"{self.base_url}/action/onuauthinfo.html?who=1&ponid={int(pon_id)}&onuid={int(onu_id)}",
                timeout=12,
            )
            resp.raise_for_status()
            return True
        finally:
            self.logout()

    def deactivate_onu(self, pon_id: int, onu_id: int) -> bool:
        self.login()
        try:
            resp = self.session.get(
                f"{self.base_url}/action/onuauthinfo.html?who=21&ponid={int(pon_id)}&onuid={int(onu_id)}&authmode=0",
                timeout=12,
            )
            resp.raise_for_status()
            return True
        finally:
            self.logout()


OLT_DRIVERS = {
    "vsol_gpon_web": VsolGponWebOLT,
}


def get_olt_driver(driver_name: str, base_url: str, user: str, password: str) -> BaseOLT:
    cls = OLT_DRIVERS.get(driver_name, VsolGponWebOLT)
    return cls(base_url=base_url, user=user, password=password)
