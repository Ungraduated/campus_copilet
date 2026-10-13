"""Database connectivity.

* The *application* database stores users, sessions, conversations, audit events, ...
* *Domain* databases are the business databases the AI may read. They are opened with a
  read-only account, using connection metadata stored in ``db_connections``. Passwords are
  never stored in the database: ``password_env`` names an environment variable.

A small DialectAdapter abstraction keeps MySQL specifics in one place so PostgreSQL (or others)
can be added later by writing one more adapter and one entry in ``DRIVER_URLS``.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Iterator

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import Settings, get_settings

log = logging.getLogger("omniagent.db")

# ─────────────────────────── application database ───────────────────────────
_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def app_database_url(settings: Settings) -> str | URL:
    if settings.app_db_url:
        return settings.app_db_url
    return URL.create(
        "mysql+pymysql",
        username=settings.app_db_user,
        password=settings.app_db_password.get_secret_value(),
        host=settings.app_db_host,
        port=settings.app_db_port,
        database=settings.app_db_name,
        query={"charset": "utf8mb4"},
    )


def init_app_db(url: str | URL | None = None) -> Engine:
    """Create (or recreate) the application engine."""
    global _engine, _SessionLocal
    settings = get_settings()
    url = url or app_database_url(settings)
    kwargs: dict = {}
    if str(url).startswith("sqlite"):
        kwargs = {"connect_args": {"check_same_thread": False}, "poolclass": StaticPool}
    else:
        kwargs = {"pool_pre_ping": True, "pool_recycle": 1800, "pool_size": 10, "max_overflow": 10}
    _engine = sa.create_engine(url, **kwargs)
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        init_app_db()
    assert _engine is not None
    return _engine


def session_factory() -> sessionmaker[Session]:
    if _SessionLocal is None:
        init_app_db()
    assert _SessionLocal is not None
    return _SessionLocal


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()


# ─────────────────────────── dialect adapters ───────────────────────────
class DialectAdapter:
    name = "generic"

    def on_connect(self, dbapi_connection, timeout_ms: int) -> None:  # pragma: no cover
        """Harden a fresh DBAPI connection (read-only, statement timeout)."""

    def date_bucket(self, column, bucket: str):
        raise NotImplementedError


class MySQLAdapter(DialectAdapter):
    name = "mysql"
    _FORMATS = {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}

    def on_connect(self, dbapi_connection, timeout_ms: int) -> None:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME={int(timeout_ms)}")
        finally:
            cur.close()

    def date_bucket(self, column, bucket: str):
        return sa.func.date_format(column, self._FORMATS[bucket])


class SQLiteAdapter(DialectAdapter):
    """Used by the automated tests; also handy for quick local experiments."""

    name = "sqlite"
    _FORMATS = {"day": "%Y-%m-%d", "month": "%Y-%m", "year": "%Y"}

    def on_connect(self, dbapi_connection, timeout_ms: int) -> None:
        cur = dbapi_connection.cursor()
        try:
            cur.execute("PRAGMA query_only = ON")
        finally:
            cur.close()

    def date_bucket(self, column, bucket: str):
        return sa.func.strftime(self._FORMATS[bucket], column)


ADAPTERS: dict[str, DialectAdapter] = {"mysql": MySQLAdapter(), "sqlite": SQLiteAdapter()}
DRIVER_URLS = {"mysql": "mysql+pymysql", "sqlite": "sqlite+pysqlite"}


def adapter_for(driver: str) -> DialectAdapter:
    if driver not in ADAPTERS:
        raise ValueError(f"Unsupported database driver: {driver}")
    return ADAPTERS[driver]


class DomainEngines:
    """Builds and caches read-only engines from ``db_connections`` rows."""

    def __init__(self) -> None:
        self._engines: dict[int, Engine] = {}
        self._lock = threading.Lock()

    def register(self, connection_id: int, engine: Engine) -> None:  # used by tests
        self._engines[connection_id] = engine

    def get(self, conn, settings: Settings | None = None) -> Engine:
        settings = settings or get_settings()
        with self._lock:
            if conn.id in self._engines:
                return self._engines[conn.id]
            adapter = adapter_for(conn.driver)
            password = os.environ.get(conn.password_env or "", "") if conn.password_env else ""
            url = URL.create(
                DRIVER_URLS[conn.driver],
                username=conn.username or None,
                password=password or None,
                host=conn.host or None,
                port=conn.port or None,
                database=conn.database,
                query={"charset": "utf8mb4"} if conn.driver == "mysql" else {},
            )
            connect_args: dict = {}
            if conn.driver == "mysql":
                connect_args = {"connect_timeout": 5, "read_timeout": max(2, settings.query_timeout_ms // 1000 + 2)}
            engine = sa.create_engine(
                url, pool_pre_ping=True, pool_recycle=1800, pool_size=5, max_overflow=5, connect_args=connect_args
            )
            timeout_ms = settings.query_timeout_ms

            @event.listens_for(engine, "connect")
            def _harden(dbapi_conn, _record):  # noqa: ANN001
                adapter.on_connect(dbapi_conn, timeout_ms)

            self._engines[conn.id] = engine
            return engine

    def dispose(self, connection_id: int) -> None:
        with self._lock:
            eng = self._engines.pop(connection_id, None)
        if eng is not None:
            eng.dispose()


domain_engines = DomainEngines()
