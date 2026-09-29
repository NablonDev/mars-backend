# Deployment & Operations

How to run the system now that it is no longer "just an API", and how to get
it onto Azure.

**Status of the Azure sections:** the `az` commands below are written against
the current code and config, but they have **not been executed** — no Azure
subscription has been provisioned for this project yet. Treat sections 5–9 as
a build sheet to execute and correct, not as a transcript of something that
already ran. Everything in sections 1–4 (local, Docker, config) **has** been
run and verified.

Related reading:
- `docs/JOB-QUEUE-WALKTHROUGH.md` — a code tour of the queue for someone
  seeing it for the first time.
- `docs/RUNBOOK.md` — the pre-queue API workflows (seeding, projections,
  facts), still current.
- `docs/DOCKER.md` — image internals and build verification.

**Every curl example below needs `-H "X-Internal-Api-Key: $INTERNAL_API_KEY"`
except `/health`** — omitted from most commands here for readability, see
`docs/API.md` "Authentication" for the full contract.

---

## 1. What actually changed

Before this work there was **one** thing to run: `uvicorn app.main:app`. A
projection ran inside the HTTP request that asked for it.

Now there is still one **image**, but three **roles**, chosen by the command
you give the container:

| Role | Command | Lifetime | Triggered by |
|---|---|---|---|
| API | `uvicorn app.main:app --host 0.0.0.0 --port 8000` (image default `CMD`) | Long-running | HTTP |
| Nightly batch | `python scripts/ops/run_daily_batch.py` | Runs to completion, exits | Cron schedule |
| Migration | `alembic upgrade head` | Runs to completion, exits | Manual / release pipeline |

Nothing calls the API to do the batch. The Container Apps Job **runs the
container** with a different command; it does not issue an HTTP request to the
API. The API and the batch are peers that share a database, not client and
server.

### The three new moving parts

1. **`process.job_item`** — a durable ledger row per unit of work, with
   `status`, `attempt_count`, `available_at`, and lease columns
   (`locked_by`/`locked_at`/`heartbeat_at`). This is the source of truth for
   work state under **both** backends, and is shared by both the `penalties`
   and `cmir`/`po_validation` domains (`job_type` on `job_run` selects which).
   It is a normal table; it needs a migration, and it is the thing you query
   when something looks stuck.
2. **`process.job_run`** — one row per batch invocation, for grouping and
   reporting. Deliberately has no `status` column; run status is derived by
   aggregating its items.
3. **Dispatch** — the pluggable part. `JOB_QUEUE_BACKEND=postgres` means
   workers poll `job_item` directly (`SELECT … FOR UPDATE SKIP LOCKED`).
   `JOB_QUEUE_BACKEND=service_bus` means a message naming the row id is sent
   to Service Bus and a consumer is woken by it. **The ledger is identical
   either way** — switching backends does not migrate data, and a redelivered
   message cannot double-execute because the claim still goes through
   `job_item`.

### What did NOT change

- Every pre-existing endpoint behaves the same. `POST /api/v1/penalties/projections`
  is still synchronous and inline.
- `scripts/ops/run_projection_cli.py` still works for single orders and ad-hoc
  runs. It was not modified.
- The penalty-projection engine (`app/services/penalties/projection/`) is untouched.

---

## 2. Prerequisites

| | Local dev | Azure |
|---|---|---|
| Python | 3.12+ | — (in image) |
| Postgres | 18+ (or the SQLite fallback for tests) | Azure Database for PostgreSQL Flexible Server |
| Azure OpenAI | A deployment + key, or summaries fail cleanly | Same, ideally via managed identity later |
| Service Bus | **Not required** — leave `JOB_QUEUE_BACKEND=postgres` | Only if you flip the flag |
| Docker | Optional | Required (ACR build) |

Service Bus genuinely is optional. Nothing in the local loop needs it, and the
`azure-servicebus`/`azure-identity` imports are deferred inside
`app/queue/service_bus.py` specifically so the postgres backend never needs
those packages importable.

---

## 3. Running the whole thing locally

### 3.1 Setup

```bash
cd /path/to/mars
uv sync                      # or: pip install -e .
cp .env.example .env
```

Edit `.env`. The minimum that must be real:

```bash
DATABASE_URL=postgresql+psycopg://mars:mars@localhost:5432/mars
JOB_QUEUE_BACKEND=postgres

# Needed only for the LLM summary half. Without these, projections still
# work and summaries land in FAILED rather than crashing.
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/openai/v1
AZURE_OPENAI_DEPLOYMENT_NAME=<deployment>
```

Note the driver prefix: `postgresql+psycopg://`, not bare `postgresql://`.
psycopg3 is what is pinned.

### 3.2 Migrate

```bash
alembic upgrade head
```

This creates every dedicated schema (`process`, `cmir`, `penalties`,
`langgraph`) plus the shared master/fulfillment tables in `public`,
including `process.job_run`/`process.job_item` and the projection/
mitigation summary fingerprint columns — **there is no code path that
creates them lazily.** An API container started against an un-migrated
database will start fine and then fail on the first `/job-runs` call.

