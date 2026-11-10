"""Administrator login (PBKDF2 + signed cookie) and RTSP Digest.

No dependencies: `hashlib`, `hmac`, `secrets`, `base64`. The administrator
password is not stored, only PBKDF2-SHA256 with a salt. The cookie is signed
with an HMAC-SHA256 secret from the config, `SameSite=Strict`, `HttpOnly`.

RTSP Digest requires knowing HA1 = MD5(user:realm:password), so the config
stores HA1 itself — the plaintext password is not needed for RTSP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
from collections import deque

PBKDF2_ITERATIONS = 200_000
COOKIE_NAME = "lookcam_session"


class AuthError(RuntimeError):
    pass


class TooManyAttempts(AuthError):
    pass


# ---------------------------------------------------------------------- passwords

def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256${}${}${}".format(
        iterations, _b64(salt), _b64(digest))


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iters, salt_b64, hash_b64 = (stored or "").split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(hash_b64)
        digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode("utf-8"),
                                     salt, int(iters))
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def digest_ha1(user: str, realm: str, password: str) -> str:
    return hashlib.md5(f"{user}:{realm}:{password}".encode("utf-8")).hexdigest()


def digest_response(ha1: str, method: str, uri: str, nonce: str,
                    qop: str = "", nc: str = "", cnonce: str = "") -> str:
    ha2 = hashlib.md5(f"{method}:{uri}".encode("utf-8")).hexdigest()
    if qop:
        raw = f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}"
    else:
        raw = f"{ha1}:{nonce}:{ha2}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def parse_authorization(value: str) -> tuple[str, dict]:
    """Parse an Authorization/Proxy-Authorization header."""
    value = (value or "").strip()
    if not value:
        return "", {}
    scheme, _, rest = value.partition(" ")
    params: dict[str, str] = {}
    for m in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]*))', rest):
        params[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return scheme.lower(), params


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# ---------------------------------------------------------------------- cookie

def sign_cookie(secret: str, ttl_s: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    payload = f"v1.{int(now)}.{int(now + ttl_s)}.{secrets.token_hex(8)}"
    sig = hmac.new(secret.encode("utf-8"), payload.encode("ascii"),
                   hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def verify_cookie(secret: str, value: str, now: float | None = None) -> bool:
    try:
        parts = (value or "").split(".")
        if len(parts) != 5 or parts[0] != "v1":
            return False
        payload = ".".join(parts[:4])
        sig = parts[4]
        expected = hmac.new(secret.encode("utf-8"), payload.encode("ascii"),
                            hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return False
        now = time.time() if now is None else now
        return int(parts[1]) <= now <= int(parts[2])
    except Exception:
        return False


def parse_cookies(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for chunk in (header or "").split(";"):
        if "=" in chunk:
            key, _, value = chunk.partition("=")
            out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------------- HTTP Auth

class Auth:
    """Login state: password check, cookie, attempt rate limiting."""

    def __init__(self, config) -> None:
        self.config = config
        self._attempts: dict[str, deque] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.config.data["admin"].get("auth", True))

    @property
    def needs_setup(self) -> bool:
        return self.enabled and not self.config.data["admin"].get("password_hash")

    def _limit(self) -> tuple[int, float]:
        admin = self.config.data["admin"]
        return int(admin.get("login_attempts") or 8), float(admin.get("login_window_s") or 300)

    def check_rate(self, ip: str) -> None:
        limit, window = self._limit()
        bucket = self._attempts.setdefault(ip or "-", deque())
        now = time.time()
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            raise TooManyAttempts("слишком много попыток входа, подождите")

    def note_failure(self, ip: str) -> None:
        self._attempts.setdefault(ip or "-", deque()).append(time.time())

    def note_success(self, ip: str) -> None:
        self._attempts.pop(ip or "-", None)

    def check_password(self, password: str) -> bool:
        stored = self.config.data["admin"].get("password_hash") or ""
        if not stored:
            return False
        return verify_password(password or "", stored)

    def make_cookie(self) -> str:
        secret = self.config.data["admin"]["cookie_secret"]
        ttl = int(self.config.data["admin"].get("session_hours") or 72) * 3600
        return sign_cookie(secret, ttl)

    def cookie_header(self) -> str:
        ttl = int(self.config.data["admin"].get("session_hours") or 72) * 3600
        return (f"{COOKIE_NAME}={self.make_cookie()}; Path=/; HttpOnly; "
                f"SameSite=Strict; Max-Age={ttl}")

    @staticmethod
    def clear_cookie_header() -> str:
        return f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"

    def authorized(self, headers: dict) -> bool:
        if not self.enabled:
            return True
        if self.needs_setup:
            return False
        value = parse_cookies(headers.get("cookie", "")).get(COOKIE_NAME, "")
        return verify_cookie(self.config.data["admin"]["cookie_secret"], value)

    def login(self, ip: str, password: str) -> str:
        self.check_rate(ip)
        if not self.check_password(password):
            self.note_failure(ip)
            raise AuthError("неверный пароль")
        self.note_success(ip)
        return self.make_cookie()


# ---------------------------------------------------------------------- RTSP Digest

class DigestRealm:
    """Nonces and Digest verification for the RTSP server."""

    def __init__(self, user: str, ha1: str, realm: str = "LookCam",
                 nonce_ttl: float = 300.0) -> None:
        self.user = user
        self.ha1 = (ha1 or "").lower()
        self.realm = realm
        self.nonce_ttl = nonce_ttl
        self._nonces: dict[str, float] = {}
        self.enabled = bool(self.user and self.ha1)

    def new_nonce(self) -> str:
        nonce = secrets.token_hex(16)
        self._nonces[nonce] = time.time()
        now = time.time()
        for key, created in list(self._nonces.items()):
            if now - created > self.nonce_ttl:
                self._nonces.pop(key, None)
        return nonce

    def challenge(self) -> str:
        return (f'Digest realm="{self.realm}", nonce="{self.new_nonce()}", '
                f'qop="auth", algorithm=MD5')

    def verify(self, method: str, uri: str, header: str) -> bool:
        if not self.enabled:
            return True
        scheme, params = parse_authorization(header)
        if scheme != "digest":
            return False
        if params.get("username") != self.user:
            return False
        nonce = params.get("nonce", "")
        if nonce not in self._nonces or time.time() - self._nonces[nonce] > self.nonce_ttl:
            return False
        # the uri from the header may differ from the requested one — check both
        uri_ok = params.get("uri", "") in (uri, "", "*") or params.get("uri", "").endswith(uri)
        if not uri_ok:
            return False
        qop = params.get("qop", "")
        expected = digest_response(self.ha1, method, params.get("uri", uri), nonce,
                                   qop, params.get("nc", ""), params.get("cnonce", ""))
        return hmac.compare_digest(expected, params.get("response", "").lower())
