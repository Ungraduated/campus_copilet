from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import get_db
from ..deps import AuthContext, authenticated, authorized_domains
from ..models import Domain, User, UserDomain
from ..security import (
    audit, client_ip, create_session, enforce_rate_limit, hash_password, hash_token,
    password_problem, verify_password,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterIn(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(max_length=256)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=256)


def _set_cookie(resp: Response, token: str, settings: Settings) -> None:
    resp.set_cookie(
        settings.cookie_name, token, max_age=settings.session_ttl_hours * 3600,
        httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
    )


def user_payload(db: Session, user: User, csrf: str) -> dict:
    return {
        "user": {"id": user.id, "email": user.email, "display_name": user.display_name, "role": user.role},
        "csrf_token": csrf,
        "domains": [{"key": d.key, "name": d.name, "description": d.description} for d in authorized_domains(db, user)],
    }


@router.post("/register", status_code=201)
def register(body: RegisterIn, request: Request, response: Response, db: Session = Depends(get_db)):
    settings = get_settings()
    ip = client_ip(request)
    if not settings.allow_registration:
        raise HTTPException(403, "Registration is disabled.")
    enforce_rate_limit(f"auth:{ip}", settings.auth_attempts_per_minute)
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(422, problem)
    email = body.email.lower()
    if db.scalar(select(User).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists.")
    user = User(email=email, display_name=body.display_name.strip(), password_hash=hash_password(body.password), role="viewer")
    db.add(user)
    db.flush()
    for key in settings.default_grants:
        dom = db.scalar(select(Domain).where(Domain.key == key))
        if dom:
            db.add(UserDomain(user_id=user.id, domain_id=dom.id))
    db.commit()
    token, sess = create_session(db, user, request)
    audit(db, "register", user.id, ip)
    _set_cookie(response, token, settings)
    return user_payload(db, user, sess.csrf_token)


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    settings = get_settings()
    ip, email = client_ip(request), body.email.lower()
    enforce_rate_limit(f"auth:{ip}", settings.auth_attempts_per_minute)
    enforce_rate_limit(f"login:{email}", settings.auth_attempts_per_minute)
    user = db.scalar(select(User).where(User.email == email))
    ok = verify_password(body.password, user.password_hash if user else None)
    if not (ok and user and user.is_active):
        audit(db, "login_failed", user.id if user else None, ip)
        raise HTTPException(401, "Incorrect email or password.")
    token, sess = create_session(db, user, request)
    audit(db, "login", user.id, ip)
    _set_cookie(response, token, settings)
    return user_payload(db, user, sess.csrf_token)


@router.post("/logout")
def logout(request: Request, response: Response, ctx: AuthContext = Depends(authenticated), db: Session = Depends(get_db)):
    db.delete(ctx.session)
    db.commit()
    audit(db, "logout", ctx.user.id, client_ip(request))
    response.delete_cookie(get_settings().cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(ctx: AuthContext = Depends(authenticated), db: Session = Depends(get_db)):
    return user_payload(db, ctx.user, ctx.session.csrf_token)
