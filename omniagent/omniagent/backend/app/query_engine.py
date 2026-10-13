"""Secure, schema-validated query execution.

The model never supplies SQL. It supplies a *plan* (table, joins, columns, filters, ...), which is
validated by Pydantic and then against the server-side schema registry. Only after every
identifier is verified is a SELECT built with SQLAlchemy Core (all values are bound parameters).
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from .db import DialectAdapter
from .registry import ColumnMeta, DomainSchema, TableMeta

log = logging.getLogger("omniagent.query")

Op = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in",
    "contains", "starts_with", "ends_with", "between", "is_null", "is_not_null",
]
Scalar = str | int | float | bool


class QueryError(Exception):
    """A problem with the request that is safe to show to the model and the user."""


# ───────────── plan models ─────────────
class Filter(BaseModel):
    model_config = {"extra": "forbid"}
    column: str
    op: Op = "eq"
    value: Scalar | None = None
    values: list[Scalar] | None = None


class Order(BaseModel):
    model_config = {"extra": "forbid"}
    column: str
    direction: Literal["asc", "desc"] = "asc"


class Search(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(min_length=1, max_length=100)
    columns: list[str] = Field(default_factory=list)


class QueryPlan(BaseModel):
    model_config = {"extra": "forbid"}
    table: str
    joins: list[str] = Field(default_factory=list, max_length=5)
    select: list[str] = Field(default_factory=list, max_length=40)
    filters: list[Filter] = Field(default_factory=list, max_length=15)
    search: Search | None = None
    order_by: list[Order] = Field(default_factory=list, max_length=5)
    limit: int | None = Field(default=None, ge=1)
    offset: int = Field(default=0, ge=0, le=100_000)


class Metric(BaseModel):
    model_config = {"extra": "forbid"}
    func: Literal["count", "count_distinct", "sum", "avg", "min", "max"]
    column: str | None = None
    alias: str | None = Field(default=None, max_length=40)


class GroupBy(BaseModel):
    model_config = {"extra": "forbid"}
    column: str
    bucket: Literal["none", "day", "month", "year"] = "none"


class AggregatePlan(BaseModel):
    model_config = {"extra": "forbid"}
    table: str
    joins: list[str] = Field(default_factory=list, max_length=5)
    metrics: list[Metric] = Field(min_length=1, max_length=6)
    group_by: list[GroupBy] = Field(default_factory=list, max_length=3)
    filters: list[Filter] = Field(default_factory=list, max_length=15)
    search: Search | None = None
    order_by: list[Order] = Field(default_factory=list, max_length=3)
    limit: int | None = Field(default=None, ge=1)


def parse_plan(model: type[BaseModel], args: dict) -> Any:
    try:
        return model.model_validate(args)
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:5])
        raise QueryError(f"Invalid arguments: {problems}") from None


# ───────────── value coercion ─────────────
_DAYS_AGO = re.compile(r"^(\d{1,4})_days_ago$")
_ALIAS = re.compile(r"[^A-Za-z0-9_]")


def resolve_date_token(raw: str, today: date) -> date | None:
    t = raw.strip().lower()
    if t == "today":
        return today
    if t == "yesterday":
        return today - timedelta(days=1)
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t == "start_of_week":
        return today - timedelta(days=today.weekday())
    if t == "start_of_month":
        return today.replace(day=1)
    if t == "start_of_next_month":
        return (today.replace(day=28) + timedelta(days=4)).replace(day=1)
    if t == "start_of_last_month":
        return (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    if t == "start_of_year":
        return today.replace(month=1, day=1)
    m = _DAYS_AGO.match(t)
    if m:
        return today - timedelta(days=int(m.group(1)))
    return None


def coerce(value: Any, col: ColumnMeta, today: date) -> Any:
    k = col.kind
    bad = QueryError(f"Value {value!r} is not valid for column '{col.name}' (type {k}).")
    try:
        if k == "integer":
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, float):
                if value != int(value):
                    raise bad
                return int(value)
            sv = str(value).strip().lower()
            if sv in ("true", "yes"):  # TINYINT(1) flag columns
                return 1
            if sv in ("false", "no"):
                return 0
            return int(sv)
        if k == "number":
            if isinstance(value, bool):
                raise bad
            return Decimal(str(value).strip())
        if k == "boolean":
            if isinstance(value, bool):
                return value
            s = str(value).strip().lower()
            if s in ("true", "1", "yes"):
                return True
            if s in ("false", "0", "no"):
                return False
            raise bad
        if k in ("date", "datetime"):
            s = str(value).strip()
            d = resolve_date_token(s, today)
            if d is not None:
                return d if k == "date" else datetime.combine(d, time.min)
            if k == "date":
                return date.fromisoformat(s[:10])
            return datetime.fromisoformat(s) if len(s) > 10 else datetime.combine(date.fromisoformat(s), time.min)
        if k == "time":
            return time.fromisoformat(str(value).strip())
        if k == "string":
            return str(value)
    except QueryError:
        raise
    except (ValueError, InvalidOperation):
        raise bad from None
    raise bad


def to_json(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(round(v, 4))
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return None
    return str(v)


# ───────────── engine ─────────────
class _Scope:
    """The tables participating in one query, plus the FROM clause joining them."""

    def __init__(self, base: TableMeta):
        self.base = base
        self.tables: dict[str, TableMeta] = {base.name: base}
        self.source: sa.FromClause = base.table


class QueryEngine:
    def __init__(
        self,
        engine: Engine,
        schema: DomainSchema,
        adapter: DialectAdapter,
        *,
        default_limit: int = 25,
        max_limit: int = 200,
        timezone: str = "UTC",
    ) -> None:
        self.engine = engine
        self.schema = schema
        self.adapter = adapter
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.tz = timezone

    # ── helpers ──
    def today(self) -> date:
        try:
            return datetime.now(ZoneInfo(self.tz)).date()
        except Exception:
            return datetime.utcnow().date()

    def _table(self, name: str) -> TableMeta:
        t = self.schema.tables.get(name)
        if t is None:
            raise QueryError(f"Unknown table '{name}'. Available tables: {', '.join(self.schema.tables)}.")
        return t

    def _scope(self, table: str, joins: list[str]) -> _Scope:
        scope = _Scope(self._table(table))
        for j in joins:
            if j in scope.tables:
                continue
            jt = self._table(j)
            on = None
            for fk in self.schema.foreign_keys:
                if fk.from_table == j and fk.to_table in scope.tables:
                    other = scope.tables[fk.to_table].table
                    on = sa.and_(*[jt.table.c[a] == other.c[b] for a, b in zip(fk.from_columns, fk.to_columns)])
                elif fk.to_table == j and fk.from_table in scope.tables:
                    other = scope.tables[fk.from_table].table
                    on = sa.and_(*[other.c[a] == jt.table.c[b] for a, b in zip(fk.from_columns, fk.to_columns)])
                if on is not None:
                    break
            if on is None:
                raise QueryError(
                    f"No declared relationship connects '{j}' to {', '.join(scope.tables)}. "
                    "List intermediate tables in `joins` in order."
                )
            scope.source = scope.source.join(jt.table, on)
            scope.tables[j] = jt
        return scope

    def _col(self, ref: str, scope: _Scope, purpose: str = "use") -> tuple[sa.ColumnElement, ColumnMeta, str]:
        ref = ref.strip()
        if "." in ref:
            tname, cname = ref.split(".", 1)
            if tname not in scope.tables:
                raise QueryError(f"Table '{tname}' is not part of this query; add it to `joins`.")
            candidates = [(scope.tables[tname], cname)]
        else:
            candidates = [(t, ref) for t in scope.tables.values() if ref in t.columns]
            if len(candidates) > 1:
                names = ", ".join(f"{t.name}.{ref}" for t, _ in candidates)
                raise QueryError(f"Column '{ref}' is ambiguous. Use a qualified name: {names}.")
            if not candidates:
                raise QueryError(
                    f"Unknown column '{ref}'. Columns in this query: "
                    + "; ".join(f"{t.name}({', '.join(t.columns)})" for t in scope.tables.values())
                )
        tmeta, cname = candidates[0]
        cmeta = tmeta.columns.get(cname)
        if cmeta is None:
            raise QueryError(f"Column '{tmeta.name}.{cname}' does not exist or is not permitted.")
        if not (cmeta.selectable and cmeta.filterable):
            raise QueryError(f"Column '{tmeta.name}.{cname}' has an unsupported type and cannot be {purpose}d.")
        return tmeta.table.c[cname], cmeta, f"{tmeta.name}.{cname}"

    def _limit(self, requested: int | None, default: int | None = None) -> int:
        return max(1, min(requested or default or self.default_limit, self.max_limit))

    # ── filters / search ──
    def _where(self, plan: QueryPlan | AggregatePlan, scope: _Scope) -> list[sa.ColumnElement]:
        today = self.today()
        clauses: list[sa.ColumnElement] = []
        for f in plan.filters:
            col, meta, _ = self._col(f.column, scope, "filter")
            op = f.op
            if op == "is_null":
                clauses.append(col.is_(None))
                continue
            if op == "is_not_null":
                clauses.append(col.is_not(None))
                continue
            if op in ("in", "not_in"):
                vals = f.values if f.values else ([f.value] if f.value is not None else [])
                if not vals or len(vals) > 100:
                    raise QueryError(f"Filter on '{f.column}' with '{op}' needs 1-100 `values`.")
                conv = [coerce(v, meta, today) for v in vals]
                clauses.append(col.in_(conv) if op == "in" else col.not_in(conv))
                continue
            if op == "between":
                vals = f.values or []
                if len(vals) != 2:
                    raise QueryError(f"Filter on '{f.column}' with 'between' needs exactly 2 `values`.")
                clauses.append(col.between(coerce(vals[0], meta, today), coerce(vals[1], meta, today)))
                continue
            if f.value is None:
                raise QueryError(f"Filter on '{f.column}' with '{op}' needs a `value`.")
            if op in ("contains", "starts_with", "ends_with"):
                if meta.kind != "string":
                    raise QueryError(f"'{op}' only works on text columns; '{f.column}' is {meta.kind}.")
                text = str(f.value)
                clauses.append(
                    {"contains": col.contains, "starts_with": col.startswith, "ends_with": col.endswith}[op](
                        text, autoescape=True
                    )
                )
                continue
            v = coerce(f.value, meta, today)
            clauses.append(
                {"eq": col == v, "ne": col != v, "gt": col > v, "gte": col >= v, "lt": col < v, "lte": col <= v}[op]
            )
        if plan.search:
            cols: list[sa.ColumnElement] = []
            if plan.search.columns:
                for ref in plan.search.columns[:10]:
                    c, m, _ = self._col(ref, scope, "search")
                    if m.kind != "string":
                        raise QueryError(f"Cannot text-search non-text column '{ref}'.")
                    cols.append(c)
            else:
                cols = [scope.base.table.c[c.name] for c in scope.base.columns.values() if c.kind == "string"][:10]
            if not cols:
                raise QueryError("No text columns available to search.")
            clauses.append(sa.or_(*[c.contains(plan.search.text, autoescape=True) for c in cols]))
        return clauses

    # ── execution ──
    def _run(self, stmt: sa.Select, count_stmt: sa.Select | None = None) -> tuple[list[Any], int | None]:
        try:
            with self.engine.connect() as conn:
                total = conn.execute(count_stmt).scalar_one() if count_stmt is not None else None
                rows = conn.execute(stmt).all()
            return rows, total
        except SQLAlchemyError as e:
            log.warning("domain query failed: %s", getattr(e, "orig", e))
            raise QueryError("The database could not complete this query (it may have timed out). Try narrowing it.") from None

    def query_records(self, args: dict | QueryPlan) -> dict:
        plan = args if isinstance(args, QueryPlan) else parse_plan(QueryPlan, args)
        scope = self._scope(plan.table, plan.joins)
        wanted = plan.select or [c.name for c in scope.base.columns.values() if c.selectable]
        cols = []
        seen: set[str] = set()
        for ref in wanted:
            col, meta, qual = self._col(ref, scope, "select")
            if qual in seen:
                continue
            seen.add(qual)
            cols.append((col, meta, qual))
        if not cols:
            raise QueryError("No selectable columns.")
        names = [q.split(".", 1)[1] for _, _, q in cols]
        labels = [q if names.count(q.split(".", 1)[1]) > 1 else q.split(".", 1)[1] for _, _, q in cols]
        where = self._where(plan, scope)
        stmt = sa.select(*[c.label(lbl) for (c, _, _), lbl in zip(cols, labels)]).select_from(scope.source)
        count_stmt = sa.select(sa.func.count()).select_from(scope.source)
        for w in where:
            stmt, count_stmt = stmt.where(w), count_stmt.where(w)
        for o in plan.order_by:
            oc, _, _ = self._col(o.column, scope, "order")
            stmt = stmt.order_by(oc.desc() if o.direction == "desc" else oc.asc())
        if not plan.order_by and scope.base.primary_key:
            pk = scope.base.primary_key[0]
            stmt = stmt.order_by(scope.base.table.c[pk])  # stable pagination
        limit = self._limit(plan.limit)
        rows, total = self._run(stmt.limit(limit).offset(plan.offset), count_stmt)
        return {
            "kind": "records",
            "table": plan.table,
            "columns": [{"name": lbl, "type": m.kind} for (_, m, _), lbl in zip(cols, labels)],
            "rows": [[to_json(v) for v in r] for r in rows],
            "total_rows": total,
            "limit": limit,
            "offset": plan.offset,
            "has_more": (plan.offset + len(rows)) < (total or 0),
        }

    def aggregate(self, args: dict | AggregatePlan) -> dict:
        plan = args if isinstance(args, AggregatePlan) else parse_plan(AggregatePlan, args)
        scope = self._scope(plan.table, plan.joins)
        out_cols: list[sa.ColumnElement] = []
        info: list[dict] = []
        label_map: dict[str, sa.ColumnElement] = {}
        group_exprs: list[sa.ColumnElement] = []

        for g in plan.group_by:
            col, meta, qual = self._col(g.column, scope, "group")
            name = qual.split(".", 1)[1]
            if g.bucket != "none":
                if meta.kind not in ("date", "datetime"):
                    raise QueryError(f"`bucket` only applies to date columns; '{g.column}' is {meta.kind}.")
                expr = self.adapter.date_bucket(col, g.bucket)
                name = f"{name}_{g.bucket}"
            else:
                expr = col
            if name in label_map:
                name = qual.replace(".", "_")
            labeled = expr.label(name)
            out_cols.append(labeled)
            group_exprs.append(expr)
            label_map[name] = expr
            info.append({"name": name, "type": "string" if g.bucket != "none" else meta.kind, "role": "group"})

        for m in plan.metrics:
            if m.func == "count" and not m.column:
                expr, mtype, base = sa.func.count(), "integer", "count"
            else:
                if not m.column:
                    raise QueryError(f"Metric '{m.func}' needs a `column`.")
                col, meta, qual = self._col(m.column, scope, "aggregate")
                base = f"{m.func}_{qual.split('.', 1)[1]}"
                if m.func in ("sum", "avg") and meta.kind not in ("integer", "number"):
                    raise QueryError(f"'{m.func}' needs a numeric column; '{m.column}' is {meta.kind}.")
                if m.func in ("min", "max") and meta.kind == "boolean":
                    raise QueryError(f"'{m.func}' is not supported on boolean column '{m.column}'.")
                expr = {
                    "count": sa.func.count(col),
                    "count_distinct": sa.func.count(sa.distinct(col)),
                    "sum": sa.func.sum(col),
                    "avg": sa.func.avg(col),
                    "min": sa.func.min(col),
                    "max": sa.func.max(col),
                }[m.func]
                mtype = "integer" if m.func in ("count", "count_distinct") else ("number" if m.func in ("sum", "avg") else meta.kind)
            name = _ALIAS.sub("_", m.alias or base)[:40] or "value"
            if not re.match(r"[A-Za-z_]", name):
                name = f"m_{name}"
            while name in label_map:
                name += "_"
            out_cols.append(expr.label(name))
            label_map[name] = expr
            info.append({"name": name, "type": mtype, "role": "metric"})

        stmt = sa.select(*out_cols).select_from(scope.source)
        for w in self._where(plan, scope):
            stmt = stmt.where(w)
        if group_exprs:
            stmt = stmt.group_by(*group_exprs)
        for o in plan.order_by:
            target = label_map.get(o.column)
            if target is None:
                target = self._col(o.column, scope, "order")[0]
            stmt = stmt.order_by(target.desc() if o.direction == "desc" else target.asc())
        if group_exprs and not plan.order_by:
            stmt = stmt.order_by(*group_exprs)
        limit = self._limit(plan.limit, 50)
        rows, _ = self._run(stmt.limit(limit + 1))
        has_more = len(rows) > limit
        rows = rows[:limit]
        return {
            "kind": "aggregate",
            "table": plan.table,
            "columns": [{"name": i["name"], "type": i["type"]} for i in info],
            "group_by": [i["name"] for i in info if i["role"] == "group"],
            "metrics": [i["name"] for i in info if i["role"] == "metric"],
            "rows": [[to_json(v) for v in r] for r in rows],
            "total_rows": len(rows) if not has_more else None,
            "limit": limit,
            "has_more": has_more,
        }

    def distinct_values(self, table: str, column: str, contains: str | None = None, limit: int = 25) -> dict:
        """Most common values of a column - lets the model map 'AI branch' to the stored spelling."""
        agg = AggregatePlan(
            table=table,
            metrics=[Metric(func="count", alias="occurrences")],
            group_by=[GroupBy(column=column)],
            filters=[Filter(column=column, op="contains", value=contains)] if contains else [],
            order_by=[Order(column="occurrences", direction="desc")],
            limit=min(max(limit, 1), 50),
        )
        return self.aggregate(agg)

    def describe_table(self, table: str) -> dict:
        t = self._table(table)
        rel = [
            f"{fk.from_table}.{','.join(fk.from_columns)} -> {fk.to_table}.{','.join(fk.to_columns)}"
            for fk in self.schema.foreign_keys
            if table in (fk.from_table, fk.to_table)
        ]
        return {
            "table": t.name,
            "description": t.description,
            "primary_key": t.primary_key,
            "columns": [
                {"name": c.name, "type": c.kind, "description": c.description} for c in t.columns.values()
            ],
            "relationships": rel,
        }
