import os
import sys
from datetime import date, datetime
from pathlib import Path

os.environ.update({
    "APP_DB_URL": "sqlite+pysqlite://", "SECRET_KEY": "test-secret", "APP_ENV": "test",
    "ALLOW_REGISTRATION": "true", "DEFAULT_DOMAIN_GRANTS": "shop", "AUTH_ATTEMPTS_PER_MINUTE": "1000",
    "GEMINI_API_KEY": "", "APP_TIMEZONE": "UTC",
})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
import sqlalchemy as sa
from sqlalchemy.pool import StaticPool

from app.config import get_settings
from app.db import SQLiteAdapter, domain_engines, init_app_db
from app.models import Base, DbConnection, Domain
from app.query_engine import QueryEngine
from app.registry import DomainConfig, introspect
from app.runtime import runtimes
from app.security import limiter

SHOP_CONFIG = {
    "key": "shop", "name": "Shop", "description": "Test shop", "database": ":memory:",
    "tables": {
        "categories": {"columns": "*"},
        "products": {"columns": "*"},
        "customers": {"columns": "*", "exclude": ["email"]},
        "orders": {"columns": "*"},
    },
    "reports": [{
        "name": "orders_by_status", "description": "Orders per status", "kind": "aggregate",
        "plan": {"table": "orders", "metrics": [{"func": "count", "alias": "n"}], "group_by": [{"column": "status"}]},
    }],
    "examples": ["How many orders?"],
}


def build_shop_engine():
    eng = sa.create_engine("sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    this_month = date.today().replace(day=2)
    with eng.begin() as c:
        c.exec_driver_sql("CREATE TABLE categories (id INTEGER PRIMARY KEY, name VARCHAR(40))")
        c.exec_driver_sql("CREATE TABLE products (id INTEGER PRIMARY KEY, name VARCHAR(40), price NUMERIC(10,2), stock INTEGER, category_id INTEGER REFERENCES categories(id))")
        c.exec_driver_sql("CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR(40), email VARCHAR(60))")
        c.exec_driver_sql("CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), status VARCHAR(20), total NUMERIC(10,2), placed_at DATETIME)")
        c.exec_driver_sql("CREATE TABLE secrets (id INTEGER PRIMARY KEY, token VARCHAR(40))")
        c.exec_driver_sql("INSERT INTO secrets VALUES (1, 'TOP-SECRET')")
        c.exec_driver_sql("INSERT INTO categories VALUES (1,'Books'),(2,'Toys')")
        c.exec_driver_sql("INSERT INTO products VALUES (1,'Novel',9.5,3,1),(2,'Atlas',20,50,1),(3,'Robot',30,5,2),(4,'Puzzle',12,40,2)")
        c.exec_driver_sql("INSERT INTO customers VALUES (1,'Asha','asha@x.com'),(2,'Ravi','ravi@x.com')")
        rows = [
            (1, 1, "paid", 40, datetime.combine(this_month, datetime.min.time()).replace(hour=12)),
            (2, 2, "cancelled", 15, datetime.combine(this_month, datetime.min.time()).replace(hour=12)),
            (3, 1, "paid", 70, datetime(2020, 1, 15)),
        ]
        for r in rows:
            c.execute(sa.text("INSERT INTO orders VALUES (:a,:b,:c,:d,:e)"), dict(zip("abcde", r)))
    return eng


@pytest.fixture()
def shop():
    eng = build_shop_engine()
    cfg = DomainConfig.model_validate(SHOP_CONFIG)
    schema = introspect(eng, cfg)
    return QueryEngine(eng, schema, SQLiteAdapter(), default_limit=25, max_limit=100), cfg, eng


@pytest.fixture()
def app_client():
    """TestClient with a seeded 'shop' domain backed by SQLite."""
    from fastapi.testclient import TestClient

    get_settings.cache_clear()
    from app.main import create_app

    engine = init_app_db("sqlite+pysqlite://")
    Base.metadata.create_all(engine)
    from app.db import session_factory

    with session_factory()() as db:
        conn = DbConnection(name="shop_db", driver="sqlite", database=":memory:")
        db.add(conn)
        db.flush()
        db.add(Domain(key="shop", name="Shop", description="d", connection_id=conn.id, config_json=SHOP_CONFIG))
        db.add(Domain(key="private", name="Private", description="d", connection_id=conn.id, config_json=SHOP_CONFIG))
        db.commit()
        domain_engines.register(conn.id, build_shop_engine())
    runtimes.invalidate()
    limiter.reset()
    with TestClient(create_app(), base_url="http://localhost:8000") as client:
        yield client
    domain_engines._engines.clear()