### 3.3 Seed something to work on

```bash
python scripts/demo/seed_master_data.py
```

The batch enqueues one item per **OPEN** order. With zero OPEN orders the
batch is a successful no-op, which looks identical to a broken batch. Seed
first.

### 3.4 Start the API

```bash
uvicorn app.main:app --reload --port 8000
```

`http://localhost:8000/docs` for the OpenAPI UI.

### 3.5 The full batch flow, end to end

This is the part that has no pre-queue equivalent, so it is worth doing once
by hand.

**Option A — one command, does everything:**

```bash
python scripts/ops/run_daily_batch.py
```

That single invocation: takes a Postgres advisory lock (so two of these can
never overlap) → reclaims stale items abandoned by a dead worker → sweeps
stranded PENDING summary rows → enqueues one `ORDER_RUN` item per OPEN order →
drains the queue with a worker pool → prints a summary line → releases the
lock.

**Option B — enqueue and drain separately**, which is what you want when you
are trying to see the queue actually behave like a queue:

```bash
# Terminal 1 — enqueue only, then exit
python scripts/ops/run_daily_batch.py --enqueue-only

# Look at the queue before anything drains it
psql "$DATABASE_URL" -c \
  "select status, count(*) from process.job_item group by status"

# Terminal 2 — drain what's pending
python scripts/ops/run_daily_batch.py --drain-only
```

**Option C — enqueue via the API, drain via the script.** This is the shape
that will run in production, with the enqueue coming from a user action rather
than the schedule:

```bash
curl -X POST localhost:8000/api/v1/job-runs \
  -H 'content-type: application/json' -d '{}'
# → 202 { "job_run_id": "...", "requested_item_count": 4,
#         "dispatch_mode": "postgres",
#         "execution_note": "Enqueued; will be processed by the next scheduled batch drain." }

curl localhost:8000/api/v1/job-runs/<job_run_id>
# → counts by status, total_items, is_complete

python scripts/ops/run_daily_batch.py --drain-only

curl localhost:8000/api/v1/job-runs/<job_run_id>
# → is_complete: true
```

Read that `execution_note` field literally. Under `postgres` a 202 means
"written to the ledger", and **nothing will process it until a drain runs.**
That is the single most common local confusion: the API accepted the batch and
appears to have done nothing. It did exactly what it said.

**Other useful flags:**

```bash
python scripts/ops/run_daily_batch.py --dry-run          # count only, no lock, no writes
python scripts/ops/run_daily_batch.py --date 2026-08-13  # backfill a specific date
python scripts/ops/run_daily_batch.py --concurrency 8    # override worker pool for this run
python scripts/ops/run_daily_batch.py --stacking-mode MAX
python scripts/ops/run_daily_batch.py --fail-on-dead     # CI framing: exit 1 if anything died
```

### 3.6 The on-demand flow

On-demand deliberately does **not** wait for a batch, and does not run
inline in the API process either: `get_or_schedule` enqueues a
`process.job_run`/`job_item` (plus its matching `penalty_job_item_context`
row) and returns immediately with `status=PENDING` -- **nothing processes
it until a worker is run separately.** In local dev that's either
`python scripts/ops/run_daily_batch.py --drain-only` or the standalone
worker container, `docker compose --profile tools run --rm
fines-projection-worker` (§3.8 -- gated behind `profiles: [tools]`, so it
is **not** started by a plain `docker compose up`); in a deployed
environment it's the nightly Container Apps Job (§6.6). Until one of
those actually runs, the row sits at `PENDING` indefinitely with no LLM
call ever made -- that is expected, by-design behavior, not a bug. There
is no `BackgroundTasks` slot and no process-local concurrency governor to
configure here:

The path is `/api/v1/penalties/projections/summary`, and **the body is
required** — it needs at least `purchase_order_id` (the PO's surrogate
UUID, not its business number like `WMT-100234` -- look it up via
`GET /api/v1/purchase-orders` first); omitting the body entirely gets a
`422`, not a default:

```bash
curl -X POST localhost:8000/api/v1/penalties/projections/summary \
  -H 'content-type: application/json' -d '{"purchase_order_id": "<purchase_order_id>"}'
# → 200 with a cached summary, or 202 with status PENDING
```

Optional body fields: `as_of_date` (defaults to today) and
`force_regenerate` (defaults to `false`). This route is idempotent trigger-and-poll
in one: call it again with the same body to check on a `PENDING` job, or poll
via the dedicated `GET /api/v1/penalties/projections/summary?purchase_order_id=&as_of_date=`
read route, or `GET /api/v1/penalties/projections/{projection_id}?include=summary`
once the row's `projection_id` is known. It never 404s on the trigger route for a
missing job -- `get_or_schedule` creates one if none exists. The dedicated `GET`
read route above is a plain `200` with `status: null` if none was ever triggered
for this `purchase_order_id` -- the same "nothing to show yet, not an error"
convention `?include=summary` already uses elsewhere in this API; an unknown
`purchase_order_id` is still a genuine `404 PO_NOT_FOUND`. A genuinely wrong URL
still gets Starlette's own `{"detail":"Not Found"}`, distinguishable from any
app-level error envelope.

