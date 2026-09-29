# API

All endpoints are mounted under `/api/v1`. Interactive docs (Swagger UI) are
served at `/docs` when the app is running (`uvicorn app.main:app`) and
`APP_DOCS_ENABLED=true` -- closed by default, see `docs/DEPLOYMENT.md`
"Configuration reference". Full request/response schemas: `app/schemas/`.
Router implementations: `app/api/v1/`.

## Response envelope

Every route returns the same `{success, message, data, error}` shape
(`app/core/envelope.py`), with one exception noted below:

```json
{ "success": true, "message": "OK", "data": { "...": "..." }, "error": null }
```

```json
{ "success": false, "message": "Unknown thread_id.", "data": null,
  "error": { "code": "THREAD_NOT_FOUND", "details": { "thread_id": "..." } } }
```

`GET /api/v1/health` is the one route in the whole API that returns a bare
body instead of the envelope (see below) -- it has to keep working for load
balancers that only check the HTTP status and don't parse JSON.

## Authentication

Every route requires the `X-Internal-Api-Key` header, checked against
`APP_INTERNAL_API_KEY` (`app/api/dependencies.py::require_internal_api_key`,
via `secrets.compare_digest`, wired in globally at the router-aggregation
point in `app/api/router.py` -- new routers are covered automatically,
nothing per-route to remember). The one exception is `GET /api/v1/health`,
left open for load balancers/uptime monitors. Missing or wrong key ->
`401`, reshaped into the standard envelope as `error.code = "HTTP_401"`; it
never echoes what was sent or what was expected.

```bash
curl http://127.0.0.1:8000/api/v1/purchase-orders -H "X-Internal-Api-Key: $APP_INTERNAL_API_KEY"
```

Every curl example below omits this header for brevity -- add it to every
call except `/health`.

## Error codes

Every expected error raises one of six `AppError` subclasses
(`app/core/exceptions.py`), each constructed with a `code: str` rather than
a dedicated class per failure case:

| Class | HTTP status | Example `code` values |
|---|---|---|
| `NotFoundError` | 404 | `PO_NOT_FOUND`, `PROJECTION_NOT_FOUND`, `MITIGATION_OPTION_NOT_FOUND`, `JOB_RUN_NOT_FOUND`, `THREAD_NOT_FOUND`, `EMAIL_NOT_FOUND`, `CARRIER_NOT_FOUND`, `PO_DELIVERY_CHANGE_REQUEST_NOT_FOUND` |
| `ConflictError` | 409 | `THREAD_STALE`, `CMIR_VERSION_CONFLICT`, `ACTIVE_PO_DELIVERY_CHANGE_REQUEST_EXISTS`, PO-uniqueness conflicts |
| `ValidationError` | 422 | `VALIDATION_ERROR`, `VIEW_NOT_SUPPORTED`, `MATERIAL_NOT_FOUND`, `PO_HAS_NO_LINES`, `INVALID_AS_OF_DATE`, `INVALID_PO_DELIVERY_CHANGE_RESPONSE`, `INVALID_INCLUDE` |
| `BusinessRuleError` | 409 | `NO_PROJECTION_EXISTS`, `NO_ACTIVE_RULES`, `NO_MITIGATION_OPTIONS_EXIST`, `PO_DELIVERY_CHANGE_LEAD_TIME_ERROR` |
| `ExternalServiceError` | 502 | `WORKFLOW_RESUME_FAILED`, `WORKFLOW_STATE_CORRUPT`, `QUEUE_NOT_CONFIGURED` |
| `NotAuthenticatedError` | 401 | Declared for the contract; `require_internal_api_key` currently raises a bare FastAPI `HTTPException(401)` instead, reshaped to `code="HTTP_401"` by the generic handler |

