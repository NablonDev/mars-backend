# Runbook

Every way to set up, run, seed, exercise, and troubleshoot this service.
For endpoint-by-endpoint reference, see `docs/API.md`.
This file is about running it.

**Every curl example below against a protected route carries
`-H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"`, except `/api/v1/health`,**
the one route that needs no key -- see `docs/API.md` "Authentication" for
the full contract (missing/wrong key -> `401`, generic body, nothing echoed
back).

**Sections 2 onward cover the API and the single-order paths.** For the
batch job queue -- every OPEN order, scheduled and run as a group -- see
`docs/DEPLOYMENT.md`:

- **The scheduled daily run is `scripts/ops/run_daily_batch.py`**, executed
  as the Azure Container Apps Job on a nightly cron (§6.6). It both
  enqueues today's work *and* drains it in the same call -- nothing else
  needs to run.
- **`POST /api/v1/batches/runs` is not a substitute for the schedule.**
  Under the default `postgres` backend it only enqueues; nothing processes
  those rows until a drain happens (the same script, or the nightly job).
  It's for triggering a run on demand from outside, not for scheduling
  (§6.7). Endpoint reference: `docs/API.md` "Batches".
- Full local walkthrough (enqueue, watch, drain, inspect a stuck queue),
  every `JOB_QUEUE_*`/`SERVICE_BUS_*` config variable, and the Azure build
  sheet: `docs/DEPLOYMENT.md` §3, §4, §6.

## 1. CMIR / PO Validation operations

Operational reference for the CMIR Resolution Agent and PO Validation Agent,
which share the FastAPI process the rest of this runbook covers but add their
own deployed components and failure modes.

### Deployed components

Three independently-running processes, sharing the same Postgres database (the `cmir`
schema):

