# Mars Petcare Backend -- CMIR Email Resolution, PO Validation, and Projected Penalties, Mitigation & Dispute Resolution

Two agentic backends in one FastAPI app, separated by Postgres schema:

- **CMIR Resolution Agent** (`cmir` schema) — processes inbound CMIR emails,
  extracts CMIR draft data with Azure OpenAI via a LangGraph workflow, pauses
  for human review, and persists approved records in Postgres. Also runs a
  PO Validation agent against the same email-ingest/HITL infrastructure.

- **Projected Penalties** (`penalties` schema) — forecasts, ahead of delivery, the
  retailer chargebacks Mars Petcare is likely to incur on open purchase orders,
  driven by production shortfalls and shipment delays; after delivery,
  re-adjudicates a retailer's already-charged deduction against Mars's own
  rules and the real, final delivery facts (not the risk-adjusted ones
  projection works with). A deterministic rules engine computes both the
  projection and the dispute verdict; the LLM only explains the projection, or
  narrates the dispute outcome, in plain language. A separate LangGraph
  pipeline extracts candidate penalty rules from an uploaded contract, with a
  human-review step before anything is promoted into a live rule.

Both domains share a `common` schema (retailers, materials, purchase orders,
...) and a `process` schema (the job/agent/workflow backbone: `job_run`,
`job_item`, `workflow_thread`, `agent_run`, ...). `public` holds no domain
tables; LangGraph's own checkpoint tables live in their own `langgraph`
schema.

## Stack

Python 3.12+, FastAPI, PostgreSQL (SQLAlchemy 2.0 + Alembic), LangGraph
(CMIR/PO-validation workflows, PostgreSQL checkpointer), Azure Service Bus
(CMIR mail-processing queue), Azure OpenAI (CMIR extraction and the
penalty-projection-summary / penalty-mitigation-summary features).

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
uv sync   # or: pip install -e .

cp .env.example .env   # edit DATABASE_URL, and AZURE_OPENAI_* / EMAIL_* /
                        # SERVICE_BUS_* / JOB_QUEUE_* for the features you're running

alembic upgrade head
```

## Running

There is one image and one codebase, but **two things you can run**. Which
one you want depends on whether you are serving requests or processing the
day's backlog.

### The API

```bash
uvicorn app.main:app --reload
```

Interactive docs: `http://127.0.0.1:8000/docs`. Health check:
`curl http://127.0.0.1:8000/api/v1/health`.

Start a CMIR email-ingest batch:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/cmir/email-events \
  -H "Content-Type: application/json" \
  -d '{"max_workers":4,"source":"gmail","filters":{"subject_contains":"CMIR","unread_only":true}}'
```

List the CMIR reviewer queue: `GET /api/v1/workflow-threads?domain=cmir`.

Seed penalties demo data and try it out:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/admin/seed-master-data
curl -X POST http://127.0.0.1:8000/api/v1/admin/simulate-daily-run
curl -X POST http://127.0.0.1:8000/api/v1/penalties/projections \
  -H "Content-Type: application/json" -d '{"purchase_order_id": "<purchase_order_id>"}'
```

`seed-master-data` also seeds a separate, additive set of dispute fixtures
(not touched by `simulate-daily-run`). A dispute moves through
`OPEN` → `ANALYZED` → `RESOLVED`/`OVERRIDDEN`; the deterministic rules
engine — never the LLM — computes the verdict, matched against the rules
in force on the historical charge date, not today. See `docs/RUNBOOK.md`
§11 for the full walkthrough.

### The batch worker

Every OPEN purchase order, projected and summarised concurrently through a
durable queue. In production this is an Azure Container Apps Job on a
nightly cron; locally it is the same script:

```bash
python scripts/ops/run_daily_batch.py            # enqueue + drain
python scripts/ops/run_daily_batch.py --dry-run  # count only, writes nothing
```

The API can enqueue a batch too (`POST /api/v1/job-runs`), but under the
default `postgres` backend that only writes the ledger rows — nothing runs
them until a drain happens. Watch progress with
`GET /api/v1/job-runs/{job_run_id}`.

### A single PO, no server

```bash
python scripts/ops/run_projection_cli.py --purchase-order-id <uuid> --date 2026-08-05
python scripts/ops/run_projection_cli.py --all-open
```

## Tests

```bash
pytest tests/unit/ -v
```

Runs against an in-memory SQLite database (every schema translated away for
SQLite, see `app/db/session.py`) — no live Postgres required.
`tests/integration/` exercises a real Postgres connection and skips (rather
than failing) when one isn't reachable at `DATABASE_URL`.