### 3.7 Inspecting a stuck queue

```sql
-- What is the queue doing right now?
select status, count(*), min(available_at), max(attempt_count)
from process.job_item group by status;

-- Items a worker claimed and never finished
select id, dedupe_key, locked_by, locked_at, heartbeat_at, attempt_count
from process.job_item
where status = 'RUNNING' and heartbeat_at < now() - interval '5 minutes';

-- Why things died
select last_error_code, count(*)
from process.job_item where status = 'DEAD' group by last_error_code;
```

`dedupe_key` is queue infrastructure, not a domain column -- it's a generic
string the domain layer computes at enqueue time (e.g. `f"{purchase_order_id}:{projection_date}"`
for penalties). Domain-specific identifiers (PO id, email id, ...) live on
each domain's own `job_item_context` extension table
(`penalties.penalty_job_item_context`, `cmir.cmir_job_item_context`), joined
on `job_item_id` when you need to trace a stuck row back to its order.

A `RUNNING` row with a stale heartbeat is not lost. The next run's
`reclaim_stale` (or `--drain-only`) returns it to `PENDING`. That is the
recovery mechanism; there is nothing manual to do unless it keeps happening.

### 3.8 Docker Compose

```bash
docker compose up                                                    # db + api -- does NOT start the worker
docker compose --profile tools run --rm migrate                      # migrations
docker compose --profile tools run --rm fines-projection-worker      # one batch drain
```

`fines-projection-worker` is gated behind `profiles: [tools]` in
`docker-compose.yml`, so a plain `docker compose up` never starts it --
anything enqueued via the API (job-runs, on-demand summaries) sits at
`PENDING` until this is run separately, or in the deployed nightly
Container Apps Job (§6.6).

Compose hardcodes `mars:mars@db:5432/mars` for the app services, so set
`POSTGRES_USER=mars`, `POSTGRES_PASSWORD=mars`, `POSTGRES_DB=mars` in the host
`.env` or the `db` container's healthcheck will never pass.

---

## 4. Configuration reference

Which process reads what. **Anything marked "both" must be set identically on
the API and the batch container** — they are two processes reading one
database, and disagreeing about `JOB_QUEUE_BACKEND` or
`JOB_QUEUE_MAX_ATTEMPTS` produces behaviour that looks like a bug.

Every variable below is a flat, single-underscore env var name — there is no
nested-delimiter scheme. The `app/core/config/` split still groups settings
into nested Python classes (`app`, `database`, `llm`, `service_bus`,
`job_queue`, `email`, `summary`, accessed as `settings.llm.api_key` etc.), but
each group is its own `pydantic-settings` `BaseSettings` subclass whose
fields read these flat names directly via a per-field `validation_alias`
(e.g. `settings.llm.api_key` reads `AZURE_OPENAI_API_KEY`, not `LLM__API_KEY`
or `llm_api_key`). The Azure OpenAI and database variables keep their
historical pre-split names (`AZURE_OPENAI_API_KEY`, `DATABASE_URL`, ...);
every other group just uses its own prefix (`JOB_QUEUE_MAX_ATTEMPTS`,
`SERVICE_BUS_NAMESPACE`, ...).

### Required everywhere

| Variable | Default | Read by | Notes |
|---|---|---|---|
| `DATABASE_URL` | localhost | both | Must use the `postgresql+psycopg://` prefix |
| `APP_ENVIRONMENT` | `development` | both | Set `production` on Azure |
| `APP_DOCS_ENABLED` | `false` | api | Closed by default — it gates `/docs`, `/redoc`, `/openapi.json`. Set `true` explicitly for local dev |
| `APP_INTERNAL_API_KEY` | — (required, no default) | both | Shared-secret gate on every route except `/api/v1/health` (`X-Internal-Api-Key` header). App startup fails if unset, blank, the `.env.example` placeholder, or under 64 characters — generate with `python -c "import secrets; print(secrets.token_hex(32))"`, see `app/core/config/app.py` |
| `APP_LOG_LEVEL` | `INFO` | both | |

### Azure OpenAI

| Variable | Default | Read by | Notes |
|---|---|---|---|
| `AZURE_OPENAI_API_KEY` | `""` | both | Secret. Never in the image — see §8 |
| `AZURE_OPENAI_ENDPOINT` | `""` | both | Full v1 base URL, including the `/openai/v1` suffix |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | `""` | both | Deployment name, not model name |
| `AZURE_OPENAI_TIMEOUT_SECONDS` | `90` | both | Per call |
| `AZURE_OPENAI_MAX_ATTEMPTS` | `3` | both | SDK-level retries. There is no second app-level retry layer on top |

