import socket
import hashlib
import binascii
import logging

logger = logging.getLogger("mikrotik")

class MikrotikAPI:
    def __init__(self, host, port=8728, user="admin", password=""):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.sock = None

    def connect(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.settimeout(10.0)
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
            c2 = self.sock.recv(1)[0]
            return ((c & 0x3F) << 8) | c2
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
                    if len(parts) == 2:
                        attrs[parts[0]] = parts[1]
                    else:
                        attrs[parts[0]] = ""
            replies.append((status, attrs))
            if status == "!done" or status == "!trap" or status == "!fatal":
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

    # --- Live PPPoE Monitoring with Usage & MAC ---
    def get_active_pppoe(self):
        """Returns dict of active PPPoE sessions with IP, Uptime, MAC, and Data Usage (GB)"""
        res = self.talk(["/ppp/active/print"])
        users = {}
        for status, attrs in res:
            if status == "!re":
                name = attrs.get("name")
                if name:
                    users[name] = {
                        "address": attrs.get("address", "0.0.0.0"),
                        "uptime": attrs.get("uptime", "0s"),
                        "id": attrs.get(".id", ""),
                        "caller_id": attrs.get("caller-id", "No MAC"),
                        "session_id": attrs.get("session-id", "")
                    }
        return users

    def get_live_speed(self, username):
        """Measures live RX (Download) and TX (Upload) speed in Mbps for a user interface"""
        # Dynamic interface is named <pppoe-username>
        iface_candidates = [f"<pppoe-{username}>", username]
        speed_data = {"rx_mbps": 0.0, "tx_mbps": 0.0, "online": False}
        for iface in iface_candidates:
            res = self.talk(["/interface/monitor-traffic", f"=interface={iface}", "=once="])
            for status, attrs in res:
                if status == "!re":
                    rx_bps = int(attrs.get("rx-bits-per-second", 0))
                    tx_bps = int(attrs.get("tx-bits-per-second", 0))
                    speed_data["rx_mbps"] = round(rx_bps / 1_000_000, 2)
                    speed_data["tx_mbps"] = round(tx_bps / 1_000_000, 2)
                    speed_data["online"] = True
                    return speed_data
        return speed_data

    def kick_user(self, username):
        actives = self.get_active_pppoe()
        if username in actives:
            active_id = actives[username]["id"]
            self.talk(["/ppp/active/remove", f"=.id={active_id}"])
            logger.info(f"Kicked active session for {username}")
            return True
        return False

    def create_pppoe_secret(self, username, password, profile="default", comment=""):
        res = self.talk(["/ppp/secret/print", f"?name={username}"])
        exists = any(status == "!re" for status, attrs in res)
        if exists:
            return self.update_pppoe_secret(username, password, profile, comment)
        
        words = ["/ppp/secret/add", f"=name={username}", f"=password={password}", f"=service=pppoe", f"=profile={profile}"]
        if comment:
            words.append(f"=comment={comment}")
        self.talk(words)
        return True

    def update_pppoe_secret(self, username, password=None, profile=None, comment=None):
        res = self.talk(["/ppp/secret/print", f"?name={username}"])
        secret_id = None
        for status, attrs in res:
            if status == "!re":
                secret_id = attrs.get(".id")
                break
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

    def delete_pppoe_secret(self, username):
        res = self.talk(["/ppp/secret/print", f"?name={username}"])
        secret_id = None
        for status, attrs in res:
            if status == "!re":
                secret_id = attrs.get(".id")
                break
        if secret_id:
            self.talk(["/ppp/secret/remove", f"=.id={secret_id}"])
            self.kick_user(username)
            return True
        return False

    def block_user(self, ip_address, comment="Expired-Billing"):
        res = self.talk(["/ip/firewall/address-list/print", "?list=Expired-Users", f"?address={ip_address}"])
        exists = any(status == "!re" for status, attrs in res)
        if not exists:
            self.talk(["/ip/firewall/address-list/add", "=list=Expired-Users", f"=address={ip_address}", f"=comment={comment}"])

    def unblock_user(self, ip_address):
        res = self.talk(["/ip/firewall/address-list/print", "?list=Expired-Users", f"?address={ip_address}"])
        for status, attrs in res:
            if status == "!re" and ".id" in attrs:
                self.talk(["/ip/firewall/address-list/remove", f"=.id={attrs['.id']}"])

    def unblock_user_by_name(self, username):
        actives = self.get_active_pppoe()
        if username in actives:
            ip = actives[username]["address"]
            self.unblock_user(ip)
            return ip
        res = self.talk(["/ip/firewall/address-list/print", "?list=Expired-Users", f"?comment={username}"])
        for status, attrs in res:
            if status == "!re" and ".id" in attrs:
                self.talk(["/ip/firewall/address-list/remove", f"=.id={attrs['.id']}"])
        return None