1. **FastAPI app** (`app.main:app`, same process as the penalties API below) -- the only
   process that runs LangGraph. Handles reviewer HTTP traffic and
   `/api/v1/internal/process-email` (the queue-consumer's forwarding target).
2. **Azure Function** (`function_app.py`, timer-triggered every minute) -- claims new
   `email_events` rows and enqueues them to Azure Service Bus. Does **not** run any
   workflow logic itself; trigger definitions live in `azure_functions/` as Blueprints
   registered onto the root `FunctionApp()`.
3. **Service Bus consumer** (`app/workers/cmir_service_bus_consumer.py`, run via `python -m
   app.workers.cmir_service_bus_consumer`) -- a standalone long-running listener. Deserializes
   each queue message and forwards it over HTTP to the FastAPI process; never touches
   the graph or Postgres directly.

All LangGraph execution -- queue-driven or reviewer-driven -- funnels through the one
FastAPI process, keeping checkpoint state centralized.

Run the Service Bus consumer separately when testing the async queue path:
```bash
python -m app.workers.cmir_service_bus_consumer
```

### Required configuration

Loaded once into `app/core/config/` (nested pydantic-settings groups), same as the
penalties config below. Minimum required beyond `DATABASE_URL`: `EMAIL_USERNAME`/
`EMAIL_PASSWORD`/`EMAIL_IMAP_SERVER`, `AZURE_OPENAI_API_KEY`/`AZURE_OPENAI_ENDPOINT`/
`AZURE_OPENAI_DEPLOYMENT_NAME`. Service Bus needs
`SERVICE_BUS_NAMESPACE`/`SERVICE_BUS_CONNECTION_STRING` for anything
beyond local defaults. **Never commit `.env` or `local.settings.json`** -- rotate any
credential that leaks outside a secrets manager.

### Troubleshooting

#### Thread update/decision returns `THREAD_STALE` (409)
Another reviewer (or the system) updated the thread after the client last fetched it.
Fetch `GET /api/v1/workflow-threads/{thread_id}` (add `?include=snapshot` for the full
snapshot) for the current `updated_at` and retry with that value as `expected_updated_at`.

#### Thread action returns `THREAD_NOT_WAITING` (409)
The thread isn't paused at the stage that API expects. Check
`workflow_threads.status`/`stage` -- missing-fields APIs require
`waiting_missing_fields`; update/decision APIs require `waiting_approval`; PO
Validation's resume APIs require `waiting_manual_cmir_entry` /
`waiting_qty_mismatch_decision` respectively.

#### Decision returns `CMIR_VERSION_CONFLICT` (409)
Another thread's approval already superseded the active `cmir_records` row for this
customer/material while this thread was pending. The thread closes to
`COMPLETED_CONFLICT`/`completed_conflict` -- it does **not** reopen for retry
automatically. Fetch `GET /threads/{thread_id}/snapshot` for the thread that actually
won, or start a new one, against the current record.

#### PO Validation resume returns `MATERIAL_NOT_FOUND` (422)
The reviewer-submitted/chosen SAP material number has no `material_master` row for
that plant. This is checked *before* the graph is touched -- nothing was written to
`purchase_order_line`/`process.processing_error` for this attempt. Confirm the
material/plant combination against the SAP mirror sync, or ask the reviewer to pick a
different material.

#### Gmail ingest returns no emails
Check, in order: `filters.subject_contains`, `filters.unread_only`, the Gmail app
password (not the normal account password), IMAP enabled on the account,
`EmailConfig.lookback_days`, and whether the target emails are actually unread when
`unread_only=true`.

#### A thread is stuck at `FAILED` with `current_node: persist_email`
The graph raised before `email_id`/the `workflow_threads` row could be created
(`CmirService._process_email_thread`'s exception path). Check `agent_runs.error`
for the underlying exception message -- usually a Postgres connectivity issue or a
constraint violation on `email_events`/`cmir_records`.

#### Service Bus consumer keeps abandoning messages
`app/workers/cmir_service_bus_consumer.py::_process_message` abandons (rather than
completes) any message where the HTTP forward to `/internal/process-email` raises --
check the FastAPI process's logs for the actual failure, not the consumer's. The
consumer retries its receive loop with a 5s backoff on connection-level errors.

### Useful debug queries

Reviewer queue backlog:
```sql
SELECT id, stage, status, updated_at
FROM process.workflow_thread
WHERE status NOT LIKE 'completed%'
ORDER BY updated_at DESC;
```

Open pending actions older than expected (possible stuck reviews):
```sql
SELECT id, interrupt_type, requested_at
FROM process.human_action
WHERE status = 'open'
ORDER BY requested_at ASC;
```

Per-node timing/failures for one run:
```sql
SELECT node_name, status, duration_ms, error
FROM process.agent_trace
WHERE agent_run_id = :agent_run_id
ORDER BY started_at ASC;
```

Current CMIR mapping for one customer/material (post-SCD2):
```sql
SELECT * FROM cmir.cmir_record
WHERE customer_identity_key = UPPER(REGEXP_REPLACE(:customer_identity, '[^A-Za-z0-9]', '', 'g'))
  AND target_customer_material_ref_key = UPPER(REGEXP_REPLACE(:material_ref, '[^A-Za-z0-9]', '', 'g'))
  AND is_current;
```

`process.workflow_thread`/`process.human_action`/`process.agent_trace` are shared by
both `cmir` and `penalties` (the Phase 1 `process`-schema consolidation) -- filter by
joining to the relevant subject/context table when you need one domain only.

### Deployment notes

- `function_app.py`, `host.json`, `local.settings.json` must stay at the repo root --
  Azure Functions Core Tools and the Functions runtime discover them there by
  convention, not via configuration.
- `requirements.txt` (not `pyproject.toml`/`uv.lock`) is what Azure Functions' Python
  deployment model builds from -- keep it regenerated (`uv export`) after any dependency
  change, or the Function App deployment silently uses stale versions.
- Already-deployed databases on either project's old migration chain cannot be moved
  forward by Alembic itself -- `alembic upgrade head` aborts before any DDL. One whose
  contents are worth keeping is repaired in place, rows intact, by
  `scripts/ops/repair_pre_squash_db.py`; see "A database stranded on the pre-squash
  chain" under "4. Database: migrate" below. Dropping and recreating is now the
  fallback, not the only option. This supersedes the old CMIR-branch guidance to
  `alembic stamp head` against a pre-Alembic database; that guidance no longer
  applies now that both domains share one squashed history.

## 2. Prerequisites

- Python 3.12+
- A Postgres instance. SQLite is enough to create the schema and to run
  the test suite, but a SQLite database built by `alembic upgrade head`
  rejects every write (see "Switching database backends" in step 4), so
  the seeding, projection, and mitigation walkthroughs below all need
  real Postgres.

## 3. First-time setup

```bash
git clone <this repo> && cd mars
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env: point DATABASE_URL at your Postgres, and set APP_INTERNAL_API_KEY
# to a real value -- the app refuses to start without one.
```

`app/core/config/database.py::DatabaseSettings` reads `DATABASE_URL` from
`.env` via `pydantic-settings`. If `.env` doesn't exist or doesn't set it, the
default is `postgresql+psycopg://postgres:postgres@localhost:5432/mars`
-- a real Postgres URL, not a SQLite fallback, and not the placeholder
`.env.example` carries -- so on a machine with no Postgres running,
always set `.env` (or export `DATABASE_URL` directly) before doing
anything else.

`APP_INTERNAL_API_KEY` has no default at all. `Settings` declares it required
and rejects a blank or too-short value, so a missing key fails app
startup rather than quietly booting something unauthenticated. Every
route except `/api/v1/health` is gated on it.

**Check for a second `APP_INTERNAL_API_KEY` further down `.env` before
debugging a `401`.** Compose parses that file as plain dotenv and keeps
the *last* assignment to a name, then interpolates it into the backend as
`${APP_INTERNAL_API_KEY:?...}` (`docker-compose.yml`). A duplicate definition
lower in the file therefore wins silently: `docker compose up` starts a
backend holding a different key from the one at the top of the file, and
every authenticated call begins returning `401` with nothing in the
response to say why. Grep the whole file, not just its first hit.

## 4. Database: migrate

```bash
alembic upgrade head          # run from the repo root, not app/
```

This creates every table in `docs/DATABASE.md`'s table list. Safe to
re-run (Alembic tracks the applied revision in `alembic_version`).

**Switching database backends.** Changing `DATABASE_URL` and re-running
`alembic upgrade head` builds the schema on either backend -- the DDL
itself compiles on both (`docs/DATABASE.md` "Primary keys" -- the
surrogate `id` type works on both).

```bash
# One-off SQLite database in /tmp, no Postgres needed at all:
export DATABASE_URL="sqlite:////tmp/mars_demo.db"
alembic upgrade head
```

**A SQLite database built that way cannot be written to, though.** Both
migrations give every `created_at`/`updated_at` column
`server_default=sa.text("now()")` -- 67 of them, with no dialect branch.
SQLite accepts that as `DEFAULT (now())` at `CREATE TABLE` time and only
fails on the first insert:

```
sqlite3.OperationalError: unknown function: now()
```

so the recipe above looks like it worked and then breaks the moment
anything writes a row. The test suite never hits this: `tests/conftest.py`
builds its SQLite database with `Base.metadata.create_all()`, and the
ORM's `server_default=func.now()` compiles to `CURRENT_TIMESTAMP` on
SQLite, so the migration path is never exercised there. Treat the SQLite
recipe as a way to inspect schema shape, not to run the app.

**Adding a new migration** after changing `app/models/`:

```bash
alembic revision -m "add whatever" --autogenerate   # needs a live DB connection
```

Then hand-check the generated file against `app/models/` before
committing -- `tests/unit/db/test_migration_parity.py` will fail the
build if they disagree.

### A database stranded on the pre-squash chain

**This section describes the *first* squash (down to the two-schema
`e803d9470f31`/`43d8ced96170` chain) and is now itself superseded.** The
current migration chain is a *second*, later squash: five revisions,
`0824321a02a4` (shared master/fulfillment tables, unqualified in `public`)
-> `ff53dabe6e4c` (process) -> `374aa902b053`
(cmir) -> `4b41f6bcb2f3` (penalties) -> `a5b39c6e2181` (langgraph, head).
`scripts/ops/repair_pre_squash_db.py` still targets the *old* two-schema
shape (`fines`/`cmir`/`public`) and imports `FINES_SCHEMA`/`PUBLIC_SCHEMA`
from `app.db.base` -- constants that no longer exist there (replaced by
`PROCESS_SCHEMA`/`CMIR_SCHEMA`/`PENALTIES_SCHEMA`/`LANGGRAPH_SCHEMA`, plus
the former `common`-schema tables now living unqualified in `public`), so
the script as written does not run against the current codebase. A
database stranded on *either* old chain's head today
has no in-place repair path verified against the current models -- treat
drop-and-recreate as the only confirmed option until a new repair script
(or an update to this one) is written for the current five-schema shape.
The rest of this subsection is kept for its description of the *mechanism*
(in-place rename/retype instead of drop-and-recreate) but its specific
commands and target revision are stale:

A database last migrated before commit `37f5f56` (the squash down to two
initial migrations) still holds the old chain's head in `alembic_version`,
and that revision file no longer exists:

```
ERROR [alembic.util.messaging] Can't locate revision identified by 'e4b7c391a052'
```

Alembic stops there, before any DDL, so nothing is half-applied. Earlier
versions of this runbook and of `docs/DATABASE.md` said the only way out
was to drop and recreate. That is no longer true:
`scripts/ops/repair_pre_squash_db.py` turns the pre-squash physical schema
into the post-squash one in place, preserving every row, and stamps
`alembic_version` to `43d8ced96170`.

```bash
python scripts/ops/repair_pre_squash_db.py --dry-run   # prints every statement, changes nothing
python scripts/ops/repair_pre_squash_db.py             # the real run
python scripts/ops/repair_pre_squash_db.py --database-url postgresql+psycopg://...
```

It renames the 16 `dim_`/`fact_`-prefixed fines tables along with their
indexes and constraints, syncs every column type and nullability against
`app/models/` (derived from `Base.metadata` at runtime rather than
transcribed, so it cannot drift from the ORM), remaps
`job_item.task_type = 'SUMMARY_REGEN'` to `'PROJECTION_SUMMARY_REGEN'` and
rebuilds that `CHECK`, creates the three tables with no pre-squash
counterpart (`mitigation_input`, `mitigation_option`,
`mitigation_summary`), and stamps the version row. LangGraph's own
`public.checkpoint_*` tables are left alone. The script's module docstring
is the authoritative description.

Worth knowing before running it:

- **`--dry-run` is genuinely read-only.** It opens a plain connection, no
  transaction, prints the statements it would execute, and exits.
- **It refuses anything that isn't the exact pre-squash shape.**
  `alembic_version` must hold exactly `e4b7c391a052`, every old table name
  must be present, and no post-squash name may exist yet -- a partially
  repaired schema is rejected rather than guessed at. A pre-flight pass
  also confirms every value still fits its narrowed column and names the
  offending column and row if not. Exit `2` is a refusal, exit `3` a
  failed data check; neither writes anything.
- **The real run is a single transaction** ending in a `compare_metadata`
  check against `Base.metadata`. Any residual difference raises and rolls
  the whole thing back, so there is no half-repaired outcome to clean up.
- **Stop the backend (and any worker or consumer) first.** The table,
  index and constraint renames and the `ALTER COLUMN ... TYPE` statements
  all take `ACCESS EXCLUSIVE` locks for the length of that transaction; a
  live app either blocks the repair or gets blocked by it.
- It is deliberately not an Alembic revision: a third revision on top of
  `43d8ced96170` would also run against fresh databases and would have to
  detect-and-no-op there, baking a legacy repair into the history
  permanently. `scripts/ops/migrate_legacy_cmir_data.py` is the precedent
  for one-off surgery living in `scripts/ops/`.

### The `public`, `process`, `cmir`, `penalties`, and `langgraph` schemas

On Postgres, this app owns three dedicated schemas plus `langgraph`, and
uses `public` for its shared tables rather than reserving it: `public`
(shared master/fulfillment data used by both domains -- `retailer`, `sku`,
`purchase_order`/`purchase_order_line`, ... -- unqualified, no schema
override), `process` (the shared job/agent/workflow backbone -- `job_run`,
`job_item`, `workflow_thread`, `agent`, `agent_run`, `agent_trace`,
`human_action`, `processing_error` -- used by both `cmir`/`po_validation`
and `penalties`), `cmir` (CMIR/PO-validation-only tables), `penalties`
(penalties-only tables), and `langgraph` (LangGraph's own checkpoint
tables, created empty by its own migration and never touched by
autogenerate) -- see `docs/DATABASE.md`'s "Postgres schema separation"
section.
Each dedicated schema name is declared once, in `app/db/base.py`
(`PROCESS_SCHEMA`/`CMIR_SCHEMA`/`PENALTIES_SCHEMA`/`LANGGRAPH_SCHEMA`), and
every model's `__table_args__` binds to one of them; models with no schema
override resolve to `public` the same way any unqualified SQLAlchemy model
would, so nothing in `app/` needs to qualify a table name by hand beyond
that.

Three consequences worth knowing:

- Alembic's own `alembic_version` bookkeeping table lives in `public`
  (`_version_table_schema` in `alembic/env.py` returns `None`), alongside
  the shared master/fulfillment tables -- it tracks one linear migration
  history covering every schema this project owns, so it belongs in a
  schema no single domain owns.
- `alembic/env.py::ensure_project_schemas_exist` runs `CREATE SCHEMA IF
  NOT EXISTS` for `process`, `cmir`, and `penalties` before Alembic touches
  anything else (`langgraph` creates its own schema via its own migration;
  `public` always already exists on a fresh Postgres database), so
  `alembic upgrade head` bootstraps a brand-new empty database with no
  manual setup.
- SQLite has no schemas at all. `app/db/session.py::apply_sqlite_schema_translation`
  translates `process`/`cmir`/`penalties` away at the connection level
  (SQLAlchemy's `schema_translate_map`) -- `public`'s tables need no
  translation, they're already unqualified -- which is why the SQLite
  paths -- the whole test suite, and the `sqlite:////tmp/mars_demo.db`
  recipe above -- keep working unchanged. Schema translation is not what
  breaks writes on a migration-built SQLite database; the `now()` defaults
  above are.

## 5. Running the API

```bash
uvicorn app.main:app --reload
```

- Interactive docs (try every endpoint from the browser): `http://127.0.0.1:8000/docs`
- Raw OpenAPI schema: `http://127.0.0.1:8000/openapi.json`
- Health check: `curl http://127.0.0.1:8000/api/v1/health`

The app builds its own `Database` from `Settings` at startup
(`app/main.py::create_app`) -- it does **not** run migrations for you.
Step 4 has to happen first, once, against whatever `DATABASE_URL`
you're pointing at.

## 6. Seeding the four worked examples

Two admin endpoints do this over HTTP -- no direct database writes. Both
are idempotent: safe to call repeatedly (e.g. to reset a demo).

```bash
# With the server running (step 5), in another terminal:
python scripts/demo/seed_master_data.py                    # defaults to http://127.0.0.1:8000/api/v1
python scripts/demo/seed_master_data.py --base-url http://localhost:9000/api/v1   # different host/port

python scripts/demo/demo_daily_simulation.py               # replays all 4 scenarios, prints the day-by-day trend
```

Or hit the endpoints directly:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/admin/seed-master-data \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"
curl -X POST http://127.0.0.1:8000/api/v1/admin/simulate-daily-run \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"
```

`seed-master-data` creates the master data (retailers, materials, SKUs,
plants, carriers), 4 penalty rules, and 4 purchase-order headers (all
`OPEN`) for the worked-example walkthrough below, plus a separate,
additive set of fixtures for the dispute-resolution demo: 5 dispute-only
penalty rules, 8 `DELIVERED` purchase orders, and one `actual_penalty`
charge per order (see `docs/API.md`'s Admin section) -- neither set
touches the other. See §11 for the full dispute-resolution lifecycle
walkthrough against these fixtures. `simulate-daily-run`
walks all four orders through their entire scripted history
(`app/services/seeding/scenario_data_projection.py`), writing each day's facts and running a
projection, then marks every order `DELIVERED`. Running it twice in a
row will re-simulate from scratch and re-mark everything `DELIVERED` --
that's expected, not an error.

## 7. Running projections

**Via the API** (what a real integration would call). Every path below
takes the PO's surrogate UUID, not its business number like `WMT-100234` --
look it up first with `GET /api/v1/purchase-orders`:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/penalties/projections \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "<purchase_order_id>", "projection_date": "2026-08-05"}'

# Override the retailer's stacking policy for this run only:
curl -X POST http://127.0.0.1:8000/api/v1/penalties/projections \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "<purchase_order_id>", "stacking_mode_override": "MAX"}'

curl "http://127.0.0.1:8000/api/v1/penalties/projections?purchase_order_id=<purchase_order_id>" \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"                                                  # full dated history
curl "http://127.0.0.1:8000/api/v1/penalties/exposure?purchase_order_id=<purchase_order_id>" \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"                                                  # latest total only
curl "http://127.0.0.1:8000/api/v1/penalties/projections?status=OPEN" \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"                                                  # flat cross-PO list
```

**Every open order, today, is no longer one synchronous call.** The old
`POST /projections/runs` (`all_open: true`) conflated "compute inline" with
"queue a batch job" -- it's split now: `POST /api/v1/job-runs` (queues an
async `PENALTY_PROJECTION_BATCH`, see `docs/DEPLOYMENT.md` §1/§3.5 for the
dispatch/drain mechanics) for actually running every open order, and
`GET /api/v1/penalties/projections?status=OPEN` (above) for a synchronous
cross-PO read of whatever has already been projected. There is no
single-call synchronous "project every open order right now" endpoint any
more.

**Without a server** (batch/cron-style, straight against the database):

```bash
python scripts/ops/run_projection_cli.py --purchase-order-id <uuid>
python scripts/ops/run_projection_cli.py --purchase-order-id <uuid> --date 2026-08-05
python scripts/ops/run_projection_cli.py --all-open
python scripts/ops/run_projection_cli.py --all-open --date 2026-08-05 --stacking-mode MAX

# Runs the penalty projection summary inline (no BackgroundTasks needed in a
# one-shot process) right after each PO's projection succeeds. Needs
# AZURE_OPENAI_* configured (step 9):
python scripts/ops/run_projection_cli.py --all-open --with-summary
```

This is the no-HTTP-server path for one PO or a backfilled date --
standing up a server just to run a batch job is unnecessary overhead. The
*scheduled* daily run is `scripts/ops/run_daily_batch.py` instead (it
enqueues and drains in one call; see the note at the top of this file and
`docs/DEPLOYMENT.md` §3.5). No scheduler is actually provisioned yet --
`docs/DEPLOYMENT.md` §6.6 is the build sheet for the nightly job, §10
tracks it as outstanding.

### Mitigation options are keyed by `(purchase_order_id, projection_date)`, or a `projection_id` alias

`POST /api/v1/penalties/mitigations` never computes a projection of its
own. It accepts either `purchase_order_id`+`projection_date` directly, or a
`projection_id` -- resolved to that same pair via
`PenaltyProjectionRepository.get_by_id` (a specific persisted
`penalty_projection` row's own surrogate id, one row per PO/rule/date, not a
value you construct yourself). The order is always projection first,
mitigation second:

```bash
# 1. Project this PO today:
curl -X POST http://127.0.0.1:8000/api/v1/penalties/projections \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "<purchase_order_id>"}'

# 2a. Rank mitigation actions directly against that (purchase_order_id, projection_date):
curl -X POST http://127.0.0.1:8000/api/v1/penalties/mitigations \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "<purchase_order_id>", "projection_date": "2026-08-05"}'

# 2b. ...or look up a projection_id for that PO/date from its history and use that instead:
curl "http://127.0.0.1:8000/api/v1/penalties/projections?purchase_order_id=<purchase_order_id>" \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"
# -> take the `id` off any violation row for the date you want

curl -X POST http://127.0.0.1:8000/api/v1/penalties/mitigations \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"projection_id": "<projection_id>"}'
```

**Step 2b's history lookup is only needed for an already-run, older
projection date.** For the projection step 1 just ran, skip the round trip
entirely -- `POST /penalties/projections`'s own response already carries
each violation's `projection_id` directly (`response.data.violations[].projection_id`,
one per persisted `penalty_projection` row -- see `docs/API.md`
"Projections" and `app/schemas/penalties/projections.py`'s `ViolationResponse`),
ready to feed straight into `POST /penalties/mitigations`'s `projection_id`
field without a separate `GET .../projections?purchase_order_id=` call.

**A freshly seeded database has projection rows and still needs step 2
run explicitly.** `simulate-daily-run` replays each PO's scripted history
and stops there -- 2026-08-02 to 08-11 for WMT-100234, 08-06 to 08-14 for
WMT-100511, 08-03 to 08-13 for AMZ-778501, 08-11 to 08-17 for AMZ-780112
(`app/services/seeding/scenario_data_projection.py`). It is a historical
time series, not a projection for the current date, so
`GET /api/v1/penalties/projections?purchase_order_id=` looks perfectly
healthy while a mitigation call against today's (nonexistent)
`projection_date`/`projection_id` fails `404 PROJECTION_NOT_FOUND`. Pick a
date the history actually covers, or run today's projection first (step 1
above).

**There is no combined "projection + summary" or "mitigation + summary" or
"run everything" endpoint any more.** The old chained convenience routes
(`POST /orders/{id}/projections/runs`, `POST /orders/{id}/mitigation-options/runs`,
`POST /orders/{id}/fine-runs`) don't exist in the current API -- call each
step separately: `POST /penalties/projections`, then
`POST /penalties/projections/summary`; `POST /penalties/mitigations`,
then `POST /penalties/mitigations/summary`. `?include=summary` on the
`GET` routes attaches an already-generated summary to the response, but
never triggers generation itself (pure read, see `docs/API.md`); the two
compute routes (`POST /penalties/projections`, `POST /penalties/mitigations`)
don't accept `include=` at all -- their response always leaves
`summary`/`mitigations`/`mitigation_summary` unset. The dedicated
`GET /penalties/projections/summary` and `GET /penalties/mitigations/summary`
routes poll the same job without re-fetching the projection/mitigation
options.

**Without a server:** `scripts/ops/run_mitigation_cli.py` mirrors
`run_projection_cli.py` for the mitigation side --
`--purchase-order-id <uuid>`/`--all-open` and `--date` to rank, plus
`--with-summary` to also run the penalty mitigation summary inline (no
`BackgroundTasks` needed in a one-shot process) right after each PO's
ranking succeeds. Same prerequisite as the API: a projection has to exist
for that date first.

## 8. Feeding new facts (not one of the four scripted scenarios)

Master data is now the ERP-normalized shape (`docs/redesigned-schema.md`):
a purchase order has header + line rows, each line references a `material`
(plant-agnostic identity) and, for stock/logistics facts, a
`material-master` row (one per `(material, plant)`). Create master data,
then a PO, then post facts as they arrive -- every create response's `id`
feeds the next call:

```bash
RETAILER_ID=$(curl -s -X POST http://127.0.0.1:8000/api/v1/retailers \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"retailer_code": "RET-TGT", "retailer_name": "Target", "stacking_mode": "SUM"}' \
  | jq -r .data.id)

MATERIAL_ID=$(curl -s -X POST http://127.0.0.1:8000/api/v1/materials \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"material_code": "MAT-PED30", "description": "Premium Dog Food 30lb"}' \
  | jq -r .data.id)

PLANT_ID=$(curl -s -X POST http://127.0.0.1:8000/api/v1/plants \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"plant_code": "PLANT-ATL", "plant_name": "Atlanta DC", "country_code": "US"}' \
  | jq -r .data.id)

# A rule has to exist for the retailer or a penalty-projection call returns 422 NO_ACTIVE_RULES:
curl -X POST http://127.0.0.1:8000/api/v1/penalties/rules \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"rule_code": "RULE-TGT-OTIF", "retailer_id": "$RETAILER_ID", "violation_type": "OTIF_LATE", "calc_type": "PERCENT_OF_PO", "rate": 0.03}'

PO_ID=$(curl -s -X POST http://127.0.0.1:8000/api/v1/purchase-orders \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_number": "TGT-1", "retailer_id": "$RETAILER_ID", "order_date": "2026-09-01", "requested_delivery_date": "2026-09-10" "required_ship_date": "2026-09-08", "lines": [{"line_number": "1", "material_id": "$MATERIAL_ID", "plant_id": "$PLANT_ID", "ordered_quantity": 500, "unit_price": 20.0}]}" \
  | jq -r .data.id)

# SAP just cut the order (purchase_order_line_id comes off the PO create response's lines[]):
curl -X POST http://127.0.0.1:8000/api/v1/purchase-orders/$PO_ID/confirmations \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"confirmation_number": "CONF-TGT-1-01", "confirmation_date": "2026-09-03T06:00:00", "status": "CONFIRMED", "lines": [{"purchase_order_line_id": "<purchase_order_line_id>", "confirmed_quantity": 450}]}'

curl -X POST http://127.0.0.1:8000/api/v1/penalties/projections \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "$PO_ID"}'
```

`POST .../shipments` and `POST .../demand-exceptions` work the same way --
see `docs/API.md`. Every write is historized where the schema calls for it
(`docs/DATABASE.md` "Historization"), so posting the same kind of fact
again doesn't overwrite the last one, it adds to the timeline
`build_snapshot` reads "as of" a given date from.

## 9. Azure OpenAI configuration (for the penalty-projection-summary and penalty-mitigation-summary features)

`POST /penalties/projections/summary` and
`POST /penalties/mitigations/summary`
(see `docs/API.md` "Projections"/"Mitigations") are the only things in this
codebase that call out to an LLM. The mitigation one is a full mirror of
the projection one -- same bounded tool-calling loop, same
cache/background-job mechanics -- just explaining a PO's ranked mitigation
options instead of its projection trace
(`app/services/penalties/mitigation/summary_service.py`). Everything else
works with no Azure credentials set at all. **`POST .../summary` only
enqueues** (`get_or_schedule` writes the `process.job_item` row and returns
`202` with `status=PENDING` immediately -- it never calls Azure OpenAI
itself). Generation runs later, out of band, as that same queued
`process.job_item`, not inline with the `POST` -- **nothing drains that
queue automatically in the default `docker compose up` stack** (see the
note at the top of this file and §3.6 of `docs/DEPLOYMENT.md`); a worker
has to actually claim and run it -- `python scripts/ops/run_daily_batch.py
--drain-only` (or with no flag, which also enqueues), the standalone
worker container (`docker compose --profile tools run --rm
fines-projection-worker` -- **not** `--rm worker`; the service is named
`fines-projection-worker` in `docker-compose.yml` despite an older
comment nearby still saying `worker`), or the nightly Azure Container Apps
Job in a deployed environment -- before a `PENDING` row ever becomes
`READY` or `FAILED`. Until one of those runs, the row sits at `PENDING`
indefinitely with no LLM call ever made; that is expected, by-design
behavior, not a bug. Missing/bad credentials surface as a `FAILED` status
once that drain happens, not as a failure of the `POST` itself.

```bash
# Field names/values come from app/core/config/llm.py::AzureOpenAISettings,
# each read via its AZURE_OPENAI_* validation_alias (a flat env var name,
# no nesting). ENDPOINT is the full v1 API base URL (note the /openai/v1
# suffix) -- no AZURE_OPENAI_API_VERSION var; the v1 GA API dropped the
# dated api-version param entirely (2026-08).
AZURE_OPENAI_API_KEY=<your key>
AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/openai/v1
AZURE_OPENAI_DEPLOYMENT_NAME=<your deployment name>

# Optional -- defaults shown. AzureOpenAIChatClient passes both values
# straight through to the SDK's ChatOpenAI client -- no app-level retry
# wrapper on top, so the SDK's own retry logic is the only retry
# authority (an earlier version of this client stacked its own retry
# loop on top of the SDK's, which could multiply a slow call's wall-clock
# time well past AZURE_OPENAI_MAX_ATTEMPTS x AZURE_OPENAI_TIMEOUT_SECONDS
# -- that wrapper is gone). 3 attempts is safe with only one retry layer.
# Raise the timeout if a real deployment's latency runs long; only raise
# AZURE_OPENAI_MAX_ATTEMPTS further if you have evidence a retry actually
# recovers failures on your specific deployment (a genuinely transient
# blip, not normal variance):
AZURE_OPENAI_TIMEOUT_SECONDS=90
AZURE_OPENAI_MAX_ATTEMPTS=3
```

### Why a slow/flaky call surfaces as a background-job `FAILED` status, not a hang or a raw `500`

Generation runs whenever a worker later claims the queued `job_item` and
calls `SummaryServiceBase.run_generation` (via `app/workers/dispatch.py`'s
`run_summary`/`run_mitigation_summary` -- **not** `FastAPI.BackgroundTasks`,
and not inline with the `POST`), so a failure -- upstream timeout, rate
limit, bad credentials, anything `ChatOpenAI.invoke()` can raise -- is
caught there and persisted as a `FAILED` row rather than propagating into
an HTTP response at all. The real
exception is logged server-side (`logger.exception(...)`); only a generic,
client-safe message (coded `PENALTY_PROJECTION_SUMMARY_UPSTREAM_FAILED` /
`PENALTY_MITIGATION_SUMMARY_UPSTREAM_FAILED`) reaches the row a client can
poll, per this app's message/detail split (`app/core/exceptions.py`).

With those set (`<purchase_order_id>` is the PO's surrogate UUID, from
`GET /api/v1/purchase-orders`):

```bash
# Fast, no LLM call inline -- 200 (cache hit) or 202 (job scheduled):
curl -X POST http://127.0.0.1:8000/api/v1/penalties/projections/summary \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "<purchase_order_id>"}'

# Poll again with the same call until status leaves PENDING, or read the
# dedicated summary route directly:
curl "http://127.0.0.1:8000/api/v1/penalties/projections/summary?purchase_order_id=<purchase_order_id>&as_of_date=2026-08-05" \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"

# Force a fresh LLM call even if today's summary is already cached:
curl -X POST http://127.0.0.1:8000/api/v1/penalties/projections/summary \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "<purchase_order_id>", "force_regenerate": true}'
```

The mitigation summary is the same shape against
`/penalties/mitigations/summary` instead, with the same
`purchase_order_id`/`as_of_date`/`force_regenerate` body fields -- it needs
ranked mitigation options to already exist for that date
(`POST /penalties/mitigations` first, see step 7):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/penalties/mitigations/summary \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "<purchase_order_id>"}'

