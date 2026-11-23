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
        m = re.match(r'^(https?://[^/]+)', url, re.IGNORECASE)
        return m.group(1).rstrip("/") if m else url.rstrip("/")

    def test_connection(self) -> dict: raise NotImplementedError
    def get_system_info(self) -> dict: raise NotImplementedError
    def get_onus(self, pon_id=None) -> dict: raise NotImplementedError
    def get_onu_optical(self, pon_id: int, onu_id: int) -> dict: raise NotImplementedError
    def reboot_onu(self, pon_id: int, onu_id: int) -> bool: raise NotImplementedError
    def deactivate_onu(self, pon_id: int, onu_id: int) -> bool: raise NotImplementedError
    def set_onu_vlan_profile(self, pon_id: int, onu_id: int, linename: str, srvname: str) -> bool: raise NotImplementedError


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
            data={"user": self.user, "pass": self.password, "button": "Submit", "who": "100"},
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
        return {"ok": True, "info": self.get_system_info()}

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
    def _extract_vlan_num(profile_name: str) -> str:
        if not profile_name or profile_name.upper() == "N/A":
            return "N/A"
        m = re.search(r'(\d+)$', profile_name.strip())
        return m.group(1) if m else profile_name.strip()

    @staticmethod
    def _parse_onu_table(html: str):
        soup = BeautifulSoup(html, "html.parser")
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
            pon_id, onu_id = int(m.group(1)), int(m.group(2))
            status_text = tds[1].get_text(strip=True)
            onus.append({
                "label": onu_label,
                "pon_id": pon_id,
                "onu_id": onu_id,
                "status": status_text,
                "online": status_text.lower() == "online",
                "description": tds[2].get_text(strip=True),
                "model": tds[3].get_text(strip=True),
                "profile": tds[4].get_text(strip=True),
                "mode": tds[5].get_text(strip=True),
                "sn": tds[6].get_text(strip=True),
                "line_profile": "N/A",
                "service_profile": "N/A",
                "vlan": "N/A",
            })
        return pon_ports, onus

    @staticmethod
    def _parse_bind_table(html: str, default_pon_id: int):
        soup = BeautifulSoup(html, "html.parser")
        bindings = {}
        for tr in soup.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 4:
                continue
            first_col = tds[0].get_text(strip=True)
            if not first_col.isdigit():
                continue
            onu_id = int(first_col)
            line_prof = tds[2].get_text(strip=True)
            srv_prof = tds[3].get_text(strip=True)
            bindings[(int(default_pon_id), onu_id)] = {
                "line_profile": line_prof,
                "service_profile": srv_prof,
            }
        return bindings

    def _get_available_profiles(self, sample_pon: int = 1, sample_onu: int = 1):
        line_profiles, srv_profiles = [], []
        try:
            r = self.session.get(
                f"{self.base_url}/action/onuprofilebindcfg.html?ponid={sample_pon}&onuid={sample_onu}",
                timeout=10,
            )
            soup = BeautifulSoup(r.text, "html.parser")
            s1 = soup.find("select", {"name": "linename"})
            if s1:
                line_profiles = [o.get("value", o.get_text(strip=True)).strip() for o in s1.find_all("option")]
            s2 = soup.find("select", {"name": "srvname"})
            if s2:
                srv_profiles = [o.get("value", o.get_text(strip=True)).strip() for o in s2.find_all("option")]
        except Exception as e:
            logger.warning(f"Could not fetch profile list: {e}")

        presets = []
        srv_by_num = {self._extract_vlan_num(s): s for s in srv_profiles if s != "N/A"}
        for lp in line_profiles:
            if lp == "N/A":
                continue
            num = self._extract_vlan_num(lp)
            sp = srv_by_num.get(num, srv_profiles[0] if srv_profiles else "N/A")
            presets.append({"vlan": num, "linename": lp, "srvname": sp})
        return line_profiles, srv_profiles, presets

    def get_onus(self, pon_id=None) -> dict:
        self.login()
        try:
            resp = self.session.get(f"{self.base_url}/action/onuauthinfo.html", timeout=15)
            resp.raise_for_status()
            pon_ports, first_onus = self._parse_onu_table(resp.text)
            if not pon_ports:
                pon_ports = [{"id": 1, "name": "PON1"}]

            target_pons = [int(pon_id)] if (pon_id and str(pon_id).isdigit()) else [p["id"] for p in pon_ports]
            loaded_pon = first_onus[0]["pon_id"] if first_onus else None

            all_onus = []
            all_bindings = {}

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
                    _, pon_onus = self._parse_onu_table(r_pon.text)
                    all_onus.extend(pon_onus)

                # একই সেশনে onuprofilebind.html থেকে বর্তমান Line/Service Profile ও VLAN আনা
                try:
                    r_bind = self.session.post(
                        f"{self.base_url}/action/onuprofilebind.html",
                        data={"select": str(pid), "who": "100", "onuid": "0"},
                        timeout=15,
                    )
                    all_bindings.update(self._parse_bind_table(r_bind.text, pid))
                except Exception as e:
                    logger.warning(f"Profile bind fetch failed for PON {pid}: {e}")

            for o in all_onus:
                b = all_bindings.get((o["pon_id"], o["onu_id"]))
                if b:
                    o["line_profile"] = b["line_profile"]
                    o["service_profile"] = b["service_profile"]
                    o["vlan"] = self._extract_vlan_num(b["line_profile"])

            sample_pon = target_pons[0] if target_pons else 1
            sample_onu = all_onus[0]["onu_id"] if all_onus else 1
            line_profiles, srv_profiles, vlan_presets = self._get_available_profiles(sample_pon, sample_onu)

            online_count = sum(1 for o in all_onus if o["online"])
            total_count = len(all_onus)
            return {
                "pon_ports": pon_ports,
                "onus": all_onus,
                "online": online_count,
                "offline": total_count - online_count,
                "total": total_count,
                "line_profiles": line_profiles,
                "service_profiles": srv_profiles,
                "vlan_presets": vlan_presets,
            }
        finally:
            self.logout()

    def set_onu_vlan_profile(self, pon_id: int, onu_id: int, linename: str, srvname: str,
                             alaname: str = "N/A", priname: str = "N/A", fmtname: str = "N/A") -> bool:
        self.login()
        try:
            cfg_url = f"{self.base_url}/action/onuprofilebindcfg.html?ponid={int(pon_id)}&onuid={int(onu_id)}"
            try:
                self.session.get(cfg_url, timeout=10)
            except Exception:
                pass

            resp = self.session.post(
                f"{self.base_url}/action/onuprofilebindcfg.html",
                data={
                    "linename": linename,
                    "srvname": srvname,
                    "alaname": alaname,
                    "priname": priname,
                    "fmtname": fmtname,
                    "Commit": "Commit",
                    "who": "0",
                    "pon_id": str(int(pon_id)),
                    "onu_id": str(int(onu_id)),
                },
                headers={"Referer": cfg_url},
                timeout=15,
            )
            resp.raise_for_status()
            return True
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
                    k, v = tds[0].get_text(strip=True), tds[1].get_text(strip=True)
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
