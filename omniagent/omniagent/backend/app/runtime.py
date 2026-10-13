"""Per-domain runtime: engine + cached schema + query engine, with refresh support."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from .config import Settings, get_settings
from .db import adapter_for, domain_engines
from .models import Domain
from .query_engine import QueryEngine
from .registry import DomainConfig, DomainSchema, introspect

log = logging.getLogger("omniagent.runtime")


class DomainUnavailable(Exception):
    """The domain database cannot be reached (safe message only)."""


@dataclass
class DomainRuntime:
    domain_key: str
    config: DomainConfig
    schema: DomainSchema
    queries: QueryEngine
    loaded_at: float


class RuntimeManager:
    def __init__(self) -> None:
        self._cache: dict[str, DomainRuntime] = {}
        self._lock = threading.Lock()

    def get(self, domain: Domain, settings: Settings | None = None, refresh: bool = False) -> DomainRuntime:
        settings = settings or get_settings()
        with self._lock:
            rt = self._cache.get(domain.key)
            if rt and not refresh and time.time() - rt.loaded_at < settings.schema_cache_ttl_seconds:
                return rt
            try:
                config = DomainConfig.model_validate(domain.config_json)
                engine = domain_engines.get(domain.connection, settings)
                schema = introspect(engine, config)
            except SQLAlchemyError:
                log.exception("cannot introspect domain %s", domain.key)
                raise DomainUnavailable("The domain database is currently unavailable.") from None
            queries = QueryEngine(
                engine,
                schema,
                adapter_for(domain.connection.driver),
                default_limit=settings.default_row_limit,
                max_limit=min(config.max_rows or settings.max_row_limit, settings.max_row_limit),
                timezone=settings.app_timezone,
            )
            rt = DomainRuntime(domain.key, config, schema, queries, time.time())
            self._cache[domain.key] = rt
            return rt

    def invalidate(self, key: str | None = None) -> None:
        with self._lock:
            if key:
                self._cache.pop(key, None)
            else:
                self._cache.clear()


runtimes = RuntimeManager()