curl -X POST http://127.0.0.1:8000/api/v1/penalties/mitigations/summary \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "<purchase_order_id>", "force_regenerate": true}'
```

Or the scripted equivalent, which looks up each PO's actual latest
projection (or, for mitigation, mitigation-options) date first rather
than relying on today happening to fall inside the mock scenarios' Aug
2026 date range:

```bash
python scripts/demo/demo_penalty_projection_summary.py                              # every PO on file
python scripts/demo/demo_penalty_projection_summary.py --purchase-order-id <uuid>     # one PO
python scripts/demo/demo_penalty_projection_summary.py --purchase-order-id <uuid> --force-regenerate

python scripts/demo/demo_penalty_mitigation_summary.py                              # same idea, for mitigation options
python scripts/demo/demo_penalty_mitigation_summary.py --purchase-order-id <uuid>
python scripts/demo/demo_penalty_mitigation_summary.py --purchase-order-id <uuid> --force-regenerate
```

**The full tour, in one command** -- seed, replay all four scenarios,
then explain each order's final number, chained together
(`scripts/demo/run_end_to_end_demo.py`):

```bash
python scripts/demo/run_end_to_end_demo.py                   # needs Azure OpenAI creds for the last stage
python scripts/demo/run_end_to_end_demo.py --skip-penalty-projection-summary  # engine-only, no LLM cost, no creds needed
```

Leave the `AZURE_OPENAI_*` vars unset/blank to run every other part of
the app normally -- `Settings` defaults them all to `""`, and the
penalty-projection-summary and penalty-mitigation-summary endpoints only fail
(`AzureOpenAIConfigError`, surfaced as a clean error, not a stack trace)
the first time either is actually called, not at app startup.

### Known limitation: no request-level rate limiting

`POST /penalties/projections/summary` and
`POST /penalties/mitigations/summary` both validate `as_of_date`
against the PO's real history -- projection dates for the former,
mitigation-options dates for the latter (rejects anything before the
earliest such date or after today, 422) -- specifically so a caller can't
mint unbounded cache keys -- and therefore unbounded real Azure OpenAI
calls -- just by varying that one field. That closes the "vary a
parameter to always miss cache" vector, but there is still no
(checked `app/main.py`, `app/core/`, `pyproject.toml` -- no `slowapi` or
request-level rate limiting (per-IP/per-caller) anywhere in this codebase
equivalent is installed) protecting either endpoint's real per-call cost
from an authenticated-but-abusive or simply high-volume caller. Adding
one is deliberately out of scope here -- it's infra-level and belongs
applied consistently across the app if/when other endpoints need it
too, not bolted onto either route as a one-off. Needed before either
endpoint is exposed in production.

## 10. PO delivery-date change requests (negotiation)

This is the retailer-negotiation step ops takes before falling back to
penalty mitigation: ask the retailer for more delivery time rather than
accepting whatever mitigation costs the projection implies.
`PoDeliveryChangeRequestService` (`app/services/penalties/delivery_change.py`)
records the retailer's decision as a mock/manual entry -- no inbound
webhook in this pass -- and on every terminal outcome (`ACCEPTED`/
`COUNTERED`/`REJECTED`/`EXPIRED`) immediately re-triggers the projection
engine for that PO, so the same-day projection reflects the outcome
instead of waiting for tomorrow's nightly batch. There is no shadow
mitigation tracking kept in parallel: while a request is `PENDING`, the
existing daily projection/mitigation cycle is itself the fallback plan.

**Via the API**, using WMT-100234's PO id (one of the four seeded demo
orders -- look up its UUID via `GET /api/v1/purchase-orders`):

```bash
# Ask the retailer for a later delivery date:
curl -X POST http://127.0.0.1:8000/api/v1/delivery-change-requests \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"purchase_order_id": "<purchase_order_id>", "reason_code": "SHORTAGE", "proposed_delivery_date": "2026-08-14", "notes": "SAP confirms a real cut to 1,850/2,000; requesting 3 extra days."}'