Also normalized into the same envelope: FastAPI's native `RequestValidationError`
(`code="REQUEST_VALIDATION_ERROR"`, 422), any bare `HTTPException`
(`code=f"HTTP_{status_code}"`), and an unhandled exception
(`code="INTERNAL_ERROR"`, 500, generic message only -- never a stack trace).
`details` is only present on a 4xx; a 5xx withholds it entirely, since some
call sites put internal failure text there.

## Health

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/health` | Readiness check, including DB connectivity |

`200` with `{"status": "ok", "database": "ok"}` when a `SELECT 1` over a
short-lived pooled connection succeeds; `503` with
`{"status": "degraded", "database": "unreachable"}` when it doesn't. No
driver or connection detail is ever returned -- it's logged instead. Not
wrapped in the `{success, message, data, error}` envelope (see above).

## Common (master data, purchase orders, fulfillment facts)

Routes in `app/api/v1/common/`, backed by `app/repositories/common/`.

### Master data

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/retailers` | Create retailer |
| GET | `/api/v1/retailers` | List retailers |
| POST | `/api/v1/retailers/{retailer_id}/locations` | Create a retailer-owned location |
| GET | `/api/v1/retailers/{retailer_id}/locations` | List a retailer's locations |
| POST | `/api/v1/skus` | Create SKU |
| GET | `/api/v1/skus` | List SKUs |
| POST | `/api/v1/materials` | Create material (plant-agnostic identity) |
| GET | `/api/v1/materials` | List materials |
| POST | `/api/v1/material-masters` | Create material master (one row per `(material, plant)`) |
| GET | `/api/v1/material-masters` | List material masters |
| POST | `/api/v1/plants` | Create plant |
| GET | `/api/v1/plants` | List plants |
| POST | `/api/v1/carriers` | Create carrier |
| GET | `/api/v1/carriers` | List carriers |
| GET | `/api/v1/carriers/{carrier_id}` | Get one carrier (`404 CARRIER_NOT_FOUND`) |

All creates return `201` with the created row (request fields plus `id`);
list routes take no query filters except where noted.

### Purchase orders and fulfillment facts

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/purchase-orders` | Create PO header + lines |
| GET | `/api/v1/purchase-orders?order_status=` | List purchase orders, optionally filtered by status |
| POST | `/api/v1/purchase-orders/{purchase_order_id}/confirmations` | Record an order confirmation (header + lines) |
| GET | `/api/v1/purchase-orders/{purchase_order_id}/confirmations` | Flat per-line confirmation history for the PO |
| POST | `/api/v1/purchase-orders/{purchase_order_id}/shipments` | Record a shipment (auto-creates the `delivery` header) |
| GET | `/api/v1/purchase-orders/{purchase_order_id}/shipments` | List shipments for the PO |
| POST | `/api/v1/purchase-orders/{purchase_order_id}/demand-exceptions` | Flag a demand exception (`422 PO_HAS_NO_LINES` if the PO has no lines and none is given) |
| GET | `/api/v1/purchase-orders/{purchase_order_id}/demand-exceptions` | List demand exceptions across the PO's lines |

Every create route returns `201`; every list route returns `200` with a
plain JSON array as `data`. Actual (post-delivery) penalties are a
`penalties`-domain resource -- see `/api/v1/penalties/actual-penalties`
below, not nested under `/purchase-orders/{purchase_order_id}/...` like the
facts above.

### Delivery-change requests

A procurement/EDI concept (vendor delivery-date renegotiation, SAP
ORDRSP/EDI-865 equivalent), not a penalty-calculation concept -- the penalty
projection/mitigation engines never read this resource, so it lives here,
flat and unprefixed, rather than under `/penalties/...`. Same reasoning as
Projections/Mitigations below for flattening, except `POST
.../{delivery_change_request_id}/response`, which stays nested under the
request's own surrogate `id` (a sub-action on one resource's own id, not a
second URL shape for the collection).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/delivery-change-requests` (body: `purchase_order_id` + ...) | Open a delivery-date negotiation (`reason_code` is `SHORTAGE`\|`DELAY`\|`OTHER`) |
| GET | `/api/v1/delivery-change-requests?purchase_order_id=` | List delivery-change-request history, optionally filtered to one PO; omitted lists across every PO |
| GET | `/api/v1/delivery-change-requests/{delivery_change_request_id}` | Single request read by its surrogate `id` (`404 PO_DELIVERY_CHANGE_REQUEST_NOT_FOUND`) |
| POST | `/api/v1/delivery-change-requests/{delivery_change_request_id}/response` | Record the retailer's response (`ACCEPTED`\|`COUNTERED`\|`REJECTED`); re-runs the projection engine, so an accepted/countered response can legitimately surface `409 NO_ACTIVE_RULES` if no rule covers the retailer |

