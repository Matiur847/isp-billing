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
        # RouterOS v6 Challenge-response MD5 or direct login
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

    # High-level actions
    def get_active_pppoe(self):
        """Returns dict of {username: ip_address}"""
        res = self.talk(["/ppp/active/print"])
        users = {}
        for status, attrs in res:
            if status == "!re":
                name = attrs.get("name")
                addr = attrs.get("address")
                if name and addr:
                    users[name] = addr
        return users

    def block_user(self, ip_address, comment="Expired-Billing"):
        """Adds user IP to Expired-Users firewall address-list"""
        # First check if already in list
        res = self.talk(["/ip/firewall/address-list/print", "?list=Expired-Users", f"?address={ip_address}"])
        exists = any(status == "!re" for status, attrs in res)
        if not exists:
            self.talk(["/ip/firewall/address-list/add", "=list=Expired-Users", f"=address={ip_address}", f"=comment={comment}"])
            logger.info(f"Added {ip_address} to Expired-Users")

    def unblock_user(self, ip_address):
        """Removes user IP from Expired-Users firewall address-list"""
        res = self.talk(["/ip/firewall/address-list/print", "?list=Expired-Users", f"?address={ip_address}"])
        for status, attrs in res:
            if status == "!re" and ".id" in attrs:
                entry_id = attrs[".id"]
                self.talk(["/ip/firewall/address-list/remove", f"=.id={entry_id}"])
                logger.info(f"Removed {ip_address} (.id={entry_id}) from Expired-Users")

    def unblock_user_by_name(self, username):
        """Finds user IP from active connections and removes from Expired-Users"""
        actives = self.get_active_pppoe()
        if username in actives:
            ip = actives[username]
            self.unblock_user(ip)
            return ip
        # Also clean by comment if IP wasn't online
        res = self.talk(["/ip/firewall/address-list/print", "?list=Expired-Users", f"?comment={username}"])
        for status, attrs in res:
            if status == "!re" and ".id" in attrs:
                self.talk(["/ip/firewall/address-list/remove", f"=.id={attrs['.id']}"])
        return None
