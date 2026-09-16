"""Tests for app/workers/dispatch.py -- the single per-item execution
entry point.

Runs against the real (SQLite, in-memory) `database` fixture with real
ProjectionService/ProjectionSummaryService/MitigationSummaryService, since
`execute_job` constructs them directly from a fresh session -- only the LLM
call is faked (`FakeChatClient`, the same duck-typed shape
`tests/unit/services/test_projection_summary_service.py`'s fake uses). No
live API call anywhere here.

Seeds via `database.session()` directly (auto-committing) rather than the
`repos`/`db_session` fixtures, which hold one long-lived, uncommitted
session open for the whole test -- `execute_job` opens and closes its own
sessions, and on the single shared SQLite connection the `database` fixture
uses, mixing an open uncommitted session with `execute_job`'s own sessions
would make inserts invisible across sessions.

Was written against the pre-restructure `ClaimedJob` (`order_id`/
`projection_date`/`task_type`/`stacking_mode_override`/
`force_regenerate_summary` carried directly on the dataclass) -- rewritten
against the domain-agnostic `ClaimedJob` (`item_type`/`dedupe_key` only)
plus the matching `penalties.penalty_job_item_context` row each per-item
function now looks up itself (see `app.queue.types` and
`app.workers.penalty_projection`'s module docstrings).
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date
from uuid import UUID, uuid4

import pytest

from app.core.config import Settings
from app.core.exceptions import BusinessRuleError, NotFoundError
from app.db.session import Database
from app.models.enums import SummaryType
from app.queue.types import ClaimedJob
from app.repositories.common.master_data import MasterDataRepository
from app.repositories.common.purchase_order import PurchaseOrderRepository
from app.repositories.common.retailer_agreement import RetailerAgreementRepository
from app.repositories.penalties.job_context import PenaltyJobItemContextRepository
from app.repositories.penalties.mitigation import MitigationOptionRepository
from app.repositories.penalties.projection import PenaltyProjectionRepository
from app.repositories.penalties.rule import PenaltyRuleRepository
from app.repositories.penalties.summary import PenaltySummaryRepository
from app.repositories.process.job_queue import JobQueueRepository
from app.services.penalties.mitigation.types import MitigationOption
from app.workers.dispatch import execute_job


class _FakeAIMessage:
    def __init__(self, content: str | None, tool_calls: list[dict]) -> None:
        self.content = content
        self.tool_calls = tool_calls


class FakeChatClient:
    """Always answers immediately with no tool calls. `model_name`/
    `invoke(messages, *, tools=None)` is the only shape
    `ProjectionSummaryService`/`MitigationSummaryService` need -- no live
    Azure OpenAI call."""

    model_name = "fake-model"

    def __init__(self, final_content: str | None = "All clear.") -> None:
        self.final_content = final_content
        self.invocations: list[dict] = []

    def invoke(self, messages, *, tools=None):
        self.invocations.append({"messages": messages, "tools": tools})
        return _FakeAIMessage(content=self.final_content, tool_calls=[])


def _seed_purchase_order(database: Database, po_number: str, *, with_rules: bool = True) -> UUID:
    with database.session() as session:
        master_data = MasterDataRepository(session)
        rules = PenaltyRuleRepository(session)
        purchase_orders = PurchaseOrderRepository(session)

        retailer = master_data.add_retailer(f"RET-{po_number}", f"Retailer {po_number}", None, "SUM")
        material = master_data.add_material(f"MAT-{po_number}", None)
        plant = master_data.add_plant(f"PLANT-{po_number}", None, None)
        purchase_order = purchase_orders.create_purchase_order(
            purchase_order_number=po_number,
            retailer_id=retailer["id"],
            order_date=date(2026, 8, 1),
            requested_delivery_date=date(2026, 8, 10),
            required_ship_date=date(2026, 8, 8),
        )
        purchase_orders.add_line(
            purchase_order_id=purchase_order["id"],
            line_number="10",
            ordered_quantity=100,
            unit_price=5.0,
            material_id=material["id"],
            plant_id=plant["id"],
        )
        if with_rules:
            retailer_agreement = RetailerAgreementRepository(session).add_retailer_agreement(
                retailer_id=retailer["id"],
                contract_code=f"TEST-{po_number}",
                title="Test retailer agreement",
                document_sha256="0" * 64,
            )
            rules.add_rule(
                rule_code=f"RULE-{po_number}-FLAT",
                retailer_id=retailer["id"],
                violation_type="OTIF_LATE",
                penalty_category="OTIF_LATE",
                retailer_agreement_id=retailer_agreement["id"],
                calc_type="FLAT_FEE",
                rate=50.0,
            )
        return purchase_order["id"]


def _make_job_with_context(
    database: Database,
    item_type: str,
    purchase_order_id: UUID,
    projection_date: date,
    *,
    stacking_mode_override: str | None = None,
    force_regenerate_summary: bool = False,
    metadata: dict | None = None,
) -> ClaimedJob:
    with database.session() as session:
        job_queue = JobQueueRepository(session)
        job_context = PenaltyJobItemContextRepository(session)
        run = job_queue.create_run(job_type=item_type, trigger_type="ON_DEMAND")
        dedupe_key = f"{purchase_order_id}:{projection_date.isoformat()}:{item_type}"
        item = job_queue.enqueue(
            run["id"], item_type=item_type, dedupe_key=dedupe_key, max_attempts=5, metadata=metadata
        )
        assert item is not None
        job_context.create(
            job_item_id=item["id"],
            purchase_order_id=purchase_order_id,
            projection_date=projection_date,
            task_type=item_type,
            stacking_mode_override=stacking_mode_override,
            force_regenerate_summary=force_regenerate_summary,
        )
        job_item_id = item["id"]
        job_run_id = run["id"]

    return ClaimedJob(
        job_item_id=job_item_id,
        job_run_id=job_run_id,
        item_type=item_type,
        dedupe_key=dedupe_key,
        attempt_count=1,
        max_attempts=5,
    )


def _make_bare_job(item_type: str) -> ClaimedJob:
    """A job with no matching context row -- for the unknown-item-type
    case, which `execute_job` rejects before ever looking one up."""
    return ClaimedJob(
        job_item_id=uuid4(),
        job_run_id=uuid4(),
        item_type=item_type,
        dedupe_key=None,
        attempt_count=1,
        max_attempts=5,
    )


def test_order_run_runs_projection_then_schedules_and_generates_summary(database):
    purchase_order_id = _seed_purchase_order(database, "PO-A")
    llm = FakeChatClient()
    job = _make_job_with_context(database, "ORDER_RUN", purchase_order_id, date(2026, 8, 5))

    execute_job(job, database, Settings(), llm, heartbeat=None)

    with database.session() as session:
        history = PenaltyProjectionRepository(session).list_history(purchase_order_id)
        summary_row = PenaltySummaryRepository(session).get_by_key(
            purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 5)
        )

    assert history, "projection should have been persisted"
    assert summary_row is not None
    assert summary_row["status"] == "READY"
    assert summary_row["summary"] == "All clear."
    assert len(llm.invocations) >= 1


def test_summary_regen_only_generates_summary_no_new_projection(database):
    purchase_order_id = _seed_purchase_order(database, "PO-B")
    llm = FakeChatClient()

    # ORDER_RUN first so a projection exists (PROJECTION_SUMMARY_REGEN's precondition).
    execute_job(
        _make_job_with_context(database, "ORDER_RUN", purchase_order_id, date(2026, 8, 5)),
        database,
        Settings(),
        llm,
        heartbeat=None,
    )

    with database.session() as session:
        history_before = PenaltyProjectionRepository(session).list_history(purchase_order_id)

    # Force regeneration on the same date; PROJECTION_SUMMARY_REGEN must not touch
    # the projection history.
    regen_job = _make_job_with_context(
        database,
        "PROJECTION_SUMMARY_REGEN",
        purchase_order_id,
        date(2026, 8, 5),
        force_regenerate_summary=True,
    )
    execute_job(regen_job, database, Settings(), llm, heartbeat=None)

    with database.session() as session:
        history_after = PenaltyProjectionRepository(session).list_history(purchase_order_id)
        summary_row = PenaltySummaryRepository(session).get_by_key(
            purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 5)
        )

    assert history_after == history_before
    assert summary_row is not None
    assert summary_row["status"] == "READY"
    # One LLM round-trip pair (round + final) per generation -- forced
    # regeneration means a second generation actually ran.
    assert len(llm.invocations) == 4


def test_summary_regen_without_a_projection_raises_no_projection_exists(database):
    purchase_order_id = _seed_purchase_order(database, "PO-NOPROJ")
    llm = FakeChatClient()
    job = _make_job_with_context(database, "PROJECTION_SUMMARY_REGEN", purchase_order_id, date(2026, 8, 5))

    with pytest.raises(BusinessRuleError) as exc_info:
        execute_job(job, database, Settings(), llm, heartbeat=None)
    assert exc_info.value.code == "NO_PROJECTION_EXISTS"


def test_order_run_propagates_purchase_order_not_found(database):
    llm = FakeChatClient()
    job = _make_job_with_context(database, "ORDER_RUN", uuid4(), date(2026, 8, 5))

    with pytest.raises(NotFoundError) as exc_info:
        execute_job(job, database, Settings(), llm, heartbeat=None)
    assert exc_info.value.code == "PO_NOT_FOUND"


def test_order_run_propagates_no_active_rules(database):
    purchase_order_id = _seed_purchase_order(database, "PO-NORULES", with_rules=False)
    llm = FakeChatClient()
    job = _make_job_with_context(database, "ORDER_RUN", purchase_order_id, date(2026, 8, 5))

    with pytest.raises(BusinessRuleError) as exc_info:
        execute_job(job, database, Settings(), llm, heartbeat=None)
    assert exc_info.value.code == "NO_ACTIVE_RULES"


def test_unknown_task_type_raises_value_error(database):
    llm = FakeChatClient()
    job = _make_bare_job("BOGUS_TASK_TYPE")

    with pytest.raises(ValueError, match="Unknown item_type"):
        execute_job(job, database, Settings(), llm, heartbeat=None)


def test_heartbeat_invoked_once_per_tool_round(database):
    """FakeChatClient never returns tool_calls, so the loop breaks after
    round 1 -- exactly one heartbeat call per generation, times two
    generations (projection summary's own get_or_schedule/run_generation
    pair only runs one generation for ORDER_RUN)."""
    purchase_order_id = _seed_purchase_order(database, "PO-D")
    llm = FakeChatClient()
    job = _make_job_with_context(database, "ORDER_RUN", purchase_order_id, date(2026, 8, 5))
    heartbeat_calls = []

    execute_job(job, database, Settings(), llm, heartbeat=lambda: heartbeat_calls.append(1))

    assert len(heartbeat_calls) == 1


def test_session_is_not_held_across_the_llm_call(database):
    """Regression guard for the documented session-lifetime contract:
    the projection phase and the summary phase must each get their own
    session, never one held open across `llm.invoke`."""
    purchase_order_id = _seed_purchase_order(database, "PO-E")
    # Strong references to the actual Session objects, not `id(session)` --
    # once a session's `with` block exits it can be garbage-collected, and
    # CPython is free to hand a *new* object the exact same id(), which
    # would make an id()-based comparison here flaky (occasionally "equal"
    # by pure memory-address reuse, not by the two sessions actually being
    # the same object).
    seen_sessions: list[object] = []
    real_session_cm = database.session

    @contextmanager
    def _tracking_session():
        with real_session_cm() as session:
            seen_sessions.append(session)
            yield session

    job = _make_job_with_context(database, "ORDER_RUN", purchase_order_id, date(2026, 8, 5))

    database.session = _tracking_session  # type: ignore[method-assign]
    try:
        llm = FakeChatClient()
        execute_job(job, database, Settings(), llm, heartbeat=None)
    finally:
        database.session = real_session_cm  # type: ignore[method-assign]

    # One session for the projection phase, one fresh session for the
    # summary phase -- never the same session object reused across both.
    assert len(seen_sessions) == 2
    assert seen_sessions[0] is not seen_sessions[1]


def _seed_mitigation_options(database: Database, purchase_order_id: UUID, projection_date: date) -> None:
    with database.session() as session:
        MitigationOptionRepository(session).save_results(
            purchase_order_id,
            projection_date,
            [
                MitigationOption(
                    action="ACCEPT",
                    projected_penalty_after=100.0,
                    action_cost=0.0,
                    net_saving=0.0,
                    risk_level="HIGH",
                    confidence="CONFIRMED",
                    rationale="Pay the projected penalty as-is.",
                )
            ],
        )
        session.commit()


def test_mitigation_run_computes_and_persists_options(database):
    """MITIGATION_RUN (the PENALTY_MITIGATION_BATCH per-item worker path) --
    computes and persists mitigation options via the same
    `MitigationService.run_for_purchase_order` compute path
    `POST /penalties/mitigations` uses synchronously, no LLM
    call involved."""
    purchase_order_id = _seed_purchase_order(database, "PO-MIT-RUN")
    llm = FakeChatClient()
    # ORDER_RUN first so a projection exists (MITIGATION_RUN's precondition,
    # same as MitigationService.run_for_purchase_order's own check).
    execute_job(
        _make_job_with_context(database, "ORDER_RUN", purchase_order_id, date(2026, 8, 5)),
        database,
        Settings(),
        llm,
        heartbeat=None,
    )
    llm_invocations_after_order_run = len(llm.invocations)

    job = _make_job_with_context(database, "MITIGATION_RUN", purchase_order_id, date(2026, 8, 5))
    execute_job(job, database, Settings(), llm, heartbeat=None)

    with database.session() as session:
        options = MitigationOptionRepository(session).list_for_date(purchase_order_id, date(2026, 8, 5))

    assert options, "mitigation options should have been persisted"
    # No LLM round-trip for MITIGATION_RUN itself -- only ORDER_RUN's own
    # summary generation invoked the fake client.
    assert len(llm.invocations) == llm_invocations_after_order_run


def test_mitigation_run_without_a_projection_raises_no_projection_exists(database):
    purchase_order_id = _seed_purchase_order(database, "PO-MIT-RUN-NOPROJ")
    llm = FakeChatClient()
    job = _make_job_with_context(database, "MITIGATION_RUN", purchase_order_id, date(2026, 8, 5))

    with pytest.raises(BusinessRuleError) as exc_info:
        execute_job(job, database, Settings(), llm, heartbeat=None)
    assert exc_info.value.code == "NO_PROJECTION_EXISTS"


def test_mitigation_run_propagates_purchase_order_not_found(database):
    llm = FakeChatClient()
    job = _make_job_with_context(database, "MITIGATION_RUN", uuid4(), date(2026, 8, 5))

    with pytest.raises(NotFoundError) as exc_info:
        execute_job(job, database, Settings(), llm, heartbeat=None)
    assert exc_info.value.code == "PO_NOT_FOUND"


def test_mitigation_summary_regen_generates_summary(database):
    purchase_order_id = _seed_purchase_order(database, "PO-MIT-WORKER")
    _seed_mitigation_options(database, purchase_order_id, date(2026, 8, 5))
    llm = FakeChatClient()
    job = _make_job_with_context(database, "MITIGATION_SUMMARY_REGEN", purchase_order_id, date(2026, 8, 5))

    execute_job(job, database, Settings(), llm, heartbeat=None)

    with database.session() as session:
        summary_row = PenaltySummaryRepository(session).get_by_key(
            purchase_order_id, SummaryType.MITIGATION, date(2026, 8, 5)
        )

    assert summary_row is not None
    assert summary_row["status"] == "READY"
    assert summary_row["summary"] == "All clear."


def test_mitigation_summary_regen_without_options_raises_no_mitigation_options_exist(database):
    purchase_order_id = _seed_purchase_order(database, "PO-MIT-NOOPT")
    llm = FakeChatClient()
    job = _make_job_with_context(database, "MITIGATION_SUMMARY_REGEN", purchase_order_id, date(2026, 8, 5))

    with pytest.raises(BusinessRuleError) as exc_info:
        execute_job(job, database, Settings(), llm, heartbeat=None)
    assert exc_info.value.code == "NO_MITIGATION_OPTIONS_EXIST"


# ---------------------------------------------------------------------
# PENALTY_FULL_RUN (job_type=PENALTY_FULL_RUN_BATCH's per-item worker path)
# ---------------------------------------------------------------------


def test_penalty_full_run_executes_all_four_steps_in_order(database):
    """The new dispatch-table entry: `execute_job` routes `PENALTY_FULL_RUN`
    to `app.workers.penalty_full_run.run_full_run`, which reads `steps` off
    `process.job_item.metadata` and runs all four steps -- projection,
    projection summary, mitigation, and mitigation summary -- against one
    claimed job item, regardless of the order `steps` lists them in."""
    purchase_order_id = _seed_purchase_order(database, "PO-FULL-RUN")
    llm = FakeChatClient()
    job = _make_job_with_context(
        database,
        "PENALTY_FULL_RUN",
        purchase_order_id,
        date(2026, 8, 5),
        metadata={"steps": ["mitigation_summary", "mitigation", "projection_summary", "projection"]},
    )

    execute_job(job, database, Settings(), llm, heartbeat=None)

    with database.session() as session:
        history = PenaltyProjectionRepository(session).list_history(purchase_order_id)
        projection_summary = PenaltySummaryRepository(session).get_by_key(
            purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 5)
        )
        options = MitigationOptionRepository(session).list_for_date(purchase_order_id, date(2026, 8, 5))
        mitigation_summary = PenaltySummaryRepository(session).get_by_key(
            purchase_order_id, SummaryType.MITIGATION, date(2026, 8, 5)
        )

    assert history, "projection step should have persisted a projection"
    assert projection_summary is not None and projection_summary["status"] == "READY"
    assert options, "mitigation step should have persisted mitigation options"
    assert mitigation_summary is not None and mitigation_summary["status"] == "READY"


def test_penalty_full_run_runs_only_the_requested_steps(database):
    """Only `projection` requested -- no summary, no mitigation options,
    no mitigation summary should be produced."""
    purchase_order_id = _seed_purchase_order(database, "PO-FULL-RUN-PARTIAL")
    llm = FakeChatClient()
    job = _make_job_with_context(
        database,
        "PENALTY_FULL_RUN",
        purchase_order_id,
        date(2026, 8, 5),
        metadata={"steps": ["projection"]},
    )

    execute_job(job, database, Settings(), llm, heartbeat=None)

    with database.session() as session:
        history = PenaltyProjectionRepository(session).list_history(purchase_order_id)
        projection_summary = PenaltySummaryRepository(session).get_by_key(
            purchase_order_id, SummaryType.PROJECTION, date(2026, 8, 5)
        )
        options = MitigationOptionRepository(session).list_for_date(purchase_order_id, date(2026, 8, 5))

    assert history, "projection step should have persisted a projection"
    assert projection_summary is None
    assert options == []
    assert llm.invocations == []


def test_penalty_full_run_with_no_steps_in_metadata_raises_value_error(database):
    purchase_order_id = _seed_purchase_order(database, "PO-FULL-RUN-NOSTEPS")
    llm = FakeChatClient()
    job = _make_job_with_context(
        database, "PENALTY_FULL_RUN", purchase_order_id, date(2026, 8, 5), metadata={}
    )

    with pytest.raises(ValueError, match="steps"):
        execute_job(job, database, Settings(), llm, heartbeat=None)
