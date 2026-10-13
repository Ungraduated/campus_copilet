"""Domain configuration and the schema registry.

The registry is the server-side source of truth for *what may be touched*: only tables listed in
the domain config, and only their permitted columns. Everything the AI asks for is validated
against it. Metadata comes from live introspection of the database and is cached with refresh.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy.engine import Engine

log = logging.getLogger("omniagent.registry")


# ───────────── domain configuration (JSON files → domains.config_json) ─────────────
class TableConfig(BaseModel):
    description: str = ""
    columns: list[str] | Literal["*"] = "*"
    exclude: list[str] = Field(default_factory=list)  # e.g. emails, phone numbers
    column_descriptions: dict[str, str] = Field(default_factory=dict)


class ReportConfig(BaseModel):
    name: str
    description: str
    kind: Literal["records", "aggregate"]
    plan: dict


class DomainConfig(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,48}$")
    name: str
    description: str = ""
    database: str
    hints: str = ""
    tables: dict[str, TableConfig]
    reports: list[ReportConfig] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    max_rows: int | None = None


# ───────────── introspected metadata ─────────────
@dataclass
class ColumnMeta:
    name: str
    kind: str  # integer | number | boolean | date | datetime | time | string | unsupported
    db_type: str
    nullable: bool
    primary_key: bool
    description: str = ""

    @property
    def selectable(self) -> bool:
        return self.kind != "unsupported"

    @property
    def filterable(self) -> bool:
        return self.kind != "unsupported"


@dataclass
class TableMeta:
    name: str
    description: str
    columns: dict[str, ColumnMeta]
    primary_key: list[str]
    table: sa.Table = field(repr=False)  # includes ALL columns (join keys); access is gated by `columns`


@dataclass
class ForeignKeyMeta:
    from_table: str
    from_columns: list[str]
    to_table: str
    to_columns: list[str]


@dataclass
class DomainSchema:
    domain_key: str
    tables: dict[str, TableMeta]
    foreign_keys: list[ForeignKeyMeta]
    loaded_at: float = field(default_factory=time.time)

    def column_count(self) -> int:
        return sum(len(t.columns) for t in self.tables.values())

    def to_prompt(self, detailed: bool = True) -> str:
        lines: list[str] = []
        for t in self.tables.values():
            head = f"- {t.name}" + (f": {t.description}" if t.description else "")
            lines.append(head)
            if detailed:
                for c in t.columns.values():
                    flags = " (primary key)" if c.primary_key else ""
                    desc = f" — {c.description}" if c.description else ""
                    lines.append(f"    {c.name} [{c.kind}]{flags}{desc}")
        if self.foreign_keys:
            lines.append("Relationships (usable via `joins`):")
            for fk in self.foreign_keys:
                lines.append(
                    f"- {fk.from_table}.{', '.join(fk.from_columns)} -> {fk.to_table}.{', '.join(fk.to_columns)}"
                )
        return "\n".join(lines)

    def to_public(self) -> dict:
        return {
            "domain": self.domain_key,
            "tables": [
                {
                    "name": t.name,
                    "description": t.description,
                    "primary_key": t.primary_key,
                    "columns": [{"name": c.name, "type": c.kind, "nullable": c.nullable} for c in t.columns.values()],
                }
                for t in self.tables.values()
            ],
            "relationships": [
                {"from": f"{fk.from_table}.{','.join(fk.from_columns)}", "to": f"{fk.to_table}.{','.join(fk.to_columns)}"}
                for fk in self.foreign_keys
            ],
        }


def classify(sa_type) -> str:
    if isinstance(sa_type, sa.Boolean):
        return "boolean"
    if isinstance(sa_type, sa.Integer):
        return "integer"
    if isinstance(sa_type, (sa.Numeric, sa.Float)):
        return "number"
    if isinstance(sa_type, sa.DateTime):
        return "datetime"
    if isinstance(sa_type, sa.Date):
        return "date"
    if isinstance(sa_type, sa.Time):
        return "time"
    if isinstance(sa_type, (sa.String, sa.Text, sa.Enum)):
        return "string"
    return "unsupported"  # BLOB, JSON, geometry, bit, ...


def introspect(engine: Engine, config: DomainConfig) -> DomainSchema:
    insp = sa.inspect(engine)
    existing = set(insp.get_table_names())
    meta = sa.MetaData()
    tables: dict[str, TableMeta] = {}

    for tname, tcfg in config.tables.items():
        if tname not in existing:
            log.warning("Configured table %s.%s does not exist; skipping", config.key, tname)
            continue
        raw_cols = insp.get_columns(tname)
        pk = list(insp.get_pk_constraint(tname).get("constrained_columns") or [])
        all_names = [c["name"] for c in raw_cols]
        permitted = all_names if tcfg.columns == "*" else [c for c in tcfg.columns if c in all_names]
        permitted = [c for c in permitted if c not in set(tcfg.exclude)]
        cols: dict[str, ColumnMeta] = {}
        for c in raw_cols:
            if c["name"] in permitted:
                cols[c["name"]] = ColumnMeta(
                    name=c["name"],
                    kind=classify(c["type"]),
                    db_type=str(c["type"]),
                    nullable=bool(c.get("nullable", True)),
                    primary_key=c["name"] in pk,
                    description=tcfg.column_descriptions.get(c["name"], ""),
                )
        sa_table = sa.Table(tname, meta, *[sa.Column(c["name"], c["type"]) for c in raw_cols])
        tables[tname] = TableMeta(tname, tcfg.description, cols, pk, sa_table)

    fks: list[ForeignKeyMeta] = []
    for tname in tables:
        for fk in insp.get_foreign_keys(tname):
            ref = fk.get("referred_table")
            if ref in tables and fk.get("constrained_columns"):
                fks.append(ForeignKeyMeta(tname, list(fk["constrained_columns"]), ref, list(fk["referred_columns"])))
    return DomainSchema(config.key, tables, fks)
