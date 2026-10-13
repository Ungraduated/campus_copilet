"""Application tables (users, sessions, conversations, audit, documents, domain config)."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

LongText = sa.Text().with_variant(mysql.MEDIUMTEXT(), "mysql")
LongBlob = sa.LargeBinary().with_variant(mysql.MEDIUMBLOB(), "mysql")


def utcnow() -> datetime:
    """Naive UTC timestamp (stored in UTC everywhere)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(sa.String(255), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(sa.String(120))
    password_hash: Mapped[str] = mapped_column(sa.String(255))
    role: Mapped[str] = mapped_column(sa.String(20), default="viewer")  # admin | analyst | viewer
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(sa.String(64), unique=True, index=True)
    csrf_token: Mapped[str] = mapped_column(sa.String(64))
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    ip: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(sa.String(255), default=None)
    user: Mapped[User] = relationship()


class DbConnection(Base):
    """Connection metadata. Holds the *name* of the env var with the password, never the password."""

    __tablename__ = "db_connections"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(sa.String(80), unique=True)
    driver: Mapped[str] = mapped_column(sa.String(20), default="mysql")
    host: Mapped[str | None] = mapped_column(sa.String(255), default=None)
    port: Mapped[int | None] = mapped_column(default=None)
    database: Mapped[str] = mapped_column(sa.String(255))
    username: Mapped[str | None] = mapped_column(sa.String(120), default=None)
    password_env: Mapped[str | None] = mapped_column(sa.String(120), default=None)
    is_active: Mapped[bool] = mapped_column(default=True)


class Domain(Base):
    __tablename__ = "domains"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(sa.String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(sa.String(120))
    description: Mapped[str] = mapped_column(sa.String(500), default="")
    connection_id: Mapped[int] = mapped_column(sa.ForeignKey("db_connections.id"))
    config_json: Mapped[dict] = mapped_column(sa.JSON)
    is_active: Mapped[bool] = mapped_column(default=True)
    connection: Mapped[DbConnection] = relationship()


class UserDomain(Base):
    """Per-user domain permission."""

    __tablename__ = "user_domains"
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    domain_id: Mapped[int] = mapped_column(sa.ForeignKey("domains.id", ondelete="CASCADE"), primary_key=True)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    domain_id: Mapped[int] = mapped_column(sa.ForeignKey("domains.id"))
    title: Mapped[str] = mapped_column(sa.String(200), default="New chat")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    domain: Mapped[Domain] = relationship()
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", order_by="Message.id"
    )


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        sa.ForeignKey("conversations.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(sa.String(12))  # user | assistant
    content: Mapped[str] = mapped_column(LongText)
    data_json: Mapped[list | None] = mapped_column(sa.JSON, default=None)  # result blocks for the UI
    steps_json: Mapped[list | None] = mapped_column(sa.JSON, default=None)  # sanitized tool trace
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(default=None, index=True)
    event_type: Mapped[str] = mapped_column(sa.String(60), index=True)
    detail: Mapped[dict | None] = mapped_column(sa.JSON, default=None)
    ip: Mapped[str | None] = mapped_column(sa.String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(sa.String(36), primary_key=True, default=new_id)
    user_id: Mapped[int] = mapped_column(sa.ForeignKey("users.id", ondelete="CASCADE"), index=True)
    domain_id: Mapped[int | None] = mapped_column(sa.ForeignKey("domains.id"), default=None)
    filename: Mapped[str] = mapped_column(sa.String(255))
    content_type: Mapped[str] = mapped_column(sa.String(100))
    size_bytes: Mapped[int]
    chunk_count: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(sa.ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[int] = mapped_column(index=True)  # denormalised for owner-scoped retrieval
    chunk_index: Mapped[int]
    text: Mapped[str] = mapped_column(LongText)
    embedding: Mapped[bytes] = mapped_column(LongBlob)
