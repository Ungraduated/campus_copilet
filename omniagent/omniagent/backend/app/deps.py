"""Request dependencies: authentication, CSRF, role checks and domain authorization."""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import AuthSession, Domain, User, UserDomain, utcnow
from .security import hash_token

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class AuthContext:
    user: User
    session: AuthSession


def authenticated(request: Request, db: Session = Depends(get_db)) -> AuthContext:
    """Validates the session cookie and, for state-changing requests, the CSRF header."""
    settings = get_settings()
    token = request.cookies.get(settings.cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="Not signed in.")
    sess = db.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(token)))
    if sess is None or sess.expires_at < utcnow():
        if sess is not None:
            db.delete(sess)
            db.commit()
        raise HTTPException(status_code=401, detail="Session expired. Please sign in again.")
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Account unavailable.")
    if request.method not in SAFE_METHODS:
        header = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(header, sess.csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token.")
    return AuthContext(user=user, session=sess)


def require_roles(*roles: str):
    def checker(ctx: AuthContext = Depends(authenticated)) -> AuthContext:
        if ctx.user.role not in roles:
            raise HTTPException(status_code=403, detail="You do not have permission for this action.")
        return ctx

    return checker


def authorized_domains(db: Session, user: User) -> list[Domain]:
    q = select(Domain).where(Domain.is_active.is_(True)).order_by(Domain.id)
    if user.role != "admin":
        q = q.join(UserDomain, UserDomain.domain_id == Domain.id).where(UserDomain.user_id == user.id)
    return list(db.scalars(q))


def get_authorized_domain(db: Session, user: User, key: str) -> Domain:
    """Server-side domain authorization. Same error for 'missing' and 'forbidden' (no enumeration)."""
    for d in authorized_domains(db, user):
        if d.key == key:
            return d
    raise HTTPException(status_code=403, detail="You do not have access to this domain.")
