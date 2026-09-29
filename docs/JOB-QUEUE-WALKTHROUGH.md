# Job queue walkthrough: how the code actually works

This is a code tour, not an architecture argument (why Postgres, why not
Celery/Kafka/Redis, why Service Bus is only a dispatch backend) — it just
points at the files and functions that implement it.

Audience: you know Python and FastAPI, you don't know queue terminology, and
you weren't around when this landed.

---

## 1. The mental model, in three sentences

A **job** here is one row in the `process.job_item` Postgres table --
shared infrastructure used by both the `penalties` and `cmir`/`po_validation`
domains, discriminated by `item_type`. A batch is one `process.job_run` row
plus N `job_item` rows under it.

We use a database table as a queue instead of a message broker because the
volume is tiny (about 5,000 jobs a night) and Postgres can already do "hand
out a row exactly once to one caller" via `SELECT ... FOR UPDATE SKIP LOCKED`.

The one idea to hold onto everywhere below: **`job_item` in Postgres is
always the durable record of what work exists and what state it's in. The
queue *backend* (Postgres polling, or Azure Service Bus) only handles
*dispatch* — telling a worker "item X is ready, go look now."** Status,
retries, and history all live in the row, never in a message. That's why
swapping `JOB_QUEUE_BACKEND=postgres` for `service_bus` changes nothing
about correctness — only how a worker finds out there's work to do.

---

## 2. File-by-file tour

### `app/queue/` — the dispatch seam

