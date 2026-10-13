"""One-time setup CLI.

    python -m scripts.setup all            # everything below, in order
    python -m scripts.setup init-db        # create MySQL databases + least-privilege users
    python -m scripts.setup create-tables  # application tables
    python -m scripts.setup seed-demo      # (re)create demo domain schemas + sample data
    python -m scripts.setup sync-domains   # load domains/*.json into the application database
    python -m scripts.setup create-admin   # explicit admin account (password from env or prompt)

Admin MySQL credentials (MYSQL_ADMIN_*) are used only here, never by the running server.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.engine import URL

from app.config import DOMAINS_DIR, SQL_DIR, get_settings
from app.db import init_app_db, session_factory
from app.models import Base, DbConnection, Domain, User, UserDomain
from app.registry import DomainConfig
from app.security import hash_password, password_problem
from scripts.seed_data import SEEDERS

IDENT = re.compile(r"^[A-Za-z0-9_]{1,64}$")


def load_configs() -> list[DomainConfig]:
    return [DomainConfig.model_validate(json.loads(p.read_text())) for p in sorted(DOMAINS_DIR.glob("*.json"))]


def admin_engine(database: str | None = None) -> sa.Engine:
    s = get_settings()
    pw = os.environ.get("MYSQL_ADMIN_PASSWORD")
    if pw is None:
        pw = getpass.getpass("MySQL admin password: ")
    url = URL.create(
        "mysql+pymysql", username=os.environ.get("MYSQL_ADMIN_USER", "root"), password=pw,
        host=s.domain_db_host, port=s.domain_db_port, database=database, query={"charset": "utf8mb4"},
    )
    return sa.create_engine(url, isolation_level="AUTOCOMMIT")


def split_sql(text: str) -> list[str]:
    lines = [ln for ln in text.splitlines() if not ln.strip().startswith("--")]
    return [st.strip() for st in "\n".join(lines).split(";") if st.strip()]


def init_db() -> None:
    s = get_settings()
    host = os.environ.get("MYSQL_USER_HOST", "localhost")
    ro_pw = os.environ.get("DOMAIN_DB_READONLY_PASSWORD", "")
    app_pw = s.app_db_password.get_secret_value()
    if not ro_pw or not app_pw:
        sys.exit("Set APP_DB_PASSWORD and DOMAIN_DB_READONLY_PASSWORD in .env first.")
    domain_dbs = [c.database for c in load_configs()]
    for ident in [s.app_db_name, s.app_db_user, s.domain_db_readonly_user, *domain_dbs]:
        if not IDENT.match(ident):
            sys.exit(f"Unsafe identifier in configuration: {ident!r}")
    eng = admin_engine()
    with eng.connect() as c:
        for db in [s.app_db_name, *domain_dbs]:
            c.execute(sa.text(f"CREATE DATABASE IF NOT EXISTS `{db}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"))
        c.execute(sa.text(f"CREATE USER IF NOT EXISTS '{s.app_db_user}'@'{host}' IDENTIFIED BY :p"), {"p": app_pw})
        c.execute(sa.text(f"ALTER USER '{s.app_db_user}'@'{host}' IDENTIFIED BY :p"), {"p": app_pw})
        c.execute(sa.text(f"GRANT ALL PRIVILEGES ON `{s.app_db_name}`.* TO '{s.app_db_user}'@'{host}'"))
        c.execute(sa.text(f"CREATE USER IF NOT EXISTS '{s.domain_db_readonly_user}'@'{host}' IDENTIFIED BY :p"), {"p": ro_pw})
        c.execute(sa.text(f"ALTER USER '{s.domain_db_readonly_user}'@'{host}' IDENTIFIED BY :p"), {"p": ro_pw})
        for db in domain_dbs:  # SELECT only: the AI path can never modify data
            c.execute(sa.text(f"GRANT SELECT ON `{db}`.* TO '{s.domain_db_readonly_user}'@'{host}'"))
        c.execute(sa.text("FLUSH PRIVILEGES"))
    print("✓ databases and users ready")


def create_tables() -> None:
    init_app_db()
    from app.db import get_engine

    Base.metadata.create_all(get_engine())
    print("✓ application tables ready")


def seed_demo() -> None:
    s = get_settings()
    today = datetime.now(ZoneInfo(s.app_timezone)).date()
    for cfg in load_configs():
        eng = admin_engine(cfg.database)
        with eng.connect() as conn:
            for stmt in split_sql((SQL_DIR / f"{cfg.key}.sql").read_text()):
                conn.exec_driver_sql(stmt)
            conn.exec_driver_sql("SET FOREIGN_KEY_CHECKS = 1")
        with eng.begin() as conn:
            SEEDERS[cfg.key](conn, today)
        print(f"✓ seeded {cfg.key}")


def sync_domains() -> None:
    s = get_settings()
    init_app_db()
    with session_factory()() as db:
        for cfg in load_configs():
            conn = db.scalar(select(DbConnection).where(DbConnection.name == f"{cfg.key}_db"))
            if conn is None:
                conn = DbConnection(name=f"{cfg.key}_db")
                db.add(conn)
            conn.driver, conn.host, conn.port, conn.database = "mysql", s.domain_db_host, s.domain_db_port, cfg.database
            conn.username, conn.password_env = s.domain_db_readonly_user, "DOMAIN_DB_READONLY_PASSWORD"
            db.flush()
            dom = db.scalar(select(Domain).where(Domain.key == cfg.key))
            if dom is None:
                dom = Domain(key=cfg.key, connection_id=conn.id, name=cfg.name, config_json={})
                db.add(dom)
            dom.name, dom.description, dom.connection_id = cfg.name, cfg.description, conn.id
            dom.config_json = cfg.model_dump()
        db.commit()
    print("✓ domains synced")


def create_admin() -> None:
    init_app_db()
    email = (os.environ.get("ADMIN_EMAIL") or input("Admin email: ")).strip().lower()
    password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password (min 10 chars): ")
    problem = password_problem(password)
    if problem:
        sys.exit(problem)
    with session_factory()() as db:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(email=email, display_name="Administrator", password_hash=hash_password(password), role="admin")
            db.add(user)
        else:
            user.role, user.is_active, user.password_hash = "admin", True, hash_password(password)
        db.flush()
        for dom in db.scalars(select(Domain)):
            if not db.get(UserDomain, (user.id, dom.id)):
                db.add(UserDomain(user_id=user.id, domain_id=dom.id))
        db.commit()
    print(f"✓ admin account ready: {email}")


COMMANDS = {
    "init-db": init_db, "create-tables": create_tables, "seed-demo": seed_demo,
    "sync-domains": sync_domains, "create-admin": create_admin,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["all", *COMMANDS])
    cmd = ap.parse_args().command
    for name in (COMMANDS if cmd == "all" else [cmd]):
        COMMANDS[name]()


if __name__ == "__main__":
    main()