## Docs

Start here:

- [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) — **how to run the whole system**: local
  workflow with the queue, every config variable, and the Azure build sheet
- [`docs/JOB-QUEUE-WALKTHROUGH.md`](docs/JOB-QUEUE-WALKTHROUGH.md) — code tour of the
  queue, for someone seeing it for the first time

Reference:

- [`docs/API.md`](docs/API.md) — every endpoint, request/response shapes, error codes
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — setup, seeding, troubleshooting, in depth
- [`docs/DATABASE.md`](docs/DATABASE.md) — schema, migrations, conventions
- [`docs/DOCKER.md`](docs/DOCKER.md) — image internals

## Layout

```
app/
  main.py                 -- FastAPI app factory
  api/v1/                   -- routers: common/, penalties/, cmir.py, po_validation.py,
                                workflow_threads.py + processing_errors.py (shared), admin.py
  core/                       -- config/ (nested settings), exceptions.py + envelope.py,
                                middleware/, rate_limit.py
  services/                     -- business logic, domain-first: penalties{projection,mitigation,dispute}/,
                                     cmir/, po_validation/, seeding/
  agents/                            -- LLM/LangGraph layer: providers/ (shared),
                                        penalties/{projection,mitigation,dispute}/, cmir/, po_validation/
  queue/                               -- job-queue dispatch backends behind one Protocol
                                          (shared by both domains), plus the CMIR Service Bus producer
  workers/                              -- the claim/execute/settle loop (shared), plus the
                                            CMIR Service Bus consumer
  models/                                 -- SQLAlchemy ORM + enums, one folder per schema:
                                             common/, process/ (shared backbone), cmir/, penalties/
  repositories/                            -- database access, mirrors models/ 1:1
  schemas/                                   -- Pydantic request/response models
alembic/                -- migrations (single linear history across every schema)
tests/unit/             -- pytest (in-memory SQLite)
tests/integration/      -- pytest against a live Postgres
scripts/ops/            -- operational entry points (nightly batch, CLI)
scripts/demo/           -- seed and demo scripts
```

## CMIR Resolution Agent — feature notes

The current implementation follows `docs/prd.md`: one backend ingest batch
can process many emails, and each email-derived review workflow gets its
own UI-facing `thread_id`.

**Important identity rule**: reviewer/UI actions must use `thread_id` — not
sender email, `batch_id`, `agent_run_id`, or `email_id`. One batch can
contain multiple emails, and multiple emails can come from the same sender.

Main endpoints (base path `/api/v1`):

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/cmir/email-events` | Start one email ingest batch. |
| `GET` | `/workflow-threads?domain=cmir` | Reviewer queue: list workflow threads. |
| `GET` | `/job-runs/{job_run_id}` | Ops view: run rollup for one ingest batch (`job_run_id` is the `batch_id` returned by `POST /cmir/email-events`; shared job-queue resource — no filter-by-`job_type` list route exists). |
| `GET` | `/workflow-threads/{thread_id}` | Get current stage/status for one thread (`?include=snapshot` for the full snapshot). |
| `POST` | `/workflow-threads/{thread_id}/missing-fields` | Submit missing mandatory fields and resume graph. |
| `PATCH` | `/workflow-threads/{thread_id}/draft` | Save reviewer draft edits. |
| `POST` | `/workflow-threads/{thread_id}/decisions` | Approve or reject a draft (`decision_type` discriminator). |

Common error responses (see `app/core/exceptions.py`'s collapsed `AppError`
hierarchy — `{success, message, data, error}` envelope on every route):

- `THREAD_STALE` — refetch the latest `updated_at` and retry as `expected_updated_at`.
- `THREAD_NOT_WAITING` — missing-fields requires `waiting_missing_fields`;
  update/decision require `waiting_approval` (check `workflow_thread.status`).
- `CMIR_VERSION_CONFLICT` (HTTP 409) — another thread's approval already
  superseded the active `cmir_record` row for this customer/material while
  this thread was waiting. The thread closes to `COMPLETED_CONFLICT` and does
  not reopen automatically; fetch the winning thread's snapshot or start a new one.

Gmail note: `EMAIL_USERNAME`/`EMAIL_PASSWORD` need a Gmail app password, not
the normal account password, with IMAP enabled.

Debugging guide: open `docs/debug_flow.html` in a browser for the full
ingest-flow walkthrough, LangGraph node sequence, and common-failure table.

## Security

Never commit `.env` or real credentials. Rotate any Gmail app password,
database password, Azure OpenAI key, or Service Bus connection string that
has been shared outside a secure secret manager.