# Retailer accepts the proposed date outright (<delivery_change_request_id> is
# the "id" field from the create response above):
curl -X POST http://127.0.0.1:8000/api/v1/delivery-change-requests/<delivery_change_request_id>/response \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision": "ACCEPTED"}'

# Retailer counters with a date strictly between baseline and proposed:
curl -X POST http://127.0.0.1:8000/api/v1/delivery-change-requests/<delivery_change_request_id>/response \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision": "COUNTERED", "countered_delivery_date": "2026-08-12"}'

# Retailer rejects outright -- current_delivery_date/current_required_ship_date are left untouched:
curl -X POST http://127.0.0.1:8000/api/v1/delivery-change-requests/<delivery_change_request_id>/response \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision": "REJECTED"}'

curl "http://127.0.0.1:8000/api/v1/delivery-change-requests?purchase_order_id=<purchase_order_id>" \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"   # full history
```

**Without a server** (`scripts/ops/run_po_delivery_change_cli.py`, same
no-HTTP-server convention as `run_projection_cli.py`/`run_mitigation_cli.py`):

```bash
python scripts/ops/run_po_delivery_change_cli.py create --purchase-order-id <uuid> --reason-code SHORTAGE --proposed-delivery-date 2026-08-20
python scripts/ops/run_po_delivery_change_cli.py respond --id <uuid> --decision ACCEPTED
python scripts/ops/run_po_delivery_change_cli.py respond --id <uuid> --decision COUNTERED --countered-delivery-date 2026-08-18
python scripts/ops/run_po_delivery_change_cli.py history --purchase-order-id <uuid>
python scripts/ops/run_po_delivery_change_cli.py expire-sweep
```

`expire-sweep` runs automatically inside the nightly batch
(`scripts/ops/run_daily_batch.py`, via
`app.workers.penalty_projection.sweep_expired_po_delivery_change_requests`) and
never needs manual triggering -- there is no HTTP equivalent for it; the CLI
subcommand above exists purely for ad-hoc/local testing of the sweep.

**Seeding also exercises all four negotiation outcomes now.** §6's
`simulate-daily-run` fires one PO delivery-change request per seeded order
and walks each to a different terminal outcome automatically: WMT-100234
`ACCEPTED`, WMT-100511 `COUNTERED`, AMZ-778501 `REJECTED`, and AMZ-780112
`EXPIRED` (never responded to; its 24h SLA lapses before the scenario's
last scripted day, so it's the recovery sweep -- not a `respond` call --
that resolves it). See `app/services/seeding/scenario_data_projection.py`'s
`_NEGOTIATION_SCENARIOS` for the exact trigger dates and rationale behind
each.

### Troubleshooting

#### Create returns `ACTIVE_PO_DELIVERY_CHANGE_REQUEST_EXISTS` (409)
The PO already has a `PENDING` request -- only one active request per PO at
a time. Check `GET /delivery-change-requests?purchase_order_id=`
for the pending row's `id`, then either respond to it or wait for
the nightly sweep (or `expire-sweep --as-of ...` ad hoc) to expire it
before creating another.

#### Create returns `PO_DELIVERY_CHANGE_LEAD_TIME_ERROR` (422)
Fewer days remain before the PO's `current_required_ship_date` than the
retailer's `extension_min_lead_days` policy allows (`retailer.extension_min_lead_days`,
default 2). Check the retailer's policy via `GET /retailers` and the
PO's current required-ship date via `GET /purchase-orders`.

#### Response returns `PO_DELIVERY_CHANGE_REQUEST_NOT_FOUND` (404)
No `po_delivery_change_request` row exists for that `id`.
Confirm the id from the `create` response or the history endpoint -- it's
the delivery-change request's own surrogate id, not the PO id.

#### Response returns `INVALID_PO_DELIVERY_CHANGE_RESPONSE` (422)
Either the request is no longer `PENDING` (already responded to, or already
expired by the nightly sweep), or the `COUNTERED` payload is malformed --
`countered_delivery_date` missing when `decision=COUNTERED`, present when
it isn't, or not strictly between `baseline_delivery_date` and
`proposed_delivery_date`. Check the request's current `status` via history
first.

## 11. Post-delivery penalty dispute resolution

This is the other side of the retailer relationship from §10: instead of
negotiating a still-open PO's delivery date before a charge exists, this
handles a retailer's `actual_penalty` charge that has already been
recorded (post-delivery, a real deduction/invoice) and re-adjudicates it
against Mars's own rule engine rather than accepting it at face value.
`DisputeResolutionService` (`app/services/penalties/dispute/service.py`) owns the
lifecycle: `open_dispute()` records a dispute against one `actual_penalty`
row; `analyze()` is a synchronous, deterministic recompute
(`app/services/penalties/dispute/engine.py`, reusing the exact same
PER_UNIT/PERCENT_OF_PO/FLAT_FEE/TIERED pricing functions the projection
engine uses, fed real, final post-delivery facts instead of risk-adjusted
ones) that classifies the charge as `NO_PAY`/`PAY_PARTIAL`/`PAY_FULL` and
moves the dispute to `ANALYZED`; `resolve()` then records a human decision
-- either accepting that verdict (`RESOLVED`) or overriding it with a
different one and a required reason (`OVERRIDDEN`). Like §10, there is no
blocking human-approval gate before a verdict is written -- no LangGraph,
no job queue for `analyze()` itself -- a human accepts or overrides only
after the deterministic verdict already exists. The one asynchronous piece
is the LLM narrative over an already-computed verdict (`.../summary`),
which mirrors §9's projection/mitigation summary trigger/poll pattern
exactly, including its background-job, cache, and `FAILED`-status
mechanics -- see §9 for all of that; this section only covers what's
dispute-specific. At most one `OPEN`/`ANALYZED` dispute may be active
against a given `actual_penalty` charge at a time; a charge can go through
more than one dispute cycle over its life, just not two open at once.

**Via the API**, using seed scenario (a) `correct_shortage` from
`app/services/seeding/scenario_data_dispute.py` (`purchase_order_number`
`ORD-DSP-A1`, seeded by `seed-master-data`'s additive dispute fixtures --
see §6): a 100-unit, $10/unit PO against retailer `RET-DSPA`, delivered
90 units (a confirmed 10-unit shortfall) against a `SHORT_SHIP` rule
charging `$5`/unit with no threshold, cap, or grace period. The retailer's
charge (`actual_penalty_number` `AP-ORD-DSP-A1`) is exactly `$50.00` --
Mars's own rule recomputes the identical amount, so this scenario is
scripted to land on `PAY_FULL` with a `$0` delta, proving the "correct
charge" path end to end. Every path below takes the PO's surrogate UUID,
not its business number -- look it up first with
`GET /api/v1/purchase-orders` (`purchase_order_number` `ORD-DSP-A1`), then
its one `actual_penalty` charge with
`GET /api/v1/penalties/actual-penalties?purchase_order_id=<purchase_order_id>`
(`actual_penalty_number` `AP-ORD-DSP-A1`, `$50.00` `SHORT_SHIP`):

```bash
# Open a dispute against that charge (reason_code is caller-supplied, not
# part of the seed fixture -- AMOUNT_INCORRECT fits since the whole premise
# of this scenario is questioning whether the charged amount is right):
curl -X POST http://127.0.0.1:8000/api/v1/penalties/disputes \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"actual_penalty_id": "<actual_penalty_id>", "reason_code": "AMOUNT_INCORRECT", "claimed_amount": 50.0}'
```

Analyze it -- computes and persists the verdict immediately, no polling
(`<dispute_id>` is the `id` field from the create response above):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/penalties/disputes/<dispute_id>/analyze \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"
```