### Queue backend

| Variable | Default | Read by | Notes |
|---|---|---|---|
| `JOB_QUEUE_BACKEND` | `postgres` | both | `postgres` \| `service_bus`. Typed as an enum, so an invalid value fails at **startup**, not at first use |
| `JOB_QUEUE_SERVICE_BUS_NAMESPACE` | `""` | both | FQDN, e.g. `mars-fines.servicebus.windows.net`. Only used when the backend is `service_bus` |
| `JOB_QUEUE_SERVICE_BUS_QUEUE_NAME` | `fine-projection-jobs` | both | |
| `JOB_QUEUE_SERVICE_BUS_MAX_WAIT_SECONDS` | `10` | worker | Receive long-poll window |

There is no Service Bus connection string setting, on purpose. Auth is
`DefaultAzureCredential` only, so there is no Service Bus secret to leak.

### Worker tuning

| Variable | Default | Read by | Notes |
|---|---|---|---|
| `JOB_QUEUE_WORKER_CONCURRENCY` | `5` | worker | Thread pool size. **The primary governor on Azure OpenAI load** |
| `JOB_QUEUE_BATCH_SIZE` | `5` | worker | Items claimed per poll |
| `JOB_QUEUE_MAX_ATTEMPTS` | `5` | both | Retries before DEAD. Stamped onto each row at enqueue time, so changing it affects newly enqueued items only — items already in the queue keep the value they were created with |
| `JOB_QUEUE_BACKOFF_BASE_SECONDS` | `30` | worker | Exponential base |
| `JOB_QUEUE_BACKOFF_CAP_SECONDS` | `1800` | worker | |
| `JOB_QUEUE_BACKOFF_JITTER_SECONDS` | `30` | worker | Prevents synchronised retry waves |
| `JOB_QUEUE_VISIBILITY_TIMEOUT_SECONDS` | `300` | worker | No heartbeat within this → reclaimable |
| `JOB_QUEUE_ITEM_DEADLINE_SECONDS` | `600` | worker | Hard per-item ceiling |
| `JOB_QUEUE_POLL_INTERVAL_SECONDS` | `5` | worker | |
| `JOB_QUEUE_IDLE_POLL_MAX_SECONDS` | `30` | worker | Idle backoff ceiling |
| `AZURE_OPENAI_RATE_LIMIT_BACKOFF_SECONDS` | `60` | both | Shared 429 gate. One thread hitting 429 pauses all of them |
| `DATABASE_POOL_SIZE` | `5` | both | **Raise with worker concurrency** |
| `DATABASE_MAX_OVERFLOW` | `10` | both | |
| `DATABASE_POOL_TIMEOUT` | `30` | both | |

**The coupling that bites:** `JOB_QUEUE_WORKER_CONCURRENCY` threads each want
a DB session. If concurrency exceeds `DATABASE_POOL_SIZE + DATABASE_MAX_OVERFLOW`,
threads block waiting for a connection instead of doing work, and the batch
gets *slower* as you raise concurrency. Keep `DATABASE_POOL_SIZE >=
JOB_QUEUE_WORKER_CONCURRENCY`.

### On-demand and summary reuse

| Variable | Default | Read by | Notes |
|---|---|---|---|
| `SUMMARY_REUSE_ENABLED` | `false` | both | Off until fingerprint data justifies turning it on |
| `SUMMARY_MAX_REUSE_DAYS` | `7` | both | |
| `SUMMARY_PENDING_SWEEP_DAYS` | `3` | worker | Recovery-sweep lookback |
| `SUMMARY_BUSINESS_TIMEZONE` | `UTC` | both | Set to `America/Chicago` for Mars |
| `SUMMARY_DAILY_RUN_TIME` | `01:00` | — | Informational. **Does not schedule anything** — the ACA cron expression does |

`SUMMARY_DAILY_RUN_TIME` is a documentation value, not a scheduler. Changing
it changes nothing about when the batch runs. Change the cron expression on
the Container Apps Job (§6).

---

## 5. Azure: what you are building

```
                       ┌──────────────────────────────┐
                       │ Container Registry (ACR)     │
                       │   mars-fines:<tag>           │  ← one image
                       └──────────────┬───────────────┘
                                      │
              ┌───────────────────────┴───────────────────────┐
              │        Container Apps Environment             │
              │                                               │
              │  ┌─────────────────┐   ┌───────────────────┐  │
              │  │ Container App   │   │ Container Apps Job│  │
              │  │ "mars-fines-api"│   │ "…-nightly-batch" │  │
              │  │ CMD: uvicorn    │   │ CMD: run_daily_…  │  │
              │  │ always on       │   │ cron 0 1 * * *    │  │
              │  └────────┬────────┘   └─────────┬─────────┘  │
              └───────────┼──────────────────────┼────────────┘
                          │                      │
              ┌───────────┴──────────────────────┴────────────┐
              │                                               │
     ┌────────▼─────────┐      ┌──────────────┐      ┌────────▼─────────┐
     │ PostgreSQL       │      │ Azure OpenAI │      │ Service Bus      │
     │ Flexible Server  │      │ deployment   │      │ (phase 2 only)   │
     │ 4 schemas +      │      │              │      │                  │
     │ public           │      │              │      │                  │
     └──────────────────┘      └──────────────┘      └──────────────────┘
```

