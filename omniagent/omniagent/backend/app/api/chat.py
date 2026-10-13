from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..agent import Agent, history_to_turns
from ..config import get_settings
from ..db import get_db
from ..deps import AuthContext, authenticated, authorized_domains, get_authorized_domain, require_roles
from ..llm import LLMError, get_llm
from ..models import Conversation, Message, utcnow
from ..rag import RagService
from ..runtime import DomainUnavailable, runtimes
from ..security import audit, client_ip, enforce_rate_limit
from ..tools import ToolContext, registry

router = APIRouter(prefix="/api", tags=["chat"])


class ChatIn(BaseModel):
    domain: str = Field(min_length=1, max_length=50)
    message: str = Field(min_length=1)
    conversation_id: str | None = Field(default=None, max_length=36)


def _msg_out(m: Message) -> dict:
    return {
        "id": m.id, "role": m.role, "content": m.content,
        "data": m.data_json or [], "steps": m.steps_json or [], "created_at": m.created_at.isoformat() + "Z",
    }


def _owned_conversation(db: Session, user_id: int, conv_id: str) -> Conversation:
    conv = db.get(Conversation, conv_id)
    if conv is None or conv.user_id != user_id:  # ownership check is mandatory
        raise HTTPException(404, "Conversation not found.")
    return conv


@router.post("/chat")
def chat(body: ChatIn, request: Request, ctx: AuthContext = Depends(authenticated), db: Session = Depends(get_db)):
    settings, user, ip = get_settings(), ctx.user, client_ip(request)
    enforce_rate_limit(f"chat:{user.id}", settings.chat_requests_per_minute)
    text = body.message.strip()
    if not text or len(text) > settings.max_message_chars:
        raise HTTPException(422, f"Message must be 1-{settings.max_message_chars} characters.")

    try:
        domain = get_authorized_domain(db, user, body.domain)
    except HTTPException:
        audit(db, "access_denied", user.id, ip, domain=body.domain[:50])
        raise

    if body.conversation_id:
        conv = _owned_conversation(db, user.id, body.conversation_id)
        if conv.domain_id != domain.id:
            raise HTTPException(400, "This conversation belongs to a different domain.")
    else:
        conv = Conversation(user_id=user.id, domain_id=domain.id, title=text[:60])
        db.add(conv)
        db.flush()

    try:
        runtime = runtimes.get(domain, settings)
        llm = get_llm()
    except DomainUnavailable as e:
        raise HTTPException(503, str(e)) from None
    except LLMError as e:
        raise HTTPException(503, str(e)) from None

    history = history_to_turns(conv.messages, settings.history_messages)
    db.add(Message(conversation_id=conv.id, role="user", content=text))
    conv.updated_at = utcnow()
    db.commit()

    tctx = ToolContext(user=user, runtime=runtime, settings=settings, db=db,
                       rag=RagService(settings, llm) if settings.rag_enabled else None)
    try:
        result = Agent(llm, registry).run(tctx, history, text)
    except LLMError as e:
        audit(db, "chat_error", user.id, ip, domain=domain.key)
        raise HTTPException(502, str(e)) from None

    reply = Message(conversation_id=conv.id, role="assistant", content=result.text,
                    data_json=result.blocks or None, steps_json=result.steps or None)
    db.add(reply)
    conv.updated_at = utcnow()
    db.commit()
    audit(db, "chat", user.id, ip, domain=domain.key, tools=[s.get("tool") for s in result.steps],
          failures=result.failures)
    return {"conversation_id": conv.id, "title": conv.title, "message": _msg_out(reply)}


@router.get("/conversations")
def list_conversations(domain: str | None = None, ctx: AuthContext = Depends(authenticated), db: Session = Depends(get_db)):
    q = select(Conversation).where(Conversation.user_id == ctx.user.id).order_by(Conversation.updated_at.desc()).limit(100)
    allowed = {d.id: d.key for d in authorized_domains(db, ctx.user)}
    rows = [c for c in db.scalars(q) if c.domain_id in allowed and (domain is None or allowed[c.domain_id] == domain)]
    return [{"id": c.id, "title": c.title, "domain": allowed[c.domain_id], "updated_at": c.updated_at.isoformat() + "Z"} for c in rows]


@router.get("/conversations/{conv_id}")
def get_conversation(conv_id: str, ctx: AuthContext = Depends(authenticated), db: Session = Depends(get_db)):
    conv = _owned_conversation(db, ctx.user.id, conv_id)
    get_authorized_domain(db, ctx.user, conv.domain.key)  # access may have been revoked since
    return {"id": conv.id, "title": conv.title, "domain": conv.domain.key, "messages": [_msg_out(m) for m in conv.messages]}


@router.post("/conversations/{conv_id}/clear")
def clear_conversation(conv_id: str, ctx: AuthContext = Depends(authenticated), db: Session = Depends(get_db)):
    conv = _owned_conversation(db, ctx.user.id, conv_id)
    for m in list(conv.messages):
        db.delete(m)
    db.commit()
    return {"ok": True}


@router.delete("/conversations/{conv_id}")
def delete_conversation(conv_id: str, ctx: AuthContext = Depends(authenticated), db: Session = Depends(get_db)):
    conv = _owned_conversation(db, ctx.user.id, conv_id)
    db.delete(conv)
    db.commit()
    return {"ok": True}


# ───────────── domains ─────────────
@router.get("/domains")
def domains(ctx: AuthContext = Depends(authenticated), db: Session = Depends(get_db)):
    return [
        {"key": d.key, "name": d.name, "description": d.description, "examples": (d.config_json or {}).get("examples", [])}
        for d in authorized_domains(db, ctx.user)
    ]


@router.get("/domains/{key}/schema")
def domain_schema(key: str, ctx: AuthContext = Depends(authenticated), db: Session = Depends(get_db)):
    domain = get_authorized_domain(db, ctx.user, key)
    try:
        return runtimes.get(domain).schema.to_public()
    except DomainUnavailable as e:
        raise HTTPException(503, str(e)) from None


@router.post("/domains/{key}/refresh-schema")
def refresh_schema(key: str, request: Request, ctx: AuthContext = Depends(require_roles("admin")), db: Session = Depends(get_db)):
    domain = get_authorized_domain(db, ctx.user, key)
    try:
        rt = runtimes.get(domain, refresh=True)
    except DomainUnavailable as e:
        raise HTTPException(503, str(e)) from None
    audit(db, "schema_refresh", ctx.user.id, client_ip(request), domain=key)
    return {"ok": True, "tables": len(rt.schema.tables)}


@router.get("/config")
def public_config(ctx: AuthContext = Depends(authenticated)):
    s = get_settings()
    return {"rag_enabled": s.rag_enabled, "max_upload_mb": s.max_upload_mb, "max_message_chars": s.max_message_chars,
            "gemini_configured": bool(s.gemini_api_key.get_secret_value())}