```json
{
  "success": true,
  "message": "Penalty dispute analyzed.",
  "data": {
    "id": "<dispute_id>",
    "dispute_number": "DSP-3F9A2B1C4D5E",
    "actual_penalty_id": "<actual_penalty_id>",
    "purchase_order_id": "<purchase_order_id>",
    "rule_id": "<rule_id for RULE-DSPA-SHORT>",
    "reason_code": "AMOUNT_INCORRECT",
    "claimed_amount": 50.0,
    "computed_amount": 50.0,
    "delta_amount": 0.0,
    "verdict": "PAY_FULL",
    "dispute_status": "ANALYZED",
    "analysis_breakdown": {
      "rule_id": "<rule_id for RULE-DSPA-SHORT>",
      "rule_code": "RULE-DSPA-SHORT",
      "calc_type": "PER_UNIT",
      "violation_family": "SHORTAGE",
      "as_of_date": "2026-06-12",
      "facts": {
        "order_qty": 100,
        "unit_price": 10.0,
        "delivered_qty": 90.0,
        "shortfall_units": 10.0,
        "required_delivery_date": "2026-06-10",
        "actual_delivery_date": null,
        "deadline": null,
        "is_late": null,
        "grace_period_days": 0
      },
      "cap_amount": null,
      "cap_applied": false,
      "claimed_amount": 50.0,
      "computed_amount": 50.0,
      "delta_amount": 0.0
    },
    "analyzed_at": "<timestamp>",
    "resolved_at": null,
    "resolved_by": null,
    "override_verdict": null,
    "override_reason": null,
    "notes": null
  },
  "error": null
}
```

