"""Approved tools the model may call.

Tools never accept a domain, connection or SQL. They run against the ``ToolContext`` that the
backend built after authorizing the user for exactly one domain. Add a tool by registering a
``ToolSpec``; domain configs can add parameterized ``reports`` without any code.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy.orm import Session

from .config import Settings
from .models import User
from .query_engine import AggregatePlan, QueryError, QueryPlan, parse_plan
from .rag import RagService
from .runtime import DomainRuntime

log = logging.getLogger("omniagent.tools")


@dataclass
class ToolContext:
    user: User
    runtime: DomainRuntime
    settings: Settings
    db: Session
    rag: RagService | None = None


@dataclass
class ToolResult:
    ok: bool
    model: dict  # what Gemini sees (bounded)
    ui: dict | None = None  # what the browser renders
    step: dict = field(default_factory=dict)  # sanitized trace entry


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    handler: Callable[[ToolContext, dict], ToolResult]
    enabled: Callable[[ToolContext], bool] = lambda ctx: True


# ───────────── JSON-schema fragments (Gemini dialect) ─────────────
S, I = {"type": "STRING"}, {"type": "INTEGER"}
STR_LIST = {"type": "ARRAY", "items": S}
FILTER = {
    "type": "OBJECT",
    "properties": {
        "column": {"type": "STRING", "description": "Column name, or table.column when joins are used."},
        "op": {
            "type": "STRING",
            "enum": ["eq", "ne", "gt", "gte", "lt", "lte", "in", "not_in", "contains", "starts_with",
                     "ends_with", "between", "is_null", "is_not_null"],
        },
        "value": {
            "type": "STRING",
            "description": "Single value. Dates: YYYY-MM-DD or tokens today, yesterday, start_of_week, "
                           "start_of_month, start_of_next_month, start_of_last_month, start_of_year, N_days_ago.",
        },
        "values": {"type": "ARRAY", "items": S, "description": "For in / not_in (any count) and between (exactly 2)."},
    },
    "required": ["column", "op"],
}
ORDER = {
    "type": "OBJECT",
    "properties": {"column": S, "direction": {"type": "STRING", "enum": ["asc", "desc"]}},
    "required": ["column"],
}
SEARCH = {
    "type": "OBJECT",
    "description": "Free-text search across text columns.",
    "properties": {"text": S, "columns": STR_LIST},
    "required": ["text"],
}
FILTERS = {"type": "ARRAY", "items": FILTER}
JOINS = {"type": "ARRAY", "items": S, "description": "Other tables to join via declared relationships."}


def _bounded(result: dict, max_rows: int) -> dict:
    """Limit what is sent back to the model (context stays small; the browser gets the full page)."""
    out = {k: v for k, v in result.items() if k != "rows"}
    out["rows"] = result["rows"][:max_rows]
    if len(result["rows"]) > max_rows:
        out["note"] = f"Only the first {max_rows} of {len(result['rows'])} returned rows are shown to you; the user sees all."
    return out


def _data_result(ctx: ToolContext, name: str, result: dict, title: str | None = None) -> ToolResult:
    ui = {"type": "aggregate" if result["kind"] == "aggregate" else "table", "title": title or result["table"], **result}
    return ToolResult(
        True,
        _bounded(result, ctx.settings.llm_max_rows),
        ui,
        {"tool": name, "table": result["table"], "rows": len(result["rows"]), "total": result.get("total_rows")},
    )


# ───────────── handlers ─────────────
def _query_records(ctx: ToolContext, args: dict) -> ToolResult:
    return _data_result(ctx, "query_records", ctx.runtime.queries.query_records(args))


def _aggregate(ctx: ToolContext, args: dict) -> ToolResult:
    return _data_result(ctx, "aggregate_records", ctx.runtime.queries.aggregate(args))


def _distinct(ctx: ToolContext, args: dict) -> ToolResult:
    table, column = str(args.get("table", "")), str(args.get("column", ""))
    res = ctx.runtime.queries.distinct_values(table, column, args.get("contains"), int(args.get("limit") or 25))
    return ToolResult(True, _bounded(res, 50), None, {"tool": "get_distinct_values", "table": table, "column": column})


def _describe(ctx: ToolContext, args: dict) -> ToolResult:
    res = ctx.runtime.queries.describe_table(str(args.get("table", "")))
    return ToolResult(True, res, None, {"tool": "describe_table", "table": res["table"]})


def _report(ctx: ToolContext, args: dict) -> ToolResult:
    name = str(args.get("report", ""))
    rep = next((r for r in ctx.runtime.config.reports if r.name == name), None)
    if rep is None:
        raise QueryError(f"Unknown report '{name}'.")
    plan = copy.deepcopy(rep.plan)
    plan["filters"] = list(plan.get("filters", [])) + list(args.get("filters") or [])
    q = ctx.runtime.queries
    result = q.query_records(parse_plan(QueryPlan, plan)) if rep.kind == "records" else q.aggregate(parse_plan(AggregatePlan, plan))
    res = _data_result(ctx, "run_report", result, rep.name.replace("_", " "))
    res.step["report"] = name
    return res


def _search_docs(ctx: ToolContext, args: dict) -> ToolResult:
    assert ctx.rag is not None
    query = str(args.get("query", "")).strip()
    if not query:
        raise QueryError("`query` is required.")
    hits = ctx.rag.search(ctx.db, ctx.user.id, query, int(args.get("top_k") or 0) or None)
    model = {
        "passages": [{"source": f"{h['document']} (passage {h['chunk']})", "text": h["text"]} for h in hits],
        "note": "Answer only from these passages and cite the source names." if hits else "No relevant passages found.",
    }
    ui = (
        {"type": "sources", "items": [
            {"document": h["document"], "chunk": h["chunk"], "score": h["score"], "excerpt": h["text"][:240]} for h in hits
        ]} if hits else None
    )
    return ToolResult(True, model, ui, {"tool": "search_documents", "passages": len(hits)})


# ───────────── registry ─────────────
class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def available(self, ctx: ToolContext) -> list[ToolSpec]:
        return [t for t in self._tools.values() if t.enabled(ctx)]

    def declarations(self, ctx: ToolContext) -> list[dict]:
        out = []
        for t in self.available(ctx):
            params = copy.deepcopy(t.parameters)
            if t.name == "run_report":
                params["properties"]["report"]["enum"] = [r.name for r in ctx.runtime.config.reports]
                params["properties"]["report"]["description"] = "; ".join(
                    f"{r.name}: {r.description}" for r in ctx.runtime.config.reports
                )
            out.append({"name": t.name, "description": t.description, "parameters": params})
        return out

    def execute(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        spec = next((t for t in self.available(ctx) if t.name == name), None)
        if spec is None:  # model asked for something that is not approved
            return ToolResult(False, {"error": f"Tool '{name}' is not available."}, None, {"tool": name, "error": "not_available"})
        try:
            return spec.handler(ctx, args or {})
        except QueryError as e:
            return ToolResult(False, {"error": str(e)}, None, {"tool": name, "error": str(e)[:160]})
        except Exception:
            log.exception("tool %s crashed", name)
            return ToolResult(False, {"error": "The tool failed unexpectedly."}, None, {"tool": name, "error": "internal"})


def build_default_registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(ToolSpec(
        "query_records",
        "List rows from a table, optionally joined to related tables, with filters, text search, sorting and "
        "pagination. Use for 'show / list / find' questions. Returns total_rows and a page of rows.",
        {"type": "OBJECT", "properties": {
            "table": S, "joins": JOINS,
            "select": {"type": "ARRAY", "items": S, "description": "Columns to return (default: all permitted)."},
            "filters": FILTERS, "search": SEARCH, "order_by": {"type": "ARRAY", "items": ORDER},
            "limit": I, "offset": I}, "required": ["table"]},
        _query_records))
    r.register(ToolSpec(
        "aggregate_records",
        "Compute count / count_distinct / sum / avg / min / max, optionally grouped (dates can be bucketed by "
        "day / month / year). Use for 'how many', totals, averages, breakdowns and summaries.",
        {"type": "OBJECT", "properties": {
            "table": S, "joins": JOINS,
            "metrics": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "func": {"type": "STRING", "enum": ["count", "count_distinct", "sum", "avg", "min", "max"]},
                "column": S, "alias": S}, "required": ["func"]}},
            "group_by": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
                "column": S, "bucket": {"type": "STRING", "enum": ["none", "day", "month", "year"]}},
                "required": ["column"]}},
            "filters": FILTERS, "search": SEARCH, "order_by": {"type": "ARRAY", "items": ORDER, "description": "Column or metric alias."},
            "limit": I}, "required": ["table", "metrics"]},
        _aggregate))
    r.register(ToolSpec(
        "get_distinct_values",
        "Most common values of a column with counts. Use BEFORE filtering on a category/status/name to learn the "
        "exact stored spelling (e.g. whether 'AI' is stored as 'AI' or 'Artificial Intelligence').",
        {"type": "OBJECT", "properties": {"table": S, "column": S, "contains": S, "limit": I}, "required": ["table", "column"]},
        _distinct))
    r.register(ToolSpec(
        "describe_table", "Columns, types and relationships of one permitted table.",
        {"type": "OBJECT", "properties": {"table": S}, "required": ["table"]}, _describe))
    r.register(ToolSpec(
        "run_report", "Run a predefined report configured for this domain, with optional extra filters.",
        {"type": "OBJECT", "properties": {"report": {"type": "STRING", "enum": ["placeholder"]}, "filters": FILTERS},
         "required": ["report"]},
        _report, enabled=lambda ctx: bool(ctx.runtime.config.reports)))
    r.register(ToolSpec(
        "search_documents",
        "Semantic search over the user's uploaded PDF/DOCX documents. Use for questions about document content.",
        {"type": "OBJECT", "properties": {"query": S, "top_k": I}, "required": ["query"]},
        _search_docs, enabled=lambda ctx: ctx.rag is not None))
    return r


registry = build_default_registry()