## Penalties

Routes in `app/api/v1/penalties/`, backed by `app/services/penalties/`.

### Rules

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/penalties/rules` | Create a penalty rule, with tiers when `calc_type="TIERED"` |
| GET | `/api/v1/penalties/rules?retailer_id=` | List penalty rules, optionally by retailer |

### Projections

Flat, not nested under `/purchase-orders/{id}/...` -- a resource with its own
globally-meaningful id, fetched directly and listed cross-parent as a
first-class case, shouldn't have 2-3 different URL shapes for the same
resource type.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/penalties/projections` (body: `purchase_order_id`, `projection_date?`, `stacking_mode_override?`) | Compute and persist a projection for one PO (synchronous). Does not accept `?include=` -- fetch `summary`/`mitigations`/`mitigation_summary` afterward via the `GET` routes below |
| GET | `/api/v1/penalties/projections?purchase_order_id=&status=&projection_date=&projection_date_from=&projection_date_to=` `?include=summary,mitigations,mitigation_summary` | Merges the old PO-scoped history and cross-PO open-projection list into one route: `purchase_order_id` given returns that PO's full history (any status/date filters narrowing further); omitted returns the cross-PO list, defaulting `status` to `OPEN` |
| GET | `/api/v1/penalties/projections/{projection_id}?include=summary,mitigations,mitigation_summary` | Single projection read (`404 PROJECTION_NOT_FOUND`) |
| GET | `/api/v1/penalties/exposure?purchase_order_id=` | Latest projection's total-expected-penalty summary (`404 NO_PROJECTION_EXISTS` if none exist yet) |
| POST | `/api/v1/penalties/projections/summary` (body: `purchase_order_id`, `as_of_date?`, `force_regenerate?`) | Trigger/poll the LLM projection-summary job (`200` with a cached/ready summary, or `202 PENDING`) |
| GET | `/api/v1/penalties/projections/summary?purchase_order_id=&as_of_date=` | Dedicated pure-read poll for the same summary job, without re-fetching the projection (`200`, `status: null` if none was ever requested for this PO -- see below; unknown `purchase_order_id` is still `404 PO_NOT_FOUND`) |

`?include=` is validated against a per-route allow-list and is a pure read --
it never schedules generation as a side effect. Both `GET` projections routes
above (list and single-read) share the identical
`{summary, mitigations, mitigation_summary}` allow-list -- no `GET` route
gets a narrower one. `POST /api/v1/penalties/projections` does not accept
`include=` at all: a compute call always returns the bare projection result,
with `summary`/`mitigations`/`mitigation_summary` left unset; fetch those
separately via the `GET` routes once the projection exists.

**Combined dashboard read.** Both `GET` routes above embed each of
`summary`/`mitigations`/`mitigation_summary`, when requested, onto every row
returned (a single object for the single-read route, per-row for the list
route):

- `summary` / `summary_status` -- the row's cached projection-summary job.
- `mitigations` -- that row's ranked mitigation options (`MitigationOptionRepository.list_for_date`
  keyed on the row's own `(purchase_order_id, projection_date)`), or `null` if none have been
  computed yet for that date (a legitimate empty state, not an error).