Resolve it -- accepting the engine's own verdict outright:

```bash
curl -X POST http://127.0.0.1:8000/api/v1/penalties/disputes/<dispute_id>/resolve \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"resolved_by": "ops-reviewer"}'
# -> dispute_status "RESOLVED", verdict stays "PAY_FULL"
```

...or, instead, overriding it with a different verdict (requires
`override_reason`; this is an alternative ending for the same dispute, not
a second call on top of the one above -- `resolve` only runs once, from
`ANALYZED`):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/penalties/disputes/<dispute_id>/resolve \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"resolved_by": "ops-reviewer", "override_verdict": "PAY_PARTIAL", "override_reason": "Retailer agreed off-line to split the difference."}'
# -> dispute_status "OVERRIDDEN"; verdict stays "PAY_FULL" (the engine's own
#    computed verdict, left as the audit trail); override_verdict
#    "PAY_PARTIAL" and override_reason record the human's different call
#    alongside it.
```

Read it back:

```bash
curl http://127.0.0.1:8000/api/v1/penalties/disputes/<dispute_id> \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"

curl "http://127.0.0.1:8000/api/v1/penalties/disputes?purchase_order_id=<purchase_order_id>" \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"   # every dispute ever opened on this PO
```

And the summary trigger/poll pair, same shape as §9's (needs
`AZURE_OPENAI_*` configured per §9 -- same enqueue-then-drain, cache, and
`force_regenerate` mechanics, just keyed by `dispute_id` instead of
`purchase_order_id`, and only callable once the dispute is `ANALYZED` or
later):

```bash
curl -X POST http://127.0.0.1:8000/api/v1/penalties/disputes/<dispute_id>/summary \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" -d '{}'

curl http://127.0.0.1:8000/api/v1/penalties/disputes/<dispute_id>/summary \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"
```

There is no CLI script for disputes (unlike projections/mitigations/
delivery-change-requests) -- the API walkthrough above is the only way to
exercise this feature outside the seed data's own scripted scenarios.

### Troubleshooting

#### Open returns `ACTUAL_PENALTY_NOT_FOUND` (404)
`actual_penalty_id` doesn't exist. Confirm it via
`GET /penalties/actual-penalties?purchase_order_id=`.

#### Open returns `ACTIVE_DISPUTE_EXISTS` (409)
That charge already has an `OPEN`/`ANALYZED` dispute -- at most one active
dispute per `actual_penalty` at a time. Check
`GET /penalties/disputes?purchase_order_id=` for the pending one's `id`;
resolve it (or let it reach `RESOLVED`/`OVERRIDDEN`) before opening a
second cycle against the same charge.

#### Any dispute-scoped route returns `DISPUTE_NOT_FOUND` (404)
No `penalty_dispute` row exists for that `dispute_id`. Confirm the id from
the `open` response or from `GET /penalties/disputes?purchase_order_id=`.

#### Analyze returns `DISPUTE_ALREADY_RESOLVED` (422)
The dispute is already `RESOLVED` or `OVERRIDDEN` -- `analyze()` refuses to
recompute underneath a verdict a human has already acted on. It's freely
re-runnable while still `OPEN` or `ANALYZED` (e.g. after correcting the
rule it matched against).

#### Analyze returns `NO_MATCHING_RULE_FOR_DISPUTE` (409)
No `penalty_rule` was effective for the PO's retailer/`violation_type` on
the charge's `invoice_or_deduction_date`. Add or correct the rule (§8),
then re-analyze.

#### Analyze returns `INSUFFICIENT_DATA_FOR_DISPUTE` (409)
The real, final fact this violation family needs was never recorded as of
the charge date -- `delivered_qty` for a `SHORTAGE`-family dispute,
`actual_delivery_date` for a `DELAY`-family one. This is never silently
treated as "confirmed zero"/"confirmed on time" -- post the missing
delivery/shipment fact (§8) then re-analyze.

#### Analyze returns `DISPUTE_CALC_NOT_SUPPORTED` (409)
The effective rule's `calc_type` can't be priced for this violation family
-- today, only a `TIERED` delay rule (tiered pricing is implemented for
shortage rules, banded by shortfall %, not for delay rules).

#### Resolve returns `DISPUTE_NOT_ANALYZED` (422)
The dispute isn't `ANALYZED` yet. Run `POST .../analyze` first.

#### Resolve returns `OVERRIDE_REASON_REQUIRED` (422), or the request never reaches the service at all
`override_verdict` was set without `override_reason` (or vice versa).
`DisputeResolveRequest`'s own Pydantic validator rejects this before the
request reaches the service, surfacing as `422 REQUEST_VALIDATION_ERROR`;
`DisputeResolutionService.resolve` enforces the identical rule server-side (`code=
"OVERRIDE_REASON_REQUIRED"`) as defense in depth, in case a future caller
bypasses schema validation.

#### Summary trigger/poll returns `DISPUTE_NOT_ANALYZED` (409)
`POST`/`GET .../{dispute_id}/summary` refuse a narrative request before a
verdict exists. Despite the identical-looking code string, this one is
raised as a `BusinessRuleError` (`409`), **not** the `ValidationError`
(`422`) `resolve` uses for the same code above -- two different call sites,
two different HTTP statuses for the same `code`. Run `POST .../analyze`
first.

#### A dispute-summary poll reports `status: "FAILED"`
Same cause and mechanics as §9's projection/mitigation summaries -- the
model didn't return valid structured output within the bounded
tool-calling loop, coded `PENALTY_DISPUTE_SUMMARY_UPSTREAM_FAILED`. `POST`
again (or with `force_regenerate: true`), or check the deployment in the
Azure portal.

## 12. Tests

```bash
pytest tests/unit/ -v         # 582 passed, 1 xfailed, in-memory SQLite
pytest tests/unit/services/test_penalty_engine.py -v   # just the pure engine
pytest tests/unit/db/test_migration_parity.py -v    # just the Alembic/ORM parity check
```

`test_penalty_engine.py` (was `test_fine_engine.py`): the pure calc
engine's own classes (`PenaltyRule`, `PenaltyRuleTier`, ...) are now
renamed to the `Penalty*` vocabulary -- see
`app/services/penalties/projection/types.py`'s module docstring.

`tests/integration/test_job_queue_postgres.py` and
`tests/integration/test_cmir_repository_integration.py` (both requiring a
live Postgres) are exercised via `pytest tests/unit/ tests/integration/`
against `DATABASE_URL=postgresql+psycopg://mars:mars@localhost:5433/mars`.

