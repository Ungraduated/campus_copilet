"""Password hashing, session tokens, rate limiting and audit logging."""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .models import AuditEvent, AuthSession, User, utcnow

log = logging.getLogger("omniagent.security")
_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("not-a-real-password")


# ───────────── passwords ─────────────
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Constant-ish time: verifies against a dummy hash when the user does not exist."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_problem(password: str) -> str | None:
    if len(password) < 10:
        return "Password must be at least 10 characters."
    if len(password) > 128:
        return "Password must be at most 128 characters."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must contain at least one letter and one digit."
    return None


# ───────────── sessions ─────────────
def hash_token(token: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return hmac.new(settings.signing_key, token.encode(), hashlib.sha256).hexdigest()


def client_ip(request: Request, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if settings.trust_proxy_headers:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


def create_session(db: Session, user: User, request: Request) -> tuple[str, AuthSession]:
    settings = get_settings()
    token = secrets.token_urlsafe(32)
    row = AuthSession(
        token_hash=hash_token(token),
        csrf_token=secrets.token_urlsafe(24),
        user_id=user.id,
        expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:255],
    )
    db.add(row)
    user.last_login_at = utcnow()
    db.commit()
    return token, row


# ───────────── rate limiting (in-process sliding window) ─────────────
class RateLimiter:
    """Simple sliding-window limiter. For multi-process deployments swap for Redis."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str, limit: int, window: int = 60) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > window:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            if len(self._hits) > 10_000:  # opportunistic cleanup
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > window]:
                    self._hits.pop(k, None)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def enforce_rate_limit(key: str, limit: int, window: int = 60) -> None:
    if not limiter.check(key, limit, window):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a moment and try again.")


# ───────────── audit ─────────────
def audit(db: Session, event_type: str, user_id: int | None = None, ip: str | None = None, **detail) -> None:
    """Record a security-relevant event. Never pass secrets or full prompts in ``detail``."""
    try:
        db.add(AuditEvent(user_id=user_id, event_type=event_type, detail=detail or None, ip=ip))
        db.commit()
    except Exception:  # auditing must never break the request
        db.rollback()
        log.exception("failed to write audit event %s", event_type)
