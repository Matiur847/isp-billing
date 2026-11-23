import socket
import ssl
import hashlib
import binascii
import logging
import threading

logger = logging.getLogger("mikrotik")


class BaseRouterDriver:
    def __init__(self, host, port, user="admin", password=""):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password

    def connect(self): raise NotImplementedError
    def close(self): raise NotImplementedError
    def test_connection(self): raise NotImplementedError
    def get_active_pppoe(self): raise NotImplementedError
    def get_all_secrets(self): raise NotImplementedError
    def get_profiles(self): raise NotImplementedError
    def get_live_speed(self, username): raise NotImplementedError


class MikrotikAPI(BaseRouterDriver):
    def __init__(self, host, port=8728, user="admin", password="", use_ssl=False):
        super().__init__(host, port, user, password)
        self.use_ssl = use_ssl
        self.sock = None

    def connect(self):
        if not self.host:
            raise ValueError("MikroTik host is not configured")
        raw_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        raw_sock.settimeout(8.0)
        if self.use_ssl:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=self.host)
        else:
            self.sock = raw_sock
        self.sock.connect((self.host, self.port))
        self._login()

    def _send_len(self, length):
        if length < 0x80:
            self.sock.sendall(bytes([length]))
        elif length < 0x4000:
            length |= 0x8000
            self.sock.sendall(bytes([(length >> 8) & 0xFF, length & 0xFF]))
        elif length < 0x200000:
            length |= 0xC00000
            self.sock.sendall(bytes([(length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF]))
        elif length < 0x10000000:
            length |= 0xE0000000
            self.sock.sendall(bytes([(length >> 24) & 0xFF, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF]))
        else:
            self.sock.sendall(bytes([0xF0, (length >> 24) & 0xFF, (length >> 16) & 0xFF, (length >> 8) & 0xFF, length & 0xFF]))

    def _read_len(self):
        b = self.sock.recv(1)
        if not b:
            raise ConnectionError("Socket closed while reading length")
        c = b[0]
        if (c & 0x80) == 0x00:
            return c
        elif (c & 0xC0) == 0x80:
            return ((c & 0x3F) << 8) | self.sock.recv(1)[0]
        elif (c & 0xE0) == 0xC0:
            c2, c3 = self.sock.recv(2)
            return ((c & 0x1F) << 16) | (c2 << 8) | c3
        elif (c & 0xF0) == 0xE0:
            c2, c3, c4 = self.sock.recv(3)
            return ((c & 0x0F) << 24) | (c2 << 16) | (c3 << 8) | c4
        elif (c & 0xF8) == 0xF0:
            c2, c3, c4, c5 = self.sock.recv(4)
            return (c2 << 24) | (c3 << 16) | (c4 << 8) | c5
        return 0

    def _send_sentence(self, words):
        for w in words:
            b_word = w.encode("utf-8")
            self._send_len(len(b_word))
            self.sock.sendall(b_word)
        self.sock.sendall(b"\x00")

    def _read_sentence(self):
        words = []
        while True:
            length = self._read_len()
            if length == 0:
                break
            data = b""
            while len(data) < length:
                chunk = self.sock.recv(length - len(data))
                if not chunk:
                    raise ConnectionError("Socket closed while reading word")
                data += chunk
            words.append(data.decode("utf-8", errors="replace"))
        return words

    def talk(self, words):
        self._send_sentence(words)
        replies = []
        while True:
            sentence = self._read_sentence()
            if not sentence:
                continue
            status = sentence[0]
            attrs = {}
            for item in sentence[1:]:
                if item.startswith("="):
                    parts = item[1:].split("=", 1)
                    attrs[parts[0]] = parts[1] if len(parts) == 2 else ""
            replies.append((status, attrs))
            if status in ("!done", "!trap", "!fatal"):
                break
        return replies

    def _login(self):
        res = self.talk(["/login", f"=name={self.user}", f"=password={self.password}"])
        for status, attrs in res:
            if status == "!done" and "ret" not in attrs:
                return True
            if "ret" in attrs:
                challenge = binascii.unhexlify(attrs["ret"])
                md = hashlib.md5()
                md.update(b"\x00")
                md.update(self.password.encode("utf-8"))
                md.update(challenge)
                response = "00" + md.hexdigest()
                res2 = self.talk(["/login", f"=name={self.user}", f"=response={response}"])
                for s2, a2 in res2:
                    if s2 == "!done":
                        return True
                    if s2 == "!trap":
                        raise Exception(f"Mikrotik login failed: {a2.get('message', 'Unknown error')}")
            if status == "!trap":
                raise Exception(f"Mikrotik login failed: {attrs.get('message', 'Unknown error')}")
        return True

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None

    def test_connection(self):
        res = self.talk(["/system/identity/print"])
        identity = "MikroTik"
        for status, attrs in res:
            if status == "!re":
                identity = attrs.get("name", "MikroTik")
        res_r = self.talk(["/system/resource/print"])
        board, version = "", ""
        for status, attrs in res_r:
            if status == "!re":
                board = attrs.get("board-name", "")
                version = attrs.get("version", "")
        return {"ok": True, "identity": identity, "board": board, "version": version}

    def get_router_clock(self):
        try:
            res = self.talk(["/system/clock/print"])
            for status, attrs in res:
                if status == "!re":
                    return f"{attrs.get('date', '')} {attrs.get('time', '')}".strip()
        except Exception:
            pass
        return ""

    def get_active_pppoe(self):
        res = self.talk(["/ppp/active/print"])
        iface_bytes = {}
        try:
            ires = self.talk(["/interface/print", "?type=pppoe-in"])
            for st, at in ires:
                if st == "!re":
                    iname = at.get("name", "")
                    rx_b = int(at.get("rx-byte", 0) or 0)
                    tx_b = int(at.get("tx-byte", 0) or 0)
                    iface_bytes[iname] = round((rx_b + tx_b) / (1024 ** 3), 2)
        except Exception:
            pass

        users = {}
        for status, attrs in res:
            if status == "!re":
                name = attrs.get("name")
                if name:
                    usage_gb = iface_bytes.get(f"<pppoe-{name}>", iface_bytes.get(name, 0.0))
                    users[name] = {
                        "address": attrs.get("address", "0.0.0.0"),
                        "uptime": attrs.get("uptime", "0s"),
                        "id": attrs.get(".id", ""),
                        "caller_id": attrs.get("caller-id", "No MAC"),
                        "session_id": attrs.get("session-id", ""),
                        "usage_gb": usage_gb,
                    }
        return users

    def get_all_secrets(self):
        res = self.talk(["/ppp/secret/print"])
        secrets_list = []
        for status, attrs in res:
            if status == "!re" and attrs.get("name"):
                secrets_list.append({
                    "id": attrs.get(".id", ""),
                    "name": attrs.get("name"),
                    "password": attrs.get("password", "123456"),
                    "profile": attrs.get("profile", "default"),
                    "service": attrs.get("service", "pppoe"),
                    "caller_id": attrs.get("caller-id", ""),
                    "last_caller_id": attrs.get("last-caller-id", ""),
                    "last_logged_out": attrs.get("last-logged-out", ""),
                    "disabled": attrs.get("disabled", "false") == "true",
                    "comment": attrs.get("comment", ""),
                })
        return secrets_list

    def get_profiles(self):
        res = self.talk(["/ppp/profile/print"])
        profiles = []
        for status, attrs in res:
            if status == "!re" and attrs.get("name"):
                profiles.append(attrs["name"])
        return profiles or ["default"]

    def get_live_speed(self, username):
        # RouterOS <pppoe-user>: tx = router->customer (Download), rx = customer->router (Upload)
        iface = f"<pppoe-{username}>"
        res = self.talk(["/interface/monitor-traffic", f"=interface={iface}", "=once="])
        for status, attrs in res:
            if status == "!re":
                tx_bps = int(attrs.get("tx-bits-per-second", 0) or 0)
                rx_bps = int(attrs.get("rx-bits-per-second", 0) or 0)
                dl = round(tx_bps / 1_000_000, 2)
                ul = round(rx_bps / 1_000_000, 2)
                return {
                    "rx_mbps": dl,
                    "tx_mbps": ul,
                    "download_mbps": dl,
                    "upload_mbps": ul,
                    "dl_bps": tx_bps,
                    "ul_bps": rx_bps,
                    "online": True,
                }
        return {
            "rx_mbps": 0.0, "tx_mbps": 0.0,
            "download_mbps": 0.0, "upload_mbps": 0.0,
            "dl_bps": 0, "ul_bps": 0, "online": False,
        }

    def kick_user(self, username):
        actives = self.get_active_pppoe()
        if username in actives:
            self.talk(["/ppp/active/remove", f"=.id={actives[username]['id']}"])
            logger.info(f"Kicked active session for {username}")
            return True
        return False

    def create_pppoe_secret(self, username, password, profile="default", comment=""):
        res = self.talk(["/ppp/secret/print", f"?name={username}"])
        if any(status == "!re" for status, _ in res):
            return self.update_pppoe_secret(username, password, profile, comment)
        words = ["/ppp/secret/add", f"=name={username}", f"=password={password}", "=service=pppoe", f"=profile={profile}"]
        if comment:
            words.append(f"=comment={comment}")
        self.talk(words)
        return True

    def update_pppoe_secret(self, username, password=None, profile=None, comment=None):
        res = self.talk(["/ppp/secret/print", f"?name={username}"])
        secret_id = next((attrs.get(".id") for st, attrs in res if st == "!re"), None)
        if not secret_id:
            return False
        words = ["/ppp/secret/set", f"=.id={secret_id}"]
        if password:
            words.append(f"=password={password}")
        if profile:
            words.append(f"=profile={profile}")
        if comment is not None:
            words.append(f"=comment={comment}")
        self.talk(words)
        return True

    def set_secret_disabled(self, username, disabled=True):
        res = self.talk(["/ppp/secret/print", f"?name={username}"])
        secret_id = next((attrs.get(".id") for st, attrs in res if st == "!re"), None)
        if not secret_id:
            return False
        val = "yes" if disabled else "no"
        self.talk(["/ppp/secret/set", f"=.id={secret_id}", f"=disabled={val}"])
        if disabled:
            self.kick_user(username)
        return True

    def bind_mac(self, username, mac_address):
        res = self.talk(["/ppp/secret/print", f"?name={username}"])
        secret_id = next((attrs.get(".id") for st, attrs in res if st == "!re"), None)
        if not secret_id:
            return False
        self.talk(["/ppp/secret/set", f"=.id={secret_id}", f"=caller-id={mac_address}"])
        return True

    def delete_pppoe_secret(self, username):
        res = self.talk(["/ppp/secret/print", f"?name={username}"])
        secret_id = next((attrs.get(".id") for st, attrs in res if st == "!re"), None)
        if secret_id:
            self.talk(["/ppp/secret/remove", f"=.id={secret_id}"])
            self.kick_user(username)
            return True
        return False

    def block_user(self, ip_address, comment="Expired-Billing"):
        if not ip_address or ip_address == "0.0.0.0":
            return
        res = self.talk(["/ip/firewall/address-list/print", "?list=Expired-Users", f"?address={ip_address}"])
        if not any(status == "!re" for status, _ in res):
            self.talk(["/ip/firewall/address-list/add", "=list=Expired-Users", f"=address={ip_address}", f"=comment={comment}"])

    def unblock_user(self, ip_address):
        res = self.talk(["/ip/firewall/address-list/print", "?list=Expired-Users", f"?address={ip_address}"])
        for status, attrs in res:
            if status == "!re" and ".id" in attrs:
                self.talk(["/ip/firewall/address-list/remove", f"=.id={attrs['.id']}"])

    def block_customer(self, username, ip_address=None, mode="address_list"):
        if mode in ("address_list", "both"):
            if not ip_address:
                actives = self.get_active_pppoe()
                if username in actives:
                    ip_address = actives[username]["address"]
            if ip_address:
                self.block_user(ip_address, comment=username)
        if mode in ("disable_secret", "both"):
            self.set_secret_disabled(username, disabled=True)

    def unblock_user_by_name(self, username):
        self.set_secret_disabled(username, disabled=False)
        actives = self.get_active_pppoe()
        ip = None
        if username in actives:
            ip = actives[username]["address"]
            self.unblock_user(ip)
        res = self.talk(["/ip/firewall/address-list/print", "?list=Expired-Users", f"?comment={username}"])
        for status, attrs in res:
            if status == "!re" and ".id" in attrs:
                self.talk(["/ip/firewall/address-list/remove", f"=.id={attrs['.id']}"])
        return ip


class MikrotikSSLAPI(MikrotikAPI):
    def __init__(self, host, port=8729, user="admin", password=""):
        super().__init__(host, port, user, password, use_ssl=True)


ROUTER_DRIVERS = {
    "mikrotik_api": MikrotikAPI,
    "mikrotik_api_ssl": MikrotikSSLAPI,
}


def get_router_driver(driver_name, host, port, user, password) -> BaseRouterDriver:
    cls = ROUTER_DRIVERS.get(driver_name, MikrotikAPI)
    return cls(host=host, port=port, user=user, password=password)


class LiveMikrotikPool:
    def __init__(self):
        self._lock = threading.Lock()
        self._client = None
        self._sig = None

    def get_live_speed(self, driver_name, host, port, user, password, username):
        sig = (driver_name, host, int(port), user, password)
        with self._lock:
            for attempt in range(2):
                try:
                    if self._client is None or self._sig != sig:
                        if self._client:
                            self._client.close()
                        self._client = get_router_driver(driver_name, host, port, user, password)
                        self._client.connect()
                        self._sig = sig
                    return self._client.get_live_speed(username)
                except Exception as e:
                    if self._client:
                        self._client.close()
                    self._client = None
                    if attempt == 1:
                        raise e


live_pool = LiveMikrotikPool()