The suite itself never touches Postgres. Re-running the worked examples
against a real Postgres instance is still worth doing after any change to
the engine or the seeding data:

```bash
export DATABASE_URL="postgresql+psycopg://<real-connection-string>"
alembic upgrade head
python scripts/demo/seed_master_data.py   # (with uvicorn running against the same DATABASE_URL)
python scripts/demo/demo_daily_simulation.py
```

## 13. Linting and formatting

```bash
ruff check app/ scripts/ tests/ alembic/
ruff format app/ scripts/ tests/ alembic/
```

## 14. Common tasks, quick reference

| I want to... | Run |
|---|---|
| Start completely fresh (drop and recreate schema) | `alembic downgrade base && alembic upgrade head` |
| Reset the four demo orders to their initial state | Re-run `alembic downgrade base && alembic upgrade head`, then step 6 again |
| Check what rules a retailer has | `curl http://127.0.0.1:8000/api/v1/penalties/rules?retailer_id=<uuid> -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"` |
| See a PO's full projection trend | `curl "http://127.0.0.1:8000/api/v1/penalties/projections?purchase_order_id=<purchase_order_id>" -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"` |
| Run one PO for a backfilled date | `python scripts/ops/run_projection_cli.py --purchase-order-id <uuid> --date 2026-08-05` |
| See the whole system, end to end, in one command | `python scripts/demo/run_end_to_end_demo.py` |
| Get an LLM summary of why a PO's penalty is what it is | `python scripts/demo/demo_penalty_projection_summary.py --purchase-order-id <uuid>` |
| Rank mitigation actions for a PO | Project that date first, then `curl -X POST http://127.0.0.1:8000/api/v1/penalties/mitigations -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" -d '{"purchase_order_id": "<id>", "projection_date": "<date>"}'` (§7) |
| Get an LLM summary of a PO's ranked mitigation options | `python scripts/demo/demo_penalty_mitigation_summary.py --purchase-order-id <uuid>` |
| Repair a database stuck on the old migration chain | No verified repair path today (§4's callout) -- drop and recreate |
| Add a new violation type | Update `SHORTAGE_VIOLATION_TYPES`/`DELAY_VIOLATION_TYPES` in `app/services/penalties/projection/types.py` |
| Add a DB column | `app/models/*.py` + `alembic revision --autogenerate` + update `docs/mars_penalties_erp_schema.sql` + confirm `tests/unit/db/test_migration_parity.py` still passes |
| Run the full nightly batch locally | `python scripts/ops/run_daily_batch.py` (see `docs/DEPLOYMENT.md` §3.5) |
| See why the queue looks stuck | `docs/DEPLOYMENT.md` §3.7 — the SQL to run and what each status means |
| Deploy to Azure | `docs/DEPLOYMENT.md` §6 |

## 15. Troubleshooting

- **`sqlalchemy.exc.OperationalError` / `relation "sales_order" does not
  exist`**: migrations haven't been applied against the `DATABASE_URL`
  the app/script is actually using. Run `alembic upgrade head` with that
  exact `DATABASE_URL` exported first. Easy to hit if a shell without
  `.env` loaded runs a script separately from the one that started
  `uvicorn` -- each process reads its own environment independently.
- **`alembic upgrade head` fails with `Can't locate revision identified
  by 'e4b7c391a052'` or `'43d8ced96170'`**: the database is stranded on
  one of the two pre-squash migration chains, whose head revision file a
  later squash deleted. Alembic aborts before any DDL, so nothing is
  half-applied. See "A database stranded on the pre-squash chain" in step
  4 -- there is no repair script verified against the current five-schema
  chain today; drop and recreate if the rows don't matter.
- **`401` on every call**: the header name is exactly `X-Internal-Api-Key`.
  A **missing** header and a **wrong** key produce byte-identical
  responses, so the body tells you nothing about which of the two you hit:
  `require_internal_api_key` (`app/api/dependencies.py`) raises
  `HTTPException(401, detail="Not authenticated")`, which
  `register_exception_handlers`'s own `HTTPException` handler
  (`app/core/exceptions.py`) reshapes into this project's usual
  `{"success": false, "message": ..., "error": {"code": "HTTP_401", ...}}`
  envelope, same as every other error. Debug it from both ends instead:
  confirm the header is actually being sent, then confirm its value
  matches the `APP_INTERNAL_API_KEY` the *running process* loaded -- not
  merely the first one in `.env` (step 3, on duplicate definitions).
  `curl http://127.0.0.1:8000/api/v1/health` needs no key and separates
  "is the app up" from "is my key right".
- **`404` from `/health`**: the health route sits under the API prefix
  like everything else -- it's `GET /api/v1/health`. It is also the one
  route that needs no API key.
- **`404` `PROJECTION_NOT_FOUND` from `POST /penalties/mitigations`**: no
  `penalty_projection` row exists for the given `projection_id` (or
  `purchase_order_id`+`projection_date` pair). Mitigation reads a persisted
  projection and never computes one, so run `POST /penalties/projections`
  for the date you need, then either pass that `purchase_order_id`+
  `projection_date` directly or take a `projection_id` off its history
  (`GET /penalties/projections?purchase_order_id=`). On a freshly seeded
  database this bites even though the PO has plenty of projection rows --
  the seeded history stops in mid-August 2026, so today's date has none
  yet. See step 7.
- **`409` `NO_MITIGATION_OPTIONS_EXIST` from `POST`/`GET
  /penalties/mitigations/summary`**: no ranked mitigation options exist
  for that PO/date yet -- run `POST /penalties/mitigations` (which itself
  needs a projection first, see above) before asking for a summary of
  them.
- **`409` `NO_ACTIVE_RULES` from a penalty-projection call**: the PO's
  retailer has no active penalty rules yet. Seed master data (step 6) or
  add a rule (step 8) first.
- **`404` `PO_NOT_FOUND` from a purchase-order-scoped route**: the
  `purchase_order_id` doesn't exist -- check `GET /purchase-orders` for
  the real UUID.
- **`POST /admin/simulate-daily-run` returns `500` / `IntegrityError` /
  `duplicate key`**: fixed -- if you still see this, you're on an older
  build. Update; the fact-writing methods are idempotent on their
  natural key now.
- **Calling `simulate-daily-run` more than once gives a different
  AMZ-778501 number for Aug 11 the second time onward** (WMT-100234,
  WMT-100511, and AMZ-780112 are unaffected): expected, not a bug -- see
  `tests/unit/services/test_known_limitations.py`. AMZ-778501 and
  AMZ-780112 share a
  production line with contradictory scripted statuses on their
  overlapping dates; only the *first* `simulate-daily-run` call is
  guaranteed to match the published table for AMZ-778501 -- every call
  after that settles onto a different, but internally consistent,
  number, because AMZ-780112's facts from call 1 are visible the whole
  time AMZ-778501 gets re-projected in call 2 onward. Reset with
  `alembic downgrade base && alembic upgrade head` before re-seeding if
  you need the original numbers back.
- **Port already in use on `uvicorn --reload`**: `uvicorn app.main:app --reload --port 8001`,
  and point scripts at it with `--base-url http://127.0.0.1:8001/api/v1`.
- **`AzureOpenAIConfigError` / the polled job never leaves `FAILED`**:
  one or more of `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT_NAME` isn't
  set -- see step 9. Since generation is now a background job, this
  surfaces as a `FAILED` status on `GET /penalties/projections/{id}?include=summary`
  or the mitigation-option equivalent (both share the same Azure OpenAI
  config), not as an immediate error from the triggering `POST`.
- **A projection- or mitigation-summary poll reports `status: "FAILED"`**:
  the model didn't return valid structured output within the bounded
  tool-calling loop (`ExternalServiceError`, coded
  `PENALTY_PROJECTION_SUMMARY_UPSTREAM_FAILED` /
  `PENALTY_MITIGATION_SUMMARY_UPSTREAM_FAILED` respectively) -- an Azure
  OpenAI-side issue (bad deployment, model overloaded, etc.), not a
  client input error. `POST` again (or with `force_regenerate: true`), or
  check the deployment in the Azure portal.

## 16. Penalty rule extraction

Turns an uploaded retailer agreement into reviewed `penalty_rule` rows.
Full design: `docs/architecture/penalty-rule-extraction.md`. Endpoint
reference: `docs/API.md` "Retailer agreements and rule extraction".
`scripts/seed/seed_agents.py` now also seeds the `penalty_rule_extractor`
run-owner row and the three per-stage prompt rows the adapter reads at
inference time (`penalty_rule_screening`, `penalty_rule_classification`,
`penalty_rule_fact_extraction`), alongside the summary and CMIR/PO-validation
agents it already seeded: no separate seeding step needed beyond §6.

The lifecycle, end to end:

```bash
# 1. Upload the retailer agreement (idempotent: re-posting the same markdown_text
#    returns the existing row, 200, instead of a duplicate):
curl -X POST http://127.0.0.1:8000/api/v1/penalties/retailer-agreements \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"retailer_id": "<retailer_id>", "contract_code": "CT-TGT-2026", "title": "Target Master Agreement 2026", "markdown_text": "..."}'

# 2. Start extraction (returns agent_run_id and workflow_thread_id; runs
#    the LangGraph pipeline through to a human-review interrupt):
curl -X POST http://127.0.0.1:8000/api/v1/penalties/retailer-agreements/<retailer_agreement_id>/extract \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"

# 3. List what is pending review:
curl "http://127.0.0.1:8000/api/v1/penalties/retailer-agreements/<retailer_agreement_id>/extracted-rules?status=PENDING_REVIEW" \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"

# 4. Approve or reject each candidate (<extracted_rule_id> from step 3):
curl -X POST http://127.0.0.1:8000/api/v1/penalties/retailer-agreements/<retailer_agreement_id>/extracted-rules/<extracted_rule_id>/review \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status": "APPROVED"}'

curl -X POST http://127.0.0.1:8000/api/v1/penalties/retailer-agreements/<retailer_agreement_id>/extracted-rules/<extracted_rule_id>/review \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status": "REJECTED", "review_notes": "Not a PO shortage/delay clause."}'

# 5. Resume the run once every rule you care about is decided (workflow_thread_id
#    from step 2's response; expected_updated_at from that thread's current stage,
#    e.g. GET /api/v1/workflow-threads/<workflow_thread_id>). Carries no verdicts of
#    its own -- apply_decisions re-reads step 4's decisions from the database:
curl -X POST http://127.0.0.1:8000/api/v1/workflow-threads/<workflow_thread_id>/decisions \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"decision_type": "RULE_REVIEW_RESUME", "actor": "reviewer@company.com", "expected_updated_at": "<updated_at>"}'

# 6. Publish the approved rules into live penalty_rule rows:
curl -X POST http://127.0.0.1:8000/api/v1/penalties/retailer-agreements/<retailer_agreement_id>/publish \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"

# 7. Read the publication audit (every outcome ever recorded, plus a
#    rejection-reason histogram):
curl http://127.0.0.1:8000/api/v1/penalties/retailer-agreements/<retailer_agreement_id>/publications \
  -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"
```

Approving a rule is necessary but not sufficient for publication: `POST
.../publish` only admits a rule that is both `APPROVED` and
`pricing_readiness = READY`. An approved rule stuck at
`NEEDS_EXTERNAL_FIGURE`/`AWAITING_DATA`/`UNSUPPORTED_SHAPE`/`NOT_A_CHARGE`
is skipped and recorded as `NOT_READY` in the publication audit, not
published, and not an error.

Step 5 (resume) closes out the run's own bookkeeping (the `workflow_thread`
and `agent_run` rows) but is not a publication prerequisite: `POST .../publish`
reads `extracted_penalty_rule.status` directly and never checks whether the
run's `workflow_thread` has completed, so an operator who skips step 5 can
still publish -- the run just stays parked at `waiting_rule_review` instead of
closing.