| File | What it's responsible for |
|---|---|
| `interfaces.py` | Two `Protocol`s: `JobDispatcher` (the write side — `dispatch`, `close`) and `JobSource` (the read/execute side — `claim_batch`, `heartbeat`, `ack`, `nack`, `dead_letter`, `release`, `reclaim_stale`). Nothing outside `app/queue/` may import a concrete backend class; everything else depends only on these two Protocols. |
| `types.py` | `ClaimedJob` — a plain dataclass carrying everything a worker needs about one claimed row (`job_item_id`, `job_run_id`, `item_type`, `dedupe_key`, attempt counts, an opaque `receipt` for Service Bus's message handle). The domain-shaped business key it used to carry directly (`order_id`/`projection_date`/`task_type`) is gone -- it moved off `process.job_item` onto each domain's own `job_item_context` extension table; a caller needing those fields looks up the matching context row itself, keyed on `job_item_id`. No SQLAlchemy or transport-SDK imports, so both backends can share it. Named `types.py`, not `models.py`, because in this repo `models` means SQLAlchemy ORM tables and `schemas` means Pydantic DTOs — `ClaimedJob` is neither. |
| `postgres.py` | `PostgresJobQueue` — implements both Protocols by wrapping `JobQueueRepository` calls in short-lived sessions. `dispatch` here is nearly a no-op (see §3). |
| `service_bus.py` | `ServiceBusJobQueue` — implements both Protocols by sending/receiving Azure Service Bus messages whose body is just a `job_item_id`, and still calling `JobQueueRepository.claim_batch` for the actual claim. PEEK_LOCK vs RECEIVE_AND_DELETE is a short comment at the top of this file. |
| `factory.py` | `build_job_queue(settings, database)` — picks `PostgresJobQueue` or `ServiceBusJobQueue` based on `settings.job_queue.backend`. The only place a concrete backend class is named outside `app/queue/` itself. |

### `app/repositories/process/job_queue.py` — the actual SQL

One class, `JobQueueRepository`. This is where the "atomic claim" really
happens (`claim_batch`, using `FOR UPDATE SKIP LOCKED` on Postgres, with a
single-threaded SQLite fallback for tests — see the file's module docstring
for the dialect-branching pattern used throughout). Other functions worth
knowing: `enqueue` / `enqueue_many` (idempotent inserts, keyed on
`dedupe_key` — a request already in flight returns the existing row
instead of a duplicate), `mark_succeeded` / `mark_failed` / `mark_dead` (the three
terminal-or-retry transitions), `reclaim_stale` (the visibility-timeout
sweep), and `find_stranded_pending_projection_summaries` / `find_stranded_pending_mitigation_summaries`
(used only by `app/workers/penalty_projection.py` / `app/workers/penalty_mitigation.py`, see below).

### `app/workers/` — claims and runs the work

| File | What it's responsible for |
|---|---|
| `loop.py` | `process_jobs` — the concurrent claim/execute/settle loop that powers the nightly batch (§4 walks through it end to end). Domain-agnostic: also `classify_failure`, `compute_backoff_seconds`, and `WorkerLoopSummary`. (`SweepResult`, the shared return type for both domains' sweep functions, lives in `app/queue/types.py` instead — putting it in `loop.py` would create an import cycle, since `loop.py` imports `dispatch.py`, which imports both domain modules.) |
| `dispatch.py` | `execute_job` — the dispatch table for a single claimed item: routes `ORDER_RUN`/`PROJECTION_SUMMARY_REGEN` to `penalty_projection.py`, `MITIGATION_RUN`/`MITIGATION_SUMMARY_REGEN` to `penalty_mitigation.py` (was `fine_projection.py`/`fine_mitigation.py`), and `PENALTY_FULL_RUN` to `penalty_full_run.py`. `ClaimedJob.task_type` was renamed `item_type`, matching `process.job_item.item_type`. Everything in `loop.py` exists to call this safely and handle what it raises. |
| `penalty_projection.py` | `run_projection`/`run_summary` (the actual per-item work for `ORDER_RUN`/`PROJECTION_SUMMARY_REGEN`), `enqueue_daily_run` (builds one `job_run` plus one `ORDER_RUN` `job_item` per OPEN purchase order), and `sweep_stranded_pending_projection_summaries` — a narrow recovery job for one specific durability gap (see §4.3), not a general retry mechanism. Also `sweep_expired_po_delivery_change_requests` (`docs/RUNBOOK.md` §10). |
| `penalty_mitigation.py` | `run_mitigation`/`run_mitigation_summary` and `sweep_stranded_pending_mitigation_summaries` — the mitigation-summary mirror of `penalty_projection.py`'s per-item work and sweep; mitigation has no nightly-enqueue equivalent (on-demand only). |
| `penalty_full_run.py` | `run_full_run` — dispatched for `PENALTY_FULL_RUN` (from `job_type=PENALTY_FULL_RUN_BATCH`): reads the `steps` requested for one job item off `process.job_item.metadata` and calls only those of `run_projection`/`run_summary`/`run_mitigation`/`run_mitigation_summary`, always in that fixed dependency order. Adds no domain logic of its own — pure sequencing on top of the four functions above, mirroring `ORDER_RUN`'s existing `run_projection` → `run_summary` chain in `dispatch.py`. |

### `scripts/ops/run_daily_batch.py`

The CLI entry point wired into the nightly Azure Container Apps Job. Takes
an advisory lock, reclaims stale rows, runs the sweep, enqueues today's
work, drains the queue, and picks its own exit code deliberately (§4.1 and
§7).

### `app/api/v1/penalties/batches.py`

Read-only(-ish) observability, shared by both domains: `POST /api/v1/job-runs` (dispatch a batch job,
`job_type` selects the domain), `GET /api/v1/job-runs/{job_run_id}`
(status/counts), `GET /api/v1/job-runs/{job_run_id}/items` (per-item
detail, with `last_error` never returned raw — only a fixed message built
from `last_error_code`).

---

## 3. Vocabulary, in the order things happen to a job

| Term | Lives at | Meaning |
|---|---|---|
| `create_run` | `JobQueueRepository` | Create one `job_run` row — the "batch" a set of items belongs to. |
| `enqueue` / `enqueue_many` | `JobQueueRepository` | Insert one (or many) `job_item` rows, PENDING, idempotently — a duplicate request with the same `dedupe_key` reuses the existing in-flight row instead of creating a second one. |
| `set_requested_item_count` | `JobQueueRepository` | Correct a run's forecast count after `enqueue`/`enqueue_many` skipped some already-in-flight rows, so "is this run complete" can reconcile later. |
| `dispatch` | `JobDispatcher` (both backends) | Tell a worker "this item is ready." Nearly a no-op on Postgres (polling finds the row regardless); a real message send on Service Bus. |
| `claim_batch` | `JobSource` (both backends) | A worker atomically takes ownership of up to N PENDING rows, flipping them to RUNNING. |
| `heartbeat` | `JobSource` | A worker periodically proves it's still alive on an item it holds, so a crashed worker's items can be told apart from a slow-but-alive one. |
| `ack` | `JobSource` | Success. Terminal — the item becomes SUCCEEDED. |
| `nack` | `JobSource` | Retryable failure. Back to PENDING with a backoff delay, unless attempts are exhausted (then DEAD). |
| `dead_letter` | `JobSource` | Non-retryable failure. DEAD immediately, regardless of attempts remaining. |
| `release` | `JobSource` | A worker voluntarily gives an item back (graceful shutdown) without spending an attempt. |
| `reclaim_stale` | `JobSource` | The system takes an item back from a worker whose heartbeat went silent. |

### Why there's no `dequeue`

This is a **lease** queue, not a removal queue. Claiming a row (`claim_batch`)
doesn't delete it — it flips `status` to `RUNNING`, and the row can come
back to `PENDING` three different ways (`nack`, `release`,
`reclaim_stale`). A method called `dequeue` would imply the row is gone,
which is never true here. This is also why real brokers like SQS and
Service Bus call the operation "receive," not "dequeue" — same reasoning.

### `release` vs `reclaim_stale`

Both end with the same outcome: the row is back at PENDING, available for
another claim. The difference is *who* decided that, and *why*:

- **`release`** — the worker itself gives the item up on purpose. This
  happens during a graceful shutdown (SIGTERM): a worker that's about to
  exit hands back anything still in flight rather than let it sit RUNNING
  with a stale heartbeat until someone else notices. No attempt is
  consumed — the worker didn't fail the item, it just ran out of time to
  finish.
- **`reclaim_stale`** — nobody asked. The *system* notices a row has been
  RUNNING with no heartbeat for longer than
  `job_queue_visibility_timeout_seconds`, and takes it back on the
  assumption that whatever worker held it is dead or hung.

Self-initiated vs. externally enforced — that's the whole distinction.

### `ack` / `nack` / `dead_letter`

Standard message-queue vocabulary, not invented here: **ack** = succeeded,
**nack** = failed but worth retrying, **dead_letter** = failed permanently,
don't retry. `classify_failure` in `app/workers/loop.py` is the function
that decides which of the three a given exception maps to (domain-agnostic
— it classifies by exception type/category (`NotFoundError`, `ValidationError`,
`BusinessRuleError` → DEAD_LETTER; everything else → NACK), not by which
penalty sub-domain raised it).

---

## 4. Three traced flows

### 4.1 Nightly batch

`scripts/ops/run_daily_batch.py::main`:

1. Takes a Postgres advisory lock (`JobQueueRepository.try_advisory_lock`,
   key `837_401_559`). If another run already holds it, log and exit 0 —
   not an error, just "still draining from before."
2. `job_source.reclaim_stale(...)` — reset anything left RUNNING by a
   worker that died mid-item.
3. `sweep_stranded_pending_projection_summaries(...)` (`app/workers/penalty_projection.py`) — a
   narrower recovery pass, see §4.3. `sweep_stranded_pending_mitigation_summaries(...)`
   (`app/workers/penalty_mitigation.py`) does the same for the mitigation-summary ledger.
4. `enqueue_daily_run(...)` (`app/workers/penalty_projection.py`) — resolve "today" in
   `settings.summary.business_timezone`, create one `job_run`
   (`SCHEDULED_DAILY`), bulk-insert one `ORDER_RUN` `job_item` per OPEN
   purchase order via `JobQueueRepository.enqueue_many`, then `dispatch()` each new
   item id one at a time.
5. `process_jobs(...)` (`app/workers/loop.py`) — claims batches, submits
   each to a `ThreadPoolExecutor`, and loops until the queue is empty
   (`mode="drain"`). Each claimed item runs through `_process_job`, which
   calls `dispatch.execute_job` under a per-item deadline, classifies
   whatever it raises via `classify_failure`, and settles it
   (`ack`/`nack`/`dead_letter`) accordingly.
6. Release the advisory lock; print a summary line; pick an exit code
   (a DEAD item does not fail the run).

Per-item execute path (`dispatch.execute_job`): for `ORDER_RUN`, runs the
projection (`ProjectionService.run_for_order`) then the summary
(`ProjectionSummaryService.get_or_schedule` + `run_generation`), in a fresh DB
session for each phase — never a session held open across the LLM call.
For `PROJECTION_SUMMARY_REGEN`, only the summary half runs (it assumes a projection
already exists). Both live in `app/workers/penalty_projection.py`; the mirror
path for `MITIGATION_SUMMARY_REGEN` lives in `app/workers/penalty_mitigation.py`.

### 4.2 On-demand summary — `POST /penalties/projections/summary`

`app/api/v1/penalties/projections.py::trigger_penalty_projection_summary`,
via `ProjectionSummaryService.get_or_schedule` (`app/services/penalties/_summary_base.py`,
shared with the mitigation-summary side):

1. Cache hit (or a reusable prior summary, when `SUMMARY_REUSE_ENABLED`)?
   Return the READY summary synchronously (200).
2. Cache miss? Write a PENDING `penalty_summary` ledger row (a *different*
   table from `job_item` — this one holds the generated narrative, not
   queue state), then `_enqueue_regeneration_job(...)` creates a
   `process.job_run`/`job_item` (with a matching `penalty_job_item_context`
   row in the same transaction) for a worker to later pick up, and commits.
3. Returns 202 PENDING immediately.

**This on-demand path no longer runs the LLM call itself.** Earlier
versions of this flow handed the actual work to FastAPI's
`BackgroundTasks` right after enqueuing the `job_item` (`background_tasks.add_task(...)`,
gated by a process-local semaphore/`RateLimitGate`). That inline-execution
step, and the semaphore/gate around it, do not exist in the current
codebase — the route's own comment says so
(`app/api/v1/penalties/projections.py::trigger_penalty_projection_summary`:
"No worker in this pass actually drains that queue"). **Practically, a 202
PENDING here only ever leaves PENDING once a worker actually claims and
runs that `job_item`** -- today that means `scripts/ops/run_daily_batch.py`
(or `run_projection_cli.py --with-summary`), not the API process itself.
`SUMMARY_ON_DEMAND_MAX_CONCURRENT_SUMMARIES`/`SUMMARY_ON_DEMAND_ACQUIRE_TIMEOUT_SECONDS`
were the same class of dead-config gap flagged for
`JOB_QUEUE_MAX_ATTEMPTS` previously (declared in `app/core/config/summary.py`
but read by nothing) — removed rather than left sitting there once this
was confirmed; `get_or_schedule` has no concurrency-bounded inline path to
configure at all.

`POST /penalties/mitigations/summary`
(`app/api/v1/penalties/mitigations.py`) does the same thing for its
summary half, via `MitigationSummaryService.get_or_schedule`.

### 4.3 What happens when something fails

Inside `_process_job` (`app/workers/loop.py`), any exception from
`dispatch.execute_job` goes through `classify_failure`:

- `NotFoundError`, `ValidationError`, or `BusinessRuleError` (the collapsed
  `AppError` categories -- e.g. `PO_NOT_FOUND`, `NO_ACTIVE_RULES`,
  `NO_PROJECTION_EXISTS`, `INVALID_AS_OF_DATE`), or a bare `ValueError` not
  wrapped in an `AppError` (bad/unknown `item_type`)
  → **DEAD_LETTER**. Retrying would fail identically every time.
- Everything else (`ExternalServiceError`, `OperationalError`/`DBAPIError`,
  an exceeded item deadline, or any unclassified exception) → **NACK**.

A NACK'd item goes back to PENDING with a delay computed by
`compute_backoff_seconds` — exponential, capped, with jitter — *unless*
`attempt_count` has already reached `max_attempts` (default 5 on the
`job_item` row), in which case `JobQueueRepository.mark_failed` sends it
DEAD instead. A DEAD_LETTER'd item skips that budget entirely and goes DEAD
on the very first occurrence.

The separate two-commit durability gap that `app/workers/penalty_projection.py`'s
sweep exists for is narrower than ordinary retry: `ProjectionSummaryService.get_or_schedule`
writes its PENDING `penalty_summary` row and commits *before* the caller
gets a chance to write the matching `job_item` in a second commit. If the
process dies in that window, the ledger row is stuck PENDING with nothing
watching it — no `job_item` ever existed to retry or dead-letter. The
sweep finds exactly that: a PENDING `penalty_summary` row with **no**
`job_item` at all (any status, any item_type), and enqueues one.

---

## 5. What changed versus the last commit (`d2047ce`)

New:

- `app/queue/` (the whole dispatch seam), `app/workers/` (renamed from
  the old `app/worker/`, and expanded — `loop.py`, `dispatch.py`,
  `fine_projection.py`, `fine_mitigation.py`), `app/repositories/job_queue.py`, `app/models/job_queue.py`
  (`JobRun`/`JobItem`), `app/models/enums.py` (centralizes `JobItemStatus`,
  `JobTaskType`, `JobRunType`, `SummaryStatus`).
- `app/api/v1/batches.py` + `app/schemas/batches.py` — new observability
  endpoints.
- `app/core/rate_limit.py` — the shared 429-backoff gate, used by both the
  batch worker and the on-demand path.
- `scripts/ops/run_daily_batch.py` — new nightly CLI entry point,
  alongside (not replacing) `run_projection_cli.py`.
- Two new Alembic migrations creating `job_run`/`job_item` and deriving
  their CHECK constraints from the enum vocabulary.
- Test coverage: `tests/unit/queue/`, `tests/unit/workers/`,
  `tests/integration/test_job_queue_postgres.py`,
  `tests/unit/api/test_api_batches.py`,
  `tests/unit/api/test_api_fine_projection_summary_job_queue.py`,
  `tests/unit/api/test_on_demand_summary_concurrency.py`,
  `tests/unit/core/test_rate_limit.py`, and more.

Existing files that gained something:

- `app/api/v1/fine_projection/summaries.py` and `app/api/v1/fine_projection/projections.py` — both
  on-demand endpoints (`POST /orders/{id}/projection-summary`, `POST
  /orders/{id}/run`) now write a durable `job_item` via
  `enqueue_and_dispatch_summary_job` before handing work to
  `BackgroundTasks`, instead of just firing the background task with no
  durable record.
- `app/api/dependencies.py` — gained `get_job_queue`, `get_job_dispatcher`,
  `get_job_queue_repository`, `enqueue_and_dispatch_summary_job`; the
  returned callable from `get_fine_projection_summary_job_runner` now claims and
  settles a `job_item` when one is passed, and is gated by a
  process-local semaphore (`ON_DEMAND_MAX_CONCURRENT_SUMMARIES`) plus its
  own `RateLimitGate` — previously it just ran generation with no
  concurrency bound at all.
- `app/main.py` — the lifespan now builds one `(dispatcher, source)` pair
  at startup and closes the dispatcher at shutdown.
- `app/api/router.py` — mounts the new `batches` router.
- `app/core/config.py` — new settings: `job_queue_backend`, worker
  concurrency/batch-size/backoff/deadline knobs, `service_bus_*`,
  `on_demand_max_concurrent_summaries`, etc.
- `app/services/fine_projection/summary.py` — `run_generation` now re-raises after
  persisting a FAILED ledger row instead of swallowing the exception
  (needed so a queue worker can classify retry-vs-dead); also gained
  `content_fingerprint` computation and the (currently dark,
  `SUMMARY_REUSE_ENABLED`-gated) reuse path — a separate feature riding
  in the same diff, not part of the job queue itself.
- `app/repositories/fine_projection/summary.py` and `app/models/fine_projection/summary.py` —
  `projection_summary` gained `content_fingerprint`/`source_as_of_date`
  columns and `find_reusable`/`create_reused` methods, supporting that
  same reuse feature.
- `app/agents/fine_projection/prompts/v2.py` → a new `v3.py` is now the
  active prompt version.

Reorganized, not changed in behavior:

- `app/worker/` → `app/workers/`.
- `tests/` split into `tests/unit/` (mirroring the `app/` package layout)
  and `tests/integration/` (real-Postgres-only tests, e.g.
  `test_job_queue_postgres.py`).
- `scripts/` split into `scripts/ops/` (operational: `run_daily_batch.py`,
  `run_projection_cli.py`) and `scripts/demo/` (seed data, demo scripts).
- Docker (`Dockerfile`, `docker-compose.yml`, `.dockerignore`) moved to
  the repo root.

---

## 6. Where do I look when...

| Symptom | Look here |
|---|---|
| A nightly run didn't finish / didn't start | `scripts/ops/run_daily_batch.py` logs (advisory lock message, or an infra-failure exit); `GET /api/v1/job-runs/{job_run_id}` for counts; check whether the advisory lock (key `837_401_559`) is stuck held by a dead process. |
| A PO's summary is stuck PENDING | `GET /penalties/projections?purchase_order_id=&include=summary` (or the dedicated `GET /penalties/projections/summary?purchase_order_id=&as_of_date=`) for the `penalty_summary` status; find its `job_item` via the matching `penalties.penalty_job_item_context` row — if there's no `job_item` at all, the next `run_daily_batch.py` sweep will recover it; if there's a stale RUNNING one, `reclaim_stale` recovers it after `JOB_QUEUE_VISIBILITY_TIMEOUT_SECONDS`. Remember (§4.2): nothing drains this on-demand `job_item` except a batch run today -- it will not resolve on its own from `BackgroundTasks`. |
| An item keeps failing and won't stop retrying | `GET /api/v1/job-runs/{job_run_id}/items?status=DEAD` for `last_error_code`; check `classify_failure` in `app/workers/loop.py` for whether that error type should actually be DEAD_LETTER instead of NACK. |
| I want to add a new job type | Add a `JobTaskType` member (`app/models/enums.py` — needs a migration, since the CHECK constraint is derived from it), handle it in `dispatch.execute_job` (dispatching to `penalty_projection.py`/`penalty_mitigation.py` as appropriate), and decide whether `classify_failure` needs a new non-retryable `AppError` category for it. |
| I want to change how many workers run concurrently | `settings.job_queue.worker_concurrency` (`JOB_QUEUE_WORKER_CONCURRENCY`) — but raising this without raising `settings.database.pool_size`/`max_overflow` causes a silent stall, not an error. |
| Rate limit (429) errors from Azure OpenAI | `app/core/rate_limit.py` — `RateLimitGate`/`looks_like_rate_limit`; check `summary.rate_limit_hits` in the batch's printed summary line. |
| I want to switch dispatch backends | `JOB_QUEUE_BACKEND` env var / `settings.job_queue.backend`; see `app/queue/factory.py`. Service Bus is implemented and tested but has never been validated against a live namespace — be careful before flipping this in production. |
