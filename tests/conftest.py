"""
Shared fixtures. All API/repository tests run against an in-memory
SQLite database (StaticPool keeps the single in-memory connection alive
across the whole test), never a real Postgres -- fast, no external
service required.

Phase 2 (repositories) of the common/process/cmir/penalties restructure
(see `/home/kaustubhtrivedi/.claude/plans/snoopy-crafting-kazoo.md`)
restores the repository-layer fixtures Phase 1 temporarily stripped, at
their new locations. Phase 3 (services) adds the `penalty_job_item_context`/
`penalty_job_run_context`/`cmir_job_item_context`/`cmir_job_run_context`
repository fixtures those tables were deferred to this phase for.

Phase 7a (API surface) restores the `app`/`client`/`seeded_client`
fixtures, for the `common`/`penalties` domain at least (CMIR/PO-validation
routes are mounted but not exercised through these fixtures -- `service`/
`po_service` are fakes, see `_FakeCmirService`/`_FakePoValidationService`
below, so `create_app` never has to build the real Postgres-backed LangGraph
composition root for a test that only needs `common`/`penalties`).

No composition-root class to stub for the database itself: `app.state.database`
is set directly to the SQLite `database` fixture (the FastAPI app's own
`lifespan` handler, which would otherwise build a Postgres `Database`, never
runs -- `TestClient(app)` used outside a `with` block skips it entirely; see
`test_main_lifespan.py` for the one place that does use a `with` block).
`require_internal_api_key`/`get_llm_client`/`get_job_queue` are overridden
via `dependency_overrides` instead.
"""

import os

# APP_INTERNAL_API_KEY is a required setting (see app/core/config/app.py)
# with no default -- Settings() raises without it. setdefault() so a real
# value in the environment/.env wins, but the suite never depends on one
# existing: this must run before anything below imports app.* (app.api.
# dependencies calls get_settings() at import time). Exposed as a constant so
# tests/unit/api/test_internal_api_key.py -- which exercises the real
# dependency instead of the override below -- can assert against it.
TEST_INTERNAL_API_KEY = "test-internal-api-key-do-not-use-in-prod-000000000000000000000000"
os.environ.setdefault("APP_INTERNAL_API_KEY", TEST_INTERNAL_API_KEY)

from types import SimpleNamespace

import pytest
from sqlalchemy.pool import StaticPool

from app.db.session import Database
from app.repositories.cmir.action_log import ActionLogRepository
from app.repositories.cmir.cmir_record import CmirRecordRepository
from app.repositories.cmir.email import EmailRepository
from app.repositories.cmir.job_context import CmirJobItemContextRepository, CmirJobRunContextRepository
from app.repositories.common.delivery_change_request import PoDeliveryChangeRequestRepository
from app.repositories.common.fulfillment import FulfillmentRepository
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.dispute import PenaltyDisputeRepository
from app.repositories.penalties.job_context import (
    PenaltyJobItemContextRepository,
    PenaltyJobRunContextRepository,
)
from app.repositories.penalties.mitigation import MitigationInputRepository, MitigationOptionRepository
from app.repositories.penalties.projection import ActualPenaltyRepository, PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.agent_registry import (
    AgentRegistryRepository,
    AgentRunRepository,
    AgentTraceRepository,
)
from app.repositories.process.job_queue import JobQueueRepository
from app.repositories.process.workflow import (
    HumanActionRepository,
    ProcessingErrorRepository,
    WorkflowThreadRepository,
)