The 4 dedicated schemas: `process` (shared job/agent/workflow backbone,
used by both domains), `cmir`, `penalties`, and `langgraph` (LangGraph's
own checkpoint tables, not app-managed). `public` holds the shared
master/fulfillment data (unqualified, no dedicated schema of its own).

Resource inventory:

| Resource | Required? | Why |
|---|---|---|
| Resource group | Yes | |
| Container Registry | Yes | Somewhere to push the image |
| PostgreSQL Flexible Server | Yes | The ledger and all domain data |
| Container Apps Environment | Yes | Shared network/logging boundary |
| Container App (API) | Yes | Serves HTTP |
| Container Apps Job (nightly) | Yes | Runs the batch on a schedule |
| Container Apps Job (migration) | Recommended | Run `alembic upgrade head` as a manual job rather than from a laptop |
| Log Analytics workspace | Yes (auto-created) | Where container logs land |
| Azure OpenAI | Yes | Summaries |
| Key Vault | Recommended | Holds the OpenAI key |
| Service Bus namespace + queue | **No** | Only when flipping the flag |

### Variables used throughout

```bash
RG=rg-mars-fines
LOC=eastus
ACR=marsfinesacr                 # must be globally unique, lowercase alnum
ENVNAME=cae-mars-fines
PG=pg-mars-fines                 # must be globally unique
IMAGE_TAG=v0.1.0
```

---

## 6. Azure: build sheet

### 6.1 Resource group and registry

```bash
az group create -n $RG -l $LOC

az acr create -g $RG -n $ACR --sku Basic --admin-enabled false
```

Build in ACR rather than locally — it avoids an amd64/arm64 mismatch if you
are on an Apple Silicon machine, which is the single most common "works
locally, crash-loops in Azure" cause:

```bash
az acr build -r $ACR -t mars-fines:$IMAGE_TAG -t mars-fines:latest .
```

The build context is the repo root. Before the first build, confirm the image
is clean — the Dockerfile uses `COPY . .`, so `.dockerignore` is the only
thing keeping `.env` out of it:

```bash
pytest tests/unit/test_dockerignore.py
```

### 6.2 PostgreSQL

```bash
az postgres flexible-server create \
  -g $RG -n $PG -l $LOC \
  --tier Burstable --sku-name Standard_B1ms \
  --version 16 --storage-size 32 \
  --admin-user marsadmin --admin-password '<strong-password>' \
  --public-access 0.0.0.0    # placeholder rule; tighten below

az postgres flexible-server db create -g $RG -s $PG -d mars
```

`--public-access 0.0.0.0` opens it to Azure services only, which is enough to
get moving but is **not** where you should leave it. For anything beyond a
demo, put the Container Apps Environment on a VNet and use a private endpoint
instead.

Connection string for the app settings:

```
postgresql+psycopg://marsadmin:<password>@$PG.postgres.database.azure.com:5432/mars?sslmode=require
```

Both the `+psycopg` prefix and `sslmode=require` matter. Azure rejects
non-TLS connections.

### 6.3 Container Apps Environment

```bash
az extension add --name containerapp --upgrade
az provider register -n Microsoft.App --wait
az provider register -n Microsoft.OperationalInsights --wait

az containerapp env create -g $RG -n $ENVNAME -l $LOC
```

### 6.4 The API container app

```bash
az containerapp create \
  -g $RG -n mars-fines-api \
  --environment $ENVNAME \
  --image $ACR.azurecr.io/mars-fines:$IMAGE_TAG \
  --registry-server $ACR.azurecr.io \
  --system-assigned \
  --target-port 8000 --ingress external \
  --min-replicas 1 --max-replicas 3 \
  --cpu 1.0 --memory 2.0Gi \
  --secrets \
      db-url="postgresql+psycopg://marsadmin:<pw>@$PG.postgres.database.azure.com:5432/mars?sslmode=require" \
      aoai-key="<azure-openai-key>" \
  --env-vars \
      APP_ENVIRONMENT=production \
      APP_DOCS_ENABLED=false \
      APP_LOG_LEVEL=INFO \
      DATABASE_URL=secretref:db-url \
      AZURE_OPENAI_API_KEY=secretref:aoai-key \
      AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com/openai/v1" \
      AZURE_OPENAI_DEPLOYMENT_NAME="<deployment>" \
      JOB_QUEUE_BACKEND=postgres \
      DATABASE_POOL_SIZE=5 DATABASE_MAX_OVERFLOW=10 \
      SUMMARY_BUSINESS_TIMEZONE=America/Chicago
```

No `--command` — the image's default `CMD` already starts uvicorn.

