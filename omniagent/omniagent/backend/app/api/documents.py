from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..deps import AuthContext, require_roles
from ..llm import LLMError, get_llm
from ..models import Document
from ..rag import DocumentError, RagService
from ..security import audit, client_ip, enforce_rate_limit

router = APIRouter(prefix="/api/documents", tags=["documents"])


def _rag() -> RagService:
    s = get_settings()
    if not s.rag_enabled:
        raise HTTPException(404, "Document Q&A is not enabled on this server.")
    try:
        return RagService(s, get_llm())
    except LLMError as e:
        raise HTTPException(503, str(e)) from None


def _out(d: Document) -> dict:
    return {"id": d.id, "filename": d.filename, "size_bytes": d.size_bytes, "chunks": d.chunk_count,
            "created_at": d.created_at.isoformat() + "Z"}


@router.get("")
def list_documents(ctx: AuthContext = Depends(require_roles("admin", "analyst", "viewer")), db: Session = Depends(get_db)):
    if not get_settings().rag_enabled:
        return []
    rows = db.scalars(select(Document).where(Document.user_id == ctx.user.id).order_by(Document.created_at.desc()))
    return [_out(d) for d in rows]


@router.post("", status_code=201)
async def upload(request: Request, file: UploadFile = File(...),
                 ctx: AuthContext = Depends(require_roles("admin", "analyst")), db: Session = Depends(get_db)):
    rag, settings = _rag(), get_settings()
    enforce_rate_limit(f"upload:{ctx.user.id}", 10)
    limit = settings.max_upload_mb * 1024 * 1024
    data = await file.read(limit + 1)  # never read more than the limit
    try:
        doc = rag.ingest(db, ctx.user.id, None, file.filename or "document", data)
    except DocumentError as e:
        raise HTTPException(422, str(e)) from None
    audit(db, "document_upload", ctx.user.id, client_ip(request), document=doc.filename, chunks=doc.chunk_count)
    return _out(doc)


@router.delete("/{doc_id}")
def delete_document(doc_id: str, request: Request, ctx: AuthContext = Depends(require_roles("admin", "analyst")),
                    db: Session = Depends(get_db)):
    rag = _rag()
    if not rag.delete(db, ctx.user.id, doc_id):  # owner-only
        raise HTTPException(404, "Document not found.")
    audit(db, "document_delete", ctx.user.id, client_ip(request), document_id=doc_id)
    return {"ok": True}