@pytest.fixture
def database() -> Database:
    db = Database(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    db.create_all_tables()
    return db


@pytest.fixture
def db_session(database: Database):
    with database.session() as session:
        yield session


@pytest.fixture
def repos(db_session):
    """A lightweight bundle of repositories sharing one Session, for tests
    that exercise the repository layer directly rather than through a
    service or HTTP. Just test plumbing (SimpleNamespace) -- production
    code wires these per-request via app/api/dependencies.py, not through
    a bundle. Unlike the pre-restructure `services` fixture this replaces,
    this bundles repositories only -- no service-layer objects (those are
    Phase 3, and still keyed on deleted models today)."""
    return SimpleNamespace(
        master_data=MasterDataRepository(db_session),
        contracts=RetailerAgreementRepository(db_session),
        purchase_orders=PurchaseOrderRepository(db_session),
        fulfillment=FulfillmentRepository(db_session),
        penalty_rules=PenaltyRuleRepository(db_session),
        penalty_projections=PenaltyProjectionRepository(db_session),
        actual_penalties=ActualPenaltyRepository(db_session),
        disputes=PenaltyDisputeRepository(db_session),
        penalty_summaries=PenaltySummaryRepository(db_session),
        mitigation_inputs=MitigationInputRepository(db_session),
        mitigation_options=MitigationOptionRepository(db_session),
        delivery_change_requests=PoDeliveryChangeRequestRepository(db_session),
        job_queue=JobQueueRepository(db_session),
        agent_registry=AgentRegistryRepository(db_session),
        agent_runs=AgentRunRepository(db_session),
        agent_traces=AgentTraceRepository(db_session),
        workflow_threads=WorkflowThreadRepository(db_session),
        human_actions=HumanActionRepository(db_session),
        processing_errors=ProcessingErrorRepository(db_session),
        emails=EmailRepository(db_session),
        action_log=ActionLogRepository(db_session),
        cmir_records=CmirRecordRepository(db_session),
        penalty_job_item_context=PenaltyJobItemContextRepository(db_session),
        penalty_job_run_context=PenaltyJobRunContextRepository(db_session),
        cmir_job_item_context=CmirJobItemContextRepository(db_session),
        cmir_job_run_context=CmirJobRunContextRepository(db_session),
    )


class _UnconfiguredFakeChatClient:
    """Default fake LLM client for tests.

    Any test that actually reaches an LLM-calling code path must provide
    its own richer fake through dependency_overrides[get_llm_client].

    Kept verbatim (unused) during the Phase 1/2 conftest reduction above --
    it is the guard against live LLM calls in tests and is wired back into
    the `app` fixture's dependency_overrides once that fixture returns.
    """

    model_name = "fake-model"

    def invoke(self, messages, *, tools=None):
        raise AssertionError(
            "The default test LLM client was invoked. "
            "This test reaches an LLM-calling path and must provide "
            "its own dependency_overrides[get_llm_client]."
        )


class _NoOpJobQueue:
    """Minimal JobDispatcher+JobSource double for the `app`/`client`
    fixtures below -- no lifespan runs (no `with TestClient(app):` block),
    so nothing here needs to actually deliver anything; only `dispatch` is
    ever called by a route in this test configuration (POST /job-runs)."""

    def dispatch(self, job_item_id, *, delay_seconds: int = 0) -> None:
        pass

    def close(self) -> None:
        pass

    def claim_batch(self, worker_id: str, limit: int) -> list:
        return []

    def heartbeat(self, job, worker_id: str) -> bool:
        return True

    def ack(self, job, worker_id: str) -> None:
        pass

    def nack(self, job, worker_id: str, *, error: str, error_code: str, retry_in_seconds: int) -> None:
        pass

    def dead_letter(self, job, worker_id: str, *, error: str, error_code: str) -> None:
        pass

    def release(self, job, worker_id: str) -> None:
        pass

    def reclaim_stale(self, visibility_timeout_seconds: int) -> int:
        return 0


class _FakeCmirService:
    """Stand-in passed to `create_app(service=...)` so lifespan's `if
    app.state.service is None: build_service()` branch is never reached
    (these fixtures never run lifespan at all -- no `with TestClient(app):`
    block -- but `create_app` still needs *some* value for `app.state.service`
    up front). `build_service()` -> `Container.build()` opens a real
    Postgres-backed LangGraph checkpointer, which no test using these
    fixtures should ever need.

    Default value of the `cmir_service` fixture below -- a test module
    that needs the `client`/`app` fixtures to actually exercise cmir routes
    overrides `cmir_service` (same fixture name) with a richer fake; see
    `tests/unit/api/test_cmir_api.py`/`test_workflow_threads_api.py`."""


class _FakePoValidationService:
    """Same purpose as `_FakeCmirService`, for `app.state.po_service` /
    the `po_validation_service` fixture below."""


@pytest.fixture
def cmir_service() -> object:
    """Default fake for `app.state.service` -- override this fixture (same
    name) in a test module to supply a fake implementing the
    `CmirService` methods your test's routes actually call."""
    return _FakeCmirService()


@pytest.fixture
def po_validation_service() -> object:
    """Default fake for `app.state.po_service` -- override this fixture
    (same name) in a test module to supply a fake implementing the
    `PoValidationService` methods your test's routes actually call."""
    return _FakePoValidationService()


@pytest.fixture
def app(database: Database, cmir_service: object, po_validation_service: object):
    """A real `create_app()` FastAPI app, wired to the SQLite `database`
    fixture instead of a Postgres-backed lifespan.

    No `with` block is used, so lifespan never runs (see
    `test_main_lifespan.py`, which uses a real `with` block precisely to
    exercise it) -- `app.state.database` is set directly instead, `
    require_internal_api_key`/`get_llm_client`/`get_job_queue` are
    overridden, and `service`/`po_service` come from the
    `cmir_service`/`po_validation_service` fixtures (trivial stand-ins
    by default, so `create_app` never has to build the CMIR/PO-validation
    composition root -- a real Postgres LangGraph checkpointer -- for tests
    that only exercise `common`/`penalties` routes; overridable per test
    module for cmir/po_validation/workflow-threads route coverage).
    """
    from app.api.dependencies import get_job_queue, get_llm_client, require_internal_api_key
    from app.main import create_app

    test_app = create_app(service=cmir_service, po_service=po_validation_service)
    test_app.state.database = database

    test_app.dependency_overrides[require_internal_api_key] = lambda: None
    test_app.dependency_overrides[get_llm_client] = lambda: _UnconfiguredFakeChatClient()
    test_app.dependency_overrides[get_job_queue] = lambda: (_NoOpJobQueue(), _NoOpJobQueue())

    return test_app


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


@pytest.fixture
def seeded_client(client):
    """A `client` with the four worked-example master data/orders/rules
    already seeded and their day-by-day scenario replayed -- see
    `PenaltySeedingService.seed_master_data`/`simulate_daily_run`."""
    resp = client.post("/api/v1/admin/seed-master-data")
    assert resp.status_code == 200, resp.text
    resp = client.post("/api/v1/admin/simulate-daily-run")
    assert resp.status_code == 200, resp.text
    return client