- `mitigation_summary` / `mitigation_summary_status` -- the row's cached
  mitigation-summary job, same `READY`/`PENDING`/`FAILED`-from-cache
  contract as `summary_status`.

None of the three ever trigger computation -- a row with nothing cached simply
comes back with the corresponding field(s) `null`.

`GET /api/v1/penalties/mitigations` (see Mitigations below) also accepts
`?include=summary`, attaching each listed option's cached mitigation-summary
job the same way the single-mitigation route already does.
`POST /api/v1/penalties/mitigations` does not accept `include=` -- its
response's `options[].summary_status`/`options[].summary` always come back
unset; fetch a computed option's mitigation summary afterward via
`GET /api/v1/penalties/mitigations` or the single-mitigation route.

**Response shape -- probability, raw amount, and combined figure are always
three separate numbers, never just one blended figure.** Every violation
(in the live `POST /api/v1/penalties/projections` response, and in every
persisted row returned by the `GET` routes above, including
`.../penalties/exposure`) carries:

- `projection_id` (`POST /api/v1/penalties/projections` response only) -- the
  surrogate id of the `penalty_projection` row this violation was just
  persisted to (one row per violation, same id the `GET` routes' `id` field
  and the `projection_id` field elsewhere refer to). Use it
  directly against `GET /api/v1/penalties/projections/{projection_id}` or
  `POST /api/v1/penalties/mitigations` (`projection_id` in the body) -- no
  separate list/query call needed to look it up after a run.
- `probability` / `failure_probability` -- the raw probability of the
  violation occurring, 0-1.
- `penalty_amount` -- the raw dollar amount the retailer would charge **if**
  the violation occurs (same field name in both the live-run response and
  every persisted row). Not probability-weighted.
- `expected_penalty_amount` -- `probability * penalty_amount`, a
  risk-adjusted decision-support figure. **Never a predicted or guaranteed
  cost** -- it is what the exposure is worth in expectation, not what will
  be billed.

