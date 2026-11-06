"""The optional password. With no auth.json Encore stays open, as it always has.

Deleting data/auth.json and restarting turns the password off, which is the way back in if
it's forgotten. Changing the password makes a new signing key, so every session ends.
"""
import hashlib
import hmac
import json
import os
import secrets
import time

SESSION_DAYS = 90
MIN_LENGTH = 8
MAX_FAILS = 5
LOCK_SECONDS = 60


def scrypt(password, salt):
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


class Auth:
    def __init__(self, path):
        self.path = path
        self.data = json.loads(path.read_text()) if path.exists() else None
        self.fails = {}

    @property
    def enabled(self):
        return bool(self.data)

    def check(self, password):
        if not self.data:
            return False
        expected = bytes.fromhex(self.data["hash"])
        return hmac.compare_digest(scrypt(password, bytes.fromhex(self.data["salt"])), expected)

    def set(self, password):
        salt = secrets.token_bytes(16)
        data = {"salt": salt.hex(), "hash": scrypt(password, salt).hex(), "key": secrets.token_hex(32)}
        temp = self.path.with_suffix(".tmp")
        with open(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as out:
            json.dump(data, out)
        temp.replace(self.path)
        self.data = data

    def clear(self):
        self.path.unlink(missing_ok=True)
        self.data = None

    def sign(self, expires):
        return hmac.new(bytes.fromhex(self.data["key"]), str(expires).encode(), hashlib.sha256).hexdigest()

    def token(self):
        expires = int(time.time()) + SESSION_DAYS * 86400
        return f"{expires}.{self.sign(expires)}"

    def valid(self, token):
        if not self.data or "." not in (token or ""):
            return False
        expires, _, signature = token.partition(".")
        return expires.isdigit() and int(expires) > time.time() and hmac.compare_digest(signature, self.sign(int(expires)))

    def locked(self, address):
        count, until = self.fails.get(address, (0, 0))
        return count >= MAX_FAILS and time.time() < until

    def failed(self, address):
        count, until = self.fails.get(address, (0, 0))
        count = 1 if count >= MAX_FAILS and time.time() >= until else count + 1
        self.fails[address] = (count, time.time() + LOCK_SECONDS)

    def passed(self, address):
        self.fails.pop(address, None)
