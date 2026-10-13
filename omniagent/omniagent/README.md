# OmniAgent 2.0 — Universal AI Database Agent

Ask relational databases questions in plain language. Google Gemini picks from a small set of **approved, validated tools**; the backend checks every request, runs real read-only MySQL queries, and Gemini explains the actual results. One FastAPI server serves both the JSON API and the frontend.

```
Browser (HTML/CSS/vanilla JS, Chart.js)
   │  fetch + HttpOnly session cookie + CSRF header
   ▼
FastAPI ── auth / RBAC / rate limits / audit
   │
   ├─ Agent loop (Gemini function calling, max N tool calls, timeouts)
   │      │ tool call (table, filters, joins … — never SQL)
   │      ▼
   │   Tool registry ──► Pydantic validation ──► Schema registry check ──► SQLAlchemy Core (bound params)
   │                                                                          │ read-only MySQL account
   ├─ App DB (MySQL): users, sessions, conversations, messages, domains, connections, audit, documents
   └─ Domain DBs (MySQL): students · ecommerce · helpdesk  (add your own with one JSON file)
```

## Quick start

Requirements: Python 3.11+, MySQL 8+, a Gemini API key from <https://aistudio.google.com/apikey>.

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
#   edit .env: SECRET_KEY, GEMINI_API_KEY, APP_DB_PASSWORD, DOMAIN_DB_READONLY_PASSWORD,
#   MYSQL_ADMIN_PASSWORD (one-time setup only), ADMIN_EMAIL / ADMIN_PASSWORD

