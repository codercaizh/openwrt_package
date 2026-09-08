"""Cookie session authentication, password hashing, CSRF and login limits."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from urllib.parse import urlsplit
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, Request, Response, status

from .storage import Storage


PASSWORD_SCHEME = "pbkdf2_sha256"
PASSWORD_ROUNDS = 310_000
SESSION_COOKIE = "owrt_session"
CSRF_COOKIE = "owrt_csrf"


def hash_password(password: str, rounds: int = PASSWORD_ROUNDS) -> str:
    if not isinstance(password, str) or len(password) < 12:
        raise ValueError("管理员密码至少需要 12 个字符")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return f"{PASSWORD_SCHEME}${rounds}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, rounds_text, salt_text, digest_text = encoded.split("$", 3)
        if scheme != PASSWORD_SCHEME:
            return False
        rounds = int(rounds_text)
        salt = _unb64(salt_text)
        expected = _unb64(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _expiry_iso(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class AuthSettings:
    session_seconds: int = 12 * 60 * 60
    login_window_seconds: int = 15 * 60
    login_max_attempts: int = 8
    secure_cookie: bool = False
    cookie_samesite: str = "strict"
    allowed_origin: str | None = None
    allowed_origins: tuple[str, ...] = ()
    allow_any_origin: bool = False


class AuthManager:
    def __init__(self, storage: Storage, settings: AuthSettings | None = None):
        self.storage = storage
        self.settings = settings or AuthSettings()

    @staticmethod
    def _limit_key(request: Request, username: str) -> str:
        host = request.client.host if request.client else "unknown"
        normalized = username.strip().casefold()[:160]
        return f"{host}:{normalized}"

    def login(self, request: Request, response: Response, username: str, password: str) -> dict[str, Any]:
        self.validate_origin(request)
        key = self._limit_key(request, username)
        allowed, retry_after = self.storage.login_limit(key, int(time.time()), self.settings.login_window_seconds, self.settings.login_max_attempts)
        if not allowed:
            raise HTTPException(status_code=429, detail="登录尝试过于频繁，请稍后再试", headers={"Retry-After": str(max(1, retry_after))})
        user = self.storage.get_admin_by_username(username.strip())
        if user is None or not verify_password(password, user["password_hash"]):
            # Do not distinguish an unknown account from a wrong password.
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        self.storage.clear_login_limit(key)
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        self.storage.create_session(
            int(user["id"]), token, csrf, _expiry_iso(self.settings.session_seconds),
            request.headers.get("user-agent"), request.client.host if request.client else None,
        )
        self.storage.mark_login(int(user["id"]))
        self.set_cookies(response, token, csrf)
        return {"id": int(user["id"]), "username": user["username"], "csrf": csrf, "expires_in": self.settings.session_seconds}

    def set_cookies(self, response: Response, token: str, csrf: str) -> None:
        common = {
            "secure": self.settings.secure_cookie,
            "httponly": False,
            "samesite": self.settings.cookie_samesite,
            "max_age": self.settings.session_seconds,
            "path": "/",
        }
        response.set_cookie(SESSION_COOKIE, token, httponly=True, **{k: v for k, v in common.items() if k != "httponly"})
        response.set_cookie(CSRF_COOKIE, csrf, **common)

    def clear_cookies(self, response: Response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")
        response.delete_cookie(CSRF_COOKIE, path="/")

    def session(self, request: Request) -> dict[str, Any] | None:
        token = request.cookies.get(SESSION_COOKIE)
        if not token:
            return None
        session = self.storage.get_session(token)
        if session is None:
            return None
        if session["expires_at"] <= _expiry_iso(0):
            self.storage.delete_session(token)
            return None
        return session

    def require_session(self, request: Request) -> dict[str, Any]:
        session = self.session(request)
        if session is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="需要管理员登录")
        request.state.session = session
        request.state.user_id = int(session["user_id"])
        request.state.username = session["username"]
        return session

    def validate_csrf(self, request: Request) -> dict[str, Any]:
        session = self.require_session(request)
        csrf_cookie = request.cookies.get(CSRF_COOKIE)
        csrf_header = request.headers.get("x-csrf-token")
        if not csrf_cookie or not csrf_header or not hmac.compare_digest(csrf_cookie, csrf_header):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        stored = session["csrf_hash"]
        if not hmac.compare_digest(hashlib.sha256(csrf_header.encode("utf-8")).hexdigest(), stored):
            raise HTTPException(status_code=403, detail="CSRF 校验失败")
        self.validate_origin(request)
        return session

    def validate_origin(self, request: Request) -> None:
        """Require a valid Origin and apply the configured origin policy.

        ``OWRT_ALLOWED_ORIGINS`` is a comma-separated allow-list.  Its
        explicit ``*`` value accepts every syntactically valid HTTP(S)
        Origin; the CSRF cookie/header comparison still protects state
        changing calls.  Without that setting, ``OWRT_PUBLIC_ORIGIN`` is
        preferred.  If neither is configured, the expected origin is derived
        from the request host; forwarded headers are accepted only when
        ``OWRT_TRUST_PROXY=1`` is explicitly enabled.
        """
        origin = request.headers.get("origin")
        if not origin:
            raise HTTPException(status_code=403, detail="缺少 Origin")
        origin_key = _origin_key(origin)
        if not origin_key:
            raise HTTPException(status_code=403, detail="Origin 不合法")
        if self.settings.allow_any_origin or "*" in self.settings.allowed_origins:
            return
        configured = tuple(_origin_key(value) for value in self.settings.allowed_origins)
        if not configured and self.settings.allowed_origin:
            configured = (_origin_key(self.settings.allowed_origin),)
        configured = tuple(value for value in configured if value)
        if configured:
            if origin_key in configured:
                return
            raise HTTPException(status_code=403, detail="Origin 不在允许列表中")
        expected = _origin_key(self._request_origin(request))
        if origin_key != expected:
            raise HTTPException(status_code=403, detail="Origin 不被允许")

    @staticmethod
    def _request_origin(request: Request) -> str:
        trust_proxy = os.getenv("OWRT_TRUST_PROXY", "0").lower() in {"1", "true", "yes"}
        if trust_proxy:
            proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",", 1)[0].strip()
            host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc)).split(",", 1)[0].strip()
        else:
            proto = request.url.scheme
            host = request.headers.get("host", request.url.netloc)
        return f"{proto}://{host}"


def _origin_key(value: str) -> str:
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or any(character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in candidate):
        return ""
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return ""
    scheme = parsed.scheme.lower()
    host = hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port is not None and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def settings_from_env() -> AuthSettings:
    raw_allowed_origins = os.getenv("OWRT_ALLOWED_ORIGINS", "").strip()
    allowed_origins: tuple[str, ...] = ()
    allow_any_origin = False
    if raw_allowed_origins:
        configured_values = tuple(item.strip() for item in raw_allowed_origins.split(",") if item.strip())
        invalid = [item for item in configured_values if item != "*" and not _origin_key(item)]
        if invalid:
            raise ValueError("OWRT_ALLOWED_ORIGINS 包含非法 Origin")
        allow_any_origin = "*" in configured_values
        if not allow_any_origin:
            allowed_origins = tuple(dict.fromkeys(_origin_key(item) for item in configured_values))
    return AuthSettings(
        session_seconds=max(300, int(os.getenv("OWRT_SESSION_SECONDS", str(12 * 60 * 60)))),
        login_window_seconds=max(60, int(os.getenv("OWRT_LOGIN_WINDOW_SECONDS", str(15 * 60)))),
        login_max_attempts=max(1, int(os.getenv("OWRT_LOGIN_MAX_ATTEMPTS", "8"))),
        secure_cookie=os.getenv("OWRT_SECURE_COOKIE", "0").lower() in {"1", "true", "yes"},
        cookie_samesite=os.getenv("OWRT_COOKIE_SAMESITE", "strict").lower(),
        allowed_origin=(os.getenv("OWRT_PUBLIC_ORIGIN") or None) if not raw_allowed_origins else None,
        allowed_origins=allowed_origins,
        allow_any_origin=allow_any_origin,
    )