`.../penalties/exposure`'s `total_expected_penalty_amount` is the `SUM`/`MAX`
(per the retailer's `stacking_mode`) of the latest projection date's
per-violation `expected_penalty_amount` figures. Like the per-violation
figure it aggregates, it
is a risk-adjusted estimate, not a certain amount -- decompose it back into
per-violation probability + raw amount via the `violations` array whenever
the underlying components matter, rather than treating the total as a single
authoritative number.

### Mitigations

Also flat. Every route accepts either a direct `(purchase_order_id,
projection_date)` pair or a `projection_id` (resolved to that same pair) --
exactly one of the two shapes, never both, never neither (`422` otherwise).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/penalties/mitigations?projection_id=` or `?purchase_order_id=&projection_date=` `?include=summary` | List ranked mitigation options, optionally with each option's cached mitigation summary |
| POST | `/api/v1/penalties/mitigations` (body: `projection_id` or `purchase_order_id`+`projection_date`) | Compute and persist ranked mitigation options (synchronous). Does not accept `?include=` -- fetch each option's cached mitigation summary afterward via the `GET` route above |
| GET | `/api/v1/penalties/mitigations/{mitigation_id}?include=summary` | Single mitigation option read (`404 MITIGATION_OPTION_NOT_FOUND`) |
| POST | `/api/v1/penalties/mitigations/summary` (body: `purchase_order_id`, `as_of_date?`, `force_regenerate?`) | Trigger/poll the LLM mitigation-summary job (`200`/`202`, same contract as the projection summary above) |
| GET | `/api/v1/penalties/mitigations/summary?purchase_order_id=&as_of_date=` | Dedicated pure-read poll for the same summary job (`200`, `status: null` if none was ever requested for this PO -- same convention as the projection-summary GET above; unknown `purchase_order_id` is still `404 PO_NOT_FOUND`) |

### Actual penalties

Flat, same reasoning as Projections/Mitigations above.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/penalties/actual-penalties` (body: `purchase_order_id` + ...) | Record an actual (post-delivery) penalty |
| GET | `/api/v1/penalties/actual-penalties?purchase_order_id=` | List actual penalties, optionally filtered to one PO; omitted lists across every PO |
| GET | `/api/v1/penalties/actual-penalties/{actual_penalty_id}` | Single actual-penalty read (`404 ACTUAL_PENALTY_NOT_FOUND`) |

### Disputes

Flat, same reasoning as Projections/Mitigations/Actual penalties above.
Routes in `app/api/v1/penalties/disputes.py`, backed by
`app.services.penalties.dispute.service.DisputeResolutionService`; schemas in
`app/schemas/penalties/disputes.py`. `.../{dispute_id}/analyze`,
`.../{dispute_id}/resolve`, and `.../{dispute_id}/summary` are sub-actions
nested under the dispute's own id, not a second URL shape for the
collection (mirrors the delivery-change-request `.../response` convention
under Common above).

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/penalties/disputes` (body: `actual_penalty_id`, `reason_code`, `claimed_amount`, `notes?`, `claim_facts?`) | Open a dispute against an existing actual penalty (`201`). `claim_facts` (defect_units, defect_rate_pct, replacement_cost_paid, occurrence_count, storage_days -- all optional, bounds-checked, unknown keys rejected) is written once onto the charge, never mutated after (`409 CLAIM_FACTS_ALREADY_SET` on a second attempt); only QUALITY/COVER_PURCHASE/FINANCIAL/STORAGE_DURATION_FEE-family disputes need it |
| GET | `/api/v1/penalties/disputes?purchase_order_id=` | List disputes, optionally filtered to one PO (`404` if the PO itself doesn't exist); omitted lists across every PO |
| GET | `/api/v1/penalties/disputes/{dispute_id}` | Single dispute read (`404 DISPUTE_NOT_FOUND`) |
| POST | `/api/v1/penalties/disputes/{dispute_id}/analyze` | Compute and persist the verdict against the original rule (synchronous -- no job-queue involvement, no blocking human-approval gate by design), moving the dispute to `ANALYZED` |
| POST | `/api/v1/penalties/disputes/{dispute_id}/resolve` (body: `resolved_by`, `override_verdict?`, `override_reason?`) | Record a human decision on an already-`ANALYZED` dispute; omit `override_verdict` to accept the engine's own verdict, or set both it and `override_reason` to override it (`422` if only one of the two is set) |
| POST | `/api/v1/penalties/disputes/{dispute_id}/summary` (body: `force_regenerate?`) | Trigger/poll the LLM dispute-summary job (`200` with a cached/ready summary, or `202 PENDING`) |
| GET | `/api/v1/penalties/disputes/{dispute_id}/summary` | Dedicated pure-read poll for the same summary job (`200`, `status: null` if none was ever requested for this dispute -- same "never-requested is success, not 404" convention as the projection/mitigation summary `GET` routes above) |

A dispute's `reason_code` is one of `AMOUNT_INCORRECT`, `NOT_LATE`,
`QTY_CONFIRMED`, `RULE_MISAPPLIED`, `OTHER`; its `verdict` (set by
`analyze`) and `override_verdict` (set by `resolve`) are one of `NO_PAY`,
`PAY_PARTIAL`, `PAY_FULL`. The response also carries `computed_amount` /
`delta_amount` (the engine's re-derived charge and its difference from
`claimed_amount`) and a full `analysis_breakdown` of the facts and rule
the verdict was computed against, once analyzed.

### Retailer agreements and rule extraction

Routes in `app/api/v1/penalties/rule_extraction.py`, backed by
`app.services.penalties.rule_extraction.service.PenaltyRuleExtractionService`;
schemas in `app/schemas/penalties/rule_extraction.py`. Turns a retailer
agreement's prose into reviewed `penalty_rule` rows: upload the agreement,
run extraction, review each candidate clause, publish the approved ones.
See `docs/architecture/penalty-rule-extraction.md` for the full design.

Only an endpoint that runs an agent/LLM stays in mars-backend, plus `publish`
(the deterministic rule compiler, an engine-work exception to that split).
Every other rule-extraction endpoint -- retailer agreement create/list/get,
extraction status, extraction runs, extracted-rule reads, review, and
revision listing -- moved to mars-bff, under the same
`/penalties/retailer-agreements/...` paths.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/penalties/retailer-agreements/{retailer_agreement_id}/extract` | Run the extraction graph to completion for the retailer agreement (`202`), returning `agent_run_id` and `staged_count`. No interrupt: the graph ends at `stage_rules`, and the run is `completed` or `failed` before this call returns |
| POST | `/api/v1/penalties/retailer-agreements/{retailer_agreement_id}/extracted-rules/{extracted_rule_id}/revisions` (body: `instruction`, `requested_by?`) | Queue a reviewer's re-extraction instruction for one staged rule (`202`); runs in the background, replaces the rule's facts, and resets it to `PENDING_REVIEW` |
| POST | `/api/v1/penalties/retailer-agreements/{retailer_agreement_id}/publish` | Run the publisher over the retailer agreement's approved extracted rules from the latest completed run, inserting live `penalty_rule` rows and a `rule_publication` audit row for every outcome |

Retailer agreement create/list/get (`document_sha256`-deduped upload) and the
`?status=` note about `retailer_agreement` having no status column live with
mars-bff's endpoint reference now.

The extraction graph carries no interrupt: `POST .../extract` runs it through
to `stage_rules -> END` and only returns once the run is `completed` or
`failed`. Review (mars-bff) is a plain database status change, not a graph
resume -- there is no `/workflow-threads/{thread_id}/decisions` call in this
flow. Which run is "the latest" (for extraction-run listing, review, and
publish alike) is always the newest `process.agent_run` row tagged with this
retailer agreement whose `status = completed`; the same definition is
duplicated in mars-bff (see `docs/architecture/penalty-rule-extraction.md`
section 3.1.1).

A reviewer who wants a clause re-read rather than approved/rejected as-is can
request a revision instead: `POST .../extracted-rules/{id}/revisions` queues
an `extracted_penalty_rule_revision` row and runs the re-extraction in the
background, replacing the rule's facts and resetting it to
`PENDING_REVIEW`. `RuleRevisionResponse.status` is one of `QUEUED`,
`RUNNING`, `COMPLETED`, `FAILED` (a revision open longer than 10 minutes is
marked `FAILED` with `error: "Revision timed out."` the next time anything
checks it). mars-bff's review endpoint refuses while a revision is still
open on the same rule (`409 RULE_REVISION_IN_PROGRESS`). Listing a rule's
revision history also moved to mars-bff.

`POST .../publish` returns `RulePublicationResultResponse`
(`published_count`, `rejected_count`, `outcomes`), each `outcomes[]` entry
an `extracted_rule_id`, `outcome` (`PUBLISHED`\|`REJECTED`),
`penalty_rule_id` (set only when published), `reason_code` (set only when
rejected, see `docs/RUNBOOK.md` for the full list), and `reason_detail`. The
publication audit read (every outcome ever recorded, plus a
`rejection_reason_histogram`) moved to mars-bff.

Every response above uses the standard envelope.

## Job runs (`penalties` domain)

Routes in `app/api/v1/job_runs.py`; request schemas in
`app/schemas/penalties/batches.py`. `job_run`/`job_item` live in the
`process` schema and are shared infrastructure, but `/job-runs` itself only
covers the `penalties` domain's batch job types -- CMIR email ingestion is
triggered exclusively via the dedicated `POST /cmir/email-events` (see the
CMIR section below), not through `/job-runs`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/job-runs` | Dispatch a batch job; `job_type` selects the batch (`PENALTY_PROJECTION_BATCH` default, `PENALTY_MITIGATION_BATCH`, or `PENALTY_FULL_RUN_BATCH`) |
| GET | `/api/v1/job-runs/{job_run_id}` | Run status: counts by item status, `total_items`, `is_complete` (`404 JOB_RUN_NOT_FOUND`) |
| GET | `/api/v1/job-runs/{job_run_id}/items?status=&limit=&offset=` | List the items in one run |

`POST /api/v1/job-runs` always returns `202`. Under `job_type=PENALTY_PROJECTION_BATCH`
this only writes the ledger row -- nothing runs it until a drain happens
(`JOB_QUEUE_BACKEND=postgres`) or a consumer picks up the dispatched message
(`JOB_QUEUE_BACKEND=service_bus`); see `docs/JOB-QUEUE-WALKTHROUGH.md`.
`job_type=PENALTY_MITIGATION_BATCH` mirrors that same enqueue/dispatch
shape (`item_type=MITIGATION_RUN` per item), except the batch resolves each
OPEN purchase order's own *latest existing* penalty projection rather than
running one fresh -- a purchase order with no projection at all is skipped,
not errored, the same eligibility `MitigationService.run_for_purchase_order`
itself enforces for a single PO/date (`app/services/penalties/mitigation/
service.py`). It takes no `projection_date`/`stacking_mode_override` body
field (unlike `PENALTY_PROJECTION_BATCH`) since each purchase order is
evaluated against its own latest projection date, not one shared date.

`job_type=PENALTY_FULL_RUN_BATCH` (`item_type=PENALTY_FULL_RUN` per item)
runs the requested subset of `projection` -> `projection_summary` ->
`mitigation` -> `mitigation_summary` for every purchase order matching
`scope`, always in that fixed dependency order regardless of the order
`steps` is submitted in:

```json
{
  "job_type": "PENALTY_FULL_RUN_BATCH",
  "steps": ["projection", "projection_summary", "mitigation", "mitigation_summary"],
  "scope": {"purchase_order_status": "OPEN"},
  "projection_date": "2026-09-02"
}
```

`steps` must be non-empty. `scope` accepts either `purchase_order_status`
(default `"OPEN"`) or `purchase_order_ids` (a specific list) -- not both;
providing both is a `422` at the request-validation level, not a service
error. Eligibility mirrors `PENALTY_MITIGATION_BATCH` exactly when
`"projection"` is not itself in `steps`: a purchase order with no existing
projection is skipped, not failed. When `"projection"` IS in `steps`, every
matching purchase order is eligible. The requested `steps` (and the run's
resolved `projection_date`) travel on `process.job_item.metadata`, not a
new column.

There is currently no "list all job runs" endpoint -- only single-run
lookups.

## CMIR

Routes in `app/api/v1/cmir.py`, backed by `app.services.cmir.service.CmirService`.
See `docs/prd.md` for the business rules and the README's identity rule
before touching reviewer/queue code -- `thread_id` is the only identifier
reviewer/UI actions may key on.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/cmir/email-events` | Start one email-ingest batch (`202`; `source` must be `"gmail"` or `422 VALIDATION_ERROR`) |
| POST | `/api/v1/internal/process-email` | Internal Service Bus consumer callback, not PRD-facing -- polymorphic result shape, deliberately untyped |

## PO Validation

Routes in `app/api/v1/po_validation.py`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/po-validation/purchase-order-lines` | Ingest PO lines into the validation pipeline (runs CMIR-matching/material-master checks as a side effect); `202`. Each line also creates and inline-settles one `process.job_item` (`item_type=PO_VALIDATION`) under one `process.job_run` (`job_type=PO_VALIDATION_BATCH`) per call -- `batch_id` in the response *is* that `job_run_id`, queryable via `GET /api/v1/job-runs/{batch_id}` |
| GET | `/api/v1/purchase-order-lines?purchase_order_id=&status=&limit=&cursor=` | The one `purchase_order_line` listing route, paginated on `updated_at` (replaces what used to be this flat route plus a separate nested `GET /purchase-orders/{purchase_order_id}/lines`). Both `purchase_order_id`/`status` omitted defaults to the "ready" set (`READY_FOR_SO_CREATION`/`READY_FOR_SO_CREATION_PARTIAL`) across every PO; `purchase_order_id` given with `status` omitted returns that PO's lines regardless of status; an explicit `status` filters on that exact `line_status` value, optionally also scoped to one PO |

## Workflow threads (shared: `cmir` + `po_validation`)

Routes in `app/api/v1/workflow_threads.py`. `workflow_thread` is a shared
`process`-schema resource used by both, not owned by either router. Penalty
rule extraction does not use it: the extraction graph has no interrupt to
pause on, review is a plain `extracted_penalty_rule.status` update (see
"Retailer agreements and rule extraction" above), and
`workflow_thread_subject` has no `RETAILER_AGREEMENT` subject type.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/workflow-threads?domain=&status=&stage=&limit=&cursor=` | List workflow threads (`domain` is `cmir`\|`po_validation`) |
| GET | `/api/v1/workflow-threads/{thread_id}?include=snapshot` | Thread stage (always) plus its domain-specific snapshot (opt-in via `include`) |
| POST | `/api/v1/workflow-threads/{thread_id}/missing-fields` | Submit missing mandatory fields and resume the graph (CMIR-only in substance today) |
| PATCH | `/api/v1/workflow-threads/{thread_id}/draft` | Save reviewer draft edits (CMIR-only in substance today) |
| POST | `/api/v1/workflow-threads/{thread_id}/decisions` | Record a decision -- one generic endpoint, `decision_type` discriminator selects `CMIR_APPROVAL`, `QTY_MISMATCH`, or `MANUAL_CMIR_ENTRY` (body: `actor`, `expected_updated_at`) |

Common errors: `THREAD_NOT_FOUND` (404, unknown `thread_id`), `THREAD_STALE`
(409, `expected_updated_at` didn't match -- refetch and retry), `THREAD_NOT_WAITING`
(409, the thread isn't paused for that specific action), `CMIR_VERSION_CONFLICT`
(409, another approval already superseded the active `cmir_records` row for
this customer/material while this thread was waiting -- the thread closes to
`COMPLETED_CONFLICT` and does not reopen automatically).

## Processing errors (shared: `cmir` + `po_validation`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/processing-errors?purchase_order_line_id=` | List processing errors for one PO line (`purchase_order_line_id` is required) -- found directly via `processing_error.purchase_order_line_id`, so a line that failed before ever reaching a human interrupt (no `workflow_thread` yet) is still discoverable |

`processing_error` is a `process`-schema table shared across domains
(generalizes the old `po_line_errors`).

## Admin

Routes in `app/api/v1/admin.py`, backed by `app.services.seeding.service.PenaltySeedingService`.
Spans both domains, so it stays a top-level module rather than living under
`common/` or `penalties/`.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/v1/admin/seed-master-data?force=` | Seed the four worked-example scenarios' master/master-adjacent data, plus a separate additive set of dispute-resolution fixtures (5 dispute rules, 8 delivered purchase orders, 8 actual penalties) |
| POST | `/api/v1/admin/simulate-daily-run` | Replay the seeded scenarios day by day, returning each day's projected shortage/delay penalty and negotiation outcome |

`seed-master-data`'s response (`SeedDataResponse`,
`app/schemas/penalties/admin.py`) reports every table it touched as a flat
count: `retailers`, `materials`, `skus`, `plants`, `carriers`, `rules`,
`orders`, `mitigation_inputs` for the four worked-example scenarios, plus
`dispute_rules`, `dispute_orders`, `dispute_actual_penalties` for the
dispute fixtures above. Idempotent like the rest of this endpoint: a
repeat call with data already seeded returns `0` for every field it
skipped, not an error.