### Rejection reason codes

Every rule the publisher does not admit gets one row in `rule_publication`
with a `reason_code`. The common ones and what to do about them:

| `reason_code` | Meaning | Operator action |
|---|---|---|
| `NOT_APPROVED` | The rule is still `PENDING_REVIEW`, or was `REJECTED` | Review it (step 4) before the next publication run, if it should be priced |
| `NOT_READY` | `pricing_readiness` isn't `READY` | Read the rule's own `pricing_readiness` value; `NEEDS_EXTERNAL_FIGURE`/`AWAITING_DATA` usually means a fact the contract doesn't state has to be entered by hand instead |
| `NOT_PO_SCOPED` | Neither `po_shortage_flag` nor `po_delay_flag` is set | Expected for a clause outside this engine's scope (liability caps, indemnities); no action needed |
| `UNSUPPORTED_CALC_TYPE` | `calc_type` is `FORMULA_OTHER`, `UNSPECIFIED`, `NON_MONETARY`, or `LIMIT_ONLY` | The engine has no pricing function for this shape; write the rule by hand if it must be priced, or leave it out |
| `NO_RATE_VALUE` | A monetary rule has no `RATE` fact carrying a number | Re-check the source clause; if the rate really is stated, this is an extraction miss worth reporting |
| `MARGINAL_TIERS` | The rule's tiers are `MARGINAL`, and the engine only prices `CLIFF` | Write the rule by hand if `MARGINAL` tiering must be priced |
| `NON_HALF_OPEN_TIERS` / `TIER_BAND_GAP` | The tier bands aren't clean half-open intervals, or gap/overlap | Read `review_notes` and the flagged attribute rows; usually needs a person to re-derive the bands from the clause and enter them by hand |
| `NON_AMOUNT_CAP` | The cap is a rate, duration, or quantity ceiling, not an amount ceiling | Expected; the engine only applies amount caps today |
| `UNSUPPORTED_BASIS` / `UNSUPPORTED_ACCRUAL` | `basis_type` isn't `PO_VALUE`/`UNIT_COST`/`SHORTFALL_UNITS`, or `applies_per` is a per-period accrual the engine can't price | Write the rule by hand if it must be priced |
| `THRESHOLD_OUT_OF_RANGE` | A threshold falls outside `[0, 1]` after unit conversion | Almost always an extraction error (percent vs. fraction); re-extract or correct by hand |
| `EXTERNAL_FIGURE` | The value lives outside the contract (an index, a separately negotiated rate) | Expected; enter the rule by hand once the external figure is known |
| `PERCENT_OF_INVOICE` | No invoice value in the projection snapshot | Needs an engine change, not an operator fix; the fact the rule needs isn't tracked yet |
| `MIXED_CURRENCY` | The contract prices in more than one currency | The engine prices one currency per rule; split the contract's rules by currency and enter the non-primary ones by hand |
| `ALREADY_PUBLISHED` | A `penalty_rule` with this rule's deterministic `rule_code` already exists | Expected on a repeat publish of the same run; no action needed |

`reason_code` is `null` on a `PUBLISHED` outcome. The
`rejection_reason_histogram` on `GET .../publications` counts each code
across every publication run recorded for the retailer agreement, so a large
`NOT_READY` or `UNSUPPORTED_CALC_TYPE` count is a signal to look at the
extraction pipeline's prompts or the engine's coverage, not at any one
rule.
