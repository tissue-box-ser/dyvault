"""访问密码：PBKDF2 存储 + HMAC 会话 Cookie + 登录失败限速。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Dict, Optional, Tuple

PBKDF2_ROUNDS = 150_000
SESSION_DAYS = 30
MAX_FAILS = 8
LOCK_SECONDS = 120


def hash_password(password: str, salt: str = "") -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = (stored or "").split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    try:
        rounds_int = int(rounds)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac(
        "sha256", (password or "").encode("utf-8"), salt.encode("utf-8"), rounds_int
    )
    return hmac.compare_digest(calc.hex(), digest)


class Auth:
    SESSION_COOKIE = "dy_session"

    def __init__(self, settings):
        self.settings = settings
        self._fails: Dict[str, Tuple[int, float]] = {}

    # ------------------------------------------------------------ 密码

    def password_set(self) -> bool:
        return bool(self.settings.get("password_hash"))

    def set_password(self, password: str) -> None:
        self.settings.set("password_hash", hash_password(password))
        self._fails.clear()

    def verify(self, password: str) -> bool:
        stored = self.settings.get("password_hash")
        if not stored:
            return False
        return verify_password(password, stored)

    # ------------------------------------------------------------ 会话

    def _secret(self) -> str:
        secret = self.settings.get("secret_key")
        if not secret:
            secret = secrets.token_hex(32)
            self.settings.set("secret_key", secret)
        return secret

    def _sign_key(self) -> bytes:
        # 绑上当前密码指纹：改密码后老会话自动失效
        return (self._secret() + "|" + self.settings.get("password_hash", "")).encode("utf-8")

    def issue(self, days: int = SESSION_DAYS) -> str:
        exp = int(time.time()) + days * 86400
        payload = base64.urlsafe_b64encode(str(exp).encode()).decode().rstrip("=")
        sig = hmac.new(self._sign_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{payload}.{sig}"

    def verify_session(self, token: Optional[str]) -> bool:
        if not token or "." not in token or not self.password_set():
            return False
        payload, _, sig = token.partition(".")
        expect = hmac.new(self._sign_key(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return False
        try:
            padded = payload + "=" * (-len(payload) % 4)
            exp = int(base64.urlsafe_b64decode(padded).decode())
        except (ValueError, UnicodeDecodeError):
            return False
        return exp > int(time.time())

    # ------------------------------------------------------------ 限速

    def locked(self, ip: str) -> int:
        fails, until = self._fails.get(ip, (0, 0))
        remain = int(until - time.time())
        return remain if remain > 0 else 0

    def fail(self, ip: str) -> None:
        fails, until = self._fails.get(ip, (0, 0))
        fails += 1
        if fails >= MAX_FAILS:
            until = time.time() + LOCK_SECONDS
            fails = 0
        self._fails[ip] = (fails, until)

    def reset(self, ip: str) -> None:
        self._fails.pop(ip, None)
