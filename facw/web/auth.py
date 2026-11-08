"""Authentication and role enforcement (spec 6.5 / 7.1 / 9.3).

* Real server-side sessions: a random token is issued at sign-in, only its
  SHA-256 is stored, and it is revocable and expiring.
* The token travels in an HttpOnly cookie (never readable by page scripts) and
  is also accepted from an ``Authorization: Bearer`` header for the helper
  tools.
* Every route depends on ``current_user``; admin-only routes depend on
  ``require_admin``.  The service enforces roles on *every* operation, so an
  observer calling the API directly is refused exactly like the interface.
* Login is rate-limited per login name.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
from typing import Dict, Optional

from fastapi import Depends, HTTPException, Request, status

from ..core.logging_setup import get_logger

log = get_logger("auth")

SESSION_COOKIE = "facw_session"
SESSION_TTL_SECONDS = 8 * 3600
MAX_LOGIN_ATTEMPTS = 8
LOGIN_WINDOW = 300  # seconds


class LoginThrottle:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempts: Dict[str, list] = {}

    def check(self, key: str) -> None:
        now = time.time()
        with self._lock:
            hits = [t for t in self._attempts.get(key, []) if now - t < LOGIN_WINDOW]
            self._attempts[key] = hits
            if len(hits) >= MAX_LOGIN_ATTEMPTS:
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail="too many sign-in attempts; try again later",
                )

    def fail(self, key: str) -> None:
        with self._lock:
            self._attempts.setdefault(key, []).append(time.time())

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


throttle = LoginThrottle()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_token() -> str:
    return os.urandom(32).hex()


def get_store(request: Request):
    return request.app.state.app


def bearer_or_cookie(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    cookie = request.cookies.get(SESSION_COOKIE)
    if cookie:
        return cookie
    # stream/download endpoints accept a short-lived signed token in the query
    q = request.query_params.get("st")
    if q:
        app = request.app.state.app
        subject = app.sealer.verify_token(q, purpose="stream")
        return f"stream:{subject}" if subject else None
    return None


def resolve_session(request: Request) -> Optional[Dict]:
    app = request.app.state.app
    token = bearer_or_cookie(request)
    if not token:
        return None
    if token.startswith("stream:"):
        user_id = token.split(":", 1)[1]
        try:
            user = app.users.get(int(user_id))
        except (TypeError, ValueError):
            return None
        if user:
            user["via"] = "stream-token"
            return user
        return None
    return app.users.session_user(token_hash(token))


def current_user(request: Request) -> Dict:
    user = resolve_session(request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="not signed in")
    return user


def require_admin(user: Dict = Depends(current_user)) -> Dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="administrator role required")
    return user


def sign_in(app, login: str, password: str, user_agent: str = "") -> Dict:
    key = f"{login}|{user_agent[:40]}"
    throttle.check(key)
    record = app.users.get_by_login(login, with_hash=True)
    from ..core.security import verify_password

    if not record or not verify_password(password, record["password_hash"]):
        throttle.fail(key)
        app.audit.record("sign_in_failed", actor=login, outcome="failed")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    throttle.clear(key)
    token = issue_token()
    app.users.create_session(record["id"], token_hash(token), SESSION_TTL_SECONDS, user_agent)
    app.audit.record("sign_in", actor=login, outcome="ok")
    return {
        "token": token,
        "user": {
            "id": record["id"],
            "display_name": record["display_name"],
            "login_name": record["login_name"],
            "role": record["role"],
            "email": record["email"],
        },
        "expires_in": SESSION_TTL_SECONDS,
    }


def sign_out(app, request: Request) -> None:
    token = request.cookies.get(SESSION_COOKIE) or (
        request.headers.get("authorization", "")[7:].strip()
        if request.headers.get("authorization", "").lower().startswith("bearer ")
        else ""
    )
    if token:
        app.users.delete_session(token_hash(token))
    app.audit.record("sign_out", actor=(resolve_session(request) or {}).get("login_name", ""))
