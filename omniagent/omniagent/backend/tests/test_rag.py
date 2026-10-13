import io
import zlib

import pytest

from app.config import get_settings
from app.db import session_factory
from app.models import User
from app.rag import DocumentError, RagService, chunk_text, validate_upload


class HashEmbedder:
    """Deterministic bag-of-words embedder so retrieval is testable offline."""

    def embed(self, texts, task):
        out = []
        for t in texts:
            v = [0.0] * 64
            for w in t.lower().split():
                v[zlib.crc32(w.strip(".,").encode()) % 64] += 1.0
            out.append(v)
        return out


def make_docx(paragraphs):
    from docx import Document

    d = Document()
    for p in paragraphs:
        d.add_paragraph(p)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_validation_rules():
    s = get_settings()
    with pytest.raises(DocumentError):
        validate_upload("notes.txt", b"hello", s)
    with pytest.raises(DocumentError):
        validate_upload("fake.pdf", b"not a pdf", s)  # magic bytes must match
    with pytest.raises(DocumentError):
        validate_upload("big.pdf", b"%PDF-" + b"0" * (s.max_upload_mb * 1024 * 1024), s)
    assert validate_upload("../../etc/passwd.docx", b"PK\x03\x04x", s)[0] == "passwd.docx"


def test_chunking_overlaps_and_bounds():
    chunks = chunk_text("Sentence number one. " * 200, 300, 50)
    assert len(chunks) > 5 and all(len(c) <= 300 for c in chunks)


def test_ingest_search_and_owner_scope(app_client):
    s = get_settings()
    rag = RagService(s, HashEmbedder())
    with session_factory()() as db:
        u1 = User(email="a@x.com", display_name="a", password_hash="x")
        u2 = User(email="b@x.com", display_name="b", password_hash="x")
        db.add_all([u1, u2]); db.commit()
        data = make_docx(["Refund policy: refunds are issued within fourteen days.", "Shipping takes five days."])
        doc = rag.ingest(db, u1.id, None, "policy.docx", data)
        assert doc.chunk_count >= 1
        hits = rag.search(db, u1.id, "refunds fourteen days")
        assert hits and hits[0]["document"] == "policy.docx" and "refund" in hits[0]["text"].lower()
        assert rag.search(db, u2.id, "refunds fourteen days") == []   # other users never see it
        assert not rag.delete(db, u2.id, doc.id)                      # nor delete it
        assert rag.delete(db, u1.id, doc.id)