**`--min-replicas 1`, not 0.** Scale-to-zero saves cost for an MVP, but a
cold replica adds real latency to whichever request wakes it. On-demand
summary generation does **not** run inside this `backend` container at all
(see §3.6) -- it's a queued `process.job_item`, claimed and executed later
by the separate worker (the nightly Container Apps Job, or a manual
`run_daily_batch.py --drain-only`), so scaling `backend` to zero cannot
strand a summary mid-generation. It does mean a user waiting on a
`PENDING` summary sees no progress until that worker actually runs, same as
in local development.

Grant the app pull access to ACR:

```bash
API_MI=$(az containerapp show -g $RG -n mars-fines-api \
          --query identity.principalId -o tsv)
ACR_ID=$(az acr show -g $RG -n $ACR --query id -o tsv)

az role assignment create --assignee $API_MI --role AcrPull --scope $ACR_ID
```

### 6.5 The migration job

Run migrations as their own job so no application container ever races another
to migrate:

```bash
az containerapp job create \
  -g $RG -n mars-fines-migrate \
  --environment $ENVNAME \
  --trigger-type Manual \
  --replica-timeout 600 --replica-retry-limit 0 \
  --image $ACR.azurecr.io/mars-fines:$IMAGE_TAG \
  --registry-server $ACR.azurecr.io \
  --system-assigned \
  --cpu 0.5 --memory 1.0Gi \
  --command "alembic" --args "upgrade,head" \
  --secrets db-url="postgresql+psycopg://..." \
  --env-vars DATABASE_URL=secretref:db-url

az containerapp job start -g $RG -n mars-fines-migrate
```

`--replica-retry-limit 0`: a failed migration should stay failed and visible,
not be retried automatically.

Run this **before** the first nightly batch. Alembic is the only thing that
creates `job_run`/`job_item`.

### 6.6 The nightly batch job

```bash
az containerapp job create \
  -g $RG -n mars-fines-nightly-batch \
  --environment $ENVNAME \
  --trigger-type Schedule \
  --cron-expression "0 6 * * *" \
  --replica-timeout 5400 \
  --replica-retry-limit 1 \
  --replica-completion-count 1 \
  --parallelism 1 \
  --image $ACR.azurecr.io/mars-fines:$IMAGE_TAG \
  --registry-server $ACR.azurecr.io \
  --system-assigned \
  --cpu 2.0 --memory 4.0Gi \
  --command "python" --args "scripts/ops/run_daily_batch.py" \
  --secrets db-url="postgresql+psycopg://..." aoai-key="<key>" \
  --env-vars \
      APP_ENVIRONMENT=production APP_LOG_LEVEL=INFO \
      DATABASE_URL=secretref:db-url \
      AZURE_OPENAI_API_KEY=secretref:aoai-key \
      AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.com/openai/v1" \
      AZURE_OPENAI_DEPLOYMENT_NAME="<deployment>" \
      JOB_QUEUE_BACKEND=postgres \
      JOB_QUEUE_WORKER_CONCURRENCY=8 \
      DATABASE_POOL_SIZE=10 DATABASE_MAX_OVERFLOW=10 \
      SUMMARY_BUSINESS_TIMEZONE=America/Chicago
```

Four parameters here are load-bearing and worth understanding rather than
copying:

**`--cron-expression "0 6 * * *"` — this is UTC, and Container Apps Jobs
have no timezone setting.** The business requirement is 01:00
America/Chicago, and Chicago is not a fixed offset:

| | Offset | 01:00 local = |
|---|---|---|
| CDT (Mar–Nov) | UTC−5 | **06:00 UTC** |
| CST (Nov–Mar) | UTC−6 | **07:00 UTC** |

So `0 6 * * *` is exactly right for eight months of the year and fires at
midnight Chicago time for the other four. There is no cron expression that
covers both. Three options, in order of preference:

1. Accept the drift. The batch runs at 00:00 or 01:00 local; for an
   overnight job nobody is waiting on, this is a non-issue. **Pick this
   unless someone objects.**
2. Change the expression twice a year at the DST boundaries. Reliable, but
   only if someone remembers.
3. Schedule at a time where an hour of drift is obviously harmless (e.g.
   `0 8 * * *`, which is 02:00/03:00 local) and stop thinking about it.

What you must not do is assume the platform handles it, or write
`SUMMARY_DAILY_RUN_TIME=01:00` in the env vars and believe that scheduled
anything. It does not — nothing in the code reads that setting.

**`--replica-timeout 5400`** (90 min) is the hard kill. Size it from measured
latency, not from this default: `orders ÷ concurrency × seconds-per-order ×
1.5` for headroom. With 5,000 orders, concurrency 8, and 6s per order, that is
~63 minutes, so 90 is reasonable — but the per-order figure is a guess until
you measure it in staging. If the replica is killed mid-run, in-flight items
are left `RUNNING` and the next run reclaims them; nothing is lost, but the
run reports as failed.

