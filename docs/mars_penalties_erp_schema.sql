-- Mars Penalties -- ERP-normalized schema reference.
--
-- Postgres-specific (unlike the old mars_fines_projection_schema.sql, this
-- file makes no cross-DB-portability claim -- SQLite is a test-only
-- translation of this schema, not a target it was designed for).
--
-- Every table here is created by the 5-revision Alembic chain in
-- alembic/versions/ (0824321a02a4 -> ff53dabe6e4c -> 374aa902b053 ->
-- 4b41f6bcb2f3 -> a5b39c6e2181) and mirrored 1:1 by app/models/. This file
-- is the canonical, hand-maintained cross-reference between the two --
-- keep it in sync whenever either changes (see docs/DATABASE.md's
-- "Schema-change checklist").
--
-- Conventions: singular snake_case table names, no dim_/fact_ prefix (see
-- docs/DATABASE.md's "Why no dim_/fact_ prefix"), surrogate UUID `id`
-- primary key on every table, ids are client-generated (Python-side
-- generate_uuid7()) -- no `id` column carries a server-side default.
--
-- Five raw-DDL constructs below (three partial unique indexes, two CHECK
-- constraints) are NOT expressible as a plain column/table declaration and
-- exist only as raw DDL in their migration (see that migration's docstring
-- for the full reasoning):
--   1. process.job_item: UNIQUE INDEX uq_job_item_inflight ON
--      process.job_item (item_type, dedupe_key) WHERE status IN
--      ('PENDING', 'RUNNING') AND dedupe_key IS NOT NULL -- restores the
--      pre-restructure schema's in-flight dedupe on top of a generic
--      dedupe_key column (see app/models/process/job.py::JobItem's
--      docstring).
--   2. process.agent: UNIQUE INDEX uq_agent_one_active_per_code ON
--      process.agent (agent_code) WHERE is_active.
--   3. cmir.cmir_record: UNIQUE INDEX uq_cmir_record_current_identity ON
--      cmir.cmir_record (customer_identity_key,
--      target_customer_material_ref_key) WHERE is_current.
--   4-5. process.workflow_thread_subject and cmir.cmir_job_item_context
--      each carry their own CHECK (num_nonnulls(...) = 1) constraint over
--      their two nullable "exactly one of" FK columns.
--
-- All five are partial indexes or Postgres-only-builtin CHECKs: Alembic's
-- own `alembic check` / `alembic revision --autogenerate` will always
-- report them as phantom drops against a freshly-migrated database (it
-- doesn't compare postgresql_where=, and num_nonnulls() isn't reflected as
-- anything ORM-comparable) -- hand-strip any such diff, it's expected, not
-- a regression.
--
-- `inventory_position`/`demand_forecast` (mentioned in an earlier draft of
-- this schema, docs/redesigned-schema.md) do NOT appear below -- neither
-- has an ORM class, a migration, or a locked-in design; see
-- docs/DATABASE.md's "Known gaps / not modeled yet".

-- =============================================================================
-- PUBLIC SCHEMA (unqualified) -- shared master data and fulfillment facts.
-- No CREATE SCHEMA statement: these tables live in Postgres's default
-- `public` schema, not a dedicated schema of their own (reversed from an
-- earlier version of this design, which used a dedicated `common` schema).
-- =============================================================================

CREATE TABLE material (
    id                  uuid PRIMARY KEY,
    material_code       varchar(64) NOT NULL UNIQUE,
    description         text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

CREATE TABLE plant (
    id                  uuid PRIMARY KEY,
    plant_code          varchar(50) NOT NULL UNIQUE,
    plant_name          varchar(200),
    country_code        varchar(10),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

CREATE TABLE retailer (
    id                              uuid PRIMARY KEY,
    retailer_code                   varchar(50) NOT NULL UNIQUE,
    retailer_name                   varchar(200) NOT NULL,
    priority_tier                   varchar(30),
    stacking_mode                   varchar(30) NOT NULL,
    source_system                   varchar(50),
    -- Per-retailer PO delivery-change-request policy -- kept from the
    -- pre-ERP-split model, not in the original redesigned-schema.md draft.
    extension_min_lead_days         integer NOT NULL,
    extension_response_sla_hours    integer NOT NULL,
    extension_penalty_threshold     numeric(10,2) NOT NULL,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),
    deleted_at                      timestamptz
);

CREATE TABLE carrier (
    id                              uuid PRIMARY KEY,
    carrier_code                    varchar(50) NOT NULL UNIQUE,
    carrier_name                    varchar(200) NOT NULL,
    historical_reliability_score    numeric(5,2) NOT NULL,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),
    deleted_at                      timestamptz
);

CREATE TABLE sku (
    id                  uuid PRIMARY KEY,
    sku_code            varchar(100) NOT NULL UNIQUE,
    description         text,
    material_id         uuid REFERENCES material(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

CREATE TABLE storage_location (
    id                          uuid PRIMARY KEY,
    plant_id                    uuid NOT NULL REFERENCES plant(id),
    storage_location_code       varchar(50) NOT NULL,
    storage_location_name       varchar(200),
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    UNIQUE (plant_id, storage_location_code)
);

CREATE TABLE warehouse (
    id                  uuid PRIMARY KEY,
    warehouse_code      varchar(50) NOT NULL UNIQUE,
    warehouse_name      varchar(200),
    plant_id            uuid REFERENCES plant(id),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

CREATE TABLE retailer_location (
    id                  uuid PRIMARY KEY,
    retailer_id         uuid NOT NULL REFERENCES retailer(id),
    location_code       varchar(100) NOT NULL,
    location_name       varchar(200),
    location_type       varchar(50),
    address_line_1      varchar(255),
    address_line_2      varchar(255),
    city                varchar(100),
    state_province      varchar(100),
    postal_code         varchar(30),
    country_code        varchar(10),
    is_active           boolean NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    UNIQUE (retailer_id, location_code)
);

CREATE TABLE material_master (
    id                          uuid PRIMARY KEY,
    material_id                 uuid NOT NULL REFERENCES material(id),
    sap_material_number         varchar(64) NOT NULL,
    plant_id                    uuid REFERENCES plant(id),
    description                 text,
    available_quantity          numeric(18,3),
    uom                         varchar(30),
    discontinuation_indicator   varchar(30),
    effective_out_date          date,
    follow_up_material_id       uuid REFERENCES material(id),
    source_system               varchar(50),
    last_synced_at              timestamptz,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    UNIQUE (material_id, plant_id)
);

CREATE TABLE purchase_order (
    id                              uuid PRIMARY KEY,
    purchase_order_number           varchar(50) NOT NULL UNIQUE,
    retailer_id                     uuid NOT NULL REFERENCES retailer(id),
    retailer_po_number               varchar(100),
    order_date                      date NOT NULL,
    requested_delivery_date         date,
    required_ship_date              date,
    order_status                    varchar(50) NOT NULL,
    source_system                   varchar(50),
    source_document_type            varchar(50),
    source_document_number          varchar(100),
    -- Kept from the pre-ERP-split `sales_order` model (PO delivery-change
    -- feature) -- not in the original redesigned-schema.md draft.
    current_delivery_date           date,
    current_required_ship_date      date,
    negotiation_status              varchar(30) NOT NULL,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),
    deleted_at                      timestamptz
);
CREATE INDEX ix_purchase_order_negotiation_status ON purchase_order (negotiation_status);

CREATE TABLE po_delivery_change_request (
    id                          uuid PRIMARY KEY,
    request_id                   varchar(50) NOT NULL UNIQUE,
    purchase_order_id           uuid NOT NULL REFERENCES purchase_order(id),
    reason_code                  varchar(30) NOT NULL CHECK (reason_code IN ('SHORTAGE', 'DELAY', 'OTHER')),
    requested_at                 timestamptz NOT NULL,
    baseline_delivery_date        date NOT NULL,
    proposed_delivery_date        date NOT NULL,
    expires_at                   timestamptz NOT NULL,
    status                      varchar(30) NOT NULL
        CHECK (status IN ('PENDING', 'ACCEPTED', 'COUNTERED', 'REJECTED', 'EXPIRED')),
    retailer_response_date        date,
    countered_delivery_date       date,
    resolved_at                  timestamptz,
    response_payload              jsonb,
    notes                        text,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);
CREATE INDEX ix_po_delivery_change_request_po_status
    ON po_delivery_change_request (purchase_order_id, status);
CREATE INDEX ix_po_delivery_change_request_status_expires
    ON po_delivery_change_request (status, expires_at);

CREATE TABLE purchase_order_line (
    id                          uuid PRIMARY KEY,
    purchase_order_id           uuid NOT NULL REFERENCES purchase_order(id),
    line_number                 varchar(50) NOT NULL,
    retailer_po_line_number     varchar(50),
    sku_id                      uuid REFERENCES sku(id),
    material_id                 uuid REFERENCES material(id),
    retailer_material_code      varchar(100),
    plant_id                    uuid REFERENCES plant(id),
    storage_location_id         uuid REFERENCES storage_location(id),
    ship_to_location_id         uuid REFERENCES retailer_location(id),
    ordered_quantity            numeric(18,3) NOT NULL,
    unit_price                  numeric(10,2) NOT NULL,  -- kept from pre-ERP sales_order; PERCENT_OF_PO/TIERED fine calc needs it
    uom                         varchar(30),
    requested_delivery_date     date,
    required_ship_date          date,
    line_status                 varchar(50) NOT NULL,
    raw_payload                 jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    UNIQUE (purchase_order_id, line_number)
);

CREATE TABLE order_confirmation (
    id                      uuid PRIMARY KEY,
    confirmation_number     varchar(100) NOT NULL UNIQUE,
    purchase_order_id       uuid NOT NULL REFERENCES purchase_order(id),
    confirmation_date       timestamptz NOT NULL,
    status                  varchar(50),
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    deleted_at              timestamptz
);

CREATE TABLE order_confirmation_line (
    id                          uuid PRIMARY KEY,
    order_confirmation_id       uuid NOT NULL REFERENCES order_confirmation(id),
    purchase_order_line_id      uuid NOT NULL REFERENCES purchase_order_line(id),
    confirmed_quantity          numeric(18,3) NOT NULL,
    confirmed_delivery_date     date,
    cut_reason_code             varchar(100),
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    UNIQUE (order_confirmation_id, purchase_order_line_id)
);

CREATE TABLE delivery (
    id                          uuid PRIMARY KEY,
    delivery_number             varchar(50) NOT NULL UNIQUE,
    purchase_order_id           uuid NOT NULL REFERENCES purchase_order(id),
    ship_from_plant_id          uuid REFERENCES plant(id),
    ship_from_warehouse_id      uuid REFERENCES warehouse(id),
    ship_to_location_id         uuid REFERENCES retailer_location(id),
    delivery_status             varchar(50) NOT NULL,
    planned_delivery_date       date,
    actual_delivery_date        date,
    planned_ship_date           date,
    actual_ship_date            date,
    goods_issue_date            date,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);

CREATE TABLE delivery_line (
    id                          uuid PRIMARY KEY,
    delivery_id                 uuid NOT NULL REFERENCES delivery(id),
    purchase_order_line_id      uuid NOT NULL REFERENCES purchase_order_line(id),
    delivered_quantity          numeric(18,3) NOT NULL,
    uom                         varchar(30),
    status                      varchar(50),
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    UNIQUE (delivery_id, purchase_order_line_id)
);

CREATE TABLE shipment (
    id                          uuid PRIMARY KEY,
    shipment_number             varchar(100) NOT NULL UNIQUE,
    delivery_id                 uuid NOT NULL REFERENCES delivery(id),
    carrier_id                  uuid REFERENCES carrier(id),
    expected_ship_date          date,
    actual_ship_date            date,
    expected_delivery_date      date,
    actual_delivery_date        date,
    expected_transit_days       integer,
    appointment_status          varchar(50),
    shipment_status             varchar(50) NOT NULL,
    recorded_at                 timestamptz NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);

CREATE TABLE production_order (
    id                          uuid PRIMARY KEY,
    production_order_number     varchar(100) NOT NULL UNIQUE,
    material_id                 uuid REFERENCES material(id),
    plant_id                    uuid REFERENCES plant(id),
    planned_quantity             numeric(18,3),
    produced_quantity            numeric(18,3),
    planned_start_date          date,
    actual_start_date           date,
    planned_end_date            date,
    actual_end_date             date,
    status                      varchar(50) NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);

CREATE TABLE production_schedule (
    id                          uuid PRIMARY KEY,
    production_order_id         uuid REFERENCES production_order(id),
    material_id                 uuid REFERENCES material(id),
    plant_id                    uuid NOT NULL REFERENCES plant(id),
    scheduled_quantity           numeric(18,3),
    scheduled_start_at           timestamptz,
    scheduled_end_at             timestamptz,
    status                      varchar(50) NOT NULL,  -- ON_TRACK / AT_RISK / BEHIND
    status_at                   timestamptz NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);

CREATE TABLE demand_exception (
    id                          uuid PRIMARY KEY,
    exception_id                varchar(50) NOT NULL UNIQUE,
    purchase_order_line_id      uuid NOT NULL REFERENCES purchase_order_line(id),
    flagged_date                date NOT NULL,
    resolved                    boolean NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);

-- =============================================================================
-- SCHEMA: process -- shared job/agent/workflow backbone (cmir + penalties)
-- =============================================================================

-- No `status` column (deliberate -- see app/models/process/job.py::JobRun's
-- docstring): a run's status is derived at read time by grouping job_item
-- rows, not stored, to avoid a hot-lock bottleneck on one shared row.
CREATE TABLE process.job_run (
    id                      uuid PRIMARY KEY,
    job_type                varchar(100) NOT NULL,
    trigger_type             varchar(50) NOT NULL
        CHECK (trigger_type IN ('MANUAL_BATCH', 'ON_DEMAND', 'SCHEDULED_DAILY')),
    requested_item_count    integer NOT NULL,
    started_at              timestamptz,
    completed_at            timestamptz,
    error                   text,
    metadata                jsonb NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    deleted_at              timestamptz
);

CREATE TABLE process.agent (
    id                  uuid PRIMARY KEY,
    agent_code          varchar(100) NOT NULL,
    agent_name          varchar(200) NOT NULL,
    domain              varchar(50) NOT NULL CHECK (domain IN ('cmir', 'penalties')),
    prompt_version      varchar(50) NOT NULL,
    system_prompt       text NOT NULL,
    description         text,
    is_active           boolean NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    UNIQUE (agent_code, prompt_version)
);
CREATE INDEX ix_agent_agent_code ON process.agent (agent_code);
-- Partial unique index -- at most one active prompt version per agent code.
CREATE UNIQUE INDEX uq_agent_one_active_per_code ON process.agent (agent_code) WHERE is_active;

-- dedupe_key is queue infrastructure, not a domain column: the domain layer
-- computes and passes it at enqueue time (e.g.
-- f"{purchase_order_id}:{projection_date}" for penalties, the email event id
-- for CMIR). item_type is the real dedupe discriminator; note that
-- penalties.penalty_job_item_context.task_type currently duplicates it with
-- nothing keeping the two in sync (see app/models/process/job.py::JobItem's
-- docstring).
CREATE TABLE process.job_item (
    id                      uuid PRIMARY KEY,
    job_run_id              uuid NOT NULL REFERENCES process.job_run(id),
    item_type               varchar(100) NOT NULL
        CHECK (item_type IN ('EMAIL_INGEST', 'MITIGATION_RUN', 'MITIGATION_SUMMARY_REGEN',
                              'ORDER_RUN', 'PENALTY_FULL_RUN', 'PO_VALIDATION',
                              'PROJECTION_SUMMARY_REGEN')),
    status                  varchar(50) NOT NULL
        CHECK (status IN ('DEAD', 'PENDING', 'RUNNING', 'SUCCEEDED')),
    attempt_count           integer NOT NULL DEFAULT 0,
    max_attempts            integer NOT NULL DEFAULT 5,
    available_at            timestamptz NOT NULL DEFAULT now(),
    locked_by               varchar(100),
    locked_at               timestamptz,
    heartbeat_at            timestamptz,
    dispatched_at           timestamptz,
    completed_at            timestamptz,
    last_error              text,
    last_error_code         varchar(100),
    metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
    dedupe_key              varchar(200),
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    deleted_at              timestamptz
);
CREATE INDEX ix_job_item_claimable ON process.job_item (status, available_at);
CREATE INDEX ix_job_item_job_run_id ON process.job_item (job_run_id);
-- Partial unique index -- in-flight dedupe, restored on top of the generic
-- dedupe_key column (see the comment above CREATE TABLE process.job_item).
CREATE UNIQUE INDEX uq_job_item_inflight ON process.job_item (item_type, dedupe_key)
    WHERE status IN ('PENDING', 'RUNNING') AND dedupe_key IS NOT NULL;

CREATE TABLE process.workflow_thread (
    id                  uuid PRIMARY KEY,
    job_item_id         uuid REFERENCES process.job_item(id),
    status              varchar(50) NOT NULL,
    stage               varchar(100) NOT NULL,
    current_node        varchar(100),
    completed_at        timestamptz,
    error               text,
    metadata            jsonb NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

-- Created by the cmir migration, not the process migration, because it FKs
-- into cmir.email_event (see the FK-cycle note in the module docstring
-- above and 374aa902b053_cmir_schema.py's docstring).
CREATE TABLE process.workflow_thread_subject (
    workflow_thread_id          uuid PRIMARY KEY REFERENCES process.workflow_thread(id),
    email_event_id               uuid REFERENCES cmir.email_event(id),
    purchase_order_line_id       uuid REFERENCES purchase_order_line(id),
    created_at                   timestamptz NOT NULL DEFAULT now(),
    updated_at                   timestamptz NOT NULL DEFAULT now(),
    deleted_at                   timestamptz,
    CONSTRAINT ck_workflow_thread_subject_one_of
        CHECK (num_nonnulls(email_event_id, purchase_order_line_id) = 1)
);

CREATE TABLE process.agent_run (
    id                      uuid PRIMARY KEY,
    job_item_id             uuid REFERENCES process.job_item(id),
    workflow_thread_id      uuid REFERENCES process.workflow_thread(id),
    agent_id                uuid NOT NULL REFERENCES process.agent(id),
    status                  varchar(50) NOT NULL,
    run_type                varchar(50) NOT NULL,
    started_at              timestamptz,
    completed_at            timestamptz,
    error                   text,
    metadata                jsonb NOT NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    deleted_at              timestamptz
);

CREATE TABLE process.agent_trace (
    id                  uuid PRIMARY KEY,
    agent_run_id        uuid NOT NULL REFERENCES process.agent_run(id),
    node_name           varchar(100) NOT NULL,
    status              varchar(50) NOT NULL,
    started_at          timestamptz NOT NULL,
    completed_at        timestamptz,
    duration_ms         bigint,
    input_snapshot      jsonb,
    output_snapshot     jsonb,
    error               text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

CREATE TABLE process.human_action (
    id                      uuid PRIMARY KEY,
    job_item_id             uuid REFERENCES process.job_item(id),
    workflow_thread_id      uuid REFERENCES process.workflow_thread(id),
    agent_run_id            uuid REFERENCES process.agent_run(id),
    action_type             varchar(100),
    interrupt_type          varchar(100) NOT NULL,
    request_payload         jsonb NOT NULL,
    state_snapshot          jsonb,
    status                  varchar(50) NOT NULL,  -- open | completed
    response_payload        jsonb,
    decision                varchar(100),
    reason                  text,
    actor                   varchar(255),
    requested_at            timestamptz NOT NULL DEFAULT now(),
    responded_at            timestamptz,
    created_at              timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),
    deleted_at              timestamptz
);

CREATE TABLE process.processing_error (
    id                  uuid PRIMARY KEY,
    job_item_id         uuid REFERENCES process.job_item(id),
    agent_run_id        uuid REFERENCES process.agent_run(id),
    purchase_order_line_id uuid REFERENCES purchase_order_line(id),
    error_type          varchar(100) NOT NULL,
    error_code          varchar(100),
    error_message       text,
    node_name           varchar(100),
    raw_error_detail    jsonb,
    occurred_at         timestamptz NOT NULL DEFAULT now(),
    resolved            boolean NOT NULL,
    resolved_at         timestamptz,
    resolved_by         varchar(255),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

-- =============================================================================
-- SCHEMA: cmir -- CMIR/PO-validation-only tables
-- =============================================================================

CREATE TABLE cmir.email_event (
    id                          uuid PRIMARY KEY,
    sender                      text NOT NULL,
    subject                     text,
    raw_content                 text,
    source_message_id           text,
    source_imap_id               varchar(100),
    extracted_json              jsonb,
    missing_fields               jsonb,
    status                      varchar(64),
    queue_status                 varchar(30) NOT NULL,  -- new -> enqueueing -> queued -> processing -> processed/failed
    queue_message_id             varchar(200),
    queue_error                  text,
    queued_at                    timestamptz,
    processing_started_at        timestamptz,
    processed_at                 timestamptz,
    queue_delivery_count         integer NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);

CREATE TABLE cmir.cmir_job_run_context (
    job_run_id                  uuid PRIMARY KEY REFERENCES process.job_run(id),
    source_type                 varchar(50) NOT NULL,
    external_batch_id            varchar(100),
    service_bus_topic            varchar(200),
    service_bus_subscription     varchar(200),
    metadata                     jsonb NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);

CREATE TABLE cmir.cmir_job_item_context (
    job_item_id                  uuid PRIMARY KEY REFERENCES process.job_item(id),
    email_event_id                uuid REFERENCES cmir.email_event(id),
    purchase_order_line_id        uuid REFERENCES purchase_order_line(id),
    metadata                     jsonb NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    CONSTRAINT ck_cmir_job_item_context_one_of
        CHECK (num_nonnulls(email_event_id, purchase_order_line_id) = 1)
);

CREATE TABLE cmir.cmir_record (
    id                                      uuid PRIMARY KEY,
    purchase_order_line_id                  uuid REFERENCES purchase_order_line(id),
    email_event_id                          uuid REFERENCES cmir.email_event(id),
    sender_type                             varchar(100) NOT NULL,
    customer_identity                       varchar(255) NOT NULL,
    material_identity                       varchar(255) NOT NULL,
    intent_phrase                           text,
    existing_cmir_ref                       varchar(255) NOT NULL,
    brand                                   varchar(100) NOT NULL,
    site                                    varchar(100) NOT NULL,
    target_grd_code                         varchar(255) NOT NULL,
    target_customer_material_ref            varchar(255) NOT NULL,
    effective_date                          date,
    reason                                  text,
    customer_identity_key                   varchar(255) NOT NULL,
    target_customer_material_ref_key        varchar(255) NOT NULL,
    is_current                              boolean NOT NULL,
    valid_from                              timestamptz NOT NULL DEFAULT now(),
    valid_to                                timestamptz,
    superseded_by_id                        uuid REFERENCES cmir.cmir_record(id),
    created_at                              timestamptz NOT NULL DEFAULT now(),
    updated_at                              timestamptz NOT NULL DEFAULT now(),
    deleted_at                              timestamptz
);
-- Partial unique index -- SCD2 "exactly one current row" constraint.
CREATE UNIQUE INDEX uq_cmir_record_current_identity ON cmir.cmir_record
    (customer_identity_key, target_customer_material_ref_key) WHERE is_current;

CREATE TABLE cmir.email_action_log (
    id                  uuid PRIMARY KEY,
    email_event_id       uuid NOT NULL REFERENCES cmir.email_event(id),
    action              varchar(100) NOT NULL,
    actor               varchar(100) NOT NULL,
    details             jsonb NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz
);

-- =============================================================================
-- SCHEMA: penalties -- penalties-only tables (full fine(s) -> penalty(ies) rename)
-- =============================================================================

CREATE TABLE penalties.penalty_rule (
    id                          uuid PRIMARY KEY,
    rule_code                   varchar(20) NOT NULL UNIQUE,
    retailer_id                 uuid NOT NULL REFERENCES retailer(id),
    violation_type               varchar(30) NOT NULL,
    threshold_pct                numeric(6,4) NOT NULL,
    calc_type                    varchar(20) NOT NULL,  -- PER_UNIT / PERCENT_OF_PO / FLAT_FEE / TIERED
    rate                         numeric(10,4) NOT NULL,
    cap_amount                   numeric(12,2),
    is_active                   boolean NOT NULL,
    grace_period_days            integer NOT NULL,
    effective_start_date         date NOT NULL,
    effective_end_date           date,
    source_doc_reference         varchar(200),
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);

CREATE TABLE penalties.penalty_rule_tier (
    id                  uuid PRIMARY KEY,
    rule_id             uuid NOT NULL REFERENCES penalties.penalty_rule(id),
    tier_code           varchar(50) NOT NULL,
    band_min            numeric(6,4) NOT NULL,
    band_max            numeric(6,4) NOT NULL,
    rate                numeric(10,4) NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    deleted_at          timestamptz,
    UNIQUE (rule_id, tier_code)
);

CREATE TABLE penalties.penalty_job_run_context (
    job_run_id               uuid PRIMARY KEY REFERENCES process.job_run(id),
    projection_date          date NOT NULL,
    stacking_mode_override    varchar(30),
    metadata                 jsonb NOT NULL,
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    deleted_at               timestamptz
);

CREATE TABLE penalties.penalty_job_item_context (
    job_item_id                  uuid PRIMARY KEY REFERENCES process.job_item(id),
    purchase_order_id            uuid NOT NULL REFERENCES purchase_order(id),
    projection_date              date NOT NULL,
    task_type                    varchar(100) NOT NULL,
    stacking_mode_override        varchar(30),
    force_regenerate_summary     boolean NOT NULL,
    created_at                   timestamptz NOT NULL DEFAULT now(),
    updated_at                   timestamptz NOT NULL DEFAULT now(),
    deleted_at                   timestamptz
);

CREATE TABLE penalties.mitigation_input (
    id                                          uuid PRIMARY KEY,
    purchase_order_id                           uuid NOT NULL UNIQUE REFERENCES purchase_order(id),
    shortage_cause                              varchar(50) NOT NULL,
    shortage_cause_confirmed                    boolean NOT NULL,
    capacity_boost_cost_per_unit                 numeric(10,2),
    capacity_boost_max_units_per_day             numeric(10,2),
    capacity_boost_data_confirmed                boolean NOT NULL,
    express_carrier_cost                        numeric(10,2),
    express_carrier_transit_days                 integer,
    express_carrier_data_confirmed               boolean NOT NULL,
    split_shipment_handling_cost                 numeric(10,2) NOT NULL,
    created_at                                  timestamptz NOT NULL DEFAULT now(),
    updated_at                                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                                  timestamptz
);

CREATE TABLE penalties.mitigation_option (
    id                          uuid PRIMARY KEY,
    purchase_order_id           uuid NOT NULL REFERENCES purchase_order(id),
    projection_date             date NOT NULL,
    action                      varchar(50),  -- ACCEPT / SPEED_UP_PRODUCTION / SPLIT_SHIPMENT / FASTER_CARRIER
    projected_penalty_after      numeric(12,2) NOT NULL,
    action_cost                 numeric(12,2) NOT NULL,
    net_saving                  numeric(12,2) NOT NULL,
    risk_level                  varchar(30) NOT NULL,  -- LOW / MEDIUM / HIGH
    confidence                  varchar(30) NOT NULL,  -- CONFIRMED / ESTIMATED
    rationale                   text,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    UNIQUE (purchase_order_id, projection_date, action)
);

CREATE TABLE penalties.penalty_summary (
    id                          uuid PRIMARY KEY,
    purchase_order_id           uuid NOT NULL REFERENCES purchase_order(id),
    summary_type                 varchar(30) NOT NULL CHECK (summary_type IN ('PROJECTION', 'MITIGATION')),
    as_of_date                  date NOT NULL,
    agent_id                    uuid NOT NULL REFERENCES process.agent(id),
    context_hash                 varchar(64) NOT NULL,
    content_fingerprint           varchar(64),
    source_as_of_date             date,
    status                      varchar(30) NOT NULL,  -- PENDING / READY / FAILED
    model_name                  varchar(100),
    summary                     text,
    error_message                text,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    UNIQUE (purchase_order_id, summary_type, as_of_date)
);

CREATE TABLE penalties.penalty_projection (
    id                          uuid PRIMARY KEY,
    purchase_order_id           uuid NOT NULL REFERENCES purchase_order(id),
    rule_id                     uuid NOT NULL REFERENCES penalties.penalty_rule(id),
    projection_date             date NOT NULL,
    violation_type               varchar(50) NOT NULL,
    failure_probability           numeric(5,4) NOT NULL,
    penalty_amount               numeric(12,2) NOT NULL,
    expected_penalty_amount        numeric(12,2) NOT NULL,
    days_to_delivery             integer NOT NULL,
    projection_status            varchar(30) NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    UNIQUE (purchase_order_id, rule_id, projection_date)
);

CREATE TABLE penalties.actual_penalty (
    id                          uuid PRIMARY KEY,
    actual_penalty_number         varchar(50) NOT NULL UNIQUE,
    purchase_order_id           uuid NOT NULL REFERENCES purchase_order(id),
    violation_type               varchar(50) NOT NULL,
    actual_penalty_amount         numeric(12,2) NOT NULL,
    invoice_or_deduction_date     date NOT NULL,
    dispute_status                varchar(50) NOT NULL,  -- NONE / DISPUTED / WAIVED / UPHELD
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz
);

CREATE TABLE penalties.extracted_penalty_rule_revision (
    id                          uuid PRIMARY KEY,
    extracted_rule_id           uuid NOT NULL REFERENCES penalties.extracted_penalty_rule(id) ON DELETE CASCADE,
    revision_no                 integer NOT NULL,
    instruction                 text NOT NULL,
    requested_by                varchar(255),
    status                      varchar(20) NOT NULL,  -- QUEUED / RUNNING / COMPLETED / FAILED
    agent_reply                 text,
    before_snapshot             jsonb NOT NULL,
    after_snapshot              jsonb,
    error                       text,
    started_at                  timestamptz,
    completed_at                timestamptz,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),
    deleted_at                  timestamptz,
    UNIQUE (extracted_rule_id, revision_no)
);
CREATE INDEX ix_extracted_penalty_rule_revision_extracted_rule_id ON
    penalties.extracted_penalty_rule_revision (extracted_rule_id);

-- =============================================================================
-- SCHEMA: langgraph -- empty, owned entirely by LangGraph's PostgresSaver
-- =============================================================================

-- Created empty by alembic/versions/a5b39c6e2181_langgraph_schema.py.
-- PostgresSaver.setup() creates checkpoints/checkpoint_blobs/
-- checkpoint_writes/checkpoint_migrations here at runtime -- never via
-- Alembic, and this file intentionally does not attempt to describe those
-- tables (they are langgraph-checkpoint-postgres's own schema, not ours).