python -m scripts.setup all      # creates DBs + least-privilege users, tables, sample data, admin
uvicorn app.main:app --reload    # open http://localhost:8000
```

`scripts.setup` sub-commands (`init-db`, `create-tables`, `seed-demo`, `sync-domains`, `create-admin`) can be run individually. `seed-demo` is repeatable and regenerates sample data relative to today's date, so "orders this month" always has rows.

Your **Gemini key lives only in `backend/.env`** (read server-side by the Google Gen AI SDK). It is never sent to the browser. If it is missing, chat returns a clear "key not configured" message instead of failing silently. `GEMINI_MODEL` is configurable; change it if you want a different Gemini model.

## What you get

| Area | Implementation |
|---|---|
| Chat UI | Sidebar, domain selector, conversation history, example prompts, loading + error states with retry, copy answer, clear conversation, result tables, summary cards, Chart.js charts (bar/line/grouped), CSV download, "How I got this" trace, light/dark, mobile layout, settings panel |
| Agent | Intent → schema-aware tool choice → backend validation → execution → grounded answer. Configurable max tool calls and total timeout; tool errors are fed back to the model and are never hidden; UI result blocks come only from successful tool results |
| Tools | `query_records`, `aggregate_records`, `get_distinct_values`, `describe_table`, `run_report` (per-domain predefined reports), `search_documents` (RAG, optional). Register more in `app/tools.py` |
| DB engine | Introspected tables/columns/types/PKs/FKs, cached with TTL + admin refresh, filters (14 operators), text search, sort, pagination with totals, joins via declared foreign keys, count/sum/avg/min/max, date bucketing |
| Auth | Registration/login/logout, Argon2id hashes, server-side sessions (HMAC-hashed tokens in MySQL), HttpOnly + SameSite cookie, CSRF token + Origin check, rate limits, roles `admin / analyst / viewer`, per-user domain grants |
| Persistence | Users, roles, sessions, conversations, messages, domain configs, connection metadata, audit events, document metadata and chunks |
| Domains | Student management, E-commerce, Helpdesk with sample data (JSON config + DDL + seeder) |
| RAG (optional) | PDF/DOCX upload (type, magic-byte and size checks) → text → overlapping chunks → Gemini embeddings → owner-scoped cosine retrieval → answers with source references |

## Security model

The prompt is **not** a security boundary. Everything below is enforced in code the model cannot alter:

- **No SQL from the model.** There is no `execute_sql` tool. The model supplies a *plan* (table, joins, columns, filters…). It is validated by Pydantic (`extra="forbid"`, approved operators only), then every identifier is checked against the schema registry, then compiled to a `SELECT` with SQLAlchemy Core. All values are bound parameters; `LIKE` wildcards are escaped. Multiple statements, DDL, DML and admin operations cannot be expressed.
- **Allow-listed schema.** Only tables in the domain's JSON config are visible; columns can be excluded (the demo hides emails and phone numbers). Excluded columns can't be selected, filtered, sorted or grouped.
- **Read-only database account.** Domain databases are opened with an account that has only `SELECT`. MySQL sessions also run `SET SESSION TRANSACTION READ ONLY` and `MAX_EXECUTION_TIME`. Row limits, pagination and timeouts are enforced server-side. (Verified against a real MySQL 8: `DELETE`, `INSERT`, `DROP` all fail.)
- **Authorization.** The domain comes from the authenticated request and is checked against the user's grants; tools take no domain/connection argument, so prompt injection can't switch databases. Conversations and documents are owner-checked on every access.
- **Secrets stay server-side.** Connection metadata stores only the *name* of an env var for the password. Errors returned to the browser are generic; stack traces and config go to server logs only. The frontend stores nothing sensitive (only the theme choice in localStorage).
- **Web hardening.** CSP (`script-src 'self'`, Chart.js is vendored), `X-Frame-Options`, `nosniff`, `no-store` on API responses, CSRF header for all state-changing calls, Origin allow-list, rate limiting on login/register/chat/upload, audit trail of logins, denials, chats and admin actions.
- **Bounded LLM context.** Only the last `HISTORY_MESSAGES` turns, only the first `LLM_MAX_ROWS` rows of a result (the user still sees the full page), and only permitted schema metadata are sent to Gemini. No credentials or unrelated records.

Optional text-to-SQL is **intentionally not implemented**; the validated-plan tools cover the required scenarios without the risk.

## Add your own domain (no agent changes)

1. Create a read-only database user with `SELECT` on your database.
2. Add `backend/domains/<key>.json`:
   ```json
   {
     "key": "hr", "name": "HR", "description": "Employees and leave.", "database": "my_hr_db",
     "hints": "Free-text guidance for the model, e.g. how statuses are spelled.",
     "tables": {
       "employees": {"description": "…", "columns": "*", "exclude": ["salary", "home_address"]},
       "departments": {"columns": "*"}
     },
     "reports": [{"name": "headcount_by_department", "description": "…", "kind": "aggregate",
                  "plan": {"table": "employees", "joins": ["departments"],
                           "metrics": [{"func": "count", "alias": "headcount"}],
                           "group_by": [{"column": "departments.name"}]}}],
     "examples": ["How many employees are in each department?"]
   }
   ```
3. `python -m scripts.setup sync-domains`, then grant access in **Settings → People and access** (admins see every domain).
4. After schema changes use **Settings → Refresh schema** (or wait for `SCHEMA_CACHE_TTL_SECONDS`).

Other SQL engines: add a `DialectAdapter` + a `DRIVER_URLS` entry in `app/db.py`; the query engine itself is dialect-neutral SQLAlchemy Core.

## Optional: document Q&A

Set `RAG_ENABLED=true`. Analysts and admins can upload PDF/DOCX files in **Settings → Documents**; the agent then gains a `search_documents` tool and cites passages as sources. With RAG off, none of this is loaded and database chat is unaffected. Vectors are stored in MySQL and searched with numpy; for large corpora swap `RagService.search` for FAISS or ChromaDB (the surface is `ingest` / `search` / `delete`).

## Roles and access

| Role | Can |
|---|---|
| viewer | Chat in granted domains, view own documents |
| analyst | + upload/delete own documents |
| admin | + all domains, user/role/grant management, schema refresh, audit log (`GET /api/admin/audit`) |

New sign-ups are `viewer`. `DEFAULT_DOMAIN_GRANTS` controls which domains they get automatically — set it empty to require admin approval, or `ALLOW_REGISTRATION=false` to disable self-registration.

## API (all JSON, all under `/api`)

`POST auth/register · auth/login · auth/logout`, `GET auth/me` · `POST chat` · `GET conversations[?domain=]`, `GET|DELETE conversations/{id}`, `POST conversations/{id}/clear` · `GET domains`, `GET domains/{key}/schema`, `POST domains/{key}/refresh-schema` (admin) · `GET|POST documents`, `DELETE documents/{id}` · `GET admin/users`, `PATCH admin/users/{id}`, `GET admin/audit` · `GET health`. Interactive docs at `/api/docs` outside production.

## Tests

```bash
cd backend && pytest -q      # 39 tests, no MySQL or Gemini key needed
```

Covers: query validation and injection attempts, excluded columns, joins/aggregates/date tokens/pagination/limits, auth + cookies + CSRF + Origin, domain authorization, conversation ownership, the agent loop with a scripted fake LLM (real tool results reach the model, errors are reported honestly, unapproved tools and cross-domain arguments are rejected), and RAG validation/ownership.

## Production checklist

- `APP_ENV=production`, a strong `SECRET_KEY`, `COOKIE_SECURE=true`, HTTPS in front (and `ALLOWED_ORIGINS` set to your real origin).
- Remove `MYSQL_ADMIN_*` from the runtime environment after setup.
- Rate limits and the login throttle are in-process; with several workers move `RateLimiter` to Redis.
- Run `uvicorn`/`gunicorn` behind a reverse proxy; set `TRUST_PROXY_HEADERS=true` only if the proxy overwrites `X-Forwarded-For`.
- Back up the application database (conversation and audit data).

## Project layout

```
backend/app/        main · config · db · models · security · deps · registry · query_engine
                    runtime · tools · agent · llm · rag · api/{auth,chat,documents,admin}
backend/domains/    one JSON file per business domain
backend/sql/        DDL for the three sample domains
backend/scripts/    setup CLI + sample-data seeders
backend/tests/      pytest suite
frontend/           index.html · styles.css · app.js · vendor/chart.umd.js
```