**`--parallelism 1`.** One replica per execution. The advisory lock inside the
script already prevents overlapping runs, but there is no reason to rely on
the belt when the braces are free.

**`--replica-retry-limit 1`.** The script exits 0 for "completed, some items
DEAD" precisely so ACA does not re-run the entire batch over individual item
failures. Non-zero means genuine infrastructure failure — unreachable DB, bad
config — and those are worth one retry.

**`--cpu 2.0`** is higher than the API's because this process runs a thread
pool; the API mostly waits on I/O.

### 6.7 On-demand batch trigger from outside

If someone needs to kick a full batch manually without waiting for the
schedule:

```bash
az containerapp job start -g $RG -n mars-fines-nightly-batch
```

That is safe at any time — the advisory lock makes a concurrent invocation
exit 0 immediately rather than double-processing.

The API's `POST /api/v1/job-runs` is *not* a substitute under the postgres
backend: it enqueues but does not drain. Under `service_bus` it does wake a
consumer. This asymmetry is exactly what the `execution_note` field in the
response reports.

---

## 7. Optional: switching to Service Bus

Do this when there is a reason — a real inbound connector pushing work, or a
second service that needs to enqueue without database access. Under today's
volume (5,000 items/day ≈ 0.06 jobs/sec) the postgres backend is not the
constraint; Azure OpenAI throughput is.

### 7.1 Provision

```bash
SB=sb-mars-fines            # globally unique

az servicebus namespace create -g $RG -n $SB -l $LOC --sku Standard

az servicebus queue create -g $RG --namespace-name $SB \
  -n fine-projection-jobs \
  --max-delivery-count 5 \
  --lock-duration PT5M \
  --default-message-time-to-live P1D \
  --enable-dead-lettering-on-message-expiration true
```

Three settings to get right, and one trap:

- **`--lock-duration PT5M`** must be ≥ how long one item takes. Five minutes
  matches `JOB_QUEUE_VISIBILITY_TIMEOUT_SECONDS=300`. Keep them aligned; if
  the Service Bus lock expires first you get redelivery while the worker is
  still running, and the ledger claim is what saves you rather than the
  design working as intended.
- **`--max-delivery-count 5`** should match `JOB_QUEUE_MAX_ATTEMPTS`.
- **Sessions: leave them OFF.** The reference architecture from the mail-
  processing service in this engagement enables sessions. Do not copy that
  here. Sessions serialise delivery within a session id, and these 5,000
  orders are mutually independent — enabling sessions would turn a
  ~2-hour batch into a strictly sequential one.

### 7.2 RBAC

No connection strings. The code uses `DefaultAzureCredential`, so each
identity needs a role:

```bash
SB_ID=$(az servicebus namespace show -g $RG -n $SB --query id -o tsv)

API_MI=$(az containerapp show -g $RG -n mars-fines-api --query identity.principalId -o tsv)
JOB_MI=$(az containerapp job show -g $RG -n mars-fines-nightly-batch --query identity.principalId -o tsv)

# API enqueues
az role assignment create --assignee $API_MI \
  --role "Azure Service Bus Data Sender" --scope $SB_ID

# Batch both sends and receives
az role assignment create --assignee $JOB_MI \
  --role "Azure Service Bus Data Sender" --scope $SB_ID
az role assignment create --assignee $JOB_MI \
  --role "Azure Service Bus Data Receiver" --scope $SB_ID
```

Role assignments take a few minutes to propagate. A `403` immediately after
creating them usually means "wait", not "wrong".

### 7.3 Flip the flag

On **both** the API and the job:

```bash
az containerapp update -g $RG -n mars-fines-api \
  --set-env-vars JOB_QUEUE_BACKEND=service_bus \
                 JOB_QUEUE_SERVICE_BUS_NAMESPACE=$SB.servicebus.windows.net \
                 JOB_QUEUE_SERVICE_BUS_QUEUE_NAME=fine-projection-jobs

az containerapp job update -g $RG -n mars-fines-nightly-batch \
  --set-env-vars JOB_QUEUE_BACKEND=service_bus \
                 JOB_QUEUE_SERVICE_BUS_NAMESPACE=$SB.servicebus.windows.net \
                 JOB_QUEUE_SERVICE_BUS_QUEUE_NAME=fine-projection-jobs
```

No data migration and no downtime window. `job_item` is unchanged; only how a
worker learns a row exists changes. Rolling back is the same command with
`postgres`. Any messages already in the queue when you roll back become
orphaned, but their `job_item` rows are still `PENDING` and the next drain
picks them up — which is the whole point of keeping one ledger.

### 7.4 The event-driven variant

Once on Service Bus you can replace the cron job with a KEDA-scaled job that
starts a replica when messages arrive:

```bash
az containerapp job create \
  -g $RG -n mars-fines-worker \
  --environment $ENVNAME \
  --trigger-type Event \
  --polling-interval 30 \
  --min-executions 0 --max-executions 5 \
  --scale-rule-name sb-queue \
  --scale-rule-type azure-servicebus \
  --scale-rule-metadata queueName=fine-projection-jobs \
                        namespace=$SB messageCount=20 \
  --scale-rule-identity system \
  --image $ACR.azurecr.io/mars-fines:$IMAGE_TAG \
  --command "python" --args "scripts/ops/run_daily_batch.py,--drain-only" \
  ...
```

Keep the scheduled job as well, at least initially — it is what enqueues the
day's work and runs the recovery sweep. The event-driven job only drains.

---

## 8. Secrets

The `--secrets` flag above stores values in Container Apps' own secret store,
which is adequate but keeps the OpenAI key in the app definition. Key Vault
references are better:

```bash
az keyvault create -g $RG -n kv-mars-fines -l $LOC --enable-rbac-authorization
az keyvault secret set --vault-name kv-mars-fines -n azure-openai-key --value '<key>'

KV_ID=$(az keyvault show -g $RG -n kv-mars-fines --query id -o tsv)
az role assignment create --assignee $API_MI \
  --role "Key Vault Secrets User" --scope $KV_ID

az containerapp secret set -g $RG -n mars-fines-api \
  --secrets aoai-key=keyvaultref:https://kv-mars-fines.vault.azure.net/secrets/azure-openai-key,identityref:system
```

Rules that hold regardless:

- The image contains no secrets. `.dockerignore` excludes `.env`, and
  `tests/unit/test_dockerignore.py` fails the build if that regresses.
- No Service Bus connection string exists anywhere — managed identity only.
- The Postgres password is the weakest link in the current setup. Moving to
  Entra ID authentication for Postgres removes it entirely and is the obvious
  next hardening step.

---

## 9. Post-deploy verification

Run these in order the first time. Each one fails distinctly.

```bash
# 0. Every call below except /health needs this -- a 401 here means the key
#    is wrong/missing, not that the thing being tested actually failed:
AUTH_HEADER="X-Internal-Api-Key: $INTERNAL_API_KEY"

# 1. API is up (no key needed)
curl -f https://<app-fqdn>/api/v1/health

# 2. Migrations actually ran — this 404s cleanly on a migrated DB and
#    500s on an un-migrated one
curl -i https://<app-fqdn>/api/v1/job-runs/00000000-0000-0000-0000-000000000000 \
     -H "$AUTH_HEADER"

# 3. Enqueue works
curl -X POST https://<app-fqdn>/api/v1/job-runs \
     -H "$AUTH_HEADER" -H 'content-type: application/json' -d '{}'

# 4. The batch job runs
az containerapp job start -g $RG -n mars-fines-nightly-batch
az containerapp job execution list -g $RG -n mars-fines-nightly-batch -o table

# 5. Its logs
az containerapp job logs show -g $RG -n mars-fines-nightly-batch \
   --container mars-fines-nightly-batch --follow

# 6. The run reconciles
curl https://<app-fqdn>/api/v1/job-runs/<job_run_id> -H "$AUTH_HEADER"   # is_complete: true
```

### Symptom → cause

| Symptom | Likely cause |
|---|---|
| API starts, `/job-runs/*` 500s | Migrations not run |
| Batch exits 0 instantly, `enqueued_count: 0` | No OPEN orders, or the lock was already held by an overlapping run |
| Everything DEAD with an LLM error code | `AZURE_OPENAI_*` wrong, or the deployment name is a model name |
| Items stuck `RUNNING` | Replica killed by `--replica-timeout`. Next run reclaims them; raise the timeout |
| Batch slower as concurrency rises | `DATABASE_POOL_SIZE` < `JOB_QUEUE_WORKER_CONCURRENCY` |
| Repeated 429s in logs | Azure OpenAI quota. Lower concurrency or raise the deployment's TPM |
| Service Bus `403` right after RBAC setup | Role assignment still propagating |
| Container crash-loops with `exec format error` | Image built for arm64. Rebuild with `az acr build` |

### What to watch after go-live

Nothing here is provisioned yet — these are the alerts worth creating:

- Nightly job execution **Failed**, or did not run at all within the expected
  window.
- `count(*) where status='DEAD'` above a threshold for the day.
- `count(*) where status='PENDING' and available_at < now() - interval '1 hour'`
  — work nobody is draining.
- Service Bus dead-letter queue depth > 0 (once on that backend).
- Batch duration trending toward `--replica-timeout`.

---

## 10. Still outstanding

Infrastructure, not code. Nothing in the repo blocks any of it:

1. Provision the resource group, ACR, and push the first image.
2. Provision Postgres and run the migration job.
3. Create the Container Apps Environment, API app, and nightly job.
4. **Measure real per-order latency in staging** and re-derive
   `--replica-timeout` and `JOB_QUEUE_WORKER_CONCURRENCY` from it. Every
   sizing number in this document is arithmetic on an assumed 6s per order.
5. Decide the DST behaviour for the cron expression.
6. Create the alerts in §9.
7. Service Bus, only when a real connector needs it.
