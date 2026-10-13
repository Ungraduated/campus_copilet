from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..deps import AuthContext, require_roles
from ..models import AuditEvent, AuthSession, Domain, User, UserDomain
from ..security import audit, client_ip

router = APIRouter(prefix="/api/admin", tags=["admin"])
admin_only = require_roles("admin")


class UserUpdate(BaseModel):
    role: Literal["admin", "analyst", "viewer"] | None = None
    is_active: bool | None = None
    domains: list[str] | None = None  # domain keys


@router.get("/users")
def users(ctx: AuthContext = Depends(admin_only), db: Session = Depends(get_db)):
    doms = {d.id: d.key for d in db.scalars(select(Domain))}
    grants: dict[int, list[str]] = {}
    for g in db.scalars(select(UserDomain)):
        grants.setdefault(g.user_id, []).append(doms.get(g.domain_id, "?"))
    return {
        "domains": [{"key": k, "name": k} for k in doms.values()],
        "users": [
            {"id": u.id, "email": u.email, "display_name": u.display_name, "role": u.role,
             "is_active": u.is_active, "domains": sorted(grants.get(u.id, []))}
            for u in db.scalars(select(User).order_by(User.id))
        ],
    }


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserUpdate, request: Request, ctx: AuthContext = Depends(admin_only),
                db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    if user.id == ctx.user.id and (body.role not in (None, "admin") or body.is_active is False):
        raise HTTPException(400, "You cannot demote or deactivate your own account.")
    changes: dict = {}
    if body.role is not None:
        user.role, changes["role"] = body.role, body.role
    if body.is_active is not None:
        user.is_active, changes["is_active"] = body.is_active, body.is_active
        if not body.is_active:
            db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
    if body.domains is not None:
        wanted = list(db.scalars(select(Domain).where(Domain.key.in_(body.domains))))
        db.execute(delete(UserDomain).where(UserDomain.user_id == user.id))
        for d in wanted:
            db.add(UserDomain(user_id=user.id, domain_id=d.id))
        changes["domains"] = [d.key for d in wanted]
    db.commit()
    audit(db, "admin_user_update", ctx.user.id, client_ip(request), target=user.id, **changes)
    return {"ok": True}


@router.get("/audit")
def audit_log(limit: int = 100, ctx: AuthContext = Depends(admin_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(max(1, min(limit, 500))))
    return [{"id": a.id, "user_id": a.user_id, "event": a.event_type, "detail": a.detail, "ip": a.ip,
             "at": a.created_at.isoformat() + "Z"} for a in rows]
